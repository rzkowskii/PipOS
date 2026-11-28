"""Explicitly enable fs-verity on one disposable signed model-media copy.

This diagnostic helper never selects, mounts or modifies production
``/models``.  It requires all of the following independent gates:

* the existing CMS-authenticated model pack and build-bound descriptor;
* exact whole-file size and SHA-256 agreement with that descriptor;
* an externally prepared fs-verity baseline whose exact JSON-file SHA-256 was
  retained separately;
* a private marker at the selected disposable model root;
* the exact expected ``major:minor`` device; and
* a literal irreversible-operation confirmation token.

The trusted baseline must be produced on qualified Linux tooling from the
identical signed bytes.  This file intentionally does not reimplement
libfsverity's Merkle-tree format without pinned conformance vectors.  Enabling
verity is irreversible for that inode; replacement requires creating a new
file.  Use only on a separately selected disposable diagnostic copy.

No command in this module mounts media, creates keys, signs data, or enables
production enforcement.
"""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile

from lab import model_verity
from mind import model_pack


FORMAT = "pipos-model-verity-provision-v1"
BASELINE_FORMAT = "pipos-fsverity-baseline-v1"
MARKER = ".being-disposable-fsverity"
MARKER_TEXT = b"pipos-disposable-model-media-v1\n"
CONFIRM = "ENABLE_FS_VERITY_ON_DISPOSABLE_COPY"
MAX_BASELINE = 64 * 1024
MAX_TOOL_BYTES = 32 * 1024 * 1024
MAX_TOOL_OUTPUT = 256
TOOL_SECONDS = 600
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
DEVICE = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z")
FS_VERITY_HASH_ALG_SHA256 = 1
FS_VERITY_BLOCK_SIZE = 4096
# Linux UAPI: _IOW('f', 133, struct fsverity_enable_arg), 128-byte struct.
FS_IOC_ENABLE_VERITY = 0x40806685
ENABLE_STRUCT = struct.Struct("=IIIIQIIQ11Q")


def _outside_production(root):
    supplied = Path(root)
    before = supplied.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise ValueError("disposable model root may not be a symlink")
    root = supplied.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("disposable model root is not a directory")
    production = Path(model_pack.MODELS)
    if production.exists() and root.samefile(production):
        raise ValueError("active /models is never a disposable provisioning target")
    try:
        root.relative_to(production.resolve(strict=False))
    except ValueError:
        pass
    else:
        raise ValueError("active /models is never a disposable provisioning target")
    return root


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _disposable_mount(root, expected_device):
    """Pin one exact standalone ext4 mount, distinct from system state."""
    root = _outside_production(root)
    entries = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if len(fields) > separator + 2 and _unescape_mount(fields[4]) == str(root):
            entries.append((fields, separator))
    if len(entries) != 1:
        raise ValueError("disposable model root must be one exact mounted filesystem root")
    fields, separator = entries[0]
    if (fields[3] != "/" or fields[2] != expected_device
            or fields[separator + 1] != "ext4" or not fields[separator + 2].startswith("/dev/")):
        raise ValueError("disposable model root must be the selected standalone ext4 device")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(root_fd)
    if _device(info) != expected_device:
        os.close(root_fd)
        raise ValueError("disposable mount identity changed while opening")
    protected = (Path("/"), Path("/usr"), Path("/mind"), Path(model_pack.MODELS), Path(model_pack.SHARE))
    for path in protected:
        try:
            protected_info = path.stat()
        except OSError:
            continue
        if protected_info.st_dev == info.st_dev:
            os.close(root_fd)
            raise ValueError("disposable model filesystem is not independent from system state")
    return root, root_fd, info


def _open_asset_at(root_fd, relative):
    relative = model_pack._relative(relative)
    descriptors = [os.dup(root_fd)]
    try:
        current = descriptors[0]
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


def _reject_active_inode(models, role, asset):
    active = Path(model_pack.MODELS) / model_pack.ROLE_DIRS[role] / asset
    selected = models / model_pack.ROLE_DIRS[role] / asset
    try:
        if active.exists() and selected.samefile(active):
            raise ValueError("selected path aliases an active /models inode")
    except FileNotFoundError:
        pass


def _marker(root_fd):
    descriptor = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=root_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size != len(MARKER_TEXT)):
            raise ValueError("disposable marker is not a private owner-controlled regular file")
        value = os.read(descriptor, len(MARKER_TEXT) + 1)
        if value != MARKER_TEXT:
            raise ValueError("disposable marker has the wrong content")
    finally:
        os.close(descriptor)


