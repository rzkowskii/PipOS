"""Explicit, bounded inspection and ejection of foreign removable media.

Insertion never calls this module.  The everyday skill starts the command only
after the owner asks what is on a stick or asks to eject it.  ``inspect`` uses
one read-only FAT/exFAT mount, walks metadata without following links, and
unmounts in ``finally``.  Existing installer, restore, backup and Try-log
mounts deliberately remain separate explicit workflows.
"""

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import re
import resource
import select
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from . import install_disks
from .reply import Reply, is_guest

MAX_ITEMS = 4096
MAX_DEPTH = 8
MAX_SECONDS = 8.0
MAX_OUTPUT = 16 * 1024
KEEP_MAX_FILE = 64 << 20
KEEP_MAX_BATCH = 256 << 20
KEEP_MAX_ITEMS = 20_000
KEEP_HEADER = 4096
READER_UID = 65534
READER_GID = 65534
# Files remain unreadable for writes but traversable by the capability-free
# reader UID after the root parent has mounted them.
MOUNT_TYPES = (("vfat", "ro,nodev,nosuid,noexec,noatime,utf8=1,fmask=0133,dmask=0022"),
               ("exfat", "ro,nodev,nosuid,noexec,noatime,fmask=0133,dmask=0022"))
BLOCKING_REASONS = frozenset(("boot_disk", "boot_disk_unknown", "mounted", "active_swap",
                              "held_or_stacked", "inspection_failed", "unsupported_device"))
SD_QUALIFICATION = Path("/etc/being/sd-card-qualified")
SD_QUALIFICATION_TOKEN = "broadcom-14e4:16bc-sdhci-pci-v1"
SD_CARD_FIELDS = ("cid", "manfid", "oemid", "name", "serial")

_INSPECT = re.compile(
    r"^(?:(?:hey|hi|okay|ok)\s+pip\W*\s*)?(?:pip\W*\s*)?(?:please\s+|can\s+you\s+|could\s+you\s+)?"
    r"(?:what(?:['’]s|\s+is)\s+on\s+(?:the|this|my)\s+(?:usb\s+)?(?:stick|drive)"
    r"|inspect\s+(?:the|this|my)\s+(?:usb\s+)?(?:stick|drive)"
    r"|look\s+(?:at|on)\s+(?:the|this|my)\s+(?:usb\s+)?(?:stick|drive))"
    r"(?:\s+(?:pip|please|now|for\s+me))*\s*[?.!]*$", re.I)
_EJECT = re.compile(
    r"^(?:(?:hey|hi|okay|ok)\s+pip\W*\s*)?(?:pip\W*\s*)?(?:please\s+|can\s+you\s+|could\s+you\s+)?"
    r"(?:eject|safely\s+remove)(?:\s+(?:the|this|my))?(?:\s+(?:usb\s+)?(?:stick|drive))?"
    r"(?:\s+(?:pip|please|now|for\s+me))*\s*[?.!]*$", re.I)
_INSPECT_SD = re.compile(
    r"^(?:(?:hey|hi|okay|ok)\s+pip\W*\s*)?(?:pip\W*\s*)?(?:please\s+|can\s+you\s+|could\s+you\s+)?"
    r"(?:what(?:['’]s|\s+is)\s+on\s+(?:the|this|my)\s+sd\s+card"
    r"|inspect\s+(?:the|this|my)\s+sd\s+card"
    r"|look\s+(?:at|on)\s+(?:the|this|my)\s+sd\s+card)"
    r"(?:\s+(?:pip|please|now|for\s+me))*\s*[?.!]*$", re.I)


def classify(text):
    if _INSPECT_SD.fullmatch(text.strip()):
        return "inspect-sd"
    if _INSPECT.fullmatch(text.strip()):
        return "inspect"
    if _EJECT.fullmatch(text.strip()):
        return "eject"
    return None


def may_match(text):
    said = text or ""
    return bool(re.search(r"\b(?:eject|stick|usb drive|sd card|safely remove)\b", said, re.I) and classify(said))


