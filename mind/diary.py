"""The day record: what a day held, kept as counts and a few topic words.

Each night the night shift writes one record for each finished day: how many
conversations there were, the three things the owner talked about most,
how many reminders rang, how many timers were set, and what Pip did for its
own body (restarts, repairs, backups) as counts. It never keeps a sentence.

A topic is a single word from the owner's own speech, linked to the
sentences it came from. Forgetting any of those sentences takes the topic
with it (memory's triggers), so "what did we talk about on Monday?" can never
bring back something forgotten.

`may_match` and `handle` answer "what did we talk about yesterday?" as an
everyday skill; `answer` is the same answer for the `diary` command kind.
"""

import json
import re
import sqlite3
import time
from collections import Counter
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

from .memory import _HISTORY, _STOP, search_terms

TOPICS = 3
GAP_SECONDS = 10 * 60  # a pause this long starts a new conversation
MIN_MENTIONS = 2  # a topic must come up in at least two sentences
BODY_KINDS = ("repair.organ", "repair.audio", "backup", "backup.auto", "backup.failed",
              "update.install", "shutdown", "reboot")
# Words that come up in any conversation and say nothing about its subject.
_DULL = frozenset(
    """going get got getting go goes went make made making take took think thought want wanted need needed
    really very much many lot lots bit little good great nice fine bad sure maybe probably today tomorrow
    yesterday now later soon time times day days week morning afternoon evening night tonight thing things
    stuff way back right left one two three first next last new old pip please thanks thank sorry okay
    look looking see saw seen come came coming put let lets kind sort said says tell told talk talking
    called call name doing done does work working start started keep kept feel feeling felt guess mean""".split()
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ASK = re.compile(
    r"^(?:hey pip[, ]+)?(?:pip[, ]+)?what (?:did|have) (?:we|you and i|i) (?:talk(?:ed)? about|do|done|say|said)"
    r"(?: (?P<when>today|yesterday|last night|on (?P<day>" + "|".join(_WEEKDAYS) + r")))?[?.! ]*$",
    re.IGNORECASE,
)


def day_bounds(day, zone=None):
    start = datetime.combine(day, clock(0), tzinfo=zone)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _topics(rows, limit=TOPICS):
    """The owner's most-mentioned words of substance, with the sentences
    they came from. Each counts once per sentence."""
    mentions, surfaces, sources = Counter(), {}, {}
    for row_id, text in rows:
        seen = set()
        for word in re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text):
            terms = search_terms(word)
            if not terms:
                continue
            key = terms[0]
            if key in _STOP or key in _DULL or len(key) < 3 or key.isdigit():
                continue
            if len(key) > 3 and key.endswith("s") and not key.endswith("ss"):
                key = key[:-1]
            surfaces.setdefault(key, Counter())[word] += 1
            if key not in seen:
                seen.add(key)
                mentions[key] += 1
                sources.setdefault(key, []).append(row_id)
    ranked = sorted((key for key, count in mentions.items() if count >= MIN_MENTIONS),
                    key=lambda key: (-mentions[key], key))
    return [(surfaces[key].most_common(1)[0][0], sources[key]) for key in ranked[:limit]]


def _conversations(stamps):
    count, last = 0, None
    for ts in stamps:
        if last is None or ts - last > GAP_SECONDS:
            count += 1
        last = ts
    return count


def _actions(state_dir, start, end):
    if state_dir is None:
        return Counter()
    try:
        from . import action_log
    except ImportError:
        return Counter()
    try:
        found = action_log.entries(state_dir, since=int(start), until=int(end))
    except (OSError, ValueError, TypeError):
        return Counter()
    counts = Counter()
    for entry in found:
        counts[entry["kind"]] += entry["count"]
    return counts


def collect(db, owner, day, *, zone=None, state_dir=None):
    """The record of `day`, computed from the archive and the action log now."""
    start, end = day_bounds(day, zone)
    rows = db.execute(
        f"SELECT e.id, e.ts, e.text FROM episodes e WHERE {_HISTORY} AND e.kind = 'heard' AND e.speaker = ? "
        "AND NOT coalesce(json_extract(e.meta, '$.command'), 0) AND e.ts >= ? AND e.ts < ? ORDER BY e.ts, e.id",
        (owner, start, end)).fetchall()
    actions = _actions(state_dir, start, end)
    return {
        "day": day.isoformat(),
        "conversations": _conversations([ts for _, ts, _ in rows]),
        "owner_lines": len(rows),
        "reminders_done": actions.get("reminder.rang", 0),
        "timers": actions.get("timer.set", 0),
        "body_events": {kind: actions[kind] for kind in BODY_KINDS if actions.get(kind)},
        "topics": _topics([(row_id, text) for row_id, _, text in rows]),
    }


def build_day(memory, owner, day, *, zone=None, state_dir=None):
    """Write (or rewrite) the stored record of a finished day."""
    record = collect(memory.db, owner, day, zone=zone, state_dir=state_dir)
    with memory.db:
        memory.db.execute("DELETE FROM diary_days WHERE owner = ? AND day = ?", (owner, record["day"]))
        memory.db.execute(
            "INSERT INTO diary_days (owner, day, conversations, owner_lines, reminders_done, timers, body_events, "
            "built) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner, record["day"], record["conversations"], record["owner_lines"], record["reminders_done"],
             record["timers"], json.dumps(record["body_events"], sort_keys=True), time.time()))
        for rank, (topic, sources) in enumerate(record["topics"], 1):
            topic_id = memory.db.execute(
                "INSERT INTO diary_topics (owner, day, rank, topic) VALUES (?, ?, ?, ?)",
                (owner, record["day"], rank, topic)).lastrowid
            memory.db.executemany("INSERT OR IGNORE INTO diary_topic_sources VALUES (?, ?)",
                                  ((topic_id, source) for source in sources))
    return record


