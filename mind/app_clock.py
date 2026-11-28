"""The clock: a big clock with the owner's timers and next reminders.

"Show me the clock" or "show my timers" opens it. hands (mind/hands.py) runs
it like the monitor: once a second, just after the second turns, it reads the
reminders database read-only in a worker thread and writes render()'s ANSI
into `being-console --panel clock`. The database belongs to the mind
(mind/reminders.py); nothing here ever writes it, and the clock never rings
anything: the mind stays the only announcer.

Times are shown in the owner's zone (/mind/skills.json, else the image's
factory zone, else the device's own), with a plain note when that zone is
not known or the clock itself is in doubt.
"""

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

COLUMNS, ROWS = 107, 23
TITLE = "Pip's clock"
MAX_TIMERS = 4
MAX_REMINDERS = 3
ZONE_EVERY = 60.0           # seconds between looks at the owner's zone
TEXT = 48                   # characters of a reminder's words shown

RESET, BOLD = "\x1b[0m", "\x1b[1m"
CYAN, YELLOW, GREEN, GREY = "\x1b[36m", "\x1b[33m", "\x1b[32m", "\x1b[90m"

# A 3x5 font for the digits, each pixel drawn 4 cells wide and 2 rows tall,
# so a digit is 12 columns by 10 rows: readable across the room.
FONT = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", ".#.", ".#."),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    ":": (".", "#", ".", "#", "."),
    " ": ("...", "...", "...", "...", "..."),
}
PIXEL_W, PIXEL_H = 4, 2
GAP = 2


def big_text(text):
    """The rows of `text` in the big font (plain cells: █ and spaces)."""
    rows = [""] * (5 * PIXEL_H)
    for index, char in enumerate(text):
        glyph = FONT[char]
        for y, pattern in enumerate(glyph):
            cells = "".join(("█" if pixel == "#" else " ") * PIXEL_W for pixel in pattern)
            if index:
                cells = " " * GAP + cells
            for repeat in range(PIXEL_H):
                rows[y * PIXEL_H + repeat] += cells
    return rows


# -- what the clock reads ------------------------------------------------------------------

