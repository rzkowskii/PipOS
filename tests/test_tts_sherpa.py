import asyncio
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from mind import sherpa, tts_sherpa
from mind.tts_sherpa import PiperEngine
from tests.test_sherpa import FakeLibrary


def voice_dir(root, dataset="alba"):
    folder = Path(root) / "default"
    (folder / "espeak-ng-data").mkdir(parents=True)
    (folder / "en_GB-alba-medium.onnx").write_bytes(b"onnx")
    (folder / "en_GB-alba-medium.onnx.json").write_text(json.dumps({"dataset": dataset}))
    (folder / "tokens.txt").write_text("_ 0\n")
    return folder


class PiperEngineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.folder = voice_dir(self.root)

    def test_resident_voice_loads_once_on_one_thread_and_scales_length(self):
        fake = FakeLibrary()
        engine = PiperEngine(self.folder, library=fake)
        self.assertTrue(engine.available())
        self.assertEqual(engine.label(), "Alba")

        async def speak():
            first = await engine.synthesize("Hello there, it's me.", self.root / "a.wav", 1.0)
            second = await engine.synthesize("Slower now, please.", self.root / "b.wav", 0.8)
            return first, second, {t.name for t in threading.enumerate() if t.name.startswith("being-piper")}

        first, second, workers = asyncio.run(speak())
        self.assertEqual(fake.calls.count("SherpaOnnxCreateOfflineTts"), 1)
        self.assertEqual(fake.configs["tts"]["threads"], 1)
        self.assertEqual(fake.configs["tts"]["provider"], b"cpu")
        self.assertEqual(fake.configs["tts"]["model"], str(self.folder / "en_GB-alba-medium.onnx").encode())
        # length_scale = 1 / rate, which the VITS speed carries as rate.
        self.assertEqual(fake.configs["speeds"], [1.0, 0.8])
        self.assertEqual(len(workers), 1)
        with wave.open(str(self.root / "b.wav")) as audio:
            self.assertEqual((audio.getsampwidth(), audio.getnchannels()), (2, 1))
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), second, places=3)
        self.assertGreater(second, first * 0.9)
        engine.close()
        self.assertEqual(fake.live, {})

    def test_errors_raise_so_the_voice_can_use_flite(self):
        engine = PiperEngine(self.folder, library=FakeLibrary(error=RuntimeError("boom")))
        with self.assertRaises(RuntimeError):
            asyncio.run(engine.synthesize("Hello.", self.root / "x.wav", 1.0))
        with self.assertRaises(ValueError):
            engine.synthesize_blocking("   ", self.root / "x.wav", 1.0)
        with self.assertRaises(ValueError):
            engine.synthesize_blocking("Hello.", self.root / "x.wav", 0)
        engine.close()

    def test_a_voice_that_will_not_load_is_not_retried_every_clause(self):
        calls = []

        def loader():
            calls.append(1)
            raise sherpa.Unavailable("the voice would not load")

        engine = PiperEngine(self.folder, library=FakeLibrary(), loader=loader)
        for _ in range(3):
            with self.assertRaises(sherpa.Unavailable):
                engine.synthesize_blocking("Hello.", self.root / "x.wav")
        self.assertEqual(len(calls), 1)

    def test_missing_files_and_library_are_listed(self):
        empty = self.root / "empty"
        empty.mkdir()
        engine = PiperEngine(empty, library=self.root / "lib.so")
        missing = engine.missing()
        self.assertEqual(len(missing), 4)
        self.assertFalse(engine.available())
        self.assertEqual(engine.label(), "Alba")
        with self.assertRaises(sherpa.Unavailable):
            engine.synthesize_blocking("Hello.", self.root / "x.wav")

    def test_two_models_in_one_folder_are_ambiguous(self):
        (self.folder / "other.onnx").write_bytes(b"onnx")
        self.assertFalse(PiperEngine(self.folder, library=FakeLibrary()).available())

    def test_odd_card_dataset_falls_back_to_the_default_label(self):
        odd = voice_dir(self.root / "odd", dataset="<script>")
        self.assertEqual(PiperEngine(odd, library=FakeLibrary()).label(), tts_sherpa.DEFAULT_LABEL)

    def test_length_scale(self):
        self.assertEqual(tts_sherpa.length_scale(2), 0.5)
        self.assertAlmostEqual(tts_sherpa.length_scale(0.85), 1 / 0.85)
        self.assertEqual(tts_sherpa.spoken("-  hello \n there "), "hello there")


if __name__ == "__main__":
    unittest.main()
