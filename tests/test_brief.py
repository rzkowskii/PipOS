"""The morning brief composer (L5.6)."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from mind import brief
from mind.reminders import Reminder

SYDNEY = ZoneInfo("Australia/Sydney")
MORNING = datetime(2026, 11, 24, 7, 40, tzinfo=SYDNEY)
DREAM = "I might have that wrong, but I think the orchid needs more light."


def at(hour, minute=0):
    return datetime(2026, 11, 24, hour, minute, tzinfo=SYDNEY).timestamp()


def words(line):
    """Spoken words: the emotion tag is not said."""
    return len(line.split("] ", 1)[-1].split())


def clauses(line):
    return [part for part in line.split("] ", 1)[1].replace("?", ".").split(". ") if part]


class Compose(unittest.TestCase):
    def test_a_plain_morning(self):
        line = brief.compose(MORNING)
        self.assertTrue(line.startswith("[joy] "), line)
        self.assertIn("morning", line.casefold())

    def test_reminders_body_and_dream_in_order_within_budget(self):
        today = [Reminder(1, "call my sister", at(9), "reminder"), Reminder(2, "put the bins out", at(20), "reminder")]
        line = brief.compose(MORNING, today, {"battery": 12, "on_ac": False}, DREAM)
        self.assertLessEqual(words(line), brief.MAX_WORDS, line)
        self.assertIn("two reminders today: call your sister at 9:00 in the morning", line)
        self.assertIn("plug me in", line, "a body note that matters beats the dream")
        self.assertNotIn("orchid", line, "three clauses at most")

    def test_the_dream_remark_when_the_body_is_fine(self):
        today = [("call my sister", at(9))]
        line = brief.compose(MORNING, today, {"battery": 80, "on_ac": True, "temp": 48.0}, DREAM)
        self.assertIn("call your sister", line)
        self.assertIn("I might have that wrong", line)
        self.assertLessEqual(words(line), brief.MAX_WORDS)

    def test_a_long_day_is_shortened_not_cut_mid_sentence(self):
        today = [Reminder(i, f"do the long and complicated errand number {i} in town", at(9 + i), "reminder")
                 for i in range(1, 6)]
        line = brief.compose(MORNING, today, {"battery": 10, "on_ac": False}, DREAM)
        self.assertLessEqual(words(line), brief.MAX_WORDS, line)
        self.assertIn("five reminders today", line)
        self.assertTrue(line.endswith("."), line)

    def test_days_away_and_an_alarm(self):
        line = brief.compose(MORNING, [Reminder(3, "Alarm", at(8), "alarm")], {}, None, days_away=3)
        self.assertEqual(line, "[joy] You're back. It's been three days. Your alarm is set for 8:00 in the morning.")
        self.assertIn("two weeks", brief.compose(MORNING, days_away=15))

    def test_no_zone_still_greets(self):
        self.assertEqual(brief.compose(None), "[joy] Hello. Good to see you.")

    def test_body_notes_only_when_they_matter(self):
        self.assertIsNone(brief.body_clause({"battery": 15, "charging": True}))
        self.assertIsNone(brief.body_clause({"battery": 55, "on_ac": False, "temp": 60}))
        self.assertIn("warm", brief.body_clause({"temp": 84.0}))
        self.assertIsNone(brief.body_clause(None))

    def test_one_clause_for_a_command_first_morning(self):
        self.assertIsNone(brief.reminder_clause(MORNING, []))
        clause = brief.reminder_clause(MORNING, [Reminder(1, "take my tablets", at(8), "reminder")])
        self.assertEqual(clause, "You have one reminder today: take your tablets at 8:00 in the morning.")

    def test_every_combination_keeps_the_limits(self):
        today = [Reminder(i, "water the tomatoes in the back garden", at(9 + i), "reminder") for i in range(4)]
        for reminders in ((), today[:1], today):
            for body in ({}, {"battery": 5, "on_ac": False}, {"temp": 90}):
                for dream in (None, DREAM):
                    for away in (None, 2, 40):
                        line = brief.compose(MORNING, reminders, body, dream, away)
                        with self.subTest(line=line):
                            self.assertLessEqual(words(line), brief.MAX_WORDS)
                            self.assertLessEqual(len(clauses(line)), brief.MAX_CLAUSES + 1)  # "You're back. It's been..."

    def test_a_tagged_dream_remark_keeps_one_tag_at_the_start(self):
        line = brief.compose(MORNING, (), {}, "[curiosity] " + DREAM)
        self.assertTrue(line.startswith("[joy] "), line)
        self.assertEqual(line.count("["), 1, line)
        self.assertIn("I might have that wrong", line)

    def test_today_window(self):
        start, end = brief.today_window(MORNING)
        self.assertEqual(end - start, (datetime(2026, 11, 25, tzinfo=SYDNEY) - MORNING).total_seconds())


if __name__ == "__main__":
    unittest.main()
