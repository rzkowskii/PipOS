import asyncio
import json
import os
import tempfile
import threading
import unittest
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mind import voice_engines as ve
from mind.reply import Context
from mind.tts import FliteEngine
from tests.test_asr_sherpa import FakeMoonshine, FakeWake


def write_tone(path, level=8000, frames=1600, rate=16000):
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(array("h", [level, -level] * (frames // 2)).tobytes())


class FakePiper:
    """A natural voice. `sound`: "tone" (speaks), "raise" (fails like sherpa-onnx
    2.0.3 without espeak-ng), "silence" (all-zero samples), "empty" (no samples),
    or "hang" (waits for `release`)."""

    name = "piper"

    def __init__(self, installed=True, seconds=2.0, elapsed=None, sound="tone"):
        self.voice_dir = Path("/models/voice/piper/default")
        self.installed = installed
        self.seconds = seconds
        self.elapsed = elapsed
        self.loads = 0
        self._executor = None
        self.sound = sound
        self.texts = []
        self.release = threading.Event()

    def available(self):
        return self.installed

    def label(self):
        return "Alba"

    def resident(self):
        self.loads += 1

    @property
    def loaded(self):
        return self.loads > 0 and not getattr(self, "closed", False)

    def close(self):
        self.closed = True

    def synthesize_blocking(self, text, wav, rate=1.0):
        assert text in (ve.SENTENCE, ve.PROBE_TEXT) and rate == 1.0
        self.texts.append(text)
        if self.sound == "raise":
            raise RuntimeError("the voice produced no audio")
        if self.sound == "hang":
            self.release.wait(10)
            raise RuntimeError("released")
        if self.sound == "empty":
            write_tone(wav, frames=0)
            return 0.0
        write_tone(wav, level=0 if self.sound == "silence" else 8000)
        return self.seconds

    def executor(self):
        self._executor = self._executor or ThreadPoolExecutor(max_workers=1)
        return self._executor


class Clock:
    def __init__(self, steps):
        self.steps = list(steps)
        self.now = 0.0

    def __call__(self):
        value = self.now
        if self.steps:
            self.now += self.steps.pop(0)
        return value


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_preference_lives_in_the_voice_settings_and_keeps_volume(self):
        from mind.voice import load_engine_choice, load_voice_settings, save_voice_settings

        self.assertEqual(ve.load_preference(self.state), "auto")
        save_voice_settings(self.state, volume=40, chirps=False, tts="flite")
        self.assertEqual(ve.load_preference(self.state), "flite")
        ve.save_preference(self.state, "piper")
        self.assertEqual((ve.load_preference(self.state), load_engine_choice(self.state)), ("piper", "piper"))
        self.assertEqual(load_voice_settings(self.state), {"volume": 40, "chirps": False})
        self.assertEqual((self.state / ve.VOICE_SETTINGS).stat().st_mode & 0o777, 0o600)
        (self.state / ve.VOICE_SETTINGS).write_text("{not json")
        self.assertEqual(ve.load_preference(self.state), "auto")
        with self.assertRaises(ValueError):
            ve.save_preference(self.state, "loud")

    def test_cache_is_keyed_by_build_and_expires_after_a_week(self):
        now = 1_800_000_000.0
        ve.record(self.state, "tts", "b1|piper", {"rtf": 0.3, "runs": [0.3, 0.31, 0.29]}, now)
        self.assertEqual(ve.cached(self.state, "tts", "b1|piper", now)["rtf"], 0.3)
        self.assertIsNone(ve.cached(self.state, "tts", "b2|piper", now))
        self.assertIsNotNone(ve.cached(self.state, "tts", "b1|piper", now + 6 * 86400))
        self.assertIsNone(ve.cached(self.state, "tts", "b1|piper", now + 8 * 86400))
        self.assertIsNone(ve.cached(self.state, "tts", "b1|piper", now - 60))  # a clock that went back
        ve.record(self.state, "tts", "b2|piper", {"rtf": 0.2, "runs": [0.2]}, now)
        data = json.loads((self.state / ve.ENGINES_FILE).read_text())
        self.assertEqual(set(data["tts"]), {"b2|piper"})

    def test_build_id(self):
        path = self.state / "build-id"
        self.assertEqual(ve.build_id(path), "unknown")
        path.write_text("pipos-2026.11.24-abc\n")
        self.assertEqual(ve.build_id(path), "pipos-2026.11.24-abc")
        path.write_text("../../etc")
        self.assertEqual(ve.build_id(path), "unknown")


class MeasureTests(unittest.TestCase):
    def test_tts_rtf_is_the_median_of_three_runs_after_loading(self):
        piper = FakePiper(seconds=2.0)
        result = ve.measure_tts(piper, clock=Clock([0.6, 0, 0.8, 0, 0.7, 0]))
        self.assertEqual(piper.loads, 1)
        self.assertEqual(len(result["runs"]), 3)
        self.assertAlmostEqual(result["rtf"], 0.35)

    def test_asr_rtf_on_a_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / "0.wav"
            with wave.open(str(wav), "wb") as out:
                out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                out.writeframes(array("h", [0] * 32000).tobytes())
            moonshine = FakeMoonshine(["warm-up", "hello"])
            result = ve.measure_asr(moonshine, wav, clock=Clock([0.5]))
        self.assertAlmostEqual(result["rtf"], 0.25)
        self.assertEqual(result["text"], "hello")
        # A 1 s warm-up decode first, not counted; then the whole 2 s recording.
        self.assertEqual(moonshine.seen, [16000, 32000])

    def test_asr_rtf_on_the_packs_24_khz_recording(self):
        """The Moonshine packs' test_wavs/0.wav is 24 kHz; it must measure, not be refused."""
        rates = []

        class Recognizer(FakeMoonshine):
            def transcribe(self, samples, rate=16000):
                rates.append(rate)
                return super().transcribe(samples, rate)

        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / "0.wav"
            with wave.open(str(wav), "wb") as out:
                out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                out.writeframes(array("h", [0] * 96000).tobytes())
            result = ve.measure_asr(Recognizer(["", "hello"]), wav, clock=Clock([1.0]))
            self.assertEqual(len(ve.read_wav_16k(wav)), 64000)
        self.assertEqual(rates, [24000, 24000])
        self.assertAlmostEqual(result["rtf"], 0.25)


class ChooseTtsTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1_800_000_000.0
        self.enterContext(patch.dict(ve._silent, clear=True))
        self.enterContext(patch.object(ve, "RUN_STATE", self.state / "run" / "tts.json"))

    def choose(self, preference, piper, **options):
        return ve.choose_tts(preference, state_dir=self.state, build="b1", engine=piper, now=self.now, **options)

    def measured(self, rtf, piper=None):
        ve.record(self.state, "tts", ve.tts_key("b1", piper or FakePiper()), {"rtf": rtf, "runs": [rtf]}, self.now)

    def test_original_and_forced_natural(self):
        self.assertIsInstance(self.choose("flite", FakePiper())[0], FliteEngine)
        piper = FakePiper()
        self.assertEqual(self.choose("piper", piper), (piper, None))
        engine, note = self.choose("piper", FakePiper(installed=False))
        self.assertIsInstance(engine, FliteEngine)
        self.assertIn("not installed", note)

    def test_auto_uses_piper_only_at_or_under_the_limit(self):
        piper = FakePiper()
        # Natural first: unmeasured, the natural voice is used, and measured later.
        engine, note = self.choose("auto", piper)
        self.assertIs(engine, piper)
        self.assertIn("measured later", note)
        self.measured(0.7)
        self.assertIs(self.choose("auto", piper)[0], piper)
        self.measured(0.71)
        engine, note = self.choose("auto", piper)
        self.assertIsInstance(engine, FliteEngine)
        self.assertIn("0.71", note)

    def test_auto_measures_when_asked_and_records_it(self):
        piper = FakePiper()
        with patch.object(ve, "measure_tts", return_value={"rtf": 0.3, "runs": [0.3, 0.29, 0.31]}) as measure:
            self.assertIs(self.choose("auto", piper, measure=True)[0], piper)
            self.assertIs(self.choose("auto", piper, measure=True)[0], piper)
        measure.assert_called_once_with(piper)

    def test_saved_preference_is_used_when_none_is_given(self):
        ve.save_preference(self.state, "flite")
        self.assertIsInstance(self.choose(None, FakePiper())[0], FliteEngine)

    def test_refresh_waits_for_idle_then_switches(self):
        piper = FakePiper()
        flite = FliteEngine()

        async def refresh(idle):
            return await ve.refresh_tts(flite, "auto", state_dir=self.state, build="b1", engine=piper,
                                        idle=lambda: idle, now=self.now)

        self.assertIsNone(asyncio.run(refresh(False)))
        with patch.object(ve, "measure_tts", return_value={"rtf": 0.25, "runs": [0.25] * 3}):
            self.assertEqual(asyncio.run(refresh(True))[0], piper)
        self.assertIsNone(asyncio.run(ve.refresh_tts(piper, "auto", state_dir=self.state, build="b1", engine=piper,
                                                     idle=lambda: True, now=self.now)))

    def test_a_voice_measured_too_slow_is_unloaded(self):
        piper = FakePiper()
        with patch.object(ve, "measure_tts", side_effect=lambda engine, runs=3: engine.resident() or
                          {"rtf": 0.8, "runs": [0.8] * runs}):
            found = asyncio.run(ve.refresh_tts(FliteEngine(), "auto", state_dir=self.state, build="b1",
                                               engine=piper, idle=lambda: True, now=self.now))
        self.assertIsNone(found)
        self.assertTrue(piper.closed)

    def test_refresh_measures_one_sentence_per_job_and_stops_if_busy_again(self):
        piper = FakePiper()
        calls = []

        def one(engine, runs=3):
            calls.append(runs)
            return {"rtf": 0.2, "runs": [0.2] * runs}

        answers = iter([True, True, False])     # idle to start, then busy before the third sentence
        with patch.object(ve, "measure_tts", side_effect=one):
            found = asyncio.run(ve.refresh_tts(FliteEngine(), "auto", state_dir=self.state, build="b1",
                                               engine=piper, idle=lambda: next(answers), now=self.now))
        self.assertIsNone(found)
        self.assertEqual(calls, [1, 1])
        self.assertIsNone(ve.cached(self.state, "tts", ve.tts_key("b1", piper), self.now))
        with patch.object(ve, "measure_tts", side_effect=one):
            found = asyncio.run(ve.refresh_tts(FliteEngine(), "auto", state_dir=self.state, build="b1",
                                               engine=piper, idle=lambda: True, now=self.now))
        self.assertIs(found[0], piper)
        self.assertEqual(ve.cached(self.state, "tts", ve.tts_key("b1", piper), self.now)["runs"], [0.2] * 3)

    def test_a_voice_that_fails_to_measure_is_unloaded_and_not_retried_each_refresh(self):
        self.enterContext(patch.dict(ve._failures, clear=True))
        attempts = []

        def failing(engine, runs=3):
            attempts.append(1)
            engine.resident()
            raise RuntimeError("no audio")

        voices = [FakePiper(), FakePiper()]
        for piper in voices:
            with patch.object(ve, "measure_tts", side_effect=failing):
                found = asyncio.run(ve.refresh_tts(FliteEngine(), "auto", state_dir=self.state, build="b1",
                                                   engine=piper, idle=lambda: True, now=self.now))
            self.assertIsNone(found)
        self.assertEqual(attempts, [1])
        self.assertTrue(voices[0].closed)        # its memory was given back
        self.assertEqual(voices[1].loads, 0)     # the second refresh did not load it at all

    def test_the_voice_in_use_is_left_for_the_voice_organ_to_close(self):
        piper = FakePiper()
        piper.resident()
        self.measured(0.8, piper)
        found = asyncio.run(ve.refresh_tts(piper, "auto", state_dir=self.state, build="b1", engine=piper,
                                           idle=lambda: True, now=self.now))
        self.assertIsInstance(found[0], FliteEngine)
        self.assertFalse(getattr(piper, "closed", False))   # it may still be speaking a clause

    def test_status_for_the_voice_page(self):
        status = ve.tts_status(self.state, build="b1", engine=FakePiper(), now=self.now)
        self.assertEqual(status, {"preference": "auto", "installed": True, "label": "Alba", "rtf": None,
                                  "active": "piper", "silent": None})
        self.measured(0.2)
        self.assertEqual(ve.tts_status(self.state, build="b1", engine=FakePiper(), now=self.now)["active"], "piper")
        self.measured(0.9)
        self.assertEqual(ve.tts_status(self.state, build="b1", engine=FakePiper(), now=self.now)["active"], "flite")


class ProbeTests(unittest.TestCase):
    """Pip never goes silent: the natural voice must say one word before it is chosen."""

    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1_800_000_000.0
        self.enterContext(patch.dict(ve._silent, clear=True))
        self.enterContext(patch.object(ve, "RUN_STATE", self.state / "run" / "tts.json"))

    def choose(self, preference, piper, **options):
        # Made here, so a voice that fails is the chooser's to unload.
        with patch.object(ve, "_piper", return_value=piper):
            return ve.choose_tts(preference, state_dir=self.state, build="b1", now=self.now, **options)

    def test_a_voice_that_raises_is_set_aside_with_a_plain_note(self):
        piper = FakePiper(sound="raise")
        engine, note = self.choose("auto", piper)
        self.assertIsInstance(engine, FliteEngine)
        self.assertEqual(note, "The natural voice made no sound in a test (the voice produced no audio). "
                               "Using the original voice.")
        self.assertTrue(piper.closed)
        self.assertEqual(piper.texts, [ve.PROBE_TEXT])

    def test_the_failed_probe_is_kept_for_this_build_and_not_retried(self):
        first = FakePiper(sound="raise")
        self.choose("auto", first)
        again = FakePiper()
        engine, note = self.choose("auto", again)
        self.assertIsInstance(engine, FliteEngine)
        self.assertIn("made no sound in a test", note)
        self.assertEqual((again.texts, again.loads), ([], 0))
        self.assertEqual(ve.silent_reason("b1", again), "the voice produced no audio")
        # Another build is tested afresh.
        with patch.object(ve, "_piper", return_value=again):
            engine, _ = ve.choose_tts("auto", state_dir=self.state, build="b2", now=self.now)
        self.assertIs(engine, again)

    def test_silence_and_no_samples_count_as_no_sound(self):
        for sound, why in (("silence", "it gave only silence"), ("empty", "it gave no samples")):
            with self.subTest(sound=sound), patch.dict(ve._silent, clear=True):
                engine, note = self.choose("auto", FakePiper(sound=sound))
                self.assertIsInstance(engine, FliteEngine)
                self.assertIn(f"({why})", note)

    def test_a_voice_that_takes_too_long_is_set_aside_without_waiting(self):
        piper = FakePiper(sound="hang")
        try:
            engine, note = self.choose("auto", piper, probe_timeout=0.05)
            self.assertIsInstance(engine, FliteEngine)
            self.assertIn("took more than 0.05 s", note)
        finally:
            piper.release.set()
        for thread in threading.enumerate():
            if thread.name == "being-piper-unload":
                thread.join(5)
        self.assertTrue(piper.closed)

    def test_forced_natural_voice_still_falls_back_and_says_so(self):
        engine, note = self.choose("piper", FakePiper(sound="raise"))
        self.assertIsInstance(engine, FliteEngine)
        self.assertEqual(note, "The natural voice made no sound in a test (the voice produced no audio). "
                               "Using the original voice instead of the natural voice that was asked for.")

    def test_a_voice_that_speaks_is_probed_once_and_chosen(self):
        piper = FakePiper()
        self.assertIs(self.choose("auto", piper)[0], piper)
        self.assertIs(self.choose("piper", piper)[0], piper)
        self.assertEqual(piper.texts, [ve.PROBE_TEXT])
        self.assertEqual(ve.probe_tts(piper, build="b1"), (True, None))

    def test_a_slow_measurement_needs_no_probe(self):
        ve.record(self.state, "tts", ve.tts_key("b1", FakePiper()), {"rtf": 0.9, "runs": [0.9]}, self.now)
        piper = FakePiper(sound="raise")
        self.assertIsInstance(self.choose("auto", piper)[0], FliteEngine)
        self.assertEqual(piper.texts, [])

    def test_status_never_claims_the_natural_voice_after_it_fell_back(self):
        piper = FakePiper(sound="raise")
        self.choose("auto", piper)
        status = ve.tts_status(self.state, build="b1", engine=FakePiper(), now=self.now)
        self.assertEqual((status["active"], status["silent"]), ("flite", "the voice produced no audio"))

    def test_refresh_never_switches_to_a_voice_that_makes_no_sound(self):
        piper = FakePiper(sound="silence")
        flite = FliteEngine()
        with patch.object(ve, "measure_tts", return_value={"rtf": 0.2, "runs": [0.2]}):
            found = asyncio.run(ve.refresh_tts(flite, "auto", state_dir=self.state, build="b1", engine=piper,
                                               idle=lambda: True, now=self.now))
        # No engine to switch to, and one note for the voice to log.
        self.assertEqual(found, (None, "The natural voice made no sound in a test (it gave only silence). "
                                       "Using the original voice."))
        self.assertTrue(piper.closed)
        # Kept for this build: the next refresh does not even load it.
        again = FakePiper()
        with patch.object(ve, "measure_tts") as measure:
            self.assertIsNone(asyncio.run(ve.refresh_tts(flite, "auto", state_dir=self.state, build="b1",
                                                         engine=again, idle=lambda: True, now=self.now)))
        measure.assert_not_called()
        self.assertEqual(again.loads, 0)

    def test_refresh_leaves_a_silenced_voice_in_use_alone(self):
        current = FakePiper()
        current.silenced = "the voice produced no audio"
        with patch.object(ve, "measure_tts") as measure:
            self.assertIsNone(asyncio.run(ve.refresh_tts(current, "auto", state_dir=self.state, build="b1",
                                                         idle=lambda: True, now=self.now)))
        measure.assert_not_called()

    def test_a_clause_failure_later_is_kept_for_this_build(self):
        piper = FakePiper()
        self.assertIs(self.choose("auto", piper)[0], piper)
        self.assertTrue(piper.sticky, "only the voice organ's engine silences itself")
        piper.on_silent("a clause took more than 9 s")
        self.assertEqual(ve.silent_reason("b1", piper), "a clause took more than 9 s")
        engine, note = self.choose("auto", FakePiper())
        self.assertIsInstance(engine, FliteEngine)
        self.assertIn("(a clause took more than 9 s)", note)


class ChooseAsrTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.library = self.root / "lib.so"
        self.now = 1_800_000_000.0
        # These tests are about the speed rule; the Ivy Bridge smoke gate has its own tests.
        self.enterContext(patch.object(ve, "smoke", lambda *args, **kwargs: (True, None)))

    def install(self, wav=True):
        self.library.write_bytes(b"")
        (self.root / "silero_vad.onnx").write_bytes(b"")
        folder = self.root / "moonshine-tiny"
        folder.mkdir()
        for name in ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"):
            (folder / name).write_bytes(b"")
        if wav:
            with wave.open(str(folder / "test.wav"), "wb") as out:
                out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                out.writeframes(bytes(32000))

    def choose(self, preference, measure=False, rtf=0.2):
        wake = FakeWake()
        built = []

        def factory():
            engine = type("Engine", (), {"recognizer": FakeMoonshine(), "vad": FakeMoonshine()})()
            built.append(engine)
            return engine

        with patch.object(ve, "measure_asr", return_value={"rtf": rtf, "runs": [rtf], "text": "x"}):
            engine, note = ve.choose_asr(wake, preference, state_dir=self.state, build="b1", root=self.root,
                                         library=self.library, measure=measure, now=self.now, factory=factory)
        return wake, engine, note, built

    def test_pocketsphinx_and_missing_files(self):
        wake, engine, note, _ = self.choose("pocketsphinx")
        self.assertIs(engine, wake)
        self.assertIsNone(note)
        wake, engine, note, _ = self.choose("auto")
        self.assertIs(engine, wake)
        self.assertIn("5 files missing", note)
        wake, engine, note, _ = self.choose("whisper")
        self.assertIn("Unknown", note)

    def test_auto_needs_a_measurement_at_or_under_point_three(self):
        self.install()
        wake, engine, note, _ = self.choose("auto")
        self.assertIs(engine, wake)
        self.assertIn("not been measured", note)
        wake, engine, note, built = self.choose("auto", measure=True, rtf=0.31)
        self.assertIs(engine, wake)
        self.assertTrue(built[0].recognizer.closed)
        self.assertIn("0.31", note)
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        wake, engine, note, built = self.choose("auto", measure=True, rtf=0.3)
        self.assertIs(engine, built[0])
        # Cached now: no new measurement is needed.
        wake, engine, note, built = self.choose("auto", rtf=9)
        self.assertIs(engine, built[0])

    def test_no_test_recording_keeps_pocketsphinx(self):
        self.install(wav=False)
        wake, engine, note, built = self.choose("auto", measure=True)
        self.assertIs(engine, wake)
        self.assertIn("no test recording", note)
        self.assertEqual(built, [])     # nothing was loaded just to find that out

    def refresh(self, factory, idle=True):
        wake = FakeWake()
        return wake, asyncio.run(ve.refresh_asr(wake, wake, state_dir=self.state, build="b1", root=self.root,
                                                library=self.library, idle=lambda: idle, now=self.now,
                                                factory=factory))

    def test_refresh_loads_nothing_without_a_recording_and_backs_off_after_a_failure(self):
        self.enterContext(patch.dict(ve._failures, clear=True))
        self.enterContext(patch.dict("os.environ", {"BEING_ASR_ENGINE": "auto"}))
        builds = []

        def broken():
            builds.append(1)
            from mind import sherpa
            raise sherpa.Unavailable("the model would not load")

        self.install(wav=False)
        self.assertIsNone(self.refresh(broken)[1])
        self.assertEqual(builds, [])
        with wave.open(str(self.root / "moonshine-tiny/test.wav"), "wb") as out:
            out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            out.writeframes(bytes(32000))
        self.assertIsNone(self.refresh(broken)[1])
        self.assertIsNone(self.refresh(broken)[1])
        self.assertEqual(builds, [1])   # the second refresh waited instead of loading again

    def test_refresh_switches_on_a_kept_speed_without_measuring_again(self):
        # The hardware smoke keeps Moonshine's speed; a running voice's refresh then adopts it.
        self.enterContext(patch.dict(ve._failures, clear=True))
        self.enterContext(patch.dict("os.environ", {"BEING_ASR_ENGINE": "auto"}))
        self.install(wav=False)
        built = []

        def factory():
            built.append(type("Engine", (), {"recognizer": FakeMoonshine(), "vad": FakeMoonshine()})())
            return built[-1]

        ve.record(self.state, "asr", ve.asr_key("b1", "tiny"), {"rtf": 0.9, "runs": [0.9]}, self.now)
        self.assertIsNone(self.refresh(factory)[1])
        self.assertEqual(built, [], "a speed over the limit loads nothing")
        ve.record(self.state, "asr", ve.asr_key("b1", "tiny"), {"rtf": 0.138, "runs": [0.138]}, self.now)
        self.assertIsNone(self.refresh(factory, idle=False)[1])
        self.assertEqual(built, [])
        with patch.object(ve, "measure_asr", side_effect=AssertionError("measured again")):
            wake, result = self.refresh(factory)
        engine, note = result
        self.assertIs(engine, built[0])
        self.assertIn("0.14", note)

    def test_refresh_with_a_kept_speed_backs_off_when_moonshine_will_not_load(self):
        self.enterContext(patch.dict(ve._failures, clear=True))
        self.enterContext(patch.dict("os.environ", {"BEING_ASR_ENGINE": "auto"}))
        self.install()
        ve.record(self.state, "asr", ve.asr_key("b1", "tiny"), {"rtf": 0.138, "runs": [0.138]}, self.now)
        builds = []

        def broken():
            builds.append(1)
            from mind import sherpa
            raise sherpa.Unavailable("the model would not load")

        self.assertIsNone(self.refresh(broken)[1])
        self.assertIsNone(self.refresh(broken)[1])
        self.assertEqual(builds, [1])

    def test_forced_moonshine_skips_the_speed_rule(self):
        self.install()
        ve.record(self.state, "asr", ve.asr_key("b1", "tiny"), {"rtf": 0.9, "runs": [0.9]}, self.now)
        wake, engine, note, built = self.choose("sherpa-onnx")
        self.assertIs(engine, built[0])

    def test_real_engine_construction_reports_unavailable_honestly(self):
        self.install()
        ve.record(self.state, "asr", ve.asr_key("b1", "tiny"), {"rtf": 0.2, "runs": [0.2]}, self.now)
        wake = FakeWake()
        engine, note = ve.choose_asr(wake, "auto", state_dir=self.state, build="b1", root=self.root,
                                     library=self.library, now=self.now)
        self.assertIs(engine, wake)
        self.assertIn("Moonshine is unavailable", note)
        self.assertIn("Using PocketSphinx", note)


def context(state, *, guest=False, voice=None, sent=None):
    async def send(topic, **fields):
        sent.append((topic, fields))

    return Context(now=None, clock_trusted=True, speaker="guest" if guest else "Richard", guest=guest,
                   state_dir=state, owner="Richard", last_heard=None, last_said=None, body={}, voice=voice or {},
                   send=send)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(ve._silent, clear=True))
        self.enterContext(patch.object(ve, "RUN_STATE", self.state / "run" / "tts.json"))
        self.sent = []
        self.piper = FakePiper()
        self.enterContext(patch.object(ve, "_piper", return_value=self.piper))
        self.enterContext(patch.object(ve, "build_id", return_value="b1"))

    def say(self, text, **options):
        return asyncio.run(ve.handle(text, context(self.state, sent=self.sent, **options)))

    def test_phrases_match(self):
        for phrase in ("Use your natural voice", "Hey Pip, use your original voice", "Which voice are you using?",
                       "switch to your natural voice please", "go back to your original voice",
                       "what voice is this", "choose your voice yourself"):
            self.assertTrue(ve.may_match(phrase), phrase)
        for phrase in ("what's your name", "use your words", "the voice of reason", "speak up"):
            self.assertFalse(ve.may_match(phrase), phrase)

    def test_natural_voice_is_sent_to_the_voice_which_keeps_it(self):
        reply = self.say("Use your natural voice")
        self.assertEqual(reply.line, "[joy] Okay, I'll use my natural voice.")
        self.assertEqual(self.sent, [("voice.engine", {"tts": "piper"})])
        self.assertFalse((self.state / ve.VOICE_SETTINGS).exists())
        self.assertFalse(reply.keep)

    def test_a_slow_natural_voice_says_so(self):
        ve.record(self.state, "tts", ve.tts_key("b1", self.piper), {"rtf": 0.9, "runs": [0.9]})
        self.assertIn("slower to answer", self.say("Use your natural voice").line)

    def test_original_voice(self):
        self.assertEqual(self.say("Use your original voice").line, "[neutral] Okay, I'll use my original voice.")
        self.assertEqual(self.sent, [("voice.engine", {"tts": "flite"})])
        self.assertEqual(self.say("choose your voice yourself").line,
                         "[neutral] Okay. I'll choose my voice by how fast it runs here.")
        self.assertEqual(self.sent[-1], ("voice.engine", {"tts": "auto"}))

    def test_not_installed_and_guest(self):
        self.piper.installed = False
        self.assertIn("isn't installed", self.say("Use your natural voice").line)
        self.assertEqual(self.sent, [])
        self.piper.installed = True
        self.assertEqual(self.say("Use your natural voice", guest=True).line,
                         "[neutral] Only Richard can change my voice.")
        self.assertEqual(self.sent, [])

    def test_which_voice_answers_from_the_voice_organ_first(self):
        self.assertEqual(self.say("Which voice are you using?", voice={"tts": "piper"}).line,
                         "[neutral] I'm using my natural voice, Alba. I'll check it keeps up on this laptop "
                         "when I'm resting.")
        self.assertEqual(self.say("Which voice are you using?").line,
                         "[neutral] I'm using my natural voice, Alba. I'll check it keeps up on this laptop "
                         "when I'm resting.")
        ve.record(self.state, "tts", ve.tts_key("b1", self.piper), {"rtf": 0.9, "runs": [0.9]})
        self.assertEqual(self.say("Which voice are you using?").line,
                         "[neutral] I'm using my original voice. My natural voice measured too slow on this laptop.")
        ve.save_preference(self.state, "flite")
        self.assertEqual(self.say("what voice is this").line, "[neutral] I'm using my original voice.")
        # Guests may ask.
        self.assertIsNotNone(self.say("Which voice are you using?", guest=True))

    def report(self, why="the voice produced no audio", pid=None, build="b1"):
        """What the voice organ (another process) writes when the natural voice made no sound."""
        ve._write_json(ve.RUN_STATE, {"key": ve.tts_key(build, self.piper), "reason": why,
                                      "pid": os.getpid() if pid is None else pid})

    def test_speaking_flite_alone_is_not_a_fall_back(self):
        # While the natural voice loads, or with being.tts=flite, the voice reports Flite.
        self.assertEqual(self.say("Which voice are you using?", voice={"tts": "flite"}).line,
                         "[neutral] I'm using my original voice for now.")
        reply = self.say("Use your natural voice", voice={"tts": "flite"})
        self.assertEqual(reply.line, "[joy] Okay, I'll use my natural voice.")
        self.assertEqual(self.sent, [("voice.engine", {"tts": "piper"})])

    def test_which_voice_is_honest_after_the_natural_voice_fell_back(self):
        self.report()
        for voice in ({}, {"tts": "flite"}):
            self.assertEqual(self.say("Which voice are you using?", voice=voice).line,
                             "[neutral] I'm using my original voice. My natural voice made no sound on this "
                             "laptop, so I switched.")
        reply = self.say("Use your natural voice", voice={"tts": "flite"})
        self.assertEqual(reply.line, "[concern] My natural voice made no sound on this laptop, so I'll keep "
                                     "this one for now.")
        self.assertEqual(self.sent, [("voice.engine", {"tts": "piper"})])
        # The owner's own choice of the original voice is not a fall-back...
        ve.save_preference(self.state, "flite")
        self.assertEqual(self.say("what voice is this", voice={"tts": "flite"}).line,
                         "[neutral] I'm using my original voice.")
        # ...but asking for the natural voice again still makes no promise.
        self.assertEqual(self.say("Use your natural voice", voice={"tts": "flite"}).line,
                         "[concern] My natural voice made no sound on this laptop, so I'll keep this one for now.")
        # The voice organ speaking with the natural voice wins over an old report.
        ve.save_preference(self.state, "auto")
        self.assertIn("natural voice, Alba", self.say("Which voice are you using?", voice={"tts": "piper"}).line)

    def test_a_report_from_a_voice_that_has_gone_or_another_build_counts_for_nothing(self):
        dead = 2 ** 22 + 12345
        with patch.object(ve, "_alive", side_effect=lambda pid: pid != dead):
            self.report(pid=dead)
            self.assertIsNone(ve.tts_status(self.state)["silent"])
            self.report(build="b0")
            self.assertIsNone(ve.tts_status(self.state)["silent"])
            self.report()
            status = ve.tts_status(self.state)
        self.assertEqual((status["active"], status["silent"]), ("flite", "the voice produced no audio"))
        self.assertTrue(ve._alive(os.getpid()))
        self.assertFalse(ve._alive("1"))

    def test_the_voice_organ_reports_its_fall_back_to_setup_and_clears_it_when_the_voice_speaks(self):
        # Setup's voice page is another process: it reads the report, not the voice's RAM.
        ve._keep_silent("b1", self.piper, "the voice produced no audio")
        report = json.loads(ve.RUN_STATE.read_text())
        self.assertEqual(report, {"key": ve.tts_key("b1", self.piper), "reason": "the voice produced no audio",
                                  "pid": os.getpid()})
        ve._silent.clear()
        status = ve.tts_status(self.state)
        self.assertEqual((status["active"], status["silent"]), ("flite", "the voice produced no audio"))
        # After a restart whose probe passes, the report goes.
        ve._clear_silent("b1", self.piper)
        self.assertFalse(ve.RUN_STATE.exists())
        self.assertEqual(ve.tts_status(self.state)["active"], "piper")

    def test_other_words_pass(self):
        self.assertIsNone(self.say("tell me a joke"))

    def test_lines_are_short_and_tagged(self):
        lines = [self.say(p).line for p in ("Use your natural voice", "Use your original voice",
                                             "Which voice are you using?", "choose your voice yourself")]
        for line in lines:
            self.assertRegex(line, r"^\[(neutral|joy|concern)\] ")
            self.assertLessEqual(len(line.split()) - 1, 25, line)


if __name__ == "__main__":
    unittest.main()
