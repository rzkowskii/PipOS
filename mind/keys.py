"""The keyboard's top row, as the mind answers it.

The laptop boots with `hid_apple.fnmode=1`, so the MacBookPro9,2's top row
does what is printed on it and fn+key sends F1 to F12 (Pip's own F1, F2, F9
and F10 then need fn). The face (crates/face/src/keys.rs), or the Try menu
through mind/local_screen.py, sends these keys to the brainstem, which passes
them on here:

- `key.brightness {dir}`: the panel, one Mac-sized step (about a sixteenth)
  through `being.brightness`, which the brainstem owns and clamps. Tagged
  `req: "key"` (REQ), which the brainstem echoes on `body.brightness`, so
  the turn loop never takes a key's answer for a spoken "brighter".
- `key.kbd_light {dir}`: the keyboard's light, `up`, `down` or `toggle`,
  through `being.kbd_light` (the brainstem owns the applesmc LED).
- `key.volume {dir}`: the speaker, in sixteenths, through `voice.volume`, the
  same path "louder" takes. `feedback: true` asks the voice for a short click
  at the new level, as a Mac makes.
- `key.mute_speaker`: the speaker off (volume 0) and back to where it was.
  This is the speaker; the microphone's mute stays on M and fn+F10. A key
  mute is for now only: the saved level stays the one from before it
  (`voice.volume` carries `persist: false`, and `volume_report` keeps the
  voice's report of 0 out of the mind's record); a ring or a plea brings
  the speaker back first (`unmute`), and the next start puts back the level
  a mute left silent (`start`, from a marker in the state directory). The
  volume keys stepped down to nothing are the same kind of mute.
- `key.apps` (Launchpad): the workshop terminal, as "open the terminal"
  opens it; pressed again it puts the terminal away.
- `ui.action`: a Quick Menu selection made with the keyboard, through the
  same workshop open route as its spoken command.

Mission Control (the menu, as F2) and play/pause (listen, or stop a reply)
are the face's own and never reach the mind; previous, next and eject do
nothing. Code answers every key, never the model, and no key speaks: the face
shows a level bar from what the brainstem and the voice report they set
(`body.brightness`, `body.kbd_light`, `voice.volume_state`).
"""

import asyncio
import sys
from pathlib import Path

from . import actions

TOPICS = ("key.brightness", "key.volume", "key.mute_speaker", "key.kbd_light", "key.apps", "ui.action")
# The tag on the brainstem requests a key makes; its answer carries it back.
REQ = "key"
# How Pip names its menu key on the laptop, whose printed F2 is brightness up
# (hid_apple.fnmode=1): for every spoken or written "press F2".
MENU_KEYS = "Mission Control, or fn and F2"
# Left in the state directory while a key mute holds the speaker at 0, so a
# restart puts the sound back (the voice may have stored the 0).
MUTED_MARKER = "speaker-muted"
# A Mac's level keys have sixteen steps from nothing to full.
STEPS = 16
# One brightness key, in percentage points (the brainstem takes 1 to 50 and
# never goes below its own floor, so the face always shows).
BRIGHTNESS_STEP = 6
# Where the speaker comes back to after a mute it does not remember
# (the mind restarted while it was muted).
UNMUTE_DEFAULT = 50
# What Launchpad asks the workshop for, by what the terminal shows now.
APP_PHRASES = {"shown": "minimise the terminal", "minimised": "show the terminal"}
APP_OPEN = "open the terminal"
DIRECTIONS = ("up", "down")


def _log(*parts):
    print("mind: keys:", *parts, file=sys.stderr, flush=True)


def volume_step(level, up):
    """The next speaker level from `level` (0 to 100), one sixteenth up or down,
    on the same sixteen steps a Mac uses (0, 6, 12, 19 ... 94, 100)."""
    level = max(0, min(100, int(level)))
    step = round(level * STEPS / 100) + (1 if up else -1)
    step = max(0, min(STEPS, step))
    return round(step * 100 / STEPS)


