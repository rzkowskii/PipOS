"""Pip's own controls by voice: stop listening, brightness, switching off and
restarting, its version, and whether its last update worked.

- "Stop listening" presses the same mute the M key does (`key.mute` with
  `on: true`; the brainstem lets the mind turn the microphone off, never
  back on) and says how to undo it.
- Brightness goes to the brainstem as `being.brightness` (`step` or
  `level`); the brainstem keeps it within bounds and puts it back on exit.
  The reply waits briefly for its `body.brightness` answer, so at the ends
  Pip says "That's as bright as I go." or "That's as dim as I go.", and a
  change that failed is never reported as done.
- Switching off and restarting always ask first (`Confirm`, strict: only a
  plain yes, never "okay" or "sure"), then the mind saves itself and runs
  `Reply.action`.
- The version is /etc/being/build-id. "Did your update work?" is answered
  from the boot health check and the action log, never guessed.
- "When did you last back up?" and "back yourself up" (to a stick labelled
  for backups, after a yes); see backup.py. The backup runs in a worker
  thread: the answer comes when it ends, or after 20 s with "I'm still
  writing", and the action log records how it really ended.
"""

import asyncio
import re
import time
from pathlib import Path

from . import action_log
from .keys import MENU_KEYS
from .body_talk import Confirm, Reply, is_guest, said

BUILD_ID = Path("/etc/being/build-id")
SLOT_GOOD = Path("/run/being-slot-good")
CMDLINE = Path("/proc/cmdline")
UPTIME = Path("/proc/uptime")

# The boot health check needs a minute of health inside three minutes.
HEALTH_DEADLINE_S = 180
RECENT_UPDATE_S = 14 * 86400

_LEAD = r"^(?:(?:hey|hi|okay|ok)\s+pip\W*\s*)?(?:pip\W*\s*)?(?:please\s+|can\s+you\s+|could\s+you\s+|will\s+you\s+)?"
_END = r"(?:\s+(?:pip|please|now|for\s+(?:a\s+)?(?:bit|while|me)|for\s+now))*\s*[?.!]*$"
_YOU = r"(?:yourself|you)"

STOP_LISTENING = re.compile(
    _LEAD + r"(?:stop\s+listening(?:\s+to\s+(?:me|us))?|don['’]?t\s+listen(?:\s+to\s+(?:me|us))?|do\s+not\s+listen(?:\s+to\s+(?:me|us))?"
    r"|mute\s+(?:yourself|your\s+(?:microphone|mic|ears))|turn\s+(?:off\s+your\s+(?:microphone|mic|ears)|your\s+(?:microphone|mic|ears)\s+off)"
    r"|close\s+your\s+ears|stop\s+hearing\s+me)" + _END, re.IGNORECASE)
BRIGHTER = re.compile(
    _LEAD + r"(?:(?:be\s+|get\s+|go\s+)?brighter|(?:make\s+(?:your\s+(?:screen|face)|it|yourself)\s+)?brighter"
    r"|turn\s+(?:up\s+(?:your|the)\s+brightness|(?:your|the)\s+brightness\s+up)|brightness\s+up|increase\s+(?:your|the)\s+brightness"
    r"|brighten\s+(?:up|your\s+(?:screen|face)))" + _END, re.IGNORECASE)
DIMMER = re.compile(
    _LEAD + r"(?:(?:be\s+|get\s+|go\s+)?(?:dimmer|darker)|(?:make\s+(?:your\s+(?:screen|face)|it|yourself)\s+)?(?:dimmer|darker)"
    r"|turn\s+(?:down\s+(?:your|the)\s+brightness|(?:your|the)\s+brightness\s+down)|brightness\s+down|decrease\s+(?:your|the)\s+brightness"
    r"|dim\s+(?:your\s+(?:screen|face)|yourself|down))" + _END, re.IGNORECASE)
LEVEL = re.compile(
    _LEAD + r"(?:set\s+(?:your\s+|the\s+)?brightness\s+to|(?:your\s+|the\s+)?brightness\s+(?:to\s+)?)\s*(?P<n>\d{1,3}|[a-z]+(?:[\s-][a-z]+)?)\s*(?:percent|%)?"
    + _END, re.IGNORECASE)
