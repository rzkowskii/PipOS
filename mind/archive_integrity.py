"""Opt-in, bounded full SQLite check for the installed archive.

The ordinary night shift keeps using its quick check.  This module is the
slower native-validation gate: it is inert unless a private, root-owned
marker exists, opens only the installed archive read-only, and records no
SQLite diagnostic text or memory content.
"""

import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .storage import atomic_write


MARKER = "archive-integrity.enabled"
STATUS = "archive-integrity.json"
VERSION = 2
FULL_INTERVAL_DAYS = 28
RETRY_INTERVAL_DAYS = {"damaged": 1, "timeout": 7, "unavailable": 1}
MAX_SECONDS = 300.0
MAX_STATUS_BYTES = 4096
MAX_DATABASE_BYTES = 32 * 1024 * 1024 * 1024
OUTCOMES = frozenset(("ok", "damaged", "timeout", "unavailable"))
INSTALLED = Path("/run/systemd/generator.early/being-installed")
LIVE_ROOT = Path("/mind")
LIVE_DATABASE = LIVE_ROOT / "mind.db"
SIDECARS = ("-wal", "-shm", "-journal")


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def enabled(root):
    """The appliance-only native validation marker; absent is the default."""
    root = Path(root)
    if (root != Path("/mind") or not Path("/etc/being/env").is_file()
            or not INSTALLED.is_dir()):
        return False
    try:
        descriptor = os.open(root / MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o077
                and info.st_size == 2 and source.read(3) == b"1\n")


def _status(root):
    try:
        descriptor = os.open(Path(root) / STATUS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                    or info.st_size > MAX_STATUS_BYTES):
                return None
            value = json.loads(source.read(MAX_STATUS_BYTES + 1))
        if not isinstance(value, dict) or value.get("version") not in (1, VERSION):
            return None
        checked = date.fromisoformat(value["day"])
        if (value.get("outcome") not in OUTCOMES
                or ("attempt_outcome" in value and value.get("attempt_outcome") not in OUTCOMES)):
            return None
        attempted = date.fromisoformat(value["attempt_day"]) if "attempt_day" in value else checked
        if attempted < checked:
            return None
        return {"day": checked, "outcome": value["outcome"], "attempt_day": attempted,
                "attempt_outcome": value.get("attempt_outcome", value["outcome"])}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def due(root, night):
    """Use only the trusted ISO night supplied by Turn's believed-clock gate."""
    if not enabled(root):
        return False
    try:
        current = date.fromisoformat(night)
    except (TypeError, ValueError):
        return False
    previous = _status(root)
    if previous is None:
        return True
    elapsed = (current - previous["attempt_day"]).days
    interval = RETRY_INTERVAL_DAYS.get(previous["attempt_outcome"], FULL_INTERVAL_DAYS)
    if previous["outcome"] == "damaged":
        interval = min(interval, RETRY_INTERVAL_DAYS["damaged"])
    return elapsed >= interval


def latest(root):
    """Show known installed status even if future full scans are disabled."""
    return _status(root) if Path(root) == LIVE_ROOT and INSTALLED.is_dir() else None


def record(root, night, outcome):
    """Persist only cadence and a bounded verdict, never SQLite's error text."""
    if outcome not in OUTCOMES:
        raise ValueError("invalid archive-integrity outcome")
    checked = date.fromisoformat(night).isoformat()
    previous = _status(root)
    # An unavailable/expired retry cannot disprove known damage. Preserve the
    # last meaningful verdict while recording when and why this attempt ended.
    if (outcome in ("timeout", "unavailable") and previous is not None
            and previous["outcome"] == "damaged"):
        value = {"version": VERSION, "day": previous["day"].isoformat(), "outcome": "damaged",
                 "attempt_day": checked, "attempt_outcome": outcome}
    else:
        value = {"version": VERSION, "day": checked, "outcome": outcome}
    atomic_write(Path(root) / STATUS,
                 json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                 mode=0o600)