class Keys:
    """Answers the top row's keys. `send` is the bus's `send(topic, **fields)`;
    `volume()` is the mind's speaker level now and `set_volume(level)` keeps a
    new one (the turn loop's own record, as "louder" keeps it); `apps` is the
    workshop's AppBridge and `guest()` says whether a guest is here;
    `state_dir` holds MUTED_MARKER (None: no marker, nothing survives)."""

    def __init__(self, send, *, volume, set_volume, apps=None, guest=None, state_dir=None, log=_log):
        self.send = send
        self.volume = volume
        self.set_volume = set_volume
        self.apps = apps
        self.guest = guest or (lambda: None)
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.log = log
        # While a key holds the speaker at 0: the level to come back to (the
        # saved one) and the level the volume keys step from.
        self.muted_from = None
        self._step_from = None
        self._app_task = None

    @property
    def muted(self):
        """Whether a key has the speaker silent now."""
        return self.muted_from is not None

    async def handle(self, event):
        """Answer one key event. Returns True when it was one of TOPICS."""
        topic = event.get("t")
        if topic not in TOPICS:
            return False
        try:
            if topic == "key.brightness":
                await self._brightness(event.get("dir"))
            elif topic == "key.kbd_light":
                await self._kbd_light(event.get("dir"))
            elif topic == "key.volume":
                await self._volume(event.get("dir"))
            elif topic == "key.mute_speaker":
                await self._mute()
            elif topic == "key.apps":
                self._launchpad()
            elif topic == "ui.action" and event.get("via") == "key":
                if phrase := actions.quick_menu_app_phrase(event.get("id")):
                    self._open_app(phrase, "Quick Menu")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a key must never stop the mind
            self.log(f"{topic} failed: {exc!r}")
        return True

    def volume_report(self, level):
        """The voice said its level is `level` (`voice.volume_state`). True
        when that is a key mute's silence, which the mind must not keep as
        the owner's level; any other level ends the mute (a "louder" said
        while muted brings the sound back)."""
        if type(level) is not int:
            return False
        if self.muted and level == 0:
            return True
        if self.muted and level > 0:
            self._unmuted()
        return False

    async def unmute(self, why=""):
        """Bring the speaker back from a key mute before something must be
        heard (a ring, a plea). True when it was muted."""
        if not self.muted:
            return False
        level = self.muted_from
        self._unmuted()
        try:
            await self.send("voice.volume", level=level)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"could not bring the speaker back: {exc!r}")
            return False
        self.log(f"the speaker is back at {level}" + (f": {why}" if why else ""))
        return True

    async def start(self):
        """Once the mind is ready: a key mute does not outlive the mind, so a
        speaker one left silent (the marker is still there) gets its saved
        level back. Nothing is sent otherwise."""
        if self.muted or not self._marker_exists():
            return
        self._clear_marker()
        level = self._current_volume() or UNMUTE_DEFAULT
        try:
            await self.send("voice.volume", level=level)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"could not bring the speaker back after a restart: {exc!r}")
            return
        self.log(f"a key mute ended with the last run: the speaker is back at {level}")

    async def _brightness(self, direction):
        if direction not in DIRECTIONS:
            self.log(f"ignored a brightness key going {direction!r}")
            return
        await self.send("being.brightness", step=1 if direction == "up" else -1, by=BRIGHTNESS_STEP, req=REQ)

    async def _kbd_light(self, direction):
        if direction == "toggle":
            await self.send("being.kbd_light", toggle=True, req=REQ)
        elif direction in DIRECTIONS:
            await self.send("being.kbd_light", step=1 if direction == "up" else -1, req=REQ)
        else:
            self.log(f"ignored a keyboard-light key going {direction!r}")

    def _current_volume(self):
        level = self.volume()
        return level if type(level) is int and 0 <= level <= 100 else UNMUTE_DEFAULT

    async def _set(self, level):
        self._unmuted()
        self.set_volume(level)
        await self.send("voice.volume", level=level, feedback=True)

    async def _silence(self, back_to, step_from):
        """The speaker to 0 for now: the saved level stays `back_to`."""
        self.muted_from, self._step_from = back_to, step_from
        self._write_marker()
        await self.send("voice.volume", level=0, persist=False)

    async def _volume(self, direction):
        if direction not in DIRECTIONS:
            self.log(f"ignored a volume key going {direction!r}")
            return
        # A speaker the mute key silenced steps from the level it was muted
        # at, as a Mac's does; one stepped down to nothing steps from nothing.
        base = self._step_from if self.muted else self._current_volume()
        level = volume_step(base, direction == "up")
        if level > 0:
            await self._set(level)
        elif self.muted:
            # Still muted, now from nothing: the bar says so, and up is the
            # quietest step.
            self._step_from = 0
            await self.send("voice.volume", level=0, persist=False)
        else:
            # The lowest step is silence, as on a Mac: a mute like the key's,
            # which comes back at the quietest step.
            await self._silence(base if base > 0 else UNMUTE_DEFAULT, 0)

    async def _mute(self):
        if self.muted:
            await self._set(self.muted_from)
            return
        current = self._current_volume()
        if current > 0:
            await self._silence(current, current)
        else:
            # Left at 0 by an older build: bring it back somewhere audible.
            await self._set(UNMUTE_DEFAULT)

    def _unmuted(self):
        if self.muted:
            self._clear_marker()
        self.muted_from = self._step_from = None

    def _marker(self):
        return self.state_dir / MUTED_MARKER if self.state_dir is not None else None

    def _marker_exists(self):
        marker = self._marker()
        return marker is not None and marker.exists()

    def _write_marker(self):
        marker = self._marker()
        if marker is None:
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text("1\n", encoding="utf-8")
        except OSError as exc:
            self.log(f"could not note the speaker mute: {exc}")

    def _clear_marker(self):
        marker = self._marker()
        if marker is None:
            return
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"could not clear the speaker mute note: {exc}")

    def _launchpad(self):
        view = (getattr(self.apps, "view", None) or {}).get("terminal")
        self._open_app(APP_PHRASES.get(view, APP_OPEN), "Launchpad")

    def _open_app(self, phrase, source):
        if self.apps is None:
            self.log(f"{source}: no workshop here")
            return
        if self._app_task is not None and not self._app_task.done():
            self.log(f"{source}: the last press is still being answered")
            return
        command = self.apps.parse(phrase, self.guest())
        if command is None:
            self.log(f"{source}: nothing to do")
            return
        self._app_task = asyncio.get_running_loop().create_task(self._run_app(command, source))

    async def _run_app(self, command, source):
        said = []

        async def say(line):
            # A key has no turn to speak in; the panel on screen is the answer,
            # and a refusal is logged.
            said.append(line)

        try:
            await self.apps.handle(command, say)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"{source}: the workshop did not answer: {exc!r}")
            return
        if command.kind == "say" and said:
            self.log(f"{source}: {said[-1]}")

    async def close(self):
        """Stop a workshop key request still being answered."""
        task, self._app_task = self._app_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
