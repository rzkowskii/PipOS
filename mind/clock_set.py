"""Telling Pip the date and time, by voice or by typing.

A laptop whose clock battery is flat starts every power-up with a reset clock
(on the first real boot it read 17 October for 23 November). Pip then cannot
say the time, and until now the date could only be fixed on the Setup screen:
"set the time to" moves the time of day only, and refuses once the date is
doubted. This everyday skill takes the whole thing in one go:

    "It's the 28th of November 2026, twenty to four in the afternoon."

(plain forms too: "28 November 2026, 3:40 pm", "2026-11-28 15:40", or the date
first and the time in the next sentence). Pip reads it back and asks for a yes,
and only on the yes does it set the clock, through the same confirmed path as
the Setup screen (`care.set_clock`: moves running timers with the clock, saves
the trusted floor and what memory believes, and writes the battery clock when
it can). A guest cannot set it. A date that cannot be right is refused with its
reason, and nothing is changed without the owner's yes. Only the appliance's own
clock is ever set from here, never a development machine's.

The skill is the registry's usual pair: `may_match(text)` (a cheap gate, also
used to keep the model from starting to read the sentence) and
`handle(text, ctx)` (an `everyday` handler returning a `Reply`).
"""

import asyncio
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import care, clock_epoch
from .body_talk import Confirm, Reply, is_guest, said as tidy_said
from .skills import speakable_time
from .try_mode import is_try_boot

# How long a date said on its own waits for the time to follow.
PENDING_SECONDS = 90.0
# The question stays open this long for the owner's yes.
CONFIRM_SECONDS = 30.0
# No date further ahead of what Pip remembers than this: a slip of a year (2036
# for 2026) is a mistake, and the Setup screen can still set any date.
MAX_AHEAD = timedelta(days=730)
SETUP = "The setup screen can set it."


def _clock():
    return time.monotonic()

_MONTHS = {
    name: number for number, names in enumerate(
        (("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
         ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"),
         ("october", "oct"), ("november", "nov"), ("december", "dec")), 1)
    for name in names
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}
_ORDINAL_WORDS = "|".join(sorted(_ORDINALS, key=len, reverse=True))
_DAY_NAMES = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_SETTING = r"(?:set|change) (?:the |your )?(?:date|time|clock)(?: and (?:the )?(?:time|date))? to"
_INTENT = re.compile(
    rf"^(?:actually |now )?(?:it'?s|it is|today is|today'?s|the date is|the time is|the date and time (?:is|are)|"
    rf"the time and date (?:is|are)|{_SETTING})\b")
_LEAD = re.compile(
    rf"^(?:(?:and |actually |now )?(?:it'?s|it is|today is|today'?s|the date is|the date and time (?:is|are)|"
    rf"the time and date (?:is|are)|{_SETTING}|the time is|time is|at|and) ?)+")

# The date said on its own, waiting for a time: per state directory.
_pending = {}


# -- reading what was said ---------------------------------------------------

def _tidy(text):
    """Lower case, one space, and the punctuation that does not matter gone."""
    text = (text or "").casefold().replace("’", "'").replace("‘", "'")
    text = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", text)
    text = re.sub(r"(?<!\d)[.,;!?]+|[.,;!?]+(?!\d)", " ", text)
    return " ".join(text.split())


def _numbers(text):
    """Number words as digits: "twenty to four" -> "20 to 4", "three forty five" -> "3 45"."""
    out, words, i = [], text.split(), 0
    while i < len(words):
        word = words[i]
        following = words[i + 1] if i + 1 < len(words) else ""
        if word in _TENS:
            value = _TENS[word]
            if following in _ONES and 0 < _ONES[following] < 10:
                value += _ONES[following]
                i += 1
            out.append(str(value))
        elif word == "oh" and following in _ONES and 0 < _ONES[following] < 10:
            out.append(f"0{_ONES[following]}")
            i += 1
        elif word in _ONES:
            out.append(str(_ONES[word]))
        else:
            out.append(word)
        i += 1
    return " ".join(out)


def _year(text):
    """A year said in digits or words ("twenty twenty six", "two thousand and twenty seven"), or None."""
    text = (text or "").strip()
    if re.fullmatch(r"\d{4}", text):
        return int(text)
    words = [word for word in text.replace("-", " ").split() if word != "and"]
    if words[:2] == ["two", "thousand"]:
        rest = _numbers(" ".join(words[2:]))
        if not rest:
            return 2000
        return 2000 + int(rest) if re.fullmatch(r"\d{1,2}", rest) else None
    if words and words[0] == "twenty":
        rest = _numbers(" ".join(words[1:]))
        if re.fullmatch(r"\d{1,2}", rest):
            return 2000 + int(rest)
    return None


def _day_number(token):
    token = token.strip().replace("-", " ")
    token = re.sub(r"(?<=\d)(?:st|nd|rd|th)$", "", token)
    if token.isdigit():
        return int(token)
    if token in _ORDINALS:
        return _ORDINALS[token]
    parts = token.split()
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ORDINALS and _ORDINALS[parts[1]] < 10:
        return _TENS[parts[0]] + _ORDINALS[parts[1]]
    return None


_DAY = (rf"(?:\d{{1,2}}(?:st|nd|rd|th)?|(?:twenty|thirty)[ -](?:first|second|third|fourth|fifth|sixth|seventh|eighth|"
        rf"ninth)|{_ORDINAL_WORDS})")
_YEAR = (r"(?:\d{4}|two thousand(?: and)?(?: (?:twenty|thirty|forty|fifty|one|two|three|four|five|six|seven|eight|nine|"
         r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen))?"
         r"(?: (?:one|two|three|four|five|six|seven|eight|nine))?|twenty[ -]twenty(?:[ -](?:one|two|three|four|five|"
         r"six|seven|eight|nine|ten))?)")
