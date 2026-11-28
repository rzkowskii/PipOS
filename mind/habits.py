"""Explicit phrase habits, stored and erased with the owner's lists."""

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

from . import intents, lists
from .reminder_language import parse_reminder, spoken_rule
from .reminders import next_occurrence, valid_rule
from .reply import Reply, clean
from .try_mode import installed_trial_pending

MAX_HABITS = 20
MAX_STEPS = 6
EVENT_COOLDOWN = 10 * 60.0
EVENT_DAILY_CAP = 6
# Stable IDs stay as strings for old rows. The one typed step below is an
# exact local alarm time whose per-run confirmation is owned by Turn.
STEP_IDS = frozenset(("clock.time", "clock.date", "reminders.today", "reminders.tomorrow",
                      "body.battery", "device.brighter", "device.dimmer",
                      "device.louder", "device.quieter", "app.clock.open", "app.monitor.open"))
PRIVATE_STEP_IDS = frozenset(("reminders.today", "reminders.tomorrow"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS phrase_habits(
    trigger TEXT PRIMARY KEY NOT NULL,
    steps TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    trigger_kind TEXT NOT NULL DEFAULT 'phrase',
    schedule_rule TEXT,
    at_local TEXT,
    event TEXT,
    next_at REAL,
    last_run REAL,
    run_day TEXT,
    run_count INTEGER NOT NULL DEFAULT 0
)
"""
_EVENT_DAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS habit_event_days(
    event TEXT PRIMARY KEY NOT NULL CHECK(event = 'owner.first_words'),
    day TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'delivered'))
)
"""
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_EXTRA_COLUMNS = {
    "trigger_kind": "TEXT NOT NULL DEFAULT 'phrase'", "schedule_rule": "TEXT", "at_local": "TEXT",
    "event": "TEXT", "next_at": "REAL", "last_run": "REAL", "run_day": "TEXT",
    "run_count": "INTEGER NOT NULL DEFAULT 0",
}
_HABIT_FIELDS = ("trigger, steps, enabled, trigger_kind, schedule_rule, at_local, event, "
                 "next_at, last_run, run_day, run_count")
_EVENTS = {
    "when you're unplugged": "power.unplugged", "when you are unplugged": "power.unplugged",
    "when you're plugged in": "power.plugged", "when you are plugged in": "power.plugged",
    "when it gets dark": "light.dark", "when it gets bright": "light.bright",
    "when i stop my alarm": "alarm.stopped", "when an alarm is stopped": "alarm.stopped",
    "when my backup drive is connected": "media.backup_arrived",
    "when my backup drive is plugged in": "media.backup_arrived",
    "when i first talk to you today": "owner.first_words",
    "when i first talk to you for the day": "owner.first_words",
    "when someone appears": "presence.appeared",
    "when a person appears": "presence.appeared",
}
_EXACT = {
    "new habit": "new",
    "save habit": "save",
    "cancel habit setup": "cancel",
    "what habits do you have": "list",
    "turn on habit suggestions": "notice_on",
    "turn off habit suggestions": "notice_off",
    "forget my habit suggestions": "notice_off",
    "are habit suggestions on": "notice_status",
}
_EDIT = re.compile(r"(?:habit trigger (?P<trigger>.+)|(?:add )?habit step (?P<step>.+)"
                   r"|(?P<kind>run|stop|forget) (?:the|my) (?P<name>.+) habit)\Z")
_ONE_LINE = re.compile(r"new habit (?P<body>.+)\Z")
_STEP_BREAK = re.compile(r"\s*(?:,|\bthen\b|\band\b)\s*")
_ALARM_WORDS = re.compile(r"(?:set|create|make)(?: me)?(?: an?| my| the)? alarm\b", re.I)
_EXPLICIT_MERIDIEM = re.compile(r"\b[ap]\s*\.?\s*m\.?\Z", re.I)
_AMBIGUOUS_CLOCK = re.compile(
    r"(?:\b(?:1[0-2]|[1-9])(?::[0-5][0-9])?|"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve))\Z",
    re.I,
)
_DAY_WORDS = re.compile(
    r"\b(?:today|tomorrow|tonight|daily|morning|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)


@dataclass(frozen=True)
class AlarmStep:
    """A phrase-only recipe step: one local wall-clock alarm after a fresh yes."""
    at_local: str
    ambiguous_12h: bool = False

    @property
    def available(self):
        return (re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.at_local) is not None
                and (not self.ambiguous_12h or int(self.at_local[:2]) <= 11))


