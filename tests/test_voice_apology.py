"""The voice says sorry itself when it misses the words after the wake phrase.

The mind's replies only play while the brainstem is thinking or speaking, and a
miss leaves it attending or resting, so the apology cannot go through
say.request. The voice speaks it through its own Flite and aplay path.
"""

import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mind.tts import synthesis_command
from mind.voice import APOLOGY, CHIRP_TAIL_SECONDS
from tests.test_voice_feedback import SILENCE, START, WAKE, Harness, real_sleep, tone

MISS = (False, [("hear.cancel", {})])


class Synthesis:
    """Stands in for spawning flite: records commands and writes the output file."""

    def __init__(self, status=0):
        self.commands = []
        self.status = status

    @asynccontextmanager
    async def __call__(self, *command, **options):
        self.commands.append(command)
        if self.status == 0:
            Path(command[command.index("-o") + 1]).write_bytes(b"RIFF")
        yield Mock(wait=AsyncMock(return_value=self.status))


class HeldPlay:
    """Stands in for Voice.play: records each file and holds it until released."""

    def __init__(self):
        self.played = []
        self.release = asyncio.Event()

    async def __call__(self, audio, first):
        self.played.append((Path(audio).name, Path(audio).exists(), first))
        await self.release.wait()


async def settle(voice):
    async with asyncio.timeout(1):
        while voice.apologising:
            await real_sleep(0)


class Apology(unittest.IsolatedAsyncioTestCase):
    def harness(self, script, state="attending"):
        harness = Harness(script)
        harness.voice.state = state
        self.synthesis = Synthesis()
        self.player = HeldPlay()
        self.enterContext(patch("mind.voice.child", self.synthesis))
        harness.voice.play = self.player
        return harness

    async def test_a_miss_after_the_wake_phrase_is_answered_aloud_once_with_the_microphone_silenced(self):
        harness = self.harness([WAKE, START, MISS, (False, []), START, MISS])
        voice = harness.voice
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(500), tone(4000), tone(10))
                self.assertEqual(harness.topics(), ["hear.wake", "hear.speech_start", "hear.cancel", "hear.missed"])
                async with asyncio.timeout(1):
                    while not self.player.played:
                        await real_sleep(0)
                # Synthesized like a reply clause, with the being's own voice and rate.
                command = self.synthesis.commands[0]
                self.assertEqual(list(command), synthesis_command(voice.voice, voice.rate, APOLOGY,
                                                                  command[command.index("-o") + 1]))
                self.assertEqual(self.player.played, [("sorry.wav", True, False)])
                self.assertTrue(voice.apologising)

                # The being does not hear itself apologise.
                await harness.frames(tone(3000))
                self.assertEqual(harness.recognizer.fed[-1][0], SILENCE)
                self.player.release.set()
                await settle(voice)
                harness.clock.now += CHIRP_TAIL_SECONDS + 0.01
                harness.sent.clear()

                # A second miss in the same wake is neither reported again nor
                # spoken: "say that again" goes out at most once per wake.
                voice.expecting = True
                await harness.frames(tone(4000), tone(10))
                self.assertEqual(harness.topics(), ["hear.speech_start", "hear.cancel"])
                self.assertEqual(len(self.synthesis.commands), 1)
                voice.disarm()
        self.assertTrue(any("said sorry" in line for line in logs.output))
        self.assertTrue(any("asking only once" in line for line in logs.output))

    async def test_attention_lapsing_after_the_wake_phrase_is_answered_aloud(self):
        harness = self.harness([WAKE])
        voice = harness.voice
        self.player.release.set()
        async with asyncio.TaskGroup() as voice.tasks:
            voice.capture = voice.tasks.create_task(voice.listen())
            await harness.frames(tone(500))
            await harness.event("state.changed", state="idle", muted=False)
            await settle(voice)
            self.assertIn(("hear.missed", {"reason": "no_speech"}), harness.sent)
            self.assertEqual(len(self.player.played), 1)
            # The next attention lapse in the same wake stays quiet.
            voice.expecting = True
            await harness.event("state.changed", state="attending", muted=False)
            await harness.event("state.changed", state="idle", muted=False)
            await settle(voice)
            self.assertEqual(len(self.player.played), 1)
            voice.disarm()

    async def test_no_apology_when_muted_asleep_closed_or_never_woken(self):
        cases = {
            "muted": lambda voice: setattr(voice, "muted", True),
            "asleep": lambda voice: setattr(voice, "state", "asleep"),
            "closed": lambda voice: setattr(voice, "lid_open", False),
            "never woken": lambda voice: setattr(voice, "apology_owed", False),
        }
        for name, change in cases.items():
            with self.subTest(name):
                harness = self.harness([])
                voice = harness.voice
                voice.tasks = Mock()
                voice.apology_owed = True
                change(voice)
                await voice.missed("unclear")
                voice.tasks.create_task.assert_not_called()
                self.assertIsNone(voice.apology)

    async def test_muting_closing_the_lid_or_a_reply_stops_the_apology(self):
        for stop in ("mute", "lid", "reply", "say.stop"):
            with self.subTest(stop):
                harness = self.harness([])
                voice = harness.voice
                voice.speak = AsyncMock()
                voice.capture = Mock(done=Mock(return_value=False))
                async with asyncio.TaskGroup() as voice.tasks:
                    voice.apology_owed = True
                    await voice.missed("unclear")
                    async with asyncio.timeout(1):
                        while not self.player.played:
                            await real_sleep(0)
                    apology = voice.apology
                    if stop == "mute":
                        voice.stop_capture = AsyncMock()
                        await harness.event("voice.mute", on=True)
                    elif stop == "lid":
                        voice.stop_capture = AsyncMock()
                        await harness.event("body.lid", closed=True)
                    elif stop == "reply":
                        voice.stop_capture = AsyncMock()
                        await harness.event("state.changed", state="thinking", muted=False)
                        await harness.event("say.request", text="Hello.", last=True)
                    else:
                        await harness.event("say.stop")
                    async with asyncio.timeout(1):
                        while not apology.done():
                            await real_sleep(0)
                    self.assertTrue(apology.cancelled())
                    self.assertFalse(voice.apologising)
                    voice.disarm()

    async def test_a_failed_synthesis_is_logged_and_listening_goes_on(self):
        harness = self.harness([WAKE, START, MISS])
        self.synthesis.status = 1
        voice = harness.voice
        with self.assertLogs("being.voice", "WARNING") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(500), tone(4000), tone(10))
                await settle(voice)
                harness.clock.now += CHIRP_TAIL_SECONDS + 0.01
                await harness.frames(tone(2000))
                self.assertEqual(harness.recognizer.fed[-1][0], tone(2000))
                voice.disarm()
        self.assertEqual(self.player.played, [])
        self.assertTrue(any("could not say sorry" in line and "flite exit status 1" in line for line in logs.output))


