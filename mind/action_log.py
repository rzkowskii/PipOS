"""What Pip did, as counts and kinds only, for "what did you do last night?".

Each line of /mind/log/actions.jsonl is exactly {"ts", "kind", "count"}: a
whole number of seconds, one kind from a fixed list, and a whole number. No
words, names, list items or anything said can be stored, because nothing
else is accepted. The file is rotated at 1 MB (one older generation is
kept), it goes into backups, and "forget everything" removes both files.

Writers call `record(state_dir, kind, count)`. It never raises: an unknown
kind or a full disk is a skipped line, never a failed turn.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = "log"
LOG_NAME = "actions.jsonl"
OLD_NAME = "actions.jsonl.1"
ROTATE_BYTES = 1024 * 1024
MAX_COUNT = 1_000_000
# Readers never take in more than this, whatever is on disk.
READ_LIMIT = 2 * ROTATE_BYTES

# Every kind, with how it is told: (singular, plural with {n}). A kind is
# told only through these templates, so a log line can never carry words.
KINDS = {
    # The night shift and self-care.
    "night.tidy": ("tidied my memory", "tidied my memory"),
    "backup": ("backed myself up", "backed myself up {n} times"),
    "backup.auto": ("backed myself up", "backed myself up {n} times"),
    "backup.failed": ("tried to back myself up but couldn't", "tried to back myself up {n} times but couldn't"),
    "dream": ("dreamed", "dreamed"),
    "repair.organ": ("restarted a stuck part of me", "restarted stuck parts of me {n} times"),
    "repair.audio": ("reset my microphone", "reset my microphone {n} times"),
    "model.recheck": ("checked my model files", "checked my model files"),
    "update.install": ("installed an update", "installed {n} updates"),
    # Things done for the owner.
    "forget": ("forgot something you asked me to", "forgot {n} things you asked me to"),
    "forget.all": ("forgot everything, as you asked", "forgot everything, as you asked"),
    "memory.remember": ("remembered something new", "remembered {n} new things"),
    "timer.set": ("set a timer", "set {n} timers"),
    "reminder.set": ("set a reminder", "set {n} reminders"),
    "alarm.set": ("set an alarm", "set {n} alarms"),
    "reminder.rang": ("rang a reminder", "rang {n} reminders"),
    "list.add": ("added something to your lists", "added {n} things to your lists"),
    "list.remove": ("took something off your lists", "took {n} things off your lists"),
    "list.clear": ("cleared a list", "cleared {n} lists"),
    "list.delete": ("deleted a list", "deleted {n} lists"),
    "note.add": ("made a note", "made {n} notes"),
    "note.delete": ("deleted a note", "deleted {n} notes"),
    "note.clear": ("cleared your notes", "cleared your notes {n} times"),
    "guest.start": ("had a guest", "had guests {n} times"),
    "guest.end": None,
    "workshop.open": ("opened my workshop", "opened my workshop {n} times"),
    "workshop.run": ("ran a program", "ran {n} programs"),
    "brightness": None,
    "mute": ("stopped listening when you asked", "stopped listening {n} times when you asked"),
    "shutdown": ("switched off", "switched off {n} times"),
    "reboot": ("restarted", "restarted {n} times"),
}
# Told first when there is more than fits in one breath.
PRIORITY = (
    "forget.all", "update.install", "backup.failed", "repair.organ", "repair.audio", "backup.auto", "backup",
    "night.tidy", "dream", "forget", "reminder.rang", "alarm.set", "timer.set", "reminder.set", "list.add",
    "note.add", "memory.remember", "workshop.run", "workshop.open", "guest.start", "list.remove", "list.clear",
    "list.delete", "note.delete", "note.clear", "mute", "reboot", "shutdown", "model.recheck",
)
MOST_TOLD = 3
NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def log_path(state_dir):
    return Path(state_dir) / LOG_DIR / LOG_NAME


def _valid(kind, count):
    return (isinstance(kind, str) and kind in KINDS and type(count) is int and 0 <= count <= MAX_COUNT)


def keeps_nothing(state_dir):
    """True while the memory mode is session-only ("nothing after a
    restart"), or the policy cannot be read: then no line is written, by any
    writer, because this file outlives the restart. Never raises."""
    try:
        from .retention import load_policy

        return load_policy(state_dir).mode == "session-only"
    except Exception:
        return True


def record(state_dir, kind, count=1, *, now=None):
    """Append one line; True if it was written. Never raises.

    Nothing is written in session-only mode: the turn, the voice backup, the
    night shift and the doctor's repairs all come through here.
    """
    if not _valid(kind, count):
        return False
    if keeps_nothing(state_dir):
        return False
    try:
        stamp = int(time.time() if now is None else now)
        line = json.dumps({"ts": stamp, "kind": kind, "count": count}, separators=(",", ":")) + "\n"
        directory = Path(state_dir) / LOG_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / LOG_NAME
        try:
            if path.lstat().st_size + len(line) > ROTATE_BYTES:
                os.replace(path, directory / OLD_NAME)
        except FileNotFoundError:
            pass
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            # One write below PIPE_BUF: lines never interleave.
            os.write(descriptor, line.encode("ascii"))
        finally:
            os.close(descriptor)
        return True
    except (OSError, ValueError, OverflowError):
        return False


def _lines(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    with os.fdopen(descriptor, "rb") as source:
        data = source.read(READ_LIMIT)
    return data.decode("ascii", "replace").splitlines()


def entries(state_dir, *, since=None, until=None):
    """Valid entries, oldest first. Anything malformed is skipped."""
    found = []
    directory = Path(state_dir) / LOG_DIR
    for path in (directory / OLD_NAME, directory / LOG_NAME):
        for line in _lines(path):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if (not isinstance(value, dict) or set(value) != {"ts", "kind", "count"} or type(value["ts"]) is not int
                    or not _valid(value["kind"], value["count"])):
                continue
            if since is not None and value["ts"] < since:
                continue
            if until is not None and value["ts"] >= until:
                continue
            found.append(value)
    return found


def last(state_dir, kinds):
    """The newest entry of any of these kinds, or None."""
    kinds = set(kinds)
    newest = None
    for entry in entries(state_dir):
        if entry["kind"] in kinds and (newest is None or entry["ts"] >= newest["ts"]):
            newest = entry
    return newest


def clear(state_dir):
    """Forget everything: both generations go. True if nothing is left."""
    directory = Path(state_dir) / LOG_DIR
    gone = True
    for name in (LOG_NAME, OLD_NAME):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            gone = False
    return gone


def erase_all(state_dir):
    """The erase path's name for `clear`."""
    return clear(state_dir)


# --- telling it --------------------------------------------------------------

def count_word(n):
    return NUMBER_WORDS[n] if 0 <= n < len(NUMBER_WORDS) else str(n)


def totals(found):
    """{kind: count} in the order each kind first happened."""
    counts = {}
    for entry in sorted(found, key=lambda entry: entry["ts"]):
        if KINDS.get(entry["kind"]) is not None and entry["count"] > 0:
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + entry["count"]
    return counts


def phrases(counts, most=MOST_TOLD):
    """The `most` weightiest kinds, told in the order they happened."""
    chosen = set([kind for kind in PRIORITY if kind in counts][:most])
    told = []
    for kind, n in counts.items():
        if kind in chosen:
            one, many = KINDS[kind]
            told.append(one if n == 1 else many.format(n=count_word(n)))
    return told


def join(parts):
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def summary(found, when):
    """One spoken line for the period `when` ("last night", "today", ...)."""
    counts = totals(found)
    told = phrases(counts)
    if not told:
        return f"[neutral] Nothing worth telling {when}: I mostly waited for you."
    rest = len(counts) - len(told)
    line = f"[neutral] {when[0].upper()}{when[1:]} I {join(told)}."
    if rest > 0:
        line += " And a few small things."
    return line


# --- the handler ---------------------------------------------------------------

_LEAD = r"^(?:(?:hey|hi|okay|ok)\s+pip\W*\s*)?(?:pip\W*\s*)?(?:so\s+|and\s+)?"
_PATTERN = re.compile(
    _LEAD + r"(?:what\s+(?:did|have)\s+you\s+(?:been\s+)?(?:do|done|doing|up\s+to)"
    r"(?:\s+(?P<when>last\s+night|overnight|tonight|today|this\s+morning|yesterday|while\s+i\s+was\s+(?:away|out|asleep)"
    r"|lately|recently))?"
    r"|what\s+have\s+you\s+been\s+up\s+to(?:\s+(?P<when2>today|lately|recently|last\s+night))?"
    r"|what\s+did\s+you\s+get\s+up\s+to(?:\s+(?P<when3>last\s+night|today|yesterday))?)"
    r"(?:\s+pip)?\s*[?.!]*$",
    re.IGNORECASE,
)
_HINT = re.compile(r"\bwhat\s+(?:did|have)\s+you\s+(?:been\s+)?(?:do|done|doing|up\s+to|get\s+up)", re.IGNORECASE)


def may_match(text):
    from .body_talk import said

    return bool(_HINT.search(text or "")) and bool(_PATTERN.match(said(text)))


def local_now():
    """The device's local time, for a handler given no `now` (the mind does
    not know the owner's time zone yet). Tests replace this."""
    return datetime.now().astimezone()


def period(when, now):
    """(since, until, words) in unix seconds for a spoken period.

    "Last night" is always the night before this day: 18:00 yesterday to
    09:00 today (or now, if that is earlier), even asked in the evening.
    "Tonight" is this evening from 18:00; asked before 18:00 it can only
    mean the night just gone."""
    when = re.sub(r"\s+", " ", (when or "").lower())
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if when == "tonight" and now.hour >= 18:
        return (midnight + timedelta(hours=18)).timestamp(), now.timestamp(), "tonight"
    if when in ("last night", "overnight", "tonight") or when.startswith("while i was asleep"):
        start = midnight - timedelta(hours=6)  # 18:00 yesterday
        end = min(now, midnight + timedelta(hours=9))
        return start.timestamp(), end.timestamp(), "last night"
    if when == "yesterday":
        return (midnight - timedelta(days=1)).timestamp(), midnight.timestamp(), "yesterday"
    if when == "this morning":
        return midnight.timestamp(), now.timestamp(), "this morning"
    if when == "today":
        return midnight.timestamp(), now.timestamp(), "today"
    # "what have you been up to", "while I was away", "lately": the last day.
    return (now - timedelta(hours=24)).timestamp(), now.timestamp() + 1, "since yesterday"


async def handle(text, ctx):
    from .body_talk import Reply, is_guest, said

    match = _PATTERN.match(said(text, ctx))
    if not match:
        return None
    if is_guest(ctx):
        return Reply("[neutral] I keep that for my owner.")
    now = getattr(ctx, "now", None) or local_now()
    when = match.group("when") or match.group("when2") or match.group("when3")
    since, until, words = period(when, now)
    return Reply(summary(entries(ctx.state_dir, since=int(since), until=int(until) + 1), words))
