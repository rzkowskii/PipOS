import asyncio
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
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
        with self.assertRaises(RuntimeError), contextlib.redirect_stderr(io.StringIO()):
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
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(3):
                with self.assertRaises(sherpa.Unavailable):
                    engine.synthesize_blocking("Hello.", self.root / "x.wav")
        self.assertEqual(len(calls), 1)

    def test_a_clause_with_no_audio_silences_the_voice_for_the_session(self):
        fake = FakeLibrary(error=RuntimeError("no espeak-ng"))
        engine = PiperEngine(self.folder, library=fake)
        told = []
        engine.on_silent = told.append
        self.assertEqual(engine.name, "piper")
        self.assertTrue(callable(engine.synthesize))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for _ in range(3):
                with self.assertRaises(RuntimeError) as caught:
                    engine.synthesize_blocking("Hello there.", self.root / "x.wav")
                self.assertEqual(str(caught.exception), "the voice produced no audio")
        # One log line, one generation attempt, and the reason kept once.
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("The natural voice made no sound for a clause (the voice produced no audio). "
                      "Using the original voice for the rest of this session.", err.getvalue())
        self.assertEqual(len(fake.configs["speeds"]), 1)
        self.assertEqual(told, ["the voice produced no audio"])
        # It now says it speaks with Flite, and hands the voice organ Flite's command.
        self.assertEqual(engine.name, "flite")
        self.assertIsNone(engine.synthesize)
        self.assertEqual(engine.command("slt", 1, "Hi.", "/tmp/x.wav"),
                         ["flite", "-voice", "slt", "-t", "Hi.", "-o", "/tmp/x.wav"])
        # Bad input is still the caller's mistake.
        with self.assertRaises(ValueError):
            engine.synthesize_blocking("  ", self.root / "x.wav")
        engine.close()

    def test_a_measuring_tool_keeps_trying_every_sentence(self):
        fake = FakeLibrary(error=RuntimeError("no espeak-ng"))
        engine = PiperEngine(self.folder, library=fake)
        engine.sticky = False
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    engine.synthesize_blocking("Hello there.", self.root / "x.wav")
        self.assertEqual(len(fake.configs["speeds"]), 2)
        self.assertEqual((engine.name, engine.silenced, err.getvalue()), ("piper", None, ""))
        engine.close()

    def test_a_clause_that_takes_too_long_silences_the_voice(self):
        class Slow:
            sample_rate = 22050

            def write(self, text, path, *, length_scale):
                time.sleep(0.3)
                return 1.0

            def close(self):
                pass

        engine = PiperEngine(self.folder, library=FakeLibrary(), loader=Slow)
        with unittest.mock.patch.object(tts_sherpa, "CLAUSE_BASE_S", 0.05), \
                unittest.mock.patch.object(tts_sherpa, "CLAUSE_CHARS_PER_S", 10 ** 6), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(TimeoutError):
                asyncio.run(engine.synthesize("Hello.", self.root / "x.wav", 1.0))
        self.assertEqual(engine.name, "flite")
        self.assertIn("a clause took more than", engine.silenced)
        self.assertIn("for the rest of this session", err.getvalue())
        engine.close()

    def test_the_probe_of_a_real_engine_that_gives_no_audio_picks_flite(self):
        from mind import voice_engines as ve
        from mind.tts import FliteEngine

        engine = PiperEngine(self.folder, library=FakeLibrary(error=RuntimeError("no espeak-ng")))
        with unittest.mock.patch.dict(ve._silent, clear=True), \
                unittest.mock.patch.object(engine, "verified", return_value=(True, None)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            chosen, note = ve.choose_tts("auto", state_dir=self.root, build="b1", engine=engine)
        self.assertIsInstance(chosen, FliteEngine)
        self.assertEqual(note, "The natural voice made no sound in a test (the voice produced no audio). "
                               "Using the original voice.")
        self.assertEqual(err.getvalue(), "", "the probe's note is the only line")
        self.assertEqual(engine.name, "flite")
        engine.close()

    def test_missing_files_and_library_are_listed(self):
        empty = self.root / "empty"
        empty.mkdir()
        engine = PiperEngine(empty, library=self.root / "lib.so")
        missing = engine.missing()
        self.assertEqual(len(missing), 4)
        self.assertFalse(engine.available())
        self.assertEqual(engine.label(), "Alba")
        with self.assertRaises(sherpa.Unavailable), contextlib.redirect_stderr(io.StringIO()):
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
