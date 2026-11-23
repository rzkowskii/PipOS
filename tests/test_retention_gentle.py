"""Gentle, opt-in expiry: whole local days, batches, only while resting (task 3.6)."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mind import retention
from mind.memory import Memory


class GentleExpiry(unittest.TestCase):
    def test_cutoff_is_a_local_midnight_in_the_owners_zone(self):
        sydney = ZoneInfo("Australia/Sydney")
        now = datetime(2026, 11, 23, 11, 0, tzinfo=sydney).timestamp()
        cutoff = retention.local_cutoff(now, 7, sydney)
        self.assertEqual(datetime.fromtimestamp(cutoff, sydney), datetime(2026, 11, 16, 0, 0, tzinfo=sydney))
        later = datetime(2026, 11, 23, 23, 59, tzinfo=sydney).timestamp()
        self.assertEqual(retention.local_cutoff(later, 7, sydney), cutoff, "one cutoff for the whole local day")
        self.assertIsNone(retention.local_cutoff(None, 7, sydney), "an unbelieved clock expires nothing")
        self.assertIsNone(retention.local_cutoff(now, None, sydney))

    def test_expiry_only_while_resting(self):
        self.assertTrue(retention.expiry_allowed("asleep"))
        self.assertTrue(retention.expiry_allowed("idle"))
        for state in ("attending", "listening", "thinking", "speaking"):
            self.assertFalse(retention.expiry_allowed(state))
        self.assertFalse(retention.expiry_allowed("idle", busy=True))

    def test_batches_expire_everything_old_and_stop_when_woken(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        memory = Memory(root)
        with memory.db:
            for index in range(450):
                memory.db.execute("INSERT INTO episodes (ts, kind, speaker, text) VALUES (?, 'heard', 'R', ?)",
                                  (1_700_000_000 + index, f"old line {index}"))
            memory.db.execute("INSERT INTO episodes (ts, kind, speaker, text) VALUES (?, 'heard', 'R', 'recent')",
                              (1_800_000_000,))
        steps = retention.expiry_steps(memory.db, 1_750_000_000, batch=200)
        self.assertEqual(len(steps), 3, steps)
        memory.close()

        calls = []
        edits = retention.expire_gently(root, 1_750_000_000, batch=200,
                                        keep_going=lambda: calls.append(1) or len(calls) < 2)
        self.assertEqual(len(edits), 1, "woken after the first batch")
        check = Memory(root)
        self.addCleanup(check.close)
        self.assertTrue(check.contains("old line 300"))
        self.assertFalse(check.contains("old line 0"))
        edits = retention.expire_gently(root, 1_750_000_000, batch=200)
        self.assertFalse(check.contains("old line 300"))
        self.assertTrue(check.contains("recent"))


if __name__ == "__main__":
    unittest.main()
