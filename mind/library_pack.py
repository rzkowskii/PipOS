"""Explicit owner control for optional offline library packs."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .library import (DICTIONARY, DICTIONARY_ENABLE, ENCYCLOPEDIA, ENCYCLOPEDIA_ENABLE,
                      OEWN_LICENSE, OEWN_SOURCE, SIMPLE_LICENSE, SIMPLE_SOURCE, _dictionary, article)
from .storage import atomic_write, sync_directory

CMDLINE = Path("/proc/cmdline")
MODELS = Path("/models")
IMPORT_ROLES = {"import-dictionary": "library", "import-simple": "library_simple"}
IMPORT_LOCK = Path("/run/being-library-import.lock")
IMPORT_STAGE = ".being-library-import"
PACKS = {
    "library": (OEWN_SOURCE, OEWN_LICENSE, "oewn", "dictionary-enabled",
                "Open English WordNet contributors and Princeton University WordNet."),
    "library_simple": (SIMPLE_SOURCE, SIMPLE_LICENSE, "simple-en", "simple-en-enabled",
                       "Simple English Wikipedia contributors."),
}


def _signed_notice(base, files, name, limit):
    from . import model_pack

    size, digest = files.get(name, (0, ""))
    if not 0 < size <= limit:
        raise ValueError("missing or oversized signed library notice")
    raw = model_pack.metadata_bytes(base / name, limit)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("library notice differs from the signed description")
    return raw


def pack_information(mind_dir, role, *, models=MODELS, certificate=None):
    """Authenticate the inventory's metadata without opening the corpus."""
    from . import model_pack

    name, licence, folder, marker, credit = PACKS[role]
    base = Path(models) / "library" / folder
    info = {"name": name, "licence": licence, "snapshot": None, "credit": credit,
            "written_licence": None, "written_attribution": None,
            "state": "Not installed", "authenticated": False,
            "enabled": (Path(mind_dir) / "library" / marker).is_file()}
    if not base.is_dir():
        return info
    info["state"] = "Pack details unavailable"
    try:
        envelope = model_pack.metadata_bytes(base / model_pack.envelope_name(role),
                                             model_pack.MAX_DESCRIPTOR + 8192)
        pack = model_pack.authenticate(envelope, certificate or model_pack.certificate_default(),
                                       base / "pack.json", role)
        snapshot = pack.get("snapshot")
        if (pack.get("name") != name or pack.get("license") != licence or not isinstance(snapshot, str)
                or not 0 < len(snapshot) <= 96 or not snapshot.strip() or not snapshot.isprintable()):
            return info
        files = {filename: (size, digest) for filename, size, digest in model_pack.files_of(pack)}
        _signed_notice(base, files, "LICENSE", 64 * 1024)
        if role == "library_simple":
            attribution = _signed_notice(base, files, "ATTRIBUTION", 8192).decode("utf-8").strip()
            if not attribution:
                return info
            info["credit"] = attribution
            info["written_attribution"] = str(base / "ATTRIBUTION")
        info.update(snapshot=snapshot, authenticated=True, written_licence=str(base / "LICENSE"),
                    state="Use allowed" if info["enabled"] else "Use off")
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        pass
    return info