FULL = re.compile(_LEAD + r"(?:full\s+brightness|(?:set\s+)?(?:your\s+|the\s+)?brightness\s+(?:to\s+)?(?:full|max(?:imum)?))" + _END,
                  re.IGNORECASE)
SHUTDOWN = re.compile(
    _LEAD + r"(?:shut\s*down|shut\s+" + _YOU + r"\s+down|switch\s+" + _YOU + r"\s+off|switch\s+off|turn\s+" + _YOU + r"\s+off"
    r"|power\s+(?:off|down)|power\s+" + _YOU + r"\s+(?:off|down))" + _END, re.IGNORECASE)
RESTART = re.compile(
    _LEAD + r"(?:restart(?:\s+" + _YOU + r")?|reboot(?:\s+" + _YOU + r")?|start\s+" + _YOU + r"\s+again)" + _END, re.IGNORECASE)
VERSION = re.compile(
    _LEAD + r"(?:what\s+version\s+(?:are\s+you|of\s+pipos\s+are\s+you(?:\s+running)?|are\s+you\s+running|is\s+this)"
    r"|which\s+version\s+(?:are\s+you|is\s+this|are\s+you\s+running)|what(?:['’]s|\s+is)\s+your\s+(?:version|build)(?:\s+number)?"
    r"|what\s+build\s+are\s+you(?:\s+running)?|which\s+build\s+are\s+you)" + _END, re.IGNORECASE)
UPDATE = re.compile(
    _LEAD + r"(?:did\s+(?:your|the)\s+update\s+(?:work|go\s+(?:okay|ok|well|alright))|did\s+(?:the|your)\s+update\s+(?:take|stick)"
    r"|(?:is|was)\s+(?:your|the)\s+update\s+(?:okay|ok|working|good|successful)|how\s+did\s+(?:your|the)\s+update\s+go"
    r"|did\s+you\s+update\s+(?:okay|ok|properly|successfully))" + _END, re.IGNORECASE)
LAST_BACKUP = re.compile(
    _LEAD + r"(?:when\s+(?:did\s+you\s+last\s+back\s*up|were\s+you\s+last\s+backed\s+up|was\s+(?:your|the)\s+last\s+back\s*up)"
    r"(?:\s+yourself)?|have\s+you\s+(?:been\s+)?backed\s+(?:yourself\s+)?up(?:\s+(?:recently|lately))?"
    r"|are\s+you\s+backed\s+up|when(?:['’]s|\s+is)\s+your\s+last\s+back\s*up"
    r"|did\s+(?:the|your|my|that)\s+back\s*up\s+(?:work|go\s+(?:okay|ok|well|alright))"
    r"|(?:is|was)\s+(?:the|your)\s+back\s*up\s+(?:done|finished|okay|ok)|have\s+you\s+finished\s+backing\s+up)"
    + _END, re.IGNORECASE)
BACK_UP = re.compile(
    _LEAD + r"(?:back\s*up\s+yourself|back\s+yourself\s+up|back\s+up(?:\s+(?:your\s+memory|everything))?"
    r"|(?:make|do|run|take)\s+a\s+back\s*up(?:\s+of\s+yourself)?|save\s+a\s+copy\s+of\s+yourself)" + _END, re.IGNORECASE)
_HINT = re.compile(r"\b(?:listen|listening|mute|ears|mic|microphone|bright|brighter|brightness|dimmer|darker|dim|shut|switch|turn"
                   r"|power|restart|reboot|start|version|build|update|back|backup|backed|copy)\b", re.IGNORECASE)
NUMBER_WORDS = {
    "zero": 0, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "seventy five": 75, "eighty": 80, "ninety": 90, "hundred": 100,
    "a hundred": 100, "one hundred": 100, "half": 50,
}


def _strict(on_yes):
    """A confirm only a plain yes answers: switching off cannot be taken back."""
    try:
        return Confirm(on_yes, strict=True)
    except TypeError:  # the spec's frozen Confirm, without `strict`
        return Confirm(on_yes)


