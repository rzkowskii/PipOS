"""Regressions: an answer after the last ring, forgetting, clock corrections, "tonight" and erasing."""

import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mind import reminders as reminders_module, skills
from mind.commands import parse
from mind.reminder_language import parse_reminder
from mind.reminders import Reminders

SYDNEY = ZoneInfo("Australia/Sydney")


def local(*parts):
    return datetime(*parts, tzinfo=SYDNEY).timestamp()


def wall(stamp):
    return datetime.fromtimestamp(stamp, SYDNEY).replace(tzinfo=None)


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.reminders = Reminders(self.root)
        self.addCleanup(self.reminders.close)


class AnswerAfterTheLastRing(QueueCase):
    def test_okay_after_the_third_ring_keeps_tomorrows_alarm(self):
        start = local(2026, 11, 24, 7, 0)
        alarm = self.reminders.add("Alarm", start, kind="alarm", rule="daily", at_local="07:00", zone=SYDNEY)
        self.assertEqual([self.reminders.rang(alarm.id, start + gap) for gap in (0, 120, 240)],
                         [True, True, False])
        self.assertEqual(wall(self.reminders.get(alarm.id).due_at), datetime(2026, 11, 25, 7, 0))
        self.assertTrue(self.reminders.acknowledge(alarm.id, start + 270))
        self.assertEqual(wall(self.reminders.get(alarm.id).due_at), datetime(2026, 11, 25, 7, 0),
                         "tomorrow's alarm is not skipped")

    def test_okay_while_ringing_still_moves_a_repeat_on(self):
        start = local(2026, 11, 24, 7, 0)
        alarm = self.reminders.add("Alarm", start, kind="alarm", rule="daily", at_local="07:00", zone=SYDNEY)
        self.reminders.rang(alarm.id, start)
        self.reminders.acknowledge(alarm.id, start + 30)
        self.assertEqual(wall(self.reminders.get(alarm.id).due_at), datetime(2026, 11, 25, 7, 0))

    def test_okay_to_a_snoozed_repeat_moves_it_to_its_next_time(self):
        start = local(2026, 11, 24, 7, 0)
        alarm = self.reminders.add("Alarm", start, kind="alarm", rule="daily", at_local="07:00", zone=SYDNEY)
        self.reminders.snooze(alarm.id, 10, now=start + 30)
        self.reminders.acknowledge(alarm.id, start + 60)
        self.assertEqual(wall(self.reminders.get(alarm.id).due_at), datetime(2026, 11, 25, 7, 0))


class ForgettingSparesPlaceholders(QueueCase):
    def test_a_fact_about_an_alarm_clock_cancels_no_alarm_or_timer(self):
        start = local(2026, 11, 25, 7, 0)
        alarm = self.reminders.add("Alarm", start, kind="alarm", rule="daily", at_local="07:00", zone=SYDNEY)
        timer = self.reminders.add("Timer", start, kind="timer", duration=600)
        call = self.reminders.add("call Mum", start)
        removed = self.reminders.forget_mentioning(["The egg timer lives in the drawer by the alarm clock"])
        self.assertEqual(removed, 0)
        self.assertEqual([entry.id for entry in self.reminders.upcoming()], [alarm.id, timer.id, call.id])

    def test_words_are_matched_whole(self):
        self.reminders.add("ring Sam", 100)
        self.reminders.add("call Mum", 100)
        self.assertEqual(self.reminders.forget_mentioning(["Samantha is bringing cake"]), 0,
                         "'ring' inside 'bringing' and 'Sam' inside 'Samantha' are not those words")
        self.assertEqual(self.reminders.forget_mentioning(["Mum"]), 1)
        self.assertEqual(self.reminders.forget_mentioning(["I must ring Sam on Friday"]), 1)
        self.assertEqual(self.reminders.upcoming(), [])

    def test_a_labelled_timer_is_still_forgotten(self):
        self.reminders.add("pasta", 100, kind="timer", duration=600)
        self.assertEqual(self.reminders.forget_mentioning(["the pasta is gluten free"]), 1)


