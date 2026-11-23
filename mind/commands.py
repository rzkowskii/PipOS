"""Spoken commands handled in code, never by the language model.

A small model follows instructions unreliably, and "forget that" must not be
left to chance. Anything matched here is answered directly; everything else
goes to the model.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .reminder_language import ReminderRequest, may_be_reminder, parse_reminder


@dataclass(frozen=True)
class Command:
    kind: str
    arg: str = ""
    reminder: ReminderRequest | None = None


def _strip(text):
    return text.strip().rstrip(".!?").strip()


# Tried in order. Recall comes before remember, so "do you remember X" is a
# question, not a new memory.
_PATTERNS = [
    ("guest_start", r"start guest mode|don['’]?t remember this(?:[\s,.:;!?][\s\S]*)?"),
    ("guest_end", r"end guest mode"),
    ("recall", r"(?:what|anything)(?: else)? do you (?:remember|know) about (?P<x>.+)"),
    ("recall", r"do you remember (?:anything about |what i (?:said|told you) about )?(?P<x>.+)"),
    ("recall", r"(?:can|could) you remember (?:what i (?:said|told you) about |what we talked about )(?P<x>.+)"),
    ("recall_heard", r"what (?:have i told|did i tell) you about (?P<x>.+)"),
    ("recall_heard", r"what did i (?:say|mention) about (?P<x>.+)"),
    ("recall_said", r"what did you (?:say|tell me) about (?P<x>.+)"),
    ("recall_heard", r"when did i (?:tell you about|mention) (?P<x>.+)"),
    ("recall", r"what (?:did we talk|were we talking) about(?: (?P<x>.+))?"),
    ("forget_last", r"forget (?:that|it|this)(?: again| please)?"),
    ("forget", r"forget (?:about |what i (?:said|told you) about |everything about |that )?(?P<x>.+)"),
    ("correct", r"(?:no[,. ]+)?that(?:'s| is) (?:wrong|not right|incorrect)[,.:; ]+(?P<x>.+)"),
    ("remember", r"(?:please )?(?:can you |could you )?remember (?:that |this[:, ]+)?(?P<x>.+)"),
    ("remember", r"(?:please )?don'?t forget (?:that )?(?P<x>.+)"),
    ("time", r"(?:what(?:['’]s| is) the time|what time is it|tell me the time)(?: now)?"),
    ("date", r"(?:what(?:['’]s| is) (?:the|today['’]s) date|what date is it|tell me the date)(?: today)?"),
    ("day", r"what day(?: of the week)? (?:is it(?: today)?|is today)"),
    ("date_tomorrow", r"what(?:['’]s| is) tomorrow['’]s date|what (?:day|date) is (?:it )?tomorrow"),
    ("weather_location", r"(?:set|change) (?:my|the|your) weather (?:location|city) to (?P<x>.+)"),
    ("clear_weather_location", r"(?:clear|remove) (?:my|the|your) weather (?:location|city)"),
    ("clock_zone", r"(?:set|change) (?:my|the|your) (?:time zone|timezone) to (?P<x>.+)"),
    ("device_zone", r"use the (?:system|device) (?:time zone|timezone)"),
    ("skills", r"what can you do|what (?:skills|commands) (?:do you have|can i use)"),
    ("name", r"what(?:['’]s| is) your name|what (?:should|do) i call you"),
    ("age", r"how old are you|what(?:['’]s| is) your age|when were you born"),
    ("battery", r"how much charge do you have|how is your battery|what(?:'s| is) your battery level"),
    ("sleep", r"go to sleep|good ?night|sleep now|time to sleep"),
    ("look", r"(?:take a )?look at this|what(?:'s| is) this|what do you see"),
]
_COMPILED = [(kind, re.compile(rf"^(?:{pattern})$", re.I)) for kind, pattern in _PATTERNS]
_COMMAND_START = re.compile(
    r"^(?:(?:remember|forget|don'?t\s+forget)\b"
    r"|(?:start|end) guest\b|don'?t remember\b"
    r"|(?:can|could) you\b|(?:what (?:did|do|have|were)|when did|anything(?: else)? do)\b"
    r"|(?:that(?:'s| is)|no[, ]+that|go to|good night|sleep now|time to|(?:take a )?look at)\b"
    r"|what(?:'s| is) (?:the (?:time|date|weather)|today's date|tomorrow's date|it like outside)\b"
    r"|what (?:time|day|date|can you do|skills|commands)\b|tell me (?:the (?:time|date|weather)|about the weather)\b"
    r"|(?:how(?:'s| is) the weather|weather|(?:what(?:'s| is) the )?forecast)\b"
    r"|(?:what(?:'s| is) )?(?:today|tomorrow)'s (?:weather|forecast)\b"
    r"|(?:look up|give me) (?:the )?(?:(?:today|tomorrow)'s )?(?:weather|forecast)\b"
    r"|(?:will it rain|is it raining)\b"
    r"|(?:set|change|clear|remove) (?:my|the|your) (?:weather|time zone|timezone)\b"
    r"|use the (?:system|device) (?:time zone|timezone)\b"
    r"|what(?:['’]s| is) your name\b|what (?:should|do) i call you\b"
    r"|how (?:much charge|is your battery)\b|what(?:'s| is) your battery\b)",
    re.I,
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WHEN = re.compile(
    rf"(?<!\S)(?:(?:on )?(?P<day>\d{{4}}-\d{{2}}-\d{{2}}|today|yesterday|last week)"
    rf"|(?P<weekday_marker>on |last )?(?P<weekday>{'|'.join(_WEEKDAYS)}))$",
    re.I,
)
_SPOKEN_WORD = re.compile(r"[\w']+")
_SALUTATION = re.compile(r"(?:(?:hi|hey|hello)(?: there)?(?: again)?|good (?:morning|afternoon|evening))\b")
_CHECK_IN = re.compile(
    r"(?:(?:how are you(?: doing)?|how have you been|how(?:'s|s| is) it going)"
    r"(?: today)?|what(?:'s|s| is) up)"
)
_PRESENCE = re.compile(r"(?:are you (?:still )?there|can you hear me)")
_WEATHER = re.compile(
    r"(?:(?:what(?:'s| is)|how(?:'s| is)|tell me(?: about)?|give me|look up) (?:the )?)?"
    r"(?:(?P<before>today|tomorrow)'s )?(?P<subject>weather(?: forecast)?|forecast)"
    r"(?P<description> like| going to be(?: like)?)?"
    r"(?P<tail>(?: (?:for )?(?:today|tomorrow|right now|now))?(?: (?:in|for|at) .+)?)",
    re.I,
)
_OUTSIDE = re.compile(r"what(?:'s| is) it like outside(?: (?:right now|now))?", re.I)
_RAIN = re.compile(
    r"(?P<subject>will it rain|is it raining)"
    r"(?P<tail>(?: (?:today|tomorrow|right now|now))?(?: in .+)?)", re.I
)


def greeting(text, name=""):
    """Recognize a whole social turn, including punctuation-free speech."""
    said = " ".join(_SPOKEN_WORD.findall(text.casefold().replace("’", "'")))
    address = " ".join(_SPOKEN_WORD.findall(name.casefold().replace("’", "'")))
    if address:
        said = said.removeprefix(f"hey {address} ").removeprefix(address + " ").removesuffix(" " + address)
    if salutation := _SALUTATION.match(said):
        said = said[salutation.end():].lstrip()
        if address:
            said = said.removeprefix(address + " ")
        if not said:
            return "hello"
    if _CHECK_IN.fullmatch(said):
        return "check_in"
    if _PRESENCE.fullmatch(said):
        return "presence"
    return None


def history_query(text, *, today=None, zone=None):
    """Extract a trailing date and return a half-open range in the owner's zone.

    The system clock is UTC, so "today" and "yesterday" are the owner's days
    only when the owner's zone is given; without one the device zone is used.
    """
    text = text.strip()
    match = _WHEN.search(text)
    if not match:
        return text, None, None
    topic = text[:match.start()].rstrip(" ,")
    today = today or datetime.now(zone).date()
    day = (match["day"] or "").lower()
    duration = 1
    if day == "today":
        start = today
    elif day == "yesterday":
        start = today - timedelta(days=1)
    elif day == "last week":
        start = today - timedelta(days=today.weekday() + 7)
        duration = 7
    elif day:
        try:
            start = date.fromisoformat(day)
        except ValueError:
            return text, None, None
    else:
        if topic and match["weekday_marker"] is None:
            return text, None, None
        weekday = _WEEKDAYS.index(match["weekday"].lower())
        marker = (match["weekday_marker"] or "").lower()
        offset = (today.weekday() - weekday) % 7
        start = today - timedelta(days=offset or (7 if marker == "last " else 0))
    try:
        end = start + timedelta(days=duration)
        return (topic, datetime.combine(start, time.min, tzinfo=zone).timestamp(),
                datetime.combine(end, time.min, tzinfo=zone).timestamp())
    except (OverflowError, ValueError):
        return text, None, None


def _without_address(text, name):
    said = re.sub(r"^please[, ]+", "", _strip(text), flags=re.I)
    if name:
        # "Pip, remember that..." and "hey Pip remember..."
        said = re.sub(rf"^(?:hey |ok(?:ay)? )?{re.escape(name)}(?:[,.! ]+|$)", "", said, flags=re.I)
    return re.sub(r"^please[, ]+|[, ]+please$", "", said, flags=re.I).strip()


def may_be_command(text, name=""):
    """Avoid starting a speculative model read while a command is being said."""
    said = _without_address(text, name).replace("’", "'")
    return not said or bool(_COMMAND_START.match(said)) or may_be_reminder(said)


def _weather_command(said):
    said = said.replace("’", "'")
    if _OUTSIDE.fullmatch(said):
        return Command("weather_current")
    if weather := _WEATHER.fullmatch(said):
        before = weather["before"]
        predictive = "forecast" in weather["subject"].casefold() or "going" in (weather["description"] or "").casefold()
        default = "today" if predictive else "current"
    elif weather := _RAIN.fullmatch(said):
        before = None
        default = "today" if weather["subject"].casefold() == "will it rain" else "current"
    else:
        return None
    # Accept both "weather tomorrow in Sydney" and "weather in Sydney tomorrow".
    period = before.casefold() if before else default
    tail = weather["tail"].strip()
    if leading := re.match(r"^(?:for )?(today|tomorrow|right now|now)(?: |$)", tail, re.I):
        period = leading[1].casefold()
        tail = tail[leading.end():]
    location = re.sub(r"^(?:in|for|at) ", "", tail, flags=re.I)
    if following := re.search(r" (today|tomorrow|right now|now)$", location, re.I):
        period = following[1].casefold()
        location = location[:following.start()]
    if period.endswith("now"):
        period = "current"
    return Command(f"weather_{period}", location)


def parse(text, name="", *, now=None):
    """Return the Command in `text`, or None if it is ordinary conversation."""
    said = _without_address(text, name)
    if reminder := parse_reminder(said, now=now):
        return Command("reminder", reminder=reminder)
    for kind, pattern in _COMPILED:
        match = pattern.match(said)
        if match:
            captured = match.groupdict().get("x")
            arg = _strip(captured or "")
            if captured is not None and len(arg) < 2:
                return None
            return Command(kind, arg)
    return _weather_command(said)