def enable(marker=DICTIONARY_ENABLE, dictionary=DICTIONARY):
    # Validate with a temporary consent token: authentication and metadata
    # must succeed before durable consent is recorded.
    marker = Path(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(".dictionary-enable-check")
    atomic_write(temporary, "check\n")
    try:
        db, status = _dictionary(dictionary, temporary)
        if db is not None:
            db.close()
        if status != "ready":
            raise ValueError(f"dictionary cannot be enabled: {status}")
        atomic_write(marker, "Open English WordNet dictionary enabled by owner\n")
        sync_directory(marker.parent)
    finally:
        temporary.unlink(missing_ok=True)


def disable(marker=DICTIONARY_ENABLE):
    marker = Path(marker)
    marker.unlink(missing_ok=True)
    if marker.parent.is_dir():
        sync_directory(marker.parent)


def enable_simple(marker=ENCYCLOPEDIA_ENABLE, encyclopedia=ENCYCLOPEDIA):
    marker = Path(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(".simple-en-enable-check")
    atomic_write(temporary, "check\n")
    try:
        status, _ = article("BeingOS pack validation", path=encyclopedia, enabled=temporary)
        if status != "ready":
            raise ValueError(f"Simple English library cannot be enabled: {status}")
        atomic_write(marker, "Simple English Wikipedia library enabled by owner\n")
        sync_directory(marker.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _models_bound(models=MODELS, cmdline=CMDLINE):
    try:
        from .install_binding import PARTUUIDS, installed_mapping, verify_partitions
        from .install_disks import node_number
        guids, active = installed_mapping(cmdline.read_text(encoding="ascii"))
        root = os.stat("/").st_dev
        verify_partitions(guids, active, f"{os.major(root)}:{os.minor(root)}")
        mounted = os.stat(models).st_dev
        number = f"{os.major(mounted)}:{os.minor(mounted)}"
        return Path(models).is_mount() and number == node_number(str(PARTUUIDS / guids[3]))
    except (OSError, ValueError):
        return False


def _remount(writable, mount=MODELS, run=subprocess.run):
    mode = "rw" if writable else "ro"
    run(["/bin/mount", "-o", f"remount,{mode}", str(mount)], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
    readonly = bool(os.statvfs(mount).f_flag & getattr(os, "ST_RDONLY", 1))
    if readonly == writable:
        raise OSError(f"models partition did not remount {mode}")


@contextmanager
def _import_lock(path=IMPORT_LOCK):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("library import lock is not a private root file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another library import is already running") from error
        yield
    finally:
        os.close(fd)


def _import_capacity(models, files, metadata_sizes, destination):
    """Estimate the allocation of a verified pack on the live filesystem."""
    filesystem = os.statvfs(models)
    block = filesystem.f_frsize
    # The role directory and its children, plus the staging root and its
    # library directory. Publishing may also create /models/library.
    directories = {Path(".")}
    for relative, _, _ in files:
        directories.update(Path(relative).parents)
    new_directories = len(directories) + 2 + int(not destination.parent.is_dir())
    sizes = (*metadata_sizes, *(size for _, size, _ in files))
    blocks_needed = sum((size + block - 1) // block for size in sizes) + new_directories
    inodes_needed = len(sizes) + new_directories
    free = filesystem.f_bavail * block
    occupied = (filesystem.f_blocks - filesystem.f_bfree) * block
    required = blocks_needed * block
    remaining = max(0, free - required)
    return {
        "available_bytes": free,
        "occupied_bytes": occupied,
        "estimated_required_bytes": required,
        "estimated_remaining_bytes": remaining,
        "available_inodes": filesystem.f_favail,
        "required_inodes": inodes_needed,
        "fits": required <= free and inodes_needed <= filesystem.f_favail,
        "estimated_room_for_current_contents_replacement": required + occupied <= free,
    }


def _staging_exists(models, stage):
    try:
        info = stage.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
            or info.st_dev != models.stat().st_dev or stage.is_mount()):
        raise ValueError("library import staging path is not a private directory on the installed models partition")
    return True


def _require_capacity(capacity):
    if not capacity["fits"]:
        raise ValueError(
            f"insufficient models partition space: {capacity['available_bytes']} bytes and "
            f"{capacity['available_inodes']} inodes available, need about "
            f"{capacity['estimated_required_bytes']} bytes and {capacity['required_inodes']} inodes"
        )


def _sync_staged_pack(root, paths):
    directories = {root}
    for name in paths:
        path = root / name
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        directories.update(root / parent for parent in Path(name).parents)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        sync_directory(directory)


def import_pack(source, role, *, models=MODELS, certificate=None, check=False):
    """Authenticate one optional pack and check or import it on an installed SSD.

    `source` is the role directory produced by the offline packager. Nothing
    scans removable media or enables the pack. Existing roles are not replaced:
    an update/removal lifecycle needs its own interruption and rollback design.
    """
    with _import_lock():
        return _import_pack(source, role, models=models, certificate=certificate, check=check)


def _import_pack(source, role, *, models=MODELS, certificate=None, check=False):
    from . import model_pack

    if os.geteuid() != 0 or not _models_bound(models):
        raise ValueError("optional library import is available only as root on an installed system")
    models = Path(models)
    if not os.statvfs(models).f_flag & getattr(os, "ST_RDONLY", 1):
        raise ValueError("installed models partition was not read-only before import")
    if role not in ("library", "library_simple"):
        raise ValueError("unsupported optional library role")
    source = Path(source).resolve()
    expected = Path(model_pack.ROLE_DIRS[role])
    if tuple(source.parts[-len(expected.parts):]) != expected.parts:
        raise ValueError(f"source must end in {expected}")
    donor = source.parents[len(expected.parts) - 1]
    descriptor = source / "pack.json"
    envelope = source / model_pack.envelope_name(role)
    certificate = certificate or model_pack.certificate_default()
    # Full authentication and hashes happen before /models is made writable.
    signed, pack = model_pack.verify_envelope(donor, envelope, certificate, descriptor, role,
                                              verdicts=None, build_id=None)
    destination = model_pack.root(role, models)
    if destination.exists():
        raise ValueError(f"{role} is already installed; this import route never replaces a pack")
    files = model_pack.files_of(pack)
    metadata_sizes = (len(signed), descriptor.stat().st_size)
    capacity = _import_capacity(models, files, metadata_sizes, destination)
    stage = models / IMPORT_STAGE
    unfinished = _staging_exists(models, stage)
    if check:
        return {"role": role, "unfinished_stage": unfinished, **capacity}
    if not unfinished:
        _require_capacity(capacity)

    parent = destination.parent
    ours = False
    try:
        _remount(True, models)
        if unfinished:
            shutil.rmtree(stage)
            _require_capacity(_import_capacity(models, files, metadata_sizes, destination))
        parent.mkdir(parents=True, exist_ok=True)
        stage.mkdir(mode=0o700)
        ours = True
        temporary = stage / expected
        temporary.mkdir(parents=True)
        for relative, _size, _digest in files:
            source_file = source / relative
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target, follow_symlinks=False)
            if target.is_symlink():
                raise ValueError(f"library source changed during copy: {relative}")
            target.chmod(0o644)
        shutil.copyfile(descriptor, temporary / "pack.json", follow_symlinks=False)
        shutil.copyfile(envelope, temporary / model_pack.envelope_name(role), follow_symlinks=False)
        # Prove the copied bytes, not only the donor bytes, before publication.
        model_pack.verify_envelope(stage, temporary / model_pack.envelope_name(role), certificate,
                                   temporary / "pack.json", role, verdicts=None, build_id=None)
        _sync_staged_pack(temporary, [*(relative for relative, _, _ in files), descriptor.name, envelope.name])
        os.replace(temporary, destination)
        sync_directory(parent)
        sync_directory(models)
        model_pack.forget_trust()
    finally:
        # Once cleanup begins, finish restoring the mount before delivering a
        # terminal interruption that arrives during that cleanup.
        signals = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM))
        try:
            try:
                if ours:
                    shutil.rmtree(stage)
            finally:
                _remount(False, models)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, signals)