def _read_baseline(path, trusted_digest):
    if not DIGEST.fullmatch(trusted_digest):
        raise ValueError("trusted baseline SHA-256 must be 64 lowercase hexadecimal digits")
    path = Path(path)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("verity baseline must be a regular non-symlink file")
    if not 0 < before.st_size <= MAX_BASELINE:
        raise ValueError("verity baseline is empty or exceeds its size bound")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if model_verity._identity(opened) != model_verity._identity(before):
            raise ValueError("verity baseline changed while opening")
        raw = b""
        while len(raw) < opened.st_size:
            block = os.read(descriptor, opened.st_size - len(raw))
            if not block:
                raise ValueError("verity baseline changed while reading")
            raw += block
        if model_verity._identity(os.fstat(descriptor)) != model_verity._identity(opened):
            raise ValueError("verity baseline changed while reading")
    finally:
        os.close(descriptor)
    if not hashlib.sha256(raw).hexdigest() == trusted_digest:
        raise ValueError("verity baseline does not match the independently retained SHA-256")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("verity baseline is invalid JSON") from error
    fields = {"format", "role", "asset", "bytes", "signed_sha256", "envelope_sha256",
              "hash_algorithm", "block_size", "verity_digest"}
    if (not isinstance(value, dict) or set(value) != fields or value.get("format") != BASELINE_FORMAT
            or value.get("hash_algorithm") != "sha256"
            or value.get("block_size") != FS_VERITY_BLOCK_SIZE
            or type(value.get("bytes")) is not int or value["bytes"] < 0
            or not isinstance(value.get("role"), str) or not isinstance(value.get("asset"), str)
            or not isinstance(value.get("signed_sha256"), str)
            or not DIGEST.fullmatch(value["signed_sha256"])
            or not isinstance(value.get("envelope_sha256"), str)
            or not DIGEST.fullmatch(value["envelope_sha256"])
            or not isinstance(value.get("verity_digest"), str)
            or not DIGEST.fullmatch(value["verity_digest"])):
        raise ValueError("verity baseline has the wrong schema")
    return value


def _pinned_executable(path, trusted_digest):
    if not DIGEST.fullmatch(trusted_digest):
        raise ValueError("trusted fsverity binary SHA-256 must be 64 lowercase hexadecimal digits")
    path = Path(path)
    before = path.lstat()
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= MAX_TOOL_BYTES):
        raise ValueError("fsverity binary must be a bounded regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    opened = os.fstat(descriptor)
    if model_verity._identity(opened) != model_verity._identity(before):
        os.close(descriptor)
        raise ValueError("fsverity binary changed while opening")
    digest = hashlib.sha256()
    remaining = opened.st_size
    while remaining:
        block = os.read(descriptor, min(1 << 20, remaining))
        if not block:
            os.close(descriptor)
            raise ValueError("fsverity binary changed while reading")
        digest.update(block)
        remaining -= len(block)
    if (not hmac.compare_digest(digest.hexdigest(), trusted_digest)
            or model_verity._identity(os.fstat(descriptor)) != model_verity._identity(opened)):
        os.close(descriptor)
        raise ValueError("fsverity binary does not match its independently retained SHA-256")
    return descriptor, opened


def _official_digest(tool_fd, asset_fd):
    if not Path("/proc/self/fd").is_dir():
        raise ValueError("pinned fsverity execution requires Linux /proc")
    command = [f"/proc/self/fd/{tool_fd}", "digest", "--compact",
               "--hash-alg=sha256", f"--block-size={FS_VERITY_BLOCK_SIZE}",
               f"/proc/self/fd/{asset_fd}"]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                    pass_fds=(tool_fd, asset_fd), close_fds=True,
                                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                                    timeout=TOOL_SECONDS, check=False)
        except subprocess.TimeoutExpired as error:
            raise ValueError("official fsverity digest exceeded its time bound") from error
        stdout.seek(0)
        output = stdout.read(MAX_TOOL_OUTPUT + 1)
        stderr.seek(0)
        error_output = stderr.read(MAX_TOOL_OUTPUT + 1)
    if result.returncode != 0 or error_output or len(output) > MAX_TOOL_OUTPUT:
        raise ValueError("official fsverity digest failed or produced unexpected diagnostics")
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        raise ValueError("official fsverity digest produced unexpected output")
    try:
        digest = output[:-1].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("official fsverity digest produced non-ASCII output") from error
    if not DIGEST.fullmatch(digest):
        raise ValueError("official fsverity digest has the wrong algorithm or length")
    return digest


def _publish_baseline(path, value):
    data = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not write complete verity baseline")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(data).hexdigest()


def _device(info):
    return f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"


def _enable(fd):
    argument = ENABLE_STRUCT.pack(
        1, FS_VERITY_HASH_ALG_SHA256, FS_VERITY_BLOCK_SIZE, 0, 0, 0, 0, 0,
        *([0] * 11))
    if len(argument) != 128:
        raise RuntimeError("fs-verity enable UAPI layout is unavailable")
    fcntl.ioctl(fd, FS_IOC_ENABLE_VERITY, argument)


