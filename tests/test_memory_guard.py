"""Search-index upkeep, the disk guard and the lost clock."""

import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mind import memory as memory_module
from mind.memory import Memory


def statvfs(free_fraction, total_blocks=1_000_000, block=4096):
    return os.statvfs_result((block, block, total_blocks, int(total_blocks * free_fraction),
                              int(total_blocks * free_fraction), 0, 0, 0, 0, 255))


class SchemaGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "mind.db"

    def test_reject_incompatible_or_malformed_stamps_before_writing(self):
        for version, error, message in (("0", RuntimeError, "schema is version 0"),
                                        ("200", RuntimeError, "schema is version 200"),
                                        ("unknown", ValueError, "schema version is invalid")):
            with self.subTest(version=version):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    db.execute("INSERT INTO meta VALUES ('schema', ?)", (version,))
                before = self.path.read_bytes()
                with self.assertRaisesRegex(error, message):
                    Memory(self.root, embedder=None)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(set(self.root.iterdir()), {self.path})
                self.path.unlink()

    def test_unstamped_old_archive_keeps_the_original_initialisation_path(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO meta VALUES ('unrelated', 'preserve')")
        with closing(Memory(self.root, embedder=None)) as archive:
            self.assertEqual(memory_module.archive_schema(archive.db), memory_module.SCHEMA_VERSION)
            self.assertEqual(archive.db.execute("SELECT value FROM meta WHERE key='unrelated'").fetchone(), ("preserve",))


class IncrementalSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Start from a roomy disk whatever the dev machine has; tests that
        # are about the guard patch their own reading over this one.
        self.enterContext(patch("mind.memory.os.statvfs", return_value=statvfs(0.5)))
        self.memory = Memory(self.root, embedder=None)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def test_secure_delete_is_on_for_both_indexes(self):
        self.assertTrue(self.memory._fts_secure)
        for table in ("episodes_fts", "facts_fts"):
            value = self.memory.db.execute(
                f"SELECT v FROM {table}_config WHERE k = 'secure-delete'").fetchone()
            self.assertEqual(value, (1,))

    def test_a_forget_at_ten_thousand_rows_takes_at_most_50_ms(self):
        words = "orchid garden kitchen sister birthday plumber thursday weather tomato bicycle".split()
        with self.memory.db:
            self.memory.db.executemany(
                "INSERT INTO episodes (ts, kind, speaker, text) VALUES (?, ?, 'Richard', ?)",
                ((1.78e9 + i, "heard" if i % 2 else "said", f"{words[i % 10]} {words[(i * 7) % 10]} note {i}")
                 for i in range(10_000)))
            self.memory.db.executemany(
                "INSERT INTO facts (ts, text, speaker) VALUES (?, ?, 'Richard')",
                ((1.78e9 + i, f"ordinary fact {i} about {words[i % 10]}") for i in range(500)))
        episode = self.memory.log("heard", "remember that the safe word is Quixotically", "Richard", command=True)
        fact = self.memory.remember("the safe word is Quixotically", "Richard", episode)
        start = time.perf_counter()
        self.memory.forget([fact])
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.050, f"{elapsed * 1000:.1f} ms")
        self.assertFalse(self.memory.contains("quixotically"))
        self.memory.close()
        for path in self.root.iterdir():
            self.assertNotIn(b"quixot", path.read_bytes().lower(), path.name)
        self.memory = Memory(self.root, embedder=None)

    def test_the_nightly_optimise_keeps_search_working(self):
        self.memory.log("heard", "The orchids are flowering", "Richard")
        self.memory.remember("my orchid is called Silverleaf", "Richard")
        self.memory.optimize_search()
        self.assertEqual(len(self.memory.recall_history("orchids")), 1)
        self.assertEqual(len(self.memory.recall("orchid")), 1)

    def test_event_time_is_opt_in_and_legacy_rows_fall_back_to_telling_time(self):
        event = self.memory.log("heard", "Yesterday I repaired the radio", "Richard",
                                event_start=100.0, event_end=200.0)
        legacy = self.memory.log("heard", "I cleaned the radio", "Richard")
        told = self.memory.db.execute("SELECT ts FROM episodes WHERE id = ?", (legacy,)).fetchone()[0]
        self.assertEqual([row.id for row in self.memory.recall_history(
            "radio", start=100.0, end=200.0, time_basis="event")], [event])
        self.assertEqual([row.id for row in self.memory.recall_history(
            "radio", start=told - 1.0, end=told + 1.0)], [legacy, event])
        self.assertIn(legacy, [row.id for row in self.memory.recall_history(
            "radio", start=told - 1.0, end=told + 1.0, time_basis="event")])


class DiskGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Start from a roomy disk whatever the dev machine has; tests that
        # are about the guard patch their own reading over this one.
        self.enterContext(patch("mind.memory.os.statvfs", return_value=statvfs(0.5)))
        self.memory = Memory(self.root, embedder=None)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def at(self, fraction):
        patcher = patch("mind.memory.os.statvfs", return_value=statvfs(fraction))
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.memory.disk_state(refresh=True)

    def test_states_follow_the_free_space(self):
        self.assertEqual(self.at(0.5)["state"], "ok")
        patch.stopall()
        self.assertEqual(self.at(0.09)["state"], "low")
        patch.stopall()
        reading = self.at(0.04)
        self.assertEqual(reading["state"], "critical")
        self.assertAlmostEqual(reading["free_fraction"], 0.04, places=3)
        self.assertGreater(reading["free_bytes"], 0)

    def test_low_disk_stops_archiving_conversation_but_keeps_commands_and_facts(self):
        self.at(0.08)
        self.assertIsNone(self.memory.log("heard", "I had a lovely walk today", "Richard"))
        self.assertIsNone(self.memory.log("said", "That sounds nice.", "Pip"))
        self.assertIsNotNone(self.memory.log("command", "recall", "Richard"))
        episode = self.memory.log("heard", "remember that my orchid is Silverleaf", "Richard", command=True)
        self.assertIsNotNone(episode)
        self.assertIsNotNone(self.memory.remember("my orchid is Silverleaf", "Richard", episode))
        self.assertIsNotNone(self.memory.log("life", "woke", "Pip", wake=1))

    def test_critical_disk_keeps_only_what_the_owner_asked_to_keep(self):
        self.at(0.03)
        self.assertIsNone(self.memory.log("command", "recall", "Richard"))
        self.assertIsNone(self.memory.log("heard", "I had a lovely walk today", "Richard"))
        episode = self.memory.log("heard", "remember that my orchid is Silverleaf", "Richard", command=True)
        self.assertIsNotNone(self.memory.remember("my orchid is Silverleaf", "Richard", episode))
        self.assertEqual([f.text for f in self.memory.recall("orchid")], ["my orchid is Silverleaf"])

    def test_the_reading_is_cached_between_checks(self):
        with patch("mind.memory.os.statvfs", return_value=statvfs(0.5)) as reading:
            self.memory.disk_state(refresh=True)
            for _ in range(50):
                self.memory.log("heard", "hello there", "Richard")
            self.assertEqual(reading.call_count, 1)

    def test_compaction_vacuums_only_with_room_for_the_copy(self):
        for i in range(200):
            self.memory.log("heard", f"sentence {i} about the garden", "Richard")
        with patch("mind.memory.os.statvfs", return_value=statvfs(0.08, total_blocks=1000)), \
                patch.object(self.memory, "_vacuum") as vacuum:
            self.memory.compact()
            vacuum.assert_not_called()
        with patch("mind.memory.os.statvfs", return_value=statvfs(0.5)), \
                patch.object(self.memory, "_vacuum", wraps=self.memory._vacuum) as vacuum:
            self.memory.compact()
            vacuum.assert_called_once()
        self.assertEqual(len(self.memory.recall_history("garden", limit=3)), 3)

    def test_an_archive_in_ram_is_never_full(self):
        self.assertEqual(Memory(None, embedder=None).disk_state()["state"], "ok")


class LostClock(unittest.TestCase):
    """The first real boot: the clock battery was dead, the RTC read
    2022-01-03 and systemd moved the clock to its 2026-10-17 epoch."""

    EPOCH = 1_792_195_200.0  # 2026-10-17 UTC
    REAL = 1_795_392_000.0  # 2026-11-23 UTC

    def setUp(self):
        from mind import clock_epoch as real

        class Fake:
            """The real clock_epoch, except for the one question under test."""
            epoch_advanced = staticmethod(lambda: True)

            def __getattr__(self, name):
                return getattr(real, name)

        self.fake = Fake()
        # Memory binds the module at import time, so patch it where it is used.
        patcher = patch.object(memory_module, "clock_epoch", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.memory = Memory(None, embedder=None)

    def test_nothing_may_expire_while_the_clock_is_lost(self):
        sure, stamp = self.memory.clock(self.EPOCH)
        self.assertIsNone(sure)
        self.assertEqual(stamp, self.EPOCH)

    def test_stamps_never_go_back_and_are_put_right_when_the_time_is_set(self):
        self.fake.epoch_advanced = lambda: False
        self.memory.clock(self.REAL)
        before = self.memory.log("heard", "said on the real day", "Richard")
        self.fake.epoch_advanced = lambda: True
        self.memory._lost_cache = None
        with patch("mind.memory.time.time", return_value=self.EPOCH):
            after = self.memory.log("heard", "said with the lost clock", "Richard")
        stamps = dict(self.memory.db.execute("SELECT id, ts FROM episodes"))
        self.assertGreaterEqual(stamps[after], stamps[before])
        self.assertEqual(self.memory.clock(self.EPOCH)[0], None)
        # The owner says what time it is: believed from then on.
        sure, _ = self.memory.owner_set_clock(self.REAL + 3600)
        self.assertFalse(self.memory.clock_lost())
        self.assertIsNotNone(self.memory.clock(self.REAL + 3700)[0])

    def test_an_unreadable_epoch_is_not_a_reason_to_doubt(self):
        def broken():
            raise OSError("no rtc")
        self.fake.epoch_advanced = broken
        self.memory._lost_cache = None
        self.assertFalse(self.memory.clock_lost())

    def test_the_answer_is_cached(self):
        calls = []
        self.fake.epoch_advanced = lambda: calls.append(1) or True
        self.memory._lost_cache = None
        for _ in range(20):
            self.memory.clock(self.REAL)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
