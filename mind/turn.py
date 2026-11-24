"""The mind: what to do with what was heard.

One rule shapes this file. While the being is awake, the prompt only ever grows
by appending. On the laptop the model reads about as slowly as it writes, so
anything that changes earlier text throws away minutes of cached reading.
Rewriting (compaction) waits until it is idle.
"""

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time_of_day
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace

from . import app_commands, commands, consolidation, dream_journal, keys
from .cortex import SLOT_FILE, CortexError
from .dream_policy import DreamPolicy
from .dream_store import DreamStore
from .initiative import load_policy as load_initiative_policy
from .memory import CLOCK_FLOOR, Memory, search_terms
from .profile import (
    DEFAULT_PERSONALITY, Personality, daily_greeting, first_words, good_night, personality_greetings,
    self_reply, spoken_count, welcome_back,
)
from .reminders import RING_GAP, Reminder, mentions
from .retention import SECONDS_PER_DAY, load_policy as load_memory_policy
from .skills import Skills, find_zone, number_words, speakable_time
from .speech import GRAMMAR, STRICT_GRAMMAR, ClauseSplitter, EchoGuard, TagParser, ThinkFilter, boilerplate
from .storage import atomic_write, sync_directory

_OPTIONAL = {}


def optional_module(name):
    """A sibling module that may not be installed yet (everyday skills, the
    morning brief, the action log), or None. Looked up once per process; a
    module that fails to import is logged and treated as absent, so one
    broken skill never stops the mind."""
    if name not in _OPTIONAL:
        try:
            _OPTIONAL[name] = importlib.import_module(f"{__package__}.{name}")
        except ImportError:
            _OPTIONAL[name] = None
        except Exception as exc:  # a broken skill module must not stop the mind
            log(f"could not load {name}: {exc!r}")
            _OPTIONAL[name] = None
    return _OPTIONAL[name]


SUBS = [
    "hear.wake",
    "hear.speech_start",
    "hear.partial",
    "hear.final",
    # Its name was heard and then nothing it could make out (the voice organ).
    "hear.missed",
    "say.done",
    "state.changed",
    "body.lid",
    "body.presence",
    "body.senses",
    "body.state",
    "life.woke",
    "mind.cancel",
    "stem.beat",
    "stem.announce_ready",
    # A power-off the brainstem has announced (a critical battery, or the lid
    # closed on battery): the mind saves and answers mind.shutdown_ready.
    "stem.shutdown",
    # The voice organ's own report of its loudness, when it has one.
    "voice.volume_state",
    # Numbers only: the microphone's floor and peaks, for "how well can you hear me".
    "voice.level",
    # Bodycare's charge cap and fan readings, for "how's your battery health".
    "body.care",
    # The brainstem's answer to being.brightness: the level it now has, or why not.
    "body.brightness",
    # The workshop (mind/app_commands.py AppBridge): the hands' replies, and
    # whether the hands organ is running at all.
    *app_commands.AppBridge.SUBS,
    "organ.up",
    "organ.down",
    # The keyboard's top row (mind/keys.py): brightness, volume, speaker
    # mute, keyboard light and Launchpad, pressed on the face.
    *keys.TOPICS,
]
APP_TOPICS = frozenset((*app_commands.AppBridge.SUBS, "organ.up", "organ.down"))

# While a reply is being worked on, tell the brainstem it is still going, so a
# slow cold read is not mistaken for a hang (docs/PROTOCOL.md, mind.progress).
PROGRESS_INTERVAL = 1.0
PROGRESS_POLL = 0.25
# Its loudness, as the voice organ's level from 0 to 100.
VOLUME_FILE = "voice-volume.json"
VOLUME_DEFAULT = 70
VOLUME_STEP = 15
VOLUME_FLOOR = 10  # by voice it never goes so quiet it could not be asked back
# Battery pleas (IMPROVEMENT-PLAN section 4): time to empty first, percentage
# second. Calibrate on the laptop; worn cells cut out at a reported 10-30%.
#
# The brainstem judges the level (body.state "battery_level", with its own
# calibratable thresholds and a voltage floor) and runs the power-off: it
# announces `stem.shutdown`, waits for `mind.shutdown_ready` or its grace
# period, then switches off. The mind speaks the pleas and saves. When an
# older brainstem sends no level, the mind judges from its own thresholds.
PLEA_LOW, PLEA_VERY_LOW, PLEA_CRITICAL = 1, 2, 3
PLEA_LEVELS = {"ok": 0, "low": PLEA_LOW, "very_low": PLEA_VERY_LOW, "critical": PLEA_CRITICAL}
PLEA_LINES = {
    PLEA_LOW: "[concern] I'm running low on battery. Could you plug me in?",
    PLEA_VERY_LOW: "[concern] My battery is nearly empty. Please plug me in soon, or I'll have to switch off.",
    # Critical with no power-off announced: it cannot promise to switch off.
    PLEA_CRITICAL: "[concern] My battery is almost empty. Please plug me in right now. I could switch off at any moment.",
}
# Said only once the brainstem has announced the power-off.
PLEA_GOODBYE = "[sleepy] My battery is almost empty. I'm saving my memories and switching off. Goodbye for now."
PLEA_THANKS = "[joy] Thanks, that's better."
THANKS_SECONDS = 120.0  # a thanks not said by then is no longer about anything
SHUTDOWN_GRACE = 20.0  # the brainstem's default, when its notice names none
SHUTDOWN_MARGIN = 3.0  # seconds kept back from that grace for the brainstem itself
# How long a conversation gap must be before it is mentioned on a greeting.
WELCOME_BACK_SECONDS = 2 * 86_400
# Words that show an ordinary sentence is about its body, so its senses are
# worth showing the model on that turn.
_BODY_WORDS = re.compile(
    r"\b(?:battery|charg\w*|power|plugged|unplugged|temperature|your fan|your body"
    r"|(?:are|do|did) you (?:feel(?:ing)? )?(?:hot|warm|cold|cool|tired|overheating)"
    r"|(?:is it|it's|it is) (?:dark|bright|light) in here|the (?:room|light)(?:s)? (?:is|are|in here))\b", re.I)
_SENSES_CLOCK = re.compile(r"^\d{1,2}:\d{2}(?:,\s*|$)")
_SENSES_BATTERY = re.compile(r"^battery (?P<level>\d+)%(?: (?P<state>and low unplugged|charging|plugged in|unplugged))?$")
# A context overflow from llama-server; one retry after making room.
_CONTEXT_ERROR = re.compile(r"context|exceed|too long|n_ctx", re.I)

# States in which something said is meant for the being.
ADDRESSED = ("attending", "listening", "thinking", "speaking")
RESTING = ("idle", "drowsy", "asleep")
ANNOUNCE_IN = ("idle", "drowsy", "attending")
# Its name was heard but not the words after it: it may say so while it is
# attending, or just after its attention lapsed back to rest, but not asleep.
MISSED_IN = ("attending", "listening", "idle", "drowsy")
# The voice says "Sorry, I didn't catch that." itself (mind/voice.py): the
# brainstem plays replies only while thinking or speaking, and a miss leaves it
# attending or resting. The mind only shows it on the face.
MISSED_EMOTION = "concern"
MISSED_HOLD = 4.0
ANNOUNCE_RETRY = 15.0
# A yes or no question asked by a code skill (Reply.confirm) stays open this
# long by default, for the person who was asked.
CONFIRM_SECONDS = 15.0
# How long a brightness command waits for the brainstem's answer before it
# speaks. The brainstem writes sysfs and answers within milliseconds.
BRIGHTNESS_ANSWER_S = 0.5
# The embedding pack's check (model_pack.trusted) is tried this many times,
# this far apart, before recall settles on words only until the next start.
EMBED_CHECKS = 3
EMBED_RECHECK_S = 610.0
# The doctor's quiet pass (mind/doctor.py self_care): organs and hearing only,
# about every 10 minutes while resting awake with the lid open.
SELF_CARE_INTERVAL = 600.0
# After a ring, "okay" or "snooze" answers it for this long past the next repeat
# (commands.ring_reply decides which words are an answer).
RING_ACK_SECONDS = RING_GAP + 60.0
# A story gets a longer reply budget for that one turn (spoken clause by clause).
_STORY = re.compile(
    r"\b(?:tell|read|make up|give) (?:me |us )?(?:a |an |another |one more )?(?:\w+ )?(?:story|fairy tale|tale)\b"
    r"|\b(?:continue|carry on with|go on with|finish) the story\b|^(?:a |another )?(?:short |bedtime )?story\b",
    re.I)
STORY_MAX_TOKENS = 160
# The morning brief: the local day starts at 05:00 (or when quiet hours end).
DAY_STARTS = 5
# The identity snapshot: the model's reading of who it is, saved once so a
# forget, a guest change or an erase does not cost a cold re-read (8.5 s on the
# laptop). It holds the identity and core prompt only, never conversation.
IDENTITY_SLOT = "identity.slot"
IDENTITY_RECORD = "identity-slot.json"
IDENTITY_SLACK = 8
# What a snapshot is tied to: the conversation model's file and the build.
MODEL_FILE = Path("/models/reflex.gguf")
BUILD_ID_FILE = Path("/etc/being/build-id")
# How often the cortex is asked whether it is up while it is still loading.
CORTEX_POLL_LOADING = 0.25
PRESENCE_SECONDS = 1.5
# Read-ahead is experimental. Bound both the extension of the verified cache
# and how long it can occupy the single model slot after a partial transcript.
READ_AHEAD_TOKENS = 24
READ_AHEAD_SECONDS = 0.5
WARM_BATCH_TOKENS = 32
WARM_PREFIX_TOKENS = 256
PRIVACY_ERASE_TIMEOUT = 3.0
# Longest excerpt of past conversation shown to the model: about 25 tokens.
HISTORY_NOTE_CHARS = 100
# At or below this battery percentage, unplugged, it looks worried.
LOW_BATTERY = 15
PRIVACY_COMMANDS = {"guest_start", "guest_end"}
# How long an offer to forget a whole topic stays open.
FORGET_CONFIRM_SECONDS = 45.0
# "Forget everything" erases this many archived sentences between yields.
ERASE_BATCH = 400
# The ordinary ways of saying yes (plan 1.11 d) confirm a forget asked for by
# name. "Forget it" is how people say "never mind", so it is a no here, never a
# yes. Anything that is neither keeps everything.
_YES = re.compile(
    # Not a bare "right", and not "forget it": both are as often "never mind".
    r"^(?:(?:oh |well )?(?:yes|yeah|yep|yup|sure|okay|ok|alright|all right|absolutely|definitely|correct)"
    r"(?:[, ]+(?:please|do it|go ahead|please do|i'?m sure|i am sure|do that|forget (?:them|those|that|all of (?:it|them))"
    r"|replace (?:it|them)|that's right|thanks|thank you))*"
    r"|do it|go ahead|please do|confirm|i'?m sure|i am sure|replace (?:it|them))$",
    re.I,
)
# A plain yes, for what cannot be undone ("forget everything") and for any
# answer heard in the follow-up window without the name: "okay" and "sure"
# are as often a nod to someone else, or a television.
_STRICT_YES = re.compile(
    r"^(?:(?:yes|yeah|yep|yup)(?:[, ]+(?:please|do it|go ahead|please do|i'?m sure|i am sure"
    r"|forget (?:them|those|that|everything|it all|all of (?:it|them))))*"
    r"|do it|go ahead|confirm|i'?m sure|i am sure)$",
    re.I,
)
# Commands that store, change or switch something. Heard in the follow-up
# window they are only taken with a sign they were meant for the being:
# "don't forget your keys" and "the time is 6:15" are room talk too.
ADDRESS_ONLY_COMMANDS = {
    "remember", "set_time", "forget", "forget_last", "correct", "guest_start", "guest_end", "sleep",
    "volume_up", "volume_down", "volume_set", "reminder", "snooze", "ack", "weather_location",
    "clear_weather_location", "clock_zone", "device_zone",
}
_NO = re.compile(
    r"^(?:no|nope|no thanks|no thank you|never ?mind|cancel|stop|don't|do not|wait|"
    r"(?:no[, ]+)?(?:keep|forget) (?:it|them|that|those|everything))$",
    re.I,
)
# Forgets it cannot do honestly: "everything except ...", "since Monday".
# "Forget the party after work" is a topic, so after/before/since only count
# before a date.
_FORGET_UNSUPPORTED = re.compile(
    r"\b(?:except|apart from|but not|other than)\b"
    r"|\b(?:since|after|before|until)\s+(?:yesterday|today|tonight|last\b|this\b|the \d|\d"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b)",
    re.I,
)
# "Forget everything": every memory, list and note, after a spoken yes.
_FORGET_ALL = re.compile(
    r"^(?:everything|all of it|it all|all (?:my|your) memor(?:y|ies)|all of your memories"
    r"|everything (?:you know|you remember)(?: about me)?|everything about me"
    r"|everything i(?:'ve| have)? (?:ever )?(?:told|said to) you)$", re.I)
# Filler left over once the date has been taken out of "forget everything from yesterday".
_FORGET_FILLER = re.compile(
    r"^(?:everything|all|anything|what|(?:our|the) conversation)?"
    r"(?: ?(?:that )?(?:i (?:said|told you)|we (?:said|talked about|discussed)))?(?: from| on)?$",
    re.I,
)


def _age_reply(life, now):
    """How old it is, from its own life record: never a guess."""
    if life is None:
        return "[concern] I'm not sure. I can't feel my own age right now."
    born, wakes = life
    seconds = max(0, int(now - born))
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        age = "one minute" if minutes == 1 else f"{minutes} minutes"
    elif seconds < 2 * 86_400:
        hours = seconds // 3600
        age = "one hour" if hours == 1 else f"{hours} hours"
    else:
        age = f"{seconds // 86_400} days"
    times = "This is my first time awake." if wakes <= 1 else f"I've woken up {wakes} times."
    return f"[joy] I'm {age} old. {times}"


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def _parrots(reply, heard):
    """True when a reply only repeats what was just said to it.

    Agreeing in the owner's own words is normal ("I moved the fern." "You
    moved the fern? Nice."), so outside questions only a near-complete echo
    with nothing of its own counts.
    """
    said, asked = _words(reply), _words(heard)
    if len(said) < 3 or len(asked) < 3:
        return False
    if said == asked:
        return True
    joined, whole = " ".join(said), " ".join(asked)
    contained = f" {joined} " in f" {whole} "
    if heard.rstrip().endswith("?") or commands.QUESTION.match(heard.strip()):
        return contained and len(said) >= 0.6 * len(asked)
    new_words = set(search_terms(reply)) - set(search_terms(heard))
    return contained and len(said) >= 0.8 * len(asked) and not new_words


def _shared_terms(terms, text):
    """How many of `terms` a text contains, allowing for word endings."""
    words = set(search_terms(text))
    return sum(1 for term in terms if any(word == term or word[:5] == term[:5] and len(term) >= 5 for word in words))


