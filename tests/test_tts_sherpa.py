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
from unittest.mock import patch

from mind import sherpa, tts_sherpa
from mind.tts_sherpa import PiperEngine
from tests.test_phonemize import FakePhonemizer
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
        with patch.dict("os.environ", {}, clear=True), patch("mind.tts_sherpa.available_cpus", return_value=4):
            engine = PiperEngine(self.folder, library=fake, phonemizer_library=FakePhonemizer())
        self.assertTrue(engine.available())
        self.assertEqual(engine.label(), "Alba")

        async def speak():
            first = await engine.synthesize("Hello there, it's me.", self.root / "a.wav", 1.0)
            second = await engine.synthesize("Slower now, please.", self.root / "b.wav", 0.8)
            return first, second, {t.name for t in threading.enumerate() if t.name.startswith("being-piper")}

        first, second, workers = asyncio.run(speak())
        self.assertEqual(fake.calls.count("SherpaOnnxCreateOfflineTts"), 1)
        # Two ONNX Runtime threads by default: the fastest on the laptop's two cores.
        self.assertEqual(fake.configs["tts"]["threads"], 2)
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
        engine = PiperEngine(self.folder, library=FakeLibrary(error=RuntimeError("boom")),
                             phonemizer_library=FakePhonemizer())
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

        engine = PiperEngine(self.folder, library=FakeLibrary(), phonemizer_library=FakePhonemizer(), loader=loader)
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(3):
                with self.assertRaises(sherpa.Unavailable):
                    engine.synthesize_blocking("Hello.", self.root / "x.wav")
        self.assertEqual(len(calls), 1)

    def test_a_clause_with_no_audio_silences_the_voice_for_the_session(self):
        fake = FakeLibrary(error=RuntimeError("no espeak-ng"))
        engine = PiperEngine(self.folder, library=fake, phonemizer_library=FakePhonemizer())
        engine.sticky = True    # the voice organ's engine (voice_engines._watch)
        told = []
        engine.on_silent = told.append
        self.assertEqual(engine.name, "piper")
        self.assertTrue(callable(engine.synthesize))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for _ in range(3):
                with self.assertRaises(RuntimeError) as caught:
                    engine.synthesize_blocking("Hello there.", self.root / "x.wav")
                self.assertEqual(str(caught.exception), "the voice produced no audio")
        # One log line, the clause and one short word tried once, and the reason kept once.
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("The natural voice made no sound for a clause (the voice produced no audio). "
                      "Using the original voice for the rest of this session.", err.getvalue())
        self.assertEqual(len(fake.configs["speeds"]), 2)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if "reprobe" in p.name), [])
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

    def test_an_engine_outside_the_voice_organ_keeps_trying_and_prints_nothing(self):
        # Setup's preview, pip-diag and the bench use a plain engine: each failure
        # raises, nothing is printed over their screens and nothing is silenced.
        fake = FakeLibrary(error=RuntimeError("no espeak-ng"))
        engine = PiperEngine(self.folder, library=fake, phonemizer_library=FakePhonemizer())
        self.assertFalse(engine.sticky)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    engine.synthesize_blocking("Hello there.", self.root / "x.wav")
            with self.assertRaises(RuntimeError):
                asyncio.run(engine.synthesize("Hello there.", self.root / "x.wav", 1.0))
        self.assertEqual(len(fake.configs["speeds"]), 3)
        self.assertEqual((engine.name, engine.silenced, err.getvalue()), ("piper", None, ""))
        engine.close()

    def test_one_clause_without_audio_does_not_silence_a_voice_that_still_speaks(self):
        class Picky:
            """Says words, but gives no audio for one clause."""
            sample_rate = 22050

            def __init__(self):
                self.said = []

            def write(self, text, path, *, length_scale):
                self.said.append(text)
                if text in ("Grüße an Bob.", "..."):
                    raise RuntimeError("the voice produced no audio")
                with wave.open(str(path), "wb") as out:
                    out.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                    out.writeframes(b"\x00\x10" * 2205)
                return 0.1

            def close(self):
                pass

        voice = Picky()
        engine = PiperEngine(self.folder, library=FakeLibrary(), loader=lambda: voice, phonemizer_library=FakePhonemizer())
        engine.sticky = True
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(RuntimeError):
                engine.synthesize_blocking("Grüße an Bob.", self.root / "a.wav")
            # Only punctuation: nothing to say, so no second try either.
            with self.assertRaises(RuntimeError):
                engine.synthesize_blocking("...", self.root / "b.wav")
            self.assertEqual(engine.synthesize_blocking("Here is your timer.", self.root / "c.wav"), 0.1)
        self.assertEqual(voice.said, ["Grüße an Bob.", "Hello.", "...", "Here is your timer."])
        self.assertEqual((engine.name, engine.silenced, err.getvalue()), ("piper", None, ""))
        self.assertTrue(callable(engine.synthesize))
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if "reprobe" in p.name), [])
        self.assertFalse(tts_sherpa.sayable("?!"))
        self.assertTrue(tts_sherpa.sayable("5"))

    def test_one_slow_clause_is_not_a_broken_voice(self):
        class Slow:
            sample_rate = 22050

            def __init__(self):
                self.delays = [0.3, 0.0, 0.0]

            def write(self, text, path, *, length_scale):
                time.sleep(self.delays.pop(0) if self.delays else 0.0)
                return 1.0

            def close(self):
                pass

        engine = PiperEngine(self.folder, library=FakeLibrary(), loader=Slow, phonemizer_library=FakePhonemizer())
        engine.sticky = True

        async def speak():
            with self.assertRaises(TimeoutError):
                await engine.synthesize("Hello.", self.root / "x.wav", 1.0)
            await asyncio.sleep(0.4)        # the worker finishes the slow clause
            return await engine.synthesize("Hello again.", self.root / "y.wav", 1.0)

        with unittest.mock.patch.object(tts_sherpa, "CLAUSE_BASE_S", 0.1), \
                unittest.mock.patch.object(tts_sherpa, "CLAUSE_CHARS_PER_S", 10 ** 6), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(asyncio.run(speak()), 1.0)
        self.assertEqual((engine.name, engine.silenced, err.getvalue()), ("piper", None, ""))
        engine.close()

    def test_a_late_clause_whose_folder_is_gone_is_not_a_broken_voice(self):
        # Seen in QEMU on the v6 cable stick: a clause ran out of time, Flite
        # said it, speak_clauses removed its folder, and the worker's late write
        # into that folder (and the re-probe beside it) failed, which silenced
        # Alba for the rest of the session.
        class SlowWriter:
            sample_rate = 22050

            def __init__(self):
                self.delays = [0.3]

            def write(self, text, path, *, length_scale):
                time.sleep(self.delays.pop(0) if self.delays else 0.0)
                if not Path(path).parent.is_dir():
                    return 0.0          # sherpa-onnx: "Failed to create ..." and no audio
                Path(path).write_bytes(b"RIFF")
                return 1.0

            def close(self):
                pass

        engine = PiperEngine(self.folder, library=FakeLibrary(), loader=SlowWriter,
                             phonemizer_library=FakePhonemizer())
        engine.sticky = True
        silenced = []
        engine.on_silent = silenced.append
        work = self.root / "pip-clause"
        work.mkdir()

        async def speak():
            with self.assertRaises(TimeoutError):
                await engine.synthesize("Hello, it is twenty past one.", work / "clause.wav", 1.0)
            work.rmdir()                    # Flite said it; speak_clauses tidies its folder
            await asyncio.sleep(0.5)        # the worker finishes the late clause, into nothing
            again = self.root / "pip-next"
            again.mkdir()
            return await engine.synthesize("Hello again.", again / "clause.wav", 1.0)

        with unittest.mock.patch.object(tts_sherpa, "CLAUSE_BASE_S", 0.1), \
                unittest.mock.patch.object(tts_sherpa, "CLAUSE_CHARS_PER_S", 10 ** 6), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(asyncio.run(speak()), 1.0)
        self.assertEqual((engine.name, engine.silenced, silenced, err.getvalue()), ("piper", None, [], ""))
        self.assertEqual(engine._abandoned, set())
        engine.close()

    def test_a_worker_stuck_on_a_clause_silences_the_voice_at_the_next_clause(self):
        release = threading.Event()

        class Stuck:
            sample_rate = 22050

            def write(self, text, path, *, length_scale):
                release.wait(5)
                return 1.0

            def close(self):
                pass

        engine = PiperEngine(self.folder, library=FakeLibrary(), loader=Stuck, phonemizer_library=FakePhonemizer())
        engine.sticky = True

        async def speak():
            with self.assertRaises(TimeoutError):
                await engine.synthesize("Hello.", self.root / "x.wav", 1.0)
            self.assertEqual(engine.name, "piper", "one late clause is said by Flite and no more")
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await engine.synthesize("Hello again.", self.root / "y.wav", 1.0)
            return time.monotonic() - started

        with unittest.mock.patch.object(tts_sherpa, "CLAUSE_BASE_S", 0.1), \
                unittest.mock.patch.object(tts_sherpa, "CLAUSE_CHARS_PER_S", 10 ** 6), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            waited = asyncio.run(speak())
        release.set()
        self.assertLess(waited, 0.1, "the second clause does not wait behind the stuck one")
        self.assertEqual(engine.name, "flite")
        self.assertIn("still on a clause that ran out of time", engine.silenced)
        self.assertIn("for the rest of this session", err.getvalue())
        self.assertIsNone(engine.synthesize)
        engine.close()

    def test_clauses_that_keep_running_out_of_time_silence_the_voice(self):
        class Slow:
            sample_rate = 22050

            def write(self, text, path, *, length_scale):
                time.sleep(0.15)
                return 1.0

            def close(self):
                pass

        engine = PiperEngine(self.folder, library=FakeLibrary(), loader=Slow, phonemizer_library=FakePhonemizer())
        engine.sticky = True

        async def speak():
            for n in range(tts_sherpa.STUCK_LIMIT):
                with self.assertRaises(TimeoutError):
                    await engine.synthesize(f"Clause {n}.", self.root / f"{n}.wav", 1.0)
                await asyncio.sleep(0.2)

        with unittest.mock.patch.object(tts_sherpa, "CLAUSE_BASE_S", 0.05), \
                unittest.mock.patch.object(tts_sherpa, "CLAUSE_CHARS_PER_S", 10 ** 6), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            asyncio.run(speak())
        self.assertEqual(engine.name, "flite")
        self.assertIn(f"{tts_sherpa.STUCK_LIMIT} clauses in a row took too long", engine.silenced)
        self.assertEqual(err.getvalue().count("\n"), 1)
        engine.close()

    def test_the_probe_of_a_real_engine_that_gives_no_audio_picks_flite(self):
        from mind import voice_engines as ve
        from mind.tts import FliteEngine

        engine = PiperEngine(self.folder, library=FakeLibrary(error=RuntimeError("no espeak-ng")),
                             phonemizer_library=FakePhonemizer())
        with unittest.mock.patch.dict(ve._silent, clear=True), \
                unittest.mock.patch.object(ve, "RUN_STATE", self.root / "run" / "tts.json"), \
                unittest.mock.patch.object(engine, "verified", return_value=(True, None)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            chosen, note = ve.choose_tts("auto", state_dir=self.root, build="b1", engine=engine)
            self.assertEqual(ve.silent_reason("b1", engine), "the voice produced no audio")
        self.assertIsInstance(chosen, FliteEngine)
        self.assertEqual(note, "The natural voice made no sound in a test (the voice produced no audio). "
                               "Using the original voice.")
        self.assertEqual(err.getvalue(), "", "the probe's note is the only line")
        engine.close()

    def test_a_voice_nobody_closed_gives_its_memory_back_when_dropped(self):
        import gc

        fake = FakeLibrary()
        engine = PiperEngine(self.folder, library=fake, phonemizer_library=FakePhonemizer())
        engine.resident()
        self.assertEqual([kind for kind, _ in fake.live.values()], ["tts"])
        del engine
        gc.collect()
        self.assertEqual(fake.live, {})
        # A closed voice is destroyed once, not again when it is dropped.
        engine = PiperEngine(self.folder, library=fake, phonemizer_library=FakePhonemizer())
        engine.resident()
        engine.close()
        del engine
        gc.collect()
        self.assertEqual(fake.calls.count("SherpaOnnxDestroyOfflineTts"), 2)

    def test_missing_files_and_library_are_listed(self):
        empty = self.root / "empty"
        empty.mkdir()
        engine = PiperEngine(empty, library=self.root / "lib.so", phonemizer_library=self.root / "pp.so")
        missing = engine.missing()
        self.assertEqual(len(missing), 5)
        self.assertIn(str(self.root / "pp.so"), missing)
        self.assertFalse(engine.available())
        self.assertEqual(engine.label(), "Alba")
        with self.assertRaises(sherpa.Unavailable), contextlib.redirect_stderr(io.StringIO()):
            engine.synthesize_blocking("Hello.", self.root / "x.wav")

    def test_the_voice_speaks_piper_phonemes_in_its_cards_espeak_voice(self):
        (self.folder / "en_GB-alba-medium.onnx.json").write_text(
            json.dumps({"dataset": "alba", "espeak": {"voice": "en-gb-x-rp"}}))
        fake, phonemes = FakeLibrary(), FakePhonemizer()
        engine = PiperEngine(self.folder, library=fake, phonemizer_library=phonemes)
        seconds = engine.synthesize_blocking("Hello. I'm Pip.", self.root / "p.wav")
        self.assertGreater(seconds, 0)
        self.assertEqual(phonemes.texts[-1], (b"Hello. I'm Pip.", b"en-gb-x-rp"))
        self.assertEqual(fake.configs["phonemes"], [(b"", [6, 9], "hello. i'm pip.")])
        engine.close()
        self.assertEqual((fake.live, phonemes.live), ({}, {}))

    def test_a_clause_with_no_phonemes_is_skipped_without_silencing_the_voice(self):
        fake, phonemes = FakeLibrary(), FakePhonemizer()
        engine = PiperEngine(self.folder, library=fake, phonemizer_library=phonemes)
        engine.sticky = True    # the voice organ's engine
        told = []
        engine.on_silent = told.append
        engine.resident()       # loaded and probed while espeak-ng still speaks
        phonemes.silent = True  # espeak-ng gives no phonemes, as for "..." or "?!"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for clause in ("...", ".", "?!", "\u2026", "Hmm."):
                with self.assertRaisesRegex(ValueError, "nothing to say"):
                    engine.synthesize_blocking(clause, self.root / "x.wav")
        self.assertEqual((err.getvalue(), told, engine.name), ("", [], "piper"))
        self.assertNotIn("speeds", fake.configs)     # sherpa-onnx was never asked
        phonemes.silent = False
        self.assertGreater(engine.synthesize_blocking("Done!", self.root / "x.wav"), 0)
        engine.close()
        self.assertEqual((fake.live, phonemes.live), ({}, {}))

    def test_a_voice_without_its_card_is_not_available(self):
        (self.folder / "en_GB-alba-medium.onnx.json").unlink()
        engine = PiperEngine(self.folder, library=FakeLibrary(), phonemizer_library=FakePhonemizer())
        self.assertIn(str(self.folder / "en_GB-alba-medium.onnx.json"), engine.missing())

    def test_two_models_in_one_folder_are_ambiguous(self):
        (self.folder / "other.onnx").write_bytes(b"onnx")
        self.assertFalse(PiperEngine(self.folder, library=FakeLibrary(), phonemizer_library=FakePhonemizer()).available())

    def test_odd_card_dataset_falls_back_to_the_default_label(self):
        odd = voice_dir(self.root / "odd", dataset="<script>")
        engine = PiperEngine(odd, library=FakeLibrary(), phonemizer_library=FakePhonemizer())
        self.assertEqual(engine.label(), tts_sherpa.DEFAULT_LABEL)

    def test_length_scale(self):
        self.assertEqual(tts_sherpa.length_scale(2), 0.5)
        self.assertAlmostEqual(tts_sherpa.length_scale(0.85), 1 / 0.85)
        self.assertEqual(tts_sherpa.spoken("-  hello \n there "), "hello there")


class ThreadSetting(unittest.TestCase):
    def test_two_threads_by_default_never_more_than_the_cpus(self):
        self.assertEqual(tts_sherpa.threads_setting({}, cpus=4), 2)
        self.assertEqual(tts_sherpa.threads_setting({}, cpus=1), 1)

    def test_the_environment_chooses_within_range(self):
        self.assertEqual(tts_sherpa.threads_setting({"BEING_PIPER_THREADS": "1"}, cpus=4), 1)
        self.assertEqual(tts_sherpa.threads_setting({"BEING_PIPER_THREADS": " 4 "}, cpus=4), 4)
        with patch("builtins.print") as printed:
            for bad in ("0", "9", "two", "-1", "1.5"):
                self.assertEqual(tts_sherpa.threads_setting({"BEING_PIPER_THREADS": bad}, cpus=4), 2, bad)
        self.assertIn("BEING_PIPER_THREADS", printed.call_args.args[0])

    def test_an_explicit_count_wins_and_the_voice_organs_engine_uses_the_setting(self):
        self.assertEqual(PiperEngine("/nonexistent", threads=1).threads, 1)
        with patch.dict("os.environ", {"BEING_PIPER_THREADS": "3"}), \
                patch("mind.tts_sherpa.available_cpus", return_value=4):
            self.assertEqual(PiperEngine("/nonexistent").threads, 3)
            from mind import voice_engines

            self.assertEqual(voice_engines._piper("/nonexistent").threads, 3)


if __name__ == "__main__":
    unittest.main()
