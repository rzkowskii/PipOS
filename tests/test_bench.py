"""Bench mode (mind/bench.py, being-bench.service, the bench stick) against a fake llama-server (task L7.6)."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from lab import bench_report, quant_candidates
from lab.tests.host.test_bench_guest import FAKE_SERVER, free_port
from mind import bench

REPO = Path(__file__).resolve().parent.parent


class FullRun(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.server = self.root / "llama-server"
        self.server.write_text(f"#!{sys.executable}\n" + FAKE_SERVER)
        self.server.chmod(0o755)
        self.models = self.root / "models/bench"
        self.models.mkdir(parents=True)
        (self.models / "q4_0.gguf").write_bytes(b"GGUF q4_0")
        (self.models / "manifest.json").write_text(json.dumps({"models": [{"file": "q4_0.gguf", "name": "Q4_0"}]}))
        self.dream = self.root / "models/dream/dream.gguf"
        self.dream.parent.mkdir(parents=True)
        self.dream.write_bytes(b"GGUF dream")
        self.out = self.root / "PIPLOGS-boot"
        self.out.mkdir()

    def run_bench(self, *extra):
        argv = ["--output", str(self.out), "--models-dir", str(self.models), "--server", str(self.server),
                "--port", str(free_port()), "--scratch", str(self.root), "--dream-model", str(self.dream), *extra]
        with patch.object(bench.signal, "signal"), patch.object(bench.atexit, "register"), patch("builtins.print"):
            self.assertEqual(bench.main(argv), 0)
        (found,) = self.out.glob("bench-*.json")
        return json.loads(found.read_text())

    def test_everything_the_spec_asks_for_is_measured(self):
        voice = types.ModuleType("mind.voice_bench")
        calls = []

        def run(asr_root, voice_dir, busy, cortex):
            calls.append((asr_root, voice_dir, busy, cortex))
            return {"vad": {"idle": {"ms_per_window": 0.4}, "busy": {"ms_per_window": 0.6}},
                    "moonshine_tiny": {"idle": {"rtf": 0.2}, "busy": {"rtf": 0.3}},
                    "piper": {"idle": {"rtf": 0.3}, "busy": {"rtf": 0.45}},
                    "busy": {"decode_slowdown_pct": 9.5}, "verdict": {"piper_auto": True}, "errors": {}}

        voice.run = run
        with patch.dict(sys.modules, {"mind.voice_bench": voice}):
            data = self.run_bench("--voice-models", str(self.root / "models"))
        self.assertTrue(data["finished"])
        (model,) = data["models"]
        self.assertNotIn("error", model, model.get("error"))
        self.assertEqual(model["setting"], bench.PRODUCTION)
        self.assertEqual([row["tokens"] for row in model["prefill"]], [16, 32, 128, 512])
        self.assertEqual(model["decode"]["predicted_n"], 64)
        self.assertEqual(len(model["turns"]), 6)
        self.assertIn("cache_n_after_restore", model["slot"])
        self.assertIsNotNone(model["cancel_to_release_s"])
        self.assertEqual(set(model["slot_isolation"]), {"cache_n_before", "cache_n_after", "prompt_n_after", "kept"})
        sweep = [(row["setting"]["threads"], row["setting"]["threads_batch"]) for row in model["sweep"]]
        self.assertEqual(sweep, [(2, 2), (2, 4), (3, 2), (3, 4), (4, 2), (4, 4)])
        self.assertTrue(all("error" not in row and row["decode"]["predicted_n"] == 64 for row in model["sweep"]))
        self.assertEqual([row["tokens"] for row in data["dream"]["prefill"]], [128, 512])
        self.assertEqual(data["dream"]["setting"]["ctx_size"], 2048)
        (call,) = calls
        self.assertEqual(call[0], self.root / "models/asr")
        self.assertEqual(call[1], self.root / "models/voice/piper/default")
        self.assertEqual(call[2], "cortex")
        self.assertTrue(call[3].startswith("http://127.0.0.1:"), "beside the bench's own model server")
        self.assertEqual(model["voice"]["engine"], "voice_bench")
        lines = "\n".join(bench_report.summarise(data)["lines"])
        for expected in ("threads 4/4: prefill", "slot 1 code job: slot 0 cache_n", "dream dream.gguf: prefill",
                         "voice (voice_bench, model busy): moonshine_tiny idle 0.20/busy 0.30",
                         "decode slowed 9.5%", "verdict piper_auto=pass"):
            self.assertIn(expected, lines)
        self.assertFalse(list(self.root.glob("being-bench-slots-*")), "slot scratch left behind")

    def test_production_server_shape_and_dream_shape(self):
        command = bench.server_command("/models/m.gguf", dict(bench.PRODUCTION), 8091, "/tmp/s")
        self.assertEqual(command[command.index("--ctx-size") + 1], "8192")
        self.assertEqual(command[command.index("--parallel") + 1], "2")
        dream = bench.server_command("/models/d.gguf", {**bench.PRODUCTION, **bench.DREAM_SERVER}, 8091, "/tmp/s")
        self.assertEqual((dream[dream.index("--ctx-size") + 1], dream[dream.index("--parallel") + 1]), ("2048", "1"))
        env = bench.server_env({})
        self.assertEqual((env["LLAMA_ARG_BATCH"], env["LLAMA_ARG_UBATCH"]), ("512", "512"))
        self.assertNotIn("LLAMA_ARG_MLOCK", env)
        self.assertEqual(bench.PREFILL_SIZES, (16, 32, 128, 512))

    def test_voice_falls_back_to_flite_and_pocketsphinx_without_voice_bench(self):
        with patch.dict(sys.modules, {"mind.voice_bench": None}), \
                patch.object(bench, "speech_rtf", return_value={"tts": {"rtf": 0.05}}):
            result = bench.voice_rtf()
        self.assertEqual(result["engine"], "flite+pocketsphinx")
        self.assertIn("not installed", result["note"])
        broken = types.ModuleType("mind.voice_bench")
        broken.run = lambda **options: 1 / 0
        with patch.dict(sys.modules, {"mind.voice_bench": broken}), \
                patch.object(bench, "speech_rtf", return_value={}):
            self.assertIn("ZeroDivisionError", bench.voice_rtf(cortex_url="http://127.0.0.1:1")["note"])

    def test_the_real_voice_bench_has_the_run_this_calls(self):
        import inspect
        try:
            from mind import voice_bench
        except ImportError:
            self.skipTest("mind/voice_bench.py has not landed")
        self.assertTrue({"asr_root", "voice_dir", "busy", "cortex"} <= set(inspect.signature(voice_bench.run).parameters))

    def test_a_bench_stick_records_its_pack_check(self):
        self.assertIsNone(bench.bench_pack(self.models))
        (self.models / "beingos-bench.cms").write_bytes(b"not a signature")
        result = bench.bench_pack(self.models)
        self.assertFalse(result["ok"])

    def test_a_bench_pack_that_does_not_verify_keeps_its_models_from_the_server(self):
        (self.models / "beingos-bench.cms").write_bytes(b"not a signature")
        fallback = self.root / "models/reflex.gguf"
        fallback.write_bytes(b"GGUF reflex")
        data = self.run_bench("--quick", "--no-speech", "--fallback-model", str(fallback))
        self.assertFalse(data["bench_pack"]["ok"])
        self.assertIn("bench_models_skipped", data)
        self.assertEqual([model["file"] for model in data["models"]], ["reflex.gguf"])

    def test_a_model_that_differs_from_its_manifest_is_not_run(self):
        (self.models / "manifest.json").write_text(json.dumps(
            {"models": [{"file": "q4_0.gguf", "name": "Q4_0", "sha256": "0" * 64}]}))
        data = self.run_bench("--quick", "--no-speech")
        (model,) = data["models"]
        self.assertFalse(model["sha256_ok"])
        self.assertIn("skipped", model["error"])
        self.assertNotIn("prefill", model)

    def test_the_lab_name_is_the_same_module(self):
        from lab import bench_guest
        self.assertIs(bench_guest, bench)


class SlotClient:
    """A client whose server keeps one cache per slot, or one shared cache."""

    def __init__(self, shared=False):
        self.shared, self.cache, self.saved = shared, {0: [], 1: []}, {}

    def tokenize(self, text):
        return list(range(len(text.split())))

    def _run(self, prompt, n_predict, cache, slot):
        tokens = list(prompt) if isinstance(prompt, list) else prompt.split()
        key = 0 if self.shared else slot
        old, hit = self.cache[key], 0
        while cache and hit < min(len(old), len(tokens)) and old[hit] == tokens[hit]:
            hit += 1
        self.cache[key] = tokens
        return {"timings": {"prompt_n": len(tokens) - hit, "cache_n": hit, "predicted_n": n_predict,
                            "prompt_per_second": 20.0, "predicted_per_second": 10.0}}

    def complete(self, prompt, n_predict, *, cache=True, seed=1, slot=0):
        return self._run(prompt, n_predict, cache, slot)

    def stream(self, prompt, n_predict, *, stop_after=None):
        import time
        timings = self._run(prompt, n_predict, True, 0)["timings"]
        return 0.01, timings, time.monotonic() if stop_after else None

    def slot(self, action, filename):
        if action == "save":
            self.saved[filename] = list(self.cache[0])
            return {"n_saved": len(self.cache[0])}
        self.cache[0] = list(self.saved[filename])
        return {"n_restored": len(self.cache[0])}


class SlotIsolationProbe(unittest.TestCase):
    def probe(self, shared):
        with tempfile.TemporaryDirectory() as slots:
            return bench.bench_model(SlotClient(shared), slots, sizes=(16,), decode=4)

    def test_separate_slots_keep_the_conversation_cache(self):
        result = self.probe(shared=False)
        isolation = result["slot_isolation"]
        self.assertGreater(isolation["cache_n_before"], 0)
        self.assertEqual(isolation["cache_n_after"], isolation["cache_n_before"])
        self.assertTrue(isolation["kept"])
        self.assertEqual(result["slot"]["cache_n_after_restore"], isolation["cache_n_after"])

    def test_a_code_job_that_evicts_slot_0_is_caught(self):
        isolation = self.probe(shared=True)["slot_isolation"]
        self.assertEqual(isolation["cache_n_after"], 0)
        self.assertFalse(isolation["kept"])


class Packaging(unittest.TestCase):
    def test_service_is_a_oneshot_behind_the_kernel_switch(self):
        unit = (REPO / "os/package/being/being-bench.service").read_text()
        lines = {line.partition("=")[0]: line.partition("=")[2] for line in unit.splitlines() if "=" in line
                 and not line.startswith("#")}
        self.assertEqual(lines["ConditionKernelCommandLine"], "being.bench=1")
        self.assertEqual(lines["Type"], "oneshot")
        self.assertEqual(lines["ExecStart"], "/usr/bin/python3 -m mind.bench")
        self.assertIn("PYTHONPATH=/usr/lib/being", lines["Environment"])
        self.assertIn("stop being-cortex.service", lines["ExecStartPre"])
        self.assertIn("start being-cortex.service", lines["ExecStopPost"])
        self.assertIn("being-usb-logs.service", lines["After"])
        self.assertEqual(bench.PIPLOGS_STATE, Path("/run/being-usb-logs/folder"))

    def test_bench_stick_boots_every_entry_into_the_bench_with_the_bench_pack(self):
        script = (REPO / "os/board/beingos/try-image.sh").read_text()
        self.assertIn("1) bench_option=being.bench=1 ;;", script)
        self.assertIn('-e "s/TRY_SCREEN/$try_options/g"', script)
        self.assertIn("python3 -m lab.package_model", script)
        self.assertIn("--bench", script)
        post = (REPO / "os/board/beingos/post-image.sh").read_text()
        self.assertIn("BEING_TRY_BENCH", post)

    def test_try_options_join_screen_and_bench(self):
        import subprocess
        snippet = ('try_options="$screen_option${screen_option:+${bench_option:+ }}$bench_option"; '
                   'printf "[%s]" "$try_options"')
        for screen, bench_option, expected in (("", "", "[]"), ("being.screen=1", "", "[being.screen=1]"),
                                               ("", "being.bench=1", "[being.bench=1]"),
                                               ("being.screen=1", "being.bench=1", "[being.screen=1 being.bench=1]")):
            result = subprocess.run(["sh", "-c", f"screen_option='{screen}'; bench_option='{bench_option}'; {snippet}"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, expected)


def gguf(path, file_type):
    """A minimal GGUF header holding general.file_type (uint32)."""
    import struct
    key = b"general.file_type"
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", len(key)) + key
                     + struct.pack("<II", 4, file_type))


FAKE_CONVERT = """import sys, struct
out = sys.argv[sys.argv.index("--outfile") + 1]
key = b"general.file_type"
open(out, "wb").write(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", len(key)) + key
                      + struct.pack("<II", 4, int(open(sys.argv[1] + "/WIDTH").read())))
