import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mind.tts import MAX_SEGMENT_METADATA_BYTES, shorten_leading_pause, synthesis_command


RATE = 16000
METADATA = b"pau:0.170 pau:0.200 s:0.266 t:0.320 pau:0.400 \n"


class LeadingPause(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.audio = Path(work.name) / "clause.wav"

    def write_wav(self, *, rate=RATE, channels=1, width=2):
        frames = 400 * rate // 1000
        pcm = struct.pack(f"<{frames}h", *(1000 if i % 2 == 0 else -1300 for i in range(frames)))
        with wave.open(str(self.audio), "wb") as wav:
            wav.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        return pcm

    def test_keeps_forty_ms_of_pause_with_fade_and_preserves_pcm_after_fade(self):
        original = self.write_wav()
        self.audio.chmod(0o640)
        self.assertTrue(shorten_leading_pause(self.audio, METADATA))
        with wave.open(str(self.audio), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, RATE))
            self.assertEqual(wav.getnframes(), 240 * RATE // 1000)
            pcm = wav.readframes(wav.getnframes())
        trim = 160 * RATE // 1000
        fade = 5 * RATE // 1000
        expected = bytearray(original[2 * trim:])
        for frame in range(fade):
            sample, = struct.unpack_from("<h", expected, frame * 2)
            struct.pack_into("<h", expected, frame * 2, round(sample * frame / (fade - 1)))
        self.assertEqual(pcm, expected)
        self.assertEqual(self.audio.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.audio.parent.iterdir()), [self.audio])

    def test_accepts_a_six_ms_clock_difference(self):
        for end in (b"0.394", b"0.406"):
            with self.subTest(end=end):
                self.write_wav(rate=8000)
                self.assertTrue(shorten_leading_pause(self.audio, METADATA.replace(b"0.400", end)))

    def test_declines_invalid_timing_without_touching_the_wav(self):
        self.write_wav()
        original = self.audio.read_bytes()
        for metadata in (
            b"", b"pau:0.400", b"pau:0.200 pau:0.400", b"s:0.200 pau:0.400",
            b"pau:0.200 s:0.300 t:0.400", b"pau:0.200 s:0.200 pau:0.400",
            b"pau:0.200 s:0.199 pau:0.400", b"pau:0.200 s:0.300 pau:0.407",
            b"pau:0.200 s:0.300 pau:0.393", b"pau:0.040 s:0.300 pau:0.400",
            b"pau:0.401 s:0.402 pau:0.403", b"pau:0.200 s:3e-1 pau:0.400",
            b"pau:0.200 s:0.300\npau:0.400", b"pau:0.20 s:0.300 pau:0.400",
            b"pau:0.200 s:0.300 pau:0.400 extra", b"x" * (MAX_SEGMENT_METADATA_BYTES + 1),
        ):
            with self.subTest(metadata=metadata[:72]):
                self.assertFalse(shorten_leading_pause(self.audio, metadata))
                self.assertEqual(self.audio.read_bytes(), original)

    def test_declines_noncanonical_or_truncated_wavs_without_touching_them(self):
        self.write_wav()
        canonical = self.audio.read_bytes()
        self.write_wav(channels=2)
        stereo = self.audio.read_bytes()
        self.write_wav(width=1)
        unsigned = self.audio.read_bytes()
        malformed = [
            b"", b"not a wave file", stereo, unsigned, canonical[:-2], canonical + b"xx",
            canonical[:28] + bytes(4) + canonical[32:], b"x" * (2 * 1024 * 1024 + 1),
        ]
        for data in malformed:
            with self.subTest(start=data[:44], length=len(data)):
                self.audio.write_bytes(data)
                self.assertFalse(shorten_leading_pause(self.audio, METADATA))
                self.assertEqual(self.audio.read_bytes(), data)

    def test_failed_atomic_replacement_leaves_only_the_original(self):
        self.write_wav()
        original = self.audio.read_bytes()
        with patch("mind.tts.os.replace", side_effect=PermissionError):
            self.assertFalse(shorten_leading_pause(self.audio, METADATA))
        self.assertEqual(self.audio.read_bytes(), original)
        self.assertEqual(list(self.audio.parent.iterdir()), [self.audio])

    def test_missing_output_is_not_a_successful_trim(self):
        self.assertFalse(shorten_leading_pause(self.audio, METADATA))
        self.assertFalse(self.audio.exists())

    def test_metadata_flag_is_opt_in(self):
        command = synthesis_command("slt", 1.15, "What?", self.audio)
        with_times = synthesis_command("slt", 1.15, "What?", self.audio, segment_times=True)
        self.assertNotIn("-psdur", command)
        with_times.remove("-psdur")
        self.assertEqual(with_times, command)
