import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mind import usb_logs

REPO = Path(__file__).resolve().parents[1]


class FakeMachine(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def link(self, relative, target):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)

    def disk(self, name, bus, partitions, root_index=None):
        """A sysfs disk with GPT partition names, and optionally the root mount on one."""
        disk = f"sys/devices/pci0000:00/{bus}/block/{name}"
        self.write(f"{disk}/size", "1000000\n")
        for index, partname in enumerate(partitions, 1):
            part = f"{disk}/{name}{index}"
            self.write(f"{part}/partition", f"{index}\n")
            self.write(f"{part}/dev", f"8:{index}\n")
            self.write(f"{part}/uevent", f"MAJOR=8\nMINOR={index}\nDEVNAME={name}{index}\nPARTN={index}\nPARTNAME={partname}\n")
            self.link(f"sys/dev/block/8:{index}" if root_index else f"sys/dev/block/9:{index}", self.root / part)
        if root_index:
            self.write("proc/self/mountinfo",
                       f"22 1 8:{root_index} / / ro,relatime - squashfs /dev/root ro\n"
                       "23 22 0:21 / /run rw - tmpfs tmpfs rw\n")
        self.link(f"sys/class/block/{name}", self.root / disk)


class FindPartition(FakeMachine):
    def test_finds_try_logs_on_the_usb_boot_disk(self):
        self.disk("sdb", "0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0",
                  ["try-esp", "try-system", "try-models", "try-logs"], root_index=2)
        self.write("dev/sdb4", "")
        device, part, disk = usb_logs.find_partition(
            self.root, stat=lambda path: SimpleNamespace(st_rdev=os.makedev(8, 4)))
        self.assertEqual(device, self.root / "dev/sdb4")
        self.assertEqual(disk, "sdb")

    def test_refuses_a_boot_disk_not_on_usb(self):
        self.disk("sda", "0000:00:1f.2/ata1/host0/target0:0:0/0:0:0:0",
                  ["try-esp", "try-system", "try-models", "try-logs"], root_index=2)
        with self.assertRaisesRegex(ValueError, "not attached over USB"):
            usb_logs.find_partition(self.root)

    def test_ignores_try_logs_on_another_disk(self):
        # The internal SSD has a partition with the same name; the boot stick has none.
        self.disk("sdb", "0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0",
                  ["try-esp", "try-system", "try-models"], root_index=2)
        self.disk("sda", "0000:00:1f.2/ata1/host0/target0:0:0/0:0:0:0", ["esp", "try-logs"])
        with self.assertRaisesRegex(ValueError, "has 0 try-logs partitions"):
            usb_logs.find_partition(self.root)

    def test_refuses_a_device_node_that_is_not_the_partition(self):
        self.disk("sdb", "0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0",
                  ["try-esp", "try-system", "try-models", "try-logs"], root_index=2)
        self.write("dev/sdb4", "")   # a regular file: rdev 0:0
        with self.assertRaisesRegex(ValueError, "is not the try-logs partition"):
            usb_logs.find_partition(self.root)

    def test_needs_a_root_mount(self):
        with self.assertRaisesRegex(ValueError, "root file system"):
            usb_logs.find_partition(self.root)


class FatLabel(FakeMachine):
    def sector(self, fat32=True, label=b"PIPLOGS    "):
        data = bytearray(512)
        if fat32:
            data[71:82], data[82:90] = label, b"FAT32   "
        else:
            data[43:54], data[54:62] = label, b"FAT16   "
        data[510:512] = b"\x55\xaa"
        path = self.root / "part"
        path.write_bytes(bytes(data))
        return path

    def test_reads_fat32_and_fat16_labels(self):
        self.assertEqual(usb_logs.fat_label(self.sector()), "PIPLOGS")
        self.assertEqual(usb_logs.fat_label(self.sector(fat32=False, label=b"OTHER      ")), "OTHER")

    def test_rejects_other_file_systems(self):
        path = self.root / "ext4"
        path.write_bytes(bytes(1024))
        with self.assertRaisesRegex(ValueError, "no FAT boot sector"):
            usb_logs.fat_label(path)


