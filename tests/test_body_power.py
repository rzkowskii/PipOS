"""Host checks for the laptop's power and watchdog settings.

Measured on the MacBookPro9,2 over the cable (25 November 2026): the
chipset's TCO watchdog is disabled by the firmware, so systemd's
RuntimeWatchdogSec had no device to feed. These tests keep the kernel
fragment, the systemd setting and the manual in agreement.
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "os/board/beingos"


def options(path):
    found = {}
    for line in path.read_text().splitlines():
        if match := re.match(r"^(CONFIG_\w+)=(.*)$", line):
            found[match[1]] = match[2]
        elif match := re.match(r"^# (CONFIG_\w+) is not set$", line):
            found[match[1]] = "n"
    return found


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.lab = options(BOARD / "linux.config")
        self.laptop = {**self.lab, **options(BOARD / "linux-laptop.config")}

    def test_the_laptop_has_a_watchdog_device_for_systemd_to_feed(self):
        settings = (BOARD / "watchdog.conf").read_text()
        self.assertRegex(settings, r"(?m)^RuntimeWatchdogSec=30s$")
        self.assertEqual(self.laptop.get("CONFIG_WATCHDOG"), "y")
        # The firmware disables iTCO_wdt on this Mac: softdog is the device.
        self.assertEqual(self.laptop.get("CONFIG_SOFT_WATCHDOG"), "y")
        # The TCO driver stays, for hardware where it works (it registers first).
        self.assertEqual(self.laptop.get("CONFIG_ITCO_WDT"), "y")

    def test_the_lab_keeps_its_emulated_watchdog_and_no_softdog(self):
        self.assertEqual(self.lab.get("CONFIG_I6300ESB_WDT"), "y")
        self.assertNotEqual(self.lab.get("CONFIG_SOFT_WATCHDOG"), "y")
        self.assertEqual(self.laptop.get("CONFIG_I6300ESB_WDT"), "n")

    def test_no_lockup_detector_turns_a_hang_into_a_reboot_loop(self):
        # The laptop never panics on an oops (slots.sh); a lockup panic would
        # be the same endless loop with nothing on the screen.
        for option in ("CONFIG_BOOTPARAM_SOFTLOCKUP_PANIC", "CONFIG_BOOTPARAM_HARDLOCKUP_PANIC",
                       "CONFIG_BOOTPARAM_HUNG_TASK_PANIC"):
            self.assertNotEqual(self.laptop.get(option), "y", option)
        self.assertNotIn("SOFT_PANIC", (BOARD / "linux-laptop.config").read_text())

    def test_the_fragment_and_the_manual_do_not_claim_a_hardware_watchdog(self):
        fragment = (BOARD / "linux-laptop.config").read_text()
        self.assertNotIn("iTCO watchdog) is untouched", fragment)
        self.assertIn("NO_REBOOT", fragment)
        manual = (ROOT / "docs/manual/ARCHITECTURE.md").read_text()
        self.assertNotIn("| Hardware watchdog | systemd feeds it", manual)
        row = next(line for line in manual.splitlines() if line.startswith("| System watchdog |"))
        for fact in ("RuntimeWatchdogSec=30s", "iTCO_wdt", "softdog", "NO_REBOOT", "interrupts off"):
            self.assertIn(fact, row)

    def test_the_watchdog_in_use_can_be_named(self):
        # pip-diag and the hardware report read /sys/class/watchdog/watchdog0/identity.
        self.assertEqual(self.laptop.get("CONFIG_WATCHDOG_SYSFS"), "y")

    def test_the_manual_names_the_health_watch_and_what_it_will_not_do(self):
        manual = (ROOT / "docs/manual/ARCHITECTURE.md").read_text()
        row = next(line for line in manual.splitlines() if line.startswith("| Health watch |"))
        for fact in ("being-health-watch.service", "10 minutes for the brainstem", "5 for the face",
                     "Try and verbose boots only log", "ExecCondition"):
            self.assertIn(fact, row)
        script = (ROOT / "os/package/being/being-health-watch").read_text()
        self.assertIn("being-brainstem.service:600 being-face.service:300", script)
        self.assertIn("every=${BEING_HEALTH_EVERY:-10}", script)

    def test_the_laptop_kernel_arguments_keep_the_keys_and_the_msr_guard(self):
        # Other arguments may be added once measured (an HDA position fix, an
        # ASPM policy); only these must stay, and nothing may turn a hang or
        # an oops into a reboot loop, or switch the system watchdog off.
        slots = (BOARD / "slots.sh").read_text()
        line = re.search(r"(?m)^being_laptop_args='([^']*)'$", slots)
        self.assertIsNotNone(line, "being_laptop_args is a single-quoted line in slots.sh")
        args = line[1].split()
        self.assertIn("hid_apple.fnmode=1", args)
        self.assertIn("msr.allow_writes=off", args)
        for arg in args:
            name = arg.split("=", 1)[0]
            self.assertNotIn(name, {"oops", "panic_on_warn", "softlockup_panic", "hardlockup_panic",
                                    "hung_task_panic", "nowatchdog", "nmi_watchdog", "systemd.watchdog_sec",
                                    "iTCO_wdt.heartbeat", "softdog.soft_panic"}, arg)


class PowerUeventTests(unittest.TestCase):
    """A pack that goes while BAT0 stays listed is seen from the uevent."""

    STAMP = "run/being-power-changed"

    def test_every_power_supply_change_touches_the_stamp_both_pollers_watch(self):
        rules = (ROOT / "os/package/being/60-being-power.rules").read_text().splitlines()
        lines = [line for line in rules if line.strip() and not line.startswith("#")]
        stamp = [line for line in lines if "being-power-changed" in line]
        self.assertEqual(stamp, ['SUBSYSTEM=="power_supply", ACTION=="change", '
                                 f'RUN+="/bin/touch /{self.STAMP}"'])
        # After the USB section's end label, so its ACTION!="add" GOTO cannot skip it.
        self.assertGreater(lines.index(stamp[0]), lines.index('LABEL="being_power_end"'))
        for source in ("crates/bodycare/src/hw.rs", "crates/brainstem/src/body.rs"):
            self.assertIn(f'POWER_STAMP: &str = "{self.STAMP}"', (ROOT / source).read_text(), source)

    def test_a_usb_flash_stick_is_not_flagged_as_a_spinning_disk(self):
        rules = (ROOT / "os/package/being/60-being-power.rules").read_text().splitlines()
        lines = [line for line in rules if line.strip() and not line.startswith("#")]
        rotational = [line for line in lines if "rotational" in line]
        self.assertEqual(rotational, ['ACTION=="add|change", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", '
                                      'SUBSYSTEMS=="usb", ATTR{removable}=="1", ATTR{queue/rotational}=="1", '
                                      'ATTR{queue/rotational}="0"'])
        # After the USB-device section, whose GOTOs skip anything that is not a USB device.
        self.assertGreater(lines.index(rotational[0]), lines.index('LABEL="being_power_end"'))
        self.assertNotIn("ID_BUS", rotational[0], "60-persistent-storage.rules sets it after this file")

    def test_unmeasured_power_rules_wait_for_the_live_test(self):
        # Item 15: nothing ships before power-live-test.sh says it helps.
        rules = (ROOT / "os/package/being/60-being-power.rules").read_text()
        active = "\n".join(line for line in rules.splitlines() if not line.startswith("#"))
        for candidate in ("820a|820b", "0x1e2d", "0x5901", "0x16bc", "0x1513"):
            self.assertIn(candidate, rules, candidate)
            self.assertNotIn(candidate, active, f"{candidate} is not measured yet")
        for step in ("bt-hid-autosuspend", "usb-hosts-auto", "driverless-pci-auto"):
            self.assertIn(f"{step}", rules)
        live = (ROOT / "out/hardware-logs/cable-20261125/power-live-test.sh")
        if live.exists():
            for step in ("bt-hid-autosuspend", "usb-hosts-auto", "driverless-pci-auto", "aspm-powersave"):
                self.assertIn(f'("{step}",', live.read_text(), "the TODOs name real steps")
        config = (BOARD / "linux-laptop.config").read_text()
        self.assertNotIn("\nCONFIG_THUNDERBOLT=", config)
        self.assertIn("TODO(power-live-test)", config)
        slots = (BOARD / "slots.sh").read_text()
        self.assertNotIn("pcie_aspm.policy=", re.search(r"(?m)^being_laptop_args='([^']*)'$", slots)[1])

    def test_the_rules_file_is_installed(self):
        mk = (ROOT / "os/package/being/being.mk").read_text()
        self.assertIn("/usr/lib/udev/rules.d/60-being-power.rules", mk)

if __name__ == "__main__":
    unittest.main()
