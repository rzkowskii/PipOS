import tempfile
import unittest
from unittest.mock import patch

from mind.dream_store import DreamStore
from mind.memory import Memory


def believe_any_clock(case):
    """These tests stamp rows with tiny made-up times; take the clock at its word."""
    import time as _time
    from unittest.mock import patch as _patch
    from mind.memory import Memory as _Memory
    case.enterContext(_patch.object(_Memory, "clock", lambda self, reading=None: (_time.time() if reading is None else reading,) * 2))


class DreamRetrievalTests(unittest.TestCase):
    owner = "Richard"
    topic = "amber trail"
    padding = " There was a humming refrigerator near the entrance."

    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.memory = Memory(root)
        self.addCleanup(self.memory.close)
        self.store = DreamStore(self.memory)

    def archive(self):
        # Amber is rare and repeats in a short distractor. The complete
        # source is a detail from a much longer owner utterance.
        with patch("mind.memory.time.time", return_value=10):
            source = self.memory.log(
                "heard", "The amber trail map is on the cedar shelf." + self.padding * 30, self.owner,
            )
        with patch("mind.memory.time.time", return_value=20):
            partial = self.memory.log("heard", "Amber labels amber stickers amber sheets.", self.owner)
            for number in range(8):
                self.memory.log("heard", f"The trail signpost is number {number}.", "Visitor")
        self.assertEqual(self.match(), partial)
        self.assertEqual(self.match(all_terms=True), source)
        return source, partial

    def publish(self):
        self.assertTrue(self.store.publish(self.store.stage(self.owner)))

    def match(self, **filters):
        rows = self.memory.recall_history(self.topic, limit=1, **filters)
        return rows[0].id if rows else None

    def test_published_complete_utterance_displaces_a_partial_match(self):
        source, partial = self.archive()
        self.assertEqual(self.match(journal_owner=self.owner), partial)

        self.publish()

        self.assertEqual(self.match(), partial)
        self.assertEqual(self.match(journal_owner=self.owner), source)
        self.assertEqual(self.match(journal_owner="Visitor"), partial)

    def test_stronger_unlinked_full_match_blocks_journal_promotion(self):
        _, partial = self.archive()
        self.publish()
        unlinked = self.memory.log(
            "heard", "The amber trail map is in the hallway cabinet." + self.padding * 10, self.owner,
        )

        self.assertEqual(self.match(all_terms=True), unlinked)
        self.assertEqual(self.match(), partial)
        self.assertEqual(self.match(journal_owner=self.owner), partial)

    def test_preserves_an_original_complete_match(self):
        self.memory.log("heard", "The amber trail map was on the cedar shelf." + self.padding * 30, self.owner)
        self.publish()
        newer = self.memory.log("heard", "The amber trail map is on the desk.", self.owner)

        self.assertEqual(self.match(), newer)
        self.assertEqual(self.match(all_terms=True), newer)
        self.assertEqual(self.match(journal_owner=self.owner), newer)

    def test_journal_respects_dates_kind_and_fact_source_filters(self):
        believe_any_clock(self)
        source, partial = self.archive()
        self.publish()
        self.assertEqual(self.match(journal_owner=self.owner, start=20), partial)
        self.assertEqual(self.match(journal_owner=self.owner, end=20, kind="heard"), source)

        said = self.memory.log("said", "Amber sticky labels are handy.", "Pip")
        self.assertEqual(self.match(journal_owner=self.owner, kind="said"), said)

        self.memory.remember("The trail map was moved.", self.owner, source=source)
        self.assertEqual(self.match(journal_owner=self.owner, include_fact_sources=False), partial)

    def test_edits_invalidate_link_and_republish_only_the_canonical_revision(self):
        source, partial = self.archive()
        self.publish()
        self.assertEqual(self.match(journal_owner=self.owner), source)

        self.memory.edit(episode_ids=[source], replacement="The amber trail map is on the coat rack." + self.padding * 30)
        self.assertEqual(self.match(journal_owner=self.owner), partial)
        self.publish()
        hit, = self.memory.recall_history(self.topic, limit=1, journal_owner=self.owner)
        self.assertEqual(hit.id, source)
        self.assertIn("coat rack", hit.text)
        self.assertNotIn("cedar shelf", hit.text)

        self.memory.edit(episode_ids=[source])
        self.assertEqual(self.match(journal_owner=self.owner), partial)
        self.assertEqual(self.memory.recall_history("coat rack", limit=1, journal_owner=self.owner), [])

    def test_skips_the_journal_for_baseline_and_single_word_queries(self):
        self.archive()
        self.publish()

        traced = []
        self.memory.db.set_trace_callback(traced.append)
        try:
            self.match()
            self.memory.recall_history("amber", limit=1, journal_owner=self.owner)
            self.match(journal_owner=self.owner, all_terms=True)
            self.match(journal_owner=self.owner, for_edit=True)
        finally:
            self.memory.db.set_trace_callback(None)
        self.assertFalse(any("dream_journal" in statement for statement in traced))