class Folders(FakeMachine):
    def test_numbers_boots_and_keeps_counting(self):
        number, first = usb_logs.new_folder(self.root, 0)
        self.assertEqual((number, first.name), (1, "boot-0001-19700101-000000Z"))
        (self.root / "boot-0009-20261124-000000Z").mkdir()
        (self.root / "boot-notes").mkdir()
        number, later = usb_logs.new_folder(self.root, 86400)
        self.assertEqual(number, 10)
        self.assertTrue(later.name.startswith("boot-0010-19700102"))

    def test_prunes_oldest_only_when_short_and_keeps_newest(self):
        names = [f"boot-{n:04d}-x" for n in range(1, 9)]
        for name in names:
            (self.root / name).mkdir()
        current = self.root / names[-1]
        self.assertEqual(usb_logs.prune(self.root, current, low=10, free=lambda _: 100), [])
        space = iter([0, 0, 100])
        removed = usb_logs.prune(self.root, current, low=10, keep=5, free=lambda _: next(space))
        self.assertEqual(removed, names[:2])
        always_full = usb_logs.prune(self.root, current, low=10, keep=5, free=lambda _: 0)
        self.assertEqual(always_full, names[2:3])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), names[3:])

    def test_write_file_replaces_atomically(self):
        path = self.root / "summary.txt"
        usb_logs.write_file(path, "one\n")
        usb_logs.write_file(path, "two\n")
        self.assertEqual(path.read_text(), "two\n")
        self.assertEqual([p.name for p in self.root.iterdir()], ["summary.txt"])


