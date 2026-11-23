"""Everyday spoken tools that run without the language model."""

import importlib.util
import json
import os
import re
import time
import zoneinfo
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from . import clock_epoch
from .network_policy import INTERNET, load_policy
from .reminder_language import spoken_rule
from .reminders import Reminders
from .storage import atomic_write
from .weather import LookupDenied, Weather, normalize_location

WEATHER_PERMISSION = "Weather needs Internet mode and permission for web lookups in setup."


def speakable_time(hour, minute):
    if hour < 12:
        part = "in the morning"
    elif hour < 18:
        part = "in the afternoon"
    elif hour < 22:
        part = "in the evening"
    else:
        part = "at night"
    return f"{hour % 12 or 12}:{minute:02d} {part}"


def speakable_date(day):
    return f"{day:%A, %B} {day.day}, {day.year}"


def speakable_duration(seconds):
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [f"{count} {unit}{'s' if count != 1 else ''}"
             for count, unit in ((hours, "hour"), (minutes, "minute"), (seconds, "second")) if count]
    return " and ".join(parts)


_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(number):
    """0 to 999 in words, as Flite should say them in a sentence."""
    if not 0 <= number < 1000:
        return str(number)
    if number < 20:
        return _ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(number, 100)
    return f"{_ONES[hundreds]} hundred" + (f" and {number_words(rest)}" if rest else "")


