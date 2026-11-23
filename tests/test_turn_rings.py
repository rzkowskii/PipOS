"""Timers, reminders and alarms ring like a person would (task L3.5)."""

import asyncio
import time
from unittest.mock import patch

from mind.reminders import MAX_RINGS, RING_GAP
from tests.test_mind import MindCase


class Rings(MindCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reminders = self.mind.skills.reminders

    async def ring(self, count):
        """Let it offer the due entry, grant the chance to speak, and finish playback."""
        self.bus.push("state.changed", state="idle")
        self.bus.push("stem.beat")
        token = (await self.bus.wait("mind.announce", count))["announcement"]
        start = len(self.bus.of("say.request"))
        self.bus.push("state.changed", state="speaking")
        self.bus.push("stem.announce_ready", announcement=token)
        await self.bus.wait("mind.reply_done", len(self.bus.of("mind.reply_done")) + 1)
        clauses = self.bus.of("say.request")[start:]
        self.bus.push("say.done", final=True, announcement=token)
        self.bus.push("state.changed", state="attending")
        await asyncio.sleep(0)
        return clauses

    async def ask(self, text):
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard")
        await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        await asyncio.sleep(0)
        return " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])

    def spoken(self, clauses):
        return " ".join(c["text"] for c in clauses if c["text"])

    async def test_a_timer_rings_with_a_bell_and_its_owners_name_and_repeats(self):
        entry = self.reminders.add("timer", time.time() - 1, kind="timer", duration=600)
        clauses = await self.ring(1)
        self.assertEqual(clauses[0].get("chirp"), "bell", "the bell comes first")
        self.assertEqual(self.spoken(clauses), "Richard, your ten-minute timer is done.")
        self.assertNotIn("Timer 1", self.spoken(clauses))
        self.assertEqual(self.reminders.delivery(entry.id).rings, 1)
        self.assertIsNone(self.reminders.due(), "the repeat waits for the ring gap")
        # Two minutes later, unanswered: again, up to three times in all.
        for count in range(2, MAX_RINGS + 1):
            later = time.time() + RING_GAP * (count - 1) + 1
            with patch("mind.reminders.time.time", return_value=later):
                clauses = await self.ring(count)
            self.assertTrue(self.spoken(clauses).startswith("Again: Richard, your ten-minute timer"))
        self.assertEqual(self.reminders.upcoming(), [], "after the third ring it is done")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_okay_answers_a_ring_and_stops_the_repeats(self):
        entry = self.reminders.add("call Mum", time.time() - 1)
        clauses = await self.ring(1)
        self.assertIn("call Mum", self.spoken(clauses))
        self.assertEqual(await self.ask("okay thanks"), "Okay.")
        self.assertIsNone(self.reminders.delivery(entry.id), "answered: it is not said again")
        self.assertEqual(self.cortex_server.chats, [])
        # Afterwards "okay" is conversation again.
        await self.ask("okay")
        self.assertEqual(len(self.cortex_server.chats), 1)

    async def test_snooze_moves_it_on(self):
        entry = self.reminders.add("timer", time.time() - 1, kind="timer", duration=300)
        await self.ring(1)
        self.assertEqual(await self.ask("five more minutes"), "Okay. I'll ring again in five minutes.")
        moved = self.reminders.get(entry.id)
        self.assertAlmostEqual(moved.due_at, time.time() + 300, delta=5)
        self.assertEqual(self.reminders.delivery(entry.id).rings, 0)
        self.assertEqual(self.cortex_server.chats, [])

    async def test_snooze_with_nothing_ringing_says_so(self):
        self.assertEqual(await self.ask("snooze"), "Nothing is ringing right now.")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_a_late_ring_says_it_was_late_because_the_lid_was_closed(self):
        entry = self.reminders.add("timer", time.time() - 12 * 60, kind="timer", duration=600)
        self.bus.push("body.lid", closed=True, seq=2)
        await asyncio.sleep(0.02)
        self.mind._lid_closed_at -= 13 * 60  # it was shut before the timer finished
        self.bus.push("body.lid", closed=False, seq=3)
        await asyncio.sleep(0.02)
        clauses = await self.ring(1)
        line = self.spoken(clauses)
        self.assertIn("finished twelve minutes ago while my lid was closed", line)
        self.assertEqual(self.reminders.delivery(entry.id).rings, 1)

    async def test_late_for_another_reason_it_does_not_blame_the_lid(self):
        # Due while the mind was not running (switched off, or restarting):
        # no closure was seen, so none is claimed.
        self.reminders.add("timer", time.time() - 12 * 60, kind="timer", duration=600)
        line = self.spoken(await self.ring(1))
        self.assertIn("finished twelve minutes ago", line)
        self.assertNotIn("lid", line)


class WhatMayRing(MindCase):
    """Quiet hours, a doubted clock and sleep, as the owner would expect them."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reminders = self.mind.skills.reminders

    ring = Rings.ring
    spoken = Rings.spoken

    def quiet_all_day(self):
        from mind.initiative import InitiativePolicy, QuietHours, save_policy

        save_policy(self.root, InitiativePolicy(quiet_hours=QuietHours("00:00", "23:59")))

    async def offered(self):
        self.bus.push("state.changed", state="idle")
        self.bus.push("stem.beat")
        await asyncio.sleep(0.05)
        return self.bus.of("mind.announce")

    async def test_an_alarm_rings_in_quiet_hours_and_a_timer_waits(self):
        self.quiet_all_day()
        self.reminders.add("timer", time.time() - 1, kind="timer", duration=60)
        self.assertEqual(await self.offered(), [], "a timer waits for quiet hours to end")
        self.reminders.add("wake up", time.time() - 1, kind="alarm")
        clauses = await self.ring(1)
        self.assertTrue(self.spoken(clauses), "the alarm is said")
        self.assertNotIn("timer", self.spoken(clauses))

    async def test_an_alarm_is_not_stuck_behind_a_reminder_held_for_quiet_hours(self):
        self.quiet_all_day()
        self.reminders.add("put the bins out", time.time() - 3600)
        alarm = self.reminders.add("wake up", time.time() - 1, kind="alarm")
        await self.ring(1)
        self.assertEqual(self.reminders.delivery(alarm.id).rings, 1, "the alarm rang")

    async def test_a_timer_still_rings_while_the_clock_is_doubted(self):
        entry = self.reminders.add("timer", time.time() - 1, kind="timer", duration=300)
        reminder = self.reminders.add("call Mum", time.time() - 1)
        with patch.object(self.mind, "_clock_doubt", return_value="reset"):
            clauses = await self.ring(1)
            self.assertIn("timer", self.spoken(clauses))
            self.assertEqual(self.reminders.delivery(reminder.id).rings, 0, "a wall-clock reminder is held")
        self.assertEqual(self.reminders.delivery(entry.id).rings, 1)

    async def test_asleep_with_the_lid_open_a_due_timer_wakes_it_once(self):
        self.reminders.add("timer", time.time() - 1, kind="timer", duration=300)
        self.bus.push("state.changed", state="asleep")
        for _ in range(3):
            self.bus.push("stem.beat")
            await asyncio.sleep(0.02)
            self.mind._announcement_retry = 0.0
        self.assertEqual(len(self.bus.of("being.wake")), 1)
        self.assertEqual(self.bus.of("mind.announce"), [], "it rings once awake, not asleep")
