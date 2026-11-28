"""The one-breath window: "Hey Pip, <pause> <request>" must not be sent as noise.

Evidence label: host test with a stand-in for Silero, on synthetic audio. The
stand-in (tests/test_sherpa.py FakeLibrary) calls a 512-sample window speech
when its mean level is high enough; the audio here is generated in the test
(a noise floor with dither, and amplitude-modulated tones for speech). So this
shows the decision logic: which audio is sent to the recogniser, when a window
waits, and that the request is not heard as a second wake. It does not show
that real Silero finds no speech in the noise after "Hey Pip", nor what
Moonshine writes: lab/oneshot_window_replay.py does that on the laptop, with
the real models, on recorded windows. Moonshine here is a fake that counts the
audio it was given.
"""

import json
import math
import random
import tempfile
import unittest
from array import array
from pathlib import Path

from lab import oneshot_window_replay
from mind import asr_sherpa
from mind.recognition import FRAME_SAMPLES
from tests.test_asr_sherpa import FakeMoonshine, FakeWake, engine, frames, until_final

FRAME_BYTES = FRAME_SAMPLES * 2
RATE = 16000


def pcm(samples):
    out = array("h", (max(-32768, min(32767, int(x * 32767))) for x in samples))
    out.extend([0] * (-len(out) % FRAME_SAMPLES))
    data = out.tobytes()
    return [data[i:i + FRAME_BYTES] for i in range(0, len(data), FRAME_BYTES)]


def floor(seconds, seed=1, level=0.004):
    """A noise floor with dither: far below what the stand-in calls speech."""
    rng = random.Random(seed)
    return [rng.uniform(-level, level) for _ in range(int(seconds * RATE))]


def speech(seconds):
    """Speech-like audio: a few harmonics, 4 Hz amplitude modulation that never gets quiet."""
    return [(0.22 + 0.08 * math.sin(2 * math.pi * 4 * n / RATE))
            * (math.sin(2 * math.pi * 140 * n / RATE) + 0.5 * math.sin(2 * math.pi * 280 * n / RATE))
            for n in range(int(seconds * RATE))]


# What the wake replays after the name, as 30 ms frames of 16-bit PCM.
NOISE_WINDOW = pcm(floor(0.15, seed=2, level=0.02) + floor(1.4, seed=3))      # the name's tail, then the floor
REQUEST = pcm(speech(1.18))                                                     # "what time is it?"
WINDOW_WITH_REQUEST = pcm(speech(1.0) + floor(0.56, seed=4))                   # "Hey Pip, what time is it?"
WINDOW_WITH_SHORT_REQUEST = pcm(speech(0.6) + floor(0.96, seed=5))             # "Hey Pip, brighter."


def quiet(seconds):
    return frames(seconds)


def start(replay, moonshine=None, **options):
    """A recognizer that heard `replay` as the audio after the name, then spotted a one-breath wake."""
    wake = FakeWake({len(replay): [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})]})
    wake.oneshot_queue = [b""] * len(replay)
    recognizer = engine(wake=wake, moonshine=moonshine or FakeMoonshine(["hey pip what time is it"]), **options)
    events = []
    for frame in replay:
        events.extend(recognizer.feed(frame, False))
    return recognizer, wake, events


def feed(recognizer, chunks, attending=True):
    events = []
    for frame in chunks:
        events.extend(recognizer.feed(frame, attending))
    return events


