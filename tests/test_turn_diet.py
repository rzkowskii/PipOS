"""The prompt diet: how long it has been awake and its age are answered in code, not sent to the model every minute."""

import unittest

from mind import commands
from mind.turn import _awake_reply
from tests.test_mind import MindCase, SENSES
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


class Route(unittest.TestCase):
    def test_the_ways_of_asking(self):
        for text in ("How long have you been awake?", "how long have you been up", "How long are you awake",
                     "when did you wake up", "how long have you been switched on", "how long have you been running"):
            self.assertEqual(commands.parse(text), commands.Command("awake", ""), text)

    def test_other_questions_are_not_taken(self):
        for text in ("how long is a piece of string", "how old are you", "how long will the battery last",
                     "how long have I been awake"):
            command = commands.parse(text)
            self.assertFalse(command is not None and command.kind == "awake", text)

    def test_the_answer_is_the_brainstems_own_count(self):
        self.assertEqual(_awake_reply("19:05, awake 1 min, battery 82%"), "[neutral] I've been awake for about a minute.")
        self.assertEqual(_awake_reply("awake 37 min, 1 day old"), "[neutral] I've been awake for about 37 minutes.")
        self.assertEqual(_awake_reply("awake 1 hour"), "[neutral] I've been awake for about an hour.")
        self.assertEqual(_awake_reply("awake 5 hours, 12 days old"), "[neutral] I've been awake for about 5 hours.")
        self.assertIn("not sure", _awake_reply(""))
        self.assertIn("not sure", _awake_reply("19:05, battery 82%"))


class InTheMind(MindCase):
    async def ask(self, text):
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard")
        await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        return " ".join(e["text"] for e in self.bus.of("say.request")[-4:] if e["text"])

    async def test_it_answers_from_code_with_no_model_turn(self):
        self.bus.push("body.senses", line="19:05, awake 12 min, 3 days old, battery 82% charging, cool, room bright")
        await asyncio_sleep()
        reply = await self.ask("How long have you been awake?")
        self.assertIn("about 12 minutes", reply)
        self.assertEqual(self.cortex_server.chats, [])

    def test_the_awake_and_age_notes_are_gone_from_the_senses_note(self):
        for line in ("19:05, awake 12 min, 3 days old, battery 82% charging, cool, room bright",
                     "19:05, awake 1 hour, born today, battery 82%"):
            note = self.mind._senses_note(line, "what is the weather like")
            self.assertNotIn("awake", note)
            self.assertNotIn("old", note)
            self.assertNotIn("born", note)
        # And a change in the count does not re-send anything.
        first = self.mind._senses_note("19:05, awake 12 min, 3 days old", "tell me a joke")
        second = self.mind._senses_note("19:06, awake 13 min, 3 days old", "tell me a joke")
        self.assertEqual((first, second), ("", ""))

    def test_other_notable_things_still_come_through(self):
        note = self.mind._senses_note("19:05, awake 12 min, battery 12% and low, unplugged, about 25 min left, cool",
                                      "tell me a joke")
        self.assertIn("battery", note)
        self.assertNotIn("awake", note)


async def asyncio_sleep():
    import asyncio
    await asyncio.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
