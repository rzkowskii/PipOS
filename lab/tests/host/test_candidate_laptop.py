"""The laptop candidate lane: sealing laptop media beside lab media, and rootfs parity."""

import io
import json
from contextlib import redirect_stderr, redirect_stdout
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from lab import candidate_manifest as manifest
from lab import candidate_parity as parity
from tests.test_candidate_build import CandidateBuild
from tests.test_candidate_manifest import disk_image

SECTOR = 512


def laptop_payload(path, names):
    """Give every system slot in a disk_image() fixture the laptop's own root."""
    with path.open("r+b") as disk:
        for index, name in enumerate(names):
            if name in ("system-a", "system-b", "try-system"):
                disk.seek((40 + index * 3) * SECTOR + 109)
                disk.write(b"\x07")


class LaptopRoles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = Path(__file__).resolve().parents[3]
        self.ids = {k: uuid.UUID(v) for k, v in manifest.factory_slots(self.root).items()}
        self.paths = {}
        for role, names, laptop in (("factory", manifest.FACTORY, False), ("try_image", manifest.TRY, False),
                                    ("laptop_factory", manifest.FACTORY, True),
                                    ("laptop_try", manifest.LAPTOP_TRY, True)):
            path = self.dir / f"{role}.img"
            disk_image(path, names, self.ids if "factory" in role else None)
            if laptop:
                laptop_payload(path, names)
            self.paths[role] = path

    def records(self):
        return manifest.artifact_records(self.paths, self.dir, manifest.factory_slots(self.root))

    def test_lab_and_laptop_roots_are_grouped_separately(self):
        records = self.records()
        lab = records["factory"]["partitions"][1]["rootfs"]["sha256"]
        laptop = records["laptop_factory"]["partitions"][1]["rootfs"]["sha256"]
        self.assertNotEqual(lab, laptop)
        self.assertEqual(records["laptop_try"]["partitions"][1]["rootfs"]["sha256"], laptop)
        self.assertEqual([p["name"] for p in records["laptop_try"]["partitions"]], list(manifest.LAPTOP_TRY))

    def test_laptop_media_with_the_lab_root_is_refused(self):
        disk_image(self.paths["laptop_try"], manifest.LAPTOP_TRY)
        with self.assertRaisesRegex(ValueError, "laptop system payloads differ"):
            self.records()
        disk_image(self.paths["laptop_factory"], manifest.FACTORY, self.ids)
        with self.assertRaisesRegex(ValueError, "carry the lab system payload"):
            self.records()

    def test_laptop_try_without_its_log_partition_is_refused(self):
        disk_image(self.paths["laptop_try"], manifest.TRY)
        with self.assertRaisesRegex(ValueError, "unexpected laptop_try partitions"):
            self.records()


