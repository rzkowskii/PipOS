"""The whole being, minus the face: the real brainstem binary, the real mind as
its own process, a stand-in model server, and the simulated body.

Needs the brainstem built first:  cargo build -p brainstem
"""

import asyncio
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from mind.bus import Bus
from mind.initiative import InitiativePolicy, QuietHours, save_policy
from sim.body import SUBS, SimBody
from sim.fake_cortex import FakeCortex

ROOT = Path(__file__).resolve().parent.parent
BRAINSTEM = ROOT / "target" / "debug" / "brainstem"


class Watcher:
    """Sees everything on the bus."""

    def __init__(self, path):
        self.bus = Bus("watcher", ["*"], path=path)
        self.events = []
        self._task = None

    async def start(self):
        await self.bus.start()
        self._task = asyncio.create_task(self._collect())
        await asyncio.wait_for(self.bus.connected.wait(), 5)

    async def _collect(self):
        while True:
            self.events.append(await self.bus.recv())

    async def stop(self):
        self._task.cancel()
        await self.bus.stop()

    def states(self):
        return [e["state"] for e in self.events if e["t"] == "state.changed" and "prev" in e]

    async def until(self, what, test, timeout=10.0):
        for _ in range(int(timeout / 0.02)):
            if any(test(e) for e in self.events):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"never saw {what}; states so far: {self.states()}")

    async def state(self, name, timeout=10.0):
        await self.until(f"state {name}", lambda e: e["t"] == "state.changed" and e.get("state") == name, timeout)