"""
FAKE_QUANTIZE = """#!{python}
import sys, struct, json
args = [a for a in sys.argv[1:] if not a.startswith("--")]
if "--imatrix" in sys.argv:
    args.remove(sys.argv[sys.argv.index("--imatrix") + 1])
src, out, kind = args[:3]
codes = {{"Q4_0": 2, "Q8_0": 7, "IQ4_NL": 25}}
key = b"general.file_type"
open(out, "wb").write(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", len(key)) + key
                      + struct.pack("<II", 4, codes[kind]) + kind.encode())
open(out + ".argv", "w").write(json.dumps(sys.argv))
"""


class QuantCandidates(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.hf = self.root / "hf"
        self.hf.mkdir()
        (self.hf / "config.json").write_text("{}")
        (self.hf / "model.safetensors").write_bytes(b"weights")
        (self.hf / "LICENSE").write_text("Apache")
        (self.hf / "WIDTH").write_text("32")
        self.llama = self.root / "llama.cpp"
        (self.llama / "build/bin").mkdir(parents=True)
        (self.llama / "convert_hf_to_gguf.py").write_text(FAKE_CONVERT)
        quantize = self.llama / "build/bin/llama-quantize"
        quantize.write_text(FAKE_QUANTIZE.format(python=sys.executable))
        quantize.chmod(0o755)
        self.bench = self.root / "bench"

    def build(self, model="qwen3.5-0.8b", **options):
        return quant_candidates.build(model, self.hf, self.llama, bench=self.bench, work=self.root / "work", **options)

    def test_candidates_are_the_spec_set(self):
        self.assertEqual([(entry["model"], entry["type"]) for entry in quant_candidates.CANDIDATES],
                         [("qwen3.5-0.8b", "Q4_0"), ("qwen3.5-0.8b", "IQ4_NL"), ("qwen3.5-0.8b", "Q8_0"),
                          ("qwen3-1.7b", "Q4_0")])
        self.assertEqual(quant_candidates.SOURCES["qwen3.5-0.8b"]["revision"],
                         json.loads((REPO / "os/package/being/default-model/pack.json").read_text())
                         ["model_source"].rsplit("/", 1)[1])

    def test_build_converts_once_then_quantizes_each_type_and_records_hashes(self):
        entries = self.build(imatrix=self.root / "imatrix.dat")
        self.assertEqual([entry["quantization"] for entry in entries], ["Q4_0", "IQ4_NL", "Q8_0"])
        manifest = json.loads((self.bench / "manifest.json").read_text())
        self.assertEqual([entry["file"] for entry in manifest["models"]],
                         ["qwen3.5-0.8b-q4_0.gguf", "qwen3.5-0.8b-iq4_nl.gguf", "qwen3.5-0.8b-q8_0.gguf"])
        for entry in manifest["models"]:
            self.assertEqual(entry["sha256"], quant_candidates.sha256(self.bench / entry["file"]))
            self.assertEqual(entry["from"]["intermediate"], "BF16")
            self.assertEqual(entry["license"], "Apache-2.0")
            self.assertTrue(entry["license_sha256"])
            self.assertNotIn("settings", entry, "bench gives each candidate the full run and the sweep")
        argv = json.loads((self.bench / ".qwen3.5-0.8b-iq4_nl.gguf.part.argv").read_text())
        self.assertIn("--imatrix", argv)
        self.assertNotIn("--allow-requantize", argv)
        self.assertTrue(argv[-2].endswith("qwen3.5-0.8b-iq4_nl.gguf.part"))
        self.assertTrue(argv[-3].endswith("qwen3.5-0.8b-bf16.gguf"))
        # The bench reads exactly this manifest.
        models = bench.load_models(self.bench, None)
        self.assertEqual([model["name"] for model in models],
                         ["Qwen3.5-0.8B Q4_0", "Qwen3.5-0.8B IQ4_NL", "Qwen3.5-0.8B Q8_0"])
        self.assertTrue(all(model["sweep"] for model in models))
        # A second model adds to the manifest.
        self.build("qwen3-1.7b")
        self.assertEqual(len(json.loads((self.bench / "manifest.json").read_text())["models"]), 4)

    def test_a_quantized_input_is_refused(self):
        (self.hf / "WIDTH").write_text("15")
        with self.assertRaisesRegex(ValueError, "never from a quantized file"):
            self.build()
        self.assertFalse(self.bench.joinpath("qwen3.5-0.8b-q4_0.gguf").exists())
        shipped = REPO / "out/models/reflex.gguf"
        if shipped.is_file():
            with self.assertRaisesRegex(ValueError, "Q4_K_M"):
                quant_candidates.require_unquantized(shipped)
        q = self.root / "q.gguf"
        gguf(q, 2)
        with self.assertRaisesRegex(ValueError, "Q4_0"):
            quant_candidates.require_unquantized(q)
        gguf(q, 1)
        self.assertEqual(quant_candidates.require_unquantized(q), "F16")

    def test_original_weights_are_required_and_fetch_records_are_checked(self):
        (self.hf / "model.safetensors").unlink()
        with self.assertRaisesRegex(ValueError, "original weights"):
            self.build()
        (self.hf / "model.safetensors").write_bytes(b"weights")
        (self.hf / "SOURCE.json").write_text(json.dumps({"repo": "Qwen/Qwen3.5-0.8B", "revision": "abc", "files": [
            {"name": "model.safetensors", "bytes": 7, "sha256": "0" * 64}]}))
        with self.assertRaisesRegex(ValueError, "changed since it was fetched"):
            self.build()

    def test_fetch_takes_only_conversion_inputs_at_the_pinned_commit_and_checks_hashes(self):
        import io
        revision = quant_candidates.SOURCES["qwen3.5-0.8b"]["revision"]
        blobs = {"model.safetensors": b"weights", "config.json": b"{}", "LICENSE": b"Apache",
                 "image.png": b"no", "sub/x.safetensors": b"no"}
        requested = []

        def opener(request, timeout):
            url = request.full_url
            requested.append(url)
            if "/api/models/" in url:
                self.assertIn(f"/revision/{revision}?blobs=true", url)
                body = {"sha": revision, "siblings": [
                    {"rfilename": name, **({"lfs": {"sha256": quant_candidates.hashlib.sha256(data).hexdigest()}}
                                           if name.endswith(".safetensors") else {})}
                    for name, data in blobs.items()]}
                return io.BytesIO(json.dumps(body).encode())
            self.assertIn(f"/resolve/{revision}/", url)
            return io.BytesIO(blobs[url.rsplit("/", 1)[1]])

        record = quant_candidates.fetch("qwen3.5-0.8b", self.root / "fetched", opener=opener)
        self.assertEqual(sorted(entry["name"] for entry in record["files"]),
                         ["LICENSE", "config.json", "model.safetensors"])
        self.assertEqual(record["revision"], revision)
        blobs["model.safetensors"] = b"tampered"
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            def lying(request, timeout):
                if "/api/models/" in request.full_url:
                    body = {"sha": revision, "siblings": [{"rfilename": "model.safetensors",
                                                           "lfs": {"sha256": "0" * 64}}]}
                    return io.BytesIO(json.dumps(body).encode())
                return io.BytesIO(b"weights")
            quant_candidates.fetch("qwen3.5-0.8b", self.root / "fetched2", opener=lying)


if __name__ == "__main__":
    unittest.main()
