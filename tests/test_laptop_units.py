"""Every unit the mind or the being's scripts name exists on the laptop image (task 1.20).

The laptop image is built without the lab body and without LAN maintenance, so
being-lab-body, being-lab-observe and being-lab-dev are absent there. Naming a
missing unit to systemctl fails the whole command, which is how Setup showed a
red error and network changes always failed on the laptop.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "os/package/being"
UNIT = re.compile(r"\b(?:being|systemd|rauc)[a-z0-9-]*\.service\b")
LAB_ONLY = {"being-lab-body.service", "being-lab-observe.service", "being-lab-dev.service"}
# Only development Try sticks built with BEING_LAPTOP_CABLE=1 have these (docs/LAB.md).
CABLE_ONLY = {"being-cable-link.service", "being-cable-ssh.service", "being-cable-workbench.service"}
# The blocks that only lab, maintenance or cable development builds include.
OPTIONAL_BLOCK = re.compile(r"ifeq \(\$\(BR2_PACKAGE_BEING_(?:LAN_MAINTENANCE|LAB_BODY|CABLE_DEV)\),y\).*?endif",
                            re.S)
# Transient units: started with systemd-run --unit=NAME, so no unit file exists
# anywhere; the module naming one must be the one that creates it.
TRANSIENT = {"being-backup-publish.service", "being-removable-media.service", "being-restore-stage.service"}
# Modules that only run on images that ship the lab units.
LAB_MODULES = {"lab_access.py"}


def laptop_units():
    """Units being.mk installs whatever the lab options, plus generated and system ones."""
    recipe = (PACKAGE / "being.mk").read_text()
    kept = OPTIONAL_BLOCK.sub("", recipe)
    units = set(re.findall(r"system/(being-[a-z0-9-]+\.service)\b", kept))
    units |= set(re.findall(r"system/(being-[a-z0-9-]+\.service)\b",
                            (REPO / "os/package/cortex/cortex.mk").read_text()))
    for generator in PACKAGE.glob("being-*-generator"):
        units |= set(re.findall(r"(being-[a-z0-9-]+\.service)", generator.read_text()))
    return units


def named_units():
    named = {}
    sources = [*sorted((REPO / "mind").glob("*.py")),
               *sorted(path for path in PACKAGE.glob("being-*") if path.is_file() and path.suffix == "")]
    for path in sources:
        for unit in UNIT.findall(path.read_text(errors="replace")):
            named.setdefault(unit, set()).add(path.name)
    return named


class LaptopUnits(unittest.TestCase):
    def test_lab_units_are_not_on_the_laptop_image(self):
        units = laptop_units()
        self.assertIn("being-mind.service", units)
        self.assertIn("being-voice.service", units)
        self.assertTrue(LAB_ONLY.isdisjoint(units), LAB_ONLY & units)

    def test_cable_units_are_not_on_the_laptop_image(self):
        units = laptop_units()
        self.assertTrue(CABLE_ONLY.isdisjoint(units), CABLE_ONLY & units)

    def test_wave_two_units_are_on_the_laptop_image(self):
        # The microphone mixer runs before the ears on every image, and the
        # bench unit ships everywhere but only a measuring boot starts it.
        units = laptop_units()
        for name in ("being-audio-init.service", "being-bench.service"):
            self.assertIn(name, units)
        recipe = OPTIONAL_BLOCK.sub("", (PACKAGE / "being.mk").read_text())
        self.assertIn("$(TARGET_DIR)/usr/libexec/being/being-audio-init", recipe)

    def test_every_named_unit_exists_or_is_guarded(self):
        units = laptop_units()
        for unit, files in sorted(named_units().items()):
            if unit.startswith(("systemd-", "rauc")) or unit in units:
                continue
            with self.subTest(unit=unit):
                if unit in TRANSIENT:
                    for name in files:
                        text = (REPO / "mind" / name).read_text() if (REPO / "mind" / name).exists() else ""
                        self.assertIn(f"--unit={unit.removesuffix('.service')}", text,
                                      f"{name} names the transient {unit} without creating it")
                    continue
                self.assertIn(unit, LAB_ONLY, f"{unit} (named in {sorted(files)}) is not on the laptop image")
                for name in files - LAB_MODULES:
                    text = (REPO / "mind" / name).read_text() if (REPO / "mind" / name).exists() else ""
                    self.assertIn("installed(", text, f"{name} names {unit} without checking that it exists")


if __name__ == "__main__":
    unittest.main()


class RuntimeDirectories(unittest.TestCase):
    """systemd hands a unit's runtime directories back to its user and
    RuntimeDirectoryMode before every command it starts (systemd 258), so an
    ExecStartPre chown or chmod of one is undone by the next command."""

    def test_no_unit_changes_its_runtime_directory_before_the_last_command(self):
        for path in sorted(PACKAGE.glob("*.service")):
            text = path.read_text()
            folders = [word for line in re.findall(r"(?m)^RuntimeDirectory=(.*)$", text) for word in line.split()]
            for folder in folders:
                pre = [line for line in re.findall(r"(?m)^ExecStartPre=(.*)$", text)
                       if re.search(rf"\bch(own|mod)\b.*/run/{re.escape(folder)}\b", line)]
                self.assertEqual(pre, [], f"{path.name}: ExecStartPre changes /run/{folder}, which systemd undoes")

    def test_hands_gives_the_panel_frames_to_the_renderer_after_it_starts(self):
        text = (PACKAGE / "being-hands.service").read_text()
        self.assertRegex(text, r"(?m)^RuntimeDirectory=.*\bbeing-panel\b")
        posts = re.findall(r"(?m)^ExecStartPost=(.*)$", text)
        self.assertEqual(len(posts), 1, posts)
        self.assertIn("chown being-panel:being-panel /run/being-panel", posts[0])
        self.assertIn("chmod 0750 /run/being-panel", posts[0])