def _qualified_sd():
    """Require a deliberately installed, immutable-enough hardware qualification."""
    descriptor = None
    try:
        descriptor = os.open(SD_QUALIFICATION, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        value = os.read(descriptor, 128)
        if os.read(descriptor, 1):
            return False
        value = value.decode("ascii").strip()
    except (OSError, UnicodeError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and value == SD_QUALIFICATION_TOKEN)


def _sd_identity(block):
    """Return a private stable digest only for the qualified Broadcom SD path."""
    if not re.fullmatch(r"mmcblk[0-9]+", block.name):
        return None
    card = next((path for path in (block.path, *block.path.parents)
                 if re.fullmatch(r"mmc[0-9]+:[0-9a-f]+", path.name)), None)
    host = next((path for path in (block.path, *block.path.parents)
                 if re.fullmatch(r"mmc[0-9]+", path.name) and path.parent.name == "mmc_host"), None)
    if card is None or host is None or install_disks.optional_text(card / "type") != "SD":
        return None
    pci = next((path for path in (host, *host.parents)
                if (path / "vendor").exists() and (path / "device").exists()
                and (path / "class").exists()), None)
    try:
        driver = (pci / "driver").resolve(strict=True).name if pci is not None else None
    except OSError:
        return None
    if (install_disks.optional_text(pci / "vendor") != "0x14e4"
            or install_disks.optional_text(pci / "device") != "0x16bc"
            or not (install_disks.optional_text(pci / "class") or "").startswith("0x0805")
            or driver != "sdhci-pci"):
        return None
    fields = [install_disks.optional_text(card / name) for name in SD_CARD_FIELDS]
    if any(value is None for value in fields):
        return None
    # Raw card identifiers never leave this helper or become display text.
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def _sd_candidate(*, inspect_contents=True):
    if not _qualified_sd():
        raise ValueError("SD card use has not been qualified on this laptop.")
    blocks, failed = install_disks.block_devices()
    if failed:
        raise ValueError("I couldn't verify the SD card's device path safely.")
    report = install_disks.inventory(min_size_bytes=1)
    candidates = []
    for disk in report.get("disks", ()):
        block = next((item for item in blocks.values()
                      if item.parent is None and item.device == disk.get("device")), None)
        identity = _sd_identity(block) if block is not None else None
        reasons = set(disk.get("reasons", ()))
        if (identity is None or not disk.get("device_number")
                or type(disk.get("device_sequence")) is not int
                or (reasons - {"unsupported_device", "read_only"})):
            continue
        candidate = {**disk, "origin": "sd", "card_identity": identity}
        if inspect_contents:
            contents = install_disks.disk_contents(candidate["device"],
                                                    candidate.get("logical_sector_bytes") or 512)
            if not contents.get("readable"):
                continue
            candidate["contents"] = contents
        candidates.append(candidate)
    if not candidates:
        raise ValueError("I can't find a qualified SD card that is safe to open.")
    if len(candidates) != 1:
        raise ValueError("I can see more than one qualified SD card device.")
    disk = candidates[0]
    if inspect_contents and disk["contents"].get("being"):
        raise ValueError("That looks like another PipOS disk. I won't open it.")
    if inspect_contents and disk["contents"].get("macos"):
        raise ValueError("That's a Mac disk. I can't read it.")
    return disk


def _candidate(*, inspect_contents=True, origin="usb"):
    """Return the one safe foreign USB disk, failing closed on ambiguity."""
    if origin == "sd":
        return _sd_candidate(inspect_contents=inspect_contents)
    if origin != "usb":
        raise ValueError("That removable-media source is not supported.")
    report = install_disks.inventory(min_size_bytes=1)
    candidates = []
    for disk in report.get("disks", ()):
        if disk.get("transport") != "usb" or BLOCKING_REASONS.intersection(disk.get("reasons", ())):
            continue
        if inspect_contents:
            contents = install_disks.disk_contents(disk["device"], disk.get("logical_sector_bytes") or 512)
            if not contents.get("readable"):
                continue
            disk = {**disk, "contents": contents}
        candidates.append(disk)
    if not candidates:
        raise ValueError("I can't find a USB stick that is safe to open.")
    if len(candidates) != 1:
        raise ValueError("I can see more than one USB stick. Leave only the one you mean.")
    disk = candidates[0]
    if inspect_contents and disk["contents"].get("being"):
        raise ValueError("That looks like another PipOS disk. I won't open it.")
    if inspect_contents and disk["contents"].get("macos"):
        raise ValueError("That's a Mac disk. I can't read it.")
    return disk


def _same_disk(expected):
    """Re-read diskseq/topology so a removed device path cannot be reused."""
    if expected.get("origin") == "sd":
        try:
            current = _sd_candidate(inspect_contents=False)
        except (OSError, ValueError):
            return False
        return all(current.get(key) == expected.get(key) for key in
                   ("device", "device_number", "device_sequence", "card_identity"))
    for disk in install_disks.inventory(min_size_bytes=1).get("disks", ()):
        if (disk.get("device") == expected.get("device")
                and disk.get("device_number") == expected.get("device_number")
                and disk.get("device_sequence") == expected.get("device_sequence")
                and disk.get("transport") == "usb"):
            return True
    return False


def _mounted_from(root, device):
    try:
        mounted = root.stat().st_dev
        source = os.stat(device, follow_symlinks=True).st_rdev
    except OSError:
        return False
    return source != 0 and os.major(mounted) == os.major(source) and os.minor(mounted) == os.minor(source)


def _limits(file_size=0):
    # A corrupt tree cannot grow the helper indefinitely even if the service
    # manager's limits were accidentally relaxed.
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_AS, (192 << 20, 192 << 20))
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))


