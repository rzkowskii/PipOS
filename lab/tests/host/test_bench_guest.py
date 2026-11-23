"""Bench mode (lab/bench_guest.py) against a fake llama-server, on the Mac. No model, no root."""

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from lab import bench_guest, bench_report

FAKE_SERVER = textwrap.dedent('''
    import json, sys, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    port = int(sys.argv[sys.argv.index("--port") + 1])
    slots = sys.argv[sys.argv.index("--slot-save-path") + 1]
    cached = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self.reply({"status": "ok"})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            if self.path == "/tokenize":
                return self.reply({"tokens": list(range(len(body["content"].split())))})
            if self.path.startswith("/slots/0"):
                if "save" in self.path:
                    open(slots + "/" + body["filename"], "w").write("slot")
                    return self.reply({"n_saved": cached["n"]})
                return self.reply({"n_restored": cached["n"]})
            prompt = body["prompt"]
            n = len(prompt) if isinstance(prompt, list) else len(prompt.split())
            hit = min(cached["n"], n) if body.get("cache_prompt") else 0
            cached["n"] = n + body["n_predict"]
            timings = {"prompt_n": n - hit, "cache_n": hit, "prompt_ms": 10.0, "prompt_per_second": 25.0,
                       "predicted_n": body["n_predict"], "predicted_per_second": 12.0}
            if not body.get("stream"):
                return self.reply({"content": "x", "timings": timings})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for i in range(min(body["n_predict"], 8)):
                    self.wfile.write(b"data: " + json.dumps({"content": "w"}).encode() + b"\\n\\n")
                    self.wfile.flush()
                    time.sleep(0.005)
                self.wfile.write(b"data: " + json.dumps({"content": "", "stop": True, "timings": timings}).encode()
                                 + b"\\n\\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
''')


BENCH_MINIMAL = {"kind": "pipos-bench-v1", "finished": "2026-11-24T09:30:00Z", "system": {}, "models": []}


