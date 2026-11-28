"""Diagnostic logging, wake feedback, missed-speech events, retries and microphone gain."""

import asyncio
import logging
import math
import struct
import tempfile
import unittest
import wave
from array import array
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from mind.recognition import (
    FRAME_BYTES, FRAME_SAMPLES, MAX_AUTO_GAIN, MicLevel, Recognizer, amplify, dbfs, levels, pcm,
)
from mind.tts import ChirpBank
from mind.voice import (
    CAPTURE_RETRY_FIRST, CHIRP_TAIL_SECONDS, FLOOR_FIRST_REPORT_SECONDS, PLAYBACK_ATTEMPTS, PlaybackStuck, Voice,
    mic_gain,
)


def tone(amplitude):
    return struct.pack(f"<{FRAME_SAMPLES}h", *([amplitude, -amplitude] * (FRAME_SAMPLES // 2)))


SILENCE = bytes(FRAME_BYTES)
# Patching mind.voice.asyncio.sleep replaces asyncio.sleep everywhere. Keep a real yield.
real_sleep = asyncio.sleep


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScriptedRecognizer:
    """Replays (in_speech, events) for each fed frame and records what it was given."""

    def __init__(self, script=()):
        self.script = list(script)
        self.fed = []
        self.in_speech = False
        self.wake_score = -4200
        self.captures = 0

    def start_capture(self):
        self.captures += 1

    def stop_capture(self):
        pass

    def feed(self, frame, attending, oneshot=True):
        self.fed.append((frame, attending))
        self.in_speech, events = self.script.pop(0) if self.script else (False, [])
        return [(topic, dict(fields)) for topic, fields in events]

    def confidence(self):
        return 0.83


class Stream:
    def __init__(self):
        self.frames = asyncio.Queue()

    async def readexactly(self, size):
        frame = await self.frames.get()
        if frame is None:
            raise asyncio.IncompleteReadError(b"", size)
        return frame


class Harness:
    """A voice with a scripted recognizer, a queue for a microphone and a recorded bus."""

    def __init__(self, script=(), **options):
        self.recognizer = ScriptedRecognizer(script)
        self.clock = Clock()
        self.voice = Voice(self.recognizer, "Richard", ["arecord", "-D", "plughw:0,0"], "plughw:0,0",
                           clock=self.clock, **options)
        self.voice.work_dir = None
        self.sent = []
        self.stream = Stream()
        self.voice.bus.send = self.send
        self.voice.bus.connected.set()
        self.voice.lid_open = True
        self.voice.state = "idle"
        self.voice.muted = False

        @asynccontextmanager
        async def recording():
            yield self.stream

        self.voice.recording = recording

    async def send(self, topic, **fields):
        self.sent.append((topic, fields))

    def topics(self):
        return [topic for topic, _ in self.sent]

    async def frames(self, *frames):
        target = len(self.recognizer.fed) + len(frames)
        for frame in frames:
            await self.stream.frames.put(frame)
        async with asyncio.timeout(2):
            while len(self.recognizer.fed) < target:
                await real_sleep(0)
        # Let the events of the last frame go out.
        for _ in range(5):
            await real_sleep(0)

    async def event(self, topic, **fields):
        event = {"t": topic, **fields}
        self.voice.on_event_received(event)
        await self.voice.handle(event)


class HeldChild:
    """Stand-in for spawning aplay: records commands and holds playback until released."""

    def __init__(self):
        self.commands = []
        self.release = asyncio.Event()

    @asynccontextmanager
    async def __call__(self, *command, **options):
        self.commands.append(command)
        Path(command[-1]).stat()

        async def wait():
            await self.release.wait()
            return 0

        yield Mock(wait=wait)


WAKE = (False, [("hear.wake", {})])
START = (True, [("hear.speech_start", {})])
SPEECH = (True, [])


class WakeFeedback(unittest.IsolatedAsyncioTestCase):
    """The wake phrase is logged with its score and level. The ears-open earcon
    (ChirpBank, on the brainstem's move to attending) is not heard by the ears."""

    async def test_wake_is_logged_with_score_and_level_and_its_earcon_is_not_heard(self):
        with tempfile.TemporaryDirectory() as work:
            harness = Harness([SPEECH, SPEECH, WAKE], chirp_bank=ChirpBank(Path(work)), chirps=True)
            voice = harness.voice
            player = HeldChild()
            with patch("mind.voice.child", player), self.assertLogs("being.voice", "INFO") as logs:
                async with asyncio.TaskGroup() as voice.tasks:
                    voice.capture = voice.tasks.create_task(voice.listen())
                    await harness.frames(tone(1000), tone(3000), tone(10))
                    self.assertEqual(harness.topics(), ["hear.wake"])
                    self.assertTrue(voice.expecting)
                    await harness.event("state.changed", state="attending", prev="idle", muted=False)
                    async with asyncio.timeout(1):
                        while not player.commands:
                            await real_sleep(0)
                    self.assertEqual(player.commands[0][:4], ("aplay", "-q", "-D", "plughw:0,0"))
                    self.assertIn("ears-", player.commands[0][4])

                    # Frames heard while the earcon plays reach the recognizer as silence.
                    await harness.frames(tone(2000))
                    self.assertEqual(harness.recognizer.fed[-1][0], SILENCE)
                    player.release.set()
                    async with asyncio.timeout(1):
                        while not voice.chirp_task.done():
                            await real_sleep(0)
                    await harness.frames(tone(2000))
                    self.assertEqual(harness.recognizer.fed[-1][0], SILENCE)
                    harness.clock.now += CHIRP_TAIL_SECONDS + 0.01
                    await harness.frames(tone(2000))
                    self.assertEqual(harness.recognizer.fed[-1][0], tone(2000))
                    voice.disarm()
        wake = next(line for line in logs.output if "heard the wake phrase" in line)
        self.assertIn("score -4200", wake)
        self.assertIn(f"peak {dbfs(3000):.1f} dBFS", wake)
        self.assertTrue(any("capture open on plughw:0,0" in line for line in logs.output))
        self.assertFalse(voice.expecting)

    async def test_a_failed_earcon_keeps_listening(self):
        with tempfile.TemporaryDirectory() as work:
            harness = Harness([WAKE], chirp_bank=ChirpBank(Path(work)), chirps=True)

            @asynccontextmanager
            async def broken(*command, **options):
                raise FileNotFoundError("aplay")
                yield

            with patch("mind.voice.child", broken), self.assertLogs("being.voice", "WARNING") as logs:
                async with asyncio.TaskGroup() as harness.voice.tasks:
                    harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                    await harness.frames(tone(500))
                    await harness.event("state.changed", state="attending", prev="idle", muted=False)
                    async with asyncio.timeout(1):
                        while harness.voice.chirp_task is None or not harness.voice.chirp_task.done():
                            await real_sleep(0)
                    harness.clock.now += CHIRP_TAIL_SECONDS + 0.01
                    await harness.frames(tone(700))
                    self.assertEqual(harness.recognizer.fed[-1][0], tone(700))
                    harness.voice.disarm()
        self.assertIn("could not play the ears chirp", logs.output[0])


class Transcripts(unittest.IsolatedAsyncioTestCase):
    async def run_script(self, script, frames, state="attending", level="INFO", expecting=False):
        harness = Harness(script)
        harness.voice.state = state
        harness.voice.expecting = expecting
        with self.assertLogs("being.voice", level) as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(*frames)
                harness.voice.disarm()
        return harness, logs.output

    async def test_final_logs_length_and_confidence_but_text_only_at_debug(self):
        script = [START, (True, [("hear.partial", {"text": "what time"})]),
                  (False, [("hear.final", {"text": "what time is it"})])]
        harness, output = await self.run_script(script, [tone(4000)] * 3, expecting=True)
        self.assertEqual(harness.topics(), ["hear.speech_start", "hear.partial", "hear.final"])
        self.assertEqual(harness.sent[-1][1], {"text": "what time is it", "speaker": "Richard"})
        self.assertTrue(any("dictation started while attending" in line for line in output))
        self.assertTrue(any("partial transcript: 9 characters" in line for line in output))
        final = next(line for line in output if "dictation ended" in line)
        self.assertIn("final transcript of 15 characters, confidence 0.83", final)
        self.assertIn(f"peak {dbfs(4000):.1f} dBFS", final)
        self.assertFalse(any("what time" in line for line in output))
        self.assertFalse(harness.voice.expecting)

        _, output = await self.run_script(script, [tone(4000)] * 3, level="DEBUG")
        self.assertTrue(any("'what time is it'" in line and line.startswith("DEBUG") for line in output))

    async def test_failed_dictation_after_the_wake_phrase_asks_to_be_repeated(self):
        script = [START, (False, [("hear.cancel", {})])]
        harness, output = await self.run_script(script, [tone(4000)] * 2, expecting=True)
        self.assertEqual(harness.topics(), ["hear.speech_start", "hear.cancel", "hear.missed"])
        self.assertEqual(harness.sent[-1][1], {"reason": "unclear"})
        self.assertNotIn("text", harness.sent[-1][1])
        self.assertTrue(any("with no words" in line for line in output))

    async def test_a_short_noise_in_a_follow_up_window_stays_quiet_but_a_long_one_does_not(self):
        script = [START, (False, [("hear.cancel", {})])]
        harness, _ = await self.run_script(script, [tone(4000)] * 2)
        self.assertNotIn("hear.missed", harness.topics())

        long_script = [START, *[SPEECH] * 20, (False, [("hear.cancel", {})])]
        harness, _ = await self.run_script(long_script, [tone(4000)] * 22)
        self.assertEqual(harness.topics()[-1], "hear.missed")


class MissedSpeech(unittest.IsolatedAsyncioTestCase):
    async def test_attention_closing_after_the_wake_phrase_reports_no_speech(self):
        harness = Harness()
        voice = harness.voice
        voice.capture = Mock(done=Mock(return_value=False))
        voice.tasks = Mock()
        voice.expecting = True
        await harness.event("state.changed", state="attending", muted=False)
        self.assertEqual(harness.sent, [])
        await harness.event("state.changed", state="idle", muted=False)
        self.assertEqual(harness.sent, [("hear.missed", {"reason": "no_speech"})])
        self.assertFalse(voice.expecting)

        harness.sent.clear()
        await harness.event("state.changed", state="attending", muted=False)
        await harness.event("state.changed", state="idle", muted=False)
        self.assertEqual(harness.sent, [])

    async def test_an_answered_or_muted_wake_reports_nothing(self):
        for closing in ({"state": "thinking", "muted": False}, {"state": "idle", "muted": True}):
            with self.subTest(closing=closing):
                harness = Harness()
                harness.voice.capture = Mock(done=Mock(return_value=False))
                harness.voice.tasks = Mock()
                harness.voice.stop_capture = AsyncMock()
                harness.voice.expecting = True
                await harness.event("state.changed", state="attending", muted=False)
                await harness.event("state.changed", **closing)
                self.assertEqual(harness.sent, [])
                self.assertFalse(harness.voice.expecting)

    async def test_a_reply_clears_a_wake_the_brainstem_did_not_answer(self):
        # An ignored wake, then an announcement from idle, then its follow-up window lapsing.
        harness = Harness()
        harness.voice.capture = Mock(done=Mock(return_value=False))
        harness.voice.tasks = Mock()
        harness.voice.stop_capture = AsyncMock()
        harness.voice.expecting = True
        for state in ("thinking", "speaking", "attending", "idle"):
            await harness.event("state.changed", state=state, muted=False)
        self.assertEqual(harness.sent, [])
        self.assertFalse(harness.voice.expecting)

    async def test_missed_speech_carries_the_lid_reading(self):
        harness = Harness()
        harness.voice.lid_seq = 7
        harness.voice.capture = Mock(done=Mock(return_value=False))
        harness.voice.tasks = Mock()
        harness.voice.expecting = True
        harness.voice.state = "listening"
        await harness.event("state.changed", state="drowsy", muted=False)
        self.assertEqual(harness.sent, [("hear.missed", {"reason": "no_speech", "lid_seq": 7})])


class CaptureRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_microphone_is_reopened_with_growing_pauses(self):
        harness = Harness()
        voice = harness.voice
        attempts = []
        stream = harness.stream

        @asynccontextmanager
        async def recording():
            attempts.append(len(attempts))
            if len(attempts) <= 3:
                raise OSError("Device or resource busy")
            yield stream

        voice.recording = recording
        pauses = []

        async def pause(seconds):
            pauses.append(seconds)
            await real_sleep(0)

        with patch("mind.voice.asyncio.sleep", pause), self.assertLogs("being.voice", "WARNING") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(100))
                self.assertFalse(voice.capture.done())
                await stream.frames.put(None)
                async with asyncio.timeout(1):
                    while len(attempts) < 5:
                        await real_sleep(0)
                voice.disarm()
        self.assertEqual(pauses[:4], [CAPTURE_RETRY_FIRST, 2 * CAPTURE_RETRY_FIRST, 4 * CAPTURE_RETRY_FIRST,
                                      8 * CAPTURE_RETRY_FIRST])
        self.assertIn("capture failed on plughw:0,0: Device or resource busy", logs.output[0])
        self.assertTrue(any("microphone stream closed" in line for line in logs.output))
        self.assertEqual(harness.recognizer.captures, 2)

    async def test_capture_failure_reports_the_arecord_status(self):
        harness = Harness()
        voice = harness.voice

        @asynccontextmanager
        async def recording():
            voice.capture_process = Mock(returncode=1)
            yield harness.stream

        voice.recording = recording

        async def pause(seconds):
            voice.lid_open = False
            await real_sleep(0)

        with patch("mind.voice.asyncio.sleep", pause), self.assertLogs("being.voice", "WARNING") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.stream.frames.put(None)
                await asyncio.wait_for(voice.capture, 1)
        self.assertIn("(arecord exit status 1)", logs.output[0])

    async def test_a_dictation_cut_off_by_a_capture_failure_is_cancelled(self):
        harness = Harness([START, SPEECH])
        voice = harness.voice
        voice.state = "attending"

        async def pause(seconds):
            voice.lid_open = False
            await real_sleep(0)

        with patch("mind.voice.asyncio.sleep", pause), self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(4000), tone(4000))
                await harness.stream.frames.put(None)
                await asyncio.wait_for(voice.capture, 1)
        self.assertEqual(harness.topics(), ["hear.speech_start", "hear.cancel"])
        self.assertTrue(any("dictation ended by the capture failure" in line for line in logs.output))
        self.assertIsNone(voice.dictation)


class PlaybackRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_playback_is_retried_then_reported(self):
        voice = Voice(Mock(), "Richard", [], "plughw:0,0")
        voice.play_once = AsyncMock(side_effect=[RuntimeError("aplay exit status 1"), None])
        pauses = []

        async def pause(seconds):
            pauses.append(seconds)
            await real_sleep(0)

        with patch("mind.voice.asyncio.sleep", pause), self.assertLogs("being.voice", "WARNING"):
            await voice.play(Path("clause.wav"), first=True)
        self.assertEqual([item.args[1] for item in voice.play_once.await_args_list], [True, True])
        self.assertEqual(pauses, [0.2])

        voice.play_once = AsyncMock(side_effect=RuntimeError("aplay exit status 1"))
        with patch("mind.voice.asyncio.sleep", pause), self.assertLogs("being.voice", "WARNING"):
            with self.assertRaisesRegex(RuntimeError, "could not play the reply on plughw:0,0"):
                await voice.play(Path("clause.wav"), first=False)
        self.assertEqual(voice.play_once.await_count, PLAYBACK_ATTEMPTS)

    async def test_a_clause_that_cannot_play_or_synthesize_is_skipped_and_the_reply_still_ends(self):
        voice = Voice(Mock(), "Richard", [], "")
        voice.send_audio = AsyncMock()
        played = []

        async def play(audio, first):
            played.append(first)
            if len(played) == 1:
                raise RuntimeError("ALSA could not play the reply")

        @asynccontextmanager
        async def fake_child(*command, **options):
            yield Mock(wait=AsyncMock(return_value=1 if command[-3] == "Broken." else 0))

        for text, last in (("First.", False), ("Broken.", False), ("Third.", False), ("", True)):
            voice.clauses.put_nowait({"text": text, "last": last})
        with patch("mind.voice.child", fake_child), patch.object(voice, "play", play), \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")), \
                self.assertLogs("being.voice", "WARNING") as logs:
            await asyncio.wait_for(voice.speak(), 2)
        self.assertEqual(played, [True, True])
        voice.send_audio.assert_any_await("say.done", final=True, failed=True)
        self.assertTrue(any("could not synthesize a clause of 7 characters" in line for line in logs.output))
        self.assertTrue(any("skipping a clause that could not play" in line for line in logs.output))

    async def test_a_stuck_speaker_is_not_tried_again(self):
        voice = Voice(Mock(), "Richard", [], "plughw:0,0")
        voice.play_once = AsyncMock(side_effect=PlaybackStuck("ALSA playback did not finish"))
        with self.assertRaisesRegex(RuntimeError, "did not finish"):
            await voice.play(Path("clause.wav"), first=True)
        self.assertEqual(voice.play_once.await_count, 1)

    async def test_an_unexpected_reply_failure_still_ends_the_reply(self):
        voice = Voice(Mock(), "Richard", [], "")
        voice.send_audio = AsyncMock()
        voice.clauses.put_nowait({"text": "Pending.", "last": False})
        with patch("mind.voice.tempfile.TemporaryDirectory", side_effect=OSError("no space")), \
                self.assertLogs("being.voice", "ERROR"):
            await voice.speak()
        self.assertEqual([item for item in voice.send_audio.await_args_list if item.args[0] == "say.done"],
                         [call("say.done", final=True, failed=True)])
        self.assertTrue(voice.clauses.empty())


class MicrophoneGain(unittest.TestCase):
    def test_levels_and_clipping_gain(self):
        samples = pcm(tone(1000))
        self.assertEqual(levels(samples), (1000, 1000.0))
        self.assertEqual(levels(array("h")), (0, 0.0))
        self.assertAlmostEqual(dbfs(32768), 0.0)
        self.assertEqual(dbfs(0), -120.0)
        self.assertEqual(pcm(amplify(samples, 2.0))[:2].tolist(), [2000, -2000])
        self.assertEqual(pcm(amplify(pcm(tone(20000)), 4.0))[:2].tolist(), [32767, -32768])

    def test_environment_gain_is_parsed_and_clamped(self):
        self.assertEqual(mic_gain({}), 1.0)
        self.assertEqual(mic_gain({"BEING_MIC_GAIN": " 2.5 "}), 2.5)
        with self.assertLogs("being.voice", "WARNING"):
            for raw in ("loud", "0", "-3", "nan", "inf"):
                self.assertEqual(mic_gain({"BEING_MIC_GAIN": raw}), 1.0, raw)
        with self.assertLogs("being.voice", "WARNING"):
            self.assertEqual(mic_gain({"BEING_MIC_GAIN": "100"}), 16.0)
            self.assertEqual(mic_gain({"BEING_MIC_GAIN": "0.01"}), 0.1)

    def test_digital_silence_is_never_lifted(self):
        mic = MicLevel()
        for _ in range(500):
            mic.observe(0, 0.0, False)
        self.assertEqual(mic.gain, 1.0)
        self.assertEqual(mic.floor, -120.0)

    def test_a_quiet_floor_is_lifted_slowly_and_a_loud_peak_drops_the_lift_at_once(self):
        mic = MicLevel()
        quiet = 32768 * 10 ** (-80 / 20)
        for _ in range(20):
            mic.observe(quiet * 2, quiet, False)
        self.assertGreater(mic.gain, 1.0)
        self.assertLess(mic.gain, 1.5)
        for _ in range(400):
            mic.observe(quiet * 2, quiet, False)
        self.assertAlmostEqual(mic.gain, MAX_AUTO_GAIN)

        # Speech never raises the lift, and a peak near full scale lowers it at once.
        lift = mic.gain
        mic.observe(8000, 3000, True)
        self.assertLessEqual(mic.gain, lift)
        mic.observe(16000, 6000, True)
        self.assertLessEqual(dbfs(16000 * mic.gain), -3.0 + 1e-9)

    def test_a_normal_floor_is_left_alone_and_manual_gain_is_fixed(self):
        mic = MicLevel()
        normal = 32768 * 10 ** (-50 / 20)
        for _ in range(300):
            mic.observe(normal * 3, normal, False)
        self.assertEqual(mic.gain, 1.0)

        mic = MicLevel(3.0, auto=False)
        for _ in range(300):
            mic.observe(0, 0.0, False)
            mic.observe(30000, 20000, True)
        self.assertEqual(mic.gain, 3.0)

    def test_gain_is_applied_before_recognition(self):
        harness = Harness([SPEECH], mic=MicLevel(2.0, auto=False))

        async def run():
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(tone(1000))
                harness.voice.disarm()

        asyncio.run(run())
        self.assertEqual(harness.recognizer.fed[0][0], tone(2000))


class IdleReport(unittest.IsolatedAsyncioTestCase):
    async def test_idle_noise_floor_is_reported_and_digital_silence_is_named(self):
        harness = Harness([(False, []), SPEECH, (False, [])])
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(SILENCE, tone(3000), SILENCE)
                harness.clock.now += FLOOR_FIRST_REPORT_SECONDS
                await harness.frames(SILENCE)
                harness.voice.disarm()
        report = next(line for line in logs.output if "listening for the wake phrase" in line)
        self.assertIn("noise floor", report)
        self.assertIn(f"loudest {dbfs(3000):.1f} dBFS, 1 sounds, mic gain 1.00x", report)
        self.assertIn("digital silence", report)


class RecognizerDiagnostics(unittest.TestCase):
    def recognizer(self):
        recognizer = Recognizer.__new__(Recognizer)
        recognizer.lib = Mock()
        recognizer.decoder = Mock()
        return recognizer

    def test_confidence_converts_the_log_posterior(self):
        recognizer = self.recognizer()
        recognizer._get_prob = Mock(return_value=-2000)
        recognizer._get_logmath = Mock(return_value=1234)
        recognizer._logmath_exp = Mock(side_effect=lambda logmath, value: math.exp(value / 10000))
        self.assertAlmostEqual(recognizer.confidence(), math.exp(-0.2))
        recognizer._logmath_exp = Mock(return_value=3.0)
        self.assertIsNone(recognizer.confidence())
        # A raw 0 is the library saying it computed no posterior, not certainty.
        recognizer._get_prob = Mock(return_value=0)
        recognizer._logmath_exp = Mock(return_value=1.0)
        self.assertIsNone(recognizer.confidence())
        del recognizer._get_prob
        self.assertIsNone(recognizer.confidence())

    def test_wake_score_comes_from_the_detected_keyword_segment(self):
        recognizer = self.recognizer()
        recognizer._seg_prob = Mock(return_value=-1850)
        self.assertEqual(recognizer.segment_score(4321), -1850)
        self.assertEqual(recognizer._seg_prob.call_args.args[0], 4321)
        recognizer._seg_prob = Mock(return_value=Mock())
        self.assertIsNone(recognizer.segment_score(4321))
        recognizer._seg_prob = None
        self.assertIsNone(recognizer.segment_score(4321))

        # The score of the segment that set the keyword's end is the one reported.
        recognizer = self.recognizer()
        recognizer._seg_prob = Mock(return_value=-900)
        recognizer.lib.ps_seg_iter.return_value = 11
        recognizer.lib.ps_seg_next.return_value = None
        recognizer.lib.ps_endpointer_speech_start.return_value = 1.0
        recognizer.endpoint = Mock()
        recognizer.search_start = 1.0
        recognizer.wake_end = None
        recognizer.wake_candidate_score = None

        def frames(segment, start, end):
            start._obj.value, end._obj.value = 5, 60

        recognizer.lib.ps_seg_frames.side_effect = frames
        recognizer.track_wake()
        self.assertAlmostEqual(recognizer.wake_end, 1.6)
        self.assertEqual(recognizer.wake_candidate_score, -900)


if __name__ == "__main__":
    unittest.main()
