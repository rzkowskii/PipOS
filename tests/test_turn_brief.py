"""The morning brief on the owner's first words of the day (task L3.6)."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mind.initiative import InitiativePolicy, save_policy
from tests.test_mind import MindCase

SYDNEY = ZoneInfo("Australia/Sydney")
MORNING = datetime(2026, 11, 24, 8, 0, tzinfo=SYDNEY)


class MorningBrief(MindCase):
    cfg = {"morning_brief": True}
    when = MORNING

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mind.skills.zone = SYDNEY
        self.enterContext(patch.object(self.mind.skills, "now", side_effect=lambda: self.when))
        self.reminders = self.mind.skills.reminders

    async def ask(self, text, speaker="Richard"):
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker=speaker)
        await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        await asyncio.sleep(0)
        return " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])

    def remind_at(self, hour, text="call Mum"):
        due = self.when.replace(hour=hour, minute=0)
        return self.reminders.add(text, due.timestamp(), now=self.when.timestamp() - 60)

    async def test_a_greeting_gets_the_brief_once(self):
        self.remind_at(9)
        first = await self.ask("Good morning, Pip")
        self.assertIn("one reminder today", first)
        self.assertIn("call Mum", first)
        self.assertLessEqual(len(first.split()), 35)
        self.assertEqual(await self.ask("Good morning, Pip"), "Hi. Good to hear you.", "once a day")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_with_nothing_to_tell_the_greeting_is_the_ordinary_one(self):
        self.assertEqual(await self.ask("Good morning, Pip"), "Hi. Good to hear you.")
        self.assertTrue(self.reminders.greeted_on(self.when.date()), "the day's brief is spent")

    async def test_a_command_first_gets_its_answer_and_one_clause(self):
        self.remind_at(9)
        self.remind_at(17, "put the bins out")
        reply = await self.ask("what time is it")
        self.assertTrue(reply.startswith("It's 8:00 in the morning."), reply)
        self.assertIn("two reminders today", reply)
        self.assertNotIn("Good morning", reply)
        self.assertEqual(await self.ask("Good morning, Pip"), "Hi. Good to hear you.")

    async def test_the_brief_is_not_copied_into_the_archive(self):
        # A cancelled reminder or a forget must not leave a copy of it behind
        # in what Pip said.
        self.remind_at(9, "collect the Zanzibar parcel")
        self.assertIn("Zanzibar", await self.ask("Good morning, Pip"))
        self.assertFalse(self.memory.contains("Zanzibar"))
        self.when = self.when + timedelta(days=1)
        self.remind_at(10, "ring the Quillfeather bakery")
        self.assertIn("one reminder today", await self.ask("what's the date"))
        self.assertFalse(self.memory.contains("reminder today"))

    async def test_days_away_are_mentioned(self):
        self.memory.log("heard", "see you soon", "Richard")
        with patch.object(self.memory, "now", return_value=self.when.timestamp() + 0):
            with patch.object(self.memory, "last_heard", return_value=self.when.timestamp() - 3 * 86_400):
                reply = await self.ask("Good morning, Pip")
        self.assertIn("You're back. It's been three days.", reply)

    async def test_no_brief_for_guests_or_when_switched_off(self):
        self.remind_at(9)
        await self.ask("start guest mode")
        self.assertNotIn("reminder", await self.ask("Good morning"))
        await self.ask("end guest mode")
        save_policy(self.root, InitiativePolicy(brief=False))
        self.assertNotIn("reminder", await self.ask("Good morning, Pip"))
        self.assertFalse(self.reminders.greeted_on(self.when.date()))


class BeforeTheDayStarts(MorningBrief):
    when = MORNING.replace(hour=3)

    async def test_the_small_hours_are_still_last_night(self):
        self.remind_at(4)
        self.assertNotIn("reminder", await self.ask("Good morning, Pip"))
        self.assertFalse(self.reminders.greeted_on(self.when.date()))
        self.when = self.when + timedelta(hours=3)
        self.remind_at(9)
        self.assertIn("one reminder today", await self.ask("Good morning, Pip"))

    # The inherited cases assume a morning.
    test_a_greeting_gets_the_brief_once = None
    test_with_nothing_to_tell_the_greeting_is_the_ordinary_one = None
    test_a_command_first_gets_its_answer_and_one_clause = None
    test_days_away_are_mentioned = None
    test_no_brief_for_guests_or_when_switched_off = None
    test_the_brief_is_not_copied_into_the_archive = None
