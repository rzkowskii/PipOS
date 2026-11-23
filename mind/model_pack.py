"""Authenticate the model packs on /models against the running system's trust root.

Each role (conversation, dream, vad, asr, tts, embeddings, speaker, bench) is
one signed pack: a JSON descriptor naming every file with its size and
SHA-256, signed with the builder's model-pack key as a CMS envelope beside the
files. A pack qualifies when its envelope verifies against the pinned signer,
the signed descriptor equals the one this system build ships (where it ships
one), and every file is a regular file with the right size and digest.

    verify(role)  -> the pack's descriptor, or ValueError
    root(role)    -> its directory on /models

Hashing 0.5-1.3 GB on every boot is slow on the laptop. A verdict cache
(/mind/cortex/pack-verdict.json) records, for each file that hashed correctly,
its partition, inode, size, mtime and ctime in nanoseconds, the expected
SHA-256 and this system's build-id. While all of those match, and the envelope
still verifies, the full hash is skipped. Writing a file changes its ctime,
which user space cannot set back, so a changed file is always hashed again.
`rehash_job` re-hashes everything weekly from the night shift.

The conversation pack keeps its first format (beingos-model-pack-v1,
/models/reflex.gguf with /models/beingos-conversation.cms) so installed
systems and their tooling keep working. This module is run as a script by
being-cortex.service (ExecCondition), so it imports nothing from the mind.
"""

import argparse
import asyncio
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


MODELS = Path("/models")
SHARE = Path("/usr/share/being")
DEFAULT_DESCRIPTOR = SHARE / "default-model/pack.json"
PACK_DESCRIPTORS = SHARE / "model-packs"
# The model pack's own signer, separate from the update keyring
# (post-build.sh); older images only have the keyring.
PACK_CERTIFICATE = Path("/usr/share/being/model-pack.pem")
KEYRING = Path("/etc/rauc/keyring.pem")
VERDICT = Path("/mind/cortex/pack-verdict.json")
BUILD_ID = Path("/etc/being/build-id")
ENVELOPE = "beingos-conversation.cms"
WEIGHTS = "reflex.gguf"
MAX_METADATA = 64 * 1024
MAX_DESCRIPTOR = 512 * 1024
FORMAT_V1 = "beingos-model-pack-v1"
FORMAT_V2 = "beingos-model-pack-v2"
VERDICT_FORMAT = "beingos-pack-verdict-v1"
REHASH_SECONDS = 7 * 24 * 3600
CHUNK = 1 << 20

# Where each role lives under /models (docs/PIP-FUNCTIONS.md section 5.2).
ROLE_DIRS = {
    "conversation": "",
    "dream": "dream",
    "vad": "asr",
    "asr": "asr/moonshine-tiny",
    "tts": "voice/piper/default",
    "embeddings": "embeddings/blink-8M",
    "speaker": "speaker",
    "bench": "bench",
}
ROLES = tuple(ROLE_DIRS)
# Packs whose files come with every image; bench only on a bench stick.
IMAGE_ROLES = tuple(role for role in ROLES if role != "bench")


def certificate_default():
    return PACK_CERTIFICATE if PACK_CERTIFICATE.exists() else KEYRING


DEFAULT_CERTIFICATE = certificate_default()


def envelope_name(role):
    return ENVELOPE if role == "conversation" else f"beingos-{role}.cms"


def check_role(role):
    if role not in ROLE_DIRS:
        raise ValueError(f"unknown model pack role: {role!r}")
    return role


def root(role, models=MODELS):
    """The directory that holds a role's files (no check that they are there)."""
    check_role(role)
    return Path(models) / ROLE_DIRS[role] if ROLE_DIRS[role] else Path(models)


def descriptor_path(role, share=SHARE):
    """The descriptor this system build ships for a role. None for bench,
    whose content is chosen per stick and is described by its own envelope."""
    check_role(role)
    if role == "bench":
        return None
    if role == "conversation":
        return Path(share) / "default-model/pack.json"
    return Path(share) / "model-packs" / role / "pack.json"


@contextmanager
def regular_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"expected a regular file at {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"expected a regular file at {path}")
        yield stream


def metadata_bytes(path, limit=MAX_METADATA):
    with regular_file(path) as stream:
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError(f"missing or oversized model metadata: {path}")
    return data