@dataclass(frozen=True)
class SleepStep:
    """A final phrase-only step that always asks before the existing sleep path."""


def alarm_phrase(step):
    hour, minute = (int(part) for part in step.at_local.split(":"))
    if step.ambiguous_12h:
        spoken_hour = hour or 12
        clock = str(spoken_hour) if minute == 0 else f"{spoken_hour}:{minute:02d}"
        return f"set an alarm for {clock}, whichever matching time comes next"
    suffix = "a.m." if hour < 12 else "p.m."
    spoken_hour = hour % 12 or 12
    clock = str(spoken_hour) if minute == 0 else f"{spoken_hour}:{minute:02d}"
    return f"set an alarm for {clock} {suffix}"


def step_phrase(step):
    if isinstance(step, AlarmStep) and step.available:
        return alarm_phrase(step)
    if isinstance(step, str):
        intent = intents.BY_ID.get(step)
        if intent is not None and step in STEP_IDS:
            return intent.canonical
    if isinstance(step, SleepStep):
        return "go to sleep"
    return "unavailable step"


def private_steps(steps):
    return any(isinstance(step, (AlarmStep, SleepStep)) or step in PRIVATE_STEP_IDS
               for step in steps if isinstance(step, (str, AlarmStep, SleepStep)))


def _step_available(step):
    return ((isinstance(step, str) and step in STEP_IDS and step in intents.BY_ID)
            or (isinstance(step, AlarmStep) and step.available)
            or isinstance(step, SleepStep))


def _encoded_steps(steps):
    return [step if isinstance(step, str) else
            ({"kind": "alarm", "at_local": step.at_local, "clock": "12h-next"}
             if isinstance(step, AlarmStep) and step.ambiguous_12h else
             {"kind": "alarm", "at_local": step.at_local} if isinstance(step, AlarmStep)
             else {"kind": "sleep"}) for step in steps]


def _decoded_step(value):
    if isinstance(value, str):
        return value
    if (isinstance(value, dict) and set(value) == {"kind", "at_local"}
            and value.get("kind") == "alarm" and isinstance(value.get("at_local"), str)):
        step = AlarmStep(value["at_local"])
        return step if step.available else None
    if (isinstance(value, dict) and set(value) == {"kind", "at_local", "clock"}
            and value.get("kind") == "alarm" and value.get("clock") == "12h-next"
            and isinstance(value.get("at_local"), str)):
        step = AlarmStep(value["at_local"], ambiguous_12h=True)
        return step if step.available and int(step.at_local[:2]) <= 11 else None
    if isinstance(value, dict) and value == {"kind": "sleep"}:
        return SleepStep()
    return None


@dataclass(frozen=True)
class Habit:
    trigger: str
    steps: tuple[str | AlarmStep | SleepStep, ...]
    enabled: bool = True
    trigger_kind: str = "phrase"
    schedule_rule: str | None = None
    at_local: str | None = None
    event: str | None = None
    next_at: float | None = None
    last_run: float | None = None
    run_day: str | None = None
    run_count: int = 0

    @property
    def available(self):
        trigger_ok = (self.trigger_kind == "phrase" or
                      self.trigger_kind == "time" and valid_rule(self.schedule_rule) and self.at_local or
                      self.trigger_kind == "event" and self.event in set(_EVENTS.values()))
        return (bool(trigger_ok) and 0 < len(self.steps) <= MAX_STEPS
                and (self.trigger_kind == "phrase" or not private_steps(self.steps))
                and all(not isinstance(step, SleepStep) or index == len(self.steps) - 1
                        for index, step in enumerate(self.steps))
                and all(_step_available(step) for step in self.steps))


@dataclass(frozen=True)
class Draft:
    trigger: str = ""
    steps: tuple[str | AlarmStep | SleepStep, ...] = ()
    trigger_kind: str = "phrase"
    schedule_rule: str | None = None
    at_local: str | None = None
    event: str | None = None


