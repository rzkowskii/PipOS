"""Owner-selected encrypted snapshots and deletion-aware offline restore.

A snapshot holds who the being is, how it is set up, its archive, the
owner's lists (lists.db) and its action log (counts only). Once the owner has
confirmed a destination in Care, backups by voice and the night shift go only
to that filesystem UUID; older installations retain their labelled-stick
route until the next explicit menu backup enrolls one. All routes use the same
folder and keep the newest `KEEP` sets there. Each backup made here is recorded in
last-backup.json (a time and how it was made, nothing else).
"""

import argparse
import asyncio
import ctypes
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .backup_crypto import BackupCryptoError, decrypt, encrypt, generate_key
from .initiative import POLICY_PATH as INITIATIVE_PATH, load_policy as load_initiative, save_policy as save_initiative
from .memory import Memory
from .profile import FACTORY_CONFIG, PROFILE_FILE, load_profile, save_profile
from .retention import POLICY_FILE, MemoryPolicy, load_policy, read_policy, save_policy
from .storage import atomic_write, private_directory, sync_directory


VERSION = 1
LIFE_FILE = "life.json"
DB_FILE = "mind.db"
SKILLS_FILE = "skills.json"
INITIATIVE_FILE = str(INITIATIVE_PATH)
CHECKPOINT_KIND = "pipos-deletion-checkpoint"
MANIFEST = "manifest.json"
KEEP_CATALOG_FILE = "keep-catalog.json"
KEEP_CATALOG_LIMIT = 32 * 1024 * 1024
RESTORE_MARKER = ".being-restore-in-progress"
RESTORE_STAGE = ".being-restore-stage"
UPDATE_CHECKPOINT_DIR = "update-checkpoint"
UPDATE_CHECKPOINT_FILE = "before-update.age"
# Reminders, Wi-Fi, lab access, the weather city, the enrolled backup-media
# UUID, caches and core profiles
# (core.md and core-profile.md) are left out: a backup holds who the being is, how it is set up,
# and its archive, and forgetting must still reach everything restored. A
# restored being explicitly enrolls its writable destination again; restore
# media is never silently made the future backup target.
SETTINGS = (LIFE_FILE, PROFILE_FILE, SKILLS_FILE, POLICY_FILE, INITIATIVE_FILE)
# The owner's lists and notes (a database of its own) and the action log
# (kinds and counts only). Both are cleared by "forget everything", which
# counts itself in ERASURES_FILE, so an older snapshot never brings them back.
LISTS_FILE = "lists.db"
ACTIONS_FILE = "log/actions.jsonl"      # in the mind directory
ACTIONS_ARCHIVED = "actions.jsonl"      # in the snapshot
ERASURES_FILE = "erasures.json"
SNAPSHOT_FILES = {*SETTINGS, DB_FILE, LISTS_FILE, ACTIONS_ARCHIVED, ERASURES_FILE, KEEP_CATALOG_FILE}
DATABASES = {DB_FILE, LISTS_FILE}
ACTIONS_LIMIT = 3 * 1024 * 1024
METADATA_LIMIT = 64 * 1024
RESTORE_ENCRYPTED_LIMIT = 16 * 1024**3
DELETION_PROOF_LIMIT = 64 * 1024**2
RESTORE_IDENTITY_LIMIT = 1024 * 1024
RESTORE_ARTIFACTS = frozenset(("snapshot", "identity", "deletions", "checkpoint"))
RESTORE_RUN_RESERVE = 768 * 1024**2
BACKUP_MEDIA_RESERVE = 16 * 1024**2
DEVICE_BACKUP_UNIT = "being-device-backup"
RESTORE_DEVICE_UNIT = "being-restore-device"
DIGEST = re.compile(r"[0-9a-f]{64}")
LIFE_ID = re.compile(r"[0-9a-f]{32}")
ZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")


