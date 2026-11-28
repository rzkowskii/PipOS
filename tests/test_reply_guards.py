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

    async def test_boilerplate_after_a_short_first_clause_stops_the_reply_there(self):
        self.cortex_server.reply = lambda _: "[neutral] Of course, how can I help you today?"
        spoken = await self.say("How do kites fly so high")
        self.assertEqual(spoken, ["Of course,"])
        self.assertEqual(self.mind.last_timing["retries"], 0, "nothing said is taken back, so no second try")
        self.assertEqual(self.mind.messages[-1]["content"], "[neutral] Of course,")

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


if __name__ == "__main__":
    unittest.main()
