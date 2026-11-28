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


def _log(*parts):
    print("mind: chat:", *parts, file=sys.stderr, flush=True)


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

    def __init__(self, bus, *, name="Pip", owner="friend", guest=guest_latched, log=_log):
        self.bus = bus
        self.guest = guest
        self.log = log
        self.mode = DEFAULT
        self._guest_event = None
        self._published = None
        self._chat = type_chat.TypeChat(bus, name=name, owner=owner, show=self._chat_says)
        self._tasks = set()
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
        if tone in ("warning", "danger"):
            task = asyncio.get_running_loop().create_task(self._notice(text, "warning"))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _notice(self, text, tone="muted"):
        text = type_chat.printable(text or "")[:NOTICE_MAX]
        if text:
            await self.bus.send("chat.notice", text=text, tone=tone if tone in ("warning", "muted") else "muted")

    async def type_line(self, text):
        """One typed line from the dock. True if it was passed on."""
        if not isinstance(text, str):
            return False
        if self.guest_here():
            return False  # a guest has no written chat; nothing of it is kept
        if self.mode == DEFAULT:
            await self._notice("Switch to written chat to type to me.", "muted")
            return False
        return await self._chat.say(text)

    # -- the bus -----------------------------------------------------------

    async def handle(self, event):
        topic = event.get("t")
        if topic == "ui.text":
            await self.type_line(event.get("text"))
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
        else:
            self._chat.on_event(event)

    async def start(self):
        await self.bus.start()
        self._listener = asyncio.create_task(self._listen())
        self._watcher = asyncio.create_task(self._watch())

    async def stop(self):
        for task in (self._listener, self._watcher):
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


async def run(path, mind_dir):
    name, owner = type_chat.profile_names(mind_dir)
    bridge = ChatBridge(Bus(ORGAN, SUBS, path=path), name=name, owner=owner)
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
    args = parser.parse_args(argv)
    asyncio.run(run(args.bus, Path(args.mind_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
