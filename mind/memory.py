"""Memory: an append-only log of what happened, and facts it was asked to keep.

Plain SQLite with full-text search. Everything here can be read with the
`sqlite3` command-line tool, and forgetting really deletes.
"""

import json
import re
import secrets
import sqlite3
import time
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
"""

# Words too common to search on.
_STOP = frozenset(
    """a about again all also am an and any are as at be been but by can could did do does
    for from had has have he her him his how i if in is it its just know like me my no not
    of on or our please remember said say she so some tell than that the their them then
    there these they thing things this to told too us was we were what when where which
    who why will with would you your yours anything something everything""".split()
)
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")

_HISTORY = """e.kind IN ('heard', 'said') AND e.text NOT IN ('', '[forgotten]')
              AND NOT coalesce(json_extract(e.meta, '$.memory_reply'), 0)"""
_HISTORY_SCORE = "bm25(episodes_fts) * CASE e.kind WHEN 'heard' THEN 1.25 ELSE 1 END"
_DELETION_ID = re.compile(r"[0-9a-f]{32}")
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
    return [w for w in words if len(w) > 1 and w not in _STOP]


class Memory:
    def __init__(self, root):
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
            added = self.db.execute("INSERT OR IGNORE INTO meta VALUES ('episode_search', '1')")
            if added.rowcount:
                self.db.execute("INSERT INTO episodes_fts (episodes_fts) VALUES ('rebuild')")
        self._initialize_deletions()

    def close(self):
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
            self.db.execute(
                "UPDATE episodes SET text = '[forgotten]', meta = NULL "
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

    def _rebuild_search(self):
        # FTS segments retain old terms after logical deletion.
        self.db.execute("INSERT INTO facts_fts (facts_fts) VALUES ('rebuild')")
        self.db.execute("INSERT INTO episodes_fts (episodes_fts) VALUES ('rebuild')")

    def _advance_revision(self, amount=1):
        self.db.execute(
            "INSERT INTO meta VALUES ('memory_revision', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + CAST(excluded.value AS INTEGER)",
            (str(amount),),
        )

    # -- the log ---------------------------------------------------------

    def log(self, kind, text, speaker=None, **meta):
        """Append to the record of what happened. Returns the episode id."""
        now = self.now()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO episodes (ts, kind, speaker, text, meta) VALUES (?, ?, ?, ?, ?)",
                (now, kind, speaker, text, json.dumps(meta) if meta else None),
            )
        return cur.lastrowid

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
        if self.db.in_transaction:
            return self._clock(time.time() if reading is None else reading)
        with self.db:
            return self._clock(time.time() if reading is None else reading)

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
        """Ids of facts first told in a time range."""
        return [row[0] for row in self.db.execute("SELECT id FROM facts WHERE ts >= ? AND ts < ? ORDER BY id", (start, end))]

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
        include_fact_sources=True, journal_owner=None,
    ):
        """Search retained conversations by relevance or a date range."""
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
        if terms and type(limit) is int and 0 < limit <= 6:
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
        """Keep a fact. Telling it the same thing twice confirms the first."""
        text = " ".join(text.split())
        now = self.now()
        with self.db:
            row = self.db.execute("SELECT id FROM facts WHERE lower(text) = lower(?)", (text,)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE facts SET confirmed = ?, confidence = max(confidence, ?) WHERE id = ?",
                    (now, confidence, row[0]),
                )
                return row[0]
            cur = self.db.execute(
                "INSERT INTO facts (ts, text, speaker, source, confidence) VALUES (?, ?, ?, ?, ?)",
                (now, text, speaker, source, confidence),
            )
        return cur.lastrowid

    def recall(self, query, limit=3, all_terms=False):
        """Facts that share a meaningful word with `query`, best match first.

        With `all_terms`, a fact must contain every such word. Forgetting uses
        that, so "forget my sister's birthday" does not take every birthday.
        """
        terms = search_terms(query)
        if not terms:
            return []
        match = (" AND " if all_terms else " OR ").join(f'"{t}"' for t in terms)
        rows = self.db.execute(
            "SELECT f.id, f.ts, f.text, f.speaker, f.confidence FROM facts_fts "
            "JOIN facts f ON f.id = facts_fts.rowid WHERE facts_fts MATCH ? "
            "ORDER BY bm25(facts_fts), f.ts DESC LIMIT ?",
            (match, limit),
        ).fetchall()
        return [self._fact(r) for r in rows]

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
                        SELECT 'fact', id, 'episode', source FROM facts WHERE source IS NOT NULL
                    ),
                    expired(kind, id) AS (
                        SELECT 'episode', id FROM episodes
                        WHERE ts < ? AND kind <> 'life' AND (text <> '[forgotten]' OR meta IS NOT NULL)
                        UNION
                        SELECT 'fact', id FROM facts WHERE ts < ?
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

    def _redact_episodes(self, episode_ids):
        self.db.execute(
            "UPDATE episodes SET text = '[forgotten]', meta = NULL "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(episode_ids),),
        )

    def _delete_facts(self, fact_ids):
        self.db.execute("DELETE FROM facts WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(fact_ids),))

    def edit(self, *, fact_ids=(), episode_ids=(), replacement=None):
        """Correct or forget selected memories and invalidate their derived text."""
        episode_ids = set(episode_ids)
        with self.db:
            # Take the write lock before collecting source IDs. Another writer
            # cannot commit a new dependent episode halfway through this edit.
            self.db.execute("BEGIN IMMEDIATE")
            facts = self.db.execute(
                "SELECT id, text, source FROM facts WHERE id IN (SELECT value FROM json_each(?)) "
                "OR source IN (SELECT value FROM json_each(?)) ORDER BY id",
                (json.dumps(tuple(fact_ids)), json.dumps(sorted(episode_ids))),
            ).fetchall()
            fact_ids = tuple(row[0] for row in facts)
            fact_texts = tuple(row[1] for row in facts)
            episode_ids.update(row[2] for row in facts if row[2] is not None)
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
            self._begin_deletion_batch()
            self._redact_episodes(stale)
            if replacement is None:
                self._delete_facts(fact_ids)
            else:
                corrected = " ".join(replacement.split())
                self.db.execute(
                    "UPDATE facts SET text = ?, confirmed = ? WHERE id IN (SELECT value FROM json_each(?))",
                    (corrected, stamp, json.dumps(fact_ids)),
                )
                self.db.execute(
                    "UPDATE episodes SET text = ? WHERE id IN (SELECT value FROM json_each(?))",
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
        if not texts:
            return []
        whole = self._whole_words(texts)
        rows = self.db.execute(
            "SELECT e.id, e.text FROM episodes e WHERE EXISTS "
            "(SELECT 1 FROM json_each(?) WHERE instr(lower(e.text), lower(value)) > 0)",
            (json.dumps(texts),),
        )
        return [episode for episode, text in rows if whole.search(text)]

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
