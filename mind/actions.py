"""Discoverable owner actions. The existing command routes execute the examples."""

from dataclasses import dataclass

from .look import DISPLAY_FLAGS, FACE_LAYERS, WIDGET_POSITIONS, WIDGETS


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    group: str
    phrases: tuple[str, ...] = ()
    keys: str = ""
    owner_only: bool = False
    panel: bool = False


SURFACE_NAMES = {"terminal": "terminal", "monitor": "monitor", "clock": "clock", "calc": "calculator",
                 "lists": "lists", "memory": "memory", "journal": "journal", "mind": "mind",
                 "lookup": "lookup"}
GROUPS = {
    "appearance": "Appearance",
    "surfaces": "Screen apps",
    "calculator": "Calculator",
    "keys": "Keys",
    "everyday": "Daily commands",
    "maintenance": "Maintenance",
    "memory": "Memory commands",
    "privacy": "Guest mode",
    "night_jobs": "Night jobs",
    "habits": "Phrase habits",
    "guide": "Guide",
    "settings": "Settings",
    "chat": "Written chat",
}

_TOPICS = {
    "look": "appearance", "eyes": "appearance",
    "apps": "surfaces", "screen apps": "surfaces", "panels": "surfaces",
    "keyboard": "keys", "shortcuts": "keys",
    "daily commands": "everyday", "memory commands": "memory",
    "guest mode": "privacy", "night jobs": "night_jobs", "voice shortcuts": "voice_shortcuts",
    "options": "settings", "phrase habits": "habits",
    "written chat": "chat", "typing": "chat", "chat": "chat",
}

_LOOK = {
    "glow": ("Eye colour", ("Make your eyes blue", "Make your eyes cyan", "Make your eyes amber",
                            "Pick a random colour")),
    "shape": ("Eye shape", ("Make your eyes round", "Make your eyes sharp", "Make your eyes soft")),
    "strength": ("Glow strength", ("Set your glow to seventy percent", "Make your eyes brighter")),
    "size": ("Face size", ("Make your face smaller", "Set your face size to full")),
    "brows": ("Brows", ("Show your brows", "Hide your brows")),
    "mouth": ("Mouth", ("Show your mouth", "Hide your mouth")),
    **{key: (label.capitalize(), (f"Show your {label}", f"Hide your {label}")) for key, label in FACE_LAYERS.items()},
    **{key: (name.capitalize(), (f"Show the {name}", f"Hide the {name}")) for key, name in WIDGETS.items()},
    "menu_position": ("Quick Menu position", ("Set the Quick Menu to left", "Set the Quick Menu to off")),
    "dock": ("Quick Menu dock", ("Hide the dock", "Show the dock")),
    **{key: (f"{name.capitalize()} position", (f"Move the {name} to the bottom right",))
       for key, name in WIDGET_POSITIONS.items()},
    **{key: (label[0].upper() + label[1:], (f"Show the {label}", f"Hide the {label}"))
       for key, label in DISPLAY_FLAGS.items()},
}
LOOK_FIELDS = {
    field: Action(f"look.{field}", label, "appearance", phrases, owner_only=True)
    for field, (label, phrases) in _LOOK.items()
}

ACTIONS = (
    *LOOK_FIELDS.values(),
    Action("look.current", "Current look", "appearance", ("What is your current look",)),
    *(
        Action(f"app.{app}.{verb}", f"{verb.capitalize()} {name}", "surfaces",
               (f"{verb.capitalize()} the {name}",), owner_only=True, panel=True)
        for app, name in SURFACE_NAMES.items() for verb in ("open", "close")
    ),
    Action("app.close", "Close the current panel", "surfaces", ("Close it",), "Esc",
           owner_only=True, panel=True),
    Action("app.terminal.minimise", "Hide the terminal", "surfaces", ("Hide the terminal",), "fn+F3",
           owner_only=True, panel=True),
    Action("app.terminal.restore", "Bring back the terminal", "surfaces", ("Bring back the terminal",), "fn+F3",
           owner_only=True, panel=True),
    Action("calc.sum", "Calculate", "calculator", ("What is six times seven",), "Type a sum, then Enter"),
    Action("keys.brightness", "Screen brightness", "keys", ("Brighter", "Dimmer"), "Brightness keys"),
    Action("keys.volume", "Speaker volume", "keys", ("Louder", "Quieter"), "Volume keys"),
    Action("keys.captions", "Show or hide captions", "keys", keys="fn+F1"),
    Action("chat.mode.spoken", "Talk out loud", "chat", ("Back to talking",), "The dock's mode chip or F7",
           owner_only=True),
    Action("chat.mode.typed_voice", "Written chat, spoken replies", "chat", ("Let's type",),
           "The dock's mode chip or F7", owner_only=True),
    Action("chat.mode.typed_text", "Written chat, written replies", "chat", ("Silent mode",),
           "The dock's mode chip or F7", owner_only=True),
    Action("chat.mode.next", "Next chat mode", "chat", keys="F7 (the previous-track key)", owner_only=True),
    Action("keys.setup", "Open Setup", "keys", keys="fn+F2"),
    Action("shell.quick_menu.open", "Open Quick Menu", "keys", ("Open the Quick Menu",), "Command+Space or fn+F6",
           owner_only=True, panel=True),
    Action("shell.quick_menu.close", "Dismiss Quick Menu", "keys", ("Close the Quick Menu",), "Esc",
           owner_only=True, panel=True),
    Action("shell.quick_menu.position", "Move Quick Menu", "keys", ("Move the Quick Menu left",
           "Move the Quick Menu to the center", "Move the Quick Menu right"), owner_only=True, panel=True),
    Action("keys.microphone", "Mute or unmute the microphone", "keys", keys="fn+F10"),
)
_BY_ID = {action.id: action for action in ACTIONS}