def json_text(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def read_metadata(path):
    with regular_file(path) as source:
        raw = source.read(METADATA_LIMIT + 1)
    if len(raw) > METADATA_LIMIT:
        raise ValueError(f"{path.name} is too large.")
    return json.loads(raw)


@contextmanager
def regular_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Expected a regular file at {path}.")
        yield source


def working_directory():
    # On the appliance /run is tmpfs. Keep decrypted and unsanitized data there.
    runtime = Path("/run")
    parent = runtime if sys.platform == "linux" and Path("/etc/being/env").is_file() else None
    return tempfile.TemporaryDirectory(dir=parent, prefix="being-backup-")


def _restore_artifact_limit(role, name):
    """Size cap for one selected, still-encrypted restore input."""
    if role not in RESTORE_ARTIFACTS:
        raise ValueError("That is not a permitted restore artifact.")
    lower = name.lower()
    if role == "snapshot" and lower.endswith(".age") and not lower.endswith("-deletions.age"):
        return RESTORE_ENCRYPTED_LIMIT
    if role == "deletions" and lower.endswith("-deletions.age"):
        return DELETION_PROOF_LIMIT
    if role == "identity" and lower.endswith((".age", ".agekey", ".key")):
        return RESTORE_IDENTITY_LIMIT
    if role == "checkpoint" and lower.endswith(".txt"):
        return METADATA_LIMIT
    raise ValueError(f"The selected {role} file has an unexpected name.")


def _mem_available(path=Path("/proc/meminfo")):
    try:
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                fields = line.split()
                return int(fields[1]) * 1024 if len(fields) >= 3 and fields[2] == "kB" else None
    except (OSError, ValueError, IndexError):
        pass
    return None


def _restore_stage_budget(destination, incoming):
    """Refuse an appliance tmpfs copy before it can crowd boot services out."""
    run = Path("/run")
    try:
        on_runtime = Path(destination).resolve().is_relative_to(run.resolve())
    except (OSError, ValueError):
        on_runtime = False
    # Developer hosts use an ordinary temporary filesystem. The appliance is
    # identified the same way as working_directory(), and must have both real
    # tmpfs capacity and reclaimable memory information before staging.
    if not on_runtime or not Path("/etc/being/env").is_file():
        return
    memory = _mem_available()
    if memory is None:
        raise ValueError("I couldn't verify enough safe memory to stage that backup.")
    filesystem = os.statvfs(run)
    free = filesystem.f_bavail * filesystem.f_frsize
    already = sum(path.stat().st_size for path in Path(destination).glob("*.source") if path.is_file())
    total = already + incoming
    # `incoming` is the new opaque copy. Twice `total` remains as headroom for
    # age's plaintext temporary file plus bounded extracted/prepared files.
    # The fixed reserve keeps the face, brainstem, voice and other boot
    # services out of tmpfs pressure.
    required = incoming + 2 * total + RESTORE_RUN_RESERVE
    if min(free, memory) < required:
        raise ValueError(
            "That encrypted backup is too large to stage safely in memory on this being. "
            "Use a smaller backup or restore it on a being with more memory."
        )


def _backup_stage_budget(mind_dir):
    """Conservative appliance preflight before backup work consumes /run."""
    estimated = 0
    for name in (*SETTINGS, DB_FILE, f"{DB_FILE}-wal", LISTS_FILE, f"{LISTS_FILE}-wal",
                 ACTIONS_FILE, ERASURES_FILE, "keep/index.sqlite", "keep/index.sqlite-wal"):
        try:
            info = (Path(mind_dir) / name).stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            estimated += info.st_size
    memory = _mem_available()
    filesystem = os.statvfs("/run")
    free = filesystem.f_bavail * filesystem.f_frsize
    # Snapshot DB, tar plaintext and encrypted output can overlap, with one
    # further source-sized allowance for SQLite compaction.  Keep's JSON
    # catalog and current deletion/naming proof are derived from SQLite and
    # can be substantially larger than its pages when names use worst-case
    # UTF-8. Account for their format caps independently; they can each
    # overlap a plaintext and encrypted/staged copy in the installed flow.
    derived = 2 * (KEEP_CATALOG_LIMIT + DELETION_PROOF_LIMIT)
    required = 4 * max(estimated, METADATA_LIMIT) + derived + RESTORE_RUN_RESERVE
    if memory is None or min(memory, free) < required:
        raise ValueError("This being does not have enough safe working memory to make that backup right now.")


def publish_backup_artifact(source, folder, name, *, device=None, expected_uuid=None, isolated=True):
    """Publish one opaque staged artifact at the existing durability boundary."""
    if (isolated and device is not None and Path("/etc/being/env").is_file()
            and shutil.which("systemd-run") and Path("/usr/sbin/being-backup").is_file()):
        command = [
            "systemd-run", "--unit=being-backup-publish", "--quiet", "--wait", "--collect",
            "--service-type=exec", "--property=Type=exec", "--property=PrivateNetwork=yes",
            "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
            "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=",
            "--property=DevicePolicy=closed", "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=RestrictNamespaces=yes", "--property=RestrictSUIDSGID=yes",
            "--property=LockPersonality=yes", "--property=SystemCallArchitectures=native",
            "--property=ProtectKernelTunables=yes", "--property=ProtectKernelModules=yes",
            "--property=ProtectKernelLogs=yes", "--property=ProtectControlGroups=yes",
            "--property=ProtectClock=yes",
            "--property=TasksMax=8", "--property=MemorySwapMax=0", "--property=UMask=0077",
            "--property=RuntimeMaxSec=15min", "--property=TimeoutStopSec=10s",
            f"--property=ReadOnlyPaths={Path(source).parent}", f"--property=ReadWritePaths={folder}",
            "/usr/sbin/being-backup", "publish-artifact", "--source", str(source),
            "--folder", str(folder), "--name", name, "--device", device, "--uuid", expected_uuid,
        ]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, timeout=900, check=False)
        except subprocess.TimeoutExpired:
            # Killing systemd-run does not necessarily stop the transient
            # service. Explicitly stop our fixed unit so no hidden writer can
            # outlive the backup lock or race the caller's unmount.
            try:
                stopped = subprocess.run(("systemctl", "stop", "being-backup-publish.service"),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=15, check=False)
                if stopped.returncode:
                    subprocess.run(("systemctl", "kill", "--kill-whom=all", "--signal=KILL",
                                    "being-backup-publish.service"), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                try:
                    subprocess.run(("systemctl", "kill", "--kill-whom=all", "--signal=KILL",
                                    "being-backup-publish.service"), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass
            raise ValueError("I stopped a backup file transfer that took too long.") from None
        if result.returncode:
            detail = (result.stderr or "").strip().splitlines()
            raise ValueError((detail[-1].removeprefix("being-backup: ") if detail else
                              "The backup file could not be written safely.")[:300])
        return
    if device is not None and (media_identity(device) != expected_uuid or not mounted_from(folder, device)):
        raise ValueError("The backup stick changed while a backup file was being written.")


def _drop_all_capabilities():
    """Keep uid 0 for a mode-0700 mount, but no Linux capability or escalation."""
    libc = ctypes.CDLL(None, use_errno=True)
    for capability in range(64):
        if libc.prctl(24, capability, 0, 0, 0) != 0 and ctypes.get_errno() not in (0, 22):
            raise OSError(ctypes.get_errno(), "could not drop capability bounding set")

    class CapHeader(ctypes.Structure):
        _fields_ = (("version", ctypes.c_uint32), ("pid", ctypes.c_int))

    class CapData(ctypes.Structure):
        _fields_ = (("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                    ("inheritable", ctypes.c_uint32))

    if libc.capset(ctypes.byref(CapHeader(0x20080522, 0)), ctypes.byref((CapData * 2)())) != 0:
        raise OSError(ctypes.get_errno(), "could not clear capabilities")
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not set no-new-privileges")


def device_backup_operation(device, expected_uuid, *, source=None, name=None, prune_keep=None,
                            newest=None, clock_trusted=None):
    """Service side: private mount, capability-free opaque operation, unmount."""
    root = STICK_DIR
    mounted = False
    try:
        root = mount_stick(device, root)
        mounted = True
        if media_identity(device) != expected_uuid or not mounted_from(root, device):
            raise ValueError("The backup stick changed while it was being mounted.")
        folder = root / BACKUP_FOLDER
        folder.mkdir(mode=0o700, exist_ok=True)
        pid = os.fork()
        if pid == 0:
            try:
                _drop_all_capabilities()
                if source is not None:
                    publish_backup_artifact(source, folder, name, device=device,
                                            expected_uuid=expected_uuid, isolated=False)
                else:
                    prune(folder, prune_keep, newest=newest, clock_trusted=clock_trusted)
                os._exit(0)
            except BaseException:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        if status != 0:
            raise ValueError("The confined backup media operation failed.")
        if media_identity(device) != expected_uuid or not mounted_from(root, device):
            raise ValueError("The backup stick changed during the backup operation.")
    finally:
        if mounted:
            unmount_stick(root)


def _discover_restore_files(root):
    found = []
    stack = [(Path(root), 0)]
    while stack and len(found) < 60:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(
                        (".age", ".agekey", ".key", ".txt")):
                    relative = str(Path(entry.path).relative_to(root))
                    label = "".join(character if character.isprintable() else "?" for character in relative)
                    found.append({"id": relative, "label": label[:240]})
                    if len(found) >= 60:
                        break
                elif depth < 3 and entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
            except (OSError, ValueError):
                continue
    return found


def restore_device_operation(device, expected_uuid, *, selected=None, destination=None):
    """Private read-only mount for discovery or exact selected-file staging."""
    from . import local_menu

    root = local_menu._mount_media(device, local_menu.RECOVERY_MOUNTS,
                                   "FAT, exFAT and ext4 backup media")
    try:
        if media_identity(device) != expected_uuid or not mounted_from(root, device):
            raise ValueError("The restore stick changed while it was being mounted.")
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                _drop_all_capabilities()
                if selected is None:
                    answer = {"files": _discover_restore_files(root)}
                else:
                    requests = [(role, Path(root) / relative) for role, relative in selected]
                    staged = stage_restore_artifacts(root, device, expected_uuid, requests, destination)
                    answer = {"staged": {role: str(path) for role, path in staged.items()}}
                output = memoryview(json_text(answer).encode("utf-8"))
                while output:
                    written = os.write(write_fd, output)
                    if written <= 0:
                        raise OSError("could not return restore-media result")
                    output = output[written:]
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        data = bytearray()
        while len(data) <= 64 * 1024:
            block = os.read(read_fd, 4096)
            if not block:
                break
            data.extend(block)
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
        if status != 0 or len(data) > 64 * 1024:
            raise ValueError("The confined restore-media operation failed.")
        answer = json.loads(data)
        if media_identity(device) != expected_uuid or not mounted_from(root, device):
            raise ValueError("The restore stick changed during the operation.")
        return answer
    finally:
        local_menu.unmount_media()


def _device_backup_command(device, expected_uuid, *, source=None, name=None, prune_keep=None,
                           newest=None, clock_trusted=None):
    command = [
        "systemd-run", f"--unit={DEVICE_BACKUP_UNIT}", "--quiet", "--wait", "--collect",
        "--service-type=exec", "--property=Type=exec", "--property=PrivateMounts=yes",
        "--property=PrivateNetwork=yes", "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=CAP_SYS_ADMIN",
        "--property=AmbientCapabilities=CAP_SYS_ADMIN", "--property=DevicePolicy=closed",
        f"--property=DeviceAllow={device} rw", "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=TasksMax=8", "--property=MemorySwapMax=0", "--property=RuntimeMaxSec=15min",
        "--property=TimeoutStopSec=10s", f"--property=ReadWritePaths={STICK_DIR}",
    ]
    if source is not None:
        command.append(f"--property=ReadOnlyPaths={Path(source).parent}")
    command.extend(("/usr/sbin/being-backup", "device-operation", "--device", device, "--uuid", expected_uuid))
    if source is not None:
        command.extend(("--source", str(source), "--name", name))
    else:
        command.extend(("--prune-keep", str(prune_keep), "--newest", newest or ""))
        if clock_trusted:
            command.append("--clock-trusted")
    return command


def _run_device_backup(command):
    # Only the empty mountpoint is host-visible; the transient service's
    # PrivateMounts namespace owns every actual mount and removes this again.
    STICK_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, timeout=910, check=False)
    except subprocess.TimeoutExpired:
        try:
            stopped = subprocess.run(("systemctl", "stop", f"{DEVICE_BACKUP_UNIT}.service"),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=15, check=False)
            if stopped.returncode:
                subprocess.run(("systemctl", "kill", "--kill-whom=all", "--signal=KILL",
                                f"{DEVICE_BACKUP_UNIT}.service"), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            try:
                subprocess.run(("systemctl", "kill", "--kill-whom=all", "--signal=KILL",
                                f"{DEVICE_BACKUP_UNIT}.service"), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        raise ValueError("I stopped a backup-media operation that took too long.") from None
    if result.returncode:
        raise ValueError("The private backup-media operation failed.")


def backup_to_device(mind_dir, device, expected_uuid, recipient, *, now=None, keep=None,
                     clock_trusted=None, prune_after=True):
    """Installed voice/night path: no mount enters the Mind process."""
    _backup_stage_budget(mind_dir)
    keep = KEEP if keep is None else keep
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now))
    names = (f"pip-{stamp}.age", f"pip-{stamp}-deletions.age", f"pip-{stamp}-checkpoint.txt")
    with working_directory() as directory:
        stage = Path(directory)
        snapshot, proof, record = (stage / name for name in names)
        created = create(mind_dir, snapshot, recipient)
        _run_device_backup(_device_backup_command(device, expected_uuid, source=snapshot, name=names[0]))
        exported = export_deletions(mind_dir, proof, recipient)
        _run_device_backup(_device_backup_command(device, expected_uuid, source=proof, name=names[1]))
        if exported["life_id"] != created["life_id"]:
            raise ValueError("The being changed while it was backed up.")
        atomic_write(record, checkpoint_text(created["life_id"], exported["checkpoint"], created["created"]),
                     mode=0o600)
        _run_device_backup(_device_backup_command(device, expected_uuid, source=record, name=names[2]))
        if prune_after:
            _run_device_backup(_device_backup_command(device, expected_uuid, prune_keep=keep,
                                                      newest=stamp, clock_trusted=clock_trusted))
    folder = STICK_DIR / BACKUP_FOLDER
    return {"snapshot": folder / names[0], "deletions": folder / names[1],
            "checkpoint_file": folder / names[2], "checkpoint": exported["checkpoint"],
            "life_id": created["life_id"], "pruned": None}
    if SET.fullmatch(name) is None and KEY_ARTIFACT.fullmatch(name) is None:
        raise ValueError("Invalid backup artifact name.")
    source = Path(source)
    size = source.stat().st_size
    folder = Path(folder)
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    opened_device = os.fstat(directory).st_dev
    try:
        wanted = os.stat(device, follow_symlinks=True).st_rdev if device is not None else opened_device
    except OSError:
        os.close(directory)
        raise ValueError("The backup stick changed while a backup file was being written.") from None
    if opened_device != wanted:
        os.close(directory)
        raise ValueError("The backup stick changed while a backup file was being written.")
    space = os.fstatvfs(directory)
    if space.f_bavail * space.f_frsize < size + BACKUP_MEDIA_RESERVE:
        os.close(directory)
        raise ValueError("The backup stick does not have enough free space for this backup.")
    if os.path.lexists(folder / name):
        os.close(directory)
        raise ValueError("A backup with this time already exists. Wait a second and try again.")
    pending = f".{name}.being-pending"
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
    except BaseException:
        os.close(directory)
        raise
    try:
        with regular_file(source) as data, os.fdopen(descriptor, "wb") as output:
            if stat.S_IMODE(os.fstat(output.fileno()).st_mode) & 0o077:
                raise ValueError("The backup destination did not preserve private file permissions.")
            shutil.copyfileobj(data, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.rename(pending, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        readback = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        with regular_file(source) as original, os.fdopen(readback, "rb") as written:
            if (not stat.S_ISREG(os.fstat(readback).st_mode)
                    or not hmac.compare_digest(hashlib.file_digest(original, "sha256").digest(),
                                               hashlib.file_digest(written, "sha256").digest())):
                raise ValueError("The backup file did not read back correctly from the stick.")
        if os.fstat(directory).st_dev != wanted or (device is not None and media_identity(device) != expected_uuid):
            raise ValueError("The backup stick changed while a backup file was being written.")
    finally:
        try:
            os.unlink(pending, dir_fd=directory)
        except OSError:
            pass
        os.close(directory)
    if device is not None and (media_identity(device) != expected_uuid or not mounted_from(folder, device)):
        raise ValueError("The backup stick changed while a backup file was being written.")


def stage_restore_artifacts(root, device, expected_uuid, selected, destination):
    """Copy selected opaque restore inputs off a read-only removable mount.

    No decryptor, JSON parser, or archive reader runs here. Each source is an
    allowlisted regular file opened once without following a final symlink;
    the stable descriptor is copied with a role-specific bound. The caller
    must unmount before consuming the returned private staging paths. Passing
    no device is reserved for explicitly typed, externally mounted rescue
    paths; descriptor and size checks still apply, but mount ownership does not.
    """
    root = Path(root).resolve(strict=True)
    destination = Path(destination)
    if len(selected) > 4 or len({role for role, _ in selected}) != len(selected):
        raise ValueError("Choose each restore artifact once.")
    if device is not None and (media_identity(device) != expected_uuid or not mounted_from(root, device)):
        raise ValueError("The backup stick changed before its files could be staged.")
    root_device = root.stat().st_dev
    staged = {}
    for role, path in selected:
        path = Path(path)
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ValueError("Restore files must be on the selected backup stick.") from None
        if not relative.parts or len(relative.parts) > 5 or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError("That restore path is not permitted.")
        limit = _restore_artifact_limit(role, path.name)
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_dev != root_device
                    or before.st_size <= 0 or before.st_size > limit):
                raise ValueError(f"The selected {role} file is not a bounded regular file.")
            _restore_stage_budget(destination, before.st_size)
            target = destination / f"{role}.source"
            output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                copied = 0
                while copied < before.st_size:
                    block = os.read(descriptor, min(1024 * 1024, before.st_size - copied))
                    if not block:
                        raise ValueError(f"The selected {role} file ended while it was being staged.")
                    view = memoryview(block)
                    while view:
                        written = os.write(output, view)
                        if written <= 0:
                            raise OSError("could not stage restore input")
                        view = view[written:]
                    copied += len(block)
                if os.read(descriptor, 1):
                    raise ValueError(f"The selected {role} file grew while it was being staged.")
                os.fsync(output)
            finally:
                os.close(output)
                if target.exists() and target.stat().st_size != before.st_size:
                    target.unlink(missing_ok=True)
            after = os.fstat(descriptor)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                target.unlink(missing_ok=True)
                raise ValueError(f"The selected {role} file changed while it was being staged.")
            staged[role] = target
        finally:
            os.close(descriptor)
    if device is not None and (media_identity(device) != expected_uuid or not mounted_from(root, device)):
        for target in staged.values():
            target.unlink(missing_ok=True)
        raise ValueError("The backup stick changed while its files were being staged.")
    sync_directory(destination)
    return staged


def source_directory(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or (root / RESTORE_MARKER).exists():
        raise ValueError("Select an available PipOS mind directory.")
    return root


def life_record(root):
    value = read_metadata(root / LIFE_FILE)
    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
            or not LIFE_ID.fullmatch(value["id"]) or type(value.get("born")) is not int
            or value["born"] < 0):
        raise ValueError("The life record is invalid.")
    return value


def copy_eligible_settings(source, target, life):
    atomic_write(target / LIFE_FILE, json_text(life))
    profile = source / PROFILE_FILE
    if profile.exists() or profile.is_symlink():
        read_metadata(profile)
    save_profile(target, load_profile(FACTORY_CONFIG, source))
    initiative = source / INITIATIVE_FILE
    if initiative.exists() or initiative.is_symlink():
        # Strict: a damaged announcement policy fails the backup loudly.
        save_initiative(target, load_initiative(source, strict=True))
    try:
        settings = read_metadata(source / SKILLS_FILE)
    except FileNotFoundError:
        return
    zone = settings.get("time_zone", "") if isinstance(settings, dict) else ""
    # Preserve valid zone keys if the receiving image has a smaller tzdata set.
    if not isinstance(zone, str) or len(zone) > 100 or (zone and not ZONE.fullmatch(zone)):
        raise ValueError("The saved time zone is invalid.")
    atomic_write(target / SKILLS_FILE, json_text({"time_zone": zone}))


def database_snapshot(root, target, policy, *, read_only=False):
    path = root / DB_FILE
    if policy.mode == "session-only" and not os.path.lexists(path):
        with closing(Memory(target.parent)):
            return
    with regular_file(path):
        pass
    # A normal export establishes the deletion lineage on an older archive.
    # An update can also repair a degraded system, so its checkpoint reads
    # the active database without changing its journal, schema, or metadata.
    source = (sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
              if read_only else Memory(root))
    with closing(source), closing(sqlite3.connect(target)) as destination:
        deadline = time.monotonic() + 120

        def check_progress(status, remaining, total):
            if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
                raise TimeoutError("The SQLite snapshot did not complete within two minutes.")

        database = source if read_only else source.db
        database.backup(destination, pages=512, progress=check_progress, sleep=0.1)


def sqlite_copy(source, target):
    """A consistent copy of a live SQLite file, checked, in rollback mode."""
    with regular_file(source):
        pass
    with closing(sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as live, \
            closing(sqlite3.connect(target)) as copy:
        live.backup(copy, pages=256, sleep=0.05)
        copy.execute("PRAGMA journal_mode = DELETE")
        copy.execute("VACUUM")
        if copy.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError(f"{Path(source).name} failed SQLite's integrity check.")
    os.chmod(target, 0o600)


def actions_copy(source, target):
    """Only valid {ts, kind, count} lines are copied: no text can ride along."""
    from . import action_log

    lines = [json.dumps(entry, separators=(",", ":")) + "\n" for entry in action_log.entries(source.parent.parent)]
    if lines:
        atomic_write(target, "".join(lines), mode=0o600)


def extras_snapshot(root, stage, policy, count=None):
    """The lists and the action log, beside the archive.

    Lists are things the owner said to keep, so a memory that keeps nothing
    (session-only) backs none up. The action log holds no words at all.

    `count` is the erase count read before anything was copied (create()
    passes it). A "forget everything" that lands during the copies then
    leaves this snapshot counting fewer erasures than its proof, so a restore
    drops these extras instead of bringing forgotten lists back.
    """
    if count is None:
        count = erasures(root)
    if policy.mode != "session-only" and (root / LISTS_FILE).exists():
        sqlite_copy(root / LISTS_FILE, stage / LISTS_FILE)
    if (root / ACTIONS_FILE).exists():
        actions_copy(root / ACTIONS_FILE, stage / ACTIONS_ARCHIVED)
    if count:
        write_erasures(stage, count)


def erasures(mind_dir):
    """How many times "forget everything" has run in this life (0 if never)."""
    try:
        value = read_metadata(Path(mind_dir) / ERASURES_FILE)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        # Unreadable: treat as "something was erased", so nothing old returns.
        return 1 << 30
    count = value.get("count") if isinstance(value, dict) else None
    return count if type(count) is int and count >= 0 else 1 << 30


def write_erasures(mind_dir, count):
    atomic_write(Path(mind_dir) / ERASURES_FILE, json_text({"version": VERSION, "count": count}), mode=0o600)


def note_erase(mind_dir):
    """Called by "forget everything": older snapshots must not restore lists or the log."""
    write_erasures(mind_dir, min(erasures(mind_dir), (1 << 30) - 1) + 1)


def compact_archive(root, policy, proof=None, *, apply_expiry=False):
    with closing(Memory(root)) as memory:
        if proof is not None:
            memory.apply_deletion_ledger(proof)
        # Expiry deletes for good; a clock that cannot be believed expires nothing.
        sure, _ = memory.clock()
        cutoff = policy.cutoff(sure) if sure is not None else None
        if cutoff is not None:
            memory.expire_before(cutoff, snapshot=not apply_expiry)
        # Recreate search tables from current rows before compacting free pages.
        with memory.db:
            if policy.mode != "persistent":
                # Export filtering changes this copy's eligibility, while the
                # deletion proof must still extend its original ledger prefix.
                memory.db.execute("BEGIN IMMEDIATE")
                memory._begin_deletion_batch()
                memory.db.execute("DELETE FROM episodes")
                if policy.mode == "session-only":
                    memory.db.execute("DELETE FROM facts")
                memory._finish_deletion_batch(replayed=())
            memory.db.execute("DELETE FROM dream_journal")
            memory.db.execute("DELETE FROM dream_checkpoints")
            for table in ("facts_fts", "episodes_fts"):
                memory.db.execute(f"INSERT INTO {table} ({table}) VALUES ('rebuild')")
        memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        memory.db.execute("PRAGMA journal_mode = DELETE")
        memory.db.execute("VACUUM")
        if memory.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("The memory database failed SQLite's integrity check.")


def checkpoint_id(proof):
    return hashlib.sha256(json_text(proof).encode("utf-8")).hexdigest()


def deletion_checkpoint(root):
    root = source_directory(root)
    life = life_record(root)
    policy = read_policy(root)
    if policy.mode == "session-only" and not (root / DB_FILE).exists():
        raise ValueError("This life has no durable archive to checkpoint.")
    with regular_file(root / DB_FILE):
        pass
    with closing(Memory(root)) as memory:
        ledger = memory.deletion_ledger()
    result = {"kind": "deletions", "version": VERSION, "life_id": life["id"], "ledger": ledger,
              "policy": policy.to_dict(), "erasures": erasures(root)}
    try:
        from . import keep

        keep_erasures = keep.export_erasures(root)
    except (ImportError, AttributeError):
        keep_erasures = None
    if keep_erasures is not None:
        result["keep_erasures"] = keep_erasures
    return result


def write_encrypted(output, recipient, source, *, outside):
    output = Path(output)
    parent = output.parent.resolve(strict=True)
    if parent == outside or parent.is_relative_to(outside):
        raise ValueError("Choose a backup destination outside the source mind directory.")
    _publish_encrypted(parent / output.name, recipient, source)


def _publish_encrypted(output, recipient, source):
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError("Choose a regular output file.")
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as dst:
        pending = Path(dst.name)
        try:
            source.seek(0)
            encrypt(recipient, source, dst)
            dst.flush()
            os.fsync(dst.fileno())
            os.replace(pending, output)
            sync_directory(output.parent)
        finally:
            pending.unlink(missing_ok=True)


def create(mind_dir, output, recipient):
    root = source_directory(mind_dir)
    return _create_snapshot(root, lambda plaintext: write_encrypted(output, recipient, plaintext, outside=root))


def create_update_checkpoint(mind_dir, recipient):
    """Replace the one on-device encrypted checkpoint after a complete export."""
    root = source_directory(mind_dir)
    folder = root / UPDATE_CHECKPOINT_DIR
    with backup_lock():
        private_directory(folder)
        sync_directory(root)
        output = folder / UPDATE_CHECKPOINT_FILE
        return _create_snapshot(root, lambda plaintext: _publish_encrypted(output, recipient, plaintext),
                                read_only=True)


def _create_snapshot(root, publish, *, read_only=False):
    if Path("/etc/being/env").is_file():
        # `create` is also a public packaged CLI entry point; do not rely on
        # Care/voice/night having performed their outer workflow preflight.
        _backup_stage_budget(root)
    life = life_record(root)
    # A damaged policy must fail the backup, not quietly back up nothing.
    policy = read_policy(root)
    # Read once, before any copy: see extras_snapshot. Reading it again later
    # would let an erase in the middle make old lists look current.
    erased = erasures(root)
    with working_directory() as directory:
        stage = Path(directory)
        copy_eligible_settings(root, stage, life)
        if (root / POLICY_FILE).exists() or policy != MemoryPolicy():
            save_policy(stage, policy)
        database_snapshot(root, stage / DB_FILE, policy, read_only=read_only)
        compact_archive(stage, policy)
        extras_snapshot(root, stage, policy, erased)
        try:
            from . import keep

            keep_catalog = keep.export_catalog(root)
        except (ImportError, AttributeError):
            keep_catalog = None
        if keep_catalog is not None:
            encoded = json_text(keep_catalog)
            if len(encoded.encode("utf-8")) > KEEP_CATALOG_LIMIT:
                raise ValueError("The Keep catalog is too large for a backup.")
            atomic_write(stage / KEEP_CATALOG_FILE, encoded, mode=0o600)
        manifest = {"kind": "snapshot", "version": VERSION, "life_id": life["id"], "created": int(time.time()),
                    "erasures": erased}
        atomic_write(stage / MANIFEST, json_text(manifest))
        with tempfile.TemporaryFile(dir=stage) as plaintext:
            with tarfile.open(fileobj=plaintext, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                for name in sorted({MANIFEST, *SNAPSHOT_FILES}):
                    path = stage / name
                    if path.exists():
                        bundle.add(path, arcname=name, recursive=False)
            plaintext.flush()
            publish(plaintext)
    return {"life_id": life["id"], "created": manifest["created"]}


def export_deletions(mind_dir, output, recipient):
    root = source_directory(mind_dir)
    if Path("/etc/being/env").is_file():
        # The packaged CLI can export a proof directly. Its Keep naming proof
        # shares the same bounded /run workspace as the normal backup flow.
        _backup_stage_budget(root)
    proof = deletion_checkpoint(root)
    with working_directory() as directory, tempfile.TemporaryFile(dir=directory) as plaintext:
        encoded = json_text(proof).encode("utf-8")
        if len(encoded) > DELETION_PROOF_LIMIT:
            raise ValueError("The deletion proof is too large for a portable backup.")
        plaintext.write(encoded)
        plaintext.flush()
        write_encrypted(output, recipient, plaintext, outside=root)
    return {"life_id": proof["life_id"], "checkpoint": checkpoint_id(proof)}


def extract_snapshot(plaintext, stage):
    required = {MANIFEST, LIFE_FILE, PROFILE_FILE, DB_FILE}
    permitted = {MANIFEST, *SNAPSHOT_FILES}
    seen = set()
    with tarfile.open(fileobj=plaintext, mode="r:") as archive:
        for entry in archive:
            if (entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or entry.sparse is not None
                    or entry.name not in permitted or entry.name in seen
                    or entry.size <= 0 or (entry.name not in DATABASES
                                           and entry.size > (KEEP_CATALOG_LIMIT if entry.name == KEEP_CATALOG_FILE
                                                             else ACTIONS_LIMIT if entry.name == ACTIONS_ARCHIVED
                                                             else METADATA_LIMIT))):
                raise ValueError("Invalid file in the encrypted snapshot.")
            seen.add(entry.name)
            descriptor = os.open(stage / entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with archive.extractfile(entry) as source, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output)
    if not required <= seen:
        raise ValueError("The encrypted snapshot is incomplete.")
    manifest = read_metadata(stage / MANIFEST)
    life = life_record(stage)
    if (not isinstance(manifest, dict) or manifest.get("kind") != "snapshot"
            or type(manifest.get("version")) is not int or manifest["version"] != VERSION
            or manifest.get("life_id") != life["id"]):
        raise ValueError("The encrypted snapshot format or life ID is invalid.")
    return life


def authenticate(path, identity, plaintext, *, limit=None):
    with regular_file(path) as source:
        if limit is not None and os.fstat(source.fileno()).st_size > limit:
            raise ValueError("The encrypted deletion proof is too large.")
        decrypt(identity, source, plaintext)
    if limit is not None and plaintext.tell() > limit:
        raise ValueError("The decrypted deletion proof is too large.")
    plaintext.seek(0)
    content_hash = hashlib.file_digest(plaintext, "sha256").hexdigest()
    plaintext.seek(0)
    return content_hash


def read_proof(path, identity, stage, life_id, expected):
    with tempfile.TemporaryFile(dir=stage) as plaintext:
        proof_hash = authenticate(path, identity, plaintext, limit=DELETION_PROOF_LIMIT)
        value = json.load(plaintext)
    fields = {"kind", "version", "life_id", "ledger"}
    optional = {"policy", "erasures", "keep_erasures"}
    if (not isinstance(value, dict) or not fields <= set(value) or not set(value) <= fields | optional
            or ("erasures" in value and (type(value["erasures"]) is not int or value["erasures"] < 0))
            or value["kind"] != "deletions" or type(value["version"]) is not int
            or value["version"] != VERSION or value["life_id"] != life_id):
        raise ValueError("The deletion proof is for a different life or format.")
    if not hmac.compare_digest(checkpoint_id(value), expected):
        raise ValueError("The deletion proof does not match the trusted current checkpoint.")
    policy = MemoryPolicy.from_dict(value["policy"]) if "policy" in value else None
    return value["ledger"], policy, proof_hash, value.get("erasures", 0), value.get("keep_erasures")


def read_keep_catalog(path):
    with regular_file(path) as source:
        raw = source.read(KEEP_CATALOG_LIMIT + 1)
    if len(raw) > KEEP_CATALOG_LIMIT:
        raise ValueError("The Keep catalog is too large.")
    return json.loads(raw)


def restore_extras(unpacked, prepared, policy, erased):
    """Lists and the action log come back only if nothing was erased since.

    A "forget everything" after the snapshot (the proof counts more erasures
    than the snapshot does) leaves both out: forgetting stays forgetting.
    """
    try:
        snapshot_erasures = read_metadata(unpacked / MANIFEST).get("erasures", 0)
    except (OSError, ValueError, AttributeError):
        snapshot_erasures = None
    if erased:
        write_erasures(prepared, erased)
    if type(snapshot_erasures) is not int or snapshot_erasures != erased:
        return
    if policy.mode != "session-only" and (unpacked / LISTS_FILE).exists():
        sqlite_copy(unpacked / LISTS_FILE, prepared / LISTS_FILE)
    if (unpacked / ACTIONS_ARCHIVED).exists():
        (prepared / "log").mkdir(mode=0o700, exist_ok=True)
        actions_copy(_as_log(unpacked), prepared / ACTIONS_FILE)


def _as_log(unpacked):
    """The archived action log, where action_log expects to read it."""
    folder = unpacked / "log"
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / "actions.jsonl"
    if not target.exists():
        os.replace(unpacked / ACTIONS_ARCHIVED, target)
    return target


def _publish_file(source, destination):
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    os.replace(source, destination)
    sync_directory(destination.parent)


def _copy_staged(source, destination):
    # A previous interrupted write may have left this filename in the private stage.
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.unlink(missing_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as data:
        shutil.copyfileobj(data, output)
        output.flush()
        os.fsync(output.fileno())


def publish(target, prepared, token):
    marker = target / RESTORE_MARKER
    scratch = target / RESTORE_STAGE
    names = [name for name in (*SETTINGS, DB_FILE, LISTS_FILE, ERASURES_FILE, ACTIONS_FILE)
             if (prepared / name).exists()]
    for name in ("keep/forget-policy", "keep/index.sqlite"):
        if (prepared / name).exists():
            names.append(name)
    atomic_write(prepared / RESTORE_MARKER, json_text(token))
    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            existing = read_metadata(marker)
        except FileNotFoundError:
            existing = None
        permitted = {"lost+found", RESTORE_STAGE}
        if existing is not None:
            if existing != token:
                raise ValueError("Retry the unfinished restore with the same authenticated inputs.")
            permitted.update((RESTORE_MARKER, *(Path(name).parts[0] for name in names)))
        if any(path.name not in permitted for path in target.iterdir()):
            raise ValueError("Select an empty, inactive mind partition.")
        private_directory(scratch)
        sync_directory(target)
        if existing is None:
            _copy_staged(prepared / RESTORE_MARKER, scratch / RESTORE_MARKER)
            os.replace(scratch / RESTORE_MARKER, marker)
            sync_directory(target)
        for name in names:
            _copy_staged(prepared / name, scratch / name)
            _publish_file(scratch / name, target / name)
        # Catalog recovery deliberately creates missing-file rows without
        # putting Keep blobs in the ordinary archive.  Preserve its empty,
        # private object store so the separately authorised blob-recovery
        # service can fill only those indexed hashes after publication.
        prepared_objects = prepared / "keep/objects"
        if prepared_objects.is_dir():
            private_directory(target / "keep/objects")
            sync_directory(target / "keep")
        for folder in sorted({(scratch / name).parent for name in names if "/" in name}, reverse=True):
            folder.rmdir()
        scratch.rmdir()
        sync_directory(target)
        marker.unlink()
        sync_directory(target)
    finally:
        os.close(descriptor)


def restore(mind_dir, input_path, identity, deletions=None, current_checkpoint=None):
    if deletions is not None or current_checkpoint is not None:
        if deletions is None or not current_checkpoint or not DIGEST.fullmatch(current_checkpoint):
            raise ValueError("History restore requires both a deletion proof and the independently known current checkpoint.")
    target = Path(mind_dir).resolve(strict=True)
    live_mind = Path("/mind")
    if not target.is_dir() or (live_mind.is_dir() and target.samefile(live_mind)):
        raise ValueError("Select an empty, inactive mind partition.")
    with working_directory() as directory:
        unpacked = Path(directory) / "unpacked"
        prepared = Path(directory) / "prepared"
        unpacked.mkdir(mode=0o700)
        prepared.mkdir(mode=0o700)
        with tempfile.TemporaryFile(dir=directory) as plaintext:
            snapshot_hash = authenticate(input_path, identity, plaintext)
            life = extract_snapshot(plaintext, unpacked)
        proof_hash = None
        policy = load_policy(unpacked)
        copy_eligible_settings(unpacked, prepared, life)
        if deletions is not None:
            proof, current_policy, proof_hash, erased, keep_proof = read_proof(
                deletions, identity, directory, life["id"], current_checkpoint
            )
            policy = current_policy or policy
            compact_archive(unpacked, policy, proof, apply_expiry=True)
            (unpacked / DB_FILE).replace(prepared / DB_FILE)
            restore_extras(unpacked, prepared, policy, erased)
        else:
            with closing(Memory(prepared)):
                pass
        if (unpacked / POLICY_FILE).exists() or policy != MemoryPolicy():
            save_policy(prepared, policy)
        if deletions is not None and keep_proof is not None and (unpacked / KEEP_CATALOG_FILE).exists():
            try:
                from . import keep

                keep.recover_catalog(prepared, read_keep_catalog(unpacked / KEEP_CATALOG_FILE), keep_proof)
            except (ImportError, AttributeError):
                pass
        if deletions is not None and policy.mode == "persistent":
            from .ponder import prune_kept_jobs

            with closing(Memory(prepared)) as memory:
                prune_kept_jobs(memory, prepared)
                memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        token = {"snapshot": snapshot_hash, "proof": proof_hash, "version": VERSION}
        publish(target, prepared, token)
    return {"life_id": life["id"], "history_restored": deletions is not None and policy.mode != "session-only"}


# --- the owner's USB stick ---------------------------------------------------

def fingerprint(checkpoint):
    """The first and last 8 characters, for the owner to compare by eye."""
    if not isinstance(checkpoint, str) or not DIGEST.fullmatch(checkpoint):
        raise ValueError("Not a deletion checkpoint.")
    return f"{checkpoint[:8]}…{checkpoint[-8:]}"


def checkpoint_text(life_id, checkpoint, created):
    return json_text({"kind": CHECKPOINT_KIND, "version": VERSION, "life_id": life_id,
                      "checkpoint": checkpoint, "created": created})


def read_checkpoint_file(path):
    """(life_id, checkpoint) from a file written by backup_to_folder."""
    value = read_metadata(Path(path))
    if (not isinstance(value, dict) or value.get("kind") != CHECKPOINT_KIND or value.get("version") != VERSION
            or not isinstance(value.get("life_id"), str) or not LIFE_ID.fullmatch(value["life_id"])
            or not isinstance(value.get("checkpoint"), str) or not DIGEST.fullmatch(value["checkpoint"])):
        raise ValueError("That file is not a PipOS deletion checkpoint.")
    return value["life_id"], value["checkpoint"]


def backup_to_folder(mind_dir, folder, recipient, *, now=None, device=None, expected_uuid=None):
    """Snapshot, deletion proof and checkpoint file, side by side in one folder.

    The checkpoint comes from the same proof that was written, so a restore can
    check the pair. Returns the three paths and the checkpoint.
    """
    folder = Path(folder)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now))
    snapshot = folder / f"pip-{stamp}.age"
    proof = folder / f"pip-{stamp}-deletions.age"
    record = folder / f"pip-{stamp}-checkpoint.txt"
    if any(path.exists() or path.is_symlink() for path in (snapshot, proof, record)):
        raise ValueError("A backup with this time already exists. Wait a second and try again.")
    # The installed image stages ciphertext privately and gives the writable
    # media side only opaque files, one at the same durability boundaries as
    # the legacy path. A valid snapshot therefore remains if proof generation
    # later fails; Care/voice/night retention behavior is unchanged.
    if Path("/etc/being/env").is_file() and folder.is_relative_to(Path("/run")):
        _backup_stage_budget(mind_dir)
        with working_directory() as directory:
            stage = Path(directory)
            staged_snapshot = stage / snapshot.name
            created = create(mind_dir, staged_snapshot, recipient)
            publish_backup_artifact(staged_snapshot, folder, snapshot.name,
                                    device=device, expected_uuid=expected_uuid)
            staged_proof = stage / proof.name
            exported = export_deletions(mind_dir, staged_proof, recipient)
            publish_backup_artifact(staged_proof, folder, proof.name,
                                    device=device, expected_uuid=expected_uuid)
            if exported["life_id"] != created["life_id"]:
                raise ValueError("The being changed while it was backed up.")
            staged_record = stage / record.name
            atomic_write(staged_record,
                         checkpoint_text(created["life_id"], exported["checkpoint"], created["created"]), mode=0o600)
            publish_backup_artifact(staged_record, folder, record.name,
                                    device=device, expected_uuid=expected_uuid)
        return {"snapshot": snapshot, "deletions": proof, "checkpoint_file": record,
                "checkpoint": exported["checkpoint"], "life_id": created["life_id"]}
    created = create(mind_dir, snapshot, recipient)
    exported = export_deletions(mind_dir, proof, recipient)
    if exported["life_id"] != created["life_id"]:
        raise ValueError("The being changed while it was backed up.")
    atomic_write(record, checkpoint_text(created["life_id"], exported["checkpoint"], created["created"]), mode=0o600)
    return {"snapshot": snapshot, "deletions": proof, "checkpoint_file": record,
            "checkpoint": exported["checkpoint"], "life_id": created["life_id"]}


def has_conversation(mind_dir):
    """Whether an archive holds anything the owner said: facts or heard lines."""
    database = Path(mind_dir) / DB_FILE
    try:
        if not stat.S_ISREG(database.lstat().st_mode):
            return False
        with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as db:
            facts, = db.execute("SELECT EXISTS (SELECT 1 FROM facts)").fetchone()
            heard, = db.execute("SELECT EXISTS (SELECT 1 FROM episodes WHERE kind = 'heard' "
                                "AND text <> '[forgotten]')").fetchone()
        return bool(facts or heard)
    except (OSError, sqlite3.Error):
        return False


def carry(mind_dir, target):
    """Bring a Try being into a fresh install: its life record, settings and archive.

    It follows the restore path (the same eligible settings and publish step)
    without encryption, because nothing leaves this machine's RAM. Expiry and
    the memory policy apply to the carried copy, and the dream journal is not
    carried, exactly as for a backup.
    """
    root = source_directory(mind_dir)
    life = life_record(root)
    policy = read_policy(root)
    target = Path(target).resolve(strict=True)
    with working_directory() as directory:
        prepared = Path(directory) / "prepared"
        prepared.mkdir(mode=0o700)
        copy_eligible_settings(root, prepared, life)
        database_snapshot(root, prepared / DB_FILE, policy)
        if (prepared / DB_FILE).exists():
            compact_archive(prepared, policy, apply_expiry=True)
        if (root / POLICY_FILE).exists() or policy != MemoryPolicy():
            save_policy(prepared, policy)
        if policy.mode != "session-only" and (root / LISTS_FILE).exists():
            sqlite_copy(root / LISTS_FILE, prepared / LISTS_FILE)
        if (root / ACTIONS_FILE).exists():
            (prepared / "log").mkdir(mode=0o700, exist_ok=True)
            actions_copy(root / ACTIONS_FILE, prepared / ACTIONS_FILE)
        if erasures(root):
            write_erasures(prepared, erasures(root))
        publish(target, prepared, {"carry": life["id"], "version": VERSION})
    return {"life_id": life["id"]}


# --- the labelled stick, by voice and at night ---------------------------------

LAST_RECORD = "last-backup.json"        # read by the mind (turn.py BACKUP_RECORD)
RECIPIENT_FILE = "backup-recipient.txt"
MEDIA_BINDING_FILE = "backup-media.json"
BACKUP_FOLDER = "PipOS-backups"
# A FAT label holds 11 characters, so the short forms are accepted too.
STICK_LABELS = ("being-backup", "pip-backup", "beingbackup", "pipbackup")
BY_LABEL = Path("/dev/disk/by-label")
BY_UUID = Path("/dev/disk/by-uuid")
FILESYSTEM_UUID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{2,127}")
KEEP = 5
SET = re.compile(r"pip-(\d{8}-\d{6})(?:\.age|-deletions\.age|-checkpoint\.txt)")
KEY_ARTIFACT = re.compile(r"pip-backup-key\.(?:age|key)")
HOW = ("menu", "voice", "night")
# The night shift backs up at most once in this long.
NIGHT_GAP_S = 20 * 3600
# The nice value of backup worker threads (the memory's vector thread uses 10).
BACKGROUND_NICE = 10


class Unavailable(Exception):
    """Why a backup cannot be made now: `reason` is one of the codes below."""

    REASONS = ("no_tool", "no_key", "no_stick", "busy", "failed")

    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason


def record_backup(mind_dir, how, *, now=None):
    """Note a finished backup: when, and how it was made. Nothing else."""
    if how not in HOW:
        raise ValueError(f"Unknown backup kind {how!r}.")
    stamp = int(time.time() if now is None else now)
    atomic_write(Path(mind_dir) / LAST_RECORD, json_text({"version": VERSION, "ts": stamp, "how": how}), mode=0o600)


def last_backup(mind_dir):
    """The time of the last backup made here (unix seconds), or None."""
    try:
        value = read_metadata(Path(mind_dir) / LAST_RECORD)
    except (OSError, ValueError):
        return None
    stamp = value.get("ts") if isinstance(value, dict) else None
    return stamp if type(stamp) is int and stamp > 0 else None


def recipient_on_file(mind_dir):
    from .backup_crypto import RECIPIENT

    try:
        value = (Path(mind_dir) / RECIPIENT_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if RECIPIENT.fullmatch(value) else None


def find_stick(by_label=BY_LABEL):
    """The device of a partition labelled for backups, or None."""
    try:
        entries = sorted(os.scandir(by_label), key=lambda entry: entry.name)
    except OSError:
        return None
    for entry in entries:
        # udev escapes characters in labels as \xNN; a backup label has none.
        if entry.name.lower() in STICK_LABELS:
            try:
                return str(Path(entry.path).resolve(strict=True))
            except OSError:
                continue
    return None


def media_identity(device, by_uuid=BY_UUID):
    """The filesystem UUID naming exactly this block node, or None.

    Labels are owner-editable and therefore never identities. UUID discovery
    reads udev's existing links and opens no filesystem.
    """
    try:
        wanted = os.stat(device, follow_symlinks=True)
        if not stat.S_ISBLK(wanted.st_mode):
            return None
        entries = os.scandir(by_uuid)
    except OSError:
        return None
    with entries:
        for entry in sorted(entries, key=lambda item: item.name)[:256]:
            if not FILESYSTEM_UUID.fullmatch(entry.name):
                continue
            try:
                found = entry.stat(follow_symlinks=True)
            except OSError:
                continue
            if stat.S_ISBLK(found.st_mode) and found.st_rdev == wanted.st_rdev:
                return entry.name
    return None


def media_binding(mind_dir):
    try:
        value = read_metadata(Path(mind_dir) / MEDIA_BINDING_FILE)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    uuid = value.get("uuid") if isinstance(value, dict) and value.get("version") == VERSION else None
    return uuid if isinstance(uuid, str) and FILESYSTEM_UUID.fullmatch(uuid) else None


def save_media_binding(mind_dir, uuid):
    if not isinstance(uuid, str) or not FILESYSTEM_UUID.fullmatch(uuid):
        raise ValueError("The backup stick has no stable filesystem identity.")
    atomic_write(Path(mind_dir) / MEDIA_BINDING_FILE,
                 json_text({"version": VERSION, "uuid": uuid}), mode=0o600)


def bound_device(mind_dir, by_uuid=BY_UUID):
    uuid = media_binding(mind_dir)
    if uuid is None:
        return None
    path = Path(by_uuid) / uuid
    return str(path) if media_identity(path, by_uuid) == uuid else None


def mounted_from(root, device):
    """Whether this mount root is backed by this exact block partition."""
    try:
        mounted = Path(root).stat().st_dev
        source = os.stat(device, follow_symlinks=True).st_rdev
    except OSError:
        return False
    return source != 0 and os.major(mounted) == os.major(source) and os.minor(mounted) == os.minor(source)


def set_stamp(path):
    """The time stamp a backup file is named by, or None."""
    match = SET.fullmatch(Path(path).name)
    return match[1] if match else None


def prune(folder, keep=KEEP, *, newest=None, clock_trusted=None):
    """Keep the newest `keep` backup sets in the folder; remove the rest.

    Only files this tool names (pip-<time>.age, its proof and checkpoint) are
    touched. Older sets can hold what was forgotten since, so going is good.

    `newest` is the stamp of the set just written. It is never removed, even
    when a clock that went back (a flat clock battery) named it older than
    the sets already there. Sets are named by the clock, so the others are
    ranked by name; only when the clock is trusted are sets named after the
    one just written known to come from a clock that ran ahead, and they go
    first.
    """
    sets = {}
    for path in Path(folder).iterdir():
        match = SET.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            sets.setdefault(match[1], []).append(path)
    keep = max(1, int(keep))
    others = sorted((stamp for stamp in sets if stamp != newest), reverse=True)
    if newest in sets:
        if clock_trusted:
            # Named in the future by a clock that ran ahead: really older.
            others = [stamp for stamp in others if stamp < newest] + [stamp for stamp in others if stamp > newest]
        kept = {newest, *others[:keep - 1]}
    else:
        kept = set(others[:keep])
    removed = 0
    for stamp in sorted(stamp for stamp in sets if stamp not in kept):
        for path in sets[stamp]:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        sync_directory(Path(folder))
    return removed


def readiness(mind_dir, *, which=shutil.which, by_label=BY_LABEL):
    """(device, recipient), or raise Unavailable with the reason."""
    if not (which("age") and which("age-keygen")):
        raise Unavailable("no_tool")
    recipient = recipient_on_file(mind_dir)
    if recipient is None:
        raise Unavailable("no_key")
    # Existing installations remain label-compatible until the owner next
    # chooses a destination in Care. Once enrolled, labels can no longer
    # redirect unattended or spoken backups to another stick.
    device = bound_device(mind_dir) if media_binding(mind_dir) is not None else find_stick(by_label)
    if device is None:
        raise Unavailable("no_stick")
    return device, recipient


# The installed voice/night transient service mounts here in its private mount
# namespace. The legacy/host path uses the same fixed root directly. Neither
# shares Care's restore-media root.
STICK_DIR = Path("/run/being-backup-stick")
# One backup at a time across processes: the mind (voice, night) and the F2
# menu (care.py) both take this lock.
BACKUP_LOCK = Path("/run/being-backup.lock")


def lock_path():
    runtime = BACKUP_LOCK.parent
    if runtime.is_dir() and os.access(runtime, os.W_OK):
        return BACKUP_LOCK
    return Path(tempfile.gettempdir()) / f"being-backup-{os.getuid()}.lock"


@contextmanager
def backup_lock(path=None):
    """Hold the one-backup-at-a-time lock, or raise Unavailable("busy")."""
    path = Path(path) if path is not None else lock_path()
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Unavailable("busy") from None
        yield
    finally:
        os.close(descriptor)


def unmount_stick(mount_dir=STICK_DIR):
    if os.path.ismount(mount_dir):
        result = subprocess.run(["umount", str(mount_dir)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=60, check=False)
        if result.returncode:
            raise OSError("the backup stick could not be unmounted")
    try:
        Path(mount_dir).rmdir()
    except OSError:
        pass


def mount_stick(device, mount_dir=STICK_DIR):
    """Mount the labelled stick writable (FAT, exFAT or ext4; nothing on it runs)."""
    from .local_menu import WRITABLE_MOUNTS

    mount_dir = Path(mount_dir)
    unmount_stick(mount_dir)
    mount_dir.mkdir(mode=0o700, exist_ok=True)
    for filesystem, options in WRITABLE_MOUNTS:
        try:
            result = subprocess.run(["mount", "-t", filesystem, "-o", options, device, str(mount_dir)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return mount_dir
    unmount_stick(mount_dir)
    raise ValueError("The backup stick could not be mounted. PipOS writes FAT, exFAT and ext4 sticks.")


def lower_priority():
    """Run the calling thread (and the age child it starts) below the face,
    the voice and the turn, as the memory's vector thread does. A backup is
    a minute of SQLite, tar and age on two slow cores; it may take longer,
    the owner's conversation may not. Linux only; elsewhere nothing changes."""
    import threading

    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), BACKGROUND_NICE)
        return True
    except (AttributeError, OSError):
        return False


def stick_backup(mind_dir, how, *, mount=None, unmount=None, which=shutil.which, by_label=BY_LABEL, now=None,
                 keep=KEEP, lock=None, clock_trusted=None):
    """Back up to the labelled stick (voice or night). Returns the result dict.

    Raises Unavailable. The menu's backup (care.py) keeps its own stick picker
    but takes the same lock. The set just written always survives the prune
    (see prune), whatever the clock named it.
    """
    with backup_lock(lock):
        device, recipient = readiness(mind_dir, which=which, by_label=by_label)
        mount = mount or mount_stick
        unmount = unmount or unmount_stick
        if (Path("/etc/being/env").is_file() and shutil.which("systemd-run")
                and Path("/usr/sbin/being-backup").is_file() and mount is mount_stick and unmount is unmount_stick):
            transfer_identity = media_binding(mind_dir) or media_identity(device)
            if transfer_identity is None:
                raise Unavailable("failed", "The backup stick has no stable identity.")
            try:
                result = backup_to_device(mind_dir, device, transfer_identity, recipient, now=now,
                                          keep=keep, clock_trusted=clock_trusted)
            except (BackupCryptoError, OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError,
                    subprocess.SubprocessError) as error:
                raise Unavailable("failed", str(error)[:200]) from None
            record_backup(mind_dir, how, now=now)
            return result
        completed = False
        try:
            root = Path(mount(device))
            enrolled = media_binding(mind_dir)
            if enrolled is not None and (media_identity(device) != enrolled or not mounted_from(root, device)):
                raise ValueError("The mounted stick is not the enrolled backup destination.")
            transfer_identity = enrolled or media_identity(device)
            if transfer_identity is None or not mounted_from(root, device):
                raise ValueError("The mounted backup stick has no stable identity.")
            folder = root / BACKUP_FOLDER
            folder.mkdir(mode=0o700, exist_ok=True)
            result = backup_to_folder(Path(mind_dir), folder, recipient, now=now,
                                      device=device, expected_uuid=transfer_identity)
            written = result.get("snapshot")
            result["pruned"] = prune(folder, keep, newest=set_stamp(written) if written else None,
                                     clock_trusted=clock_trusted)
            os.sync()
            completed = True
        except Unavailable:
            raise
        except (BackupCryptoError, OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError,
                subprocess.SubprocessError) as error:
            raise Unavailable("failed", str(error)[:200]) from None
        finally:
            try:
                unmount()
            except (OSError, subprocess.SubprocessError) as error:
                # Do not claim or record a completed backup while its writable
                # private mount remains behind. If the backup itself already
                # failed, preserve that more useful primary error.
                if completed:
                    raise Unavailable("failed", f"Backup was written, but the stick could not be unmounted: {error}") from None
        record_backup(mind_dir, how, now=now)
        return result


class AutoBackupJob:
    """The night shift's backup (plan 8.6): once a night, to the labelled stick.

    The night shift gives every chunk about a second, and a backup is one
    piece of disk work of seconds to a minute that cannot be split. So the
    first chunk starts it in a worker thread and each chunk waits for it for
    at most its budget: False while it is still running (the shift asks again
    after letting wake and lid events through), True when there is nothing
    more to do tonight: done, done recently, or not possible (no stick, no
    key). An interrupt stops the waiting at once; the backup itself finishes
    in the background, whole, and the next chunk collects its outcome.
    """

    name = "backup"
    POLL_S = 0.1

    def __init__(self, mind_dir, *, backup=None, clock=time.time, gap_s=NIGHT_GAP_S):
        self.mind_dir = Path(mind_dir)
        self.backup = backup or (lambda: stick_backup(self.mind_dir, "night"))
        self.clock = clock
        self.gap_s = gap_s
        self.last_reason = None
        self._worker = None
        self._finished = None
        self._outcome = None

    def _attempt(self):
        lower_priority()
        try:
            self.backup()
            self._outcome = "done"
        except Unavailable as error:
            self._outcome = error.reason if error.reason in Unavailable.REASONS else "failed"
        except Exception:  # whatever went wrong, the night carries on
            self._outcome = "failed"
        finally:
            self._finished.set()

    @property
    def running(self):
        return self._worker is not None and not self._finished.is_set()

    async def run_chunk(self, budget_s):
        import threading

        from . import action_log

        if self._worker is None:
            last = last_backup(self.mind_dir)
            # A last backup "in the future" means the clock went back since
            # (a flat clock battery): not recent, or the night would never
            # back up again until the clock caught up.
            if last is not None and 0 <= self.clock() - last < self.gap_s:
                self.last_reason = "recent"
                return True
            self._outcome = None
            self._finished = threading.Event()
            self._worker = threading.Thread(target=self._attempt, name="being-night-backup", daemon=True)
            self._worker.start()
        deadline = time.monotonic() + max(0.0, float(budget_s))
        while not self._finished.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                self.last_reason = "running"
                return False
            await asyncio.sleep(min(self.POLL_S, left))
        self._worker = None
        self.last_reason = self._outcome
        if self._outcome == "done":
            action_log.record(self.mind_dir, "backup.auto")
        elif self._outcome == "failed":
            action_log.record(self.mind_dir, "backup.failed")
        return True


def auto_backup_job(mind_dir, **options):
    """The job the night shift registers: `NightShift.register("backup", auto_backup_job(state_dir))`."""
    return AutoBackupJob(mind_dir, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-backup", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    keygen = commands.add_parser("keygen", help="generate an owner identity and print its public recipient")
    keygen.add_argument("--identity", type=Path, required=True)
    stage = commands.add_parser("stage-restore", help=argparse.SUPPRESS)
    stage.add_argument("--root", type=Path, required=True)
    stage.add_argument("--device", required=True)
    stage.add_argument("--uuid", required=True)
    stage.add_argument("--destination", type=Path, required=True)
    stage.add_argument("--artifact", action="append", default=[])
    publish_one = commands.add_parser("publish-artifact", help=argparse.SUPPRESS)
    publish_one.add_argument("--source", type=Path, required=True)
    publish_one.add_argument("--folder", type=Path, required=True)
    publish_one.add_argument("--name", required=True)
    publish_one.add_argument("--device", required=True)
    publish_one.add_argument("--uuid", required=True)
    device_op = commands.add_parser("device-operation", help=argparse.SUPPRESS)
    device_op.add_argument("--device", required=True)
    device_op.add_argument("--uuid", required=True)
    device_op.add_argument("--source", type=Path)
    device_op.add_argument("--name")
    device_op.add_argument("--prune-keep", type=int)
    device_op.add_argument("--newest")
    device_op.add_argument("--clock-trusted", action="store_true")
    restore_op = commands.add_parser("restore-device", help=argparse.SUPPRESS)
    restore_op.add_argument("--device", required=True)
    restore_op.add_argument("--uuid", required=True)
    restore_op.add_argument("--destination", type=Path)
    restore_op.add_argument("--selected", action="append", default=[])
    for name in ("create", "export-deletions", "checkpoint", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--mind-dir", type=Path, required=True)
        if name in ("create", "export-deletions"):
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--recipient", required=True)
        if name == "restore":
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--identity", type=Path, required=True)
            command.add_argument("--deletions", type=Path)
            command.add_argument("--current-checkpoint")
    args = parser.parse_args(argv)
    try:
        if args.action == "keygen":
            print(generate_key(args.identity))
            return 0
        if args.action == "restore-device":
            selected = []
            for value in args.selected:
                role, separator, relative = value.partition("=")
                path = Path(relative)
                if (not separator or role not in RESTORE_ARTIFACTS or path.is_absolute()
                        or not path.parts or len(path.parts) > 5 or any(part in ("", ".", "..") for part in path.parts)):
                    raise ValueError("Invalid selected restore artifact.")
                selected.append((role, str(path)))
            if bool(selected) != bool(args.destination):
                raise ValueError("Restore staging requires selections and a destination together.")
            result = restore_device_operation(args.device, args.uuid,
                                              selected=selected or None, destination=args.destination)
        elif args.action == "device-operation":
            publishing = args.source is not None or args.name is not None
            pruning = args.prune_keep is not None
            if publishing == pruning or (publishing and (args.source is None or args.name is None)):
                raise ValueError("Invalid device backup operation.")
            device_backup_operation(args.device, args.uuid, source=args.source, name=args.name,
                                    prune_keep=args.prune_keep, newest=args.newest or None,
                                    clock_trusted=args.clock_trusted)
            result = {"complete": True}
        elif args.action == "publish-artifact":
            publish_backup_artifact(args.source, args.folder, args.name, device=args.device,
                                    expected_uuid=args.uuid, isolated=False)
            result = {"published": args.name}
        elif args.action == "stage-restore":
            selected = []
            for value in args.artifact:
                role, separator, path = value.partition("=")
                if not separator or role not in RESTORE_ARTIFACTS or not path:
                    raise ValueError("Invalid restore artifact request.")
                selected.append((role, Path(path)))
            result = {role: str(path) for role, path in stage_restore_artifacts(
                args.root, args.device, args.uuid, selected, args.destination
            ).items()}
        elif args.action == "restore":
            result = restore(args.mind_dir, args.input, args.identity, args.deletions, args.current_checkpoint)
        elif args.action == "checkpoint":
            proof = deletion_checkpoint(args.mind_dir)
            result = {"life_id": proof["life_id"], "checkpoint": checkpoint_id(proof)}
        else:
            operation = create if args.action == "create" else export_deletions
            result = operation(args.mind_dir, args.output, args.recipient)
        print(json_text(result), end="")
        return 0
    except (BackupCryptoError, OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError, RecursionError) as exc:
        print(f"being-backup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
