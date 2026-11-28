"""The keyword spotter's score and speech flag, for the voice's diagnostics."""

import unittest
from collections import deque
from unittest.mock import Mock

from mind.recognition import FRAME_SECONDS, ONESHOT_SKIP_FRAMES, Recognizer


class WakeScore(unittest.TestCase):
    def test_a_one_breath_wake_keeps_the_score_of_its_keyword(self):
        recognizer = Recognizer.__new__(Recognizer)
        recognizer.lib = Mock()
        recognizer.lib.ps_activate_search.return_value = 0
        recognizer.lib.ps_start_utt.return_value = 0
        recognizer.decoder = Mock()
        recognizer.dictation = b"_default"
        recognizer.mode = b"wake"
        recognizer.capture_frames = 40
        recognizer.frames = 30
        recognizer.search_start = 0.0
        recognizer.wake_end = 0.6
        recognizer.wake_history = deque(range(30))
        recognizer.wake_preroll = deque()
        recognizer.wake_candidate_score = -1234
        events = recognizer.begin_oneshot()
        self.assertEqual(events[0], ("hear.wake", {"oneshot": True}))
        # begin() for the dictation clears the candidate; the reported score survives it.
        self.assertEqual(recognizer.wake_score, -1234)
        self.assertIsNone(recognizer.wake_candidate_score)
        # The replay starts ONESHOT_SKIP_FRAMES after the name's aligned end.
        self.assertEqual(list(recognizer.oneshot_queue)[0], int(0.6 / FRAME_SECONDS) + ONESHOT_SKIP_FRAMES)

    def test_instances_built_without_init_still_answer(self):
        recognizer = Recognizer.__new__(Recognizer)
        self.assertFalse(recognizer.in_speech)
        self.assertIsNone(recognizer.wake_score)


if __name__ == "__main__":
    unittest.main()