_DATE_DMY = re.compile(rf"\b(?P<d>{_DAY})\s+(?:of\s+)?(?P<m>{_MONTH})\b(?:\s+(?P<y>{_YEAR})\b)?")
_DATE_MDY = re.compile(rf"\b(?P<m>{_MONTH})\s+(?:the\s+)?(?P<d>{_DAY})\b(?:\s+(?P<y>{_YEAR})\b)?")
_DATE_ISO = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")


def _find_date(text):
    """(day, month, year or None, what is left) for the date in the tidied `text`, or None."""
    text = re.sub(rf"\b{_DAY_NAMES}\b", " ", text)
    text = re.sub(rf"\bthe\b(?=\s+(?:\d|{_ORDINAL_WORDS}|twenty|thirty))", " ", text)
    for pattern in (_DATE_ISO, _DATE_DMY, _DATE_MDY):
        match = pattern.search(text)
        if match is None:
            continue
        found = match.groupdict()
        month = int(found["m"]) if found["m"].isdigit() else _MONTHS[found["m"]]
        day = _day_number(found["d"])
        year = _year(found["y"]) if found.get("y") else None
        if day is None or (found.get("y") and year is None):
            continue
        rest = " ".join((text[:match.start()] + " " + text[match.end():]).split())
        return day, month, year, rest
    return None


_NIGHT = re.compile(r"\b(?:at night|tonight)\b")
_AFTERNOON = re.compile(r"\bin the afternoon\b")
_EVENING = re.compile(r"\b(?:in the evening|pm)\b")
_AM = re.compile(r"\b(?:in the morning|am)\b")
_DAYPARTS = (_NIGHT, _AFTERNOON, _EVENING, _AM)
_AMBIGUOUS = "Say morning or afternoon with the time, or use the 24-hour clock."


