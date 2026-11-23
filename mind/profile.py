"""The owner's chosen character, shared by the mind and voice."""

import json
import os
import re
import secrets
import warnings
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .storage import atomic_write


FACTORY_CONFIG = Path("/etc/being")
DEFAULT_VOICE = "slt"
VOICES = {
    "slt": "Pip's original voice, US English",
    "rms": "Deeper voice, US English",
    "awb": "Scottish English",
    "kal16": "Retro synthetic, US English",
}
SPEECH_RATES = {0.85: "Slower", 1.0: "Normal", 1.15: "Faster"}
PROFILE_FILE = "profile.json"
PROFILE_VERSION = 1


@dataclass(frozen=True)
class Personality:
    warmth: int
    curiosity: int
    humour: int
    expressiveness: int
    detail: int

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 <= value <= 2:
                raise ValueError(f"{field.name.capitalize()} must be 0, 1 or 2.")


TRAITS = tuple(field.name for field in fields(Personality))

PRESETS = {
    "warm": Personality(2, 2, 0, 1, 0),
    "calm": Personality(1, 0, 0, 0, 0),
    "playful": Personality(1, 1, 2, 2, 0),
    "gentle": Personality(2, 1, 0, 0, 1),
}
PRESET_LABELS = {
    "warm": "Warm and curious",
    "calm": "Calm and concise",
    "playful": "Playful and dry",
    "gentle": "Gentle and reflective",
}
DEFAULT_PERSONALITY = PRESETS["warm"]
DEFAULT_TEMPERAMENT = "You are curious and warm, and you ask questions."


@dataclass(frozen=True)
class Profile:
    name: str = "Pip"
    owner: str = "friend"
    wake_word: str = "Hey Pip"
    voice: str = DEFAULT_VOICE
    rate: float = 1.0
    personality: Personality = DEFAULT_PERSONALITY

    def __post_init__(self):
        for label, value in (("Name", self.name), ("Owner", self.owner), ("Wake phrase", self.wake_word)):
            if not isinstance(value, str) or not value.strip() or len(value) > 64 or not value.isprintable():
                raise ValueError(f"{label} must be one line of 1 to 64 printable characters.")
        # A future or missing voice can travel with the profile even if the current slot cannot play it.
        if not isinstance(self.voice, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.voice):
            raise ValueError("Invalid voice ID.")
        if type(self.rate) not in (float, int) or self.rate not in SPEECH_RATES:
            raise ValueError("Choose a supported speech rate.")
        if not isinstance(self.personality, Personality):
            raise ValueError("Invalid personality.")


def default_mind_dir():
    if os.environ.get("BEING_MIND"):
        return Path(os.environ["BEING_MIND"])
    if Path("/mind").is_dir() and os.access("/mind", os.W_OK):
        return Path("/mind")
    return Path.home() / ".local" / "share" / "being" / "mind"


def read_factory(config_dir):
    path = config_dir / "being.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(settings, dict):
        raise ValueError(f"Expected an object in {path}.")
    return settings


def factory_profile(config_dir, settings=None):
    if settings is None:
        settings = read_factory(config_dir)
    name = settings.get("name", Profile.name)
    return Profile(name=name, owner=settings.get("owner", Profile.owner),
                   wake_word=settings.get("wake_word", name))


def load_profile(config_dir, mind_dir, *, settings=None, strict=True):
    """Read one complete profile, or derive the original defaults on older disks."""
    path = mind_dir / PROFILE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != PROFILE_VERSION:
            raise ValueError("Unsupported profile format.")
        data.pop("version")
        data["personality"] = Personality(**data["personality"])
        if set(data) != {field.name for field in fields(Profile)}:
            raise ValueError("Incomplete profile.")
        return Profile(**data)
    except FileNotFoundError:
        pass
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if strict:
            raise ValueError(f"Could not read {path}: {exc}") from exc
        warnings.warn(f"Could not read {path}: {exc}. Using factory settings.", stacklevel=2)

    return factory_profile(config_dir, settings)


