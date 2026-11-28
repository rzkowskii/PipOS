"""Incremental, encrypted per-file backups of the owner's Keep.

The enrolled backup filesystem is mounted only in one transient service. Each
Keep object is an immutable directory named by its plaintext digest; its data
and private filename metadata are separate age ciphertexts. Existing objects
are skipped only after their ciphertext checksums verify. Invalid objects are
never overwritten, so an interrupted run is safe to retry incrementally.
"""

import asyncio
import ctypes
import hashlib
import json
import os
import re
import resource
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from . import backup
from .backup_crypto import BackupCryptoError, IDENTITY, RECIPIENT, _read_identity, encrypt
from .keep import MAX_FILE, Item, Store

UNIT = "being-keep-file-backup"
ROOT = "keep-files-v1"
DATA = "content.age"
META = "metadata.age"
PROOF = "cipher-sha256.json"
MAX_REPLY = 4096
RENAME_NOREPLACE = 1
SHA256 = re.compile(r"[0-9a-f]{64}")
META_CLEAR_MAX = 4096
META_CIPHER_MAX = 64 * 1024
DATA_CIPHER_MAX = MAX_FILE + (1 << 20)


def _items(store):
    uri = f"{(store.root / 'index.sqlite').resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        return tuple(Item(*row) for row in db.execute(
            "SELECT digest, filename, kind, size, modified FROM items ORDER BY digest"))