class QuietAfterTheWake(Apology):
    """A plain "Hey Pip" followed by nothing: sorry once the room is quiet, still attending.

    On the real laptop (round L and the owner's own voice, 28 Nov 2026) the
    apology came only when the brainstem's 20 s attention lapsed, at idle, with
    the microphone deaf while it played."""

    async def run_after_wake(self, steps):
        """Wake plainly, then for each (seconds, frame, in_speech) advance the clock and feed one frame."""
        script = [WAKE] + [(in_speech, []) for _, _, in_speech in steps]
        harness = self.harness(script)
        voice = harness.voice
        self.player.release.set()
        async with asyncio.TaskGroup() as voice.tasks:
            voice.capture = voice.tasks.create_task(voice.listen())
            await harness.frames(tone(500))
            for seconds, frame, _ in steps:
                harness.clock.now += seconds
                await harness.frames(frame)
            await settle(voice)
            state = voice.state
            voice.disarm()
        return harness, state

    def missed(self, harness):
        return [fields for topic, fields in harness.sent if topic == "hear.missed"]

    async def test_quiet_after_a_plain_wake_is_answered_while_still_attending(self):
        harness, state = await self.run_after_wake([(1.0, tone(10), False)] * 7)
        self.assertEqual(self.missed(harness), [{"reason": "no_speech"}])
        self.assertEqual(len(self.player.played), 1)
        self.assertEqual(state, "attending")

    async def test_not_before_the_owner_has_had_time_to_speak(self):
        harness, _ = await self.run_after_wake([(1.0, tone(10), False)] * 3)
        self.assertEqual(self.missed(harness), [])
        self.assertEqual(self.player.played, [])

    async def test_not_while_the_room_is_still_loud(self):
        # Sound the recognizer did not take for a dictation still counts as someone talking.
        harness, _ = await self.run_after_wake([(1.0, tone(4000), True)] * 8)
        self.assertEqual(self.missed(harness), [])
        harness, _ = await self.run_after_wake([(1.0, tone(4000), True)] * 8 + [(1.0, tone(10), False)] * 3)
        self.assertEqual(self.missed(harness), [{"reason": "no_speech"}])

    async def test_not_after_a_dictation_began(self):
        script_start = (1.0, tone(4000), True)
        harness = self.harness([WAKE, START] + [(False, [])] * 8)
        voice = harness.voice
        self.player.release.set()
        async with asyncio.TaskGroup() as voice.tasks:
            voice.capture = voice.tasks.create_task(voice.listen())
            await harness.frames(tone(500), tone(4000))
            for _ in range(8):
                harness.clock.now += 1.0
                await harness.frames(tone(10))
            await settle(voice)
            voice.disarm()
        self.assertEqual(self.missed(harness), [])

    async def test_not_after_a_one_breath_wake(self):
        harness = self.harness([(False, [("hear.wake", {"oneshot": True})])] + [(False, [])] * 8)
        voice = harness.voice
        self.player.release.set()
        async with asyncio.TaskGroup() as voice.tasks:
            voice.capture = voice.tasks.create_task(voice.listen())
            await harness.frames(tone(500))
            for _ in range(8):
                harness.clock.now += 1.0
                await harness.frames(tone(10))
            await settle(voice)
            voice.disarm()
        self.assertEqual(self.missed(harness), [])

    async def test_the_lapse_after_it_says_nothing_more(self):
        harness = self.harness([WAKE] + [(False, [])] * 7)
        voice = harness.voice
        self.player.release.set()
        async with asyncio.TaskGroup() as voice.tasks:
            voice.capture = voice.tasks.create_task(voice.listen())
            await harness.frames(tone(500))
            for _ in range(7):
                harness.clock.now += 1.0
                await harness.frames(tone(10))
            await settle(voice)
            await harness.event("state.changed", state="idle", muted=False)
            await settle(voice)
            voice.disarm()
        self.assertEqual(self.missed(harness), [{"reason": "no_speech"}])
        self.assertEqual(len(self.player.played), 1)


# Only the new cases here: the Apology tests run once, in their own class.
for _name in [name for name in vars(Apology) if name.startswith("test_")]:
    setattr(QuietAfterTheWake, _name, None)


if __name__ == "__main__":
    unittest.main()
