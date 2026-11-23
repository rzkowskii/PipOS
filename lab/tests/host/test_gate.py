"""Gate hygiene: source-ids on images, skips as failures, organ health in the smoke log."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab import source_id
from lab.tests import test_laptop_smoke as smoke

ROOT = Path(__file__).resolve().parents[3]


def tiny_tree(root):
    for scope in source_id.IMAGE_SCOPES:
        path = root / scope
        if Path(scope).suffix:
            path.write_text(scope)
        else:
            path.mkdir(parents=True)
            (path / "file.txt").write_text(scope)
    return root


class SourceId(unittest.TestCase):
    def setUp(self):
        source_id.cached_tree_id.cache_clear()
        self.addCleanup(source_id.cached_tree_id.cache_clear)
        self.root = tiny_tree(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.image = self.root / "beingos.img"
        self.image.write_bytes(b"image")
        self.warnings = []

    def enforce(self, **env):
        return source_id.enforce(self.image, self.root, env=env, warn=self.warnings.append)

    def test_only_image_inputs_change_the_id(self):
        before = source_id.tree_id(self.root)
        (self.root / "lab").mkdir()
        (self.root / "lab/test_x.py").write_text("a test")
        (self.root / "docs").mkdir()
        (self.root / "docs/x.md").write_text("words")
        self.assertEqual(source_id.tree_id(self.root), before)
        (self.root / "mind/file.txt").write_text("changed")
        self.assertNotEqual(source_id.tree_id(self.root), before)

    def test_lab_files_the_build_reads_and_execute_bits_change_the_id(self):
        (self.root / "lab").mkdir()
        before = source_id.tree_id(self.root)
        (self.root / "lab/package_model.py").write_text("packs the model for post-image.sh")
        with_packager = source_id.tree_id(self.root)
        self.assertNotEqual(with_packager, before)
        (self.root / "lab/bench_guest.py").write_text("installed as /usr/lib/being/bench.py")
        self.assertNotEqual(source_id.tree_id(self.root), with_packager)
        script = self.root / "os/file.txt"
        plain = source_id.tree_id(self.root)
        script.chmod(0o755)
        self.assertNotEqual(source_id.tree_id(self.root), plain)

    def test_stamp_takes_the_id_from_before_the_build(self):
        early = source_id.tree_id(self.root)
        (self.root / "mind/file.txt").write_text("edited while the build ran")
        source_id.stamp(self.image, self.root, ident=early)
        self.assertEqual(source_id.image_id(self.image), early)
        self.assertEqual(source_id.check(self.image, self.root)[0], "stale")
        with self.assertRaises(ValueError):
            source_id.stamp(self.image, self.root, ident="not-an-id")
        with patch("sys.stderr"):
            self.assertEqual(source_id.main(["stamp", str(self.image)]), 2)

    def test_matching_stale_and_unknown_images(self):
        self.assertEqual(self.enforce(), "unknown")
        self.assertTrue(self.warnings)
        with self.assertRaises(source_id.StaleImage):
            self.enforce(BEING_GATE="1")
        source_id.stamp(self.image, self.root)
        self.assertEqual(self.enforce(BEING_GATE="1"), "match")
        (self.root / "crates/file.txt").write_text("new code")
        source_id.cached_tree_id.cache_clear()
        with self.assertRaises(source_id.StaleImage) as caught:
            self.enforce()
        self.assertIn("BEING_ALLOW_STALE=1", str(caught.exception))
        self.assertEqual(self.enforce(BEING_ALLOW_STALE="1"), "stale")

    def test_environment_override_and_garbage_sidecar(self):
        source_id.sidecar(self.image).write_text("not a hash\n")
        self.assertIsNone(source_id.image_id(self.image))
        with patch.dict(os.environ, BEING_IMAGE_SOURCE_ID=source_id.tree_id(self.root)):
            self.assertEqual(self.enforce(), "match")
        self.assertIn("BEING_IMAGE_SOURCE_ID", self.warnings[-1], "a stated id must be reported")

    def test_vm_refuses_a_stale_image_before_starting_qemu(self):
        from lab import vm
        source_id.sidecar(self.image).write_text("0" * 64 + "\n")
        machine = vm.VM(image=self.image)
        with patch.dict(os.environ, {"BEING_ALLOW_STALE": "0"}), patch.object(vm, "overlay") as overlay:
            with self.assertRaises(source_id.StaleImage):
                machine.start()
            overlay.assert_not_called()


class GateMode(unittest.TestCase):
    def run_child(self, gate):
        script = ("import unittest, sys\n"
                  "import lab.tests\n"
                  "class T(unittest.TestCase):\n"
                  "    @unittest.skip('no image')\n"
                  "    def test_a(self): pass\n"
                  "    def test_b(self): self.skipTest('missing tool')\n"
                  "r = unittest.TextTestRunner(stream=sys.stdout).run(unittest.defaultTestLoader.loadTestsFromTestCase(T))\n"
                  "print('RESULT', len(r.skipped), len(r.failures))\n")
        env = dict(os.environ, BEING_GATE=gate)
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=30)
        return result.stdout.strip().splitlines()[-1]

    def test_skips_fail_only_in_gate_mode(self):
        self.assertEqual(self.run_child("0"), "RESULT 2 0")
        self.assertEqual(self.run_child("1"), "RESULT 0 2")


class OrganHealth(unittest.TestCase):
    def test_restarts_and_failures_of_organs_are_reported(self):
        journal = ("[   10.0] pipos systemd[1]: being-voice.service: Scheduled restart job, restart counter is at 2.\n"
                   "[   11.0] pipos systemd[1]: being-usb-logs.service: Scheduled restart job, restart counter is at 1.\n"
                   "[   12.0] pipos systemd[1]: being-mind.service: Main process exited, code=killed, status=9/KILL\n")
        trouble = smoke.organ_trouble(journal)
        self.assertEqual(len(trouble), 2)
        self.assertIn("being-voice.service restarted 2 times", trouble)
        self.assertTrue(any("being-mind.service failed" in line for line in trouble))
        self.assertEqual(smoke.organ_trouble("[ 1.0] pipos being-brainstem[3]: brainstem: waking -> idle\n"), [])


if __name__ == "__main__":
    unittest.main()
