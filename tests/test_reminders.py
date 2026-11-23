import stat
import tempfile
import unittest
from pathlib import Path

from mind.reminders import Reminders, mentions


class RemindersTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reminders = self.open_queue()

    def open_queue(self):
        reminders = Reminders(self.root)
        self.addCleanup(reminders.close)
        return reminders

    def test_due_entry_remains_pending_until_playback_is_confirmed(self):
        entry = self.reminders.add("Call Sam", 3600)

        self.assertIsNone(self.reminders.due(3599.99))
        self.assertEqual(self.reminders.due(3600), entry)
        reopened = self.open_queue()
        self.assertEqual(reopened.due(4000), entry)

        self.assertTrue(reopened.complete(entry.id))
        self.assertFalse(self.reminders.complete(entry.id))
        self.assertIsNone(self.open_queue().due(4000))

    def test_oldest_first_across_reminders_and_timers(self):
        later = self.reminders.add("Check the mail", 20)
        timer = self.reminders.add("Tea", 10, kind="timer")
        earlier = self.reminders.add("Call Sam", 10)

        self.assertEqual(self.reminders.upcoming(), [timer, earlier, later])
        self.assertEqual(self.reminders.upcoming(1, kind="reminder"), [earlier])
        self.assertEqual(self.reminders.upcoming(kind="timer"), [timer])
        self.assertEqual(self.reminders.due(30), timer)
        self.assertFalse(self.reminders.cancel(timer.id, kind="reminder"))
        self.assertTrue(self.reminders.cancel(timer.id, kind="timer"))
        self.assertEqual(self.reminders.due(30), earlier)

    def test_matching_can_distinguish_duplicate_tasks_by_kind(self):
        first = self.reminders.add("Check    the tea", 10)
        second = self.reminders.add("Check the tea", 20)
        self.reminders.add("Check the tea", 30, kind="timer")
        self.reminders.add("Check the teas", 40)

        self.assertEqual(self.reminders.matching(" CHECK the TEA ", kind="reminder"), [first, second])
        self.assertTrue(self.reminders.cancel(first.id))
        self.assertFalse(self.reminders.cancel(first.id))
        self.assertEqual(self.reminders.matching("Check the tea", kind="reminder"), [second])

    def test_completed_ids_are_never_reused(self):
        first = self.reminders.add("Call Sam", 10)
        self.reminders.complete(first.id)
        second = self.open_queue().add("Call Sam", 10)

        self.assertGreater(second.id, first.id)
        self.assertFalse(self.reminders.complete(first.id))
        self.assertEqual(self.reminders.due(10), second)

    def test_private_database_and_invalid_input(self):
        self.assertEqual(stat.S_IMODE((self.root / "reminders.db").stat().st_mode), 0o600)
        for text, due_at, kind in (("  ", 123, "reminder"), ("Tea", float("inf"), "timer"), ("Tea", 30, "unknown")):
            with self.subTest(text=text, due_at=due_at, kind=kind), self.assertRaises(ValueError):
                self.reminders.add(text, due_at, kind=kind)
        self.assertEqual(self.reminders.upcoming(), [])


class MentionsTest(unittest.TestCase):
    """The one rule forgetting uses for reminders, for an entry in the queue
    or one held by a ring the owner may still answer."""

    def test_it_matches_whole_words_either_way(self):
        self.assertTrue(mentions("call Mum", ["Mum"]))
        self.assertTrue(mentions("call Mum", ["I must call Mum on Sunday"]))
        self.assertFalse(mentions("call Mumford", ["Mum"]))
        self.assertFalse(mentions("buy milk", ["Mum likes milk"]))

    def test_placeholders_and_empty_words_never_match(self):
        self.assertFalse(mentions("Alarm", ["the alarm clock is broken"]))
        self.assertFalse(mentions("Timer", ["the egg timer"]))
        self.assertFalse(mentions("call Mum", []))
        self.assertFalse(mentions("call Mum", None))


class RingingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reminders = Reminders(self.root)
        self.addCleanup(self.reminders.close)

    def test_an_unanswered_item_rings_three_times_with_gaps(self):
        from mind.reminders import MAX_RINGS, RING_GAP

        entry = self.reminders.add("Tea", 100, kind="timer", duration=600, now=0)
        self.assertEqual(self.reminders.delivery(entry.id).duration, 600)
        now = 100
        for ring in range(1, MAX_RINGS):
            self.assertEqual(self.reminders.due(now), entry)
            self.assertTrue(self.reminders.rang(entry.id, now))
            self.assertIsNone(self.reminders.due(now + RING_GAP - 1), "waits between repeats")
            now += RING_GAP
            self.assertEqual(self.reminders.delivery(entry.id).rings, ring)
        self.assertFalse(self.reminders.rang(entry.id, now), "the last ring ends it")
        self.assertIsNone(self.reminders.due(now + 10_000))

    def test_acknowledgement_stops_repeats(self):
        entry = self.reminders.add("Call Sam", 10)
        self.reminders.rang(entry.id, 10)
        self.assertTrue(self.reminders.acknowledge(entry.id))
        self.assertIsNone(self.reminders.due(10_000))

    def test_forgetting_reaches_reminders_that_mention_it(self):
        kept = self.reminders.add("Water the basil", 10)
        self.reminders.add("Buy a present for Tom the cat", 20)
        self.assertEqual(self.reminders.forget_mentioning(["tom the cat"]), 1)
        self.assertEqual(self.reminders.upcoming(), [kept])

    def test_an_older_queue_gains_the_new_columns(self):
        import sqlite3

        old = self.root / "old"
        old.mkdir()
        db = sqlite3.connect(old / "reminders.db")
        db.executescript("CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, "
                         "due_at REAL NOT NULL, kind TEXT NOT NULL); "
                         "INSERT INTO reminders (text, due_at, kind) VALUES ('Tea', 5, 'timer');")
        db.close()
        upgraded = Reminders(old)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.due(10).text, "Tea")
        self.assertEqual(upgraded.delivery(1).rings, 0)


