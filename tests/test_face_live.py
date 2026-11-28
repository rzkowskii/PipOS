"""The real face binary, running hidden against the real brainstem.

Needs both built first:  cargo build
"""

import asyncio
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from mind.bus import Bus

ROOT = Path(__file__).resolve().parent.parent
FACE = ROOT / "target" / "debug" / "face"
BRAINSTEM = ROOT / "target" / "debug" / "brainstem"
OUT = ROOT / "out"


@unittest.skipUnless(FACE.exists() and BRAINSTEM.exists(), "not built: run `cargo build`")
class FaceLive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="bface-", dir="/tmp")
        self.path = os.path.join(self.dir.name, "bus.sock")
        self.stem = subprocess.Popen(
            [str(BRAINSTEM), "--bus", self.path, "--require", "mind,voice"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(250):
            if os.path.exists(self.path):
                break
            await asyncio.sleep(0.02)
        self.mind = Bus("mind", ["state.changed"], path=self.path)
        self.voice = Bus("voice", [], path=self.path)
        for bus in (self.mind, self.voice):
            await bus.start()
            await asyncio.wait_for(bus.connected.wait(), 5)
        self.state = "waking"
        self.face = None

    async def asyncTearDown(self):
        if self.face is not None and self.face.returncode is None:
            self.face.kill()
            await self.face.wait()
        await self.mind.stop()
        await self.voice.stop()
        if self.stem.poll() is None:
            self.stem.terminate()
        self.stem.wait(timeout=5)
        self.dir.cleanup()

    async def reach(self, state, timeout=6.0):
        async def wait():
            while self.state != state:
                event = await self.mind.recv()
                if event["t"] == "state.changed":
                    self.state = event["state"]
        await asyncio.wait_for(wait(), timeout)

    async def start_face(self, seconds, name, bus=None):
        OUT.mkdir(exist_ok=True)
        self.face = await asyncio.create_subprocess_exec(
            str(FACE), "--hidden", "--bus", bus or self.path, "--exit-after", str(seconds),
            "--record", str(OUT / f"live-{name}.png"), "--size", "640x400",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    async def showing(self):
        """Wait for the face to quit and return the expression it ended on."""
        out, err = await asyncio.wait_for(self.face.communicate(), 20)
        self.assertEqual(self.face.returncode, 0, err.decode())
        match = re.search(r"showing: (\w+)", out.decode())
        self.assertIsNotNone(match, out.decode())
        return match.group(1)

    async def test_shows_the_reply_emotion_while_speaking(self):
        await self.mind.send("mind.ready", cortex=True)
        await self.reach("idle")
        await self.start_face(3.5, "joy")
        await asyncio.sleep(1.2)  # connected and awake
        await self.voice.send("hear.wake")
        await self.voice.send("hear.speech_start")
        await self.voice.send("hear.final", text="hello")
        await self.reach("thinking")
        await self.voice.send("say.started")
        await self.reach("speaking")
        await self.voice.send("say.level", v=0.8, face_cue="joy")
        for _ in range(20):
            await self.voice.send("say.level", v=0.8)
            await asyncio.sleep(0.05)
        self.assertEqual(await self.showing(), "joy")

    async def test_discards_the_reply_emotion_if_interrupted_before_speech(self):
        await self.mind.send("mind.ready", cortex=True)
        await self.reach("idle")
        await self.start_face(3.5, "canceled-thought")
        await asyncio.sleep(1.2)
        await self.voice.send("hear.wake")
        await self.voice.send("hear.speech_start")
        await self.voice.send("hear.final", text="hello")
        await self.reach("thinking")
        await self.mind.send("mind.emotion", name="joy", hold=30)
        await asyncio.sleep(0.25)
        await self.voice.send("key.interrupt")
        await self.reach("attending")
        self.assertEqual(await self.showing(), "attentive")

    async def test_interruption_releases_a_playing_clause_and_drops_late_cues(self):
        await self.mind.send("mind.ready", cortex=True)
        await self.reach("idle")
        await self.start_face(3.5, "interrupted-clause")
        await asyncio.sleep(1.2)
        await self.voice.send("hear.wake")
        await self.voice.send("hear.speech_start")
        await self.voice.send("hear.final", text="hello")
        await self.reach("thinking")
        await self.voice.send("say.started")
        await self.reach("speaking")
        await self.voice.send("say.level", v=0.8, face_cue="joy")
        await self.voice.send("key.interrupt")
        await self.reach("attending")
        await self.voice.send("say.level", v=0.8, face_cue="concern")
        self.assertEqual(await self.showing(), "attentive")

    async def test_a_face_that_joins_late_catches_up(self):
        await self.mind.send("mind.ready", cortex=True)
        await self.reach("idle")
        await self.voice.send("sim.body", lid_closed=True)
        await self.reach("asleep")
        await self.start_face(2.0, "asleep")
        self.assertEqual(await self.showing(), "asleep")

    async def test_mute_shows(self):
        await self.mind.send("mind.ready", cortex=True)
        await self.reach("idle")
        await self.start_face(2.5, "muted")
        await asyncio.sleep(1.2)
        await self.voice.send("key.mute")
        self.assertEqual(await self.showing(), "muted")

    async def test_looks_unwell_when_the_brainstem_dies(self):
        await self.mind.send("mind.ready", cortex=True)
        await self.reach("idle")
        await self.start_face(6.0, "orphaned")
        await asyncio.sleep(1.2)
        self.stem.kill()
        self.assertEqual(await self.showing(), "dazed")

    async def test_looks_unwell_with_no_brainstem_at_all(self):
        await self.start_face(4.5, "alone", bus=os.path.join(self.dir.name, "nobody-home.sock"))
        self.assertEqual(await self.showing(), "dazed")


if __name__ == "__main__":
    unittest.main()
