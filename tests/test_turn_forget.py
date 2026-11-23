""""Forget everything" by voice: the mind's erase path (PIP-FUNCTIONS 4.1, gate 4)."""

import asyncio
import json
from datetime import date
from unittest.mock import patch

from mind import diary
from tests.test_mind import MindCase


class ForgetEverything(MindCase):
    async def ask(self, text, speaker="Richard"):
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker=speaker)
        await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        return " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])

    async def seed(self):
        await self.ask("Remember that my locker code is zebra-7741")
        await self.ask("I walked the dog along the canal")
        await self.ask("Add milk to my shopping list")
        await self.ask("Make a note: the plumber comes Thursday")

    async def test_it_asks_first_and_a_no_keeps_everything(self):
        await self.seed()
        self.assertIn("Say yes", await self.ask("Forget everything"))
        self.assertEqual(await self.ask("no"), "Okay. I've kept everything.")
        self.assertTrue(self.memory.contains("zebra-7741"))
        self.assertIn("milk", await self.ask("What's on my shopping list?"))

    async def test_yes_erases_memories_lists_notes_and_the_action_log(self):
        await self.seed()
        await self.ask("Forget everything")
        self.assertEqual(await self.ask("yes"), "Okay. I've forgotten everything.")
        self.assertEqual(self.memory.facts(), [])
        for needle in ("zebra-7741", "canal", "milk", "plumber"):
            self.assertFalse(self.memory.contains(needle), needle)
        self.assertFalse((self.root / "lists.db").exists(), "lists and notes are gone, file and all")
        self.assertNotIn("milk", json.dumps(self.mind.messages))
        log = [json.loads(line)["kind"] for line in (self.root / "log" / "actions.jsonl").read_text().splitlines()]
        self.assertEqual(log, ["forget.all"], "only the erase itself is left in the log")
        self.assertIn(("action=erase", "being.slot"), self.cortex_server.slot_calls)

    async def test_only_the_owner_and_never_a_guest(self):
        await self.seed()
        self.assertIn("Only the person I live with", await self.ask("Forget everything", speaker="Visitor"))
        await self.ask("start guest mode")
        self.assertIn("guest session", await self.ask("Forget everything"))
        await self.ask("end guest mode")
        self.assertTrue(self.memory.contains("zebra-7741"))


class ForgetEverythingAtScale(MindCase):
    """Years of memories go in batches, with the event loop free between them,
    and the stored day records go too."""

    ask = ForgetEverything.ask

    async def test_every_batch_is_erased_and_the_loop_keeps_running(self):
        for number in range(9):
            self.memory.log("heard", f"the heron came back to pond number {number} again", "Richard")
        diary.build_day(self.memory, "Richard", date(2026, 11, 20))
        ticks = []

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0)

        task = asyncio.create_task(ticker())
        try:
            with patch("mind.turn.ERASE_BATCH", 2):
                await self.ask("Forget everything")
                before = len(ticks)
                self.assertEqual(await self.ask("yes"), "Okay. I've forgotten everything.")
            self.assertGreater(len(ticks) - before, 4, "the loop ran between batches")
        finally:
            task.cancel()
        self.assertFalse(self.memory.contains("heron"))
        self.assertEqual(self.memory.db.execute("SELECT count(*) FROM diary_days").fetchone()[0], 0,
                         "no day record counts conversations that are no longer remembered")
