"""Procedural earcons, software gain and the speech engine choice."""

import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

from mind.tts import (
    CHIRP_PEAK, CHIRP_RATE, CHIRPS, ChirpBank, FliteEngine, PiperEngine, chirp_samples, scale_wav,
    select_engine, volume_gain, write_wav,
)


class Earcons(unittest.TestCase):
    def test_every_chirp_is_short_soft_and_click_free(self):
        self.assertTrue({"heard", "purr", "yes", "no", "sigh", "thanks", "bell", "ears", "trouble"} <= CHIRPS)
        for name in CHIRPS:
            with self.subTest(name=name):
                samples = chirp_samples(name)
                seconds = len(samples) / CHIRP_RATE
                self.assertGreater(seconds, 0.05)
                self.assertLess(seconds, 1.2)
                peak = max(abs(value) for value in samples)
                self.assertGreater(peak, 500)
                self.assertLessEqual(peak, CHIRP_PEAK * 1.2)
                # Gentle onset and a fully faded end.
                self.assertLess(abs(samples[0]), 200)
                self.assertLess(max(abs(value) for value in samples[-20:]), 400)

    def test_variants_are_jittered_but_reproducible(self):
        self.assertEqual(chirp_samples("heard", 1), chirp_samples("heard", 1))
        self.assertNotEqual(chirp_samples("heard", 0), chirp_samples("heard", 1))

    def test_gain_scales_the_chirp(self):
        loud = max(abs(value) for value in chirp_samples("yes", 0))
        soft = max(abs(value) for value in chirp_samples("yes", 0, gain=0.25))
        self.assertAlmostEqual(soft / loud, 0.25, delta=0.02)
        self.assertEqual(max(abs(value) for value in chirp_samples("yes", 0, gain=0)), 0)

    def test_unknown_chirp_is_refused(self):
        with self.assertRaises(ValueError):
            chirp_samples("siren")

    def test_bank_renders_once_into_a_private_directory(self):
        with tempfile.TemporaryDirectory() as work:
            bank = ChirpBank(Path(work) / "chirps")
            first = bank.path("bell", 1.0, variant=2)
            self.assertEqual(first.name, "bell-2-g100.wav")
            self.assertEqual((first.parent.stat().st_mode & 0o777), 0o700)
            stamp = first.stat().st_mtime_ns
            self.assertEqual(bank.path("bell", 1.0, variant=2), first)
            self.assertEqual(first.stat().st_mtime_ns, stamp)
            with wave.open(str(first)) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, CHIRP_RATE))
            self.assertEqual(bank.path("bell", 0.5, variant=2).name, "bell-2-g50.wav")
            # A volume change replaces the cached sound rather than piling up copies in RAM.
            self.assertFalse(first.exists())
            self.assertEqual(sorted(item.name for item in first.parent.glob("bell-2-*")), ["bell-2-g50.wav"])
            self.assertIn(bank.path("ears").name.split("-")[1], {"0", "1", "2", "3"})


class SoftwareGain(unittest.TestCase):
    def test_curve(self):
        self.assertEqual(volume_gain(100), 1.0)
        self.assertEqual(volume_gain(0), 0.0)
        self.assertAlmostEqual(volume_gain(50), 0.25)
        self.assertEqual(volume_gain(250), 1.0)

    def test_scale_wav_in_place(self):
        with tempfile.TemporaryDirectory() as work:
            audio = Path(work) / "clause.wav"
            write_wav(audio, array("h", [1000, -2000, 32767]), rate=16000)
            self.assertTrue(scale_wav(audio, 0.5))
            with wave.open(str(audio)) as wav:
                self.assertEqual(wav.getframerate(), 16000)
                self.assertEqual(list(array("h", wav.readframes(3))), [500, -1000, 16383])
            self.assertFalse(scale_wav(audio, 1.0))
            self.assertFalse(scale_wav(Path(work) / "missing.wav", 0.5))
            self.assertEqual(sorted(path.name for path in Path(work).iterdir()), ["clause.wav"])


class Engines(unittest.TestCase):
    def test_flite_is_the_default_and_the_fallback(self):
        engine, note = select_engine(None)
        self.assertIsInstance(engine, FliteEngine)
        self.assertIsNone(note)
        engine, note = select_engine("espeak")
        self.assertIsInstance(engine, FliteEngine)
        self.assertIn("Unknown", note)
        with tempfile.TemporaryDirectory() as root:
            engine, note = select_engine("piper", voice_name="amy", piper_root=root)
        self.assertIsInstance(engine, FliteEngine)
        self.assertIn("not installed", note)

    def test_flite_engine_builds_the_stock_command(self):
        self.assertEqual(FliteEngine().command("slt", 1.0, "Hi.", "out.wav"),
                         ["flite", "-voice", "slt", "-t", "Hi.", "-o", "out.wav"])

    def test_piper_is_chosen_only_with_every_file(self):
        with tempfile.TemporaryDirectory() as root:
            voice = Path(root) / "amy"
            (voice / "espeak-ng-data").mkdir(parents=True)
            (voice / "en_US-amy-low.onnx").write_bytes(b"model")
            with patch("mind.tts.shutil.which", return_value="/usr/bin/sherpa-onnx-offline-tts"):
                self.assertFalse(PiperEngine(voice).available())
                (voice / "tokens.txt").write_text("a 1\n")
                engine, note = select_engine("piper", voice_name="amy", piper_root=root)
            self.assertIsInstance(engine, PiperEngine)
            self.assertIsNone(note)
            command = engine.command("slt", 1.25, "--Hello.", "/run/out.wav")
            self.assertEqual(command, [
                "sherpa-onnx-offline-tts", f"--vits-model={voice / 'en_US-amy-low.onnx'}",
                f"--vits-tokens={voice / 'tokens.txt'}", f"--vits-data-dir={voice / 'espeak-ng-data'}",
                "--num-threads=1", "--vits-length-scale=0.8", "--output-filename=/run/out.wav", "Hello.",
            ])
            self.assertFalse(engine.supports_segment_times)
            with patch("mind.tts.shutil.which", return_value=None):
                self.assertFalse(engine.available())


if __name__ == "__main__":
    unittest.main()
