"""Recording the Ivy Bridge speech smoke on the laptop, and what the voice accepts as a pass."""

import hashlib
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import voice_engines as ve

SIGILL = 128 + signal.SIGILL


class Runtime:
    """A fake installed runtime: the C API and the ONNX Runtime beside it."""

    def __init__(self, folder):
        self.folder = Path(folder) / "lib"
        self.folder.mkdir()
        self.api = self.folder / "libsherpa-onnx-c-api.so"
        self.api.write_bytes(b"api bytes")
        (self.folder / "libonnxruntime.so").write_bytes(b"onnx runtime bytes")

    def line(self, stage, **report):
        paths = [str(self.api), str(self.folder / "libonnxruntime.so")]
        return ve.SMOKE_MARK + json.dumps({"stage": stage, "passed": True, "runtime_paths": paths, **report})


class HardwareSmoke(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = Runtime(self.root)
        self.state = self.root / "mind"
        self.build = self.root / "build-id"
        self.build.write_text("v6-test\n")
        self.enterContext(patch.object(ve, "BUILD_ID", self.build))

    def run_smoke(self, outcomes=None, record=True):
        outcomes = outcomes or {}

        def runner(stage):
            return outcomes.get(stage, (0, self.runtime.line(stage)))

        return ve.hardware_smoke(self.state, record=record, library=self.runtime.api, runner=runner, now=123.0)

    def test_a_pass_is_pinned_to_the_installed_bytes_and_this_build(self):
        report = self.run_smoke()
        self.assertTrue(report["passed"])
        self.assertIs(report["sigill"], False)
        self.assertEqual([stage["stage"] for stage in report["stages"]], ["vad", "moonshine", "piper"])
        self.assertEqual(report["runtime_sha256"], {
            "libsherpa-onnx-c-api.so": hashlib.sha256(b"api bytes").hexdigest(),
            "libonnxruntime.so": hashlib.sha256(b"onnx runtime bytes").hexdigest()})
        self.assertEqual((report["source"], report["build"], report["measured"]), ("hardware", "v6-test", 123.0))
        kept = json.loads((self.state / ve.SMOKE_STATE).read_text())
        self.assertEqual(kept, report)
        # The voice's own check takes it, on this build with these bytes.
        self.assertEqual(ve.sherpa_smoke_passed(self.state, paths=[], library=self.runtime.api), (True, None))

    def test_another_build_or_other_bytes_withdraw_the_pass(self):
        self.run_smoke()
        self.build.write_text("v7-test\n")
        ok, reason = ve.sherpa_smoke_passed(self.state, paths=[], library=self.runtime.api)
        self.assertFalse(ok)
        self.assertIn("another build", reason)
        self.build.write_text("v6-test\n")
        (self.runtime.folder / "libonnxruntime.so").write_bytes(b"a new runtime")
        ok, reason = ve.sherpa_smoke_passed(self.state, paths=[], library=self.runtime.api)
        self.assertFalse(ok)
        self.assertIn("another sherpa-onnx or ONNX Runtime", reason)

    def test_sigill_or_any_failed_stage_is_no_pass_and_replaces_an_older_pass(self):
        self.run_smoke()
        report = self.run_smoke({"moonshine": (SIGILL, "")})
        self.assertFalse(report["passed"])
        self.assertTrue(report["sigill"])
        self.assertIn("SIGILL", report["stages"][1]["error"])
        self.assertFalse(ve.sherpa_smoke_passed(self.state, paths=[], library=self.runtime.api)[0])
        for outcome in ((-signal.SIGILL, ""), (1, "Traceback"), (0, "no verdict"), (None, "it took more than 300 s"),
                        (0, self.runtime.line("vad", passed=False)),
                        (0, ve.SMOKE_MARK + json.dumps({"passed": True, "runtime_paths": ["/elsewhere/lib.so"]}))):
            report = self.run_smoke({"vad": outcome})
            self.assertFalse(report["passed"], outcome)
            self.assertTrue(report["stages"][0]["error"], outcome)

    def shipped(self, **changes):
        """A passing QEMU verdict shipped in the image, pinned to the installed bytes."""
        path = self.root / "shipped.json"
        verdict = {"source": "qemu-ivybridge", "passed": True, "sigill": False,
                   "stages": [{"stage": stage, "passed": True, "sigill": False} for stage in ve.SMOKE_STAGES],
                   "runtime_sha256": ve.runtime_pins(self.runtime.api), **changes}
        path.write_text(json.dumps(verdict))
        return path

    def test_a_failure_recorded_on_this_laptop_outweighs_a_shipped_pass(self):
        shipped = self.shipped()
        self.assertEqual(ve.sherpa_smoke_passed(self.state, paths=[shipped], library=self.runtime.api), (True, None))
        for outcome in ((SIGILL, ""), (1, "Traceback")):
            self.run_smoke({"moonshine": outcome})
            ok, reason = ve.sherpa_smoke_passed(self.state, paths=[shipped], library=self.runtime.api)
            self.assertFalse(ok, outcome)
            self.assertIn("failed smoke on this laptop", reason)
        # A failure that pinned nothing (the runtime could not be read) still decides for this build.
        (self.state / ve.SMOKE_STATE).write_text(json.dumps({"build": "v6-test", "passed": False, "stages": []}))
        self.assertFalse(ve.sherpa_smoke_passed(self.state, paths=[shipped], library=self.runtime.api)[0])

    def test_a_record_from_another_build_or_other_bytes_leaves_the_shipped_verdict_to_decide(self):
        shipped = self.shipped()
        self.run_smoke({"moonshine": (SIGILL, "")})
        self.build.write_text("v7-test\n")
        self.assertEqual(ve.sherpa_smoke_passed(self.state, paths=[shipped], library=self.runtime.api), (True, None))
        self.build.write_text("v6-test\n")
        recorded = json.loads((self.state / ve.SMOKE_STATE).read_text())
        recorded["runtime_sha256"] = {name: "0" * 64 for name in ve.SMOKE_PINNED}
        (self.state / ve.SMOKE_STATE).write_text(json.dumps(recorded))
        self.assertEqual(ve.sherpa_smoke_passed(self.state, paths=[shipped], library=self.runtime.api), (True, None))

    def test_a_pass_recorded_here_counts_without_a_shipped_verdict_and_a_shipped_fail_is_no_pass(self):
        self.assertFalse(ve.sherpa_smoke_passed(self.state, paths=[self.shipped(passed=False)],
                                                library=self.runtime.api)[0])
        self.run_smoke()
        self.assertEqual(ve.sherpa_smoke_passed(self.state, paths=[self.shipped(passed=False)],
                                                library=self.runtime.api), (True, None))

    def test_a_recorded_pass_keeps_its_moonshine_speed_for_this_build(self):
        self.run_smoke({"moonshine": (0, self.runtime.line("moonshine", rtf=0.138))})
        found = ve.cached(self.state, "asr", ve.asr_key("v6-test", "tiny"), now=123.0)
        self.assertEqual(found["rtf"], 0.138)
        # A failed smoke measures nothing.
        other = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ve.hardware_smoke(other, record=True, library=self.runtime.api,
                          runner=lambda stage: (0, self.runtime.line(stage, rtf=0.1)) if stage != "piper"
                          else (1, ""))
        self.assertIsNone(ve.cached(other, "asr", ve.asr_key("v6-test", "tiny")))

    def test_without_record_nothing_is_kept(self):
        report = self.run_smoke(record=False)
        self.assertTrue(report["passed"])
        self.assertFalse((self.state / ve.SMOKE_STATE).exists())

    def test_a_missing_runtime_is_a_fail_without_running_anything(self):
        (self.runtime.folder / "libonnxruntime.so").unlink()
        ran = []
        report = ve.hardware_smoke(self.state, record=True, library=self.runtime.api,
                                   runner=lambda stage: ran.append(stage))
        self.assertFalse(report["passed"])
        self.assertEqual(ran, [])
        self.assertIn("could not be read", report["error"])

    def test_bytes_that_change_during_the_test_are_no_pass(self):
        def runner(stage):
            if stage == "piper":
                (self.runtime.folder / "libonnxruntime.so").write_bytes(b"replaced")
            return 0, self.runtime.line(stage)

        report = ve.hardware_smoke(self.state, library=self.runtime.api, runner=runner)
        self.assertFalse(report["passed"])
        self.assertIn("changed during the test", report["error"])

    def test_recording_needs_the_mind(self):
        with self.assertRaises(ValueError):
            ve.hardware_smoke(None, record=True, library=self.runtime.api, runner=lambda stage: (0, ""))

    def test_the_lines_say_what_happened(self):
        lines = ve.smoke_lines(self.run_smoke({"piper": (SIGILL, "")}, record=False))
        self.assertEqual(lines[:2], ["vad: passed", "moonshine: passed"])
        self.assertTrue(lines[2].startswith("piper: SIGILL"))
        self.assertIn("Moonshine stays off", lines[-1])


class Command(unittest.TestCase):
    def test_smoke_record_runs_and_keeps_the_verdict(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ve, "hardware_smoke", return_value={"passed": True, "stages": []}) as smoke, \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(ve.main(["smoke", "--record", "--mind", folder]), 0)
        smoke.assert_called_once_with(Path(folder), record=True)
        self.assertIn("Moonshine may be chosen automatically", out.getvalue())

    def test_a_failed_smoke_exits_one_and_json_prints_the_report(self):
        with patch.object(ve, "hardware_smoke", return_value={"passed": False, "stages": []}), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(ve.main(["smoke", "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"passed": False, "stages": []})

    def test_a_stage_runs_in_its_own_python_process(self):
        calls = []

        def run(command, **options):
            calls.append((command, options))
            return type("Done", (), {"returncode": 0, "stdout": "SMOKE {}", "stderr": ""})()

        with patch("subprocess.run", run):
            self.assertEqual(ve.run_stage("moonshine", python="/usr/bin/python3"), (0, "SMOKE {}"))
        command, options = calls[0]
        self.assertEqual(command, ["/usr/bin/python3", "-m", "mind.voice_engines", "smoke-stage", "moonshine"])
        self.assertIn(str(Path(ve.__file__).resolve().parent.parent), options["env"]["PYTHONPATH"])
        self.assertEqual(options["timeout"], ve.SMOKE_STAGE_TIMEOUT_S)


class Stage(unittest.TestCase):
    """The child side, with stand-ins for the engines."""

    def run_stage(self, stage, **patches):
        out = io.StringIO()
        with patch("mind.sherpa.version", return_value={"sherpa_onnx": "2.0.3"}), \
                patch("mind.sherpa.read_wav", return_value=([0.0] * 24000, 24000)), \
                patch("mind.sherpa.resample", return_value=[0.0] * 16000), \
                patch("mind.asr_sherpa.test_wav", return_value=Path("/x/0.wav")):
            with patch.multiple("mind.asr_sherpa", **patches) if patches else patch("builtins.id"):
                ve.smoke_stage(stage, out=out)
        line = out.getvalue().strip()
        self.assertTrue(line.startswith(ve.SMOKE_MARK))
        return json.loads(line[len(ve.SMOKE_MARK):])

    def test_vad_and_moonshine(self):
        class Vad:
            def __init__(self):
                self.windows = 0

            def accept(self, samples):
                self.windows += 1

            def pop_segments(self):
                return [(0, [0.0])] if self.windows == 3 else []

            def flush(self):
                pass

            def close(self):
                pass

        class Moonshine:
            def transcribe(self, samples, rate):
                return "Ask not what your country can do for you."

            def close(self):
                pass

        report = self.run_stage("vad", load_vad=lambda root, library: Vad())
        self.assertEqual((report["stage"], report["segments"], report["passed"]), ("vad", 1, True))
        report = self.run_stage("moonshine", load_moonshine=lambda root, size, library: Moonshine())
        self.assertTrue(report["passed"])
        self.assertNotIn("text", report, "the words stay out of the report")

    def test_piper(self):
        class Piper:
            def __init__(self, **options):
                pass

            def synthesize_blocking(self, text, wav, rate):
                return 2.5

            def close(self):
                pass

        with patch("mind.tts_sherpa.PiperEngine", Piper):
            report = self.run_stage("piper")
        self.assertEqual((report["audio_seconds"], report["passed"]), (2.5, True))

    def test_an_unknown_stage_raises(self):
        with patch("mind.sherpa.version", return_value={}), self.assertRaises(ValueError):
            ve.smoke_stage("whisper", out=io.StringIO())


if __name__ == "__main__":
    unittest.main()
