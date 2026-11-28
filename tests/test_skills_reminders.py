"""Spoken reminder wording, time zones and the factory zone."""

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mind import skills
from mind.commands import parse
from mind.initiative import InitiativePolicy, QuietHours, save_policy
from mind.reminder_language import ReminderRequest


class Wording(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "skills.json").write_text(json.dumps({"time_zone": "Australia/Sydney"}))
        self.skills = skills.Skills(self.root)
        self.addCleanup(self.skills.close)

    def test_numbers_and_timer_names_read_like_speech(self):
        self.assertEqual(skills.number_words(30), "thirty")
        self.assertEqual(skills.number_words(45), "forty-five")
        self.assertEqual(skills.number_words(120), "one hundred and twenty")
        self.assertEqual(skills.timer_name(1800), "thirty-minute timer")
        self.assertEqual(skills.timer_name(7200), "two-hour timer")
        self.assertEqual(skills.timer_name(4200), "timer for one hour and ten minutes")
        self.assertEqual(skills.timer_name(None), "timer")

    def test_on_time_and_late_deliveries_are_worded_honestly_without_ids(self):
        timer = self.skills.reminders.add("Timer", 1000, kind="timer", duration=1800)
        line = self.skills.announcement(timer, "Richard", now=1005)
        self.assertEqual(line, "Richard, your thirty-minute timer is done.")
        late = self.skills.announcement(timer, "Richard", now=1000 + 12 * 60, slept=True)
        self.assertEqual(late, "Richard, your thirty-minute timer finished twelve minutes ago while my lid was "
                               "closed. Sorry I'm late.")
        reminder = self.skills.reminders.add("call Mum", 1000)
        self.assertEqual(self.skills.announcement(reminder, "Richard", now=1000),
                         "Richard, you asked me to remind you: call Mum.")
        self.assertEqual(self.skills.announcement(reminder, "Richard", now=1000 + 3700),
                         "Richard, sorry, this reminder is an hour late: call Mum.")
        self.assertEqual(self.skills.announcement(reminder, "Richard", now=1000 + 3700, slept=True),
                         "Richard, sorry, this reminder is an hour late because my lid was closed: call Mum.")
        self.assertTrue(self.skills.announcement(reminder, "Richard", now=1000, rings=1).startswith("Again: "))
        for text in (line, late):
            self.assertNotRegex(text, r"\d")

    def test_creation_says_when_it_will_not_be_spoken(self):
        due = 1_800_000_000.0
        request = ReminderRequest("create", "call Mum", due)
        self.assertNotIn("switched off", self.skills._reminder_reply(request))
        save_policy(self.root, InitiativePolicy(reminders=False))
        self.assertIn("switched off", self.skills._reminder_reply(request))
        hour = skills.datetime.fromtimestamp(due, self.skills.zone).hour
        save_policy(self.root, InitiativePolicy(quiet_hours=QuietHours(f"{hour:02d}:00", f"{(hour + 1) % 24:02d}:00")))
        self.assertIn("quiet hours", self.skills._reminder_reply(request))