def stored(db, owner, day):
    """The stored record of `day`, or None. Topics whose sentences were
    forgotten are already gone."""
    try:
        row = db.execute(
            "SELECT conversations, owner_lines, reminders_done, timers, body_events FROM diary_days "
            "WHERE owner = ? AND day = ?", (owner, day.isoformat())).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    topics = [topic for topic, in db.execute(
        "SELECT topic FROM diary_topics WHERE owner = ? AND day = ? ORDER BY rank", (owner, day.isoformat()))]
    try:
        body = json.loads(row[4])
    except ValueError:
        body = {}
    return {"day": day.isoformat(), "conversations": row[0], "owner_lines": row[1], "reminders_done": row[2],
            "timers": row[3], "body_events": body if isinstance(body, dict) else {},
            "topics": [(topic, ()) for topic in topics]}


def record_for(db, owner, day, *, zone=None, state_dir=None, today=None):
    """Today is computed live; a past day comes from its stored record, or
    is computed if the night shift has not written it yet."""
    if today is not None and day >= today:
        return collect(db, owner, day, zone=zone, state_dir=state_dir)
    return stored(db, owner, day) or collect(db, owner, day, zone=zone, state_dir=state_dir)


def _count(n, one, many):
    words = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    if n == 1:
        return one
    return f"{words[n] if n < len(words) else n} {many}"


def _join(items):
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _first_up(text):
    return text[:1].upper() + text[1:]


def spoken(record, when):
    """At most 25 words, from counts and topic words only."""
    if not record["owner_lines"] and not record["reminders_done"] and not record["timers"]:
        return f"[neutral] We didn't talk {when}, as far as I remember."
    parts = []
    if record["conversations"]:
        times = "once" if record["conversations"] == 1 else (
            "twice" if record["conversations"] == 2 else _count(record["conversations"], "", "times"))
        parts.append(f"{_first_up(when)} we talked {times}.")
    topics = [topic for topic, _ in record["topics"]][:TOPICS]
    if topics:
        parts.append(f"You mentioned {_join(topics)}.")
    did = []
    if record["reminders_done"]:
        did.append("rang " + _count(record["reminders_done"], "a reminder", "reminders"))
    if record["timers"]:
        did.append("set " + _count(record["timers"], "a timer", "timers"))
    if did:
        parts.append(f"I {_join(did)}.")
    line = " ".join(parts) or f"{_first_up(when)} was quiet."
    words = line.split()
    if len(words) > 25:
        line = " ".join(words[:25]).rstrip(",.") + "."
    return f"[neutral] {line}"


def which_day(text, today):
    """(day, how to say it) for a diary question, or None."""
    match = _ASK.match(" ".join(text.strip().split()))
    if match is None:
        return None
    when = (match.group("when") or "today").lower()
    if when == "today":
        return today, "today"
    if when in ("yesterday", "last night"):
        return today - timedelta(days=1), "yesterday"
    target = _WEEKDAYS.index(match.group("day").lower())
    back = (today.weekday() - target) % 7 or 7
    return today - timedelta(days=back), f"on {match.group('day').capitalize()}"


def answer(db, owner, text, *, now=None, zone=None, state_dir=None):
    """The spoken answer to a diary question, or None if it is not one."""
    now = datetime.now(zone) if now is None else now
    found = which_day(text, now.date())
    if found is None:
        return None
    day, when = found
    record = record_for(db, owner, day, zone=zone, state_dir=state_dir, today=now.date())
    return spoken(record, when)


# -- as an everyday skill -------------------------------------------------------

def may_match(text):
    return _ASK.match(" ".join(text.strip().split())) is not None


async def handle(text, ctx):
    if not may_match(text):
        return None
    try:
        from .reply import Reply
    except ImportError:
        return None
    if ctx.guest:
        return Reply("[neutral] I keep our days private to the person I live with.")
    if ctx.now is None:
        return Reply("[concern] I'm not sure what day it is, so I can't look back yet.")
    path = Path(ctx.state_dir) / "mind.db"
    if not path.is_file():
        return Reply(spoken({"owner_lines": 0, "reminders_done": 0, "timers": 0}, "today"))
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        line = answer(db, ctx.owner, text, now=ctx.now, zone=ctx.now.tzinfo, state_dir=ctx.state_dir)
    except sqlite3.Error:
        line = None
    finally:
        db.close()
    return Reply(line) if line else None


# -- as a night job ---------------------------------------------------------------

class DiaryJob:
    """Write the record of every finished day since the last one written,
    one day per chunk, at most a week back."""

    def __init__(self, memory, owner, *, today, zone=None, state_dir=None):
        self.memory, self.owner, self.zone, self.state_dir = memory, owner, zone, state_dir
        self.today = today
        self.cursor = None  # the last day written, ISO

    def resume(self, cursor):
        if isinstance(cursor, str):
            try:
                date.fromisoformat(cursor)
            except ValueError:
                return
            self.cursor = cursor

    async def run_chunk(self, budget_s):
        today = self.today() if callable(self.today) else self.today
        first = today - timedelta(days=7)
        if self.cursor is not None:
            first = max(first, date.fromisoformat(self.cursor) + timedelta(days=1))
        if first >= today:
            return True
        build_day(self.memory, self.owner, first, zone=self.zone, state_dir=self.state_dir)
        self.cursor = first.isoformat()
        return first + timedelta(days=1) >= today
