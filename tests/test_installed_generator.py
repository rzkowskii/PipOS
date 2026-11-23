"""Exercise installed boot binding with firmware-style cmdline fixtures."""

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import install_binding


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "os/board/beingos"
GENERATOR = ROOT / "os/package/being/being-installed-generator"
UPDATE_READY = BOARD / "being-update-ready"
INSTALLED_CONFIG = "/run/systemd/generator.early/being-installed/rauc.conf"
GUIDS = {field: f"00000000-0000-4000-8000-{number:012d}"
         for number, field in enumerate(("esp", "slot_a", "slot_b", "models", "mind"), 1)}


def installed_args(slot="A"):
    active = GUIDS[f"slot_{slot.lower()}"]
    args = f"root=PARTUUID={active} ro rauc.slot={slot} being.installed=1 fstab=no systemd.gpt_auto=no"
    return f"{args} " + " ".join(f"being.{field}={guid}" for field, guid in GUIDS.items())


class InstalledGeneratorTests(unittest.TestCase):
    def generate(self, args):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        cmdline = work / "cmdline"
        cmdline.write_text(args + "\n")
        outputs = tuple(work / name for name in ("normal", "early", "late"))
        for output in outputs:
            output.mkdir()
        script = GENERATOR.read_text().replace("/proc/cmdline", shlex.quote(str(cmdline)))
        script = script.replace("/usr/share/being/installed-rauc.conf", shlex.quote(str(BOARD / "rauc.conf")))
        escape = work / "systemd-escape"
        escape.write_text("#!/bin/sh\n"
                          'test "$1" = --path || exit 1\n'
                          'case "$2 $3" in\n'
                          '  "--suffix=device /dev/disk/by-partuuid/"*) printf "target-%s.device\\n" "${3##*/}" ;;\n'
                          '  "--template=systemd-fsck@.service /dev/disk/by-partuuid/' + GUIDS["mind"] + '") '
                          'printf "systemd-fsck@target.service\\n" ;;\n'
                          '  "--template=systemd-fsck@.service /dev/disk/by-partuuid/' + GUIDS["esp"] + '") '
                          'printf "systemd-fsck@esp.service\\n" ;;\n'
                          '  *) exit 1 ;;\n'
                          'esac\n')
        escape.chmod(0o755)
        env = dict(os.environ, PATH=f"{work}:{os.environ['PATH']}")
        result = subprocess.run(["/bin/sh", "-c", script, str(GENERATOR), *map(str, outputs)],
                                env=env, text=True, capture_output=True, check=True, timeout=6)
        for output in (outputs[0], outputs[2]):
            self.assertEqual(list(output.iterdir()), [])
        return outputs[1], result.stderr

    def test_factory_and_try_modes_keep_their_own_generators(self):
        for args in ("root=PARTUUID=lab rauc.slot=A", "being.try=1", f"{installed_args()} being.try=1"):
            with self.subTest(args=args):
                early, stderr = self.generate(args)
                self.assertEqual(list(early.iterdir()), [])
                self.assertFalse(stderr)

    def test_mounts_and_rauc_use_one_installed_mapping_for_either_slot(self):
        for slot in ("A", "B"):
            with self.subTest(slot=slot):
                early, stderr = self.generate(installed_args(slot))
                self.assertFalse(stderr)
                requires = early / "local-fs.target.requires"
                for mount, field in (("bootstate", "esp"), ("models", "models"), ("mind", "mind")):
                    unit = (early / f"{mount}.mount").read_text()
                    self.assertIn(f"What=/dev/disk/by-partuuid/{GUIDS[field]}\nWhere=/{mount}\n", unit)
                    self.assertEqual((requires / f"{mount}.mount").resolve(), early / f"{mount}.mount")
                self.assertIn("Requires=being-installed-bind.service systemd-fsck@target.service",
                              (early / "mind.mount").read_text())
                for mount in ("bootstate", "models", "mind"):
                    self.assertIn("After=being-installed-bind.service", (early / f"{mount}.mount").read_text())
                binding = (early / "being-installed-bind.service").read_text()
                for guid in GUIDS.values():
                    self.assertIn(f"target-{guid}.device", binding)
                for check in ("systemd-fsck@target.service", "systemd-fsck@esp.service"):
                    fsck = (early / f"{check}.d" / "being-installed.conf").read_text()
                    self.assertIn("Requires=being-installed-bind.service\nAfter=being-installed-bind.service", fsck)
                # A dirty boot-state FAT is repaired before mounting, but a
                # failed check must not stop the confirmed slot from booting.
                esp_fsck = (early / "bootstate.mount.d" / "being-esp-fsck.conf").read_text()
                self.assertIn("Wants=systemd-fsck@esp.service\nAfter=systemd-fsck@esp.service", esp_fsck)
                self.assertNotIn("Requires=", esp_fsck)
                self.assertNotIn("systemd-fsck@esp.service", (early / "bootstate.mount").read_text())
                self.assertIn("Options=ro,noload,", (early / "models.mount").read_text())
                self.assertIn("What=tmpfs\nWhere=/var\nType=tmpfs", (early / "var.mount").read_text())
                rauc = early / "being-installed" / "rauc.conf"
                config = rauc.read_text()
                for field in ("slot_a", "slot_b"):
                    self.assertIn(f"device=/dev/disk/by-partuuid/{GUIDS[field]}", config)
                service = (early / "rauc.service.d" / "being-installed.conf").read_text()
                self.assertIn(f"ExecStart=/usr/bin/rauc --conf={rauc} ", service)
                self.assertIn("RequiresMountsFor=/bootstate /mind /models", service)

    def test_bad_or_ambiguous_ids_never_fall_back_to_factory_mounts(self):
        valid = installed_args()
        for args in (valid.replace("being.installed=1", "being.installed"),
                     valid.replace("fstab=no", "fstab=yes"),
                     valid.replace("rauc.slot=A", "rauc.slot=B"),
                     valid.replace(GUIDS["models"], GUIDS["mind"]),
                     valid.replace(GUIDS["mind"], "../path"),
                     valid.replace(GUIDS["esp"], "00000000-0000-0000-0000-000000000000"),
                     f"{valid} being.esp={GUIDS['esp']}", f"{valid} root=PARTUUID={GUIDS['slot_a']}"):
            with self.subTest(args=args):
                early, stderr = self.generate(args)
                self.assertIn("invalid installed boot identity", stderr)
                for unit in ("bootstate.mount", "mind.mount", "models.mount", "rauc.service",
                             "being-boot-health.service", "being-trial-reboot.service"):
                    self.assertEqual((early / unit).readlink(), Path("/dev/null"))
                self.assertFalse((early / "rauc.service.d").exists())

    def test_factory_grub_renders_with_the_existing_slot_ids(self):
        script = '. "$1"; slot_ids "$2"'
        result = subprocess.run(["/bin/sh", "-c", script, "render", str(BOARD / "slots.sh"), str(BOARD / "grub.cfg")],
                                text=True, capture_output=True, check=True, timeout=5)
        self.assertIn('set install_esp=""', result.stdout)
        self.assertIn("bbafccdc-0615-4e77-ab86-032633aedd4a", result.stdout)
        self.assertIn("14c028c4-8d14-415c-af2f-4348c7f5893b", result.stdout)
        self.assertNotRegex(result.stdout, r"\b(?:SLOT|INSTALL)_[A-Z_]+\b")

    def test_update_hook_rejects_a_factory_config_on_an_installed_boot(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cmdline = work / "cmdline"
        ready = work / "ready"
        ready.touch()
        script = UPDATE_READY.read_text().replace("/proc/cmdline", shlex.quote(str(cmdline)))
        script = script.replace("/run/being-slot-good", shlex.quote(str(ready)))
        script = script.replace("/usr/bin/python3 -m mind.install_binding", 'test "$MOCK_INSTALLED_TOPOLOGY" = valid')
        for args, config, topology, allowed in ((installed_args(), INSTALLED_CONFIG, "valid", True),
                                                (installed_args(), INSTALLED_CONFIG, "invalid", False),
                                                (installed_args(), "/etc/rauc/system.conf", "valid", False),
                                                ("being.try=1", INSTALLED_CONFIG, "valid", False),
                                                ("rauc.slot=A", "/etc/rauc/system.conf", "unused", True)):
            with self.subTest(args=args, config=config):
                cmdline.write_text(args + "\n")
                env = dict(os.environ, RAUC_SYSTEM_CONFIG=config, MOCK_INSTALLED_TOPOLOGY=topology)
                result = subprocess.run(["/bin/sh", "-c", script], env=env,
                                        text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0 if allowed else 1, result.stderr)


class InstalledBindingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        sysfs = self.root / "sys-dev-block"
        sysfs.mkdir()
        self.enterContext(patch.object(install_binding, "SYS_DEV_BLOCK", sysfs))
        self.partitions = {}
        self.aliases = {}
        for index, guid in enumerate(GUIDS.values(), 1):
            path = self.partition("sda", index, f"8:{index}")
            self.partitions[guid] = path
            self.aliases[str(install_binding.PARTUUIDS / guid)] = f"8:{index}"
        self.enterContext(patch.object(install_binding, "node_number", self.aliases.get))

    def partition(self, disk, index, dev):
        path = self.root / disk / f"{disk}{index}"
        path.mkdir(parents=True)
        (path / "partition").write_text(f"{index}\n")
        (path / "dev").write_text(f"{dev}\n")
        (install_binding.SYS_DEV_BLOCK / dev).symlink_to(path)
        return path

    def verify(self, slot="A", root_number="8:2"):
        guids, active = install_binding.installed_mapping(installed_args(slot))
        return install_binding.verify_partitions(guids, active, root_number)

    def test_accepts_either_exact_active_partition_on_the_same_disk(self):
        self.verify()
        self.verify("B", "8:3")

    def test_rejects_attached_disk_aliases_and_wrong_root(self):
        self.partition("sdb", 5, "8:21")
        path = str(install_binding.PARTUUIDS / GUIDS["mind"])
        self.aliases[path] = "8:21"
        with self.assertRaisesRegex(ValueError, "different disks"):
            self.verify()
        self.aliases[path] = "8:5"
        with self.assertRaisesRegex(ValueError, "running root"):
            self.verify("A", "8:3")

    def test_rejects_missing_alias_or_wrong_partition_number(self):
        path = str(install_binding.PARTUUIDS / GUIDS["models"])
        self.aliases.pop(path)
        with self.assertRaisesRegex(ValueError, "Missing block device for models"):
            self.verify()
        self.aliases[path] = "8:5"
        with self.assertRaisesRegex(ValueError, "Unexpected partition number for models"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
