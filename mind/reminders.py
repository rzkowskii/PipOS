"""Durable queue for the mind's spoken reminders, timers and alarms.

The mind is the single announcer. Reading a due row leaves it in the queue
until that announcer confirms playback. If the mind or voice stops before
confirmation, the row can be announced again after restart.

A row may repeat. Its rule is "daily", "weekdays" or "weekly:<day>" (mon to
sun), with the wall-clock time it rings at and the time zone it was set in.
Finishing a repeating row moves it to its next occurrence instead of
deleting it; only a cancel (or a forget) removes the whole series.

A row said as a delay ("in two hours", "an alarm for ten minutes") is
`relative`: like a timer it counts elapsed seconds, so it moves with a clock
correction and may ring while the clock is doubted. A clock time set while
the clock is doubted is `held_at` its spoken day offset and wall time
("+1 09:00"), and is worked out again once the owner sets the clock.
"""

import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_RINGS = 3          # a due item is said up to three times until acknowledged
RING_GAP = 120.0       # seconds between repeats
SNOOZE_LIMIT = 120     # minutes; a longer snooze is a new reminder
STALE_REPEAT = 12 * 3600.0  # a repeat missed by this much skips to its next time
KINDS = ("reminder", "timer", "alarm")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEMA_VERSION = 3


@dataclass(frozen=True)
class Delivery:
    """How an entry has been delivered so far, for honest wording."""
    created_at: float | None
    duration: int | None
    rings: int


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    due_at: float
    kind: str
    rule: str | None = None
    at_local: str | None = None


def valid_rule(rule):
    return rule in ("daily", "weekdays") or (
        isinstance(rule, str) and rule.startswith("weekly:") and rule[7:] in WEEKDAYS)


def rule_days(rule):
    """The weekdays (0 is Monday) a rule rings on."""
    if rule == "daily":
        return frozenset(range(7))
    if rule == "weekdays":
        return frozenset(range(5))
    if valid_rule(rule):
        return frozenset((WEEKDAYS.index(rule[7:]),))
    raise ValueError("unknown repeat rule")


def _zone(zone):
    if zone is None or hasattr(zone, "utcoffset"):
        return zone
    try:
        return ZoneInfo(zone) if zone else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def wall_stamp(day, hour, minute, zone):
    """The moment a wall clock in `zone` shows hour:minute on `day`.

    A time skipped when the clocks go forward rings at the same distance
    after the change (2:30 becomes 3:30); a time that happens twice when
    they go back rings the first time.
    """
    zone = _zone(zone)
    when = datetime.combine(day, clock_time(hour, minute))
    if zone is None:
        return when.timestamp()
    return when.replace(tzinfo=zone, fold=0).timestamp()


PLACEHOLDERS = ("alarm", "timer")  # names Pip gives, not words the owner said


def _words(text):
    return re.findall(r"[\w']+", str(text).casefold().replace("’", "'"))


def _holds(outer, inner):
    """True if the word list `inner` appears, in order and unbroken, in `outer`."""
    size = len(inner)
    return 0 < size <= len(outer) and any(outer[start:start + size] == inner
                                          for start in range(len(outer) - size + 1))


def mentions(task_text, texts):
    """True if a task holds forgotten words, or is held whole in them
    (Reminders.forget_mentioning). "Alarm" and "Timer" never match."""
    task = _words(task_text)
    if not task or str(task_text).casefold() in PLACEHOLDERS:
        return False
    wanted = [words for text in texts or () if (words := _words(text))]
    return any(_holds(task, text) or (len(" ".join(task)) >= 4 and _holds(text, task)) for text in wanted)


