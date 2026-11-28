"""Owner appearance settings and their boot-time copy for the face."""

import fcntl
import json
import logging
import random
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .storage import atomic_write

APPLIANCE = Path("/mind")
MIRROR = Path("/run/being/look.json")
FILE = "look.json"
MAX_BYTES = 4096
MIN_STRENGTH, MAX_STRENGTH = 0.4, 1.6
WIDGETS = {"clock_widget": "clock widget", "system_widget": "system widget", "weather_widget": "weather widget"}
WIDGET_POSITIONS = {f"{key}_position": label for key, label in WIDGETS.items()}
CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
EYE_COLOURS = ("cyan", "blue", "teal", "mint", "violet", "green", "amber", "red", "rose", "yellow", "white")
COLOUR_ALIASES = {
    "default": "cyan", "aqua": "teal", "turquoise": "teal", "purple": "violet", "indigo": "violet",
    "lime": "green", "orange": "amber", "pink": "rose", "magenta": "rose", "gold": "yellow",
    "silver": "white", "grey": "white", "gray": "white",
}
CHOICES = {
    "glow": EYE_COLOURS,
    "shape": ("soft", "sharp", "round"),
    "size": ("small", "medium", "full"),
    "menu_position": ("left", "center", "right", "off"),
    # The always-visible Quick Menu dock at the top: "fades" is always there and
    # shrinks to a pill when idle; "off" hides it (the menu keys still work).
    "dock": ("fades", "always", "off"),
    **dict.fromkeys(WIDGET_POSITIONS, CORNERS),
}
DISPLAY_FLAGS = {
    "menu_labels": "Quick Menu labels",
    "captions": "captions",
    "caption_cost": "route badge",
    "caption_heard": "heard line",
    "idle_glances": "idle glances",
}
FACE_LAYERS = {"effects": "emotes", "eye_details": "eye details", "mood_colour": "mood colour"}
FLAGS = ("brows", "mouth", *FACE_LAYERS, *WIDGETS, *DISPLAY_FLAGS)
FIELDS = (*CHOICES, "strength", *FLAGS)
LOG = logging.getLogger(__name__)


def valid(key, value):
    if key in CHOICES:
        return isinstance(value, str) and value in CHOICES[key]
    if key in FLAGS:
        return type(value) is bool
    return key == "strength" and type(value) in (int, float) and MIN_STRENGTH <= value <= MAX_STRENGTH


@dataclass(frozen=True)
class Look:
    glow: str = "cyan"
    shape: str = "soft"
    strength: float = 1.0
    size: str = "full"
    brows: bool = False
    mouth: bool = False
    revision: int = 0
    clock_widget: bool = False
    system_widget: bool = False
    weather_widget: bool = False
    menu_position: str = "center"
    menu_labels: bool = False
    dock: str = "fades"
    clock_widget_position: str = "top-left"
    system_widget_position: str = "top-right"
    weather_widget_position: str = "top-right"
    captions: bool = False
    caption_cost: bool = True
    caption_heard: bool = True
    effects: bool = False
    eye_details: bool = False
    mood_colour: bool = False
    idle_glances: bool = True

    def payload(self):
        return asdict(self)

    @classmethod
    def decode(cls, data):
        if not isinstance(data, dict):
            return cls()
        fields = {key: data[key] for key in FIELDS if key in data and valid(key, data[key])}
        rev = data.get("revision")
        if type(rev) is int and 0 <= rev < 2**53:
            fields["revision"] = rev
        return cls(**fields)

    def describe(self):
        return (f"{self.glow} eyes, {self.shape} shape, {round(self.strength * 100)} percent glow, "
                f"and {self.size} face size")

    def features(self):
        layers = {key: 'on' if getattr(self, key) else 'off' for key in FACE_LAYERS}
        return (f"My brows are {'on' if self.brows else 'off'} and my mouth is {'on' if self.mouth else 'off'}. "
                f"Emotes are {layers['effects']}, eye details are {layers['eye_details']}, "
                f"and mood colour is {layers['mood_colour']}.")


def seed(directory, look):
    """Write the selected appearance into a new installation's staging tree."""
    if not isinstance(look, Look) or Look.decode(look.payload()) != look:
        raise ValueError("Invalid installed appearance settings.")
    atomic_write(Path(directory) / FILE, Store._encoded(replace(look, revision=0)))


