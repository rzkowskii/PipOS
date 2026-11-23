"""Everyday spoken tools that run without the language model."""

import json
import os
import time
import zoneinfo
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .network_policy import INTERNET, load_policy
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

    async def reply(self, command, body_clock=None):
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
        if kind == "skills":
            return ("Ask me for the time, date, weather, or my battery level. "
                    "I can set timers and reminders and remember things you tell me.")
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
        return self._reminder_reply(command.reminder)

    def announcement(self, entry, owner, *, now=None, rings=0, slept=False):
        """What to say when a reminder or timer is due: human, honest about lateness.

        `rings` is how many times it was already said; `slept` says the delay
        came from the lid being closed or the being asleep.
        """
        now = time.time() if now is None else now
        delivery = self.reminders.delivery(entry.id)
        late = now - entry.due_at
        name = f"{owner}, " if owner else ""
        again = "Again: " if rings else ""
        if entry.kind == "timer":
            what = timer_name(delivery.duration if delivery else None)
            if late > LATE_AFTER:
                why = " while my lid was closed" if slept else ""
                return f"{again}{name}your {what} finished {lateness(late)} ago{why}. Sorry I'm late."
            return f"{again}{name}your {what} is done."
        if late > LATE_AFTER:
            due = datetime.fromtimestamp(entry.due_at, self.zone)
            why = ", because my lid was closed" if slept else ""
            return (f"{again}{name}this is {lateness(late)} late{why}. At {speakable_time(due.hour, due.minute)} "
                    f"you wanted me to remind you: {entry.text}.")
        return f"{again}{name}you asked me to remind you: {entry.text}."

    def _delivery_note(self, due_at):
        """Say at creation if the reminder will not be spoken when it is due."""
        from .initiative import load_policy as load_initiative

        policy = load_initiative(self.path.parent)
        if not policy.reminders:
            return " Automatic reminders are switched off, so I'll only tell you if you ask."
        if policy.is_quiet(datetime.fromtimestamp(due_at, self.zone)):
            return " That's in your quiet hours, so I'll say it when they end."
        return ""

    def _reminder_reply(self, request):
        if request.kind == "clarify":
            return request.message
        if request.kind in ("create", "timer"):
            kind = "timer" if request.kind == "timer" else "reminder"
            duration = request.duration_seconds if kind == "timer" and request.duration_seconds else None
            entry = self.reminders.add(request.task, request.due_at, kind=kind, duration=duration)
            note = self._delivery_note(entry.due_at)
            if kind == "timer":
                return f"Timer {entry.id} started for {speakable_duration(request.duration_seconds)}.{note}"
            return f"Reminder {entry.id} set for {self._due_description(entry.due_at)}.{note}"
        kind = request.item_kind
        if request.kind == "list":
            entries = self.reminders.upcoming(6, kind=kind)
            if not entries:
                return f"You have no pending {kind + 's' if kind else 'reminders or timers'}."
            parts = []
            for entry in entries[:5]:
                when = "overdue" if entry.due_at <= self.now().timestamp() else self._due_description(entry.due_at)
                task = f", {entry.text}" if entry.kind == "reminder" else ""
                parts.append(f"{entry.kind.capitalize()} {entry.id}{task}, {when}.")
            if len(entries) > 5:
                parts.append("There are more pending.")
            return " ".join(parts)
        if request.reminder_id is None:
            matches = self.reminders.matching(request.task, kind=kind)
            if len(matches) > 1:
                return f"More than one {kind} matches. Say the {kind} number to cancel it."
            entry_id = matches[0].id if matches else None
        else:
            entry_id = request.reminder_id
        if entry_id is not None and self.reminders.cancel(entry_id, kind=kind):
            return f"Cancelled {kind or 'reminder'} {entry_id}."
        return f"I couldn't find that pending {kind or 'reminder'}."
