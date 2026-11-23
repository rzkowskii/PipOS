"""Recall by meaning as well as by words: blink-8M vectors fused with FTS5.

The paraphrase sets are fixed here. MIXED is the acceptance set: half the
questions share one word with the fact ("where's the spare key?"), half share
none. HARD and HELD_OUT share no words at all; they show the static model's
limit honestly rather than hide it. The unrelated sets check that a weak
match becomes "I don't recall", never a guess.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import embed
from mind.memory import Memory, search_terms

MODEL = Path(__file__).resolve().parents[1] / "out" / "models" / "embeddings" / "blink-8M"
HAVE_MODEL = (MODEL / "model.safetensors").is_file()

FACTS = [
    "my orchid is called Silverleaf",
    "the spare key is in the blue pot by the door",
    "my sister's birthday is on the 14th of March",
    "the plumber comes on Thursday",
    "my favourite colour is green",
    "my dog is called Biscuit",
    "I'm allergic to peanuts",
    "my car is a red Toyota",
    "the wifi password is on the fridge",
    "my doctor is Dr Patel",
    "I take my tablets at breakfast",
    "my mum lives in Brisbane",
    "the bins go out on Tuesday night",
    "I work as a nurse at the hospital",
    "my best friend is called Sam",
    "I'm learning to play the piano",
    "my anniversary is in June",
    "the dentist appointment is next Wednesday",
    "my favourite food is lasagne",
    "I support the Sydney Swans",
    "my bike is locked in the garage",
    "I prefer tea to coffee",
    "my son's teacher is Mrs Lee",
    "my passport expires in 2028",
    "I planted tomatoes in the back garden",
    "my gym class is on Monday mornings",
    "the cat's vet is on King Street",
    "I'm saving up for a trip to Japan",
    "my landlord is called Peter",
    "I don't eat meat",
]
# One word in common with the fact...
OVERLAPPING = [
    "what did I name my orchid", "where's the spare key", "when is my sister's big day",
    "what day is the plumber coming", "what's my favourite shade", "what's my dog's name",
    "what am I allergic to", "what kind of car have I got", "where's the wifi code written",
    "who's my doctor", "when do I take my tablets", "which city is mum in", "when do the bins go out",
    "where do I work", "what's my best friend's name",
]
# ...and none at all.
HARD = [
    "what's my flower called", "where did I hide the extra house key", "when was my sibling born",
    "when is the tradesman fixing the pipes", "which colour do I like best", "what's the name of my puppy",
    "what food am I allergic to", "what vehicle do I drive", "where can I find the internet code",
    "who is my GP", "when should I have my pills", "where does my mother live",
    "which night is rubbish collection", "what is my job", "who is my closest mate",
    "what instrument am I practising", "when is our wedding date", "when do I see the tooth doctor",
    "what meal do I love most", "which football team do I follow", "where is my bicycle",
    "what hot drink do I like", "who teaches my boy", "when does my travel document run out",
    "what vegetables did I grow", "when do I go to exercise", "where is the animal clinic",
    "where am I planning to travel", "who owns the flat I rent", "am I vegetarian",
]
MIXED = OVERLAPPING + HARD[15:]

HELD_OUT = [
    ("my brother is called Tom", "what's my sibling's name"),
    ("the boiler was serviced in August", "when was the heater last checked"),
    ("my glasses are on the bedside table", "where did I leave my specs"),
    ("I'm afraid of spiders", "what creature scares me"),
    ("my daughter's school is Greenfield Primary", "where does my girl study"),
    ("the rent is due on the first of the month", "when do I pay the landlord"),
    ("my blood type is O negative", "what group is my blood"),
    ("I grew up in Melbourne", "which city was I raised in"),
    ("my bank is the Commonwealth", "who do I keep my money with"),
    ("I play tennis on Saturdays", "which sport do I do at the weekend"),
    ("my favourite band is Crowded House", "what music group do I like most"),
    ("the fuse box is under the stairs", "where is the electrical switchboard"),
    ("I'm reading a book about the Romans", "what novel am I in the middle of"),
    ("my neighbour is called Margaret", "who lives next door"),
    ("my phone number ends in 42", "what are the last digits of my mobile"),
    ("I parked on level three", "which floor is the car on"),
    ("the vacuum cleaner is in the hall cupboard", "where do I keep the hoover"),
    ("my grandmother turns ninety in May", "how old will my nan be"),
    ("I like my coffee black", "how do I take my coffee"),
    ("my cat sleeps on the sofa", "where does the kitty nap"),
    ("my favourite film is Casablanca", "what movie do I love"),
    ("I have a meeting with the bank on Friday", "when is my appointment with the bank"),
    ("the tomatoes need watering every evening", "how often do the plants need water"),
    ("my uncle is a pilot", "what does my uncle do for a living"),
    ("I'm going camping next weekend", "what are my plans for the weekend"),
    ("my laptop password hint is my first pet", "what is the clue for my computer login"),
    ("the recycling bin is the yellow one", "which bin is for recycling"),
    ("my partner hates mushrooms", "what food does my partner dislike"),
    ("I broke my wrist in 2019", "when did I injure my arm"),
    ("my yoga class is on Wednesday", "when do I do yoga"),
]

UNRELATED = [
    "what is the capital of France", "how far away is the moon", "tell me about volcanoes",
    "who won the world cup", "how do aeroplanes fly", "what's the square root of two", "explain quantum physics",
    "how many legs does a spider have", "what is photosynthesis", "who wrote Hamlet", "how deep is the ocean",
    "what language do they speak in Brazil", "why is the sky blue", "how do magnets work",
    "what's the tallest mountain", "when did the Romans invade Britain", "how are rainbows made",
    "what do bees eat", "how old is the universe", "what is a black hole",
]
# About the owner, but nothing they ever said: the hardest kind of miss.
UNRELATED_PERSONAL = [
    "what's my shoe size", "when is my car registration due", "what's my favourite season",
    "where did I go to university", "what's my middle name", "how tall am I", "when did I last get a haircut",
    "what's my star sign", "who is my accountant", "what time does my train leave", "where is my umbrella",
    "what is my email address", "when is my next holiday", "what flavour ice cream do I like",
    "who cuts my hair", "what's my cousin's name", "where do I buy my groceries", "when did I move house",
    "what is my postcode", "which hand do I write with",
]


def scored(embedder, facts, queries):
    memory = Memory(None, embedder=embedder)
    ids = [memory.remember(fact, "Richard") for fact in facts]
    hits = sum(1 for query, want in zip(queries, ids)
               if (found := memory.recall(query, limit=1)) and found[0].id == want)
    memory.close()
    return hits


def false_recalls(embedder, facts, queries):
    memory = Memory(None, embedder=embedder)
    for fact in facts:
        memory.remember(fact, "Richard")
    count = sum(1 for query in queries if memory.recall(query, limit=1))
    memory.close()
    return count


@unittest.skipUnless(HAVE_MODEL, "blink-8M is not downloaded")
class ParaphraseRecall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = embed.load(MODEL)

    def test_the_acceptance_set_reaches_80_percent_and_beats_words_alone(self):
        self.assertEqual(len(MIXED), 30)
        hybrid = scored(self.model, FACTS, MIXED)
        words = scored(None, FACTS, MIXED)
        self.assertGreaterEqual(hybrid, 24, f"hit@1 {hybrid}/30")
        self.assertGreater(hybrid, words)

    def test_questions_sharing_no_words_still_mostly_find_their_fact(self):
        # The static model's limit, kept visible: 19-20 of 30 when measured.
        for facts, queries in ((FACTS, HARD), ([f for f, _ in HELD_OUT], [q for _, q in HELD_OUT])):
            hybrid, words = scored(self.model, facts, queries), scored(None, facts, queries)
            self.assertGreaterEqual(hybrid, 17)
            self.assertGreater(hybrid, words + 8)

    def test_unrelated_questions_are_not_answered_from_memory(self):
        self.assertLessEqual(false_recalls(self.model, FACTS, UNRELATED), 1)
        self.assertLessEqual(false_recalls(self.model, [f for f, _ in HELD_OUT], UNRELATED), 1)

    def test_personal_questions_with_no_fact_guess_no_more_than_words_alone(self):
        for facts in (FACTS, [f for f, _ in HELD_OUT]):
            self.assertLessEqual(false_recalls(self.model, facts, UNRELATED_PERSONAL),
                                 false_recalls(None, facts, UNRELATED_PERSONAL))

    def test_forgetting_keeps_to_the_words_said(self):
        memory = Memory(None, embedder=self.model)
        self.addCleanup(memory.close)
        orchid = memory.remember("my orchid is called Silverleaf", "Richard")
        memory.remember("my dog is called Biscuit", "Richard")
        # all_terms is how forgetting finds its matches: never by meaning.
        self.assertEqual(memory.recall("flower", all_terms=True), [])
        self.assertEqual([f.id for f in memory.recall("what's the name of my flower", limit=1)], [orchid])

    def test_conversation_is_found_by_meaning_within_its_filters(self):
        memory = Memory(None, embedder=self.model)
        self.addCleanup(memory.close)
        with patch("mind.memory.time.time", side_effect=[1_780_000_000 + i * 60 for i in range(20)]):
            owner = memory.log("heard", "I repotted the orchids this morning", "Richard")
            memory.log("said", "That should help them flower.", "Pip", reply_to=owner)
            guest = memory.log("heard", "My roses are struggling in the shade", "Sam")
            memory.log("heard", "We should buy more milk tomorrow", "Richard")
        found = memory.recall_history("what did I do with my flowers", limit=1, kind="heard", speaker="Richard")
        self.assertEqual([e.id for e in found], [owner])
        found = memory.recall_history("what did I do with my flowers", limit=1, kind="heard", speaker="Sam")
        self.assertEqual([e.id for e in found], [guest])
        self.assertEqual(memory.recall_history("flowers", limit=1, end=1_780_000_000 - 1), [])
        # Forget-by-topic still needs the words themselves.
        self.assertEqual(memory.recall_history("flowers", limit=-1, all_terms=True, for_edit=True), [])
        # A caller that wants shared words only can skip the vector search.
        self.assertEqual(memory.recall_history("what did I do with my flowers", limit=1, kind="heard",
                                               speaker="Richard", meaning=False), [])

    def test_a_search_over_ten_thousand_sentences_stays_fast(self):
        memory = Memory(None, embedder=self.model)
        self.addCleanup(memory.close)
        words = "orchid garden kitchen sister birthday plumber thursday weather tomato bicycle".split()
        with memory.db:
            memory.db.executemany(
                "INSERT INTO episodes (ts, kind, speaker, text) VALUES (?, 'heard', 'Richard', ?)",
                ((1.78e9 + i, f"{words[i % 10]} {words[(i * 7) % 10]} {words[(i * 3) % 10]} note {i}")
                 for i in range(10_000)))
        while memory.backfill_vectors(5000):
            pass
        start = time.perf_counter()
        memory.recall_history("what did I say about my sister's birthday", limit=1)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"{elapsed * 1000:.1f} ms")


@unittest.skipUnless(HAVE_MODEL, "blink-8M is not downloaded")
class VectorTables(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = Memory(self.root, embedder=embed.load(MODEL))

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def wait_for_vectors(self, episodes, facts, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.memory.vector_counts() == (episodes, facts):
                return
            time.sleep(0.01)
        self.fail(f"vectors {self.memory.vector_counts()} never reached {(episodes, facts)}")

    def test_sentences_are_embedded_off_the_event_loop(self):
        episode = self.memory.log("heard", "remember that my orchid is called Silverleaf", "Richard", command=True)
        self.memory.remember("my orchid is called Silverleaf", "Richard", episode)
        self.memory.log("said", "Okay. I'll remember that.", "Pip")
        self.assertIsNotNone(self.memory._indexer, "a thread of its own does the embedding")
        self.wait_for_vectors(2, 1)

    def test_a_forget_leaves_no_vector_rows(self):
        episode = self.memory.log("heard", "remember that the safe word is Quixotically", "Richard", command=True)
        fact = self.memory.remember("the safe word is Quixotically", "Richard", episode)
        reply = self.memory.log("said", "I'll keep the safe word Quixotically to myself.", "Pip", reply_to=episode)
        self.memory.index_pending(100)
        self.assertEqual(self.memory.vector_counts(), (2, 1))
        self.memory.forget([fact])
        rows = self.memory.db.execute(
            "SELECT count(*) FROM episode_vec WHERE episode_id IN (?, ?)", (episode, reply)).fetchone()[0]
        self.assertEqual(rows, 0)
        self.assertEqual(self.memory.vector_counts(), (0, 0))
        # A tombstone is never embedded again.
        self.memory.backfill_vectors()
        self.assertEqual(self.memory.vector_counts(), (0, 0))

    def test_a_correction_drops_the_old_vector_and_the_new_words_get_one(self):
        fact = self.memory.remember("my orchid is called Silverleaf", "Richard")
        self.memory.index_pending(10)
        old = self.memory.db.execute("SELECT q8 FROM fact_vec WHERE fact_id = ?", (fact,)).fetchone()[0]
        self.memory.correct(fact, "my orchid is called Moonleaf")
        self.memory.backfill_vectors()
        new = self.memory.db.execute("SELECT q8 FROM fact_vec WHERE fact_id = ?", (fact,)).fetchone()[0]
        self.assertNotEqual(old, new)

    def test_a_vector_is_never_stored_for_words_that_changed_while_it_was_made(self):
        episode = self.memory.log("heard", "the safe word is Quixotically", "Richard")
        if self.memory._indexer is not None:
            self.memory._indexer.stop()
            self.memory._indexer = None
        real = self.memory.embedder

        class Racing:
            model_id = real.model_id

            def embed(inner, text):
                # The owner forgets the sentence while its vector is being made.
                self.memory.edit(episode_ids=[episode])
                return real.embed(text)

        self.memory.embedder = Racing()
        self.memory.index_pending(10)
        self.memory.embedder = real
        self.assertEqual(self.memory.vector_counts(), (0, 0))

    def test_vectors_from_an_older_recipe_are_remade_at_night(self):
        self.memory.remember("my orchid is called Silverleaf", "Richard")
        self.memory.index_pending(10)
        self.memory.db.execute("UPDATE fact_vec SET model = 'older'")
        self.memory.db.commit()
        self.assertGreater(self.memory.backfill_vectors(), 0)
        model, = self.memory.db.execute("SELECT model FROM fact_vec").fetchone()
        self.assertNotEqual(model, "older")


class NoModel(unittest.TestCase):
    def test_without_the_model_recall_is_exactly_word_based(self):
        memory = Memory(None, embedder=None)
        self.addCleanup(memory.close)
        memory.remember("my orchid is called Silverleaf", "Richard")
        self.assertEqual(memory.recall("what's the name of my flower"), [])
        self.assertEqual([f.text for f in memory.recall("orchid")], ["my orchid is called Silverleaf"])
        self.assertEqual(memory.index_pending(), 0)
        self.assertEqual(memory.vector_counts(), (0, 0))

    def test_search_terms_are_what_a_vector_is_made_from(self):
        from mind.memory import vector_text

        self.assertEqual(vector_text("What's my flower called?"), " ".join(search_terms("What's my flower called?")))
        self.assertEqual(vector_text("what is it"), "what is it")


if __name__ == "__main__":
    unittest.main()