class Store:
    def __init__(self, state_dir, *, mirror=None):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / FILE
        self.mirror = Path(mirror) if mirror is not None else MIRROR if self.state_dir == APPLIANCE else None

    def read(self):
        try:
            with self.path.open("rb") as source:
                data = source.read(MAX_BYTES + 1)
            if len(data) <= MAX_BYTES:
                return Look.decode(json.loads(data))
        except (OSError, ValueError):
            pass
        return Look()

    def _mirror(self, look):
        if self.mirror is not None:
            try:
                self.mirror.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(self.mirror, self._encoded(look))
            except OSError as error:
                LOG.warning("could not mirror the saved look: %s", error)

    @staticmethod
    def _encoded(look):
        return json.dumps(look.payload(), separators=(",", ":")) + "\n"

    def _lock(self):
        # Setup and the mind can be running together. Serialize the read, edit,
        # and mirror so changing shape never undoes a simultaneous colour change.
        handle = (self.state_dir / ".look.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            handle.close()
            raise
        return handle

    def refresh(self):
        if self.mirror is None:
            return self.read()
        with self._lock():
            active = self.read()
            self._mirror(active)
            return active

    def set(self, key, value):
        with self._lock():
            before = self.read()
            if callable(value):
                value = value(before)
            if not valid(key, value):
                if key == "glow":
                    raise ValueError(f"I can make my eyes {', '.join(EYE_COLOURS[:-1])}, or {EYE_COLOURS[-1]}.")
                if key == "strength":
                    raise ValueError("Glow strength can be between forty and one hundred and sixty percent.")
                raise ValueError("That look setting isn't available.")
            if getattr(before, key) == value:
                return before
            active = replace(before, **{key: value}, revision=before.revision + 1)
            atomic_write(self.path, self._encoded(active))
            self._mirror(active)
            return active


# Each phrase is anchored so mentions such as "add blue paint to my list"
# or "why are your eyes blue?" cannot change the appearance.
_VOICE = (
    ("current", r"(?:what(?:'s| is) your (?:current )?look|how do you look|tell me your current look)"),
    *((field, rf"(?P<value>show|hide) (?:the )?{label}") for field, label in WIDGETS.items()),
    *((field, rf"(?:move|put) (?:the )?{label} (?:to |in |at )?(?:the )?(?P<value>(?:top|bottom)[ -](?:left|right))")
      for field, label in WIDGET_POSITIONS.items()),
    ("menu_position", r"set (?:the )?quick menu to (?P<value>left|center|centre|right|off)"),
    ("dock", r"(?P<value>show|hide) (?:the )?dock"),
    ("dock", r"set (?:the )?dock to (?P<value>always|fades|off)"),
    *((field, rf"(?P<value>show|hide) (?:the )?{label.casefold()}") for field, label in DISPLAY_FLAGS.items()),
    ("captions", r"turn (?:the )?captions (?P<value>on|off)"),
    ("idle_glances", r"(?P<value>stop|start) looking around"),
    ("brows", r"(?P<value>show|hide) (?:your )?(?:eye)?brows"),
    ("mouth", r"(?P<value>show|hide) (?:your )?mouth"),
    ("brows", r"turn (?:your )?(?:eye)?brows (?P<value>on|off)"),
    ("mouth", r"turn (?:your )?mouth (?P<value>on|off)"),
    *((key, rf"(?P<value>show|hide) (?:(?:your|the) )?{label.replace('colour', 'colou?r')}")
      for key, label in FACE_LAYERS.items()),
    *((key, rf"turn (?:(?:your|the) )?{label.replace('colour', 'colou?r')} (?P<value>on|off)")
      for key, label in FACE_LAYERS.items()),
    ("shape", r"(?:make your eyes|set your eye shape to) (?P<value>soft|sharp|square|round)"),
    ("size", r"(?:make your face|set your face size to) (?P<value>small|medium|full|large|normal|smaller|bigger|larger)"),
    ("strength", r"(?:set|change) your (?:eye )?glow(?: strength)? to (?P<value>[a-z0-9 -]+(?: percent| per cent| *%))"),
    ("strength", r"make your eyes (?P<value>brighter|dimmer)"),
    ("glow", r"(?:pick|choose|try) (?:a )?(?P<value>random) (?:eye )?colou?r"),
    ("glow", r"(?:make your eyes|(?:change|set) your eye colou?r to) (?P<value>[a-z]+)"),
)
_VOICE = [(key, re.compile(pattern)) for key, pattern in _VOICE]


def parse(said):
    said = " ".join(said.casefold().replace("’", "'").split())
    said = re.sub(r"^(?:can|could|would) you ", "", said)
    for key, pattern in _VOICE:
        if match := pattern.fullmatch(said):
            return key, match.groupdict().get("value", "")
    return None


def spoken_value(key, phrase, current, number_word):
    if key in FLAGS:
        return phrase in ("show", "on", "start")
    if key == "glow":
        if phrase == "random":
            return random.choice([colour for colour in EYE_COLOURS if colour != current.glow])
        return COLOUR_ALIASES.get(phrase, phrase)
    if key in WIDGET_POSITIONS:
        return phrase.replace(" ", "-")
    if key == "menu_position":
        return "center" if phrase == "centre" else phrase
    if key == "dock":
        return {"show": "fades", "hide": "off"}.get(phrase, phrase)
    if key == "shape":
        return "sharp" if phrase == "square" else phrase
    if key == "size":
        sizes = CHOICES[key]
        if phrase in ("smaller", "bigger", "larger"):
            step = -1 if phrase == "smaller" else 1
            return sizes[max(0, min(len(sizes) - 1, sizes.index(current.size) + step))]
        return {"large": "full", "normal": "full"}.get(phrase, phrase)
    if key == "strength":
        if phrase in ("brighter", "dimmer"):
            step = 0.2 if phrase == "brighter" else -0.2
            return round(min(MAX_STRENGTH, max(MIN_STRENGTH, current.strength + step)), 2)
        said = re.sub(r"\s*(?:per cent|percent|%)$", "", phrase).replace("-", " ").strip()
        if re.fullmatch(r"[0-9]{1,3}", said):
            return int(said) / 100
        hundred = re.fullmatch(r"(?:one|a) hundred(?: (?:and )?(.*))?", said)
        if hundred:
            rest = number_word(hundred[1]) if hundred[1] else 0
            return (100 + rest) / 100 if rest is not None else None
        value = number_word(said)
        return value / 100 if value is not None else None
    return phrase


if __name__ == "__main__":
    # The face's boot hook mirrors the data before it creates its first frame.
    # A missing mount is allowed. The face can still display its default look.
    if APPLIANCE.is_mount():
        Store(APPLIANCE).refresh()
