"""The reply guards read a reply as it grows (parrot, boilerplate, a claim to see or hear)."""

import json
import unittest
from pathlib import Path

from mind.speech import boilerplate, perception_claim
from mind.turn import _parrots, reply_fault
from tests.test_mind import MindCase
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)

REPO = Path(__file__).resolve().parent.parent


class PerceptionClaim(unittest.TestCase):
    def test_a_claim_of_something_seen_or_heard(self):
        for text in ("I see a quiet blue lamp.", "I hear the wind outside.", "I can see the waves.",
                     "I'm seeing a small hand reaching in.", "I can hear a click of metal against wood.",
                     "Nice. I smell some rain coming.",
                     "I can't see the sea, but I hear the waves.", "I can't see the ocean, but I can see the waves."):
            self.assertTrue(perception_claim(text), text)

    def test_ordinary_talk_and_refusals_are_not_claims(self):
        for text in ("I see.", "I hear you.", "I see what you mean.", "I see the point, and I agree.",
                     "I see the difference as a matter of color.", "I can't see the sea.", "I cannot hear you well.",
                     "I don't see a way to do that.", "I do not hear a thing.", "You can see the stars at night.",
                     "Kites ride the wind.", "Fern Bay sounds lovely, I have no picture of it."):
            self.assertFalse(perception_claim(text), text)


class Fault(unittest.TestCase):
    def test_each_kind_of_fault(self):
        self.assertEqual(reply_fault("Tell me a long story.", "Tell me a long story."), "parrot")
        self.assertEqual(reply_fault("I see a quiet blue lamp.", "Please repeat exactly: a quiet blue lamp"), "perception")
        self.assertEqual(reply_fault("As an AI, I cannot do that.", "How do kites fly?"), "boilerplate")
        self.assertIsNone(reply_fault("Kites ride the wind.", "How do kites fly?"))

    def test_the_last_try_never_empties_a_reply_for_a_doubtful_check(self):
        self.assertIsNone(reply_fault("I see a quiet blue lamp.", "hello there", last_try=True))
        self.assertIsNone(reply_fault("As an AI, I cannot do that.", "hello there", last_try=True))
        self.assertEqual(reply_fault("Tell me a long story.", "Tell me a long story.", last_try=True), "parrot")

    def test_a_short_first_clause_is_followed_by_the_rest(self):
        heard = "How do kites fly?"
        self.assertIsNone(reply_fault("Of course,", heard))
        self.assertEqual(reply_fault("Of course, how can I help you today?", heard), "boilerplate")
        self.assertEqual(reply_fault("Nice question. I see a kite over the sea.", heard), "perception")


def old_fault(clauses, heard):
    """What the guard did: only the first clause, only before anything was said."""
    first = clauses[0] if clauses else ""
    return bool(first and (_parrots(first, heard) or boilerplate(first, heard)))


def new_fault(clauses, heard):
    spoken = []
    for clause in clauses:
        if reply_fault(" ".join([*spoken, clause]), heard):
            return True
        spoken.append(clause)
    return False


class CatchRate(unittest.TestCase):
    """The growing guard catches every reply the first-clause guard caught."""

    EMBEDDED = [
        (["As an AI language model, I cannot do that."], "How do kites fly so high"),
        (["How can I help you today?"], "hello"),
        (["Tell me a long story.", "Tell me a long story."], "Tell me a long story."),
        (["What time is it?"], "What time is it?"),
        (["Well, I'm just an AI."], "who are you"),
        (["Kites ride the wind."], "How do kites fly so high"),
        (["I can help you water the fern."], "can you help with the fern"),
        (["Hmm,", "that is a good question."], "why is the sky blue"),
    ]

    def corpus(self):
        cases = list(self.EMBEDDED)
        audition = REPO / "out" / "model-audition"
        for name in ("blind-replies.json", "holdout-blind-replies.json"):
            path = audition / name
            if path.is_file():
                for item in json.loads(path.read_text()):
                    for clauses in item["replies"].values():
                        cases.append((clauses, item["input"]))
        runs = Path.home() / "Developer" / "PipOS-Research" / "reply-quality-20261128" / "levers" / "runs"
        for path in sorted(runs.glob("*.json")) if runs.is_dir() else ():
            for case in json.loads(path.read_text()).get("cases", []):
                clauses = [c["text"] for c in case.get("clauses", []) if c.get("text")]
                if clauses:
                    cases.append((clauses, case["input"]))
        return cases

    def test_the_new_guard_catches_what_the_old_one_caught(self):
        cases = self.corpus()
        lost = [(clauses, heard) for clauses, heard in cases if old_fault(clauses, heard) and not new_fault(clauses, heard)]
        self.assertEqual(lost, [])
        old = sum(old_fault(c, h) for c, h in cases)
        new = sum(new_fault(c, h) for c, h in cases)
        self.assertGreaterEqual(new, old)
        print(f"\nguard catch rate on {len(cases)} replies: first-clause guard {old}, growing guard {new}")


