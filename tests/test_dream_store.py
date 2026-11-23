import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mind.dream_store import DreamStore, MAX_BATCH_ROWS, MAX_NIGHT_BATCHES, journal
from mind.memory import Memory


OWNER = "Richard"
NIGHT = "2026-11-21"


class DreamStorage(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory = Memory(self.root)
        self.addCleanup(lambda: self.memory.close())
        self.store = DreamStore(self.memory)

    def heard(self, text, speaker=OWNER, **meta):
        return self.memory.log("heard", text, speaker, **meta)

    def entries(self, owner=OWNER):
        return journal(self.memory.db, owner)

    def reopen(self):
        self.memory.close()
        self.memory = Memory(self.root)
        self.store = DreamStore(self.memory)

    def test_page_is_bounded_and_only_committed_progress_resumes(self):
        guest = self.heard("I painted the porch ultramarine.", "Guest")
        self.heard("The borrowed boat goes to South Dock.", memory_reply=True)
        self.memory.log("said", "The invented catch phrase was oranges.", "Pip")
        self.heard("Where did we leave the bicycle?")
        first = self.heard("I planted the mint beside the garage.")
        self.heard("I keep the spare drill in the pantry.")
        self.heard("I moved the basil to the sunny window.")
        last = self.heard("The lemon tree is in a terracotta pot.")

        staged = self.store.stage(OWNER, limit=7, night=NIGHT)
        self.assertEqual(staged.page, tuple(range(guest, last)))
        self.assertEqual(set(staged.source_ids), {first, first + 1, first + 2})
        self.assertEqual(staged.after, 0)
        self.assertEqual(self.entries(), [])
        self.reopen()
        self.assertEqual(self.store.stage(OWNER, limit=7, night=NIGHT), staged)
        self.assertTrue(self.store.publish(staged))
        self.assertFalse(self.store.publish(staged))

        self.reopen()
        next_page = self.store.stage(OWNER, limit=7, night=NIGHT)
        self.assertEqual(next_page.after, staged.through)
        self.assertEqual(next_page.page, (last,))
        self.assertEqual(next_page.night_batches, 1)
        self.assertTrue(self.store.publish(next_page))
        self.assertIsNone(self.store.stage(OWNER, night=NIGHT))
        self.assertEqual(self.entries("Guest"), [])
        self.assertEqual({entry.source.id for entry in self.entries()}, {first, first + 1, first + 2, last})

    def test_concurrent_correction_rejects_pending_batch_and_invalidates_only_its_link(self):
        orchids = self.heard("I moved the orchids into the study.")
        bays = self.heard("Our canoe is moored at Cedar Bay.")
        self.assertTrue(self.store.publish(self.store.stage(OWNER, night=NIGHT)))
        untouched = next(entry for entry in self.entries() if entry.source.id == bays)
        self.heard("The sewing kit is in the hallway cupboard.")
        pending = self.store.stage(OWNER, night=NIGHT)

        writer = Memory(self.root)
        self.addCleanup(writer.close)
        writer.edit(episode_ids=(orchids,), replacement="I moved the orchids into the conservatory.")
        self.assertFalse(self.store.publish(pending))
        self.assertEqual(self.entries(), [untouched])
        revised = self.store.stage(OWNER, night=NIGHT)
        self.assertEqual(revised.after, orchids - 1)
        self.assertEqual(revised.revision, pending.revision + 1)
        self.assertTrue(self.store.publish(revised))
        entry = next(row for row in self.entries() if row.source.id == orchids)
        self.assertEqual(entry.source.text, "I moved the orchids into the conservatory.")
        self.assertIn(untouched, self.entries())

        writer.edit(episode_ids=(orchids,))
        self.assertNotIn(orchids, {entry.source.id for entry in self.entries()})
        self.assertIn(untouched, self.entries())
        self.assertFalse(self.store.publish(revised))
        self.assertFalse(self.memory.contains("into the conservatory"))

    def test_two_workers_can_publish_a_checkpoint_only_once(self):
        self.heard("I finished sanding the writing desk.")
        competitor = Memory(self.root)
        self.addCleanup(competitor.close)
        other = DreamStore(competitor)
        first, second = self.store.stage(OWNER, night=NIGHT), other.stage(OWNER, night=NIGHT)
        self.assertEqual(first, second)
        self.assertTrue(other.publish(second))
        self.assertFalse(self.store.publish(first))
        self.assertEqual(len(self.entries()), 1)

    def test_publish_rechecks_canonical_source_and_page_bound(self):
        source = self.heard("I left the cello by the window.")
        staged = self.store.stage(OWNER)
        self.assertFalse(self.store.publish(replace(staged, page=tuple(range(1, MAX_BATCH_ROWS + 2)))))
        with self.memory.db:
            self.memory.db.execute("UPDATE episodes SET text = ? WHERE id = ?",
                                   ("I left the cello by the stairs.", source))
        self.assertEqual(self.memory.revision, staged.revision)
        self.assertFalse(self.store.publish(staged))
        self.assertEqual(self.entries(), [])
        self.assertTrue(self.store.publish(self.store.stage(OWNER)))
        self.assertEqual(self.entries()[0].source.text, "I left the cello by the stairs.")

    def test_lock_contention_is_immediate_and_restores_regular_timeout(self):
        self.heard("The blue tape measures three metres.")
        draft = self.store.stage(OWNER, night=NIGHT)
        competitor = Memory(self.root)
        self.addCleanup(competitor.close)
        self.memory.db.execute("PRAGMA busy_timeout = 3000")
        competitor.db.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as failure:
                self.store.publish(draft)
            self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(self.memory.db.execute("PRAGMA busy_timeout").fetchone(), (3000,))
            self.assertEqual(self.entries(), [])
        finally:
            competitor.db.rollback()
        self.assertTrue(self.store.publish(draft))

    def test_failed_checkpoint_rolls_back_links_and_leaves_the_batch_resumable(self):
        self.heard("The wood screws are in the red drawer.")
        first = self.store.stage(OWNER, night=NIGHT)
        self.memory.db.executescript(
            "CREATE TEMP TRIGGER reject_checkpoint BEFORE INSERT ON dream_checkpoints "
            "BEGIN SELECT RAISE(ABORT, 'synthetic interruption'); END;"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic interruption"):
            self.store.publish(first)
        self.assertEqual(self.entries(), [])
        self.reopen()
        self.assertEqual(self.store.stage(OWNER, night=NIGHT), first)
        self.assertTrue(self.store.publish(first))
        self.assertEqual(len(self.entries()), 1)

    def test_night_budget_survives_restart_and_ignores_clock_rollback(self):
        for number in range(MAX_NIGHT_BATCHES + 2):
            self.heard(f"The garden shelf holds seed tray {number}.")
        for count in range(MAX_NIGHT_BATCHES):
            draft = self.store.stage(OWNER, limit=1, night=NIGHT)
            self.assertEqual(draft.night_batches, count)
            self.assertTrue(self.store.publish(draft))
            self.reopen()
        self.assertIsNone(self.store.stage(OWNER, limit=1, night=NIGHT))
        self.assertIsNone(self.store.stage(OWNER, limit=1, night="2026-11-20"))
        self.assertEqual(len(self.entries()), MAX_NIGHT_BATCHES)
        future = self.store.stage(OWNER, limit=1, night="2026-11-22")
        self.assertEqual(future.night_batches, 0)
        self.assertTrue(self.store.publish(future))
        with self.assertRaises(ValueError):
            self.store.stage(OWNER, limit=MAX_BATCH_ROWS + 1)

    def test_reader_is_read_only_and_publication_retains_only_a_bounded_journal(self):
        reader = sqlite3.connect((self.root / "mind.db").as_uri() + "?mode=ro", uri=True)
        self.addCleanup(reader.close)
        with patch("mind.dream_store.JOURNAL_CAPACITY", 2):
            ids = [self.heard(f"I counted {count} blue marbles in the biscuit tin.") for count in (3, 4, 5)]
            for _ in ids:
                self.assertTrue(self.store.publish(self.store.stage(OWNER, limit=1)))
        self.assertEqual([entry.source.id for entry in journal(reader, OWNER)], ids[:0:-1])
        self.assertEqual(journal(reader, "Guest"), [])
        self.assertEqual(len(journal(reader, OWNER, limit=1)), 1)
        self.assertTrue(self.memory.contains("counted 3 blue marbles"))
        self.assertEqual(self.memory.db.execute("SELECT count(*) FROM dream_journal").fetchone(), (2,))
        old = sqlite3.connect(":memory:")
        self.addCleanup(old.close)
        old.execute("PRAGMA query_only=ON")
        self.assertEqual(journal(old, OWNER), [])

    def test_existing_archive_gets_dream_tables_without_losing_episodes_or_facts(self):
        source = self.heard("Remember that the rain barrel is beside the fence.")
        fact = self.memory.remember("the rain barrel is beside the fence", OWNER, source)
        self.memory.db.executescript("""
            DROP TRIGGER episodes_dream_update;
            DROP TRIGGER episodes_dream_insert;
            DROP TRIGGER episodes_dream_delete;
            DROP TABLE dream_journal;
            DROP TABLE dream_checkpoints;
        """)
        self.reopen()
        self.assertEqual(self.memory.recall("rain barrel")[0].id, fact)
        self.assertEqual(self.entries(), [])
        self.assertTrue(self.store.publish(self.store.stage(OWNER)))
        self.assertEqual(self.entries()[0].source.id, source)


if __name__ == "__main__":
    unittest.main()
