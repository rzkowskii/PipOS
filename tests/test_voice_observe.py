"""Seeing what the voice heard and did: voice debugging, playback timing, arecord's
reports, the asleep one-breath wake, forced engines, the Try measurement and the
laptop's microphone channel default."""

import asyncio
import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from mind import mic_channel, voice as voice_module, voice_engines as ve
from mind.asr_sherpa import SherpaRecognizer
from mind.tts import FliteEngine
from mind.voice import Voice, capture_command, voice_debug
from tests.test_asr_sherpa import FakeMoonshine, FakeWake
from tests.test_recognition_oneshot import run, scripted
from tests.test_voice_feedback import WAKE, Harness, real_sleep, tone


def write(folder, name, text):
    path = Path(folder) / name
    path.write_text(text)
    return path


class VoiceDebug(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())

    def test_the_environment_turns_it_on_anywhere(self):
        self.assertEqual(voice_debug({"BEING_VOICE_DEBUG": "1"}, Path("/nonexistent"), try_boot=False),
                         (True, "BEING_VOICE_DEBUG"))

    def test_the_kernel_argument_counts_only_on_a_try_boot(self):
        line = write(self.folder, "cmdline", "ro being.try=1 being.voice_debug=1 quiet")
        self.assertEqual(voice_debug({}, line, try_boot=True), (True, "the kernel command line"))
        with self.assertLogs("being.voice", "INFO") as logs:
            self.assertEqual(voice_debug({}, line, try_boot=False), (False, None))
        self.assertIn("only on a Try boot", logs.output[0])
        self.assertEqual(voice_debug({}, write(self.folder, "plain", "ro being.try=1"), try_boot=True),
                         (False, None))
        self.assertEqual(voice_debug({}, write(self.folder, "zero", "being.voice_debug=0"), try_boot=True),
                         (False, None))
        self.assertEqual(voice_debug({"BEING_VOICE_DEBUG": "0"}, Path("/nonexistent")), (False, None))

    def test_the_last_kernel_argument_counts(self):
        line = write(self.folder, "cmdline", "being.try=1 being.cable=1 being.voice_debug=1 quiet being.voice_debug=0")
        self.assertEqual(voice_debug({}, line, try_boot=True), (False, None))
        line = write(self.folder, "again", "being.voice_debug=0 being.voice_debug=1")
        self.assertEqual(voice_debug({}, line, try_boot=True), (True, "the kernel command line"))

    def test_a_diagnostic_microphone_that_did_not_answer_drops_its_debugging(self):
        from mind.voice import debug_environment

        environ = {"BEING_DIAG_MIC": "/run/being-diag/mic.sock", "BEING_VOICE_DEBUG": "1", "PATH": "/bin"}
        self.assertIs(debug_environment(environ, environ["BEING_DIAG_MIC"], True), environ)
        self.assertIs(debug_environment({"BEING_VOICE_DEBUG": "1"}, "", False).get("BEING_VOICE_DEBUG"), "1")
        with self.assertLogs("being.voice", "INFO") as logs:
            dropped = debug_environment(environ, environ["BEING_DIAG_MIC"], False)
        self.assertEqual(dropped, {"BEING_DIAG_MIC": "/run/being-diag/mic.sock", "PATH": "/bin"})
        self.assertIn("did not answer", logs.output[0])
        self.assertEqual(voice_debug(dropped, Path("/nonexistent"), try_boot=True), (False, None))


