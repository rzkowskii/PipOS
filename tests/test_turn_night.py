"""The night shift's hooks in the mind (task L3.9)."""

import asyncio
from datetime import datetime
from unittest.mock import patch

from mind.care import set_dreams
from mind.cortex import Cortex
from mind.dream_store import journal
from mind.night_shift import Gate, NightShift
from mind.turn import Config, Mind, RewarmJob
from tests.test_mind import FakeBus, MindCase
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


class FakeShift:
    """NightShift's interface: register, step(eligible, night=), interrupt, done_tonight."""

    def __init__(self):
        self.jobs = {}
        self.steps = []
        self.interrupts = 0
        self.started = asyncio.Event()
        self.hold = None
        self.done = []

    def register(self, name, job):
        self.jobs[name] = job

    async def step(self, eligible, *, night=None):
        self.steps.append((eligible(), night))
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
            return {"journal": "interrupted"}
        journal = self.jobs.get("journal")
        if journal is not None:
            journal.published += 1
        self.done = list(self.jobs)
        return {name: "done" for name in self.jobs}

    def done_tonight(self):
        return tuple(self.done)

    def interrupt(self):
        self.interrupts += 1
        if self.hold is not None:
            self.hold.set()


class NightHooks(MindCase):
    async def asyncSetUp(self):
        self.enterContext(patch("mind.turn.DREAM_INTERVAL", 0))
        await super().asyncSetUp()
        self.shift = FakeShift()
        self.mind.night_shift = self.shift
        (self.root / "dreams-enabled").touch()

    def enter_sleep(self, hour=2):
        self.mind.skills.now = lambda: datetime(2026, 11, 21, hour, 30)
        self.bus.push("body.lid", closed=True, seq=2)
        self.bus.push("state.changed", state="asleep")
        self.bus.push("body.state", hour=hour, minute=30, temp=48.0, on_ac=True, lid_closed=True)
        self.bus.push("stem.beat", state="asleep")

    async def test_the_mind_registers_its_morning_rewarm(self):
        shift = FakeShift()
        mind = Mind(FakeBus(), Cortex(self.cortex_server.url), self.memory,
                    Config(name="Pip", owner="Richard", identity="You are Pip."), self.root, night_shift=shift)
        self.assertIsInstance(shift.jobs.get("rewarm"), RewarmJob)
        mind.skills.close()

    async def test_an_eligible_night_steps_the_shift_and_reports_a_committed_dream(self):
        self.enter_sleep()
        await asyncio.wait_for(self.shift.started.wait(), 2)
        await self.bus.wait("mind.dream_committed")
        self.assertEqual(self.shift.steps, [(True, "2026-11-21")])
        # Tonight's own jobs were built for this night, with the mind's re-warm.
        self.assertTrue({"checkpoint", "search", "vectors", "journal", "diary"} <= set(self.shift.jobs))
        self.assertEqual(self.bus.of("mind.dream_committed")[0]["lid_seq"], 2)
        self.bus.push("stem.beat", state="asleep")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.shift.steps), 1, "done for tonight: no more steps")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_daytime_dreaming_follows_live_care_window(self):
        self.enter_sleep(hour=14)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.shift.started.wait(), 0.1)

        self.shift.hold = asyncio.Event()
        set_dreams(self.root, True, "closed")
        self.bus.push("stem.beat", state="asleep")
        await asyncio.wait_for(self.shift.started.wait(), 2)
        self.assertEqual(self.shift.steps, [(True, "2026-11-21")])

        set_dreams(self.root, True, "night")
        self.bus.push("body.state", hour=14, minute=30, temp=48.0, on_ac=True, lid_closed=True)
        await asyncio.wait_for(self.mind._dream_task, 2)
        self.assertGreaterEqual(self.shift.interrupts, 1)
        self.assertEqual([event["active"] for event in self.bus.of("mind.dreaming")], [True, False])

    async def test_the_night_waits_while_the_embedding_pack_is_first_checked(self):
        # Started in the night: the vectors job must not find no embedder
        # and count itself done before the pack's check has answered.
        self.mind._embedder_ok = None
        self.enter_sleep()
        await asyncio.sleep(0.1)
        self.assertEqual(self.shift.steps, [])
        self.mind._embedder_ok = True
        self.bus.push("stem.beat", state="asleep")
        await asyncio.wait_for(self.shift.started.wait(), 2)
        self.assertEqual(self.shift.steps, [(True, "2026-11-21")])

    async def test_night_work_waits_for_an_archive_opened_in_the_confirmed_format(self):
        self.memory.format_ready = False
        self.enter_sleep()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.shift.started.wait(), 0.1)

        # Model the archive reopened after confirmation.
        self.memory.format_ready = True
        self.bus.push("stem.beat", state="asleep")
        await asyncio.wait_for(self.shift.started.wait(), 2)
        self.assertEqual(self.shift.steps, [(True, "2026-11-21")])

    async def test_opening_the_lid_interrupts_it(self):
        self.shift.hold = asyncio.Event()
        self.enter_sleep()
        await asyncio.wait_for(self.shift.started.wait(), 2)
        self.bus.push("body.lid", closed=False, seq=3)
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(self.shift.interrupts, 1)
        self.assertTrue(self.mind._dream_task.done())

    async def test_night_work_is_bracketed_by_mind_dreaming(self):
        self.shift.hold = asyncio.Event()
        self.enter_sleep()
        await asyncio.wait_for(self.shift.started.wait(), 2)
        self.assertEqual(self.bus.of("mind.dreaming"), [{"t": "mind.dreaming", "active": True}],
                         "bodycare hears it before the work, so it can say why it holds the package")
        self.bus.push("body.lid", closed=False, seq=3)
        async with asyncio.timeout(2):
            while not self.mind._dream_task.done():
                await asyncio.sleep(0.01)
        self.assertEqual([event["active"] for event in self.bus.of("mind.dreaming")], [True, False],
                         "an interrupted night still says it stopped")

    async def test_a_finished_step_says_it_stopped_dreaming(self):
        self.enter_sleep()
        await self.bus.wait("mind.dream_committed")
        async with asyncio.timeout(2):
            while not self.mind._dream_task.done():
                await asyncio.sleep(0.01)
        self.assertEqual([event["active"] for event in self.bus.of("mind.dreaming")], [True, False])

    async def test_no_dreaming_is_claimed_on_a_night_that_is_not_eligible(self):
        (self.root / "dreams-enabled").unlink()
        self.mind.skills.now = lambda: datetime(2026, 11, 21, 2, 30)
        self.bus.push("body.lid", closed=True, seq=2)
        self.bus.push("state.changed", state="asleep")
        # On battery: no night work at all, upkeep included.
        self.bus.push("body.state", hour=2, minute=30, temp=48.0, on_ac=False, lid_closed=True)
        self.bus.push("stem.beat", state="asleep")
        await asyncio.sleep(0.1)
        self.assertEqual(self.bus.of("mind.dreaming"), [])
        self.assertEqual(getattr(self.shift, "steps", []), [])

    async def test_without_dream_consent_the_upkeep_still_runs_and_is_not_called_dreaming(self):
        (self.root / "dreams-enabled").unlink()
        self.enter_sleep()
        await asyncio.wait_for(self.shift.started.wait(), 2)
        (gate, night), = self.shift.steps
        self.assertEqual(night, "2026-11-21")
        self.assertIsInstance(gate, Gate)
        for upkeep in ("checkpoint", "expiry", "search", "vectors", "diary", "backup", "rehash", "rewarm"):
            self.assertTrue(gate.allows(upkeep), upkeep)
        self.assertFalse(gate.allows("journal"))
        self.assertFalse(gate.allows("dream"))
        async with asyncio.timeout(2):
            while not self.mind._dream_task.done():
                await asyncio.sleep(0.01)
        self.assertEqual(self.bus.of("mind.dreaming"), [], "upkeep is not a dream")
        # Done for tonight without consent; the owner says yes, and the dreams follow.
        self.bus.push("stem.beat", state="asleep")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.shift.steps), 1)
        (self.root / "dreams-enabled").touch()
        self.shift.started.clear()
        self.bus.push("stem.beat", state="asleep")
        await asyncio.wait_for(self.shift.started.wait(), 2)
        self.assertEqual(self.shift.steps[-1], (True, "2026-11-21"))
        await self.bus.wait("mind.dreaming")

    async def test_the_rewarm_job_reads_the_whole_prompt(self):
        await self.warmed()
        self.mind.messages = [{"role": "user", "content": "x " * 300}, {"role": "assistant", "content": "[joy] ok"}]
        before = len(self.cortex_server.prefills)
        self.assertTrue(await RewarmJob(self.mind).run_chunk(5.0))
        primes = [p["prompt"] for p in self.cortex_server.prefills[before:]]
        self.assertIn("[joy] ok", primes[-1])
        self.assertFalse(self.mind._needs_warmup)

    async def test_the_rewarm_job_has_nothing_to_do_without_a_model(self):
        self.mind.cortex_ok = False
        self.assertTrue(await RewarmJob(self.mind).run_chunk(1.0))


