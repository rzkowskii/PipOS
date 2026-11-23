"""A simulated body for development.

Your keyboard is its ears and the Mac's `say` command is its voice. This takes
the place of the `voice` organ, and lets you set the body's sensors by hand.

Type a line and press Return to say it to the being. Lines starting with a
slash are commands:

  /mute              press the mute key
  /interrupt         press Esc
  /lid close|open    close or open the lid
  /battery N         set the battery to N percent, unplugged
  /plug              plug the charger in
  /lux N             set room light (below 5 is dark)
  /temp N            set body temperature in Celsius
  /hour N            pretend it is N o'clock (/hour -1 to stop pretending)
  /quit              leave
"""

import argparse
import asyncio
import math
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind.bus import Bus  # noqa: E402
from mind.profile import default_mind_dir, load_profile  # noqa: E402

SUBS = ["say.request", "say.stop", "voice.mute", "state.changed"]
AWAKE = ("attending", "listening")


class SimBody:
    def __init__(self, bus, owner, silent=False, rate=190, out=sys.stdout):
        self.bus = bus
        self.owner = owner
        self.silent = silent or shutil.which("say") is None
        self.rate = rate
        self.out = out
        self.state = "waking"
        self.muted = False
        self._queue = asyncio.Queue()
        self._state_changed = asyncio.Event()
        self._proc = None
        self._stop = False
        self._started = False

    def show(self, text):
        print(text, file=self.out, flush=True)

    async def run(self, lines):
        """`lines` is an async iterator of what the person types."""
        await self.bus.start()
        speaker = asyncio.create_task(self._speak_loop())
        listener = asyncio.create_task(self._bus_loop())
        try:
            async for line in lines:
                line = line.strip()
                if line == "/quit":
                    break
                if line.startswith("/"):
                    await self._command(line)
                elif line:
                    await self.hear(line)
        finally:
            await self._stop_speaking()
            speaker.cancel()
            listener.cancel()
            await self.bus.stop()

    # -- ears ------------------------------------------------------------

    async def _wait_state(self, wanted, timeout):
        deadline = time.monotonic() + timeout
        while self.state not in wanted:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._state_changed.clear()
            try:
                await asyncio.wait_for(self._state_changed.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        return True

    async def hear(self, text):
        """Say `text` to the being, the way the real ears would report it."""
        if self.muted:
            self.show("  (the mic is closed: it cannot hear you. /mute to open it)")
            return
        if self.state in ("thinking", "speaking"):
            await self.bus.send("key.interrupt")
            await self._wait_state(AWAKE, 3.0)
        if self.state not in AWAKE:
            await self.bus.send("hear.wake")
            if not await self._wait_state(AWAKE, 8.0):
                self.show(f"  (it is {self.state} and did not respond to its name)")
                return
        await self.bus.send("hear.speech_start")
        words = text.split()
        for i in range(1, len(words)):
            await self.bus.send("hear.partial", text=" ".join(words[:i]))
            await asyncio.sleep(0.05)
        await self.bus.send("hear.final", text=text, speaker=self.owner)

    async def _command(self, line):
        parts = line.split()
        cmd, arg = parts[0], parts[1] if len(parts) > 1 else ""

        def number():
            try:
                return float(arg)
            except ValueError:
                return None

        if cmd == "/mute":
            await self.bus.send("key.mute")
        elif cmd == "/interrupt":
            await self.bus.send("key.interrupt")
        elif cmd == "/lid" and arg in ("close", "open"):
            await self.bus.send("sim.body", lid_closed=arg == "close")
        elif cmd == "/battery" and number() is not None:
            await self.bus.send("sim.body", battery=number(), charging=False)
        elif cmd == "/plug":
            await self.bus.send("sim.body", charging=True)
        elif cmd == "/lux" and number() is not None:
            await self.bus.send("sim.body", lux=number())
        elif cmd == "/temp" and number() is not None:
            await self.bus.send("sim.body", temp=number())
        elif cmd == "/hour" and number() is not None:
            await self.bus.send("sim.body", hour=number())
        else:
            self.show(__doc__.split("slash are commands:")[1].rstrip())

    # -- voice -----------------------------------------------------------

    async def _bus_loop(self):
        while True:
            event = await self.bus.recv()
            topic = event.get("t")
            if topic == "say.request":
                self._queue.put_nowait(event)
            elif topic == "say.stop":
                await self._stop_speaking()
            elif topic == "voice.mute":
                self.muted = bool(event.get("on"))
                self.show("  (mic closed)" if self.muted else "  (mic open)")
            elif topic == "state.changed":
                state = event.get("state")
                if state and state != self.state:
                    self.state = state
                    self._state_changed.set()
                    self.show(f"  ({state})")
                if event.get("unwell"):
                    self.show("  (unwell: an organ is missing)")

    async def _stop_speaking(self):
        self._stop = True
        self._started = False
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass

    async def _speak_loop(self):
        while True:
            event = await self._queue.get()
            self._stop = False
            text = (event.get("text") or "").strip()
            if text:
                if not self._started:
                    self._started = True
                    await self.bus.send("say.started")
                self.show(f"  {text}")
                await self._utter(text)
            if self._stop:
                continue
            if event.get("last"):
                fields = {"final": True}
                announcement = event.get("announcement")
                if self._started and isinstance(announcement, str) and announcement:
                    fields["announcement"] = announcement
                self._started = False
                await self.bus.send("say.done", **fields)
            elif self._queue.empty() and self._started:
                await self.bus.send("say.done", final=False)

    async def _utter(self, text):
        """Speak one clause, reporting loudness while it plays."""
        t0 = time.monotonic()
        if self.silent:
            length = min(0.06 * len(text.split()) + 0.05, 0.6)
            done = lambda: time.monotonic() - t0 >= length  # noqa: E731
        else:
            self._proc = await asyncio.create_subprocess_exec("say", "-r", str(self.rate), text)
            done = lambda: self._proc.returncode is not None  # noqa: E731
            waiter = asyncio.create_task(self._proc.wait())
        while not done() and not self._stop:
            t = time.monotonic() - t0
            level = 0.45 + 0.4 * abs(math.sin(t * 9.0)) * abs(math.sin(t * 2.3 + 1.0))
            await self.bus.send("say.level", v=round(level, 2))
            await asyncio.sleep(0.04)
        if not self.silent:
            if self._proc.returncode is None:
                self._proc.terminate()
            await waiter
            self._proc = None
        await self.bus.send("say.level", v=0.0)


async def stdin_lines():
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        yield line


async def main():
    parser = argparse.ArgumentParser(description="Simulated body: keyboard ears, `say` voice.")
    parser.add_argument("--bus", help="bus socket path")
    parser.add_argument("--owner", help="who is speaking (default: the owner in the config)")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config")
    parser.add_argument("--silent", action="store_true", help="print what it says, do not speak aloud")
    args = parser.parse_args()
    owner = args.owner
    if owner is None:
        try:
            owner = load_profile(args.config, default_mind_dir(), strict=False).owner
        except (OSError, ValueError):
            owner = "friend"
    body = SimBody(Bus("voice", SUBS, path=args.bus), owner, silent=args.silent)
    print("Type to talk to it. /help for body controls, /quit to leave.", flush=True)
    await body.run(stdin_lines())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
