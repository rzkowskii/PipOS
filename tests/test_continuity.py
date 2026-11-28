"""Conversation bookmarks share the archive's source and deletion proof."""

from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mind import commands, continuity
from mind.memory import Memory
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.memory = self.enterContext(closing(Memory(self.root, embedder=None)))
        self.owner = "Richard"

    def test_bookmark_uses_the_latest_owner_conversation_and_survives_reopen(self):
        source = self.memory.log("heard", "I'd like to finish the greenhouse", self.owner)
        self.memory.log("heard", "Hello Pip", self.owner, greeting=True)
        self.memory.log("heard", "What time is it?", self.owner, command=True)
        self.memory.log("heard", "Guest words", "Guest")
        thread = continuity.bookmark(self.memory, self.owner)
        self.assertEqual(thread.source, source)
        with closing(Memory(self.root, embedder=None)) as reopened:
            self.assertEqual(continuity.active(reopened, self.owner), thread)
            self.assertIsNone(continuity.active(reopened, "Guest"))
            self.assertEqual(continuity.close(reopened, self.owner), thread)
            self.assertIsNone(continuity.active(reopened, self.owner))
            self.assertEqual(reopened.owner_lines(self.owner, limit=1)[0].id, source)

    def test_source_correction_and_forgetting_revoke_the_bookmark(self):
        source = self.memory.log("heard", "I'm repairing the greenhouse", self.owner)
        continuity.bookmark(self.memory, self.owner)
        self.memory.edit(episode_ids=(source,), replacement="I'm repairing the shed")
        self.assertIsNone(continuity.active(self.memory, self.owner))
        thread = continuity.bookmark(self.memory, self.owner)
        self.assertEqual(thread.text, "I'm repairing the shed")
        self.memory.edit(episode_ids=(source,))
        self.assertIsNone(continuity.active(self.memory, self.owner))

    def test_closing_a_thread_survives_restore_without_forgetting_the_source(self):
        source = self.memory.log("heard", "I'd like to finish the greenhouse", self.owner)
        continuity.bookmark(self.memory, self.owner)
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        with closing(sqlite3.connect(snapshot / "mind.db")) as copied:
            self.memory.db.backup(copied)
        continuity.close(self.memory, self.owner)
        with closing(Memory(snapshot, embedder=None)) as recovered:
            recovered.apply_deletion_ledger(self.memory.deletion_ledger())
            self.assertIsNone(continuity.active(recovered, self.owner))
            self.assertEqual(recovered.owner_lines(self.owner, limit=1)[0].id, source)

    def test_dated_followup_can_only_be_claimed_once_after_the_day(self):
        today = date(2027, 1, 1)
        line = "I have a dentist appointment tomorrow"
        start, end = commands.event_range(line, today=today, zone=timezone.utc)
        self.assertEqual(datetime.fromtimestamp(start, timezone.utc).date(), date(2027, 1, 2))
        self.assertEqual(end - start, 86400)
        self.assertIsNone(commands.event_range(line, today=today, zone=timezone.utc,
                                               relative_allowed=lambda: False))
        with patch.object(self.memory, "now", return_value=start - 3600):
            self.memory.log("heard", line, self.owner, event_start=start, event_end=end)
            thread = continuity.bookmark(self.memory, self.owner)
        self.assertIsNone(continuity.claim_followup(self.memory, self.owner, end - 1))
        self.assertEqual(continuity.claim_followup(self.memory, self.owner, end), thread)
        self.assertIsNone(continuity.claim_followup(self.memory, self.owner, end + 365 * 86400))
