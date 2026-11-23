"""Revision-checked local dream checkpoints and journal source links."""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime

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


# -- candidates from the dream model ------------------------------------------
#
# What the dream model found, after dream_verify passed it. A candidate is
# never told as a fact. It is promoted when it is found again, on a later
# night, in a different sentence said on a later day, or when the owner
# confirms it. Each
# rests on the exact quotes it came from: forgetting any of those sentences
# takes the candidate, and anything derived from it, with it.

PROMOTED_CONFIDENCE = 0.7
_MODEL_CURSOR = "dream_model_through:"


@dataclass(frozen=True)
class Candidate:
    id: int
    kind: str
    claim: str
    modality: str
    status: str
    first_night: str
    last_night: str
    quotes: tuple  # ((episode id, quote), ...)
    fact_id: int | None = None


def claim_key(text):
    """Two claims are the same claim when their words of substance agree
    (stemmed, in any order, a negation kept apart): the model seldom words a
    recurring fact identically on two nights."""
    from .dream_verify import claim_signature

    return claim_signature(text)


def dream_cursor(memory, owner):
    """The last episode the dream model has read for `owner`."""
    row = memory.db.execute("SELECT value FROM meta WHERE key = ?", (_MODEL_CURSOR + owner,)).fetchone()
    try:
        return int(row[0]) if row else 0
    except ValueError:
        return 0


def _said_on(ts, zone):
    return datetime.fromtimestamp(ts, zone).date()


def save_candidates(memory, owner, items, *, night, through=None, zone=None):
    """Store checked items as candidates, and move the cursor, together.

    Returns the ids of candidates promoted by this save: found again, on a
    later night, in a different sentence that was said on a later day (in
    `zone`, the owner's) than one it was found in before. The night a chunk
    was read is not enough on its own: two sentences from one evening can be
    read either side of midnight. An item whose sentence has gone since it
    was read is dropped.
    """
    promoted = []
    db = memory.db
    stamp = time.time()
    with _background_transaction(db, "BEGIN IMMEDIATE"):
        for item in items:
            alive = db.execute(
                "SELECT ts FROM episodes WHERE id = ? AND text NOT IN ('', '[forgotten]') AND instr(text, ?) > 0",
                (item.episode_id, item.quote)).fetchone()
            if not alive:
                continue
            said = _said_on(alive[0], zone)
            key = claim_key(item.claim)
            row = db.execute(
                "SELECT id, status, first_night FROM candidate_facts WHERE owner = ? AND claim_key = ?",
                (owner, key)).fetchone()
            if row is None:
                candidate = db.execute(
                    "INSERT INTO candidate_facts (owner, kind, claim, claim_key, modality, first_night, last_night, "
                    "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (owner, item.kind, item.claim, key, item.modality, night, night, stamp)).lastrowid
                db.execute("INSERT OR IGNORE INTO candidate_sources VALUES (?, ?, ?, ?)",
                           (candidate, item.episode_id, night, item.quote))
                continue
            candidate, status, first_night = row
            earlier = any(_said_on(ts, zone) < said for ts, in db.execute(
                "SELECT e.ts FROM candidate_sources s JOIN episodes e ON e.id = s.episode_id "
                "WHERE s.candidate_id = ? AND s.night < ? AND s.episode_id <> ?",
                (candidate, night, item.episode_id)))
            db.execute("INSERT OR IGNORE INTO candidate_sources VALUES (?, ?, ?, ?)",
                       (candidate, item.episode_id, night, item.quote))
            db.execute("UPDATE candidate_facts SET last_night = max(last_night, ?) WHERE id = ?", (night, candidate))
            if status == "candidate" and earlier:
                _promote(db, candidate, owner, stamp)
                promoted.append(candidate)
        if through is not None:
            db.execute(
                "INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (_MODEL_CURSOR + owner, str(int(through))))
    return promoted


def _promote(db, candidate, owner, stamp):
    """Make a candidate a fact, linked to every sentence it rests on."""
    from .memory import normalize

    claim, = db.execute("SELECT claim FROM candidate_facts WHERE id = ?", (candidate,)).fetchone()
    sources = [row[0] for row in db.execute(
        "SELECT episode_id FROM candidate_sources WHERE candidate_id = ? ORDER BY episode_id", (candidate,))]
    known = next((fact_id for fact_id, text in db.execute("SELECT id, text FROM facts")
                  if normalize(text) == normalize(claim)), None)
    if known is None:
        fact = db.execute(
            "INSERT INTO facts (ts, text, speaker, source, confidence) VALUES (?, ?, ?, ?, ?)",
            (stamp, " ".join(claim.split()), owner, sources[-1] if sources else None, PROMOTED_CONFIDENCE)).lastrowid
        db.executemany("INSERT OR IGNORE INTO fact_tellings VALUES (?, ?)", ((fact, source) for source in sources))
    else:
        # The owner already said it: their own fact stays theirs, and is not
        # tied to the dream's quotes.
        fact = None
    db.execute("UPDATE candidate_facts SET status = 'promoted', promoted = ?, fact_id = ? WHERE id = ?",
               (stamp, fact, candidate))
    return fact


def confirm_candidate(memory, candidate_id, owner):
    """The owner said yes: promote it now. Returns the new fact id, or None."""
    with _background_transaction(memory.db, "BEGIN IMMEDIATE"):
        row = memory.db.execute(
            "SELECT status FROM candidate_facts WHERE id = ? AND owner = ?", (candidate_id, owner)).fetchone()
        if row is None or row[0] != "candidate":
            return None
        return _promote(memory.db, candidate_id, owner, time.time())


def derive(memory, child_id, parents):
    """Record that candidate `child_id` was made from `parents`, a list of
    ("episode" | "fact" | "candidate" | "diary_topic", id): it lives only while
    every one of them does."""
    with memory.db:
        memory.db.executemany(
            "INSERT OR IGNORE INTO derivations VALUES ('candidate', ?, ?, ?)",
            ((child_id, kind, parent_id) for kind, parent_id in parents))


def candidates(db, owner, *, status=None, night=None, limit=50):
    """Stored candidates, newest night first, with their quotes."""
    rows = db.execute(
        "SELECT id, kind, claim, modality, status, first_night, last_night, fact_id FROM candidate_facts "
        "WHERE owner = ? AND (? IS NULL OR status = ?) AND (? IS NULL OR last_night = ?) "
        "ORDER BY last_night DESC, id DESC LIMIT ?",
        (owner, status, status, night, night, limit)).fetchall()
    found = []
    for row_id, kind, claim, modality, state, first, last, fact in rows:
        quotes = tuple(db.execute(
            f"SELECT s.episode_id, s.quote FROM candidate_sources s JOIN episodes e ON e.id = s.episode_id "
            f"WHERE s.candidate_id = ? AND {_HISTORY} ORDER BY s.episode_id", (row_id,)))
        if quotes:
            found.append(Candidate(row_id, kind, claim, modality, state, first, last, quotes, fact))
    return found
