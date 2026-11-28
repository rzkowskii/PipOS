"""Written chat on the face screen: the mode, and typed lines from the dock.

Run:  python3 -m mind.chat_bridge --bus PATH

The owner picks how to talk to the being from the face's dock, a key, or by
voice. There are three modes (`MODES`):

- `spoken`: hear and answer aloud, as ever.
- `typed_voice`: the owner types, the being answers aloud and the words show
  as they are played (the face's captions, from the voice's `say.played`).
- `typed_text`: the owner types, the being stays silent and answers in text;
  actions still run and their confirmations show as text.

This organ holds the mode and publishes it (`chat.mode`, retained by the
brainstem, so the face and the voice learn it on connect). It also takes the
dock's typed lines (`ui.text`) and puts them on the bus exactly as the local
menu's typing chat does (mind/type_chat.py: the being's name heard, then the
finished words, with the owner as the speaker), so the being's state, the lid
and the mute key decide whether it listens as for any other line.

Rules kept here, not on the face:

- The mode is never saved: every start is `spoken`, so a reboot cannot leave
  the being deaf (the typed modes switch the microphone's capture off) or
  silent with nobody typing.
- A guest forces `spoken`, and no typed line is passed on while one is here.
- Nothing here waits for the voice: written replies work with the voice organ
  down or absent (the face captions a reply when it ends if no receipts come).
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from . import type_chat
from .bus import Bus

# Its own name: "keyboard" is the local menu's typing chat, which another unit
# may not take (crates/brainstem/src/peer.rs), and two organs of one name
# replace each other.
ORGAN = "chat"
MODES = ("spoken", "typed_voice", "typed_text")
DEFAULT = "spoken"
# (input, reply) for each mode, as the voice reads them.
FIELDS = {
    "spoken": ("voice", "voice"),
    "typed_voice": ("typed", "voice"),
    "typed_text": ("typed", "text"),
}
ACTION_PREFIX = "chat.mode."
ACTIONS = {ACTION_PREFIX + name: name for name in (*MODES, "next")}
SUBS = (*type_chat.SUBS, "ui.text", "ui.action", "chat.mode.request", "app.privacy")
GUEST_LATCH = "/run/being/guest"
NOTICE_MAX = 120
GUEST_POLL = 1.0
# A line waiting longer than this for its turn is dropped, not delivered late;
# and only this many may wait at once.
LINE_MAX_AGE = 10.0
LINE_QUEUE = 8


def _log(*parts):
    print("mind: chat:", *parts, file=sys.stderr, flush=True)


def written_chat_enabled(config_dir):
    """The image's switch for the two written modes: `"written_chat": false` in
    being.json turns them off (every start is spoken and stays spoken). Anything
    else, a missing file or key included, leaves them on."""
    try:
        settings = json.loads((Path(config_dir) / "being.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(settings, dict) and settings.get("written_chat") is False)


def next_mode(mode):
    return MODES[(MODES.index(mode) + 1) % len(MODES)] if mode in MODES else DEFAULT


def guest_latched(path=GUEST_LATCH):
    """The boot-local guest latch (the brainstem's own test: any entry counts)."""
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


class ChatBridge:
    """The mode, and the typed lines of the dock. `bus` is a mind.bus.Bus
    joined as `keyboard`; `guest()` says whether a guest is here."""

    def __init__(self, bus, *, name="Pip", owner="friend", guest=guest_latched, log=_log, enabled=True):
        self.bus = bus
        self.enabled = enabled
        self.guest = guest
        self.log = log
        self.mode = DEFAULT
        self._guest_event = None
        self._published = None
        self._chat = type_chat.TypeChat(bus, name=name, owner=owner, show=self._chat_says, allow=self._may_send)
        self._tasks = set()
        # Typed lines in flight (waiting for their turn or being said).
        self._lines = set()
        self._listener = None
        self._watcher = None

    # -- the mode --------------------------------------------------------

    def fields(self, mode=None):
        mode = mode or self.mode
        inp, reply = FIELDS[mode]
        return {"mode": mode, "input": inp, "reply": reply}

    def guest_here(self):
        if self._guest_event is not None:
            return self._guest_event
        try:
            return bool(self.guest())
        except Exception:
            return True  # when unsure, nobody sees the owner's words

    async def publish(self, *, force=False):
        """Tell everyone the mode (only when it changed, unless forced)."""
        key = (self.mode, self._bus_generation())
        if not force and key == self._published:
            return
        self._published = key
        await self.bus.send("chat.mode", **self.fields())

    def _bus_generation(self):
        return getattr(self.bus, "generation", 0)

    async def set_mode(self, mode, *, source="key"):
        """Switch to `mode` ("next" cycles). A guest keeps `spoken`.
        Returns the mode now in force."""
        if mode == "next":
            mode = next_mode(self.mode)
        if mode not in MODES:
            return self.mode
        if mode != DEFAULT and not self.enabled:
            await self._notice("Written chat is switched off in this image.", "warning")
            return self.mode
        if mode != DEFAULT and self.guest_here():
            mode = DEFAULT
            await self._notice("Written chat is for the owner. A guest talks to me out loud.", "warning")
        changed = mode != self.mode
        self.mode = mode
        if changed:
            self.log(f"mode {mode} ({source})")
        await self.publish(force=changed)
        return self.mode

    async def guest_changed(self, guest):
        """A guest arrived or left. Arriving puts the mode back to spoken."""
        self._guest_event = bool(guest)
        if guest and self.mode != DEFAULT:
            self.mode = DEFAULT
            self.log("guest arrived: spoken")
            await self.publish(force=True)

    # -- notices and lines -------------------------------------------------

    def _chat_says(self, text, tone="text"):
        """What the typing chat would print. Only its problems reach the dock;
        replies are the face's captions."""
        if tone in ("warning", "danger") and self._may_send():
            task = asyncio.get_running_loop().create_task(self._notice(text, "warning"))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _notice(self, text, tone="muted"):
        text = type_chat.printable(text or "")[:NOTICE_MAX]
        if text:
            await self.bus.send("chat.notice", text=text, tone=tone if tone in ("warning", "muted") else "muted")

    def _may_send(self):
        """Asked right before a line goes out: still a written mode, no guest."""
        return self.mode != DEFAULT and not self.guest_here()

    async def type_line(self, text):
        """One typed line from the dock. True if it was passed on."""
        if not isinstance(text, str):
            return False
        if self.guest_here():
            return False  # a guest has no written chat; nothing of it is kept
        if self.mode == DEFAULT:
            await self._notice("Switch to written chat to type to me.", "muted")
            return False
        return await self._chat.say(text, expires=asyncio.get_running_loop().time() + LINE_MAX_AGE)

    def _line(self, text):
        """Say one line as its own task: waking the being waits for the state
        updates that this same receive loop delivers, so it must never be
        awaited here. The typing chat's lock keeps lines in order."""
        task = asyncio.get_running_loop().create_task(self._line_task(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _line_task(self, text):
        if len(self._lines) >= LINE_QUEUE:
            await self._notice("Too many lines waiting. Try again in a moment.", "warning")
            return
        self._lines.add(asyncio.current_task())
        try:
            await self.type_line(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"typed line failed: {exc!r}")
        finally:
            self._lines.discard(asyncio.current_task())

    async def idle(self):
        """Wait until every typed line in flight is done (for tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- the bus -----------------------------------------------------------

    async def handle(self, event):
        topic = event.get("t")
        # What the being is doing reaches the typing chat first, always, so a
        # line waiting for it to wake is never waiting on its own task.
        self._chat.on_event(event)
        if topic == "ui.text":
            self._line(event.get("text"))
        elif topic == "ui.action":
            mode = ACTIONS.get(event.get("id"))
            if mode is not None:
                await self.set_mode(mode, source=event.get("via") or "ui")
        elif topic == "chat.mode.request":
            mode = event.get("mode")
            if isinstance(mode, str) and mode in (*MODES, "next"):
                await self.set_mode(mode, source="voice")
        elif topic == "app.privacy":
            if type(event.get("guest")) is bool:
                await self.guest_changed(event["guest"])

    async def start(self):
        await self.bus.start()
        self._listener = asyncio.create_task(self._listen())
        self._watcher = asyncio.create_task(self._watch())

    async def stop(self):
        for task in (self._listener, self._watcher, *self._tasks):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.bus.stop()

    async def _listen(self):
        while True:
            event = await self.bus.recv()
            try:
                await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad event must not stop the bridge
                self.log(f"{event.get('t')} failed: {exc!r}")

    async def _watch(self):
        """Republish after a bus reconnect (the brainstem may have restarted and
        lost the retained mode), and follow the guest latch."""
        while True:
            await asyncio.sleep(GUEST_POLL)
            try:
                if self.bus.connected.is_set():
                    await self.publish()
                    if self._guest_event is None and self.guest_here():
                        await self.guest_changed(True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"watch failed: {exc!r}")


async def run(path, mind_dir, config_dir):
    name, owner = type_chat.profile_names(mind_dir)
    bridge = ChatBridge(Bus(ORGAN, SUBS, path=path), name=name, owner=owner,
                        enabled=written_chat_enabled(config_dir))
    await bridge.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await bridge.publish(force=True)
    await stop.wait()
    await bridge.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bus", default=None)
    parser.add_argument("--mind-dir", default=type_chat.MIND_DIR)
    parser.add_argument("--config", default="/etc/being")
    args = parser.parse_args(argv)
    asyncio.run(run(args.bus, Path(args.mind_dir), args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
