"""Initiated speech through Mind's normal bus, settings, and voice receipt."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from mind.initiative import InitiativePolicy, QuietHours, save_policy
from tests.test_mind import GREET_WITH_TOPIC, MindCase


QUIET_NIGHT = QuietHours("22:00", "07:00")


class InitiativeMind(MindCase):
    async def heartbeat(self, state=None):
        if state is not None:
            self.bus.push("state.changed", state=state)
        self.bus.push("stem.beat")
        await asyncio.sleep(0)

    def report_presence(self, lid_seq):
        self.bus.push("body.presence", present=True, lid_seq=lid_seq)

    async def offer(self, count=1):
        event = await self.bus.wait("mind.announce", count, timeout=1.0)
        return event["announcement"]

    def spoken_since(self, start):
        clauses = self.bus.of("say.request")[start:]
        return " ".join(clause["text"] for clause in clauses if clause["text"])

    async def ask(self, text):
        start = len(self.bus.of("say.request"))
        await self.hear(text, len(self.bus.of("mind.reply_done")) + 1)
        return self.spoken_since(start)

    async def grant(self, token):
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="thinking")
        self.bus.push("stem.announce_ready", announcement=token)
        done = await self.bus.wait("mind.reply_done", replies + 1, timeout=1.0)
        if done["spoke"]:
            self.assertEqual(self.bus.of("say.request")[-1]["announcement"], token)
        return self.spoken_since(start)

    def confirm(self, token):
        self.bus.push("say.done", final=True, announcement=token)
        self.bus.push("state.changed", state="attending")

    async def test_due_reminder_and_timer_wait_through_quiet_hours(self):
        save_policy(self.root, InitiativePolicy(quiet_hours=QUIET_NIGHT))
        self.bus.push("body.lid", closed=False, seq=1)
        await self.ask("Set my time zone to Australia slash Sydney")

        # Start on the evening side of a window that crosses local midnight.
        moment = datetime(2025, 2, 20, 11, 0, tzinfo=timezone.utc)
        with patch("mind.skills.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone: moment.astimezone(zone)
            self.assertIn("Reminder 1 set", await self.ask("Remind me in one minute to water the basil"))
            self.assertIn("Timer 2 started", await self.ask("Set a timer for two minutes"))
            self.assertIn("Reminder 3 set", await self.ask("Remind me in three minutes to water the mint"))
            moment = moment.replace(minute=5)
            await self.heartbeat("idle")
            self.assertEqual(self.bus.of("mind.announce"), [])

            # It is now 2 a.m. the next day in Sydney, and both items are due.
            moment = moment.replace(hour=15, minute=0)
            await self.heartbeat()
            self.assertEqual(self.bus.of("mind.announce"), [])
            self.assertEqual(await self.ask("Cancel reminder three"), "Cancelled reminder 3.")
            self.assertEqual(len(self.mind.skills.reminders.upcoming()), 2)
            self.assertIn("water the basil, overdue", await self.ask("What are my reminders?"))
            self.assertIn("Timer 2, overdue", await self.ask("What are my timers?"))
            self.assertEqual(await self.ask("What time is it?"), "It's 2:00 in the morning.")
            self.assertEqual(await self.ask("Pip, are you there?"), "I'm here. I can hear you.")
            self.assertIn("It's good to hear you", await self.ask(GREET_WITH_TOPIC))
            self.assertEqual(len(self.cortex_server.chats), 1)

            # The end of quiet hours is exclusive. Playback can start at 7 a.m.
            moment = moment.replace(hour=20)
            await self.heartbeat()
            first = await self.offer()
            self.assertIn("water the basil", await self.grant(first))
            self.assertEqual(len(self.mind.skills.reminders.upcoming()), 2)
            self.confirm(first)
            await self.heartbeat()
            second = await self.offer(2)
            self.assertEqual(await self.grant(second), "Timer 2 is up.")
            self.confirm(second)
            self.assertEqual(self.mind.skills.reminders.upcoming(), [])

    async def test_disabled_reminders_can_be_cancelled_and_policy_changes_with_live_mind(self):
        disabled = InitiativePolicy(reminders=False)
        save_policy(self.root, disabled)
        self.bus.push("body.lid", closed=False, seq=1)
        self.mind.skills.reminders.add("Cancel this task", 1)
        kept = self.mind.skills.reminders.add("Keep this task", 2)

        await self.heartbeat("idle")
        self.assertEqual(self.bus.of("mind.announce"), [])
        self.assertEqual(await self.ask("Cancel reminder one"), "Cancelled reminder 1.")
        self.assertEqual(self.mind.skills.reminders.upcoming(), [kept])

        save_policy(self.root, InitiativePolicy())
        await self.heartbeat()
        token = await self.offer()
        save_policy(self.root, disabled)
        self.assertEqual(await self.grant(token), "")
        self.assertEqual(self.mind.skills.reminders.upcoming(), [kept])
        self.assertEqual(await self.ask("What time is it?"), "It's 7:05 in the evening.")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_due_reminder_retries_after_lid_close_and_restart_until_confirmed(self):
        self.bus.push("body.lid", closed=False, seq=1)
        entry = self.mind.skills.reminders.add("Check the oven.", 1)
        await self.heartbeat("idle")
        first = await self.offer()
        self.assertIn(entry.text, await self.grant(first))

        self.bus.push("body.lid", closed=True, seq=2)
        await self.stop_mind()
        await self.start_mind()
        self.bus.push("body.lid", closed=True, seq=2)
        await self.heartbeat("idle")
        self.assertEqual(self.bus.of("mind.announce"), [])
        self.assertEqual(self.mind.skills.reminders.upcoming(), [entry])

        self.bus.push("body.lid", closed=False, seq=3)
        await self.heartbeat()
        retry = await self.offer()
        self.assertNotEqual(retry, first)
        self.assertIn(entry.text, await self.grant(retry))
        self.bus.push("say.done", final=True, announcement=first)
        self.assertEqual(self.mind.skills.reminders.upcoming(), [entry])
        self.confirm(retry)
        self.bus.push("body.lid", closed=True, seq=4)
        await self.stop_mind()
        await self.start_mind()
        self.assertEqual(self.mind.skills.reminders.upcoming(), [])

    async def test_greetings_are_opt_in_and_once_per_local_date_across_restart(self):
        await self.ask("Set my time zone to Australia slash Sydney")
        self.bus.push("body.lid", closed=False, seq=1)
        moment = datetime(2025, 2, 20, 10, 59, tzinfo=timezone.utc)
        with patch("mind.skills.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone: moment.astimezone(zone)
            await self.heartbeat("idle")
            self.report_presence(lid_seq=1)
            await self.heartbeat()
            self.assertEqual(self.bus.of("mind.announce"), [])

            save_policy(self.root, InitiativePolicy(greetings=True, quiet_hours=QUIET_NIGHT))
            moment = moment.replace(hour=11, minute=0)  # Sydney 10 p.m.
            self.report_presence(lid_seq=1)
            await self.heartbeat()
            self.assertEqual(self.bus.of("mind.announce"), [])

            moment = moment.replace(hour=20)  # Sydney 7 a.m. the next day.
            self.mind.skills.reminders.add("Open the curtains.", 1)
            self.report_presence(lid_seq=1)
            await self.heartbeat()
            reminder = await self.offer()
            self.assertIn("Open the curtains.", await self.grant(reminder))
            self.confirm(reminder)

            self.report_presence(lid_seq=1)
            await self.heartbeat()
            self.assertEqual(len(self.bus.of("mind.announce")), 1)
            await self.heartbeat("idle")
            greeting = await self.offer(2)
            # Sydney 7 a.m.: a morning greeting in the owner's zone, not UTC's evening.
            self.assertEqual(await self.grant(greeting), "Morning. I was hoping you'd come by.")
            self.assertEqual(self.bus.of("mind.emotion")[-1]["name"], "joy")
            self.confirm(greeting)
            await self.stop_mind()
            await self.start_mind()

            # UTC has changed dates, but the configured local date is unchanged.
            moment = moment.replace(day=21, hour=10, minute=59)
            self.bus.push("body.lid", closed=False, seq=2)
            await self.heartbeat("idle")
            self.report_presence(lid_seq=2)
            await self.heartbeat()
            self.assertEqual(self.bus.of("mind.announce"), [])

            # Now UTC has the same date but the configured local date has advanced.
            moment = moment.replace(hour=20, minute=0)
            self.report_presence(lid_seq=2)
            await self.heartbeat()
            next_greeting = await self.offer()
            # The next day's morning greeting is the other one.
            self.assertEqual(await self.grant(next_greeting), "Good morning. Good to see you.")
            self.confirm(next_greeting)
        self.assertEqual(self.cortex_server.chats, [])

    async def test_the_first_person_it_sees_after_birth_hears_its_first_words(self):
        save_policy(self.root, InitiativePolicy(greetings=True))
        self.bus.push("life.woke", id="abc", born=1, wakes=1, first=True, lost_power=False, new_body=False)
        self.bus.push("body.lid", closed=False, seq=1)
        await self.heartbeat("idle")
        self.report_presence(lid_seq=1)
        await self.heartbeat()
        token = await self.offer()
        self.assertEqual(await self.grant(token), "Oh. Hello. I'm Pip. You're the very first person I've met.")
        self.assertEqual(self.bus.of("mind.emotion")[-1]["name"], "joy")
        self.confirm(token)
        await asyncio.sleep(0.05)
        self.assertFalse(self.mind._newborn)
        # Without a known time zone, later greetings stay plain.
        self.assertEqual(self.mind._greeting_line(), "[joy] Hello. Good to see you.")

    async def test_greeting_requires_fresh_presence_from_current_open_lid(self):
        save_policy(self.root, InitiativePolicy(greetings=True))
        self.bus.push("body.lid", closed=False, seq=1)
        await self.heartbeat("listening")
        self.report_presence(lid_seq=1)
        await self.heartbeat()
        await asyncio.sleep(1.6)
        await self.heartbeat("idle")
        self.assertEqual(self.bus.of("mind.announce"), [])

        await self.heartbeat("listening")
        self.report_presence(lid_seq=1)
        await self.heartbeat()
        self.bus.push("body.lid", closed=True, seq=2)
        self.bus.push("body.lid", closed=False, seq=3)
        await self.heartbeat("idle")
        self.assertEqual(self.bus.of("mind.announce"), [])
        self.report_presence(lid_seq=1)
        await self.heartbeat()
        self.assertEqual(self.bus.of("mind.announce"), [])

        self.report_presence(lid_seq=3)
        await self.heartbeat()
        token = await self.offer()
        self.assertEqual(await self.grant(token), "Hello. Good to see you.")
        await self.stop_mind()
        await self.start_mind()

        # An unacknowledged greeting did not use today's greeting. The valid
        # open event and first presence sample can arrive in the same bus read.
        self.bus.push("body.lid", closed=False, seq=3)
        self.report_presence(lid_seq=3)
        await self.heartbeat("idle")
        retry = await self.offer()
        self.assertNotEqual(retry, token)
        self.assertEqual(await self.grant(retry), "Hello. Good to see you.")
        self.confirm(retry)