def free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BenchGuest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.server = self.root / "llama-server"
        self.server.write_text(f"#!{sys.executable}\n" + FAKE_SERVER)
        self.server.chmod(0o755)
        self.models = self.root / "models"
        self.models.mkdir()
        (self.models / "tiny.gguf").write_bytes(b"GGUF")
        (self.models / "manifest.json").write_text(json.dumps({"models": [
            {"file": "tiny.gguf", "name": "Tiny", "settings": [{"threads": 2, "threads_batch": 2},
                                                               {"threads": 1, "threads_batch": 2, "mlock": True}]}]}))
        self.out = self.root / "PIPLOGS-boot"
        self.out.mkdir()
        # These tests measure; signing is tests/test_bench.py's. Stand in for a
        # bench pack that verified, with its signed digests.
        digests = {name: bench_guest.sha256(self.models / name) for name in ("tiny.gguf", "manifest.json")}
        self.enterContext(patch.object(bench_guest, "bench_pack", lambda models_dir: {
            "ok": True, "files": 2, "bound": False, "digests": dict(digests)}))

    def run_bench(self, *extra):
        argv = ["--output", str(self.out), "--models-dir", str(self.models), "--server", str(self.server),
                "--port", str(free_port()), "--scratch", str(self.root), "--no-speech", "--quick", *extra]
        with patch.object(bench_guest.signal, "signal"), patch.object(bench_guest.atexit, "register"), \
                patch("builtins.print"):
            self.assertEqual(bench_guest.main(argv), 0)
        files = list(self.out.glob("bench-*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_full_run_writes_one_finished_report_per_setting(self):
        data = self.run_bench()
        self.assertEqual(data["kind"], "pipos-bench-v1")
        self.assertTrue(data["finished"])
        self.assertEqual(len(data["models"]), 2)
        model = data["models"][0]
        self.assertNotIn("error", model, model.get("error"))
        self.assertEqual([row["tokens"] for row in model["prefill"]], [16, 128])
        self.assertTrue(all(row["cache_n"] == 0 for row in model["prefill"]), "prefill must be cold")
        self.assertEqual(model["decode"]["predicted_n"], 64)
        self.assertEqual(len(model["turns"]), 6)
        self.assertGreater(model["turns"][2]["cache_n"], 0)
        self.assertIsNotNone(model["turns"][0]["first_token_s"])
        self.assertIn("cache_n_after_restore", model["slot"])
        self.assertIsNotNone(model["cancel_to_release_s"])
        self.assertFalse(list(self.root.glob("being-bench-slots-*")), "slot scratch left behind")
        summary = bench_report.summarise(data)
        self.assertEqual(len(summary["models"]), 2)
        self.assertIn("Tiny t1/2 mlock", "\n".join(summary["lines"]))

    def test_production_flags(self):
        command = bench_guest.server_command("/models/m.gguf", {"threads": 2}, 8091, "/tmp/s")
        for flag in ("--ctx-size", "8192", "--parallel", "--cache-ram", "--no-cache-idle-slots",
                     "--reasoning-budget", "--no-webui", "127.0.0.1"):
            self.assertIn(flag, command)
        env = bench_guest.server_env({"mlock": True})
        self.assertEqual((env["LLAMA_ARG_BATCH"], env["LLAMA_ARG_UBATCH"], env["LLAMA_ARG_CTX_CHECKPOINTS"]),
                         ("512", "512", "6"))
        self.assertEqual(env["LLAMA_ARG_MLOCK"], "1")

    def test_a_failing_model_is_recorded_and_the_run_still_finishes(self):
        self.server.write_text("#!/bin/sh\nexit 3\n")
        data = self.run_bench()
        self.assertTrue(data["finished"])
        self.assertIn("exited with 3", data["models"][0]["error"])

    def test_manifest_cannot_point_outside_its_folder(self):
        (self.models / "manifest.json").write_text(json.dumps({"models": [{"file": "../x.gguf"}]}))
        with self.assertRaises(ValueError):
            bench_guest.load_models(self.models, None)

    def test_cmdline_options(self):
        self.assertEqual(bench_guest.cmdline_options("quiet being.bench=1 being.bench.soak=30 being.bench.quick=1"),
                         {"soak_minutes": 30, "quick": True})


class PowerCapsRestore(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cpu, self.pstate = root / "cpu", root / "intel_pstate"
        self.pstate.mkdir()
        (self.pstate / "no_turbo").write_text("0")
        for n in (0, 1):
            policy = self.cpu / f"cpu{n}/cpufreq"
            policy.mkdir(parents=True)
            (policy / "cpuinfo_max_freq").write_text("3100000")
            (policy / "scaling_max_freq").write_text("3100000")

    def values(self):
        return ((self.pstate / "no_turbo").read_text(),
                [(self.cpu / f"cpu{n}/cpufreq/scaling_max_freq").read_text() for n in (0, 1)])

    def test_caps_apply_then_restore_exactly(self):
        caps = bench_guest.PowerCaps(self.cpu, self.pstate)
        caps.set(1800000)
        self.assertEqual(self.values(), ("1", ["1800000", "1800000"]))
        caps.set(None)
        self.assertEqual(self.values(), ("0", ["3100000", "3100000"]))
        caps.set(2500000)
        self.assertEqual(caps.restore(), [])
        self.assertEqual(self.values(), ("0", ["3100000", "3100000"]))

    def test_soak_restores_and_counts_energy(self):
        caps = bench_guest.PowerCaps(self.cpu, self.pstate)

        class Client:
            def complete(self, prompt, n, cache=True):
                return {"timings": {"predicted_n": n}}

            def tokenize(self, text):
                return [1, 2, 3]

        class Energy:
            readings = iter([1_000_000, 11_000_000] * 3)

            def read(self):
                return next(self.readings)

            def joules(self, start, end):
                return (end - start) / 1e6

        results = bench_guest.soak(Client(), caps, 0.0005, Energy(), temp=lambda: 70.0)
        self.assertEqual([row["cap"] for row in results], ["turbo", "2.5GHz", "1.8GHz"])
        self.assertTrue(all(row["joules"] == 10.0 for row in results))
        self.assertEqual(self.values(), ("0", ["3100000", "3100000"]))

    def test_soak_stops_a_step_when_the_package_runs_hot(self):
        caps = bench_guest.PowerCaps(self.cpu, self.pstate)

        class Client:
            def complete(self, prompt, n, cache=True):
                return {"timings": {"predicted_n": n}}

            def tokenize(self, text):
                return [1]

        class Energy:
            def read(self):
                return None

            def joules(self, start, end):
                return None

        results = bench_guest.soak(Client(), caps, 10, Energy(), temp=lambda: 96.0)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(row["stopped_early"] == "package reached 96 C" for row in results))
        self.assertTrue(all(row["tokens"] == 128 for row in results), "one request per step, then stop")
        self.assertEqual(self.values(), ("0", ["3100000", "3100000"]))
        line = "\n".join(bench_report.summarise(dict(BENCH_MINIMAL, models=[{"name": "m", "soak": results}]))["lines"])
        self.assertIn("stopped early: package reached 96 C", line)

    def test_on_mains_reads_the_charger(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertIsNone(bench_guest.on_mains(root))
        for name, kind, online in (("ADP1", "Mains", "0"), ("BAT0", "Battery", "1")):
            supply = root / "sys/class/power_supply" / name
            supply.mkdir(parents=True)
            (supply / "type").write_text(kind)
            (supply / "online").write_text(online)
        self.assertIs(bench_guest.on_mains(root), False)
        (root / "sys/class/power_supply/ADP1/online").write_text("1")
        self.assertIs(bench_guest.on_mains(root), True)

    def test_rapl_wraps(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "max_energy_range_uj").write_text("1000")
        self.assertEqual(bench_guest.Energy(root).joules(900, 100), 200 / 1e6)


if __name__ == "__main__":
    unittest.main()
