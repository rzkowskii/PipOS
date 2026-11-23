"""lab/try_timings.py and lab/bench_report.py against a fixture PIPLOGS folder (no stick, no VM)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lab import bench_report, try_timings

SUMMARY = """PipOS Try boot #3
Written 2026-11-24 09:20:00 UTC, 612 s after power-on

Machine:        Apple Inc. MacBookPro9,2
CPU:            Intel(R) Core(TM) i5-3210M CPU @ 2.50GHz (4 threads)
GPU:            Intel HD Graphics 4000 (Ivy Bridge) driver=i915
GL renderer:    Mesa Intel(R) HD Graphics 4000 (IVB GT2)
Audio:          0 [PCH            ]: HDA-Intel - HDA Intel PCH
Battery:        found BAT0, 81% Charging
Lid switch:     found (Lid Switch), state open
applesmc:       NOT found
Memory:         15934 MiB
Failed units:   none

--- Failures and restarts (first 1) ---
being-voice.service: Failed with result 'exit-code'.
"""

JOURNAL = """\
[    3.100000] pipos kernel: Command line: being.try=1 quiet
[    9.512301] pipos being-face[310]: face: drawn by Mesa Intel(R) HD Graphics 4000 (IVB GT2)
[   10.000100] pipos being-brainstem[300]: brainstem: waking -> idle
[   40.000000] pipos llama-server[400]: 0.00.742.087 I slot load_slot_ch: id  0 | task -1 | restored 6 context checkpoint(s) from '/mind/cortex/previous.slot'
[   41.000000] pipos llama-server[400]: 0.00.746.143 I slot launch_slot_: id  0 | task 1 | processing task, is_child = 0
[   61.000000] pipos llama-server[400]: 0.01.170.880 I slot print_timing: id  0 | task 1 | prompt eval time =   20000.44 ms /   256 tokens (   78.13 ms per token,    12.80 tokens per second)
[   61.000100] pipos llama-server[400]: 0.01.170.882 I slot print_timing: id  0 | task 1 |        eval time =       0.00 ms /     1 tokens (    0.00 ms per token,     0.00 tokens per second)
[   61.000200] pipos llama-server[400]: 0.01.170.900 I slot      release: id  0 | task 1 | stop processing: n_tokens = 256, truncated = 0
[  100.000000] pipos being-brainstem[300]: brainstem: idle -> attending
[  101.000000] pipos being-brainstem[300]: brainstem: attending -> listening
[  103.000000] pipos being-brainstem[300]: brainstem: listening -> thinking
[  103.100000] pipos llama-server[400]: 0.01.193.990 I slot launch_slot_: id  0 | task 24 | processing task, is_child = 0
[  106.000000] pipos llama-server[400]: 0.01.605.008 I slot print_timing: id  0 | task 24 | prompt eval time =    1500.00 ms /    30 tokens (   50.00 ms per token,    20.00 tokens per second)
[  106.000100] pipos llama-server[400]: 0.01.605.011 I slot print_timing: id  0 | task 24 |        eval time =    1800.00 ms /    18 tokens (  100.00 ms per token,    10.00 tokens per second)
[  106.000200] pipos llama-server[400]: 0.01.605.032 I slot      release: id  0 | task 24 | stop processing: n_tokens = 304, truncated = 0
[  104.700000] pipos being-brainstem[300]: brainstem: thinking -> speaking
[  110.000000] pipos being-brainstem[300]: brainstem: speaking -> idle
[  120.000000] pipos being-brainstem[300]: brainstem: listening -> thinking
[  240.000000] pipos being-brainstem[300]: brainstem: thinking -> idle
[  241.000000] pipos systemd[1]: being-voice.service: Scheduled restart job, restart counter is at 3.
[  241.500000] pipos systemd[1]: being-voice.service: Failed with result 'exit-code'.
"""

SERVICES = """\
  UNIT                     LOAD   ACTIVE SUB     DESCRIPTION
  being-face.service       loaded active running Being: face