class DelaysFollowTheClock(QueueCase):
    def test_delays_move_with_a_clock_correction_and_clock_times_stay(self):
        before = local(2026, 11, 24, 4, 0)   # the clock says 4:00; it is really 7:00
        delay = self.reminders.add("Alarm", before + 3600, kind="alarm", now=before, relative=True)
        later = self.reminders.add("call Sam", before + 7200, now=before, relative=True)
        fixed = self.reminders.add("Alarm", local(2026, 11, 24, 9, 0), kind="alarm", now=before)
        jump = 3 * 3600.0
        self.reminders.clock_moved(jump, now=before + jump)
        self.assertEqual(wall(self.reminders.get(delay.id).due_at), datetime(2026, 11, 24, 8, 0))
        self.assertEqual(wall(self.reminders.get(later.id).due_at), datetime(2026, 11, 24, 9, 0))
        self.assertEqual(wall(self.reminders.get(fixed.id).due_at), datetime(2026, 11, 24, 9, 0))
        self.assertIsNone(self.reminders.due(before + jump + 60), "nothing rings at once as late")

    def test_a_delay_rings_while_the_clock_is_doubted(self):
        delay = self.reminders.add("Alarm", 1000, kind="alarm", now=400, relative=True)
        self.reminders.add("Alarm", 900, kind="alarm", now=400)
        self.assertEqual(self.reminders.due(1000, clock_trusted=False), delay)

    def test_a_held_clock_time_is_worked_out_from_the_corrected_day(self):
        doubted = local(2026, 10, 18, 10, 0)     # the date is 37 days behind
        tomorrow_nine = local(2026, 10, 19, 9, 0)
        held = self.reminders.add("call the bank", tomorrow_nine, now=doubted, zone=SYDNEY, held=True)
        self.assertIsNone(self.reminders.due(tomorrow_nine + 60, clock_trusted=False), "held while doubted")
        jump = local(2026, 11, 24, 10, 0) - doubted
        self.reminders.clock_moved(jump, now=doubted + jump)
        self.assertEqual(wall(self.reminders.get(held.id).due_at), datetime(2026, 11, 25, 9, 0),
                         "'tomorrow at 9' is the real tomorrow, not 36 days late")
        self.assertIsNone(self.reminders.due(doubted + jump + 60))

    def test_a_repeat_pushed_too_far_by_a_wrong_date_comes_back(self):
        ahead = local(2026, 12, 31, 6, 0)      # the clock is a month ahead
        alarm = self.reminders.add("Alarm", local(2026, 12, 31, 7, 0), kind="alarm", rule="daily",
                                   at_local="07:00", zone=SYDNEY, now=ahead)
        real = local(2026, 11, 24, 6, 0)
        self.reminders.clock_moved(real - ahead, now=real)
        self.assertEqual(wall(self.reminders.get(alarm.id).due_at), datetime(2026, 11, 24, 7, 0))

    def test_language_marks_delays(self):
        now = datetime(2026, 11, 24, 5, 0, tzinfo=SYDNEY)
        for said, relative in (("Remind me to call Mum in two hours", True),
                               ("Set an alarm for ten minutes", True),
                               ("Wake me in eight hours", True),
                               ("Remind me to call Mum at 7 pm", False),
                               ("Wake me up at 7", False)):
            with self.subTest(said=said):
                self.assertIs(parse_reminder(said, now=now).relative, relative)