class PauseAfterTheNameTests(unittest.TestCase):
    def test_a_window_of_noise_is_not_sent_it_waits_for_the_request(self):
        moonshine = FakeMoonshine(["what time is it"])
        recognizer, wake, events = start(NOISE_WINDOW, moonshine)
        self.assertEqual([topic for topic, _ in events], ["hear.wake", "hear.speech_start"])
        # Well past the 0.63 s that used to close the window.
        events = feed(recognizer, quiet(1.5))
        self.assertEqual(events, [])
        self.assertIsNone(recognizer.decoding)
        self.assertEqual(moonshine.seen, [])
        # The request arrives 1.5 s after the name.
        events = feed(recognizer, REQUEST + quiet(0.6))
        events += until_final(recognizer)
        self.assertEqual(events[-1], ("hear.final", {"text": "what time is it", "oneshot": True}))
        self.assertEqual(len(moonshine.seen), 1)
        # Only the request (1.18 s plus Silero's pre-roll), not 1.5 s of noise before it.
        self.assertLess(moonshine.seen[0], int(2.0 * 16000))
        self.assertGreater(moonshine.seen[0], int(1.0 * 16000))
        recognizer.close()

    def test_the_request_is_not_heard_as_a_second_wake(self):
        recognizer, wake, _ = start(NOISE_WINDOW)
        fed = len(wake.fed)
        feed(recognizer, quiet(1.5) + REQUEST + quiet(0.6))
        self.assertEqual(len(wake.fed), fed)    # the wake engine never heard the request
        recognizer.close()

    def test_a_speech_start_follows_when_the_request_begins(self):
        recognizer, wake, _ = start(NOISE_WINDOW)
        feed(recognizer, quiet(1.2))
        self.assertFalse(recognizer.in_speech)
        events = feed(recognizer, REQUEST)
        self.assertEqual(events, [("hear.speech_start", {"oneshot": True})])
        recognizer.close()

    def test_nothing_said_ends_empty_after_the_wait(self):
        moonshine = FakeMoonshine(["so"])
        recognizer, wake, _ = start(NOISE_WINDOW, moonshine)
        events = feed(recognizer, quiet(4.5))
        self.assertEqual(events, [])
        events = feed(recognizer, quiet(1.0))
        self.assertEqual(events, [("hear.final", {"text": "", "oneshot": True})])
        self.assertEqual(moonshine.seen, [])
        self.assertFalse(recognizer.oneshot)
        recognizer.close()

    def test_a_pause_of_any_length_inside_the_wait_works(self):
        for pause in (0.2, 0.6, 1.0, 2.0, 3.5):
            with self.subTest(pause=pause):
                moonshine = FakeMoonshine(["open the terminal"])
                recognizer, wake, _ = start(quiet(1.0), moonshine)
                events = feed(recognizer, quiet(pause) + REQUEST + quiet(0.6))
                events += until_final(recognizer)
                self.assertEqual(events[-1], ("hear.final", {"text": "open the terminal", "oneshot": True}))
                recognizer.close()


class OneBreathStillWorksTests(unittest.TestCase):
    """No-regression checks: they pass on the code before the fix too, except where noted."""

    def test_a_request_inside_the_replay_is_sent_with_the_replayed_audio(self):
        for name, window, seconds in (("what time is it", WINDOW_WITH_REQUEST, 1.56),
                                      ("brighter", WINDOW_WITH_SHORT_REQUEST, 1.56)):
            with self.subTest(name=name):
                moonshine = FakeMoonshine(["hey pip what time is it"])
                recognizer, wake, _ = start(window, moonshine)
                events = feed(recognizer, quiet(0.9)) + until_final(recognizer)
                self.assertEqual(events[-1][0], "hear.final")
                self.assertTrue(events[-1][1]["oneshot"])
                self.assertGreater(moonshine.seen[0], int((seconds - 0.2) * 16000))
                recognizer.close()

    def test_speech_still_running_in_the_replay_is_followed_to_its_end(self):
        moonshine = FakeMoonshine(["hey pip set a timer for five minutes"])
        recognizer, wake, _ = start(frames(0.9, 0.3), moonshine)
        self.assertTrue(recognizer.vad_seen)
        events = feed(recognizer, frames(1.0, 0.3) + quiet(0.5)) + until_final(recognizer)
        self.assertEqual(events[-1], ("hear.final", {"text": "set a timer for five minutes", "oneshot": True}))
        self.assertGreater(moonshine.seen[0], int(1.9 * 16000))
        recognizer.close()

    def test_the_replays_own_silence_counts_towards_the_end(self):
        """Speech then 0.5 s of quiet inside the replay: the window ends on the first live frames."""
        moonshine = FakeMoonshine(["hey pip stop it"])
        recognizer, wake, _ = start(frames(0.6, 0.3) + quiet(0.5), moonshine)
        self.assertTrue(recognizer.vad_finished)
        feed(recognizer, quiet(0.06))
        self.assertIsNotNone(recognizer.decoding)
        recognizer.close()

    def test_a_window_that_never_reaches_silero_is_not_cut_short_by_the_wait(self):
        """Live speech with no speech in the replay (the name's tail only) is sent as before."""
        moonshine = FakeMoonshine(["hey pip stop"])
        recognizer, wake, _ = start(quiet(0.3), moonshine)
        events = feed(recognizer, frames(0.5, 0.3) + quiet(0.6)) + until_final(recognizer)
        self.assertEqual(events[-1], ("hear.final", {"text": "stop", "oneshot": True}))
        recognizer.close()