def build_baseline(role, asset, models, share, certificate, fsverity, trusted_fsverity,
                   expected_device, output):
    """Compute one official read-only reference from pinned signed bytes."""
    if not DEVICE.fullmatch(expected_device):
        raise ValueError("expected device must be an exact major:minor value")
    model_pack.check_role(role)
    asset = model_pack._relative(asset)
    models, root_fd, root_info = _disposable_mount(models, expected_device)
    asset_fd = tool_fd = None
    try:
        output_parent = Path(output).parent.resolve(strict=True)
        if output_parent.stat().st_dev == root_info.st_dev:
            raise ValueError("baseline output must be retained outside the disposable model filesystem")
        _marker(root_fd)
        _reject_active_inode(models, role, asset)
        qualified = model_verity.qualify(role, asset, models, share, certificate)
        if qualified["status"] == "tampered":
            raise ValueError("selected disposable asset does not match the signed descriptor")
        if qualified.get("file", {}).get("device") != expected_device:
            raise ValueError("selected asset is not on the explicitly expected disposable device")
        signed = qualified["signed"]
        relative = (Path(model_pack.ROLE_DIRS[role]) / asset).as_posix()
        asset_fd = _open_asset_at(root_fd, relative)
        asset_before = os.fstat(asset_fd)
        if _device(asset_before) != expected_device or asset_before.st_nlink != 1:
            raise ValueError("selected asset device changed before baseline computation")
        digest, asset_pinned = model_verity._hash_asset(asset_fd, signed["bytes"])
        if digest != signed["sha256"]:
            raise ValueError("selected asset changed before baseline computation")
        tool_fd, tool_pinned = _pinned_executable(fsverity, trusted_fsverity)
        verity_digest = _official_digest(tool_fd, asset_fd)
        if model_verity._identity(os.fstat(asset_fd)) != model_verity._identity(asset_pinned):
            raise ValueError("selected asset changed during baseline computation")
        if model_verity._identity(os.fstat(tool_fd)) != model_verity._identity(tool_pinned):
            raise ValueError("fsverity binary changed during baseline computation")
        if os.fstat(root_fd).st_dev != root_info.st_dev:
            raise ValueError("disposable mount identity changed during baseline computation")
    finally:
        if asset_fd is not None:
            os.close(asset_fd)
        os.close(root_fd)
        if tool_fd is not None:
            os.close(tool_fd)
    baseline = {"format": BASELINE_FORMAT, "role": role, "asset": asset,
                "bytes": signed["bytes"], "signed_sha256": signed["sha256"],
                "envelope_sha256": signed["envelope_sha256"],
                "hash_algorithm": "sha256", "block_size": FS_VERITY_BLOCK_SIZE,
                "verity_digest": verity_digest}
    baseline_digest = _publish_baseline(output, baseline)
    return {"format": BASELINE_FORMAT, "baseline_sha256": baseline_digest,
            "fsverity_binary_sha256": trusted_fsverity,
            "signed_asset_sha256": signed["sha256"], "device": expected_device,
            "read_only": True}