class Heard(unittest.IsolatedAsyncioTestCase):
    async def final(self, transcripts, guest=False, level="INFO"):
        script = [(True, [("hear.speech_start", {})]), (False, [("hear.final", {"text": "what time is it"})])]
        harness = Harness(script, transcripts=transcripts)
        if guest:
            await harness.voice.handle({"t": "app.privacy", "guest": True})
        harness.voice.state = "attending"
        with self.assertLogs("being.voice", level) as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(tone(4000), tone(4000))
                harness.voice.disarm()
        return logs.output

    async def test_the_heard_line_appears_only_in_voice_debugging(self):
        output = await self.final(True)
        heard = [line for line in output if "heard: " in line]
        self.assertEqual(len(heard), 1)
        self.assertIn('heard: "what time is it" (engine pocketsphinx, confidence 0.83, log-posterior unknown)',
                      heard[0])
        self.assertTrue(heard[0].startswith("INFO"))
        output = await self.final(False)
        self.assertFalse(any("what time" in line for line in output))

    async def test_a_guests_words_never_reach_the_log(self):
        output = await self.final(True, guest=True, level="DEBUG")
        self.assertFalse(any("what time" in line for line in output), output)
        self.assertTrue(any("dictation ended" in line for line in output))
        output = await self.final(True, level="DEBUG")
        self.assertTrue(any("final transcript text: 'what time is it'" in line for line in output))

    async def test_guest_mode_follows_app_privacy(self):
        voice = Harness(transcripts=True).voice
        self.assertIn("app.privacy", voice_module.SUBSCRIPTIONS)
        self.assertTrue(voice.text_logged())
        with self.assertLogs("being.voice", "INFO") as logs:
            await voice.handle({"t": "app.privacy", "guest": True})
        self.assertFalse(voice.text_logged())
        self.assertIn("guest mode on: transcript text stays out of the log", logs.output[0])
        await voice.handle({"t": "app.privacy", "guest": False})
        self.assertTrue(voice.text_logged())

    async def test_the_end_of_a_dictation_reports_its_voiced_part(self):
        output = await self.final(False)
        ended = next(line for line in output if "dictation ended" in line)
        self.assertIn("; voiced ", ended)


class Playback(unittest.IsolatedAsyncioTestCase):
    async def test_each_clause_logs_its_length_rate_and_wall_time_without_words(self):
        voice = Harness().voice
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "clause.wav"
            with wave.open(str(clip), "wb") as out:
                out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                out.writeframes(bytes(2 * 1600))       # 0.1 s

            class Done:
                returncode = 0

                async def wait(self):
                    return 0

            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def player(*command, **options):
                yield Done()

            with patch("mind.voice.child", player), self.assertLogs("being.voice", "INFO") as logs:
                await voice.play_once(clip, False)
        line = next(line for line in logs.output if "playback {" in line)
        timing = json.loads(line.split("playback ", 1)[1])
        self.assertEqual(set(timing), {"wav_s", "rate", "wall_s"})
        self.assertEqual((timing["wav_s"], timing["rate"]), (0.1, 16000))
        self.assertGreaterEqual(timing["wall_s"], 0.0)

    def test_a_clip_far_off_its_length_is_reported(self):
        voice = Harness().voice
        with self.assertLogs("being.voice", "INFO") as logs:
            voice.playback_timing(2.17, 22050, 2.36)
            voice.playback_timing(5.0, 22050, 3.0)
            voice.playback_timing(1.0, 22050, 2.5)
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 2)
        self.assertIn("a 5.00 s clip took 3.00 s", warnings[0])


class Arecord(unittest.TestCase):
    def test_capture_is_not_quiet_so_overruns_reach_the_journal(self):
        command = capture_command("plughw:0,0", "auto")
        self.assertEqual(command[:3], ["arecord", "-D", "plughw:0,0"])
        self.assertNotIn("-q", command)


class AsleepOneBreath(unittest.TestCase):
    def test_asleep_a_run_on_wake_phrase_still_wakes_plainly(self):
        recognizer = scripted(set(range(41, 141)), wake_frame=60)
        recognizer.run_on_wake = True
        events, _ = run(recognizer, 160, oneshot=False)
        self.assertEqual(events, [("hear.sound", {}), ("hear.wake", {"run_on": True})])
        self.assertFalse(recognizer.oneshot)
        self.assertIsNone(recognizer.mode)

    def test_thinking_still_ignores_it(self):
        recognizer = scripted(set(range(41, 141)), wake_frame=60)
        events, _ = run(recognizer, 160, oneshot=False)
        self.assertEqual(events, [("hear.sound", {})])

    def test_moonshine_passes_the_switch_to_its_wake_spotter(self):
        wake = FakeWake()
        engine = SherpaRecognizer(wake, FakeMoonshine(), FakeMoonshine())
        self.assertFalse(engine.run_on_wake)
        engine.run_on_wake = True
        self.assertTrue(wake.run_on_wake)
        self.assertTrue(engine.run_on_wake)