class ReplayScriptTests(unittest.TestCase):
    """lab/oneshot_window_replay.py, run here with the stand-ins; on the laptop it uses the real models."""

    def run_replay(self, window, request, moonshine):
        wake = oneshot_window_replay.OneBreathWake(len(window), len(window))
        recognizer = engine(wake=wake, moonshine=moonshine)
        try:
            return oneshot_window_replay.replay(recognizer, window, request, pause=1.5, tail=0.9)
        finally:
            recognizer.close()

    def test_the_noise_window_is_never_sent_and_only_the_request_is(self):
        result = self.run_replay(NOISE_WINDOW, REQUEST, FakeMoonshine(["what time is it"]))
        self.assertEqual(len(result["sent"]), 1, result)
        self.assertLess(result["sent"][0], 2.0)
        self.assertEqual(result["finals"], ["what time is it"])

    def test_a_window_that_holds_the_request_is_sent_as_one(self):
        result = self.run_replay(WINDOW_WITH_REQUEST, None, FakeMoonshine(["hey pip what time is it"]))
        self.assertEqual(len(result["sent"]), 1, result)
        self.assertGreater(result["sent"][0], 1.3)

    def test_a_wav_is_read_as_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.wav"
            import wave
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(bytes(2 * 16000))
            frames_read = oneshot_window_replay.read_frames(path)
        self.assertEqual(len(frames_read) * FRAME_SAMPLES, 16000 + (-16000 % FRAME_SAMPLES))


class WindowDumpTests(unittest.TestCase):
    def dumped(self, enabled):
        with tempfile.TemporaryDirectory() as folder:
            recognizer, wake, _ = start(frames(0.9, 0.3))
            recognizer.window_dump_enabled = enabled
            recognizer.window_dump_dir = folder
            feed(recognizer, frames(0.3, 0.3) + quiet(0.8))
            until_final(recognizer)
            files = sorted(path.name for path in Path(folder).iterdir())
            info = [json.loads(path.read_text()) for path in sorted(Path(folder).glob("*.json"))]
            recognizer.close()
            return files, info

    def test_off_unless_the_voice_turns_it_on(self):
        self.assertEqual(self.dumped(False), ([], []))

    def test_on_it_keeps_the_window_and_the_decision(self):
        files, info = self.dumped(True)
        self.assertEqual(len(files), 2)
        self.assertEqual(info[0]["decision"], "sent")
        self.assertGreater(info[0]["seconds"], 1.0)
        self.assertTrue(info[0]["vad_seen"])

    def test_only_the_newest_are_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            for index in range(asr_sherpa.DUMP_KEEP + 5):
                recognizer, wake, _ = start(frames(0.9, 0.3))
                recognizer.window_dump_enabled = True
                recognizer.window_dump_dir = folder
                recognizer.generation = index
                feed(recognizer, quiet(0.8))
                until_final(recognizer)
                recognizer.close()
            self.assertLessEqual(len(list(Path(folder).glob("*.wav"))), asr_sherpa.DUMP_KEEP)

    def test_a_waiting_window_is_dumped_too(self):
        with tempfile.TemporaryDirectory() as folder:
            recognizer, wake, _ = start(quiet(1.0))
            recognizer.window_dump_enabled = True
            recognizer.window_dump_dir = folder
            feed(recognizer, quiet(1.0))
            info = [json.loads(path.read_text()) for path in Path(folder).glob("*.json")]
            self.assertEqual([item["decision"] for item in info], ["waiting"])
            recognizer.close()


if __name__ == "__main__":
    unittest.main()