def _find_time(text):
    """(hour, minute) for the clock time in `text`, None when there is none, or a
    ValueError when there is one that cannot be taken (no morning or afternoon).

    The morning or afternoon belongs to the minute that is named: "quarter to
    twelve at night" is 23:45 and "quarter to twelve in the morning" 11:45."""
    text = _numbers(_tidy(text))
    text = _LEAD.sub("", text).strip()
    text = re.sub(r"(?: (?:on|at|the))+$", "", text).strip()
    if not text:
        return None
    night, afternoon = bool(_NIGHT.search(text)), bool(_AFTERNOON.search(text))
    evening, am = bool(_EVENING.search(text)), bool(_AM.search(text))
    body = text
    for pattern in _DAYPARTS:
        body = pattern.sub(" ", body)
    body = " ".join(body.replace("a m", " ").replace("p m", " ").split())
    if body in ("noon", "midday"):
        return 12, 0
    if body == "midnight":
        return 0, 0
    shift = 0
    if match := re.fullmatch(r"(\d{1,2})[:.](\d{2})", body):
        hour, minute = int(match[1]), int(match[2])
    elif match := re.fullmatch(r"(quarter|half|\d{1,2}) (past|to) (\d{1,2})", body):
        amount = int(match[1]) if match[1].isdigit() else {"quarter": 15, "half": 30}[match[1]]
        if not 0 < amount < 60 or (match[1] == "half" and match[2] == "to"):
            return None
        hour, minute = int(match[3]), 0
        shift = amount if match[2] == "past" else -amount
    elif match := re.fullmatch(r"(\d{1,2}) o'?clock", body):
        hour, minute = int(match[1]), 0
    elif match := re.fullmatch(r"(\d{1,2}) (\d{2})", body):
        hour, minute = int(match[1]), int(match[2])
    elif match := re.fullmatch(r"(\d{1,2})", body):
        hour, minute = int(match[1]), 0
    else:
        return None
    if not (0 <= minute <= 59 or shift):
        return None
    if night or afternoon or evening or am:
        if not 1 <= hour <= 12:
            return None
        # The named minute, on the 12-hour clock: past or to the hour, wrapping round.
        total = ((hour % 12) * 60 + minute + shift) % 720
        clock12, minute = (total // 60) or 12, total % 60
        if am:
            hour = clock12 % 12
        elif night:
            hour = clock12 + 12 if 6 <= clock12 <= 11 else clock12 % 12
        elif afternoon:
            if clock12 == 11:
                raise ValueError("Eleven o'clock is not in the afternoon. Say it as morning, evening or night.")
            hour = clock12 % 12 + 12
        else:
            hour = clock12 % 12 + 12
        return hour, minute
    if 1 <= hour <= 12:
        raise ValueError(_AMBIGUOUS)
    if not (0 <= hour <= 23):
        return None
    total = (hour * 60 + minute + shift) % 1440
    return total // 60, total % 60


class Said:
    """What the owner said about the date and the time: either may be missing."""

    def __init__(self, day=None, month=None, year=None, clock=None):
        self.day, self.month, self.year, self.clock = day, month, year, clock

    @property
    def has_date(self):
        return self.day is not None

    @property
    def date(self):
        return (self.day, self.month, self.year)


def read(text):
    """The `Said` in a sentence. A ValueError carries what was wrong with it."""
    tidy = _tidy(text)
    found = _find_date(tidy)
    if found is None:
        return Said(clock=_find_time(tidy))
    day, month, year, rest = found
    return Said(day, month, year, _find_time(rest) if rest.strip() else None)


def may_match(text):
    """A cheap gate: a date said as "it's ...", or with a time, or a time while a date waits."""
    tidy = _tidy(text)
    if not tidy:
        return False
    if _pending_open() and re.search(r"\d|noon|midnight|midday|o'?clock|\bquarter\b|\bhalf\b", _numbers(tidy)):
        return True
    found = _find_date(tidy)
    if found is None:
        return False
    return bool(_INTENT.match(tidy)) or bool(re.search(r"\d|noon|midnight|midday|o'?clock|\bquarter\b|\bhalf\b",
                                                        _numbers(found[3])))


def _pending_open():
    now = _clock()
    for key in [key for key, (_, until) in _pending.items() if until < now]:
        del _pending[key]
    return bool(_pending)


# -- turning it into a moment, or a reason it cannot be --------------------

def moment(said, zone, today, state_dir=None, *, floor=None):
    """The aware datetime named by `said` (date and clock both present), or a
    ValueError saying why it cannot be right. The year is never borrowed from
    Pip's own clock (it is the one in doubt, and over New Year it could be a
    year out). `today` is kept for callers and not used. `floor` overrides the
    trusted clock floor (a correction in the same boot as a wrong set)."""
    year = said.year
    if year is None:
        raise ValueError("Say the year too.")
    try:
        when = datetime(year, said.month, said.day, said.clock[0], said.clock[1], tzinfo=zone)
    except ValueError:
        raise ValueError(f"There is no {said.day} {_month_name(said.month)} {year}.") from None
    stamp = when.timestamp()
    if stamp < clock_epoch.CLOCK_FLOOR or stamp > clock_epoch.MAX_CLOCK:
        raise ValueError(f"I can't be set to a date before 2026 or after 2100. {SETUP}")
    # A time the clocks skip, or play twice, that day: ask for the setup screen.
    shown = datetime.fromtimestamp(stamp, zone)
    if (shown.hour, shown.minute) != (when.hour, when.minute):
        raise ValueError(f"{speakable_time(when.hour, when.minute)} doesn't exist that day, because the clocks "
                         f"change. {SETUP}")
    if when.replace(fold=1).utcoffset() != when.utcoffset():
        raise ValueError(f"{speakable_time(when.hour, when.minute)} happens twice that day, because the clocks "
                         f"change. {SETUP}")
    if floor is None:
        floor = clock_epoch.floor(state_dir) if state_dir is not None else clock_epoch.CLOCK_FLOOR
    if stamp < floor:
        before = datetime.fromtimestamp(floor, zone)
        raise ValueError(f"That is earlier than things I already remember, from {before.day} "
                         f"{_month_name(before.month)} {before.year}. {SETUP}")
    if stamp > floor + MAX_AHEAD.total_seconds():
        raise ValueError(f"That is a long way after anything I remember. {SETUP}")
    return when


def _month_name(number):
    return date(2000, number, 1).strftime("%B")


def spoken_moment(when):
    return f"{when:%A} {when.day} {when:%B} {when.year}, {speakable_time(when.hour, when.minute)}"


# -- the skill ---------------------------------------------------------------

ASK_FOR_TIME = "[neutral] Okay. And what time is it? Say it like: twenty to four in the afternoon."


def appliance():
    """Only the appliance's own clock is set from here, never a development machine's."""
    return Path("/etc/being").is_dir()


_set_this_boot = {}  # state dir -> the clock floor from before this skill's own first set


async def handle(text, ctx):
    key = str(ctx.state_dir)
    # Only while the clock is in doubt, or to correct what this skill set in this boot: a
    # trusted clock is never moved by a sentence that happens to hold a date.
    if getattr(ctx, "clock_trusted", True) and key not in _set_this_boot:
        _pending.pop(key, None)
        return None
    heard = tidy_said(text, ctx)
    waiting = _pending.pop(key, None)
    if waiting is not None and waiting[1] < _clock():
        waiting = None
    try:
        got = read(heard)
    except ValueError as reason:
        # "it's the 28th of November, 3:40": a request with something wrong in it.
        if _INTENT.match(_tidy(heard)) and _find_date(_tidy(heard)) is not None and not is_guest(ctx):
            return Reply(f"[concern] {reason}")
        return None
    if waiting is not None and not got.has_date and got.clock is not None:
        got = Said(*waiting[0], got.clock)
    if not got.has_date or (got.clock is None and not _INTENT.match(_tidy(heard))):
        return None  # a date mentioned in passing is not a request to set the clock
    if is_guest(ctx):
        return Reply("[neutral] Only the person I live with can set my clock.")
    now = getattr(ctx, "now", None)
    if now is None:
        return Reply(f"[concern] I need to know your time zone before I can set my clock. {SETUP}")
    if got.clock is None:
        _pending[key] = (got.date, _clock() + PENDING_SECONDS)
        return Reply(ASK_FOR_TIME)
    try:
        when = moment(got, now.tzinfo, now.date(), ctx.state_dir, floor=_set_this_boot.get(key))
    except ValueError as reason:
        return Reply(f"[concern] {reason}")
    heard_at = _clock()

    async def on_yes():
        return await apply(ctx, when, waited=max(0.0, _clock() - heard_at))

    return Reply(f"[neutral] I heard {spoken_moment(when)}. Shall I set my clock to that? Say yes.",
                 confirm=Confirm(on_yes=on_yes, seconds=CONFIRM_SECONDS, strict=True))


async def apply(ctx, when, *, waited=0.0):
    """Set the clock to `when` (plus the seconds that went by since it was heard), after the
    owner's yes: the Setup screen's own path. Try sessions set the system clock only."""
    if not appliance():
        return Reply("[concern] I can only set my own clock on my laptop.")
    when = when + timedelta(seconds=waited)
    key = str(ctx.state_dir)
    before = clock_epoch.floor(ctx.state_dir)
    write_rtc = not is_try_boot()
    try:
        battery = await asyncio.to_thread(care.set_clock, ctx.state_dir, when.timestamp(),
                                          date_confirmed=True, write_rtc=write_rtc)
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return Reply("[concern] My timers are busy. Say it again in a moment.")
        return Reply(f"[concern] I couldn't set my clock. {SETUP}")
    except (OSError, ValueError, sqlite3.Error):
        return Reply(f"[concern] I couldn't change my clock myself. {SETUP}")
    _set_this_boot.setdefault(key, before)
    line = f"[joy] Thanks. It is {spoken_moment(when)}, and my clock says so now."
    if not write_rtc:
        line += " I will forget it when I restart."
    elif battery is False:
        line += (" I couldn't write it to my battery clock, so my clock will be wrong again after a restart. "
                 "The setup screen can save it to the battery clock.")
    return Reply(line)