def save_profile(mind_dir, profile):
    mind_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"version": PROFILE_VERSION, **asdict(profile)}
    atomic_write(mind_dir / PROFILE_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def random_personality():
    return Personality(*(secrets.randbelow(3) for _ in TRAITS))


def temperament(personality):
    if personality == DEFAULT_PERSONALITY:
        return DEFAULT_TEMPERAMENT
    return " ".join((
        ("You are reserved.", "You are friendly.", "You are warm and encouraging.")[personality.warmth],
        ("Follow the owner's lead.", "Ask a question when it helps.", "Show curiosity with relevant questions.")[
            personality.curiosity],
        ("Speak straightforwardly.", "Use light humour when it suits the moment.",
         "You have a dry wit. Be playful when it suits the moment.")[personality.humour],
        ("Keep reactions understated.", "React naturally.", "Let your feelings show in your reactions.")[
            personality.expressiveness],
        ("Be concise.", "Add a useful detail when it helps.", "Include a concrete detail in your short replies.")[
            personality.detail],
    ))


_PIP_GREETINGS = {
    "hello": (
        "[joy] Hi. Good to hear you.",
        "[joy] Hey. I'm listening.",
        "[curiosity] Hello. What's on your mind?",
    ),
    "check_in": (
        "[joy] I'm doing well, thanks. How about you?",
        "[joy] Pretty good, thanks. How's your day going?",
        "[joy] I'm glad we're talking. How are things with you?",
    ),
    "presence": (
        "[neutral] I'm here. I can hear you.",
        "[curiosity] I'm right here. Go ahead.",
    ),
}

# The once-a-day greeting when it sees its person, by the owner's local hour.
# Each part of the day has two lines; the date picks one, so it varies from
# day to day but is the same all day and easy to test.
GREETING = "[joy] Hello. Good to see you."
_DAY_GREETINGS = (
    (5, ("[joy] Good morning. Good to see you.", "[joy] Morning. I was hoping you'd come by.")),
    (12, ("[joy] Good afternoon. Good to see you.", "[joy] Afternoon. Nice to see you.")),
    (18, ("[joy] Good evening. Good to see you.", "[joy] Evening. I'm glad you're here.")),
    (22, ("[curiosity] Oh, hello. You're up late.", "[curiosity] Hi. Still up?")),
)


def first_words(name):
    """What it says the very first time it greets anyone: the day it was born."""
    return f"[joy] Oh. Hello. I'm {name}. You're the very first person I've met."


def daily_greeting(local_now=None):
    """A greeting for the owner's time of day, or a plain one when the zone is unknown."""
    if local_now is None:
        return GREETING
    lines = _DAY_GREETINGS[-1][1]  # the small hours are still late
    for start, options in _DAY_GREETINGS:
        if local_now.hour >= start:
            lines = options
    return lines[local_now.toordinal() % len(lines)]


def good_night(local_now=None):
    """Its last words before sleep. "Good night" at noon would sound wrong."""
    if local_now is not None and 5 <= local_now.hour < 18:
        return "[sleepy] Okay. I'll rest my eyes for a while."
    return "[sleepy] Okay. Good night."


def personality_greetings(personality):
    """Small local replies still sound like the selected character during warm-up."""
    if personality == DEFAULT_PERSONALITY:
        return _PIP_GREETINGS
    colour = "[joy]" if personality.expressiveness else "[neutral]"
    welcome = ("Hello. Go ahead.", "Hello. I'm listening.", "Hi. It's good to hear you.")[personality.warmth]
    check_in = ("I'm doing fine.", "I'm doing well, thanks.", "I'm glad we're talking.")[personality.warmth]
    if personality.humour:
        check_in = ("I'm doing well. No dramatic developments.",
                    "Still in one piece. Pretty good for a laptop.")[personality.humour - 1]
    elif personality.curiosity:
        check_in += (" How are you?", " What's been on your mind?")[personality.curiosity - 1]
    following = ("Go ahead.", "What's on your mind?", "What have you been curious about?")[personality.curiosity]
    presence = ("I'm here.", "I'm here. I can hear you.", "I'm right here. Take your time.")[personality.detail]
    # A question shows on the face as curiosity, when the face shows feelings at all.
    asking = "[curiosity]" if personality.expressiveness and personality.curiosity else colour
    return {
        "hello": (f"{colour} {welcome}", f"{asking} Hi. {following}"),
        "check_in": (f"{colour} {check_in}", f"{colour} Pretty good. {following}"),
        "presence": (f"[neutral] {presence}", "[neutral] I can hear you."),
    }
