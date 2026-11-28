"""The clock (mind/app_clock.py): the big clock, the owner's timers and next
reminders read from reminders.db without ever writing it, the owner's zone,
and honest notes when the zone or the clock itself is in doubt."""

import json
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from mind import app_clock
from mind.app_clock import Reader, big_text, countdown, read_entries, render, timer_name, when
from mind.reminders import Reminders

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SYDNEY = ZoneInfo("Australia/Sydney")
# Tuesday 24 November 2026, 7:05:09 pm in Sydney (daylight saving: UTC+11).
NOW = datetime(2026, 11, 24, 19, 5, 9, tzinfo=SYDNEY).timestamp()


def lines(frame):
    return [ANSI.sub("", row) for row in re.split(r"\x1b\[\d+;1H", frame)[1:]]


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.mind = Path(self.dir.name)

    def store(self):
        store = Reminders(self.mind)
        self.addCleanup(store.close)
        return store

    def test_the_big_digits(self):
        rows = big_text("7:05")
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual(rows[0][:12], "████████████", "the 7's top bar")
        self.assertTrue(set("".join(rows)) <= {"█", " "})

    def test_a_frame_with_timers_and_reminders(self):
        store = self.store()
        store.add("Timer", NOW + 252, kind="timer", duration=600, now=NOW - 348)
        store.add("Timer", NOW + 3600, kind="timer", duration=5400, now=NOW - 1800)
        store.add("call Mum", NOW + 14 * 3600, now=NOW)
        store.add("put the bins out", NOW + 3 * 86400, now=NOW)
        reader = Reader(self.mind)
        reader.zone, reader.zone_known, reader._zone_at = SYDNEY, True, time.monotonic()
        text = "\n".join(lines(render(NOW, reader.read(NOW))))
        for words in ("Tuesday 24 November 2026", "pm", "09",
                      "10-minute timer", "4:12 left", "90-minute timer", "1:00:00 left",
                      "Next reminder   call Mum  tomorrow at 9:05 am", "put the bins out  on Friday at 7:05 pm"):
            self.assertIn(words, text)
        self.assertNotIn("time zone isn't set", text)

    def test_never_writes_the_database(self):
        store = self.store()
        store.add("call Mum", NOW + 60, now=NOW)
        database = self.mind / "reminders.db"
        before = (database.stat().st_mtime_ns, database.read_bytes())
        for _ in range(3):
            self.assertEqual(len(read_entries(self.mind)), 1)
        self.assertEqual((database.stat().st_mtime_ns, database.read_bytes()), before)
        with self.assertRaises(sqlite3.OperationalError):
            db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            db.execute("DELETE FROM reminders")

    def test_no_database_and_a_broken_one(self):
        self.assertEqual(read_entries(self.mind), [])
        text = "\n".join(lines(render(NOW, {"entries": [], "zone": SYDNEY, "zone_known": True})))
        self.assertIn('No timers or reminders. Say "set a timer for ten minutes".', text)
        (self.mind / "reminders.db").write_bytes(b"not a database at all" * 100)
        self.assertIsNone(read_entries(self.mind))
        text = "\n".join(lines(render(NOW, {"entries": None, "zone": SYDNEY, "zone_known": True})))
        self.assertIn("I can't read my timers and reminders just now.", text)

    def test_reminder_words_are_cleaned(self):
        store = self.store()
        store.add("ring\x1b[31m the \x07vet " + "x" * 80, NOW + 60, now=NOW)
        entry = read_entries(self.mind)[0]
        self.assertNotIn("\x1b", entry["text"])
        self.assertNotIn("\x07", entry["text"])
        self.assertLessEqual(len(entry["text"]), app_clock.TEXT)
        self.assertTrue(entry["text"].endswith("…"))

    def test_zone_from_the_owners_settings(self):
        (self.mind / "skills.json").write_text(json.dumps({"time_zone": "Europe/London"}))
        self.assertEqual(str(app_clock.owner_zone(self.mind)), "Europe/London")
        (self.mind / "skills.json").write_text(json.dumps({"time_zone": ""}))
        self.assertIsNone(app_clock.owner_zone(self.mind), "the device zone, chosen on purpose")
        text = "\n".join(lines(render(NOW, {"entries": [], "zone": None, "zone_known": False})))
        self.assertIn("My time zone isn't set, so this is my own clock's time.", text)

    def test_a_doubted_clock_says_so(self):
        text = "\n".join(lines(render(NOW, {"entries": [], "zone": SYDNEY, "zone_known": True, "doubted": True})))
        self.assertIn("My clock may be wrong. Check the date and time in Setup.", text)
        from mind import clock_epoch
        with mock.patch.object(clock_epoch, "doubted", lambda: True):
            self.assertTrue(app_clock.clock_doubted())
        with mock.patch.object(clock_epoch, "doubted", mock.Mock(side_effect=OSError)):
            self.assertFalse(app_clock.clock_doubted(), "a check that cannot run is not a reason to doubt")

    def test_words(self):
        self.assertEqual(countdown(252.2), "4:13")
        self.assertEqual(countdown(3600), "1:00:00")
        self.assertEqual(countdown(-5), "0:00")
        self.assertEqual(timer_name({"duration": 600}), "10-minute timer")
        self.assertEqual(timer_name({"duration": 7200}), "2-hour timer")
        self.assertEqual(timer_name({"duration": 45}), "45-second timer")
        self.assertEqual(timer_name({"duration": None, "text": "Timer"}), "Timer")
        now = datetime(2026, 11, 24, 19, 5, tzinfo=SYDNEY)
        self.assertEqual(when(datetime(2026, 11, 24, 21, 0, tzinfo=SYDNEY), now), "at 9:00 pm")
        self.assertEqual(when(datetime(2026, 12, 25, 7, 30, tzinfo=SYDNEY), now), "on 25 December at 7:30 am")

    def test_a_finished_timer_says_done(self):
        text = "\n".join(lines(render(NOW, {"entries": [{"kind": "timer", "text": "Timer", "due_at": NOW - 5,
                                                          "created_at": NOW - 65, "duration": 60}],
                                             "zone": SYDNEY, "zone_known": True})))
        self.assertIn("1-minute timer          done", text)

    def test_the_next_alarm_is_shown_and_always_fits(self):
        store = self.store()
        store.add("alarm", NOW + 12 * 3600, kind="alarm", now=NOW)
        store.add("alarm", NOW + 36 * 3600, kind="alarm", now=NOW)
        reader = Reader(self.mind)
        reader.zone, reader.zone_known, reader._zone_at = SYDNEY, True, time.monotonic()
        text = "\n".join(lines(render(NOW, reader.read(NOW))))
        self.assertIn("Next alarm      tomorrow at 7:05 am", text)
        self.assertEqual(text.count("Next alarm"), 1, "only the soonest")
        self.assertNotIn("No timers or reminders", text)
        # A full clock: four timers and three reminders still leave the alarm its row.
        for minutes in range(1, 9):
            store.add("Timer", NOW + minutes * 60, kind="timer", duration=minutes * 60, now=NOW)
            store.add(f"reminder number {minutes}", NOW + minutes * 3600, now=NOW)
        rows = lines(render(NOW, reader.read(NOW)))
        self.assertEqual(len(rows), app_clock.ROWS)
        self.assertTrue(any(row.strip().startswith("Next alarm") for row in rows), rows)
        self.assertTrue(any("Next reminder" in row for row in rows), "the next reminder stays too")

    def test_every_row_fits_and_a_frame_is_quick(self):
        store = self.store()
        for minutes in range(1, 9):
            store.add("Timer", NOW + minutes * 60, kind="timer", duration=minutes * 60, now=NOW)
            store.add(f"reminder number {minutes}", NOW + minutes * 3600, now=NOW)
        reader = Reader(self.mind)
        data = reader.read(NOW)
        frame = render(NOW, data)
        rows = lines(frame)
        self.assertEqual(len(rows), app_clock.ROWS)
        self.assertTrue(all(len(row) <= app_clock.COLUMNS for row in rows), max(map(len, rows)))
        self.assertNotIn("\n", frame)
        runs = []
        for _ in range(50):
            started = time.perf_counter()
            render(NOW, reader.read(NOW))
            runs.append((time.perf_counter() - started) * 1000)
        runs.sort()
        self.assertLessEqual(runs[len(runs) // 2], 5.0, runs)


if __name__ == "__main__":
    unittest.main()
