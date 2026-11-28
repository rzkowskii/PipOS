""""Forget everything" by voice: the mind's erase path (PIP-FUNCTIONS 4.1, gate 4)."""

import asyncio
import json
import time
import unittest
from datetime import date
from unittest.mock import patch

from mind import diary
from mind.retention import MemoryPolicy, save_policy
from mind.turn import Announcement, Ring, _spoken_words
from tests.test_mind import MindCase
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


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


class ForgettingReachesReminders(MindCase):
    """Reminders hold the owner's own words: forgetting takes them too."""

    ask = ForgetEverything.ask

    def reminders(self):
        return [entry.text for entry in self.mind.skills.reminders.upcoming()]

    async def test_forget_everything_erases_reminders_and_greeted_days(self):
        reminders = self.mind.skills.reminders
        reminders.add("call the dentist about the crown", time.time() + 3600)
        reminders.add("Timer", time.time() + 600, kind="timer", duration=600)
        reminders.record_greeting(date(2026, 11, 20))
        await self.ask("Forget everything")
        self.assertEqual(await self.ask("yes"), "Okay. I've forgotten everything.")
        self.assertEqual(self.reminders(), [])
        self.assertFalse(reminders.greeted_on(date(2026, 11, 20)), "no record of the days it greeted anyone")

    async def test_a_confirmed_topic_forget_cancels_reminders_holding_those_words(self):
        reminders = self.mind.skills.reminders
        self.memory.log("heard", "I must ring Imogen about the sailing club on Friday", "Richard")
        reminders.add("ring Imogen about the sailing club", time.time() + 3600)
        reminders.add("water the tomatoes", time.time() + 3600)
        self.assertIn("Say yes", await self.ask("Forget about Imogen"))
        self.assertEqual(len(self.reminders()), 2, "nothing goes before the yes")
        self.assertIn("forgotten", await self.ask("yes"))
        self.assertEqual(self.reminders(), ["water the tomatoes"])

    async def test_a_refused_topic_forget_keeps_the_reminders(self):
        self.memory.log("heard", "I must ring Imogen about the sailing club on Friday", "Richard")
        self.mind.skills.reminders.add("ring Imogen about the sailing club", time.time() + 3600)
        await self.ask("Forget about Imogen")
        await self.ask("no")
        self.assertEqual(self.reminders(), ["ring Imogen about the sailing club"])

    async def test_forget_that_cancels_a_reminder_holding_the_fact(self):
        self.mind.skills.reminders.add("check the boiler pressure gauge", time.time() + 3600)
        self.mind.skills.reminders.add("water the tomatoes", time.time() + 3600)
        await self.ask("Remember that I must check the boiler pressure gauge every month")
        self.assertEqual(await self.ask("Forget that"), "Okay. That's forgotten.")
        self.assertEqual(self.reminders(), ["water the tomatoes"])

    async def test_a_correction_keeps_the_reminder(self):
        self.mind.skills.reminders.add("feed my cat Tom", time.time() + 3600)
        await self.ask("Remember that I feed my cat Tom at six")
        self.assertIn("Got it", await self.ask("That's wrong, I feed my cat Tom at seven"))
        self.assertEqual([fact.text for fact in self.memory.facts()], ["I feed my cat Tom at seven"])
        self.assertEqual(self.reminders(), ["feed my cat Tom"])

    async def test_a_ring_still_open_cannot_set_a_forgotten_reminder_again(self):
        # Its last ring finished it, so a "snooze" would set it again from
        # the words the ring still holds.
        entry = self.mind.skills.reminders.add("ring Imogen about the sailing club", time.time() - 1)
        self.mind.skills.reminders.cancel(entry.id)
        self.mind._ring = Ring(entry, time.monotonic() + 600, "Richard")
        self.memory.log("heard", "I must ring Imogen about the sailing club on Friday", "Richard")
        await self.ask("Forget about Imogen")
        self.assertIn("forgotten", await self.ask("yes"))
        self.assertIsNone(self.mind._ring)
        self.assertEqual(await self.ask("snooze"), "Nothing is ringing right now.")
        self.assertEqual(self.reminders(), [])

    async def test_forget_everything_closes_any_ring(self):
        entry = self.mind.skills.reminders.add("feed the goldfish", time.time() - 1)
        self.mind._ring = Ring(entry, time.monotonic() + 600, "Richard")
        await self.ask("Forget everything")
        await self.ask("yes")
        self.assertIsNone(self.mind._ring)
        self.assertEqual(self.reminders(), [])

    async def test_an_offered_ring_of_a_forgotten_reminder_is_never_said(self):
        entry = self.mind.skills.reminders.add("ring Imogen about the sailing club", time.time() - 1)
        self.memory.log("heard", "I must ring Imogen about the sailing club on Friday", "Richard")
        await self.ask("Forget about Imogen")
        # Offered between the question and the yes; the grant comes after.
        self.mind._announcement = Announcement(entry, "tok", time.monotonic() + 60)
        self.assertIn("forgotten", await self.ask("yes"))
        self.assertTrue(self.mind._announcement.forgotten)
        start = len(self.bus.of("say.request"))
        done = len(self.bus.of("mind.reply_done"))
        await self.mind._speak_announcement("tok")
        self.assertEqual(self.bus.of("say.request")[start:], [])
        self.assertIs(self.bus.of("mind.reply_done")[done]["spoke"], False)
        self.assertIsNone(self.mind._announcement)

    async def test_an_offered_ring_of_another_reminder_is_kept(self):
        entry = self.mind.skills.reminders.add("water the tomatoes", time.time() - 1)
        self.memory.log("heard", "I must ring Imogen about the sailing club on Friday", "Richard")
        await self.ask("Forget about Imogen")
        self.mind._announcement = Announcement(entry, "tok", time.monotonic() + 60)
        await self.ask("yes")
        self.assertFalse(self.mind._announcement.forgotten)


class ForgetThatInLiveMode(MindCase):
    """Nothing archived: "forget that" drops the last exchange. Only the
    owner's words in it reach reminders, never Pip's reply."""

    reply = staticmethod(lambda messages: "[joy] You could water the tomatoes before it gets dark.")
    ask = ForgetEverything.ask

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.stop_mind()
        save_policy(self.root, MemoryPolicy(mode="explicit-only"))
        await self.start_mind()

    def reminders(self):
        return [entry.text for entry in self.mind.skills.reminders.upcoming()]

    async def test_a_reply_that_names_a_reminder_does_not_cancel_it(self):
        self.mind.skills.reminders.add("water the tomatoes", time.time() + 3600)
        await self.ask("What should I do in the garden?")
        self.assertEqual(await self.ask("Forget that"), "Okay. That's forgotten.")
        self.assertEqual(self.reminders(), ["water the tomatoes"])

    async def test_the_owners_own_words_do(self):
        self.mind.skills.reminders.add("ring Imogen about the sailing club", time.time() + 3600)
        self.mind.skills.reminders.add("water the tomatoes", time.time() + 3600)
        await self.ask("I must ring Imogen about the sailing club")
        self.assertEqual(await self.ask("Forget that"), "Okay. That's forgotten.")
        self.assertEqual(self.reminders(), ["water the tomatoes"])


class SpokenWords(unittest.TestCase):
    def test_the_notes_round_a_user_message_are_not_the_owners_words(self):
        content = "(19:05, battery 82%, lid open)\nI must ring Imogen\n(you are writing a script)"
        self.assertEqual(_spoken_words(content), "I must ring Imogen")
        self.assertEqual(_spoken_words("plain words"), "plain words")
