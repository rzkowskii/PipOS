import tempfile
import threading
import time
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch

from mind import asr_sherpa, sherpa
from mind.asr_sherpa import SherpaRecognizer, strip_wake
from mind.recognition import FRAME_BYTES, FRAME_SAMPLES, RecognizerEngine
from tests.test_sherpa import FakeLibrary


def frames(seconds, level=0.0):
    """30 ms frames of 16-bit PCM: silence, or a loud square wave."""
    value = int(level * 32767)
    frame = array("h", (value if i % 2 else -value for i in range(FRAME_SAMPLES))).tobytes()
    assert len(frame) == FRAME_BYTES
    return [frame] * round(seconds / 0.03)


class FakeWake(RecognizerEngine):
    """PocketSphinx stand-in: scripted events at given feed counts."""

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.fed = []
        self.starts = self.stops = self.closes = 0

    def start_capture(self):
        self.starts += 1

    def stop_capture(self):
        self.stops += 1

    def close(self):
        self.closes += 1

    def feed(self, frame, attending, oneshot=True):
        self.fed.append(attending)
        return list(self.script.pop(len(self.fed), []))


class FakeMoonshine:
    def __init__(self, texts=("what time is it",), delay=0.0, error=None):
        self.texts = list(texts)
        self.delay = delay
        self.error = error
        self.seen = []
        self.closed = False

    def transcribe(self, samples, rate=16000):
        self.seen.append(len(samples))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.texts.pop(0) if self.texts else ""

    def close(self):
        self.closed = True


def engine(wake=None, moonshine=None, **options):
    vad = asr_sherpa.load_vad(Path("/models/asr"), library=FakeLibrary())
    return SherpaRecognizer(wake or FakeWake(), vad, moonshine or FakeMoonshine(), **options)


def run(recognizer, chunks, attending=True):
    events = []
    for frame in chunks:
        events.extend(recognizer.feed(frame, attending))
    return events


def until_final(recognizer, attending=True, timeout=3.0):
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events.extend(recognizer.feed(frames(0.03)[0], attending))
        if any(topic == "hear.final" for topic, _ in events):
            return events
        time.sleep(0.005)
    return events


class WakeScoreTests(unittest.TestCase):
    def test_the_wake_spotters_score_is_the_engines(self):
        wake = FakeWake()
        recognizer = engine(wake=wake)
        self.assertIsNone(recognizer.wake_score)
        wake.wake_score = -1805
        self.assertEqual(recognizer.wake_score, -1805)
        recognizer.close()


