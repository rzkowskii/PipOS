"""The mind: what to do with what was heard.

One rule shapes this file. While the being is awake, the prompt only ever grows
by appending. On the laptop the model reads about as slowly as it writes, so
anything that changes earlier text throws away minutes of cached reading.
Rewriting (compaction) waits until it is idle.
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from pathlib import Path

from . import commands, consolidation
from .cortex import SLOT_FILE, CortexError
from .dream_policy import DreamPolicy
from .dream_store import DreamStore
from .initiative import load_policy as load_initiative_policy
from .memory import Memory, search_terms
from .profile import (
    DEFAULT_PERSONALITY, Personality, daily_greeting, first_words, good_night, personality_greetings,
)
from .reminders import Reminder
from .retention import SECONDS_PER_DAY, load_policy as load_memory_policy
from .skills import Skills, speakable_time
from .speech import GRAMMAR, ClauseSplitter, EchoGuard, TagParser, ThinkFilter
from .storage import sync_directory

SUBS = [
    "hear.wake",
    "hear.speech_start",
    "hear.partial",
    "hear.final",
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
]

# States in which something said is meant for the being.
ADDRESSED = ("attending", "listening", "thinking", "speaking")
RESTING = ("idle", "drowsy", "asleep")
ANNOUNCE_IN = ("idle", "drowsy", "attending")
ANNOUNCE_RETRY = 15.0
PRESENCE_SECONDS = 1.5
# Read-ahead is experimental. Bound both the extension of the verified cache
# and how long it can occupy the single model slot after a partial transcript.
READ_AHEAD_TOKENS = 24
READ_AHEAD_SECONDS = 0.5
WARM_BATCH_TOKENS = 32
WARM_PREFIX_TOKENS = 256
PRIVACY_ERASE_TIMEOUT = 3.0
# At or below this battery percentage, unplugged, it looks worried.
LOW_BATTERY = 15
PRIVACY_COMMANDS = {"guest_start", "guest_end"}
# How long an offer to forget a whole topic stays open.
FORGET_CONFIRM_SECONDS = 45.0
# Only a plain yes confirms. "Forget it" is how people say "never mind", so it
# is a no here, never a yes.
_YES = re.compile(r"^(?:yes|yeah|yep|yes please|yes[, ]+do(?: it)?|do it|go ahead|please do|confirm)$", re.I)
_NO = re.compile(
    r"^(?:no|nope|no thanks|no thank you|never ?mind|cancel|stop|don't|do not|wait|"
    r"(?:no[, ]+)?(?:keep|forget) (?:it|them|that|those|everything))$",
    re.I,
)
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
    """True when a reply only repeats what was just said to it."""
    said, asked = _words(reply), _words(heard)
    if len(said) < 3 or len(asked) < 3:
        return False
    if said == asked:
        return True
    joined, whole = " ".join(said), " ".join(asked)
    return f" {joined} " in f" {whole} " and len(said) >= 0.6 * len(asked)


def _uptime():
    """Seconds since boot, counting time spent suspended where the system can say."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()

PRIVATE_CLOCK_COMMANDS = {"time", "date", "date_tomorrow", "day"}
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


def log(*parts):
    print("mind:", *parts, file=sys.stderr, flush=True)


@dataclass
class Announcement:
    entry: Reminder | date
    token: str
    deadline: float
    speaking: bool = False


def history_excerpt(episode, max_chars):
    text = " ".join(episode.text.split())
    if len(text) > max_chars:
        text = " ".join((episode.excerpt or text).split())
    if len(text) > max_chars:
        head = text[: max_chars + 1]
        return (head.rsplit(" ", 1)[0] if " " in head else head[:max_chars]) + "…"
    return text


