"""The speaker report keeps matches, failures and insufficient audio distinct."""

from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from lab import speaker_audition as audition
from mind import sherpa, speaker_id


class SpeakerAuditionTests(unittest.TestCase):
    def test_counts_false_accepts_and_extraction_failures_separately(self):
        enrollment = ["enroll"] * 5
        held_out = [
            {"kind": "owner", "file": "owner-match"},
            {"kind": "owner", "file": "owner-miss"},
            {"kind": "other", "file": "other-match"},
            {"kind": "other", "file": "other-fail"},
            {"kind": "silence", "file": "silence-unsuitable"},
        ]
        extractor = Mock()
        extractor.embedding.side_effect = [
            *[(1.0, 0.0)] * 5,
            (1.0, 0.0), (0.0, 1.0), (1.0, 0.0),
            sherpa.Unavailable("inference error"), speaker_id.InsufficientAudio("not ready"),
        ]
        with (patch.object(audition, "load_spec", return_value=(enrollment, held_out, [0.5])),
              patch.object(audition, "verify_model", return_value="pinned"),
              patch.object(audition, "read_clip", return_value=[]),
              patch.object(sherpa, "version", return_value={}),
              patch.object(speaker_id, "Extractor", return_value=nullcontext(extractor))):
            report = audition.audition(Path("clips.json"), Path("model.onnx"))["conditions"]

        self.assertEqual(report["owner"]["thresholds"][0]["accepted"], 1)
        self.assertEqual(report["owner"]["scored"], 2)
        self.assertEqual(report["other"]["thresholds"][0]["accepted"], 1)
        self.assertEqual(report["other"]["scored"], 1)
        self.assertEqual(report["other"]["failed"], 1)
        self.assertEqual(report["silence"]["insufficient"], 1)
        self.assertEqual(report["silence"]["failed"], 0)