class AttendingTests(unittest.TestCase):
    def test_event_order_speech_start_then_one_final_and_no_partials(self):
        recognizer = engine(moonshine=FakeMoonshine(["set a timer for five minutes"]))
        recognizer.start_capture()
        events = run(recognizer, frames(0.3) + frames(1.0, 0.3) + frames(0.45))
        events += until_final(recognizer)
        topics = [topic for topic, _ in events]
        self.assertEqual(topics, ["hear.speech_start", "hear.final"])
        self.assertEqual(events[1][1], {"text": "set a timer for five minutes"})
        recognizer.close()

    def test_segment_keeps_audio_from_before_detection(self):
        moonshine = FakeMoonshine()
        recognizer = engine(moonshine=moonshine)
        run(recognizer, frames(0.6) + frames(1.0, 0.3) + frames(0.45))
        until_final(recognizer)
        # 1.0 s of speech, plus pre-roll before Silero's 0.25 s onset rule, plus the 0.3 s tail.
        self.assertGreater(moonshine.seen[0], int(1.5 * 16000))
        recognizer.close()

    def test_an_empty_result_is_an_empty_final(self):
        recognizer = engine(moonshine=FakeMoonshine([""]))
        events = run(recognizer, frames(0.8, 0.3) + frames(0.45)) + until_final(recognizer)
        self.assertEqual(events[-1], ("hear.final", {"text": ""}))
        recognizer.close()

    def test_feed_stays_fast_during_a_long_decode_with_one_worker(self):
        moonshine = FakeMoonshine(["one", "two"], delay=0.8)
        recognizer = engine(moonshine=moonshine)
        slowest = 0.0
        for _ in range(2):
            run(recognizer, frames(0.8, 0.3) + frames(0.45))
            self.assertIsNotNone(recognizer.decoding)
            events = []
            deadline = time.monotonic() + 3
            while not events and time.monotonic() < deadline:
                start = time.perf_counter()
                events = recognizer.feed(frames(0.03)[0], True)
                slowest = max(slowest, time.perf_counter() - start)
                time.sleep(0.01)
            self.assertEqual(events[0][0], "hear.final")
        workers = [t for t in threading.enumerate() if t.name == "being-asr-decode"]
        self.assertEqual(len(workers), 1)
        self.assertLessEqual(slowest, 0.002)
        recognizer.close()
        self.assertFalse(any(t.name == "being-asr-decode" and t.is_alive() for t in threading.enumerate()))

    def test_stop_capture_drops_buffers_and_a_pending_result(self):
        recognizer = engine(moonshine=FakeMoonshine(["stale words"], delay=0.2))
        run(recognizer, frames(0.8, 0.3) + frames(0.45))
        self.assertIsNotNone(recognizer.decoding)
        recognizer.stop_capture()
        self.assertEqual((recognizer.segment, len(recognizer.history)), ([], 0))
        time.sleep(0.3)
        recognizer.start_capture()
        events = run(recognizer, frames(0.3))
        self.assertNotIn("hear.final", [topic for topic, _ in events])
        recognizer.close()

    def test_mid_speech_stop_leaves_nothing_behind(self):
        recognizer = engine()
        events = run(recognizer, frames(0.6, 0.3))
        self.assertEqual([topic for topic, _ in events], ["hear.speech_start"])
        recognizer.stop_capture()
        self.assertFalse(recognizer.in_speech)
        self.assertEqual(recognizer.segment_samples, 0)
        recognizer.close()

    def test_fifteen_seconds_closes_the_segment(self):
        moonshine = FakeMoonshine(["a long ramble"])
        recognizer = engine(moonshine=moonshine)
        run(recognizer, frames(15.5, 0.3))
        self.assertIsNotNone(recognizer.decoding)
        until_final(recognizer)
        self.assertTrue(15 * 16000 <= moonshine.seen[0] <= 15 * 16000 + 2 * FRAME_SAMPLES, moonshine.seen)
        recognizer.close()

    def test_three_failures_hand_dictation_back_to_pocketsphinx(self):
        wake = FakeWake()
        self.enterContext(patch.object(asr_sherpa, "log"))
        recognizer = engine(wake=wake, moonshine=FakeMoonshine(error=RuntimeError("bad graph")))
        for _ in range(3):
            run(recognizer, frames(0.8, 0.3) + frames(0.45))
            self.assertEqual(until_final(recognizer)[-1], ("hear.final", {"text": ""}))
        self.assertTrue(recognizer.broken)
        before = len(wake.fed)
        recognizer.feed(frames(0.03)[0], True)
        self.assertEqual(wake.fed[before:], [True])
        recognizer.close()