class Mind:
    def __init__(self, bus, cortex, memory, cfg, state_dir, *, policy=None):
        self.bus = bus
        self.cortex = cortex
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.policy = policy if policy is not None else load_memory_policy(self.state_dir)
        self._session_memory = Memory(None) if self.policy.mode == "session-only" else None
        self._primary_memory = self._session_memory if self._session_memory is not None else memory
        self.memory = self._primary_memory
        self._guest_memory = None
        self._expiry_day = None
        self._expiry_revision = None
        self.skills = Skills(self.state_dir)
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
        self._last_memory = None
        self._pending_forget = None
        self._last_telling = None
        self._life = None  # (birth time, times woken), from the life record
        self._clock_warned = False
        self._clock_sent = None
        self._greeting_replies = personality_greetings(cfg.personality)
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
                date = time.strftime("%Y-%m-%d", time.localtime(source.ts))
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
        self._last_partial = None
        self._utterance_senses = None
        self._speculative_text = ""
        self._discard_cache = self._erase_required = True
        self._restore_pending = False
        self._needs_warmup = True
        self._slot_generation += 1
        self._saved_slot = None
        self._warmed_prefix = ()
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
            await self.bus.start()
            watcher = asyncio.create_task(self._watch_cortex())
            while True:
                await self._on_event(await self.bus.recv())
        finally:
            running = [task for task in (watcher, self._turn) if task is not None and not task.done()]
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
            self._start_warmup()
            decided = ok or time.monotonic() - started >= self.cfg.cortex_wait
            if decided and self.bus.connected.is_set() and self._announced != self.bus.generation:
                self._announced = self.bus.generation
                await self.bus.send("mind.ready", cortex=self._model_available)
            await asyncio.sleep(1.0 if not decided else 5.0)

    def _start_warmup(self):
        if (
            self._needs_warmup and self.lid_open and self._model_available
            and self.state in ("waking", *RESTING)
            and (self._turn is None or self._turn.done())
            and (self._warm_task is None or self._warm_task.done())
            and (self._housekeeping is None or self._housekeeping.done())
        ):
            self._warm_task = asyncio.create_task(self._warm())

    async def _warm(self):
        """On waking: get the model's reading of who it is, and of the
        conversation so far, back into its cache before anyone speaks.

        Restore a slot only when its recent conversation was accepted. The
        verified prefix can be prepared while the face and controls wake.
        """
        if self._erase_required:
            if not await self._clear_live_slot():
                return
        elif self._discard_cache:
            await self._erase_cache()
        elif self._restore_pending:
            self._restore_pending = False
            self._slot_generation += 1
            saved = self._slot_stamp()
            if await self.cortex.slot("restore"):
                if saved == self._slot_stamp():
                    self._saved_slot = saved
                self._needs_warmup = False
                return
        await self._warm_prefix()
        self._needs_warmup = False

    async def _warm_prefix(self):
        self._warmed_prefix = ()
        self._slot_generation += 1
        try:
            self._warmed_prefix = await self.cortex.warm_prefix(
                self._prompt(), batch_tokens=WARM_BATCH_TOKENS, max_tokens=WARM_PREFIX_TOKENS,
            )
        except CortexError as exc:
            log(f"could not warm up: {exc}" if self._durable_context else "could not warm the private prompt")
        finally:
            # Warmup can update the slot over several separately awaited requests.
            self._slot_generation += 1
        if self._warmed_prefix:
            log(f"warmed {len(self._warmed_prefix)} verified prompt tokens")

    def _interrupt_background(self, *, keep_warmup=False):
        work = (self._prefill_task, self._housekeeping)
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
                self._close_lid()
            self._latest_lid = event
        elif topic == "body.presence":
            lid = self._latest_lid
            if (lid is not None and lid.get("closed") is False and lid.get("seq") is not None
                    and event.get("lid_seq") == lid["seq"]):
                self._presence_at = time.monotonic() if event.get("present") is True else None

    def _close_lid(self):
        # An unanswered offer to forget does not wait for whoever opens the lid.
        self._pending_forget = None
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

    def _connection_lost(self):
        self._close_lid()
        self._dream_policy.reset()
        self._interrupt_dream()
        self._dream_idle_night = None
        self.state = "waking"
        self.battery = self.on_ac = self.charging = None

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
                self.lid_open = True
                self.lid_seq = event.get("seq")
                self._start_warmup()
        elif topic == "body.senses":
            self.senses = event.get("line") or ""
        elif topic == "body.state":
            hour, minute = event.get("hour"), event.get("minute")
            if isinstance(hour, int) and isinstance(minute, int):
                self.clock = (hour, minute)
            battery = event.get("battery")
            self.battery = round(battery) if type(battery) in (int, float) and 0 <= battery <= 100 else None
            self.on_ac = event.get("on_ac")
            self.charging = event.get("charging")
        elif topic == "state.changed":
            self.state = event.get("state") or self.state
            if self._announcement is not None and self._announcement.speaking and self.state not in ("thinking", "speaking"):
                self._retry_announcement()
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
            elif self._housekeeping is not None:
                self._housekeeping.cancel()
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
                # Cancellation closes each in-flight HTTP connection. Wait for
                # local cancellation before submitting anything to the slot.
                # Direct replies can speak while the model warms.
                background = self._interrupt_background(
                    keep_warmup=not privacy_change and (command is not None or greeting is not None)
                )
                if background:
                    await asyncio.gather(*background, return_exceptions=True)
                if self._turn is not None and not self._turn.done():
                    self._cancel()
                    await asyncio.wait([self._turn])
                if self.lid_open:
                    speaker = event.get("speaker") or self.cfg.owner
                    self._turn = asyncio.create_task(self._run_turn(text, speaker, greeting, command=command))
        elif topic == "say.done" and event.get("final"):
            if not event.get("announcement"):
                self._said_all.set()
        elif topic == "stem.beat":
            await self._publish_clock()
            if self._turn is None or self._turn.done():
                await self._check_expiry()
            await self._offer_announcement()
            self._start_dream()
        elif topic == "stem.announce_ready":
            await self._speak_announcement(event.get("announcement"))
        elif topic == "life.woke":
            self._on_woke(event)

    def _retry_announcement(self):
        if self._announcement is not None:
            self._announcement = None
            self._announcement_retry = time.monotonic() + ANNOUNCE_RETRY

    def _has_fresh_presence(self, now):
        return self._presence_at is not None and now - self._presence_at < PRESENCE_SECONDS

    async def _offer_announcement(self):
        if not self._owner_services:
            return
        now = time.monotonic()
        if (self._announcement is not None and not self._announcement.speaking
                and now >= self._announcement.deadline):
            self._retry_announcement()
        if (self._announcement is not None or now < self._announcement_retry
                or not self.lid_open or self.state not in ANNOUNCE_IN
                or (self._turn is not None and not self._turn.done())):
            return
        try:
            entry = self.skills.reminders.due()
            greeting_possible = self.state == "idle" and self._has_fresh_presence(now)
            if entry is None and not greeting_possible:
                return
            policy = load_initiative_policy(self.state_dir)
            local_now = self.skills.now()
            if policy.is_quiet(local_now):
                return
            if entry is None or not policy.reminders:
                if not (policy.greetings and greeting_possible):
                    return
                entry = local_now.date()
                if self.skills.reminders.greeted_on(entry):
                    return
        except (OSError, sqlite3.Error) as exc:
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
        if (not self._owner_services or pending is None or pending.token != token or pending.speaking
                or not self.lid_open):
            return
        # A yes said to a reminder must not answer an offer to forget.
        self._pending_forget = None
        if self._turn is not None and not self._turn.done():
            self._announcement = None
            return
        entry = pending.entry
        policy = load_initiative_policy(self.state_dir)
        local_now = self.skills.now()
        if isinstance(entry, Reminder):
            allowed = policy.reminders
            reply = f"Timer {entry.id} is up." if entry.kind == "timer" else f"Reminder {entry.id}. {entry.text}"
        else:
            allowed = policy.greetings and entry == local_now.date() and self._has_fresh_presence(time.monotonic())
        if not allowed or policy.is_quiet(local_now):
            self._announcement = None
            await self.bus.send("mind.reply_done", spoke=False, lid_seq=self.lid_seq)
            return
        if isinstance(entry, Reminder):
            line = f"[neutral] {reply}"
        else:
            line = self._greeting_line()
        pending.speaking = True
        self._cancelled = False
        await self._say(line, keep=False, announcement=token)

    def _local_now(self):
        """The owner's local time, or None while their time zone is unknown."""
        return self.skills.now() if self.skills.zone is not None else None

    def _greeting_line(self):
        if self._newborn and self._guest_memory is None:
            return first_words(self.cfg.name)
        return daily_greeting(self._local_now())

    def _complete_announcement(self, token):
        pending = self._announcement
        if (not self._owner_services or pending is None or pending.token != token or not pending.speaking
                or not self.lid_open):
            return
        try:
            if isinstance(pending.entry, Reminder):
                self.skills.reminders.complete(pending.entry.id)
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
        local_reply = commands.may_be_command(text, self.cfg.name) or commands.greeting(text, self.cfg.name)
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
        try:
            async with asyncio.timeout(READ_AHEAD_SECONDS):
                await self.cortex.prefill(prompt, prefix=prefix, max_new_tokens=READ_AHEAD_TOKENS)
        except (CortexError, TimeoutError):
            pass
        finally:
            self._slot_generation += 1

    # -- a turn ----------------------------------------------------------

    async def _run_turn(self, text, speaker, greeting=None, *, command=None):
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
        try:
            await self._check_expiry()
            if self._discard_reply:
                return
            pending, self._pending_forget = self._pending_forget, None
            if pending is not None and _uptime() <= pending["until"] and speaker == pending["speaker"]:
                answer = text.strip().rstrip(".!?").strip()
                if _YES.match(answer):
                    await self._forget_confirmed(pending)
                    self.save_context()
                    return
                if _NO.match(answer):
                    await self._say("[neutral] Okay. I've kept everything.")
                    self.save_context()
                    return
                # Anything else keeps them too, and is then taken as said.
            if command is None:
                command = commands.parse(text, self.cfg.name, now=self.skills.now())
            if command is None:
                # "Forget that" must mean this utterance. If this turn ends
                # without recording it, there is nothing for it to mean.
                self._last_memory = None
            if command is not None:
                await self._command(command, text, speaker)
            elif greeting := greeting or commands.greeting(text, self.cfg.name):
                await self._greet(greeting, text, speaker)
            elif not self._model_available:
                episode = self._record("heard", text, speaker)
                self._last_memory = ("episode", episode) if episode is not None else None
                reply = ("Private conversations need my local language model." if self.cortex_ok
                         else "I can't think right now. My language model isn't answering.")
                await self._say(f"[concern] {reply}")
            elif self._erase_required and not await self._clear_live_slot():
                await self._say("[concern] I'm waiting for my conversation cache to clear.", keep=False)
            else:
                await self._converse(text, speaker, senses)
        except Exception as exc:  # a turn must always end, whatever went wrong
            log(f"turn failed: {exc!r}" if self._durable_context and not self._erase_required else "private turn failed")
            await self._send_reply("mind.reply_done", spoke=False)
        self.save_context()

    async def _greet(self, greeting, text, speaker):
        episode = self._record("heard", text, speaker)
        self._last_memory = ("episode", episode) if episode is not None else None
        if greeting == "hello" and self._newborn and self._guest_memory is None:
            await self._say(first_words(self.cfg.name), reply_to=episode, episodes=[episode])
            if not self._cancelled:
                self._newborn = False
            return
        replies = self._greeting_replies[greeting]
        index = self._greeting_counts.get(greeting, 0)
        self._greeting_counts[greeting] = (index + 1) % len(replies)
        await self._say(replies[index], reply_to=episode, episodes=[episode])

    async def _say(self, reply, *, keep=True, announcement=None, **sources):
        """Speak a fixed reply. It starts with an [emotion] tag like the model's."""
        if self._cancelled:
            return
        tags = TagParser()
        text = tags.feed(reply) + tags.flush()
        splitter = ClauseSplitter()
        clauses = splitter.feed(text)
        rest = splitter.flush()
        if rest:
            clauses.append(rest)
        self._said_all.clear()
        if tags.emotion:
            await self._send_reply("mind.emotion", name=tags.emotion, hold=30.0)
        for i, clause in enumerate(clauses, 1):
            await self._send_reply("say.request", text=clause, seq=i, last=False)
        if clauses:
            marker = {"announcement": announcement} if announcement is not None else {}
            await self._send_reply("say.request", text="", seq=len(clauses) + 1, last=True, **marker)
        await self._send_reply("mind.reply_done", spoke=bool(clauses))
        if keep and not self._cancelled:
            self._record("said", " ".join(clauses), self.cfg.name, emotion=tags.emotion or "neutral", **sources)

    def _recall_note(self, facts, speaker):
        parts = []
        for fact in facts:
            who = "they" if fact.speaker == speaker else (fact.speaker or "someone")
            part = f'{who} told you "{fact.text}"'
            if len("; ".join(parts + [part])) > self.cfg.recall_chars and parts:
                break
            parts.append(part[: self.cfg.recall_chars])
        return "you remember: " + "; ".join(parts) if parts else ""

    def _history_note(self, episodes):
        lines = []
        for episode in episodes:
            date = time.strftime("%Y-%m-%d", time.localtime(episode.ts))
            speaker = episode.speaker or ("you" if episode.kind == "said" else "someone")
            # Transcripts can contain line breaks. Keep each excerpt inside the note.
            lines.append(f"{date} {speaker}: {history_excerpt(episode, self.cfg.recall_chars)}")
        return "past conversation: " + " / ".join(lines) if lines else ""

    async def _converse(self, text, speaker, senses):
        topic, start, end = commands.history_query(text.rstrip(".!?"), zone=self.skills.zone)
        facts = self.memory.recall(topic, limit=2, all_terms=True) if start is None else []
        active_ids = {message.get("episode_id") for message in self.messages}
        history = []
        if self._archive_enabled and search_terms(topic) and not facts:
            matches = self.memory.recall_history(
                topic, limit=6, start=start, end=end, include_fact_sources=False
            )
            history = [episode for episode in matches if episode.id not in active_ids][:1]
            if history:
                neighbor = self.memory.history_neighbor(history[0])
                if neighbor and neighbor.id not in active_ids:
                    history.append(neighbor)
        if not facts and not history and start is None:
            facts = self.memory.recall(topic, limit=2)
        note = self._recall_note(facts, speaker) or self._history_note(history)
        lookup = bool(MEMORY_LOOKUP.match(text))
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
            self._last_memory = ("fact", facts[0].id)
        else:
            matched = history[0].id if history and lookup else episode_id
            self._last_memory = ("episode", matched) if matched is not None else None
        message = {
            "role": "user",
            "content": self._user_content(text, senses, note),
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

        spoken = []
        emotion_sent = False
        failed = None
        parroted = False
        rest = ""

        async def out(visible):
            nonlocal emotion_sent, parroted
            piece = tags.feed(visible)
            if tags.emotion and not emotion_sent:
                emotion_sent = True
                await self._send_reply("mind.emotion", name=tags.emotion, hold=30.0)
            for clause in splitter.feed(piece):
                if not spoken and _parrots(clause, text):
                    parroted = True
                    return
                spoken.append(clause)
                await self._send_reply("say.request", text=clause, seq=len(spoken), last=False)

        self._said_all.clear()
        # A very small model sometimes answers a question by repeating it. The
        # first clause is checked before it is spoken: a parrot is never said,
        # and the reply is asked for once more, a little less cautiously.
        for temperature in (self.cfg.temperature, min(1.0, self.cfg.temperature + 0.3)):
            think, guard, tags, splitter = ThinkFilter(), EchoGuard(senses), TagParser(), ClauseSplitter()
            parroted = False
            self._slot_generation += 1
            stream = self.cortex.stream(
                self._prompt(),
                grammar=GRAMMAR,
                max_tokens=self.cfg.max_tokens,
                temperature=temperature,
            )
            self._stream = stream
            try:
                async for delta in stream:
                    if self._cancelled:
                        break
                    await out(guard.feed(think.feed(delta)))
                    if guard.stopped or parroted:
                        # Reciting its own notes ends the reply; a parrot ends this try.
                        stream.cancel()
                        break
            except CortexError as exc:
                failed = exc
            except asyncio.CancelledError:
                stream.cancel()
                raise
            finally:
                self._stream = None
            if failed is None and not parroted and not self._cancelled and not self._discard_reply:
                await out(guard.feed(think.flush()))
                await out(guard.flush())
                rest = (tags.flush() + " " + splitter.flush()).strip()
                if rest and not spoken and _parrots(rest, text):
                    parroted, rest = True, ""
            if not parroted or failed is not None or self._cancelled or self._discard_reply:
                break
            log("the reply only repeated the question; asking again")

        if self._discard_reply:
            return
        emotion = tags.emotion or "neutral"
        if self._cancelled:
            # Keep what it got out, marked as cut off. The brainstem has already
            # stopped the voice, so nothing more is sent.
            said = " ".join(spoken)
            if said:
                reply_id = self._record(
                    "said", said, self.cfg.name, emotion=emotion, interrupted=True, reply_to=episode_id, **sources
                )
                self.messages.append({
                    "role": "assistant", "content": f"[{emotion}] {said} —", "episode_id": reply_id, **sources,
                })
            return

        if failed is None:
            self._needs_warmup = False
            if rest:
                spoken.append(rest)
                await self._send_reply("say.request", text=rest, seq=len(spoken), last=False)
        else:
            log(f"cortex failed mid-reply: {failed}" if self._durable_context else "cortex failed during a private reply")

        if self._discard_reply:
            return
        if not spoken:
            # Nothing came out. Say so; do not leave the model's silence in the context.
            self.messages.pop()
            await self._say("[concern] Sorry. I lost my thought.")
            return

        said = " ".join(spoken)
        reply_id = self._record("said", said, self.cfg.name, emotion=emotion, reply_to=episode_id, **sources)
        self.messages.append({
            "role": "assistant", "content": f"[{emotion}] {said}", "episode_id": reply_id, **sources,
        })
        await self._send_reply("say.request", text="", seq=len(spoken) + 1, last=True)
        await self._send_reply("mind.reply_done", spoke=True)

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
        if entering:
            self._guest_memory = self.memory = Memory(None)
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
            episode = self._record("heard", text, speaker)
            self._last_memory = ("fact", self.memory.remember(arg, speaker, episode))
            # A second telling confirms the first fact; "forget that" must
            # still take the sentence just said.
            self._last_telling = episode
            if self._owner_services:
                await self._say("[joy] Okay. I'll remember that.")
            else:
                await self._say("[joy] Okay. I'll remember that for this session.")
        elif kind in ("recall", "recall_said", "recall_heard"):
            self._record("command", "recall", speaker)
            await self._recall_command(arg, speaker, episode_kind=kind.removeprefix("recall_") if kind != "recall" else None)
        elif kind == "forget_last":
            if await self._edit_last():
                await self._say("[neutral] Okay. That's forgotten.")
            else:
                await self._say("[concern] I'm not sure what to forget. Tell me what it was about.")
        elif kind == "forget":
            # The request itself is not logged: it may name the very thing to forget.
            # "Forget yesterday" means that day's conversation: the date stays a
            # date range and is never turned into a word to search for.
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
        elif kind == "correct":
            self._record("command", "correction", speaker)
            if await self._edit_last(replacement=arg):
                await self._say("[neutral] Got it. I've changed that.")
            else:
                await self._say("[concern] I'm not sure which memory you mean.")
        elif kind == "time":
            self._record("command", text, speaker)
            await self._say(f"[neutral] {await self.skills.reply(command, self.clock)}")
            await self._publish_clock()
        elif kind == "name":
            self._record("command", text, speaker)
            await self._say(f"[joy] My name is {self.cfg.name}.")
        elif kind == "age":
            self._record("command", text, speaker)
            await self._say(_age_reply(self._life, time.time()))
        elif kind == "battery":
            if self.battery is None:
                await self._say("[concern] I can't read my battery level right now.")
                return
            # Running low and not plugged in is worth a worried look.
            low = self.battery <= LOW_BATTERY and self.charging is not True and self.on_ac is not True
            reply = f"[{'concern' if low else 'neutral'}] My battery is at {self.battery} percent."
            if self.charging is True:
                reply += " It's charging."
            elif self.on_ac is True:
                reply += " I'm plugged in."
            elif self.on_ac is False:
                reply += " I'm running on battery."
            await self._say(reply)
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
            await self._say("[concern] I can't see yet. My eyes aren't connected.")
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
                reply = await self.skills.reply(command)
            except (OSError, sqlite3.Error) as exc:
                log("local skill failed:", exc)
                reply = "I couldn't access my local settings or reminders."
            await self._say(f"[neutral] {reply}", keep=False)

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
            self._last_memory = ("fact", facts[0].id)
            told = " ".join(
                f"{'You' if fact.speaker == speaker else fact.speaker or 'Someone'} told me: {fact.text}."
                for fact in facts
            )
            await self._say(f"[joy] {told}", recalled=[fact.id for fact in facts], memory_reply=True)
        elif history:
            episode = history[0]
            self._last_memory = ("episode", episode.id)
            who = "you" if episode.speaker == speaker else episode.speaker or "someone"
            if episode.kind == "said":
                who = "I"
            when = time.localtime(episode.ts)
            date = f"{time.strftime('%B', when)} {when.tm_mday}, {when.tm_year}"
            excerpt = history_excerpt(episode, 260)
            await self._say(
                f"[joy] On {date}, {who} said: {excerpt.rstrip('.!?')}.",
                episodes=[episode.id], memory_reply=True,
            )
        else:
            self._last_memory = None
            await self._say("[concern] I don't remember anything about that.")

    async def _forget_confirmed(self, pending):
        change = self.memory.edit(fact_ids=pending["facts"], episode_ids=pending["episodes"])
        if change:
            await self._scrub(change)
            self._last_memory = None
            count = "That's" if len(pending["facts"]) + len(pending["episodes"]) == 1 else "Those are"
            await self._say(f"[neutral] Okay. {count} forgotten.")
        else:
            await self._say("[neutral] There was nothing left to forget.")

    async def _edit_last(self, replacement=None):
        if self._last_memory is None:
            return None
        kind, key = self._last_memory
        targets = {"fact_ids" if kind == "fact" else "episode_ids": [key]}
        if kind == "fact" and replacement is None and self._last_telling is not None:
            targets["episode_ids"] = [self._last_telling]
        self._last_telling = None
        change = self.memory.edit(**targets, replacement=replacement)
        if change:
            await self._scrub(change)
        if not change or replacement is None:
            self._last_memory = None
        return change

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
            return True
        return False

    # -- offline overnight maintenance -----------------------------------

    @cached_property
    def _dreams(self):
        return DreamStore(self.memory)

    def _interrupt_dream(self):
        task = self._dream_task
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            return task
        return None

    def _eligible_night(self):
        if not self._durable_context:
            return None
        local_time = self.skills.now()
        if self._dream_policy.eligible(local_time=local_time) and self._dream_consent.is_file():
            return local_time.date().isoformat()
        return None

    def _start_dream(self):
        now = time.monotonic()
        if (self.state != "asleep" or self.lid_open or now < self._dream_next_at
                or (self._dream_task is not None and not self._dream_task.done())
                or (self._turn is not None and not self._turn.done())):
            return
        night = self._eligible_night()
        if night is None or night == self._dream_idle_night:
            return
        self._dream_next_at = now + DREAM_INTERVAL
        self._dream_task = asyncio.create_task(self._dream_batch(night))

    async def _dream_batch(self, night):
        started = time.monotonic()
        if self._eligible_night() != night:
            return
        try:
            batch = self._dreams.stage(self.cfg.owner, night=night)
            if batch is None:
                self._dream_idle_night = night
                return
            # Give the bus a chance to deliver wake, power, or lid events before committing.
            await asyncio.sleep(DREAM_YIELD)
            if time.monotonic() - started <= DREAM_STAGE_BUDGET and self._eligible_night() == night:
                published = self._dreams.publish(batch)
                lid = self._latest_lid
                if published and batch.sources and lid and lid.get("closed") is True and type(lid.get("seq")) is int:
                    # Only a receipt for the current lid closure crosses the bus.
                    await self.bus.send("mind.dream_committed", lid_seq=lid["seq"])
        except sqlite3.Error as exc:
            log("could not update the overnight journal:", exc)

    # -- keeping the context in bounds -------------------------------------

    def _make_room(self):
        """Last resort, mid-conversation: drop the oldest turns to fit.

        Dropping changes the start of the prompt, so the model must read all of
        it again. Trimming just under the limit would mean paying that on every
        later turn; trimming to half pays it once and buys many turns.
        """
        if self._tokens() <= 0.9 * self.cfg.ctx_tokens:
            return
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
        await self._check_expiry()
        if self._tokens() > 0.6 * self.cfg.ctx_tokens:
            await self._compact()
        self.save_context()
        if self.state == "asleep":
            await self._save_slot()

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
            await self._warm_prefix()
