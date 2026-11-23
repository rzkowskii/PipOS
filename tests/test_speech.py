import unittest

from mind.speech import (
    EMOTIONS, GRAMMAR, STRICT_GRAMMAR, ClauseSplitter, EchoGuard, TagParser, ThinkFilter, boilerplate, clean,
)


def feed_all(obj, pieces):
    return "".join(obj.feed(p) for p in pieces) + obj.flush()


class TagParserTest(unittest.TestCase):
    def test_reads_a_tag_that_arrives_in_pieces(self):
        tags = TagParser()
        text = feed_all(tags, ["[", "jo", "y", "] ", "Hello", " there"])
        self.assertEqual(tags.emotion, "joy")
        self.assertEqual(text, "Hello there")

    def test_says_nothing_until_the_tag_is_settled(self):
        tags = TagParser()
        self.assertEqual(tags.feed("[curio"), "")
        self.assertIsNone(tags.emotion)
        self.assertEqual(tags.feed("sity] Oh"), "Oh")
        self.assertEqual(tags.emotion, "curiosity")

    def test_no_tag_means_no_emotion_and_no_lost_text(self):
        tags = TagParser()
        self.assertEqual(feed_all(tags, ["Hello ", "there"]), "Hello there")
        self.assertIsNone(tags.emotion)

    def test_an_invented_tag_is_dropped_not_spoken(self):
        tags = TagParser()
        self.assertEqual(feed_all(tags, ["[ecstatic] Wow"]), "Wow")
        self.assertIsNone(tags.emotion)

    def test_a_long_bracket_is_ordinary_text(self):
        tags = TagParser()
        text = feed_all(tags, ["[this is not a tag at all, just a bracket"])
        self.assertIn("this is not a tag", text)

    def test_an_unfinished_tag_at_the_end_is_not_spoken(self):
        tags = TagParser()
        self.assertEqual(feed_all(tags, ["[jo"]), "")


class EchoGuardTest(unittest.TestCase):
    def run_guard(self, pieces, senses=""):
        guard = EchoGuard(senses)
        return "".join(guard.feed(p) for p in pieces) + guard.flush(), guard.stopped

    def test_stops_where_it_starts_reciting_its_notes(self):
        text, stopped = self.run_guard(["[joy] Tom sleeps well.", "\n", "(19:05, battery 82% charging", ", cool)"])
        self.assertEqual(text, "[joy] Tom sleeps well.")
        self.assertTrue(stopped)

    def test_the_break_and_the_bracket_may_arrive_together_or_apart(self):
        for pieces in (["Fine.\n(you remember"], ["Fine.", "\n", "  ", "(you"], ["Fine.\n", "(you"]):
            self.assertEqual(self.run_guard(pieces), ("Fine.", True), pieces)

    def test_ordinary_replies_pass_untouched(self):
        text, stopped = self.run_guard(["[neutral] Hello.", "\n", "\nHow are you?", " (I hope well.)"])
        self.assertEqual(text, "[neutral] Hello.\n\nHow are you? (I hope well.)")
        self.assertFalse(stopped)

    def test_a_line_break_at_the_very_end_is_kept(self):
        self.assertEqual(self.run_guard(["Hello.\n"]), ("Hello.\n", False))

    def test_recognizes_the_exact_current_senses_inline(self):
        senses = "19:05, battery 82% charging, cool, room bright"
        note = f"({senses})"
        for split in range(1, len(note)):
            with self.subTest(split=split):
                self.assertEqual(
                    self.run_guard(["The picnic is at Fern Bay. " + note[:split], note[split:] + " And again."], senses),
                    ("The picnic is at Fern Bay. ", True),
                )

    def test_stops_at_inline_labels_split_across_chunks(self):
        for label in ("(Private Note:", "(you reMEMber:", "(Past Conversation:"):
            with self.subTest(label=label):
                pieces = ["Yes. (Quite right.) ", *label, " text to keep private)"]
                self.assertEqual(self.run_guard(pieces), ("Yes. (Quite right.) ", True))

    def test_incomplete_prefixes_and_ordinary_parentheses_are_preserved(self):
        senses = "19:05, battery 82% charging"
        for text in ("Fine (private", "Fine (19:05, bat", "Fine (private property).", "Fine (19:05, battery 12%)."):
            with self.subTest(text=text):
                self.assertEqual(self.run_guard(list(text), senses), (text, False))

    def test_stops_at_the_first_private_note(self):
        pieces = ["Fine. (Private note: hidden) more text\n(another note)"]
        self.assertEqual(self.run_guard(pieces), ("Fine. ", True))