class Rings(unittest.TestCase):
    """L5.5: every ring is at most 20 words and honest about lateness."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "skills.json").write_text(json.dumps({"time_zone": "Australia/Sydney"}))
        self.skills = skills.Skills(self.root)
        self.addCleanup(self.skills.close)
        self.zone = ZoneInfo("Australia/Sydney")

    def test_every_ring_fits_in_twenty_words(self):
        long_task = ("take my tablets and then walk the dog around the big park near the river before lunch "
                     "with my sister and her two children")
        entries = [
            self.skills.reminders.add("Timer", 1000, kind="timer", duration=4200),
            self.skills.reminders.add(long_task, 1000),
            self.skills.reminders.add("call Mum", 1000),
            self.skills.reminders.add("Alarm", datetime(2026, 11, 25, 7, tzinfo=self.zone).timestamp(), kind="alarm"),
            self.skills.reminders.add("Alarm", datetime(2026, 11, 25, 19, tzinfo=self.zone).timestamp(), kind="alarm"),
        ]
        for entry in entries:
            for late, slept, rings in ((0, False, 0), (720, True, 0), (3700, False, 2), (3 * 86400, True, 1)):
                with self.subTest(entry=entry.text[:10], late=late, slept=slept, rings=rings):
                    line = self.skills.announcement(entry, "Richard", now=entry.due_at + late, slept=slept, rings=rings)
                    self.assertLessEqual(len(line.split()), skills.ANNOUNCE_WORDS, line)
                    self.assertFalse(line.rstrip(".").endswith((" the", " and", " near")), line)
                    if late > skills.LATE_AFTER:
                        self.assertRegex(line, r"late|ago", "a late ring says so")
                    if slept and late > skills.LATE_AFTER and entry.text != long_task:
                        self.assertIn("lid was closed", line)

    def test_long_tasks_lose_the_preamble_before_any_of_the_task(self):
        task = "pick up the blue folder from the post office counter before it closes at five"
        entry = self.skills.reminders.add(task, 1000)
        line = self.skills.announcement(entry, "Richard", now=1000)
        self.assertEqual(line, "Richard, a reminder: pick up the blue folder from the post office counter before it "
                               "closes at five.")

    def test_alarm_lines(self):
        due = datetime(2026, 11, 25, 7, tzinfo=self.zone).timestamp()
        alarm = self.skills.reminders.add("Alarm", due, kind="alarm")
        self.assertEqual(self.skills.announcement(alarm, "Richard", now=due),
                         "Good morning, Richard. It's 7:00 in the morning. Time to get up.")
        self.assertEqual(self.skills.announcement(alarm, "Richard", now=due + 720, slept=True),
                         "Richard, sorry, your alarm for 7:00 in the morning is twelve minutes late, "
                         "because my lid was closed.")
        evening = self.skills.reminders.add("Alarm", due + 12 * 3600, kind="alarm")
        self.assertEqual(self.skills.announcement(evening, "", now=due + 12 * 3600),
                         "It's 7:00 in the evening. This is your alarm.")

    def test_repeating_reminders_read_the_task_back_to_its_owner(self):
        entry = self.skills.reminders.add("take my tablets", 1000, rule="weekdays", at_local="07:00", zone=self.zone)
        self.assertEqual(self.skills.announcement(entry, "Richard", now=1000),
                         "Richard, you asked me to remind you: take your tablets.")


class Replies(unittest.IsolatedAsyncioTestCase):
    """L5.1 to L5.4: creation, how long left, snooze, acknowledgement and the time elsewhere."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "skills.json").write_text(json.dumps({"time_zone": "Australia/Sydney"}))
        self.skills = skills.Skills(self.root)
        self.addCleanup(self.skills.close)
        self.zone = ZoneInfo("Australia/Sydney")

    async def say(self, text, **kwargs):
        command = parse(text, "Pip", now=self.skills.now())
        self.assertIsNotNone(command, text)
        return await self.skills.reply(command, **kwargs)

    async def test_a_lone_timer_has_no_number_and_a_second_one_does(self):
        self.assertEqual(await self.say("Set a timer for ten minutes", clock_trusted=True),
                         "Okay, ten minutes, starting now.")
        second = await self.say("Start a five minute timer", clock_trusted=True)
        self.assertRegex(second, r"^Okay, five minutes, starting now\. That's timer \d+; you have two running\.$")

    async def test_listing_timers_counts_down_and_reads_tasks_back(self):
        now = time.time()
        timer = self.skills.reminders.add("Timer", now + 300.4, kind="timer", duration=600)
        self.assertEqual(await self.say("What timers do I have?"), f"Timer {timer.id}, five minutes left.")
        reminder = self.skills.reminders.add("take my tablets", now + 7200)
        self.assertIn(f"Reminder {reminder.id}, take your tablets,", await self.say("What are my reminders?"))

    async def test_how_long_left_on_timers(self):
        self.assertEqual(await self.say("How long left on my timer?"), "You don't have a timer running.")
        now = time.time()
        self.skills.reminders.add("Timer", now + 263.4, kind="timer", duration=600)
        self.assertEqual(await self.say("How long left on my timer?"), "Four minutes and twenty-three seconds left.")
        self.skills.reminders.add("Timer", now + 45 * 60, kind="timer", duration=3600)
        both = await self.say("How much time is left on the timer?")
        self.assertEqual(both, "Your ten-minute timer has four minutes and twenty-three seconds left; "
                               "your one-hour timer has about forty-five minutes left.")

    async def test_how_long_until_reminders_and_alarms(self):
        self.assertEqual(await self.say("How long until my reminder?"), "You don't have any reminders set.")
        self.assertEqual(await self.say("When is my alarm?"), "You don't have any alarms set.")
        due = time.time() + 2 * 3600 + 60
        self.skills.reminders.add("call my sister", due)
        line = await self.say("How long until my next reminder?")
        self.assertTrue(line.startswith("Your next reminder is "), line)
        self.assertTrue(line.endswith(", in about two hours and one minute: call your sister."), line)

    async def test_alarms_are_created_held_and_listed(self):
        line = await self.say("Wake me up at 7 am", clock_trusted=True)
        self.assertRegex(line, r"^Okay, your alarm is set for (today|tomorrow) at 7:00 in the morning\. "
                               r"Leave my lid open so I can ring\.$")
        held = await self.say("Set an alarm for 6:30 tomorrow", clock_trusted=False)
        self.assertIn("That's alarm", held)
        self.assertIn("hold it until you tell me the time", held)
        listed = await self.say("What are my alarms?")
        self.assertEqual(listed.count("Alarm "), 2)
        weekly = await self.say("Set an alarm for 7 every weekday", clock_trusted=True)
        self.assertIn("will ring every weekday at 7:00 in the morning", weekly)
        self.assertIn("every weekday at 7:00 in the morning", await self.say("What are my alarms?"))

    async def test_repeating_reminder_reply_has_no_number_when_alone(self):
        line = await self.say("Every weekday at 7, remind me to take my tablets", clock_trusted=True)
        self.assertEqual(line, "Okay. Every weekday at 7:00 in the morning, I'll remind you to take your tablets.")
        entry = self.skills.reminders.upcoming()[0]
        self.assertEqual((entry.rule, entry.at_local), ("weekdays", "07:00"))
        self.assertIn("every weekday at 7:00 in the morning", await self.say("What are my reminders?"))

    async def test_cancel_by_a_word_of_the_task(self):
        self.skills.reminders.add("call Mum", time.time() + 3600)
        kept = self.skills.reminders.add("feed the cat", time.time() + 3600)
        self.assertRegex(await self.say("Cancel the reminder about Mum"), r"^Cancelled reminder \d+\.$")
        self.assertEqual(self.skills.reminders.upcoming(), [kept])
        alarm = self.skills.reminders.add("Alarm", time.time() + 3600, kind="alarm")
        self.assertEqual(await self.say("Cancel my alarm"), f"Cancelled alarm {alarm.id}.")

    async def test_snooze_and_acknowledge_the_ringing_item(self):
        self.assertEqual(await self.say("Snooze"), "Nothing is ringing right now.")
        now = time.time()
        timer = self.skills.reminders.add("Timer", now - 5, kind="timer", duration=300)
        self.assertEqual(await self.say("Five more minutes"), "Okay, I'll ring again in five minutes.")
        moved = self.skills.reminders.get(timer.id)
        self.assertAlmostEqual(moved.due_at, now + 300, delta=5)
        self.assertIsNone(self.skills.reminders.due(now + 200))
        daily = self.skills.reminders.add("take my tablets", now - 5, rule="daily", at_local="07:00", zone=self.zone)
        line = await self.say("I'm up")
        self.assertTrue(line.startswith("Okay. The next one is "), line)
        self.assertGreater(self.skills.reminders.get(daily.id).due_at, now)

    async def test_the_time_elsewhere_and_the_difference(self):
        moment = datetime(2026, 11, 24, 20, 30, tzinfo=self.zone)
        with patch("mind.skills.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone=None: moment.astimezone(zone)
            self.assertEqual(await self.say("What time is it in London?", clock_trusted=True),
                             "In London it's 9:30 in the morning.")
            self.assertEqual(await self.say("What's the time in Tokyo right now?", clock_trusted=True),
                             "In Tokyo it's 6:30 in the evening.")
            self.assertEqual(await self.say("what time is it in new york", clock_trusted=True),
                             "In New York it's 4:30 in the morning.")
            self.assertEqual(await self.say("What time is it in India?", clock_trusted=True),
                             "In India it's 3:00 in the afternoon.")
            self.assertEqual(await self.say("What's the time difference to London?", clock_trusted=True),
                             "London is eleven hours behind you.")
            self.assertEqual(await self.say("What's the time difference with Adelaide?", clock_trusted=True),
                             "Adelaide is thirty minutes behind you.")
            self.assertEqual(await self.say("What's the time difference between London and Tokyo?", clock_trusted=True),
                             "Tokyo is nine hours ahead of London.")
            self.assertEqual(await self.say("How many hours ahead is Delhi?", clock_trusted=True),
                             "Delhi is five and a half hours behind you.")
            self.assertEqual(await self.say("What time is it in Atlantis?", clock_trusted=True),
                             "I don't know the time zone for Atlantis. Try the nearest big city.")
            self.assertIn("not sure of the time", await self.say("What time is it in London?", clock_trusted=False))
        # In the southern winter Sydney is ten hours ahead of London, and a day name shows a new day.
        winter = datetime(2026, 7, 1, 8, 0, tzinfo=self.zone)
        with patch("mind.skills.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone=None: winter.astimezone(zone)
            self.assertEqual(await self.say("What's the time difference to London?", clock_trusted=True),
                             "London is nine hours behind you.")
            self.assertEqual(await self.say("What time is it in London?", clock_trusted=True),
                             "In London it's 11:00 at night, Tuesday.")

    async def test_help_covers_every_group_within_twenty_five_words(self):
        overview = await self.say("What can you do?")
        self.assertIn("timers, alarms", overview)
        self.assertIn("what else", overview)
        more = await self.say("What else can you do?")
        self.assertTrue(more.startswith("I can also "), more)
        for line in (overview, more):
            self.assertLessEqual(len(line.split()), 25, line)
        for group, (_, line) in skills.HELP_GROUPS.items():
            with self.subTest(group=group):
                self.assertLessEqual(len(line.split()), 25, line)
        for said, clue in (("What can you do with lists?", "shopping list"),
                           ("What can you do with the terminal?", "open the terminal"),
                           ("What can you do with your body?", "battery"),
                           ("Help me with alarms", "wake me up"),
                           ("What can I say about reminders?", "remind me"),
                           ("What can you do with timers?", "timer"),
                           ("What can you do with games?", "game"),
                           ("What can you do with guests?", "guest mode")):
            with self.subTest(said=said):
                group = skills.help_group(parse(said, "Pip").arg)
                if group is not None and all(skills.has_feature(module) for module in skills.HELP_GROUPS[group][0]):
                    self.assertIn(clue, await self.say(said))
        # A topic it has no help for goes to the conversation; the line stays for a group not installed.
        self.assertIsNone(parse("What can you do with cheese?", "Pip"))
        self.assertIn("not sure I can do that", skills.help_reply("skills_group", "cheese"))

    def test_the_voice_help_names_the_laptops_menu_keys(self):
        # The laptop's printed F2 is brightness up, so a bare "F2" would mislead.
        line = skills.help_reply("skills_group", "your voice")
        self.assertIn("Or type: Mission Control, or fn and F2, then 6, or Y once installed.", line)
        self.assertLessEqual(len(line.split()), 25, line)

    def test_help_says_only_what_is_installed(self):
        with patch("mind.skills.has_feature", return_value=False):
            self.assertNotIn("lists", skills.help_reply("skills"))
            self.assertEqual(skills.help_reply("skills_more"),
                             "I can also dream with my lid closed. Ask me about one.")
            self.assertIn("not sure", skills.help_reply("skills_group", "the terminal"))
        with patch("mind.skills.has_feature", return_value=True):
            self.assertIn("reminders and lists", skills.help_reply("skills"))
            self.assertIn("open the terminal", skills.help_reply("skills_group", "the terminal"))

    def test_help_groups_cover_section_two(self):
        named = {skills.help_group(topic) for topic in (
            "the time", "timers", "alarms", "reminders", "memory", "lists", "notes", "your body", "battery",
            "the terminal", "programs", "games", "sums", "your voice", "hearing", "dreams", "guest mode")}
        self.assertEqual(named, set(skills.HELP_GROUPS))

    async def test_reminders_today_for_the_brief(self):
        now = self.skills.now()
        soon = self.skills.reminders.add("call Mum", now.timestamp() + 60)
        self.skills.reminders.add("Timer", now.timestamp() + 30, kind="timer")
        self.skills.reminders.add("next week", now.timestamp() + 8 * 86400)
        today = self.skills.reminders_today(now)
        self.assertEqual(today[:1], [soon] if datetime.fromtimestamp(soon.due_at, self.zone).date() == now.date() else [])

    async def test_unknown_kinds_get_a_line_not_an_exception(self):
        from mind.commands import Command

        self.assertIsInstance(await self.skills.reply(Command("diary", "yesterday")), str)
        self.assertIsInstance(await self.skills.reply(Command("something_new")), str)


class Zones(unittest.TestCase):
    def test_a_city_alone_finds_its_zone(self):
        self.assertEqual(skills.find_zone("Sydney").key, "Australia/Sydney")
        self.assertEqual(skills.find_zone("los angeles").key, "America/Los_Angeles")
        self.assertIsNone(skills.find_zone(""))
        self.assertIsNone(skills.find_zone("Atlantis"))

    def test_factory_zone_applies_until_the_owner_chooses(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = root / "config"
        config.mkdir()
        (config / "being.json").write_text(json.dumps({"time_zone": "Australia/Sydney"}))
        with patch("mind.profile.FACTORY_CONFIG", config):
            fresh = skills.Skills(root)
            self.assertEqual((fresh.zone.key, fresh.zone_known), ("Australia/Sydney", True))
            fresh.set_zone(skills.find_zone("Europe/London"))
            self.assertEqual(skills.Skills(root).zone.key, "Europe/London")
            fresh.set_zone(None)
            device = skills.Skills(root)
            self.assertIsNone(device.zone, "an explicit device-clock choice is not overridden")
            self.assertFalse(device.zone_known)

    def test_without_any_zone_the_clock_is_not_known(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch("mind.profile.FACTORY_CONFIG", root / "missing"):
            self.assertFalse(skills.Skills(root).zone_known)


if __name__ == "__main__":
    unittest.main()