def _cancel_import(_number, _frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise InterruptedError("library import was cancelled")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable", "enable-simple", "disable-simple",
                                            "import-dictionary", "import-simple", "status", "credits"))
    parser.add_argument("--source", type=Path, help="explicit signed role directory from the offline packager")
    parser.add_argument("--certificate", type=Path, help="signer certificate (default: installed release certificate)")
    parser.add_argument("--check", action="store_true", help="authenticate an import source and report its fit without copying")
    args = parser.parse_args(argv)
    if args.check and args.action not in IMPORT_ROLES:
        parser.error("--check requires an import action")
    if args.action in IMPORT_ROLES:
        if args.source is None:
            parser.error("--source is required for import")
        handler = signal.signal(signal.SIGTERM, _cancel_import)
        try:
            capacity = import_pack(args.source, IMPORT_ROLES[args.action], certificate=args.certificate, check=args.check)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            operation = "check failed" if args.check else "import failed"
            parser.exit(1, f"Library pack {operation}: {error}\n")
        finally:
            signal.signal(signal.SIGTERM, handler)
        if args.check:
            print(json.dumps(capacity, indent=2))
            return 0 if capacity["fits"] else 1
        print("Signed library pack imported. It remains disabled until the owner enables it separately.")
    elif args.action == "enable":
        enable()
        print("Open English WordNet dictionary enabled.")
    elif args.action == "disable":
        disable()
        print("Dictionary disabled; its signed pack was not deleted.")
    elif args.action == "enable-simple":
        enable_simple()
        print("Simple English Wikipedia library enabled.")
    elif args.action == "disable-simple":
        disable(ENCYCLOPEDIA_ENABLE)
        print("Simple English Wikipedia library disabled; its signed pack was not deleted.")
    elif args.action == "credits":
        print(f"{OEWN_SOURCE}; {OEWN_LICENSE}. Written licence: /models/library/oewn/LICENSE")
        print(f"{SIMPLE_SOURCE}; {SIMPLE_LICENSE}. Written licence and attribution: /models/library/simple-en/")
    else:
        dictionary = "enabled" if DICTIONARY_ENABLE.is_file() else "disabled"
        simple = "enabled" if ENCYCLOPEDIA_ENABLE.is_file() else "disabled"
        print(f"dictionary {dictionary}; Simple English {simple}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