class Tonight(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 24, 5, 0, tzinfo=SYDNEY)

    def test_tonight_is_the_evening(self):
        for said, expected in (
            ("Set an alarm for 9 tonight", datetime(2026, 11, 24, 21, 0)),
            ("Wake me up at 7 tonight", datetime(2026, 11, 24, 19, 0)),
            ("Set an alarm for tonight at 9", datetime(2026, 11, 24, 21, 0)),
            ("Set an alarm for 12 tonight", datetime(2026, 11, 25, 0, 0)),
            ("Set an alarm for midnight tonight", datetime(2026, 11, 25, 0, 0)),
            ("Remind me to call Mum at 7 tonight", datetime(2026, 11, 24, 19, 0)),
            ("Remind me to call Mum tonight at 7", datetime(2026, 11, 24, 19, 0)),
            ("Remind me to lock up at 12 tonight", datetime(2026, 11, 25, 0, 0)),
        ):
            with self.subTest(said=said):
                request = parse_reminder(said, now=self.now)
                self.assertIn(request.kind, ("alarm", "create"), request.message)
                self.assertEqual(wall(request.due_at), expected)

    def test_a_past_evening_time_tonight_is_refused(self):
        late = datetime(2026, 11, 24, 22, 0, tzinfo=SYDNEY)
        self.assertEqual(parse_reminder("Set an alarm for 9 tonight", now=late).kind, "clarify")
        self.assertEqual(parse_reminder("Remind me to call Mum at 9 tonight", now=late).kind, "clarify")


class EraseAll(QueueCase):
    def test_forget_everything_empties_the_queue_and_greeted_days(self):
        self.reminders.add("call the lawyer about the divorce", time.time() + 3600)
        self.reminders.add("Alarm", time.time() + 7200, kind="alarm")
        self.reminders.record_greeting(datetime(2026, 11, 24).date())
        reminders_module.erase_all(self.root)
        self.assertEqual(self.reminders.upcoming(), [], "the open queue sees the erase")
        self.assertFalse(self.reminders.greeted_on(datetime(2026, 11, 24).date()))
        self.assertNotIn(b"lawyer", (self.root / "reminders.db").read_bytes())
        again = self.reminders.add("feed the cat", time.time() + 60)
        reopened = Reminders(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(again.id), again, "the file still works after the erase")

    def test_erase_without_a_queue_is_harmless(self):
        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reminders_module.erase_all(empty)
        self.assertFalse((empty / "reminders.db").exists())


class SkillsReplies(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "skills.json").write_text(json.dumps({"time_zone": "Australia/Sydney"}))
        self.skills = skills.Skills(self.root)
        self.addCleanup(self.skills.close)

    async def say(self, text, **kwargs):
        command = parse(text, "Pip", now=self.skills.now())
        self.assertIsNotNone(command, text)
        return await self.skills.reply(command, **kwargs)

    async def test_a_delay_set_while_doubted_is_not_held(self):
        line = await self.say("Wake me in an hour", clock_trusted=False)
        self.assertNotIn("hold it", line)
        entry = self.skills.reminders.upcoming(kind="alarm")[0]
        relative, held_at = self.skills.reminders.db.execute(
            "SELECT relative, held_at FROM reminders WHERE id = ?", (entry.id,)).fetchone()
        self.assertEqual((relative, held_at), (1, None))

    async def test_a_clock_time_set_while_doubted_is_held_for_rework(self):
        line = await self.say("Set an alarm for 6:30 tomorrow", clock_trusted=False)
        self.assertIn("hold it until you tell me the time", line)
        entry = self.skills.reminders.upcoming(kind="alarm")[0]
        held_at = self.skills.reminders.db.execute(
            "SELECT held_at FROM reminders WHERE id = ?", (entry.id,)).fetchone()[0]
        self.assertEqual(held_at, "+1 06:30")


class Migration(unittest.TestCase):
    def test_a_version_two_queue_gains_the_new_columns(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db = sqlite3.connect(root / "reminders.db")
        db.executescript("""
            CREATE TABLE reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due_at REAL NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reminder', 'timer', 'alarm')), created_at REAL,
                duration INTEGER, rings INTEGER NOT NULL DEFAULT 0, next_at REAL, rule TEXT,
                at_local TEXT, zone TEXT);
            INSERT INTO reminders (text, due_at, kind, created_at, duration) VALUES ('Timer', 700, 'timer', 100, 600);
            PRAGMA user_version = 2;
        """)
        db.close()
        reminders = Reminders(root)
        self.addCleanup(reminders.close)
        self.assertEqual(reminders.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(reminders.clock_moved(50, now=200), 1, "an old timer still follows the clock")
        self.assertEqual(reminders.get(1).due_at, 750)


if __name__ == "__main__":
    unittest.main()
