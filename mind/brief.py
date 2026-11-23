"""The morning brief: what Pip says to its owner's first words of the day.

Built in code, never by the model. At most three clauses and 35 words, in
this order of importance: a greeting (or "you're back" after days away),
today's reminders, a note about its body when one matters, and one
tentative dream remark. The dream remark is worded elsewhere (it comes only
from checked items and is marked as uncertain); this module only places it.

Off in guest mode and when the owner turns the brief off in Care; the mind
checks both (`InitiativePolicy.brief`).
"""

import re
from datetime import datetime, timedelta

from .profile import daily_greeting, spoken_count

MAX_CLAUSES = 3
MAX_WORDS = 35
LOW_BATTERY = 20    # percent, off the charger
WARM = 80.0         # package degrees Celsius, the owner's target
TAG = "[joy]"


def _words(text):
    return len(text.split())


def _clock(stamp, zone):
    from .skills import speakable_time

    due = datetime.fromtimestamp(stamp, zone)
    return speakable_time(due.hour, due.minute)


def _field(entry, name, index):
    if isinstance(entry, (tuple, list)):
        return entry[index] if len(entry) > index else None
    return getattr(entry, name, None)


def reminder_clause(now, reminders_today, *, most=2):
    """One clause about today's reminders and alarms, or None when there are none.

    `reminders_today` holds Reminder rows, or (text, due_at[, kind]) tuples.
    """
    from .skills import _task, number_words

    items = []
    for entry in reminders_today or ():
        text, due_at, kind = _field(entry, "text", 0), _field(entry, "due_at", 1), _field(entry, "kind", 2)
        if not isinstance(text, str) or type(due_at) not in (int, float):
            continue
        items.append((due_at, text, kind or "reminder"))
    if not items:
        return None
    items.sort()
    zone = now.tzinfo if now is not None else None
    alarms = [item for item in items if item[2] == "alarm"]
    reminders = [item for item in items if item[2] != "alarm"]
    parts = []
    if reminders:
        named = [f"{_task(text)} at {_clock(due_at, zone)}" for due_at, text, _ in reminders[:most]]
        count = len(reminders)
        head = "You have one reminder today" if count == 1 else f"You have {number_words(count)} reminders today"
        listed = " and ".join(named)
        if count > most:
            listed += f", and {number_words(count - most)} more"
        parts.append(f"{head}: {listed}.")
    if alarms and not reminders:
        parts.append(f"Your alarm is set for {_clock(alarms[0][0], zone)}.")
    return " ".join(parts) or None


def body_clause(body):
    """A note about its body only when it matters: a low battery off the charger, or running hot."""
    body = body or {}
    battery, on_ac, charging = body.get("battery"), body.get("on_ac"), body.get("charging")
    if type(battery) in (int, float) and battery <= LOW_BATTERY and on_ac is not True and charging is not True:
        return f"My battery is at {round(battery)} percent, so could you plug me in?"
    temp = body.get("temp")
    if type(temp) in (int, float) and temp >= WARM:
        return "I'm running warm, so I'm taking things slowly."
    return None


def greeting_clause(now, days_away=None):
    if days_away is not None and days_away >= 1:
        days = int(days_away)
        if days >= 14:
            return f"You're back. It's been {spoken_count(days // 7, 'week')}."
        return f"You're back. It's been {spoken_count(days, 'day')}."
    line = daily_greeting(now)
    return line.split("] ", 1)[1] if line.startswith("[") else line


def compose(now, reminders_today=(), body=None, dream_remark=None, days_away=None):
    """The brief as one spoken line with a leading emotion tag.

    `now` is the owner's local aware time (None when the zone is unknown),
    `body` the latest body.state fields, `dream_remark` one checked sentence
    or None, and `days_away` whole days since they last spoke.
    """
    clauses = [greeting_clause(now, days_away)]
    # The line carries one emotion tag, at its start: a tagged remark loses its own.
    remark = re.sub(r"^\[[\w-]+\]\s*", "", (dream_remark or "").strip()) or None
    for extra in (reminder_clause(now, reminders_today), body_clause(body), remark):
        if extra and len(clauses) < MAX_CLAUSES:
            if not extra.endswith((".", "?", "!")):
                extra += "."
            clauses.append(extra)
    # Over the word budget: shorten the reminders, then drop the least important clause.
    while sum(_words(clause) for clause in clauses) > MAX_WORDS and len(clauses) > 1:
        reminder = reminder_clause(now, reminders_today, most=1)
        if reminder and reminder_clause(now, reminders_today) in clauses and reminder not in clauses:
            clauses[clauses.index(reminder_clause(now, reminders_today))] = reminder
            continue
        clauses.pop()
    line = " ".join(clauses)
    words = line.split()
    if len(words) > MAX_WORDS:
        line = " ".join(words[:MAX_WORDS]).rstrip(",.:;") + "."
    return f"{TAG} {line}"


def today_window(now):
    """[start, end) of the rest of the owner's day, as Unix times."""
    end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
    return now.timestamp(), end.timestamp()
