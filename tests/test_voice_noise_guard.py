"""What the recogniser writes for noise, or for the name said alone, never reaches the mind.

Evidence label: host test over transcripts from the real laptop. On 28 Nov 2026
(round L, Moonshine, the owner's own voice) 11 of 14 model turns answered "You",
and the name said again in the follow-up window ("Hey, Pip.", "Hey, kip.",
"Pay-pip", "A tip.") went to the model or the greeting as a request. The heard
texts below are those, as text only.
"""

import asyncio
import unittest

from mind.voice import CommandGrammar, command_phrases
from tests.test_voice_feedback import Harness, ScriptedRecognizer, tone

# Moonshine and PocketSphinx on the laptop, from noise or a cut-off window.
NOISE = ("You", "You.", "So", "It's", "Oh my", "Oh.", "Um.", "of", "but", "and", "if")
# The name said again, misheard.
NAME_ALONE = ("Hey, Pip.", "Hey, kip.", "Pay-pip", "A tip.", "Pip.", "Hey Pip", "Okay Pip.")
# Short requests and answers that must still reach the mind.
SHORT_REQUESTS = ("Yes.", "No.", "Yep.", "Stop.", "Cancel.", "Okay.", "Thank you.", "Brighter.", "Hello.",
                  "What time is it?", "Open a terminal.", "You can stop now.", "So what's the time?",
                  # Answers that only look like the name or noise (Integration's review, 28 Nov).
                  "Pop.", "Peep.", "Pit.", "Up.", "Beep.", "Hip.")


class NoiseGuard(unittest.IsolatedAsyncioTestCase):
    async def dictate(self, text, *, oneshot=False, followup=False):
        flags = {"oneshot": True} if oneshot else {}
        script = [(True, [("hear.speech_start", flags)]), (False, [("hear.final", {"text": text, **flags})])]
        harness = Harness(grammar=CommandGrammar(command_phrases()))
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
        return harness.topics(), logs.output

    async def test_noise_never_reaches_the_mind(self):
        for oneshot, followup in ((True, False), (False, False), (False, True)):
            for text in NOISE:
                topics, output = await self.dictate(text, oneshot=oneshot, followup=followup)
                self.assertNotIn("hear.final", topics, (text, oneshot, followup))
                self.assertIn("hear.cancel", topics, (text, oneshot, followup))

    async def test_the_name_alone_is_the_name_and_listening_goes_on(self):
        for oneshot, followup in ((False, True), (False, False)):
            for text in NAME_ALONE:
                topics, output = await self.dictate(text, oneshot=oneshot, followup=followup)
                self.assertNotIn("hear.final", topics, (text, followup))
                self.assertIn("hear.cancel", topics, (text, followup))
                # In the follow-up window the check of how much sound was
                # heard may answer first; either way it is not passed on.
                if not followup:
                    self.assertTrue(any("the name alone" in line for line in output), text)

    async def test_the_log_never_carries_the_words(self):
        _, output = await self.dictate("Hey, kip.", followup=True)
        self.assertFalse(any("kip" in line.lower() for line in output if "heard:" not in line))

    async def test_short_requests_still_reach_the_mind(self):
        # The follow-up window has its own check of how much sound was heard
        # (Voice.room_noise), which this harness's two frames do not pass.
        for oneshot in (True, False):
            for text in SHORT_REQUESTS:
                topics, _ = await self.dictate(text, oneshot=oneshot)
                self.assertIn("hear.final", topics, (text, oneshot))