@dataclass(frozen=True)
class OneLine:
    trigger: str
    steps: tuple[str | AlarmStep | SleepStep, ...]


@dataclass(frozen=True)
class Run(Reply):
    habit: Habit | None = None


@dataclass(frozen=True)
class MediaArrival:
    event: str
    enrollment: str
    device_number: str
    device_sequence: int


@dataclass(frozen=True)
class DailyEvent:
    event: str
    day: str


@dataclass(frozen=True)
class PresenceArrival:
    event: str
    camera_seq: int
    lid_seq: int
    deadline: float


def _authenticated_usb_state(state_dir):
    """Return (present USB edges, enrolled UUID plus edge), or ``None``.

    The UUID binding is the existing owner-controlled authentication boundary.
    Kernel device number plus disk sequence provides the fresh-arrival edge.
    This opens no block device and mounts nothing. The current UUID remains
    only in the private RAM receipt. Any incomplete or changing view fails closed.
    """
    from . import backup, install_disks

    enrollment = backup.media_binding(state_dir)
    candidate = backup.BY_UUID / enrollment if enrollment is not None else None
    device = (str(candidate) if candidate is not None
              and backup.media_identity(candidate) == enrollment else None)
    try:
        blocks, failed = install_disks.block_devices()
    except (OSError, ValueError):
        return None
    if failed:
        return None
    present = {}
    for disk in blocks.values():
        if disk.parent is not None or install_disks.disk_transport(disk) != "usb":
            continue
        sequence = install_disks.optional_number(disk.path / "diskseq")
        if sequence is None:
            return None
        present[disk.path] = (disk.number, sequence)
    enrolled = None
    if device is not None:
        try:
            metadata = os.stat(device, follow_symlinks=True)
        except (OSError, ValueError):
            return None
        number = f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"
        block = blocks.get(number)
        disk_path = block.parent if block is not None else None
        if block is not None and block.parent is None:
            disk_path = block.path
        edge = present.get(disk_path)
        enrolled = (enrollment, *edge) if enrollment is not None and edge is not None else None
    # Do not combine topology resolved under one enrollment with another one
    # written concurrently by Care.
    if backup.media_binding(state_dir) != enrollment:
        return None
    return frozenset(present.values()), enrolled


class MediaEvents:
    """Recognise only a newly arrived, already-enrolled backup drive.

    A drive already present when Mind starts is baseline state rather than an
    arrival.  No device identity or label is retained or placed on the bus.
    """

    def __init__(self, state_dir, *, state=_authenticated_usb_state):
        self.state_dir = state_dir
        self.state = state
        initial = state(state_dir)
        self._ready = initial is not None
        self._present = initial[0] if initial is not None else frozenset()

    def observe(self, attached):
        state = self.state(self.state_dir)
        if state is None:
            self._ready = False
            return None
        current, enrolled = state
        arrived = (self._ready and attached is True and enrolled is not None
                   and enrolled[1:] not in self._present)
        self._ready = True
        self._present = current
        return MediaArrival("media.backup_arrived", *enrolled) if arrived else None

    def valid(self, arrival):
        """Recheck both the exact kernel edge and today's Care enrollment."""
        if not isinstance(arrival, MediaArrival) or arrival.event != "media.backup_arrived":
            return False
        state = self.state(self.state_dir)
        if state is None:
            self._ready = False
            return False
        current, enrolled = state
        edge = (arrival.device_number, arrival.device_sequence)
        return enrolled == (arrival.enrollment, *edge) and edge in current


def control(text, name, now=None):
    said = clean(text, name).casefold()
    kind = _EXACT.get(said)
    if kind:
        return kind, ""
    if match := _ONE_LINE.fullmatch(said):
        composed = one_line(match["body"], name, now)
        return ("compose", composed) if composed is not None else ("compose_invalid", "")
    match = _EDIT.fullmatch(said)
    if match is None:
        return None
    if match["kind"]:
        return match["kind"], match["name"]
    return ("trigger", match["trigger"]) if match["trigger"] else ("step", match["step"])


def step_id(text, name):
    intent = intents.BY_PHRASE.get(intents.phrase(text, name))
    return intent.id if intent is not None and intent.id in STEP_IDS and intent.risk == intents.TELL else None