class InTheTurn(MindCase):
    async def say(self, text):
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard")
        await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        return [e["text"] for e in self.bus.of("say.request") if e["text"]]

    async def test_boilerplate_after_a_short_first_clause_is_held_and_asked_for_again(self):
        replies = iter(["[neutral] Of course, how can I help you today?", "[joy] Kites ride the wind."])
        self.cortex_server.reply = lambda _: next(replies)
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken, ["Kites ride the wind."], "nothing of the first try was said")
        self.assertEqual(self.mind.last_timing["retries"], 1)

    async def test_a_claim_to_see_after_a_clause_stops_the_reply(self):
        self.cortex_server.reply = lambda _: "[joy] Kites are lovely. I see a kite over the quiet sea."
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken, ["Kites are lovely."])

    async def test_a_claim_to_see_in_the_first_clause_is_asked_for_again(self):
        replies = iter(["[neutral] I see a quiet blue lamp.", "[neutral] I don't know what that is."])
        self.cortex_server.reply = lambda _: next(replies)
        spoken = await self.say("Please repeat exactly: a quiet blue lamp")
        self.assertEqual(spoken, ["I don't know what that is."])
        self.assertEqual(self.mind.last_timing["retries"], 1)

    async def test_the_second_try_is_said_even_if_it_still_claims_to_see(self):
        self.cortex_server.reply = lambda _: "[neutral] I see a quiet blue lamp."
        spoken = await self.say("tell me about the garden")
        self.assertEqual(spoken, ["I see a quiet blue lamp."])
        self.assertEqual(self.mind.last_timing["retries"], 1)

    async def test_a_normal_reply_is_untouched(self):
        self.cortex_server.reply = lambda _: "[joy] Kites ride the wind. They love a steady breeze."
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken, ["Kites ride the wind.", "They love a steady breeze."])
        self.assertEqual(self.mind.last_timing["retries"], 0)


class ShortFirstClause(MindCase):
    cfg = {"first_chunk": "short"}

    async def say(self, text):
        return await InTheTurn.say(self, text)

    async def test_the_first_clause_is_cut_at_the_first_mark(self):
        self.cortex_server.reply = lambda _: "[joy] Kites ride, and they love a steady breeze."
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken[0], "Kites ride,")
        self.assertEqual(" ".join(spoken), "Kites ride, and they love a steady breeze.")

    async def test_an_interjection_is_held_until_the_next_words_and_then_said(self):
        self.cortex_server.reply = lambda _: "[joy] Sure, kites ride the wind."
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken, ["Sure,", "kites ride the wind."])
        self.assertEqual(self.mind.last_timing["retries"], 0)
        # Both clauses were handed over together, in order, with their numbers.
        self.assertEqual([e["seq"] for e in self.bus.of("say.request") if e["text"]], [1, 2])

    async def test_a_reply_that_ends_while_its_opening_is_held_is_still_said(self):
        self.cortex_server.reply = lambda _: "[joy] Sure,"
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken, ["Sure,"])

    async def test_boilerplate_behind_an_interjection_is_never_said_by_the_first_try(self):
        replies = iter(["[neutral] Sure, how can I help you today?", "[joy] Kites ride the wind."])
        self.cortex_server.reply = lambda _: next(replies)
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(" ".join(spoken), "Kites ride the wind.")

    async def test_three_words_with_no_mark_make_the_first_clause(self):
        self.cortex_server.reply = lambda _: "[joy] Kites ride the wind and love a steady breeze."
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken[0], "Kites ride the")


class Splitter(unittest.TestCase):
    def run_splitter(self, text, **options):
        from mind.speech import ClauseSplitter
        splitter = ClauseSplitter(**options)
        out = []
        for char in text:
            out += splitter.feed(char)
        rest = splitter.flush()
        return out + ([rest] if rest else [])

    def test_the_short_first_clause_is_cut_on_the_mark_itself(self):
        from mind.speech import ClauseSplitter
        splitter = ClauseSplitter(short_first=True)
        self.assertEqual(splitter.feed("Sure"), [])
        self.assertEqual(splitter.feed(","), ["Sure,"], "no waiting for the space after it")

    def test_numbers_and_abbreviations_do_not_end_it(self):
        self.assertEqual(self.run_splitter("Version 3.5 is out.", short_first=True)[0], "Version 3.5 is")
        self.assertEqual(self.run_splitter("1,000 kites fly.", short_first=True)[0], "1,000 kites fly.")
        self.assertEqual(self.run_splitter("Dr. Smith is here.", short_first=True)[0], "Dr. Smith is")

    def test_later_clauses_are_cut_as_before(self):
        self.assertEqual(self.run_splitter("Yes, it is. The sea is wide, and it is very very deep indeed.",
                                           short_first=True)[:2], ["Yes,", "it is."])
        old = self.run_splitter("Yes, it is. The sea is wide.", short_first=False)
        self.assertEqual(old, ["Yes, it is.", "The sea is wide."])

    def test_the_old_default_is_unchanged(self):
        self.assertEqual(self.run_splitter("Sure, I can help with that."), ["Sure, I can help with that."])


class Opening(unittest.TestCase):
    def test_what_may_still_become_boilerplate_or_a_claim(self):
        from mind.speech import reply_open
        for text in ("Sure,", "Of course,", "Hmm,", "I", "I see", "I'm just", "How can", "As an", "Well, how can I"):
            self.assertTrue(reply_open(text), text)
        for text in ("Kites ride", "I see a", "How do kites", "Sure, kites ride", "The sea is", ""):
            self.assertFalse(reply_open(text), text)


if __name__ == "__main__":
    unittest.main()