def _mount(device, target):
    for filesystem, options in MOUNT_TYPES:
        result = subprocess.run(("mount", "-t", filesystem, "-o", options, device, str(target)),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        if result.returncode == 0:
            return filesystem
    raise ValueError("I can only read FAT and exFAT sticks.")


def _kind(name):
    suffix = Path(name).suffix.casefold()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}:
        return "photos"
    if suffix in {".mp3", ".m4a", ".flac", ".wav", ".ogg"}:
        return "audio files"
    if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
        return "videos"
    if suffix in {".txt", ".md", ".rtf", ".pdf", ".doc", ".docx", ".odt"}:
        return "documents"
    return "other files"


def _write_all(fd, payload):
    while payload:
        written = os.write(fd, payload)
        if written <= 0:
            raise BrokenPipeError("keep receiver closed")
        payload = payload[written:]


def _keep_frame(fd, record):
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > KEEP_HEADER:
        raise ValueError("keep record is too large")
    _write_all(fd, struct.pack(">I", len(payload)) + payload)


def _read_exact_fd(fd, size):
    chunks = []
    while size:
        chunk = os.read(fd, size)
        if not chunk:
            raise OSError("The confined media reader ended early.")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _keep_record(fd):
    size = struct.unpack(">I", _read_exact_fd(fd, 4))[0]
    if size > KEEP_HEADER:
        raise ValueError("The confined media reader returned an unsafe record.")
    record = json.loads(_read_exact_fd(fd, size).decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("The confined media reader returned an unsafe record.")
    return record


@contextmanager
def _keep_owner(state_dir):
    """Adopt the existing private Keep owner only for Store operations."""
    root = Path(state_dir) / "keep"
    info = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("I couldn't open the private keep safely.")
    if info.st_uid == 0:
        yield
        return
    old_groups = os.getgroups()
    old_gid = os.getegid()
    os.setgroups([])
    os.setegid(info.st_gid)
    os.seteuid(info.st_uid)
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(old_gid)
        os.setgroups(old_groups)


def _name_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _cursor_update(digest, directory, name, size, modified):
    relative = str(Path(directory, name)).lstrip("/")
    digest.update(relative.encode("utf-8", "surrogateescape"))
    digest.update(b"\0")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(modified).encode("ascii"))
    digest.update(b"\n")