class AsleepVoice(unittest.IsolatedAsyncioTestCase):
    async def test_the_voice_sets_the_switch_only_asleep_and_sends_a_plain_wake(self):
        harness = Harness([(False, [("hear.wake", {"run_on": True})])])
        harness.voice.state = "asleep"
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(tone(3000))
                self.assertTrue(harness.recognizer.run_on_wake)
                harness.voice.state = "idle"
                await harness.frames(tone(3000))
                self.assertFalse(harness.recognizer.run_on_wake)
                harness.voice.disarm()
        self.assertEqual(harness.sent[0], ("hear.wake", {}))
        self.assertTrue(any("ran on into more words while asleep" in line for line in logs.output))


class Forced(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())

    def test_the_environment_wins_over_the_kernel_and_the_old_name_comes_last(self):
        line = write(self.folder, "cmdline", "being.asr=pocketsphinx being.tts=piper")
        self.assertEqual(ve.forced("asr", {"BEING_ASR": "Moonshine"}, line), ("moonshine", "BEING_ASR"))
        self.assertEqual(ve.forced("asr", {"BEING_ASR_ENGINE": "moonshine"}, line),
                         ("pocketsphinx", "the kernel command line"))
        self.assertEqual(ve.forced("asr", {"BEING_ASR_ENGINE": "sherpa"}, Path("/nonexistent")),
                         ("moonshine", "BEING_ASR_ENGINE"))
        self.assertEqual(ve.forced("tts", {}, line), ("piper", "the kernel command line"))
        self.assertEqual(ve.forced("tts", {"BEING_TTS": "flite"}, line), ("flite", "BEING_TTS"))
        self.assertEqual(ve.forced("tts", {"BEING_TTS": "kal"}, Path("/nonexistent")), ("auto", None))
        self.assertEqual(ve.forced("tts", {}, Path("/nonexistent")), ("auto", None))

    def test_a_forced_moonshine_needs_no_smoke_record_but_auto_does(self):
        root = Path(self.folder) / "asr"
        library = root / "lib.so"
        root.mkdir()
        library.write_bytes(b"")
        (root / "silero_vad.onnx").write_bytes(b"")
        (root / "moonshine-tiny").mkdir()
        for name in ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"):
            (root / "moonshine-tiny" / name).write_bytes(b"")
        wake, built = FakeWake(), []

        def factory():
            built.append(1)
            return type("Engine", (), {"recognizer": FakeMoonshine(), "vad": FakeMoonshine()})()

        state = Path(self.folder) / "state"
        with patch.object(ve, "SMOKE_VERDICTS", ()):
            engine, note = ve.choose_asr(wake, "auto", state_dir=state, build="b1", root=root, library=library,
                                         measure=True, factory=factory)
            self.assertIs(engine, wake)
            self.assertIn("waits for a passed Ivy Bridge speech test", note)
            self.assertEqual(built, [], "nothing is loaded before the CPU is known to run it")
            engine, note = ve.choose_asr(wake, "moonshine", state_dir=state, build="b1", root=root,
                                         library=library, factory=factory)
            self.assertIsNot(engine, wake)
            self.assertIsNone(note)

    def test_refresh_never_loads_moonshine_without_a_smoke_record(self):
        async def check():
            with patch.object(ve, "smoke", lambda *args, **kwargs: (False, "none")), \
                    patch("mind.asr_sherpa.missing", return_value=[]), \
                    patch("mind.asr_sherpa.test_wav", return_value=Path("/x.wav")), \
                    patch.dict("os.environ", {}, clear=True), \
                    patch.object(ve, "choose_asr", side_effect=AssertionError("loaded")):
                wake = FakeWake()
                return await ve.refresh_asr(wake, wake, state_dir=self.folder, build="b1", idle=lambda: True)

        self.assertIsNone(asyncio.run(check()))