def openssl(*args, data):
    result = subprocess.run(["openssl", *args], input=data, capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError(f"model pack cryptographic operation failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def check_der(envelope):
    # OpenSSL accepts trailing bytes after a DER value. Require exactly one
    # definite-length ContentInfo before treating the envelope as candidate media.
    if len(envelope) < 2 or envelope[0] != 0x30:
        raise ValueError("invalid model envelope")
    width = envelope[1] & 0x7f if envelope[1] & 0x80 else 0
    if envelope[1] == 0x80 or width > 3 or len(envelope) < 2 + width:
        raise ValueError("invalid model envelope length")
    size = int.from_bytes(envelope[2:2 + width], "big") if width else envelope[1]
    if size + 2 + width != len(envelope):
        raise ValueError("unexpected data in model envelope")


def signed_content(envelope, certificate):
    # This release uses the builder's leaf certificate as the explicit
    # signer. -nointern ignores embedded signer certs; -noverify disables
    # CA-chain/clock validation. The pinned signer's signature is still required.
    check_der(envelope)
    return openssl("cms", "-verify", "-binary", "-inform", "DER", "-nointern", "-noverify",
                   "-certfile", str(certificate), data=envelope)


def _relative(path):
    """A descriptor's file path: relative, normalised, no dot segments."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError(f"bad file path in model pack: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"bad file path in model pack: {path!r}")
    return path


def check_descriptor(pack, role):
    """Shape checks for a decoded descriptor of either format."""
    if not isinstance(pack, dict) or pack.get("role") != role:
        raise ValueError("unsupported model pack role, format or file")
    if role == "conversation":
        if pack.get("format") != FORMAT_V1 or pack.get("file") != WEIGHTS:
            raise ValueError("unsupported model pack role, format or file")
        return pack
    if pack.get("format") != FORMAT_V2 or pack.get("dir") != ROLE_DIRS[role]:
        raise ValueError("unsupported model pack role, format or file")
    files = pack.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("model pack lists no files")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("bad file entry in model pack")
        path = _relative(entry.get("path"))
        if path in seen or path.startswith("beingos-") or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
            raise ValueError(f"bad file entry in model pack: {path}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"bad digest in model pack: {path}")
        seen.add(path)
    return pack


def files_of(pack):
    """[(relative path, bytes, sha256)] for a pack of either format."""
    if pack["format"] == FORMAT_V1:
        return [(pack["file"], pack["bytes"], pack["sha256"])]
    return [(entry["path"], entry["bytes"], entry["sha256"]) for entry in pack["files"]]


def authenticate(envelope, certificate, descriptor, role="conversation"):
    """The signed descriptor, decoded, if the envelope verifies.

    With `descriptor` (the copy this system ships), the signed bytes must be
    exactly that file. Without it (bench, or an older image that does not ship
    that role's descriptor), the signature alone vouches for the content, and
    the result says so with "bound": False.
    """
    check_role(role)
    canonical = None
    if descriptor is not None:
        canonical = metadata_bytes(descriptor, MAX_METADATA if role == "conversation" else MAX_DESCRIPTOR)
    signed = signed_content(envelope, certificate)
    if canonical is not None and signed != canonical:
        raise ValueError("signed model descriptor differs from the qualified system descriptor")
    try:
        pack = json.loads(signed)
    except ValueError as error:
        raise ValueError("signed model descriptor is not JSON") from error
    pack = check_descriptor(pack, role)
    pack = dict(pack)
    pack["bound"] = canonical is not None
    return pack


# --- the verdict cache --------------------------------------------------------

def partition_id(device):
    """A stable name for the partition holding a file: its partition UUID
    from the udev database when readable (it is, inside the cortex sandbox),
    else /dev/disk/by-partuuid, else the device number."""
    major, minor = os.major(device), os.minor(device)
    try:
        for line in Path(f"/run/udev/data/b{major}:{minor}").read_text().splitlines():
            if line.startswith("E:ID_PART_ENTRY_UUID="):
                return line.partition("=")[2].strip().lower()
    except OSError:
        pass
    try:
        for link in Path("/dev/disk/by-partuuid").iterdir():
            try:
                if os.stat(link).st_rdev == device:
                    return link.name.lower()
            except OSError:
                continue
    except OSError:
        pass
    return f"dev:{major}:{minor}"


def file_key(info, sha256, build_id, partition=partition_id):
    return {"partuuid": partition(info.st_dev), "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "sha256": sha256,
            "build_id": build_id}


def read_build_id(path=BUILD_ID):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text[:200] or None


class Verdicts:
    """The verdict file: {"format", "roles": {role: {"descriptor", "files": {path: key}, "rehashed"}}}.

    Every error reading or writing it is swallowed: without it, packs are
    simply hashed in full.
    """

    def __init__(self, path=VERDICT):
        self.path = Path(path) if path is not None else None

    def load(self):
        if self.path is None:
            return {}
        try:
            data = json.loads(metadata_bytes(self.path, MAX_DESCRIPTOR))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("format") != VERDICT_FORMAT or not isinstance(data.get("roles"), dict):
            return {}
        return data["roles"]

    def role(self, role):
        entry = self.load().get(role)
        return entry if isinstance(entry, dict) else {}

    def update(self, role, entry):
        """Replace one role's entry (None removes it). Returns True when written."""
        if self.path is None:
            return False
        try:
            directory = self.path.parent
            if not directory.is_dir():
                return False
            with open(directory / ".pack-verdict.lock", "a") as lock:
                try:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
                roles = self.load()
                if entry is None:
                    roles.pop(role, None)
                else:
                    roles[role] = entry
                data = json.dumps({"format": VERDICT_FORMAT, "roles": roles}, sort_keys=True).encode()
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".pack-verdict-", delete=False) as temp:
                    name = Path(temp.name)
                    try:
                        temp.write(data)
                        temp.flush()
                        os.fchmod(temp.fileno(), 0o644)
                        os.fsync(temp.fileno())
                        os.replace(name, self.path)
                    finally:
                        name.unlink(missing_ok=True)
            return True
        except OSError:
            return False


def descriptor_digest(pack):
    data = {key: value for key, value in pack.items() if key != "bound"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _check_path(base, relative):
    """The file at base/relative, refusing symbolic links on the way."""
    path = Path(base)
    for part in relative.split("/"):
        path = path / part
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"expected a regular file at {path}")
    return path


def hash_stream(stream, size_hint=None):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(CHUNK), b""):
        digest.update(block)
    return digest.hexdigest()


def check_files(base, pack, *, verdict=None, build_id=None, partition=partition_id, hashed=None):
    """Check every file of a pack. With a verdict entry, files whose key still
    matches are not hashed. Returns the new {path: key} for the verdict."""
    previous = (verdict or {}).get("files") if (verdict or {}).get("descriptor") == descriptor_digest(pack) else None
    previous = previous if isinstance(previous, dict) else {}
    keys = {}
    for relative, size, sha256 in files_of(pack):
        path = _check_path(base, relative)
        with regular_file(path) as stream:
            info = os.fstat(stream.fileno())
            if info.st_size != size:
                raise ValueError(f"model size differs from the qualified pack ({relative})")
            key = file_key(info, sha256, build_id, partition)
            if build_id is not None and previous.get(relative) == key:
                keys[relative] = key
                continue
            if hashed is not None:
                hashed.append(relative)
            if hash_stream(stream) != sha256:
                raise ValueError(f"model SHA-256 differs from the qualified pack ({relative})")
            keys[relative] = key
    return keys


def check_weights(models, pack):
    """The conversation weights against a v1 descriptor (always a full hash)."""
    check_files(models, pack)


def verify_envelope(models, signed_pack, certificate, descriptor, role="conversation", *,
                    verdicts=None, build_id=None, partition=partition_id, hashed=None):
    """Authenticate one envelope and check its pack's files under `models`."""
    envelope = metadata_bytes(signed_pack, MAX_METADATA if role == "conversation" else MAX_DESCRIPTOR + 8192)
    pack = authenticate(envelope, certificate, descriptor, role)
    base = root(role, models)
    entry = verdicts.role(role) if verdicts is not None else {}
    keys = check_files(base, pack, verdict=entry, build_id=build_id, partition=partition, hashed=hashed)
    if verdicts is not None and build_id is not None:
        new = {"descriptor": descriptor_digest(pack), "files": keys,
               "rehashed": entry.get("rehashed") if entry.get("files") == keys else None}
        if new != entry:
            verdicts.update(role, new)
    return envelope, pack


def verify(role="conversation", *, models=MODELS, certificate=None, descriptor="system",
           verdict=VERDICT, build_id="system", share=SHARE, hashed=None):
    """Verify a role's pack. Returns its descriptor; raises ValueError.

    `descriptor` "system" uses the copy this build ships (when it ships one);
    None verifies by signature alone. `verdict` None disables the cache.
    """
    check_role(role)
    certificate = Path(certificate) if certificate is not None else certificate_default()
    if descriptor == "system":
        descriptor = descriptor_path(role, share)
        if descriptor is not None and role != "conversation" and not descriptor.exists():
            descriptor = None      # an image that predates this role's descriptor
    if build_id == "system":
        build_id = read_build_id()
    verdicts = Verdicts(verdict) if verdict is not None else None
    _, pack = verify_envelope(models, root(role, models) / envelope_name(role), certificate, descriptor, role,
                              verdicts=verdicts, build_id=build_id, hashed=hashed)
    return pack


def provision(models, envelope):
    # An old read-only model partition is mounted separately for this explicit
    # operation. Publish complete authenticated metadata or retain its old file.
    with tempfile.NamedTemporaryFile(dir=models, prefix=".beingos-pack-", delete=False) as temp:
        name = Path(temp.name)
        try:
            temp.write(envelope)
            temp.flush()
            os.fchmod(temp.fileno(), 0o644)
            os.fsync(temp.fileno())
            os.replace(name, models / ENVELOPE)
            fd = os.open(models, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            name.unlink(missing_ok=True)


# --- the weekly re-hash (a night-shift job) -----------------------------------

class RehashJob:
    """Re-hash every pack on /models once a week, a slice at a time.

    The night shift calls `await job.run_chunk(budget_s)` until it returns
    True ("done for tonight"). Each call reads for about `budget_s` seconds in
    a worker thread and keeps its place between calls. A file that no longer
    matches loses its role's verdict, so the next start hashes it in full and
    refuses it; `failures` names it. Calling the job returns a fresh one.
    """

    def __init__(self, models=MODELS, verdict=VERDICT, *, roles=IMAGE_ROLES, certificate=None, share=SHARE,
                 build_id="system", period=REHASH_SECONDS, clock=time.time, partition=partition_id):
        self.models, self.verdict, self.roles = Path(models), verdict, tuple(roles)
        self.certificate, self.share, self.build_id = certificate, share, build_id
        self.period, self.clock, self.partition = period, clock, partition
        self.failures = []
        self._queue = None
        self._current = None

    def __call__(self):
        return RehashJob(self.models, self.verdict, roles=self.roles, certificate=self.certificate, share=self.share,
                         build_id=self.build_id, period=self.period, clock=self.clock, partition=self.partition)

    def due(self):
        verdicts = Verdicts(self.verdict)
        now = self.clock()
        roles = verdicts.load()
        for role in self.roles:
            if not root(role, self.models).joinpath(envelope_name(role)).exists():
                continue
            rehashed = (roles.get(role) or {}).get("rehashed")
            if type(rehashed) not in (int, float) or now - rehashed >= self.period or rehashed > now:
                return True
        return False

    def _plan(self):
        build_id = read_build_id() if self.build_id == "system" else self.build_id
        certificate = Path(self.certificate) if self.certificate is not None else certificate_default()
        queue = []
        for role in self.roles:
            signed = root(role, self.models) / envelope_name(role)
            if not signed.exists():
                continue
            descriptor = descriptor_path(role, self.share)
            if descriptor is not None and role != "conversation" and not descriptor.exists():
                descriptor = None
            try:
                envelope = metadata_bytes(signed, MAX_METADATA if role == "conversation" else MAX_DESCRIPTOR + 8192)
                pack = authenticate(envelope, certificate, descriptor, role)
            except (OSError, ValueError) as error:
                self._fail(role, f"envelope: {error}")
                continue
            queue.append({"role": role, "pack": pack, "files": list(files_of(pack)), "keys": {},
                          "build_id": build_id})
        return queue

    def _fail(self, role, reason):
        self.failures.append({"role": role, "reason": str(reason)[:200]})
        Verdicts(self.verdict).update(role, None)

    def _work(self, budget_s):
        """Hash for about budget_s seconds. True when every pack is done."""
        deadline = time.monotonic() + max(0.0, budget_s)
        if self._queue is None:
            self._queue = self._plan()
        while self._queue:
            item = self._queue[0]
            if self._current is None:
                if not item["files"]:
                    entry = {"descriptor": descriptor_digest(item["pack"]), "files": item["keys"],
                             "rehashed": self.clock()}
                    if item["build_id"] is not None:
                        Verdicts(self.verdict).update(item["role"], entry)
                    self._queue.pop(0)
                    continue
                relative, size, sha256 = item["files"][0]
                try:
                    path = _check_path(root(item["role"], self.models), relative)
                    with regular_file(path) as stream:
                        info = os.fstat(stream.fileno())
                    if info.st_size != size:
                        raise ValueError("size differs")
                except (OSError, ValueError) as error:
                    self._fail(item["role"], f"{relative}: {error}")
                    self._queue.pop(0)
                    continue
                self._current = {"relative": relative, "sha256": sha256, "offset": 0, "digest": hashlib.sha256(),
                                 "key": file_key(info, sha256, item["build_id"], self.partition), "path": path}
            current = self._current
            try:
                with regular_file(current["path"]) as stream:
                    info = os.fstat(stream.fileno())
                    if file_key(info, current["sha256"], item["build_id"], self.partition) != current["key"]:
                        raise ValueError("changed while it was being checked")
                    stream.seek(current["offset"])
                    while True:
                        block = stream.read(CHUNK)
                        if not block:
                            break
                        current["digest"].update(block)
                        current["offset"] += len(block)
                        if time.monotonic() >= deadline:
                            break
                    finished = current["offset"] >= info.st_size
                    if finished and hasattr(os, "posix_fadvise") and item["role"] != "conversation":
                        try:
                            # Keep the waking model in the page cache, not the night's reading.
                            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                        except OSError:
                            pass
            except (OSError, ValueError) as error:
                self._current = None
                self._fail(item["role"], f"{current['relative']}: {error}")
                self._queue.pop(0)
                continue
            if not finished:
                return False
            self._current = None
            item["files"].pop(0)
            if current["digest"].hexdigest() != current["sha256"]:
                self._fail(item["role"], f"{current['relative']}: SHA-256 differs")
                self._queue.pop(0)
                continue
            item["keys"][current["relative"]] = current["key"]
            if time.monotonic() >= deadline:
                return not self._queue
        return True

    async def run_chunk(self, budget_s):
        if self._queue is None and not await asyncio.to_thread(self.due):
            return True
        done = await asyncio.to_thread(self._work, budget_s)
        if done:
            self._queue = None
        return done


rehash_job = RehashJob()


# --- command line ---------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("verify", help="verify or print the signed, qualified pack of a role")
    check.add_argument("--json", action="store_true")
    check.add_argument("--role", default="conversation", choices=ROLES)
    check.add_argument("--verdict", type=Path, default=VERDICT, help="the verdict cache (default: %(default)s)")
    check.add_argument("--no-verdict", dest="verdict", action="store_const", const=None,
                       help="hash every file in full")
    where = sub.add_parser("root", help="print the directory of a role's files")
    where.add_argument("--role", default="conversation", choices=ROLES)
    where.add_argument("--models", default=MODELS, type=Path)
    migrate = sub.add_parser("provision", help="authenticate a donor envelope and add it to a mounted legacy pack")
    migrate.add_argument("--signed-pack", required=True, type=Path)
    for command in (check, migrate):
        command.add_argument("--models", required=command is migrate, default=MODELS, type=Path)
        command.add_argument("--certificate", type=Path, default=None)
        command.add_argument("--descriptor", type=Path, default=None,
                             help="the qualified descriptor (default: the one this system ships)")
    args = parser.parse_args(argv)
    if args.action == "root":
        print(root(args.role, args.models))
        return 0
    certificate = args.certificate or certificate_default()
    try:
        if args.action == "provision":
            descriptor = args.descriptor or DEFAULT_DESCRIPTOR
            envelope, pack = verify_envelope(args.models, args.signed_pack, certificate, descriptor)
            provision(args.models, envelope)
        else:
            pack = verify(args.role, models=args.models, certificate=certificate,
                          descriptor=args.descriptor if args.descriptor is not None else "system",
                          verdict=args.verdict)
        if getattr(args, "json", False):
            print(json.dumps(pack, sort_keys=True))
        else:
            print(f"Qualified {pack.get('model', pack['role'])} {pack.get('quantization', '')} "
                  f"({pack['sha256'] if 'sha256' in pack else str(len(pack['files'])) + ' files'}).")
        return 0
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        if isinstance(error, OSError) and error.errno == errno.ENOENT:
            error = f"missing {error.filename}"
        print(f"Model pack rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
