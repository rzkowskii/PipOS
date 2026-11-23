"""Install via the real Try screen and verify quiet hours on the installed disk.

Run with BEING_SCREEN_IMAGE pointing to a Try image that packages initiative setup.
"""

import json
import signal
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path

from lab.initiative_install_verify import verify_installed_initiative
from lab.tests.test_local_screen import SCREEN_IMAGE, TARGET_BYTES, LocalScreen
from lab.vm import VM
from mind.initiative import InitiativePolicy, QuietHours


class InitiativeInstall(LocalScreen):
    def type_line(self, value):
        for char in value:
            key = {" ": "spc", "/": "slash", ":": "semicolon"}.get(char, char.lower())
            names = ["shift", key] if char.isupper() or char == ":" else [key]
            self.vm.qmp("send-key", keys=[{"type": "qcode", "data": name} for name in names], **{"hold-time": 45})
            time.sleep(0.09)
        self.key("ret")

    def test_initiative_policy_survives_install_and_reboot(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="being-initiative-install-", dir="/tmp")))
        target = folder / "target.raw"
        with target.open("xb") as disk:
            disk.truncate(TARGET_BYTES)
        blank = target.stat()
        expected = InitiativePolicy(reminders=False, greetings=True, quiet_hours=QuietHours("22:00", "07:00"))

        self.start(target)
        self.assertIn("being.screen=1", self.sh("cat /proc/cmdline").split())
        print(f"Try image: {SCREEN_IMAGE}", flush=True)
        print(self.sh("sha256sum /usr/lib/being/mind/initiative*.py /usr/lib/being/mind/local_menu.py"
                      " /usr/lib/being/mind/install_apply.py /usr/lib/being/mind/turn.py"
                      " /usr/bin/being-brainstem"), flush=True)
        menu = self.console("initiative-01-menu")
        self.select("4")
        setup = self.console(previous=menu)
        self.select("9")
        self.console("initiative-02-controls", previous=setup)
        self.select("1")
        self.console()
        self.select("2")
        self.console()
        self.select("3")
        self.console()
        self.type_line(expected.quiet_hours.start)
        self.console()
        self.type_line(expected.quiet_hours.end)
        self.console("initiative-03-draft")
        self.select("k")
        self.console()
        policy = {"version": 1, **asdict(expected)}
        self.assertEqual(json.loads(self.sh("cat /mind/initiative.json")), policy)
        self.sh("test ! -e /mind/profile.json && touch /mind/initiative-try-only")
        self.assertIn(" - tmpfs ", self.sh("awk '$5 == \"/mind\"' /proc/self/mountinfo"))
        self.assertEqual(target.stat().st_blocks, blank.st_blocks, "Try choices modified the destination")
        print(f"Saved Try settings using the display: {policy}", flush=True)

        self.select("q")
        self.console()
        report = json.loads(self.sh("being-installer --list --json"))
        eligible = [disk for disk in report["disks"] if disk["eligible"]]
        self.assertEqual(len(eligible), 1, report)
        device, = eligible
        self.assertEqual((device["model"], device["size_bytes"]), ("BEINGOS SCREEN", TARGET_BYTES))
        self.assertRegex(device["device"], r"^/dev/sd[a-z]+$")
        self.select("2")
        disks = self.console()
        self.select("1")
        keep = self.console("initiative-04-carry-choices", previous=disks)
        self.select("y")
        self.console("initiative-05-confirm-install", previous=keep)
        print(f"Installing onto disposable {device['device']} with the selected Try settings", flush=True)
        self.type_line(f"ERASE {device['device']}")
        deadline = time.monotonic() + 240
        with target.open("rb", buffering=0) as disk:
            while time.monotonic() < deadline:
                disk.seek(512)
                if disk.read(8) == b"EFI PART":
                    break
                time.sleep(0.5)
            else:
                self.screenshot("initiative-install-timeout", self.vm.screen())
                self.fail("The screen installer did not finish writing the disposable target")
        self.console("initiative-06-install-complete")
        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(90), "Try guest did not power off")
        self.vm.destroy()

        self.vm = VM(image=target)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(120)
        self.sh("test ! -e /mind/initiative-try-only")
        print("Booted the installed disk without the Try USB", flush=True)
        report = verify_installed_initiative(self.vm, expected)
        print(json.dumps(report, indent=2), flush=True)
        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(90), "installed guest did not power off")


if __name__ == "__main__":
    def interrupted(signum, _frame):
        raise InterruptedError(f"Installed initiative QA interrupted by signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    suite = unittest.TestSuite([InitiativeInstall("test_initiative_policy_survives_install_and_reboot")])
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
