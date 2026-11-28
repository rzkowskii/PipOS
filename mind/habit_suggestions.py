"""Opt-in, seven-day notes of pairs of built-in owner commands.

Only allowlisted route IDs and local calendar dates can reach this file.
Conversation text, parameters and exact action times do not. The file is
intentionally outside the backup and model-context allowlists.
"""

from dataclasses import dataclass
from datetime import date, timedelta
import json
import os
from pathlib import Path
import stat

from . import habits
from .storage import atomic_write, sync_directory

FILE = "habit-suggestions.json"
MAX_BYTES = 32 * 1024
WINDOW_DAYS = 7
MIN_DAYS = 4
PAIR_SECONDS = 300
OFFER_SECONDS = 30
TRIGGERS = ("quick routine", "daily routine", "regular routine", "next routine")


@dataclass(frozen=True)
class Suggestion:
    pair: tuple[str, str]
    day: date
    deadline: float
    lid_seq: int | None


def _valid_pair(pair):
    return (isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] != pair[1]
            and all(isinstance(step, str) and step in habits.STEP_IDS for step in pair))


def _day(value):
    if not isinstance(value, str):
        raise ValueError("invalid habit observation date")
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise ValueError("invalid habit observation date")
    return parsed


def _pairs(values):
    limit = len(habits.STEP_IDS) * (len(habits.STEP_IDS) - 1)
    if not isinstance(values, list) or len(values) > limit or any(not _valid_pair(pair) for pair in values):
        raise ValueError("invalid habit observation pairs")
    return {tuple(pair) for pair in values}


class Store:
    def __init__(self, root):
        self.path = Path(root) / FILE

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                meta = os.fstat(fd)
                if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
                        or meta.st_mode & 0o077 or meta.st_nlink != 1):
                    return None
                text = source.read(MAX_BYTES + 1)
            if not 0 < len(text) <= MAX_BYTES:
                return None
            value = json.loads(text)
            if (not isinstance(value, dict) or set(value) != {"version", "seen", "offered", "day"}
                    or type(value["version"]) is not int or value["version"] != 1 or not isinstance(value["seen"], dict)
                    or len(value["seen"]) > WINDOW_DAYS):
                return None
            return ({_day(day): _pairs(pairs) for day, pairs in value["seen"].items()},
                    _pairs(value["offered"]), _day(value["day"]) if value["day"] is not None else None)
        except (OSError, ValueError, TypeError, RecursionError):
            return None

    def _write(self, seen, offered, last_day):
        value = {"version": 1, "seen": {day.isoformat(): sorted(pairs) for day, pairs in seen.items()},
                 "offered": sorted(offered), "day": last_day.isoformat() if last_day is not None else None}
        atomic_write(self.path, json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")

    def enable(self):
        if self.read() is None:
            self._write({}, set(), None)

    @staticmethod
    def _recent(seen, day, keep_days):
        first = day - timedelta(days=min(keep_days, WINDOW_DAYS) - 1)
        return {when: pairs for when, pairs in seen.items() if first <= when <= day}

    @staticmethod
    def _eligible(seen, offered, last_day, day, pair):
        return (pair not in offered and (last_day is None or last_day < day)
                and pair in seen.get(day, ()) and sum(pair in pairs for pairs in seen.values()) >= MIN_DAYS)

    def observe(self, day, pair=None, *, keep_days=WINDOW_DAYS):
        """Prune expired days, and if present record one pair from this day."""
        state = self.read()
        if state is None:
            return False, False
        seen, offered, last_day = state
        recent = self._recent(seen, day, keep_days)
        pair = tuple(pair) if _valid_pair(pair) else None
        new_pair = pair is not None and pair not in recent.get(day, ())
        if new_pair:
            recent[day] = recent.get(day, set()) | {pair}
        if new_pair or recent != seen:
            self._write(recent, offered, last_day)
        return True, pair is not None and self._eligible(recent, offered, last_day, day, pair)

    def claim(self, day, pair, *, keep_days=WINDOW_DAYS):
        """Reserve today's only question before asking, including across restarts."""
        state = self.read()
        if state is None or not _valid_pair(pair):
            return False
        pair = tuple(pair)
        seen, offered, last_day = state
        recent = self._recent(seen, day, keep_days)
        if not self._eligible(recent, offered, last_day, day, pair):
            return False
        self._write(recent, offered | {pair}, day)
        return True


def erase_all(root):
    path = Path(root) / FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_directory(path.parent)