@unittest.skipUnless(BRAINSTEM.exists(), "brainstem is not built: run `cargo build -p brainstem`")
class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A short path: Unix socket paths are limited to about 100 bytes.
        self.dir = tempfile.TemporaryDirectory(prefix="be2e-", dir="/tmp")
        self.path = os.path.join(self.dir.name, "bus.sock")
        self.cortex = FakeCortex(delay=0.01).start()
        self.procs = []
        self.stem = self.spawn([str(BRAINSTEM), "--bus", self.path, "--require", "mind,voice"])
        for _ in range(250):
            if os.path.exists(self.path):
                break
            await asyncio.sleep(0.02)
        self.watcher = Watcher(self.path)
        await self.watcher.start()
        self.mind = self.start_mind()
        self.heard = io.StringIO()
        self.body = SimBody(Bus("voice", SUBS, path=self.path), "Richard", silent=True, out=self.heard)
        self.lines = asyncio.Queue()
        self.body_task = asyncio.create_task(self.body.run(self.typed()))

    def spawn(self, argv, **kwargs):
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        self.procs.append(proc)
        return proc

    def start_mind(self):
        self.mind_dir = Path(self.dir.name) / "mind"
        return self.spawn(
            [sys.executable, "-m", "mind", "--bus", self.path, "--cortex", self.cortex.url,
             "--mind-dir", str(self.mind_dir)],
            cwd=ROOT,
        )

    def quiet_now(self, *, greetings=False):
        now = datetime.now()
        window = QuietHours((now - timedelta(minutes=2)).strftime("%H:%M"), (now + timedelta(hours=1)).strftime("%H:%M"))
        save_policy(self.mind_dir, InitiativePolicy(greetings=greetings, quiet_hours=window))

    async def test_due_reminder_waits_for_lid_and_quiet_hours_across_restart(self):
        w = self.watcher
        await w.state("idle")
        self.quiet_now()
        self.lines.put_nowait("Remind me in 3 seconds to check the kettle")
        await w.until("reminder confirmation", lambda e: e["t"] == "say.done" and e.get("final"))
        self.assertIn("Reminder 1 set for", self.said())
        self.lines.put_nowait("/lid close")
        await w.state("asleep")

        self.mind.terminate()
        self.mind.wait(timeout=5)
        w.events.clear()
        self.mind = self.start_mind()
        await w.until("restarted mind", lambda e: e["t"] == "mind.ready")
        await asyncio.sleep(3)
        self.assertFalse(any(e["t"] == "mind.announce" for e in w.events))
        self.assertNotIn("check the kettle", self.said().casefold())

        self.lines.put_nowait("/lid open")
        await w.state("idle")
        w.events.clear()
        await w.until("heartbeat with the lid open", lambda e: e["t"] == "stem.beat")
        self.assertFalse(any(e["t"] == "mind.announce" for e in w.events))
        self.assertNotIn("check the kettle", self.said().casefold())
        save_policy(self.mind_dir, InitiativePolicy())
        await w.until("reminder playback", lambda e: e["t"] == "say.done" and e.get("announcement"))
        self.assertIn("check the kettle", self.said())
        await asyncio.sleep(0.7)
        self.assertEqual(len([e for e in w.events if e["t"] == "stem.announce_ready"]), 1)
        self.assertEqual(self.cortex.chats, [])

    async def test_opt_in_greeting_uses_validated_presence_outside_quiet_hours(self):
        w = self.watcher
        await w.state("idle")
        self.quiet_now(greetings=True)
        eyes = Bus("eyes", ["body.lid", "camera.policy"], path=self.path)
        await eyes.start()
        self.addAsyncCleanup(eyes.stop)
        async with asyncio.timeout(5):
            lid = await eyes.recv()
            self.assertEqual(lid["t"], "body.lid")
            self.assertIs(lid["closed"], False)
            await eyes.send("camera.request", enabled=True)
            while not (grant := await eyes.recv()).get("allowed"):
                pass
        reading = {"present": True, "lid_seq": lid["seq"], "camera_seq": grant["seq"], "x": 0.0, "y": 0.0}
        await eyes.send("see.presence", **reading)
        await w.until("validated presence", lambda e: e["t"] == "body.presence" and e["present"])
        w.events.clear()
        await w.until("heartbeat during quiet hours", lambda e: e["t"] == "stem.beat")
        self.assertFalse(any(e["t"] == "mind.announce" for e in w.events))
        save_policy(self.mind_dir, InitiativePolicy(greetings=True))
        await eyes.send("see.presence", **reading)
        await w.until("greeting playback", lambda e: e["t"] == "say.done" and e.get("announcement"))
        self.assertIn("Hello. Good to see you.", self.said())
        self.assertEqual(self.cortex.chats, [])

    def said(self):
        """Everything it has said aloud so far, as one string."""
        lines = (line.strip() for line in self.heard.getvalue().splitlines())
        return " ".join(line for line in lines if line and not line.startswith("("))

    async def typed(self):
        while True:
            line = await self.lines.get()
            if line is None:
                return
            yield line

    async def asyncTearDown(self):
        self.lines.put_nowait(None)
        await asyncio.wait_for(self.body_task, 5)
        await self.watcher.stop()
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            proc.wait(timeout=5)
        self.cortex.stop()
        self.dir.cleanup()

    async def test_a_day_in_the_life(self):
        w = self.watcher
        await w.state("idle")

        # A conversation turn, typed in place of speech.
        self.lines.put_nowait("Hello there, what makes a kite fly?")
        await w.until("the reply to finish", lambda e: e["t"] == "say.done" and e.get("final"))
        await w.state("attending")
        self.assertEqual(w.states(), ["idle", "attending", "listening", "thinking", "speaking", "attending"])
        self.assertIn("Hello! It's good to hear you.", self.said())
        emotion = next(e for e in w.events if e["t"] == "face.express")
        self.assertEqual(emotion["name"], "joy")
        self.assertTrue(any(e["t"] == "say.level" and e["v"] > 0 for e in w.events), "the face needs the voice level")

        # The body's senses reach the model, and its answer is true.
        self.lines.put_nowait("/battery 37")
        await w.until("the new reading", lambda e: e["t"] == "body.senses" and "37%" in e["line"])
        w.events.clear()
        self.lines.put_nowait("how is your battery")
        await w.until("the reply to finish", lambda e: e["t"] == "say.done" and e.get("final"))
        self.assertIn("37 percent", self.said())

        # Memory, through the whole stack.
        w.events.clear()
        self.lines.put_nowait("remember that my cat is called Tom")
        await w.until("the reply to finish", lambda e: e["t"] == "say.done" and e.get("final"))
        w.events.clear()
        self.lines.put_nowait("what do you remember about my cat")
        await w.until("the reply to finish", lambda e: e["t"] == "say.done" and e.get("final"))
        self.assertIn("You told me: my cat is called Tom.", self.said())

        # Interrupting a long answer.
        w.events.clear()
        self.lines.put_nowait("tell me a long story")
        await w.state("speaking")
        self.lines.put_nowait("/interrupt")
        await w.until("say.stop", lambda e: e["t"] == "say.stop")
        await w.state("attending")
        spoken = self.said().count("sentence number")
        await asyncio.sleep(0.5)
        self.assertEqual(self.said().count("sentence number"), spoken, "it kept talking after Esc")
        self.assertLess(spoken, 29)

        # Closing the lid puts it to sleep; opening it wakes it.
        w.events.clear()
        self.lines.put_nowait("/lid close")
        await w.state("asleep")
        self.lines.put_nowait("/lid open")
        await w.state("waking")
        await w.state("idle")

        # Losing the mind shows; getting it back clears it, and memory is intact.
        w.events.clear()
        self.mind.terminate()
        self.mind.wait(timeout=5)
        await w.until("unwell", lambda e: e["t"] == "state.changed" and e.get("unwell") is True)
        self.mind = self.start_mind()
        await w.until("well again", lambda e: e["t"] == "state.changed" and e.get("unwell") is False)
        await w.until("the mind to be ready", lambda e: e["t"] == "mind.ready")
        w.events.clear()
        self.lines.put_nowait("what do you remember about my cat")
        await w.until("the reply to finish", lambda e: e["t"] == "say.done" and e.get("final"))
        self.assertEqual(self.said().count("You told me: my cat is called Tom."), 2)


if __name__ == "__main__":
    unittest.main()
