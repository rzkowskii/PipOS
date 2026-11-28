"""Authenticate the model packs on /models against the running system's trust root.

Each role (conversation, dream, vad, asr, tts, embeddings, speaker, bench) is
one signed pack: a JSON descriptor naming every file with its size and
SHA-256, signed with the builder's model-pack key as a CMS envelope beside the
files. A pack qualifies when its envelope verifies against the pinned signer,
the signed descriptor equals the one this system build ships (where it ships
one), and every file is a regular file with the right size and digest.

    verify(role)  -> the pack's descriptor, or ValueError
    root(role)    -> its directory on /models
    trusted(role, paths) -> (ok, reason): may an organ load these files?
                     The voice, the ears and the embedder ask before loading
                     a pack; a refused pack means Flite, PocketSphinx or
                     words-only recall, with a log line.

Hashing 0.5-1.3 GB on every boot is slow on the laptop. A verdict cache
(/mind/cortex/pack-verdict.json) records, for each file that hashed correctly,
its partition, inode, size, mtime and ctime in nanoseconds, the expected
SHA-256 and this system's build-id. While all of those match, and the envelope
still verifies, the full hash is skipped. Writing a file changes its ctime,
which user space cannot set back, so a changed file is always hashed again.
`rehash_job` re-hashes everything weekly from the night shift.

A Try stick's /mind is a fresh tmpfs, so that cache is empty at every boot,
and hashing the 532 MB conversation pack from a USB 2 stick took 23.4 s
before the cortex could answer (cable log, 25 Nov). On Try media only (see
`provisional_allowed`), a pack whose envelope verifies against the pinned
signer and the shipped descriptor, and whose files are all regular files of
the signed sizes, is trusted provisionally: `verify` returns it with
"checked": "provisional" and hashes nothing. being-model-check.service then
hashes every pack in full (`check_media`) at idle CPU and IO priority. A pack
that fails is marked refused in /run/being-model-check, so every later check
on this boot hashes it in full and refuses it, and the organs that may have
loaded it are restarted so their own check refuses it (Flite, PocketSphinx,
words-only recall, or no model), or stopped when systemd will not restart
them. Only proof of a bad pack refuses it (`integrity_failure`): a check that
could not run (openssl starved under the boot's load, a read error) is tried
again a few times and then reported "unchecked". The night's re-hash marks a
pack it proves bad the same way, so a later start cannot trust it again.

The threat model this keeps: the Try stick has no verified boot, so its root
filesystem (this file, the pinned certificate, the shipped descriptors) is as
writable as its models partition to anyone holding the stick. The full hash
on Try therefore never defended against a deliberate attacker with the stick;
it catches damaged or mismatched media (a failing stick, a bad write, a models
partition from another build). The signature, descriptor and size checks
still run before anything is loaded; damage is caught in the background
within the first minutes, and the model servers that would read a damaged
file are sandboxed and offline. Nothing that outlives the boot takes
provisional trust: installed systems, bench boots, `--no-verdict`, `--full`
and `being.model_check=full` always hash before use, and an installed copy of
the stick's packs is hashed in full on its own first boot (its partition and
verdict cache are new).

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
import threading
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
# How long one openssl call may take. The background check runs openssl at
# idle CPU priority during the busiest part of a Try boot, so it allows more,
# and a timeout there means "try again later", never "refuse the pack".
OPENSSL_TIMEOUT_S = 15
BACKGROUND_OPENSSL_TIMEOUT_S = 120

# Provisional trust on Try media (see the module docstring).
CMDLINE = Path("/proc/cmdline")
# try-image.sh labels the Try stick's models partition so.
TRY_LABEL = "being-try-models"
# being-model-check.service: its results, and a "<role>.refused" mark for a
# pack that failed its full check on this boot.
CHECK_DIR = Path("/run/being-model-check")
CHECK_UNIT = Path("/usr/lib/systemd/system/being-model-check.service")
CHECK_FORMAT = "beingos-model-check-v1"
# Kernel argument that turns provisional trust off: every pack is hashed before use.
FULL_CHECK_ARG = "being.model_check=full"
# The order the background check takes: what the first answers need first.
CHECK_ORDER = ("conversation", "tts", "vad", "asr", "embeddings", "speaker", "dream")
# A pack the background check could not read (openssl starved past its
# timeout, a read error from the stick) is checked again after each of these
# pauses, then reported "unchecked": it is never refused for that.
CHECK_RETRY_DELAYS_S = (10.0, 30.0, 90.0)
# The organs that load each role's files, restarted when its pack fails.
ROLE_UNITS = {
    "conversation": ("being-cortex.service",),
    "dream": ("being-dream-cortex.service",),
    "vad": ("being-voice.service",),
    "asr": ("being-voice.service",),
    "tts": ("being-voice.service",),
    "speaker": (),      # staged only: nothing loads it yet
    "embeddings": ("being-mind.service",),
}

# Where each role lives under /models (docs/PIP-FUNCTIONS.md section 5.2).
ROLE_DIRS = {
    "conversation": "",
    "dream": "dream",
    "vad": "asr",
    "asr": "asr/moonshine-tiny",
    "tts": "voice/piper/default",
    "embeddings": "embeddings/blink-8M",
    "speaker": "speaker",
    # Optional multimodal pack.  It is deliberately not in IMAGE_ROLES: a
    # normal image must remain usable without vision media.
    "vision": "vision",
    "bench": "bench",
    "library": "library/oewn",
    "library_simple": "library/simple-en",
}
ROLES = tuple(ROLE_DIRS)
# Bench, vision and the owner-selected dictionary are optional media roles.
IMAGE_ROLES = tuple(role for role in ROLES if role not in ("bench", "vision", "library", "library_simple"))


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
    result = subprocess.run(["openssl", *args], input=data, capture_output=True, timeout=OPENSSL_TIMEOUT_S)
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
    if role == "vision":
        # These bindings are part of the runtime contract, rather than an
        # invitation to import arbitrary model assets from the staging tree.
        if pack.get("vision_profile") not in ("small", "reference"):
            raise ValueError("unsupported vision profile")
        weights = pack.get("vision_weights")
        if weights not in ("conversation", "vision.gguf"):
            raise ValueError("unsupported vision weights")
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
    if role == "vision":
        entries = {entry["path"]: entry for entry in files}
        if "mmproj.gguf" not in entries or entries["mmproj.gguf"]["bytes"] <= 0:
            raise ValueError("vision pack lacks positive-sized mmproj.gguf")
        if pack["vision_weights"] == "conversation":
            pinned = pack.get("conversation_sha256")
            if (not isinstance(pinned, str) or len(pinned) != 64
                    or any(c not in "0123456789abcdef" for c in pinned)):
                raise ValueError("vision pack lacks conversation weight pin")
        else:
            if "conversation_sha256" in pack:
                raise ValueError("dedicated vision weights must not pin conversation weights")
            if "vision.gguf" not in entries or entries["vision.gguf"]["bytes"] <= 0:
                raise ValueError("vision pack lacks positive-sized vision.gguf")
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


# --- provisional trust on Try media -------------------------------------------

def filesystem_label(device):
    """The filesystem label udev recorded for a block device, or None when
    it cannot be read (then nothing is trusted provisionally)."""
    try:
        for line in Path(f"/run/udev/data/b{os.major(device)}:{os.minor(device)}").read_text().splitlines():
            if line.startswith("E:ID_FS_LABEL="):
                return line.partition("=")[2].strip()
    except OSError:
        pass
    return None


def refused_mark(role, check_dir=CHECK_DIR):
    """Where the background check marks a pack that failed on this boot."""
    return Path(check_dir) / f"{check_role(role)}.refused"


# A listed file that is gone, or a path that no longer leads to a regular
# file, is the pack's fault, not the machine's.
_INTEGRITY_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EISDIR})


def integrity_failure(error, models=MODELS):
    """Does `error`, raised while checking a pack on `models`, prove the pack
    bad (refuse it), or only that it could not be checked just now (try again)?

    Operational, never a refusal: openssl running past its timeout, running
    out of memory, and any OS error (a read error from the stick, openssl or
    the certificate missing) except a pack file or folder on `models` that is
    missing or not what the descriptor names. Everything else, the ValueError
    for a bad envelope, signature, descriptor, type, size or SHA-256 above
    all, refuses: an unexpected error fails safe.
    """
    if isinstance(error, (subprocess.TimeoutExpired, MemoryError)):
        return False
    if isinstance(error, OSError):
        return (error.errno in _INTEGRITY_ERRNOS and isinstance(error.filename, (str, os.PathLike))
                and on_models([error.filename], models))
    return True


def provisional_allowed(role, models=MODELS, *, cmdline=CMDLINE, label=filesystem_label,
                        check_dir=CHECK_DIR, check_unit=CHECK_UNIT):
    """May `role`'s pack be trusted before its full hash? Only when all hold:

    - a Try boot (being.try=1), not a bench boot, without being.model_check=full;
    - /models is mounted read-only and carries the Try stick's label;
    - the background check (being-model-check.service) is installed and has
      started on this boot (its /run folder exists), so a full hash follows;
    - that check, or the night's re-hash, has not refused this role on this boot.

    Anything that cannot be read answers no, which means a full hash.
    """
    check_role(role)
    # Provisional trust is safe only for roles the boot's background checker
    # will actually hash. Optional owner media is verified in full on use.
    if role not in CHECK_ORDER:
        return False
    try:
        words = Path(cmdline).read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return False
    if "being.try=1" not in words or "being.bench=1" in words or FULL_CHECK_ARG in words:
        return False
    try:
        info = os.stat(models)
        if not os.statvfs(models).f_flag & os.ST_RDONLY:
            return False
    except (OSError, AttributeError):
        return False
    if label(info.st_dev) != TRY_LABEL or not Path(check_unit).is_file():
        return False
    try:
        return Path(check_dir).is_dir() and not os.path.lexists(refused_mark(role, check_dir))
    except (OSError, ValueError):
        return False


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
                # Make the rename itself durable: a power cut must not roll a
                # removed (distrusted) entry back into the folder.
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return True
        except OSError:
            return False

    def drop(self, role):
        """Stop trusting a role's cached verdict, failing safe. True when it is gone.

        Rewriting the file can fail (a full or failing /mind). Unlinking it
        then still works on a full disk, and costs only a full hash of every
        pack at the next start, never a skipped hash of a damaged file.
        """
        if self.path is None:
            return True
        if self.update(role, None):
            return True
        try:
            self.path.unlink(missing_ok=True)
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except OSError:
            return not self.path.exists() and not self.path.is_symlink()


def descriptor_digest(pack):
    data = {key: value for key, value in pack.items() if key not in ("bound", "checked")}
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


def check_files(base, pack, *, verdict=None, build_id=None, partition=partition_id, hashed=None,
                deferred=None):
    """Check every file of a pack. With a verdict entry, files whose key still
    matches are not hashed. With a `deferred` list (provisional trust), files
    with no matching verdict are checked for type and size only and listed
    there, unhashed. Returns the new {path: key} for the verdict: only files
    that were hashed or matched a verdict."""
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
            if deferred is not None:
                deferred.append(relative)
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
                    verdicts=None, build_id=None, partition=partition_id, hashed=None, deferred=None):
    """Authenticate one envelope and check its pack's files under `models`."""
    envelope = metadata_bytes(signed_pack, MAX_METADATA if role == "conversation" else MAX_DESCRIPTOR + 8192)
    pack = authenticate(envelope, certificate, descriptor, role)
    base = root(role, models)
    entry = verdicts.role(role) if verdicts is not None else {}
    keys = check_files(base, pack, verdict=entry, build_id=build_id, partition=partition, hashed=hashed,
                       deferred=deferred)
    # A provisional check proved nothing new about the bytes: the cache keeps
    # what the last full check recorded.
    if verdicts is not None and build_id is not None and not deferred:
        new = {"descriptor": descriptor_digest(pack), "files": keys,
               "rehashed": entry.get("rehashed") if entry.get("files") == keys else None}
        if new != entry:
            verdicts.update(role, new)
    return envelope, pack