def recipe_step(text, name, now=None):
    """One allowlisted string step or a bounded, non-relative local alarm."""
    if (stable := step_id(text, name)) is not None:
        return stable
    value = intents.phrase(text, name)
    if value == "go to sleep":
        return SleepStep()
    if not _ALARM_WORDS.match(value) or _DAY_WORDS.search(value):
        return None
    now = datetime.now().astimezone() if now is None else now
    request = parse_reminder(value, now=now)
    if (request is None or request.kind != "alarm" or request.relative or request.rule is not None
            or request.due_at is None):
        return None
    due = datetime.fromtimestamp(request.due_at, now.tzinfo)
    explicit = _EXPLICIT_MERIDIEM.search(value) is not None
    explicit_24h = re.search(r"\b(?:0[0-9]|1[3-9]|2[0-3])(?::[0-5][0-9])?\b", value) is not None
    if explicit or explicit_24h:
        return AlarmStep(f"{due.hour:02d}:{due.minute:02d}")
    if _AMBIGUOUS_CLOCK.search(value) is None:
        return None
    return AlarmStep(f"{due.hour % 12:02d}:{due.minute:02d}", ambiguous_12h=True)


def _step_sequence(text, name, now=None):
    """The unique allowlisted grouping around comma/then/and boundaries."""
    atoms = [part.strip(" ,") for part in _STEP_BREAK.split(text) if part.strip(" ,")]
    if not atoms or len(atoms) > 2 * MAX_STEPS:
        return None
    found = set()

    def group(start, chosen):
        if len(chosen) >= MAX_STEPS:
            return
        for end in range(start + 1, len(atoms) + 1):
            step = recipe_step(" and ".join(atoms[start:end]), name, now)
            if step is None:
                # Joining with spaces also reconstructs comma/then-separated
                # phrases without pretending those words are part of a command.
                step = recipe_step(" ".join(atoms[start:end]), name, now)
            if step is None:
                continue
            sequence = (*chosen, step)
            if end == len(atoms):
                found.add(sequence)
            else:
                group(end, sequence)

    group(0, ())
    return next(iter(found)) if len(found) == 1 else None


def one_line(body, name, now=None):
    """Parse `trigger: steps` or ASR-safe `trigger do steps`, unambiguously."""
    candidates = []
    if ":" in body:
        left, right = body.split(":", 1)
        candidates.append((left, right))
    # `do` is the exact spoken delimiter. Try every occurrence and accept only
    # one complete allowlisted interpretation, so a `do` inside a phrase is safe.
    words = list(re.finditer(r"\s+do\s+", body))
    candidates.extend((body[:match.start()], body[match.end():]) for match in words)
    parsed = set()
    for raw_trigger, raw_steps in candidates:
        raw_trigger = raw_trigger.strip(" ,:")
        if not raw_trigger.startswith(("when ", "every ")):
            continue
        steps = _step_sequence(raw_steps, name, now)
        if raw_trigger and steps:
            parsed.add((raw_trigger, steps))
    if len(parsed) != 1:
        return None
    raw_trigger, steps = next(iter(parsed))
    return OneLine(raw_trigger, steps)


def trigger(text, name, now):
    """Parse an explicitly authored trigger; ordinary phrases remain unchanged."""
    value = intents.phrase(text, name)
    spoken = re.fullmatch(r"when i say (?P<phrase>.+)", value)
    if spoken:
        return spoken["phrase"].strip(" ,:'\""), "phrase", None, None, None
    event = _EVENTS.get(value)
    if event:
        return value, "event", None, None, event
    request = parse_reminder(f"{value}, remind me to run habit", now=now)
    if request is not None and request.kind == "create" and request.rule and request.at_local:
        label = f"{spoken_rule(request.rule)} at {request.at_local}"
        return label, "time", request.rule, request.at_local, None
    return value, "phrase", None, None, None


