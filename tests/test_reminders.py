import stat
import tempfile
import unittest
from pathlib import Path

from mind.reminders import Reminders


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
