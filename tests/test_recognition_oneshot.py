"""One-breath wake, the keyword decode cap and onset sounds with a scripted decoder."""

import struct
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from mind.recognition import (
    FRAME_BYTES, FRAME_SECONDS, SKIP, WAKE_DECODE_CAP_SECONDS, WAKE_PREROLL_FRAMES, WAKE_REPLAY_FRAMES,
    Recognizer, RecognizerEngine, engine_choice, sherpa_onnx_missing,
)

DICTATION = b"_default"


class FakeLib:
    """Stands in for libpocketsphinx: a scripted endpointer and a silent decoder."""

    def __init__(self, voiced, speech_end=None, hypothesis=b"what time is it"):
        self.voiced = voiced
        self.current = False
        self.speech_end = speech_end
        self.hyp = hypothesis
        self.ended = 0

    def ps_endpointer_in_speech(self, endpoint):
        return self.current

    def ps_endpointer_process(self, endpoint, samples):
        self.current = samples[0] in self.voiced
        return samples if self.current else None

    def ps_endpointer_speech_start(self, endpoint):
        return (min(self.voiced) - 1) * FRAME_SECONDS

    def ps_endpointer_speech_end(self, endpoint):
        return self.speech_end if self.speech_end is not None else max(self.voiced) * FRAME_SECONDS

    def ps_activate_search(self, decoder, mode):
        return 0

    def ps_start_utt(self, decoder):
        return 0

    def ps_end_utt(self, decoder):
        self.ended += 1
        return 0

    def ps_get_hyp(self, decoder, score):
        return self.hyp


def scripted(voiced, *, wake_frame=None, **options):
    """A recognizer whose keyword search 'hears' the name end at `wake_frame`."""
    recognizer = Recognizer.__new__(Recognizer)
    recognizer.lib = FakeLib(voiced, **options)
    recognizer.decoder = recognizer.endpoint = object()
    recognizer.dictation = DICTATION
    recognizer.mode = None
    recognizer.frames = 0
    recognizer.last_partial = ""
    recognizer.wake_end = None
    recognizer.wake_preroll = deque(maxlen=WAKE_PREROLL_FRAMES + 1)
    recognizer.capture_frames = 0
    recognizer.decoded = []

    def decode(samples):
        recognizer.decoded.append((recognizer.mode, samples[0]))
        recognizer.frames += 1

    def track_wake():
        if wake_frame is not None and recognizer.decoded[-1][1] >= wake_frame:
            recognizer.wake_end = recognizer.wake_end or wake_frame * FRAME_SECONDS

    recognizer.decode = decode
    recognizer.track_wake = track_wake
    return recognizer


def frame(index):
    return struct.pack("<h", index) + bytes(FRAME_BYTES - 2)


def run(recognizer, last, *, attending=False, oneshot=True, start=1):
    events = []
    largest_step = 0
    for index in range(start, last + 1):
        before = len(recognizer.decoded)
        events.extend(recognizer.feed(frame(index), attending, oneshot))
        if recognizer.lib.current:
            largest_step = max(largest_step, len(recognizer.decoded) - before)
    return events, largest_step