class Store:
    def __init__(self, state_dir):
        self.state_dir = state_dir

    def _rows(self, trigger=None):
        path = lists.database(self.state_dir)
        if not path.is_file():
            return []
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)) as db:
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='phrase_habits' AND type='table'").fetchone():
                return []
            columns = {row[1] for row in db.execute("PRAGMA table_info(phrase_habits)")}
            extra = all(name in columns for name in _EXTRA_COLUMNS)
            selected = _HABIT_FIELDS if extra else "trigger, steps, enabled"
            if trigger is None:
                return db.execute(f"SELECT {selected} FROM phrase_habits ORDER BY trigger LIMIT ?",
                                  (MAX_HABITS,)).fetchall()
            return db.execute(f"SELECT {selected} FROM phrase_habits WHERE trigger=?", (trigger,)).fetchall()

    @staticmethod
    def _habit(row):
        name, encoded, enabled = row[:3]
        try:
            steps = json.loads(encoded)
        except (TypeError, ValueError):
            steps = []
        decoded = tuple(_decoded_step(step) for step in steps) if isinstance(steps, list) else ()
        decoded = decoded if decoded and all(step is not None for step in decoded) else ()
        return Habit(name, decoded, bool(enabled), *row[3:])

    @staticmethod
    def _migrate(db):
        db.execute(_SCHEMA)
        columns = {row[1] for row in db.execute("PRAGMA table_info(phrase_habits)")}
        for name, declaration in _EXTRA_COLUMNS.items():
            if name not in columns:
                db.execute(f"ALTER TABLE phrase_habits ADD COLUMN {name} {declaration}")

    def habits(self, trigger=None):
        return [self._habit(row) for row in self._rows(trigger)]

    def get(self, trigger):
        found = self.habits(trigger)
        return found[0] if found else None

    def claim_event_day(self, event, day):
        """Atomically claim one fixed, text-free local-day event edge."""
        if (event != "owner.first_words" or not isinstance(day, str) or not _DAY.fullmatch(day)
                or installed_trial_pending(self.state_dir)):
            return None
        with closing(lists._connect(self.state_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(_EVENT_DAY_SCHEMA)
            previous = db.execute("SELECT day, state FROM habit_event_days WHERE event=?", (event,)).fetchone()
            if previous is None or previous[0] != day:
                db.execute("INSERT OR REPLACE INTO habit_event_days(event, day, state) VALUES (?, ?, 'pending')",
                           (event, day))
            else:
                # A pending edge is recovered by pending_event_day(), not
                # re-issued for every later utterance in the same process.
                return None
        return DailyEvent(event, day)

    def pending_event_day(self, event, day):
        if (event != "owner.first_words" or not isinstance(day, str) or not _DAY.fullmatch(day)
                or installed_trial_pending(self.state_dir)):
            return None
        path = lists.database(self.state_dir)
        if not path.is_file():
            return None
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)) as db:
            if not db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='habit_event_days' AND type='table'").fetchone():
                return None
            row = db.execute("SELECT state FROM habit_event_days WHERE event=? AND day=?", (event, day)).fetchone()
        return DailyEvent(event, day) if row == ("pending",) else None

    def consume_event_day(self, receipt):
        """Persist delivery before any habit step can execute."""
        if (not isinstance(receipt, DailyEvent) or receipt.event != "owner.first_words"
                or not _DAY.fullmatch(receipt.day) or installed_trial_pending(self.state_dir)):
            return False
        path = lists.database(self.state_dir)
        if not path.is_file():
            return False
        with closing(lists._connect(self.state_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""UPDATE habit_event_days SET state='delivered'
                                WHERE event=? AND day=? AND state='pending'""",
                             (receipt.event, receipt.day))
        return bool(row.rowcount)

    @staticmethod
    def _valid(habit):
        if not isinstance(habit.trigger, str) or not habit.available:
            return False
        if habit.trigger_kind == "phrase":
            return intents.valid_alias(habit.trigger)
        return 1 < len(habit.trigger) <= 75

    @classmethod
    def _current(cls, db, trigger):
        row = db.execute(f"SELECT {_HABIT_FIELDS} FROM phrase_habits WHERE trigger=?", (trigger,)).fetchone()
        return cls._habit(row) if row else None

    def save(self, habit):
        if not self._valid(habit):
            return "invalid"
        if installed_trial_pending(self.state_dir):
            return "trial"
        with closing(lists._connect(self.state_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._migrate(db)
            if db.execute("SELECT COUNT(*) FROM phrase_habits").fetchone()[0] >= MAX_HABITS:
                return "full"
            next_at = habit.next_at
            if habit.trigger_kind == "time" and next_at is None:
                next_at = next_occurrence(habit.schedule_rule, habit.at_local, None, time.time())
            if not db.execute("""INSERT OR IGNORE INTO phrase_habits
                    (trigger, steps, trigger_kind, schedule_rule, at_local, event, next_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (habit.trigger, json.dumps(_encoded_steps(habit.steps)), habit.trigger_kind, habit.schedule_rule,
                     habit.at_local, habit.event, next_at)).rowcount:
                return "exists"
        return "saved"

    def replace(self, previous, revised, *, zone=None):
        """Apply a confirmed local edit only if the inspected row is current."""
        path = lists.database(self.state_dir)
        if not self._valid(revised) or installed_trial_pending(self.state_dir) or not path.is_file():
            return False
        with closing(lists._connect(self.state_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='phrase_habits' AND type='table'").fetchone():
                return False
            self._migrate(db)
            if self._current(db, previous.trigger) != previous:
                return False
            next_at = None
            if revised.trigger_kind == "time":
                same_time = (previous.trigger_kind, previous.schedule_rule, previous.at_local) == (
                    revised.trigger_kind, revised.schedule_rule, revised.at_local)
                if same_time and (previous.enabled or not revised.enabled):
                    next_at = previous.next_at
                else:
                    next_at = next_occurrence(revised.schedule_rule, revised.at_local, zone, time.time())
                    if next_at is None:
                        return False
            updated = db.execute("""UPDATE OR IGNORE phrase_habits
                SET trigger=?, steps=?, enabled=?, trigger_kind=?, schedule_rule=?, at_local=?, event=?, next_at=?
                WHERE trigger=?""",
                (revised.trigger, json.dumps(_encoded_steps(revised.steps)), int(revised.enabled), revised.trigger_kind,
                 revised.schedule_rule, revised.at_local, revised.event, next_at, previous.trigger)).rowcount
        return bool(updated)

    def due_time(self, now, zone, *, clock_trusted):
        if not clock_trusted:
            return None
        return next((habit for habit in self.habits() if habit.enabled and habit.available
                     and habit.trigger_kind == "time" and habit.next_at is not None and habit.next_at <= now), None)

    def due_event(self, event, now, day):
        # Restored habits remain inert until the installed trial is accepted,
        # just like newly authored habits.
        if installed_trial_pending(self.state_dir):
            return None
        return next((habit for habit in self.habits() if habit.enabled and habit.available
                     and habit.trigger_kind == "event" and habit.event == event
                     and (habit.last_run is None or now - habit.last_run >= EVENT_COOLDOWN)
                     and (habit.run_count if habit.run_day == day else 0) < EVENT_DAILY_CAP), None)

    def finish_run(self, habit, now, zone, *, skipped=False):
        if installed_trial_pending(self.state_dir):
            return False
        day = datetime.fromtimestamp(now, zone).date().isoformat()
        count = habit.run_count + 1 if habit.run_day == day else 1
        next_at = (next_occurrence(habit.schedule_rule, habit.at_local, zone, now)
                   if habit.trigger_kind == "time" else habit.next_at)
        with closing(lists._connect(self.state_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._migrate(db)
            if self._current(db, habit.trigger) != habit:
                return False
            row = db.execute("""UPDATE phrase_habits SET next_at=?, last_run=?, run_day=?, run_count=?
                                WHERE trigger=? AND enabled=1""",
                             (next_at, None if skipped else now, day, count, habit.trigger)).rowcount
        return bool(row)

    def remove(self, habit, *, forget):
        # The trial's mutation path must not create or migrate a shared store.
        if installed_trial_pending(self.state_dir):
            return False
        path = lists.database(self.state_dir)
        if not path.is_file():
            return False
        with closing(lists._connect(self.state_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='phrase_habits' AND type='table'").fetchone():
                return False
            self._migrate(db)
            if self._current(db, habit.trigger) != habit:
                return False
            if forget:
                return bool(db.execute("DELETE FROM phrase_habits WHERE trigger=?", (habit.trigger,)).rowcount)
            return bool(db.execute("UPDATE phrase_habits SET enabled=0 WHERE trigger=? AND enabled=1",
                                   (habit.trigger,)).rowcount)
