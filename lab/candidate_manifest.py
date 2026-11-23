"""Fingerprint one PipOS candidate before building, after packaging and before promotion.

Run snapshot in the frozen build checkout. Seal the factory and Try media made
from one Buildroot output, then verify the sealed manifest after testing.
Only named regular artifacts are inspected. Bundle hashes identify exact bytes;
signature, compatibility and deployment are separate RAUC acceptance tests.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
import tempfile
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path


# Include staged model inputs, build recipes, organ sources and lab test inputs.
SCOPES = ("Cargo.toml", "Cargo.lock", "crates", "config", "mind", "os", "sim", "lab", "scripts", "tests")
OPTIONAL_SCOPES = (".cargo", "rust-toolchain", "rust-toolchain.toml", "out/models")
FACTORY = ("esp", "system-a", "system-b", "models", "mind")
TRY = ("try-esp", "try-system", "try-models")
IMAGES = ("factory", "try_image", "installed_image")
ARTIFACTS = (*IMAGES, "factory_bundle", "installed_bundle", "update_fixtures")
SECTOR = 512  # Buildroot/genimage raw outputs use 512-byte logical sectors.
BLOCK = 1 << 20


@contextmanager
def regular(path):
    def signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError(f"expected a regular file: {path}")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if signature(os.fstat(stream.fileno())) != signature(initial):
            raise ValueError(f"file replaced before reading: {path}")
        yield stream, initial
        if signature(path.lstat()) != signature(initial) or signature(os.fstat(stream.fileno())) != signature(initial):
            raise ValueError(f"file changed while reading: {path}")


def fingerprint(stream, length):
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        data = stream.read(min(remaining, BLOCK))
        if not data:
            raise ValueError(f"unexpected EOF in {stream.name}")
        digest.update(data)
        remaining -= len(data)
    return {"bytes": length, "sha256": digest.hexdigest()}


def source_files(path):
    if stat.S_ISDIR(path.lstat().st_mode):
        for child in sorted(path.iterdir()):
            if child.name not in ("__pycache__", ".DS_Store") and child.suffix not in (".pyc", ".pyo"):
                yield from source_files(child)
    else:
        yield path


def read_json(path):
    with regular(path) as (stream, _):
        return json.load(stream)


def factory_slots(root):
    # These assignments also drive the Buildroot GPT, GRUB and RAUC templates.
    with regular(root / "os/board/beingos/slots.sh") as (stream, _):
        assignments = dict(re.findall(r"^slot_([ab])_uuid=([0-9a-f-]+)$", stream.read().decode(), re.M))
    return {f"system-{key}": str(uuid.UUID(assignments[key])) for key in ("a", "b")}


def source_snapshot(root):
    files = {}
    for scope in (*SCOPES, *OPTIONAL_SCOPES):
        if scope in OPTIONAL_SCOPES and not os.path.lexists(root / scope):
            continue
        for path in source_files(root / scope):
            with regular(path) as (stream, info):
                record = fingerprint(stream, info.st_size)
                record["execute_bits"] = info.st_mode & 0o111
                files[path.relative_to(root).as_posix()] = record
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    result = {"kind": "beingos-source-v1", "files": files, "sha256": hashlib.sha256(encoded).hexdigest()}
    # The appliance always loads this path, irrespective of the manifest name.
    model_key = "out/models/reflex.gguf"
    manifest_key = "out/models/manifest.json"
    if model_key in files or manifest_key in files:
        if model_key not in files or manifest_key not in files:
            raise ValueError("the model and its manifest must be supplied together in out/models")
        entry = read_json(root / manifest_key)["reflex"]
        model = files[model_key]
        if (entry["file"] != "reflex.gguf" or entry["bytes"] != model["bytes"] or
                entry["sha256"] != model["sha256"] or not isinstance(entry["from"], str) or not entry["from"]):
            raise ValueError("model identity, size or hash does not match out/models/manifest.json")
        result["model"] = entry
    return result


def check_source(root, expected):
    if expected.get("kind") != "beingos-source-v1":
        raise ValueError("expected a PipOS source snapshot")
    actual = source_snapshot(root)
    if actual != expected:
        before, now = expected["files"], actual["files"]
        changed = sorted(name for name in before.keys() | now.keys() if before.get(name) != now.get(name))
        raise ValueError("source changed since snapshot: " + ", ".join(changed[:10]))


def gpt(stream, lba, last_lba):
    stream.seek(lba * SECTOR)
    header = stream.read(SECTOR)
    if len(header) != SECTOR:
        raise ValueError("truncated GPT header")
    magic, rev, size, crc, _, here, other, first, last, guid, table, count, width, table_crc = struct.unpack_from(
        "<8sIIIIQQQQ16sQIII", header)
    if (magic != b"EFI PART" or rev != 0x10000 or not 92 <= size <= SECTOR or here != lba or
            other != (last_lba if lba == 1 else 1) or not 1 < first <= last < last_lba or
            not 1 <= count <= 4096 or width != 128 or table >= last_lba or
            zlib.crc32(header[:16] + bytes(4) + header[20:size]) != crc):
        raise ValueError("invalid GPT header or CRC")
    table_end = table + (width * count + SECTOR - 1) // SECTOR
    outside_partitions = 2 <= table and table_end <= first if lba == 1 else last < table and table_end <= last_lba
    if not outside_partitions:
        raise ValueError("GPT entry array outside metadata area")
    stream.seek(table * SECTOR)
    entries = stream.read(width * count)
    if len(entries) != width * count or zlib.crc32(entries) != table_crc:
        raise ValueError("invalid GPT partition array or CRC")
    parts = []
    for kind, unique, start, end, _, name_bytes in struct.iter_unpack("<16s16sQQQ72s", entries):
        if kind == bytes(16):
            continue
        if not first <= start <= end <= last or (parts and start <= parts[-1]["last_lba"]):
            raise ValueError("overlapping or out-of-range partition")
        parts.append({"name": name_bytes.decode("utf-16-le").rstrip("\0"), "id": str(uuid.UUID(bytes_le=unique)),
                      "first_lba": start, "last_lba": end})
    return str(uuid.UUID(bytes_le=guid)), parts, first, last, table_crc


def disk_record(stream, length, role):
    last_lba = length // SECTOR - 1
    if length % SECTOR or last_lba < 3:
        raise ValueError(f"invalid {role} image size")
    first = gpt(stream, 1, last_lba)
    if gpt(stream, last_lba, last_lba) != first:
        raise ValueError(f"inconsistent primary and backup GPT in {role}")
    guid, parts, *_ = first
    expected = TRY if role == "try_image" else FACTORY
    if tuple(p["name"] for p in parts) != expected:
        raise ValueError(f"unexpected {role} partitions")
    ids = {guid, *(p["id"] for p in parts)}
    if len(ids) != len(parts) + 1 or str(uuid.UUID(int=0)) in ids:
        raise ValueError(f"invalid or duplicate {role} GPT identities")
    for part in parts:
        if part["name"] not in ("system-a", "system-b", "try-system"):
            continue
        offset = part["first_lba"] * SECTOR
        stream.seek(offset)
        header = stream.read(96)
        used, = struct.unpack_from("<Q", header, 40)
        capacity = (part["last_lba"] - part["first_lba"] + 1) * SECTOR
        if header[:4] != b"hsqs" or not 96 <= used <= capacity:
            raise ValueError(f"invalid {role} SquashFS in {part['name']}")
        stream.seek(offset)
        part["rootfs"] = fingerprint(stream, used)
    return {"disk_id": guid, "logical_sector_bytes": SECTOR, "partitions": parts}


def artifact_records(paths, directory, factory_ids):
    extra_roles = paths.keys() - set(ARTIFACTS)
    if (not {"factory", "try_image"} <= paths.keys() or
            any(not re.fullmatch(r"extra:[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", role) for role in extra_roles)):
        raise ValueError("expected named factory and Try artifacts")
    records, root_hashes, disk_ids, inodes = {}, set(), set(), set()
    for role, path in sorted(paths.items()):
        with regular(path) as (stream, info):
            if (info.st_dev, info.st_ino) in inodes or not info.st_size:
                raise ValueError(f"duplicate or empty artifact: {role}")
            inodes.add((info.st_dev, info.st_ino))
            record = fingerprint(stream, info.st_size)
            record["path"] = os.path.relpath(path, directory)
            if role in IMAGES:
                layout = disk_record(stream, info.st_size, role)
                if role == "factory" and any(p["id"] != factory_ids[p["name"]] for p in layout["partitions"]
                                             if p["name"] in factory_ids):
                    raise ValueError("factory slot IDs differ from the source board configuration")
                ids = {layout["disk_id"], *(p["id"] for p in layout["partitions"])}
                if disk_ids.intersection(ids):
                    raise ValueError(f"{role} shares GPT identities with another candidate disk")
                disk_ids.update(ids)
                root_hashes.update(p["rootfs"]["sha256"] for p in layout["partitions"] if "rootfs" in p)
                record.update(layout)
            records[role] = record
    if len(root_hashes) != 1:
        raise ValueError("candidate system payloads differ across factory, Try or installed slots")
    if ("factory_bundle" in records and "installed_bundle" in records and
            records["factory_bundle"]["sha256"] == records["installed_bundle"]["sha256"]):
        raise ValueError("factory and installed channels supplied identical bundles")
    return records


def publish(path, data):
    # Publish atomically without replacing evidence from an earlier run.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        try:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.link(tmp.name, path)
        finally:
            os.unlink(tmp.name)


def bind_model_audit(source, artifacts, audit):
    if audit["source_sha256"] != source["sha256"] or audit["model"] != source.get("model"):
        raise ValueError("model audit belongs to a different source snapshot")
    for role in ("factory", "try_image"):
        if audit["media"][role]["image_sha256"] != artifacts[role]["sha256"]:
            raise ValueError(f"model audit belongs to a different {role} image")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    before = commands.add_parser("snapshot", help="record the frozen source and any out/models inputs")
    seal = commands.add_parser("seal", help="bind explicit build outputs to a pre-build snapshot")
    verify = commands.add_parser("verify", help="recheck source, layout and all recorded bytes")
    for command in (before, seal, verify):
        command.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="source checkout")
    for command in (before, seal):
        command.add_argument("--output", type=Path, required=True, help="new JSON file outside the input scopes")
    seal.add_argument("--source", type=Path, required=True)
    seal.add_argument("--model-audit", type=Path, help="bind the completed embedded-model inspection to these exact images")
    for role in ARTIFACTS:
        seal.add_argument("--" + role.replace("_", "-"), type=Path, required=role in ("factory", "try_image"))
    seal.add_argument("--extra", action="append", default=[], metavar="NAME=PATH",
                      help="fingerprint an additional distinct regular QA artifact; repeatable")
    verify.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command != "verify":
            output = args.output.resolve()
            if any(output.is_relative_to(root / scope) for scope in (*SCOPES, *OPTIONAL_SCOPES)):
                raise ValueError("save the manifest outside the source input scopes")
        if args.command == "snapshot":
            publish(args.output, source_snapshot(root))
        elif args.command == "seal":
            source = read_json(args.source)
            check_source(root, source)
            paths = {role: getattr(args, role) for role in ARTIFACTS if getattr(args, role) is not None}
            for extra in args.extra:
                name, delimiter, filename = extra.partition("=")
                role = f"extra:{name}"
                if not delimiter or not filename or role in paths:
                    raise ValueError(f"invalid or duplicate extra artifact: {extra}")
                paths[role] = Path(filename)
            records = artifact_records(paths, args.output.parent, factory_slots(root))
            check_source(root, source)
            candidate = {"kind": "beingos-candidate-v1", "source": source, "artifacts": records}
            if args.model_audit:
                audit = read_json(args.model_audit)
                bind_model_audit(source, records, audit)
                candidate["model_audit"] = audit
            publish(args.output, candidate)
        else:
            data = read_json(args.manifest)
            if data["kind"] != "beingos-candidate-v1":
                raise ValueError("expected a sealed PipOS candidate")
            expected = data["artifacts"]
            paths = {role: args.manifest.parent / record["path"] for role, record in expected.items()}
            actual = artifact_records(paths, args.manifest.parent, factory_slots(root))
            if actual != expected:
                raise ValueError("candidate artifact changed: " + ", ".join(role for role in paths if actual[role] != expected[role]))
            check_source(root, data["source"])
            if "model_audit" in data:
                bind_model_audit(data["source"], actual, data["model_audit"])
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1
    print(f"{args.command} complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