def stream_keep(root, output_fd, selection, cursor=None, *, clock=time.monotonic):
    """Emit admitted files from inside the capability-free chroot.

    Records contain only a basename and bounded metadata followed by exactly
    ``size`` opaque bytes. Directory paths never cross the confinement pipe.
    """
    photos = selection is None
    wanted = _name_key(selection) if isinstance(selection, str) else None
    resume_position = cursor.get("position", 0) if isinstance(cursor, dict) else 0
    started = clock()
    root_dev = root.stat().st_dev
    candidates = []
    visited = 0
    rejected = 0
    truncated = False
    for directory, dirs, files, directory_fd in os.fwalk(root, topdown=True, follow_symlinks=False):
        order = lambda name: (_name_key(name), unicodedata.normalize("NFKC", name))
        dirs.sort(key=order)
        files.sort(key=order)
        depth = 0 if directory == "." else len(Path(directory).parts)
        if depth >= MAX_DEPTH:
            dirs.clear()
            truncated = True
        if clock() - started > MAX_SECONDS or visited >= KEEP_MAX_ITEMS:
            truncated = True
            break
        for name in files:
            if not photos and wanted not in (_name_key(name), _name_key(Path(name).stem)):
                continue
            kind = _kind(name)
            if photos and kind != "photos":
                continue
            # The item ceiling bounds selectable Keep entries, not unrelated
            # files. A stick with many documents before its photos must still
            # be able to advance; the wall-clock bound caps that traversal.
            visited += 1
            if visited > KEEP_MAX_ITEMS or clock() - started > MAX_SECONDS:
                truncated = True
                break
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_dev != root_dev
                    or info.st_size < 0 or info.st_size > KEEP_MAX_FILE):
                if photos and stat.S_ISREG(info.st_mode):
                    rejected += 1
                continue
            if (not 1 <= len(name) <= 140 or not name.isprintable() or name != name.strip()
                    or name in (".", "..") or "\\" in name):
                if photos:
                    rejected += 1
                continue
            candidates.append((directory, name, kind, info.st_size, int(info.st_mtime)))
            # A later batch still verifies the entire prefix, but gets a new
            # bounded discovery window once it reaches its saved boundary.
            if resume_position and len(candidates) == resume_position:
                started = clock()
        if not photos and len(candidates) > 1:
            break
    if not photos and len(candidates) != 1:
        _keep_frame(output_fd, {"type": "error", "error":
                                "I couldn't find one exact file by that name on the stick."})
        return

    position = resume_position
    expected_prefix = cursor.get("prefix_sha256") if isinstance(cursor, dict) else None
    if type(position) is not int or not 0 <= position <= len(candidates):
        _keep_frame(output_fd, {"type": "cursor-invalid"})
        return
    prefix = hashlib.sha256()
    for directory, name, _kind_name, size, modified in candidates[:position]:
        _cursor_update(prefix, directory, name, size, modified)
    if position and prefix.hexdigest() != expected_prefix:
        _keep_frame(output_fd, {"type": "cursor-invalid"})
        return

    copied, skipped, total = 0, rejected, 0
    copy_deadline = clock() + 30.0
    for index, (directory, name, kind, size, modified) in enumerate(candidates[position:], position):
        if copied >= KEEP_MAX_ITEMS or total + size > KEEP_MAX_BATCH or clock() >= copy_deadline:
            truncated = True
            break
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "rb", buffering=0) as source:
            current = os.fstat(source.fileno())
            if (not stat.S_ISREG(current.st_mode) or current.st_dev != root_dev
                    or current.st_size != size):
                skipped += 1
                _cursor_update(prefix, directory, name, size, modified)
                position = index + 1
                _keep_frame(output_fd, {"type": "progress", "position": position,
                                        "prefix_sha256": prefix.hexdigest()})
                continue
            _cursor_update(prefix, directory, name, size, modified)
            next_position = index + 1
            _keep_frame(output_fd, {"type": "file", "filename": name, "kind": kind,
                                    "size": size, "modified": modified, "position": next_position,
                                    "prefix_sha256": prefix.hexdigest()})
            remaining = size
            while remaining:
                chunk = source.read(min(1 << 20, remaining))
                if not chunk:
                    raise OSError("A USB file changed during the copy.")
                _write_all(output_fd, chunk)
                remaining -= len(chunk)
        copied += 1
        total += size
        position = next_position
    complete = position == len(candidates) and not truncated
    _keep_frame(output_fd, {"type": "done", "files": copied, "bytes": total,
                            "skipped": skipped, "partial": not complete or rejected > 0,
                            "complete": complete, "position": position,
                            "prefix_sha256": prefix.hexdigest()})