def verify(role="conversation", *, models=MODELS, certificate=None, descriptor="system",
           verdict=VERDICT, build_id="system", share=SHARE, hashed=None, provisional="auto"):
    """Verify a role's pack. Returns its descriptor; raises ValueError.

    `descriptor` "system" uses the copy this build ships (when it ships one);
    None verifies by signature alone. `verdict` None disables the cache.
    `provisional` "auto" skips the hash of files with no cached verdict on Try
    media (`provisional_allowed`; never with the cache disabled), True skips
    it wherever called, False hashes every file a cached full verdict does
    not cover (the default before Try media had this). The result says which:
    "checked" is "full" or "provisional" (some files not yet hashed).
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
    if provisional == "auto":
        provisional = verdicts is not None and provisional_allowed(role, models)
    deferred = [] if provisional else None
    _, pack = verify_envelope(models, root(role, models) / envelope_name(role), certificate, descriptor, role,
                              verdicts=verdicts, build_id=build_id, hashed=hashed, deferred=deferred)
    pack["checked"] = "provisional" if deferred else "full"
    return pack


# --- the organs' check before loading a pack ------------------------------------

# One verdict per role per process: (descriptor, None, when) or (None, reason,
# when). A later load in the same process does not hash a verified pack again;
# the night's re-hash (RehashJob) is what notices a file changing under a
# running organ. A refusal is checked again after TRUST_RETRY_S, so one
# failed check (openssl slow under the boot's load) does not keep Flite,
# PocketSphinx or words-only recall until the organ restarts.
TRUST_RETRY_S = 600.0
_trust = {}
_trust_lock = threading.Lock()


def _within(path, base):
    return path == base or base in path.parents


def on_models(paths, models=None):
    """True when any of `paths` is on the models partition, so `trusted`
    has a pack to check (and may hash it). Quick: no file is read."""
    models = Path(os.path.abspath(MODELS if models is None else models))
    return any(_within(Path(os.path.abspath(path)), models) for path in paths)


def trusted(role, paths, *, models=None, verifier=None):
    """May an organ load `paths` (files or folders) for `role`? (ok, reason).

    Anything on `models` must belong to the role's pack: under its folder,
    listed in its signed descriptor (a folder: holding listed files), and
    the pack must pass `verify(role)`. Paths elsewhere are a development
    copy no pack describes (the host tests, a lab override): those are
    allowed. Blocking, since the first check may hash the pack: call it off
    the event loop. Never raises: a pack that cannot be checked is refused.
    """
    check_role(role)
    models = Path(os.path.abspath(MODELS if models is None else models))
    base = Path(os.path.abspath(root(role, models)))
    inside = []
    for path in paths:
        path = Path(os.path.abspath(path))
        if not _within(path, models):
            continue
        if not _within(path, base):
            return False, f"{path} is not part of the {role} pack"
        inside.append(path.relative_to(base).as_posix())
    if not inside:
        return True, None
    key = (role, str(models))
    with _trust_lock:  # one check at a time: two loads never hash a pack twice
        found = _trust.get(key)
        if found is None or (found[0] is None and time.monotonic() - found[2] >= TRUST_RETRY_S):
            try:
                _trust[key] = ((verifier or verify)(role, models=models), None, time.monotonic())
            except Exception as error:  # openssl missing or slow, a bad file, a bad descriptor
                if isinstance(error, OSError) and error.errno == errno.ENOENT:
                    error = f"missing {error.filename}"
                _trust[key] = (None, str(error)[:200] or type(error).__name__, time.monotonic())
        pack, reason, _ = _trust[key]
    if pack is None:
        return False, reason
    listed = {name for name, _, _ in files_of(pack)}
    for relative in inside:
        if relative != "." and relative not in listed and not any(name.startswith(relative + "/") for name in listed):
            return False, f"{relative} is not in the signed {role} pack"
    return True, None


def forget_trust():
    """Check every pack again on its next load (tests; a pack replaced in place)."""
    with _trust_lock:
        _trust.clear()


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

    On a Try boot the next start may trust a pack with no verdict without
    hashing it (`provisional_allowed`), so a pack that proved bad is also
    marked refused in `check_dir` (when the background check made that
    folder): every later check on this boot then hashes it and refuses it.
    Only proof of a bad pack is marked (`integrity_failure`); a read error or
    a slow openssl drops the verdict, which costs a full hash, nothing more.
    """

    def __init__(self, models=MODELS, verdict=VERDICT, *, roles=IMAGE_ROLES, certificate=None, share=SHARE,
                 build_id="system", period=REHASH_SECONDS, clock=time.time, partition=partition_id,
                 check_dir=CHECK_DIR):
        self.models, self.verdict, self.roles = Path(models), verdict, tuple(roles)
        self.certificate, self.share, self.build_id = certificate, share, build_id
        self.period, self.clock, self.partition = period, clock, partition
        self.check_dir = Path(check_dir) if check_dir is not None else None
        self.failures = []
        self._queue = None
        self._current = None

    def __call__(self):
        return RehashJob(self.models, self.verdict, roles=self.roles, certificate=self.certificate, share=self.share,
                         build_id=self.build_id, period=self.period, clock=self.clock, partition=self.partition,
                         check_dir=self.check_dir)

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
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                self._fail(role, f"envelope: {error}", error)
                continue
            queue.append({"role": role, "pack": pack, "files": list(files_of(pack)), "keys": {},
                          "build_id": build_id})
        return queue

    def _fail(self, role, reason, error=None):
        """`error` None: the bytes themselves proved the pack bad."""
        failure = {"role": role, "reason": str(reason)[:200]}
        if not Verdicts(self.verdict).drop(role):
            failure["verdict_kept"] = True      # nothing on /mind could be changed; say so
        if (error is None or integrity_failure(error, self.models)) and self.check_dir is not None:
            try:
                marked = self.check_dir.is_dir() and _write_text(refused_mark(role, self.check_dir),
                                                                 f"night re-hash: {failure['reason']}\n")
            except (OSError, ValueError):
                marked = False
            if marked:
                failure["marked"] = True
        self.failures.append(failure)

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
                    self._fail(item["role"], f"{relative}: {error}", error)
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
                self._fail(item["role"], f"{current['relative']}: {error}", error)
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