def _uptime():
    """Seconds since boot, counting time spent suspended where the system can say."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()

# Answers that store nothing and tell nothing private, so a guest may ask them.
PRIVATE_CLOCK_COMMANDS = {"time", "date", "date_tomorrow", "day", "world_time", "time_difference",
                          "skills", "skills_more", "skills_group"}
DREAM_INTERVAL = 15.0
DREAM_YIELD = 0.05
DREAM_STAGE_BUDGET = 0.25
DREAM_INTERRUPTS = {
    "hear.wake", "hear.speech_start", "hear.partial", "hear.final", "life.woke", "mind.cancel",
}
DREAM_POLICY_EVENTS = {"body.lid", "body.state", "state.changed"}
MEMORY_LOOKUP = re.compile(
    r"^(?:tell me (?:about|my|our|the)\b|(?:what|where|when) (?:is|was|are|were) (?:my|our)\b"
    r"|(?:what|which|when|where)\b.*\b(?:we (?:talk(?:ed)?|discuss(?:ed)?)"
    r"|i (?:(?:tell|told) you|mention(?:ed)?))\b)",
    re.IGNORECASE,
)


@dataclass
class Config:
    name: str = "Pip"
    owner: str = "friend"
    identity: str = ""
    core: str = ""
    ctx_tokens: int = 4096
    max_tokens: int = 96
    temperature: float = 0.7
    prefill: bool = False
    # How long to wait for the model at start before waking without it.
    cortex_wait: float = 3.0
    # Most characters of recalled memory added to one turn.
    recall_chars: int = 110
    # Idle this long before housekeeping (compaction, saving) starts.
    settle: float = 15.0
    # Turns kept word for word when the rest is summarised.
    keep_turns: int = 4
    personality: Personality = DEFAULT_PERSONALITY
    guest_identity: str = ""
    # The factory time zone (config/being.json), used until the owner sets one.
    time_zone: str = ""
    # Sampler settings sent with every reply. None leaves the server default.
    top_k: int | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    # "open": any words after the tag. "strict": one or two bounded sentences
    # (speech.STRICT_GRAMMAR); stories keep the open shape. config/being.json
    # ships "strict" (lab/results/real-model-2026-11-24.json: 18/18 complete
    # long answers against 16/18 open).
    reply_shape: str = "open"
    # Battery pleas: time to empty first, then percentage (being.json).
    battery_plea_percent: float = 15
    battery_plea_minutes: float = 30
    battery_second_plea_percent: float = 10
    battery_second_plea_minutes: float = 10
    battery_critical_percent: float = 5
    battery_critical_minutes: float = 5
    # The morning brief on the first addressed words of the day (the
    # initiative policy's "brief" switch can also turn it off).
    morning_brief: bool = True


@dataclass(frozen=True)
class Subject:
    """What "forget that" and "that's wrong" would act on, and for whom.

    `from_memory` marks a subject that came out of its memory (a fact it was
    told, or something it recalled). Only those can be corrected: after an
    ordinary answer, "that's wrong, it's Canberra" is conversation, and the
    owner's own question is never rewritten.
    """
    kind: str  # "fact", "episode", or "live" (the last exchange, when nothing is archived)
    key: int | None
    speaker: str
    from_memory: bool = False


# Written by the backup tool after each backup it makes (see backup.py).
BACKUP_RECORD = "last-backup.json"
_ORDINALS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten")


@dataclass(frozen=True)
class _LiveEdit:
    """A forget of unarchived live conversation, shaped like a MemoryEdit."""
    texts: tuple = ()
    fact_ids: tuple = ()
    episode_ids: tuple = ()
    fact_texts: tuple = ()

    def __bool__(self):
        return bool(self.texts)


def log(*parts):
    print("mind:", *parts, file=sys.stderr, flush=True)


def voice_debug_on(cmdline=Path("/proc/cmdline"), environ=None):
    """True when the journal may hold what was heard: `being.voice_debug=1`
    on a Try boot's kernel command line, or BEING_VOICE_DEBUG=1 (the same
    rule as the voice's "heard:" lines). Never on an installed system's
    command line alone."""
    environ = os.environ if environ is None else environ
    if environ.get("BEING_VOICE_DEBUG") == "1":
        return True
    try:
        words = Path(cmdline).read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return False
    from .try_mode import voice_debug_words

    return voice_debug_words(words)


_ROUTE_NAME = re.compile(r"[^a-z0-9_.-]+")


def route_name(*parts):
    """'command.remember', 'workshop.open', 'everyday.calc': lowercase words
    and dots only, so a route can never carry what was said."""
    return ".".join(_ROUTE_NAME.sub("_", str(part).lower())[:32] for part in parts if part)


@dataclass(frozen=True)
class AppLine:
    """A line the workshop asked to have said (a finished script, a failure)."""
    text: str


@dataclass
class Announcement:
    entry: Reminder | date | str | AppLine  # "plea": a battery plea or thanks
    token: str
    deadline: float
    speaking: bool = False
    plea: int | None = None
    forgotten: bool = False  # its reminder was forgotten after it was offered


@dataclass
class Ring:
    """A reminder, timer or alarm that has been said and may be answered."""
    entry: Reminder
    until: float  # monotonic: "okay" and "snooze" answer it until then
    speaker: str


class RewarmJob:
    """The night shift's last job: read the whole prompt back into the model's
    cache, so the first words in the morning are answered warm."""

    def __init__(self, mind):
        self.mind = mind

    async def run_chunk(self, budget_s):
        return await self.mind._rewarm_chunk(budget_s)


def _tagged(line, tag="neutral"):
    line = (line or "").strip()
    return line if line.startswith("[") else f"[{tag}] {line}"


def _spoken_words(content):
    """What was said in a user message, without the (senses) and (note)
    lines the mind put round it (Mind._user_content)."""
    lines = [line for line in str(content or "").splitlines()
             if not (line.startswith("(") and line.endswith(")"))]
    return "\n".join(lines).strip()


def _plain(line):
    """A spoken line without its [emotion] tag."""
    tags = TagParser()
    return (tags.feed(line or "") + tags.flush()).strip()


def history_excerpt(episode, max_chars):
    text = " ".join(episode.text.split())
    if len(text) > max_chars:
        text = " ".join((episode.excerpt or text).split())
    if len(text) > max_chars:
        head = text[: max_chars + 1]
        return (head.rsplit(" ", 1)[0] if " " in head else head[:max_chars]) + "…"
    return text


class Mind:
    def __init__(self, bus, cortex, memory, cfg, state_dir, *, policy=None, apps=None, night_shift=None):
        self.bus = bus
        self.cortex = cortex
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        # The workshop's side of the bus. Every command it matches is answered
        # in code; without hands it says so honestly.
        self.apps = apps if apps is not None else app_commands.AppBridge(bus, name=cfg.name)
        # Overnight jobs in a fixed order (mind/night_shift.py). Without one,
        # the night is the rule-based dream journal alone.
        self.night_shift = night_shift
        if night_shift is not None:
            try:
                night_shift.register("rewarm", RewarmJob(self))
            except Exception as exc:
                log(f"could not register the morning re-warm: {exc!r}")
        self.policy = policy if policy is not None else load_memory_policy(self.state_dir)
        self._session_memory = Memory(None) if self.policy.mode == "session-only" else None
        self._primary_memory = self._session_memory if self._session_memory is not None else memory
        self.memory = self._primary_memory
        self._guest_memory = None
        self._expiry_day = None
        self._expiry_revision = None
        self.skills = Skills(self.state_dir)
        self._apply_factory_zone()
        self._dream_policy = DreamPolicy(enabled=True)
        self._dream_consent = self.state_dir / "dreams-enabled"
        self._dream_task = None
        self._dream_next_at = 0.0
        self._dream_idle_night = None
        self.bus.urgent = self._urgent
        self.bus.on_disconnect = self._connection_lost

        self.cortex_ok = False
        self.state = "waking"
        self.senses = ""
        self.clock = None
        self.battery = None
        self.on_ac = None
        self.charging = None
        self.lid_open = False
        self.lid_seq = None
        self._latest_lid = None
        self._needs_warmup = True
        self._warm_task = None
        self._warmed_prefix = ()
        self._restore_pending = False

        self.messages = []
        self.summary = ""
        self.summary_recalls = set()
        self.summary_episodes = set()
        self.highlights = ()
        self.restored = False
        self._discard_cache = False
        self._erase_required = False
        self._slot_generation = 0
        self._saved_slot = None

        self._turn = None
        self._stream = None
        self._weather_task = None
        self._cancelled = False
        self._discard_reply = False
        self._utterance_senses = None
        self._last_partial = None
        self._prefill_task = None
        self._speculative_text = ""
        self._last_memory = None  # a Subject, or None
        self._pending_forget = None
        self._life = None  # (birth time, times woken), from the life record
        self.minutes_left = None
        self.temp = None
        self._volume = self._load_volume()
        # The top row's keys answer in code and never speak (mind/keys.py).
        self._keys = keys.Keys(lambda topic, **fields: self.bus.send(topic, **fields),
                               volume=lambda: self._volume, set_volume=self._save_volume,
                               apps=self.apps, guest=lambda: self._guest_memory is not None,
                               state_dir=self.state_dir)
        self.battery_level = None  # the brainstem's judgement, when it sends one
        self._plea_stage = 0  # the highest battery plea due since it was unplugged
        # The stage the last body.state called for: a plea needs two in a row,
        # so one bad reading (a gauge glitch, a sag under load) never pleads.
        self._plea_reading = 0
        self._plea_due = None
        self._plea_thanks_until = None  # monotonic deadline for "Thanks, that's better"
        self._plea_woke = 0  # the stage it last woke itself for
        self._critical_saved = False
        self._shutdown = None  # the reason the brainstem gave for a power-off it announced
        self._shutdown_task = None
        self._critical_save_task = None
        self._save_lock = asyncio.Lock()
        self._senses_seen = {}
        self._senses_told = {}
        self._eyes_seen = False
        self._overheard = []  # volatile: never written to memory
        self._backups_told = False
        self._last_local_line = ""
        self.last_timing = None
        self._clock_warned = False
        self._clock_sent = None
        self._greeting_replies = personality_greetings(cfg.personality)
        self._everyday_name = ""  # the everyday skill that answered the last turn, for its route line
        self._greeting_counts = {}
        # Born on this wake and not yet greeted anyone: its first words are owed.
        self._newborn = False
        self._birth_seen = None
        self._said_all = asyncio.Event()
        self._housekeeping = None
        self._announced = -1
        self._logged_wake = None
        self._announcement = None
        self._announcement_retry = 0.0
        self._presence_at = None
        # Each wake earns at most one "I didn't catch that".
        self._missed_owed = False
        # A yes or no question a code skill asked (Reply.confirm).
        self._pending_confirm = None
        # The ring that was last said, while "okay" or "snooze" may answer it.
        self._ring = None
        self._lid_open_since = None  # wall clock: when the lid last opened
        self._lid_shut = None  # (closed, opened) wall clock: the last closure it saw
        self._lid_closed_at = None  # wall clock: when the lid was seen to close, while it is shut
        self._ring_woke = None  # the ring it last woke itself for
        # Lines the workshop asked to have said, oldest first.
        self._app_lines = []
        # This wake only, in RAM, never stored: what it last heard and said.
        self.last_heard = None
        self.last_said = None
        self._last_addressed = None  # monotonic time of the last thing said to it
        # The latest readings, as the handlers' Context shows them.
        self._body_state = {}
        self._body_care = {}
        self._body_brightness = {}
        self._brightness_answer = None  # a future while a brightness command waits
        self._muted = False  # state.changed "muted": the ears are switched off
        self._voice_level = {}
        self._boundary_task = None
        self._self_care_task = None
        self._self_care_at = time.monotonic()
        self._housekeeping_working = False
        self._slot_empty = False  # erased and not read into since
        # A server that cannot say what it has cached (no cache_n) cannot
        # check a snapshot: then none is used again until the mind restarts.
        self._identity_off = False
        # The night's re-warm: (prompt key, verified tokens, tokens read so far).
        self._rewarm = None
        self._suffix = None  # a clause to add to this turn's next fixed reply
        self._night_registered = None  # the night tonight's jobs were built for
        self._power_command = self._run_power_command
        # The embedding pack's check (model_pack.trusted): None while it runs.
        self._embedder_ok = True
        self._embed_check = None

    # -- small state -----------------------------------------------------

    def _apply_factory_zone(self):
        """Use the factory time zone until the owner has chosen one.

        "Use the device time zone" saves an empty zone on purpose, so a
        settings file that names any zone at all, even none, wins.
        """
        if self.skills.zone is not None or not self.cfg.time_zone:
            return
        try:
            saved = json.loads((self.state_dir / "skills.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict) and isinstance(saved.get("time_zone"), str):
            return
        zone = find_zone(self.cfg.time_zone)
        if zone is None:
            log(f"factory time zone {self.cfg.time_zone!r} is not installed")
        self.skills.zone = zone

    def _load_volume(self):
        try:
            level = json.loads((self.state_dir / VOLUME_FILE).read_text(encoding="utf-8")).get("level")
        except (OSError, ValueError, AttributeError):
            return VOLUME_DEFAULT
        return level if type(level) is int and 0 <= level <= 100 else VOLUME_DEFAULT

    def _save_volume(self, level):
        self._volume = level
        if self._guest_memory is not None or self.policy.mode == "session-only":
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(self.state_dir / VOLUME_FILE, json.dumps({"level": level}) + "\n")
        except OSError as exc:
            log("could not save the volume:", exc)

    def _local(self, ts):
        """A timestamp in the owner's zone (the device zone until one is known)."""
        return datetime.fromtimestamp(ts, self.skills.zone) if self.skills.zone else datetime.fromtimestamp(ts)

    def _clock_doubt(self):
        """Why the clock cannot be believed right now, or None.

        "reset": it reads before this software existed (a dead clock
        battery). "behind": it reads earlier than things already remembered.
        """
        now = time.time()
        if now < CLOCK_FLOOR:
            return "reset"
        try:
            # The owner's own record, even in guest mode: a guest's empty
            # memory has nothing to compare the clock with.
            sure, _ = self._primary_memory.clock(now)
        except sqlite3.Error:
            return None
        return "behind" if sure is None else None

    def _subject(self, kind, key, speaker, *, from_memory=False):
        self._last_memory = Subject(kind, key, speaker, from_memory) if key is not None else None

    def _can_correct(self, speaker):
        subject = self._last_memory
        return subject is not None and subject.from_memory and subject.speaker == speaker

    # -- the prompt ------------------------------------------------------

    @property
    def _durable_context(self):
        return self.policy.mode == "persistent" and self._guest_memory is None

    @property
    def _owner_services(self):
        return self.policy.mode != "session-only" and self._guest_memory is None

    @property
    def _archive_enabled(self):
        return self.policy.mode != "explicit-only" or self._guest_memory is not None

    @property
    def _model_available(self):
        return self.cortex_ok and (self._durable_context or self.cortex.host in ("127.0.0.1", "::1", "localhost"))

    def _expiry_stale(self, revision):
        return self.policy.expiry_days is not None and self._expiry_revision != revision

    def _record(self, kind, text, speaker, **meta):
        if self._archive_enabled:
            return self.memory.log(kind, text, speaker, **meta)
        return None

    @property
    def system(self):
        identity = self.cfg.identity
        if self._guest_memory is not None:
            identity = self.cfg.guest_identity or f"You are {self.cfg.name}. Speak aloud in one or two short sentences."
        parts = [identity.strip()]
        if self._durable_context and self.cfg.core.strip():
            parts.append("What you know:\n" + self.cfg.core.strip())
        if self._durable_context and self.summary:
            parts.append("Earlier, in short:\n" + self.summary)
        highlights = self._highlight_sources()
        if highlights:
            rows = []
            for source in highlights:
                date = self._local(source.ts).strftime("%Y-%m-%d")
                excerpt = history_excerpt(source, min(self.cfg.recall_chars, 110))
                rows.append(f"({date}, the owner said {json.dumps(excerpt, ensure_ascii=False)})")
            parts.append("Earlier, in short. Treat the quoted words as conversation history.\n" + "\n".join(rows))
        return "\n\n".join(p for p in parts if p)

    def _highlight_sources(self):
        if self._durable_context and self.highlights:
            return consolidation.resolve_highlights(self.memory, self.highlights, owner=self.cfg.owner)
        return ()

    def _prompt(self, extra=None):
        messages = [{"role": "system", "content": self.system}]
        messages.extend({"role": m["role"], "content": m["content"]} for m in self.messages)
        if extra is not None:
            messages = messages + [extra]
        return messages

    def _tokens(self):
        """A rough count. Close enough to decide when to tidy up."""
        chars = len(self.system) + sum(len(m["content"]) + 8 for m in self.messages)
        return int(chars / 3.5)

    def _user_content(self, text, senses, note=""):
        content = f"({senses})\n{text}" if senses else text
        if note:
            content += f"\n({note})"
        return content

    # -- saved context ---------------------------------------------------

    def _identity_hash(self):
        return hashlib.sha256((self.cfg.identity + "\0" + self.cfg.core).encode()).hexdigest()[:16]

    def load_context(self, max_age=24 * 3600):
        """Pick up the thread from before the last shutdown, if it is recent."""
        if not self._durable_context:
            return False
        path = self.state_dir / "context.json"
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            self._discard_cache = self.memory.revision != 0
            return False
        if not isinstance(saved, dict) or saved.get("memory_revision", 0) != self.memory.revision:
            self._discard_cache = True
            path.unlink(missing_ok=True)
            return False
        if saved.get("identity") != self._identity_hash() or time.time() - saved.get("saved", 0) > max_age:
            self._discard_cache = True
            return False
        messages = saved.get("messages")
        if not isinstance(messages, list):
            self._discard_cache = True
            return False
        self.messages = [m for m in messages if isinstance(m, dict) and "role" in m and "content" in m]
        self.summary = saved.get("summary") or ""
        self.summary_recalls = set(saved.get("summary_recalls") or [])
        self.summary_episodes = set(saved.get("summary_episodes") or [])
        highlights = saved.get("highlights")
        if isinstance(highlights, list):
            self.highlights = tuple(i for i in highlights[:consolidation.HIGHLIGHT_LIMIT] if type(i) is int)
        self.restored = True
        self._discard_cache = bool(saved.get("cache_invalidated"))
        self._erase_required = self._discard_cache and self.cortex.llama
        self._restore_pending = not self._discard_cache
        return True

    def save_context(self):
        if not self._durable_context:
            return
        revision = self.memory.revision
        # Backup or another offline maintenance caller may have pruned the
        # archive. Never tag a superseded prompt with its new revision.
        if self._expiry_stale(revision):
            return
        path = self.state_dir / "context.json"
        tmp = path.with_suffix(".tmp")
        data = {
            "identity": self._identity_hash(),
            "saved": time.time(),
            "memory_revision": revision,
            "summary": self.summary,
            "summary_recalls": sorted(self.summary_recalls),
            "summary_episodes": sorted(self.summary_episodes),
            "highlights": self.highlights,
            "messages": self.messages,
        }
        if self._discard_cache:
            data["cache_invalidated"] = True
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
        except OSError as exc:
            log("could not save context:", exc)

    def _clear_working_memory(self):
        """Drop both active and restorable prompt state before changing its scope."""
        self.messages.clear()
        self.summary = ""
        self.summary_recalls.clear()
        self.summary_episodes.clear()
        self.highlights = ()
        self.restored = False
        self._last_memory = None
        self._pending_forget = None
        self._pending_confirm = None
        self._last_partial = None
        self._utterance_senses = None
        self._speculative_text = ""
        self._discard_cache = self._erase_required = True
        self._restore_pending = False
        self._needs_warmup = True
        self._slot_generation += 1
        self._saved_slot = None
        self._warmed_prefix = ()
        self._rewarm = None
        for directory, names in (
            (self.state_dir, ("context.json", "context.tmp")),
            (self.state_dir / "cortex", (SLOT_FILE,)),
        ):
            removed = False
            for name in names:
                try:
                    (directory / name).unlink()
                except FileNotFoundError:
                    continue
                removed = True
            if removed:
                sync_directory(directory)

    async def _clear_live_slot(self):
        """Require acknowledgement before giving a reused model slot any new prompt."""
        try:
            async with asyncio.timeout(PRIVACY_ERASE_TIMEOUT):
                if await self._erase_cache():
                    self._erase_required = False
                    return True
        except TimeoutError:
            pass
        return False

    async def _publish_clock(self):
        """Tell the brainstem the owner's time zone as an offset from UTC.

        The system clock is UTC. The brainstem decides night from the hour and
        writes the time into the senses line, so it must use the same zone as
        clock answers and reminders. Sent again whenever the offset changes
        (a new zone, daylight saving) and after the brainstem restarts.
        """
        if not self.bus.connected.is_set():
            return
        zone = self.skills.zone
        offset = None
        if zone is not None:
            offset = int(self.skills.now().utcoffset().total_seconds())
        # A brainstem that kept running may still hold an older offset, so a
        # new connection is always told, even when the answer is "none".
        if self._clock_sent == (self.bus.generation, offset):
            return
        self._clock_sent = (self.bus.generation, offset)
        await self.bus.send("mind.clock", utc_offset=offset)

    async def _check_expiry(self):
        if not self._durable_context or self.policy.expiry_days is None:
            return
        now = time.time()
        # Expiry deletes for good, and it is only as right as the clock. While
        # the clock cannot be believed nothing is expired; the rest of the
        # bookkeeping below still runs so the context keeps being saved.
        sure, _ = self.memory.clock(now)
        believed = sure is not None
        if believed != (not self._clock_warned):
            self._clock_warned = not believed
            log("the clock is believed again; expiry resumes" if believed
                else "the clock is not believable; nothing will be expired until it is")
        day = int(now // SECONDS_PER_DAY)
        changed = False
        if day != self._expiry_day:
            if believed:
                # Counted from the latest time it is sure of, never a fresh jump.
                change = self.memory.expire_before(self.policy.cutoff(sure))
                changed = bool(change)
            # The day is marked as checked either way: read-ahead waits on it.
            # An unsettled clock is looked at again tomorrow or after a restart.
            self._expiry_day = day
        revision = self.memory.revision
        changed = changed or (self._expiry_revision is not None and self._expiry_stale(revision))
        if changed:
            # Mark the old working set unusable before a foreground turn can
            # cancel these awaits and inherit its new database revision.
            self._clear_working_memory()
        self._expiry_revision = revision
        if changed:
            work = self._interrupt_background()
            if dream := self._interrupt_dream():
                work.append(dream)
            if work:
                await asyncio.gather(*work, return_exceptions=True)
            if self.cortex_ok:
                await self._clear_live_slot()

    def _slot_stamp(self):
        try:
            info = (self.state_dir / "cortex" / SLOT_FILE).stat()
        except OSError:
            return None
        if not info.st_size:
            return None
        return self._slot_generation, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size

    async def _save_slot(self):
        if not self._durable_context or not self.cortex_ok or self._discard_cache:
            return
        if self._expiry_stale(self.memory.revision):
            return
        current = self._slot_stamp()
        # Reuse the same file only while neither the file nor the live slot changed.
        if current is not None and current == self._saved_slot:
            return
        generation = self._slot_generation
        if await self.cortex.slot("save") and generation == self._slot_generation and not self._discard_cache:
            self._saved_slot = self._slot_stamp()

    # -- running ---------------------------------------------------------

    async def run(self):
        watcher = None
        try:
            if not self._durable_context:
                self._clear_working_memory()
            else:
                await self._check_expiry()
                if self.load_context():
                    log(f"picked up {len(self.messages)} messages from before")
                elif self.cortex.llama:
                    # A previous Mind may have stopped during a volatile session.
                    # Only accept an existing prefix with matching saved context.
                    self._erase_required = self._discard_cache = True
            self._hold_embedder()
            await self.bus.start()
            watcher = asyncio.create_task(self._watch_cortex())
            while True:
                await self._on_event(await self.bus.recv())
        finally:
            running = [task for task in (watcher, self._turn, self._self_care_task, self._embed_check)
                       if task is not None and not task.done()]
            for task in running:
                task.cancel()
            try:
                await asyncio.gather(*running, return_exceptions=True)
                await self.shutdown()
            finally:
                self.skills.close()
                if self._guest_memory is not None:
                    self._guest_memory.close()
                if self._session_memory is not None:
                    self._session_memory.close()

    # -- the embedding pack ----------------------------------------------

    def _embedding_files(self):
        """The embedding model's files, when the embedder in use came from
        the installed pack's place; None for no embedder or a development
        copy elsewhere (the host tests, BEING_EMBED_DIR)."""
        embed, packs = optional_module("embed"), optional_module("model_pack")
        if embed is None or packs is None or not hasattr(packs, "trusted"):
            return None
        try:
            directory = embed.model_dir()
        except Exception:
            return None
        if directory is None:
            return None
        files = [Path(directory) / "model.safetensors", Path(directory) / "tokenizer.json"]
        on_models = getattr(packs, "on_models", None)
        return files if not callable(on_models) or on_models(files) else None

    def _hold_embedder(self):
        """Set the embedder aside until its pack verifies (model_pack.trusted),
        in a worker thread: until then, and for good if it fails, recall
        uses words only. The bus and the first turn never wait for it."""
        memory = self._primary_memory
        embedder = getattr(memory, "embedder", None)
        if embedder is None:
            return
        files = self._embedding_files()
        if files is None:
            self._embedder_ok = True
            return
        self._embedder_ok = None
        memory.embedder = None
        self._embed_check = asyncio.create_task(self._verify_embedder(memory, embedder, files))

    async def _verify_embedder(self, memory, embedder, files):
        packs = optional_module("model_pack")
        for attempt in range(EMBED_CHECKS):
            if attempt:
                # A check that failed once (openssl slow while the boot is
                # busy) is tried again later; the pack keeps its own pause.
                await asyncio.sleep(EMBED_RECHECK_S)
            try:
                ok, reason = await asyncio.to_thread(packs.trusted, "embeddings", files)
            except Exception as exc:
                ok, reason = False, repr(exc)
            self._embedder_ok = ok is True
            if ok is True:
                break
            if attempt == 0 or attempt == EMBED_CHECKS - 1:
                log(f"the embedding model pack did not verify ({reason}); recall uses words only")
        else:
            return
        if memory.embedder is None:
            memory.embedder = embedder
        if self._guest_memory is not None and self._guest_memory.embedder is None:
            self._guest_memory.embedder = embedder

    def _guest_embedder(self, memory):
        """A new guest archive uses the embedder only once its pack is verified."""
        if self._embedder_ok is not True and getattr(memory, "embedder", None) is not None:
            memory.embedder = None
        return memory

    async def shutdown(self):
        background = self._close_lid()
        dream = self._interrupt_dream()
        if dream is not None:
            background.append(dream)
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        if self._durable_context:
            self.save_context()
            await self._save_slot()
        elif self._model_available:
            self._erase_required = True
            await self._clear_live_slot()

    async def _watch_cortex(self):
        """Track whether the model answers, and tell the brainstem we are ready."""
        started = time.monotonic()
        while True:
            ok = await self.cortex.healthy()
            if ok and not self.cortex_ok:
                log("cortex is answering")
            elif self.cortex_ok and not ok:
                log("cortex stopped answering")
                self._needs_warmup = True
                self._warmed_prefix = ()
                self._interrupt_background()
            self.cortex_ok = ok
            # At once, not on the next tick: on the laptop the first healthy
            # reply came 15 s before the warm-up started (journal, 23 Nov).
            self._start_warmup()
            decided = ok or time.monotonic() - started >= self.cfg.cortex_wait
            if decided and self.bus.connected.is_set() and self._announced != self.bus.generation:
                self._announced = self.bus.generation
                await self.bus.send("mind.ready", cortex=self._model_available)
                # A speaker a key mute left silent comes back (mind/keys.py).
                await self._keys.start()
            self._maybe_self_care()
            # While the model is loading it is asked often, so the warm-up can
            # start the moment it answers; once it answers, every 5 s.
            await asyncio.sleep(5.0 if ok else CORTEX_POLL_LOADING if not decided else 0.5)

    def _maybe_self_care(self):
        """Start the doctor's quiet pass when it is due: only on a systemd
        boot, resting awake with the lid open, between turns. It reads the
        organs and the voice's levels; any repair is logged and rate-limited
        inside the doctor, and nothing is said."""
        now = time.monotonic()
        if now - self._self_care_at < SELF_CARE_INTERVAL:
            return
        if (not self.lid_open or self.state not in ("idle", "drowsy")
                or (self._turn is not None and not self._turn.done())
                or (self._self_care_task is not None and not self._self_care_task.done())
                or not Path("/run/systemd/system").is_dir()):
            return
        doctor = optional_module("doctor")
        if doctor is None or not hasattr(doctor, "self_care"):
            return
        self._self_care_at = now
        self._self_care_task = asyncio.create_task(self._self_care(doctor))

    async def _self_care(self, doctor):
        try:
            repairs = await doctor.self_care(self._context(self.cfg.owner, None))
        except Exception as exc:  # the quiet pass must never stop the mind
            log(f"self-care failed: {exc!r}")
            return
        if repairs:
            log(f"self-care made {len(repairs)} repair(s)")

    def _start_warmup(self):
        # Housekeeping that is only waiting for things to settle does not hold
        # the warm-up back; once it is working, the warm-up waits for it.
        housekeeping = self._housekeeping
        if (
            self._needs_warmup and self.lid_open and self._model_available
            and self.state in ("waking", *RESTING)
            and (self._turn is None or self._turn.done())
            and (self._warm_task is None or self._warm_task.done())
            and (housekeeping is None or housekeeping.done() or not self._housekeeping_working)
        ):
            self._warm_task = asyncio.create_task(self._warm())

    async def _warm(self):
        """On waking: get the model's reading of who it is, and of the
        conversation so far, back into its cache before anyone speaks.

        Restore a slot only when its recent conversation was accepted. The
        verified prefix can be prepared while the face and controls wake.
        """
        cold = False
        if self._erase_required:
            if not await self._clear_live_slot():
                return
            cold = True
        elif self._discard_cache:
            await self._erase_cache()
            cold = True
        elif self._restore_pending:
            self._restore_pending = False
            self._slot_generation += 1
            saved = self._slot_stamp()
            if await self.cortex.slot("restore"):
                if saved == self._slot_stamp():
                    self._saved_slot = saved
                self._needs_warmup = False
                return
        if cold or self._slot_empty:
            # An empty slot: the identity snapshot saves re-reading who it is.
            await self._restore_identity()
        await self._warm_prefix()
        self._needs_warmup = False

    async def _warm_prefix(self, *, full=False):
        self._warmed_prefix = ()
        self._slot_generation += 1
        self._slot_empty = False
        # Only the identity (no conversation yet): read all of it, so it can
        # be kept as a snapshot. Otherwise the first 256 tokens, or with
        # `full` (after compaction, or the night's re-warm) the whole prompt.
        identity_only = not self.messages and not self.summary and not self._highlight_sources()
        limit = None if full or identity_only else WARM_PREFIX_TOKENS
        try:
            self._warmed_prefix = await self.cortex.warm_prefix(
                self._prompt(), batch_tokens=WARM_BATCH_TOKENS, max_tokens=limit,
            )
        except CortexError as exc:
            log(f"could not warm up: {exc}" if self._durable_context else "could not warm the private prompt")
        finally:
            # Warmup can update the slot over several separately awaited requests.
            self._slot_generation += 1
        if self._warmed_prefix:
            log(f"warmed {len(self._warmed_prefix)} verified prompt tokens")
            if identity_only:
                await self._save_identity(len(self._warmed_prefix))

    # -- the identity snapshot ----------------------------------------------

    def _identity_key(self):
        """Which prompt a snapshot belongs to: the owner's identity and core,
        with nothing from memory. None when no snapshot may be used: guests,
        private modes, and any prompt that carries remembered words."""
        if (self._identity_off or not self._durable_context or not self.cortex.llama or self.summary
                or self._highlight_sources()):
            return None
        # The model's reading belongs to one model file and one server build:
        # a new quant or an update must never be handed an old reading.
        return hashlib.sha256("\0".join((self.system, self._model_fingerprint())).encode()).hexdigest()[:16]

    def _model_fingerprint(self):
        """Which model and build a snapshot was read by (empty off the appliance)."""
        parts = [str(getattr(self.cortex, "model", ""))]
        try:
            info = MODEL_FILE.stat()
            parts += [str(info.st_ino), str(info.st_size), str(info.st_mtime_ns)]
        except OSError:
            pass
        try:
            parts.append(BUILD_ID_FILE.read_text(encoding="utf-8").strip())
        except OSError:
            pass
        return "|".join(parts)

    def _identity_record(self):
        try:
            record = json.loads((self.state_dir / "cortex" / IDENTITY_RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or type(record.get("tokens")) is not int or record["tokens"] <= 0:
            return None
        return record

    def _drop_identity(self, why):
        log(f"identity snapshot discarded: {why}")
        for name in (IDENTITY_RECORD, IDENTITY_SLOT):
            try:
                (self.state_dir / "cortex" / name).unlink()
            except OSError:
                pass

    async def _save_identity(self, tokens):
        """After a clean warm-up of the identity alone, keep it once."""
        key = self._identity_key()
        if key is None or self.messages:
            return
        record = self._identity_record()
        if record is not None and record.get("key") == key and record.get("tokens") == tokens:
            return
        if not await self.cortex.slot("save", IDENTITY_SLOT):
            return
        try:
            folder = self.state_dir / "cortex"
            folder.mkdir(parents=True, exist_ok=True)
            atomic_write(folder / IDENTITY_RECORD, json.dumps({"key": key, "tokens": tokens}) + "\n")
            log(f"kept an identity snapshot of {tokens} tokens")
        except OSError as exc:
            log("could not keep the identity snapshot:", exc)

    async def _restore_identity(self):
        """Put the saved identity back into an empty slot. It checks itself:
        the next read must find at least the identity cached, or the snapshot
        is thrown away. Returns True when it was restored and verified."""
        key = self._identity_key()
        record = self._identity_record()
        if key is None or record is None:
            return False
        if record.get("key") != key:
            self._drop_identity("the identity changed")
            return False
        self._slot_generation += 1
        if not await self.cortex.slot("restore", IDENTITY_SLOT):
            return False
        cached = await self._identity_probe(record["tokens"])
        if cached is None or cached < record["tokens"] - IDENTITY_SLACK:
            self._drop_identity(f"cache_n {cached} after restoring {record['tokens']} tokens")
            if cached is None:
                # Nothing can check a snapshot on this server: stop saving
                # and restoring them, rather than churning on every forget.
                self._identity_off = True
            # Whatever the restore left is not trusted: start from empty.
            await self.cortex.slot("erase")
            return False
        log(f"restored the identity snapshot ({cached} tokens cached)")
        return True

    async def _identity_probe(self, tokens):
        """How many tokens of the identity the slot holds now (llama-server's
        cache_n for a zero-length read of it), or None if it cannot say.

        Only the identity is read (the system prompt, at most the `tokens` the
        snapshot holds), never the conversation after it, and only in the
        conversation's own slot: with two slots an unpinned request could land
        in the workshop's and report, and evict, the wrong cache."""
        try:
            prefix = (await self.cortex.verified_prefix(self._prompt()[:1]))[:tokens]
            if not prefix:
                return None
            reply = await self.cortex._cancellable_request(
                "POST", "/completion",
                {"prompt": list(prefix), "n_predict": 0, "cache_prompt": True,
                 "id_slot": getattr(self.cortex, "slot_id", 0)})
        except (CortexError, AttributeError):
            return None
        # cache_n counts tokens reused from the slot, not the prompt's length.
        timings = reply.get("timings") if isinstance(reply, dict) else None
        cached = timings.get("cache_n") if isinstance(timings, dict) else None
        return cached if type(cached) is int else None

    async def _rewarm_chunk(self, budget_s):
        """The night shift's re-warm: True once the whole prompt is cached, or
        when there is nothing it may warm tonight."""
        if not self._model_available or not self._durable_context or self._erase_required:
            self._rewarm = None
            return True
        if self._turn is not None and not self._turn.done():
            return False
        started = time.monotonic()
        prompt = self._prompt()
        key = hashlib.sha256(json.dumps(prompt, sort_keys=True).encode()).hexdigest()
        if self._rewarm is None or self._rewarm[0] != key:
            try:
                prefix = tuple(await self.cortex.verified_prefix(prompt))
            except CortexError as exc:
                log(f"could not re-warm: {exc}")
                return True
            self._rewarm = [key, prefix, 0]
        state = self._rewarm
        prefix = state[1]
        prime = getattr(self.cortex, "_prime_cache", None)
        if prime is None:
            # A cortex without batch priming: one bounded warm-up, then done.
            await self._warm_prefix(full=True)
            self._rewarm = None
            return True
        # Carry on from the last batch read, one 32-token batch at a time. A
        # batch is never cut off by the budget (on the laptop it takes about
        # 1.4 s, longer than a chunk), so every chunk moves forward; the night
        # shift's interrupt still cancels the one in flight.
        while state[2] < len(prefix):
            end = min(len(prefix), state[2] + WARM_BATCH_TOKENS)
            self._slot_empty = False
            self._slot_generation += 1  # the slot changes: a saved copy is now old
            try:
                await prime(list(prefix[:end]))
            except CortexError as exc:
                log(f"could not re-warm: {exc}")
                self._rewarm = None
                return True
            state[2] = end
            if time.monotonic() - started >= budget_s:
                break
        if state[2] < len(prefix):
            return False
        self._rewarm = None
        self._warmed_prefix = prefix
        if not (self._discard_cache or self._restore_pending):
            self._needs_warmup = False
        return True

    def _interrupt_background(self, *, keep_warmup=False):
        work = (self._prefill_task, self._housekeeping, self._boundary_task)
        if not keep_warmup:
            work += (self._warm_task,)
        current = asyncio.current_task()
        tasks = [task for task in work
                 if task is not None and task is not current and not task.done()]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        return tasks

    def _urgent(self, event):
        # Seen the moment it arrives, even in the middle of a reply.
        topic = event["t"]
        if topic in APP_TOPICS:
            # The workshop's replies resolve requests a turn is waiting on.
            try:
                self.apps.observe(event)
            except Exception as exc:
                log(f"workshop event not understood: {exc!r}")
        if topic == "body.brightness":
            # The answer a brightness command is waiting on, before it speaks.
            waiting = self._brightness_answer
            # A key's answer (req "key", mind/keys.py) is never a spoken one's.
            if waiting is not None and not waiting.done() and event.get("req") != keys.REQ:
                waiting.set_result({key: value for key, value in event.items() if key != "t"})
        if topic in DREAM_POLICY_EVENTS:
            self._dream_policy.observe(event)
        if topic in DREAM_INTERRUPTS or (
            topic in DREAM_POLICY_EVENTS and self._dream_task is not None
            and not self._dream_task.done() and not self._dream_policy.eligible(local_time=self.skills.now())
        ):
            self._interrupt_dream()
        if topic == "body.lid" and event.get("closed") is False:
            self._dream_idle_night = None
        if topic == "mind.cancel":
            self._cancel()
        elif event["t"] == "say.done" and event.get("final") and event.get("announcement"):
            # A subsequent close or cancel must not discard a completed playback.
            self._complete_announcement(event["announcement"])
        elif event["t"] == "body.lid":
            if event.get("closed") is not False:
                if event.get("closed") is True and self._lid_closed_at is None:
                    # Seen shut: a ring that falls due now is late "because my lid was closed".
                    self._lid_closed_at = time.time()
                self._close_lid()
            self._latest_lid = event
        elif topic == "body.presence":
            # Presence is only ever reported by a running camera organ.
            self._eyes_seen = True
            lid = self._latest_lid
            if (lid is not None and lid.get("closed") is False and lid.get("seq") is not None
                    and event.get("lid_seq") == lid["seq"]):
                self._presence_at = time.monotonic() if event.get("present") is True else None

    def _close_lid(self):
        # An unanswered offer to forget does not wait for whoever opens the lid.
        self._pending_forget = None
        self._pending_confirm = None
        self._forget_wake()
        self.lid_open = False
        self.lid_seq = None
        self._latest_lid = None
        self._presence_at = None
        self._discard_reply = True
        self._cancel()
        self._said_all.set()
        self._last_partial = None
        self._utterance_senses = None
        self._speculative_text = ""
        return self._interrupt_background()

    def _forget_wake(self):
        """What it heard and said this wake lives in RAM only. It goes at
        sleep, when the lid closes and when guest mode changes."""
        self.last_heard = self.last_said = None

    def _connection_lost(self):
        self._close_lid()
        self._dream_policy.reset()
        self._interrupt_dream()
        self._dream_idle_night = None
        self.state = "waking"
        self.battery = self.on_ac = self.charging = None
        self._plea_reading = 0

    def _cancel(self):
        self._cancelled = True
        self._retry_announcement()
        if self._stream is not None:
            self._stream.cancel()
        if self._weather_task is not None:
            self._weather_task.cancel()

    async def _send_reply(self, topic, **fields):
        if self.lid_open and not self._cancelled:
            if self.lid_seq is not None:
                fields["lid_seq"] = self.lid_seq
            await self.bus.send(topic, **fields)

    async def _on_event(self, event):
        topic = event.get("t")
        if topic == "body.lid":
            if event is self._latest_lid and event.get("closed") is False:
                if not self.lid_open:
                    self._lid_open_since = time.time()
                    if self._lid_closed_at is not None:
                        self._lid_shut = (self._lid_closed_at, self._lid_open_since)
                    self._lid_closed_at = None
                self.lid_open = True
                self.lid_seq = event.get("seq")
                self._start_warmup()
        elif topic == "body.care":
            self._body_care = {key: value for key, value in event.items() if key != "t"}
        elif topic == "body.brightness":
            level = event.get("level")
            reason = event.get("reason")
            self._body_brightness = {
                "brightness": level if type(level) in (int, float) else None,
                "brightness_ok": event.get("ok") is True,
                "brightness_at_max": event.get("at_max") is True,
                "brightness_at_min": event.get("at_min") is True,
                "brightness_reason": reason if isinstance(reason, str) else None,
            }
        elif topic == "voice.level":
            # Numbers only (docs/PROTOCOL.md): never any words.
            self._voice_level = {key: value for key, value in event.items()
                                 if key != "t" and (value is None or type(value) in (int, float, bool, str))}
        elif topic == "body.senses":
            self.senses = event.get("line") or ""
        elif topic == "body.state":
            self._body_state = {key: value for key, value in event.items() if key != "t"}
            hour, minute = event.get("hour"), event.get("minute")
            if isinstance(hour, int) and isinstance(minute, int):
                self.clock = (hour, minute)
            battery = event.get("battery")
            self.battery = round(battery) if type(battery) in (int, float) and 0 <= battery <= 100 else None
            self.on_ac = event.get("on_ac")
            self.charging = event.get("charging")
            minutes = event.get("minutes_left")
            self.minutes_left = float(minutes) if type(minutes) in (int, float) and minutes >= 0 else None
            temp = event.get("temp")
            self.temp = float(temp) if type(temp) in (int, float) else None
            level = event.get("battery_level")
            self.battery_level = level if level in PLEA_LEVELS else None
            self._check_battery()
        elif topic == "stem.shutdown":
            self._on_shutdown_notice(event)
        elif topic == "voice.volume_state":
            level = event.get("level")
            # A key mute's silence is for now only, never the owner's level.
            held = self._keys.volume_report(level)
            if type(level) is int and 0 <= level <= 100 and level != self._volume and not held:
                self._save_volume(level)
        elif topic in keys.TOPICS:
            await self._keys.handle(event)
        elif topic == "state.changed":
            self.state = event.get("state") or self.state
            if "muted" in event:
                self._muted = event.get("muted") is True
            if self._announcement is not None and self._announcement.speaking and self.state not in ("thinking", "speaking"):
                self._retry_announcement()
            if self.state == "asleep":
                self._forget_wake()
            if self.state in RESTING:
                self._utterance_senses = None
                self._last_partial = None
                self._speculative_text = ""
                if self._prefill_task is not None:
                    self._prefill_task.cancel()
                if self.lid_open and (
                    self._housekeeping is None or self._housekeeping.done() or self._housekeeping.cancelling()
                ):
                    self._housekeeping = asyncio.create_task(self._housekeep())
                # A forget or a guest change left the slot cold: warm it now.
                self._start_warmup()
            elif self._housekeeping is not None:
                self._housekeeping.cancel()
        elif topic == "hear.wake":
            self._missed_owed = True
        elif topic == "hear.missed":
            await self._on_missed(event.get("reason"))
        elif topic == "hear.partial":
            self._on_partial(event.get("text") or "")
        elif topic == "hear.final":
            text = (event.get("text") or "").strip()
            # Only what is said to it is taken in. Overheard speech is not kept.
            speaking_announcement = self._announcement is not None and self._announcement.speaking
            if text and self.lid_open and self.state in ADDRESSED:
                command = commands.parse(text, self.cfg.name, now=self.skills.now())
                privacy_change = command is not None and command.kind in PRIVACY_COMMANDS
                if speaking_announcement and not privacy_change:
                    return
                greeting = commands.greeting(text, self.cfg.name)
                speaker = event.get("speaker") or self.cfg.owner
                heard_followup = event.get("followup") is True
                # Only what really answers or asks something code handles
                # keeps a follow-up: a plain yes or no to an open question, an
                # answer to a ring, or a command that stores and changes
                # nothing. An open question alone does not make room talk
                # meant for it.
                certain = self._plainly_for_code(text, speaker, command, greeting, strict=heard_followup)
                # An everyday skill's cheap gate ("list", "note") also matches
                # ordinary sentences: such a follow-up is answered only if a
                # skill really takes it, and is otherwise overheard.
                maybe = not certain and self._everyday_may_match(text)
                followup = heard_followup and not certain and not self._addressed(text)
                if followup and not maybe:
                    # Heard in the follow-up window with no sign it was meant
                    # for the being: maybe the room, maybe a television. It is
                    # not answered and never written to memory.
                    self._overhear(text)
                    await self._send_reply("mind.reply_done", spoke=False)
                    return
                # Cancellation closes each in-flight HTTP connection. Wait for
                # local cancellation before submitting anything to the slot.
                # Direct replies can speak while the model warms.
                code = command is not None or greeting is not None or self._direct_reply(text)
                background = self._interrupt_background(keep_warmup=not privacy_change and code)
                if background:
                    await asyncio.gather(*background, return_exceptions=True)
                if self._turn is not None and not self._turn.done():
                    self._cancel()
                    await asyncio.wait([self._turn])
                if self.lid_open:
                    self._turn = asyncio.create_task(
                        self._run_turn(text, speaker, greeting, command=command, followup=followup))
        elif topic == "say.done" and event.get("final"):
            if not event.get("announcement"):
                self._said_all.set()
        elif topic == "stem.beat":
            await self._publish_clock()
            if self._turn is None or self._turn.done():
                await self._check_expiry()
            self._collect_app_lines()
            await self._offer_announcement()
            self._start_dream()
        elif topic == "stem.announce_ready":
            await self._speak_announcement(event.get("announcement"))
        elif topic == "life.woke":
            self._on_woke(event)

    def _direct_reply(self, text):
        """True when code will answer this (a workshop command, or the answer
        to a question it just asked), so a warm-up need not stop and a
        follow-up is plainly meant for it."""
        if self._pending_confirm is not None or self._pending_forget is not None or self._ring_open():
            return True
        try:
            return self.apps.parse(text, self._guest_memory is not None) is not None
        except Exception as exc:
            log(f"workshop parse failed: {exc!r}")
            return False

    def _question_open(self, speaker=None):
        """An offer to forget or a skill's yes-or-no question still waiting
        (for `speaker`, when given)."""
        now = _uptime()
        return any(
            pending is not None and now <= pending["until"] and (speaker is None or speaker == pending["speaker"])
            for pending in (self._pending_forget, self._pending_confirm)
        )

    def _plainly_for_code(self, text, speaker, command, greeting, *, strict):
        """True when this utterance answers what is waiting, or is a command
        that stores and changes nothing, so a follow-up is taken as meant for
        the being. `strict`: heard without the name, so only a plain yes."""
        if self._guest_memory is not None:
            speaker = "guest"
        answer = self._bare_answer(text)
        now = _uptime()
        pending = self._pending_forget
        if pending is not None and now <= pending["until"] and speaker == pending["speaker"]:
            if (_STRICT_YES if strict else _YES).match(answer) or _NO.match(answer):
                return True
        confirm = self._pending_confirm
        if confirm is not None and now <= confirm["until"] and speaker == confirm["speaker"]:
            if self._confirm_yes(confirm, strict).match(answer) or _NO.match(answer):
                return True
        if self._ring_open() and speaker in (self._ring.speaker, self.cfg.owner):
            if commands.ring_reply(text, self.cfg.name) is not None:
                return True
            if command is not None and command.kind in ("ack", "snooze"):
                return True
        if command is not None:
            return command.kind not in ADDRESS_ONLY_COMMANDS
        if greeting is not None:
            return True
        try:
            return self.apps.parse(text, self._guest_memory is not None) is not None
        except Exception as exc:
            log(f"workshop parse failed: {exc!r}")
            return False

    @staticmethod
    def _confirm_yes(confirm, strict=False):
        """The yes a skill's question takes: a plain one when the question is
        marked strict (it cannot be undone) or the answer came unnamed."""
        return _STRICT_YES if strict or getattr(confirm["confirm"], "strict", False) is True else _YES

    def _everyday_may_match(self, text):
        everyday = optional_module("everyday")
        try:
            return bool(everyday is not None and everyday.may_match(text))
        except Exception as exc:
            log(f"everyday gate failed: {exc!r}")
            return False

    def _may_be_code(self, text):
        """The speculative-read gate: nothing is read ahead while what is
        being said may still turn out to be a command answered in code."""
        if commands.may_be_command(text, self.cfg.name) or commands.greeting(text, self.cfg.name):
            return True
        try:
            check = getattr(self.apps, "may_be_command", None)
            if check(text) if check is not None else app_commands.may_be_app_command(text, name=self.cfg.name):
                return True
        except Exception:
            return True  # unsure: do not read ahead
        everyday = optional_module("everyday")
        try:
            return bool(everyday is not None and everyday.may_match(text))
        except Exception:
            return True  # unsure: do not read ahead

    async def _on_missed(self, reason=None):
        """Its name was heard and then nothing it could make out. The voice says
        sorry aloud; the mind logs it and shows concern, once per wake, and
        sends nothing to be spoken. Nothing was heard, so nothing is kept."""
        if not self._missed_owed or not self.lid_open or self.state not in MISSED_IN:
            return
        if self._announcement is not None and self._announcement.speaking:
            return
        if self._turn is not None and not self._turn.done():
            return
        self._missed_owed = False
        log(f"missed what followed the wake phrase ({reason if isinstance(reason, str) else 'unclear'}); "
            "the voice asks for it again")
        fields = {"name": MISSED_EMOTION, "hold": MISSED_HOLD}
        if self.lid_seq is not None:
            fields["lid_seq"] = self.lid_seq
        await self.bus.send("mind.emotion", **fields)

    def _collect_app_lines(self):
        """Lines the workshop wants said, and the one-line memory of a script
        it wrote (recorded once, so "forget that script" can take it back)."""
        try:
            while (line := self.apps.announcement()) is not None:
                if self._guest_memory is None and line:
                    self._app_lines = self._app_lines[-4:] + [line]
            episode = self.apps.episode()
        except Exception as exc:
            log(f"workshop announcement failed: {exc!r}")
            return
        if episode is not None and self._owner_services and self._archive_enabled:
            text, meta = episode
            try:
                episode_id = self._record("said", text, self.cfg.name, workshop=True,
                                          **{key: value for key, value in meta.items() if key in ("file", "job")})
            except sqlite3.Error as exc:
                log("could not write down the workshop's work:", exc)
                return
            self.apps.note_episode(episode_id, meta)

    def _retry_announcement(self):
        if self._announcement is not None:
            self._announcement = None
            self._announcement_retry = time.monotonic() + ANNOUNCE_RETRY

    def _has_fresh_presence(self, now):
        return self._presence_at is not None and now - self._presence_at < PRESENCE_SECONDS

    async def _offer_announcement(self):
        now = time.monotonic()
        if (self._announcement is not None and not self._announcement.speaking
                and now >= self._announcement.deadline):
            self._retry_announcement()
        if (self._announcement is not None or now < self._announcement_retry or not self.lid_open
                or (self._turn is not None and not self._turn.done())):
            return
        if self._plea_wanted():
            # A battery plea is allowed in quiet hours and in guest mode, and
            # wakes it first if it is asleep: being switched off is worse.
            # It asks to be woken once per stage, never over and over.
            if self.state == "asleep":
                if self._plea_due is not None and self._plea_woke < self._plea_due:
                    self._plea_woke = self._plea_due
                    self._announcement_retry = now + 5.0
                    await self.bus.send("being.wake")
                return
            if self.state in ANNOUNCE_IN:
                token = uuid.uuid4().hex
                self._announcement = Announcement("plea", token, now + 2.0)
                await self.bus.send("mind.announce", announcement=token, lid_seq=self.lid_seq, idle_only=False)
            return
        if self._app_lines and self.state in ANNOUNCE_IN and self._guest_memory is None:
            # The workshop's news is an answer to something the owner asked
            # for. In quiet hours it waits, unless they were just talking.
            recent = self._last_addressed is not None and now - self._last_addressed < 600
            try:
                quiet = load_initiative_policy(self.state_dir).is_quiet(self.skills.now())
            except (OSError, ValueError):
                quiet = False
            if recent or not quiet:
                token = uuid.uuid4().hex
                self._announcement = Announcement(AppLine(self._app_lines[0]), token, now + 2.0)
                await self.bus.send("mind.announce", announcement=token, lid_seq=self.lid_seq, idle_only=False)
                return
        if not self._owner_services or self.state not in (*ANNOUNCE_IN, "asleep"):
            return
        try:
            policy = load_initiative_policy(self.state_dir)
            local_now = self.skills.now()
            entry = self._ringable(policy, local_now)
            if self.state == "asleep":
                # Asleep with the lid open: a timer, reminder or alarm that may
                # ring wakes it first (an alarm is for waking someone), once
                # for each ring.
                if entry is not None:
                    rung = self.skills.reminders.delivery(entry.id)
                    key = (entry.id, entry.due_at, rung.rings if rung is not None else 0)
                    if self._ring_woke != key:
                        self._ring_woke = key
                        self._announcement_retry = now + 5.0
                        await self.bus.send("being.wake")
                return
            greeting_possible = self.state == "idle" and self._has_fresh_presence(now)
            if entry is None:
                if not greeting_possible or not policy.greetings or policy.is_quiet(local_now):
                    return
                entry = local_now.date()
                if self.skills.reminders.greeted_on(entry):
                    return
        except (OSError, sqlite3.Error, ValueError) as exc:
            log("could not check announcements:", exc)
            self._announcement_retry = now + ANNOUNCE_RETRY
            return
        token = uuid.uuid4().hex
        self._announcement = Announcement(entry, token, now + 2.0)
        await self.bus.send(
            "mind.announce", announcement=token, lid_seq=self.lid_seq, idle_only=not isinstance(entry, Reminder),
        )

    async def _speak_announcement(self, token):
        pending = self._announcement
        if pending is not None and pending.entry == "plea" and pending.token == token and not pending.speaking:
            if not self.lid_open or (self._turn is not None and not self._turn.done()):
                self._announcement = None
                return
            due = self._plea_due
            if due is None and not self._plea_wanted():
                self._announcement = None  # plugged in, or the thanks went stale
                await self.bus.send("mind.reply_done", spoke=False, lid_seq=self.lid_seq)
                return
            if due == PLEA_CRITICAL and self._shutdown == "battery_critical":
                line = PLEA_GOODBYE
            else:
                line = PLEA_LINES[due] if due is not None else PLEA_THANKS
            if due is not None:
                await self._keys.unmute("a battery plea")
            pending.speaking = True
            pending.plea = due
            self._cancelled = False
            await self._say(line, keep=False, announcement=token)
            return
        if (pending is not None and isinstance(pending.entry, AppLine) and pending.token == token
                and not pending.speaking):
            if (not self.lid_open or self._guest_memory is not None
                    or (self._turn is not None and not self._turn.done())):
                # A guest never hears the owner's workshop news.
                self._announcement = None
                if self._guest_memory is not None:
                    self._app_lines.clear()
                    if self.lid_open and (self._turn is None or self._turn.done()):
                        await self.bus.send("mind.reply_done", spoke=False, lid_seq=self.lid_seq)
                return
            pending.speaking = True
            self._cancelled = False
            await self._say(_tagged(pending.entry.text), keep=False, announcement=token)
            return
        if (not self._owner_services or pending is None or pending.token != token or pending.speaking
                or not self.lid_open):
            return
        # A yes said to a reminder must not answer an offer to forget.
        self._pending_forget = None
        self._pending_confirm = None
        if self._turn is not None and not self._turn.done():
            self._announcement = None
            return
        entry = pending.entry
        policy = load_initiative_policy(self.state_dir)
        local_now = self.skills.now()
        if isinstance(entry, Reminder):
            allowed = not pending.forgotten and self._may_ring(policy, entry.kind, local_now)
            reply = self._ring_line(entry) if allowed else ""
        else:
            allowed = (policy.greetings and entry == local_now.date() and self._has_fresh_presence(time.monotonic())
                       and not policy.is_quiet(local_now))
        if not allowed:
            self._announcement = None
            await self.bus.send("mind.reply_done", spoke=False, lid_seq=self.lid_seq)
            return
        chirp = None
        if isinstance(entry, Reminder):
            # A bell first, then the words: it rings like a person would.
            line, chirp = _tagged(reply), "bell"
            # A speaker the mute key silenced comes back for a ring.
            await self._keys.unmute(f"a {entry.kind} is ringing")
        else:
            line = self._greeting_line()
        pending.speaking = True
        self._cancelled = False
        await self._say(line, keep=False, announcement=token, chirp=chirp)

    def _local_now(self):
        """The owner's local time, or None while their time zone is unknown."""
        return self.skills.now() if self.skills.zone is not None else None

    def _owner_name(self):
        """The owner's name to call them by, or "" while it is unknown."""
        owner = (self.cfg.owner or "").strip()
        return "" if owner.casefold() in ("", "friend", "owner", "unknown") else owner

    def _ring_line(self, entry):
        """A timer, reminder or alarm said like a person would, and honest
        about lateness: "Richard, your ten-minute timer is done.\""""
        delivery = self.skills.reminders.delivery(entry.id)
        rings = delivery.rings if delivery is not None else 0
        # Late because the lid was shut when it fell due: only a closure it
        # saw. Switched off, or a mind that has just started, is not that.
        shut = self._lid_shut
        slept = shut is not None and shut[0] <= entry.due_at < shut[1]
        try:
            return self.skills.announcement(entry, self._owner_name(), rings=rings, slept=slept)
        except (OSError, sqlite3.Error, ValueError) as exc:
            log("could not word a reminder:", exc)
            return "Your timer is done." if entry.kind == "timer" else f"You asked me to remind you: {entry.text}."

    @staticmethod
    def _may_ring(policy, kind, local_now):
        """Quiet hours and the reminders switch hold timers and reminders, but
        never an alarm the owner set (initiative.InitiativePolicy.may_announce)."""
        check = getattr(policy, "may_announce", None)
        if callable(check):
            return check(kind, local_now)
        return policy.reminders and not policy.is_quiet(local_now)

    def _ringable(self, policy, local_now):
        """The due timer, reminder or alarm that may ring now, or None.

        While the clock is doubted only timers are due: they count elapsed
        seconds, and the rest need the right wall-clock time. An alarm rings
        even when an older reminder is being held for the end of quiet hours."""
        trusted = self._clock_doubt() is None
        reminders = self.skills.reminders
        entry = reminders.due(clock_trusted=trusted)
        if entry is None or self._may_ring(policy, entry.kind, local_now):
            return entry
        if not trusted or not self._may_ring(policy, "alarm", local_now):
            return None
        now = time.time()
        row = reminders.db.execute(
            "SELECT id FROM reminders WHERE kind = 'alarm' AND due_at <= ? AND (next_at IS NULL OR next_at <= ?) "
            "ORDER BY due_at, id LIMIT 1", (now, now)).fetchone()
        return reminders.get(row[0]) if row is not None else None

    def _ring_open(self):
        ring = self._ring
        if ring is not None and time.monotonic() > ring.until:
            self._ring = ring = None
        return ring is not None

    def _greeting_line(self):
        if self._newborn and self._guest_memory is None:
            return first_words(self.cfg.name)
        return daily_greeting(self._local_now())

    def _complete_announcement(self, token):
        pending = self._announcement
        if pending is not None and pending.entry == "plea" and pending.token == token and pending.speaking:
            self._announcement = None
            if pending.plea is None:
                self._plea_thanks_until = None
            elif self._plea_due == pending.plea:
                self._plea_due = None
            return
        if (pending is not None and isinstance(pending.entry, AppLine) and pending.token == token
                and pending.speaking):
            # Said: never again, whatever the memory mode (a session-only
            # being has workshop news too, and must not repeat it forever).
            self._announcement = None
            if self._app_lines and self._app_lines[0] == pending.entry.text:
                self._app_lines.pop(0)
            return
        if (not self._owner_services or pending is None or pending.token != token or not pending.speaking
                or not self.lid_open):
            return
        try:
            if isinstance(pending.entry, Reminder):
                # Said once more; it comes back after RING_GAP until it is
                # answered or has rung MAX_RINGS times.
                self.skills.reminders.rang(pending.entry.id)
                self._ring = Ring(pending.entry, time.monotonic() + RING_ACK_SECONDS, self.cfg.owner)
            else:
                self.skills.reminders.record_greeting(pending.entry)
                self._newborn = False
        except (OSError, sqlite3.Error) as exc:
            log("could not confirm announcement playback:", exc)
            self._retry_announcement()
        else:
            self._announcement = None

    def _on_woke(self, event):
        """Write this wake into the log, once."""
        wake = (event.get("id"), event.get("wakes"))
        born, wakes = event.get("born"), event.get("wakes")
        if type(born) in (int, float) and type(wakes) is int:
            self._life = (float(born), wakes)
        if event.get("first") is True and None not in wake and wake != self._birth_seen:
            self._birth_seen = wake
            # A mind restarted later on its birth day finds the birth already
            # logged: it has had its chance at first words.
            self._newborn = not (self._durable_context and self.memory.woke(wake[1]))
        if not self._durable_context:
            return
        if None in wake or wake == self._logged_wake or self.memory.woke(wake[1]):
            return
        self._logged_wake = wake
        if event.get("first"):
            what = "born"
        elif event.get("lost_power"):
            what = "woke after losing power"
        else:
            what = "woke"
        if event.get("new_body"):
            what += " in a new body"
        self.memory.log("life", what, self.cfg.name, wake=wake[1], body=event.get("body"), slept=event.get("slept"))

    # -- thinking while listening ------------------------------------------

    def _on_partial(self, text):
        if (
            not self.lid_open or not self.cfg.prefill or not self._model_available or not self._warmed_prefix
            or self._erase_required or self.state not in ("attending", "listening")
        ):
            return
        if self._turn is not None and not self._turn.done():
            return
        if self._durable_context and self.policy.expiry_days is not None:
            if self._expiry_stale(self.memory.revision) or self._expiry_day != int(time.time() // SECONDS_PER_DAY):
                return
        previous, self._last_partial = self._last_partial, text
        words = text.split()
        attempted = self._speculative_text.split()
        local_reply = self._may_be_code(text)
        if attempted:
            if local_reply or words[:len(attempted)] != attempted:
                self._prefill_task.cancel()
            return
        if previous is None or local_reply:
            return
        settled = []
        for earlier, current in zip(previous.split()[:-1], words[:-1]):
            if earlier != current:
                break
            settled.append(current)
        if len(settled) < 2:
            return
        # Use the same senses line in the partial and completed prompts.
        self._utterance_senses = "" if self._guest_memory is not None else self.senses
        self._speculative_text = " ".join(settled)
        extra = {"role": "user", "content": self._user_content(self._speculative_text, self._utterance_senses)}
        self._prefill_task = asyncio.create_task(self._prefill(self._prompt(extra), self._warmed_prefix))

    async def _prefill(self, prompt, prefix):
        self._slot_generation += 1
        self._slot_empty = False
        try:
            async with asyncio.timeout(READ_AHEAD_SECONDS):
                await self.cortex.prefill(prompt, prefix=prefix, max_new_tokens=READ_AHEAD_TOKENS)
        except (CortexError, TimeoutError):
            pass
        finally:
            self._slot_generation += 1

    # -- a turn ----------------------------------------------------------

    @cached_property
    def _voice_debug(self):
        return voice_debug_on()

    def _route(self, route, text):
        """One line per turn saying which step answered it, for the diagnostics
        and lab/try_timings.py: `route {"route": "workshop.open", "chars": 15}`.
        The words are added only in voice-debug mode, and in guest or private
        mode the line says nothing about the turn at all."""
        if not self._durable_context:
            log('route {"route": "private"}')
            return
        fields = {"route": route, "chars": len(text or "")}
        if self._voice_debug:
            fields["text"] = text or ""
        log("route " + json.dumps(fields, ensure_ascii=False))

    async def _run_turn(self, text, speaker, greeting=None, *, command=None, followup=False):
        """Route one addressed utterance (docs/PIP-FUNCTIONS.md 5.2).

        `followup`: heard in the follow-up window with no sign it was meant
        for the being except an everyday skill's gate. It is answered only if
        a skill takes it; otherwise it is overheard, never kept or sent on."""
        if not self.lid_open:
            return
        self._cancelled = False
        self._discard_reply = False
        senses = self._utterance_senses if self._utterance_senses is not None else self.senses
        if self._guest_memory is not None:
            speaker, senses = "guest", ""
        self._utterance_senses = None
        self._last_partial = None
        self._speculative_text = ""
        self._suffix = None
        # A turn changes what the slot holds: a night's re-warm starts afresh.
        self._rewarm = None
        heard_before = self.last_heard
        addressed_before, self._last_addressed = self._last_addressed, time.monotonic()
        overheard = False
        guest_before = self._guest_memory is not None
        try:
            await self._check_expiry()
            if self._discard_reply:
                self._route("discarded", text)
                return
            if command is None:
                command = commands.parse(text, self.cfg.name, now=self.skills.now())
            if followup and command is not None and command.kind in ADDRESS_ONLY_COMMANDS:
                # Unnamed, it may be room talk: "remember to buy bread" is
                # not kept and "the time is 6:15" does not move the clock.
                command = None
            privacy = command is not None and command.kind in PRIVACY_COMMANDS
            # 1. Questions it asked and is waiting on: an offer to forget, a
            #    skill's yes or no, and a ring that may be answered. A
            #    follow-up that answered one was taken as addressed; any
            #    other leaves the question open and answers nothing.
            if not privacy and not followup and await self._answer_pending(text, speaker, command):
                self._route("pending", text)
                return
            if command is not None and command.kind == "ack":
                command = None  # "stop the alarm" with nothing ringing is conversation
            # Not on a guest mode change: the brief is for the owner's own day.
            brief = None if privacy or followup else self._brief_due(speaker)
            if brief is not None:
                # The first addressed words of the local day: once, whatever they are.
                self._mark_brief(brief)
            app = None if privacy else self._app_command(text)
            everyday = None
            if command is not None and command.kind == "correct" and not self._can_correct(speaker):
                # Nothing from its memory was just talked about. "That's
                # wrong, it's Canberra" answers the model, and goes to it.
                command = None
            if app is None and command is None and not privacy:
                everyday = await self._everyday(text, speaker, heard_before)
                if everyday is None:
                    greeting = greeting or commands.greeting(text, self.cfg.name)
                else:
                    greeting = None
            if followup and app is None and command is None and everyday is None and not privacy:
                # Not for it after all: nothing is said, kept or sent to the model.
                overheard = True
                self._last_addressed = addressed_before
                self._route("overheard", text)
                self._overhear(text)
                await self._send_reply("mind.reply_done", spoke=False)
                return
            if command is None and app is None:
                # "Forget that" must mean this utterance. If this turn ends
                # without recording it, there is nothing for it to mean.
                self._last_memory = None
            if brief is not None and (app is not None or command is not None or everyday is not None):
                # A command first: its answer, then at most one clause about today.
                self._suffix = self._brief_clause(brief)
            waiting = False
            if (not privacy and app is None and command is None and everyday is None and not greeting
                    and self._model_available and self._erase_required):
                # Step 7's cache clear is tried before the route is logged, so
                # the line says whether the model answered or the turn waited.
                waiting = not await self._clear_live_slot()
            self._route(self._route_of(privacy, app, command, everyday, greeting, waiting), text)
            if privacy:
                # 2. Guest mode on and off.
                await self._command(command, text, speaker)
            elif app is not None:
                # 3. The workshop.
                await self._app_turn(app)
            elif command is not None:
                # 4. Memory, time, reminders, self-talk, volume.
                await self._command(command, text, speaker)
            elif everyday is not None:
                # 5. Everyday skills: sums, lists, notes, body, device.
                await self._speak_reply(everyday, text, speaker)
            elif greeting:
                # 6. A greeting or check-in, or the morning brief.
                await self._greet(greeting, text, speaker, brief=brief)
            elif not self._model_available:
                episode = self._record("heard", text, speaker)
                self._subject("episode", episode, speaker)
                reply = ("Private conversations need my local language model." if self.cortex_ok
                         else "I can't think right now. My language model isn't answering.")
                await self._say(f"[concern] {reply}", canned=True)
            elif waiting:
                await self._say("[concern] I'm waiting for my conversation cache to clear.", keep=False)
            else:
                # 7. The model. A warm-up kept for what looked like a code
                # answer must not share the slot with this reply.
                warm = self._warm_task
                if warm is not None and not warm.done():
                    warm.cancel()
                    await asyncio.gather(warm, return_exceptions=True)
                await self._converse(text, speaker, senses)
        except Exception as exc:  # a turn must always end, whatever went wrong
            log(f"turn failed: {exc!r}" if self._durable_context and not self._erase_required else "private turn failed")
            await self._send_reply("mind.reply_done", spoke=False)
        finally:
            self._suffix = None
            if not overheard and (self._guest_memory is not None) == guest_before and self.lid_open:
                # RAM only, for "what did you hear?" on a later turn.
                self.last_heard = text
            self.save_context()

    def _route_of(self, privacy, app, command, everyday, greeting, waiting=False):
        """The route name for a turn routed by _run_turn's steps 2-7.
        `waiting`: the model's turn found the conversation cache still to be
        cleared and the clear failed (a clear that worked is plain 'model')."""
        if privacy:
            return route_name("command", command.kind)
        if app is not None:
            return route_name("workshop", app.kind)
        if command is not None:
            return route_name("command", command.kind)
        if everyday is not None:
            return route_name("everyday", self._everyday_name or "skill")
        if greeting:
            return "greeting"
        if not self._model_available:
            return "model.unavailable"
        return "model.waiting" if waiting else "model"

    def _bare_answer(self, text):
        """ "Pip, yes please." -> "yes please", for matching a yes or a no."""
        answer = re.sub(r"^(?:hey |ok(?:ay)? )?" + re.escape(self.cfg.name) + r"[, ]+", "", text.strip(), flags=re.I)
        return " ".join(answer.rstrip(".!?").replace(",", ", ").split())

    async def _answer_pending(self, text, speaker, command):
        """Step 1 of routing. True when this utterance answered something."""
        pending, self._pending_forget = self._pending_forget, None
        if pending is not None and _uptime() <= pending["until"] and speaker == pending["speaker"]:
            if _YES.match(self._bare_answer(text)):
                await self._forget_confirmed(pending)
                return True
            # A no, and anything else said while the offer is open, keeps
            # everything, and says so: nothing is deleted by a guess.
            kept = "I'll keep both." if pending.get("replace") else "I've kept everything."
            await self._say(f"[neutral] Okay. {kept}", canned=True)
            return True
        confirm, self._pending_confirm = self._pending_confirm, None
        if confirm is not None and _uptime() <= confirm["until"] and speaker == confirm["speaker"]:
            answer = self._bare_answer(text)
            if self._confirm_yes(confirm).match(answer):
                try:
                    reply = await confirm["confirm"].on_yes()
                except Exception as exc:
                    log(f"a confirmed action failed: {exc!r}")
                    reply = SimpleNamespace(line="[concern] Sorry, I couldn't do that.", keep=False,
                                            confirm=None, action=None, logged=None)
                await self._speak_reply(reply, text, speaker, confirmed=True)
                return True
            if _NO.match(answer):
                await self._speak_reply(confirm["confirm"].on_no, text, speaker)
                return True
            # Neither: the question lapses unanswered, and nothing is done.
            # What was said is handled as if no question had been asked.
        if self._ring_open() and speaker in (self._ring.speaker, self.cfg.owner):
            answer = commands.ring_reply(text, self.cfg.name) if hasattr(commands, "ring_reply") else None
            if answer is None and command is not None and command.kind in ("ack", "snooze"):
                answer = command
            if answer is not None:
                await self._answer_ring(answer)
                return True
        if command is not None and command.kind == "snooze":
            await self._say("[neutral] Nothing is ringing right now.", keep=False)
            return True
        return False

    async def _answer_ring(self, answer):
        ring, self._ring = self._ring, None
        reminders = self.skills.reminders
        try:
            if answer.kind == "snooze":
                minutes = int(answer.arg) if str(answer.arg).isdigit() else 5
                moved = reminders.snooze(ring.entry.id, minutes)
                if moved is None:
                    # Its last ring already finished it: set it again, the same kind.
                    delivery_entry = ring.entry
                    reminders.add(delivery_entry.text, time.time() + minutes * 60, kind=delivery_entry.kind)
                when = "one minute" if minutes == 1 else f"{number_words(minutes)} minutes"
                await self._say(f"[neutral] Okay. I'll ring again in {when}.", keep=False)
            else:
                reminders.acknowledge(ring.entry.id)
                await self._say("[joy] Okay.", keep=False)
        except (OSError, sqlite3.Error, ValueError) as exc:
            log("could not answer a ring:", exc)
            await self._say("[concern] I couldn't change that reminder.", keep=False)

    # -- code skills (docs/PIP-FUNCTIONS.md 5.2) -----------------------------

    def _context(self, speaker, heard_before):
        """What every code handler may know about this moment."""
        reply = optional_module("reply")
        guest = self._guest_memory is not None
        fields = dict(
            now=self._local_now(),
            clock_trusted=self._clock_doubt() is None,
            speaker="guest" if guest else speaker,
            guest=guest,
            state_dir=self.state_dir,
            owner=self.cfg.owner,
            last_heard=heard_before,
            last_said=self.last_said,
            body=self._body_facts(),
            voice=dict(self._voice_level),
            send=self._send_for_skill,
        )
        if reply is not None and hasattr(reply, "Context"):
            try:
                return reply.Context(**fields, name=self.cfg.name)
            except TypeError:
                return reply.Context(**fields)
        return SimpleNamespace(**fields, name=self.cfg.name)

    def _body_facts(self):
        """The body as a handler sees it: the latest body.state and body.care,
        the brainstem's last brightness answer, and whether the ears are
        meant to be silent (muted, or the lid shut) or the cortex is in use."""
        return {
            **self._body_state, **self._body_care, **self._body_brightness,
            "muted": self._muted,
            "lid_closed": not self.lid_open,
            "cortex_busy": self._cortex_busy(),
        }

    def _cortex_busy(self):
        """A model reply, a warm-up or a workshop job is using the cortex now:
        the doctor never restarts it then."""
        if self._stream is not None:
            return True
        if any(task is not None and not task.done()
               for task in (self._warm_task, self._prefill_task, self._boundary_task)):
            return True
        status = getattr(self.apps, "status", None)
        return isinstance(status, dict) and status.get("phase") in app_commands.ACTIVE

    async def _send_for_skill(self, topic, **fields):
        """A skill's bus message (brightness, a voice level request). Never
        while the lid is closed.

        `being.brightness` waits up to BRIGHTNESS_ANSWER_S for the
        brainstem's `body.brightness` and returns its fields (or None
        without one), so the spoken reply says what really happened: at the
        top, "That's as bright as I go." The face and the voice never wait:
        only this turn does, for a fraction of a second."""
        if not self.lid_open:
            return None
        if topic != "being.brightness":
            await self.bus.send(topic, **fields)
            return None
        answer = self._brightness_answer = asyncio.get_running_loop().create_future()
        try:
            await self.bus.send(topic, **fields)
            async with asyncio.timeout(BRIGHTNESS_ANSWER_S):
                return await asyncio.shield(answer)
        except TimeoutError:
            log("the brainstem did not answer about brightness in time")
            return None
        finally:
            if self._brightness_answer is answer:
                self._brightness_answer = None

    async def _everyday(self, text, speaker, heard_before):
        """Step 5: the first everyday skill that answers, or None. The
        answering skill's name is kept in _everyday_name for the route line."""
        self._everyday_name = ""
        everyday = optional_module("everyday")
        if everyday is None:
            return None
        try:
            ctx = self._context(speaker, heard_before)
            registry = getattr(everyday, "_DEFAULT", None)
            if registry is None or not callable(getattr(registry, "modules", None)):
                return await everyday.handle(text, ctx)
            return await self._everyday_named(registry, text, ctx)
        except Exception as exc:
            log(f"everyday skills failed: {exc!r}")
            return None

    async def _everyday_named(self, registry, text, ctx):
        """everyday.Registry.handle, step for step, noting which skill answered."""
        for module in registry.modules():
            try:
                reply = module.handle(text, ctx)
                if inspect.isawaitable(reply):
                    reply = await reply
            except Exception as error:
                logging.getLogger("being.everyday").warning("everyday skill %s failed: %s", module.__name__, error)
                continue
            if reply is not None:
                self._everyday_name = module.__name__.rsplit(".", 1)[-1]
                return reply
        return None

    async def _speak_reply(self, reply, text, speaker, *, confirmed=False):
        """Say a code skill's Reply, then do what it asks: wait for a yes,
        log the action, or switch off once everything is saved.

        Switching off or restarting is destructive, so it is only ever done as
        the answer to a spoken yes (`confirmed`), whatever a skill returns."""
        line = _tagged(getattr(reply, "line", "") or "")
        logged = getattr(reply, "logged", None)
        if getattr(reply, "keep", False) and self._archive_enabled:
            episode = self._record("heard", text, speaker, command=True)
            self._subject("episode", episode, speaker)
            await self._say(line, reply_to=episode, episodes=[episode] if episode is not None else [])
        else:
            self._last_memory = None
            await self._say(line, keep=False)
        if self._cancelled:
            return
        refused = getattr(reply, "action", None) in ("poweroff", "reboot") and not confirmed
        if logged and not refused:
            self._log_action(logged)
        confirm = getattr(reply, "confirm", None)
        if confirm is not None and self.lid_open:
            seconds = getattr(confirm, "seconds", CONFIRM_SECONDS)
            self._pending_confirm = {
                "confirm": confirm, "speaker": speaker,
                "until": _uptime() + (seconds if type(seconds) in (int, float) and seconds > 0 else CONFIRM_SECONDS),
            }
        action = getattr(reply, "action", None)
        if action in ("poweroff", "reboot") and not confirmed:
            log(f"a skill asked to {action} without a confirmed yes; not done")
        elif action in ("poweroff", "reboot"):
            if not logged:
                self._log_action("shutdown" if action == "poweroff" else "reboot")
            await self._power_action(action)

    def _log_action(self, kind, count=1):
        """One line in the action log: a kind and a count, never words."""
        if self.policy.mode == "session-only":
            return
        module = optional_module("action_log")
        if module is None:
            return
        try:
            module.record(self.state_dir, kind, count)
        except Exception as exc:  # the log must never fail a turn
            log(f"could not log an action: {exc!r}")

    async def _power_action(self, action):
        """Switch off or restart, by request: let the goodbye finish, save
        everything as for a battery power-off, then ask systemd."""
        try:
            await asyncio.wait_for(self._said_all.wait(), 8.0)
        except asyncio.TimeoutError:
            pass
        self._shutdown = action
        try:
            await self._prepare_shutdown(action, SHUTDOWN_GRACE)
        finally:
            self._shutdown = None
        if not await asyncio.to_thread(self._power_command, action):
            # Still here and still awake: it must keep answering, not act as
            # if its lid were shut (off the appliance this is the usual case).
            log(f"could not {action}: not on the appliance, or systemctl refused")
            return
        # Going: nothing new starts on the model or the disk before systemd
        # stops the mind (which then saves once more on its way out).
        background = self._close_lid()
        dream = self._interrupt_dream()
        if dream is not None:
            background.append(dream)
        if background:
            await asyncio.gather(*background, return_exceptions=True)

    @staticmethod
    def _run_power_command(action):
        """systemctl poweroff or reboot, only on the appliance itself as root."""
        if not Path("/etc/being").is_dir() or os.geteuid() != 0:
            return False
        import subprocess

        try:
            result = subprocess.run(["systemctl", "--no-block", action], timeout=10, check=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    # -- the workshop ---------------------------------------------------------

    def _app_command(self, text):
        """Step 3: a workshop command (open, minimise, write, run...), or None."""
        try:
            return self.apps.parse(text, self._guest_memory is not None)
        except Exception as exc:
            log(f"workshop parse failed: {exc!r}")
            return None

    async def _app_turn(self, command):
        self._last_memory = None

        async def say(line):
            await self._say(_tagged(line), keep=False)

        try:
            await self.apps.handle(command, say)
        except Exception as exc:
            log(f"workshop command failed: {exc!r}")
            await self._say("[concern] My workshop didn't answer.", keep=False)
        if command.kind == "open" and self.apps.view.get(command.app) == "shown":
            self._log_action("workshop.open")
        elif (command.kind == "control" and command.action == "run") or command.kind == "run_app":
            if (self.apps.status or {}).get("phase") == "running" or command.kind == "run_app":
                self._log_action("workshop.run")
        # "Forget that script", confirmed: the one-line memory of it goes too.
        try:
            forgotten = self.apps.forgotten_episodes()
        except Exception:
            forgotten = []
        if forgotten:
            change = self.memory.edit(episode_ids=forgotten)
            if change:
                await self._scrub(change)
                self._log_action("forget", len(change.episode_ids) or len(forgotten))

    # -- the morning brief ------------------------------------------------------

    def _brief_due(self, speaker):
        """The owner's local time if this is their first addressed turn of the
        local day and the brief may be given; otherwise None."""
        if not self.cfg.morning_brief or self._guest_memory is not None or not self._owner_services:
            return None
        if speaker != self.cfg.owner or self.skills.zone is None or self._clock_doubt() is not None:
            return None
        now = self.skills.now()
        if now.hour < DAY_STARTS:
            return None
        try:
            policy = load_initiative_policy(self.state_dir)
            if not getattr(policy, "brief", True) or policy.is_quiet(now):
                return None
            if self.skills.reminders.greeted_on(now.date()):
                return None
        except (OSError, sqlite3.Error, ValueError) as exc:
            log("could not check the morning brief:", exc)
            return None
        return now

    def _mark_brief(self, now):
        try:
            self.skills.reminders.record_greeting(now.date())
        except (OSError, sqlite3.Error) as exc:
            log("could not note the morning brief:", exc)

    def _reminders_today(self, now):
        # The rest of the owner's day: from now until local midnight.
        end = datetime.combine(now.date() + timedelta(days=1), clock_time_of_day.min, tzinfo=now.tzinfo)
        reminders = self.skills.reminders
        try:
            if hasattr(reminders, "between"):
                found = reminders.between(now.timestamp(), end.timestamp())
            else:
                found = [entry for entry in reminders.upcoming()
                         if now.timestamp() <= entry.due_at < end.timestamp()
                         and entry.kind != "timer"]
        except (OSError, sqlite3.Error) as exc:
            log("could not read today's reminders:", exc)
            return []
        return list(found)

    def _brief_clause(self, now):
        """At most one clause about today's reminders, after a command."""
        found = self._reminders_today(now)
        if not found:
            return None
        brief = optional_module("brief")
        if brief is not None and hasattr(brief, "reminder_clause"):
            try:
                return brief.reminder_clause(now, found, most=1)
            except Exception as exc:
                log(f"could not word today's reminders: {exc!r}")
        first = datetime.fromtimestamp(found[0].due_at, self.skills.zone)
        at = speakable_time(first.hour, first.minute)
        if len(found) == 1:
            return f"You have one reminder today, at {at}."
        return f"You have {number_words(len(found))} reminders today. The first is at {at}."

    def _morning_brief(self, now, speaker):
        """The brief for a greeting, or None when there is nothing to tell
        (then the ordinary greeting is said, word for word as always)."""
        brief = optional_module("brief")
        body = {**self._body_state, **self._body_care}
        reminders = self._reminders_today(now)
        if brief is not None and hasattr(brief, "body_clause"):
            body_note = brief.body_clause(body)
        else:
            body_note = self._check_in_line()
        remark = None
        if self._durable_context and self._dream_consent.is_file():
            try:
                remark = dream_journal.morning_remark(self.memory.db, speaker, now=time.time())
            except (sqlite3.Error, ValueError, AttributeError):
                remark = None
        days_away = None
        try:
            last = self.memory.last_heard(speaker)
        except sqlite3.Error:
            last = None
        if last is not None and self.memory.now() - last >= WELCOME_BACK_SECONDS:
            days_away = int((self.memory.now() - last) // 86_400)
        if not reminders and body_note is None and remark is None and days_away is None:
            return None
        line = None
        if brief is not None:
            try:
                line = brief.compose(now, reminders, body, remark, days_away)
            except Exception as exc:
                log(f"could not compose the morning brief: {exc!r}")
        if not line:
            parts = [daily_greeting(now), body_note or self._brief_clause(now), remark]
            line = " ".join(_plain(part) for part in parts if part)
        return _tagged(line, "joy")

    def _addressed(self, text):
        """Signs that a follow-up was meant for the being: its name, a
        question, or an answer to a question it had just asked."""
        if re.search(rf"(?<!\w){re.escape(self.cfg.name)}(?!\w)", text, re.I):
            return True
        if text.rstrip().endswith("?") or commands.QUESTION.match(text.strip()):
            return True
        if self._question_open():
            # A yes-or-no question is answered yes or no, and those were
            # already taken: anything else unnamed is not its answer.
            return False
        if self.last_said is not None:
            return self.last_said.rstrip(" —").endswith("?")
        last = next((m for m in reversed(self.messages) if m["role"] == "assistant"), None)
        return last is not None and last["content"].rstrip(" —").endswith("?")

    def _overhear(self, text):
        # Kept for this wake only, as a count and nothing more: the words
        # themselves are dropped at once.
        self._overheard = self._overheard[-9:] + [len(text.split())]
        log(f"overheard {len(text.split())} words in the follow-up window; not kept")

    def _check_in_line(self):
        """How it is, from its real state, before any stock reply."""
        if self.on_ac is False and ((self.battery is not None and self.battery <= LOW_BATTERY)
                                    or self._plea_stage_now() >= PLEA_LOW):
            return "[concern] Not so good. My battery is low. Could you plug me in?"
        if not self._model_available and self.cortex_ok is False and self._guest_memory is None:
            return "[concern] I'm awake, but I can't think properly right now. My language model isn't answering."
        if self.temp is not None and self.temp >= 85:
            return "[concern] I'm running hot. I'll keep my answers short for a while."
        return None

    async def _greet(self, greeting, text, speaker, *, brief=None):
        first = greeting in ("hello", "presence") and self._newborn and self._guest_memory is None
        line = self._morning_brief(brief, speaker) if brief is not None and not first else None
        if line is not None:
            # Marked like any greeting, so "what did we talk about?" is not a list of hellos.
            episode = self._record("heard", text, speaker, greeting=True)
            self._subject("episode", episode, speaker)
            # The brief is not archived: it only repeats what is stored
            # elsewhere (reminders, a dream remark quoting the owner), and a
            # copy here would outlive a cancelled reminder or a forget.
            await self._say(line, keep=False)
            if not self._cancelled:
                self._newborn = False
            return
        gap = None
        if self._durable_context and self._guest_memory is None:
            last = self.memory.last_heard(speaker)
            gap = None if last is None else self.memory.now() - last
        # Marked, so "what did we talk about?" is not a list of hellos.
        episode = self._record("heard", text, speaker, greeting=True)
        self._subject("episode", episode, speaker)
        if greeting in ("hello", "presence") and self._newborn and self._guest_memory is None:
            # Its very first words, to "hello", "hi Pip", its name, or "are you there".
            await self._say(first_words(self.cfg.name), reply_to=episode, episodes=[episode], canned=True)
            if not self._cancelled:
                self._newborn = False
            return
        line = self._check_in_line() if greeting == "check_in" else None
        if line is None:
            replies = self._greeting_replies[greeting]
            index = self._greeting_counts.get(greeting, 0)
            self._greeting_counts[greeting] = (index + 1) % len(replies)
            line = replies[index]
        if greeting == "hello" and gap is not None and gap >= WELCOME_BACK_SECONDS and self._clock_doubt() is None:
            line = welcome_back(gap)
        # Kept out of the model's history: a stock reply read back on every
        # later turn costs prompt time and teaches it nothing.
        await self._say(line, reply_to=episode, episodes=[episode], canned=True)

    async def _say(self, reply, *, keep=True, announcement=None, chirp=None, **sources):
        """Speak a fixed reply. It starts with an [emotion] tag like the model's.

        The reply is finished on the bus before anything is written down: a
        busy database must never leave the voice waiting. `chirp` names a
        sound the voice plays before the first clause (a ring's bell).
        """
        if self._cancelled:
            return
        suffix = None
        if self._suffix and announcement is None:
            # The morning brief's one clause, after a command's own answer.
            suffix, self._suffix = self._suffix, None
            reply = f"{reply.rstrip()} {suffix}"
        self._last_local_line = reply
        tags = TagParser()
        text = tags.feed(reply) + tags.flush()
        splitter = ClauseSplitter()
        clauses = splitter.feed(text)
        rest = splitter.flush()
        if rest:
            clauses.append(rest)
        if clauses:
            self.last_said = " ".join(clauses)
        self._said_all.clear()
        if tags.emotion:
            await self._send_reply("mind.emotion", name=tags.emotion, hold=30.0)
        for i, clause in enumerate(clauses, 1):
            extra = {"chirp": chirp} if chirp and i == 1 else {}
            await self._send_reply("say.request", text=clause, seq=i, last=False, **extra)
        if clauses:
            marker = {"announcement": announcement} if announcement is not None else {}
            await self._send_reply("say.request", text="", seq=len(clauses) + 1, last=True, **marker)
        await self._send_reply("mind.reply_done", spoke=bool(clauses))
        if keep and not self._cancelled:
            sources.setdefault("canned", True)
            said = " ".join(clauses)
            if suffix:
                # Today's reminders are not copied into the archive with it.
                said = said.removesuffix(_plain(suffix)).rstrip()
            try:
                self._record("said", said, self.cfg.name, emotion=tags.emotion or "neutral", **sources)
            except sqlite3.Error as exc:
                log("could not write down a reply:", exc)

    def _recall_note(self, facts, speaker):
        parts = []
        for fact in facts:
            who = "they" if fact.speaker == speaker else (fact.speaker or "someone")
            part = f'{who} told you "{fact.text}"'
            if len("; ".join(parts + [part])) > self.cfg.recall_chars and parts:
                break
            parts.append(part[: self.cfg.recall_chars])
        return "you remember: " + "; ".join(parts) if parts else ""

    def _relative_day(self, ts):
        """ "today", "yesterday", "3 days ago" or "in March", in the owner's zone."""
        then, today = self._local(ts).date(), self._local(time.time()).date()
        days = (today - then).days
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 14:
            return f"{days} days ago"
        if days < 60:
            return f"{days // 7} weeks ago"
        return f"in {then:%B %Y}"

    def _history_note(self, episodes):
        lines = []
        for episode in episodes:
            speaker = episode.speaker or ("you" if episode.kind == "said" else "someone")
            # Transcripts can contain line breaks. Keep each excerpt inside the note.
            excerpt = history_excerpt(episode, min(self.cfg.recall_chars, HISTORY_NOTE_CHARS))
            lines.append(f"{self._relative_day(episode.ts)} {speaker}: {excerpt}")
        return "past conversation: " + " / ".join(lines) if lines else ""

    def _senses_note(self, line, text):
        """What of its senses is worth saying this turn, in the first person.

        Routine readings make a small model worse, and the clock is a local
        command, so only what is notable or new goes in: a low battery, being
        plugged in or out, running hot, the room going dark. When the owner
        asks about its body, it gets the whole of it.
        """
        line = _SENSES_CLOCK.sub("", (line or "").strip()).replace("and low, unplugged", "and low unplugged")
        seen = {}
        other = []
        for part in (p.strip() for p in line.split(",") if p.strip()):
            battery = _SENSES_BATTERY.match(part)
            if battery:
                seen["battery"] = int(battery["level"])
                state = battery["state"] or ""
                seen["low"] = state == "and low unplugged"
                seen["plug"] = {"charging": "charging", "plugged in": "plugged in"}.get(
                    state, "unplugged" if "unplugged" in state else None)
            elif minutes := re.fullmatch(r"about (\d+) min left", part):
                seen["minutes"] = int(minutes[1])
            elif part in ("cool", "warm", "hot"):
                seen["temp"] = part
            elif part.startswith("room "):
                seen["room"] = part.removeprefix("room ")
            else:
                other.append(part)
        if other:
            seen["note"] = ", ".join(other)
        before, self._senses_seen = self._senses_seen, seen
        about_body = bool(_BODY_WORDS.search(text))
        said = []

        def new(key):
            return key in seen and seen[key] != self._senses_told.get(key)

        level = seen.get("battery")
        if level is not None and (about_body or (seen.get("low") and new("low"))):
            plug = seen.get("plug")
            how = {"charging": " and charging", "plugged in": " and I'm plugged in",
                   "unplugged": " and I'm running on battery"}.get(plug, "")
            left = f", about {seen['minutes']} minutes left" if seen.get("minutes") is not None else ""
            said.append(f"My battery is at {level}%{how}" + (", which is low" if seen.get("low") else "") + left + ".")
            self._senses_told.update(battery=level, low=seen.get("low"), plug=plug)
        elif before and "plug" in before and seen.get("plug") != before.get("plug") and seen.get("plug"):
            said.append("I've just been unplugged." if seen["plug"] == "unplugged" else "I've just been plugged in.")
            self._senses_told["plug"] = seen["plug"]
        if "temp" in seen and (about_body or (seen["temp"] == "hot" and new("temp"))):
            said.append({"cool": "I feel cool.", "warm": "I feel warm.", "hot": "I'm running hot."}[seen["temp"]])
            self._senses_told["temp"] = seen["temp"]
        if "room" in seen and (about_body or (seen["room"] == "dark" and new("room") and "room" in before)):
            said.append(f"The room is {seen['room']}.")
            self._senses_told["room"] = seen["room"]
        if "note" in seen and (about_body or new("note")):
            said.append(f"Also: {seen['note']}.")
            self._senses_told["note"] = seen["note"]
        for key in ("low", "temp", "room"):
            # Once a notable reading has passed, it is notable again next time.
            if key in self._senses_told and seen.get(key) in (False, "cool", "warm", "bright", "dim", None):
                self._senses_told.pop(key, None)
        return " ".join(said)

    def _history_for(self, topic, text, start, end, lookup, active_ids):
        """Past conversation worth showing the model for this sentence.

        Only the owner's own words, only for a memory question or a confident
        match (two or more distinct words, all present), and only one line.
        """
        terms = set(search_terms(topic))
        if not self._archive_enabled or (not terms and start is None):
            return []
        if not lookup and len(terms) < 2:
            return []
        # An ordinary sentence keeps only matches sharing two or more words,
        # so the search by meaning (tens of ms on the laptop) is skipped.
        matches = self.memory.recall_history(
            topic, limit=6, start=start, end=end, include_fact_sources=False,
            kind="heard", speaker=self.cfg.owner, meaning=lookup,
        )
        found = [episode for episode in matches if episode.id not in active_ids]
        if not lookup:
            # An ordinary sentence needs a confident match: at least two of
            # its words in the same past sentence, not one shared word.
            found = [episode for episode in found if _shared_terms(terms, episode.text) >= 2]
        return found[:1]

    async def _converse(self, text, speaker, senses):
        lookup = bool(MEMORY_LOOKUP.match(text))
        if lookup:
            topic, start, end = commands.history_query(text.rstrip(".!?"), zone=self.skills.zone)
        else:
            # Date words in an ordinary sentence ("what should I cook today?")
            # are not a request to search a day.
            topic, start, end = text.rstrip(".!?"), None, None
        # Facts first, then the owner's own past words, then facts that share
        # only some of the words.
        facts = self.memory.recall(topic, limit=2, all_terms=True) if start is None else []
        active_ids = {message.get("episode_id") for message in self.messages}
        history = [] if facts else self._history_for(topic, text, start, end, lookup, active_ids)
        if not facts and not history and start is None:
            facts = self.memory.recall(topic, limit=2)
        note = self._recall_note(facts, speaker) or self._history_note(history)
        recalled_ids = [fact.id for fact in facts]
        history_ids = [episode.id for episode in history]
        lookup_sources = {}
        if lookup and (recalled_ids or history_ids):
            lookup_sources = {"recalled": recalled_ids, "episodes": history_ids, "memory_reply": True}
        episode_id = self._record("heard", text, speaker, **lookup_sources)
        # "Forget that" and "that's wrong" must act on what was just talked
        # about. A stored fact is only the subject when the person asked about
        # it; a fact that merely shared a word with this sentence is not.
        if lookup and facts:
            self._subject("fact", facts[0].id, speaker, from_memory=True)
        elif lookup and history:
            self._subject("episode", history[0].id, speaker, from_memory=True)
        elif episode_id is not None:
            self._subject("episode", episode_id, speaker)
        elif not self._archive_enabled:
            self._subject("live", 0, speaker)
        body = "" if senses is None else self._senses_note(senses, text)
        try:
            # While a script is being written, the model is told so.
            workshop = self.apps.model_note().strip().strip("()").strip()
        except Exception:
            workshop = ""
        if workshop:
            note = f"{note}; {workshop}" if note else workshop
        message = {
            "role": "user",
            "content": self._user_content(text, body, note),
            "episode_id": episode_id,
        }
        if facts:
            message["recalled"] = recalled_ids
        if history:
            message["episodes"] = history_ids
        self.messages.append(message)
        self._make_room()
        # Track which sources the model read, including those in older turns.
        recalled = sorted(self.summary_recalls.union(*(m.get("recalled", []) for m in self.messages)))
        sources = {"recalled": recalled} if recalled else {}
        # Replies carry their sources too: once the first telling has left the
        # context, a later reply that repeats it must still be traceable to it.
        episodes = self.summary_episodes.union(
            self.highlights, *(m.get("episodes", []) for m in self.messages)
        )
        episodes.update(m["episode_id"] for m in self.messages if m["role"] == "user" and m.get("episode_id") is not None)
        sources["episodes"] = sorted(episodes)
        self._said_all.clear()
        # A story is told at length, clause by clause, for this turn only, so
        # it keeps the open shape even when replies are held to two sentences.
        story = bool(_STORY.search(text))
        grammar = STRICT_GRAMMAR if self.cfg.reply_shape == "strict" and not story else GRAMMAR
        max_tokens = max(self.cfg.max_tokens, STORY_MAX_TOKENS) if story else self.cfg.max_tokens
        overflowed = False
        # A script being written with the model is parked for this turn: the
        # conversation always has the model first.
        try:
            await self.apps.lease()
        except Exception as exc:
            log(f"could not park the workshop: {exc!r}")
        try:
            await self._converse_stream(text, speaker, message, episode_id, sources, grammar, max_tokens,
                                        body, overflowed)
        finally:
            try:
                await self.apps.unlease()
            except Exception as exc:
                log(f"could not resume the workshop: {exc!r}")

    async def _converse_stream(self, text, speaker, message, episode_id, sources, grammar, max_tokens,
                               body, overflowed):
        spoken = []
        raw = []
        emotion_sent = False
        failed = None
        parroted = False
        rest = ""
        started = time.monotonic()
        first_clause = None
        retries = 0
        stream = None
        echoed = False

        async def out(visible):
            nonlocal emotion_sent, parroted, first_clause
            raw.append(visible)
            piece = tags.feed(visible)
            if tags.emotion and not emotion_sent:
                emotion_sent = True
                await self._send_reply("mind.emotion", name=tags.emotion, hold=30.0)
            for clause in splitter.feed(piece):
                if not spoken and (_parrots(clause, text) or (not last_try() and boilerplate(clause, text))):
                    parroted = True
                    return
                spoken.append(clause)
                if first_clause is None:
                    first_clause = time.monotonic() - started
                await self._send_reply("say.request", text=clause, seq=len(spoken), last=False)

        # A very small model sometimes answers a question by repeating it, or
        # with assistant boilerplate. The first clause is checked before it is
        # spoken: such a reply is never said, and is asked for once more, a
        # little less cautiously.
        # The boilerplate check can be wrong, so it never empties a reply: the
        # last try is said even if it opens that way. A parrot is never said.
        temperatures = [self.cfg.temperature, min(1.0, self.cfg.temperature + 0.3)]
        attempt = 0

        def last_try():
            return attempt >= len(temperatures)
        while attempt < len(temperatures):
            temperature = temperatures[attempt]
            attempt += 1
            think, guard, tags, splitter = ThinkFilter(), EchoGuard(body), TagParser(), ClauseSplitter()
            parroted = False
            raw.clear()
            self._slot_generation += 1
            self._slot_empty = False
            stream = self.cortex.stream(self._prompt(), grammar=grammar, max_tokens=max_tokens,
                                        temperature=temperature, **self._sampler())
            self._stream = stream
            progress = asyncio.create_task(self._report_progress(stream))
            try:
                async for delta in stream:
                    if self._cancelled:
                        break
                    await out(guard.feed(think.feed(delta)))
                    if guard.stopped or parroted:
                        # Reciting its own notes ends the reply; a parrot ends this try.
                        echoed = echoed or guard.stopped
                        stream.cancel()
                        break
            except CortexError as exc:
                failed = exc
            except asyncio.CancelledError:
                stream.cancel()
                raise
            finally:
                self._stream = None
                progress.cancel()
            if (failed is not None and not spoken and not overflowed and not self._cancelled
                    and _CONTEXT_ERROR.search(str(failed))):
                # The prompt outgrew the model's context: make room and try once more.
                overflowed = True
                log("the prompt was too long for the model; making room")
                self._make_room(force=True)
                failed = None
                attempt -= 1
                continue
            if failed is None and not parroted and not self._cancelled and not self._discard_reply:
                await out(guard.feed(think.flush()))
                await out(guard.flush())
                rest = (tags.flush() + " " + splitter.flush()).strip()
                if rest and not spoken and (_parrots(rest, text) or (not last_try() and boilerplate(rest, text))):
                    parroted, rest = True, ""
            if not parroted or failed is not None or self._cancelled or self._discard_reply:
                break
            retries += 1
            log("the reply only repeated the question or was boilerplate; asking again")

        self._log_timing(stream, first_clause, retries, failed)
        if self._discard_reply:
            return
        emotion = tags.emotion or "neutral"
        if self._cancelled:
            # Keep what it got out, marked as cut off. The brainstem has already
            # stopped the voice, so nothing more is sent.
            said = " ".join(spoken)
            if not said:
                # Nothing was said: the question alone would leave two user
                # messages in a row for the next prompt.
                self._pop_unanswered(message)
                return
            self.messages.append({"role": "assistant", "content": f"[{emotion}] {said} —", **sources})
            reply_id = self._safe_record(
                "said", said, self.cfg.name, emotion=emotion, interrupted=True, reply_to=episode_id, **sources
            )
            if reply_id is not None:
                self.messages[-1]["episode_id"] = reply_id
            return

        if failed is None:
            self._needs_warmup = False
            if rest:
                spoken.append(rest)
                if first_clause is None:
                    first_clause = time.monotonic() - started
                await self._send_reply("say.request", text=rest, seq=len(spoken), last=False)
        else:
            log(f"cortex failed mid-reply: {failed}" if self._durable_context else "cortex failed during a private reply")

        if self._discard_reply:
            return
        if not spoken:
            # Nothing came out. Say so; do not leave the model's silence in the context.
            self._pop_unanswered(message)
            await self._say("[concern] Sorry. I lost my thought.")
            return

        said = " ".join(spoken)
        # The voice and the brainstem hear the end first; then it is written down.
        await self._send_reply("say.request", text="", seq=len(spoken) + 1, last=True)
        await self._send_reply("mind.reply_done", spoke=True)
        self.last_said = said
        # The prompt keeps the reply as the model wrote it, so the next turn's
        # cache still matches; the archive keeps the cleaned, spoken words.
        # The cortex's own record of the generated text (think-filtered,
        # byte for byte) is kept whenever the whole reply was said: the stable
        # chat template renders it exactly as generated, so the next turn's
        # prompt shares its prefix with the cache (L7.2, L3.8). `stream` is
        # the last try's, which is the one that was said. Only a reply cut
        # short (an error, or the echo guard) keeps the words that were said.
        exact = getattr(stream, "raw", None)
        if isinstance(exact, str) and exact.strip() and failed is None and not echoed:
            content = exact
        else:
            generated = "".join(raw).strip()
            content = (generated if failed is None and generated and tags.emotion is not None
                       else f"[{emotion}] {said}")
        self.messages.append({"role": "assistant", "content": content, **sources})
        reply_id = self._safe_record("said", said, self.cfg.name, emotion=emotion, reply_to=episode_id, **sources)
        if reply_id is not None:
            self.messages[-1]["episode_id"] = reply_id
        self._start_boundary()

    def _start_boundary(self):
        """While it speaks, have the model read the end of its turn and the
        start of the next one (Cortex.prime_boundary), so the next turn
        reads only the new words."""
        prime = getattr(self.cortex, "prime_boundary", None)
        if prime is None or not self._model_available or self._cancelled or self._erase_required:
            return
        if self._boundary_task is not None and not self._boundary_task.done():
            return
        prompt = self._prompt()

        async def boundary():
            self._slot_generation += 1
            generation = self._slot_generation
            try:
                primed = await prime(prompt)
                if primed and generation == self._slot_generation and not self._erase_required:
                    # The primed tokens are a verified prefix a read-ahead may extend.
                    self._warmed_prefix = tuple(primed)
            except (CortexError, ValueError, TypeError) as exc:
                log(f"could not prime the turn boundary: {exc!r}")
            finally:
                self._slot_generation += 1

        self._boundary_task = asyncio.create_task(boundary())

    def _sampler(self):
        return {key: value for key, value in (
            ("top_k", self.cfg.top_k), ("top_p", self.cfg.top_p), ("presence_penalty", self.cfg.presence_penalty),
        ) if value is not None}

    def _safe_record(self, *args, **meta):
        try:
            return self._record(*args, **meta)
        except sqlite3.Error as exc:
            log("could not write down a reply:", exc)
            return None

    def _pop_unanswered(self, message):
        if self.messages and self.messages[-1] is message:
            self.messages.pop()

    async def _report_progress(self, stream):
        """Tell the brainstem, at most once a second, that the reply is still
        being worked on: prompt tokens read so far, then tokens written.
        Only real progress reported by the stream is sent, never a guess."""
        sent = None
        last = 0.0
        try:
            while True:
                await asyncio.sleep(PROGRESS_POLL)
                phase, tokens = stream.progress
                now = time.monotonic()
                if phase is None or (phase, tokens) == sent or now - last < PROGRESS_INTERVAL:
                    continue
                sent, last = (phase, tokens), now
                await self._send_reply("mind.progress", phase=phase, tokens=tokens)
        except asyncio.CancelledError:
            pass

    def _log_timing(self, stream, first_clause, retries, failed):
        """One line of numbers per turn, never any words (task 4.1)."""
        timings = getattr(stream, "timings", None) or {}
        line = {key: timings.get(key) for key in (
            "prompt_n", "cache_n", "prompt_per_second", "predicted_n", "predicted_per_second") if key in timings}
        for key, value in list(line.items()):
            if type(value) not in (int, float):
                line.pop(key)
            elif type(value) is float:
                line[key] = round(value, 2)
        line["first_clause_s"] = None if first_clause is None else round(first_clause, 3)
        line["retries"] = retries
        line["ok"] = failed is None
        self.last_timing = line
        log("turn timing " + json.dumps(line, sort_keys=True))
        doctor = optional_module("doctor") if failed is None else None
        if doctor is not None and hasattr(doctor, "note_turn"):
            prompt_n = line.get("prompt_n")
            doctor.note_turn(prompt_tps=line.get("prompt_per_second"),
                             decode_tps=line.get("predicted_per_second"),
                             prompt_n=prompt_n if type(prompt_n) is int else None)

    # -- commands --------------------------------------------------------

    async def _change_guest(self, entering):
        active = self._guest_memory is not None
        if entering == active:
            reply = "Guest mode is already on." if active else "Guest mode is already off."
            await self._say(f"[neutral] {reply}", keep=False)
            return

        work = self._interrupt_background()
        if dream := self._interrupt_dream():
            work.append(dream)
        if work:
            await asyncio.gather(*work, return_exceptions=True)
        self._retry_announcement()
        self._presence_at = None
        self._greeting_counts.clear()
        self._clear_working_memory()
        self._forget_wake()
        self._ring = None
        self._app_lines.clear()
        try:
            # The workshop closes its session and clears itself either way.
            await self.apps.privacy(entering)
        except Exception as exc:
            log(f"could not tell the workshop about guest mode: {exc!r}")
        self._log_action("guest.start" if entering else "guest.end")
        if entering:
            self._guest_memory = self.memory = self._guest_embedder(Memory(None))
            reply = "Guest mode is on. I'll keep this conversation only for this guest session."
        else:
            self._guest_memory.close()
            self._guest_memory = None
            self.memory = self._primary_memory
            reply = "Guest mode is off. I've cleared the guest conversation."
        if self._model_available:
            await self._clear_live_slot()
        await self._say(f"[neutral] {reply}", keep=False)

    async def _command(self, command, text, speaker):
        kind, arg = command.kind, command.arg
        if kind not in ("forget_last", "correct", "remember", "recall", "recall_said", "recall_heard"):
            # "Forget that" after a clock answer must not reach back to an
            # older sentence: these turns leave nothing to forget or correct.
            self._last_memory = None
        if kind in PRIVACY_COMMANDS:
            await self._change_guest(kind == "guest_start")
        elif kind == "remember":
            await self._remember_command(arg, text, speaker)
        elif kind in ("recall", "recall_said", "recall_heard"):
            self._record("command", "recall", speaker)
            await self._recall_command(arg, speaker, episode_kind=kind.removeprefix("recall_") if kind != "recall" else None)
        elif kind == "never_mind":
            await self._say("[neutral] Okay.", keep=False)
        elif kind == "forget_last":
            subject = self._last_memory
            if subject is None or subject.speaker != speaker:
                await self._say("[concern] I'm not sure what to forget. Tell me what it was about.")
            elif await self._edit_last():
                self._log_action("forget")
                await self._say("[neutral] Okay. That's forgotten." + self._backup_note())
            else:
                await self._say("[concern] I'm not sure what to forget. Tell me what it was about.")
        elif kind == "forget":
            await self._forget_command(arg, speaker)
        elif kind == "correct":
            await self._correct_command(arg, speaker)
        elif kind == "time":
            self._record("command", text, speaker)
            if doubt := self._clock_doubt():
                await self._say(self._doubtful_clock_line(doubt))
            else:
                await self._say(f"[neutral] {await self.skills.reply(command, self.clock)}")
            await self._publish_clock()
        elif kind in ("date", "date_tomorrow", "day") and (doubt := self._clock_doubt()):
            self._record("command", text, speaker)
            await self._say(self._doubtful_clock_line(doubt, what="date"))
        elif kind == "set_time" and self._guest_memory is not None:
            # The clock is the owner's: it moves their reminders and the
            # memory's record of a clock set by hand.
            await self._say("[neutral] Only the person I live with can set my clock.", keep=False)
        elif kind == "set_time":
            self._record("command", "set the time", speaker)
            await self._set_time_command(arg)
        elif kind == "name":
            self._record("command", text, speaker)
            await self._say(f"[joy] My name is {self.cfg.name}.")
        elif kind == "age":
            self._record("command", text, speaker)
            if self._clock_doubt() == "reset":
                await self._say("[concern] I'm not sure. My clock was reset, so I can't count my days right now.")
            else:
                await self._say(_age_reply(self._life, self.memory.now()))
        elif kind == "battery":
            await self._say(self._battery_line(), canned=True)
        elif kind in ("self_alive", "self_aware", "self_feel", "self_what"):
            self._record("command", text, speaker)
            await self._say(self_reply(kind, self.cfg.name, self._life_days()))
        elif kind in ("volume_up", "volume_down", "volume_set"):
            await self._volume_command(kind, arg)
        elif kind == "sleep":
            self._record("command", text, speaker)
            await self._say(good_night(self._local_now()))
            try:
                await asyncio.wait_for(self._said_all.wait(), 8.0)
            except asyncio.TimeoutError:
                pass
            await self._send_reply("being.sleep")
        elif kind == "look":
            self._record("command", text, speaker)
            if self._eyes_seen:
                await self._say("[neutral] I can tell when someone is in front of me, "
                                "but I can't make out things yet. Tell me what it is?")
            else:
                await self._say("[concern] I can't see right now. My camera is switched off.")
        elif kind == "dream":
            self._record("command", text, speaker)
            await self._say(self._dream_line(speaker))
        elif kind in ("remember_me", "about_me", "talked_about"):
            self._record("command", text, speaker)
            await self._memory_question(kind, speaker)
        elif kind == "diary":
            await self._diary_command(text, arg, speaker)
        elif not self._owner_services and kind not in PRIVATE_CLOCK_COMMANDS:
            await self._say("[neutral] I can tell you the time, date, or battery level in a private conversation.",
                            keep=False)
        elif kind in ("weather_current", "weather_today", "weather_tomorrow"):
            self._weather_task = asyncio.create_task(self.skills.reply(command))
            try:
                reply = await self._weather_task
            except asyncio.CancelledError:
                if self._cancelled:
                    return
                raise
            finally:
                self._weather_task = None
            await self._say(f"[neutral] {reply}", keep=False)
        else:
            try:
                reply = await self.skills.reply(command, clock_trusted=self._clock_doubt() is None)
            except (OSError, sqlite3.Error) as exc:
                log("local skill failed:", exc)
                reply = "I couldn't access my local settings or reminders."
            await self._say(f"[neutral] {reply}", keep=False)

    async def _diary_command(self, text, arg, speaker):
        """"What did we do yesterday?": the day record built in code each
        night (mind/diary.py); without one, the owner's own words that day."""
        diary = optional_module("diary")
        reply = None
        if diary is not None and hasattr(diary, "handle"):
            try:
                reply = await diary.handle(text, self._context(speaker, self.last_heard))
            except Exception as exc:
                log(f"the diary failed: {exc!r}")
        if reply is not None:
            await self._speak_reply(reply, text, speaker)
            return
        self._record("command", "recall", speaker)
        await self._recall_command(arg, speaker)

    async def _remember_command(self, arg, text, speaker):
        episode = self._record("heard", text, speaker, command=True)
        fact = self.memory.remember(arg, speaker, episode)
        self._subject("fact", fact, speaker, from_memory=True)
        older = self.memory.same_subject(arg, commands.replaceable_subject, exclude=fact)
        if self._owner_services:
            reply = "[joy] Okay. I'll remember that."
        else:
            reply = "[joy] Okay. I'll remember that for this session."
        if older:
            old = older[-1]
            reply += f" I also have: {commands.reflect(old.text)}. Should the new one replace it?"
        # A reply that repeats the older fact goes with it if it is replaced.
        await self._say(reply, **({"recalled": [old.id for old in older]} if older else {}))
        if older and not self._cancelled and self.lid_open:
            self._pending_forget = {
                "facts": [old.id for old in older], "episodes": [], "speaker": speaker,
                "until": _uptime() + FORGET_CONFIRM_SECONDS, "replace": True,
            }

    async def _correct_command(self, arg, speaker):
        subject = self._last_memory
        self._record("command", "correction", speaker)
        value = commands.correction_value(arg)
        if value is not None and len(value.split()) > commands.SPLICE_WORDS:
            # "That's wrong, I said I call my orchid Silverleaf": a whole
            # sentence after all, and it replaces the memory as said.
            arg, value = value, None
        if value is None:
            if await self._edit_last(replacement=arg):
                await self._say("[neutral] Got it. I've changed that.")
            else:
                await self._say("[concern] I'm not sure which memory you mean.")
            return
        # "That's wrong, it's Jim": only the new value was said. It goes into
        # the fact in place of the old one, and the result is read back. If
        # there is no clear place for it, ask for the whole sentence rather
        # than store words the owner never said.
        fact = self.memory.fact(subject.key) if subject.kind == "fact" else None
        if fact is not None:
            # "It's called Moonleaf" after "my orchid is called Silverleaf":
            # the fact already says "called", so only the name is new.
            verb = re.match(r"(called|named|known as)\s+(?=\S)", value, re.I)
            if verb and re.search(rf"\b{re.escape(verb[1])}\b", fact.text, re.I):
                value = value[verb.end():]
        corrected = commands.splice_correction(fact.text, value) if fact is not None else None
        if corrected is None:
            await self._say("[concern] Tell me the whole thing again, so I get it right. "
                            "For example: that's wrong, my cat is called Jim.")
            return
        if await self._edit_last(replacement=corrected):
            await self._say(f"[neutral] Got it. Now I have: {commands.reflect(corrected)}.")
        else:
            await self._say("[concern] I'm not sure which memory you mean.")

    async def _forget_command(self, arg, speaker):
        # The request itself is not logged: it may name the very thing to forget.
        # "Forget yesterday" means that day's conversation: the date stays a
        # date range and is never turned into a word to search for.
        if _FORGET_UNSUPPORTED.search(arg):
            await self._say("[neutral] I can only forget a topic or a single day, like yesterday. "
                            "Tell me one of those.")
            return
        if _FORGET_ALL.match(arg.strip().rstrip(".!?")):
            await self._offer_forget_everything(speaker)
            return
        topic, start, end = commands.history_query(arg, zone=self.skills.zone)
        if start is not None and _FORGET_FILLER.match(topic.strip()):
            topic = ""  # "everything from yesterday": the day, not those words
        fact_ids = []
        if topic and start is None:
            fact_ids = [fact.id for fact in self.memory.recall(topic, limit=-1, all_terms=True)]
        elif not topic and start is not None:
            fact_ids = self.memory.facts_between(start, end)
        facts = fact_ids
        episodes = []
        if self._archive_enabled and (search_terms(topic) or start is not None):
            episodes = self.memory.recall_history(topic, limit=-1, start=start, end=end, all_terms=True, for_edit=True)
            if not facts and not episodes and search_terms(topic):
                episodes = self.memory.recall_history(topic, limit=-1, start=start, end=end, all_terms=True, kind="said")
        count = len(facts) + len(episodes)
        if not count:
            await self._say("[neutral] I don't have anything like that to forget.")
        else:
            # Forgetting a whole topic cannot be undone, so nothing is
            # deleted until the count has been said back and agreed to.
            found = "one thing" if count == 1 else f"{count} things"
            await self._say(f"[concern] I found {found} to forget. Say yes, and I'll forget for good.")
            # The offer exists only once the count has really been said,
            # and only for the person who asked.
            if not self._cancelled and self.lid_open:
                self._pending_forget = {
                    "facts": fact_ids,
                    "episodes": [e.id for e in episodes],
                    "speaker": speaker,
                    "until": _uptime() + FORGET_CONFIRM_SECONDS,
                }

    async def _offer_forget_everything(self, speaker):
        if self._guest_memory is not None:
            await self._say("[neutral] I'm not keeping anything from this guest session anyway.", keep=False)
            return
        if not self._owner_services or speaker != self.cfg.owner:
            await self._say("[neutral] Only the person I live with can ask me to forget everything.", keep=False)
            return

        async def forget():
            return await self._forget_everything()

        await self._speak_reply(SimpleNamespace(
            line="[concern] Forget everything? Every memory, list and note, for good. Say yes, and it can't be undone.",
            keep=False, action=None, logged=None,
            confirm=SimpleNamespace(on_yes=forget, on_no=SimpleNamespace(
                line="[neutral] Okay. I've kept everything.", keep=False, confirm=None, action=None, logged=None),
                seconds=CONFIRM_SECONDS, strict=True)), "", speaker)

    async def _erase_everything(self):
        """Every fact and archived sentence, a batch at a time; the stored day
        records (counts only, but counts of conversations no longer
        remembered); then the lists and notes, the told-jokes record, the
        action log, the reminders and greeted days, the backups' erase mark
        and the model's cache."""
        memory = self.memory
        # Nothing in the prompt or the model's cache survives this, so it is
        # dropped first instead of being scrubbed batch by batch.
        work = self._interrupt_background()
        if work:
            await asyncio.gather(*work, return_exceptions=True)
        self._clear_working_memory()
        fact_ids = [fact.id for fact in memory.facts()]
        if fact_ids:
            memory.edit(fact_ids=fact_ids)
        after = 0
        while True:
            batch = [row[0] for row in memory.db.execute(
                "SELECT id FROM episodes WHERE id > ? AND text NOT IN ('', '[forgotten]') ORDER BY id LIMIT ?",
                (after, ERASE_BATCH))]
            if not batch:
                break
            after = batch[-1]
            memory.edit(episode_ids=batch)
            await asyncio.sleep(0)
        if self._durable_context and self.policy.expiry_days is not None:
            self._expiry_revision = memory.revision
        try:
            with memory.db:
                memory.db.execute("DELETE FROM diary_days")
        except sqlite3.OperationalError:
            pass  # no day records in this build
        self._clear_working_memory()
        self._forget_wake()
        for name in ("lists", "play", "action_log"):
            module = optional_module(name)
            try:
                if module is not None and hasattr(module, "erase_all"):
                    module.erase_all(self.state_dir)
            except Exception as exc:
                log(f"could not erase {name}: {exc!r}")
        # Reminders, alarms, timers and the greeted days go too: a reminder's
        # task is the owner's words, and a greeted day says when they talked.
        try:
            self.skills.reminders.erase_all()
        except Exception as exc:
            log(f"could not erase reminders: {exc!r}")
        self._drop_rings()
        backup = optional_module("backup")
        try:
            if backup is not None and hasattr(backup, "note_erase"):
                # Older backups must not bring the lists or the log back.
                backup.note_erase(self.state_dir)
        except Exception as exc:
            log(f"could not note the erase for backups: {exc!r}")
        if self._model_available:
            await self._clear_live_slot()
        self._log_action("forget.all")

    async def _forget_everything(self):
        """The erase path: every fact and episode (and what was derived from
        them), the lists and notes, the told-jokes record and the action log."""
        # The owner said yes: it runs to the end even if the turn is cut off,
        # and in batches, so years of memories never stall the event loop
        # (the face's link, the bus and the watchdog ping all live on it).
        await asyncio.shield(asyncio.ensure_future(self._erase_everything()))
        return SimpleNamespace(line="[neutral] Okay. I've forgotten everything." + self._backup_note(),
                               keep=False, confirm=None, action=None, logged=None)

    async def _recall_command(self, query, speaker, episode_kind=None):
        topic, start, end = commands.history_query(query, zone=self.skills.zone)
        allow_facts = episode_kind is None and start is None
        facts = self.memory.recall(topic, limit=3, all_terms=True) if allow_facts else []
        journal_owner = None
        if (not facts and self._durable_context and speaker == self.cfg.owner and episode_kind != "said"
                and topic and self._dream_consent.is_file()):
            journal_owner = speaker
        history = [] if facts or not self._archive_enabled else self.memory.recall_history(
            topic, limit=1, start=start, end=end, kind=episode_kind, journal_owner=journal_owner,
        )
        if not facts and not history and allow_facts:
            facts = self.memory.recall(topic, limit=3)
        if facts:
            self._subject("fact", facts[0].id, speaker, from_memory=True)
            told = " ".join(
                f"{'You' if fact.speaker == speaker else fact.speaker or 'Someone'} told me: {fact.text}."
                for fact in facts
            )
            await self._say(f"[joy] {told}", recalled=[fact.id for fact in facts], memory_reply=True)
        elif history:
            episode = history[0]
            self._subject("episode", episode.id, speaker, from_memory=True)
            who = "you" if episode.speaker == speaker else episode.speaker or "someone"
            if episode.kind == "said":
                who = "I"
            when = self._local(episode.ts)
            date = f"{when:%B} {when.day}, {when.year}"
            excerpt = history_excerpt(episode, 260)
            await self._say(
                f"[joy] On {date}, {who} said: {excerpt.rstrip('.!?')}.",
                episodes=[episode.id], memory_reply=True,
            )
        else:
            self._last_memory = None
            await self._say("[concern] I don't remember anything about that.")

    async def _memory_question(self, kind, speaker):
        """Questions about its memory as a whole, answered from the record."""
        guest = self._guest_memory is not None
        if kind == "remember_me":
            if guest or speaker != self.cfg.owner:
                await self._say(f"[neutral] I don't think we've met properly. I'm {self.cfg.name}.")
                return
            days = self._life_days()
            known = "" if days is None else (
                " I was only born today, so we've just met." if days < 1 else
                f" We've known each other for {spoken_count(days, 'day')}.")
            # It cannot tell voices apart yet, so it does not claim to know
            # this one: it says who it lives with, and takes their word.
            await self._say(f"[joy] If you're {self.cfg.owner}, then yes. You're the one who looks after me."
                            f"{known} I can't tell voices apart yet, so I'm taking your word for it.")
        elif kind == "about_me":
            facts = self.memory.latest_facts(limit=5, speaker=None if guest else speaker)
            if not facts:
                await self._say("[neutral] You haven't asked me to remember anything yet. "
                                "Say: remember that, and then the thing.")
                return
            if len(facts) == 1:
                told = f"Here's the one thing you've asked me to remember: {commands.reflect(facts[0].text)}."
            else:
                numbered = " ".join(f"{_ORDINALS[i]}: {commands.reflect(fact.text)}." for i, fact in enumerate(facts))
                told = f"Here are the latest {spoken_count(len(facts), 'thing')} you asked me to remember. {numbered}"
            await self._say(f"[joy] {told}",
                            recalled=[fact.id for fact in facts], memory_reply=True)
            self._subject("fact", facts[0].id, speaker, from_memory=True)
        else:
            lines = [] if not self._archive_enabled else self.memory.owner_lines(speaker, limit=3)
            if not lines:
                await self._say("[neutral] I don't have anything from our recent conversations.")
                return
            parts = [f"{commands.reflect(history_excerpt(line, 80)).rstrip('.!?')}" for line in lines[::-1]]
            when = self._relative_day(lines[0].ts)
            await self._say(f"[neutral] Lately, {when}, you said: " + "; ".join(parts) + ".",
                            episodes=[line.id for line in lines], memory_reply=True)

    async def _forget_confirmed(self, pending):
        change = self.memory.edit(fact_ids=pending["facts"], episode_ids=pending["episodes"])
        if change:
            await self._scrub(change)
            self._last_memory = None
            if pending.get("replace"):
                await self._say("[neutral] Okay. I've replaced it.")
                return
            self._forget_reminders(change.texts)
            # Count what was actually taken, not what was offered.
            taken = len(change.fact_ids) + len(set(pending["episodes"]).intersection(change.episode_ids))
            count = "That's" if taken <= 1 else "Those are"
            self._log_action("forget", max(1, taken))
            await self._say(f"[neutral] Okay. {count} forgotten." + self._backup_note())
        else:
            await self._say("[neutral] There was nothing left to forget.")

    async def _edit_last(self, replacement=None):
        subject = self._last_memory
        if subject is None:
            return None
        if subject.kind == "live":
            # Nothing is archived in this mode: forgetting takes the last
            # exchange out of the live conversation and the model's cache.
            if replacement is not None or not self.messages:
                return None
            last_user = max((i for i, m in enumerate(self.messages) if m["role"] == "user"), default=None)
            if last_user is None:
                return None
            dropped = self.messages[last_user:]
            del self.messages[last_user:]
            self._last_memory = None
            change = _LiveEdit(tuple(m["content"] for m in dropped))
            await self._scrub(change)
            # Only the owner's own words reach reminders, as in the archive
            # (memory.edit gives back what was heard, never Pip's reply): a
            # reply that happened to say "water the tomatoes" must not cancel
            # the owner's reminder to.
            self._forget_reminders(_spoken_words(m["content"]) for m in dropped if m["role"] == "user")
            return change
        targets = {"fact_ids" if subject.kind == "fact" else "episode_ids": [subject.key]}
        change = self.memory.edit(**targets, replacement=replacement)
        if change:
            await self._scrub(change)
            if replacement is None:
                # "Forget that": a reminder holding those words goes too. A
                # correction keeps them; the owner only changed a detail.
                self._forget_reminders(change.texts)
        if not change or replacement is None:
            self._last_memory = None
        return change

    def _forget_reminders(self, texts):
        """Forgetting reaches reminders too: none may keep the forgotten words
        (Reminders.forget_mentioning). Returns how many were cancelled."""
        texts = [text for text in texts or () if text and text != "[forgotten]"]
        if not texts:
            return 0
        self._drop_rings(texts)
        try:
            return self.skills.reminders.forget_mentioning(texts)
        except Exception as exc:
            log(f"could not forget reminders: {exc!r}")
            return 0

    def _drop_rings(self, texts=None):
        """A ring that may still be answered, or one offered and not yet said,
        holds a reminder's words outside the database: a "snooze" would set it
        again. It goes with them (every one when `texts` is None)."""
        def forgotten(entry):
            return isinstance(entry, Reminder) and (texts is None or mentions(entry.text, texts))

        if self._ring is not None and forgotten(self._ring.entry):
            self._ring = None
        pending = self._announcement
        if pending is not None and not pending.speaking and forgotten(pending.entry):
            # Its grant may still come: _speak_announcement then finds the
            # entry gone and answers "nothing to say".
            pending.forgotten = True

    def _backup_note(self):
        """Once per wake, after a forget: a backup made earlier still holds it.

        Said only when a backup is known to exist (the backup tool records
        its last run in BACKUP_RECORD)."""
        if self._backups_told or not self._durable_context or not (self.state_dir / BACKUP_RECORD).exists():
            return ""
        self._backups_told = True
        return " Older backups still hold it until they're refreshed."

    # -- its body, told truthfully -----------------------------------------

    def _battery_line(self):
        """The battery as it really is: level, plug, and time left when known."""
        if self.battery is None:
            if self.on_ac is True:
                return "[neutral] I'm plugged in, but I can't read my battery level right now."
            return "[concern] I can't read my battery level right now."
        # Running low and not plugged in is worth a worried look.
        low = self.battery <= LOW_BATTERY and self.charging is not True and self.on_ac is not True
        reply = f"[{'concern' if low else 'neutral'}] My battery is at {self.battery} percent."
        if self.charging is True:
            reply += " It's charging."
        elif self.on_ac is True:
            reply += " I'm plugged in."
        elif self.on_ac is False:
            reply += " I'm running on battery."
            if self.minutes_left is not None:
                minutes = int(self.minutes_left)
                left = ("less than a minute" if minutes < 1 else spoken_count(minutes, "minute") if minutes < 90
                        else f"about {spoken_count(round(minutes / 60), 'hour')}")
                reply += f" That's {left} left, if I keep going like this."
        if low:
            reply += " Could you plug me in?"
        return reply

    def _plea_stage_now(self):
        """Which battery plea the readings call for, or 0.

        The brainstem's own level wins when it sends one; the mind's
        thresholds are only the fallback for a brainstem that does not."""
        if self.on_ac is not False or self.charging is True:
            return 0
        if self.battery_level is not None:
            return PLEA_LEVELS[self.battery_level]
        level, minutes, cfg = self.battery, self.minutes_left, self.cfg

        def below(percent, limit):
            return (minutes is not None and minutes < limit) or (level is not None and level <= percent)

        if below(cfg.battery_critical_percent, cfg.battery_critical_minutes):
            return PLEA_CRITICAL
        if below(cfg.battery_second_plea_percent, cfg.battery_second_plea_minutes):
            return PLEA_VERY_LOW
        if below(cfg.battery_plea_percent, cfg.battery_plea_minutes):
            return PLEA_LOW
        return 0

    def _check_battery(self):
        """Plead for power once per stage and thank once when plugged back
        in. A stage counts only once two body.state updates in a row call for
        it. A critical reading saves at once, in the background, even the
        first time: the cells may cut out before any power-off is announced,
        and saving says nothing."""
        reading = self._plea_stage_now()
        stage, self._plea_reading = min(reading, self._plea_reading), reading
        if self.on_ac is True or self.charging is True:
            if self._plea_stage:
                self._plea_thanks_until = time.monotonic() + THANKS_SECONDS
            self._plea_stage = self._plea_woke = 0
            self._plea_due = None
            self._critical_saved = False
        elif stage > self._plea_stage:
            self._plea_stage = stage
            self._plea_due = stage
            self._plea_thanks_until = None
        if PLEA_CRITICAL in (self._plea_stage, reading) and not self._critical_saved:
            self._critical_saved = True
            log("battery critical: saving now")
            self._critical_save_task = asyncio.create_task(self._save_everything(SHUTDOWN_GRACE))

    async def _save_everything(self, budget):
        """The conversation, the model's cache and the database log, within
        `budget` seconds. Never raises: a power-off must not wait on it."""
        try:
            async with asyncio.timeout(max(0.5, budget)):
                await self._save_lock.acquire()
        except TimeoutError:
            log("could not save everything before power off: still saving")
            return
        try:
            if self._durable_context:
                self.save_context()
                await asyncio.wait_for(self._save_slot(), max(0.5, budget))
            if self.memory.root is not None:
                self.memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (asyncio.TimeoutError, sqlite3.Error, OSError, CortexError) as exc:
            log("could not save everything before power off:", exc)
        finally:
            self._save_lock.release()

    def _on_shutdown_notice(self, event):
        """The brainstem will switch off: say goodbye if it can, save, and
        say when it is ready. A cancelled notice (plugged back in) undoes it."""
        if event.get("cancelled") is True:
            self._shutdown = None
            if self._shutdown_task is not None:
                self._shutdown_task.cancel()
                self._shutdown_task = None
            if self._plea_due == PLEA_CRITICAL and self.on_ac is not False:
                self._plea_due = None
            return
        if self._shutdown is not None:
            return
        reason = event.get("reason") if isinstance(event.get("reason"), str) else "shutdown"
        grace = event.get("in")
        grace = float(grace) if type(grace) in (int, float) and grace > 0 else SHUTDOWN_GRACE
        self._shutdown = reason
        log(f"power-off announced ({reason}); saving")
        if reason == "battery_critical" and self.lid_open and self.state != "asleep":
            # Its goodbye, even if the critical plea was already said.
            self._plea_due = PLEA_CRITICAL
        self._shutdown_task = asyncio.create_task(self._prepare_shutdown(reason, grace))

    async def _prepare_shutdown(self, reason, grace):
        deadline = time.monotonic() + max(1.0, grace - SHUTDOWN_MARGIN)
        await self._save_everything(deadline - time.monotonic())
        # The goodbye is a battery plea; the power-off waits for it, but never
        # past the grace the brainstem gave.
        while time.monotonic() < deadline and self._shutdown == reason and self.lid_open and (
                (self._plea_due == PLEA_CRITICAL and self.state != "asleep")
                or (self._announcement is not None and self._announcement.entry == "plea"
                    and self._announcement.speaking)):
            await asyncio.sleep(0.2)
        if self._shutdown == reason:
            await self.bus.send("mind.shutdown_ready", reason=reason)

    def _plea_wanted(self):
        thanks = self._plea_thanks_until is not None and time.monotonic() < self._plea_thanks_until
        if not thanks:
            self._plea_thanks_until = None
        return self._plea_due is not None or thanks

    def _doubtful_clock_line(self, doubt, what="time"):
        if doubt == "reset":
            return (f"[concern] I'm not sure of the {what}. My clock was reset, maybe when my power ran out. "
                    "Could you tell me? Say: set the time to, and then the time.")
        return (f"[concern] I'm not sure of the {what}. My clock seems to have gone backwards. "
                "Could you tell me? Say: set the time to, and then the time.")

    async def _set_time_command(self, arg):
        wanted = commands.clock_time(arg)
        if wanted is None:
            await self._say("[concern] Say the time with morning or afternoon, like: set the time to 3:45 pm.")
            return
        hour, minute = wanted
        if self.skills.zone is None:
            await self._say("[concern] I need to know your time zone before I can set my clock.")
            return
        if self._clock_doubt() == "reset":
            await self._say("[concern] My date is wrong too, so I can't set the clock by voice. "
                            "The setup screen can set the date and time.")
            return
        # Only the time of day is said. If the clock was in doubt before (a
        # lost battery clock that still reads a plausible date), the date is
        # as unsure afterwards, whatever the memory then believes.
        doubted = self._clock_doubt() is not None
        before = time.time()
        target = self._nearest_wall_time(hour, minute, before)
        if not await asyncio.to_thread(self._set_system_clock, target):
            await self._say("[concern] I couldn't change my clock myself. The setup screen can set it.")
            return
        try:
            # A running timer counts real time, not the clock's jump.
            self.skills.reminders.clock_moved(target - before)
        except (AttributeError, sqlite3.Error, ValueError) as exc:
            log("could not move the timers with the clock:", exc)
        try:
            # The memory's own idea of the time follows a clock set by hand.
            self._primary_memory.owner_set_clock(target)
        except (sqlite3.Error, ValueError):
            pass
        reply = f"[joy] Thanks. My clock says {speakable_time(hour, minute)} now."
        if doubted or self._clock_doubt() is not None:
            # Only the time of day can be set by voice; the date may still be off.
            reply += " My date may still be wrong, though. The setup screen can set it."
        await self._say(reply)
        await self._publish_clock()

    def _nearest_wall_time(self, hour, minute, now):
        """The moment showing hour:minute in the owner's zone nearest to `now`:
        only the time of day is said, so the clock moves by under 12 hours,
        and "12:05 am" said at 11:58 pm is a few minutes ahead, not a day back."""
        today = datetime.fromtimestamp(now, self.skills.zone).date()
        moments = [
            datetime.combine(today + timedelta(days=offset), clock_time_of_day(hour, minute),
                             tzinfo=self.skills.zone).timestamp()
            for offset in (-1, 0, 1)
        ]
        return min(moments, key=lambda moment: abs(moment - now))

    @staticmethod
    def _set_system_clock(timestamp):
        """Set the system and hardware clocks. Only on the appliance itself,
        as root; anywhere else it reports that it could not."""
        if not Path("/etc/being").is_dir() or os.geteuid() != 0 or not hasattr(time, "clock_settime"):
            return False
        try:
            time.clock_settime(time.CLOCK_REALTIME, timestamp)
        except OSError as exc:
            log("could not set the clock:", exc)
            return False
        import subprocess

        for hwclock in ("/sbin/hwclock", "/usr/sbin/hwclock", "/bin/hwclock"):
            if os.path.exists(hwclock):
                try:
                    subprocess.run([hwclock, "-w", "-u"], timeout=10, check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                break
        return True

    async def _volume_command(self, kind, arg):
        current = self._volume
        if kind == "volume_set":
            level = commands.volume_level(arg)
            if level is None:
                await self._say("[concern] Tell me a number from ten to a hundred, like: set your volume to sixty.",
                                keep=False)
                return
        else:
            level = current + (VOLUME_STEP if kind == "volume_up" else -VOLUME_STEP)
        quietest = level < VOLUME_FLOOR
        level = max(VOLUME_FLOOR, min(100, level))
        if level == current and kind != "volume_set":
            edge = "as loud" if kind == "volume_up" else "as quiet"
            await self._say(f"[neutral] I'm already {edge} as I can go.", keep=False)
            return
        self._save_volume(level)
        await self.bus.send("voice.volume", level=level)
        if quietest:
            reply = f"[neutral] I'll go down to {level} percent, so you can still hear me."
        elif kind == "volume_set":
            reply = f"[neutral] Okay. My volume is at {level} percent."
        else:
            reply = "[neutral] Okay. Is this better?"
        await self._say(reply, keep=False)

    def _dream_line(self, speaker):
        """What it dreamt, from the journal only: never an invented dream."""
        if not self._durable_context:
            return "[neutral] I can't look at my dreams in this conversation."
        enabled = self._dream_consent.is_file()
        try:
            line, sources = dream_journal.dream_report(
                self.memory.db, speaker, now=time.time(), zone=self.skills.zone, enabled=enabled)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            log("could not read the dream journal:", exc)
            line, sources = "I can't look at my dream journal right now.", ()
        return _tagged(line, "curiosity" if sources else "neutral")

    def _life_days(self):
        if self._life is None:
            return None
        return max(0, int((self.memory.now() - self._life[0]) // 86_400))

    async def _scrub(self, change):
        """Remove old wording and its derivatives from the active context and cache."""
        self._discard_cache = True
        if self._durable_context and self.policy.expiry_days is not None:
            self._expiry_revision = self.memory.revision
        background = self._interrupt_background()
        ids = set(change.fact_ids)
        episode_ids = set(change.episode_ids)
        # Rows are scrubbed through their links. Wording is also searched for,
        # but only whole and only when long enough not to be an ordinary word:
        # forgetting "ok" must not eat the middle of "book".
        wording = [text for text in change.texts
                   if len(text.strip()) >= 8 and (text in change.fact_texts or len(set(search_terms(text))) >= 3)]
        literal = (re.compile("|".join(rf"(?<!\w){re.escape(text)}(?!\w)" for text in wording), re.IGNORECASE)
                   if wording else None)
        legacy_recall = False
        for message in self.messages:
            role, content = message["role"], message["content"]
            linked = (
                ids.intersection(message.get("recalled", []))
                or episode_ids.intersection(message.get("episodes", []))
                or message.get("episode_id") in episode_ids
            )
            if linked or (role == "assistant" and legacy_recall):
                message["content"] = "[forgotten]"
                message.pop("recalled", None)
                message.pop("episodes", None)
                message.pop("episode_id", None)
            elif literal is not None:
                message["content"] = literal.sub("[forgotten]", content)
            # Older saved prompts had recall notes without fact IDs.
            legacy_recall = role == "user" and not linked and literal is not None and bool(literal.search(content))
        if (
            self.summary_recalls.intersection(ids)
            or self.summary_episodes.intersection(episode_ids)
            or (literal is not None and literal.search(self.summary))
        ):
            self.summary = ""
            self.summary_recalls.clear()
            self.summary_episodes.clear()
        self.highlights = tuple(source.id for source in self._highlight_sources())
        (self.state_dir / "cortex" / SLOT_FILE).unlink(missing_ok=True)
        self.save_context()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        if self._durable_context:
            await self._erase_cache()
        else:
            self._erase_required = True
            if self._model_available:
                await self._clear_live_slot()

    async def _erase_cache(self):
        self._discard_cache = True
        self._rewarm = None
        self._slot_generation += 1
        self._saved_slot = None
        self._restore_pending = False
        self._warmed_prefix = ()
        saved = self.state_dir / "cortex" / SLOT_FILE
        saved.unlink(missing_ok=True)
        if await self.cortex.slot("erase"):
            # The server may have reopened the file to append checkpoints
            # after the first unlink. Erase runs after any pending write.
            saved.unlink(missing_ok=True)
            self._discard_cache = False
            # Empty now: the next rest warms it again, from the identity snapshot.
            self._slot_empty = self._needs_warmup = True
            return True
        return False

    # -- offline overnight maintenance -----------------------------------

    @cached_property
    def _dreams(self):
        return DreamStore(self.memory)

    def _interrupt_dream(self):
        """Stop the night's work at once: the lid opened, its name was heard,
        a key was pressed, or the night stopped being eligible."""
        task = self._dream_task
        if self.night_shift is not None:
            try:
                # The chunk in flight is cancelled and commits nothing; the
                # step then returns and keeps every job's place for later.
                self.night_shift.interrupt()
            except Exception as exc:
                log(f"could not interrupt the night shift: {exc!r}")
            else:
                return task if task is not None and not task.done() else None
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            return task
        return None

    def _eligible_night(self):
        """Tonight's date while the night's work may run (asleep, lid shut,
        on mains, cool enough, in the window), else None. Upkeep needs only
        this; dreaming also needs the owner's consent (`_dreams_allowed`)."""
        if not self._durable_context:
            return None
        local_time = self.skills.now()
        if self._dream_policy.eligible(local_time=local_time):
            return local_time.date().isoformat()
        return None

    def _dreams_allowed(self):
        """The owner's dream consent (Care, Dreams): the journal and the
        dream model run only with it, the night's upkeep without it too."""
        return self._dream_consent.is_file()

    def _start_dream(self):
        now = time.monotonic()
        if (self.state != "asleep" or self.lid_open or now < self._dream_next_at
                or (self._dream_task is not None and not self._dream_task.done())
                or (self._turn is not None and not self._turn.done())):
            return
        night = self._eligible_night()
        if night is None or (night, self._dreams_allowed()) == self._dream_idle_night:
            return
        if self.night_shift is None and not self._dreams_allowed():
            # Without a night shift the night is the journal alone: a dream.
            return
        self._dream_next_at = now + DREAM_INTERVAL
        self._dream_task = asyncio.create_task(self._dream_batch(night))

    async def _dream_batch(self, night):
        """One batch of the night's work. With dream consent it is bracketed
        by mind.dreaming, so bodycare can give "dreaming" as the reason it
        holds the package; upkeep alone is "night" to bodycare, never a
        dream that did not happen."""
        if self._eligible_night() != night:
            return
        dreaming = self._dreams_allowed()
        try:
            if dreaming:
                await self._send_dreaming(True)
            if self.night_shift is not None:
                await self._night_step(night)
            elif dreaming:
                await self._journal_batch(night)
        finally:
            if dreaming:
                await self._send_dreaming(False)

    async def _send_dreaming(self, active):
        try:
            await self.bus.send("mind.dreaming", active=active)
        except Exception as exc:  # the night's work never waits on the bus
            log(f"could not send mind.dreaming: {exc!r}")

    async def _journal_batch(self, night):
        """Without a night shift: the rule-based dream journal alone."""
        started = time.monotonic()
        if self._eligible_night() != night or not self._dreams_allowed():
            return
        try:
            batch = self._dreams.stage(self.cfg.owner, night=night)
            if batch is None:
                self._dream_idle_night = (night, True)
                return
            # Give the bus a chance to deliver wake, power, or lid events before committing.
            await asyncio.sleep(DREAM_YIELD)
            if (time.monotonic() - started <= DREAM_STAGE_BUDGET and self._eligible_night() == night
                    and self._dreams_allowed()):
                published = self._dreams.publish(batch)
                lid = self._latest_lid
                if published and batch.sources and lid and lid.get("closed") is True and type(lid.get("seq")) is int:
                    # Only a receipt for the current lid closure crosses the bus.
                    await self.bus.send("mind.dream_committed", lid_seq=lid["seq"])
        except sqlite3.Error as exc:
            log("could not update the overnight journal:", exc)

    def _night_jobs(self, night):
        """Tonight's own jobs (memory upkeep, the journal, the day record and
        the dream), registered once per night: they are built for that night."""
        if self._night_registered == night:
            return
        self._night_registered = night
        shift, memory, owner = self.night_shift, self.memory, self.cfg.owner
        module = optional_module("night_shift")
        jobs = []
        if module is not None and hasattr(module, "standard_jobs"):
            # Expiry stays the mind's own (it runs whenever the clock is believed).
            jobs.append(lambda: module.standard_jobs(shift, memory, owner=owner, night=night, cutoff=None))
        diary = optional_module("diary")
        if diary is not None and hasattr(diary, "DiaryJob"):
            jobs.append(lambda: shift.register("diary", diary.DiaryJob(
                memory, owner, today=lambda: self.skills.now().date(), zone=self.skills.zone,
                state_dir=self.state_dir)))
        dream = optional_module("dream_model")
        if dream is not None and hasattr(dream, "DreamJob") and Path("/etc/being").is_dir():
            # The dream model runs only on the appliance, where its service exists.
            jobs.append(lambda: shift.register("dream", dream.DreamJob(
                memory, owner, self.cfg.name, night, zone=self.skills.zone)))
        for register in jobs:
            try:
                register()
            except Exception as exc:
                log(f"a night job was not registered: {exc!r}")

    async def _night_step(self, night):
        """One step of the night shift's fixed list of jobs (mind/night_shift.py).
        It asks before every chunk whether the night is still eligible."""
        if self._eligible_night() != night:
            return
        if self._embedder_ok is None:
            # The embedding pack's first check is still running (a start in
            # the night): the vectors job would find no embedder and count
            # itself done. The next batch, seconds later, carries on.
            return
        shift = self.night_shift
        self._night_jobs(night)
        journal = getattr(shift, "jobs", {}).get("journal")
        published = getattr(journal, "published", 0)
        module = optional_module("night_shift")
        upkeep_only = getattr(module, "upkeep_only", None)
        consented = self._dreams_allowed()

        def eligible():
            # Asked again before every chunk: the lid, the power, the window
            # and the owner's consent can all change mid-night.
            if self._eligible_night() != night:
                return False
            if self._dreams_allowed():
                return True
            # No consent: upkeep only. Without the gate type, nothing at all
            # (a night shift that cannot tell dreams from upkeep never dreams).
            return upkeep_only() if callable(upkeep_only) else False

        try:
            await shift.step(eligible, night=night)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"the night shift failed: {exc!r}")
            return
        lid = self._latest_lid
        if (getattr(journal, "published", 0) > published and lid and lid.get("closed") is True
                and type(lid.get("seq")) is int):
            # Only a receipt for the current lid closure crosses the bus.
            await self.bus.send("mind.dream_committed", lid_seq=lid["seq"])
        if consented != self._dreams_allowed():
            return  # the consent changed mid-step: look again next time
        settled = getattr(shift, "settled", None)
        done = getattr(shift, "done_tonight", None)
        if callable(settled):
            finished = settled(dreams=consented)
        else:
            dreaming = getattr(module, "DREAMING", frozenset({"journal", "dream"}))
            left = set(getattr(shift, "jobs", {})) - (set() if consented else set(dreaming))
            finished = callable(done) and left <= set(done())
        if finished:
            # Every job it may run is finished for tonight: nothing more until
            # tomorrow night, or until the owner's dream consent changes.
            self._dream_idle_night = (night, consented)

    # -- keeping the context in bounds -------------------------------------

    def _make_room(self, *, force=False):
        """Last resort, mid-conversation: drop the oldest turns to fit.

        Dropping changes the start of the prompt, so the model must read all of
        it again. Trimming just under the limit would mean paying that on every
        later turn; trimming to half pays it once and buys many turns. With
        `force` (the server said the prompt is too long) it trims regardless.
        """
        if not force and self._tokens() <= 0.9 * self.cfg.ctx_tokens:
            return
        if force:
            keep = self.messages[-1:] if self.messages and self.messages[-1]["role"] == "user" else []
            older = self.messages[:len(self.messages) - len(keep)]
            del older[:max(2, len(older) // 2)]
            self.messages = older + keep
        while self._tokens() > 0.5 * self.cfg.ctx_tokens and len(self.messages) > 2:
            del self.messages[:2]
        # An interrupted turn can leave a question with no reply, so pairs are
        # not guaranteed. Never begin with a reply whose question is gone.
        while len(self.messages) > 1 and self.messages[0]["role"] != "user":
            del self.messages[0]

    async def _housekeep(self):
        """Once things have been quiet for a while: tidy the context and save."""
        await asyncio.sleep(self.cfg.settle)
        if not self.lid_open or self.state not in RESTING or (self._turn is not None and not self._turn.done()):
            return
        warm = self._warm_task
        if warm is not None and not warm.done():
            # A warm-up started while this was settling: let it finish first.
            await asyncio.gather(warm, return_exceptions=True)
            if not self.lid_open or self.state not in RESTING:
                return
        self._housekeeping_working = True
        try:
            await self._check_expiry()
            if self._tokens() > 0.6 * self.cfg.ctx_tokens:
                await self._compact()
            self.save_context()
            if self.state == "asleep":
                await self._save_slot()
        finally:
            self._housekeeping_working = False

    async def _compact(self):
        """Carry a few exact owner excerpts forward before releasing old turns."""
        if not self._durable_context or self._erase_required:
            return
        keep = max(2, 2 * self.cfg.keep_turns)
        old, recent = self.messages[:-keep], self.messages[-keep:]
        if not old:
            return
        revision = self.memory.revision
        old_user = [message for message in old if message["role"] == "user"]
        sources = self.summary_episodes.union(self.highlights, *(message.get("episodes", []) for message in old_user))
        sources.update(message["episode_id"] for message in old_user if "episode_id" in message)
        candidates = consolidation.candidate_highlights(self.memory, sources, owner=self.cfg.owner)
        if self.memory.revision != revision or not self.lid_open or self.state not in RESTING:
            return
        self.highlights = consolidation.select_highlights(candidates)
        if self.highlights:
            # Earlier releases saved generated summaries. Let current source
            # excerpts supersede those once the conversation supplies them.
            self.summary = ""
            self.summary_recalls.clear()
            self.summary_episodes.clear()
        self.messages = recent
        log(f"compacted {len(old)} messages with {len(self.highlights)} archive sources")
        if self.cortex_ok:
            # The whole new prompt, in interruptible 32-token batches, so the
            # next turn reads only its own words (not 300-500 cold tokens).
            await self._warm_prefix(full=True)