class RepeatTest(unittest.TestCase):
    """L5.1: repeating reminders and alarms, across both of Sydney's clock changes."""

    SYDNEY = "Australia/Sydney"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reminders = Reminders(self.root)
        self.addCleanup(self.reminders.close)
        from zoneinfo import ZoneInfo

        self.zone = ZoneInfo(self.SYDNEY)

    def local(self, *parts):
        from datetime import datetime

        return datetime(*parts, tzinfo=self.zone).timestamp()

    def wall(self, stamp):
        from datetime import datetime

        return datetime.fromtimestamp(stamp, self.zone).replace(tzinfo=None)

    def test_a_daily_alarm_keeps_its_wall_clock_time_across_both_changes(self):
        from datetime import datetime
        from mind.reminders import next_occurrence

        # Daylight saving starts on Sunday 4 October 2026 (2:00 becomes 3:00)
        # and ends on Sunday 5 April 2026 and 4 April 2027 (3:00 becomes 2:00).
        for after, expected in (
            (self.local(2026, 10, 3, 7, 0), datetime(2026, 10, 4, 7, 0)),
            (self.local(2026, 10, 4, 7, 0), datetime(2026, 10, 5, 7, 0)),
            (self.local(2026, 4, 4, 7, 0), datetime(2026, 4, 5, 7, 0)),
            (self.local(2027, 4, 3, 7, 0), datetime(2027, 4, 4, 7, 0)),
        ):
            with self.subTest(after=self.wall(after)):
                stamp = next_occurrence("daily", "07:00", self.SYDNEY, after)
                self.assertEqual(self.wall(stamp), expected)
        # The day of the spring change is 23 hours long, the autumn one 25.
        spring = next_occurrence("daily", "07:00", self.SYDNEY, self.local(2026, 10, 3, 7, 0))
        self.assertEqual(spring - self.local(2026, 10, 3, 7, 0), 23 * 3600)
        autumn = next_occurrence("daily", "07:00", self.SYDNEY, self.local(2027, 4, 3, 7, 0))
        self.assertEqual(autumn - self.local(2027, 4, 3, 7, 0), 25 * 3600)

    def test_skipped_and_repeated_times_ring_once(self):
        from datetime import datetime
        from mind.reminders import next_occurrence

        skipped = next_occurrence("daily", "02:30", self.SYDNEY, self.local(2026, 10, 3, 12, 0))
        self.assertEqual(self.wall(skipped), datetime(2026, 10, 4, 3, 30), "2:30 does not exist; it rings at 3:30")
        twice = next_occurrence("daily", "02:30", self.SYDNEY, self.local(2027, 4, 3, 12, 0))
        self.assertEqual(self.wall(twice), datetime(2027, 4, 4, 2, 30))
        following = next_occurrence("daily", "02:30", self.SYDNEY, twice)
        self.assertEqual(self.wall(following), datetime(2027, 4, 5, 2, 30), "the second 2:30 does not ring again")

    def test_weekday_and_weekly_rules(self):
        from datetime import datetime
        from mind.reminders import next_occurrence

        friday = self.local(2026, 11, 27, 8, 0)
        self.assertEqual(self.wall(next_occurrence("weekdays", "07:00", self.SYDNEY, friday)), datetime(2026, 11, 30, 7))
        self.assertEqual(self.wall(next_occurrence("weekly:tue", "20:00", self.SYDNEY, friday)), datetime(2026, 12, 1, 20))
        tuesday_evening = self.local(2026, 12, 1, 19, 59)
        self.assertEqual(self.wall(next_occurrence("weekly:tue", "20:00", self.SYDNEY, tuesday_evening)),
                         datetime(2026, 12, 1, 20))
        for rule in ("monthly", "weekly:xyz", "weekly:", None):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                next_occurrence(rule, "07:00", self.SYDNEY, friday)

    def test_finishing_a_repeat_moves_it_and_cancel_removes_the_series(self):
        from datetime import datetime

        due = self.local(2026, 11, 25, 7, 0)
        entry = self.reminders.add("take my tablets", due, rule="weekdays", at_local="07:00", zone=self.zone)
        self.assertEqual(entry.rule, "weekdays")
        self.assertTrue(self.reminders.complete(entry.id, now=due + 5))
        moved = self.reminders.get(entry.id)
        self.assertEqual(self.wall(moved.due_at), datetime(2026, 11, 26, 7))
        self.assertTrue(self.reminders.complete(entry.id, now=due + 6), "a repeated confirmation is safe")
        self.assertEqual(self.reminders.get(entry.id).due_at, moved.due_at, "and does not skip a day")
        # Three unanswered rings end only this occurrence.
        now = moved.due_at
        while self.reminders.rang(entry.id, now):
            now += 120
        self.assertEqual(self.wall(self.reminders.get(entry.id).due_at), datetime(2026, 11, 27, 7))
        self.assertEqual(self.reminders.delivery(entry.id).rings, 0)
        self.assertTrue(self.reminders.acknowledge(entry.id, now=self.reminders.get(entry.id).due_at))
        self.assertEqual(self.wall(self.reminders.get(entry.id).due_at), datetime(2026, 11, 30, 7), "Friday to Monday")
        self.assertTrue(self.reminders.cancel(entry.id))
        self.assertIsNone(self.reminders.get(entry.id))

    def test_a_repeat_missed_by_many_hours_waits_for_its_next_time(self):
        from datetime import datetime

        due = self.local(2026, 11, 25, 7, 0)
        entry = self.reminders.add("Alarm", due, kind="alarm", rule="daily", at_local="07:00", zone=self.SYDNEY)
        self.assertEqual(self.reminders.due(due + 3600), entry, "an hour late still rings, and says so")
        self.assertIsNone(self.reminders.due(due + 13 * 3600))
        self.assertEqual(self.wall(self.reminders.get(entry.id).due_at), datetime(2026, 11, 26, 7))

    def test_a_doubted_clock_holds_everything_but_timers(self):
        reminder = self.reminders.add("Call Sam", 10)
        alarm = self.reminders.add("Alarm", 11, kind="alarm")
        timer = self.reminders.add("Timer", 12, kind="timer", duration=60)
        self.assertEqual(self.reminders.due(100, clock_trusted=False), timer)
        self.assertEqual(self.reminders.due(100), reminder)
        self.reminders.complete(reminder.id)
        self.assertEqual(self.reminders.due(100), alarm)

    def test_invalid_repeats_are_refused(self):
        for kwargs in ({"rule": "hourly", "at_local": "07:00"}, {"rule": "daily", "at_local": None},
                       {"rule": "daily", "at_local": "25:00"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.reminders.add("Tea", 100, **kwargs)
        with self.assertRaises(ValueError):
            self.reminders.add("Tea", 100, kind="timer", rule="daily", at_local="07:00")

    def test_today_between(self):
        first = self.reminders.add("Call Sam", 10)
        self.reminders.add("Timer", 20, kind="timer")
        alarm = self.reminders.add("Alarm", 30, kind="alarm")
        self.reminders.add("Later", 50)
        self.assertEqual(self.reminders.between(0, 40), [first, alarm])


class SnoozeTest(unittest.TestCase):
    """L5.2: snooze moves the ringing item and starts its rings again."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.reminders = Reminders(Path(temporary.name))
        self.addCleanup(self.reminders.close)

    def test_snooze_and_ringing(self):
        self.assertIsNone(self.reminders.ringing(100))
        older = self.reminders.add("Call Sam", 50)
        timer = self.reminders.add("Timer", 90, kind="timer", duration=60)
        self.reminders.add("Later", 5000)
        self.assertEqual(self.reminders.ringing(100), timer, "the most recent due item")
        self.reminders.rang(older.id, 100)
        self.assertEqual(self.reminders.ringing(100), older, "one already said comes first")
        moved = self.reminders.snooze(older.id, 9, now=100)
        self.assertEqual((moved.due_at, self.reminders.delivery(older.id).rings), (640, 0))
        self.assertEqual(self.reminders.due(600), timer)
        self.reminders.complete(timer.id)
        self.assertIsNone(self.reminders.due(600))
        self.assertEqual(self.reminders.due(640), moved)
        self.assertIsNone(self.reminders.snooze(9999, 5, now=100))
        for minutes in (0, -1, 121, "5"):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                self.reminders.snooze(older.id, minutes, now=100)


class ClockMovedTest(unittest.TestCase):
    """Setting the clock by hand keeps timers counting real time."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.reminders = Reminders(Path(temporary.name))
        self.addCleanup(self.reminders.close)

    def test_only_timers_follow_a_clock_change(self):
        timer = self.reminders.add("Timer", 1600, kind="timer", duration=600, now=1000)
        reminder = self.reminders.add("Call Sam", 5000)
        self.reminders.rang(timer.id, 1600)
        jump = 37 * 86400.0
        self.assertEqual(self.reminders.clock_moved(jump), 1)
        self.assertEqual(self.reminders.get(timer.id).due_at, 1600 + jump)
        self.assertEqual(self.reminders.delivery(timer.id).created_at, 1000 + jump)
        self.assertNotEqual(self.reminders.due(1600 + jump + 60), self.reminders.get(timer.id),
                            "its ring gap moved too")
        self.assertEqual(self.reminders.due(1600 + jump + 120), self.reminders.get(reminder.id))
        self.assertEqual(self.reminders.get(reminder.id).due_at, 5000, "wall-clock times stay")
        self.assertEqual(self.reminders.clock_moved(0), 0)
        for bad in (float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.reminders.clock_moved(bad)


class MigrationTest(unittest.TestCase):
    def test_a_version_one_queue_gains_alarms_and_keeps_its_ids(self):
        import sqlite3

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        db = sqlite3.connect(root / "reminders.db")
        db.executescript("""
            CREATE TABLE reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due_at REAL NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reminder', 'timer')));
            CREATE INDEX reminders_due ON reminders (due_at, id);
            CREATE TABLE greeting_days (day TEXT PRIMARY KEY);
            ALTER TABLE reminders ADD COLUMN created_at REAL;
            ALTER TABLE reminders ADD COLUMN duration INTEGER;
            ALTER TABLE reminders ADD COLUMN rings INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE reminders ADD COLUMN next_at REAL;
            INSERT INTO reminders (text, due_at, kind, duration) VALUES ('Tea', 5, 'timer', 300);
            INSERT INTO reminders (text, due_at, kind) VALUES ('Call Sam', 6, 'reminder');
            INSERT INTO reminders (text, due_at, kind) VALUES ('Done already', 7, 'reminder');
            DELETE FROM reminders WHERE id = 3;
            INSERT INTO greeting_days VALUES ('2026-11-23');
        """)
        db.commit()
        db.close()
        upgraded = Reminders(root)
        self.addCleanup(upgraded.close)
        self.assertEqual([(entry.id, entry.text, entry.kind) for entry in upgraded.upcoming()],
                         [(1, "Tea", "timer"), (2, "Call Sam", "reminder")])
        self.assertEqual(upgraded.delivery(1).duration, 300)
        from datetime import date

        self.assertTrue(upgraded.greeted_on(date(2026, 11, 23)))
        alarm = upgraded.add("Alarm", 100, kind="alarm")
        self.assertEqual(alarm.id, 4, "an ID is never reused after the rebuild")
        reopened = Reminders(root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(4), alarm)
