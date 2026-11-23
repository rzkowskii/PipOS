"""Verify owner-supplied BCM4331 firmware and stage an optional Buildroot overlay."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


DEFAULT_OVERLAY = Path(__file__).resolve().parents[1] / "out" / "firmware-overlay"
FIRMWARE_DIR = Path("usr/lib/firmware/b43")
MANIFEST_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](?:\./)?(?:b43/)?([a-z0-9][a-z0-9_]*\.fw)")
MAX_FILE_BYTES = 1024 * 1024


def required_files(core):
    return (f"ucode{core}_mimo.fw", f"ht0initvals{core}.fw", f"ht0bsinitvals{core}.fw")


def read_local_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Expected a regular file at {path}.")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"File exceeds the size limit at {path}.")
    return data


def verify(source, manifest, core):
    expected = {}
    for line_number, line in enumerate(read_local_file(manifest).decode("ascii").splitlines(), 1):
        if not line:
            continue
        match = MANIFEST_LINE.fullmatch(line)
        if not match:
            raise ValueError(f"Expected a SHA-256 line naming a b43 .fw file at line {line_number}.")
        digest, name = match.groups()
        if name in expected:
            raise ValueError(f"Duplicate firmware checksum for {name}.")
        expected[name] = digest.lower()

    names = required_files(core)
    missing = [name for name in names if name not in expected]
    if missing:
        raise ValueError(f"Missing SHA-256 checksums for core {core}: {', '.join(missing)}.")
    verified = {}
    for name in names:
        data = read_local_file(source / name)
        if not data or hashlib.sha256(data).hexdigest() != expected[name]:
            raise ValueError(f"Firmware checksum verification failed for {name}.")
        verified[name] = data
    return verified


def require_new_overlay(output):
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Overlay already exists at {output}. Select a new path.")


def stage(verified, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    # Prepare all files on the destination filesystem before publishing the tree.
    with tempfile.TemporaryDirectory(prefix=".b43-", dir=output.parent) as work:
        temporary = Path(work)
        for subtree in reversed(FIRMWARE_DIR.parents):
            if subtree == Path("."):
                continue
            (temporary / subtree).mkdir()
            (temporary / subtree).chmod(0o755)
        path = temporary / FIRMWARE_DIR
        path.mkdir()
        path.chmod(0o755)
        for name, data in verified.items():
            target = path / name
            target.write_bytes(data)
            # The non-root Buildroot user must be able to read the approved payload.
            target.chmod(0o644)
        output.mkdir(mode=0o755)  # Claims the path without replacing an existing directory.
        try:
            output.chmod(0o755)
            (temporary / "usr").rename(output / "usr")
        except OSError:
            output.rmdir()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="local directory containing b43 .fw files")
    parser.add_argument("manifest", type=Path, help="independently supplied sha256sum manifest")
    parser.add_argument("--core", choices=(26, 29), type=int, required=True, help="HT core revision observed in Linux")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY, help="new Buildroot overlay directory")
    output.add_argument("--verify-only", action="store_true", help="check the three files without writing an overlay")
    args = parser.parse_args(argv)
    try:
        if not args.verify_only:
            require_new_overlay(args.overlay)
        verified = verify(args.source, args.manifest, args.core)
        if args.verify_only:
            print(f"Verified BCM4331 core {args.core}: {', '.join(verified)}")
        else:
            stage(verified, args.overlay)
            print(args.overlay.resolve())
    except (OSError, ValueError) as error:
        parser.exit(1, f"b43-firmware: {error}\n")


if __name__ == "__main__":
    main()
