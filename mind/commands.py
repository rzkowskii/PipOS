"""Spoken commands handled in code, never by the language model.

A small model follows instructions unreliably, and "forget that" must not be
left to chance. Anything matched here is answered directly; everything else
goes to the model.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from . import actions, look
from .reminder_language import ReminderRequest, duration_seconds, may_be_reminder, parse_reminder

SNOOZE_MINUTES = 10  # "snooze" on its own


@dataclass(frozen=True)
class Command:
    kind: str
    arg: str = ""
    reminder: ReminderRequest | None = None


def _strip(text):
    return text.strip().rstrip(".!?").strip()


# Named days only: an exact date ("on 2026-11-19") stays a search of the conversation.
_DIARY_DAYS = r"(?:yesterday|last week|(?:on |last )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))"

# Tried in order. Recall comes before remember, so "do you remember X" is a
# question, not a new memory.
_MONTH_WORDS = ("january|february|march|april|june|july|august|september|october|november|december|"
                "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
_PATTERNS = [
    ("guest_start", r"start guest mode|don['’]?t remember this(?:[\s,.:;!?][\s\S]*)?"),
    ("guest_end", r"end guest mode"),
    # Memory questions with an answer of their own, before the general recall.
    ("remember_me", r"do you (?:still )?(?:remember|know) (?:me|who i am)|(?:do you know|remember) who i am"),
    ("about_me", r"what (?:else )?do you (?:remember|know) about me|what have you (?:remembered|learned) about me"
                 r"|what (?:things )?do you remember"),
    ("thread_save", r"pick this up later|keep (?:this|that) thread open"),
    ("thread_resume", r"what (?:were|are) we working on|pick up (?:our|the) thread|resume (?:our|the) thread"),
    ("thread_close", r"close (?:this|that|the) thread"),
    # A past day has its own record (the diary), kept overnight. Today is
    # still the conversation itself.
    ("diary", rf"what did (?:we|you and i) do (?P<x>{_DIARY_DAYS})"),
    ("diary", rf"what (?:did we (?:talk|chat)|were we talking) about (?P<x>{_DIARY_DAYS})"),
    ("talked_about", r"what (?:did we talk|were we talking|have we been talking) about(?: (?:earlier|before|lately|recently))?"
                     r"|what (?:did we do|have we done)(?: today)?"),
    ("recall", r"(?:what|anything)(?: else)? do you (?:remember|know) about (?P<x>.+)"),
    ("recall", r"do you remember (?:anything about |what i (?:said|told you) about )?(?P<x>.+)"),
    ("recall", r"(?:can|could) you remember (?:what i (?:said|told you) about |what we talked about )(?P<x>.+)"),
    ("recall_heard", r"what (?:have i told|did i tell) you about (?P<x>.+)"),
    ("recall_heard", r"what did i (?:say|mention) about (?P<x>.+)"),
    ("recall_said", r"what did you (?:say|tell me) about (?P<x>.+)"),
    ("recall_heard", r"when did i (?:tell you about|mention) (?P<x>.+)"),
    ("recall", r"what (?:did we talk|were we talking) about (?P<x>.+)"),
    # "Forget it" is how people say "never mind": it never deletes anything.
    ("never_mind", r"(?:oh[, ]+)?(?:forget it|never ?mind)(?: then)?"),
    ("forget_last", r"forget (?:that|this|what i just said)(?: again| please)?"),
    ("forget", r"forget (?:about |what i (?:said|told you) about |everything about |that )?(?P<x>.+)"),
    ("correct", r"(?:no[,. ]+)?that(?:['’]s| is) (?:wrong|not right|incorrect|not true)[,.:; ]+(?P<x>.+)"),
    ("remember", r"(?:please )?(?:can you |could you )?remember (?:that |this[:, ]+)?(?P<x>.+)"),
    ("remember", r"(?:please )?don'?t forget (?:that )?(?P<x>.+)"),
    # Not when a date comes with it: mind/clock_set.py takes date and time together.
    ("set_time", rf"(?:set|change) (?:the|your) (?:time|clock) to (?P<x>(?!.*\b(?:{_MONTH_WORDS})\b).+)"),
    ("set_time", rf"(?:actually[, ]+)?the time is (?:now |actually )?(?P<x>\d(?!.*\b(?:{_MONTH_WORDS})\b).*)"),
    ("world_time", r"(?:what(?:['’]s| is) the time|what time is it|what time(?:['’]s| is) it|tell me the time)"
                   r"(?: right now| now)? (?:in|over in) (?P<x>.+?)(?: right now| now| at the moment)?"),
    ("time_difference", r"what(?:['’]s| is) the time difference (?:to|with|from|for|in|between) (?P<x>.+)"),
    ("time_difference", r"how (?:many hours|far) (?:ahead|behind|ahead or behind) (?:is|are) (?P<x>.+?)"
                        r"(?: (?:from|of|than) (?:here|us|me|you))?"),
    ("time_difference", r"is (?P<x>.+?) ahead (?:of (?:us|me|here) )?or behind(?: (?:us|me|here))?"),
    # "Well time is it": Moonshine's reading of the owner's "what time is it" (laptop, 28 Nov).
    ("time", r"(?:what(?:['’]s| is) the time|(?:what|well) time is it|tell me the time|do you know what time it is)"
             r"(?: now)?"),
    ("date", r"(?:what(?:['’]s| is) (?:the|today['’]s) date|what date is it|tell me the date)(?: today)?"),
    ("day", r"what day(?: of the week)? (?:is it(?: today)?|is today)"),
    ("date_tomorrow", r"what(?:['’]s| is) tomorrow['’]s date|what (?:day|date) is (?:it )?tomorrow"),
    ("weather_location", r"(?:set|change) (?:my|the|your) weather (?:location|city) to (?P<x>.+)"),
    ("clear_weather_location", r"(?:clear|remove) (?:my|the|your) weather (?:location|city)"),
    ("clock_zone", r"(?:set|change) (?:my|the|your) (?:time zone|timezone) to (?P<x>.+)"),
    ("device_zone", r"use the (?:system|device) (?:time zone|timezone)"),
    ("permissions", r"what are you allowed to do|what permissions do you have"),
    # Consent is deliberately limited to explicit owner declarations. These
    # exact forms must not turn questions or narrative into permission changes.
    ("camera_consent", r"you can use your camera|stop watching|turn (?:the )?camera (?:on|off)"),
    ("dream_consent", r"you can dream tonight|stop dreaming"),
    ("skills_more", r"what else can you do(?: for me)?|what else (?:can i (?:say|ask)|can you help (?:me )?with|do you do)"
                    r"|what more can you do"),
    ("voice_help", r"what can i say(?: to you)?|(?:show|give) me (?:the )?voice commands"),
    ("skills_group", r"what (?:can|could) you do (?:with|about|for) (?!me$)(?P<x>.+)"),
    ("skills_group", r"what can i (?:say|ask(?: you)?) about (?P<x>.+)"),
    ("skills_group", r"help (?:me )?with (?P<x>.+)"),
    ("skills", r"what (?:can|could) you do(?: for me)?|what (?:skills|commands) (?:do you have|can i use)"
               r"|what can i ask(?: you)?|what are you able to do|what can you help (?:me )?with|help(?: me)?"),
    # A ringing alarm, timer or reminder. Bare "okay" and "thanks" only count
    # while something rings: see ring_reply.
    ("snooze", r"snooze(?: (?:it|that|this|the alarm|my alarm|the timer|the reminder))?(?: for (?P<x>.+))?"),
    ("snooze", r"(?:give me |just )*(?P<x>(?:a few|another|\w+(?:[- ]\w+)?)) more minutes?"),
    ("ack", r"(?:stop|dismiss|silence|turn off) (?:the|my|that) (?:alarm|timer|reminder)|turn (?:the|my|that) (?:alarm|timer) off"
            r"|i(?:['’]m| am) (?:up|awake)(?: now)?"),
    ("name", r"what(?:['’]s| is) your name|what (?:should|do) i call you"),
    ("age", r"how old are you|what(?:['’]s| is) your age|when were you born"),
    ("battery", r"how much (?:charge|battery) (?:do you have|have you got)(?: left)?|how(?:['’]s| is) your battery(?: doing)?"
                r"|what(?:['’]s| is) your battery(?: level| at)?|tell me about your battery|are you charging"
                r"|how long (?:will|can) your battery last|how much battery (?:is )?left"),
    ("sleep", r"go to sleep|good ?night|sleep now|time to sleep"),
    ("look", r"(?:take a )?look at this|what do you see|can you see (?P<x>me|this|anything)"),
    ("dream", r"what did you dream(?: about)?(?: last night)?|did you dream(?: last night)?|do you dream"),
    # Questions about what it is. Answered honestly in code, never left to a
    # small model that may claim feelings or deny being anything at all.
    ("self_alive", r"are you alive|are you (?:a )?living(?: thing| being)?"),
    ("self_aware", r"are you (?:conscious|sentient|self[- ]aware|aware)|do you have (?:a )?(?:consciousness|mind of your own)"),
    ("self_feel", r"do you (?:have )?(?:feel(?:ings)?|emotions)(?: anything)?|can you feel(?: anything)?|do you get (?:lonely|sad|happy)"),
    ("self_what", r"what are you|are you (?:real|a robot|a computer|a person|human|an ai|just a program)"),
    # Its loudness. The voice organ owns the mixer; the mind only asks.
    ("volume_up", r"(?:speak|talk) (?:up|louder)|(?:be )?louder|turn (?:it|yourself|your voice|the volume) up"
                  r"|turn up (?:the |your )?volume|(?:i )?can['’]?t hear you(?: well)?"),
    ("volume_down", r"(?:speak|talk) (?:more )?(?:quieter|softer|more quietly|more softly|down)|(?:be )?quieter|(?:be )?softer"
                    r"|turn (?:it|yourself|your voice|the volume) down|turn down (?:the |your )?volume|you(?:['’]re| are) too loud"),
    ("volume_set", r"(?:set|change|put) (?:your |the )?volume (?:to |at )?(?P<x>.+)"),
]
# A remembered "fact" that is really a question or an instruction goes to the
# conversation: "remember when we went fishing?" is not a thing to store.
QUESTION = re.compile(
    r"^(?:how|when|where|what|who|whom|whose|which|why|can|could|would|will|should|did|do|does|is|are|was|were|have|has)\b",
    re.I,
)
_NOT_A_FACT = re.compile(r"^(?:when|how|what|why|where|who|whom|whose|which|if|whether)\b", re.I)
_COMPILED = [(kind, re.compile(rf"^(?:{pattern})$", re.I)) for kind, pattern in _PATTERNS]
_QUICK_MENU_VISIBILITY = re.compile(r"^(open|show|close|hide) (?:the )?quick menu$", re.I)
_QUICK_MENU_POSITION = re.compile(r"^(?:move|put) (?:the )?(?:quick )?menu (?:(?:to|on) the )?(left|right|centre|center)$", re.I)
_QUICK_MENU_FOLLOWUPS = {
    f"actually put it on the {said}": position
    for said, position in (("left", "left"), ("right", "right"), ("centre", "center"), ("center", "center"))
}
_COMMAND_START = re.compile(
    r"^(?:(?:remember|forget|don'?t\s+forget)\b"
    r"|(?:start|end) guest\b|don'?t remember\b"
    r"|pick (?:this up later|up (?:our|the) thread)\b|keep (?:this|that) thread open\b"
    r"|(?:resume (?:our|the)|close (?:this|that|the)) thread\b"
    r"|(?:can|could) you\b|(?:what (?:did|do|have|were)|when did|anything(?: else)? do)\b"
    r"|(?:that(?:'s| is)|no[, ]+that|go to|good night|sleep now|time to|(?:take a )?look at)\b"
    r"|what(?:'s| is) (?:the (?:time|date|weather)|today's date|tomorrow's date|it like outside)\b"
    r"|what (?:time|day|date|can you do|could you do|skills|commands|else can you do|can i say|did we do)\b"
    r"|well time is it\b"
    r"|what reminders do i have\b|(?:read|list|show me) (?:(?:my )?reminders|(?:today|tomorrow)['’]s reminders)\b"
    r"|(?:show|give) me (?:the )?voice commands\b"
    r"|(?:open|show|close|hide|move|put) (?:the )?quick menu\b|(?:move|put) (?:the )?menu\b"
    r"|tell me (?:the (?:time|date|weather)|about the weather)\b"
    r"|(?:snooze|dismiss)\b|(?:give me |just )?\w+ more minutes?\b|i(?:'m| am) (?:up|awake)\b"
    r"|(?:stop|turn off|silence) (?:the|my|that) (?:alarm|timer|reminder)\b|help\b"
    r"|how (?:many hours|far) (?:ahead|behind)\b|is [\w ]+ ahead or behind\b"
    r"|(?:how(?:'s| is) the weather|weather|(?:what(?:'s| is) the )?forecast)\b"
    r"|(?:what(?:'s| is) )?(?:today|tomorrow)'s (?:weather|forecast)\b"
    r"|(?:look up|give me) (?:the )?(?:(?:today|tomorrow)'s )?(?:weather|forecast)\b"
    r"|(?:will it rain|is it raining)\b"
    r"|(?:set|change|clear|remove) (?:my|the|your) (?:weather|time zone|timezone)\b"
    r"|use the (?:system|device) (?:time zone|timezone)\b"
    r"|what(?:['’]s| is) your name\b|what (?:should|do) i call you\b"
    r"|how (?:much (?:charge|battery)|is your battery)\b|what(?:'s| is) your battery\b|do you know what time\b"
    r"|never ?mind\b|(?:speak|talk|turn|be) (?:up|down|louder|quieter|softer|it|the|your|yourself|more)\b"
    r"|louder\b|quieter\b|softer\b|(?:set|change|put) (?:your |the )?(?:volume|time|clock)\b|the time is\b"
    r"|make your (?:eyes|face)\b|(?:set|change) your (?:eye|face size|glow)\b"
    r"|(?:show|hide) (?:your )?(?:(?:eye)?brows|mouth)\b"
    r"|what(?:'s| is) your (?:current )?look\b|tell me your current look\b|how do you look\b"
    r"|are you (?:alive|conscious|sentient|real|aware|self|a |an |just|human|living)\b"
    r"|do you (?:have |get |feel|dream|remember me|know me)|what (?:are you|permissions)\b|can you (?:feel|see)\b"
    r"|did you dream\b|tell me about your battery\b|how long (?:will|can) your\b|are you charging\b"
    r"|i can'?t hear you\b|you(?:'re| are) too loud\b"
    r"|you can use your camera\b|stop watching\b|turn (?:the )?camera (?:on|off)\b"
    r"|you can dream tonight\b|stop dreaming\b)",
    re.I,
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WHEN = re.compile(
    rf"(?<!\S)(?:(?:on )?(?P<day>\d{{4}}-\d{{2}}-\d{{2}}|today|yesterday|last week)"
    rf"|(?P<weekday_marker>on |last )?(?P<weekday>{'|'.join(_WEEKDAYS)}))$",
    re.I,
)
_EVENT_WHEN = re.compile(
    r"(?<!\w)(?:(?:on\s+)?(?P<date>\d{4}-\d{2}-\d{2})|(?P<relative>today|yesterday|tomorrow))(?!\w)",
    re.I,
)
_EVENT_QUESTION = re.compile(
    r"^(?:what|when|where|which|who|why|how|do|does|did|is|are|was|were|can|could|would|will|"
    r"tell|show|remember|recall|forget)\b",
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
        # Just its name, or "hey Pip" said again once the eyes have brightened,
        # is a hello: the person is waiting to be noticed.
        if said in (address, f"hey {address}", f"ok {address}", f"okay {address}"):
            return "hello"
        said = said.removeprefix(f"hey {address} ").removeprefix(address + " ").removesuffix(" " + address)
    if said in ("hey", "hiya", "yo", "oh hi", "oh hello"):
        return "hello"
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


def event_day(text):
    """Return the lone day expression in a declarative statement, if any."""
    said = re.sub(r"^remember\s+that\s+", "", text.strip(), flags=re.I)
    if (not said or said.endswith("?") or _EVENT_QUESTION.match(said)
            or re.match(r"^(?:today|yesterday|tomorrow)[, ]+(?:what|when|where|who|why|how|did|do|was|were)\b",
                        said, re.I)):
        return None
    matches = list(_EVENT_WHEN.finditer(said))
    if len(matches) != 1:
        return None
    match = matches[0]
    if match["relative"]:
        return match["relative"].lower()
    try:
        return date.fromisoformat(match["date"])
    except ValueError:
        return None


def event_range(text, *, today=None, zone=None, relative_allowed=None):
    """Return an explicitly stated event day for a declarative owner sentence.

    This deliberately recognises less than :func:`history_query`: weekdays
    need conversational context to decide whether they are past or future,
    and questions describe a search rather than an event being remembered.
    Ambiguous sentences containing two different day phrases are left with
    telling time only.
    """
    day = event_day(text)
    if day is None:
        return None
    start = day
    if isinstance(day, str):
        if relative_allowed is not None and not relative_allowed():
            return None
        today = today or datetime.now(zone).date()
        start = today + timedelta(days={"today": 0, "yesterday": -1, "tomorrow": 1}[day])
    try:
        end = start + timedelta(days=1)
        return (datetime.combine(start, time.min, tzinfo=zone).timestamp(),
                datetime.combine(end, time.min, tzinfo=zone).timestamp())
    except (OverflowError, ValueError):
        return None


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


def quick_menu_followup(text, name="", *, partial=False):
    """Position wording to use only when the caller already has the Quick Menu context."""
    said = " ".join(_without_address(text, name).casefold().split())
    if said.startswith("actually,"):
        said = "actually" + said.removeprefix("actually,")
    if partial:
        return any(phrase.startswith(said) for phrase in _QUICK_MENU_FOLLOWUPS)
    return _QUICK_MENU_FOLLOWUPS.get(said)


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
    if menu := _QUICK_MENU_VISIBILITY.fullmatch(said):
        return Command("quick_menu", "open" if menu[1].casefold() in ("open", "show") else "close")
    if menu := _QUICK_MENU_POSITION.fullmatch(said):
        return Command("quick_menu", "center" if menu[1].casefold() == "centre" else menu[1].casefold())
    if options := re.fullmatch(r"(open|show(?: me)?|close|hide) (?:the )?options", said, re.I):
        return Command("screen_options", "close" if options[1].casefold() in ("close", "hide") else "open")
    if request := look.parse(said):
        field, value = request
        return Command("look_current") if field == "current" else Command("look_set", f"{field} {value}")
    # Day-scoped reads never create, cancel, or acknowledge queue entries.
    # Their exact forms are also safe deterministic habit steps.
    day_reminders = re.fullmatch(
        r"(?:(?:read|list|show me) (?:my )?reminders (?:for )?|(what reminders do i have) )"
        r"(?P<day>today|tomorrow)", said, re.I)
    if day_reminders is None:
        day_reminders = re.fullmatch(
            r"(?:read|list|show me) (?P<day>today|tomorrow)['’]s reminders", said, re.I)
    if day_reminders:
        return Command(f"reminders_{day_reminders['day'].casefold()}")
    if reminder := parse_reminder(said, now=now):
        return Command("reminder", reminder=reminder)
    for kind, pattern in _COMPILED:
        match = pattern.match(said)
        if match:
            captured = match.groupdict().get("x")
            arg = _strip(captured or "")
            if kind == "camera_consent":
                choice = said.casefold()
                arg = "off" if choice == "stop watching" or choice.endswith(" off") else "on"
            elif kind == "dream_consent":
                arg = "off" if said.casefold() == "stop dreaming" else "tonight"
            if captured is not None and len(arg) < (1 if kind in ("set_time", "volume_set", "snooze") else 2):
                return None
            if kind == "skills_group":
                # "Help me with my homework" is conversation: only a group it
                # really has help for is answered here.
                if (group := actions.help_group(arg)) is not None:
                    return Command("voice_help", group)
                from .skills import help_group

                if help_group(arg) is None:
                    return None
            if kind == "snooze":
                minutes = snooze_minutes(arg)
                if minutes is None:
                    return None
                arg = str(minutes)
            if kind == "remember":
                # "Remember when we went fishing?" is a question, but "remember
                # that when I'm tired I get grumpy" is a fact introduced as one.
                introduced = re.search(r"\b(?:that|this)[:, ]*$", said[:match.start("x")], re.I)
                if text.rstrip().endswith("?") or (_NOT_A_FACT.match(arg) and not introduced):
                    return None
            return Command(kind, arg)
    return _weather_command(said)


def snooze_minutes(arg):
    """Minutes for "snooze for ten minutes" or "five more minutes"; None if unclear."""
    said = " ".join(arg.casefold().replace("-", " ").split())
    said = re.sub(r"^(?:another|just|a further) ", "", said)
    if not said:
        return SNOOZE_MINUTES
    if said in ("a few", "few", "a couple", "a couple of"):
        return 5
    if said in ("a minute", "one minute", "a min"):
        return 1
    seconds = duration_seconds(re.sub(r"\bmins?\b", "minutes", said))
    if seconds is None:
        number = int(said) if said.isdigit() else spoken_number(said)
        seconds = number * 60 if number is not None else None
    if not seconds or seconds % 60:
        return None
    minutes = seconds // 60
    return minutes if 1 <= minutes <= 120 else None


_RING_STOP = re.compile(r"(?:(?:can|could|would) you )?stop (?:my|the|that) (?:alarm|timer|reminder)", re.I)
_RING_ACK = re.compile(
    r"(?:(?:oh )?(?:ok(?:ay)?|alright|all right|right|thanks|thank you|cheers|got it|stop(?: it)?|enough|i heard(?: you)?"
    r"|i know|yes|yep|yeah|done|dismiss|that(?:'s| is) fine|fine|i(?:'m| am) (?:up|awake|on it|coming))"
    r"(?:[, ]+(?:then|thanks|thank you|now))*)",
    re.I,
)


def ring_reply(text, name=""):
    """While an alarm, timer or reminder rings: Command("ack"), Command("snooze", minutes), or None.

    Only for that moment. Outside it, "okay" and "thanks" are conversation.
    """
    said = _without_address(text, name).replace("’", "'")
    if name:
        said = re.sub(rf"[, ]+{re.escape(name)}$", "", said, flags=re.I)
    if _RING_STOP.fullmatch(said):
        # "Stop the timer" acknowledges a ringing one; only outside a ring
        # does it cancel (reminder_language._STOP_TIMER).
        return Command("ack")
    command = parse(said)
    if command is not None and command.kind in ("ack", "snooze"):
        return command
    return Command("ack") if _RING_ACK.fullmatch(said) else None


def volume_level(arg):
    """A spoken volume ("forty", "40 percent", "max") as 0-100, or None."""
    said = arg.casefold().replace("%", " percent").replace("-", " ")
    said = re.sub(r"\b(?:percent|per cent|please|out of (?:a )?hundred)\b", " ", said).strip()
    if said in ("max", "maximum", "full", "full volume", "all the way up", "the max", "the maximum"):
        return 100
    if said in ("half", "half way", "halfway", "the middle", "middle"):
        return 50
    if said.isdigit():
        value = int(said)
        return value if 0 <= value <= 100 else None
    value = spoken_number(said)
    return value if value is not None and 0 <= value <= 100 else None


_ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}


def spoken_number(said):
    """Whole numbers up to one hundred, in words."""
    words = said.replace(" and ", " ").split()
    if words == ["a", "hundred"] or words == ["one", "hundred"] or words == ["hundred"]:
        return 100
    if len(words) == 1:
        return _ONES.get(words[0], _TENS.get(words[0]))
    if len(words) == 2 and words[0] in _TENS and words[1] in _ONES and 0 < _ONES[words[1]] < 10:
        return _TENS[words[0]] + _ONES[words[1]]
    return None


_CLOCK = re.compile(
    r"^(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?P<half>a\.?m\.?|p\.?m\.?)?(?:\s+(?:in the (?:morning|afternoon|evening)|at night|tonight))?$",
    re.I,
)


def clock_time(arg):
    """A spoken clock time ("3:45 pm", "15:45", "9 in the morning") as (hour, minute), or None."""
    said = " ".join(arg.split())
    match = _CLOCK.match(said)
    if not match:
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    half = (match["half"] or "").replace(".", "").casefold()
    part = said.casefold()
    if not half:
        if "afternoon" in part or "evening" in part or "at night" in part or "tonight" in part:
            half = "pm"
        elif "morning" in part:
            half = "am"
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif not match["minute"] and hour <= 12 and "morning" not in part:
        # "Set the time to 3" says too little: 3 am or 3 pm.
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


# -- wording of memories -------------------------------------------------

_REFLECT = {
    "i": "you", "me": "you", "my": "your", "mine": "yours", "myself": "yourself", "am": "are",
    "i'm": "you're", "i've": "you've", "i'll": "you'll", "i'd": "you'd", "we": "we", "our": "our",
}


def reflect(text, *, start=False):
    """Turn the owner's own words round for a read-back: "my cat" -> "your cat".

    Only first-person words change. "Am" changes only after "I", so "I am
    here" reads back "you are here" and "Amsterdam" stays as it is.
    """
    out = []
    previous = ""
    for token in re.split(r"([^\w']+)", text.replace("’", "'")):
        low = token.casefold()
        if low == "am" and previous != "i":
            out.append(token)
        elif low in _REFLECT and token:
            word = _REFLECT[low]
            out.append(word.capitalize() if start and not "".join(out).strip() else word)
        else:
            out.append(token)
        if token.strip(" ,;:"):
            previous = low
    return "".join(out)


# The last linking word in a fact divides what it is about from what is said
# about it: "my cat is called | Tom", "my birthday is in | May".
_LINK = re.compile(
    r"\b(?:is|are|was|were|am|be|called|named|at|in|on|from|to|by|likes?|loves?|hates?|lives?|works?|uses?)\s+",
    re.I,
)
# What follows "that's wrong," when it is only the new value: "it's Jim".
_FRAGMENT = re.compile(
    r"^(?:no[, ]+)?(?:actually[, ]+)?(?:it(?:['’]s| is| was| should be)|its|that(?:['’]s| is| should be)|"
    r"they(?:['’]re| are)|he(?:['’]s| is)|she(?:['’]s| is)|the answer is|i said|i meant)\s+(?P<value>.+)$",
    re.I,
)
SPLICE_WORDS = 4


def correction_value(replacement):
    """The new value in a fragment such as "it's Jim", or None for a whole sentence."""
    match = _FRAGMENT.match(replacement.strip())
    return _strip(match["value"]) if match else None


def subject_of(fact):
    """The part of a fact before its last linking word, or None."""
    links = list(_LINK.finditer(fact))
    if not links:
        return None
    head = fact[:links[-1].end()].strip()
    tail = fact[links[-1].end():].strip()
    if not tail or len(tail.split()) > SPLICE_WORDS or len(head.split()) < 2:
        return None
    return head


_ONE_VALUE = re.compile(
    r"(?:\b(?:is|are|am|was|were)(?:\s+(?:in|on|at|from))?|\b(?:called|named)"
    r"|\b(?:live|lives|living|work|works|working)\s+(?:in|at|for))$", re.I)


def replaceable_subject(fact):
    """The subject of a fact that holds one value at a time, or None.

    "My cat is called Tom" and "I live in Perth" have one answer, so a new
    one may replace the old. "I like tea" and "I went to Rome" do not:
    liking coffee too is no reason to ask about the tea.
    """
    head = subject_of(fact)
    return head if head is not None and _ONE_VALUE.search(head) else None


def splice_correction(fact, value):
    """Put a short new value in place of a fact's old one: (fact, "Jim") ->
    "my cat is called Jim". None when the fact has no clear place for it."""
    if not value or len(value.split()) > SPLICE_WORDS:
        return None
    head = subject_of(fact)
    if head is None:
        return None
    # "It's called Moonleaf" for "my orchid is called Silverleaf": the fact
    # already has the verb, so only the name goes in ("called called" never).
    verb = re.match(r"(called|named|known as)\s+(?=\S)", value, re.I)
    if verb and re.search(rf"\b{re.escape(verb[1])}$", head, re.I):
        value = value[verb.end():]
    return f"{head} {value}"