class NamedFollowUp(NoiseGuard):
    """A follow-up that begins with the name, misheard or not: the name goes, the request stays.

    Real laptop, 28 Nov: "A tip open a terminal" in the follow-up window was
    taken for room talk and dropped."""

    async def final_of(self, text):
        flags = {}
        script = [(True, [("hear.speech_start", flags)]), (False, [("hear.final", {"text": text})])]
        harness = Harness(grammar=CommandGrammar(command_phrases()))
        harness.recognizer = harness.voice.recognizer = ScriptedRecognizer(script)
        harness.voice.state = "attending"
        harness.voice.followup = True
        harness.voice.room_noise = lambda fields, oneshot: None   # this harness's two frames are not enough sound
        async with asyncio.TaskGroup() as harness.voice.tasks:
            harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
            await harness.frames(tone(4000), tone(4000))
            async with asyncio.timeout(2):
                while not {"hear.final", "hear.cancel"} & set(harness.topics()):
                    await asyncio.sleep(0.005)
            harness.voice.disarm()
        finals = [fields for topic, fields in harness.sent if topic == "hear.final"]
        return finals[-1] if finals else None

    async def test_a_misheard_name_before_the_request_goes(self):
        # Still a follow-up: the mind answers it when a skill takes it, as
        # it does "open a terminal" (host test of the mind, 28 Nov).
        for text, rest in (("A tip open a terminal", "open a terminal"),
                           ("Hey, kip, how is your battery?", "how is your battery?"),
                           ("Pay-pip what time is it", "what time is it"),
                           ("Hey Pip, what time is it?", "what time is it?")):
            final = await self.final_of(text)
            self.assertEqual(final.get("text"), rest, text)

    async def test_unnamed_follow_ups_stay_follow_ups(self):
        for text in ("open a terminal", "a tipping point is near", "hey bob is here"):
            final = await self.final_of(text)
            self.assertEqual(final.get("text"), text)
            self.assertTrue(final.get("followup"), text)


for _name in [name for name in vars(NoiseGuard) if name.startswith("test_")]:
    setattr(NamedFollowUp, _name, None)


class NameThenRequest(NoiseGuard):
    """"Hey Pip" in the follow-up window, a pause, then the request: the request is heard and addressed.

    Real laptop, 28 Nov (scripted, about 1 m): "Hey, kip." and "Pay-pip" ended
    the follow-up dictation and went to the model; "how is your battery",
    begun 0.4-0.5 s later, was lost while Pip thought (twice)."""

    async def test_the_request_after_the_name_reaches_the_mind_addressed(self):
        script = [(True, [("hear.speech_start", {})]), (False, [("hear.final", {"text": "Hey, kip."})]),
                  (True, [("hear.speech_start", {})]), (False, [("hear.final", {"text": "How is your battery?"})])]
        harness = Harness(grammar=CommandGrammar(command_phrases()))
        harness.recognizer = harness.voice.recognizer = ScriptedRecognizer(script)
        voice = harness.voice
        voice.state = "attending"
        voice.followup = True
        voice.dictation_until = harness.clock.now + 5.0
        voice.room_noise = lambda fields, oneshot: None   # this harness's frames are not enough sound
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(4000), tone(4000))
                self.assertEqual(harness.topics(), ["hear.speech_start", "hear.cancel"])
                self.assertFalse(voice.followup)
                self.assertTrue(voice.expecting)
                harness.clock.now += 6.0   # past the follow-up window it was heard in
                await harness.frames(tone(4000), tone(4000))
                voice.disarm()
        finals = [fields for topic, fields in harness.sent if topic == "hear.final"]
        self.assertEqual(finals, [{"text": "How is your battery?", "speaker": "Richard"}])
        self.assertTrue(any("listening for the request" in line for line in logs.output))


for _name in [name for name in vars(NoiseGuard) if name.startswith("test_")]:
    setattr(NameThenRequest, _name, None)


class OwnSoundsInTheWait(unittest.IsolatedAsyncioTestCase):
    """While a one-breath window waits for the request (asr_sherpa, up to 5 s),
    nothing Pip plays itself reaches the recogniser as sound: a chirp or the
    apology opens a deaf window, and the recogniser is fed silence for it.

    Real laptop, 28 Nov: Silero took Pip's own reply tails, played through the
    air, for speech at every threshold (0.5 to 0.2)."""

    async def test_a_sound_pip_plays_during_the_wait_is_fed_as_silence(self):
        from tests.test_voice_feedback import SILENCE

        script = [(False, [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})])]
        harness = Harness(script)
        voice = harness.voice
        voice.state = "idle"
        async with asyncio.TaskGroup() as voice.tasks:
            voice.capture = voice.tasks.create_task(voice.listen())
            await harness.frames(tone(500))
            window = voice.deafen(1.0)          # as play_chirp and apologise do
            await harness.frames(tone(6000), tone(6000), tone(6000))
            fed_during = [frame for frame, _ in harness.recognizer.fed[-3:]]
            window[1] = voice.now()             # playback over
            harness.clock.now += 0.2
            await harness.frames(tone(6000))
            fed_after = harness.recognizer.fed[-1][0]
            voice.disarm()
        self.assertEqual(fed_during, [SILENCE] * 3)
        self.assertEqual(fed_after, tone(6000))


if __name__ == "__main__":
    unittest.main()
