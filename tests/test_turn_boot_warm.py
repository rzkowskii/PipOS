"""The first model reply after a boot (cable log, 25 Nov).

From the cortex answering at 34 s to the owner's first model turn at 155 s,
Pip only attended, listened and thought (the follow-up window kept opening),
so the warm-up never ran and the first reply read 262 prompt tokens cold:
11.5 s from the end of speech to audio. The warm-up now also runs while
attending or listening with no turn going, and a model turn still cancels it.

The voice joins the bus after the mind's one mind.ready on a cold boot, so
the mind says it again when the voice comes up.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from mind.__main__ import memory_label
from mind.cortex import Cortex
from mind.diag_system import MARKERS as BOOT_MARKERS
from mind.memory import Memory
from mind.turn import RESTING, WARM_STATES, Config, Mind
from sim.fake_cortex import FakeCortex, default_reply
from tests.test_mind import FakeBus, MindCase


class LoadingCortexCase(MindCase):
    async def asyncSetUp(self):
        self.cortex_server = FakeCortex(delay=0.0, reply=default_reply).start()
        self.cortex_server.healthy = False  # the model is still loading
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        await self.start_mind()

    async def until(self, condition, timeout=3.0):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)


class WarmsWhileAttending(LoadingCortexCase):
    async def test_the_warm_up_runs_when_the_cortex_comes_up_while_pip_is_attending(self):
        self.bus.push("state.changed", state="attending")
        await asyncio.sleep(0.1)
        self.assertIsNone(self.mind._warm_task, "nothing to warm before the model answers")
        self.cortex_server.healthy = True
        await self.until(lambda: self.mind._warm_task is not None)
        await self.mind._warm_task
        self.assertTrue(self.mind._warmed_prefix)
        self.assertFalse(self.mind._needs_warmup)
        self.assertEqual(self.mind.state, "attending", "it never rested")

    async def test_listening_for_long_does_not_hold_it_back_either(self):
        self.cortex_server.healthy = True
        self.bus.push("state.changed", state="listening")
        await self.until(lambda: self.mind._warm_task is not None and self.mind._warm_task.done())
        self.assertTrue(self.mind._warmed_prefix)

    async def test_not_while_thinking_or_speaking_or_during_a_turn(self):
        self.assertNotIn("thinking", WARM_STATES)
        self.assertNotIn("speaking", WARM_STATES)
        self.assertTrue(set(RESTING) <= set(WARM_STATES))
        self.bus.push("state.changed", state="speaking")
        await self.until(lambda: self.mind.state == "speaking")
        self.cortex_server.healthy = True
        await self.until(lambda: self.mind.cortex_ok)
        await asyncio.sleep(0.1)
        self.assertIsNone(self.mind._warm_task, "Piper and the model would share the cores")
        release = asyncio.Event()
        self.mind._turn = turn = asyncio.create_task(release.wait())
        self.bus.push("state.changed", state="attending")
        await asyncio.sleep(0.1)
        self.assertIsNone(self.mind._warm_task, "a turn owns the slot")
        release.set()
        await turn
        self.bus.push("state.changed", state="attending")
        await self.until(lambda: self.mind._warm_task is not None)
        await self.mind._warm_task

    async def test_a_prefill_of_what_is_being_said_keeps_the_slot(self):
        self.bus.push("state.changed", state="speaking")
        await self.until(lambda: self.mind.state == "speaking")
        self.cortex_server.healthy = True
        await self.until(lambda: self.mind.cortex_ok)
        release = asyncio.Event()
        self.mind._prefill_task = prefill = asyncio.create_task(release.wait())
        self.bus.push("state.changed", state="listening")
        await asyncio.sleep(0.1)
        self.assertIsNone(self.mind._warm_task)
        release.set()
        await prefill
        self.bus.push("state.changed", state="listening")
        await self.until(lambda: self.mind._warm_task is not None)
        await self.mind._warm_task


class ReadyAgainForALateVoice(MindCase):
    async def test_a_voice_that_joins_after_mind_ready_hears_it_again(self):
        self.assertEqual(len(self.bus.of("mind.ready")), 1)
        self.bus.push("organ.up", name="voice")
        ready = await self.bus.wait("mind.ready", 2)
        self.assertEqual(ready["cortex"], True)

    async def test_only_for_the_voice(self):
        self.bus.push("organ.up", name="face")
        self.bus.push("organ.up", name="hands")
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.bus.of("mind.ready")), 1)


class NotReadyYet(LoadingCortexCase):
    cfg = {"cortex_wait": 30.0}

    async def start_mind(self):
        # As MindCase's, without waiting for a mind.ready that is 30 s away.
        self.bus = FakeBus()
        self.memory = Memory(self.root)
        cfg = Config(name="Pip", owner="Richard", identity="You are Pip.", settle=0.05, morning_brief=False,
                     **self.cfg)
        self.mind = Mind(self.bus, Cortex(self.cortex_server.url), self.memory, cfg, self.root)
        self.task = asyncio.create_task(self.mind.run())
        await asyncio.sleep(0.1)

    async def test_a_voice_that_joins_before_mind_ready_waits_for_the_first_one(self):
        self.assertEqual(self.bus.of("mind.ready"), [])
        self.bus.push("organ.up", name="voice")
        await asyncio.sleep(0.1)
        self.assertEqual(self.bus.of("mind.ready"), [], "nothing is said early")


class StartLine(unittest.TestCase):
    def test_a_try_boot_does_not_claim_persistent_memory(self):
        # /mind is RAM on a Try stick: "persistent memory" misled the log reader.
        self.assertEqual(memory_label("persistent", True), "memory for this Try session only")
        self.assertEqual(memory_label("persistent", False), "persistent memory")
        self.assertEqual(memory_label("session-only", True), "session-only memory")
        # pip-diag's boot marker still finds the line.
        self.assertRegex(f"mind: Pip | {memory_label('persistent', True)} | cortex at http://127.0.0.1:8080",
                         dict(BOOT_MARKERS)["mind_started"])
