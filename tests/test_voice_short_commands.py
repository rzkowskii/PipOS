"""The voice reads a misheard one-word brightness step back (mind/short_commands.py).

Evidence label: host test. The heard texts are what Moonshine tiny wrote on
the real laptop (28 Nov 2026, round L, synthetic speech replayed through the
diagnostic mic). The recognizer here is scripted and, like Moonshine, has no
command grammar of its own (no utterance_audio, no decode_command).
"""

import asyncio
import unittest

from mind.voice import CommandGrammar, command_phrases
from tests.test_voice_feedback import Harness, ScriptedRecognizer, tone


class ShortCommands(unittest.IsolatedAsyncioTestCase):
    async def dictate(self, text, *, oneshot=False, followup=False, transcripts=False):
        flags = {"oneshot": True} if oneshot else {}
        script = [(True, [("hear.speech_start", flags)]), (False, [("hear.final", {"text": text, **flags})])]
        harness = Harness(grammar=CommandGrammar(command_phrases()), transcripts=transcripts)
        harness.recognizer = harness.voice.recognizer = ScriptedRecognizer(script)
        harness.voice.state = "attending"
        harness.voice.followup = followup
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(tone(4000), tone(4000))
                async with asyncio.timeout(2):
                    while not {"hear.final", "hear.cancel"} & set(harness.topics()):
                        await asyncio.sleep(0.005)
                harness.voice.disarm()
        final = [fields for topic, fields in harness.sent if topic == "hear.final"]
        return final[-1] if final else None, logs.output

    async def test_dimmer_heard_as_the_more_becomes_dimmer(self):
        final, output = await self.dictate("The more.")
        self.assertEqual(final["text"], "dimmer")
        line = next(line for line in output if "short command" in line)
        self.assertIn("2 words", line)
        self.assertNotIn("more", line, "no words in the log unless voice debugging is on")

    async def test_one_breath_righter_becomes_brighter(self):
        final, _ = await self.dictate("Righter.", oneshot=True)
        self.assertEqual(final, {"text": "brighter", "oneshot": True, "speaker": "Richard"})

    async def test_demo_after_the_name_becomes_dimmer(self):
        final, _ = await self.dictate("Demo.", oneshot=True)
        self.assertEqual(final["text"], "dimmer")

    async def test_the_follow_up_window_is_left_alone(self):
        # The reply's own tail may be heard here; it is never made a command.
        final, _ = await self.dictate("The more.", followup=True)
        self.assertNotEqual((final or {}).get("text"), "dimmer")

    async def test_ordinary_words_are_left_alone(self):
        for text in ("Thank you.", "Tell me more about the moon.", "What time is it?", "Retry."):
            final, _ = await self.dictate(text, oneshot=True)
            self.assertEqual(final["text"], text)

    async def test_a_command_is_left_as_it_is(self):
        final, _ = await self.dictate("Brighter.", oneshot=True)
        self.assertEqual(final["text"], "Brighter.")


if __name__ == "__main__":
    unittest.main()
