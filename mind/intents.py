"""Stage A intent descriptions and exact, explicitly learned shortcuts.

The existing command and skill handlers own execution. This module supplies
canonical phrases and the risk of reaching them through a shortcut.
"""

import re
from dataclasses import dataclass

from . import actions
from .reply import Reply, clean

TELL = "tell"
ASK = "ask"
NEVER = "never"


@dataclass(frozen=True)
class Intent:
    id: str
    canonical: str
    label: str
    group: str = "everyday"
    examples: tuple[str, ...] = ()
    risk: str = TELL
    owner_only: bool = False
    panel: bool = False


INTENTS = (
    Intent("clock.time", "what time is it", "Time", examples=("tell me the current time",), owner_only=True),
    Intent("clock.date", "what date is it", "Date", owner_only=True),
    Intent("reminders.today", "read today's reminders", "Today's reminders", owner_only=True),
    Intent("reminders.tomorrow", "read tomorrow's reminders", "Tomorrow's reminders", owner_only=True),
    Intent("body.battery", "how much battery do you have", "Battery", examples=("check your remaining charge",)),
    Intent("device.brighter", "brighter", "Increase brightness", examples=("bring the screen brightness up",)),
    Intent("device.dimmer", "dimmer", "Decrease brightness", examples=("kill the lights", "make the screen darker")),
    Intent("device.louder", "louder", "Increase volume", examples=("raise your speaking volume",)),
    Intent("device.quieter", "quieter", "Decrease volume", examples=("lower your speaking volume",)),
    Intent("device.sleep", "go to sleep", "Sleep", examples=("take a nap",), risk=ASK),
    Intent("device.mute", "stop listening", "Mute the microphone", risk=ASK),
    Intent("device.backup", "back up yourself", "Back up memory", risk=ASK, owner_only=True),
    Intent("device.processes", "show me your processes", "View processes", "maintenance", owner_only=True, panel=True),
    Intent("device.restart_face", "restart your face", "Restart the face", "maintenance", risk=NEVER, owner_only=True),
    Intent("device.shutdown", "shut down", "Shut down", "maintenance", risk=NEVER, owner_only=True),
    Intent("clock.set", "set the time to 3 pm", "Set the time", "memory", risk=NEVER, owner_only=True),
    Intent("memory.remember", "remember that my cat is called Tom", "Save a fact", "memory", risk=NEVER),
    Intent("memory.correct", "that's wrong, my cat is called Jim", "Correct a fact", "memory", risk=NEVER),
    Intent("memory.forget", "forget about my cat", "Forget a topic", "memory", risk=NEVER),
    Intent("memory.thread_save", "pick this up later", "Keep a conversation thread open", "memory",
           risk=NEVER, owner_only=True),
    Intent("memory.thread_resume", "what were we working on", "Resume the open thread", "memory",
           risk=NEVER, owner_only=True),
    Intent("memory.thread_close", "close the thread", "Close the open thread", "memory",
           risk=NEVER, owner_only=True),
    Intent("lists.clear", "clear my shopping list", "Clear a list", "memory", risk=NEVER, owner_only=True),
    Intent("privacy.enter", "start guest mode", "Start guest mode", "privacy", risk=NEVER, owner_only=True),
    Intent("privacy.leave", "end guest mode", "End guest mode", "privacy", risk=NEVER),
    Intent("ponder.queue_notes", "sleep on my notes", "Queue a night draft", "night_jobs", risk=NEVER, owner_only=True),
    Intent("ponder.status", "what are you sleeping on", "List night jobs", "night_jobs", owner_only=True),
    Intent("ponder.read", "read my night draft", "Read the night draft", "night_jobs", risk=NEVER, owner_only=True),
    Intent("ponder.cancel", "cancel night job 1", "Cancel the named night job", "night_jobs", risk=NEVER, owner_only=True),
    Intent("habit.new", "new habit", "Create a phrase habit", "habits", risk=NEVER, owner_only=True),
    Intent("habit.list", "what habits do you have", "List saved habits", "habits", risk=NEVER, owner_only=True),
    Intent("habit.suggestions_on", "turn on habit suggestions", "Enable habit suggestions", "habits",
           risk=NEVER, owner_only=True),
    Intent("habit.suggestions_off", "turn off habit suggestions", "Disable and clear habit suggestions", "habits",
           risk=NEVER, owner_only=True),
    Intent("guide.backup", "ask your guide about backup", "Backup guide", "guide", risk=NEVER),
    Intent("guide.next", "next guide step", "Next guide step", "guide", risk=NEVER),
    Intent("settings.options", "open options", "Open Options", "settings", risk=NEVER, owner_only=True),
    *(Intent(f"settings.{page}", f"open {page} settings", f"Open {page} settings", "settings",
              risk=NEVER, owner_only=True) for page in ("camera", "network", "backup", "update")),
    *(Intent(action.id, action.phrases[0].casefold(), action.label, "surfaces", owner_only=True, panel=True,
              risk=NEVER if action.id == "app.lookup.open" else TELL)
      for action in actions.ACTIONS if action.group == "surfaces" and action.id.endswith(".open")),
)
BY_ID = {intent.id: intent for intent in INTENTS}
BY_PHRASE = {intent.canonical.casefold(): intent for intent in INTENTS}
EXAMPLES = {example: intent for intent in INTENTS if intent.risk != NEVER for example in intent.examples}
COMMAND_ALIAS_KINDS = frozenset(("time", "date", "reminders_today", "reminders_tomorrow", "battery",
                                 "volume_up", "volume_down", "sleep"))