class ThinkFilterTest(unittest.TestCase):
    def test_removes_thinking_split_across_pieces(self):
        f = ThinkFilter()
        out = feed_all(f, ["<th", "ink>secret plan", " more</thi", "nk>[joy] Hi", " you"])
        self.assertEqual(out, "[joy] Hi you")

    def test_passes_ordinary_text_straight_through(self):
        f = ThinkFilter()
        self.assertEqual(f.feed("Hello "), "Hello ")
        self.assertEqual(f.feed("there"), "there")

    def test_unclosed_thinking_never_leaks(self):
        f = ThinkFilter()
        self.assertEqual(feed_all(f, ["Before. <think>and then it trails off"]), "Before. ")

    def test_a_lone_angle_bracket_is_released_at_the_end(self):
        f = ThinkFilter()
        self.assertEqual(feed_all(f, ["3 <", " 4"]), "3 < 4")


class ClauseSplitterTest(unittest.TestCase):
    def split(self, pieces):
        s = ClauseSplitter()
        out = []
        for p in pieces:
            out.extend(s.feed(p))
        rest = s.flush()
        return out + ([rest] if rest else [])

    def test_first_clause_is_cut_early_at_a_comma(self):
        s = ClauseSplitter()
        self.assertEqual(s.feed("Well, that is "), [])  # "Well," is too short to cut on
        self.assertEqual(s.feed("interesting, and I "), ["Well, that is interesting,"])
        # After the first clause, commas no longer cut.
        self.assertEqual(s.feed("think so too, really. "), ["and I think so too, really."])

    def test_a_short_opening_sentence_goes_out_at_once(self):
        s = ClauseSplitter()
        self.assertEqual(s.feed("Hm. "), ["Hm."])

    def test_waits_for_what_follows_before_cutting(self):
        s = ClauseSplitter()
        self.assertEqual(s.feed("It costs 3."), [])
        self.assertEqual(s.feed("50 today. "), ["It costs 3.50 today."])

    def test_does_not_cut_after_abbreviations(self):
        self.assertEqual(self.split(["Dr. Smith is here. ", "Good."]), ["Dr. Smith is here.", "Good."])

    def test_a_long_sentence_is_cut_at_a_comma(self):
        long = "Okay. " + "word " * 22 + "and so, it goes on"
        out = self.split([long])
        self.assertEqual(out[0], "Okay.")
        self.assertGreaterEqual(len(out), 3)

    def test_everything_said_is_kept_in_order(self):
        text = "Oh! I see, that makes sense. What happened next? Tell me"
        pieces = [text[i:i + 3] for i in range(0, len(text), 3)]
        self.assertEqual(" ".join(self.split(pieces)), text)

    def test_clean_removes_what_a_voice_should_not_read(self):
        self.assertEqual(clean("**Hello** [joy] `there` \U0001F600  friend"), "Hello there friend")
        self.assertEqual(clean("  # Title\n\nbody  "), "Title body")


class GrammarTest(unittest.TestCase):
    def test_grammar_lists_every_emotion(self):
        for emotion in EMOTIONS:
            self.assertIn(f'"{emotion}"', GRAMMAR)
        self.assertTrue(GRAMMAR.startswith("root ::="))




class ReplyShapeTest(unittest.TestCase):
    def test_the_strict_grammar_bounds_sentences_and_forbids_markup(self):
        self.assertIn('root ::= "[" emotion "] " sentence (" " sentence)?', STRICT_GRAMMAR)
        rule = next(line for line in STRICT_GRAMMAR.splitlines() if line.startswith("part ::="))
        for forbidden in ("\\n", "()", "*", "#", "<>"):
            self.assertIn(forbidden, rule)
        self.assertIn("{0,160}", STRICT_GRAMMAR)
        for emotion in EMOTIONS:
            self.assertIn(f'"{emotion}"', STRICT_GRAMMAR)

    def test_boilerplate_is_recognised(self):
        for text in ("As an AI, I can't.", "I'm just a language model.", "How can I help you today?",
                     "I don’t have feelings."):
            self.assertTrue(boilerplate(text), text)
        for text in ("Kites ride the wind.", "I can help you water the fern.", "That model train sounds fun."):
            self.assertFalse(boilerplate(text), text)


if __name__ == "__main__":
    unittest.main()
