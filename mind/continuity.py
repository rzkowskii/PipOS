"""One owner bookmark, linked to the conversation archive."""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .storage import atomic_write


_HEAD = "continuity_thread"
_KIND = "thread"
_OPEN = "Open conversation thread"
_CURIOSITY_MARKER = "curiosity.enabled"
_CURIOSITY_DAY = "curiosity_last_day"

CURIOSITY_QUESTIONS = (
    "What is something small that made today better?",
    "What are you looking forward to this week?",
    "What is something you have enjoyed learning lately?",
    "What would make tomorrow feel easier?",
)


class ArchiveFull(ValueError):
    """The archive is keeping its free-space reserve."""


def curiosity_enabled(state_dir):
    """True only for an owner-private, explicit standing permission marker."""
    try:
        info = (Path(state_dir) / _CURIOSITY_MARKER).lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def set_curiosity(state_dir, enabled):
    marker = Path(state_dir) / _CURIOSITY_MARKER
    if enabled:
        atomic_write(marker, "enabled\n", mode=0o600)
    else:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass


def claim_curiosity(memory, owner, day):
    """Claim the one fixed question for a trusted local day, or return None.

    The bounded receipt contains no owner text. Claiming precedes speaking so
    a restart cannot ask twice; an interrupted question is not replayed.
    """
    token = day.isoformat()
    with memory.db as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM meta WHERE key = ?", (_CURIOSITY_DAY,)).fetchone()
        # ISO dates sort chronologically. A trusted clock that is corrected
        # backwards must not reopen an already claimed earlier day.
        if row is not None and len(row[0]) == 10 and row[0] >= token:
            return None
        db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (_CURIOSITY_DAY, token))
    return CURIOSITY_QUESTIONS[day.toordinal() % len(CURIOSITY_QUESTIONS)]


def erase_all(state_dir):
    """Forget the non-text daily receipt; standing owner permission remains."""
    database = Path(state_dir) / "mind.db"
    if not database.is_file():
        return
    import sqlite3

    with sqlite3.connect(database, timeout=0.25) as db:
        db.execute("DELETE FROM meta WHERE key = ?", (_CURIOSITY_DAY,))


@dataclass(frozen=True)
class Thread:
    id: int
    source: int
    text: str
    told: float
    starts: float | None
    ends: float | None
    asked: bool


def active(memory, owner):
    row = memory.db.execute(
        "SELECT t.id, e.id, e.text, e.ts, json_extract(e.meta, '$.event_start'), "
        "json_extract(e.meta, '$.event_end'), json_extract(t.meta, '$.asked') "
        "FROM meta m JOIN episodes t ON t.id = CAST(m.value AS INTEGER) "
        "JOIN episodes e ON e.id = json_extract(t.meta, '$.episodes[0]') "
        "WHERE m.key = ? AND t.kind = ? AND t.text = ? AND t.speaker = ? "
        "AND e.kind = 'heard' AND e.speaker = t.speaker AND e.text NOT IN ('', '[forgotten]') "
        "AND e.deletion_ref = json_extract(t.meta, '$.source_ref') "
        "AND NOT coalesce(json_extract(e.meta, '$.command'), 0) "
        "AND NOT coalesce(json_extract(e.meta, '$.greeting'), 0)",
        (_HEAD, _KIND, _OPEN, owner),
    ).fetchone()
    return Thread(*row[:-1], bool(row[-1])) if row and row[-1] in (0, 1) else None


def _retire(db):
    # This records a deletion receipt for older backups without touching
    # the conversation the bookmark pointed to.
    db.execute(
        "UPDATE episodes SET text = '[forgotten]', speaker = NULL, meta = NULL, "
        "ts = CAST(ts / 86400 AS INTEGER) * 86400.0 "
        "WHERE id = (SELECT CAST(value AS INTEGER) FROM meta WHERE key = ?) "
        "AND kind = ? AND text = ?",
        (_HEAD, _KIND, _OPEN),
    )
    db.execute("DELETE FROM meta WHERE key = ?", (_HEAD,))


def bookmark(memory, owner):
    """Keep the latest ordinary owner utterance open across restarts."""
    if memory.disk_state()["state"] != "ok":
        raise ArchiveFull
    stamp = memory.now()
    with memory.db as db:
        db.execute("BEGIN IMMEDIATE")
        sources = memory.owner_lines(owner, limit=1)
        if not sources:
            return None
        source = sources[0]
        current = active(memory, owner)
        if current is not None and current.source == source.id:
            return current
        ref, = db.execute("SELECT deletion_ref FROM episodes WHERE id = ?", (source.id,)).fetchone()
        if ref is None:
            return None
        _retire(db)
        entry = db.execute(
            "INSERT INTO episodes (ts, kind, speaker, text, meta) VALUES (?, ?, ?, ?, ?)",
            (stamp, _KIND, owner, _OPEN, json.dumps({"episodes": [source.id], "source_ref": ref, "asked": False})),
        ).lastrowid
        db.execute("INSERT INTO meta VALUES (?, ?)", (_HEAD, str(entry)))
        return active(memory, owner)


def close(memory, owner):
    with memory.db as db:
        db.execute("BEGIN IMMEDIATE")
        thread = active(memory, owner)
        if thread is not None:
            _retire(db)
        return thread


def _due(thread, now):
    return (thread is not None and not thread.asked
            and type(thread.starts) in (float, int) and type(thread.ends) in (float, int)
            and 0 < thread.ends - thread.starts <= 26 * 3600 and thread.told < thread.ends <= now)


def claim_followup(memory, owner, now):
    """Claim one addressed, due check-in before speaking its private words."""
    if not _due(active(memory, owner), now):
        return None
    with memory.db as db:
        db.execute("BEGIN IMMEDIATE")
        thread = active(memory, owner)
        if not _due(thread, now):
            return None
        db.execute("UPDATE episodes SET meta = json_set(meta, '$.asked', json('true')) WHERE id = ?",
                   (thread.id,))
        return thread