MAX_ALIASES = 100
QUERY_HOUR = 3600
MAX_QUESTIONS = 6
_ALIAS = re.compile(r"[^\W\d_][\w'-]*(?: [^\W\d_][\w'-]*){1,7}\Z", re.UNICODE)
_TEACH = re.compile(r'when i say (?P<phrase>.+?),? (?:i mean|mean) (?P<target>.+)\Z', re.IGNORECASE)
_FORGET = re.compile(r'forget (?:my|the) (?:voice )?shortcut (?P<phrase>.+)\Z', re.IGNORECASE)
_ENABLE = re.compile(r'(?:turn on|enable) (?:my )?voice shortcuts\Z', re.IGNORECASE)
_DISABLE = re.compile(r'(?:turn off|disable) (?:my )?voice shortcuts\Z', re.IGNORECASE)


@dataclass(frozen=True)
class Resolution(Reply):
    original: str = ""
    intent: Intent | None = None


def phrase(text, name="pip"):
    """Exactly one complete, short phrase after the shared address cleanup."""
    return clean(text, name).casefold().strip('"').strip()


def control(text, name):
    said = clean(text, name)
    if _ENABLE.fullmatch(said):
        return "enable", "", ""
    if _DISABLE.fullmatch(said):
        return "disable", "", ""
    if match := _TEACH.fullmatch(said):
        return "teach", match["phrase"], match["target"]
    if match := _FORGET.fullmatch(said):
        return "forget", match["phrase"], ""
    return None


def valid_alias(value):
    return len(value) <= 75 and _ALIAS.fullmatch(value) is not None and value not in BY_PHRASE


class Learned:
    """Confirmed shortcuts are owner speech records with a route ID as metadata.

    The original phrase is the only stored text. Memory's existing edit,
    expiry and backup path owns the episode, including on older slots. The
    memory_reply marker keeps these operational words out of ordinary recall.
    """

    def __init__(self, memory, owner):
        self.memory = memory
        self.owner = owner

    def source_ids(self):
        return {row[0] for row in self.memory.db.execute(
            "SELECT id FROM episodes WHERE kind='heard' AND speaker=? AND text NOT IN ('', '[forgotten]') "
            "AND json_type(meta, '$.intent_alias')='text'", (self.owner,))}

    def entry(self, value):
        return self.memory.db.execute(
            "SELECT e.id, json_extract(e.meta, '$.intent_alias') FROM episodes_fts "
            "JOIN episodes e ON e.id=episodes_fts.rowid "
            "WHERE episodes_fts MATCH ? AND e.kind='heard' AND e.speaker=? AND e.text=? "
            "AND json_type(e.meta, '$.intent_alias')='text' ORDER BY e.id DESC LIMIT 1",
            (f'"{value}"', self.owner, value)).fetchone()

    def lookup(self, value):
        found = self.entry(value)
        intent = BY_ID.get(found[1]) if found else None
        return intent if intent is not None and intent.risk != NEVER else None

    def save(self, value, intent):
        if self.entry(value) is not None or len(self.source_ids()) >= MAX_ALIASES:
            return None
        return self.memory.log("heard", value, self.owner, command=True, memory_reply=True, intent_alias=intent.id)
