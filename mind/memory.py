"""Memory: an append-only log of what happened, and facts it was asked to keep.

Plain SQLite with full-text search. Everything here can be read with the
`sqlite3` command-line tool, and forgetting really deletes.
"""

import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

# The being has no network time by default, and an old laptop's clock battery
# dies. Expiry deletes for good, so the clock has to be believable first.
CLOCK_FLOOR = 1_767_225_600  # 2026-01-01 UTC; earlier is a dead clock battery
CLOCK_SLACK = 600.0  # small steps backwards are ordinary corrections
CLOCK_LEAP = 400 * 86_400.0  # further ahead than this is doubted
CLOCK_SETTLE = 7 * 86_400.0  # a doubted clock is believed once it has held this long
CLOCK_STEP = 86_400.0  # after a step forward larger than this, expiry counts from before it
CLOCK_HOLD = 86_400.0  # until the new time has held this long
CLOCK_SUSPECT = 60 * 86_400.0  # how long rows written after a clock went back stay protected
CLOCK_NOTE = 300.0  # how often the believed time is written down
# Fact matches whose relevance is within this fraction of the best are ranked
# by recency instead (relevance first, then the newest telling).
RECALL_BAND = 0.8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS episodes (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,           -- heard, said, command, sense, note
    speaker TEXT,
    text    TEXT NOT NULL,
    meta    TEXT
);
CREATE INDEX IF NOT EXISTS episodes_ts ON episodes (ts);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5 (
    text, content='episodes', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts (episodes_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE OF text ON episodes BEGIN
    INSERT INTO episodes_fts (episodes_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO episodes_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    text       TEXT NOT NULL,
    speaker    TEXT,
    source     INTEGER REFERENCES episodes (id) ON DELETE SET NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    confirmed  REAL
);
CREATE INDEX IF NOT EXISTS facts_ts ON facts (ts);
CREATE INDEX IF NOT EXISTS facts_source ON facts (source) WHERE source IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5 (
    text, content='facts', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts (facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF text ON facts BEGIN
    INSERT INTO facts_fts (facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO facts_fts (rowid, text) VALUES (new.id, new.text);
END;

-- Every time a fact was told. The first telling is also facts.source; a
-- retelling moves facts.source forward so the fact ages from its latest
-- telling, and forgetting the fact still finds every sentence that told it.
CREATE TABLE IF NOT EXISTS fact_tellings (
    fact_id    INTEGER NOT NULL REFERENCES facts (id) ON DELETE CASCADE,
    episode_id INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, episode_id)
);
CREATE INDEX IF NOT EXISTS fact_tellings_episode ON fact_tellings (episode_id);

CREATE TABLE IF NOT EXISTS dream_checkpoints (
    owner      TEXT PRIMARY KEY,
    through_id INTEGER NOT NULL,
    night      TEXT,
    batches    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dream_journal (
    owner        TEXT NOT NULL,
    source_id    INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    published_at REAL NOT NULL,
    revision     INTEGER NOT NULL,
    PRIMARY KEY (owner, source_id)
);
CREATE INDEX IF NOT EXISTS dream_journal_source ON dream_journal (source_id);

-- Invalidate every derived link in the same transaction as a source edit.
-- After a correction, reconsider the canonical owner statement on a future pass.
CREATE TRIGGER IF NOT EXISTS episodes_dream_update
AFTER UPDATE OF text, meta, speaker, kind, ts ON episodes
WHEN new.text IS NOT old.text OR new.meta IS NOT old.meta OR new.speaker IS NOT old.speaker
     OR new.kind IS NOT old.kind OR new.ts IS NOT old.ts
BEGIN
    DELETE FROM dream_journal WHERE source_id = old.id;
    UPDATE dream_checkpoints SET through_id = new.id - 1
      WHERE owner = new.speaker AND through_id >= new.id AND new.kind = 'heard'
        AND new.text NOT IN ('', '[forgotten]') AND NOT coalesce(json_extract(new.meta, '$.memory_reply'), 0);
END;

-- An imported or reused row ID can fall below the current high-water mark.
CREATE TRIGGER IF NOT EXISTS episodes_dream_insert AFTER INSERT ON episodes
WHEN new.kind = 'heard' AND new.text NOT IN ('', '[forgotten]')
     AND NOT coalesce(json_extract(new.meta, '$.memory_reply'), 0)
BEGIN
    UPDATE dream_checkpoints SET through_id = new.id - 1
      WHERE owner = new.speaker AND through_id >= new.id;
END;

CREATE TRIGGER IF NOT EXISTS episodes_dream_delete AFTER DELETE ON episodes BEGIN
    DELETE FROM dream_journal WHERE source_id = old.id;
END;

-- Sentence vectors (int8 codes plus one scale). They are derived from the
-- words, so they go the moment the words change or go.
CREATE TABLE IF NOT EXISTS episode_vec (
    episode_id INTEGER PRIMARY KEY REFERENCES episodes (id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    scale      REAL NOT NULL,
    q8         BLOB NOT NULL,
    bits       BLOB NOT NULL       -- sign bits, for a fast first pass
);
CREATE TABLE IF NOT EXISTS fact_vec (
    fact_id INTEGER PRIMARY KEY REFERENCES facts (id) ON DELETE CASCADE,
    model   TEXT NOT NULL,
    scale   REAL NOT NULL,
    q8      BLOB NOT NULL,
    bits    BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS episode_vec_stale AFTER UPDATE OF text ON episodes
WHEN new.text IS NOT old.text BEGIN
    DELETE FROM episode_vec WHERE episode_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS fact_vec_stale AFTER UPDATE OF text ON facts
WHEN new.text IS NOT old.text BEGIN
    DELETE FROM fact_vec WHERE fact_id = old.id;
END;

-- What the dream model proposed and code checked. A candidate is never told
-- as a fact until it recurs on a later night or the owner confirms it.
CREATE TABLE IF NOT EXISTS candidate_facts (
    id         INTEGER PRIMARY KEY,
    owner      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    claim      TEXT NOT NULL,
    claim_key  TEXT NOT NULL,
    modality   TEXT NOT NULL,
    first_night TEXT NOT NULL,
    last_night TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'candidate',   -- candidate, promoted
    created    REAL NOT NULL,
    promoted   REAL,
    fact_id    INTEGER REFERENCES facts (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS candidate_facts_key ON candidate_facts (owner, claim_key);
-- Each exact quote a candidate rests on.
CREATE TABLE IF NOT EXISTS candidate_sources (
    candidate_id INTEGER NOT NULL REFERENCES candidate_facts (id) ON DELETE CASCADE,
    episode_id   INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    night        TEXT NOT NULL,
    quote        TEXT NOT NULL,
    PRIMARY KEY (candidate_id, episode_id)
);
CREATE INDEX IF NOT EXISTS candidate_sources_episode ON candidate_sources (episode_id);
-- Anything made from other records: a child exists only while every parent does.
CREATE TABLE IF NOT EXISTS derivations (
    child_kind  TEXT NOT NULL,    -- candidate, diary_topic
    child_id    INTEGER NOT NULL,
    parent_kind TEXT NOT NULL,    -- episode, fact, candidate
    parent_id   INTEGER NOT NULL,
    PRIMARY KEY (child_kind, child_id, parent_kind, parent_id)
);
CREATE INDEX IF NOT EXISTS derivations_parent ON derivations (parent_kind, parent_id);
CREATE TRIGGER IF NOT EXISTS candidate_sources_gone AFTER UPDATE OF text ON episodes
WHEN new.text IS NOT old.text BEGIN
    DELETE FROM candidate_facts WHERE id IN (SELECT candidate_id FROM candidate_sources WHERE episode_id = old.id);
END;
-- A quote that goes takes its candidate with it, even if another quote remains.
CREATE TRIGGER IF NOT EXISTS candidate_source_deleted AFTER DELETE ON candidate_sources BEGIN
    DELETE FROM candidate_facts WHERE id = old.candidate_id;
END;
-- A promoted candidate's fact was made from it, and goes with it.
CREATE TRIGGER IF NOT EXISTS candidate_fact_deleted AFTER DELETE ON candidate_facts
WHEN old.fact_id IS NOT NULL BEGIN
    DELETE FROM facts WHERE id = old.fact_id;
END;

-- The nightly day record: counts, and topics that cite their episodes.
CREATE TABLE IF NOT EXISTS diary_days (
    owner          TEXT NOT NULL,
    day            TEXT NOT NULL,
    conversations  INTEGER NOT NULL,
    owner_lines    INTEGER NOT NULL,
    reminders_done INTEGER NOT NULL,
    timers         INTEGER NOT NULL,
    body_events    TEXT NOT NULL,       -- JSON object of counts, never words
    built          REAL NOT NULL,
    start_ts       REAL,                -- the day's [start, end) in the owner's zone
    end_ts         REAL,
    PRIMARY KEY (owner, day)
);
CREATE TABLE IF NOT EXISTS diary_topics (
    id     INTEGER PRIMARY KEY,
    owner  TEXT NOT NULL,
    day    TEXT NOT NULL,
    rank   INTEGER NOT NULL,
    topic  TEXT NOT NULL,
    FOREIGN KEY (owner, day) REFERENCES diary_days (owner, day) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS diary_topic_sources (
    topic_id   INTEGER NOT NULL REFERENCES diary_topics (id) ON DELETE CASCADE,
    episode_id INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    PRIMARY KEY (topic_id, episode_id)
);
CREATE INDEX IF NOT EXISTS diary_topic_sources_episode ON diary_topic_sources (episode_id);
CREATE TRIGGER IF NOT EXISTS diary_topic_gone AFTER UPDATE OF text ON episodes
WHEN new.text IS NOT old.text BEGIN
    DELETE FROM diary_topics WHERE id IN (SELECT topic_id FROM diary_topic_sources WHERE episode_id = old.id);
END;
CREATE TRIGGER IF NOT EXISTS diary_topic_source_deleted AFTER DELETE ON diary_topic_sources BEGIN
    DELETE FROM diary_topics WHERE id = old.topic_id;
END;
"""

# Hybrid recall. RRF fuses the word ranking with the meaning ranking; a
# candidate also needs enough cosine similarity to be worth saying.
RRF_K = 60
HYBRID_POOL = 50
# Pip's facts are the owner's own. A question about the owner ("what's my
# flower called?") may be matched on meaning at COSINE_FLOOR; one that is not
# ("why is the sky blue?") must be as close as COSINE_FLOOR_IMPERSONAL.
# Calibrated with blink-8M on the sets in tests/test_memory_hybrid.py:
# unrelated questions peak at about 0.22, paraphrases mostly score 0.2-0.6.
COSINE_FLOOR = 0.20
COSINE_FLOOR_IMPERSONAL = 0.32
# A row sharing only some of the question's words gets a cosine lift from the
# shared tokens ("why is the sky blue?" against "the key is in the blue
# pot"), so it has to be clearly on the subject as well.
COSINE_FLOOR_PARTIAL = 0.45
COSINE_FLOOR_PARTIAL_IMPERSONAL = 0.50
# Rank fusion alone cannot tell apart two rows that share the same one word;
# their closeness in meaning can.
COSINE_WEIGHT = 0.05
_PERSONAL = re.compile(r"\b(?:i|me|my|mine|myself|we|us|our|ours|i'm|i've|i'd|i'll)\b", re.IGNORECASE)
RECENCY_WEIGHT = 0.004
RECENCY_HALF_LIFE_DAYS = 45.0
EMBED_BATCH = 64

# Disk guard: below LOW, conversation is no longer archived and the archive is
# compacted; below CRITICAL only what the owner asked to keep is written.
DISK_LOW = 0.10
DISK_CRITICAL = 0.05
DISK_CHECK_SECONDS = 30.0
_DEFAULT = object()


def _search_secure_delete(db):
    """FTS5's secure-delete removes a forgotten row's terms from the index at
    once, so a forget never needs a full rebuild. SQLite 3.42 and later."""
    try:
        with db:
            for table in ("episodes_fts", "facts_fts"):
                db.execute(f"INSERT INTO {table} ({table}, rank) VALUES ('secure-delete', 1)")
        return True
    except sqlite3.Error:
        return False

# Words too common to search on, with spoken contractions and fillers.
_STOP = frozenset(
    """a about again all also am an and any are as at be been but by can could did do does
    for from had has have he her him his how i if in is it its just know like me my no not
    of on or our please remember said say she so some tell than that the their them then
    there these they thing things this to told too us was we were what when where which
    who why will with would you your yours anything something everything
    i'm i've i'll i'd you're you've you'll you'd it's that's what's where's who's how's
    there's here's let's don't doesn't didn't isn't aren't wasn't weren't can't couldn't
    won't wouldn't shouldn't haven't hasn't he's she's we're they're
    um uh er erm hmm hm oh ah okay ok yeah yes yep well really actually
    hey hi hello thanks thank""".split()
)
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
# "My cat's name" searches for "cat": the possessive never matches the fact.
_POSSESSIVE = re.compile(r"(?<=\w)'s$")

# Conversation that can be looked back on. Canned replies (greetings, clock
# answers, memory read-backs) are not conversation and are never recalled.
_HISTORY = """e.kind IN ('heard', 'said') AND e.text NOT IN ('', '[forgotten]')
              AND NOT coalesce(json_extract(e.meta, '$.memory_reply'), 0)
              AND NOT coalesce(json_extract(e.meta, '$.canned'), 0)"""
_HISTORY_SCORE = "bm25(episodes_fts) * CASE e.kind WHEN 'heard' THEN 1.25 ELSE 1 END"
_DELETION_ID = re.compile(r"[0-9a-f]{32}")
_TOMBSTONE_TS = "ts = CAST(ts / 86400 AS INTEGER) * 86400.0"
_EPISODE_SOURCE_LINKS = """
    SELECT e.id AS episode, link.value AS source
    FROM episodes e, json_each(e.meta, '$.episodes') link
    UNION ALL
    SELECT reply.id, heard.id
    FROM episodes heard JOIN episodes reply ON reply.id = heard.id + 1
    WHERE heard.kind = 'heard' AND reply.kind = 'said'
      AND reply.text NOT IN ('', '[forgotten]')
      AND reply.ts BETWEEN heard.ts AND heard.ts + 120
      AND json_type(reply.meta, '$.episodes') IS NULL
      AND coalesce(json_extract(reply.meta, '$.reply_to'), heard.id) = heard.id
"""


@dataclass(frozen=True)
class Fact:
    id: int
    ts: float
    text: str
    speaker: str
    confidence: float


@dataclass(frozen=True)
class Episode:
    id: int
    ts: float
    kind: str
    speaker: str
    text: str
    excerpt: str | None = None


@dataclass(frozen=True)
class MemoryEdit:
    texts: tuple[str, ...] = ()
    fact_ids: tuple[int, ...] = ()
    episode_ids: tuple[int, ...] = ()
    fact_texts: tuple[str, ...] = ()

    def __bool__(self):
        return bool(self.texts or self.fact_ids or self.episode_ids)


def search_terms(query):
    """The words in `query` worth searching for."""
    words = _WORD.findall(query.lower().replace("’", "'"))
    words = [w if w in _STOP else _POSSESSIVE.sub("", w) for w in words]
    return [w for w in words if len(w) > 1 and w not in _STOP]


def normalize(text):
    """Case, curly apostrophes and spacing folded, for comparing wordings."""
    return " ".join(text.casefold().replace("’", "'").replace("‘", "'").split())


def vector_text(text):
    """What a sentence's vector is made from: its words of substance.

    Function words ("what", "my", "is") are shared by nearly every question
    and fact, so in a static model they pull every pair together; without them
    paraphrases separate from unrelated sentences far better.
    """
    words = search_terms(text)
    return " ".join(words) if words else text


def _disk_reading(root):
    if root is None:
        return {"state": "ok", "free_fraction": None, "free_bytes": None}
    try:
        info = os.statvfs(root)
    except (OSError, AttributeError):
        return {"state": "ok", "free_fraction": None, "free_bytes": None}
    total = info.f_blocks * info.f_frsize
    free = info.f_bavail * info.f_frsize
    fraction = free / total if total else 1.0
    state = "critical" if fraction < DISK_CRITICAL else "low" if fraction < DISK_LOW else "ok"
    return {"state": state, "free_fraction": round(fraction, 4), "free_bytes": free}


# Newest first for the backfill; above the newest vector, oldest first, so
# the watermark that cheap catch-up relies on never skips a row.
_VECTOR_EPISODES = f"""SELECT e.id, e.text FROM episodes e
    WHERE {_HISTORY} AND e.id > ? AND NOT EXISTS (SELECT 1 FROM episode_vec v WHERE v.episode_id = e.id)
    ORDER BY CASE WHEN ? THEN e.id ELSE -e.id END LIMIT ?"""
_VECTOR_FACTS = """SELECT f.id, f.text FROM facts f
    WHERE f.id > ? AND NOT EXISTS (SELECT 1 FROM fact_vec v WHERE v.fact_id = f.id)
    ORDER BY CASE WHEN ? THEN f.id ELSE -f.id END LIMIT ?"""
VECTOR_RECIPE = "content-words-1"  # bump when vector_text changes: old vectors are remade at night
PREFILTER_MIN = 2000  # above this many rows, sign bits pick who gets an exact score
PREFILTER_KEEP = 400


def vector_model(embedder):
    return f"{embedder.model_id}:{VECTOR_RECIPE}"


def _sign_bits(vector):
    bits = 0
    for index, value in enumerate(vector):
        if value > 0:
            bits |= 1 << index
    return bits.to_bytes((len(vector) + 7) // 8, "little")


def _index_rows(db, embedder, limit, *, recent_only=False, made=None):
    """Embed rows with no vector, newest first. With `recent_only`, look only
    above the newest vector: cheap enough to run before every search.

    `made`, a dict, keeps vectors whose insert failed, keyed by row and
    words, so a retry stores them without embedding them again."""
    from .embed import quantise

    done = 0
    model = vector_model(embedder)
    for select, insert, newest in (
        (_VECTOR_FACTS, "INSERT OR IGNORE INTO fact_vec (fact_id, model, scale, q8, bits) SELECT ?, ?, ?, ?, ? "
                        "WHERE EXISTS (SELECT 1 FROM facts WHERE id = ? AND text = ?)",
         "SELECT coalesce(max(fact_id), 0) FROM fact_vec"),
        (_VECTOR_EPISODES, "INSERT OR IGNORE INTO episode_vec (episode_id, model, scale, q8, bits) "
                           "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM episodes WHERE id = ? AND text = ?)",
         "SELECT coalesce(max(episode_id), 0) FROM episode_vec"),
    ):
        if done >= limit:
            break
        above = db.execute(newest).fetchone()[0] if recent_only else 0
        rows = db.execute(select, (above, int(recent_only), limit - done)).fetchall()
        if not rows:
            continue
        coded = []
        for row_id, text in rows:
            key = (newest, row_id, text)
            entry = made.get(key) if made is not None else None
            if entry is None:
                vector = embedder.embed(vector_text(text))
                scale, q8 = quantise(vector)
                entry = (row_id, model, scale, q8, _sign_bits(vector), row_id, text)
                if made is not None:
                    made[key] = entry
            coded.append(entry)
        with db:
            db.executemany(insert, coded)
        if made is not None:
            for row_id, text in rows:
                made.pop((newest, row_id, text), None)
        done += len(rows)
    return done


def _busy(error):
    """True when SQLite only means another connection holds the lock now."""
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error).lower()
    return "locked" in message or "busy" in message


class _Indexer:
    """Embeds new sentences on a thread of its own, with its own connection,
    so recording a sentence never waits for its vector."""

    BUSY_FIRST = 0.2        # seconds before the first retry of a locked database
    BUSY_LONGEST = 5.0      # backing off up to this
    BUSY_TRIES = 8          # then waiting for the next sentence
    PARKED_SECONDS = 300.0  # after a failure that is not a lock (a full or read-only disk)

    def __init__(self, path, embedder, *, allowed=None):
        self.path, self.embedder = path, embedder
        # Asked before each batch: False (the disk guard says critical) skips it.
        self.allowed = allowed or (lambda: True)
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="memory-vectors", daemon=True)
        self._thread.start()

    def poke(self):
        self._wake.set()

    def stop(self):
        self._stopping.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        try:
            # A background chore: below the face, the voice and the turn.
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (AttributeError, OSError):
            pass
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=0.5)
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA secure_delete = ON")
            made = {}
            while not self._stopping.is_set():
                self._wake.wait()
                self._wake.clear()
                delay, tries = self.BUSY_FIRST, 0
                while not self._stopping.is_set():
                    if not self.allowed():
                        break
                    try:
                        if not _index_rows(db, self.embedder, EMBED_BATCH, recent_only=True, made=made):
                            break
                        delay, tries = self.BUSY_FIRST, 0
                    except sqlite3.OperationalError as exc:
                        if not _busy(exc):
                            # A full, read-only or failing disk does not clear
                            # in a moment: say so once, then wait a long while
                            # for the next sentence rather than spinning.
                            print("memory: vector indexing paused:", exc, flush=True)
                            made.clear()
                            if self._stopping.wait(self.PARKED_SECONDS):
                                return
                            break
                        tries += 1
                        if tries >= self.BUSY_TRIES:
                            break  # the next sentence tries again
                        # Busy with a foreground write: try again, backing off.
                        if self._stopping.wait(delay):
                            return
                        delay = min(delay * 2, self.BUSY_LONGEST)
                if len(made) > 4 * EMBED_BATCH:
                    made.clear()
        except sqlite3.Error as exc:
            print("memory: vector indexing stopped:", exc, flush=True)
        finally:
            if db is not None:
                db.close()


class Memory:
    def __init__(self, root, *, embedder=_DEFAULT):
        self.root = Path(root) if root is not None else None
        if self.root is None:
            self.db = sqlite3.connect(":memory:")
            self.db.execute("PRAGMA temp_store = MEMORY")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.root / "mind.db")
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        # Deleted rows are overwritten, not just unlinked.
        self.db.execute("PRAGMA secure_delete = ON")
        with self.db:
            self.db.executescript(_SCHEMA)
            self.db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),)
            )
        found = int(self.db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0])
        if found != SCHEMA_VERSION:
            raise RuntimeError(f"memory schema is version {found}, this code needs {SCHEMA_VERSION}")
        with self.db:
            # Day records written before they carried their bounds.
            columns = {column[1] for column in self.db.execute("PRAGMA table_info(diary_days)")}
            for column in ("start_ts", "end_ts"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE diary_days ADD COLUMN {column} REAL")
        with self.db:
            added = self.db.execute("INSERT OR IGNORE INTO meta VALUES ('episode_search', '1')")
            if added.rowcount:
                self.db.execute("INSERT INTO episodes_fts (episodes_fts) VALUES ('rebuild')")
        self._fts_secure = _search_secure_delete(self.db)
        if self._fts_secure:
            with self.db:
                # Segments written before secure-delete was on may still hold
                # the terms of rows forgotten then: merge them away once.
                if self.db.execute("INSERT OR IGNORE INTO meta VALUES ('search_secure', '1')").rowcount:
                    self._rebuild_search(force=True)
        self._initialize_deletions()
        if embedder is _DEFAULT:
            from . import embed

            embedder = embed.load()
        self.embedder = embedder or None
        self._indexer = None
        self._disk = None
        self._disk_checked = float("-inf")
        self._clock_set_by_owner = False

    def close(self):
        if self._indexer is not None:
            self._indexer.stop()
            self._indexer = None
        self.db.close()

    def _initialize_deletions(self):
        """Give old and new records stable IDs across backups and forks."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS deletion_edits (seq INTEGER PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, "
                "episodes TEXT NOT NULL, facts TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS deletion_pending (kind TEXT NOT NULL, ref TEXT NOT NULL, "
                "PRIMARY KEY (kind, ref))"
            )
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('deletion_lineage', ?)", (secrets.token_hex(16),))
            for table, changed in (
                ("episodes", "text, meta"),
                ("facts", "text"),
            ):
                if not any(column[1] == "deletion_ref" for column in self.db.execute(f"PRAGMA table_info({table})")):
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN deletion_ref TEXT")
                    self.db.execute(f"UPDATE {table} SET deletion_ref = lower(hex(randomblob(16)))")
                self.db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_deletion_ref ON {table} (deletion_ref)")
                self.db.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_assign_deletion_ref AFTER INSERT ON {table}
                    WHEN new.deletion_ref IS NULL BEGIN
                        UPDATE {table} SET deletion_ref = lower(hex(randomblob(16))) WHERE id = new.id;
                    END
                """)
                # These triggers also record edits made on a signed rollback slot.
                episodes = "json_array(old.deletion_ref)" if table == "episodes" else "'[]'"
                facts = "json_array(old.deletion_ref)" if table == "facts" else "'[]'"
                for operation in (f"UPDATE OF {changed}", "DELETE"):
                    name = operation.split()[0].lower()
                    self.db.execute(f"""
                        CREATE TRIGGER IF NOT EXISTS {table}_record_deletion_{name} AFTER {operation} ON {table}
                        BEGIN
                            INSERT OR IGNORE INTO deletion_pending (kind, ref)
                            SELECT '{table}', old.deletion_ref
                            WHERE EXISTS (SELECT 1 FROM meta WHERE key = 'deletion_batch');
                            INSERT INTO deletion_edits (nonce, episodes, facts)
                            SELECT lower(hex(randomblob(16))), {episodes}, {facts}
                            WHERE NOT EXISTS (SELECT 1 FROM meta WHERE key = 'deletion_batch');
                        END
                    """)

    def _append_deletion_edits(self, entries):
        self.db.executemany(
            "INSERT INTO deletion_edits (nonce, episodes, facts) VALUES (?, ?, ?)",
            ((nonce, json.dumps(episodes), json.dumps(facts)) for nonce, episodes, facts in entries),
        )

    def _begin_deletion_batch(self):
        self.db.execute("INSERT INTO meta VALUES ('deletion_batch', '1')")

    def _finish_deletion_batch(self, *, replayed=None):
        if replayed is None:
            pending = {"episodes": [], "facts": []}
            for kind, ref in self.db.execute("SELECT kind, ref FROM deletion_pending ORDER BY kind, ref"):
                pending[kind].append(ref)
            if pending["episodes"] or pending["facts"]:
                self._append_deletion_edits([(secrets.token_hex(16), pending["episodes"], pending["facts"])])
        else:
            self._append_deletion_edits(replayed)
        self.db.execute("DELETE FROM deletion_pending")
        self.db.execute("DELETE FROM meta WHERE key = 'deletion_batch'")

    def _read_deletion_ledger(self):
        lineage, = self.db.execute("SELECT value FROM meta WHERE key = 'deletion_lineage'").fetchone()
        edits = [
            [nonce, json.loads(episodes), json.loads(facts)]
            for nonce, episodes, facts in self.db.execute("SELECT nonce, episodes, facts FROM deletion_edits ORDER BY seq")
        ]
        return {"lineage": lineage, "edits": edits}

    def deletion_ledger(self):
        """Read a consistent proof of edits, carrying IDs and no conversation text."""
        with self.db:
            self.db.execute("BEGIN")
            return self._read_deletion_ledger()

    def apply_deletion_ledger(self, proof):
        """Scrub an old snapshot using an authoritative, matching ledger suffix."""
        self._validate_deletion_ledger(proof)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self._read_deletion_ledger()
            edits = proof["edits"]
            start = len(current["edits"])
            if proof["lineage"] != current["lineage"] or edits[:start] != current["edits"]:
                raise ValueError("Deletion history does not extend this backup.")
            newer = edits[start:]
            if not newer:
                return
            episodes = sorted({ref for _, refs, _ in newer for ref in refs})
            facts = sorted({ref for _, _, refs in newer for ref in refs})
            self._begin_deletion_batch()
            self._forget_days(row[0] for row in self.db.execute(
                "SELECT id FROM episodes WHERE deletion_ref IN (SELECT value FROM json_each(?))",
                (json.dumps(episodes),)))
            self.db.execute(
                f"UPDATE episodes SET text = '[forgotten]', meta = NULL, speaker = NULL, {_TOMBSTONE_TS} "
                "WHERE deletion_ref IN (SELECT value FROM json_each(?))",
                (json.dumps(episodes),),
            )
            self.db.execute(
                "DELETE FROM facts WHERE deletion_ref IN (SELECT value FROM json_each(?))",
                (json.dumps(facts),),
            )
            self._rebuild_search()
            # The replay triggers fire too. Record the original proof entries
            # rather than treating this restore as a new branch in the ledger.
            self._finish_deletion_batch(replayed=newer)
            self._advance_revision(len(newer))

    @staticmethod
    def _validate_deletion_ledger(proof):
        def is_id(value):
            return isinstance(value, str) and _DELETION_ID.fullmatch(value) is not None

        error = "Invalid deletion history."
        if not isinstance(proof, dict) or set(proof) != {"lineage", "edits"} or not is_id(proof["lineage"]):
            raise ValueError(error)
        if not isinstance(proof["edits"], list):
            raise ValueError(error)
        nonces = set()
        for edit in proof["edits"]:
            if not isinstance(edit, list) or len(edit) != 3:
                raise ValueError(error)
            nonce, episodes, facts = edit
            if not is_id(nonce) or nonce in nonces:
                raise ValueError(error)
            for refs in (episodes, facts):
                if not isinstance(refs, list) or not all(is_id(ref) for ref in refs) or refs != sorted(set(refs)):
                    raise ValueError(error)
            if not episodes and not facts:
                raise ValueError(error)
            nonces.add(nonce)

    def _rebuild_search(self, *, force=False):
        """Make sure no forgotten words survive in the search index.

        With FTS5 secure-delete the triggers' deletes already remove a row's
        terms from the index, so a forget costs only the rows it touches.
        Without it, segments keep old terms after a logical delete and the
        whole index has to be rebuilt.
        """
        if self._fts_secure and not force:
            self._sweep_derived()
            return
        self.db.execute("INSERT INTO facts_fts (facts_fts) VALUES ('rebuild')")
        self.db.execute("INSERT INTO episodes_fts (episodes_fts) VALUES ('rebuild')")
        self._sweep_derived()

    def optimize_search(self, db=None):
        """Nightly: merge the search index into one segment, for speed and size.

        `db`, for this and the other night work, is a connection of the night
        shift's own on a worker thread; the default is memory's own."""
        db = self.db if db is None else db
        with db:
            db.execute("INSERT INTO episodes_fts (episodes_fts) VALUES ('optimize')")
            db.execute("INSERT INTO facts_fts (facts_fts) VALUES ('optimize')")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _sweep_derived(self):
        """Delete everything derived, at any depth, from records now gone.

        Candidates and diary topics already go with a source by trigger; this
        follows the derivation table transitively ("a fact inferred from a
        candidate made from a forgotten sentence") until nothing changes.
        """
        for _ in range(32):
            dead = self.db.execute(
                """WITH RECURSIVE dead(kind, id) AS (
                       SELECT d.child_kind, d.child_id FROM derivations d WHERE
                          (d.parent_kind = 'episode' AND NOT EXISTS (SELECT 1 FROM episodes e
                               WHERE e.id = d.parent_id AND e.text NOT IN ('', '[forgotten]')))
                          OR (d.parent_kind = 'fact' AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.id = d.parent_id))
                          OR (d.parent_kind = 'candidate' AND NOT EXISTS (
                               SELECT 1 FROM candidate_facts c WHERE c.id = d.parent_id))
                          OR (d.parent_kind = 'diary_topic' AND NOT EXISTS (
                               SELECT 1 FROM diary_topics t WHERE t.id = d.parent_id))
                       UNION
                       SELECT d.child_kind, d.child_id FROM derivations d
                       JOIN dead ON d.parent_kind = dead.kind AND d.parent_id = dead.id
                   ) SELECT kind, id FROM dead"""
            ).fetchall()
            candidates = [row_id for kind, row_id in dead if kind == "candidate"]
            topics = [row_id for kind, row_id in dead if kind == "diary_topic"]
            changed = 0
            if candidates:
                changed += self.db.execute(
                    "DELETE FROM candidate_facts WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps(candidates),)).rowcount
            if topics:
                changed += self.db.execute(
                    "DELETE FROM diary_topics WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps(topics),)).rowcount
            changed += self.db.execute(
                """DELETE FROM derivations WHERE
                       (child_kind = 'candidate' AND NOT EXISTS (SELECT 1 FROM candidate_facts c WHERE c.id = child_id))
                    OR (child_kind = 'diary_topic' AND NOT EXISTS (SELECT 1 FROM diary_topics t WHERE t.id = child_id))"""
            ).rowcount
            if not changed:
                return

    def _advance_revision(self, amount=1):
        self.db.execute(
            "INSERT INTO meta VALUES ('memory_revision', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + CAST(excluded.value AS INTEGER)",
            (str(amount),),
        )

    # -- the log ---------------------------------------------------------

    def log(self, kind, text, speaker=None, **meta):
        """Append to the record of what happened. Returns the episode id, or
        None when the disk is too full to keep this kind of record."""
        if not self._disk_allows(kind, meta):
            return None
        now = self.now()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO episodes (ts, kind, speaker, text, meta) VALUES (?, ?, ?, ?, ?)",
                (now, kind, speaker, text, json.dumps(meta) if meta else None),
            )
        if kind in ("heard", "said"):
            self._poke_indexer()
        return cur.lastrowid

    # -- the disk guard ------------------------------------------------------

    def disk_state(self, *, refresh=False):
        """How full Pip's memory disk is, for the doctor and the night shift.

        {"state": "ok" | "low" | "critical", "free_fraction": float | None,
         "free_bytes": int | None}. An in-memory archive is always "ok".
        """
        now = time.monotonic()
        if refresh or self._disk is None or now - self._disk_checked >= DISK_CHECK_SECONDS:
            self._disk_checked = now
            self._disk = _disk_reading(self.root)
        return dict(self._disk)

    def _disk_allows(self, kind, meta):
        state = self.disk_state()["state"]
        if state == "ok" or kind == "life":
            return True
        explicit = kind == "heard" and bool(meta.get("command"))
        if state == "critical":
            # Only what the owner asked to keep: the sentence a fact came from.
            return explicit
        # Low: commands are kept, conversation is no longer archived.
        return explicit or kind == "command"

    def compact(self, db=None):
        """Give space back: merge the search index, truncate the WAL, and
        vacuum only when there is room for the copy a vacuum makes."""
        self.optimize_search(db)
        if self.root is None:
            return
        try:
            size = (self.root / "mind.db").stat().st_size
        except OSError:
            return
        reading = self.disk_state(refresh=True)
        if reading["free_bytes"] is not None and reading["free_bytes"] > 2 * size + (16 << 20):
            self._vacuum(db)
        self.disk_state(refresh=True)

    def _vacuum(self, db=None):
        db = self.db if db is None else db
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -- vectors ---------------------------------------------------------------

    def _poke_indexer(self):
        if self.embedder is None or self.root is None:
            return
        if self._indexer is None:
            self._indexer = _Indexer(self.root / "mind.db", self.embedder,
                                     allowed=lambda: self.disk_state()["state"] != "critical")
        self._indexer.poke()

    def index_pending(self, limit=EMBED_BATCH, *, recent_only=False, db=None):
        """Embed up to `limit` rows that have no vector yet. Returns how many.

        A vector is stored only if the words it was made from are still the
        words in the row, in the same statement: a forget that lands while a
        sentence is being embedded can never leave its vector behind.
        """
        embedder = self.embedder
        if embedder is None or self.disk_state()["state"] == "critical":
            return 0
        return _index_rows(self.db if db is None else db, embedder, limit, recent_only=recent_only)

    def backfill_vectors(self, limit=500, db=None):
        """Night work: remake vectors from an older model or recipe, then
        embed every row still without one (an edited row, an old archive).
        Returns how many rows were touched; 0 means it is all done."""
        if self.embedder is None:
            return 0
        db = self.db if db is None else db
        model = vector_model(self.embedder)
        with db:
            stale = db.execute(
                "DELETE FROM episode_vec WHERE episode_id IN (SELECT episode_id FROM episode_vec "
                "WHERE model <> ? LIMIT ?)", (model, limit)).rowcount
            stale += db.execute(
                "DELETE FROM fact_vec WHERE fact_id IN (SELECT fact_id FROM fact_vec WHERE model <> ? LIMIT ?)",
                (model, limit)).rowcount
        return stale + self.index_pending(limit, db=db)

    def vector_counts(self):
        """(episode vectors, fact vectors): for tests and the doctor."""
        return (self.db.execute("SELECT count(*) FROM episode_vec").fetchone()[0],
                self.db.execute("SELECT count(*) FROM fact_vec").fetchone()[0])

    def newest_timestamp(self):
        """When the most recent thing in the record happened, or None if empty."""
        row = self.db.execute(
            "SELECT max(ts) FROM (SELECT max(ts) AS ts FROM episodes UNION ALL SELECT max(ts) FROM facts)"
        ).fetchone()
        return row[0] if row else None

    # -- the clock ---------------------------------------------------------

    def _meta_number(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        try:
            return float(row[0]) if row else None
        except ValueError:
            return None

    def _note(self, key, value):
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.db.execute(
                "INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, repr(float(value))),
            )

    def _mark(self, name):
        """Remember which rows exist now, unless a mark of this name is already set."""
        if self._meta_number(f"clock_{name}_episode") is None:
            episode, = self.db.execute("SELECT coalesce(max(id), 0) FROM episodes").fetchone()
            fact, = self.db.execute("SELECT coalesce(max(id), 0) FROM facts").fetchone()
            self._note(f"clock_{name}_episode", episode)
            self._note(f"clock_{name}_fact", fact)

    def _raise_since(self, name, to):
        """Rows written since a mark, whose stamps cannot be relied on, are
        recent whatever they say. Give them a time no earlier than `to`, so they
        are never taken for old, and drop the mark."""
        episode, fact = self._meta_number(f"clock_{name}_episode"), self._meta_number(f"clock_{name}_fact")
        if episode is not None:
            self.db.execute("UPDATE episodes SET ts = ? WHERE id > ? AND ts < ?", (to, int(episode), to))
        if fact is not None:
            self.db.execute("UPDATE facts SET ts = ? WHERE id > ? AND ts < ?", (to, int(fact), to))
        self._note(f"clock_{name}_episode", None)
        self._note(f"clock_{name}_fact", None)

    def _lower_to(self, reading):
        """The clock is believed at `reading`: a later stamp is always wrong."""
        self.db.execute("UPDATE episodes SET ts = ? WHERE ts > ?", (reading, reading))
        self.db.execute("UPDATE facts SET ts = ? WHERE ts > ?", (reading, reading))
        self._note("clock_seen", reading)
        for key in ("clock_before", "clock_jump", "clock_behind_first", "clock_behind_last",
                    "clock_behind_episode", "clock_behind_fact"):
            self._note(key, None)

    def clock(self, reading=None):
        """Decide how far the system clock can be trusted.

        Returns (sure, stamp). `stamp` is the time to write on a new row.
        `sure` is the latest time the being is sure has been reached, the only
        time expiry may count from; it is None while the clock reads behind.

        The being has no network time by default and an old laptop's clock
        battery dies, so the clock is checked against what is already known:

        - Stamps never go backwards. A reading behind the latest known time
          (a dead clock battery, a date typed wrongly) writes the latest known
          time instead, and nothing expires. When the clock comes forward
          again, what was written meanwhile is stamped with that moment.
        - A clock that stays behind and keeps going for a week is accepted as
          right. Later stamps are brought down to it.
        - After a step forward of more than a day (time switched off, or a
          date typed wrongly) expiry keeps counting from the time before the
          step until the new time has held for as long as the step was, at
          least a day and at most a week. If the clock drops back to where it
          was, the step is undone.
        """
        run = self._lost_clock if self.clock_lost() else self._clock
        if self.db.in_transaction:
            return run(time.time() if reading is None else reading)
        with self.db:
            return run(time.time() if reading is None else reading)

    def clock_lost(self):
        """True while this boot's clock came from systemd's epoch, not a
        working clock battery, and nobody has set the time since."""
        if self._clock_set_by_owner:
            return False
        now = time.monotonic()
        cached = getattr(self, "_lost_cache", None)
        if cached is not None and now - cached[0] < 60.0:
            return cached[1]
        lost = False
        try:
            from . import clock_epoch

            lost = clock_epoch.epoch_advanced() is True
        except Exception:  # no way to tell is not a reason to doubt
            lost = False
        self._lost_cache = (now, lost)
        return lost

    def _lost_clock(self, reading):
        """A clock known to be wrong: stamps never go backwards, and nothing
        may expire, because no time from it can be believed."""
        noted, newest = self._meta_number("clock_seen"), self.newest_timestamp()
        known = max((value for value in (noted, newest) if value is not None), default=None)
        latest = max(known or CLOCK_FLOOR, CLOCK_FLOOR)
        if reading < latest - CLOCK_SLACK:
            # Rows written now are raised to the right time once it is known.
            self._mark("behind")
            return None, latest
        return None, reading

    def owner_set_clock(self, reading):
        """The clock was set by hand (Setup, or "set the time to" by voice).

        A time set back is believed at once: stamps later than it were made
        by the wrong clock and come down to it, so the being does not doubt
        the clock it was just given. A time set forward is treated like any
        other step: a mistyped year must not let expiry run early.
        """
        if reading < CLOCK_FLOOR:
            raise ValueError("a time before this software existed cannot be right")
        self._clock_set_by_owner = True
        with self.db:
            noted, newest = self._meta_number("clock_seen"), self.newest_timestamp()
            known = max((value for value in (noted, newest) if value is not None), default=None)
            if known is not None and reading < known - CLOCK_SLACK:
                self._lower_to(reading)
            return self._clock(reading)

    def _clock(self, reading):
        noted = self._meta_number("clock_seen")
        newest = self.newest_timestamp()
        known = max((value for value in (noted, newest) if value is not None), default=None)
        latest = max(known or CLOCK_FLOOR, CLOCK_FLOOR)
        before, jump = self._meta_number("clock_before"), self._meta_number("clock_jump")

        if reading < latest - CLOCK_SLACK:
            if (jump is not None and reading >= CLOCK_FLOOR
                    and before - CLOCK_SLACK <= reading <= before + CLOCK_STEP):
                # The step forward was a mistake and has been put right.
                self._lower_to(reading)
                return reading, reading
            self._mark("behind")
            if reading >= CLOCK_FLOOR:
                first, last = self._meta_number("clock_behind_first"), self._meta_number("clock_behind_last")
                if first is None or last is None or not last - CLOCK_SLACK <= reading <= last + CLOCK_LEAP:
                    first = last = reading
                    self._note("clock_behind_first", first)
                    self._note("clock_behind_last", last)
                elif reading - last >= CLOCK_NOTE:
                    self._note("clock_behind_last", reading)
                if reading - first >= CLOCK_SETTLE:
                    # It has kept going for a week, so it is taken as right.
                    # Which clock was wrong is not known: if this one is, the
                    # rows from here on are put right when it next steps forward.
                    # That covers what was written since it went back, and
                    # older rows whose later stamps are about to be lowered.
                    for table in ("episode", "fact"):
                        mark = self._meta_number(f"clock_behind_{table}")
                        lowered, = self.db.execute(
                            f"SELECT min(id) FROM {table}s WHERE ts > ?", (reading,)).fetchone()
                        earlier = self._meta_number(f"clock_suspect_{table}")
                        marks = [mark] + ([lowered - 1] if lowered is not None else []) + (
                            [earlier] if earlier is not None else [])
                        self._note(f"clock_suspect_{table}", min(marks))
                    self._note("clock_suspect_at", reading)
                    self._lower_to(reading)
                    return reading, reading
            return None, latest

        if self._meta_number("clock_behind_episode") is not None:
            self._raise_since("behind", reading)
            self._note("clock_behind_first", None)
            self._note("clock_behind_last", None)
        # The larger the step, the longer it has to hold: a day for a day or
        # two switched off, up to a week for anything from a week upwards.
        if jump is not None and reading - jump >= min(max(jump - before, CLOCK_HOLD), CLOCK_SETTLE):
            # The step has held. Rows written under a clock once accepted from
            # behind may have been stamped too early: bring them up to it.
            self._raise_since("suspect", jump)
            self._note("clock_suspect_at", None)
            self._note("clock_before", None)
            self._note("clock_jump", None)
            before = jump = None
        if jump is None and known is not None and reading - latest > CLOCK_STEP:
            before, jump = latest, reading
            self._note("clock_before", before)
            self._note("clock_jump", jump)
        suspect = self._meta_number("clock_suspect_at")
        if suspect is not None and jump is None and reading - suspect > CLOCK_SUSPECT:
            self._note("clock_suspect_episode", None)
            self._note("clock_suspect_fact", None)
            self._note("clock_suspect_at", None)
        if noted is None or reading - noted >= CLOCK_NOTE:
            self._note("clock_seen", reading)
        return (reading if jump is None else before), reading

    def now(self):
        """The time to write on a new row."""
        return self.clock()[1]

    def facts_between(self, start, end):
        """Ids of facts told, or told again, in a time range: forgetting a day
        takes what was said that day, even a fact first told earlier."""
        return [row[0] for row in self.db.execute(
            "SELECT id FROM facts WHERE (ts >= ? AND ts < ?) OR (confirmed >= ? AND confirmed < ?) ORDER BY id",
            (start, end, start, end))]

    def woke(self, wake):
        """True if wake number `wake` is already in the log."""
        row = self.db.execute(
            "SELECT 1 FROM episodes WHERE kind = 'life' AND json_extract(meta, '$.wake') = ? LIMIT 1", (wake,)
        ).fetchone()
        return row is not None

    def recent(self, limit=20):
        rows = self.db.execute(
            "SELECT ts, kind, speaker, text FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return rows[::-1]

    @property
    def revision(self):
        row = self.db.execute("SELECT value FROM meta WHERE key = 'memory_revision'").fetchone()
        return int(row[0]) if row else 0

    def recall_history(
        self, query, *, limit=3, start=None, end=None, all_terms=False, for_edit=False, kind=None,
        include_fact_sources=True, journal_owner=None, speaker=None, meaning=True,
    ):
        """Search retained conversations by relevance or a date range.

        With `meaning` (the default) and the embedding model installed, a
        relevance search also finds sentences by what they mean. A caller
        that keeps only rows sharing its words (an ordinary sentence looking
        for context) passes meaning=False and saves the vector search."""
        terms = list(dict.fromkeys(search_terms(query)))
        if not terms and query.strip():
            return []
        conditions = [_HISTORY]
        if for_edit:
            # Forgetting takes everything that was said, including the
            # questions that looked something up.
            conditions = ["e.text NOT IN ('', '[forgotten]')"]
        params = []
        source = "episodes e"
        excerpt = "NULL"
        order = "e.ts DESC, e.id DESC"
        if terms:
            source = "episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid"
            conditions.append("episodes_fts MATCH ?")
            joiner = " AND " if all_terms or for_edit else " OR "
            params.append(joiner.join(f'"{term}"' for term in terms))
            excerpt = "snippet(episodes_fts, 0, '', '', ' … ', 24)"
            order = f"{_HISTORY_SCORE}, {order}"
        if kind is not None:
            conditions.append("e.kind = ?")
            params.append(kind)
        if speaker is not None:
            conditions.append("e.speaker = ?")
            params.append(speaker)
        if for_edit:
            conditions.append("e.kind = 'heard'")
            # Outside date-scoped searches, the fact query covers these.
            include_fact_sources = start is not None or end is not None
        if not include_fact_sources:
            conditions.append("NOT EXISTS (SELECT 1 FROM facts WHERE facts.source = e.id)")
        if start is not None:
            conditions.append("e.ts >= ?")
            params.append(start)
        if end is not None:
            conditions.append("e.ts < ?")
            params.append(end)
        columns = f"e.id, e.ts, e.kind, e.speaker, e.text, {excerpt}"
        where = " AND ".join(conditions)
        rows = None
        if meaning and terms and not for_edit and not all_terms and type(limit) is int and limit != 0:
            rows = self._history_hybrid(
                query, terms, params[0], [c for c in conditions if c != "episodes_fts MATCH ?"], params[1:],
                where, params, limit)
        if rows is not None:
            pass
        elif terms and type(limit) is int and 0 < limit <= 6:
            # Tied hits can all pass SQLite's top-N filter as timestamps rise.
            # Rank narrow rows and build excerpts only for the final winners.
            # Keeping both passes in one statement preserves the read snapshot.
            rows = [Episode(*row) for row in self.db.execute(
                f"""WITH winners AS MATERIALIZED (
                    SELECT e.id, e.ts, {_HISTORY_SCORE} AS score
                    FROM {source} WHERE {where} ORDER BY score, e.ts DESC, e.id DESC LIMIT ?
                ) SELECT {columns} FROM winners w
                    CROSS JOIN episodes_fts ON episodes_fts.rowid = w.id
                    CROSS JOIN episodes e ON e.id = w.id
                    WHERE episodes_fts MATCH ? ORDER BY w.score, w.ts DESC, w.id DESC""",
                (*params, limit, params[0]),
            )]
        else:
            rows = [Episode(*row) for row in self.db.execute(
                f"SELECT {columns} FROM {source} WHERE {where} ORDER BY {order} LIMIT ?",
                (*params, limit),
            )]
        if not (journal_owner and rows and limit == 1 and len(terms) >= 2
                and kind in (None, "heard") and not all_terms and not for_edit):
            return rows
        match_all = " AND ".join(f'"{term}"' for term in terms)
        if self.db.execute(
            "SELECT 1 FROM episodes_fts WHERE rowid = ? AND episodes_fts MATCH ?",
            (rows[0].id, match_all),
        ).fetchone():
            return rows

        # Rank every strict match under the ordinary filters before checking
        # the journal link. An older linked source cannot win by default.
        row = self.db.execute(
            f"SELECT {columns}, EXISTS (SELECT 1 FROM dream_journal j "
            "WHERE j.owner = ? AND j.source_id = e.id AND e.kind = 'heard' AND e.speaker = j.owner) "
            f"FROM {source} WHERE {where} AND EXISTS (SELECT 1 FROM dream_journal WHERE owner = ?) "
            f"ORDER BY {order} LIMIT 1",
            (journal_owner, match_all, *params[1:], journal_owner),
        ).fetchone()
        if row and row[-1]:
            return [Episode(*row[:-1])]
        return rows

    def _history_hybrid(self, query, terms, match, plain_conditions, plain_params, where, params, limit):
        """Conversation found by its words or its meaning, under the same filters."""
        vector = self._query_vector(query)
        if vector is None:
            return None
        self._catch_up()
        lexical = [row[0] for row in self.db.execute(
            f"SELECT e.id FROM episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid WHERE {where} "
            f"ORDER BY {_HISTORY_SCORE}, e.ts DESC, e.id DESC LIMIT ?", (*params, HYBRID_POOL))]
        complete = set(lexical)
        if len(terms) > 1:
            every = " AND ".join(f'"{term}"' for term in terms)
            complete = {row[0] for row in self.db.execute(
                f"SELECT e.id FROM episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid WHERE {where} LIMIT ?",
                (every, *params[1:], HYBRID_POOL))}
        cosines = self._cosines("episode_vec", "episode_id", "JOIN episodes e ON e.id = v.episode_id",
                                " AND ".join(plain_conditions), plain_params, vector)
        pool = set(lexical) | set(cosines)
        if not pool:
            return []
        rows = {row[0]: row for row in self.db.execute(
            "SELECT e.id, e.ts, e.kind, e.speaker, e.text FROM episodes e "
            "WHERE e.id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(pool)),))}
        cosines = {i: c for i, c in cosines.items() if i in rows}
        ids = self._fuse([i for i in lexical if i in rows], complete, cosines,
                         {row_id: row[1] for row_id, row in rows.items()}, limit,
                         personal=bool(_PERSONAL.search(query.replace("’", "'"))))
        excerpts = {}
        words = [row_id for row_id in ids if row_id in set(lexical)]
        if words:
            excerpts = dict(self.db.execute(
                "SELECT rowid, snippet(episodes_fts, 0, '', '', ' … ', 24) FROM episodes_fts "
                "WHERE episodes_fts MATCH ? AND rowid IN (SELECT value FROM json_each(?))",
                (match, json.dumps(words))).fetchall())
        return [Episode(*rows[row_id], excerpts.get(row_id)) for row_id in ids]

    def history_neighbor(self, episode):
        """Return the other half of a nearby exchange, when available."""
        direction = 1 if episode.kind == "heard" else -1
        row = self.db.execute(
            "SELECT e.id, e.ts, e.kind, e.speaker, e.text, "
            "json_extract(e.meta, '$.reply_to'), json_extract(source.meta, '$.reply_to') "
            f"FROM episodes e JOIN episodes source ON source.id = ? WHERE e.id = ? AND {_HISTORY}",
            (episode.id, episode.id + direction),
        ).fetchone()
        opposite = "said" if direction == 1 else "heard"
        if row and row[2] == opposite and abs(row[1] - episode.ts) <= 120:
            linked_source, heard_id = (row[5], episode.id) if direction == 1 else (row[6], row[0])
            if linked_source in (None, heard_id):
                return Episode(*row[:5])
        return None

    def consolidation_sources(self, episode_ids, owner, *, limit=64):
        """Fetch a bounded set of canonical owner utterances by source ID."""
        if not episode_ids:
            return []
        rows = self.db.execute(
            f"SELECT e.id, e.ts, e.kind, e.speaker, e.text FROM episodes e WHERE {_HISTORY} "
            "AND e.kind = 'heard' AND e.speaker = ? "
            "AND e.id IN (SELECT value FROM json_each(?)) ORDER BY e.id DESC LIMIT ?",
            (owner, json.dumps(list(episode_ids)), limit),
        )
        return [Episode(*row) for row in rows]

    # -- facts -----------------------------------------------------------

    def _fact(self, row):
        return Fact(row[0], row[1], row[2], row[3] or "", row[4])

    def fact(self, fact_id):
        row = self.db.execute(
            "SELECT id, ts, text, speaker, confidence FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        return self._fact(row) if row else None

    def facts(self):
        rows = self.db.execute("SELECT id, ts, text, speaker, confidence FROM facts ORDER BY id")
        return [self._fact(r) for r in rows]

    def remember(self, text, speaker=None, source=None, confidence=1.0):
        """Keep a fact. Telling it the same thing twice confirms the first.

        Every telling is linked to the fact, and the latest becomes its
        source, so a fact retold yesterday does not age out with the first
        telling a month ago, and forgetting it takes every telling.
        """
        text = " ".join(text.split())
        now = self.now()
        with self.db:
            row = None
            for fact_id, existing in self.db.execute("SELECT id, text FROM facts"):
                if normalize(existing) == normalize(text):
                    row = (fact_id,)
                    break
            if row:
                if source is not None:
                    self.db.execute(
                        "INSERT OR IGNORE INTO fact_tellings (fact_id, episode_id) "
                        "SELECT id, source FROM facts WHERE id = ? AND source IS NOT NULL", (row[0],))
                    self.db.execute("INSERT OR IGNORE INTO fact_tellings VALUES (?, ?)", (row[0], source))
                self.db.execute(
                    "UPDATE facts SET confirmed = ?, confidence = max(confidence, ?), "
                    "source = coalesce(?, source) WHERE id = ?",
                    (now, confidence, source, row[0]),
                )
                return row[0]
            cur = self.db.execute(
                "INSERT INTO facts (ts, text, speaker, source, confidence) VALUES (?, ?, ?, ?, ?)",
                (now, text, speaker, source, confidence),
            )
            if source is not None:
                self.db.execute("INSERT OR IGNORE INTO fact_tellings VALUES (?, ?)", (cur.lastrowid, source))
        self._poke_indexer()
        return cur.lastrowid

    def latest_facts(self, limit=5, speaker=None):
        """The most recently told or confirmed facts, newest first."""
        rows = self.db.execute(
            "SELECT id, ts, text, speaker, confidence FROM facts "
            "WHERE (? IS NULL OR speaker = ?) ORDER BY max(ts, coalesce(confirmed, ts)) DESC, id DESC LIMIT ?",
            (speaker, speaker, limit),
        )
        return [self._fact(r) for r in rows]

    def same_subject(self, text, subject_of, *, exclude=None):
        """Other facts about the same thing: "my cat is called Tom" for
        "my cat is called Jim". `subject_of` divides a fact's wording."""
        head = subject_of(text)
        if head is None:
            return []
        wanted = normalize(head)
        found = []
        for row in self.db.execute(
                "SELECT id, ts, text, speaker, confidence FROM facts WHERE id IS NOT ? ORDER BY id", (exclude,)):
            other = subject_of(row[2])
            if normalize(row[2]) != normalize(text) and other is not None and normalize(other) == wanted:
                found.append(self._fact(row))
        return found

    def owner_lines(self, speaker, *, limit=3, before=None):
        """What `speaker` said most recently in conversation, newest first."""
        rows = self.db.execute(
            f"SELECT e.id, e.ts, e.kind, e.speaker, e.text FROM episodes e WHERE {_HISTORY} "
            "AND e.kind = 'heard' AND e.speaker = ? AND (? IS NULL OR e.id < ?) "
            "AND NOT coalesce(json_extract(e.meta, '$.command'), 0) "
            "AND NOT coalesce(json_extract(e.meta, '$.greeting'), 0) ORDER BY e.id DESC LIMIT ?",
            (speaker, before, before, limit),
        )
        return [Episode(*row) for row in rows]

    def last_heard(self, speaker, *, before=None):
        """When `speaker` last said anything at all, or None."""
        row = self.db.execute(
            "SELECT max(ts) FROM episodes WHERE kind IN ('heard', 'command') AND speaker = ? "
            "AND (? IS NULL OR id < ?)", (speaker, before, before)).fetchone()
        return row[0] if row else None

    def recall(self, query, limit=3, all_terms=False):
        """Facts that share a meaningful word with `query`, best match first.

        With `all_terms`, a fact must contain every such word. Forgetting uses
        that, so "forget my sister's birthday" does not take every birthday.
        """
        terms = search_terms(query)
        if not terms:
            return []
        if not all_terms:
            hybrid = self._recall_hybrid(query, terms, limit)
            if hybrid is not None:
                return hybrid
        match = (" AND " if all_terms else " OR ").join(f'"{t}"' for t in terms)
        rows = self.db.execute(
            "SELECT f.id, f.ts, f.text, f.speaker, f.confidence, bm25(facts_fts), "
            "max(f.ts, coalesce(f.confirmed, f.ts)) FROM facts_fts "
            "JOIN facts f ON f.id = facts_fts.rowid WHERE facts_fts MATCH ? "
            "ORDER BY bm25(facts_fts), f.ts DESC LIMIT ?",
            (match, -1 if limit < 0 else max(limit * 4, 8)),
        ).fetchall()
        if rows:
            # Matches about as good as the best are told newest first: a fact
            # told or confirmed last week beats an older one that is merely
            # shorter. bm25 is negative; nearer zero is a weaker match.
            best = rows[0][5]
            band = [r for r in rows if r[5] <= best * RECALL_BAND]
            rest = [r for r in rows if r[5] > best * RECALL_BAND]
            band.sort(key=lambda r: (r[6], r[0]), reverse=True)
            rows = band + rest
        if limit >= 0:
            rows = rows[:limit]
        return [self._fact(r) for r in rows]

    # -- hybrid recall ------------------------------------------------------

    def _query_vector(self, query):
        if self.embedder is None:
            return None
        vector = self.embedder.embed(vector_text(query))
        return vector if any(vector) else None

    def _catch_up(self, limit=16):
        """Embed the newest few rows the indexer has not reached yet, so a
        sentence said a moment ago can already be found by meaning. An
        archive in RAM (a guest, a session) has no indexer and is small, so
        it is brought fully up to date."""
        if self.db.in_transaction:
            return
        if self.root is None:
            limit = 100_000
        try:
            self.index_pending(limit, recent_only=True)
        except sqlite3.OperationalError:
            pass  # the indexer is writing; its rows will be there next time

    def _cosines(self, table, key, join, where, params, vector):
        """Cosine of `vector` with the stored vectors of rows passing `where`.

        Few rows are all scored exactly. Above PREFILTER_MIN, the sign bits
        (one popcount each) choose PREFILTER_KEEP rows for an exact score, so
        a search over 10,000 sentences stays a few milliseconds of Python.
        """
        from .embed import dot

        # Only vectors made by this model and recipe share the query's space
        # (and its length). A row still holding an older vector is found by
        # its words alone until the night shift remakes it.
        base = f"FROM {table} v {join} WHERE ({where}) AND v.model = ?"
        params = (*params, vector_model(self.embedder))
        count = self.db.execute(f"SELECT count(*) {base}", params).fetchone()[0]
        if not count:
            return {}
        if count > PREFILTER_MIN:
            query = int.from_bytes(_sign_bits(vector), "little")
            width = len(vector)
            close = []
            for row_id, bits in self.db.execute(f"SELECT v.{key}, v.bits {base}", params):
                close.append((width - (query ^ int.from_bytes(bits, "little")).bit_count(), row_id))
            close.sort(reverse=True)
            chosen = [row_id for _, row_id in close[:PREFILTER_KEEP]]
            rows = self.db.execute(
                f"SELECT {key}, scale, q8 FROM {table} WHERE {key} IN (SELECT value FROM json_each(?))",
                (json.dumps(chosen),))
        else:
            rows = self.db.execute(f"SELECT v.{key}, v.scale, v.q8 {base}", params)
        found = {}
        for row_id, scale, q8 in rows:
            if scale and len(q8) == len(vector):
                found[row_id] = dot(vector, array("b", q8)) * scale
        return found

    @staticmethod
    def _fuse(lexical, complete, cosines, told, limit, *, personal=True):
        """Reciprocal-rank fusion of the word ranking and the meaning ranking,
        with a small pull towards what was said most recently and towards the
        closer meaning (rank alone cannot separate two rows sharing one word).

        `lexical` ranks rows sharing any of the question's words; `complete`
        holds those with every word. A row with every word is always a
        candidate; a row with only some needs COSINE_FLOOR_PARTIAL; a row
        found by meaning alone needs COSINE_FLOOR. Questions that are not
        about the owner use the stricter _IMPERSONAL floors. Nothing near
        enough means "I don't recall", never a guess.
        """
        lexical = list(lexical)[:HYBRID_POOL]
        semantic = sorted(cosines, key=lambda row_id: (-cosines[row_id], -row_id))[:HYBRID_POOL]
        words = set(lexical)
        scores = {}
        for rank, row_id in enumerate(lexical, 1):
            scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, row_id in enumerate(semantic, 1):
            scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (RRF_K + rank)
        newest = max((told[row_id] for row_id in scores if row_id in told), default=None)
        floor = COSINE_FLOOR if personal else COSINE_FLOOR_IMPERSONAL
        partial = COSINE_FLOOR_PARTIAL if personal else COSINE_FLOOR_PARTIAL_IMPERSONAL
        kept = []
        for row_id, score in scores.items():
            similarity = cosines.get(row_id)
            if row_id in complete:
                pass
            elif row_id in words:
                if similarity is not None and similarity < partial:
                    continue
            elif similarity is None or similarity < floor:
                continue
            if similarity is not None:
                score += COSINE_WEIGHT * similarity
            if newest is not None and row_id in told:
                age_days = max(0.0, newest - told[row_id]) / 86_400
                score += RECENCY_WEIGHT * 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
            kept.append((score, row_id))
        kept.sort(key=lambda entry: (-entry[0], -entry[1]))
        ids = [row_id for _, row_id in kept]
        return ids if limit < 0 else ids[:limit]

    def _recall_hybrid(self, query, terms, limit):
        vector = self._query_vector(query)
        if vector is None:
            return None
        self._catch_up()
        match = " OR ".join(f'"{t}"' for t in terms)
        lexical = [row[0] for row in self.db.execute(
            "SELECT f.id FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid WHERE facts_fts MATCH ? "
            "ORDER BY bm25(facts_fts), f.ts DESC LIMIT ?", (match, HYBRID_POOL))]
        complete = {row[0] for row in self.db.execute(
            "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ? LIMIT ?",
            (" AND ".join(f'"{t}"' for t in terms), HYBRID_POOL))} if len(terms) > 1 else set(lexical)
        cosines = self._cosines("fact_vec", "fact_id", "", "1", (), vector)
        pool = set(lexical) | set(cosines)
        if not pool:
            return []
        rows = {row[0]: row for row in self.db.execute(
            "SELECT id, ts, text, speaker, confidence, max(ts, coalesce(confirmed, ts)) FROM facts "
            "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(pool)),))}
        told = {row_id: row[5] for row_id, row in rows.items()}
        cosines = {i: c for i, c in cosines.items() if i in rows}
        ids = self._fuse([i for i in lexical if i in rows], complete, cosines, told, limit,
                         personal=bool(_PERSONAL.search(query.replace("’", "'"))))
        return [self._fact(rows[row_id]) for row_id in ids]

    def forget(self, fact_ids):
        """Delete facts for good, and blank the log entries they came from."""
        return list(self.edit(fact_ids=fact_ids).fact_texts)

    def correct(self, fact_id, text):
        """Replace a fact's wording. Returns False if the fact is gone."""
        return bool(self.edit(fact_ids=(fact_id,), replacement=text).texts)

    def expire_before(self, cutoff: float, *, snapshot=False) -> MemoryEdit:
        """Expire aged conversation and its derivatives, preserving the life log.

        The snapshot option is for a disposable backup copy. It filters the
        archive without advancing the authoritative archive's deletion chain.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._expire_days(cutoff)
            expired = self._expired_rows(cutoff)
            if not expired:
                return MemoryEdit()
            episode_ids = tuple(row_id for kind, row_id, _ in expired if kind == "episode")
            fact_ids = tuple(row_id for kind, row_id, _ in expired if kind == "fact")
            self._begin_deletion_batch()
            self._redact_episodes(episode_ids)
            self._delete_facts(fact_ids)
            self._rebuild_search()
            self._finish_deletion_batch(replayed=() if snapshot else None)
            if not snapshot:
                self._advance_revision()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        texts = tuple(dict.fromkeys(text for _, _, text in expired if text not in ("", "[forgotten]")))
        fact_texts = tuple(text for kind, _, text in expired if kind == "fact")
        return MemoryEdit(texts, fact_ids, episode_ids, fact_texts)

    def _expired_rows(self, cutoff):
        """Follow exact provenance while leaving newer independent text eligible."""
        return self.db.execute(
            f"""WITH RECURSIVE
                    episode_links AS ({_EPISODE_SOURCE_LINKS}),
                    links(source_kind, source_id, target_kind, target_id) AS MATERIALIZED (
                        SELECT 'episode', source, 'episode', episode FROM episode_links
                        UNION ALL
                        SELECT 'episode', json_extract(meta, '$.reply_to'), 'episode', id
                        FROM episodes WHERE json_type(meta, '$.reply_to') = 'integer'
                        UNION ALL
                        SELECT 'fact', link.value, 'episode', e.id
                        FROM episodes e, json_each(e.meta, '$.recalled') link
                        UNION ALL
                        SELECT 'episode', source, 'fact', id FROM facts WHERE source IS NOT NULL
                        UNION ALL
                        -- A fact the dream promoted is the owner's newest
                        -- quote of it, not a telling of its own: that
                        -- sentence stays until it ages out itself.
                        SELECT 'fact', f.id, 'episode', f.source FROM facts f WHERE f.source IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM candidate_facts c JOIN candidate_sources s
                                        ON s.candidate_id = c.id WHERE c.fact_id = f.id AND s.episode_id = f.source)
                        UNION ALL
                        -- Any quote a candidate rests on takes it, and the
                        -- fact promoted from it, and whatever was made from it.
                        SELECT 'episode', episode_id, 'candidate', candidate_id FROM candidate_sources
                        UNION ALL
                        SELECT 'candidate', id, 'fact', fact_id FROM candidate_facts WHERE fact_id IS NOT NULL
                        UNION ALL
                        SELECT parent_kind, parent_id, 'candidate', child_id FROM derivations
                        WHERE child_kind = 'candidate'
                    ),
                    expired(kind, id) AS (
                        SELECT 'episode', id FROM episodes
                        WHERE ts < ? AND kind <> 'life' AND (text <> '[forgotten]' OR meta IS NOT NULL)
                        UNION
                        SELECT 'fact', id FROM facts WHERE max(ts, coalesce(confirmed, ts)) < ?
                        UNION
                        SELECT target_kind, target_id FROM links
                        JOIN expired ON source_kind = expired.kind AND source_id = expired.id
                    )
                SELECT expired.kind, e.id, e.text FROM expired JOIN episodes e ON e.id = expired.id
                WHERE expired.kind = 'episode' AND (e.text <> '[forgotten]' OR e.meta IS NOT NULL)
                UNION ALL
                SELECT expired.kind, f.id, f.text FROM expired JOIN facts f ON f.id = expired.id
                WHERE expired.kind = 'fact'
                ORDER BY 1, 2""",
            (cutoff, cutoff),
        ).fetchall()

    def _forget_days(self, episode_ids):
        """A day record counts the owner's conversation. When any of it is
        forgotten the record goes (called before the tombstone rounds the
        moment away), and that day is counted again from what remains
        whenever it is asked about. A record from before records kept their
        bounds goes for any day the moment could fall on, in any zone."""
        self.db.execute(
            "DELETE FROM diary_days WHERE EXISTS (SELECT 1 FROM episodes e "
            "WHERE e.id IN (SELECT value FROM json_each(?)) AND e.kind = 'heard' AND CASE "
            "WHEN diary_days.start_ts IS NULL OR diary_days.end_ts IS NULL "
            "THEN abs(julianday(diary_days.day) - julianday(e.ts, 'unixepoch', 'start of day')) <= 1 "
            "ELSE e.ts >= diary_days.start_ts AND e.ts < diary_days.end_ts END)",
            (json.dumps(list(episode_ids)),),
        )

    def _expire_days(self, cutoff):
        """Day records of days wholly before the cutoff go with that day's talk."""
        self.db.execute(
            "DELETE FROM diary_days WHERE CASE WHEN end_ts IS NULL "
            "THEN julianday(day) < julianday(?, 'unixepoch', 'start of day') - 1 ELSE end_ts <= ? END",
            (cutoff, cutoff),
        )

    def _redact_episodes(self, episode_ids):
        self._forget_days(episode_ids)
        # A tombstone keeps only that something was said on that day: not who
        # said it, nor the moment.
        self.db.execute(
            f"UPDATE episodes SET text = '[forgotten]', meta = NULL, speaker = NULL, {_TOMBSTONE_TS} "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(episode_ids),),
        )

    def _delete_facts(self, fact_ids):
        self.db.execute("DELETE FROM facts WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(fact_ids),))

    def edit(self, *, fact_ids=(), episode_ids=(), replacement=None):
        """Correct or forget selected memories and invalidate their derived text."""
        episode_ids = set(episode_ids)
        named_facts = set(fact_ids)
        with self.db:
            # Take the write lock before collecting source IDs. Another writer
            # cannot commit a new dependent episode halfway through this edit.
            self.db.execute("BEGIN IMMEDIATE")
            facts = self.db.execute(
                "SELECT id, text, source FROM facts WHERE id IN (SELECT value FROM json_each(?)) "
                "OR source IN (SELECT value FROM json_each(?)) "
                "OR id IN (SELECT fact_id FROM fact_tellings WHERE episode_id IN (SELECT value FROM json_each(?))) "
                "ORDER BY id",
                (json.dumps(tuple(fact_ids)), json.dumps(sorted(episode_ids)), json.dumps(sorted(episode_ids))),
            ).fetchall()
            fact_ids = tuple(row[0] for row in facts)
            fact_texts = tuple(row[1] for row in facts)
            # The owner's sentences each fact the dream promoted was found in.
            quotes = {}
            for fact, episode in self.db.execute(
                    "SELECT c.fact_id, s.episode_id FROM candidate_facts c "
                    "JOIN candidate_sources s ON s.candidate_id = c.id "
                    "WHERE c.fact_id IN (SELECT value FROM json_each(?))", (json.dumps(fact_ids),)):
                quotes.setdefault(fact, set()).add(episode)
            dropped = ()
            if replacement is not None:
                # A fact the dream drew from a sentence now being corrected
                # rested on the old words: it goes, rather than taking on
                # the new wording.
                dropped = tuple(fact for fact in fact_ids if fact in quotes and fact not in named_facts)
            linked = [(row[0], row[2]) for row in facts if row[2] is not None]
            linked += self.db.execute(
                "SELECT fact_id, episode_id FROM fact_tellings WHERE fact_id IN (SELECT value FROM json_each(?))",
                (json.dumps(fact_ids),)).fetchall()
            if replacement is None:
                # Every sentence that told a fact goes with it.
                episode_ids.update(episode for _, episode in linked)
            else:
                # A telling is corrected with its fact. The conversation the
                # dream quoted is not a telling: it stays the owner's own
                # words, and the corrected fact no longer rests on it.
                episode_ids.update(episode for fact, episode in linked
                                   if fact not in dropped and episode not in quotes.get(fact, ()))
            sources = self.db.execute(
                "SELECT id, text FROM episodes WHERE id IN (SELECT value FROM json_each(?)) "
                "AND text NOT IN ('', '[forgotten]') ORDER BY id",
                (json.dumps(sorted(episode_ids)),),
            ).fetchall()
            texts = tuple(dict.fromkeys((*fact_texts, *(text for _, text in sources))))
            if not texts:
                return MemoryEdit()

            episode_ids = tuple(row[0] for row in sources)
            # Rows are followed through their links. Wording is searched for as
            # well, as whole words only, when it is particular enough not to
            # turn up by chance: a fact someone asked to have kept, or a
            # sentence with several words of substance. "Good morning" and
            # "tea" are never searched for.
            literals = tuple(dict.fromkeys((
                *(text for text in fact_texts if len(text) >= 8),
                *(text for _, text in sources if len(text) >= 8 and len(set(search_terms(text))) >= 3),
            )))
            copies = self._verbatim_copies(literals)
            stale = self._stale_episodes(fact_ids, tuple(dict.fromkeys((*episode_ids, *copies))), ())
            stamp = self.now()
            if replacement is None:
                # Facts made from what is being forgotten, or repeating it, go too.
                whole = self._whole_words(literals)
                fact_ids = tuple(dict.fromkeys((*fact_ids, *(
                    fact for fact, text, source in self.db.execute("SELECT id, text, source FROM facts")
                    if source in stale or (whole is not None and whole.search(text))
                ))))
                # Facts the dream promoted from these sentences go too, and are
                # counted, so their words are scrubbed wherever they were said.
                fact_ids = tuple(dict.fromkeys((*fact_ids, *(row[0] for row in self.db.execute(
                    "SELECT c.fact_id FROM candidate_facts c JOIN candidate_sources s ON s.candidate_id = c.id "
                    "WHERE c.fact_id IS NOT NULL AND s.episode_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(list(stale)),))))))
            else:
                # The owner's correction wins: the fact is theirs now, no
                # longer the dream's, and the dream's candidate goes.
                rewritten = tuple(fact for fact in fact_ids if fact not in dropped)
                self.db.execute(
                    "UPDATE candidate_facts SET fact_id = NULL WHERE fact_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(rewritten),))
            self._begin_deletion_batch()
            if replacement is None:
                self._redact_episodes(stale)
                self._delete_facts(fact_ids)
            else:
                # The corrected sentences keep who said them and when; only
                # what was derived from the old wording becomes a tombstone.
                self._redact_episodes(tuple(row for row in stale if row not in episode_ids))
                self._delete_facts(dropped)
                corrected = " ".join(replacement.split())
                self.db.execute(
                    "UPDATE facts SET text = ?, confirmed = ? WHERE id IN (SELECT value FROM json_each(?))",
                    (corrected, stamp, json.dumps(rewritten)),
                )
                kept = set(episode_ids)
                for fact in rewritten:
                    untold = sorted(quotes.get(fact, set()) - kept)
                    if not untold:
                        continue
                    self.db.execute(
                        "DELETE FROM fact_tellings WHERE fact_id = ? AND episode_id IN (SELECT value FROM json_each(?))",
                        (fact, json.dumps(untold)))
                    # It ages from its latest remaining telling, or from the
                    # correction itself.
                    self.db.execute(
                        "UPDATE facts SET source = (SELECT max(episode_id) FROM fact_tellings WHERE fact_id = ?) "
                        "WHERE id = ? AND source IN (SELECT value FROM json_each(?))",
                        (fact, fact, json.dumps(untold)))
                self.db.execute(
                    # The old wording's excerpt and links go; that it was a
                    # command ("remember that ...") stays, so the corrected
                    # telling is still not taken for conversation.
                    "UPDATE episodes SET text = ?, meta = CASE WHEN coalesce(json_extract(meta, '$.command'), 0) "
                    "THEN json_object('command', json('true')) END WHERE id IN (SELECT value FROM json_each(?))",
                    (corrected, json.dumps(episode_ids)),
                )
            self._rebuild_search()
            self._advance_revision()
            self._finish_deletion_batch()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return MemoryEdit(texts, fact_ids, tuple(stale), fact_texts)

    @staticmethod
    def _whole_words(texts):
        if not texts:
            return None
        return re.compile("|".join(rf"(?<!\w){re.escape(text)}(?!\w)" for text in texts), re.IGNORECASE)

    def _verbatim_copies(self, texts):
        """Episodes that repeat forgotten wording as whole words, not as letters
        inside other words: forgetting "I like her" leaves "herbs" alone."""
        # A blank wording would match everywhere: it is never a copy of anything.
        texts = [text for text in texts if normalize(text)]
        if not texts:
            return []
        # Compared with case, apostrophes and spacing folded, so "Tom’s  bike"
        # is a copy of "tom's bike". SQLite only narrows the rows down, by the
        # longest word of each wording; Python decides.
        wordings = [normalize(text) for text in texts]
        whole = re.compile("|".join(rf"(?<!\w){re.escape(text)}(?!\w)" for text in wordings))
        # SQLite's lower() folds ASCII only, so only an ASCII word can narrow
        # the search; a wording with none ("Zoë") makes Python read every row.
        keys = set()
        for text in texts:
            # From the original wording: "Straße" folds to an ASCII "strasse"
            # that SQLite would never find in "straße".
            ascii_words = [word.lower() for word in re.findall(r"[^\W_]+", text) if word.isascii()]
            if not ascii_words:
                keys = None
                break
            keys.add(max(ascii_words, key=len))
        if keys is None:
            rows = self.db.execute("SELECT e.id, e.text FROM episodes e WHERE e.text NOT IN ('', '[forgotten]')")
        else:
            rows = self.db.execute(
                "SELECT e.id, e.text FROM episodes e WHERE e.text NOT IN ('', '[forgotten]') AND EXISTS "
                "(SELECT 1 FROM json_each(?) WHERE instr(lower(e.text), value) > 0)",
                (json.dumps(sorted(keys)),),
            )
        return [episode for episode, text in rows if whole.search(normalize(text))]

    def _stale_episodes(self, fact_ids, episode_ids, texts):
        """Follow derived replies and find verbatim copies in older history."""
        rows = self.db.execute(
            f"""WITH RECURSIVE
                   links AS MATERIALIZED ({_EPISODE_SOURCE_LINKS}),
                   stale(id) AS (
                       SELECT e.id FROM episodes e
                       WHERE e.id IN (SELECT value FROM json_each(?))
                          OR EXISTS (
                              SELECT 1 FROM json_each(e.meta, '$.recalled')
                              WHERE value IN (SELECT value FROM json_each(?))
                          )
                          OR EXISTS (
                              SELECT 1 FROM json_each(?) WHERE instr(lower(e.text), lower(value)) > 0
                          )
                       UNION
                       SELECT links.episode FROM stale JOIN links ON links.source = stale.id
                   )
               SELECT id FROM stale ORDER BY id""",
            (json.dumps(episode_ids), json.dumps(fact_ids), json.dumps(texts)),
        )
        return [row[0] for row in rows]

    def contains(self, needle):
        """True if `needle` appears anywhere in memory. For checking a forget."""
        like = f"%{needle.lower()}%"
        for table in ("facts", "episodes"):
            if self.db.execute(f"SELECT 1 FROM {table} WHERE lower(text) LIKE ? LIMIT 1", (like,)).fetchone():
                return True
        return False
