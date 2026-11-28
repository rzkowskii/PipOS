"""Qualify fs-verity evidence for one already authenticated model-pack asset.

This is an opt-in, read-only lab tool.  It does not enable fs-verity and an
fs-verity digest is never treated as a replacement for the model pack's CMS
signature or its signed whole-file SHA-256.  A useful result therefore binds:

* the CMS envelope to the separately supplied trusted certificate;
* its exact signed bytes to the descriptor shipped by that system build;
* the selected regular file to the descriptor's size and SHA-256; and
* the kernel's FS_IOC_MEASURE_VERITY result to that same open file.

Run on the diagnostic Try image or from the repository on trusted Linux:

    PYTHONPATH=/usr/lib/being python3 -m lab.model_verity conversation reflex.gguf evidence.json \
      --models /mnt/models --share /mnt/system/usr/share/being \
      --certificate /mnt/system/usr/share/being/model-pack.pem

"unavailable" means the authenticated file has no measurable verity state or
the running kernel/filesystem cannot measure it.  It is not an integrity pass.
"""

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile

from mind import model_pack


FORMAT = "pipos-model-verity-qualification-v1"
CHUNK = 1 << 20
MAX_VERITY_DIGEST = 64
# _IOWR('f', 134, struct fsverity_digest); the UAPI structure's fixed header
# is two u16 fields followed by the caller-sized flexible digest buffer.
FS_IOC_MEASURE_VERITY = 0xC0046686
FS_VERITY_HASH_ALGS = {1: "sha256", 2: "sha512"}
FS_VERITY_HASH_BYTES = {1: 32, 2: 64}
UNAVAILABLE_ERRNOS = frozenset({errno.ENODATA, errno.ENOTTY, errno.EOPNOTSUPP})


def _open_asset(root, relative):
    """Open a descriptor path without following a link in any component."""
    relative = model_pack._relative(relative)
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        descriptors.append(current)
        parts = relative.split("/")
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                              dir_fd=current)
            descriptors.append(current)
        asset = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=current)
        if not stat.S_ISREG(os.fstat(asset).st_mode):
            os.close(asset)
            raise ValueError("selected model asset is not a regular file")
        return asset
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_asset(fd, expected_size):
    before = os.fstat(fd)
    if before.st_size != expected_size:
        return None, before
    digest = hashlib.sha256()
    seen = 0
    while seen < expected_size:
        data = os.read(fd, min(CHUNK, expected_size - seen))
        if not data:
            break
        digest.update(data)
        seen += len(data)
    after = os.fstat(fd)
    if seen != expected_size or _identity(before) != _identity(after):
        raise ValueError("selected model asset changed or became unreadable during qualification")
    return digest.hexdigest(), after


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _measure_verity(fd):
    buffer = bytearray(4 + MAX_VERITY_DIGEST)
    struct.pack_into("=HH", buffer, 0, 0, MAX_VERITY_DIGEST)
    try:
        fcntl.ioctl(fd, FS_IOC_MEASURE_VERITY, buffer, True)
    except OSError as error:
        if error.errno in UNAVAILABLE_ERRNOS:
            return {"status": "unavailable", "errno": errno.errorcode.get(error.errno, str(error.errno))}
        raise
    algorithm, size = struct.unpack_from("=HH", buffer)
    if size != FS_VERITY_HASH_BYTES.get(algorithm):
        raise ValueError("kernel returned an unsupported fs-verity digest")
    return {"status": "measured", "algorithm": FS_VERITY_HASH_ALGS[algorithm],
            "digest": bytes(buffer[4:4 + size]).hex()}


def qualify(role, relative, models, share, certificate):
    model_pack.check_role(role)
    relative = model_pack._relative(relative)
    descriptor = model_pack.descriptor_path(role, share)
    if descriptor is None:
        raise ValueError("qualification requires an exact descriptor shipped by the system build")
    pack_root = model_pack.root(role, models)
    envelope_limit = (model_pack.MAX_METADATA if role == "conversation"
                      else model_pack.MAX_DESCRIPTOR + 8192)
    envelope = model_pack.metadata_bytes(pack_root / model_pack.envelope_name(role), envelope_limit)
    pack = model_pack.authenticate(envelope, certificate, descriptor, role)
    if not pack.get("bound"):
        raise ValueError("signed pack is not bound to the system's exact descriptor")
    entries = {path: (size, digest) for path, size, digest in model_pack.files_of(pack)}
    if relative not in entries:
        raise ValueError("selected asset is not listed by the authenticated descriptor")
    expected_size, expected_digest = entries[relative]
    role_asset = (Path(model_pack.ROLE_DIRS[role]) / relative).as_posix()
    fd = _open_asset(models, role_asset)
    signed = {"bytes": expected_size, "sha256": expected_digest,
              "envelope_sha256": hashlib.sha256(envelope).hexdigest()}
    try:
        actual_digest, info = _hash_asset(fd, expected_size)
        if actual_digest != expected_digest:
            return {"format": FORMAT, "status": "tampered", "role": role, "asset": relative,
                    "signed": signed, "production_enforcement": False}
        measurement = _measure_verity(fd)
        if _identity(os.fstat(fd)) != _identity(info):
            raise ValueError("selected model asset changed during verity measurement")
    finally:
        os.close(fd)
    status = "measured" if measurement["status"] == "measured" else "unavailable"
    return {"format": FORMAT, "status": status, "role": role, "asset": relative,
            "signed": signed,
            "file": {"device": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
                     "inode": info.st_ino}, "verity": measurement,
            "production_enforcement": False}


def _publish(path, report):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"evidence path already exists: {path}")
    data = (json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=model_pack.ROLES)
    parser.add_argument("asset", help="exact relative path listed by the role descriptor")
    parser.add_argument("output", type=Path, help="new owner-only JSON evidence file")
    parser.add_argument("--models", type=Path, default=model_pack.MODELS)
    parser.add_argument("--share", type=Path, default=model_pack.SHARE)
    parser.add_argument("--certificate", type=Path, required=True,
                        help="trusted model-pack signer from the qualified system")
    args = parser.parse_args(argv)
    try:
        report = qualify(args.role, args.asset, args.models, args.share, args.certificate)
        _publish(args.output, report)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"model verity qualification stopped: {error}\n")
    return {"measured": 0, "tampered": 1, "unavailable": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