def timer_name(seconds):
    """'thirty-minute timer', 'two-hour timer', or 'timer for one hour and ten minutes'."""
    if not seconds or seconds <= 0:
        return "timer"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    units = [(count, unit) for count, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second")) if count]
    if len(units) == 1:
        count, unit = units[0]
        return f"{number_words(count)}-{unit} timer"
    spoken = " and ".join(f"{number_words(count)} {unit}{'s' if count != 1 else ''}" for count, unit in units)
    return f"timer for {spoken}"


def lateness(seconds):
    """'12 minutes', 'an hour', for how late a delivery is."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return "a minute" if minutes <= 1 else f"{number_words(minutes)} minutes"
    hours = round(minutes / 60)
    if hours < 24:
        return "an hour" if hours == 1 else f"{number_words(hours)} hours"
    days = round(hours / 24)
    return "a day" if days == 1 else f"{number_words(min(days, 999))} days"


LATE_AFTER = 90.0  # seconds past due before a delivery is worded as late
ANNOUNCE_WORDS = 20  # most words in one ring


def spoken_length(seconds):
    """'ten minutes', 'one hour and thirty minutes': a length for the voice."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{number_words(count)} {unit}{'s' if count != 1 else ''}"
             for count, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second")) if count]
    return " and ".join(parts) or "no time"


def time_left(seconds):
    """How long until something, as a person says it: exact when it is close."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "less than a second" if seconds == 0 else f"{number_words(seconds)} second{'s' if seconds != 1 else ''}"
    if seconds < 600:
        minutes, secs = divmod(seconds, 60)
        whole = f"{number_words(minutes)} minute{'s' if minutes != 1 else ''}"
        return f"{whole} and {number_words(secs)} second{'s' if secs != 1 else ''}" if secs else whole
    if seconds < 3600:
        return f"about {number_words(round(seconds / 60))} minutes"
    hours, minutes = divmod(round(seconds / 60), 60)
    if hours >= 48:
        return f"about {number_words(min(round(hours / 24), 999))} days"
    whole = "one hour" if hours == 1 else f"{number_words(hours)} hours"
    return f"about {whole} and {spoken_length(minutes * 60)}" if minutes else f"about {whole}"


def _fit(parts, limit=ANNOUNCE_WORDS):
    """Join optional and required sentence parts within `limit` words.

    `parts` is a list of (text, priority). Parts with priority 0 always stay;
    higher numbers are dropped first. The last required part (the task) is
    cut to fit as a last resort.
    """
    def words(chosen):
        return sum(len(text.split()) for text, _ in chosen)

    chosen = list(parts)
    for level in sorted({priority for _, priority in parts if priority}, reverse=True):
        if words(chosen) <= limit:
            break
        chosen = [part for part in chosen if part[1] != level]
    line = "".join(text for text, _ in chosen)
    tokens = line.split()
    if len(tokens) > limit:
        tokens = tokens[:limit]
        # Never end on a dangling little word: "near the." says nothing.
        while len(tokens) > 1 and tokens[-1].casefold().strip(",.:;") in _DANGLING:
            tokens.pop()
        line = " ".join(tokens).rstrip(",.:;") + "."
    return line[:1].upper() + line[1:]


_DANGLING = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "at", "for", "with", "near", "by", "from",
             "my", "your", "then", "but", "so", "that", "this", "is"}


def _task(text):
    """A task said back to its owner: "take my tablets" becomes "take your tablets"."""
    from .commands import reflect

    return reflect(text).rstrip(".!? ")


# -- help, kept to what is really installed -------------------------------

def _installed(module):
    try:
        return importlib.util.find_spec(f"{__package__}.{module}") is not None
    except (ImportError, ValueError):
        return False


@lru_cache(maxsize=None)
def has_feature(module):
    return _installed(module)


# Each group: (modules that must exist, or () for always, spoken line of at most 25 words).
HELP_GROUPS = {
    "time": ((), "Ask the time here or in a city, like London, or the time difference. "
                 "I keep timers, alarms and reminders, and they can repeat."),
    "timers": ((), "Say, set a timer for ten minutes, how long left on my timer, or cancel my timer. "
                   "When it rings, say okay, or snooze."),
    "alarms": ((), "Say, wake me up at 7, or set an alarm for 6:30 tomorrow. "
                   "When it rings, say I'm up, or five more minutes."),
    "reminders": ((), "Say, remind me to call Mum tomorrow at 9 am, or, every weekday at 7, "
                      "remind me to take my tablets."),
    "memory": ((), "Say, remember that my orchid is called Silverleaf. Then ask what I remember about it. "
                   "Say forget about it, and I really forget."),
    "lists": (("lists",), "Say, add milk to my shopping list, what's on it, or cross off milk. "
                          "Make a note works too. Not in keep-nothing mode."),
    "body": ((), "Ask how's your battery, how warm are you, or are you okay. "
                 "Say go to sleep, or shut down."),
    "terminal": (("app_commands",), "Say, open the terminal, write a script that counts down from ten, "
                                     "then run it. Or say, show me the clock."),
    "fun": ((), "Ask a sum, like 17 times 23, a conversion, or how to spell a word. "
                "Ask for a joke, or say let's play a game."),
    "voice": ((), "Say speak up, be quieter, or set your volume to 5. Ask what did you hear. "
                  "Or type: F2, then 6, or Y once installed."),
    "dreams": ((), "With my lid closed and on power, I go over the day. Ask me what I dreamed about."),
    "privacy": ((), "Say, start guest mode, for visitors, or don't remember this. "
                    "Closing my lid stops my eyes, ears and camera."),
}
_HELP_WORDS = {
    "time": "time times clock clocks the time time zones zones cities world date dates",
    "timers": "timer timers countdown countdowns",
    "alarms": "alarm alarms waking wake up waking up mornings",
    "reminders": "reminder reminders routines routine repeats",
    "memory": "memory memories remembering forgetting facts your memory",
    "lists": "list lists shopping shopping list shopping lists to do to-do to do list notes note",
    "body": "your body body battery yourself health system the system your health your battery heat power",
    "terminal": "terminal the terminal workshop programs programming scripts script apps code coding computers "
                "a computer the computer",
    "fun": "games game sums maths math numbers conversions units jokes fun spelling calculations",
    "voice": "voice your voice hearing sound volume typing ears listening",
    "dreams": "dreams dreaming sleep",
    "privacy": "guests guest guest mode privacy visitors",
}
HELP_OVERVIEW = "I keep the time, timers, alarms{lists}. I remember what you tell me and look after myself. " \
                "Ask what else I can do."
_MORE = (
    ("calc", "do sums"), ("units", "convert units"), ("play", "play games"),
    ("app_commands", "run a terminal"), ("lists", "keep notes"),
)


def help_group(name):
    """The group a spoken topic names ("the terminal", "lists"), or None."""
    said = " ".join(re.sub(r"[^\w\s-]", " ", name.casefold()).split())
    said = re.sub(r"^(?:the |my |your |a )", "", said)
    if not said:
        return None
    for group, words in _HELP_WORDS.items():
        # Whole words only: "it" is not in "cities", nor "me" in "time".
        if any(re.search(rf"(?:^| ){re.escape(form)}(?: |$)", words) for form in {said, said.removesuffix("s")}
               if form):
            return group
    return None


def help_reply(kind, arg=""):
    """What it says to "what can you do?", "what else?" and "what can you do with lists?"."""
    if kind == "skills":
        return HELP_OVERVIEW.format(lists=", reminders and lists" if has_feature("lists") else " and reminders")
    if kind == "skills_more":
        things = [words for module, words in _MORE if has_feature(module)]
        things.append("dream with my lid closed")
        listed = ", ".join(things[:-1]) + f" and {things[-1]}" if len(things) > 1 else things[0]
        return f"I can also {listed}. Ask me about one."
    group = help_group(arg)
    if group is None or not all(has_feature(module) for module in HELP_GROUPS[group][0]):
        return "I'm not sure I can do that yet. Ask, what can you do, to hear the main things."
    return HELP_GROUPS[group][1]


# -- the time elsewhere ---------------------------------------------------

# Places people name that are not zone names. Zone cities ("London",
# "Tokyo", "New York") are found by find_zone itself.
PLACE_ZONES = {
    "uk": "Europe/London", "england": "Europe/London", "britain": "Europe/London", "great britain": "Europe/London",
    "scotland": "Europe/London", "wales": "Europe/London", "ireland": "Europe/Dublin", "france": "Europe/Paris",
    "germany": "Europe/Berlin", "spain": "Europe/Madrid", "italy": "Europe/Rome", "greece": "Europe/Athens",
    "netherlands": "Europe/Amsterdam", "holland": "Europe/Amsterdam", "poland": "Europe/Warsaw",
    "india": "Asia/Kolkata", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "china": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "japan": "Asia/Tokyo", "korea": "Asia/Seoul", "south korea": "Asia/Seoul", "thailand": "Asia/Bangkok",
    "vietnam": "Asia/Ho_Chi_Minh", "indonesia": "Asia/Jakarta", "bali": "Asia/Makassar",
    "philippines": "Asia/Manila", "new zealand": "Pacific/Auckland", "wellington": "Pacific/Auckland",
    "fiji": "Pacific/Fiji", "hawaii": "Pacific/Honolulu", "canberra": "Australia/Sydney",
    "gold coast": "Australia/Brisbane", "cairns": "Australia/Brisbane", "alice springs": "Australia/Darwin",
    "san francisco": "America/Los_Angeles", "california": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles", "washington": "America/New_York", "washington dc": "America/New_York",
    "boston": "America/New_York", "miami": "America/New_York", "atlanta": "America/New_York",
    "texas": "America/Chicago", "dallas": "America/Chicago", "houston": "America/Chicago",
    "montreal": "America/Toronto", "ottawa": "America/Toronto", "dubai": "Asia/Dubai", "egypt": "Africa/Cairo",
    "south africa": "Africa/Johannesburg", "cape town": "Africa/Johannesburg", "kenya": "Africa/Nairobi",
    "brazil": "America/Sao_Paulo", "rio": "America/Sao_Paulo", "rio de janeiro": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires", "buenos aires": "America/Argentina/Buenos_Aires",
    "mexico": "America/Mexico_City", "peru": "America/Lima", "russia": "Europe/Moscow", "turkey": "Europe/Istanbul",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "taiwan": "Asia/Taipei",
}
_HERE = {"here", "home", "us", "me", "you", "where you are", "where i am", "my place", "this place"}


def place_zone(name):
    """(the place as said, ZoneInfo) for a spoken place, or None."""
    said = " ".join(re.sub(r"[^\w\s'-]", " ", name).split())
    said = re.sub(r"^(?:the )", "", said, flags=re.I)
    if not said or len(said) > 60:
        return None
    key = said.casefold()
    if key in PLACE_ZONES:
        return said, ZoneInfo(PLACE_ZONES[key])
    zone = find_zone(said)
    if zone is None:
        return None
    if "/" not in zone.key and key not in ("utc", "gmt"):
        # Legacy names ("EST", "NZ") are found above or not at all: "EST"
        # would answer without daylight saving.
        return None
    return (said.upper() if key in ("utc", "gmt") else said), zone


def _offset_words(seconds):
    """'eleven hours', 'five and a half hours', 'forty-five minutes'."""
    minutes = abs(int(seconds)) // 60
    hours, rest = divmod(minutes, 60)
    if rest == 30 and hours:
        return f"{number_words(hours)} and a half hours"
    if rest and not hours:
        return f"{number_words(rest)} minutes"
    whole = "one hour" if hours == 1 else f"{number_words(hours)} hours"
    return f"{whole} and {number_words(rest)} minutes" if rest else whole


def _place_title(said):
    return " ".join(word if word.isupper() else word[:1].upper() + word[1:] for word in said.split())




@lru_cache(maxsize=1)
def zone_keys():
    """Every installed zone name, including regions reached through links.

    Buildroot's tzdata keeps the zones under posix/ and right/ and links each
    region from the top level (Australia -> posix/Australia). The standard
    available_timezones() follows no directory links, so on the device it
    lists none of the regional zones.
    """
    keys = set(available_timezones())
    for root in zoneinfo.TZPATH:
        if not os.path.isdir(root):
            continue
        seen = set()
        for directory, folders, files in os.walk(root, followlinks=True):
            real = os.path.realpath(directory)
            if real in seen:
                folders[:] = []
                continue
            seen.add(real)
            if directory == root:
                folders[:] = [name for name in folders if name not in ("posix", "right")]
            for name in files:
                path = os.path.join(directory, name)
                try:
                    with open(path, "rb") as zone:
                        if zone.read(4) != b"TZif":
                            continue
                except OSError:
                    continue
                keys.add(os.path.relpath(path, root).replace(os.sep, "/"))
    return frozenset(keys)


def find_zone(value):
    def words(key):
        return tuple(word for word in key.replace("/", " ").replace("_", " ").casefold().split() if word != "slash")

    if not value or len(value) > 100:
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        wanted = words(value)
        if not wanted:
            return None
        matches = [key for key in zone_keys() if words(key) == wanted]
        if not matches:
            # A city on its own ("Sydney") when exactly one zone ends with it.
            # Legacy aliases (Australia/NSW, Etc/...) never make a city ambiguous.
            matches = [key for key in zone_keys() if "/" in key and not key.startswith(("Etc/", "SystemV/"))
                       and words(key.rsplit("/", 1)[1]) == wanted]
        return ZoneInfo(matches[0]) if len(matches) == 1 else None


def factory_zone(config_dir=None):
    """The owner's city from the factory configuration, if the image sets one."""
    from .profile import FACTORY_CONFIG

    try:
        settings = json.loads((Path(config_dir or FACTORY_CONFIG) / "being.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    zone = settings.get("time_zone") if isinstance(settings, dict) else None
    return find_zone(zone) if isinstance(zone, str) and zone else None


class Skills:
    def __init__(self, state_dir):
        self.path = state_dir / "skills.json"
        try:
            settings = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        zone = settings.get("time_zone")
        self.zone = find_zone(zone) if isinstance(zone, str) and zone else None
        # The owner's own choice wins; otherwise the zone the image was built for.
        self.zone_known = self.zone is not None
        if self.zone is None and not (isinstance(zone, str) and "time_zone" in settings and zone == ""):
            self.zone = factory_zone()
            self.zone_known = self.zone is not None
        self.location = normalize_location(settings.get("weather_location", ""))
        self.weather = Weather(permitted=self._web_lookups_permitted)
        self._reminders = None

    def _web_lookups_permitted(self):
        policy = load_policy(self.path.parent)
        if not policy.permits_web_lookup:
            return False
        if self.path.parent == Path("/mind"):
            from .network_enforce import runtime_state

            return policy.effective_mode == INTERNET and runtime_state()["mode"] == INTERNET
        return True

    @property
    def reminders(self):
        if self._reminders is None:
            self._reminders = Reminders(self.path.parent)
        return self._reminders

    def close(self):
        if self._reminders is not None:
            self._reminders.close()
            self._reminders = None

    def now(self):
        return datetime.now(self.zone)

    def _save(self, location, zone):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = {"weather_location": location, "time_zone": zone.key if zone else ""}
        atomic_write(self.path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
        self.location, self.zone = location, zone
        self.zone_known = zone is not None

    def set_zone(self, zone):
        """Save the owner's time zone (a ZoneInfo, or None for the device clock)."""
        self._save(self.location, zone)

    def _due_description(self, timestamp):
        due = datetime.fromtimestamp(timestamp, self.zone)
        day = self.now().date()
        if due.date() == day:
            label = "today"
        elif due.date() == day + timedelta(days=1):
            label = "tomorrow"
        else:
            label = f"{due:%A, %B} {due.day}"
            if due.year != day.year:
                label += f", {due.year}"
        return f"{label} at {speakable_time(due.hour, due.minute)}"

    def clock_doubted(self):
        """The wall clock cannot be believed: reset before 2026, or moved up to systemd's epoch."""
        return clock_epoch.doubted()

    def reminders_today(self, now=None):
        """Reminders and alarms still to come today in the owner's day, for the morning brief."""
        now = self.now() if now is None else now
        start = now.timestamp()
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
        return self.reminders.between(start, tomorrow.timestamp())

    async def reply(self, command, body_clock=None, *, clock_trusted=None):
        """The spoken answer to a command handled here. Never raises for a known kind.

        `clock_trusted` is the mind's own judgement of the clock; None asks
        clock_epoch. It matters for the time elsewhere and for new alarms
        and reminders, which are held while the clock is doubted.
        """
        kind, arg = command.kind, command.arg
        if kind == "time":
            now = self.now()
            hour, minute = body_clock if body_clock is not None and self.zone is None else (now.hour, now.minute)
            return f"It's {speakable_time(hour, minute)}."
        if kind == "day":
            return f"It's {self.now():%A}."
        if kind == "date":
            return f"It's {speakable_date(self.now().date())}."
        if kind == "date_tomorrow":
            tomorrow = self.now().date() + timedelta(days=1)
            return f"Tomorrow is {speakable_date(tomorrow)}."
        if kind in ("skills", "skills_more", "skills_group"):
            return help_reply(kind, arg)
        if kind in ("world_time", "time_difference"):
            trusted = (not self.clock_doubted()) if clock_trusted is None else clock_trusted
            return self._elsewhere(kind, arg, trusted)
        if kind == "snooze":
            return self._snooze(arg)
        if kind == "ack":
            return self._acknowledge()
        if kind == "diary":
            # The mind answers this from the day record; this is only its fallback.
            return "I don't have a record of that day yet. I write one each night."
        if kind in ("weather_location", "clear_weather_location"):
            location = normalize_location(arg)
            if kind == "weather_location" and not location:
                return "Use a city name, for example Sydney, Australia."
            self._save(location, self.zone)
            return f"I saved {location} as your default weather city." if location else "I cleared the weather location."
        if kind in ("clock_zone", "device_zone"):
            zone = find_zone(arg)
            if kind == "clock_zone" and zone is None:
                return "Use a time zone name, for example Australia slash Sydney."
            self._save(self.location, zone)
            return "I've changed the time zone for clock answers and new reminders." if zone else "I'll use the device time zone."
        if kind.startswith("weather_"):
            if not self._web_lookups_permitted():
                return WEATHER_PERMISSION
            try:
                return await self.weather.reply(kind.removeprefix("weather_"), arg or self.location)
            except LookupDenied:
                return WEATHER_PERMISSION
        if command.reminder is None:
            return "I can't do that one yet."
        trusted = (not self.clock_doubted()) if clock_trusted is None else clock_trusted
        return self._reminder_reply(command.reminder, clock_trusted=trusted)

    # -- the time elsewhere ---------------------------------------------

    def _elsewhere(self, kind, arg, trusted):
        if not trusted:
            return "I'm not sure of the time right now, so I can't work that out. Tell me the time first."
        places = [arg]
        between = re.fullmatch(r"(?P<a>.+?) and (?P<b>.+)", arg.strip(), re.I) if kind == "time_difference" else None
        if between:
            places = [between["a"], between["b"]]
        found = []
        for place in places:
            if place.strip().casefold() in _HERE:
                found.append(("you", self.zone))
                continue
            located = place_zone(place)
            if located is None:
                return f"I don't know the time zone for {place.strip()}. Try the nearest big city."
            found.append((_place_title(located[0]), located[1]))
        here = self.now()
        if kind == "world_time":
            name, zone = found[0]
            if zone is None:
                return f"It's {speakable_time(here.hour, here.minute)}."
            there = datetime.now(zone)
            line = f"In {name} it's {speakable_time(there.hour, there.minute)}"
            if self.zone is not None and there.date() != here.date():
                line += f", {there:%A}"
            return line + "."
        if len(found) == 1:
            found.insert(0, ("you", self.zone))
        (first, first_zone), (second, second_zone) = found
        if None in (first_zone, second_zone):
            return "I don't know your time zone yet. Set it in setup, or say, set my time zone to Sydney."
        moment = datetime.now(first_zone)
        gap = moment.astimezone(second_zone).utcoffset() - moment.utcoffset()
        seconds = gap.total_seconds()
        if not seconds:
            return f"{second} is on the same time as {first}." if first != "you" else f"{second} is on the same time as you."
        direction = "ahead of" if seconds > 0 else "behind"
        return f"{second} is {_offset_words(seconds)} {direction} {first}."

    # -- a ringing item ---------------------------------------------------

    def _snooze(self, arg):
        entry = self.reminders.ringing(time.time())
        if entry is None:
            return "Nothing is ringing right now."
        minutes = int(arg) if str(arg).isdigit() else 10
        self.reminders.snooze(entry.id, max(1, min(minutes, 120)))
        return f"Okay, I'll ring again in {spoken_length(minutes * 60)}."

    def _acknowledge(self):
        entry = self.reminders.ringing(time.time())
        if entry is None:
            return "Nothing is ringing right now. To remove one, say, cancel my alarm."
        self.reminders.acknowledge(entry.id)
        if entry.rule is not None and (moved := self.reminders.get(entry.id)) is not None:
            return f"Okay. The next one is {self._due_description(moved.due_at)}."
        return "Okay."

    # -- rings ------------------------------------------------------------

    def announcement(self, entry, owner, *, now=None, rings=0, slept=False):
        """What to say when a timer, reminder or alarm is due: at most 20 words, honest about lateness.

        `rings` is how many times it was already said; `slept` says the delay
        came from the lid being closed or the being asleep.
        """
        now = time.time() if now is None else now
        delivery = self.reminders.delivery(entry.id)
        late = now - entry.due_at
        name = (f"{owner}, ", 2) if owner else ("", 0)
        again = ("Again: ", 0) if rings else ("", 0)
        if entry.kind == "timer":
            what = timer_name(delivery.duration if delivery else None)
            if late > LATE_AFTER:
                return _fit([again, name, (f"your {what} finished {lateness(late)} ago", 0),
                             (" while my lid was closed" if slept else "", 1), (". Sorry I'm late.", 3)])
            return _fit([again, name, (f"your {what} is done.", 0)])
        if entry.kind == "alarm":
            due = datetime.fromtimestamp(entry.due_at, self.zone)
            current = datetime.fromtimestamp(now, self.zone)
            if late > LATE_AFTER:
                why = (", because my lid was closed" if slept else "", 1)
                return _fit([again, name, (f"sorry, your alarm for {speakable_time(due.hour, due.minute)} is "
                                           f"{lateness(late)} late", 0), why, (".", 0)])
            if 4 <= current.hour < 12:
                return _fit([again, ("Good morning", 0), (f", {owner}" if owner else "", 2),
                             (f". It's {speakable_time(current.hour, current.minute)}. Time to get up.", 0)])
            return _fit([again, name, (f"it's {speakable_time(current.hour, current.minute)}. This is your alarm.", 0)])
        task = (f"{_task(entry.text)}.", 0)
        if late > LATE_AFTER:
            why = (" because my lid was closed", 1) if slept else ("", 0)
            return _fit([again, name, (f"sorry, this reminder is {lateness(late)} late", 0), why, (": ", 0), task])
        full = [again, name, ("you asked me to remind you: ", 0), task]
        if sum(len(text.split()) for text, _ in full) > ANNOUNCE_WORDS:
            full = [again, name, ("a reminder: ", 0), task]
        return _fit(full)

    def _delivery_note(self, due_at, kind="reminder"):
        """Say at creation if the item will not be spoken when it is due."""
        from .initiative import load_policy as load_initiative

        policy = load_initiative(self.path.parent)
        if kind == "alarm":
            return " Leave my lid open so I can ring."
        if not policy.reminders:
            return " Automatic reminders are switched off, so I'll only tell you if you ask."
        if policy.is_quiet(datetime.fromtimestamp(due_at, self.zone)):
            return " That's in your quiet hours, so I'll say it when they end."
        return ""

    # -- creating, listing and cancelling ---------------------------------

    def _when(self, entry):
        if entry.rule is not None and entry.at_local:
            hour, minute = (int(part) for part in entry.at_local.split(":"))
            return f"{spoken_rule(entry.rule)} at {speakable_time(hour, minute)}"
        return self._due_description(entry.due_at)

    def _reminder_reply(self, request, *, clock_trusted=True):
        if request.kind == "clarify":
            return request.message
        if request.kind == "left":
            return self._left_reply(request.item_kind or "timer")
        if request.kind in ("create", "timer", "alarm"):
            kind = {"timer": "timer", "alarm": "alarm"}.get(request.kind, "reminder")
            duration = request.duration_seconds if kind == "timer" and request.duration_seconds else None
            relative = kind == "timer" or request.relative
            entry = self.reminders.add(request.task, request.due_at, kind=kind, duration=duration,
                                       rule=request.rule, at_local=request.at_local,
                                       zone=self.zone if request.rule or not clock_trusted else None,
                                       relative=relative, held=not clock_trusted)
            running = len(self.reminders.upcoming(kind=kind))
            # A delay counts real seconds and rings anyway; a clock time is
            # held and worked out again when the owner sets the clock.
            held = "" if clock_trusted or relative else \
                " My clock may be wrong, so I'll hold it until you tell me the time."
            note = held or self._delivery_note(entry.due_at, kind)
            if kind == "timer":
                line = f"Okay, {spoken_length(request.duration_seconds)}, starting now."
                if running > 1:
                    line += f" That's timer {entry.id}; you have {number_words(running)} running."
                return line + note
            if kind == "alarm":
                line = f"Okay, your alarm will ring {self._when(entry)}." if entry.rule else \
                    f"Okay, your alarm is set for {self._due_description(entry.due_at)}."
                if running > 1:
                    line += f" That's alarm {entry.id}."
                return line + note
            if entry.rule is not None:
                line = f"Okay. {self._when(entry)[:1].upper()}{self._when(entry)[1:]}, I'll remind you to {_task(entry.text)}."
                if running > 1:
                    line += f" That's reminder {entry.id}."
                return line + note
            return f"Reminder {entry.id} set for {self._due_description(entry.due_at)}.{note}"
        kind = request.item_kind
        if request.kind == "list":
            entries = self.reminders.upcoming(6, kind=kind)
            if not entries:
                return f"You have no pending {kind + 's' if kind else 'reminders or timers'}."
            parts = []
            now = time.time()
            for entry in entries[:5]:
                if entry.due_at <= now:
                    when = "overdue"
                elif entry.kind == "timer":
                    when = f"{time_left(entry.due_at - now)} left"  # a countdown, not a clock time
                else:
                    when = self._when(entry)
                task = f", {_task(entry.text)}" if entry.kind == "reminder" else ""
                parts.append(f"{entry.kind.capitalize()} {entry.id}{task}, {when}.")
            if len(entries) > 5:
                parts.append("There are more pending.")
            return " ".join(parts)
        if request.reminder_id is None:
            matches = self._targets(request.task, kind)
            if len(matches) > 1:
                return f"More than one {kind} matches. Say the {kind} number to cancel it."
            entry_id = matches[0].id if matches else None
        else:
            entry_id = request.reminder_id
        if entry_id is not None and self.reminders.cancel(entry_id, kind=kind):
            return f"Cancelled {kind or 'reminder'} {entry_id}."
        return f"I couldn't find that pending {kind or 'reminder'}."

    def _targets(self, task, kind):
        """The entries a spoken cancel means: an exact task, then its words ("the reminder about Mum")."""
        if kind == "alarm":
            alarms = self.reminders.upcoming(kind="alarm")
            return alarms if len(alarms) <= 1 or not task else self.reminders.matching(task, kind=kind) or alarms
        matches = self.reminders.matching(task, kind=kind)
        if matches or not task:
            return matches
        words = [word for word in re.sub(r"^(?:about|for|to|with|that) ", "", task.casefold()).split()
                 if word not in ("the", "a", "my", "to", "about")]
        return self.reminders.mentioning(words, kind=kind)

    def _left_reply(self, kind):
        now = time.time()
        entries = [entry for entry in self.reminders.upcoming(kind=kind)]
        if kind == "timer":
            if not entries:
                return "You don't have a timer running."
            parts = []
            for entry in entries[:2]:
                left = entry.due_at - now
                delivery = self.reminders.delivery(entry.id)
                name = timer_name(delivery.duration if delivery else None)
                if len(entries) == 1:
                    return "It's already done." if left <= 0 else f"{time_left(left)[:1].upper()}{time_left(left)[1:]} left."
                parts.append(f"your {name} is done" if left <= 0 else f"your {name} has {time_left(left)} left")
            line = "; ".join(parts)
            if len(entries) > 2:
                line += f", and {number_words(len(entries) - 2)} more"
            return line[:1].upper() + line[1:] + "."
        noun = "alarm" if kind == "alarm" else "reminder"
        if not entries:
            return f"You don't have any {noun}s set."
        entry = entries[0]
        left = entry.due_at - now
        if left <= 0:
            return f"Your {noun} is due now." if kind == "alarm" else f"Your reminder to {_task(entry.text)} is due now."
        when = self._due_description(entry.due_at)
        if kind == "alarm":
            return f"Your alarm is set for {when}. That's {time_left(left)} from now."
        return f"Your next reminder is {when}, in {time_left(left)}: {_task(entry.text)}."
