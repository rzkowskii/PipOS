import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from mind.memory import Memory, _SCHEMA
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


class DeletionLedgerTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)

    def open_memory(self, name):
        memory = Memory(self.root / name)
        self.addCleanup(memory.close)
        return memory

    def copy_snapshot(self, memory, name):
        snapshot = self.root / name
        snapshot.mkdir()
        with closing(sqlite3.connect(snapshot / "mind.db")) as copy:
            memory.db.backup(copy)
        return self.open_memory(name)

    def test_ledger_replays_corrections_and_provenance_without_copying_text(self):
        original = self.open_memory("original")
        source = original.log("heard", "The roses are called Silversong.", "Richard")
        fact = original.remember("The roses are called Silversong.", source=source)
        reply = original.log("said", "That is a beautiful name.", recalled=[fact])
        original.log("said", "I liked that too.", episodes=[reply])
        original.remember("The tulips bloom on Tuesday.")
        copy = self.copy_snapshot(original, "snapshot")

        original.correct(fact, "The roses are called Pearlmoon.")
        proof = original.deletion_ledger()
        self.assertEqual(len(proof["edits"]), 1)
        _, episodes, facts = proof["edits"][0]
        self.assertEqual(len(episodes), 3)
        self.assertEqual(len(facts), 1)
        self.assertNotIn("Silversong", str(proof))
        self.assertNotIn("Pearlmoon", str(proof))
        self.assertTrue(original.contains("Pearlmoon"))

        copy.apply_deletion_ledger(proof)
        self.assertFalse(copy.contains("Silversong"))
        self.assertFalse(copy.contains("beautiful"))
        self.assertFalse(copy.contains("liked"))
        self.assertFalse(copy.recall_history("Silversong"))
        self.assertEqual(len(copy.recall("tulips")), 1)
        self.assertEqual(copy.deletion_ledger(), proof)

    def test_legacy_migration_and_edit_trigger_follow_transactions(self):
        live = self.root / "live"
        live.mkdir()
        db = live / "mind.db"
        with closing(sqlite3.connect(db)) as legacy, legacy:
            legacy.executescript(_SCHEMA)
            legacy.execute("INSERT INTO episodes (ts, kind, text) VALUES (0, 'heard', 'Temporary text')")
            legacy.execute("INSERT INTO facts (ts, text, source) VALUES (0, 'Old legacy fact', 1)")
        memory = self.open_memory("live")
        snapshot = self.copy_snapshot(memory, "snapshot")

        with closing(sqlite3.connect(db)) as legacy, legacy:
            legacy.execute("DELETE FROM facts WHERE id = 1")
            legacy.rollback()
            legacy.execute("UPDATE episodes SET text = '[forgotten]', meta = NULL WHERE id = 1")
        intermediate = self.copy_snapshot(memory, "intermediate")
        proof = memory.deletion_ledger()
        self.assertEqual(len(proof["edits"]), 1)
        _, episodes, facts = proof["edits"][0]
        self.assertEqual(len(episodes), 1)
        self.assertEqual(facts, [])
        snapshot.apply_deletion_ledger(proof)
        self.assertFalse(snapshot.contains("Temporary text"))
        self.assertTrue(snapshot.contains("Old legacy fact"))

        with closing(sqlite3.connect(db)) as legacy, legacy:
            legacy.execute("UPDATE facts SET text = 'New legacy fact' WHERE id = 1")
        latest = memory.deletion_ledger()
        self.assertEqual(latest["edits"][:1], proof["edits"])
        intermediate.apply_deletion_ledger(latest)
        self.assertFalse(intermediate.contains("Old legacy fact"))

    def test_reused_fact_row_id_on_another_branch_remains_unrelated(self):
        original = self.open_memory("original")
        fork = self.copy_snapshot(original, "fork")
        fact = original.remember("Original branch")
        self.assertEqual(fact, fork.remember("Different branch"))
        original.forget([fact])
        proof = original.deletion_ledger()
        fork.apply_deletion_ledger(proof)
        self.assertTrue(fork.contains("Different branch"))

        fork.forget([fact])
        with self.assertRaisesRegex(ValueError, "does not extend"):
            fork.apply_deletion_ledger(proof)
        with self.assertRaisesRegex(ValueError, "Invalid deletion history"):
            original.apply_deletion_ledger({**proof, "edits": [["different branch", [], []]]})


if __name__ == "__main__":
    unittest.main()
