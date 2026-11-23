"""Exercise the overnight worker through the normal Mind event and command paths."""

import asyncio
from datetime import datetime
from unittest.mock import patch

from mind.dream_store import journal
from tests import test_mind


class DreamLifecycle(test_mind.MindCase):
    async def asyncSetUp(self):
        self.enterContext(patch("mind.turn.DREAM_INTERVAL", 0))
        self.enterContext(patch("mind.turn.DREAM_STAGE_BUDGET", 2))
        await super().asyncSetUp()
        self.consent = self.root / "dreams-enabled"

    def enter_sleep(self, **changes):
        self.mind.skills.now = lambda: datetime(2026, 11, 21, 2, 30)
        self.bus.push("body.lid", closed=True, seq=2)
        self.bus.push("state.changed", state="asleep")
        reading = {"hour": 2, "minute": 30, "temp": 48.0, "on_ac": True, "lid_closed": True}
        self.bus.push("body.state", **(reading | changes))
        self.bus.push("stem.beat", state="asleep")

    def open_lid(self):
        self.bus.push("body.lid", closed=False, seq=3)
        self.bus.push("state.changed", state="waking")

    def subject(self):
        """What "forget that" would act on, as (kind, key)."""
        subject = self.mind._last_memory
        return None if subject is None else (subject.kind, subject.key)

    def entries(self):
        return journal(self.memory.db, "Richard")

    async def overnight_pass(self):
        previous = self.mind._dream_task
        self.enter_sleep()
        async with asyncio.timeout(2):
            while self.mind._dream_task is previous:
                await asyncio.sleep(0.005)
            await self.mind._dream_task

    async def test_only_opted_in_eligible_sleep_runs_the_offline_worker(self):
        source = self.memory.log("heard", "The orchids are in the sunroom.", "Richard")
        self.memory.log("heard", "The kitchen fan rattles at night.", "Visitor")
        self.memory.log("said", "The coffee machine needs another filter.", "Pip")
        self.enter_sleep()
        await asyncio.sleep(0)
        self.assertIsNone(self.mind._dream_task)

        self.consent.touch()
        for reading in ({"on_ac": None}, {"on_ac": False}, {"temp": None}, {"temp": 72.0}):
            with self.subTest(reading=reading):
                self.enter_sleep(**reading)
                await asyncio.sleep(0)
                self.assertIsNone(self.mind._dream_task)

        await self.overnight_pass()
        self.assertEqual([entry.source.id for entry in self.entries()], [source])
        self.assertEqual(self.bus.of("mind.dream_committed"), [{"t": "mind.dream_committed", "lid_seq": 2}])
        self.assertEqual(self.cortex_server.chats, [])
        self.assertEqual(self.bus.of("say.request"), [])

    async def test_wake_interrupts_a_staged_batch_and_restart_resumes(self):
        source = self.memory.log("heard", "The blue lantern is on the garden bench.", "Richard")
        self.consent.touch()
        staged = asyncio.Event()
        original_stage = self.mind._dreams.stage

        def stage(*args, **kwargs):
            batch = original_stage(*args, **kwargs)
            staged.set()
            return batch

        with patch.object(self.mind._dreams, "stage", side_effect=stage):
            self.enter_sleep()
            await asyncio.wait_for(staged.wait(), 2)
            task = self.mind._dream_task
            self.open_lid()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.bus.of("mind.dream_committed"), [])

        await self.stop_mind()
        await self.start_mind()
        await self.overnight_pass()
        self.assertEqual([entry.source.id for entry in self.entries()], [source])

    async def test_power_loss_cancels_publication_until_a_fresh_reading(self):
        source = self.memory.log("heard", "The knitting needles are in the striped bag.", "Richard")
        self.consent.touch()
        staged = asyncio.Event()
        original_stage = self.mind._dreams.stage

        def stage(*args, **kwargs):
            batch = original_stage(*args, **kwargs)
            staged.set()
            return batch

        with patch.object(self.mind._dreams, "stage", side_effect=stage):
            self.enter_sleep()
            await asyncio.wait_for(staged.wait(), 2)
            task = self.mind._dream_task
            self.bus.push("body.state", hour=2, minute=30, temp=48.0, on_ac=False, lid_closed=True)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.bus.of("mind.dream_committed"), [])

        await self.overnight_pass()
        self.assertEqual([entry.source.id for entry in self.entries()], [source])

    async def test_consent_is_rechecked_after_staging(self):
        self.memory.log("heard", "The camping lantern takes three batteries.", "Richard")
        self.consent.touch()
        original_stage = self.mind._dreams.stage

        def stage(*args, **kwargs):
            batch = original_stage(*args, **kwargs)
            self.consent.unlink()
            return batch

        with patch.object(self.mind._dreams, "stage", side_effect=stage):
            await self.overnight_pass()
        self.assertEqual(self.entries(), [])

    async def test_spoken_recall_prefers_a_complete_journal_source_only_with_consent(self):
        archive = self.memory.log(
            "heard", "The amber trail map is on the cedar shelf." +
            " There was a humming refrigerator near the entrance." * 30, "Richard",
        )
        partial = self.memory.log("heard", "Amber labels amber stickers amber sheets.", "Richard")
        for number in range(8):
            self.memory.log("heard", f"The trail signpost is number {number}.", "Visitor")

        request = "What did I mention about the amber trail?"
        await self.hear(request, 1)
        self.assertEqual(self.subject(), ("episode", partial))

        self.consent.touch()
        await self.overnight_pass()
        self.open_lid()
        self.bus.sent.clear()
        await self.hear(request, 1)
        self.assertEqual(self.subject(), ("episode", archive))
        self.assertIn("cedar shelf", test_mind.said(self.bus))

        self.consent.unlink()
        await self.hear(request, 2)
        self.assertEqual(self.subject(), ("episode", partial))
        self.assertEqual(self.bus.of("mind.dream_committed"), [])

    async def test_empty_journal_and_rejected_commit_do_not_signal_a_dream(self):
        self.consent.touch()
        self.memory.log("heard", "The kitchen fan rattles at night.", "Visitor")
        await self.overnight_pass()
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.bus.of("mind.dream_committed"), [])

        self.memory.log("heard", "The lantern is on the porch.", "Richard")
        with patch.object(self.mind._dreams, "publish", return_value=False):
            await self.overnight_pass()
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.bus.of("mind.dream_committed"), [])

    async def test_correction_and_forget_hide_derived_entries_through_spoken_commands(self):
        self.consent.touch()
        await self.hear("Remember that the orchids are in the sunroom.", 1)
        await self.overnight_pass()
        self.assertEqual(len(self.entries()), 1)
        original = self.entries()[0].source
        self.assertIn("sunroom", original.text)

        self.open_lid()
        await self.hear("What do you remember about the orchids?", 2)
        await self.hear("That's wrong, the orchids are on the kitchen sill.", 3)
        self.assertEqual(self.entries(), [])
        await self.overnight_pass()
        self.assertEqual([entry.source.id for entry in self.entries()], [original.id])
        self.assertIn("kitchen sill", self.entries()[0].source.text)
        self.assertFalse(self.memory.contains("sunroom"))

        self.open_lid()
        await self.hear("Forget about the orchids.", 4)
        await self.hear("Yes", 5)
        self.assertEqual(self.entries(), [])
        await self.overnight_pass()
        self.assertEqual(self.entries(), [])
        self.assertFalse(self.memory.contains("orchids"))