class Smoke(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.library = self.folder / "libsherpa-onnx-c-api.so"
        self.library.write_bytes(b"runtime bytes")
        (self.folder / "libonnxruntime.so").write_bytes(b"onnx runtime bytes")

    def pins(self, **changes):
        pins = {"libsherpa-onnx-c-api.so": hashlib.sha256(b"runtime bytes").hexdigest(),
                "libonnxruntime.so": hashlib.sha256(b"onnx runtime bytes").hexdigest()}
        pins.update(changes)
        return {name: digest for name, digest in pins.items() if digest is not None}

    def report(self, name="smoke.json", **changes):
        report = {"passed": True, "sigill": False, "qemu_cpu": "IvyBridge",
                  "runtime_sha256": self.pins(),
                  "stages": [{"stage": "vad", "passed": True}, {"stage": "moonshine", "passed": True},
                             {"stage": "piper", "passed": True}]}
        report.update(changes)
        path = self.folder / name
        path.write_text(json.dumps(report))
        return path

    def test_a_passed_report_for_this_runtime_allows_moonshine(self):
        self.assertEqual(ve.sherpa_smoke_passed(paths=[self.report()], library=self.library), (True, None))

    def test_anything_less_does_not(self):
        cases = [
            self.report("a.json", passed=False),
            self.report("b.json", sigill=True),
            self.report("c.json", stages=[{"stage": "vad", "passed": True}]),
            self.report("d.json", stages=[{"stage": "moonshine", "passed": False}]),
            self.report("e.json", runtime_sha256=self.pins(**{"libsherpa-onnx-c-api.so": "0" * 64})),
            # The runtime holds the AVX kernels: a new one with the same C API is untested.
            self.report("f.json", runtime_sha256=self.pins(**{"libonnxruntime.so": "0" * 64})),
            # A report must say which bytes it tested.
            self.report("g.json", runtime_sha256=None),
            self.report("h.json", runtime_sha256=self.pins(**{"libonnxruntime.so": None})),
            self.report("i.json", runtime_sha256="abc"),
            self.folder / "missing.json",
        ]
        for path in cases:
            ok, reason = ve.sherpa_smoke_passed(paths=[path], library=self.library)
            self.assertFalse(ok, path)
            self.assertTrue(reason)
        self.assertIn("ONNX Runtime", ve.sherpa_smoke_passed(paths=[cases[5]], library=self.library)[1])
        self.assertIn("does not pin", ve.sherpa_smoke_passed(paths=[cases[6]], library=self.library)[1])

    def test_a_missing_runtime_is_no_pass(self):
        report = self.report()
        (self.folder / "libonnxruntime.so").unlink()
        ok, reason = ve.sherpa_smoke_passed(paths=[report], library=self.library)
        self.assertFalse(ok)
        self.assertIn("could not be checked", reason)

    def test_a_later_report_can_still_pass_after_a_stale_one(self):
        stale = self.report("stale.json", runtime_sha256=self.pins(**{"libonnxruntime.so": "0" * 64}))
        self.assertEqual(ve.sherpa_smoke_passed(paths=[stale, self.report()], library=self.library), (True, None))

    def test_a_report_kept_in_the_mind_counts_too(self):
        state = self.folder / "state"
        (state / "audio").mkdir(parents=True)
        (state / ve.SMOKE_STATE).write_text(self.report().read_text())
        self.assertEqual(ve.sherpa_smoke_passed(state, paths=[], library=self.library), (True, None))


class Pressure(unittest.TestCase):
    def test_cpu_pressure_and_its_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            psi = write(folder, "cpu", "some avg10=3.20 avg60=1.00 avg300=0.50 total=12345\n"
                                       "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
            self.assertEqual(ve.cpu_pressure(psi), 3.2)
            self.assertTrue(ve.cpu_quiet(path=psi))
            self.assertFalse(ve.cpu_quiet(threshold=2.0, path=psi))
            self.assertIsNone(ve.cpu_pressure(write(folder, "bad", "some avg10=lots\n")))
            with patch("os.getloadavg", return_value=(0.2, 0, 0)):
                self.assertTrue(ve.cpu_quiet(path=Path(folder) / "none"))
            with patch("os.getloadavg", return_value=(3.0, 0, 0)):
                self.assertFalse(ve.cpu_quiet(path=Path(folder) / "none"))


class FakePiper:
    """A loaded Piper voice, as far as switching to it goes."""

    name = "piper"

    def close(self):
        pass


class FakeEngines:
    """voice_engines as the voice sees it, recording what it was asked."""

    def __init__(self, quiet):
        self.quiet = list(quiet)
        self.asked = []
        self.tts_asked = []

    def cpu_quiet(self):
        return self.quiet.pop(0) if self.quiet else True

    def choose_tts(self, choice, state_dir=None):
        self.tts_asked.append(choice)
        return (FakePiper() if choice == "piper" else FliteEngine()), None

    def choose_asr(self, wake, state_dir=None, wake_word="Hey Pip"):
        return wake, None

    async def refresh_tts(self, current, choice, *, state_dir, idle=None):
        self.asked.append(("tts", choice, idle))
        return None

    async def refresh_asr(self, current, wake, *, state_dir, wake_word, idle=None):
        self.asked.append(("asr", idle))
        return None


class TryMeasurement(unittest.IsolatedAsyncioTestCase):
    def voice(self, engines, **options):
        voice = Voice(object(), "Richard", ["arecord"], "plughw:0,0", engines=engines,
                      settings_dir=Path("/nonexistent"), **options)
        voice.state = "idle"
        return voice

    async def test_one_measurement_after_the_mind_is_ready_once_the_cpu_is_quiet(self):
        engines = FakeEngines([False, False, True])
        voice = self.voice(engines, try_boot=True)
        with patch.object(voice_module, "TRY_MEASURE_AFTER", 0.01), \
                patch.object(voice_module, "TRY_MEASURE_POLL", 0.001), self.assertLogs("being.voice", "INFO"):
            task = asyncio.create_task(voice.choose_engines())
            await real_sleep(0.05)
            self.assertEqual(engines.asked, [], "nothing before mind.ready")
            await voice.handle({"t": "mind.ready", "cortex": True})
            async with asyncio.timeout(2):
                while len(engines.asked) < 2:
                    await real_sleep(0.005)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([entry[0] for entry in engines.asked], ["tts", "asr"])
        self.assertEqual(engines.asked[0][2].__func__, FakeEngines.cpu_quiet, "each run re-checks the quiet")
        self.assertEqual(engines.quiet, [])

    async def test_it_gives_up_if_the_cpu_never_goes_quiet(self):
        engines = FakeEngines([False] * 100)
        voice = self.voice(engines, try_boot=True)
        voice.mind_ready.set()
        with patch.object(voice_module, "TRY_MEASURE_AFTER", 0), \
                patch.object(voice_module, "TRY_MEASURE_POLL", 0.001), \
                patch.object(voice_module, "TRY_MEASURE_PATIENCE", 0.005), \
                self.assertLogs("being.voice", "INFO") as logs:
            await voice.try_measurement(voice.recognizer)
        self.assertEqual(engines.asked, [])
        self.assertTrue(any("never went quiet" in line for line in logs.output))

    async def test_an_installed_boot_waits_for_the_usual_refresh(self):
        engines = FakeEngines([])
        voice = self.voice(engines, try_boot=False)
        voice.mind_ready.set()
        with patch.object(voice_module, "TRY_MEASURE_AFTER", 0):
            task = asyncio.create_task(voice.choose_engines())
            await real_sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(engines.asked, [])

    async def test_a_forced_voice_is_chosen_without_the_speed_rule_and_never_refreshed(self):
        for saved in ("flite", "auto"):
            engines = FakeEngines([])
            voice = self.voice(engines, tts_override="piper", tts_choice=saved)
            task = asyncio.create_task(voice.choose_engines())
            await real_sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(engines.tts_asked, ["piper"])
            self.assertIsInstance(voice.tts, FakePiper, f"Pip speaks with the forced voice (saved {saved})")
            await voice.refresh_engines(voice.recognizer)
            self.assertEqual([entry[0] for entry in engines.asked], ["asr"])

    async def test_the_owners_spoken_choice_ends_the_forced_voice(self):
        engines = FakeEngines([])
        voice = self.voice(engines, tts_override="piper", tts_choice="auto")
        voice.save_settings = lambda: None
        await voice.adopt_tts(voice.wanted_tts())
        self.assertIsInstance(voice.tts, FakePiper)
        with self.assertLogs("being.voice", "INFO") as logs:
            await voice.change_engine("flite")
            await voice.engine_task
        self.assertIsNone(voice.tts_override)
        self.assertIsInstance(voice.tts, FliteEngine)
        self.assertTrue(any("forced piper voice no longer applies" in line for line in logs.output))

    async def test_the_refresh_runs_while_the_try_measurement_waits(self):
        # mind.ready went out before this voice started (a pip-diag restart).
        engines = FakeEngines([])
        voice = self.voice(engines, try_boot=True)
        with patch.object(voice_module, "ENGINE_REFRESH_SECONDS", 0.005):
            task = asyncio.create_task(voice.choose_engines())
            await real_sleep(0.1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            voice.try_task.cancel()
            await asyncio.gather(voice.try_task, return_exceptions=True)
        self.assertGreater(len(engines.asked), 2, "the 900 s refresh is not held up by mind.ready")

    async def test_without_mind_ready_the_try_measurement_still_comes(self):
        engines = FakeEngines([True])
        voice = self.voice(engines, try_boot=True)
        with patch.object(voice_module, "TRY_READY_PATIENCE", 0.01), \
                patch.object(voice_module, "TRY_MEASURE_AFTER", 0.01), \
                self.assertLogs("being.voice", "INFO") as logs:
            await voice.try_measurement(voice.recognizer)
        self.assertEqual([entry[0] for entry in engines.asked], ["tts", "asr"])
        self.assertTrue(any("no mind.ready in 0.01 s" in line for line in logs.output))

    async def test_two_measurements_never_overlap(self):
        engines = FakeEngines([])
        voice = self.voice(engines)
        async with voice.measuring:
            with self.assertLogs("being.voice", "INFO") as logs:
                await voice.refresh_engines(voice.recognizer)
        self.assertEqual(engines.asked, [])
        self.assertTrue(any("already under way" in line for line in logs.output))


class MicrophoneDefault(unittest.TestCase):
    def test_the_laptop_image_defaults_to_auto_and_the_lab_to_mix(self):
        with tempfile.TemporaryDirectory() as folder:
            laptop = write(folder, "laptop", "laptop\n")
            lab = write(folder, "lab", "lab\n")
            missing = Path(folder) / "cmdline"
            self.assertEqual(mic_channel.setting({}, missing, laptop), ("auto", "the laptop default"))
            self.assertEqual(mic_channel.setting({}, missing, lab), ("mix", "the default"))
            self.assertEqual(mic_channel.setting({}, missing, Path(folder) / "none"), ("mix", "the default"))
            self.assertEqual(mic_channel.setting({"BEING_MIC_CHANNEL": "mix"}, missing, laptop),
                             ("mix", "BEING_MIC_CHANNEL"))
            line = write(folder, "cmdline2", "being.mic_channel=right")
            self.assertEqual(mic_channel.setting({}, line, laptop), ("right", "the kernel command line"))

    def test_kernel_arg_reads_the_last_value(self):
        with tempfile.TemporaryDirectory() as folder:
            line = write(folder, "cmdline", "being.asr=a being.asr=b x=1")
            self.assertEqual(mic_channel.kernel_arg("being.asr=", line), "b")
            self.assertIsNone(mic_channel.kernel_arg("being.tts=", line))


if __name__ == "__main__":
    unittest.main()