def may_match(text):
    return bool(_HINT.search(text or "")) and classify(text) is not None


def brightness_level(spoken):
    spoken = spoken.lower().replace("-", " ").strip()
    if spoken.isdigit():
        value = int(spoken)
        return value if 0 <= value <= 100 else None
    return NUMBER_WORDS.get(spoken)


def classify(text, ctx=None):
    text = said(text, ctx)
    if STOP_LISTENING.match(text):
        return ("mute", None)
    if FULL.match(text):
        return ("level", 100)
    if BRIGHTER.match(text):
        return ("step", 1)
    if DIMMER.match(text):
        return ("step", -1)
    match = LEVEL.match(text)
    if match:
        level = brightness_level(match.group("n"))
        if level is not None:
            return ("level", level)
        # "Set your brightness to banana" is answered; "your brightness is
        # fine" is not a request at all.
        return ("bad_level", None) if re.match(r"set\b", text, re.IGNORECASE) else None
    if SHUTDOWN.match(text):
        return ("poweroff", None)
    if RESTART.match(text):
        return ("reboot", None)
    if VERSION.match(text):
        return ("version", None)
    if UPDATE.match(text):
        return ("update", None)
    if LAST_BACKUP.match(text):
        return ("last_backup", None)
    if BACK_UP.match(text):
        return ("back_up", None)
    return None


def build_id(path=BUILD_ID):
    try:
        first = path.read_text(encoding="ascii", errors="strict")[:256].strip().splitlines()[0].strip()
    except (OSError, IndexError, UnicodeError):
        return None
    return first if re.fullmatch(r"[0-9A-Za-z._+-]{4,128}", first) else None


def version_line(path=BUILD_ID):
    found = build_id(path)
    if found is None:
        return f"[neutral] I can't find my build number. It's shown on the Update page in my menu: press {MENU_KEYS}."
    short = found[:8]
    return f"[neutral] I'm PipOS, build {short}. The full number is on the Update page in my menu: press {MENU_KEYS}."


def _read(path, limit=4096):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def booted_slot(cmdline=CMDLINE):
    words = (_read(cmdline) or "").split()
    for word in words:
        if word in ("rauc.slot=A", "rauc.slot=B"):
            return word[-1]
    return None


def try_boot(cmdline=CMDLINE):
    return "being.try=1" in (_read(cmdline) or "").split()


def uptime(path=UPTIME):
    try:
        return float((_read(path) or "").split()[0])
    except (IndexError, ValueError):
        return None