class Parity(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def tree(self, name, files):
        root = self.dir / name
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        return parity.listing(root)

    def test_only_allow_listed_differences_pass(self):
        common = {"usr/bin/being-face": "face", "usr/lib/being/mind/turn.py": "mind",
                  "etc/being/build-id": "x"}
        lab = self.tree("lab", {**common, "usr/sbin/sshd": "ssh", "usr/lib/being/sim/body.py": "sim",
                                "boot/bzImage": "lab kernel", "etc/being/build-id": "lab"})
        laptop = self.tree("laptop", {**common, "boot/bzImage": "laptop kernel", "etc/being/build-id": "laptop"})
        result = parity.compare(lab, laptop)
        self.assertTrue(result["ok"], result["unexplained"])
        self.assertGreaterEqual(result["same"], 2)
        self.assertLessEqual({"usr/sbin/sshd", "usr/sbin", "boot/bzImage", "etc/being/build-id"},
                             {row["path"] for row in result["explained"]})

    def test_a_mind_file_that_differs_fails(self):
        lab = self.tree("lab", {"usr/lib/being/mind/turn.py": "new"})
        laptop = self.tree("laptop", {"usr/lib/being/mind/turn.py": "old"})
        result = parity.compare(lab, laptop)
        self.assertFalse(result["ok"])
        self.assertEqual(result["unexplained"], [{"path": "usr/lib/being/mind/turn.py", "differs": True}])

    def test_lab_only_rule_does_not_excuse_a_laptop_only_file(self):
        lab = self.tree("lab", {"usr/bin/x": "x"})
        laptop = self.tree("laptop", {"usr/bin/x": "x", "usr/sbin/sshd": "ssh"})
        self.assertFalse(parity.compare(lab, laptop)["ok"])

    def test_cli(self):
        for name, files in (("lab", {"a": "1"}), ("laptop", {"a": "2"})):
            (self.dir / f"{name}.json").write_text(json.dumps(self.tree(name, files)))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(parity.main(["compare", str(self.dir / "lab.json"), str(self.dir / "laptop.json"),
                                          "--output", str(self.dir / "p.json")]), 1)
        self.assertFalse(json.loads((self.dir / "p.json").read_text())["ok"])


class LaptopTargetChecks(unittest.TestCase):
    """candidate_laptop_verify in lab/candidate-build.sh, on a fake Buildroot output."""

    def setUp(self):
        self.out = Path(self.enterContext(tempfile.TemporaryDirectory()))
        files = {
            "images/efi-part/EFI/BOOT/grub.cfg": "menuentry \"Try PipOS\" { linux /boot/bzImage quiet }\n",
            "target/usr/share/being/installed-grub.cfg": "linux /boot/bzImage quiet\n",
            "target/etc/rauc/system.conf": "[system]\ncompatible=beingos-laptop-v1\n",
            "target/usr/share/being/installed-rauc.conf": "compatible=beingos-laptop-installed-v1\n",
            "target/usr/share/being/variant": "laptop\n",
            "target/etc/being/source-id": "a" * 64 + "\n",
        }
        for name, text in files.items():
            (self.out / name).parent.mkdir(parents=True, exist_ok=True)
            (self.out / name).write_text(text)

    def verify(self):
        script = Path(__file__).resolve().parents[2] / "candidate-build.sh"
        return subprocess.run(["bash", "-c", f'. "{script}"; candidate_laptop_verify "$1"', "verify", str(self.out)],
                              env={"PATH": "/usr/bin:/bin", "BEING_SOURCE_ID": "a" * 64},
                              capture_output=True, text=True, timeout=30)

    def test_a_clean_laptop_target_passes(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_lab_files_serial_console_and_lab_identity_fail(self):
        (self.out / "target/usr/sbin").mkdir(parents=True)
        (self.out / "target/usr/sbin/sshd").write_text("")
        (self.out / "images/efi-part/EFI/BOOT/grub.cfg").write_text("serial --unit=0\nlinux x oops=panic\n")
        (self.out / "target/etc/rauc/system.conf").write_text("compatible=beingos-x86_64-v1\n")
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        for phrase in ("lab-only /usr/sbin/sshd", "serial console", "panics on an oops", "laptop update identity"):
            self.assertIn(phrase, result.stderr)


class ControllerBinding(unittest.TestCase):
    def test_laptop_smoke_runs_only_with_a_laptop_stick(self):
        from lab import candidate_tests as gate
        self.assertNotIn("lab.tests.test_laptop_smoke", gate.suite_names(False))
        self.assertIn("lab.tests.test_laptop_smoke", gate.suite_names(False, True))
        self.assertEqual(gate.BINDINGS["laptop_try"], ("BEING_LAPTOP_TRY_IMAGE",))
        self.assertNotIn("laptop_try", gate.RELEASE_ROLES)
        # The ISO suite is classified, but no candidate seals an ISO yet.
        self.assertIn("iso", gate.SUITES)
        self.assertNotIn("lab.tests.test_iso", gate.suite_names(True, True))


class LaptopCandidateBuild(unittest.TestCase):
    """`lab/build.sh candidate NAME --laptop` against the same fake container."""

    setUp = CandidateBuild.setUp

    def build(self, *args, **env):
        return subprocess.run(["bash", "lab/build.sh", "candidate", *args], cwd=self.root,
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def test_laptop_lane_seals_laptop_media_with_one_source_id(self):
        import shutil
        shutil.copy2(Path(manifest.__file__), self.root / "lab/candidate_manifest.py")
        media = Path(self.env["FIXTURE_MEDIA"])
        ids = {k: uuid.UUID(v) for k, v in manifest.factory_slots(self.root).items()}
        disk_image(media / "beingos-laptop.img", manifest.FACTORY, ids)
        laptop_payload(media / "beingos-laptop.img", manifest.FACTORY)
        disk_image(media / "beingos-laptop-try.img", manifest.LAPTOP_TRY)
        laptop_payload(media / "beingos-laptop-try.img", manifest.LAPTOP_TRY)
        (media / "laptop-buildroot.config").write_text("laptop config")
        (media / "rootfs-parity.json").write_text('{"ok": true}')
        result = self.build("laptop1", "--laptop")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory = self.root / "out/candidates/laptop1"
        sealed = json.loads((directory / "release.json").read_text())
        self.assertTrue({"laptop_factory", "laptop_try", "extra:rootfs_parity",
                         "extra:laptop_buildroot_config"} <= set(sealed["artifacts"]))
        self.assertEqual(sealed["source_id"], manifest.image_source_id(directory / "source"))
        sidecar = (directory / "media/beingos-laptop-try.img.source-id").read_text().strip()
        self.assertEqual(sidecar, sealed["source_id"])
        self.assertEqual(self.build("other", "--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
