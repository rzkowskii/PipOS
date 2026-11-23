"""Durable queue for the mind's spoken reminders and timers.

The mind is the single announcer. Reading a due row leaves it in the queue
until that announcer confirms playback. If the mind or voice stops before
confirmation, the row can be announced again after restart.
"""

import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


MAX_RINGS = 3          # a due item is said up to three times until acknowledged
RING_GAP = 120.0       # seconds between repeats


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
                    kind TEXT NOT NULL CHECK (kind IN ('reminder', 'timer'))
                );
                CREATE INDEX IF NOT EXISTS reminders_due ON reminders (due_at, id);
                CREATE TABLE IF NOT EXISTS greeting_days (
                    day TEXT PRIMARY KEY
                );
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(reminders)")}
            with self.db:
                for name, kind in (("created_at", "REAL"), ("duration", "INTEGER"),
                                   ("rings", "INTEGER NOT NULL DEFAULT 0"), ("next_at", "REAL")):
                    if name not in columns:
                        self.db.execute(f"ALTER TABLE reminders ADD COLUMN {name} {kind}")
        except sqlite3.Error:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def add(self, text, due_at, *, kind="reminder", duration=None, now=None) -> Reminder:
        """Store a task at an absolute Unix timestamp before confirming it.

        `duration` is a timer's length in seconds, kept so it can be named
        when it rings ("your thirty-minute timer").
        """
        text = " ".join(text.split())
        due_at = float(due_at)
        if not text or not math.isfinite(due_at):
            raise ValueError("a reminder needs text and a valid due time")
        if kind not in ("reminder", "timer"):
            raise ValueError("unknown reminder kind")
        if duration is not None and (type(duration) is not int or duration <= 0):
            raise ValueError("a timer duration is a positive number of seconds")
        with self.db:
            row = self.db.execute(
                "INSERT INTO reminders (text, due_at, kind, created_at, duration) VALUES (?, ?, ?, ?, ?)",
                (text, due_at, kind, time.time() if now is None else now, duration),
            )
        return Reminder(row.lastrowid, text, due_at, kind)

    def delivery(self, reminder_id) -> Delivery | None:
        row = self.db.execute("SELECT created_at, duration, rings FROM reminders WHERE id = ?",
                              (reminder_id,)).fetchone()
        return Delivery(*row) if row is not None else None

    def rang(self, reminder_id, now=None) -> bool:
        """Note one spoken delivery. Returns True if it will be said again.

        After MAX_RINGS deliveries with no acknowledgement the entry is done.
        """
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT rings FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            if row is None:
                return False
            rings = row[0] + 1
            if rings >= MAX_RINGS:
                self.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                return False
            self.db.execute("UPDATE reminders SET rings = ?, next_at = ? WHERE id = ?",
                            (rings, now + RING_GAP, reminder_id))
        return True

    def acknowledge(self, reminder_id) -> bool:
        """The owner answered: stop repeating it."""
        return self.cancel(reminder_id)

    def forget_mentioning(self, texts) -> int:
        """Forgetting reaches reminders too: drop any whose task holds forgotten words."""
        wanted = [" ".join(str(text).split()).casefold() for text in texts if str(text).strip()]
        removed = 0
        for entry in self.upcoming():
            task = entry.text.casefold()
            if any(text in task or (len(task) >= 4 and task in text) for text in wanted):
                removed += self.cancel(entry.id)
        return removed

    def upcoming(self, limit=None, *, kind=None) -> list[Reminder]:
        """Return pending entries, including any that are already due."""
        rows = self.db.execute(
            """SELECT id, text, due_at, kind FROM reminders
               WHERE (? IS NULL OR kind = ?) ORDER BY due_at, id LIMIT ?""",
            (kind, kind, -1 if limit is None else limit),
        )
        return [Reminder(*row) for row in rows]

    def matching(self, text, *, kind=None) -> list[Reminder]:
        """Return all exact task matches, ignoring casing and spacing."""
        wanted = " ".join(text.split()).casefold()
        return [entry for entry in self.upcoming(kind=kind) if entry.text.casefold() == wanted]

    def due(self, now=None) -> Reminder | None:
        """Peek at the oldest due entry while leaving it pending."""
        now = time.time() if now is None else now
        row = self.db.execute(
            """SELECT id, text, due_at, kind FROM reminders
               WHERE due_at <= ? AND (next_at IS NULL OR next_at <= ?) ORDER BY due_at, id LIMIT 1""",
            (now, now),
        ).fetchone()
        return Reminder(*row) if row is not None else None

    def cancel(self, reminder_id, *, kind=None) -> bool:
        """Remove exactly one pending entry by its unique ID."""
        with self.db:
            row = self.db.execute(
                "DELETE FROM reminders WHERE id = ? AND (? IS NULL OR kind = ?)", (reminder_id, kind, kind)
            )
        return bool(row.rowcount)

    def complete(self, reminder_id) -> bool:
        """Remove an entry after playback. Repeated confirmation is safe."""
        return self.cancel(reminder_id)

    def greeted_on(self, day) -> bool:
        return self.db.execute("SELECT 1 FROM greeting_days WHERE day = ?", (day.isoformat(),)).fetchone() is not None

    def record_greeting(self, day):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO greeting_days (day) VALUES (?)", (day.isoformat(),))
