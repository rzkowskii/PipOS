import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind.consolidation import candidate_highlights, select_highlights, validate_selection
from mind.memory import Memory, search_terms


def believe_any_clock(case):
    """These tests stamp rows with tiny made-up times; take the clock at its word."""
    import time as _time
    from unittest.mock import patch as _patch
    from mind.memory import Memory as _Memory
    case.enterContext(_patch.object(_Memory, "clock", lambda self, reading=None: (_time.time() if reading is None else reading,) * 2))


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.memory = Memory(self.root)

    def tearDown(self):
        self.memory.close()
        self.dir.cleanup()

    def test_search_terms_drop_filler(self):
        self.assertEqual(search_terms("What do you remember about my sister's birthday?"), ["sister", "birthday"])
        self.assertEqual(search_terms("what is it"), [])

    def test_remember_then_recall(self):
        self.memory.remember("my cat is called Tom", "Richard")
        self.memory.remember("the bins go out on Tuesday", "Richard")
        found = self.memory.recall("tell me about my cat")
        self.assertEqual([f.text for f in found], ["my cat is called Tom"])
        self.assertEqual(found[0].speaker, "Richard")
        # Word endings do not have to match exactly.
        self.assertEqual(len(self.memory.recall("cats")), 1)
        self.assertEqual(self.memory.recall("the weather"), [])

    def test_telling_it_twice_keeps_one_fact(self):
        a = self.memory.remember("my cat is called Tom")
        b = self.memory.remember("My cat is called  Tom")
        self.assertEqual(a, b)
        self.assertEqual(len(self.memory.facts()), 1)

    def test_all_terms_is_stricter(self):
        self.memory.remember("my birthday is in May")
        self.memory.remember("my sister's birthday is in June")
        self.assertEqual(len(self.memory.recall("sister birthday")), 2)
        strict = self.memory.recall("sister birthday", all_terms=True)
        self.assertEqual([f.text for f in strict], ["my sister's birthday is in June"])

    def test_correct_changes_what_is_recalled(self):
        fact = self.memory.remember("my cat is called Tom")
        self.assertTrue(self.memory.correct(fact, "my cat is called Tim"))
        self.assertEqual(self.memory.recall("cat")[0].text, "my cat is called Tim")
        self.assertEqual(self.memory.recall("Tom"), [])
        self.assertFalse(self.memory.correct(9999, "nothing"))

    def test_forget_removes_the_fact_and_blanks_the_log(self):
        episode = self.memory.log("heard", "remember that my locker code is zebra-7741", "Richard")
        fact = self.memory.remember("my locker code is zebra-7741", "Richard", episode)
        self.memory.log("said", "Okay. I'll remember that.", "Pip")
        self.assertTrue(self.memory.contains("zebra-7741"))

        self.assertEqual(self.memory.forget([fact]), ["my locker code is zebra-7741"])
        self.assertFalse(self.memory.contains("zebra-7741"))
        self.assertEqual(self.memory.recall("locker code"), [])
        # The log keeps its shape: the entry is still there, without the words.
        kinds = [(kind, text) for _, kind, _, text in self.memory.recent()]
        self.assertEqual(kinds[0], ("heard", "[forgotten]"))
        self.assertEqual(self.memory.forget([fact]), [], "forgetting twice is harmless")

    def test_forgetting_leaves_no_trace_in_the_files(self):
        for i in range(40):
            self.memory.remember(f"ordinary fact number {i} about gardening and weather")
        episode = self.memory.log("heard", "remember that the safe word is Quixotically", "Richard")
        fact = self.memory.remember("the safe word is Quixotically", "Richard", episode)
        self.memory.forget([fact])
        self.memory.close()
        for path in self.root.iterdir():
            data = path.read_bytes().lower()
            # "quixot" also covers the stemmed form the search index stores.
            self.assertNotIn(b"quixot", data, f"still present in {path.name}")
        self.memory = Memory(self.root)
        self.assertEqual(len(self.memory.facts()), 40)

    def test_memory_survives_reopening(self):
        self.memory.remember("my cat is called Tom", "Richard")
        self.memory.close()
        self.memory = Memory(self.root)
        self.assertEqual(self.memory.recall("cat")[0].text, "my cat is called Tom")

    def test_existing_conversations_are_backfilled_once(self):
        # Recreate the archive layout from before full-text episode search.
        self.memory.db.executescript("""
            DROP TRIGGER episodes_ai;
            DROP TRIGGER episodes_au;
            DROP TRIGGER episodes_ad;
            DROP TABLE episodes_fts;
            DELETE FROM meta WHERE key = 'episode_search';
        """)
        old = self.memory.log("heard", "We visited a café downtown", "Richard")
        for _ in range(2):
            self.memory.close()
            self.memory = Memory(self.root)
            self.assertEqual([episode.id for episode in self.memory.recall_history("cafe")], [old])
        new = self.memory.log("heard", "We passed another café", "Richard")
        self.assertEqual({episode.id for episode in self.memory.recall_history("cafe")}, {old, new})

    def test_history_search_filters_dates_and_command_echoes(self):
        believe_any_clock(self)
        with patch("mind.memory.time.time", side_effect=[1, 2, 3, 4, 5]):
            original = self.memory.log("heard", "I moved the orchids to the study", "Richard")
            reply = self.memory.log("said", "The room gets a lot of sun", "Pip", reply_to=original)
            self.memory.log("command", "What do you remember about orchids?", "Richard")
            self.memory.log("said", "You moved the orchids", "Pip", memory_reply=True)
            sibling = self.memory.log("heard", "The orchid near the kitchen looks healthy", "Richard")

        self.assertEqual([e.id for e in self.memory.recall_history("orchid", end=5)], [original])
        self.assertEqual([e.id for e in self.memory.recall_history("", start=4)], [sibling])
        self.assertEqual([e.id for e in self.memory.recall_history("orchids study", for_edit=True)], [original])
        found = self.memory.recall_history("orchid", end=5)[0]
        self.assertEqual(self.memory.history_neighbor(found).id, reply)
        found_reply = self.memory.recall_history("sun")[0]
        self.assertEqual(self.memory.history_neighbor(found_reply).id, original)

        self.memory.remember("I moved the orchids to the study", "Richard", original)
        self.assertEqual(self.memory.recall_history("orchid study", for_edit=True), [])
        self.assertEqual(
            [e.id for e in self.memory.recall_history("orchid study", end=5, for_edit=True)],
            [original],
        )

    def test_edit_history_clears_derived_replies_and_persists_after_reopen(self):
        original = self.memory.log("heard", "We keep the Quixotically card in the old box", "Richard")
        quoted = self.memory.log("said", "I remember the card is labelled Q-77", "Pip", episodes=[original])
        derived = self.memory.log("said", "I'll remember the seventies label", "Pip", episodes=[quoted])
        sibling = self.memory.log("heard", "The blue card is in the new box", "Richard")
        self.assertEqual(self.memory.revision, 0)

        change = self.memory.edit(episode_ids=[original], replacement="We keep the card in the dresser")
        self.assertEqual(change.episode_ids, (original, quoted, derived))
        self.assertEqual(change.fact_ids, ())
        self.assertEqual(self.memory.revision, 1)
        self.assertEqual(self.memory.recall_history("seventies"), [])
        self.assertEqual([e.id for e in self.memory.recall_history("blue card", all_terms=True)], [sibling])

        self.memory.close()
        for path in self.root.iterdir():
            data = path.read_bytes().lower()
            self.assertNotIn(b"quixot", data, f"stale text or search stem in {path.name}")
            self.assertNotIn(b"seventies", data, f"derived text in {path.name}")
        self.memory = Memory(self.root)
        self.assertEqual(self.memory.revision, 1)
        self.assertEqual([e.id for e in self.memory.recall_history("dresser")], [original])

        self.memory.edit(episode_ids=[original])
        self.assertEqual(self.memory.recall_history("dresser"), [])
        self.assertEqual(self.memory.revision, 2)
        self.assertFalse(self.memory.edit(episode_ids=[original]).texts)
        self.assertEqual(self.memory.revision, 2)

    def test_bounded_history_keeps_rank_ties_and_exact_excerpts_with_source_filters(self):
        believe_any_clock(self)
        text = (
            "The visitor took the road past the bridge and turned north at the blue gate "
            "before meeting an old friend outside the station where they went inside. "
            "The café lantern was lit early."
        )
        with patch("mind.memory.time.time", return_value=10):
            heard = [self.memory.log("heard", text, "Richard") for _ in range(9)]
            said = self.memory.log("said", text, "Pip")
            self.memory.log("said", text, "Pip", memory_reply=True)
        with patch("mind.memory.time.time", return_value=11):
            self.memory.log("heard", text, "Richard")
        self.memory.remember("This fact has no source episode", "Richard")
        self.memory.remember("First fact from a matching episode", "Richard", heard[-1])
        self.memory.remember("Second fact from the same episode", "Richard", heard[-1])

        filters = {"start": 10, "end": 11, "include_fact_sources": False, "all_terms": True}
        ranked = self.memory.recall_history("cafe lantern", limit=-1, **filters)
        self.assertEqual([episode.id for episode in ranked], heard[-2::-1] + [said])
        self.assertIn("café lantern", ranked[0].excerpt)
        self.assertNotEqual(ranked[0].text, ranked[0].excerpt)
        self.assertEqual(self.memory.recall_history("cafe lantern", limit=6, **filters), ranked[:6])

    def test_fact_correction_updates_historical_source_and_discards_reply(self):
        original = self.memory.log("heard", "remember that my cat is called Tom", "Richard")
        fact = self.memory.remember("my cat is called Tom", "Richard", original)
        reply = self.memory.log("said", "Tom must like the garden", "Pip", recalled=[fact])
        change = self.memory.edit(fact_ids=[fact], replacement="my cat is called Tim")
        self.assertEqual(change.episode_ids, (original, reply))
        self.assertEqual(self.memory.recall_history("Tom"), [])
        self.assertEqual([e.text for e in self.memory.recall_history("Tim")], ["my cat is called Tim"])
        self.assertEqual(self.memory.fact(fact).text, "my cat is called Tim")

    def test_forgetting_an_older_exchange_discards_the_adjacent_reply(self):
        believe_any_clock(self)
        with patch("mind.memory.time.time", side_effect=[10, 12, 13, 200]):
            original = self.memory.log("heard", "I named the fern Augustus", "Richard")
            reply = self.memory.log("said", "Gus would be a good nickname", "Pip")
            separate = self.memory.log("heard", "The rosemary needs more light", "Richard")
            unlinked = self.memory.log("said", "I wonder whether it will rain", "Pip")

        self.assertEqual(self.memory.edit(episode_ids=[original]).episode_ids, (original, reply))
        self.assertEqual(self.memory.recall_history("nickname"), [])
        self.assertEqual(self.memory.edit(episode_ids=[separate]).episode_ids, (separate,))
        self.assertEqual([e.id for e in self.memory.recall_history("rain")], [unlinked])

    def test_compaction_candidates_allow_only_canonical_owner_statements(self):
        keep = self.memory.log("heard", "Remember that the spare key lives in a tin", "Richard")
        duplicate = self.memory.log("heard", "The fern is called Florence.", "Richard")
        latest = self.memory.log("heard", "the fern is called Florence", "Richard")
        echo = self.memory.log("heard", "The fern is called Florence", "Richard", memory_reply=True)
        reply = self.memory.log("said", "The fern is called Alfred", "Pip")
        guest = self.memory.log("heard", "I keep the watering can downstairs", "Guest")
        question = self.memory.log("heard", "Where do I put the watering can", "Richard")
        ids = [keep, duplicate, latest, echo, reply, guest, question]

        rows = candidate_highlights(self.memory, ids, owner="Richard")
        self.assertEqual(select_highlights(rows), (keep, latest))
        self.assertEqual(validate_selection(rows, [latest]), (latest,))
        self.assertIsNone(validate_selection(rows, [guest]))
        self.assertIsNone(validate_selection(rows, [keep, keep]))
        self.assertIsNone(validate_selection(rows, [True]))




