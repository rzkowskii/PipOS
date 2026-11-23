"""Exercise the generator's real shell code with a fixture kernel command line."""

import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


GENERATOR = Path(__file__).resolve().parents[1] / "os/package/being/being-try-generator"
GUID = "AEC08B06-4DD2-4B82-88A0-2822192E249C"
MASKED = ("bootstate.mount", "rauc.service", "being-boot-health.service", "being-trial-reboot.service")


class TryGeneratorTests(unittest.TestCase):
    def generate(self, command_line):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cmdline = work / "cmdline"
        cmdline.write_text(command_line + "\n")
        dirs = (work / "normal", work / "early", work / "late")
        for directory in dirs:
            directory.mkdir()
        script = GENERATOR.read_text()
        self.assertEqual(script.count("/proc/cmdline"), 1)
        script = script.replace("/proc/cmdline", shlex.quote(str(cmdline)))
        result = subprocess.run(["/bin/sh", "-c", script, str(GENERATOR), *map(str, dirs)],
                                capture_output=True, text=True, check=True, timeout=5)
        for directory in (dirs[0], dirs[2]):
            self.assertEqual(list(directory.iterdir()), [])
        return dirs[1], result.stderr

    def assert_masked(self, early, unit_names):
        for unit in unit_names:
            self.assertEqual((early / unit).readlink(), Path("/dev/null"))

    def test_normal_mode_creates_no_units(self):
        early, stderr = self.generate(f"root=PARTUUID={GUID} ro being.try=0 being.models={GUID}")
        self.assertEqual(list(early.iterdir()), [])
        self.assertEqual(stderr, "")

    def test_try_mounts_only_ram_and_its_exact_read_only_model_partition(self):
        early, stderr = self.generate(f"root=PARTUUID={GUID} ro being.try=1 fstab=no systemd.gpt_auto=no being.models={GUID}")
        self.assertEqual(stderr, "")
        self.assert_masked(early, MASKED)
        required = early / "local-fs.target.requires"
        for name in ("mind", "var", "models"):
            self.assertEqual((required / f"{name}.mount").resolve(), (early / f"{name}.mount").resolve())
        for name in ("mind", "var"):
            contents = (early / f"{name}.mount").read_text()
            self.assertIn(f"What=tmpfs\nWhere=/{name}\nType=tmpfs\n", contents)
            self.assertIn("noswap", contents)
        self.assertIn("mode=0700,size=512M", (early / "mind.mount").read_text())
        contents = (early / "models.mount").read_text()
        self.assertIn(f"What=/dev/disk/by-partuuid/{GUID.lower()}\n", contents)
        self.assertIn("Type=ext4\nOptions=ro,noload,nodev,nosuid,noexec\n", contents)

    def test_missing_invalid_or_ambiguous_model_partuuid_is_masked(self):
        for argument in ("", "being.models=../root", "being.models=00000000-0000-0000-0000-000000000000",
                         f"being.models={GUID} being.models={GUID}", f"being.models being.models={GUID}"):
            with self.subTest(argument=argument):
                early, stderr = self.generate(f"being.try=1 {argument}")
                self.assert_masked(early, (*MASKED, "models.mount"))
                self.assertIn("Try mode requires one valid model partition GUID", stderr)
                required = early / "local-fs.target.requires"
                self.assertEqual({entry.name for entry in required.iterdir()}, {"mind.mount", "var.mount"})
