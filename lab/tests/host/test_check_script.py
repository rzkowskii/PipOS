"""scripts/check: skip reporting, tier contents and dry runs (nothing is booted)."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lab import candidate_manifest, candidate_tests
from tests.test_candidate_manifest import disk_image

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

    def test_boot_gate_verifies_sealed_artifacts_before_launching_guests(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for scope in candidate_manifest.SCOPES:
            target = root / scope
            if target.suffix:
                target.touch()
            else:
                target.mkdir()
        slots = root / "os/board/beingos/slots.sh"
        slots.parent.mkdir(parents=True)
        slots.write_bytes((ROOT / slots.relative_to(root)).read_bytes())
        ids = candidate_manifest.factory_slots(root)
        paths = {key: root / role for role, key in candidate_tests.RELEASE_ROLES.items()}
        for path in paths.values():
            path.write_text(path.name)
        disk_image(paths["factory"], candidate_manifest.FACTORY,
                   {name: uuid.UUID(value) for name, value in ids.items()})
        disk_image(paths["try_image"], candidate_manifest.TRY)
        release = root / "release.json"
        candidate_manifest.publish(release, {"kind": "beingos-candidate-v1",
                                             "source": candidate_manifest.source_snapshot(root),
                                             "artifacts": candidate_manifest.artifact_records(paths, root, ids)})
        suite = "lab.tests.test_l1"
        args = Namespace(manifest=release, suite=[suite], dry_run=False, jobs=1, suite_timeout=1)
        real_step = self.check.step

        def step(name, command, **kwargs):
            if name == suite:
                self.assertEqual(kwargs["env"]["BEING_FACTORY_IMAGE"], str(paths["factory"]))
                self.assertNotIn("BEING_LAPTOP_CABLE_TRY_IMAGE", kwargs["env"])
                return {"name": name, "status": "passed"}
            return real_step(name, command, **kwargs)

        with (patch.object(self.check, "ROOT", root), patch.object(self.check, "step", side_effect=step) as run,
              patch.dict(os.environ, PYTHONPATH=str(ROOT), BEING_FACTORY_IMAGE="unsealed.img",
                         BEING_LAPTOP_CABLE_TRY_IMAGE="private-cable.img"),
              redirect_stdout(io.StringIO())):
            self.assertEqual([row["status"] for row in self.check.boot_gate(args, root)], ["passed", "passed"])
            self.assertEqual([call.args[0] for call in run.call_args_list], ["verify-candidate", suite])
            run.reset_mock()
            paths["factory_bundle"].write_bytes(b"substitute")
            failed = self.check.boot_gate(args, root)
            self.assertEqual([row["status"] for row in failed], ["failed (1)"])
            self.assertIn("candidate artifact changed: factory_bundle", Path(failed[0]["log"]).read_text())
            self.assertEqual([call.args[0] for call in run.call_args_list], ["verify-candidate"])
            args = Namespace(manifest=None, suite=["lab.tests.test_cable_dev"], dry_run=True)
            self.assertEqual(self.check.boot_gate(args, root)[0]["env"]["BEING_LAPTOP_CABLE_TRY_IMAGE"],
                             "private-cable.img")

    def test_boot_gate_missing_laptop_media_cannot_fall_back_to_working_images(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        release = root / "release.json"
        release.write_text(json.dumps({"kind": "beingos-candidate-v1", "artifacts": {
            key: {"path": f"{role}.img"} for role, key in candidate_tests.RELEASE_ROLES.items()
        }}))
        for suite, error in ((None, "manifest is missing laptop_try"),
                             (["lab.tests.test_wifi"], "manifest is missing wifi_image"),
                             (["lab.tests.test_cable_dev"], "these suites use unsealed media: cable_dev")):
            with (self.subTest(suite=suite), patch.dict(os.environ, BEING_LAPTOP_TRY_IMAGE="unsealed.img"),
                  redirect_stderr(io.StringIO())):
                args = Namespace(manifest=release, suite=suite, dry_run=True, jobs=1, suite_timeout=1)
                self.assertEqual(self.check.boot_gate(args, root)[-1]["error"], error)

    def boot_fixture(self, source):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = root / "scripts/check"
        script.parent.mkdir()
        shutil.copyfile(ROOT / "scripts/check", script)
        (root / "gate_fixture.py").write_text(source)
        command = [sys.executable, str(script), "boot-gate", "--suite", "gate_fixture", "--jobs", "1"]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, (root, ROOT))), GATE_FIXTURE_ROOT=str(root))
        return root, command, env

    def test_boot_gate_rejects_a_skip_outside_the_lab_test_package(self):
        root, command, env = self.boot_fixture("""
import unittest
class Fixture(unittest.TestCase):
    @unittest.skip('fixture missing tool')
    def test_skip(self): pass
""")
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(next(root.glob("out/logs/*/summary.json")).read_text())
        self.assertFalse(report["ok"])
        self.assertEqual(report["steps"][0]["skips"][0]["reason"], "fixture missing tool")

    def test_boot_gate_termination_reaps_a_guest_child_and_removes_its_scratch(self):
        root, command, env = self.boot_fixture('''
import os, subprocess, sys, unittest
class Fixture(unittest.TestCase):
    def test_guest(self):
        child = """import json, os, socket, time
from pathlib import Path
path = Path(os.environ['GATE_FIXTURE_ROOT']) / 'ready'
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    server.listen()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps([server.getsockname()[1], os.environ['TMPDIR']]))
    temporary.replace(path)
    time.sleep(8)
"""
        subprocess.run([sys.executable, '-c', child], check=True, timeout=10)
''')
        proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL)
        try:
            ready = root / "ready"
            deadline = time.monotonic() + 5
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertTrue(ready.exists(), "fixture child did not start")
            port, scratch = json.loads(ready.read_text())
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=5), 1)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            self.assertFalse(Path(scratch).exists())
            report = json.loads(next(root.glob("out/logs/*/summary.json")).read_text())
            self.assertEqual(report["steps"][0]["status"], "interrupted")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_full_needs_a_manifest(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            self.check.main(["full", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
