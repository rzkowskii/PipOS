import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mind import dream_journal
from mind.dream_store import DreamStore
from mind.memory import Memory
from mind.profile import Profile, save_profile


class DreamJournalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mind = self.root / "mind with spaces"
        self.config = self.root / "etc"
        self.config.mkdir()
        (self.config / "being.json").write_text(json.dumps({"owner": "Factory owner"}))
        self.args = ["--mind-dir", str(self.mind), "--config", str(self.config)]

    def open_memory(self):
        memory = Memory(self.mind)
        self.addCleanup(memory.close)
        return memory

    def publish(self, store, owner):
        batch = store.stage(owner, limit=16)
        self.assertIsNotNone(batch)
        self.assertTrue(store.publish(batch))

    def read_cli(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-m", "mind.dream_journal", *self.args],
                                cwd=root, capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_absent_archive_is_not_created(self):
        self.assertEqual("No dream journal entries yet.\n", self.read_cli())
        self.assertFalse(self.mind.exists())

    def test_pre_dream_archive_is_read_only(self):
        self.mind.mkdir()
        path = self.mind / "mind.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, text TEXT)")
            db.execute("INSERT INTO episodes (text) VALUES ('legacy archive contents')")
        before = path.read_bytes()
        self.assertEqual("No dream journal entries yet.\n", self.read_cli())
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(["mind.db"], sorted(p.name for p in self.mind.iterdir()))

    def test_another_local_account_cannot_read_the_archive(self):
        self.open_memory()
        archive_owner = (self.mind / "mind.db").stat().st_uid
        with patch("mind.dream_journal.os.geteuid", return_value=archive_owner + 1), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(1, dream_journal.main(self.args))
        self.assertIn("local archive owner", error.getvalue())

    def test_cli_reads_only_the_profile_owner_sources_and_suppresses_edits(self):
        memory = self.open_memory()
        save_profile(self.mind, Profile(owner="Alex"))
        (self.mind / "skills.json").write_text(json.dumps({"time_zone": "UTC"}))
        owner_id = memory.log("heard", "Moved the orchids to the sunny window.", "Alex")
        memory.log("heard", "Left the guest bicycle behind the garage.", "Factory owner")
        timestamp = datetime(2026, 11, 20, 21, 35, tzinfo=timezone.utc).timestamp()
        with memory.db:
            memory.db.execute("UPDATE episodes SET ts = ? WHERE id = ?", (timestamp, owner_id))
        store = DreamStore(memory)
        self.publish(store, "Factory owner")
        self.publish(store, "Alex")

        result = self.read_cli()
        self.assertIn(f"Episode {owner_id} · heard 2026-11-20 21:35", result)
        self.assertIn("Moved the orchids to the sunny window.", result)
        self.assertNotIn("guest bicycle", result)

        memory.edit(episode_ids=[owner_id], replacement="Moved the orchids to the east window.")
        self.assertEqual("No dream journal entries yet.\n", self.read_cli())
        self.publish(store, "Alex")
        self.assertIn("Moved the orchids to the east window.", self.read_cli())
        memory.edit(episode_ids=[owner_id])
        self.assertEqual("No dream journal entries yet.\n", self.read_cli())

    def test_terminal_output_is_bounded_and_strips_controls(self):
        memory = self.open_memory()
        memory.log("heard", "Moved the orchids.\n\r\x1b[31m\u202E" + " remembered details" * 50, "Factory owner")
        self.publish(DreamStore(memory), "Factory owner")
        excerpt = self.read_cli().splitlines()[-1].strip()
        self.assertTrue(excerpt.startswith("Moved the orchids. "))
        self.assertTrue(excerpt.endswith("…"))
        self.assertTrue(excerpt.isprintable())
        self.assertLessEqual(len(excerpt), 240)
