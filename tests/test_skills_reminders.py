"""Spoken reminder wording, time zones and the factory zone."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import skills
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
        self.assertIn("an hour late", self.skills.announcement(reminder, "Richard", now=1000 + 3700))
        self.assertIn("an hour late, because my lid was closed. At ",
                      self.skills.announcement(reminder, "Richard", now=1000 + 3700, slept=True))
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
