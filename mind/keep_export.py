"""Explicit, confirmed Keep-to-USB export.

``identify`` makes a short-lived receipt for the one safe foreign USB disk.
Only ``export`` starts the fixed transient service.  The service receives the
name and bytes on stdin, mounts privately, and never replaces an existing
file.  Owner filenames therefore do not appear in its argv or journal.
"""

import asyncio
import ctypes
import hashlib
import json
import os
import resource
import secrets
import select
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import install_disks, removable_media
from .keep import CHUNK, MAX_FILE, Item, Store, valid_filename

UNIT = "being-keep-export"
MAX_HEADER = 4096
RECEIPT_SECONDS = 120
RENAME_NOREPLACE = 1
MOUNTS = (
    ("vfat", "rw,nodev,nosuid,noexec,noatime,utf8=1,fmask=0177,dmask=0077,flush"),
    ("exfat", "rw,nodev,nosuid,noexec,noatime,fmask=0177,dmask=0077"),
)


@dataclass(frozen=True)
class Receipt:
    device: str
    device_number: str
    device_sequence: int
    issued: float
    description: str
    origin: str = "usb"
    card_identity: str | None = None


def _identity(disk):
    return {
        "device": disk.get("device"),
        "device_number": disk.get("device_number"),
        "device_sequence": disk.get("device_sequence"),
    }


def _receipt_sync():
    disk = removable_media._candidate()
    if not removable_media._same_disk(disk):
        raise ValueError("The USB stick changed before I could identify it.")
    identity = _identity(disk)
    if not identity["device_number"] or type(identity["device_sequence"]) is not int:
        raise ValueError("I couldn't verify the current USB stick's identity.")
    size = disk.get("size_bytes")
    if type(size) is not int or size <= 0:
        raise ValueError("I couldn't verify that USB stick's capacity.")
    gib = max(1, round(size / (1 << 30)))
    return Receipt(**identity, issued=time.monotonic(),
                   description=f"the {gib}-gigabyte USB stick")


async def identify(state_dir):
    """Return the exact, bounded disk receipt to retain across confirmation."""
    del state_dir  # Symmetric public API; no Keep data is opened at this stage.
    return await asyncio.to_thread(_receipt_sync)


def _command():
    return (
        "systemd-run", f"--unit={UNIT}", "--quiet", "--wait", "--pipe", "--collect",
        "--service-type=exec", "--property=Type=exec", "--property=PrivateMounts=yes",
        "--property=PrivateNetwork=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes", "--property=PrivateTmp=yes", "--property=NoNewPrivileges=yes",
        "--property=RestrictAddressFamilies=AF_UNIX", "--property=DevicePolicy=closed",
        "--property=DeviceAllow=block-* rw", "--property=ReadWritePaths=/run",
        "--property=CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETPCAP", "--property=MemoryMax=192M",
        "--property=InaccessiblePaths=-/mind -/models -/workshop",
        "--property=Nice=10", "--property=IOSchedulingClass=idle",
        "--property=MemorySwapMax=0", "--property=TasksMax=12",
        "--property=RuntimeMaxSec=75s", "--property=TimeoutStopSec=8s",
        "--setenv=PYTHONPATH=/usr/lib/being", "--setenv=PYTHONDONTWRITEBYTECODE=1",
        "/usr/bin/python3", "-m", "mind.keep_export", "worker",
    )


def _write_all(stream, payload):
    while payload:
        written = stream.write(payload)
        if not written:
            raise OSError("The USB export stopped accepting data.")
        payload = payload[written:]


def _write_fd(fd, payload):
    while payload:
        written = os.write(fd, payload)
        if written <= 0:
            raise OSError("The USB export stopped writing.")
        payload = payload[written:]


