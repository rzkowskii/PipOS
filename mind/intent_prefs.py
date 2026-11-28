"""Private owner preference for exact learned voice shortcuts."""

import os
import stat
from pathlib import Path

from .storage import atomic_write

FILE = "intent-shortcuts"


def _path(state_dir):
    return Path(state_dir) / FILE


def enabled(state_dir, factory=False):
    path = _path(state_dir)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return False
            value = source.read(5)
    except FileNotFoundError:
        return bool(factory)
    except OSError:
        return False
    return value == b"on\n"


def set_enabled(state_dir, value):
    if type(value) is not bool:
        raise ValueError("invalid voice-shortcut preference")
    atomic_write(_path(state_dir), "on\n" if value else "off\n", mode=0o600)


def erase_all(state_dir):
    _path(state_dir).unlink(missing_ok=True)
