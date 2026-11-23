"""Alarms, repeating reminders and "how long left" requests (L5.1, L5.3)."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from mind.reminder_language import ReminderRequest, may_be_reminder, parse_reminder, spoken_rule

SYDNEY = ZoneInfo("Australia/Sydney")


def wall(request):
    return datetime.fromtimestamp(request.due_at, SYDNEY).replace(tzinfo=None)


class Alarms(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 24, 21, 0, tzinfo=SYDNEY)  # a Tuesday evening

    def parse(self, text, now=None):
        return parse_reminder(text, now=now or self.now)

    def test_one_off_alarms_take_the_next_matching_time(self):
        for said, expected in (
            ("Wake me up at 7", datetime(2026, 11, 25, 7, 0)),
            ("Could you wake me at 6:15 am", datetime(2026, 11, 25, 6, 15)),
            ("Set an alarm for 6:30 tomorrow", datetime(2026, 11, 25, 6, 30)),
            ("Set an alarm for tomorrow at 7", datetime(2026, 11, 25, 7, 0)),
            ("Set an alarm for 10", datetime(2026, 11, 24, 22, 0)),
            ("Set an alarm for 7 in the evening", datetime(2026, 11, 25, 19, 0)),
            ("set an alarm for 21:30", datetime(2026, 11, 24, 21, 30)),
            ("Set a morning alarm for 8", datetime(2026, 11, 25, 8, 0)),
            ("Wake me up on Friday at 9", datetime(2026, 11, 27, 9, 0)),
            ("Wake me up in twenty minutes", datetime(2026, 11, 24, 21, 20)),
        ):
            with self.subTest(said=said):
                request = self.parse(said)
                self.assertEqual((request.kind, request.task, request.rule), ("alarm", "Alarm", None), request)
                self.assertEqual(wall(request), expected)

    def test_repeating_alarms(self):
        for said, rule, at in (
            ("Wake me up at 7 every weekday", "weekdays", "07:00"),
            ("Every morning at 6 wake me up", "daily", "06:00"),
            ("Set an alarm for 6:45 every Saturday", "weekly:sat", "06:45"),
            ("Set a daily alarm for 6:45", "daily", "06:45"),
        ):
            with self.subTest(said=said):
                request = self.parse(said)
                self.assertEqual((request.kind, request.rule, request.at_local), ("alarm", rule, at))
                self.assertGreater(request.due_at, self.now.timestamp())

    def test_unclear_alarms_ask(self):
        for said in ("Wake me up", "Set an alarm", "Set an alarm for tomorrow", "Wake me up when it's sunny",
                     "Set an alarm for 8 pm today"):
            with self.subTest(said=said):
                request = self.parse(said)
                self.assertEqual(request.kind, "clarify")
                self.assertIsNone(request.due_at)

    def test_a_skipped_hour_rings_after_the_change(self):
        night = datetime(2026, 10, 3, 23, 0, tzinfo=SYDNEY)
        request = self.parse("Wake me up at 2:30 am", now=night)
        self.assertEqual(wall(request), datetime(2026, 10, 4, 3, 30))


class Repeats(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 24, 21, 0, tzinfo=SYDNEY)

    def parse(self, text, now=None):
        return parse_reminder(text, now=now or self.now)

    def test_every_owner_phrase_and_its_word_orders(self):
        for said, task, rule, at, first in (
            ("Every weekday at 7, remind me to take my tablets", "take my tablets", "weekdays", "07:00",
             datetime(2026, 11, 25, 7)),
            ("every weekday at 7 remind me to take my tablets", "take my tablets", "weekdays", "07:00",
             datetime(2026, 11, 25, 7)),
            ("Every Tuesday at 8 pm, remind me to put the bins out", "put the bins out", "weekly:tue", "20:00",
             datetime(2026, 12, 1, 20)),
            ("Remind me every day at 9 am to water the plants", "water the plants", "daily", "09:00",
             datetime(2026, 11, 25, 9)),
            ("Remind me to stretch every evening at 6", "stretch", "daily", "18:00", datetime(2026, 11, 25, 18)),
            ("Remind me to call Gran at 10 every Sunday", "call Gran", "weekly:sun", "10:00", datetime(2026, 11, 29, 10)),
            ("Remind me on Mondays at 10:30 to pay the rent", "pay the rent", "weekly:mon", "10:30",
             datetime(2026, 11, 30, 10, 30)),
            ("Remind me daily at 21:15 to lock the door", "lock the door", "daily", "21:15", datetime(2026, 11, 24, 21, 15)),
            ("Remind me to feed the fish each day at 8", "feed the fish", "daily", "08:00", datetime(2026, 11, 25, 8)),
        ):
            with self.subTest(said=said):
                request = self.parse(said)
                self.assertEqual((request.kind, request.task, request.rule, request.at_local),
                                 ("create", task, rule, at), request)
                self.assertEqual(wall(request), first)

    def test_words_that_only_look_like_repeats_stay_one_off(self):
        request = self.parse("Remind me to take my daily vitamins at 9 am tomorrow")
        self.assertEqual((request.task, request.rule), ("take my daily vitamins", None))
        request = self.parse("Remind me to check every room tomorrow at 9 pm")
        self.assertEqual((request.task, request.rule), ("check every room", None))

    def test_a_repeat_without_a_time_asks_for_one(self):
        request = self.parse("Remind me to water the plants every day")
        self.assertEqual(request.kind, "clarify")
        self.assertIn("What time", request.message)

    def test_first_occurrence_is_right_across_both_sydney_changes(self):
        spring_eve = datetime(2026, 10, 3, 12, 0, tzinfo=SYDNEY)
        request = self.parse("Every day at 7, remind me to take my tablets", now=spring_eve)
        self.assertEqual(wall(request), datetime(2026, 10, 4, 7))
        autumn_eve = datetime(2027, 4, 3, 12, 0, tzinfo=SYDNEY)
        request = self.parse("Every day at 7, remind me to take my tablets", now=autumn_eve)
        self.assertEqual(wall(request), datetime(2027, 4, 4, 7))
        self.assertEqual(request.due_at - autumn_eve.timestamp(), 20 * 3600, "an extra hour that night")

    def test_rules_are_said_back(self):
        self.assertEqual(spoken_rule("daily"), "every day")
        self.assertEqual(spoken_rule("weekdays"), "every weekday")
        self.assertEqual(spoken_rule("weekly:tue"), "every Tuesday")


class HowLongLeft(unittest.TestCase):
    def test_left_requests_name_their_kind(self):
        for said, kind in (
            ("How long left on my timer?", "timer"),
            ("How much time is left?", "timer"),
            ("How long is left on the timer", "timer"),
            ("How long has my timer got left?", "timer"),
            ("How long until my timer goes off?", "timer"),
            ("How long until my reminder?", "reminder"),
            ("How long until my next reminder", "reminder"),
            ("When is my next reminder?", "reminder"),
            ("When's my alarm?", "alarm"),
            ("What time is my alarm set for?", "alarm"),
            ("How long until my alarm goes off", "alarm"),
        ):
            with self.subTest(said=said):
                self.assertEqual(parse_reminder(said), ReminderRequest("left", item_kind=kind))

    def test_lists_and_cancels_know_alarms(self):
        self.assertEqual(parse_reminder("What are my alarms?"), ReminderRequest("list", item_kind="alarm"))
        self.assertEqual(parse_reminder("Cancel my alarm"), ReminderRequest("cancel", task="Alarm", item_kind="alarm"))
        self.assertEqual(parse_reminder("Cancel alarm 3"), ReminderRequest("cancel", reminder_id=3, item_kind="alarm"))
        self.assertEqual(parse_reminder("Cancel the morning alarm"),
                         ReminderRequest("cancel", task="Alarm", item_kind="alarm"))

    def test_partial_commands_hold_the_speculative_read(self):
        for partial in ("wake me", "set an alarm", "every weekday", "how long left", "when is my alarm"):
            self.assertTrue(may_be_reminder(partial), partial)
        for said in ("how long does an egg take", "every so often I wonder", "wake up and smell the coffee"):
            self.assertFalse(may_be_reminder(said), said)
            self.assertIsNone(parse_reminder(said), said)


class SpokenForms(unittest.TestCase):
    """Review fixes: commas inside a task, spoken clock forms and alarm lengths."""

    def setUp(self):
        self.now = datetime(2026, 11, 24, 21, 0, tzinfo=SYDNEY)

    def test_commas_in_the_task_keep_the_repeat(self):
        for said, task, rule, at in (
            ("Remind me to call Mum, Dad, and Sue every Sunday at 6 pm", "call Mum, Dad, and Sue", "weekly:sun", "18:00"),
            ("remind me to buy milk, eggs, and bread every day at 5 pm", "buy milk, eggs, and bread", "daily", "17:00"),
            ("remind me to buy milk, eggs every weekday at 7", "buy milk, eggs", "weekdays", "07:00"),
        ):
            with self.subTest(said=said):
                request = parse_reminder(said, now=self.now)
                self.assertEqual((request.kind, request.task, request.rule, request.at_local),
                                 ("create", task, rule, at), request)

    def test_half_past_quarter_to_and_a_dot(self):
        for said, expected in (
            ("Wake me at half past six", datetime(2026, 11, 25, 6, 30)),
            ("Wake me up at 6.30", datetime(2026, 11, 25, 6, 30)),
            ("Set an alarm for quarter to seven tomorrow", datetime(2026, 11, 25, 6, 45)),
            ("Set an alarm for a quarter past 7", datetime(2026, 11, 25, 7, 15)),
            ("Set an alarm for quarter to one", datetime(2026, 11, 25, 0, 45)),
        ):
            with self.subTest(said=said):
                request = parse_reminder(said, now=self.now)
                self.assertEqual(request.kind, "alarm", request)
                self.assertEqual(wall(request), expected)
        self.assertEqual(parse_reminder("Wake me up at half to seven", now=self.now).kind, "clarify")
        request = parse_reminder("Every weekday at quarter past 7, remind me to take my tablets", now=self.now)
        self.assertEqual((request.rule, request.at_local), ("weekdays", "07:15"))
        self.assertEqual(parse_reminder("Remind me to stretch at half past six", now=self.now).kind, "clarify",
                         "a one-off reminder still asks a.m. or p.m.")

    def test_an_alarm_for_a_length_is_relative(self):
        for said in ("Set an alarm for ten minutes", "set an alarm for 10 minutes"):
            with self.subTest(said=said):
                request = parse_reminder(said, now=self.now)
                self.assertEqual((request.kind, wall(request)), ("alarm", datetime(2026, 11, 24, 21, 10)))
        self.assertEqual(wall(parse_reminder("Set an alarm for 10", now=self.now)), datetime(2026, 11, 24, 22, 0))

    def test_more_repeat_openings_hold_the_speculative_read(self):
        for partial in ("every afternoon at", "each day at", "each weekday", "on weekdays at 7"):
            with self.subTest(partial=partial):
                self.assertTrue(may_be_reminder(partial), partial)


if __name__ == "__main__":
    unittest.main()