def when_words(stamp, now):
    days = int((now - stamp) // 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def update_line(state_dir, *, cmdline=CMDLINE, slot_good=SLOT_GOOD, uptime_path=UPTIME, now=None):
    now = time.time() if now is None else now
    if try_boot(cmdline):
        return "[neutral] I'm running from a Try stick, so there's no update to check. Install me first."
    slot = booted_slot(cmdline)
    installed = action_log.last(state_dir, ("update.install",))
    recent = installed is not None and now - installed["ts"] <= RECENT_UPDATE_S
    when = f" The update went in {when_words(installed['ts'], now)}." if recent else ""
    good = (_read(slot_good, 16) or "").strip()
    if good in ("A", "B") and (slot is None or good == slot):
        if recent:
            return f"[joy] Yes. I started on the new system and passed my health check.{when}"
        return "[neutral] I passed my health check on this start. I haven't installed an update lately."
    up = uptime(uptime_path)
    if up is not None and up < HEALTH_DEADLINE_S:
        return "[neutral] I'm still checking myself after starting. Ask me again in a couple of minutes."
    if slot is None:
        return "[neutral] I can't tell: this start didn't come through my update system."
    if recent:
        return ("[concern] Not yet: my health check hasn't passed on this start. If it fails, "
                "I'll go back to my old system by myself.")
    return "[concern] My health check hasn't passed on this start. Say 'run a check-up' and I'll look."


def ago_words(stamp, now):
    seconds = max(0, now - stamp)
    if seconds < 3600:
        return "less than an hour ago"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


NOT_READY = {
    "no_tool": "[concern] I can't make backups: this system has no backup tool.",
    # The laptop's printed F2 is brightness up (hid_apple.fnmode=1): name the
    # menu key as it is pressed there (mind/keys.py MENU_KEYS).
    "no_key": f"[neutral] I need a backup key first. Make my first backup from my menu ({MENU_KEYS}); it makes the key.",
    "no_stick": "[neutral] Plug in a USB stick labelled being-backup, then ask me again.",
    "busy": "[neutral] I'm already backing myself up.",
}
# A voice backup is answered within this long; a slower one carries on in the
# background (logged when it ends), so the turn never hangs on the stick.
ANSWER_WITHIN_S = 20.0
FAILED_WORDS = {
    "no_stick": "the stick went away before I could write to it",
    "no_key": "my backup key is missing",
    "no_tool": "this system has no backup tool",
    "busy": "another backup was already running",
    "failed": "something went wrong writing to the stick",
}


class _VoiceBackup:
    """The one voice backup in flight (RAM only): a thread, and how it ended."""

    def __init__(self):
        self.thread = None
        self.finished = None
        self.outcome = None      # "done" or an Unavailable reason
        self.ended_at = None

    @property
    def running(self):
        return self.thread is not None and not self.finished.is_set()

    def start(self, state_dir, work):
        import threading

        self.outcome, self.ended_at = None, None
        self.finished = threading.Event()

        def attempt():
            from . import backup

            try:
                # Below the face, the voice and the turn: the owner may keep talking.
                backup.lower_priority()
                work()
                self.outcome = "done"
            except Exception as error:  # every ending is told, never raised into a turn
                reason = getattr(error, "reason", None) if isinstance(error, backup.Unavailable) else None
                self.outcome = reason if reason in FAILED_WORDS else "failed"
            finally:
                self.ended_at = time.time()
                if self.outcome == "done":
                    action_log.record(state_dir, "backup")
                elif self.outcome != "busy":
                    action_log.record(state_dir, "backup.failed")
                self.finished.set()

        self.thread = threading.Thread(target=attempt, name="being-voice-backup", daemon=True)
        self.thread.start()


_voice_backup = _VoiceBackup()


def last_backup_line(state_dir, *, now=None, current=None):
    from . import backup

    current = current or _voice_backup
    now = time.time() if now is None else now
    if current.running:
        return "[neutral] I'm still backing myself up. Leave the stick in."
    stamp = backup.last_backup(state_dir)
    if (current.outcome not in (None, "done") and current.ended_at is not None
            and (stamp is None or current.ended_at > stamp)):
        tail = f" My last good one was {ago_words(stamp, now)}." if stamp is not None else ""
        return f"[concern] My last backup didn't work: {FAILED_WORDS[current.outcome]}.{tail}"
    if stamp is None:
        return "[neutral] I haven't been backed up yet. Plug in a stick labelled being-backup and say 'back yourself up'."
    days = (now - stamp) / 86400
    tag = "[concern]" if days > 14 else "[neutral]"
    return f"{tag} My last backup was {ago_words(stamp, now)}."


async def back_up(ctx, *, readiness=None, run=None, current=None, answer_within=ANSWER_WITHIN_S):
    """'Back yourself up': check what is needed, then ask before writing."""
    from . import backup

    current = current or _voice_backup
    state_dir = ctx.state_dir
    if current.running:
        return Reply(NOT_READY["busy"])
    try:
        (readiness or backup.readiness)(state_dir)
    except backup.Unavailable as error:
        return Reply(NOT_READY.get(error.reason, "[concern] I can't back myself up right now."))

    async def go():
        if current.running:
            return Reply(NOT_READY["busy"])
        trusted = getattr(ctx, "clock_trusted", None)
        current.start(state_dir, run or (lambda: backup.stick_backup(state_dir, "voice", clock_trusted=trusted)))
        # Wait in small steps, so the loop (and the face) stay free.
        deadline = time.monotonic() + answer_within
        while not current.finished.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not current.finished.is_set():
            return Reply("[neutral] I'm still writing. Leave the stick in, and ask me in a minute if it worked.")
        if current.outcome == "done":
            return Reply("[joy] Done. I've backed myself up to your stick.")
        if current.outcome in NOT_READY:
            return Reply(NOT_READY[current.outcome])
        return Reply("[concern] The backup didn't work. My older backups on the stick are still there.")

    return Reply("[neutral] Shall I back myself up to your stick now? Say yes.", confirm=Confirm(go))


async def _ask(ctx, topic, **fields):
    """(sent, answer): whether the message left, and what `send` returned
    (the mind returns the brainstem's `body.brightness` fields, or None when
    none came in time)."""
    send = getattr(ctx, "send", None)
    if send is None:
        return False, None
    try:
        answer = await send(topic, **fields)
    except Exception:  # a lost bus is said honestly, never raised into the turn
        return False, None
    return True, answer if isinstance(answer, dict) else None


async def _send(ctx, topic, **fields):
    return (await _ask(ctx, topic, **fields))[0]


BRIGHTEST = "[neutral] That's as bright as I go."
DIMMEST = "[neutral] That's as dim as I go."


def brightness_line(kind, value, answer):
    """What to say once the brainstem has answered (or not, in time)."""
    if answer is not None and answer.get("ok") is False:
        if answer.get("reason") == "no_control":
            return "[neutral] I can't change my brightness on this machine."
        return "[concern] My screen didn't change just now."
    at_max = answer is not None and answer.get("at_max") is True
    at_min = answer is not None and answer.get("at_min") is True
    if kind == "step":
        if value > 0:
            return BRIGHTEST if at_max else "[neutral] Brighter."
        return DIMMEST if at_min else "[neutral] Dimmer."
    level = answer.get("level") if answer is not None else None
    shown = int(level) if type(level) in (int, float) else max(5, value)
    line = f"[neutral] Brightness {shown} percent."
    if answer is not None and answer.get("limited") is True:
        # "Set your brightness to 0" stops at the bottom, and says so.
        line += " That's as dim as I go." if at_min else " That's as bright as I go." if at_max else ""
    return line


def _no_panel(ctx):
    body = getattr(ctx, "body", None) or {}
    return body.get("brightness_reason") == "no_control"


async def handle(text, ctx):
    found = classify(text, ctx)
    if found is None:
        return None
    kind, value = found
    guest = is_guest(ctx)
    if kind == "mute":
        if not await _send(ctx, "key.mute", on=True):
            return Reply("[concern] I couldn't reach my ears. Press M to mute me.")
        return Reply("[neutral] Okay, I'm not listening. Press M to let me listen again.", logged="mute")
    if kind in ("step", "level", "bad_level"):
        if kind == "bad_level":
            return Reply("[neutral] Tell me a brightness from 5 to 100 percent.")
        if _no_panel(ctx):
            return Reply("[neutral] I can't change my brightness on this machine.")
        fields = {"step": value} if kind == "step" else {"level": value}
        sent, answer = await _ask(ctx, "being.brightness", **fields)
        if not sent:
            return Reply("[concern] I couldn't reach my screen just now.")
        line = brightness_line(kind, value, answer)
        changed = answer is None or answer.get("ok") is not False
        return Reply(line, logged="brightness" if changed else None)
    if kind in ("poweroff", "reboot"):
        if guest:
            return Reply("[neutral] Only my owner can switch me off. Closing my lid puts me to sleep.")
        if kind == "poweroff":
            async def off():
                return Reply("[neutral] Saving myself. Goodbye for now.", action="poweroff", logged="shutdown")
            return Reply("[neutral] Shall I switch off? Say yes.", confirm=_strict(off))

        async def again():
            return Reply("[neutral] Restarting. I'll be back in about a minute.", action="reboot", logged="reboot")
        return Reply("[neutral] Shall I restart? Say yes.", confirm=_strict(again))
    if kind == "version":
        return Reply(version_line())
    if kind in ("last_backup", "back_up"):
        if guest:
            return Reply("[neutral] That's for my owner to ask.")
        if kind == "last_backup":
            return Reply(last_backup_line(ctx.state_dir))
        return await back_up(ctx)
    return Reply(update_line(ctx.state_dir))
