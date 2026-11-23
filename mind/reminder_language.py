"""Parse English reminder and timer requests without sending them to a model."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass(frozen=True)
class ReminderRequest:
    kind: str
    task: str = ""
    due_at: float | None = None
    reminder_id: int | None = None
    message: str = ""
    item_kind: str | None = None
    duration_seconds: int | None = None


class _ScheduleError(ValueError):
    """A recognizable schedule needs a correction before it can be saved."""


_POLITE = r"(?:(?:can|could|would) you )?"
_ITEMS = r"(?:reminders|timers)"
_SET = re.compile(_POLITE + r"(?:remind me|(?:set (?:me )?a|create a) reminder)\b", re.I)
_LIST = re.compile(
    _POLITE + rf"(?:(?:list|show|read)(?: me)?(?: my| the)? (?:upcoming )?{_ITEMS}"
    rf"|what are my (?:upcoming )?{_ITEMS}|what {_ITEMS} (?:do i have|are set|have i set)"
    rf"|(?:do i have )?(?:any|upcoming) {_ITEMS})", re.I,
)
_CANCEL = re.compile(
    _POLITE + r"(?:cancel|delete|remove) (?:my |the )?(reminders?|timers?)(?: (.+))?", re.I
)
_TIMER = re.compile(
    _POLITE + r"(?:start|set)(?: a| an)? (?:timer(?: for(?: (.+))?)?|(.+) timer)", re.I,
)
_COMMAND_START = re.compile(
    _POLITE + r"(?:remind\b"
    r"|(?:set|start|create)(?: me)?(?: a| an)?(?: (?:reminder|timer)\b|$)"
    r"|(?:cancel|delete|remove)(?: my| the)?(?: (?:reminders?|timers?)\b|$)"
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
_DIGITAL_CLOCK = re.compile(r"([0-9]{1,2})(?::([0-9]{2}))?")
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_DURATION = re.compile(r"(.+?) (seconds?|minutes?|hours?|days?)")
_DURATION_WORDS = {*_SMALL, *_TENS, *_UNITS, *(unit + "s" for unit in _UNITS), "a", "an", "and", "half", "timer"}

_ASK_TIME = "When should I remind you? Try saying, remind me to call Mum in ten minutes."
_ASK_TASK = "What should I remind you to do? Include the task and time in the same request."
_ASK_TIMER = "How long should the timer run? Try saying, set a timer for ten minutes."


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


def _clock(text):
    if text in ("noon", "midnight"):
        return time(12 if text == "noon" else 0)
    suffix = _MERIDIEM.search(text)
    if suffix:
        text = text[:suffix.start()].strip()
    text = text.removesuffix(" o'clock")
    if match := _DIGITAL_CLOCK.fullmatch(text):
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
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix[1] == "p" else 0)
    elif 1 <= hour <= 12 and not leading_zero:
        raise _ScheduleError("Did you mean a.m. or p.m.? Please repeat the request with the full time.")
    return time(hour, minute)


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


def _schedule(text, now):
    text = re.sub(r"\s*,\s*", " ", text).casefold().replace("’", "'").strip().removeprefix("for ")
    if text.startswith("in "):
        seconds = _duration_seconds(text[3:])
        if seconds is None:
            return None
        if not seconds:
            raise _ScheduleError("Choose a time in the future for your reminder.")
        return _relative_timestamp(now, seconds)

    text = text.removeprefix("at ")
    match = _DAY_FIRST.fullmatch(text) or _DAY_LAST.fullmatch(text)
    clock_text = match["clock"] if match else text
    if not clock_text:
        raise _ScheduleError("What time on that day? Include the task and time in the same request.")
    clock = _clock(clock_text)
    if clock is None:
        return None
    day = _calendar_day(match["day"], now.date()) if match else now.date()
    stamp = _local_timestamp(datetime.combine(day, clock), now.tzinfo)
    if stamp <= now.timestamp():
        raise _ScheduleError("That time has already passed. Choose a time in the future.")
    return stamp


def _create_request(rest, now):
    if rest[:3].casefold() == "to ":
        task_with_time = rest[3:]
        for match in _SCHEDULE_START.finditer(task_with_time):
            task = task_with_time[:match.start()].rstrip(" ,")
            if task and (stamp := _schedule(task_with_time[match.start():], now)) is not None:
                return ReminderRequest("create", task, stamp)
        return ReminderRequest("clarify", message=_ASK_TIME)

    time_text, *task_part = re.split(r"\s+to(?:\s+|$)", rest, maxsplit=1, flags=re.I)
    task = task_part[0].strip(" ,") if task_part else ""
    if not task:
        return ReminderRequest("clarify", message=_ASK_TASK)
    stamp = _schedule(time_text, now)
    if stamp is None:
        return ReminderRequest("clarify", message=_ASK_TIME)
    return ReminderRequest("create", task, stamp)


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
    if _LIST.fullmatch(said):
        item_kind = "timer" if "timers" in said.casefold().split() else "reminder"
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