def _provision_pinned(role, asset, models, root_fd, root_info, share, certificate,
                      baseline_path, trusted_baseline, expected_device):
    _marker(root_fd)
    _reject_active_inode(models, role, asset)
    baseline = _read_baseline(baseline_path, trusted_baseline)
    qualified = model_verity.qualify(role, asset, models, share, certificate)
    if qualified["status"] == "tampered":
        raise ValueError("selected disposable asset does not match the signed descriptor")
    signed = qualified["signed"]
    if (baseline["role"] != role or baseline["asset"] != asset
            or baseline["bytes"] != signed["bytes"]
            or baseline["signed_sha256"] != signed["sha256"]
            or baseline["envelope_sha256"] != signed["envelope_sha256"]):
        raise ValueError("verity baseline is not bound to this exact signed asset")
    if qualified.get("file", {}).get("device") != expected_device:
        raise ValueError("selected asset is not on the explicitly expected disposable device")
    if qualified["status"] == "measured":
        measured = qualified["verity"]
        if (measured.get("algorithm") != "sha256"
                or measured.get("digest") != baseline["verity_digest"]):
            raise ValueError("existing fs-verity state does not match the trusted baseline")
        return {"format": FORMAT, "status": "already-enabled", "role": role, "asset": asset,
                "signed": signed, "verity": measured, "device": expected_device,
                "trusted_baseline_sha256": trusted_baseline,
                "production_enforcement": False, "irreversible_ioctl_performed": False}

    relative = (Path(model_pack.ROLE_DIRS[role]) / asset).as_posix()
    fd = _open_asset_at(root_fd, relative)
    try:
        before = os.fstat(fd)
        if _device(before) != expected_device or before.st_nlink != 1:
            raise ValueError("selected asset is not one standalone disposable inode")
        active = Path(model_pack.MODELS) / model_pack.ROLE_DIRS[role] / asset
        try:
            active_info = active.stat()
        except FileNotFoundError:
            active_info = None
        if (active_info is not None
                and (active_info.st_dev, active_info.st_ino) == (before.st_dev, before.st_ino)):
            raise ValueError("selected inode aliases the active /models asset")
        digest, pinned = model_verity._hash_asset(fd, signed["bytes"])
        if digest != signed["sha256"]:
            raise ValueError("selected asset changed before fs-verity enablement")
        _marker(root_fd)
        if os.fstat(root_fd).st_dev != root_info.st_dev or os.fstat(fd).st_nlink != 1:
            raise ValueError("disposable mount or inode changed before fs-verity enablement")
        _enable(fd)
        measured = model_verity._measure_verity(fd)
        if measured.get("status") != "measured":
            raise ValueError("kernel did not expose verity after successful enablement")
        if (measured.get("algorithm") != "sha256"
                or measured.get("digest") != baseline["verity_digest"]):
            raise ValueError("enabled fs-verity digest does not match the trusted baseline")
        after = os.fstat(fd)
        if (after.st_nlink != 1 or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (pinned.st_dev, pinned.st_ino, pinned.st_size, pinned.st_mtime_ns)):
            raise ValueError("selected asset identity changed during fs-verity enablement")
    finally:
        os.close(fd)
    return {"format": FORMAT, "status": "enabled", "role": role, "asset": asset,
            "signed": signed, "verity": measured, "device": expected_device,
            "trusted_baseline_sha256": trusted_baseline,
            "production_enforcement": False, "irreversible_ioctl_performed": True}


def provision(role, asset, models, share, certificate, baseline_path, trusted_baseline,
              expected_device, confirmation):
    if confirmation != CONFIRM:
        raise ValueError("the irreversible disposable-copy confirmation token is required")
    if not DEVICE.fullmatch(expected_device):
        raise ValueError("expected device must be an exact major:minor value")
    model_pack.check_role(role)
    asset = model_pack._relative(asset)
    if (Path(share).resolve(strict=True) != Path(model_pack.SHARE).resolve(strict=True)
            or Path(certificate).resolve(strict=True) != Path(model_pack.PACK_CERTIFICATE).resolve(strict=True)):
        raise ValueError("irreversible provisioning requires the installed descriptor and fixed model-pack PEM")
    models, root_fd, root_info = _disposable_mount(models, expected_device)
    try:
        return _provision_pinned(role, asset, models, root_fd, root_info, share, certificate,
                                 baseline_path, trusted_baseline, expected_device)
    finally:
        os.close(root_fd)


def _baseline_main(argv):
    parser = argparse.ArgumentParser(description="Build an official read-only fs-verity baseline.")
    parser.add_argument("role", choices=model_pack.ROLES)
    parser.add_argument("asset")
    parser.add_argument("output", type=Path)
    parser.add_argument("--disposable-models", type=Path, required=True)
    parser.add_argument("--share", type=Path, default=model_pack.SHARE)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--fsverity", type=Path, required=True,
                        help="explicit pinned fsverity-utils executable")
    parser.add_argument("--trusted-fsverity-sha256", required=True)
    parser.add_argument("--expected-device", required=True)
    args = parser.parse_args(argv)
    try:
        report = build_baseline(
            args.role, args.asset, args.disposable_models, args.share, args.certificate,
            args.fsverity, args.trusted_fsverity_sha256, args.expected_device, args.output)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"model verity baseline stopped: {error}\n")
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "baseline":
        return _baseline_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=model_pack.ROLES)
    parser.add_argument("asset")
    parser.add_argument("output", type=Path)
    parser.add_argument("--disposable-models", type=Path, required=True)
    parser.add_argument("--share", type=Path, default=model_pack.SHARE)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--trusted-baseline-sha256", required=True)
    parser.add_argument("--expected-device", required=True)
    parser.add_argument("--confirm-irreversible", required=True, metavar=CONFIRM)
    args = parser.parse_args(argv)
    try:
        if os.path.lexists(args.output):
            raise FileExistsError("choose a new provisioning evidence path")
        parent = args.output.parent.resolve(strict=True)
        if not parent.is_dir() or _device(parent.stat()) == args.expected_device:
            raise ValueError("provisioning evidence must be written outside the disposable filesystem")
        report = provision(args.role, args.asset, args.disposable_models, args.share,
                           args.certificate, args.baseline, args.trusted_baseline_sha256,
                           args.expected_device, args.confirm_irreversible)
        model_verity._publish(args.output, report)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"disposable model verity provisioning stopped: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