def _safe_sidecars(path):
    """Open and pin every present SQLite sidecar without following links."""
    opened = {}
    try:
        for suffix in SIDECARS:
            candidate = Path(str(path) + suffix)
            try:
                before = candidate.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022
                    or before.st_size > MAX_DATABASE_BYTES):
                raise OSError("unsafe archive sidecar")
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            pinned = os.fstat(descriptor)
            if _signature(pinned) != _signature(before):
                os.close(descriptor)
                raise OSError("archive sidecar changed while opening")
            opened[suffix] = (descriptor, pinned)
        return opened
    except Exception:
        for descriptor, _ in opened.values():
            os.close(descriptor)
        raise


def _close_sidecars(path, opened):
    try:
        for suffix, (descriptor, pinned) in opened.items():
            candidate = Path(str(path) + suffix)
            current = candidate.lstat()
            # WAL content and size may advance while our read transaction holds
            # its snapshot. Replacement, links and type changes are refused.
            if (not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
                    != (pinned.st_dev, pinned.st_ino, stat.S_IFMT(pinned.st_mode))
                    or (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
                    != (pinned.st_dev, pinned.st_ino)):
                raise OSError("archive sidecar was replaced during the check")
        present = {suffix for suffix in SIDECARS if os.path.lexists(Path(str(path) + suffix))}
        if present != set(opened):
            raise OSError("archive sidecar set changed during the check")
    finally:
        for descriptor, _ in opened.values():
            os.close(descriptor)


@contextmanager
def _readonly_database(path, opened, closed):
    """Pin the live inode while SQLite reads the canonical WAL database."""
    path = Path(path)
    if path != LIVE_DATABASE:
        raise OSError("archive checker accepts only the canonical installed database")
    root = path.parent.lstat()
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != 0 or path.parent.is_symlink():
        raise OSError("archive root is not the installed root-owned directory")
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022
            or not 0 < before.st_size <= MAX_DATABASE_BYTES):
        raise OSError("archive is not a regular database")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        pinned = os.fstat(descriptor)
        if _signature(pinned) != _signature(before):
            raise OSError("archive changed while opening")
        sidecars = _safe_sidecars(path)
        try:
            # SQLite can create a shared-memory file even for a read-only WAL
            # connection. Inspect the already-open live WAL instead.
            header = os.pread(descriptor, 20, 0)
            if b"\x02" in header[18:20] and not {"-wal", "-shm"} <= sidecars.keys():
                raise OSError("live WAL sidecars are not ready")
            if "-journal" in sidecars:
                raise OSError("the archive has an active rollback journal")
            uri = f"file:{path.as_posix()}?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=0.5)
            try:
                databases = db.execute("PRAGMA database_list").fetchall()
                if len(databases) != 1 or databases[0][1:] != ("main", str(path)):
                    raise OSError("SQLite did not open the canonical archive path")
                db.execute("PRAGMA query_only = ON")
                db.execute("PRAGMA temp_store = MEMORY")
                db.execute("PRAGMA trusted_schema = OFF")
                opened(db)
                db.execute("BEGIN")
                db.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
                yield db
            finally:
                try:
                    db.rollback()
                finally:
                    closed(db)
                    db.close()
        finally:
            _close_sidecars(path, sidecars)
        if (_signature(os.fstat(descriptor)) != _signature(pinned)
                or _signature(path.lstat()) != _signature(before)):
            raise OSError("archive changed during the check")
    finally:
        os.close(descriptor)


def check_database(path, stop, *, timeout=MAX_SECONDS, opened=lambda _db: None, closed=lambda _db: None):
    """Return a content-free verdict; cancellation propagates to NightShift."""
    deadline = time.monotonic() + min(MAX_SECONDS, max(1.0, float(timeout)))
    expired = False

    def progress():
        nonlocal expired
        if stop.is_set():
            return 1
        if time.monotonic() >= deadline:
            expired = True
            return 1
        return 0

    try:
        with _readonly_database(path, opened, closed) as db:
            db.set_progress_handler(progress, 10_000)
            row = db.execute("PRAGMA integrity_check(1)").fetchone()
            if stop.is_set():
                raise sqlite3.OperationalError("interrupted")
            return "ok" if row == ("ok",) else "damaged"
    except sqlite3.OperationalError:
        if stop.is_set():
            raise
        return "timeout" if expired or time.monotonic() >= deadline else "unavailable"
    except sqlite3.DatabaseError:
        return "damaged"
    except (OSError, ValueError):
        return "unavailable"
