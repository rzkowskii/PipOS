"""Small, durable writes to the appliance's writable partitions."""

import os
import stat
import tempfile
from pathlib import Path


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError(f"Expected a private directory at {path}.")


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, contents, mode=0o600):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        pending = Path(temp.name)
        try:
            os.fchmod(temp.fileno(), mode)
            temp.write(contents)
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(pending, path)
            sync_directory(path.parent)
        finally:
            pending.unlink(missing_ok=True)
