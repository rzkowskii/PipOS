"""lab/try_timings.py reads the voice and turn timing lines and prints medians (task L7.8)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lab import try_timings

JOURNAL = """\
[   30.000000] pipos being-mind[500]: mind: turn timing {"cache_n": 0, "first_clause_s": 4.1, "ok": true, "predicted_n": 14, "predicted_per_second": 9.5, "prompt_n": 240, "prompt_per_second": 22.4, "retries": 0}
[   30.400000] pipos being-voice[300]: voice: voice timing {"eos_to_audio_s": 5.2, "eos_to_final_s": 0.5, "final_to_audio_s": 4.7}
[   60.000000] pipos being-voice[300]: voice: voice timing {"eos_to_audio_s": 0.8, "eos_to_final_s": 0.45, "final_to_audio_s": 0.35}
[  100.000000] pipos being-mind[500]: mind: turn timing {"cache_n": 260, "first_clause_s": 1.9, "ok": true, "predicted_n": 12, "predicted_per_second": 10.5, "prompt_n": 21, "prompt_per_second": 22.6, "retries": 0}
[  100.300000] pipos being-voice[300]: voice: voice timing {"eos_to_audio_s": 2.6, "eos_to_final_s": 0.55, "final_to_audio_s": 2.05, "said": "a secret"}
[  140.000000] pipos being-voice[300]: voice: voice timing {"eos_to_audio_s": 1.0, "eos_to_final_s": 0.5, "final_to_audio_s": 0.5}
[  150.000000] pipos being-voice[300]: voice: voice timing {not json}
[  160.000000] pipos being-mind[500]: mind: turn timing {"cache_n": 300, "first_clause_s": 2.2, "ok": true, "predicted_n": 10, "predicted_per_second": 10.0, "prompt_n": 19, "prompt_per_second": 22.0, "retries": 0}
[  160.200000] pipos being-voice[300]: voice: voice timing {"eos_to_audio_s": 2.8, "eos_to_final_s": 0.6, "final_to_audio_s": 2.2}
"""


class TimingLines(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "PIPLOGS"
        boot = self.root / "boot-0007-20261125-091500Z"
        boot.mkdir(parents=True)
        (boot / "journal.txt").write_text(JOURNAL)
        (boot / "summary.txt").write_text("CPU:            Intel(R) Core(TM) i5-3210M\n")

    def test_lines_are_parsed_as_numbers_only(self):
        journal = try_timings.parse_journal(JOURNAL)
        self.assertEqual(len(journal["timings"]["voice"]), 5, "the malformed line is skipped")
        self.assertEqual(len(journal["timings"]["turn"]), 3)
        self.assertNotIn("said", journal["timings"]["voice"][2])
        self.assertNotIn("secret", json.dumps(journal))

    def test_turns_claim_their_voice_line_and_the_rest_are_code_answers(self):
        groups = try_timings.classify(try_timings.parse_journal(JOURNAL)["timings"])
        self.assertEqual([line["eos_to_audio_s"] for line in groups["model"]], [5.2, 2.6, 2.8])
        self.assertEqual([line["eos_to_audio_s"] for line in groups["warm"]], [2.6, 2.8])
        self.assertEqual([line["eos_to_audio_s"] for line in groups["code"]], [0.8, 1.0])

    def test_medians_and_gates(self):
        summary = try_timings.timing_summary(try_timings.parse_journal(JOURNAL)["timings"])
        self.assertEqual(summary["voice"]["eos_to_final_s"], 0.5)
        self.assertEqual(summary["voice"]["eos_to_audio_s"], 2.6)
        self.assertEqual(summary["turn"]["prompt_n"], 21)
        self.assertEqual(summary["turn"]["cache_n"], 260)
        self.assertEqual(summary["warm_model"], {"count": 2, "median_eos_to_audio_s": 2.7, "gate_s": 3.0,
                                                 "passes": True})
        self.assertEqual(summary["code"], {"count": 2, "median_eos_to_audio_s": 0.9, "gate_s": 1.2, "passes": True})
        empty = try_timings.timing_summary({"voice": [], "turn": []})
        self.assertIsNone(empty["warm_model"]["passes"])

    def test_report_prints_the_medians_and_verdicts(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(try_timings.main([str(self.root)]), 0)
        text = output.getvalue()
        self.assertIn("Timing lines: 5 voice, 3 turn", text)
        self.assertIn("end of speech to audio 2.60 s", text)
        self.assertIn("warm model turns: 2, median end of speech to audio 2.70 s (gate <= 3.0 s: PASS)", text)
        self.assertIn("code answers: 2, median end of speech to audio 0.90 s (gate <= 1.2 s: PASS)", text)
        self.assertNotIn("secret", text)
        output = io.StringIO()
        with redirect_stdout(output):
            try_timings.main([str(self.root), "--json"])
        data = json.loads(output.getvalue())
        self.assertEqual(data["boots"][0]["timing"]["turn_lines"], 3)

    def test_a_code_answer_just_after_a_model_turn_is_not_claimed_by_it(self):
        # The turn line comes when generation ends; its own first audio was 3 s
        # earlier. A code answer heard 4 s later is nearer in time, but after.
        timings = {"voice": [{"t": 97.0, "eos_to_audio_s": 2.5}, {"t": 104.0, "eos_to_audio_s": 0.7}],
                   "turn": [{"t": 100.0, "cache_n": 250, "ok": True}]}
        groups = try_timings.classify(timings)
        self.assertEqual([line["eos_to_audio_s"] for line in groups["model"]], [2.5])
        self.assertEqual([line["eos_to_audio_s"] for line in groups["code"]], [0.7])

    def test_a_failed_turn_is_not_a_warm_model_turn(self):
        timings = {"voice": [{"t": 100.2, "eos_to_audio_s": 6.0}],
                   "turn": [{"t": 100.0, "cache_n": 250, "ok": False}]}
        groups = try_timings.classify(timings)
        self.assertEqual(len(groups["model"]), 1)
        self.assertEqual(groups["warm"], [])
        self.assertEqual(groups["code"], [])


ROUTES = """\
[   40.000000] pipos being-mind[500]: mind: route {"route": "workshop.open", "chars": 24, "text": "hey pip open a terminal"}
[   50.000000] pipos being-mind[500]: mind: route {"route": "model", "chars": 7}
[   60.000000] pipos being-mind[500]: mind: route {"route": "model", "chars": 11}
[   70.000000] pipos being-mind[500]: mind: route {"route": "private"}
[   80.000000] pipos being-mind[500]: mind: route {"route": "Not A Route!", "chars": 3}
[   90.000000] pipos being-mind[500]: mind: route {broken
"""


class RouteLines(unittest.TestCase):
    def test_routes_are_counted_and_the_words_never_read(self):
        journal = try_timings.parse_journal(ROUTES)
        self.assertEqual([r["route"] for r in journal["routes"]], ["workshop.open", "model", "model", "private"])
        self.assertEqual(journal["routes"][0], {"t": 40.0, "route": "workshop.open", "chars": 24})
        self.assertIsNone(journal["routes"][3]["chars"])
        self.assertNotIn("terminal", json.dumps(journal))
        self.assertEqual(try_timings.route_counts(journal["routes"]), {"model": 2, "private": 1, "workshop.open": 1})

    def test_the_report_prints_routes_and_a_cable_boot(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "PIPLOGS"
        boot = root / "boot-0009-20261125-091500Z"
        boot.mkdir(parents=True)
        (boot / "journal.txt").write_text(ROUTES)
        (boot / "hardware-report.txt").write_text("Command line: being.try=1 being.cable=1 being.voice_debug=1\n")
        (boot / "summary.txt").write_text("Cable link:     ON: a cable development boot\n"
                                          "Codec IDs:      hdaudioC0D0 0x10134206 subsystem 0x106b5200 rev 0x100302\n")
        data = try_timings.report(root)
        self.assertTrue(data["boots"][0]["cable"])
        self.assertIn("0x106b5200", data["boots"][0]["summary"]["Codec IDs"])
        text = try_timings.render(data)
        self.assertIn("(default entry, CABLE development boot)", text)
        self.assertIn("Routes (4 turns): model x2, private x1, workshop.open x1", text)
        self.assertNotIn("open a terminal", text)
        self.assertNotIn("open a terminal", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
