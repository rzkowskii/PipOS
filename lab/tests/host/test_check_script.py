"""scripts/check: skip reporting, tier contents and dry runs (nothing is booted)."""

import importlib.machinery
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def load():
    loader = importlib.machinery.SourceFileLoader("pipos_check", str(ROOT / "scripts/check"))
    spec = importlib.util.spec_from_loader("pipos_check", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CheckScript(unittest.TestCase):
    def setUp(self):
        self.check = load()

    def test_skips_are_listed_with_reasons(self):
        log_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = ("import unittest\n"
                  "class T(unittest.TestCase):\n"
                  "    @unittest.skip('needs age on PATH')\n"
                  "    def test_a(self): pass\n"
                  "    def test_b(self):\n"
                  "        '''A docstring line.'''\n"
                  "        self.skipTest('no model server')\n"
                  "    def test_c(self): pass\n"
                  "unittest.main(verbosity=2)\n")
        with redirect_stdout(io.StringIO()):
            result = self.check.step("demo", [self.check.sys.executable, "-c", script], log_dir=log_dir)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(sorted(skip["reason"] for skip in result["skips"]), ["needs age on PATH", "no model server"])
        self.assertTrue(all(skip["test"].endswith(("test_a", "test_b")) for skip in result["skips"]))

    def test_boot_gate_holds_the_planned_suites_and_runs_in_gate_mode(self):
        self.assertEqual(len(self.check.BOOT_GATE), 9)
        self.assertIn("lab.tests.test_l2.Thinking", self.check.BOOT_GATE)
        for suite in self.check.BOOT_GATE:
            module = suite.split(".")[2]
            self.assertTrue((ROOT / "lab/tests" / f"{module}.py").is_file(), suite)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.check.main(["boot-gate", "--dry-run"]), 0)
        planned = json.loads(out.getvalue()[out.getvalue().index("["):])
        self.assertTrue(all(row["env"]["BEING_GATE"] == "1" for row in planned))

    def test_fast_lists_its_steps_and_marks_docs_and_media_advisory(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.check.main(["fast", "--dry-run"]), 0)
        names = [row["name"] for row in json.loads(out.getvalue()[out.getvalue().index("["):])]
        self.assertEqual(names, ["rust", "python-host", "lab-host", "manual-links", "media-gallery"])

    def test_full_needs_a_manifest(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            self.check.main(["full", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
