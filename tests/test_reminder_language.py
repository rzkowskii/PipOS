import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mind.reminder_language import ReminderRequest, may_be_reminder, parse_reminder


class ReminderLanguageTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 21, 14, 35, 23, tzinfo=timezone(timedelta(hours=11)))

    def parse(self, text):
        return parse_reminder(text, now=self.now)

    def test_relative_reminders_accept_both_word_orders_and_speech_numbers(self):
        cases = [
            ("Remind me in ten minutes to check the oven", "check the oven", 600),
            ("Could you remind me to call Mum in half an hour?", "call Mum", 1800),
            ("Remind me to check in in 20 minutes", "check in", 1200),
            ("Remind me to run in twenty-five minutes", "run", 1500),
            ("Set a reminder to call Mum in an hour and thirty minutes", "call Mum", 5400),
            ("Remind me in a day to put the bins out", "put the bins out", 86400),
        ]
        for said, task, seconds in cases:
            with self.subTest(said=said):
                self.assertEqual(self.parse(said),
                                 ReminderRequest("create", task, self.now.timestamp() + seconds, relative=True))

    def test_absolute_reminders_use_the_requested_local_day_and_time(self):
        cases = [
            ("Remind me to call Mum tomorrow, at nine a m", 22, 9, 0, "call Mum"),
            ("Set a reminder for 5 pm to feed the dog", 21, 17, 0, "feed the dog"),
            ("Remind me at nine thirty a.m. tomorrow to call Mum", 22, 9, 30, "call Mum"),
            ("Remind me to buy milk for Mum at 17:30", 21, 17, 30, "buy milk for Mum"),
            ("Remind me to check the mail on Monday at noon", 23, 12, 0, "check the mail"),
            ("Set a reminder to call Mum next Saturday at 05:00", 28, 5, 0, "call Mum"),
            ("Remind me on 2026-11-25 at five oh five pm to call Mum", 25, 17, 5, "call Mum"),
            ("Remind me tonight at nine o’clock p.m. to call Mum", 21, 21, 0, "call Mum"),
            ("Remind me tomorrow at midnight to call Mum", 22, 0, 0, "call Mum"),
        ]
        for said, day, hour, minute, task in cases:
            with self.subTest(said=said):
                when = self.now.replace(day=day, hour=hour, minute=minute, second=0)
                self.assertEqual(self.parse(said), ReminderRequest("create", task, when.timestamp()))

    def test_timers_and_cancellation(self):
        for said in ("Set a timer for ten minutes", "Start a ten-minute timer", "Start a 10 minute timer"):
            with self.subTest(said=said):
                self.assertEqual(
                    self.parse(said), ReminderRequest("timer", "Timer", self.now.timestamp() + 600, duration_seconds=600)
                )
        self.assertEqual(self.parse("Cancel the timer"), ReminderRequest("cancel", task="Timer", item_kind="timer"))
        self.assertEqual(self.parse("Cancel timer two"), ReminderRequest("cancel", reminder_id=2, item_kind="timer"))
        for said in ("Cancel reminder #2", "Cancel reminder number two"):
            self.assertEqual(self.parse(said), ReminderRequest("cancel", reminder_id=2, item_kind="reminder"))
        self.assertEqual(
            self.parse("Cancel reminder to call Mum"), ReminderRequest("cancel", task="call Mum", item_kind="reminder")
        )

    def test_list_requests_keep_reminders_and_timers_distinct(self):
        for said in ("What are my upcoming reminders?", "Do I have any reminders?"):
            self.assertEqual(self.parse(said), ReminderRequest("list", item_kind="reminder"))
        for said in ("What timers do I have?", "List my timers", "What are my timers?"):
            self.assertEqual(self.parse(said), ReminderRequest("list", item_kind="timer"))

    def test_incomplete_and_ambiguous_requests_do_not_schedule_anything(self):
        cases = [
            ("Remind me to check the oven", "When"),
            ("Remind me in ten minutes", "What"),
            ("Remind me to call Mum tomorrow", "What time"),
            ("Remind me at five to check the oven", "a.m. or p.m."),
            ("Remind me to call Mum at 5:00 tomorrow", "a.m. or p.m."),
            ("Remind me to check the oven at 10 am", "already passed"),
            ("Remind me to call Mum in zero minutes", "future"),
            ("Remind me to call Mum on 2026-02-30 at 9 am", "invalid"),
            ("Remind me to check the oven in a while", "When"),
            ("Set a timer for", "How long"),
            ("Set a timer for 5", "How long"),
            ("Start a zero second timer", "How long"),
            ("Set a timer for 99999999 days", "shorter"),
            ("Remind me to call Mum in 99999999 days", "shorter"),
            ("Cancel reminder", "Which reminder"),
            ("Cancel timer 0", "Which timer"),
            ("Cancel my timers", "Which timer"),
        ]
        for said, clue in cases:
            with self.subTest(said=said):
                result = self.parse(said)
                self.assertEqual(result.kind, "clarify")
                self.assertIsNone(result.due_at)
                self.assertIn(clue, result.message)

    def test_dst_preserves_elapsed_seconds_and_clarifies_clock_changes(self):
        zone = ZoneInfo("Australia/Sydney")
        spring = datetime(2026, 10, 4, 1, 55, tzinfo=zone)
        timer = parse_reminder("Set a timer for ten minutes", now=spring)
        self.assertEqual(datetime.fromtimestamp(timer.due_at, zone), datetime(2026, 10, 4, 3, 5, tzinfo=zone))
        missing = parse_reminder("Remind me today at 2:30 am to stretch", now=spring)
        self.assertEqual(missing.kind, "clarify")
        self.assertIn("skipped", missing.message)
        autumn = datetime(2026, 4, 5, 0, tzinfo=zone)
        repeated = parse_reminder("Remind me today at 2:30 am to stretch", now=autumn)
        self.assertEqual(repeated.kind, "clarify")
        self.assertIn("twice", repeated.message)

    def test_non_reminder_turns_can_reach_their_usual_route(self):
        for said in ("Remind me what I said yesterday", "Remind me of your name", "I like reminders"):
            self.assertIsNone(self.parse(said))
        for partial in ("remind", "set a", "start a ten minute", "what are my", "cancel reminder", "read my timers"):
            self.assertTrue(may_be_reminder(partial), partial)
        for partial in (
            "I have a question", "show me how to juggle", "list the ingredients", "read me a poem",
            "start a two person game", "set a scene in London", "do I have enough battery",
        ):
            self.assertFalse(may_be_reminder(partial), partial)