class OneBreath(unittest.TestCase):
    def test_speech_after_the_name_is_replayed_into_dictation_without_gaps(self):
        recognizer = scripted(set(range(41, 141)), wake_frame=60)
        events, step = run(recognizer, 160)
        topics = [topic for topic, _ in events]
        self.assertEqual(topics[0], "hear.sound")
        self.assertEqual(events[1:3], [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})])
        self.assertEqual(events[-1], ("hear.final", {"text": "what time is it", "oneshot": True}))
        self.assertIn("hear.partial", topics)
        dictated = [index for mode, index in recognizer.decoded if mode == DICTATION]
        # Everything after the name's last frame, once, in order, through the segment's end.
        self.assertEqual(dictated, list(range(61, 142)))
        self.assertLessEqual(step, WAKE_REPLAY_FRAMES + 1)
        self.assertFalse(recognizer.oneshot)
        self.assertIsNone(recognizer.mode)

    def test_a_trimmed_history_still_replays_from_the_end_of_the_name(self):
        recognizer = scripted(set(range(41, 141)), wake_frame=60)
        with patch("mind.recognition.WAKE_HISTORY_FRAMES", 40):
            events, _ = run(recognizer, 160)
        self.assertEqual(events[-1], ("hear.final", {"text": "what time is it", "oneshot": True}))
        dictated = [index for mode, index in recognizer.decoded if mode == DICTATION]
        self.assertEqual(dictated, list(range(61, 142)))

    def test_the_conversational_wake_rule_is_unchanged(self):
        recognizer = scripted(set(range(41, 66)), wake_frame=60, speech_end=60 * FRAME_SECONDS + 0.2)
        events, _ = run(recognizer, 90)
        self.assertEqual(events, [("hear.sound", {}), ("hear.wake", {})])
        self.assertFalse(any(mode == DICTATION for mode, _ in recognizer.decoded))

    def test_without_one_breath_permission_long_speech_is_skipped(self):
        recognizer = scripted(set(range(41, 141)), wake_frame=60)
        events, _ = run(recognizer, 100, oneshot=False)
        self.assertEqual(events, [("hear.sound", {})])
        self.assertEqual(recognizer.mode, SKIP)
        decoded = len(recognizer.decoded)
        run(recognizer, 160, oneshot=False, start=101)
        self.assertEqual(len(recognizer.decoded), decoded)
        self.assertIsNone(recognizer.mode)

    def test_keyword_decoding_stops_two_seconds_after_onset(self):
        recognizer = scripted(set(range(41, 400)))
        events, _ = run(recognizer, 420)
        self.assertEqual(events, [("hear.sound", {})])
        last_decoded = recognizer.decoded[-1][1]
        self.assertLessEqual((last_decoded - 41) * FRAME_SECONDS, WAKE_DECODE_CAP_SECONDS + 0.1)
        # The next segment is searched afresh.
        recognizer.lib.voiced = set(range(430, 450))
        recognizer.lib.speech_end = None
        events, _ = run(recognizer, 460, start=421)
        self.assertEqual(events, [("hear.sound", {})])
        self.assertGreater(recognizer.decoded[-1][1], 421)

    def test_no_onset_sound_while_attending(self):
        recognizer = scripted(set(range(41, 60)))
        events, _ = run(recognizer, 80, attending=True)
        self.assertNotIn("hear.sound", [topic for topic, _ in events])
        self.assertIn("hear.speech_start", [topic for topic, _ in events])

    def test_stop_capture_resets_a_one_breath_in_progress(self):
        recognizer = scripted(set(range(41, 300)), wake_frame=60)
        run(recognizer, 120)
        self.assertTrue(recognizer.oneshot)
        recognizer.endpoint = None
        recognizer.stop_capture()
        self.assertFalse(recognizer.oneshot)
        self.assertIsNone(recognizer.mode)
        self.assertFalse(recognizer.oneshot_queue)


class Engines(unittest.TestCase):
    def test_pocketsphinx_is_the_default_and_unknown_names_fall_back(self):
        self.assertEqual(engine_choice(None), ("pocketsphinx", None))
        engine, note = engine_choice("whisper")
        self.assertEqual(engine, "pocketsphinx")
        self.assertIn("Unknown", note)

    def test_sherpa_onnx_reports_what_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            missing = sherpa_onnx_missing(root, Path(root) / "lib.so")
            self.assertIn(str(Path(root) / "lib.so"), missing)
            self.assertIn(str(Path(root) / "silero_vad.onnx"), missing)
            engine, note = engine_choice("sherpa-onnx", root, Path(root) / "lib.so")
            self.assertEqual(engine, "pocketsphinx")
            self.assertIn("not installed", note)

    def test_the_interface_is_explicit(self):
        self.assertTrue(issubclass(Recognizer, RecognizerEngine))
        with self.assertRaises(NotImplementedError):
            RecognizerEngine().feed(b"", False)


if __name__ == "__main__":
    unittest.main()
