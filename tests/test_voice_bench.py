import io
import json
import tempfile
import threading
import time
import unittest
import wave
from array import array
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mind import asr_sherpa, voice_bench
from mind.tts_sherpa import PiperEngine
from tests.test_sherpa import FakeLibrary
from tests.test_tts_sherpa import voice_dir


def write_wav(path, seconds=2.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(array("h", ((9000 if (i // 40) % 2 else -9000) for i in range(int(seconds * 16000))))
                        .tobytes())


class FakeLoad:
    def __init__(self):
        self.started = self.stopped = 0
        self.samples = [(0, 1.0)]

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def rate_between(self, start, end):
        return None

    def describe(self):
        return {"kind": "fake"}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.asr = self.root / "asr"
        for size in ("tiny", "base"):
            write_wav(self.asr / f"moonshine-{size}" / "test_wavs/0.wav")
        self.voice = voice_dir(self.root / "voice")
        self.fake = FakeLibrary(transcript="after early sunset", tts_seconds_per_word=0.2)

    def run_bench(self, **options):
        defaults = dict(
            asr_root=self.asr, busy="cpu", runs=3, library=self.fake,
            vad_factory=lambda: asr_sherpa.load_vad(self.asr, self.fake),
            asr_factory=lambda size: asr_sherpa.load_moonshine(self.asr, size, self.fake),
            tts_factory=lambda: PiperEngine(self.voice, library=self.fake),
        )
        defaults.update(options)
        return voice_bench.run(**defaults)

    def test_idle_and_busy_report_is_json_with_every_part(self):
        load = FakeLoad()
        report = self.run_bench(load_factory=lambda: load)
        json.dumps(report)
        self.assertEqual((load.started, load.stopped), (1, 1))
        self.assertEqual(report["library"]["onnxruntime"], "1.28.2")
        for part in ("vad", "moonshine_tiny", "moonshine_base", "piper"):
            self.assertEqual(set(report[part]), {"idle", "busy"}, part)
        self.assertEqual(len(report["piper"]["idle"]["runs"]), 3)
        self.assertEqual(report["moonshine_tiny"]["idle"]["text"], "after early sunset")
        self.assertGreater(report["vad"]["idle"]["windows"], 0)
        self.assertNotIn("window", report["piper"]["busy"])
        self.assertEqual(set(report["verdict"]), {"piper_auto", "piper_busy", "moonshine_tiny_auto",
                                                  "moonshine_base_auto"})
        self.assertEqual(report["errors"], {})
        self.assertEqual(report["busy"], {"kind": "fake"})

    def test_missing_parts_are_reported_not_invented(self):
        (self.asr / "moonshine-base/test_wavs/0.wav").unlink()
        empty = self.root / "novoice"
        empty.mkdir()
        report = self.run_bench(busy="none", tts_factory=lambda: PiperEngine(empty, library=self.fake))
        self.assertNotIn("piper", report)
        self.assertNotIn("moonshine_base", report)
        self.assertIn("not installed", report["errors"]["piper"])
        self.assertIn("no test recording", report["errors"]["moonshine_base"])
        self.assertEqual(set(report["moonshine_tiny"]), {"idle"})

    def test_no_library_reports_why(self):
        report = voice_bench.run(asr_root=self.asr, busy="none", library=self.root / "missing.so")
        self.assertIn("not installed", report["errors"]["library"])
        self.assertNotIn("piper", report)

    def test_cortex_decode_slowdown_beside_piper(self):
        speaking = threading.Event()

        class Speaking(PiperEngine):
            def synthesize_blocking(self, *args):
                speaking.set()
                try:
                    time.sleep(0.05)
                    return super().synthesize_blocking(*args)
                finally:
                    speaking.clear()

        def opener(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(body["n_predict"], voice_bench.CORTEX_TOKENS)
            time.sleep(0.02)
            rate = 9.0 if speaking.is_set() else 10.0
            return FakeResponse(json.dumps({"timings": {"predicted_per_second": rate}}).encode())

        with patch.object(voice_bench, "CORTEX_ALONE_S", 0.3), patch.object(voice_bench, "CORTEX_WARM_S", 1), \
                patch.object(voice_bench, "PIPER_BESIDE_S", 0.3):
            report = self.run_bench(busy="cortex", sizes=("tiny",),
                                    tts_factory=lambda: Speaking(self.voice, library=self.fake),
                                    load_factory=lambda: voice_bench.CortexLoad(opener=opener))
        busy = report["busy"]
        self.assertEqual(busy["kind"], "cortex")
        self.assertEqual(busy["decode_tps_alone"], 10.0)
        self.assertLess(busy["decode_tps_beside_piper"], 10.0)
        self.assertGreater(busy["decode_slowdown_pct"], 0)
        self.assertGreaterEqual(busy["piper_beside_sentences"], 1)
        self.assertIn("decode_slowdown", report["verdict"])

    def test_decode_speed_counts_requests_that_ran_mostly_in_the_window(self):
        load = voice_bench.CortexLoad(opener=lambda *a, **k: None)
        load.samples = [(0.0, 5.0, 10.0), (4.0, 9.0, 8.0), (9.5, 14.0, 7.0), (13.0, 20.0, 1.0)]
        # A request finishing just inside a window it mostly missed is not counted, and vice versa.
        self.assertEqual(load.rate_between(3.0, 14.5), 7.5)
        self.assertIsNone(load.rate_between(30.0, 40.0))

    def test_cli_writes_json(self):
        output = self.root / "out/bench.json"
        with patch.object(voice_bench, "run", return_value={"version": 1}) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(voice_bench.main(["--busy", "none", "--sizes", "tiny", "--output", str(output)]), 0)
        self.assertEqual(json.loads(output.read_text()), {"version": 1})
        self.assertEqual(run.call_args.kwargs["sizes"], ("tiny",))

    def test_cpu_load_spins_and_stops(self):
        load = voice_bench.CpuLoad(workers=1)
        with voice_bench.loaded(load):
            self.assertIsNone(load.processes[0].poll())
        self.assertIsNotNone(load.processes[0].poll())


if __name__ == "__main__":
    unittest.main()
