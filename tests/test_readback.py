import asyncio
import unittest
from pathlib import Path

from mind import commands, readback
from mind.reply import Context


async def _send(*args, **fields):
    return None


def ctx(**overrides):
    fields = dict(now=None, clock_trusted=True, speaker="Richard", guest=False, state_dir=Path("/nonexistent"),
                  owner="Richard", last_heard="set a timer for five minutes",
                  last_said="[joy] Okay, ten minutes, starting now.", body={}, voice={}, send=_send)
    fields.update(overrides)
    return Context(**fields)


def line(text, **overrides):
    reply = asyncio.run(readback.handle(text, ctx(**overrides)))
    return reply.line if reply else None


class ReadBack(unittest.TestCase):
    def test_what_did_you_hear(self):
        for said in ("What did you hear?", "What did I just say?", "hey Pip, what did you hear", "what did I say",
                     "what did you think I said"):
            with self.subTest(said=said):
                self.assertEqual(line(said), "[neutral] I heard: 'set a timer for five minutes'.")

    def test_read_back_is_the_exact_transcript(self):
        heard = "remind me to call Mum at 9, please"
        self.assertEqual(line("what did you hear", last_heard=heard), f"[neutral] I heard: '{heard}'.")

    def test_never_claims_to_have_heard_nothing(self):
        for heard in (None, "", "   ", "what did you hear"):
            with self.subTest(heard=heard):
                reply = line("what did you hear", last_heard=heard)
                self.assertNotIn("I heard", reply)
                self.assertEqual(reply, "[neutral] I haven't caught anything else from you since I woke up.")

    def test_repeat(self):
        for said in ("Say that again", "Repeat that", "Pardon?", "What did you say?", "Sorry?", "come again",
                     "could you say that again please", "what was that"):
            with self.subTest(said=said):
                self.assertEqual(line(said), "[neutral] Okay, ten minutes, starting now.")
        self.assertEqual(line("say that again", last_said=None), "[neutral] I haven't said anything yet since I woke up.")
        self.assertEqual(line("say that again", last_said="No tag here."), "[neutral] No tag here.")

    def test_recall_stays_recall(self):
        for said in ("what did you say about the orchid", "What did you say about my sister?"):
            with self.subTest(said=said):
                self.assertIsNone(line(said))
                self.assertFalse(readback.may_match(said))
                self.assertEqual(commands.parse(said, "Pip").kind, "recall_said")
        self.assertEqual(commands.parse("what did I say about the plumber", "Pip").kind, "recall_heard")
        self.assertIsNone(line("what did I say about the plumber"))

    def test_not_ours(self):
        for said in ("what did you dream about", "what do you hear", "tell me a joke", "say hello", "repeat after me"):
            with self.subTest(said=said):
                self.assertIsNone(line(said))

    def test_gate(self):
        self.assertTrue(readback.may_match("say that again"))
        self.assertTrue(readback.may_match("what did you hear?"))
        self.assertFalse(readback.may_match("how are you"))

    def test_stores_nothing(self):
        reply = asyncio.run(readback.handle("what did you hear", ctx()))
        self.assertFalse(reply.keep)
        self.assertIsNone(reply.logged)


class Long(unittest.TestCase):
    def test_long_speech_is_not_ours(self):
        self.assertFalse(readback.may_match("say that again " * 20))
        self.assertIsNone(line("say that again " * 20))

if __name__ == "__main__":
    unittest.main()
