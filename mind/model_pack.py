"""Authenticate the default conversation pack against the running system's trust root."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


DEFAULT_DESCRIPTOR = Path("/usr/share/being/default-model/pack.json")
DEFAULT_CERTIFICATE = Path("/etc/rauc/keyring.pem")
ENVELOPE = "beingos-conversation.cms"
WEIGHTS = "reflex.gguf"
MAX_METADATA = 64 * 1024


@contextmanager
def regular_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"expected a regular file at {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"expected a regular file at {path}")
        yield stream


def metadata_bytes(path):
    with regular_file(path) as stream:
        data = stream.read(MAX_METADATA + 1)
    if not 0 < len(data) <= MAX_METADATA:
        raise ValueError(f"missing or oversized model metadata: {path}")
    return data


def openssl(*args, data):
    result = subprocess.run(["openssl", *args], input=data, capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError(f"model pack cryptographic operation failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def authenticate(envelope, certificate, descriptor):
    # This release uses the builder's RAUC leaf certificate as the explicit
    # signer. -nointern ignores embedded signer certs; -noverify disables
    # CA-chain/clock validation. The pinned signer's signature is still required.
    canonical = metadata_bytes(descriptor)
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
    signed = openssl("cms", "-verify", "-binary", "-inform", "DER", "-nointern", "-noverify",
                     "-certfile", str(certificate), data=envelope)
    if signed != canonical:
        raise ValueError("signed model descriptor differs from the qualified system descriptor")
    pack = json.loads(signed)
    if pack["format"] != "beingos-model-pack-v1" or pack["file"] != WEIGHTS or pack["role"] != "conversation":
        raise ValueError("unsupported model pack role, format or file")
    return pack


def check_weights(models, pack):
    path = models / WEIGHTS
    with regular_file(path) as stream:
        if os.fstat(stream.fileno()).st_size != pack["bytes"]:
            raise ValueError("model size differs from the qualified pack")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != pack["sha256"]:
        raise ValueError("model SHA-256 differs from the qualified pack")


def verify(models, signed_pack, certificate, descriptor):
    envelope = metadata_bytes(signed_pack)
    pack = authenticate(envelope, certificate, descriptor)
    check_weights(models, pack)
    return envelope, pack


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("verify", help="verify or print the signed, qualified model identity")
    check.add_argument("--json", action="store_true")
    migrate = sub.add_parser("provision", help="authenticate a donor envelope and add it to a mounted legacy pack")
    migrate.add_argument("--signed-pack", required=True, type=Path)
    for command in (check, migrate):
        command.add_argument("--models", required=command is migrate, default=Path("/models"), type=Path)
        command.add_argument("--certificate", type=Path, default=DEFAULT_CERTIFICATE)
        command.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR)
    args = parser.parse_args(argv)
    try:
        source = args.signed_pack if args.action == "provision" else args.models / ENVELOPE
        envelope, pack = verify(args.models, source, args.certificate, args.descriptor)
        if args.action == "provision":
            provision(args.models, envelope)
        if getattr(args, "json", False):
            print(json.dumps(pack, sort_keys=True))
        else:
            print(f"Qualified {pack['model']} {pack['quantization']} ({pack['sha256']}).")
        return 0
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Model pack rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