● being-voice.service      loaded failed failed  Being: voice
"""

BENCH = {
    "kind": "pipos-bench-v1", "started": "2026-11-24T09:00:00Z", "finished": "2026-11-24T09:30:00Z",
    "system": {"cpu": {"model": "Intel(R) Core(TM) i5-3210M CPU @ 2.50GHz", "threads": 4, "avx2": False},
               "memory_mib": 15934, "power": {"scaling_driver": "intel_cpufreq", "governor": "schedutil",
                                              "no_turbo": "0"}},
    "models": [{"name": "Qwen3.5-0.8B", "setting": {"threads": 2, "threads_batch": 2}, "load_s": 4.2,
                "prefill": [{"tokens": 16, "prompt_tps": 20.0}, {"tokens": 512, "prompt_tps": 25.5}],
                "decode": {"predicted_tps": 11.0},
                "turns": [{"turn": 1, "first_token_s": 9.0, "prompt_n": 80, "cache_n": 0},
                          {"turn": 2, "first_token_s": 3.0, "prompt_n": 20, "cache_n": 90},
                          {"turn": 3, "first_token_s": 2.0, "prompt_n": 22, "cache_n": 120}],
                "slot": {"save_s": 0.4, "restore_s": 0.3, "cache_n_after_restore": 140},
                "cancel_to_release_s": 1.7,
                "soak": [{"cap": "turbo", "tokens_per_s": 10.5, "watts": 24.0, "tokens_per_joule": 0.44,
                          "max_package_c": 88}]}],
}


class TryTimings(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "PIPLOGS"
        self.boot = self.root / "boot-0003-20261124-091000Z"
        self.boot.mkdir(parents=True)
        (self.boot / "summary.txt").write_text(SUMMARY.replace("\n", "\r\n"))
        (self.boot / "journal.txt").write_text(JOURNAL)
        (self.boot / "services.txt").write_text(SERVICES)
        (self.boot / "bench-20261124-090000Z.json").write_text(json.dumps(BENCH))
        (self.root / "boot-0001-20261123-100000Z").mkdir()
        (self.root / "boots.txt").write_text("boot 1 started\nboot 1 ended cleanly\nboot 3 started\n")

    def test_reads_summary_states_requests_and_turns(self):
        data = try_timings.report(self.root)
        self.assertEqual([b["folder"] for b in data["boots"]],
                         ["boot-0001-20261123-100000Z", "boot-0003-20261124-091000Z"])
        self.assertEqual(data["boots_txt"]["ended_cleanly"], 1)
        boot = data["boots"][1]
        self.assertIn("i5-3210M", boot["summary"]["CPU"])
        self.assertEqual(boot["renderer"]["by"], "Mesa Intel(R) HD Graphics 4000 (IVB GT2)")
        self.assertEqual(boot["checkpoints_restored"], 6)
        self.assertEqual(boot["restarts"], {"being-voice.service": 3})
        self.assertEqual(boot["failed_services"], ["being-voice.service"])
        warm, turn = boot["requests"]
        self.assertEqual((warm["prompt_tokens"], warm["cached_tokens"]), (256, 0))
        self.assertEqual((turn["prompt_tokens"], turn["decode_tokens"], turn["cached_tokens"]), (30, 18, 257))
        self.assertEqual((turn["prompt_tps"], turn["decode_tps"]), (20.0, 10.0))
        self.assertEqual(boot["turns"][0]["seconds"], 1.7)
        self.assertIsNone(boot["turns"][1]["seconds"])
        self.assertEqual(boot["turns"][1]["ended_in"], "idle")
        self.assertEqual(boot["bench"][0]["models"][0]["cache_n_after_restore"], 140)
        self.assertEqual(data["boots"][0]["missing"], ["summary.txt", "journal.txt"])

    def test_text_report_names_the_answers_and_no_transcript(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(try_timings.main([str(self.root)]), 0)
        text = out.getvalue()
        for phrase in ("i5-3210M", "IVB GT2", "being-voice.service x3", "listening -> thinking",
                       "1.70 s", "no speech (went idle)", "12.8", "6 context checkpoints",
                       "median first token 3.00 s", "cancel to release: 1.70 s", "soak turbo"):
            self.assertIn(phrase, text)
        self.assertNotIn("SOFTWARE", text)

    def test_cached_tokens_match_real_llama_server_lines(self):
        # Lines as the pinned llama-server prints them (out/candidates/.../private-server.log):
        # a cold 224-token prompt that decoded 10 releases n_tokens = 233.
        lines = [
            "1.27.044.619 I slot print_timing: id  0 | task 2 | prompt eval time =     721.40 ms /   224 tokens "
            "(    3.22 ms per token,   310.51 tokens per second)",
            "1.27.044.622 I slot print_timing: id  0 | task 2 |        eval time =      92.02 ms /    10 tokens "
            "(   10.22 ms per token,    97.80 tokens per second)",
            "1.27.044.637 I slot      release: id  0 | task 2 | stop processing: n_tokens = 233, truncated = 0",
            "1.27.564.748 I slot print_timing: id  0 | task 15 | prompt eval time =     182.52 ms /    49 tokens "
            "(    3.72 ms per token,   268.47 tokens per second)",
            "1.27.564.750 I slot print_timing: id  0 | task 15 |        eval time =     322.85 ms /    32 tokens "
            "(   10.41 ms per token,    96.02 tokens per second)",
            "1.27.564.769 I slot      release: id  0 | task 15 | stop processing: n_tokens = 300, truncated = 0",
        ]
        journal = "".join(f"[  {50 + n:3}.000000] pipos llama-server[400]: {line}\n" for n, line in enumerate(lines))
        cold, warm = try_timings.parse_journal(journal)["requests"]
        self.assertEqual((cold["prompt_tokens"], cold["decode_tokens"], cold["cached_tokens"]), (224, 10, 0))
        self.assertEqual((warm["prompt_tokens"], warm["cached_tokens"]), (49, 220))
        self.assertEqual(warm["decode_tps"], 96.02)

    def test_one_boot_folder_and_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(try_timings.main([str(self.boot), "--json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(len(data["boots"]), 1)

    def test_software_renderer_is_flagged_and_empty_folder_fails(self):
        (self.boot / "journal.txt").write_text(
            "[    9.5] pipos being-face[310]: face: drawn by llvmpipe (LLVM 19.1.7, 256 bits)\n")
        self.assertIn("SOFTWARE", try_timings.render(try_timings.report(self.boot)))
        empty = self.root.parent / "empty"
        empty.mkdir()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(try_timings.main([str(empty)]), 1)


class BenchReport(unittest.TestCase):
    def test_unfinished_and_unrestored_runs_are_called_out(self):
        data = dict(BENCH, finished=None, restore_errors=["/sys/x: denied"])
        lines = "\n".join(bench_report.summarise(data)["lines"])
        self.assertIn("UNFINISHED", lines)
        self.assertIn("NOT RESTORED", lines)

    def test_rejects_other_json(self):
        self.assertIn("error", bench_report.summarise({"kind": "other"}))


if __name__ == "__main__":
    unittest.main()