# The optional library is absent on ordinary images and therefore costs no
# work. When installed, it receives the same weekly full re-hash as image packs.
rehash_job = RehashJob(roles=(*IMAGE_ROLES, "library", "library_simple"))


# --- the background check of Try media (being-model-check.service) -------------

def _write_text(path, text):
    """Write a small file atomically. True when written."""
    path = Path(path)
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}-", delete=False) as temp:
            name = Path(temp.name)
            try:
                temp.write(text)
                temp.flush()
                os.fchmod(temp.fileno(), 0o644)
                os.replace(name, path)
            finally:
                name.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def check_status(check_dir=CHECK_DIR):
    """The background check's results on this boot, or None before it ran:
    {"format", "done": bool, "roles": {role: {"result": "ok"|"refused"|"absent", ...}}}."""
    try:
        data = json.loads(metadata_bytes(Path(check_dir) / "status.json", MAX_METADATA))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != CHECK_FORMAT or not isinstance(data.get("roles"), dict):
        return None
    return data


def systemctl(*args):
    """Run systemctl; True when it succeeded."""
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def stop_using(role, marked, run=systemctl):
    """Make the organs that may have loaded a refused pack let go of it.

    With the refusal marked, a restart is enough: the organ's own check then
    refuses the pack (Flite, PocketSphinx, words-only recall, or no model).
    Without the mark a restart would trust it again, so they are stopped, and
    they are stopped too when the restart cannot be asked for.
    Returns (units, how): how is "restarted", "stopped", None when nothing
    loads the pack, "left" when `run` is None (asked to leave them alone),
    or "failed" when systemd took neither request."""
    units = tuple(ROLE_UNITS.get(role, ()))
    if not units:
        return units, None
    if run is None:
        return units, "left"
    if marked and run("--no-block", "try-restart", *units):
        return units, "restarted"
    if run("--no-block", "stop", *units):
        return units, "stopped"
    return units, "failed"