def summarize(root, *, clock=time.monotonic):
    """Summarize file metadata, never file contents, links or nested mounts."""
    started = clock()
    root_dev = root.stat().st_dev
    kinds, total, visited, largest, newest = Counter(), 0, 0, 0, None
    stack = [(root, 0)]
    truncated = False
    while stack:
        directory, depth = stack.pop()
        if clock() - started > MAX_SECONDS:
            truncated = True
            break
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ITEMS or clock() - started > MAX_SECONDS:
                        truncated = True
                        stack.clear()
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if info.st_dev != root_dev or stat.S_ISLNK(info.st_mode):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if depth < MAX_DEPTH:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            truncated = True
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    total += 1
                    kinds[_kind(entry.name)] += 1
                    largest = max(largest, info.st_size)
                    newest = max(newest or info.st_mtime, info.st_mtime)
        except OSError:
            continue
    return {"files": total, "kinds": dict(kinds), "largest_bytes": largest,
            "newest_epoch": newest, "truncated": truncated}


def _drop_reader_privileges(root, output_fd):
    """Enter the mounted tree and retain only the result pipe.

    Called after fork, before any untrusted directory entry is read. Failure
    aborts the child; scanning as root is never a fallback.
    """
    os.chdir(root)
    os.chroot(root)
    os.chdir("/")

    # Keep one known descriptor. stdin/stdout/stderr and systemd/journal
    # descriptors are closed, so the reader can write only its capped answer.
    os.dup2(output_fd, 3)
    if output_fd != 3:
        os.close(output_fd)
    os.closerange(4, 64)
    os.closerange(0, 3)

    libc = ctypes.CDLL(None, use_errno=True)
    # Remove every capability from the bounding set while still privileged.
    # EINVAL means this kernel has fewer capability numbers; other failures
    # mean the confinement could not be established.
    for capability in range(64):
        if libc.prctl(24, capability, 0, 0, 0) != 0 and ctypes.get_errno() not in (0, 22):
            raise OSError(ctypes.get_errno(), "could not drop capability bounding set")
    os.setgroups([])
    os.setresgid(READER_GID, READER_GID, READER_GID)
    os.setresuid(READER_UID, READER_UID, READER_UID)

    class CapHeader(ctypes.Structure):
        _fields_ = (("version", ctypes.c_uint32), ("pid", ctypes.c_int))

    class CapData(ctypes.Structure):
        _fields_ = (("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                    ("inheritable", ctypes.c_uint32))

    header = CapHeader(0x20080522, 0)
    data = (CapData * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "could not clear reader capabilities")
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "could not set no-new-privileges")
    return 3


def summarize_confined(root):
    """Fork a capability-free chrooted reader; parent remains able to unmount."""
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        try:
            os.close(read_fd)
            result_fd = _drop_reader_privileges(root, write_fd)
            payload = json.dumps({"result": summarize(Path("/"))}, separators=(",", ":")).encode("ascii")
            if len(payload) > MAX_OUTPUT:
                raise ValueError("reader result too large")
            os.write(result_fd, payload)
            os.close(result_fd)
            os._exit(0)
        except BaseException:
            for descriptor in dict.fromkeys((3, write_fd)):
                try:
                    os.write(descriptor, b'{"error":"confined reader could not start"}')
                    break
                except OSError:
                    continue
            os._exit(1)

    os.close(write_fd)
    deadline = time.monotonic() + 12.0
    data = bytearray()
    status = None
    try:
        while len(data) <= MAX_OUTPUT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("The confined media reader took too long.")
            ready, _, _ = select.select((read_fd,), (), (), remaining)
            if not ready:
                raise ValueError("The confined media reader took too long.")
            chunk = os.read(read_fd, min(4096, MAX_OUTPUT + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_OUTPUT:
            raise ValueError("The confined media reader returned too much information.")
        _, status = os.waitpid(pid, 0)
        pid = None
        answer = json.loads(data.decode("ascii"))
        if status != 0 or not isinstance(answer, dict) or not isinstance(answer.get("result"), dict):
            raise ValueError("The confined media reader could not inspect that stick.")
        return answer["result"]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("The confined media reader did not answer safely.") from error
    finally:
        os.close(read_fd)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def inspect(origin="usb"):
    disk = _candidate(origin=origin)
    medium = "SD card" if origin == "sd" else "USB stick"
    if not _same_disk(disk):
        raise ValueError(f"The {medium} changed before I could inspect it.")
    volumes = install_disks.volumes(disk["device"])
    if not volumes:
        raise ValueError("That stick has no readable volume.")
    last_error = None
    with tempfile.TemporaryDirectory(prefix="being-media-", dir="/run") as temporary:
        target = Path(temporary)
        for volume in volumes:
            if not _same_disk(disk):
                raise ValueError(f"The {medium} changed before I could inspect it.")
            mounted = False
            answer = None
            try:
                _mount(volume["device"], target)
                mounted = True
                if not _same_disk(disk) or not _mounted_from(target, volume["device"]):
                    raise ValueError(f"The {medium} changed while I was opening it.")
                answer = summarize_confined(target)
            except ValueError as error:
                last_error = error
            finally:
                # A mount command can time out after the kernel completed it.
                # Ownership is the private temporary target, so check the
                # actual mount table as well as the successful return path.
                if mounted or os.path.ismount(target):
                    unmounted = subprocess.run(("umount", str(target)), stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               timeout=8, check=False)
                    if unmounted.returncode:
                        raise ValueError("I read the stick, but couldn't make it safe to remove.")
            if answer is not None:
                return answer
    raise last_error or ValueError("I couldn't read that stick.")


def keep(selection, receipt, state_dir="/mind"):
    """Mount one candidate and feed confined opaque bytes only to Store."""
    from .backup import media_identity
    from .keep import MAX_BATCH as STORE_MAX_BATCH, Store

    if selection is not None and (not isinstance(selection, str) or not selection or len(selection) > 140):
        raise ValueError("That file name isn't safe to look for.")
    origin = receipt.get("origin", "usb") if isinstance(receipt, dict) else "usb"
    disk = _candidate(origin=origin)
    identity_keys = ["device", "device_number", "device_sequence"]
    if origin == "sd":
        identity_keys.append("card_identity")
    if (not _same_disk(disk) or not isinstance(receipt, dict)
            or any(disk.get(key) != receipt.get(key) for key in identity_keys)
            or type(receipt.get("device_sequence")) is not int):
        raise ValueError("The removable card or stick changed before I could open it.")
    volumes = install_disks.volumes(disk["device"])
    if not volumes:
        raise ValueError("That stick has no readable volume.")
    last_error = None
    with _keep_owner(state_dir):
        epoch = Store(state_dir).transfer_epoch()
    with tempfile.TemporaryDirectory(prefix="being-keep-", dir="/run") as temporary:
        target = Path(temporary)
        for volume in volumes:
            mounted = False
            read_fd = write_fd = None
            pid = None
            try:
                _mount(volume["device"], target)
                mounted = True
                if not _same_disk(disk) or not _mounted_from(target, volume["device"]):
                    raise ValueError("The USB stick changed while I was opening it.")
                media_uuid = media_identity(volume["device"])
                if media_uuid is None:
                    raise ValueError("That volume has no stable identity for a resumable copy.")
                selection_key = ("photos" if selection is None else
                                 hashlib.sha256(_name_key(selection).encode("utf-8")).hexdigest())
                with _keep_owner(state_dir):
                    progress = Store(state_dir).transfer_progress(media_uuid, selection_key)
                read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
                pid = os.fork()
                if pid == 0:
                    try:
                        os.close(read_fd)
                        result_fd = _drop_reader_privileges(target, write_fd)
                        stream_keep(Path("/"), result_fd, selection, progress)
                        os.close(result_fd)
                        os._exit(0)
                    except BaseException:
                        # Never append a framed error after a file header: the
                        # receiver could mistake those bytes for file content.
                        # Closing early makes Store.ingest reject the copy.
                        os._exit(1)
                os.close(write_fd)
                write_fd = None
                imported = duplicates = skipped = total = 0
                partial = False
                with _keep_owner(state_dir):
                    store = Store(state_dir)
                    while True:
                        record = _keep_record(read_fd)
                        kind = record.get("type")
                        if kind == "cursor-invalid":
                            store.save_transfer_progress(media_uuid, selection_key, epoch=epoch)
                            # Do not try another partition under this receipt:
                            # changed content needs a fresh owner confirmation.
                            raise OSError("The stick's contents changed. Ask me to copy them again.")
                        if kind == "error":
                            raise ValueError(str(record.get("error") or "The confined media reader failed."))
                        if kind == "done":
                            skipped += int(record.get("skipped", 0))
                            partial = bool(record.get("partial")) or skipped > 0
                            if record.get("complete") is True:
                                store.save_transfer_progress(media_uuid, selection_key, epoch=epoch)
                            break
                        if kind == "progress":
                            store.save_transfer_progress(media_uuid, selection_key,
                                                         record.get("position"), record.get("prefix_sha256"), epoch=epoch)
                            continue
                        if kind != "file":
                            raise ValueError("The confined media reader returned an unsafe record.")
                        size = record.get("size")
                        if type(size) is not int or size < 0 or total + size > STORE_MAX_BATCH:
                            raise ValueError("The USB transfer exceeded its safe batch size.")
                        added = store.ingest(record.get("filename"), record.get("kind"), size,
                                             record.get("modified"), lambda amount: os.read(read_fd, amount), epoch=epoch)
                        total += size
                        imported += bool(added)
                        duplicates += not added
                        store.save_transfer_progress(media_uuid, selection_key,
                                                     record.get("position"), record.get("prefix_sha256"), epoch=epoch)
                _, status = os.waitpid(pid, 0)
                pid = None
                if status != 0:
                    raise ValueError("The confined media reader couldn't finish the copy.")
                if not _same_disk(disk) or not _mounted_from(target, volume["device"]):
                    raise ValueError("The USB stick changed during the copy.")
                return {"imported": imported, "duplicates": duplicates, "skipped": skipped,
                        "partial": partial, "photos": selection is None}
            except ValueError as error:
                last_error = error
            finally:
                for descriptor in (read_fd, write_fd):
                    if descriptor is not None:
                        os.close(descriptor)
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)
                if mounted or os.path.ismount(target):
                    result = subprocess.run(("umount", str(target)), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            timeout=8, check=False)
                    if result.returncode:
                        raise ValueError("I copied from the stick, but couldn't make it safe to remove.")
    raise last_error or ValueError("I couldn't read that stick.")


def eject():
    disk = _candidate(inspect_contents=False)
    if not _same_disk(disk):
        raise ValueError("The USB stick changed before I could eject it.")
    # _candidate rejects every mounted origin.  Flush the device buffers but
    # do not mount it, change it, or claim to remove USB power.
    result = subprocess.run(("blockdev", "--flushbufs", disk["device"]), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=5, check=False)
    if result.returncode:
        raise ValueError("I couldn't make that stick safe to remove.")
    return {"safe": True}


def _line(result):
    if result.get("safe"):
        return "[neutral] You can take the stick out now."
    total = int(result.get("files", 0))
    kinds = result.get("kinds") or {}
    parts = [f"{count} {kind}" for kind, count in sorted(kinds.items()) if count]
    detail = ", ".join(parts[:4])
    line = f"I found {total} file{'s' if total != 1 else ''}"
    if detail:
        line += f": {detail}"
    largest = int(result.get("largest_bytes", 0))
    if largest:
        if largest >= 1 << 30:
            size = f"{largest / (1 << 30):.1f} gigabytes"
        elif largest >= 1 << 20:
            size = f"{largest / (1 << 20):.1f} megabytes"
        else:
            size = f"{max(1, largest // 1024)} kilobytes"
        line += f". The largest is {size}"
    if result.get("truncated"):
        line += ". I stopped after a bounded look, so there may be more"
    return f"[neutral] {line}."


async def _run(action):
    command = (
        "systemd-run", "--unit=being-removable-media", "--quiet", "--wait", "--pipe", "--collect",
        "--service-type=exec",
        "--property=Type=exec", "--property=PrivateMounts=yes",
        "--property=PrivateNetwork=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes", "--property=PrivateTmp=yes", "--property=NoNewPrivileges=yes",
        "--property=RestrictAddressFamilies=AF_UNIX", "--property=DevicePolicy=closed",
        "--property=DeviceAllow=block-* r", "--property=ReadWritePaths=/run",
        "--property=MemoryMax=256M", "--property=MemorySwapMax=0", "--property=TasksMax=16",
        "--property=Nice=10", "--property=IOSchedulingClass=idle",
        "--property=RuntimeMaxSec=20s", "--property=TimeoutStopSec=5s",
        "/usr/sbin/being-media", action,
    )
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
                                                   stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 25)
    except (asyncio.CancelledError, TimeoutError) as stopped:
        if process.returncode is None:
            process.kill()
            await process.wait()
        try:
            stop = await asyncio.create_subprocess_exec(
                "systemctl", "--no-block", "stop", "being-removable-media.service",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(stop.wait(), 5)
        except (OSError, TimeoutError):
            pass
        if isinstance(stopped, asyncio.CancelledError):
            raise
        raise ValueError("I stopped because the stick took too long to answer.")
    if len(output) > MAX_OUTPUT:
        raise ValueError("The media reader returned too much information.")
    try:
        answer = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("The media reader didn't answer safely.") from None
    if process.returncode or not isinstance(answer, dict):
        raise ValueError(str(answer.get("error", "I couldn't inspect that stick."))
                         if isinstance(answer, dict) else "I couldn't inspect that stick.")
    return answer


async def handle(text, ctx):
    action = classify(text)
    if action is None:
        return None
    if is_guest(ctx) or ctx.speaker != ctx.owner:
        return Reply("[neutral] Only my owner can inspect removable media.")
    lid_open = getattr(ctx, "lid_open", None)
    if lid_open is None:
        current_body = getattr(ctx, "current_body", None)
        lid_open = callable(current_body) and current_body() is not None
    if not lid_open:
        return Reply("[neutral] Open my lid before I handle removable media.")
    try:
        return Reply(_line(await _run(action)))
    except (OSError, ValueError) as error:
        return Reply(f"[concern] {error}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-media")
    parser.add_argument("action", choices=("inspect", "inspect-sd", "eject", "keep"))
    args = parser.parse_args(argv)
    try:
        _limits(KEEP_MAX_FILE if args.action == "keep" else 0)
        if args.action == "keep":
            request = sys.stdin.buffer.read(1024)
            if len(request) >= 1024:
                raise ValueError("The keep request was too large.")
            receipt = json.loads(request.decode("utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("The keep request was invalid.")
            result = keep(receipt.get("selection"), receipt)
            print(json.dumps(result, separators=(",", ":")))
            return 0
        result = inspect("sd") if args.action == "inspect-sd" else (inspect() if args.action == "inspect" else eject())
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        if getattr(args, "action", None) == "keep":
            print(json.dumps({"error": str(error)}, separators=(",", ":")))
            return 1
        print(json.dumps({"error": str(error)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
