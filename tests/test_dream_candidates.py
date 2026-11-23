"""Dream candidates: kept apart from facts, promoted only on recurrence or
the owner's yes, and forgotten with everything they rest on."""

import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from mind import diary, dream_journal, dream_store
from mind.dream_verify import Line, check
from mind.memory import Memory


def checked(memory, episode_id, claim, quote, kind="fact"):
    text = memory.db.execute("SELECT text FROM episodes WHERE id = ?", (episode_id,)).fetchone()[0]
    verdict = check({"kind": kind, "claim": claim, "quote": quote, "episode_id": f"E{episode_id}",
                     "speaker": "owner", "modality": "stated", "time_ref": ""},
                    {episode_id: Line(episode_id, "owner", text)})
    assert verdict.ok, verdict.reason
    return verdict.item


def said_days_ago(memory, episode_id, days):
    """Recurrence counts days the owner said it on, so a fixture's first
    sentence is said earlier than the one that recurs."""
    with memory.db:
        memory.db.execute("UPDATE episodes SET ts = ts - ? WHERE id = ?", (days * 86400, episode_id))


class Candidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name), embedder=None)
        self.orchid = self.memory.log("heard", "My orchid is called Silverleaf and it loves the sun", "Richard")
        self.again = self.memory.log("heard", "Silverleaf, my orchid, is flowering again", "Richard")
        said_days_ago(self.memory, self.orchid, 1)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def save(self, episode, quote, night, claim="The owner's orchid is called Silverleaf."):
        return dream_store.save_candidates(self.memory, "Richard", [checked(self.memory, episode, claim, quote)],
                                           night=night, through=episode)

    def test_a_candidate_is_not_a_fact(self):
        self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-22")
        self.assertEqual(self.memory.facts(), [])
        found = dream_store.candidates(self.memory.db, "Richard")
        self.assertEqual([c.status for c in found], ["candidate"])
        self.assertEqual(found[0].quotes, ((self.orchid, "My orchid is called Silverleaf"),))
        self.assertEqual(dream_store.dream_cursor(self.memory, "Richard"), self.orchid)

    def test_the_same_sentence_on_the_same_night_does_not_promote(self):
        self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-22")
        self.assertEqual(self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-22"), [])
        self.assertEqual(self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-23"), [])
        self.assertEqual(self.memory.facts(), [])

    def test_recurring_from_another_sentence_on_a_later_night_promotes(self):
        self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-22")
        promoted = self.save(self.again, "Silverleaf, my orchid", "2026-11-23")
        self.assertEqual(len(promoted), 1)
        facts = self.memory.facts()
        self.assertEqual([f.text for f in facts], ["The owner's orchid is called Silverleaf."])
        self.assertLess(facts[0].confidence, 1.0)
        tellings = {row[0] for row in self.memory.db.execute("SELECT episode_id FROM fact_tellings")}
        self.assertEqual(tellings, {self.orchid, self.again})

    def test_the_same_claim_in_other_words_still_recurs(self):
        self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-22")
        promoted = self.save(self.again, "Silverleaf, my orchid", "2026-11-23",
                             claim="Silverleaf is the name of the owner's orchid.")
        self.assertEqual(len(promoted), 1)
        self.assertEqual([f.text for f in self.memory.facts()], ["The owner's orchid is called Silverleaf."])

    def test_the_owner_can_confirm_a_candidate(self):
        self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-22")
        candidate = dream_store.candidates(self.memory.db, "Richard")[0]
        fact = dream_store.confirm_candidate(self.memory, candidate.id, "Richard")
        self.assertIsNotNone(fact)
        self.assertEqual(dream_store.candidates(self.memory.db, "Richard")[0].status, "promoted")
        self.assertIsNone(dream_store.confirm_candidate(self.memory, candidate.id, "Richard"))
        self.assertIsNone(dream_store.confirm_candidate(self.memory, candidate.id, "Sam"))

    def test_a_fact_the_owner_already_told_stays_theirs(self):
        told = self.memory.remember("The owner's orchid is called Silverleaf.", "Richard")
        self.save(self.orchid, "My orchid is called Silverleaf", "2026-11-22")
        self.save(self.again, "Silverleaf, my orchid", "2026-11-23")
        self.memory.edit(episode_ids=[self.again])
        self.assertIsNotNone(self.memory.fact(told))

    def test_an_item_whose_sentence_went_while_it_was_read_is_dropped(self):
        item = checked(self.memory, self.orchid, "The owner's orchid is called Silverleaf.",
                       "My orchid is called Silverleaf")
        self.memory.edit(episode_ids=[self.orchid])
        dream_store.save_candidates(self.memory, "Richard", [item], night="2026-11-22")
        self.assertEqual(self.memory.db.execute("SELECT count(*) FROM candidate_facts").fetchone()[0], 0)


class Forgetting(unittest.TestCase):
    """BEMULA's categories: direct, indirect and inferential."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = Memory(self.root, embedder=None)
        self.first = self.memory.log("heard", "My orchid is called Silverleaf and it loves the sun", "Richard")
        self.second = self.memory.log("heard", "Silverleaf, my orchid, is flowering again", "Richard")
        self.other = self.memory.log("heard", "My sister Anna is visiting on Saturday", "Richard")
        said_days_ago(self.memory, self.first, 1)
        dream_store.save_candidates(self.memory, "Richard", [checked(
            self.memory, self.first, "The owner's orchid is called Silverleaf.", "My orchid is called Silverleaf")],
            night="2026-11-22")
        dream_store.save_candidates(self.memory, "Richard", [checked(
            self.memory, self.second, "The owner's orchid is called Silverleaf.", "Silverleaf, my orchid")],
            night="2026-11-23")
        dream_store.save_candidates(self.memory, "Richard", [checked(
            self.memory, self.other, "Anna is visiting on Saturday.", "My sister Anna is visiting on Saturday",
            kind="plan")], night="2026-11-23")

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def count(self, table):
        return self.memory.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_direct_forgetting_a_quoted_sentence_takes_the_candidate_and_its_fact(self):
        self.assertEqual(self.count("facts"), 1)
        change = self.memory.edit(episode_ids=[self.first])
        self.assertEqual(self.count("facts"), 0, "the promoted fact rested on that sentence")
        self.assertEqual([c.claim for c in dream_store.candidates(self.memory.db, "Richard")],
                         ["Anna is visiting on Saturday."])
        self.assertTrue(change.fact_ids, "the derived fact is counted, so its words are scrubbed")
        # As with a fact told twice, forgetting it takes every sentence it rests on.
        self.assertFalse(self.memory.contains("is flowering again"))
        self.assertTrue(self.memory.contains("Anna is visiting"))

    def test_indirect_forgetting_the_promoted_fact_by_topic_takes_everything(self):
        fact_ids = [fact.id for fact in self.memory.recall("silverleaf", limit=-1, all_terms=True)]
        episodes = self.memory.recall_history("silverleaf", limit=-1, all_terms=True, for_edit=True)
        self.memory.edit(fact_ids=fact_ids, episode_ids=[e.id for e in episodes])
        self.assertEqual(self.count("facts"), 0)
        self.assertEqual(self.count("candidate_sources"), 1)
        self.assertFalse(self.memory.contains("silverleaf"))
        self.memory.close()
        for path in self.root.iterdir():
            self.assertNotIn(b"silverleaf", path.read_bytes().lower(), path.name)
        self.memory = Memory(self.root, embedder=None)

    def test_inferential_anything_derived_from_a_forgotten_candidate_goes_too(self):
        orchid, anna = sorted(c.id for c in dream_store.candidates(self.memory.db, "Richard"))
        # A two-step inference: a theme from both, then a note from the theme.
        theme = self.memory.db.execute(
            "INSERT INTO candidate_facts (owner, kind, claim, claim_key, modality, first_night, last_night, created) "
            "VALUES ('Richard', 'event', 'A busy weekend at home.', 'a busy weekend at home', 'stated', "
            "'2026-11-23', '2026-11-23', 0)").lastrowid
        note = self.memory.db.execute(
            "INSERT INTO candidate_facts (owner, kind, claim, claim_key, modality, first_night, last_night, created) "
            "VALUES ('Richard', 'event', 'Ask how the weekend went.', 'ask how the weekend went', 'stated', "
            "'2026-11-23', '2026-11-23', 0)").lastrowid
        self.memory.db.commit()
        dream_store.derive(self.memory, theme, [("candidate", orchid), ("candidate", anna)])
        dream_store.derive(self.memory, note, [("candidate", theme)])
        self.memory.edit(episode_ids=[self.other])
        left = {row[0] for row in self.memory.db.execute("SELECT id FROM candidate_facts")}
        self.assertEqual(left, {orchid})
        self.assertEqual(self.count("derivations"), 0)

    def test_a_correction_keeps_the_owners_fact_and_drops_the_dreams_claim(self):
        fact = self.memory.facts()[0]
        self.memory.correct(fact.id, "my orchid is called Moonleaf")
        self.assertEqual(self.memory.fact(fact.id).text, "my orchid is called Moonleaf")


class CorrectingADreamFact(unittest.TestCase):
    """The owner's conversation the dream quoted is theirs: correcting the
    claim never rewrites what they actually said."""

    BISCUIT = "ugh Biscuit chewed my shoe again, my dog is called Biscuit and he is a menace"
    VET = "I took my dog Biscuit to the vet today"

    def setUp(self):
        self.memory = Memory(None, embedder=None)
        self.addCleanup(self.memory.close)
        self.chewed = self.memory.log("heard", self.BISCUIT, "Richard")
        self.vet = self.memory.log("heard", self.VET, "Richard")
        said_days_ago(self.memory, self.chewed, 1)
        claim = "The owner's dog is called Biscuit."
        for episode, quote, night in ((self.chewed, "my dog is called Biscuit", "2026-11-22"),
                                      (self.vet, "my dog Biscuit", "2026-11-23")):
            dream_store.save_candidates(self.memory, "Richard", [checked(self.memory, episode, claim, quote)],
                                        night=night)
        self.fact, = self.memory.facts()

    def episode(self, episode_id):
        return self.memory.db.execute("SELECT text, meta, speaker FROM episodes WHERE id = ?",
                                      (episode_id,)).fetchone()

    def test_the_quoted_sentences_keep_the_owners_words(self):
        before = {episode: self.episode(episode) for episode in (self.chewed, self.vet)}
        self.assertTrue(self.memory.correct(self.fact.id, "The owner's dog is called Rex."))
        self.assertEqual(self.memory.fact(self.fact.id).text, "The owner's dog is called Rex.")
        self.assertEqual({episode: self.episode(episode) for episode in before}, before)
        self.assertEqual([e.text for e in self.memory.recall_history("vet")], [self.VET])
        self.assertEqual(self.memory.recall_history("Rex"), [], "the owner never said Rex")

    def test_the_corrected_fact_no_longer_rests_on_the_dreams_quotes(self):
        self.memory.correct(self.fact.id, "The owner's dog is called Rex.")
        tellings = self.memory.db.execute("SELECT count(*) FROM fact_tellings WHERE fact_id = ?",
                                          (self.fact.id,)).fetchone()[0]
        self.assertEqual(tellings, 0)
        source, = self.memory.db.execute("SELECT source FROM facts WHERE id = ?", (self.fact.id,)).fetchone()
        self.assertIsNone(source)
        # Forgetting a quoted sentence now leaves the owner's corrected fact.
        self.memory.edit(episode_ids=[self.chewed])
        self.assertEqual(self.memory.fact(self.fact.id).text, "The owner's dog is called Rex.")
        self.assertEqual(self.episode(self.vet)[0], self.VET)

    def test_a_reply_that_recalled_the_old_claim_goes(self):
        reply = self.memory.log("said", "Biscuit sounds like a handful.", "Pip", recalled=[self.fact.id])
        change = self.memory.edit(fact_ids=[self.fact.id], replacement="The owner's dog is called Rex.")
        self.assertIn(reply, change.episode_ids)
        self.assertEqual(self.episode(reply)[0], "[forgotten]")
        self.assertNotIn(self.chewed, change.episode_ids)

    def test_a_retelling_by_command_is_corrected_with_the_fact(self):
        command = self.memory.log("heard", "remember that the owner's dog is called Biscuit", "Richard",
                                  command=True)
        self.assertEqual(self.memory.remember("The owner's dog is called Biscuit.", "Richard", command),
                         self.fact.id)
        self.memory.correct(self.fact.id, "The owner's dog is called Rex.")
        text, meta, _ = self.episode(command)
        self.assertEqual(text, "The owner's dog is called Rex.")
        self.assertIn("command", meta)
        self.assertEqual(self.episode(self.chewed)[0], self.BISCUIT)

    def test_correcting_a_quoted_sentence_drops_the_claim_drawn_from_it(self):
        change = self.memory.edit(episode_ids=[self.chewed], replacement="ugh my dog chewed my shoe again")
        self.assertEqual(self.episode(self.chewed)[0], "ugh my dog chewed my shoe again")
        self.assertEqual(self.episode(self.vet)[0], self.VET, "the other quote is not rewritten")
        self.assertEqual(self.memory.facts(), [], "the claim rested on the old words")
        self.assertIn(self.fact.id, change.fact_ids)


class ExpiringADreamFact(unittest.TestCase):
    """A promoted fact rests on every quote: the oldest one expiring takes
    it, visibly, with the replies that recalled it."""

    def test_expiry_of_the_oldest_quote_reports_the_fact_and_takes_its_replies(self):
        memory = Memory(None, embedder=None)
        self.addCleanup(memory.close)
        old = memory.log("heard", "My sister Ana is moving to Perth next month.", "Richard")
        new = memory.log("heard", "My sister Ana is moving to Perth soon.", "Richard")
        said_days_ago(memory, old, 40)
        claim = "The owner's sister Ana is moving to Perth."
        dream_store.save_candidates(memory, "Richard", [checked(memory, old, claim, "My sister Ana is moving to Perth")],
                                    night="2026-10-15")
        dream_store.save_candidates(memory, "Richard", [checked(memory, new, claim, "My sister Ana is moving to Perth")],
                                    night="2026-11-23")
        fact, = memory.facts()
        reply = memory.log("said", "Is Ana settling in Perth?", "Pip", recalled=[fact.id])

        change = memory.expire_before(time.time() - 30 * 86400)

        self.assertEqual(change.fact_ids, (fact.id,))
        self.assertEqual(change.fact_texts, (claim,))
        self.assertIn(reply, change.episode_ids)
        self.assertEqual(memory.facts(), [])
        self.assertEqual(memory.recall_history("settling"), [], "a reply made from the fact goes with it")
        self.assertEqual([e.id for e in memory.recall_history("Perth soon")], [new],
                         "the newer sentence is independent and stays until it ages out")


class SameEvening(unittest.TestCase):
    """Two sentences from one evening, read either side of midnight, are not
    a fact recurring on a later night."""

    def test_recurrence_counts_the_days_the_owner_said_it(self):
        from datetime import datetime, timedelta, timezone

        zone = timezone(timedelta(hours=11))
        memory = Memory(None, embedder=None)
        self.addCleanup(memory.close)

        def said(text, when):
            episode = memory.log("heard", text, "Richard")
            with memory.db:
                memory.db.execute("UPDATE episodes SET ts = ? WHERE id = ?", (when.timestamp(), episode))
            return episode

        evening = datetime(2026, 11, 22, 23, 30, tzinfo=zone)
        first = said("I started my new job at Acme this week", evening)
        second = said("the new job at Acme is going well", evening + timedelta(minutes=20))
        later = said("my new job at Acme had a long meeting today", evening + timedelta(days=1, hours=-4))
        claim = "The owner has a new job at Acme."

        def save(episode, quote, night):
            return dream_store.save_candidates(memory, "Richard", [checked(memory, episode, claim, quote)],
                                               night=night, zone=zone)

        self.assertEqual(save(first, "my new job at Acme", "2026-11-22"), [])
        self.assertEqual(save(second, "new job at Acme", "2026-11-23"), [], "the same evening, past midnight")
        self.assertEqual(memory.facts(), [])
        self.assertEqual(len(save(later, "my new job at Acme", "2026-11-24")), 1, "said again the next day")
        self.assertEqual([f.text for f in memory.facts()], [claim])


class Reports(unittest.TestCase):
    def setUp(self):
        self.memory = Memory(None, embedder=None)
        self.episode = self.memory.log("heard", "I think the plumber is coming on Thursday at nine", "Richard")

    def tearDown(self):
        self.memory.close()

    def test_the_morning_remark_quotes_only_checked_words(self):
        self.assertIsNone(dream_journal.morning_remark(self.memory.db, "Richard"))
        item = checked(self.memory, self.episode, "The plumber is coming on Thursday.",
                       "the plumber is coming on Thursday", kind="event")
        dream_store.save_candidates(self.memory, "Richard", [item], night="2026-11-23")
        remark = dream_journal.morning_remark(self.memory.db, "Richard")
        self.assertEqual(remark, 'I was thinking about when you said "the plumber is coming on Thursday". '
                                 "I might have that wrong.")
        self.assertNotIn("The plumber is coming on Thursday.", remark, "the model's own claim is never said")
        line, sources = dream_journal.dream_report(self.memory.db, "Richard")
        self.assertTrue(line.startswith("I went back over one moment"), line)
        self.assertIn('"the plumber is coming on Thursday"', line)
        self.assertEqual(sources, (self.episode,))

    def test_after_a_forget_there_is_nothing_to_remark_on(self):
        item = checked(self.memory, self.episode, "The plumber is coming on Thursday.",
                       "the plumber is coming on Thursday", kind="event")
        dream_store.save_candidates(self.memory, "Richard", [item], night="2026-11-23")
        self.memory.edit(episode_ids=[self.episode])
        self.assertIsNone(dream_journal.morning_remark(self.memory.db, "Richard"))
        line, _ = dream_journal.dream_report(self.memory.db, "Richard")
        self.assertFalse(line.startswith("I went back over"), line)

    def test_old_candidates_are_not_last_nights_dream(self):
        item = checked(self.memory, self.episode, "The plumber is coming on Thursday.",
                       "the plumber is coming on Thursday", kind="event")
        dream_store.save_candidates(self.memory, "Richard", [item], night="2026-11-20")
        self.assertIsNone(dream_journal.morning_remark(self.memory.db, "Richard", now=time.time() + 3 * 86400))


class TheWholeChain(unittest.TestCase):
    """Remember, recall, dream, diary, forget: nothing is left anywhere."""

    def test_forget_reaches_the_archive_search_vectors_candidates_journal_and_diary(self):
        import asyncio

        from mind import embed
        from mind.night_shift import JournalJob

        model = Path(__file__).resolve().parents[1] / "out" / "models" / "embeddings" / "blink-8M"
        embedder = embed.load(model) if (model / "model.safetensors").is_file() else None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = Memory(root, embedder=embedder)
            day = date.today()
            heard = memory.log("heard", "remember that my orchid Quixotica loves the sun", "Richard", command=True)
            fact = memory.remember("my orchid Quixotica loves the sun", "Richard", heard)
            chat = [memory.log("heard", f"Quixotica the orchid needs watering, day {i}", "Richard") for i in range(3)]
            memory.recall("what does my flower love")
            memory.index_pending(100)
            dream_store.save_candidates(memory, "Richard", [checked(
                memory, chat[0], "Quixotica the orchid needs watering.", "Quixotica the orchid needs watering")],
                night="2026-11-23")
            journal = JournalJob(memory, "Richard", "2026-11-23")
            for _ in range(50):
                if asyncio.run(journal.run_chunk(1.0)):
                    break
            self.assertGreater(journal.published, 0)
            diary.build_day(memory, "Richard", day)
            self.assertTrue(memory.db.execute("SELECT count(*) FROM diary_topics WHERE topic LIKE 'quixotica%'")
                            .fetchone()[0])

            found = memory.recall("quixotica", limit=-1, all_terms=True)
            episodes = memory.recall_history("quixotica", limit=-1, all_terms=True, for_edit=True)
            memory.edit(fact_ids=[f.id for f in found] + [fact], episode_ids=[e.id for e in episodes])

            db = memory.db
            self.assertFalse(memory.contains("quixotica"))
            for table in ("candidate_facts", "candidate_sources", "dream_journal", "fact_vec"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)
            self.assertEqual(db.execute("SELECT count(*) FROM episode_vec WHERE episode_id IN (?, ?, ?, ?)",
                                        (heard, *chat)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM diary_topics").fetchone()[0], 0)
            self.assertEqual(memory.recall_history("quixotica"), [])
            memory.close()
            for path in root.iterdir():
                self.assertNotIn(b"quixot", path.read_bytes().lower(), path.name)


if __name__ == "__main__":
    unittest.main()
