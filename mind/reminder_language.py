"""Parse English reminder and timer requests without sending them to a model."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .reminders import WEEKDAYS as _RULE_DAYS, next_occurrence, wall_stamp


@dataclass(frozen=True)
class ReminderRequest:
    kind: str
    task: str = ""
    due_at: float | None = None
    reminder_id: int | None = None
    message: str = ""
    item_kind: str | None = None
    duration_seconds: int | None = None
    rule: str | None = None         # "daily", "weekdays" or "weekly:mon"; None rings once
    at_local: str | None = None     # a repeat's wall-clock time, "07:00"
    relative: bool = False          # said as a delay ("in two hours"): counts real seconds


class _ScheduleError(ValueError):
    """A recognizable schedule needs a correction before it can be saved."""


_POLITE = r"(?:(?:can|could|would) you )?"
_ITEMS = r"(?:reminders|timers|alarms)"
_SET = re.compile(_POLITE + r"(?:remind me|(?:set (?:me )?a|create a) reminder)\b", re.I)
_LIST = re.compile(
    _POLITE + rf"(?:(?:list|show|read)(?: me)?(?: my| the)? (?:upcoming )?{_ITEMS}"
    rf"|what are my (?:upcoming )?{_ITEMS}|what {_ITEMS} (?:do i have|are set|have i set)"
    rf"|(?:do i have )?(?:any|upcoming) {_ITEMS})", re.I,
)
_CANCEL = re.compile(
    _POLITE + r"(?:cancel|delete|remove|clear) (?:my |the |all my |all of my )?(?:(?:next|morning|wake ?up) )?"
    r"(reminders?|timers?|alarms?)(?: (.+))?", re.I
)
_TIMER = re.compile(
    _POLITE + r"(?:start|set)(?: a| an)? (?:timer(?: for(?: (.+))?)?|(.+) timer)", re.I,
)
_COMMAND_START = re.compile(
    _POLITE + r"(?:remind\b"
    r"|(?:set|start|create)(?: me)?(?: a| an)?(?: (?:reminder|timer)\b|$)"
    r"|(?:cancel|delete|remove)(?: my| the)?(?: (?:reminders?|timers?|alarms?)\b|$)"
    r"|wake me\b|(?:set|create)(?: me)?(?: an?| my| the)?(?: (?:daily |morning |wake ?up )?alarm\b|$)"
    r"|(?:every|each) (?:single )?(?:day|morning|afternoon|evening|night|weekday|week|work|mon|tue|wed|thu|fri|sat|sun)"
    r"|on weekdays\b"
    r"|how (?:long|much time)(?: is)? (?:left|remaining|until|till|before|to go)\b|how long (?:has|does) (?:my|the)\b"
    r"|when(?:'s| is| does) (?:my|the) (?:next )?(?:alarm|reminder|timer)\b|what time is my alarm\b"
    rf"|(?:list|show|read)(?: me)?(?: my| the)?(?: upcoming)?(?: {_ITEMS}\b|$)"
    rf"|what (?:are my(?: upcoming)?(?: {_ITEMS}\b|$)|{_ITEMS}\b)"
    rf"|do i have(?: any)?(?: {_ITEMS}\b|$)|any {_ITEMS}\b)", re.I,
)
_TIMER_START = re.compile(_POLITE + r"(?:set|start)(?: a| an)? (.+)", re.I)

_SMALL = {
    word: amount for amount, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    word: amount for amount, word in enumerate(
        "twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2
    )
}
_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
_DAY = (rf"(?:today|tomorrow|tonight|yesterday|(?:on )?(?:next )?(?:{'|'.join(_WEEKDAYS)})"
        r"|(?:on )?[0-9]{4}-[0-9]{2}-[0-9]{2})")
_DAY_FIRST = re.compile(rf"(?P<day>{_DAY})(?: (?:at )?(?P<clock>.+))?")
_DAY_LAST = re.compile(rf"(?P<clock>.+?) (?P<day>{_DAY})")
_SCHEDULE_START = re.compile(rf"(?<!\S)(?:in|at|for|{_DAY})\b", re.I)
_MERIDIEM = re.compile(r"(?<=[0-9\s])([ap])\.?\s*m\.?$")
_DIGITAL_CLOCK = re.compile(r"([0-9]{1,2})(?:[:.]([0-9]{2}))?")
_PAST_TO = re.compile(r"(?:a )?(?P<part>half|quarter) (?P<way>past|to) (?P<hour>[\w-]+)")
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_DURATION = re.compile(r"(.+?) (seconds?|minutes?|hours?|days?)")
_DURATION_WORDS = {*_SMALL, *_TENS, *_UNITS, *(unit + "s" for unit in _UNITS), "a", "an", "and", "half", "timer"}

_ASK_TIME = "When should I remind you? Try saying, remind me to call Mum in ten minutes."
_ASK_TASK = "What should I remind you to do? Include the task and time in the same request."
_ASK_TIMER = "How long should the timer run? Try saying, set a timer for ten minutes."
_ASK_ALARM = "What time should I wake you? Try saying, wake me up at 7."
_ASK_REPEAT_TIME = "What time should I remind you? Try saying, every weekday at 7, remind me to take my tablets."


def _number(value):
    if re.fullmatch(r"[0-9]{1,8}", value):
        return int(value)
    if value in _SMALL:
        return _SMALL[value]
    ten, _, rest = value.replace("-", " ").partition(" ")
    unit = _SMALL.get(rest, 0)
    if ten in _TENS and (not rest or 1 <= unit <= 9):
        return _TENS[ten] * 10 + unit
    return None


def _duration_seconds(text):
    text = re.sub(r"(?<=\w)-(?=[a-z])", " ", text)
    if text == "half an hour":
        return 1800
    if text == "an hour and a half":
        return 5400
    seconds = 0
    for part in text.split(" and "):
        match = _DURATION.fullmatch(part)
        if not match:
            return None
        quantity, unit = match.groups()
        amount = 1 if quantity in ("a", "an") else _number(quantity)
        if amount is None:
            return None
        seconds += amount * _UNITS[unit.removesuffix("s")]
    return seconds


def duration_seconds(text):
    """A spoken length ("ten minutes", "half an hour") in seconds, or None."""
    return _duration_seconds(" ".join(text.casefold().split()))


def _clock_parts(text):
    """(hour, minute, meridiem, leading_zero) as said, or None. Meridiem is "a", "p" or None."""
    if text in ("noon", "midday", "midnight"):
        return (0 if text == "midnight" else 12), 0, "24", False
    suffix = _MERIDIEM.search(text)
    if suffix:
        text = text[:suffix.start()].strip()
    text = text.removesuffix(" o'clock").removesuffix(" oclock")
    if spoken := _PAST_TO.fullmatch(text):
        # "Half past six", "quarter to seven": the hour on a 12-hour clock.
        base = _number(spoken["hour"])
        if base is None or not 1 <= base <= 12 or (spoken["part"], spoken["way"]) == ("half", "to"):
            return None
        if spoken["way"] == "to":
            hour, minute = (base - 1) or 12, 45
        else:
            hour, minute = base, 30 if spoken["part"] == "half" else 15
        leading_zero = False
    elif match := _DIGITAL_CLOCK.fullmatch(text):
        digits, minutes = match.groups()
        hour, minute = int(digits), int(minutes or 0)
        leading_zero = digits.startswith("0") and len(digits) == 2
    else:
        hour, minute, leading_zero = _number(text), 0, False
        if hour is None:
            words = text.replace("-", " ").split()
            for index in range(1, len(words)):
                candidate = _number(" ".join(words[:index]))
                minutes = " ".join(words[index:]).removeprefix("oh ")
                amount = _number(minutes)
                if candidate is not None and 0 <= candidate <= 23 and amount is not None:
                    hour, minute = candidate, amount
                    break
    if hour is None or hour > 23 or minute > 59:
        return None
    meridiem = suffix[1] if suffix else None
    if meridiem and not 1 <= hour <= 12:
        return None
    return hour, minute, meridiem, leading_zero


def _clock(text):
    parts = _clock_parts(text)
    if parts is None:
        return None
    hour, minute, meridiem, leading_zero = parts
    if meridiem in ("a", "p"):
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    elif meridiem is None and 1 <= hour <= 12 and not leading_zero:
        raise _ScheduleError("Did you mean a.m. or p.m.? Please repeat the request with the full time.")
    return time(hour, minute)


_PART_OF_DAY = re.compile(r" (?:in the (?P<am>morning)|in the (?P<pm>afternoon|evening)|at (?P<night>night)|(?P<tonight>tonight))$")


def _hours_for(text, hint=None):
    """The hours a spoken clock can mean, for alarms and repeats, which never ask a.m. or p.m.

    Returns (candidates, minute) or None. A bare 1 to 12 with no hint means
    both halves of the day; the caller chooses.
    """
    if part := _PART_OF_DAY.search(text):
        hint = "a" if part["am"] else "p"
        text = text[:part.start()]
    parts = _clock_parts(text.strip())
    if parts is None:
        return None
    hour, minute, meridiem, leading_zero = parts
    if meridiem in ("a", "p"):
        return [hour % 12 + (12 if meridiem == "p" else 0)], minute
    if meridiem == "24" or leading_zero or not 1 <= hour <= 12:
        return [hour], minute
    if hint in ("a", "p"):
        return [hour % 12 + (12 if hint == "p" else 0)], minute
    return [hour % 12, hour % 12 + 12], minute


def _usual_hour(candidates):
    """One hour from a bare clock, the way people mean a routine: 6 to 11 in the
    morning, 12 at noon, 1 to 5 in the afternoon."""
    if len(candidates) == 1:
        return candidates[0]
    morning = candidates[0]
    return morning if 6 <= morning <= 11 else morning + 12 if 1 <= morning <= 5 else 12


def _calendar_day(label, today):
    label = label.removeprefix("on ")
    if label in ("today", "tonight"):
        return today
    if label in ("tomorrow", "yesterday"):
        return today + timedelta(days=1 if label == "tomorrow" else -1)
    weekday = label.removeprefix("next ")
    if weekday in _WEEKDAYS:
        offset = (_WEEKDAYS.index(weekday) - today.weekday()) % 7
        if offset == 0 and label.startswith("next "):
            offset = 7
        return today + timedelta(days=offset)
    try:
        return date.fromisoformat(label)
    except ValueError:
        raise _ScheduleError("That date is invalid. Please repeat the request with a valid date.") from None


def _local_timestamp(when, zone):
    # Round trips distinguish ordinary times from skipped or repeated DST times.
    candidates = set()
    for fold in (0, 1):
        stamp = when.replace(tzinfo=zone, fold=fold).timestamp()
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == when:
            candidates.add(stamp)
    if not candidates:
        raise _ScheduleError("That time is skipped when the clocks change. Choose a different time.")
    if len(candidates) > 1:
        raise _ScheduleError("That time happens twice when the clocks change. Choose a different time.")
    return candidates.pop()


def _relative_timestamp(now, seconds):
    stamp = now.timestamp() + seconds
    try:
        datetime.fromtimestamp(stamp, now.tzinfo)
    except (OverflowError, OSError, ValueError):
        raise _ScheduleError("Choose a shorter delay.") from None
    return stamp


def _tonight(hour, today):
    """(day, hour) for an evening hour said with "tonight": 12 and the small hours are after midnight."""
    if hour == 12:
        return today + timedelta(days=1), 0
    if hour < 6:
        return today + timedelta(days=1), hour
    return today, hour


def _tonight_clock(text):
    """The 24-hour (hour, minute) for a clock said with "tonight", which never asks a.m. or p.m."""
    hours = _hours_for(text, "p")
    if hours is None:
        return None
    candidates, minute = hours
    return candidates[-1], minute


def _schedule(text, now):
    """(timestamp, relative) for a spoken time, or None if it is not one."""
    text = re.sub(r"\s*,\s*", " ", text).casefold().replace("’", "'").strip().removeprefix("for ")
    if text.startswith("in "):
        seconds = _duration_seconds(text[3:])
        if seconds is None:
            return None
        if not seconds:
            raise _ScheduleError("Choose a time in the future for your reminder.")
        return _relative_timestamp(now, seconds), True

    text = text.removeprefix("at ")
    match = _DAY_FIRST.fullmatch(text) or _DAY_LAST.fullmatch(text)
    clock_text = match["clock"] if match else text
    if not clock_text:
        raise _ScheduleError("What time on that day? Include the task and time in the same request.")
    if match and match["day"] == "tonight":
        said = _tonight_clock(clock_text.removeprefix("at "))
        if said is None:
            return None
        day, hour = _tonight(said[0], now.date())
        clock = time(hour, said[1])
    else:
        clock = _clock(clock_text)
        if clock is None:
            return None
        day = _calendar_day(match["day"], now.date()) if match else now.date()
    stamp = _local_timestamp(datetime.combine(day, clock), now.tzinfo)
    if stamp <= now.timestamp():
        raise _ScheduleError("That time has already passed. Choose a time in the future.")
    return stamp, False


def _create_request(rest, now):
    if rest[:3].casefold() == "to ":
        task_with_time = rest[3:]
        # Repeat phrases are found in a copy of the same length, so each
        # position also splits the original ("call Mum, Dad, and Sue every Sunday").
        aligned = task_with_time.replace(",", " ").replace("’", "'").lower()
        if len(aligned) != len(task_with_time):
            aligned = task_with_time
        starts = sorted({match.start() for match in _SCHEDULE_START.finditer(task_with_time)}
                        | {match.start() for match in _REPEAT.finditer(aligned)})
        for start in starts:
            task = task_with_time[:start].rstrip(" ,")
            if task and (schedule := _repeat_schedule(task_with_time[start:])) is not None:
                return _repeat_request("create", task, schedule, now)
        for match in _SCHEDULE_START.finditer(task_with_time):
            task = task_with_time[:match.start()].rstrip(" ,")
            if task and (when := _schedule(task_with_time[match.start():], now)) is not None:
                return ReminderRequest("create", task, when[0], relative=when[1])
        return ReminderRequest("clarify", message=_ASK_TIME)

    time_text, *task_part = re.split(r"\s+to(?:\s+|$)", rest, maxsplit=1, flags=re.I)
    task = task_part[0].strip(" ,") if task_part else ""
    if not task:
        return ReminderRequest("clarify", message=_ASK_TASK)
    if (schedule := _repeat_schedule(time_text)) is not None:
        return _repeat_request("create", task, schedule, now)
    when = _schedule(time_text, now)
    if when is None:
        return ReminderRequest("clarify", message=_ASK_TIME)
    return ReminderRequest("create", task, when[0], relative=when[1])


_REPEAT = re.compile(
    r"(?:(?:every|each) (?:single )?(?P<span>day|morning|afternoon|evening|night|weekday|week ?day|work ?day)"
    rf"|(?:every|each) (?:week on )?(?P<weekday>{'|'.join(_WEEKDAYS)})"
    rf"|on (?P<plural>weekdays|{'|'.join(day + 's' for day in _WEEKDAYS)})"
    r"|(?P<daily>daily)|(?P<weekdays>weekdays))(?=$|[ ,])"
)
_REPEAT_FIRST = re.compile(
    r"(?P<pre>(?:every|each|on weekdays|on \w+days|daily|weekdays)\b.*?)[, ]+"
    r"(?P<cmd>(?:(?:can|could|would) you )?(?:please )?(?:remind me|wake me|set (?:an?|my|the) alarm)\b.*)", re.I
)
_RULE_SPOKEN = {"daily": "every day", "weekdays": "every weekday"}


def spoken_rule(rule):
    """'every weekday', 'every Tuesday': a rule as it is said back."""
    if rule in _RULE_SPOKEN:
        return _RULE_SPOKEN[rule]
    return "every " + _WEEKDAYS[_RULE_DAYS.index(rule.removeprefix("weekly:"))].capitalize()


def _rule_of(match):
    """(rule, meridiem hint) for one repeat phrase."""
    if match["weekday"] or (match["plural"] and match["plural"] != "weekdays"):
        day = (match["weekday"] or match["plural"].removesuffix("s"))
        return f"weekly:{day[:3]}", None
    if match["weekdays"] or match["plural"] == "weekdays":
        return "weekdays", None
    span = (match["span"] or "day").replace(" ", "")
    if span in ("weekday", "workday"):
        return "weekdays", None
    return "daily", {"morning": "a", "afternoon": "p", "evening": "p", "night": "p"}.get(span)


def _norm(text):
    return re.sub(r"\s*,\s*", " ", text).casefold().replace("’", "'").strip()


def _repeat_schedule(text, rule=None, hint=None):
    """(rule, "HH:MM") for "every weekday at 7" or "at 7 pm every Tuesday", or None.

    Raises when a whole repeat is said with no time ("every day").
    """
    text = _norm(text).removeprefix("for ")
    rest = None
    if rule is None:
        for match in _REPEAT.finditer(text):
            if match.start() == 0 or match.end() == len(text):
                rule, hint = _rule_of(match)
                rest = (text[:match.start()] + " " + text[match.end():]).strip()
                break
        if rule is None:
            return None
    else:
        rest = text
    if not rest:
        raise _ScheduleError(_ASK_REPEAT_TIME)
    rest = rest.removeprefix("at ").strip()
    hours = _hours_for(rest, hint)
    if hours is None:
        return None
    candidates, minute = hours
    return rule, f"{_usual_hour(candidates):02d}:{minute:02d}"


def _repeat_request(kind, task, schedule, now):
    rule, at_local = schedule
    stamp = next_occurrence(rule, at_local, now.tzinfo, now.timestamp())
    return ReminderRequest(kind, task, stamp, rule=rule, at_local=at_local)


_ALARM = re.compile(
    _POLITE + r"(?:please )?(?:wake me(?: up)?|(?:set|create|make)(?: me)?(?: an?| my| the)? "
    r"(?:(?P<span>daily|morning|wake ?up) )?alarm)(?: (?P<rest>.+))?", re.I
)


def _alarm_request(match, now):
    rest = _norm(match["rest"] or "")
    rest = re.sub(r"^(?:to (?:go off|ring|wake me(?: up)?) )", "", rest)
    if not rest:
        return ReminderRequest("clarify", message=_ASK_ALARM)
    if rest.startswith("for ") and (seconds := _duration_seconds(rest[4:])):
        # "Set an alarm for ten minutes" is a length, like "in ten minutes".
        return ReminderRequest("alarm", "Alarm", _relative_timestamp(now, seconds), relative=True)
    if rest.startswith("in "):
        seconds = _duration_seconds(rest[3:])
        if not seconds:
            return ReminderRequest("clarify", message=_ASK_ALARM)
        return ReminderRequest("alarm", "Alarm", _relative_timestamp(now, seconds), relative=True)
    span = (match["span"] or "").replace(" ", "")
    if span == "daily":
        schedule = _repeat_schedule(rest.removeprefix("for "), rule="daily")
    else:
        schedule = _repeat_schedule(rest)
    if schedule is not None:
        return _repeat_request("alarm", "Alarm", schedule, now)
    rest = rest.removeprefix("for ").removeprefix("at ")
    day_match = _DAY_FIRST.fullmatch(rest) or _DAY_LAST.fullmatch(rest)
    clock_text = (day_match["clock"] if day_match else rest) or ""
    clock_text = clock_text.removeprefix("at ").removeprefix("for ")
    if not clock_text:
        return ReminderRequest("clarify", message=_ASK_ALARM)
    today = now.date()
    if day_match and day_match["day"] == "tonight":
        # "Tonight" is the evening: 9 is 21:00, and 12 is midnight.
        said = _tonight_clock(clock_text)
        if said is None:
            return ReminderRequest("clarify", message=_ASK_ALARM)
        day, hour = _tonight(said[0], today)
        stamp = wall_stamp(day, hour, said[1], now.tzinfo)
        if stamp > now.timestamp():
            return ReminderRequest("alarm", "Alarm", stamp)
        raise _ScheduleError("That time has already passed. Choose a time in the future.")
    hours = _hours_for(clock_text, "a" if span in ("morning", "wakeup") else None)
    if hours is None:
        return ReminderRequest("clarify", message=_ASK_ALARM)
    candidates, minute = hours
    days = [_calendar_day(day_match["day"], today)] if day_match and day_match["day"] else [today, today + timedelta(days=1)]
    for day in days:
        for hour in sorted(candidates):
            stamp = wall_stamp(day, hour, minute, now.tzinfo)
            if stamp > now.timestamp():
                return ReminderRequest("alarm", "Alarm", stamp)
    raise _ScheduleError("That time has already passed. Choose a time in the future.")


_LEFT = re.compile(
    _POLITE + r"(?:how (?:long|much time)(?: is| have i got| do i have| has it got| is there)? (?:left|remaining|to go)"
    r"(?: (?:on|for|in) (?:my|the) (?P<a>timers?|alarms?|reminders?))?"
    r"|how long (?:until|till|before|to) (?:my|the) (?:next )?(?P<b>timers?|alarms?|reminders?)"
    r"(?: (?:goes off|go off|is done|finishes|ends|rings|is due))?"
    r"|how long (?:has|does) (?:my|the) (?P<c>timer|alarm) (?:got )?(?:left|to go|have left)"
    r"|when(?:'s| is| does| will) (?:my|the) (?:next )?(?P<d>alarms?|reminders?|timers?)"
    r"(?: (?:set for|for|due|going off|go off|ring|finish|end|be done))?"
    r"|what time is my (?:next )?(?P<e>alarm)(?: set for)?)", re.I
)


def may_be_reminder(said):
    """Recognize opening words so a partial command stays off the model path."""
    said = " ".join(said.casefold().split())
    if _COMMAND_START.match(said):
        return True
    if match := _TIMER_START.fullmatch(said):
        words = match[1].replace("-", " ").split()
        return bool(words) and all(word.isdecimal() or word in _DURATION_WORDS for word in words)
    return False


def parse_reminder(said, *, now=None):
    """Parse a complete turn after removing any spoken address or courtesy words."""
    said = " ".join(said.strip().rstrip(".!?").split())
    if reordered := _REPEAT_FIRST.fullmatch(said):
        # "Every weekday at 7, remind me to..." says the same as "remind me ... every weekday at 7".
        said = f"{reordered['cmd']} {reordered['pre']}"
    if match := _LEFT.fullmatch(said):
        noun = next((group for group in match.groups() if group), "timer").casefold().removesuffix("s")
        return ReminderRequest("left", item_kind=noun)
    if match := _ALARM.fullmatch(said):
        try:
            return _alarm_request(match, now or datetime.now())
        except _ScheduleError as error:
            return ReminderRequest("clarify", message=str(error))
        except (OverflowError, OSError, ValueError):
            return ReminderRequest("clarify", message=_ASK_ALARM)
    if _LIST.fullmatch(said):
        words = said.casefold().split()
        item_kind = "timer" if "timers" in words else "alarm" if "alarms" in words else "reminder"
        return ReminderRequest("list", item_kind=item_kind)
    if match := _CANCEL.fullmatch(said):
        noun = match[1].lower()
        item_kind = noun.removesuffix("s")
        target = (match[2] or "").lstrip("#")
        number = _number(target.casefold().removeprefix("number "))
        if number:
            return ReminderRequest("cancel", reminder_id=number, item_kind=item_kind)
        if target and number is None:
            task = re.sub(r"^to ", "", target, flags=re.I)
            return ReminderRequest("cancel", task=task, item_kind=item_kind)
        if noun == "timer" and match[2] is None:
            return ReminderRequest("cancel", task="Timer", item_kind=item_kind)
        if item_kind == "alarm":
            # "Cancel my alarm", "cancel the alarm for tomorrow": the queue decides.
            return ReminderRequest("cancel", task="Alarm" if noun == "alarm" else "", item_kind=item_kind)
        prompt = "Say its number or the exact task." if item_kind == "reminder" else "Say its number."
        return ReminderRequest("clarify", message=f"Which {item_kind}? {prompt}")
    if match := _TIMER.fullmatch(said):
        duration = match[1] or match[2] or ""
        seconds = _duration_seconds(duration.casefold())
        if not seconds:
            return ReminderRequest("clarify", message=_ASK_TIMER)
        try:
            stamp = _relative_timestamp(now or datetime.now(), seconds)
        except _ScheduleError as error:
            return ReminderRequest("clarify", message=str(error))
        return ReminderRequest("timer", "Timer", stamp, duration_seconds=seconds)
    if match := _SET.match(said):
        rest = said[match.end():].lstrip(" ,")
        # “Remind me what I said” is a request for recall, with no future action.
        if match[0].casefold().endswith("remind me") and re.match(
            r"(?:of|what|when|where|which|who|how|why)\b", rest, re.I
        ):
            return None
        try:
            return _create_request(rest, now or datetime.now())
        except _ScheduleError as error:
            return ReminderRequest("clarify", message=str(error))
        except (OverflowError, OSError, ValueError):
            return ReminderRequest("clarify", message=_ASK_TIME)
    return None