def _store_read_only(state_dir):
    """Validate the installed Keep without running Store's schema writer."""
    root = Path(state_dir) / "keep"
    objects = root / "objects"
    for directory in (root, objects):
        info = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("I couldn't open the private Keep for backup.")
    index = root / "index.sqlite"
    descriptor = os.open(index, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("I couldn't open the private Keep index for backup.")
    finally:
        os.close(descriptor)
    store = object.__new__(Store)
    store.root, store.objects = root, objects
    return store


def _cipher_limit(name):
    return META_CIPHER_MAX if name == META else DATA_CIPHER_MAX if name == DATA else 0


def _open_cipher(directory, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        limit = _cipher_limit(name)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or not limit or info.st_size > limit:
            raise ValueError("An incremental Keep backup object is invalid.")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _regular_hash(directory, name):
    with _open_cipher(directory, name) as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _read_proof(directory):
    descriptor = os.open(PROOF, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        raw = source.read(1025)
    if not stat.S_ISREG(info.st_mode) or len(raw) > 1024:
        raise ValueError("An incremental Keep backup proof is invalid.")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("An incremental Keep backup proof is invalid.") from None
    if (not isinstance(value, dict) or set(value) != {DATA, META}
            or any(not isinstance(digest, str) or not SHA256.fullmatch(digest) for digest in value.values())):
        raise ValueError("An incremental Keep backup proof is invalid.")
    return value


def _verified_existing(root, digest):
    try:
        directory = os.open(digest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=root)
    except FileNotFoundError:
        return False
    try:
        proof = _read_proof(directory)
        if any(_regular_hash(directory, name) != proof[name] for name in (DATA, META)):
            raise ValueError("An incremental Keep backup object is damaged; I left it unchanged.")
        return True
    finally:
        os.close(directory)


def _rename_noreplace(root, source, destination):
    function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if function is None:
        raise OSError("This system cannot safely publish incremental Keep backups.")
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(root, os.fsencode(source), root, os.fsencode(destination), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        if error == 17:
            raise FileExistsError
        raise OSError(error, "Could not publish the incremental Keep backup safely.")


def _encrypt_to(directory, name, recipient, source):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
    with os.fdopen(descriptor, "w+b") as destination:
        encrypt(recipient, source, destination)
        destination.flush()
        os.fsync(destination.fileno())


def _write_object(root, item, recipient, source):
    """Capability-free child side. Return 0 new, 10 already valid."""
    from .backup import _drop_all_capabilities

    _drop_all_capabilities()
    if _verified_existing(root, item.digest):
        return 10
    space = os.fstatvfs(root)
    # Age overhead is small, but reserve a full chunk as well as the normal
    # backup-media floor before starting an object that cannot be resumed.
    if space.f_bavail * space.f_frsize < item.size + backup.BACKUP_MEDIA_RESERVE + (1 << 20):
        raise OSError("The enrolled backup stick does not have enough free space.")
    pending = f".{item.digest}.{secrets.token_hex(8)}.pending"
    os.mkdir(pending, mode=0o700, dir_fd=root)
    directory = os.open(pending, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=root)
    try:
        source.seek(0)
        _encrypt_to(directory, DATA, recipient, source)
        metadata = json.dumps({"version": 1, "digest": item.digest, "filename": item.filename,
                               "kind": item.kind, "size": item.size, "modified": item.modified},
                              sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with tempfile.TemporaryFile(mode="w+b", dir="/run") as clear:
            clear.write(metadata)
            clear.seek(0)
            _encrypt_to(directory, META, recipient, clear)
        proof = {name: _regular_hash(directory, name) for name in (DATA, META)}
        descriptor = os.open(PROOF, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as output:
            output.write(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    try:
        _rename_noreplace(root, pending, item.digest)
    except FileExistsError:
        # Another completed invocation may have won. It is acceptable only if
        # its immutable object verifies; the pending directory is then removed.
        if not _verified_existing(root, item.digest):
            raise ValueError("An incremental Keep backup object is invalid; I left it unchanged.")
        _remove_pending(root, pending)
        return 10
    os.fsync(root)
    directory = os.open(item.digest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=root)
    try:
        proof = _read_proof(directory)
        if any(_regular_hash(directory, name) != proof[name] for name in (DATA, META)):
            raise OSError("The incremental Keep backup did not read back correctly.")
    finally:
        os.close(directory)
    return 0


def _remove_pending(root, name):
    try:
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    except FileNotFoundError:
        return
    try:
        for child in (DATA, META, PROOF):
            try:
                os.unlink(child, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory)
    os.rmdir(name, dir_fd=root)


def _clean_pending(root):
    pattern = re.compile(r"\.[0-9a-f]{64}\.[0-9a-f]{16}\.pending")
    for name in os.listdir(root):
        if pattern.fullmatch(name):
            try:
                _remove_pending(root, name)
            except OSError:
                raise ValueError("An interrupted Keep backup could not be cleaned up safely.") from None


def _copy_one(root, store, item, recipient):
    try:
        source = store.open(item.digest)
    except FileNotFoundError:
        return "missing"
    with source:
        info = os.fstat(source.fileno())
        if info.st_size != item.size or hashlib.file_digest(source, "sha256").hexdigest() != item.digest:
            raise OSError("A kept file did not verify before backup.")
        source.seek(0)
        pid = os.fork()
        if pid == 0:
            try:
                os._exit(_write_object(root, item, recipient, source))
            except BaseException:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code not in (0, 10):
        raise OSError("The confined Keep backup writer failed.")
    return "added" if code == 0 else "verified"


def operation(state_dir, device, expected_uuid, recipient):
    if str(state_dir) != "/mind" or backup.media_binding(state_dir) != expected_uuid:
        raise ValueError("The enrolled backup destination changed.")
    if not RECIPIENT.fullmatch(recipient) or backup.media_identity(device) != expected_uuid:
        raise ValueError("The enrolled backup destination is unavailable.")
    store = _store_read_only(state_dir)
    items = _items(store)
    mounted = False
    with backup.backup_lock():
        try:
            mount = backup.mount_stick(device)
            mounted = True
            if backup.media_identity(device) != expected_uuid or not backup.mounted_from(mount, device):
                raise ValueError("The enrolled backup stick changed while it was opened.")
            folder = mount / backup.BACKUP_FOLDER
            folder.mkdir(mode=0o700, exist_ok=True)
            destination = folder / ROOT
            destination.mkdir(mode=0o700, exist_ok=True)
            root = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if os.fstat(root).st_dev != os.stat(device).st_rdev:
                    raise ValueError("The enrolled backup volume changed.")
                _clean_pending(root)
                counts = {"added": 0, "verified": 0, "missing": 0}
                for item in items:
                    counts[_copy_one(root, store, item, recipient)] += 1
                os.fsync(root)
            finally:
                os.close(root)
            if backup.media_identity(device) != expected_uuid or not backup.mounted_from(mount, device):
                raise ValueError("The enrolled backup stick changed during the Keep backup.")
            return {**counts, "total": len(items)}
        finally:
            if mounted or os.path.ismount(backup.STICK_DIR):
                backup.unmount_stick()


def _command(device, uuid):
    return (
        "systemd-run", f"--unit={UNIT}", "--quiet", "--wait", "--pipe", "--collect",
        "--service-type=exec", "--property=Type=exec", "--property=PrivateMounts=yes",
        "--property=PrivateNetwork=yes", "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETPCAP",
        "--property=AmbientCapabilities=CAP_SYS_ADMIN CAP_SETPCAP", "--property=DevicePolicy=closed",
        f"--property=DeviceAllow={device} rw", "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=ReadOnlyPaths=/mind/keep", f"--property=ReadWritePaths={backup.STICK_DIR} /run",
        "--property=MemoryMax=192M", "--property=MemorySwapMax=0", "--property=TasksMax=12",
        "--property=Nice=10", "--property=IOSchedulingClass=idle",
        "--property=RuntimeMaxSec=30min", "--property=TimeoutStopSec=10s",
        "--setenv=PYTHONPATH=/usr/lib/being", "--setenv=PYTHONDONTWRITEBYTECODE=1",
        "/usr/sbin/being-keep-file-backup", "worker", "--device", device, "--uuid", uuid,
    )


def _ready(state_dir):
    uuid = backup.media_binding(state_dir)
    device = backup.bound_device(state_dir) if uuid is not None else None
    recipient = backup.recipient_on_file(state_dir)
    if uuid is None or device is None:
        raise ValueError("Insert the backup stick enrolled in Care first.")
    if recipient is None:
        raise ValueError("Set up the encrypted backup key in Care first.")
    return device, uuid, recipient


def _valid_restore_mind(state_dir):
    path = Path(state_dir).resolve(strict=True)
    runtime = Path("/run").resolve()
    if path != Path("/mind") and not path.is_relative_to(runtime):
        raise ValueError("Keep blob recovery needs the active mind or a private runtime restore target.")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("The Keep recovery target is not private.")
    if any(character.isspace() or ord(character) < 32 for character in str(path)):
        raise ValueError("The Keep recovery target path is not safe for the private service.")
    return path


def _valid_media(device, uuid):
    if (not isinstance(uuid, str) or not backup.FILESYSTEM_UUID.fullmatch(uuid)
            or not isinstance(device, str) or not device.startswith("/dev/")
            or any(character.isspace() or ord(character) < 32 for character in device)
            or backup.media_identity(device) != uuid):
        raise ValueError("The selected backup stick identity is invalid.")


def _mount_read_only(device):
    from .local_menu import RECOVERY_MOUNTS

    backup.unmount_stick()
    backup.STICK_DIR.mkdir(mode=0o700, exist_ok=True)
    for filesystem, options in RECOVERY_MOUNTS:
        result = subprocess.run(("mount", "-t", filesystem, "-o", options, device, str(backup.STICK_DIR)),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=60, check=False)
        if result.returncode == 0:
            return backup.STICK_DIR
    backup.unmount_stick()
    raise ValueError("The enrolled backup stick could not be mounted read-only.")


def _decrypt_bounded(identity, source, destination, limit):
    """Authenticate with native age while the age process cannot exceed limit."""
    key = _read_identity(identity)
    reader, writer = os.pipe()
    try:
        with os.fdopen(writer, "wb") as key_stream:
            key_stream.write(key)

        def set_limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

        result = subprocess.run(
            ("age", "--decrypt", "--identity", f"/proc/self/fd/{reader}"),
            stdin=source, stdout=destination, stderr=subprocess.DEVNULL,
            pass_fds=(reader,), preexec_fn=set_limit, check=False)
        if result.returncode:
            raise BackupCryptoError("The bounded Keep backup object could not be authenticated.")
    except OSError:
        raise BackupCryptoError("Could not pass the private key to bounded age recovery.") from None
    finally:
        os.close(reader)


def _decrypt_metadata(directory, identity, item):
    with _open_cipher(directory, META) as encrypted, tempfile.TemporaryFile(mode="w+b", dir="/run") as clear:
        _decrypt_bounded(identity, encrypted, clear, META_CLEAR_MAX)
        if clear.tell() > META_CLEAR_MAX:
            raise ValueError("The encrypted Keep metadata is too large.")
        clear.seek(0)
        try:
            value = json.loads(clear.read().decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("The encrypted Keep metadata is invalid.") from None
    expected = {"version": 1, "digest": item.digest, "filename": item.filename, "kind": item.kind,
                "size": item.size, "modified": item.modified}
    if value != expected:
        raise ValueError("The encrypted Keep metadata does not match the current catalog.")


def _restore_one(store, root, identity, item):
    try:
        with store.open(item.digest):
            return "skipped"
    except FileNotFoundError:
        pass
    try:
        directory = os.open(item.digest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=root)
    except FileNotFoundError:
        return "missing"
    try:
        proof = _read_proof(directory)
        if any(_regular_hash(directory, name) != proof[name] for name in (DATA, META)):
            raise ValueError("An encrypted Keep backup object is damaged.")
        _decrypt_metadata(directory, identity, item)
        free = os.statvfs("/run")
        if free.f_bavail * free.f_frsize < item.size + (16 << 20):
            raise ValueError("There is not enough private runtime space to recover that kept file.")
        with _open_cipher(directory, DATA) as encrypted, tempfile.TemporaryFile(mode="w+b", dir="/run") as clear:
            _decrypt_bounded(identity, encrypted, clear, item.size)
            size = clear.tell()
            clear.seek(0)
            digest = hashlib.file_digest(clear, "sha256").hexdigest()
            if size != item.size or digest != item.digest:
                raise ValueError("The recovered Keep file did not match its catalog digest.")
            clear.seek(0)
            restore = getattr(store, "restore_blob", None)
            if restore is None:
                raise ValueError("This image cannot atomically recover Keep blobs.")
            return "restored" if restore(item, clear.read) else "skipped"
    finally:
        os.close(directory)


def _restore_child(state_dir, root, identity):
    from .backup import _drop_all_capabilities

    _drop_all_capabilities()
    store = Store(state_dir)
    counts = {"restored": 0, "missing": 0, "skipped": 0}
    for item in _items(store):
        counts[_restore_one(store, root, identity, item)] += 1
    return counts


def restore_operation(state_dir, device, expected_uuid, identity):
    state_dir = _valid_restore_mind(state_dir)
    _valid_media(device, expected_uuid)
    mounted = False
    with backup.backup_lock():
        try:
            mount = _mount_read_only(device)
            mounted = True
            if backup.media_identity(device) != expected_uuid or not backup.mounted_from(mount, device):
                raise ValueError("The selected backup stick changed while it was opened.")
            destination = mount / backup.BACKUP_FOLDER / ROOT
            root = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            reader, writer = os.pipe2(os.O_CLOEXEC)
            pid = os.fork()
            if pid == 0:
                os.close(reader)
                try:
                    counts = _restore_child(state_dir, root, identity)
                    os.write(writer, json.dumps(counts, separators=(",", ":")).encode("ascii"))
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(writer)
            try:
                raw = os.read(reader, MAX_REPLY + 1)
            finally:
                os.close(reader)
                os.close(root)
            _, status = os.waitpid(pid, 0)
            if os.waitstatus_to_exitcode(status) != 0 or len(raw) > MAX_REPLY:
                raise ValueError("The confined Keep recovery failed safely.")
            counts = json.loads(raw.decode("ascii"))
            if backup.media_identity(device) != expected_uuid or not backup.mounted_from(mount, device):
                raise ValueError("The selected backup stick changed during Keep recovery.")
            return counts
        finally:
            if mounted or os.path.ismount(backup.STICK_DIR):
                backup.unmount_stick()


def _restore_command(state_dir, device, uuid):
    return (
        "systemd-run", f"--unit={UNIT}-restore", "--quiet", "--wait", "--pipe", "--collect",
        "--service-type=exec", "--property=Type=exec", "--property=PrivateMounts=yes",
        "--property=PrivateNetwork=yes", "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETPCAP",
        "--property=AmbientCapabilities=CAP_SYS_ADMIN CAP_SETPCAP", "--property=DevicePolicy=closed",
        f"--property=DeviceAllow={device} r", "--property=RestrictAddressFamilies=AF_UNIX",
        f"--property=ReadWritePaths={state_dir} {backup.STICK_DIR} /run",
        "--property=MemoryMax=256M", "--property=MemorySwapMax=0", "--property=TasksMax=12",
        "--property=RuntimeMaxSec=30min", "--property=TimeoutStopSec=10s",
        "--setenv=PYTHONPATH=/usr/lib/being", "--setenv=PYTHONDONTWRITEBYTECODE=1",
        "/usr/sbin/being-keep-file-backup", "restore-worker", "--mind-dir", str(state_dir),
        "--device", device, "--uuid", uuid,
    )


async def back_up(state_dir):
    """Owner-confirmed API. Every file and private filename is age-encrypted."""
    device, uuid, recipient = await asyncio.to_thread(_ready, state_dir)
    process = await asyncio.create_subprocess_exec(
        *_command(device, uuid), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    request = json.dumps({"recipient": recipient}, separators=(",", ":")).encode("ascii")
    try:
        output, _ = await asyncio.wait_for(process.communicate(request), 1810)
    except (asyncio.CancelledError, TimeoutError) as stopped:
        stop = await asyncio.create_subprocess_exec(
            "systemctl", "stop", f"{UNIT}.service", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(stop.wait(), 15)
        except TimeoutError:
            pass
        if isinstance(stopped, asyncio.CancelledError):
            raise
        raise ValueError("I stopped because the incremental Keep backup took too long.") from None
    if len(output) > MAX_REPLY:
        raise ValueError("The incremental Keep backup returned too much information.")
    try:
        answer = json.loads(output.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("The incremental Keep backup did not answer safely.") from None
    if process.returncode or not isinstance(answer, dict) or answer.get("ok") is not True:
        raise ValueError("I couldn't complete the encrypted Keep backup.")
    added, verified, missing, total = (answer.get(key) for key in ("added", "verified", "missing", "total"))
    if (any(type(value) is not int or value < 0 for value in (added, verified, missing, total))
            or added + verified + missing != total):
        raise ValueError("The incremental Keep backup returned an invalid result.")
    line = (f"[neutral] I encrypted and backed up {added} new Keep file{'s' if added != 1 else ''}. "
            f"I verified {verified} already backed up.")
    if missing:
        line += f" I skipped {missing} catalog file{'s' if missing != 1 else ''} missing on this laptop."
    return line


async def restore_missing(state_dir, identity_path, device, uuid):
    """Explicit recovery API; the unlocked identity path never enters argv."""
    identity = await asyncio.to_thread(_read_identity, identity_path)
    process = await asyncio.create_subprocess_exec(
        "/usr/sbin/being-keep-file-backup", "restore", "--mind-dir", str(state_dir),
        "--device", device, "--uuid", uuid, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(identity), 1810)
    except (asyncio.CancelledError, TimeoutError) as stopped:
        stop = await asyncio.create_subprocess_exec(
            "systemctl", "stop", f"{UNIT}-restore.service", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(stop.wait(), 15)
        except TimeoutError:
            pass
        if process.returncode is None:
            process.kill()
            await process.wait()
        if isinstance(stopped, asyncio.CancelledError):
            raise
        raise ValueError("I stopped because Keep blob recovery took too long.") from None
    if process.returncode or len(output) > MAX_REPLY:
        raise ValueError("The encrypted Keep blob recovery failed safely.")
    try:
        answer = json.loads(output.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("The Keep blob recovery did not answer safely.") from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise ValueError("The encrypted Keep blob recovery failed safely.")
    return answer


def _native_identity_input():
    raw = sys.stdin.buffer.read(4097)
    lines = [line.strip() for line in raw.splitlines()
             if line.strip() and not line.lstrip().startswith(b"#")]
    if len(raw) > 4096 or len(lines) != 1 or not IDENTITY.fullmatch(lines[0]):
        raise ValueError("Expected one unlocked native age identity.")
    return lines[0] + b"\n"


def _restore_launcher(state_dir, device, uuid):
    state_dir = _valid_restore_mind(state_dir)
    _valid_media(device, uuid)
    identity = _native_identity_input()
    try:
        result = subprocess.run(_restore_command(state_dir, device, uuid), input=identity,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=1810, check=False)
    except subprocess.TimeoutExpired:
        subprocess.run(("systemctl", "stop", f"{UNIT}-restore.service"),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)
        raise ValueError("I stopped because Keep blob recovery took too long.") from None
    if len(result.stdout) > MAX_REPLY:
        raise ValueError("The Keep blob recovery returned too much information.")
    sys.stdout.buffer.write(result.stdout)
    return result.returncode


def _restore_worker(state_dir, device, uuid):
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE))
    identity = _native_identity_input()
    with tempfile.NamedTemporaryFile(mode="w+b", prefix="being-keep-identity-", dir="/run") as key:
        os.fchmod(key.fileno(), 0o600)
        key.write(identity)
        key.flush()
        os.fsync(key.fileno())
        result = restore_operation(state_dir, device, uuid, key.name)
    if (not isinstance(result, dict) or set(result) != {"restored", "missing", "skipped"}
            or any(type(value) is not int or value < 0 for value in result.values())):
        raise ValueError("The Keep blob recovery returned invalid counts.")
    print(json.dumps({"ok": True, **result}, separators=(",", ":")))
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="being-keep-file-backup")
    sub = parser.add_subparsers(dest="action", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--device", required=True)
    worker.add_argument("--uuid", required=True)
    restore = sub.add_parser("restore", help="recover only missing blobs in the target's existing Keep catalog")
    restore.add_argument("--mind-dir", required=True)
    restore.add_argument("--device", required=True)
    restore.add_argument("--uuid", required=True)
    restore_worker = sub.add_parser("restore-worker", help=argparse.SUPPRESS)
    restore_worker.add_argument("--mind-dir", required=True)
    restore_worker.add_argument("--device", required=True)
    restore_worker.add_argument("--uuid", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "restore":
            return _restore_launcher(args.mind_dir, args.device, args.uuid)
        if args.action == "restore-worker":
            return _restore_worker(args.mind_dir, args.device, args.uuid)
        raw = sys.stdin.buffer.read(1025)
        if len(raw) > 1024:
            raise ValueError("The Keep backup request is too large.")
        request = json.loads(raw.decode("ascii"))
        result = operation("/mind", args.device, args.uuid, request.get("recipient"))
        print(json.dumps({"ok": True, **result}, separators=(",", ":")))
        return 0
    except (BackupCryptoError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
        print('{"ok":false}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