def next_occurrence(rule, at_local, zone, after):
    """The first time strictly after `after` that the rule rings."""
    days = rule_days(rule)
    hour, minute = (int(part) for part in at_local.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("invalid repeat time")
    zone = _zone(zone)
    start = datetime.fromtimestamp(after, zone).date()
    for offset in range(9):
        day = start + timedelta(days=offset)
        if day.weekday() in days:
            stamp = wall_stamp(day, hour, minute, zone)
            if stamp > after:
                return stamp
    raise ValueError("no next occurrence")  # unreachable for a valid rule


_COLUMNS = "id, text, due_at, kind, rule, at_local"


class Reminders:
    def __init__(self, state_dir):
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / "reminders.db"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
        self.db = sqlite3.connect(path)
        try:
            self.db.execute("PRAGMA secure_delete = ON")
            self.db.execute("PRAGMA synchronous = FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('reminder', 'timer', 'alarm')),
                    created_at REAL,
                    duration INTEGER,
                    rings INTEGER NOT NULL DEFAULT 0,
                    next_at REAL,
                    rule TEXT,
                    at_local TEXT,
                    zone TEXT,
                    relative INTEGER NOT NULL DEFAULT 0,
                    held_at TEXT
                );
                CREATE INDEX IF NOT EXISTS reminders_due ON reminders (due_at, id);
                CREATE TABLE IF NOT EXISTS greeting_days (
                    day TEXT PRIMARY KEY
                );
            """)
            self._migrate()
        except sqlite3.Error:
            self.db.close()
            raise

    def _migrate(self):
        """Bring an older queue up to date without losing a row or reusing an ID."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(reminders)")}
        sql = self.db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reminders'").fetchone()[0]
        with self.db:
            for name, kind in (("created_at", "REAL"), ("duration", "INTEGER"),
                               ("rings", "INTEGER NOT NULL DEFAULT 0"), ("next_at", "REAL"),
                               ("rule", "TEXT"), ("at_local", "TEXT"), ("zone", "TEXT"),
                               ("relative", "INTEGER NOT NULL DEFAULT 0"), ("held_at", "TEXT")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE reminders ADD COLUMN {name} {kind}")
            if "'alarm'" not in sql:
                # The kind check cannot be altered in place: rebuild the table.
                self.db.execute("""
                    CREATE TABLE reminders_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        text TEXT NOT NULL,
                        due_at REAL NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('reminder', 'timer', 'alarm')),
                        created_at REAL,
                        duration INTEGER,
                        rings INTEGER NOT NULL DEFAULT 0,
                        next_at REAL,
                        rule TEXT,
                        at_local TEXT,
                        zone TEXT,
                        relative INTEGER NOT NULL DEFAULT 0,
                        held_at TEXT
                    )""")
                sequence = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'reminders'").fetchone()
                self.db.execute("""
                    INSERT INTO reminders_v2 (id, text, due_at, kind, created_at, duration, rings, next_at,
                                              rule, at_local, zone, relative, held_at)
                    SELECT id, text, due_at, kind, created_at, duration, rings, next_at, rule, at_local, zone,
                           relative, held_at
                    FROM reminders WHERE kind IN ('reminder', 'timer')""")
                self.db.execute("DROP TABLE reminders")
                self.db.execute("ALTER TABLE reminders_v2 RENAME TO reminders")
                self.db.execute("CREATE INDEX IF NOT EXISTS reminders_due ON reminders (due_at, id)")
                if sequence is not None:
                    current = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'reminders'").fetchone()
                    if current is None:
                        self.db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('reminders', ?)",
                                        (sequence[0],))
                    else:
                        self.db.execute("UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = 'reminders'",
                                        (sequence[0],))
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self):
        self.db.close()

    def add(self, text, due_at, *, kind="reminder", duration=None, now=None, rule=None, at_local=None,
            zone=None, relative=False, held=False) -> Reminder:
        """Store a task at an absolute Unix timestamp before confirming it.

        `duration` is a timer's length in seconds, kept so it can be named
        when it rings ("your thirty-minute timer"). A repeating entry has a
        `rule`, its wall-clock `at_local` ("07:00") and the `zone` it keeps.
        `relative` marks a delay ("in two hours"); `held` marks a one-off
        clock time set while the clock is doubted, worked out again from its
        day offset and wall time in `zone` when the clock is set.
        """
        text = " ".join(text.split())
        due_at = float(due_at)
        if not text or not math.isfinite(due_at):
            raise ValueError("a reminder needs text and a valid due time")
        if kind not in KINDS:
            raise ValueError("unknown reminder kind")
        if duration is not None and (type(duration) is not int or duration <= 0):
            raise ValueError("a timer duration is a positive number of seconds")
        if rule is not None:
            if kind == "timer" or not valid_rule(rule) or not isinstance(at_local, str):
                raise ValueError("a repeat needs a rule and a wall-clock time")
            next_occurrence(rule, at_local, zone, due_at - 1)  # validates the time
        created = time.time() if now is None else now
        relative = bool(relative) or kind == "timer"
        held_at = None
        if held and rule is None and not relative:
            # The day as said ("tomorrow" is +1) and the wall time, both read
            # from the doubted clock; the day moves with the correction.
            local_zone = _zone(zone)
            due_local = datetime.fromtimestamp(due_at, local_zone)
            offset = (due_local.date() - datetime.fromtimestamp(created, local_zone).date()).days
            held_at = f"{offset:+d} {due_local.hour:02d}:{due_local.minute:02d}"
        zone_key = getattr(zone, "key", zone) if rule is not None or held_at is not None else None
        with self.db:
            row = self.db.execute(
                "INSERT INTO reminders (text, due_at, kind, created_at, duration, rule, at_local, zone, "
                "relative, held_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (text, due_at, kind, created, duration,
                 rule, at_local if rule is not None else None, zone_key, 1 if relative else 0, held_at),
            )
        return Reminder(row.lastrowid, text, due_at, kind, rule, at_local if rule is not None else None)

    def get(self, reminder_id) -> Reminder | None:
        row = self.db.execute(f"SELECT {_COLUMNS} FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return Reminder(*row) if row is not None else None

    def delivery(self, reminder_id) -> Delivery | None:
        row = self.db.execute("SELECT created_at, duration, rings FROM reminders WHERE id = ?",
                              (reminder_id,)).fetchone()
        return Delivery(*row) if row is not None else None

    def _finish(self, reminder_id, now, *, only_due=False):
        """End this occurrence: delete a one-off, move a repeat to its next time.

        Runs inside the caller's transaction. Returns True if a row was found.
        With `only_due`, a repeat already moved to a later time stays put, so
        a playback confirmed twice does not skip an occurrence.
        """
        row = self.db.execute("SELECT due_at, rule, at_local, zone FROM reminders WHERE id = ?",
                              (reminder_id,)).fetchone()
        if row is None:
            return False
        due_at, rule, at_local, zone = row
        if rule is None:
            self.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            return True
        if only_due and due_at > now:
            return True
        try:
            following = next_occurrence(rule, at_local, zone, max(now, due_at))
        except ValueError:
            self.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            return True
        self.db.execute("UPDATE reminders SET due_at = ?, rings = 0, next_at = NULL WHERE id = ?",
                        (following, reminder_id))
        return True

    def rang(self, reminder_id, now=None) -> bool:
        """Note one spoken delivery. Returns True if it will be said again.

        After MAX_RINGS deliveries with no acknowledgement this occurrence is
        done: a one-off is removed and a repeat waits for its next time.
        """
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT rings FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            if row is None:
                return False
            rings = row[0] + 1
            if rings >= MAX_RINGS:
                self._finish(reminder_id, now)
                return False
            self.db.execute("UPDATE reminders SET rings = ?, next_at = ? WHERE id = ?",
                            (rings, now + RING_GAP, reminder_id))
        return True

    def acknowledge(self, reminder_id, now=None) -> bool:
        """The owner answered: stop repeating it (a repeat moves to its next time).

        An "okay" just after the last ring finds a repeat already moved to
        its next time; that one stays put, so tomorrow's alarm still rings.
        A snoozed repeat (due later, but not at its own time) still moves on.
        """
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT due_at, rule, at_local, zone FROM reminders WHERE id = ?",
                                  (reminder_id,)).fetchone()
            if row is not None:
                due_at, rule, at_local, zone = row
                if rule is not None and due_at > now:
                    try:
                        waiting = next_occurrence(rule, at_local, zone, now) == due_at
                    except ValueError:
                        waiting = False
                    if waiting:
                        return True
            return self._finish(reminder_id, now)

    def snooze(self, reminder_id, minutes, now=None) -> Reminder | None:
        """Ring again `minutes` from now. Returns the moved entry, or None."""
        now = time.time() if now is None else now
        if type(minutes) not in (int, float) or not 0 < minutes <= SNOOZE_LIMIT:
            raise ValueError(f"a snooze is between 1 and {SNOOZE_LIMIT} minutes")
        with self.db:
            moved = self.db.execute(
                "UPDATE reminders SET due_at = ?, rings = 0, next_at = NULL WHERE id = ?",
                (now + minutes * 60.0, reminder_id))
        return self.get(reminder_id) if moved.rowcount else None

    def clock_moved(self, delta, now=None, *, resolve_wall=True) -> int:
        """The wall clock was set by `delta` seconds: delays keep counting real time.

        A timer, or a reminder or alarm said as a delay ("in two hours"),
        measures elapsed seconds, so it still rings that long after it was
        set, not 37 days "late". A clock time set while the clock was doubted
        is worked out again from the corrected day. A repeat that the wrong
        date put beyond its next time comes back to it. Other reminders and
        alarms keep their wall-clock times. Returns how many entries moved.
        """
        delta = float(delta)
        if not math.isfinite(delta):
            raise ValueError("a clock change is a finite number of seconds")
        if not delta:
            return 0
        now = time.time() if now is None else now
        with self.db:
            moved = self.db.execute(
                "UPDATE reminders SET due_at = due_at + ?, created_at = created_at + ?, "
                "next_at = CASE WHEN next_at IS NULL THEN NULL ELSE next_at + ? END "
                "WHERE kind = 'timer' OR relative = 1",
                (delta, delta, delta)).rowcount
            if not resolve_wall:
                # Preserve the held request's day offset until a trusted date
                # arrives, while keeping its reference in the same clock.
                self.db.execute("UPDATE reminders SET created_at = created_at + ? WHERE held_at IS NOT NULL",
                                (delta,))
                return moved
            held = self.db.execute(
                "SELECT id, created_at, held_at, zone FROM reminders WHERE held_at IS NOT NULL").fetchall()
            for reminder_id, created_at, held_at, zone in held:
                created = (created_at if created_at is not None else now - delta) + delta
                match = re.fullmatch(r"([+-][0-9]+) ([0-9]{2}):([0-9]{2})", held_at or "")
                if match is None:
                    continue
                local_zone = _zone(zone)
                day = datetime.fromtimestamp(created, local_zone).date() + timedelta(days=int(match[1]))
                stamp = wall_stamp(day, int(match[2]), int(match[3]), local_zone)
                self.db.execute("UPDATE reminders SET due_at = ?, created_at = ?, held_at = NULL, rings = 0, "
                                "next_at = NULL WHERE id = ?", (stamp, created, reminder_id))
                moved += 1
            repeats = self.db.execute(
                "SELECT id, due_at, rule, at_local, zone FROM reminders WHERE rule IS NOT NULL AND due_at > ?",
                (now,)).fetchall()
            for reminder_id, due_at, rule, at_local, zone in repeats:
                try:
                    following = next_occurrence(rule, at_local, zone, now)
                except ValueError:
                    continue
                if due_at > following:
                    self.db.execute("UPDATE reminders SET due_at = ?, rings = 0, next_at = NULL WHERE id = ?",
                                    (following, reminder_id))
                    moved += 1
        return moved

    def ringing(self, now=None) -> Reminder | None:
        """The entry a "snooze" or "okay" is about: the latest one already due."""
        now = time.time() if now is None else now
        row = self.db.execute(
            f"""SELECT {_COLUMNS} FROM reminders WHERE due_at <= ?
                ORDER BY rings > 0 DESC, due_at DESC, id DESC LIMIT 1""", (now,)).fetchone()
        return Reminder(*row) if row is not None else None

    def forget_mentioning(self, texts) -> int:
        """Forgetting reaches reminders too: drop any whose task holds forgotten words.

        Words are matched whole and in order: a forgotten "Mum" drops "call
        Mum", and a forgotten "call Mum on Sunday" drops "call Mum". The
        placeholder names "Alarm" and "Timer" are not the owner's words, so
        forgetting a fact about an alarm clock never cancels the alarms.
        """
        removed = 0
        for entry in self.upcoming():
            if mentions(entry.text, texts):
                removed += self.cancel(entry.id)
        return removed

    def erase_all(self):
        """Delete every entry and the greeted-days record ("forget everything").

        The file stays (the mind keeps it open); secure_delete and a VACUUM
        overwrite what was in it.
        """
        with self.db:
            self.db.execute("DELETE FROM reminders")
            self.db.execute("DELETE FROM greeting_days")
        self.db.execute("VACUUM")

    def upcoming(self, limit=None, *, kind=None) -> list[Reminder]:
        """Return pending entries, including any that are already due."""
        rows = self.db.execute(
            f"""SELECT {_COLUMNS} FROM reminders
               WHERE (? IS NULL OR kind = ?) ORDER BY due_at, id LIMIT ?""",
            (kind, kind, -1 if limit is None else limit),
        )
        return [Reminder(*row) for row in rows]

    def between(self, start, end, *, kinds=("reminder", "alarm"), limit=None) -> list[Reminder]:
        """Entries due in [start, end), for "today's reminders"."""
        marks = ", ".join("?" for _ in kinds)
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM reminders WHERE due_at >= ? AND due_at < ? AND kind IN ({marks}) "
            "ORDER BY due_at, id LIMIT ?", (start, end, *kinds, -1 if limit is None else limit))
        return [Reminder(*row) for row in rows]

    def matching(self, text, *, kind=None) -> list[Reminder]:
        """Return all exact task matches, ignoring casing and spacing."""
        wanted = " ".join(text.split()).casefold()
        return [entry for entry in self.upcoming(kind=kind) if entry.text.casefold() == wanted]

    def mentioning(self, words, *, kind=None) -> list[Reminder]:
        """Entries whose task holds every one of `words` ("Mum" finds "call Mum")."""
        wanted = [word.casefold() for word in words if word]
        if not wanted:
            return []
        return [entry for entry in self.upcoming(kind=kind)
                if all(word in entry.text.casefold().split() for word in wanted)]

    def _roll_stale(self, now):
        """A repeat missed by many hours waits for its next time instead of ringing late."""
        stale = self.db.execute(
            "SELECT id FROM reminders WHERE rule IS NOT NULL AND due_at < ?", (now - STALE_REPEAT,)).fetchall()
        if stale:
            with self.db:
                for (reminder_id,) in stale:
                    self._finish(reminder_id, now)

    def due(self, now=None, *, clock_trusted=True) -> Reminder | None:
        """Peek at the oldest due entry while leaving it pending.

        While the clock is doubted only timers and delays are due: they count
        elapsed seconds, while clock-time reminders and alarms need the right
        wall-clock time.
        """
        now = time.time() if now is None else now
        if clock_trusted:
            self._roll_stale(now)
        row = self.db.execute(
            f"""SELECT {_COLUMNS} FROM reminders
               WHERE due_at <= ? AND held_at IS NULL AND (next_at IS NULL OR next_at <= ?)
                 AND (? OR kind = 'timer' OR relative = 1)
               ORDER BY due_at, id LIMIT 1""",
            (now, now, 1 if clock_trusted else 0),
        ).fetchone()
        return Reminder(*row) if row is not None else None

    def cancel(self, reminder_id, *, kind=None) -> bool:
        """Remove exactly one pending entry (a whole repeating series) by its unique ID."""
        with self.db:
            row = self.db.execute(
                "DELETE FROM reminders WHERE id = ? AND (? IS NULL OR kind = ?)", (reminder_id, kind, kind)
            )
        return bool(row.rowcount)

    def complete(self, reminder_id, now=None) -> bool:
        """End an entry's occurrence after playback. Repeated confirmation is safe."""
        now = time.time() if now is None else now
        with self.db:
            return self._finish(reminder_id, now, only_due=True)

    def greeted_on(self, day) -> bool:
        return self.db.execute("SELECT 1 FROM greeting_days WHERE day = ?", (day.isoformat(),)).fetchone() is not None

    def record_greeting(self, day):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO greeting_days (day) VALUES (?)", (day.isoformat(),))


def erase_all(state_dir):
    """"Forget everything" reaches reminders, alarms, timers and greeted days."""
    path = Path(state_dir) / "reminders.db"
    if not path.is_file():
        return
    reminders = Reminders(state_dir)
    try:
        reminders.erase_all()
    finally:
        reminders.close()