class MemoryUpkeepTest(unittest.TestCase):
    def setUp(self):
        self.memory = Memory(None)

    def tearDown(self):
        self.memory.close()

    def test_every_telling_of_a_fact_is_linked_and_forgotten_with_it(self):
        first = self.memory.log("heard", "Remember the gate code is 4411", "Richard")
        fact = self.memory.remember("the gate code is 4411", "Richard", first)
        second = self.memory.log("heard", "Remember: the gate code is 4411", "Richard")
        self.assertEqual(self.memory.remember("The  gate code is 4411", "Richard", second), fact)
        source, = self.memory.db.execute("SELECT source FROM facts WHERE id = ?", (fact,)).fetchone()
        self.assertEqual(source, second, "a retold fact ages from its latest telling")
        change = self.memory.edit(fact_ids=[fact])
        self.assertEqual(set(change.episode_ids), {first, second})
        self.assertFalse(self.memory.contains("4411"))

    def test_a_tombstone_keeps_neither_speaker_nor_moment(self):
        episode = self.memory.log("heard", "My lantern is obsidian and heavy", "Richard")
        self.memory.edit(episode_ids=[episode])
        speaker, ts, text = self.memory.db.execute(
            "SELECT speaker, ts, text FROM episodes WHERE id = ?", (episode,)).fetchone()
        self.assertEqual((speaker, text), (None, "[forgotten]"))
        self.assertEqual(ts % 86400, 0)

    def test_a_correction_keeps_who_said_it(self):
        episode = self.memory.log("heard", "My lantern is obsidian and heavy", "Richard")
        self.memory.edit(episode_ids=[episode], replacement="My lantern is brass and light")
        self.assertEqual(self.memory.db.execute("SELECT speaker, text FROM episodes WHERE id = ?", (episode,)).fetchone(),
                         ("Richard", "My lantern is brass and light"))

    def test_copies_are_found_whatever_the_apostrophes_and_spacing(self):
        fact = self.memory.remember("Tom's bike is the blue one")
        copy = self.memory.log("said", "You said tom’s   bike is the BLUE one, right?", "Pip")
        self.memory.edit(fact_ids=[fact])
        self.assertEqual(self.memory.db.execute("SELECT text FROM episodes WHERE id = ?", (copy,)).fetchone()[0],
                         "[forgotten]")

    def test_facts_about_the_same_thing_and_the_latest_facts(self):
        from mind.commands import subject_of

        tom = self.memory.remember("my cat is called Tom")
        jim = self.memory.remember("my cat is called Jim")
        self.memory.remember("my dog is called Rex")
        self.assertEqual([f.id for f in self.memory.same_subject("my cat is called Jim", subject_of, exclude=jim)], [tom])
        self.assertEqual([f.text for f in self.memory.latest_facts(2)], ["my dog is called Rex", "my cat is called Jim"])

    def test_owner_lines_are_conversation_only(self):
        self.memory.log("heard", "I planted tomatoes", "Richard")
        self.memory.log("heard", "Remember that my cat is Tom", "Richard", command=True)
        self.memory.log("said", "Lovely.", "Pip")
        self.memory.log("heard", "What do you remember?", "Richard", memory_reply=True)
        self.assertEqual([e.text for e in self.memory.owner_lines("Richard")], ["I planted tomatoes"])

    def test_canned_replies_are_not_history(self):
        self.memory.log("said", "Hi. Good to hear you.", "Pip", canned=True)
        self.memory.log("said", "Kites are lovely to hear about.", "Pip")
        self.assertEqual([e.text for e in self.memory.recall_history("hear")], ["Kites are lovely to hear about."])

    def test_copies_of_wording_without_an_ascii_word_are_still_found(self):
        fact = self.memory.remember("Zoë Ångström")
        copy = self.memory.log("said", "Did you say ZOË  ÅNGSTRÖM?", "Pip")
        street = self.memory.remember("the Straße market")
        other = self.memory.log("said", "Is THE STRASSE market near you?", "Pip")
        self.memory.edit(fact_ids=[fact, street])
        for row in (copy, other):
            self.assertEqual(self.memory.db.execute("SELECT text FROM episodes WHERE id = ?", (row,)).fetchone()[0],
                             "[forgotten]")

    def test_a_blank_wording_is_never_a_copy_of_everything(self):
        kept = self.memory.log("heard", "I planted tomatoes", "Richard")
        self.assertEqual(self.memory._verbatim_copies(["   "]), [])
        self.assertEqual(self.memory.db.execute("SELECT text FROM episodes WHERE id = ?", (kept,)).fetchone()[0],
                         "I planted tomatoes")

    def test_a_clock_set_back_by_hand_is_believed(self):
        import time as clock

        now = clock.time()
        with patch("mind.memory.time.time", return_value=now + 5 * 3600):
            self.memory.log("heard", "stamped by a clock five hours fast", "Richard")
        sure, stamp = self.memory.owner_set_clock(now)
        self.assertEqual(sure, now, "no doubt about the clock it was just given")
        self.assertLessEqual(self.memory.newest_timestamp(), now)
        with self.assertRaises(ValueError):
            self.memory.owner_set_clock(1_000_000_000)


if __name__ == "__main__":
    unittest.main()
