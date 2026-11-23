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
        except sqlite3.Error:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def add(self, text, due_at, *, kind="reminder") -> Reminder:
        """Store a task at an absolute Unix timestamp before confirming it."""
        text = " ".join(text.split())
        due_at = float(due_at)
        if not text or not math.isfinite(due_at):
            raise ValueError("a reminder needs text and a valid due time")
        if kind not in ("reminder", "timer"):
            raise ValueError("unknown reminder kind")
        with self.db:
            row = self.db.execute(
                "INSERT INTO reminders (text, due_at, kind) VALUES (?, ?, ?)", (text, due_at, kind)
            )
        return Reminder(row.lastrowid, text, due_at, kind)

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
        row = self.db.execute(
            """SELECT id, text, due_at, kind FROM reminders
               WHERE due_at <= ? ORDER BY due_at, id LIMIT 1""",
            (time.time() if now is None else now,),
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