class RestingTests(unittest.TestCase):
    def test_resting_frames_go_to_the_wake_engine_and_its_wake_passes_through(self):
        wake = FakeWake({3: [("hear.sound", {})], 5: [("hear.wake", {})]})
        recognizer = engine(wake=wake)
        events = run(recognizer, frames(0.15, 0.3), attending=False)
        self.assertEqual(events, [("hear.sound", {}), ("hear.wake", {})])
        self.assertEqual(wake.fed, [False] * 5)
        recognizer.close()

    def test_wake_dictation_events_are_not_passed_on(self):
        wake = FakeWake({1: [("hear.partial", {"text": "x"}), ("hear.final", {"text": "x"})]})
        recognizer = engine(wake=wake)
        self.assertEqual(run(recognizer, frames(0.03), attending=False), [])
        recognizer.close()

    def test_returning_to_rest_restarts_the_wake_endpointer(self):
        wake = FakeWake()
        recognizer = engine(wake=wake)
        run(recognizer, frames(0.09), attending=True)
        run(recognizer, frames(0.03), attending=False)
        self.assertEqual((wake.stops, wake.starts), (1, 1))
        recognizer.close()

    def test_one_breath_is_transcribed_from_its_start_without_the_name(self):
        wake = FakeWake({20: [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})]})
        moonshine = FakeMoonshine(["Hey Pip, what time is it?"])
        recognizer = engine(wake=wake, moonshine=moonshine)
        events = run(recognizer, frames(0.6, 0.3), attending=False)
        self.assertEqual(events, [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})])
        self.assertEqual((wake.stops, wake.starts), (1, 1))
        events = run(recognizer, frames(0.5, 0.3) + frames(0.45), attending=True) + until_final(recognizer)
        self.assertEqual(events[-1], ("hear.final", {"text": "what time is it?", "oneshot": True}))
        self.assertNotIn("hear.speech_start", [topic for topic, _ in events])
        # The breath from its start: 0.6 s before the wake report plus what followed.
        self.assertGreater(moonshine.seen[0], int(1.1 * 16000))
        recognizer.close()

    def test_one_breath_starts_where_pocketsphinx_says_the_name_ended(self):
        """Speech before the name was not addressed to Pip: it is not transcribed."""
        wake = FakeWake({20: [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})]})
        # PocketSphinx's replay queue: the 5 frames from the end of the name to now.
        wake.oneshot_queue = [b""] * 5
        moonshine = FakeMoonshine(["what time is it"])
        recognizer = engine(wake=wake, moonshine=moonshine)
        before = array("h", [1234] * FRAME_SAMPLES).tobytes()
        run(recognizer, [before] * 15 + frames(0.15, 0.3), attending=False)
        self.assertEqual(recognizer.segment_samples, 5 * FRAME_SAMPLES)
        self.assertNotIn(round(1234 / 32768, 6), {round(x, 6) for part in recognizer.segment for x in part})
        events = run(recognizer, frames(0.5, 0.3) + frames(0.45), attending=True) + until_final(recognizer)
        self.assertEqual(events[-1], ("hear.final", {"text": "what time is it", "oneshot": True}))
        self.assertLess(moonshine.seen[0], int(1.2 * 16000))
        recognizer.close()

    def test_one_breath_that_ends_at_once_is_still_decoded(self):
        wake = FakeWake({10: [("hear.wake", {"oneshot": True})]})
        recognizer = engine(wake=wake, moonshine=FakeMoonshine(["hey pip stop"]))
        run(recognizer, frames(0.3, 0.3), attending=False)
        events = run(recognizer, frames(0.7), attending=False) + until_final(recognizer, attending=False)
        self.assertEqual(events[-1], ("hear.final", {"text": "stop", "oneshot": True}))
        recognizer.close()


class WakeStripTests(unittest.TestCase):
    def test_strip(self):
        cases = {
            "Hey Pip, what time is it?": "what time is it?",
            "hey pip set a timer": "set a timer",
            "Pip. Open the terminal.": "Open the terminal.",
            "um okay hey Pip what's the date": "what's the date",
            "what time is it": "what time is it",
            "tell me about a pip in an apple core": "tell me about a pip in an apple core",
        }
        for heard, expected in cases.items():
            self.assertEqual(strip_wake(heard, "Hey Pip"), expected, heard)
        self.assertEqual(strip_wake("Hey Clover, how are you", "Hey Clover"), "how are you")

    def test_a_misheard_name_goes_too(self):
        # Moonshine transcripts of one-breath requests measured on the laptop.
        cases = {
            "Pep open a terminal.": "open a terminal.",
            "Yep, close the terminal.": "close the terminal.",
            "I ate pip, are you okay?": "are you okay?",
            "Shake it openly terminal.": "Shake it openly terminal.",
            "No, close the terminal.": "No, close the terminal.",
            "Pip.": "",
            "Pep.": "Pep.",
        }
        for heard, expected in cases.items():
            self.assertEqual(strip_wake(heard, "Hey Pip"), expected, heard)


