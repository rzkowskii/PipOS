"""Persona report validation with synthetic rows and no model server."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab import persona_eval, persona_metrics
from lab.persona_probes import PROBES
from mind.profile import PRESETS


def rows():
    return [{"preset": preset, "probe": probe.id, "seed": seed,
             "turns": [{"input": text, "spoken": "I don't know.", "emotion": "neutral", "completed": True,
                        "attempts": [{"raw": "[neutral] I don't know.", "cut_off": False}]} for text in probe.turns],
             "judge": {"invented_memory": False, "grounding": 2, "persona": 2, "tag_fit": 2}}
            for preset in PRESETS for probe in PROBES for seed in persona_metrics.SEEDS]


class PersonaGate(unittest.TestCase):
    def test_a_retry_does_not_hide_the_models_first_shape_failure(self):
        cases = rows()
        baseline = persona_metrics.score(cases)
        cases[0]["turns"][0]["attempts"].insert(0, {"raw": "As an AI, no.\n", "cut_off": None})
        after = persona_metrics.score(cases)
        preset = cases[0]["preset"]
        self.assertEqual(after["presets"][preset]["spoken_completion"],
                         baseline["presets"][preset]["spoken_completion"])
        self.assertTrue(any("tag_validity" in item for item in persona_metrics.regressions(baseline, after)))

    def test_an_increase_in_inventions_fails_even_when_one_style_improves(self):
        cases = rows()
        cases[-1]["judge"]["persona"] = 1
        baseline = persona_metrics.score(cases)
        cases[-1]["judge"]["persona"] = 2
        cases[0]["judge"]["invented_memory"] = True
        problems = persona_metrics.regressions(baseline, persona_metrics.score(cases))
        self.assertEqual(len(problems), 1)
        self.assertIn("invented_memory", problems[0])

    def test_a_changed_input_does_not_satisfy_the_probe_matrix(self):
        cases = rows()
        cases[0]["turns"][0]["input"] = "A substitute query never in the probe set."
        with self.assertRaises(ValueError):
            persona_metrics.score(cases)

    def test_release_reuses_exact_inputs_and_rejects_a_stale_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "previous.json"
            cases = rows()
            revision = {"files": dict.fromkeys(persona_eval.PERSONA_SOURCES, "a" * 64), "model_sha256": "b" * 64}
            protocol = {"sha256": "c" * 64, "server_sha256": "d" * 64, "judge_sha256": "e" * 64,
                        "system": "Darwin", "machine": "arm64"}
            path.write_text(json.dumps({"kind": persona_eval.KIND, "protocol": protocol, "revision": revision,
                                        "rows": cases, "metrics": persona_metrics.score(cases)}))
            inputs = {"model": {"sha256": revision["model_sha256"]},
                      "files": {name: {"sha256": value} for name, value in revision["files"].items()}}
            with patch.object(persona_eval, "protocol_id", return_value=protocol["sha256"]):
                evidence, problems = persona_eval.release_check({"source": inputs}, path)
                self.assertEqual((evidence["decision"], problems), ("reused", []))
                changed = copy.deepcopy(inputs)
                changed["files"]["config/identity.md"]["sha256"] = "f" * 64
                with self.assertRaisesRegex(ValueError, "does not match the sealed source"):
                    persona_eval.release_check({"source": changed}, path, path)