class Reports(FakeMachine):
    def macbook(self):
        self.write("proc/cpuinfo", "processor\t: 0\nmodel name\t: Intel(R) Core(TM) i5-3210M CPU @ 2.50GHz\n"
                   "flags\t\t: fpu sse avx f16c\n\nprocessor\t: 1\nmodel name\t: x\n")
        self.write("proc/meminfo", "MemTotal:       16303008 kB\n")
        self.write("sys/class/dmi/id/sys_vendor", "Apple Inc.\n")
        self.write("sys/class/dmi/id/product_name", "MacBookPro9,2\n")
        self.write("sys/class/dmi/id/bios_version", "MBP91.88Z.F000.B00.1904121230\n")
        self.write("sys/class/dmi/id/product_serial", "C02SECRET\n")
        self.write("sys/firmware/efi/fw_platform_size", "64\n")
        for slot, vendor, device, cls, driver in (
                ("0000:00:02.0", "0x8086", "0x0166", "0x030000", "i915"),
                ("0000:02:00.0", "0x14e4", "0x4331", "0x028000", "bcma-pci-bridge"),
                ("0000:01:00.0", "0x14e4", "0x16b4", "0x020000", "tg3")):
            base = f"sys/bus/pci/devices/{slot}"
            self.write(f"{base}/vendor", vendor)
            self.write(f"{base}/device", device)
            self.write(f"{base}/class", cls)
            self.write(f"sys/bus/pci/drivers/{driver}/.keep", "")
            self.link(f"{base}/driver", self.root / f"sys/bus/pci/drivers/{driver}")
        # The 13-inch MacBookPro9,2 panel is LVDS, not eDP.
        self.write("sys/class/drm/card0-LVDS-1/status", "connected\n")
        self.write("sys/class/drm/card0-LVDS-1/modes", "1280x800\n")
        self.write("sys/class/drm/card0-LVDS-1/dpms", "On\n")
        self.write("proc/asound/cards", " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n"
                   "                      HDA Intel PCH at 0xc0600000 irq 32\n")
        self.write("proc/asound/card0/codec#0", "Codec: Cirrus Logic CS4206\nAddress: 0\n")
        self.write("proc/bus/input/devices",
                   'I: Bus=0019\nN: Name="Lid Switch"\nH: Handlers=event0\n\n'
                   'I: Bus=0003\nN: Name="Apple Inc. Apple Internal Keyboard / Trackpad"\nH: Handlers=sysrq kbd event4\n')
        self.write("proc/acpi/button/lid/LID0/state", "state:      open\n")
        self.write("sys/class/power_supply/BAT0/uevent", "POWER_SUPPLY_TYPE=Battery\nPOWER_SUPPLY_CAPACITY=81\n"
                   "POWER_SUPPLY_STATUS=Discharging\nPOWER_SUPPLY_SERIAL_NUMBER=SECRET\n")
        self.write("sys/class/power_supply/ADP1/uevent", "POWER_SUPPLY_TYPE=Mains\nPOWER_SUPPLY_ONLINE=0\n")
        self.write("sys/class/hwmon/hwmon1/name", "applesmc\n")
        self.write("sys/class/hwmon/hwmon1/fan1_input", "2000\n")
        self.write("sys/class/hwmon/hwmon1/temp1_input", "45000\n")
        self.write("sys/class/video4linux/video0/name", "FaceTime HD Camera (Built-in)\n")
        self.write("sys/class/net/wlan0/address", "a8:20:66:12:34:56\n")
        self.write("sys/class/net/wlan0/wireless/.keep", "")
        self.write("lib/firmware/b43/ucode29_mimo.fw", "x")
        self.write("proc/uptime", "12.5 10.0\n")

    def test_summary_answers_the_first_questions(self):
        self.macbook()
        notable = {"renderer": ["[  14.1] being-face[310]: face: drawn by Mesa Intel(R) HD Graphics 4000 (IVB GT2)"],
                   "failed": ["being-voice.service: Failed with result 'exit-code'."]}
        text = usb_logs.summary(self.root, boot="#3", notable=notable, journal_bytes=4096, failed_units="")
        lines = dict(re.match(r"(.+?):\s+(.*)", line).groups() for line in text.splitlines()[3:24] if ":" in line)
        self.assertEqual(lines["Machine"], "Apple Inc. MacBookPro9,2")
        self.assertIn("i5-3210M", lines["CPU"])
        self.assertNotIn("AVX2", lines["CPU"])
        self.assertEqual(lines["GPU"], "Intel HD Graphics 4000 (Ivy Bridge) driver=i915")
        self.assertEqual(lines["GL renderer"], "Mesa Intel(R) HD Graphics 4000 (IVB GT2)")
        self.assertIn("HDA Intel PCH", lines["Audio"])
        self.assertIn("Cirrus Logic CS4206", lines["Audio codecs"])
        self.assertEqual(lines["Lid switch"], "found (Lid Switch), state open")
        self.assertEqual(lines["Battery"], "found BAT0, 81% Discharging")
        self.assertEqual(lines["Camera"], "FaceTime HD Camera (Built-in)")
        self.assertIn("BCM4331", lines["Wi-Fi chip"])
        self.assertEqual(lines["Wi-Fi firmware"], "1 b43 files, interface wlan0")
        self.assertIn("tg3", lines["Ethernet"])
        self.assertEqual(lines["Memory"], "15920 MiB")
        self.assertIn("Failures and restarts", text)
        self.assertNotIn("SECRET", text)

    def test_summary_says_what_is_missing_on_an_empty_machine(self):
        text = usb_logs.summary(self.root)
        for expected in ("Lid switch:     NOT found", "Battery:        NOT found", "Camera:         NOT found",
                         "Audio:          NO sound card", "EFI boot:       NO"):
            self.assertIn(expected, text)

    def test_hardware_report_has_every_section_and_hides_serials_and_addresses(self):
        self.macbook()
        text = usb_logs.hardware_report(self.root)
        for title in ("cpu", "pci (sysfs)", "usb (sysfs)", "drm", "alsa cards", "input devices",
                      "hwmon (applesmc, coretemp, ...)", "power supplies", "backlight", "network interfaces",
                      "block devices (read from sysfs only)", "memory", "loaded modules", "firmware (DMI/SMBIOS)"):
            self.assertIn(f"===== {title} =====", text)
        self.assertIn("MBP91.88Z", text)
        self.assertIn("a8:20:66:xx:xx:xx", text)
        self.assertNotIn("12:34:56", text)
        self.assertNotIn("SECRET", text)

    def test_report_survives_an_empty_root(self):
        self.assertIn("===== kernel =====", usb_logs.hardware_report(self.root))


