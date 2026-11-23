import hashlib
import io
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lab import b43_firmware


class FirmwareStageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root / "b43"
        self.source.mkdir()
        self.manifest = self.root / "SHA256SUMS"
        self.overlay = self.root / "overlay"
        self.contents = {}
        for name in (*b43_firmware.required_files(26), *b43_firmware.required_files(29), "ucode5.fw"):
            self.contents[name] = bytes(range(8)) + name.encode()
            (self.source / name).write_bytes(self.contents[name])
        self.manifest.write_text("".join(f"{hashlib.sha256(data).hexdigest()} *b43/{name}\n"
                                         for name, data in self.contents.items()))

    def run_helper(self, *options):
        argv = [str(self.source), str(self.manifest), *options]
        with redirect_stdout(io.StringIO()) as output:
            b43_firmware.main(argv)
        return output.getvalue().strip()

    def assert_refused(self, expected, *, core=29):
        with redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as stop:
            self.run_helper("--core", str(core), "--overlay", str(self.overlay))
        self.assertEqual(stop.exception.code, 1)
        self.assertIn(expected, error.getvalue())

    def test_stages_only_the_three_confirmed_core_files(self):
        for core in (26, 29):
            output = self.root / str(core)
            reply = self.run_helper("--core", str(core), "--overlay", str(output))
            self.assertEqual(Path(reply), output.resolve())
            stage = output / "usr/lib/firmware/b43"
            self.assertEqual({path.name for path in stage.iterdir()}, set(b43_firmware.required_files(core)))
            directories = [stage, *(path for path in stage.parents if path.is_relative_to(output))]
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o755 for path in directories))
            for path in stage.iterdir():
                self.assertEqual(path.read_bytes(), self.contents[path.name])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_verify_only_never_writes_and_tampering_stops_publication(self):
        with patch.object(b43_firmware, "stage") as stage:
            self.assertIn("Verified BCM4331 core 29", self.run_helper("--core", "29", "--verify-only"))
            stage.assert_not_called()
        (self.source / "ucode29_mimo.fw").write_bytes(b"modified")
        self.assert_refused("checksum verification failed for ucode29_mimo.fw")
        self.assertFalse(self.overlay.exists())

    def test_incomplete_duplicate_or_path_based_manifest_is_rejected(self):
        original = self.manifest.read_text().splitlines(keepends=True)
        for manifest, message in (("".join(line for line in original if "ht0bsinitvals29.fw" not in line),
                                   "Missing SHA-256 checksums"),
                                  ("".join(original + [original[-1]]), "Duplicate firmware checksum"),
                                  ("".join(original).replace("*b43/", "*../b43/"), "Expected a SHA-256 line")):
            with self.subTest(message=message):
                self.manifest.write_text(manifest)
                self.assert_refused(message)
                self.assertFalse(self.overlay.exists())

    def test_existing_overlay_is_never_modified(self):
        self.overlay.mkdir()
        existing = self.overlay / "sentinel"
        existing.write_bytes(b"retain me")
        self.manifest.unlink()
        self.assert_refused("Overlay already exists")
        self.assertEqual(list(self.overlay.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"retain me")

    def test_symlinked_firmware_is_rejected(self):
        original = self.source / "ucode29_mimo.fw"
        replacement = self.root / original.name
        original.rename(replacement)
        original.symlink_to(replacement)
        self.assert_refused("ucode29_mimo.fw")
        self.assertFalse(self.overlay.exists())


if __name__ == "__main__":
    unittest.main()
