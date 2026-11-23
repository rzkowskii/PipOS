"""Build signed model-pack media from independently pinned files in a fresh staging directory.

    python3 -m lab.package_model --source out/models --output DIR --certificate C --key K
        stage every role (conversation, dream, vad, asr, tts, embeddings,
        speaker) for the models partition; --bench adds /models/bench
    python3 -m lab.package_model describe --role tts
        recompute a role descriptor's file list from its pinned sources

Sources are out/models and out/voice (the folder above --source). Every
source file must match both its role descriptor
(os/package/being/model-packs/<role>/pack.json) and the download record in
DOWNLOADS.json. Archives (.tar.bz2) are read member by member: only the
regular files a descriptor names are written, at the paths it names, so no
archive path is ever trusted. Nothing from an archive is run.
"""

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
from pathlib import Path

from mind.model_pack import (
    ENVELOPE, FORMAT_V2, IMAGE_ROLES, MAX_DESCRIPTOR, ROLE_DIRS, WEIGHTS, _relative, check_descriptor,
    check_weights, envelope_name, metadata_bytes, openssl, root, verify_envelope,
)


REPO = Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO / "os/package/being/default-model"
PACKS = REPO / "os/package/being/model-packs"
CHUNK = 1 << 20


def sign(info, certificate, key):
    return openssl("cms", "-sign", "-binary", "-nodetach", "-nocerts", "-noattr", "-md", "sha256",
                   "-outform", "DER", "-signer", str(certificate), "-inkey", str(key), data=info)


def empty_output(output):
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError("model staging directory must be empty")
    output.mkdir(parents=True, exist_ok=True)


def stage(source, output, descriptor, certificate, key, *, fresh=True):
    """The conversation pack (format v1): /models/reflex.gguf and its envelope."""
    if fresh:
        empty_output(output)
    info = metadata_bytes(descriptor)
    pack = json.loads(info)
    if hashlib.sha256(metadata_bytes(descriptor.with_name("LICENSE"))).hexdigest() != pack["license_sha256"]:
        raise ValueError("pack licence text differs from its qualified SHA-256")
    check_weights(source, pack)
    shutil.copyfile(source / WEIGHTS, output / WEIGHTS)
    (output / ENVELOPE).write_bytes(sign(info, certificate, key))
    # Keep working readers of historical inventory metadata on the same model.
    # Runtime trust comes from the authenticated CMS, independently of this file.
    legacy = metadata_bytes(source / "manifest.json")
    inventory = json.loads(legacy)["reflex"]
    if any(inventory[field] != pack[field] for field in ("file", "bytes", "sha256")):
        raise ValueError("staged lab model inventory conflicts with the qualified default pack")
    (output / "manifest.json").write_bytes(legacy)
    verify_envelope(output, output / ENVELOPE, certificate, descriptor)


# --- format v2 ------------------------------------------------------------------

def download_record(out_root, relative):
    """The DOWNLOADS.json entry for out/<top>/<name>, or None if there is no record file."""
    top, _, name = relative.partition("/")
    record = Path(out_root) / top / "DOWNLOADS.json"
    if not record.is_file():
        return None
    for entry in json.loads(record.read_text(encoding="utf-8")).get("files", []):
        if entry.get("name") == name:
            return entry
    raise ValueError(f"{relative} is not in {top}/DOWNLOADS.json")


def source_path(out_root, source):
    relative = _relative(source["file"])
    if relative.split("/", 1)[0] not in ("models", "voice"):
        raise ValueError(f"model pack sources come from out/models or out/voice: {relative}")
    return Path(out_root) / relative


def check_source(out_root, source):
    """Size and SHA-256 of a source file against its descriptor and its download record."""
    path = source_path(out_root, source)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing model source {source['file']}")
    if path.stat().st_size != source["bytes"]:
        raise ValueError(f"model source size differs: {source['file']}")
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    if digest.hexdigest() != source["sha256"]:
        raise ValueError(f"model source SHA-256 differs: {source['file']}")
    record = download_record(out_root, source["file"])
    if record is not None and (record.get("sha256") != source["sha256"] or record.get("size_bytes") != source["bytes"]):
        raise ValueError(f"model source differs from its download record: {source['file']}")
    return path