def _worker_answer(process, timeout=85):
    """Read one bounded service answer without trusting RuntimeMaxSec alone."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    descriptor = process.stdout.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_unit()
            raise TimeoutError("The USB export took too long.")
        ready, _, _ = select.select((descriptor,), (), (), remaining)
        if not ready:
            _stop_unit()
            raise TimeoutError("The USB export took too long.")
        chunk = os.read(descriptor, MAX_HEADER + 1 - len(output))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > MAX_HEADER:
            raise ValueError("The USB export returned too much information.")
    try:
        return json.loads(output.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("The USB export did not answer safely.") from None


def _export_sync(state_dir, item, receipt, cancelled):
    if (not isinstance(item, Item) or not valid_filename(item.filename)
            or type(item.size) is not int or not 0 <= item.size <= MAX_FILE):
        raise ValueError("That kept file cannot be exported safely.")
    if (not isinstance(receipt, Receipt) or receipt.origin != "usb"
            or time.monotonic() - receipt.issued > RECEIPT_SECONDS):
        raise ValueError("That USB confirmation expired. Ask me again.")
    header = json.dumps({**_identity(receipt.__dict__), "filename": item.filename,
                         "size": item.size, "digest": item.digest},
                        ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(header) > MAX_HEADER:
        raise ValueError("That export request is too large.")
    process = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    observed = hashlib.sha256()
    try:
        _write_all(process.stdin, struct.pack(">I", len(header)) + header)
        with Store(state_dir).open(item.digest) as source:
            info = os.fstat(source.fileno())
            if info.st_size != item.size:
                raise OSError("The kept file changed before export.")
            remaining = item.size
            while remaining:
                if cancelled.is_set():
                    raise asyncio.CancelledError
                chunk = source.read(min(CHUNK, remaining))
                if not chunk:
                    raise OSError("The kept file ended during export.")
                observed.update(chunk)
                _write_all(process.stdin, chunk)
                remaining -= len(chunk)
        if observed.hexdigest() != item.digest:
            raise OSError("The kept file did not verify for export.")
        process.stdin.close()
        answer = _worker_answer(process)
        try:
            returncode = process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _stop_unit()
            raise TimeoutError("The USB export took too long.") from None
        if isinstance(answer, dict) and answer.get("error"):
            raise ValueError(str(answer.get("error") or "I couldn't export that file."))
        if returncode or answer != {"ok": True}:
            raise ValueError("I couldn't finish writing that file to the stick.")
        return f"[neutral] I copied {item.filename} to the USB stick without replacing anything."
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _stop_unit():
    subprocess.run(("systemctl", "stop", f"{UNIT}.service"),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=8, check=False)
    # A failed transient can otherwise retain the fixed name until the
    # manager's later garbage collection, blocking the owner's retry.
    subprocess.run(("systemctl", "reset-failed", f"{UNIT}.service"),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=5, check=False)


async def export(state_dir, item, receipt):
    cancelled = threading.Event()
    work = asyncio.create_task(asyncio.to_thread(_export_sync, state_dir, item, receipt, cancelled))
    try:
        # Cancelling the caller must not cancel this Task object: the thread
        # needs to observe its flag and finish the unit/unmount cleanup.
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        cancelled.set()
        await asyncio.to_thread(_stop_unit)
        try:
            await asyncio.wait_for(asyncio.shield(work), 10)
        except (asyncio.CancelledError, TimeoutError, OSError, ValueError):
            pass
        raise


def _read_exact(stream, size):
    chunks = []
    while size:
        chunk = stream.read(min(CHUNK, size))
        if not chunk:
            raise OSError("The export input ended early.")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _same_receipt(request):
    expected = {key: request.get(key) for key in ("device", "device_number", "device_sequence")}
    return removable_media._same_disk(expected)


def _mount(device, target):
    for filesystem, options in MOUNTS:
        result = subprocess.run(("mount", "-t", filesystem, "-o", options, device, str(target)),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=6, check=False)
        if result.returncode == 0:
            return
    raise ValueError("I can only write to FAT and exFAT USB sticks.")


def _publish_noreplace(directory_fd, temporary, filename):
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError("This system cannot safely publish the exported file.")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(directory_fd, os.fsencode(temporary), directory_fd,
                 os.fsencode(filename), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        if error == 17:  # EEXIST, kept literal to avoid platform-dependent errno aliases.
            raise FileExistsError("A file with that name is already on the stick.")
        raise OSError(error, "The exported file could not be published safely.")


def _write_on_mount(directory, temporary, filename, length, digest, stream):
    from .backup import _drop_all_capabilities

    pid = os.fork()
    if pid == 0:
        try:
            _drop_all_capabilities()
            space = os.fstatvfs(directory)
            if space.f_bavail * space.f_frsize < length + CHUNK:
                os._exit(21)
            published = False
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    copied = hashlib.sha256()
                    remaining = length
                    while remaining:
                        chunk = _read_exact(stream, min(CHUNK, remaining))
                        copied.update(chunk)
                        _write_fd(fd, chunk)
                        remaining -= len(chunk)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if copied.hexdigest() != digest:
                    raise OSError("The exported bytes did not verify.")
                _publish_noreplace(directory, temporary, filename)
                published = True
                check = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(check, "rb") as readback:
                    info = os.fstat(readback.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_size != length
                            or hashlib.file_digest(readback, "sha256").hexdigest() != digest):
                        raise OSError("The USB readback did not match the kept file.")
                os.fsync(directory)
            except BaseException:
                for name in ((temporary, filename) if published else (temporary,)):
                    try:
                        os.unlink(name, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                os.fsync(directory)
                raise
            os._exit(0)
        except FileExistsError:
            os._exit(20)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code == 20:
        raise FileExistsError("A file with that name is already on the stick.")
    if code == 21:
        raise ValueError("There isn't enough room left on that stick.")
    if code != 0:
        raise OSError("The confined writer couldn't verify the exported file.")


def _worker():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE))
    resource.setrlimit(resource.RLIMIT_NOFILE, (48, 48))
    resource.setrlimit(resource.RLIMIT_AS, (160 << 20, 160 << 20))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    stream = sys.stdin.buffer
    size = struct.unpack(">I", _read_exact(stream, 4))[0]
    if size > MAX_HEADER:
        raise ValueError("The export request is unsafe.")
    request = json.loads(_read_exact(stream, size).decode("ascii"))
    filename, length, digest = request.get("filename"), request.get("size"), request.get("digest")
    if (not valid_filename(filename) or type(length) is not int or not 0 <= length <= MAX_FILE
            or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            or not _same_receipt(request)):
        raise ValueError("The export request no longer matches that USB stick.")
    volumes = install_disks.volumes(request["device"])
    if not volumes:
        raise ValueError("That USB stick has no writable FAT or exFAT volume.")
    last_error = None
    with tempfile.TemporaryDirectory(prefix="being-keep-export-", dir="/run") as temporary_root:
        target = Path(temporary_root)
        for volume in volumes:
            mounted = False
            consumed = False
            temporary = f".being-export-{secrets.token_hex(12)}"
            directory_fd = None
            try:
                if not _same_receipt(request):
                    raise ValueError("The USB stick changed before export.")
                _mount(volume["device"], target)
                mounted = True
                if not _same_receipt(request) or not removable_media._mounted_from(target, volume["device"]):
                    raise ValueError("The USB stick changed while it was opened.")
                directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                if os.fstat(directory_fd).st_dev != os.stat(volume["device"]).st_rdev:
                    raise ValueError("The USB volume changed during export.")
                consumed = True
                _write_on_mount(directory_fd, temporary, filename, length, digest, stream)
                if not _same_receipt(request) or not removable_media._mounted_from(target, volume["device"]):
                    raise ValueError("The USB stick changed during export.")
                return
            except (OSError, ValueError) as error:
                last_error = error
                if directory_fd is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except OSError:
                        pass
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
                if mounted or os.path.ismount(target):
                    result = subprocess.run(("umount", str(target)), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            timeout=8, check=False)
                    if result.returncode or os.path.ismount(target):
                        raise ValueError("The file was written, but the stick could not be unmounted safely.")
            # An unsupported volume consumed nothing, so another partition is
            # safe to try. Once copying starts, the input cannot be replayed.
            if consumed:
                break
    raise last_error or ValueError("I couldn't write to that USB stick.")


def main(argv=None):
    if argv != ["worker"]:
        return 2
    try:
        _worker()
        print('{"ok":true}')
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        # Never serialize an arbitrary OS exception: it can contain the
        # destination filename. ValueErrors above are deliberately fixed.
        if isinstance(error, FileExistsError):
            message = "A file with that name is already on the stick."
        elif type(error) is ValueError:
            message = str(error)
        else:
            message = "The USB export could not finish safely."
        print(json.dumps({"error": message}, ensure_ascii=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
