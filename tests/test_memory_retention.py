import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mind.memory import Memory


def believe_any_clock(case):
    """These tests stamp rows with tiny made-up times; take the clock at its word."""
    import time as _time
    from unittest.mock import patch as _patch
    from mind.memory import Memory as _Memory
    case.enterContext(_patch.object(_Memory, "clock", lambda self, reading=None: (_time.time() if reading is None else reading,) * 2))


class MemoryRetentionTests(unittest.TestCase):
    def test_volatile_memory_uses_no_sqlite_files_and_ends_at_close(self):
        with closing(Memory(None)) as memory:
            self.assertIsNone(memory.root)
            self.assertEqual(memory.db.execute("PRAGMA database_list").fetchall(), [(0, "main", "")])
            self.assertEqual(memory.db.execute("PRAGMA temp_store").fetchone(), (2,))
            episode = memory.log("heard", "My garden has a Rubygem magnolia.")
            memory.remember("My bicycle is emerald.", source=episode)
            self.assertTrue(memory.recall_history("Rubygem"))
            self.assertTrue(memory.recall("emerald"))

        with closing(Memory(None)) as next_session:
            self.assertEqual(next_session.recall_history("Rubygem"), [])
            self.assertEqual(next_session.recall("emerald"), [])

    def test_expires_original_facts_and_newer_provenance_without_hiding_independent_text(self):
        believe_any_clock(self)
        with closing(Memory(None)) as memory:
            with patch("mind.memory.time.time", return_value=10):
                life = memory.log("life", "First wake.", wake=1)
                source = memory.log("heard", "The garden lantern is obsidian.")
                fact = memory.remember("The garden lantern is obsidian.", source=source)
                standalone = memory.remember("The anise tea is violet.")
                private_metadata = memory.log("sense", "", note="went to Tadpole Cove")
            with patch("mind.memory.time.time", return_value=30):
                self.assertEqual(memory.remember("The anise tea is violet."), standalone)
                reply = memory.log("said", "I recall the lantern you showed me.", recalled=[fact])
                later_fact = memory.remember("The recalled lantern was ornate.", source=reply)
                transitive = memory.log("said", "It sounded memorable.", recalled=[later_fact])
                later_reply = memory.log("said", "That was helpful.", reply_to=transitive)
                independent = memory.log("heard", "The garden lantern is obsidian.")
            with patch("mind.memory.time.time", return_value=20):
                boundary = memory.remember("The campsite is the Oak Patch.")

            expiry = memory.expire_before(20)

            self.assertEqual(expiry.fact_ids, (fact, standalone, later_fact))
            self.assertEqual(expiry.episode_ids, (source, private_metadata, reply, transitive, later_reply))
            self.assertEqual(memory.fact(boundary).text, "The campsite is the Oak Patch.")
            self.assertEqual([row.id for row in memory.recall_history("obsidian")], [independent])
            self.assertEqual(memory.recall_history("helpful"), [])
            self.assertEqual(memory.recall("violet"), [])
            self.assertEqual(memory.db.execute("SELECT meta FROM episodes WHERE id = ?", (private_metadata,)).fetchone(), (None,))
            self.assertTrue(memory.woke(1))
            self.assertEqual(memory.db.execute("SELECT text FROM episodes WHERE id = ?", (life,)).fetchone(), ("First wake.",))
            self.assertEqual(memory.revision, 1)
            self.assertFalse(memory.expire_before(20))
            self.assertEqual(memory.revision, 1)
            proof = memory.deletion_ledger()
            self.assertEqual(len(proof["edits"]), 1)
            _, episodes, facts = proof["edits"][0]
            self.assertEqual((len(episodes), len(facts)), (5, 3))
            self.assertNotIn("Tadpole Cove", str(proof))
            self.assertNotIn("obsidian", str(proof))

    def test_snapshot_filter_preserves_accepted_deletion_lineage(self):
        believe_any_clock(self)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "staged"
            path.mkdir()
            with closing(Memory(None)) as source:
                with patch("mind.memory.time.time", return_value=10):
                    fact = source.remember("The sparrow is called Slate.")
                with closing(sqlite3.connect(path / "mind.db")) as copy:
                    source.db.backup(copy)
                checkpoint = source.deletion_ledger()

                with closing(Memory(path)) as staged:
                    self.assertTrue(staged.expire_before(20, snapshot=True))
                    self.assertFalse(staged.contains("Slate"))
                    self.assertEqual(staged.deletion_ledger(), checkpoint)
                    self.assertEqual(staged.revision, source.revision)
                    self.assertTrue(source.contains("Slate"))

                    self.assertEqual(source.expire_before(20).fact_ids, (fact,))
                    staged.apply_deletion_ledger(source.deletion_ledger())
                    self.assertEqual(staged.deletion_ledger(), source.deletion_ledger())
                    self.assertFalse(staged.contains("Slate"))