def _copy(stream, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with open(destination, "xb") as out:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
            size += len(block)
            out.write(block)
    return size, digest.hexdigest()


def _hash(stream):
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: stream.read(CHUNK), b""):
        digest.update(block)
        size += len(block)
    return size, digest.hexdigest()


def archive_members(path, rules):
    """(member, relative path) for each regular file the rules select.

    A rule is {"member": NAME, "path": PATH} for one file, or
    {"prefix": DIR/, "path": DIR/} for every regular file below DIR/.
    Directories are skipped; any other kind of entry under a prefix, a
    member the archive lacks, or one named twice is refused.
    """
    wanted, prefixes, found = {}, [], set()
    for rule in rules:
        if "member" in rule:
            wanted[rule["member"]] = _relative(rule["path"])
        else:
            prefix, target = rule["prefix"], rule["path"]
            if not prefix.endswith("/") or not target.endswith("/"):
                raise ValueError("an archive prefix and its path end with /")
            _relative(prefix[:-1])
            _relative(target[:-1])
            prefixes.append((prefix, target))
    with tarfile.open(path, "r:bz2") as archive:
        for member in archive:
            name = member.name
            if name in wanted:
                if not member.isreg() or name in found:
                    raise ValueError(f"{name} is not one regular file in {path.name}")
                found.add(name)
                yield archive, member, wanted[name]
                continue
            for prefix, target in prefixes:
                if name.startswith(prefix) and len(name) > len(prefix):
                    if member.isdir():
                        break
                    if not member.isreg():
                        raise ValueError(f"{name} in {path.name} is not a regular file")
                    yield archive, member, _relative(target + name[len(prefix):])
                    break
    missing = set(wanted) - found
    if missing:
        raise ValueError(f"{path.name} lacks {', '.join(sorted(missing))}")


def produce(out_root, pack, destination=None):
    """Read every source of a v2 descriptor. With `destination`, write the
    pack's files there. Returns {path: (bytes, sha256)}."""
    produced = {}

    def add(relative, size_digest):
        if relative in produced:
            raise ValueError(f"two sources write {relative}")
        produced[relative] = size_digest

    for source in pack["sources"]:
        path = check_source(out_root, source)
        if "extract" in source:
            for archive, member, relative in archive_members(path, source["extract"]):
                stream = archive.extractfile(member)
                add(relative, _copy(stream, destination / relative) if destination else _hash(stream))
        else:
            relative = _relative(source["path"])
            with open(path, "rb") as stream:
                add(relative, _copy(stream, destination / relative) if destination else _hash(stream))
    return produced


def render_descriptor(pack):
    """Stable JSON: one line per file, so a change reads as a small diff."""
    head = {key: value for key, value in pack.items() if key != "files"}
    lines = json.dumps(head, indent=2, sort_keys=False)[:-2]
    files = ",\n".join("    " + json.dumps(entry, sort_keys=True) for entry in pack["files"])
    return (lines + ',\n  "files": [\n' + files + "\n  ]\n}\n").encode()


def describe(role, out_root, packs=PACKS):
    """Fill a role descriptor's file list from its sources and write it back."""
    path = packs / role / "pack.json"
    pack = json.loads(path.read_text(encoding="utf-8"))
    if pack.get("format") != FORMAT_V2 or pack.get("role") != role or pack.get("dir") != ROLE_DIRS[role]:
        raise ValueError(f"{path} is not a v2 descriptor for {role}")
    produced = produce(out_root, pack)
    pack["files"] = [{"path": name, "bytes": size, "sha256": digest}
                     for name, (size, digest) in sorted(produced.items())]
    data = render_descriptor(pack)
    if len(data) > MAX_DESCRIPTOR:
        raise ValueError(f"{role} descriptor is larger than {MAX_DESCRIPTOR} bytes")
    check_descriptor(json.loads(data), role)
    path.write_bytes(data)
    return pack