class FilesTests(unittest.TestCase):
    def test_missing_lists_what_is_absent_and_load_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library = root / "lib.so"
            absent = asr_sherpa.missing(root, library=library)
            self.assertEqual(len(absent), 5)
            with self.assertRaises(sherpa.Unavailable):
                SherpaRecognizer.load(FakeWake(), root=root, library=library)
            library.write_bytes(b"")
            (root / "silero_vad.onnx").write_bytes(b"")
            folder_tiny = root / "moonshine-tiny"
            folder_tiny.mkdir()
            for name in asr_sherpa.MOONSHINE_FILES:
                (folder_tiny / name).write_bytes(b"")
            self.assertEqual(asr_sherpa.missing(root, library=library), [])
            self.assertIsNone(asr_sherpa.test_wav(root))
            (folder_tiny / "test_wavs").mkdir()
            (folder_tiny / "test_wavs/0.wav").write_bytes(b"")
            self.assertEqual(asr_sherpa.test_wav(root), folder_tiny / "test_wavs/0.wav")


if __name__ == "__main__":
    unittest.main()


class SpeechFlagTests(unittest.TestCase):
    """The voice's mic levels read `in_speech` every frame, from either engine."""

    def test_while_resting_the_flag_is_the_wake_engines_endpointer(self):
        wake = FakeWake()
        recognizer = engine(wake=wake)
        wake.in_speech = True
        run(recognizer, frames(0.09, 0.3), attending=False)
        self.assertIs(recognizer.in_speech, True)
        wake.in_speech = False
        run(recognizer, frames(0.03), attending=False)
        self.assertIs(recognizer.in_speech, False)
        recognizer.close()

    def test_while_attending_the_flag_is_silero_not_the_wake_engine(self):
        wake = FakeWake()
        wake.in_speech = True       # stale from the rest: PocketSphinx is not fed now
        recognizer = engine(wake=wake)
        run(recognizer, frames(0.09), attending=True)
        self.assertIs(recognizer.in_speech, False)
        run(recognizer, frames(0.6, 0.3), attending=True)
        self.assertIs(recognizer.in_speech, True)
        recognizer.close()


class AttentionDropTests(unittest.TestCase):
    def test_an_announced_utterance_is_finished_when_attending_drops_under_it(self):
        """The follow-up window closes just after Silero heard the start: the
        words still end in a final, and are not cut from their start."""
        moonshine = FakeMoonshine(["and tomorrow"])
        recognizer = engine(moonshine=moonshine)
        events = run(recognizer, frames(0.6, 0.3), attending=True)
        self.assertEqual([topic for topic, _ in events], ["hear.speech_start"])
        events = run(recognizer, frames(0.4, 0.3) + frames(0.45), attending=False)
        events += until_final(recognizer, attending=False)
        self.assertEqual(events[-1], ("hear.final", {"text": "and tomorrow"}))
        self.assertNotIn("hear.speech_start", [topic for topic, _ in events])
        self.assertGreater(moonshine.seen[0], int(1.0 * 16000))
        # Then it goes back to resting as usual.
        run(recognizer, frames(0.03), attending=False)
        self.assertFalse(recognizer.was_attending)
        recognizer.close()

    def test_attending_dropping_in_silence_still_goes_straight_back_to_rest(self):
        wake = FakeWake()
        recognizer = engine(wake=wake)
        run(recognizer, frames(0.09), attending=True)
        run(recognizer, frames(0.03), attending=False)
        self.assertFalse(recognizer.was_attending)
        self.assertEqual(wake.fed, [False])
        recognizer.close()