class RealNightShift(NightHooks):
    """The mind driving the real night shift, end to end on the host."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.shift = NightShift(self.root)
        self.shift.register("rewarm", RewarmJob(self.mind))
        self.mind.night_shift = self.shift

    async def test_a_night_writes_the_journal_and_reports_it(self):
        source = self.memory.log("heard", "The orchids are in the sunroom.", "Richard")
        self.enter_sleep()
        await self.bus.wait("mind.dream_committed")
        self.assertEqual([entry.source.id for entry in journal(self.memory.db, "Richard")], [source])
        async with asyncio.timeout(2):
            while self.mind._dream_task is not None and not self.mind._dream_task.done():
                await asyncio.sleep(0.01)
        self.assertIn("journal", self.shift.done_tonight())
        self.assertEqual(self.cortex_server.chats, [])

    async def test_without_consent_the_night_tidies_memory_but_writes_no_journal(self):
        (self.root / "dreams-enabled").unlink()
        self.memory.log("heard", "The orchids are in the sunroom.", "Richard")
        self.enter_sleep()
        async with asyncio.timeout(3):
            while "rewarm" not in self.shift.done_tonight():
                await asyncio.sleep(0.01)
        done = set(self.shift.done_tonight())
        self.assertTrue({"checkpoint", "search", "vectors", "rewarm"} <= done, done)
        self.assertNotIn("journal", done)
        self.assertEqual(journal(self.memory.db, "Richard"), [], "no dream without the owner's yes")
        self.assertEqual(self.bus.of("mind.dreaming"), [])
        self.assertEqual(self.bus.of("mind.dream_committed"), [])

    # The fake-shift cases do not apply to the real one.
    test_night_work_waits_for_an_archive_opened_in_the_confirmed_format = None
    test_without_dream_consent_the_upkeep_still_runs_and_is_not_called_dreaming = None
    test_an_eligible_night_steps_the_shift_and_reports_a_committed_dream = None
    test_daytime_dreaming_follows_live_care_window = None
    test_opening_the_lid_interrupts_it = None
    test_night_work_is_bracketed_by_mind_dreaming = None
    test_a_finished_step_says_it_stopped_dreaming = None
    test_the_night_waits_while_the_embedding_pack_is_first_checked = None


class StartUp(MindCase):
    async def test_main_builds_the_night_shift_with_the_other_organs_jobs(self):
        from mind.__main__ import build_night_shift

        shift = build_night_shift(self.root, self.memory, self.mind.cfg)
        self.assertIsInstance(shift, NightShift)
        self.assertTrue({"backup", "rehash"} <= set(shift.jobs))
        self.assertEqual(shift.state_dir, self.root)