def stage_role(role, out_root, output, descriptor, certificate, key, *, signed=None):
    """One v2 role into the staged models tree `output`.

    `signed` holds the descriptor bytes to sign when they are generated here
    (bench); otherwise they are the shipped descriptor file's bytes.
    """
    info = signed if signed is not None else metadata_bytes(descriptor, MAX_DESCRIPTOR)
    pack = check_descriptor(json.loads(info), role)
    base = root(role, output)
    base.mkdir(parents=True, exist_ok=True)
    if signed is None:
        produced = produce(out_root, pack, base)
        listed = {entry["path"]: (entry["bytes"], entry["sha256"]) for entry in pack["files"]}
        if produced != listed:
            wrong = sorted(set(produced.items()) ^ set(listed.items()))
            raise ValueError(f"{role} sources do not give the files its descriptor lists: "
                             f"{', '.join(name for name, _ in wrong[:5])}")
    (base / envelope_name(role)).write_bytes(sign(info, certificate, key))
    verify_envelope(output, base / envelope_name(role), certificate, descriptor if signed is None else None, role)
    return pack


def bench_descriptor(bench_dir):
    """A bench pack described from out/models/bench/manifest.json (lab/quant_candidates.py)."""
    manifest = Path(bench_dir) / "manifest.json"
    entries = json.loads(metadata_bytes(manifest)).get("models", [])
    files = []
    for entry in entries:
        name = _relative(entry["file"])
        if "/" in name or not name.endswith(".gguf"):
            raise ValueError(f"bench models are .gguf files beside the manifest: {name}")
        path = Path(bench_dir) / name
        with open(path, "rb") as stream:
            size, digest = _hash(stream)
        if entry.get("sha256") not in (None, digest) or entry.get("bytes") not in (None, size):
            raise ValueError(f"bench model differs from its manifest: {name}")
        files.append({"path": name, "bytes": size, "sha256": digest})
    if not files:
        raise ValueError("the bench manifest lists no models")
    with open(manifest, "rb") as stream:
        size, digest = _hash(stream)
    files.append({"path": "manifest.json", "bytes": size, "sha256": digest})
    pack = {"format": FORMAT_V2, "role": "bench", "dir": ROLE_DIRS["bench"],
            "model": "bench candidates", "files": sorted(files, key=lambda entry: entry["path"])}
    return render_descriptor(pack)


def stage_bench(bench_dir, output, certificate, key):
    info = bench_descriptor(bench_dir)
    pack = json.loads(info)
    base = root("bench", output)
    base.mkdir(parents=True, exist_ok=True)
    for entry in pack["files"]:
        shutil.copyfile(Path(bench_dir) / entry["path"], base / entry["path"])
    return stage_role("bench", None, output, None, certificate, key, signed=info)


def stage_all(source, output, certificate, key, *, roles=IMAGE_ROLES, bench=False, packs=PACKS,
              default_pack=DEFAULT_PACK):
    """The whole models partition: the conversation pack, then every other role."""
    empty_output(output)
    out_root = Path(source).parent
    staged = []
    for role in roles:
        if role == "conversation":
            stage(source, output, default_pack / "pack.json", certificate, key, fresh=False)
        else:
            stage_role(role, out_root, output, packs / role / "pack.json", certificate, key)
        staged.append(role)
    if bench:
        stage_bench(Path(source) / "bench", output, certificate, key)
        staged.append("bench")
    return staged


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["describe"]:
        parser = argparse.ArgumentParser(prog="package_model describe")
        parser.add_argument("describe")
        parser.add_argument("--role", required=True, choices=[r for r in ROLE_DIRS if r not in ("conversation", "bench")])
        parser.add_argument("--out", type=Path, default=REPO / "out")
        args = parser.parse_args(argv)
        try:
            pack = describe(args.role, args.out)
        except (ValueError, KeyError, OSError, tarfile.TarError) as error:
            parser.exit(1, f"Could not describe {args.role}: {error}\n")
        print(f"{args.role}: {len(pack['files'])} files")
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, type=Path, help="out/models (out/voice beside it)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--certificate", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--roles", default=",".join(IMAGE_ROLES),
                        help="comma-separated roles to stage (default: every image role)")
    parser.add_argument("--bench", action="store_true", help="also stage out/models/bench as /models/bench")
    args = parser.parse_args(argv)
    roles = [role for role in args.roles.split(",") if role]
    unknown = [role for role in roles if role not in IMAGE_ROLES]
    if unknown:
        parser.error(f"unknown roles: {', '.join(unknown)}")
    try:
        staged = stage_all(args.source, args.output, args.certificate, args.key, roles=roles, bench=args.bench)
    except (ValueError, KeyError, OSError, tarfile.TarError) as error:
        parser.exit(1, f"Could not package the model packs: {error}\n")
    print(f"Staged model packs: {', '.join(staged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