class Linux618Layout(Reports):
    """applesmc on Linux 6.18: a nameless hwmon whose attributes sit on the platform device."""

    def smc(self, keys):
        base = "sys/devices/platform/applesmc.768"
        self.write(f"sys/bus/platform/drivers/applesmc/.keep", "")
        self.link(f"{base}/driver", self.root / "sys/bus/platform/drivers/applesmc")
        self.write(f"{base}/fan1_input", "1998\n")
        self.write(f"{base}/fan1_min", "2000\n")
        self.write(f"{base}/temp5_input", "31000\n")
        self.write(f"{base}/temp5_label", "TB0T\n")
        self.write(f"{base}/key_count", f"{len(keys)}\n")
        self.write(f"{base}/key_at_index", "0\n")
        self.write(f"{base}/hwmon/hwmon3/power/.keep", "")
        self.link("sys/class/hwmon/hwmon3", self.root / f"{base}/hwmon/hwmon3")
        self.link(f"{base}/hwmon/hwmon3/device", self.root / base)
        return self.root / base

    def test_nameless_hwmon_is_found_and_the_summary_no_longer_says_not_found(self):
        self.macbook()
        for name in ("name", "fan1_input", "temp1_input"):
            (self.root / "sys/class/hwmon/hwmon1" / name).unlink()
        self.smc([])
        text = usb_logs.summary(self.root)
        self.assertIn("applesmc:       found at sys/devices/platform/applesmc.768", text)
        self.assertIn('"fan1_input": "1998"', text)
        sensors = {sensor["name"]: sensor for sensor in usb_logs.hwmon(self.root)}
        self.assertIn("temp5_input (TB0T)", sensors["applesmc"]["readings"])

    def test_smc_keys_are_listed_and_only_bclm_and_bfcl_are_read(self):
        keys = ["#KEY", "BCLM", "F0Mn", "TB0T", "CH0B", "BFCL", "XXXX"]
        base = self.smc(keys)
        reads = []
        real_open = open

        def fake_open(path, mode="r", *args, **kwargs):
            path = Path(path)
            if path.parent == base and path.name == "key_at_index" and "w" in mode:
                class Selector:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *_):
                        return False

                    def write(self_inner, value):
                        name = keys[int(value)]
                        (base / "key_at_index_name").write_text(name + "\n")
                        (base / "key_at_index_type").write_text("ui8\n")
                        (base / "key_at_index_data_length").write_text("1\n")
                        (base / "key_at_index_data").write_bytes(b"\x50" if name == "BCLM" else b"\x01")
                return Selector()
            if path.name == "key_at_index_data":
                reads.append((base / "key_at_index_name").read_text().strip())
            return real_open(path, mode, *args, **kwargs)

        with patch("builtins.open", fake_open):
            found = usb_logs.smc_keys(self.root)
        self.assertEqual(found["key_count"], 7)
        self.assertEqual([entry.split()[0] for entry in found["keys"]], ["BCLM", "F0Mn", "TB0T", "CH0B", "BFCL"])
        self.assertEqual(found["values"], {"BCLM": "50", "BFCL": "01"})
        self.assertEqual(reads, ["BCLM", "BFCL"], "data is read for BCLM and BFCL only")

    def test_power_battery_and_connector_details(self):
        self.macbook()
        self.write("sys/class/power_supply/BAT0/uevent",
                   "POWER_SUPPLY_TYPE=Battery\nPOWER_SUPPLY_CYCLE_COUNT=812\nPOWER_SUPPLY_CHARGE_FULL=4100000\n"
                   "POWER_SUPPLY_CHARGE_FULL_DESIGN=5770000\nPOWER_SUPPLY_SERIAL_NUMBER=SECRET\n")
        self.write("sys/devices/system/cpu/cpufreq/policy0/scaling_driver", "intel_cpufreq\n")
        self.write("sys/devices/system/cpu/intel_pstate/no_turbo", "0\n")
        self.write("sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count", "3\n")
        self.write("sys/class/powercap/intel-rapl:0/name", "package-0\n")
        self.write("sys/class/powercap/intel-rapl:0/constraint_0_name", "long_term\n")
        self.write("sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw", "35000000\n")
        health = usb_logs.battery_health(self.root)[0]
        self.assertEqual((health["cycle_count"], health["health_percent"]), ("812", 71.1))
        power = usb_logs.cpu_power(self.root)
        self.assertEqual(power["scaling_driver"], "intel_cpufreq")
        self.assertEqual(power["rapl"]["intel-rapl:0"]["long_term"]["limit_uw"], "35000000")
        self.assertEqual(power["thermal_throttle"]["cpu0"]["package_throttle_count"], "3")
        self.assertEqual(usb_logs.connector_details(self.root)[0]["type"], "LVDS")
        text = usb_logs.hardware_report(self.root)
        self.assertIn("===== battery health =====", text)
        self.assertNotIn("SECRET", text)
        summary = usb_logs.summary(self.root)
        self.assertIn("Battery health: 71.1% of design", summary)

    def test_clamp_probe_flags_a_held_clock(self):
        self.write("sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "1197000\n")
        process = SimpleNamespace(wait=lambda *_: 0, kill=lambda: None)
        result = usb_logs.clamp_probe(self.root, 0.5, spawn=lambda: process, sleep=lambda _: None)
        self.assertTrue(result["clamped"])
        self.assertEqual(result["peak_mhz"], 1197)

    def test_blank_check_restores_the_backlight(self):
        self.write("sys/class/backlight/gmux_backlight/bl_power", "0\n")
        self.write("sys/class/backlight/gmux_backlight/actual_brightness", "800\n")
        seen = []
        result = usb_logs.blank_check(self.root, sleep=lambda _: seen.append(
            (self.root / "sys/class/backlight/gmux_backlight/bl_power").read_text()))
        self.assertEqual(seen, ["4\n"])
        self.assertEqual(result[0]["bl_power_after"], "0")


class Privacy(FakeMachine):
    def test_scrub_hides_serials_addresses_and_speech(self):
        line = ('usb 1-2: SerialNumber: 0001ABC wlan0 a8:20:66:12:34:56 '
                '{"t": "hear.final", "text": "my bank pin is 4321"} Serial: 8250/16550 driver')
        text = usb_logs.scrub(line)
        for secret in ("0001ABC", "12:34:56", "4321"):
            self.assertNotIn(secret, text)
        self.assertIn("a8:20:66:xx:xx:xx", text)
        self.assertIn("Serial: 8250/16550 driver", text, "driver banners are kept")

    def test_scrub_hides_quoted_serials_whole_and_keeps_json_quotes(self):
        text = usb_logs.scrub('{"serial": "S1AB 2CD", "model": "APPLE SSD"} serial=ABC123 '
                              '"POWER_SUPPLY_SERIAL_NUMBER": "3X 99"')
        for secret in ("S1AB", "2CD", "ABC123", "3X", " 99"):
            self.assertNotIn(secret, text)
        self.assertIn('"serial": "[hidden]", "model": "APPLE SSD"', text)

    def test_journal_masks_addresses_split_across_reads_and_never_keeps_speech(self):
        canary = "violet-canary-7731"
        journal = usb_logs.Journal(self.root / "journal.txt", self.root / "cursor")
        journal.feed(b"wlan0: associated with a8:20:", free=10**9)
        journal.feed(b"66:12:34:56\n", free=10**9)
        journal.feed(('being-mind[9]: {"t": "say.request", "text": "' + canary + '"}\n').encode(), free=10**9)
        journal.feed(b"tail without newline 11:22:33:44:55:66", free=10**9)
        journal.stop()
        text = (self.root / "journal.txt").read_text()
        self.assertIn("a8:20:66:xx:xx:xx", text)
        self.assertNotIn("12:34:56", text)
        self.assertNotIn(canary, text)
        self.assertIn("11:22:33:xx:xx:xx", text)
        self.assertNotIn(canary, " ".join(sum(journal.notable.values(), [])))

    def test_owner_can_switch_try_diagnostics_off_and_on(self):
        folder = self.root / "stick/boot-0001-x"
        folder.mkdir(parents=True)
        state = self.root / "state"
        state.mkdir()
        (state / "folder").write_text(f"{folder}\n")
        calls = []
        with patch.object(usb_logs, "STATE_DIR", state):
            self.assertTrue(usb_logs.logging_enabled())
            usb_logs.set_logging(False, systemctl=lambda *args: calls.append(args))
            self.assertFalse(usb_logs.logging_enabled())
            self.assertTrue((folder.parent / usb_logs.LOGGING_OFF).exists())
            usb_logs.set_logging(True, systemctl=lambda *args: calls.append(args))
            self.assertTrue((state / "enable").exists())
        self.assertEqual(calls, [("stop", "being-usb-logs.service"), ("restart", "being-usb-logs.service")])

    def test_keys_are_captured_from_evdev_records(self):
        import struct

        reader, writer = os.pipe()
        events = b"".join(struct.pack(usb_logs.EVENT, 0, 0, 1, code, 1) for code in (59, 225, 999))
        events += struct.pack(usb_logs.EVENT, 0, 0, 1, 59, 0)  # a release is not a press
        os.write(writer, events)
        os.close(writer)
        ticks = iter([0.0, 0.0, 0.1, 0.2, 5.0, 5.0, 5.0])
        found = usb_logs.capture_keys(1.0, paths=[f"/dev/fd/{reader}"], clock=lambda: next(ticks, 9.0))
        os.close(reader)
        self.assertEqual(found["pressed"], ["F1", "BrightnessUp", "code 999"])


class JournalCopy(FakeMachine):
    def test_copies_scans_and_stops_at_the_cap(self):
        journal = usb_logs.Journal(self.root / "journal.txt", self.root / "cursor", cap=64)
        journal.feed(b"[ 1.0] kernel: b43-phy0 ERROR: Firmware file not found\n", free=10**9)
        journal.sync(force=True)
        self.assertEqual(journal.notable["firmware"][0], "[ 1.0] kernel: b43-phy0 ERROR: Firmware file not found")
        journal.feed(b"x" * 100 + b"\n", free=10**9)
        self.assertTrue(journal.stopped)
        text = (self.root / "journal.txt").read_text()
        self.assertIn("size cap reached", text)
        self.assertNotIn("x" * 100, text)

    def test_stops_when_the_stick_is_nearly_full(self):
        journal = usb_logs.Journal(self.root / "journal.txt", self.root / "cursor")
        journal.feed(b"line\n", free=1)
        self.assertIn("stick nearly full", (self.root / "journal.txt").read_text())


class SaveCheck(FakeMachine):
    def test_does_nothing_without_a_stick(self):
        with patch.object(usb_logs, "STATE_DIR", self.root / "missing"):
            self.assertIsNone(usb_logs.save_check({"model": "x"}))

    def test_saves_into_this_boots_folder(self):
        folder = self.root / "stick/boot-0001-x"
        folder.mkdir(parents=True)
        self.write("state/folder", f"{folder}\n")
        with patch.object(usb_logs, "STATE_DIR", self.root / "state"):
            path = usb_logs.save_check({"model": "MacBookPro9,2"})
        self.assertEqual(path.parent, folder)
        self.assertIn("MacBookPro9,2", path.read_text())


class Serve(unittest.TestCase):
    def test_not_a_try_boot_touches_nothing(self):
        with patch.object(usb_logs, "read", return_value="root=PARTUUID=x ro"), \
                patch.object(usb_logs, "find_partition") as find:
            self.assertEqual(usb_logs.serve(), 0)
        find.assert_not_called()

    def test_missing_partition_is_quiet_success(self):
        with patch.object(usb_logs, "read", return_value="being.try=1"), \
                patch.object(usb_logs, "find_partition", side_effect=ValueError("none")), \
                patch.object(usb_logs, "mount") as mount:
            self.assertEqual(usb_logs.serve(), 0)
        mount.assert_not_called()


class Packaging(unittest.TestCase):
    def test_unit_is_try_only_and_early(self):
        unit = (REPO / "os/package/being/being-usb-logs.service").read_text()
        self.assertIn("ConditionKernelCommandLine=being.try=1", unit)
        self.assertIn("DefaultDependencies=no", unit)
        self.assertIn("WantedBy=sysinit.target", unit)
        self.assertIn("Conflicts=shutdown.target", unit)

    def test_installed_by_the_package(self):
        recipe = (REPO / "os/package/being/being.mk").read_text()
        for name in ("being-usb-logs", "being-hardware-report", "being-usb-logs.service"):
            self.assertIn(f"$(BEING_PKGDIR)/{name} ", recipe)

    def test_logs_partition_is_laptop_only(self):
        config = (REPO / "os/board/beingos/try-genimage.cfg").read_text()
        lab = subprocess.run(["sed", "/^#BEGIN try-logs/,/^#END try-logs/d"], input=config,
                             capture_output=True, text=True, check=True).stdout
        self.assertNotIn("try-logs", lab)
        self.assertNotIn("logs-part", lab)
        self.assertEqual(re.findall(r"partition (\S+) \{", lab), ["try-esp", "try-system", "try-models"])
        self.assertEqual(re.findall(r"partition (\S+) \{", config),
                         ["try-esp", "try-system", "try-models", "try-logs"])
        self.assertIn('label = "PIPLOGS"', config)
        script = (REPO / "os/board/beingos/try-image.sh").read_text()
        self.assertIn('sed \'/^#BEGIN try-logs/,/^#END try-logs/d\'', script)


if __name__ == "__main__":
    unittest.main()