def check_media(*, models=MODELS, verdict=VERDICT, check_dir=CHECK_DIR, roles=CHECK_ORDER, verifier=None,
                run=systemctl, clock=time.monotonic, out=print, sleep=time.sleep,
                retry_delays=CHECK_RETRY_DELAYS_S, openssl_timeout=BACKGROUND_OPENSSL_TIMEOUT_S):
    """Hash every pack on the Try stick in full, once, after its provisional trust.

    Each pack that passes gets a full verdict in the cache, so later checks
    on this boot skip the hash. Each that proves bad (`integrity_failure`) is
    marked refused (later checks hash it in full, and refuse it) and its
    organs are restarted, or stopped when that fails. A pack that could not
    be checked (openssl starved past its timeout, a read error) is checked
    again after each of `retry_delays`, then reported "unchecked": neither
    marked nor restarted. A pack already refused on this boot is not checked
    or restarted again. Writes `check_dir`/status.json as it goes. Returns 0,
    or 1 when any pack was refused or an organ could not be made to let go.

    `openssl_timeout` replaces OPENSSL_TIMEOUT_S while it runs: this runs in
    its own process (being-model-check.service), at idle priority."""
    global OPENSSL_TIMEOUT_S
    check_dir = Path(check_dir)
    results, refused = {}, False

    def status(done):
        _write_text(check_dir / "status.json",
                    json.dumps({"format": CHECK_FORMAT, "done": done, "roles": results}, sort_keys=True))

    def reason_of(error):
        if isinstance(error, OSError) and error.errno == errno.ENOENT:
            error = f"missing {error.filename}"
        return str(error)[:200] or type(error).__name__

    def check(role, attempt, final):
        """One try at one role. True when it needs another."""
        nonlocal refused
        mark = refused_mark(role, check_dir)
        if os.path.lexists(mark):
            try:
                reason = mark.read_text(encoding="utf-8", errors="replace").strip()[:200]
            except OSError:
                reason = ""
            results[role] = {"result": "refused", "reason": reason or "refused earlier on this boot"}
            refused = True
            return False
        started = clock()
        try:
            (verifier or verify)(role, models=models, verdict=verdict, provisional=False)
        except Exception as error:
            reason = reason_of(error)
            if not integrity_failure(error, models):
                results[role] = {"result": "unchecked", "reason": reason, "attempts": attempt}
                if final:
                    out(f"model check: could not check the {role} pack after {attempt} tries ({reason}); "
                        "it stays trusted on its signature and sizes for this boot", file=sys.stderr)
                else:
                    out(f"model check: could not check the {role} pack yet ({reason}); trying again later",
                        file=sys.stderr)
                return True
            marked = _write_text(mark, reason + "\n") if check_dir.is_dir() else False
            units, how = stop_using(role, marked, run)
            results[role] = {"result": "refused", "reason": reason, "units": list(units), "marked": marked}
            if how is not None:
                results[role]["organs"] = how
            refused = True
            if how is None:
                after = "Nothing loads it."
            elif how == "failed":
                after = f"Could not restart or stop {', '.join(units)}: it may still be in use."
            elif how == "left":
                after = f"Left {', '.join(units)} running, as asked."
            else:
                after = f"{how.capitalize()} {', '.join(units)}."
            out(f"Model pack rejected: {role}: {reason}. {after}", file=sys.stderr)
            return False
        seconds = round(clock() - started, 2)
        results[role] = {"result": "ok", "seconds": seconds}
        out(f"model check: the {role} pack passed its full check in {seconds:.1f} s")
        return False

    saved, OPENSSL_TIMEOUT_S = OPENSSL_TIMEOUT_S, openssl_timeout
    try:
        pending = []
        for role in roles:
            if not (root(role, models) / envelope_name(role)).exists():
                results[role] = {"result": "absent"}
                continue
            if check(role, 1, not retry_delays):
                pending.append(role)
            status(False)
        for number, delay in enumerate(retry_delays, start=2):
            if not pending:
                break
            sleep(delay)
            final = number == len(retry_delays) + 1
            pending = [role for role in pending if check(role, number, final)]
            status(False)
    finally:
        OPENSSL_TIMEOUT_S = saved
    status(True)
    failed = any(entry.get("organs") == "failed" for entry in results.values())
    return 1 if refused or failed else 0


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
    check.add_argument("--full", action="store_true",
                       help="hash every file with no cached verdict, even on Try media")
    media = sub.add_parser("check-media", help="hash every pack in full after a Try boot's provisional trust "
                                                "(being-model-check.service)")
    media.add_argument("--models", default=MODELS, type=Path)
    media.add_argument("--verdict", type=Path, default=VERDICT)
    media.add_argument("--status-dir", type=Path, default=CHECK_DIR)
    media.add_argument("--no-restart", action="store_true", help="mark a refused pack but leave the organs alone")
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
    if args.action == "check-media":
        return check_media(models=args.models, verdict=args.verdict, check_dir=args.status_dir,
                           run=None if args.no_restart else systemctl)
    certificate = args.certificate or certificate_default()
    try:
        if args.action == "provision":
            descriptor = args.descriptor or DEFAULT_DESCRIPTOR
            envelope, pack = verify_envelope(args.models, args.signed_pack, certificate, descriptor)
            provision(args.models, envelope)
        else:
            pack = verify(args.role, models=args.models, certificate=certificate,
                          descriptor=args.descriptor if args.descriptor is not None else "system",
                          verdict=args.verdict, provisional=False if args.full else "auto")
        if getattr(args, "json", False):
            print(json.dumps(pack, sort_keys=True))
        else:
            later = ("; signature and sizes checked, the full hash follows in the background on this Try boot"
                     if pack.get("checked") == "provisional" else "")
            print(f"Qualified {pack.get('model', pack['role'])} {pack.get('quantization', '')} "
                  f"({pack['sha256'] if 'sha256' in pack else str(len(pack['files'])) + ' files'}{later}).")
        return 0
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        if isinstance(error, OSError) and error.errno == errno.ENOENT:
            error = f"missing {error.filename}"
        print(f"Model pack rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