def help_group(text):
    topic = " ".join(text.casefold().split()).removeprefix("the ").removeprefix("your ")
    return topic if topic in GROUPS else _TOPICS.get(topic)


def for_group(group):
    direct = tuple(action for action in ACTIONS if action.group == group
                   or (group == "calculator" and action.id.startswith("app.calc.")))
    if direct:
        return direct
    from .intents import INTENTS

    return tuple(Action(intent.id, intent.label, group, (intent.canonical.capitalize(),),
                        owner_only=intent.owner_only, panel=intent.panel)
                 for intent in INTENTS if intent.group == group)


def app_phrases():
    """The unambiguous open/close examples for the existing speech grammar."""
    return tuple(action.phrases[0].casefold() for action in ACTIONS
                 if action.group == "surfaces" and action.id != "app.close")


def quick_menu_app_phrase(action_id):
    """Menu selections can open existing panels. They never run commands."""
    if not isinstance(action_id, str):
        return None
    action = _BY_ID.get(action_id)
    if action is not None and action.group == "surfaces" and action.id.endswith(".open"):
        return action.phrases[0].casefold()
    return None


def phrase_hint(action):
    return "Say " + " or ".join(f'"{phrase}"' for phrase in action.phrases)


def options_help(*, panels):
    groups = ("appearance", "keys", "surfaces", "calculator", "settings", "everyday")
    return [
        f"{GROUPS[group]}|{action.phrases[0]}"
        for group in groups for action in for_group(group)
        if action.phrases and (panels or not action.panel)
        and not (group == "surfaces" and action.id.endswith(".close") and action.id != "app.close")
    ][:48]


def spoken_help(group="", *, guest=False, panels=True, aliases=False):
    if group == "voice_shortcuts":
        if aliases and not guest:
            return ('Say "When I say kill the lights, I mean dimmer." I ask before saving it. '
                    'Say "Forget my shortcut kill the lights" to remove it, or "turn off voice shortcuts."')
        return ('Voice shortcuts are off. My owner can say "turn on voice shortcuts," then say yes. '
                'They use exact phrases, not fuzzy matching.')

    def allowed(action):
        return ((not guest or not action.owner_only) and (panels or not action.panel))

    if not group:
        examples = [_BY_ID["calc.sum"]]
        if not guest:
            examples.insert(0, LOOK_FIELDS["glow"])
        if not guest and panels:
            examples[1] = _BY_ID["app.calc.open"]
        topics = "appearance, screen apps, or the calculator" if panels and not guest else "appearance or the calculator"
        shown = " or ".join(f'"{action.phrases[0]}"' for action in examples)
        return f"Try {shown}. Ask what can I say about {topics}."
    actions = [action for action in for_group(group) if allowed(action) and action.phrases]
    if not actions:
        if group == "surfaces":
            return ("My screen apps stay closed in guest mode." if guest else
                    "My screen apps aren't available right now.")
        return "That group has no available voice commands."
    if group == "surfaces":
        actions = [action for action in actions if not (action.id.endswith(".close") and action.id != "app.close")]
    examples = ". ".join(" or ".join(action.phrases if group == "keys" or action.id in ("look.brows", "look.mouth")
                                    else action.phrases[:1]) for action in actions)
    return f"Try these. {examples}."
