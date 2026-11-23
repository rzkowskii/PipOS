"""Revision-checked local dream checkpoints and journal source links."""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date

from . import consolidation
from .memory import Episode, _HISTORY

MAX_BATCH_ROWS = 32
MAX_NIGHT_BATCHES = 4
JOURNAL_CAPACITY = 96
MAX_JOURNAL_LIMIT = 100


@dataclass(frozen=True)
class DreamBatch:
    owner: str
    revision: int
    after: int
    page: tuple[int, ...]
    sources: tuple[Episode, ...] = field(repr=False)
    night: str | None = None
    night_batches: int = 0

    @property
    def through(self):
        return self.page[-1]

    @property
    def source_ids(self):
        return tuple(source.id for source in self.sources)


@dataclass(frozen=True)
class JournalEntry:
    published_at: float
    source: Episode
    revision: int


@contextmanager
def _background_transaction(db, begin):
    """Leave SQLite's regular foreground busy policy intact outside the batch."""
    timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    db.execute("PRAGMA busy_timeout = 0")
    try:
        db.execute(begin)
        with db:
            yield
    finally:
        db.execute(f"PRAGMA busy_timeout = {timeout}")


def _night_count(night, saved_night, batches):
    if night is None or saved_night is None or night > saved_night:
        return 0
    if night < saved_night:
        # A backward clock change must not grant a second budget.
        return MAX_NIGHT_BATCHES
    return batches


class DreamStore:
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.db

    def _checkpoint(self, owner):
        row = self.db.execute(
            "SELECT through_id, night, batches FROM dream_checkpoints WHERE owner = ?", (owner,)
        ).fetchone()
        return row or (0, None, 0)

    def _page(self, after, limit):
        rows = self.db.execute("SELECT id FROM episodes WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
        return tuple(row[0] for row in rows)

    def _select(self, page, owner):
        candidates = consolidation.candidate_highlights(self.memory, page, owner=owner)
        selected = set(consolidation.select_highlights(candidates))
        return tuple(source for source in candidates if source.id in selected)

    def stage(self, owner, *, limit=MAX_BATCH_ROWS, night=None):
        """Read one bounded archive page without saving or publishing anything."""
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH_ROWS:
            raise ValueError(f"dream batch limit must be between 1 and {MAX_BATCH_ROWS}")
        if night is not None and (not isinstance(night, str) or date.fromisoformat(night).isoformat() != night):
            raise ValueError("dream night must be an ISO calendar date")
        with _background_transaction(self.db, "BEGIN"):
            revision = self.memory.revision
            after, saved_night, saved_batches = self._checkpoint(owner)
            batches = _night_count(night, saved_night, saved_batches)
            if batches >= MAX_NIGHT_BATCHES:
                return None
            page = self._page(after, limit)
            if page:
                return DreamBatch(owner, revision, after, page, self._select(page, owner), night, batches)
        return None

    def publish(self, batch):
        """Commit checked links and the matching checkpoint together."""
        if not 1 <= len(batch.page) <= MAX_BATCH_ROWS:
            return False
        with _background_transaction(self.db, "BEGIN IMMEDIATE"):
            after, saved_night, saved_batches = self._checkpoint(batch.owner)
            batches = _night_count(batch.night, saved_night, saved_batches)
            if (self.memory.revision != batch.revision or after != batch.after
                    or batches != batch.night_batches or batches >= MAX_NIGHT_BATCHES):
                return False
            if (self._page(after, len(batch.page)) != batch.page
                    or self._select(batch.page, batch.owner) != batch.sources):
                return False

            published_at = time.time()
            self.db.executemany(
                "INSERT OR IGNORE INTO dream_journal (owner, source_id, published_at, revision) VALUES (?, ?, ?, ?)",
                ((batch.owner, source_id, published_at, batch.revision) for source_id in batch.source_ids),
            )
            self.db.execute(
                "DELETE FROM dream_journal WHERE owner = ? AND source_id IN ("
                "SELECT source_id FROM dream_journal WHERE owner = ? "
                "ORDER BY published_at DESC, source_id DESC LIMIT -1 OFFSET ?)",
                (batch.owner, batch.owner, JOURNAL_CAPACITY),
            )
            night, count = (saved_night, saved_batches) if batch.night is None else (batch.night, batches + 1)
            self.db.execute(
                "INSERT INTO dream_checkpoints (owner, through_id, night, batches) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner) DO UPDATE SET through_id = excluded.through_id, "
                "night = excluded.night, batches = excluded.batches",
                (batch.owner, batch.through, night, count),
            )
        return True


def journal(db_connection, owner, *, limit=20):
    """Resolve recent links from canonical owner records, including in read-only callers."""
    if type(limit) is not int or not 1 <= limit <= MAX_JOURNAL_LIMIT:
        raise ValueError(f"dream journal limit must be between 1 and {MAX_JOURNAL_LIMIT}")
    if not db_connection.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'dream_journal'").fetchone():
        return []
    rows = db_connection.execute(
        "SELECT j.published_at, j.revision, e.id, e.ts, e.kind, e.speaker, e.text "
        f"FROM dream_journal j JOIN episodes e ON e.id = j.source_id WHERE {_HISTORY} "
        "AND e.kind = 'heard' AND e.speaker = j.owner AND j.owner = ? "
        "ORDER BY j.published_at DESC, j.source_id DESC LIMIT ?",
        (owner, limit),
    )
    return [JournalEntry(published_at, Episode(*source), revision) for published_at, revision, *source in rows]