def owner_zone(state_dir="/mind"):
    """The owner's zone as the mind keeps it, or None (the device zone)."""
    from .skills import factory_zone, find_zone

    try:
        settings = json.loads((Path(state_dir) / "skills.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    zone = settings.get("time_zone") if isinstance(settings, dict) else None
    if isinstance(zone, str) and zone:
        return find_zone(zone)
    if isinstance(zone, str) and "time_zone" in settings:
        return None  # "use the device time zone", chosen on purpose
    try:
        return factory_zone()
    except Exception:
        return None


def clock_doubted(*, now=None, state_dir=None):
    """True when the wall clock cannot be believed (mind/clock_epoch.py: it
    was reset at boot, or reads before this software existed), else False."""
    try:
        from . import clock_epoch
    except ImportError:
        return False
    check = getattr(clock_epoch, "doubted", None) or getattr(clock_epoch, "epoch_advanced", None)
    given = {}
    if now is not None:
        given["now"] = now
    if state_dir is not None and check is getattr(clock_epoch, "doubted", None):
        given["state_dir"] = state_dir
    try:
        return bool(check(**given)) if check is not None else False
    except Exception:
        return False


def _clean(text):
    text = "".join(ch for ch in str(text) if ch.isprintable())
    text = " ".join(text.split())
    return text if len(text) <= TEXT else text[:TEXT - 1] + "…"


def read_entries(state_dir="/mind", now=None, limit=24):
    """Pending timers and reminders, soonest first, read-only:
    [{"kind", "text", "due_at", "created_at", "duration"}], or None when the
    database cannot be read just now."""
    path = Path(state_dir) / "reminders.db"
    if not path.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    except sqlite3.Error:
        return None
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(reminders)")}
        extra = [name if name in columns else "NULL" for name in ("created_at", "duration")]
        rows = db.execute(
            f"SELECT kind, text, due_at, {extra[0]}, {extra[1]} FROM reminders ORDER BY due_at, id LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    out = []
    for kind, text, due_at, created_at, length in rows:
        if kind not in ("timer", "reminder", "alarm") or type(due_at) not in (int, float):
            continue
        out.append({"kind": kind, "text": _clean(text), "due_at": float(due_at),
                    "created_at": created_at if type(created_at) in (int, float) else None,
                    "duration": length if type(length) is int and length > 0 else None})
    return out


class Reader:
    """What a frame needs, read in a worker thread: the entries every second,
    the owner's zone once a minute."""

    def __init__(self, state_dir="/mind", clock=time.monotonic):
        self.state_dir = state_dir
        self.clock = clock
        self.zone = None
        self.zone_known = False
        self._zone_at = None

    def read(self, now=None):
        if self._zone_at is None or self.clock() - self._zone_at >= ZONE_EVERY:
            self.zone = owner_zone(self.state_dir)
            self.zone_known = self.zone is not None
            self._zone_at = self.clock()
        return {"entries": read_entries(self.state_dir, now), "zone": self.zone, "zone_known": self.zone_known,
                "doubted": clock_doubted(now=now, state_dir=self.state_dir)}


# -- drawing ----------------------------------------------------------------------------------

def countdown(seconds):
    seconds = max(0, int(seconds + 0.999))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def timer_name(entry):
    length = entry.get("duration")
    if length:
        if length % 3600 == 0:
            return f"{length // 3600}-hour timer"
        if length % 60 == 0:
            return f"{length // 60}-minute timer"
        return f"{length}-second timer"
    text = entry.get("text") or ""
    return text if text and text.casefold() != "timer" else "Timer"


def when(due, now):
    """'at 9:00 am', 'tomorrow at 9:00 am', 'on Friday at 6:30 pm', or a date."""
    clock = due.strftime("%I:%M %p").lstrip("0").lower()
    days = (due.date() - now.date()).days
    if days == 0:
        return f"at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if 1 < days < 7:
        return f"on {due.strftime('%A')} at {clock}"
    return f"on {due.day} {due.strftime('%B')} at {clock}"


def _centre(text, width, visible=None):
    length = len(text) if visible is None else visible
    return " " * max(0, (width - length) // 2) + text


def render(now_ts, data=None, *, columns=COLUMNS, rows=ROWS):
    """One whole frame of ANSI for the Unix time `now_ts`. `data` is a
    Reader.read() result."""
    data = data or {}
    zone = data.get("zone")
    now = datetime.fromtimestamp(now_ts, zone) if zone is not None else datetime.fromtimestamp(now_ts)
    lines = []
    # The title is the panel's own (its status bar); the date goes above the time.
    date = f"{now.strftime('%A')} {now.day} {now.strftime('%B %Y')}"
    lines.append(_centre(f"{BOLD}{CYAN}{date}{RESET}", columns, len(date)))
    lines.append("")
    hour = now.hour % 12 or 12
    digits = big_text(f"{hour}:{now.minute:02d}")
    width = len(digits[0])
    suffix = "am" if now.hour < 12 else "pm"
    seconds = f"{now.second:02d}"
    for index, row in enumerate(digits):
        tail = ""
        if index == len(digits) - 1:
            tail = f"   {GREY}{seconds}{RESET}"
        elif index == len(digits) - 3:
            tail = f"   {BOLD}{suffix}{RESET}"
        lines.append(_centre(f"{BOLD}{row}{RESET}{tail}", columns, width))
    note = ""
    if data.get("doubted"):
        note = f"{YELLOW}My clock may be wrong. Check the date and time in Setup.{RESET}"
        visible = 56
    elif not data.get("zone_known", zone is not None):
        note = f"{GREY}My time zone isn't set, so this is my own clock's time.{RESET}"
        visible = 54
    lines.append(_centre(note, columns, visible) if note else "")
    lines.append("")

    entries = data.get("entries")
    if entries is None:
        lines.append(f"  {GREY}I can't read my timers and reminders just now.{RESET}")
    else:
        timers = [entry for entry in entries if entry["kind"] == "timer"][:MAX_TIMERS]
        reminders = [entry for entry in entries if entry["kind"] == "reminder"][:MAX_REMINDERS]
        alarm = next((entry for entry in entries if entry["kind"] == "alarm"), None)
        if alarm is not None:
            # The alarm's row (and the gap above it) always fits: fewer
            # reminders are listed instead.
            room = rows - len(lines) - len(timers) - (1 if timers and reminders else 0) - (2 if timers or reminders else 1)
            reminders = reminders[:max(0, room)]
        if not timers and not reminders and alarm is None:
            lines.append(f"  {GREY}No timers or reminders. Say \"set a timer for ten minutes\".{RESET}")
        for entry in timers:
            left = entry["due_at"] - now_ts
            name = timer_name(entry)
            if left <= 0:
                lines.append(f"  {BOLD}{name:<24}{RESET}{YELLOW}done{RESET}")
                continue
            length = entry.get("duration")
            share = ""
            if length and entry.get("created_at"):
                done = max(0.0, min(1.0, 1 - left / length))
                cells = round(done * 30)
                share = f"  {GREEN}{'█' * cells}{GREY}{'░' * (30 - cells)}{RESET}"
            lines.append(f"  {BOLD}{name:<24}{RESET}{countdown(left):>8} left{share}")
        if timers and reminders:
            lines.append("")
        for index, entry in enumerate(reminders):
            due = datetime.fromtimestamp(entry["due_at"], zone) if zone is not None else datetime.fromtimestamp(entry["due_at"])
            label = "Next reminder" if index == 0 else ""
            late = entry["due_at"] <= now_ts
            moment = "waiting for the clock" if data.get("doubted") else "now" if late else when(due, now)
            lines.append(f"  {BOLD}{label:<16}{RESET}{entry['text']}  {GREY}{moment}{RESET}")
        if alarm is not None:
            # The soonest alarm (a repeating one shows its next time). Its
            # words are only "alarm" unless the owner named it.
            if timers or reminders:
                lines.append("")
            due = datetime.fromtimestamp(alarm["due_at"], zone) if zone is not None else datetime.fromtimestamp(alarm["due_at"])
            if data.get("doubted"):
                moment = "waiting for the clock"
            else:
                moment = "ringing now" if alarm["due_at"] <= now_ts else when(due, now)
            words = alarm["text"] if alarm["text"] and alarm["text"].casefold() not in ("alarm", "an alarm") else ""
            lines.append(f"  {BOLD}{'Next alarm':<16}{RESET}{moment}" + (f"  {GREY}{words}{RESET}" if words else ""))

    lines = lines[:rows]
    lines += [""] * (rows - len(lines))
    return "\x1b[?25l" + "".join(f"\x1b[{number};1H{line}\x1b[0m\x1b[K" for number, line in enumerate(lines, 1))
