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
        lines = dict(re.match(r"(.+?):\s+(.*)", line).groups() for line in text.splitlines()[3:30] if ":" in line)
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

    def codec_machine(self, dump_coef="-1"):
        if not (self.root / "proc/cpuinfo").exists():
            self.macbook()
        self.write("proc/asound/card0/codec#3", "Codec: Intel PantherPoint HDMI\nAddress: 3\n")
        if dump_coef is not None:
            self.write("sys/module/snd_hda_codec/parameters/dump_coef", f"{dump_coef}\n")
        base = "sys/bus/hdaudio/devices/hdaudioC0D0"
        for name, value in (("vendor_id", "0x10134206"), ("subsystem_id", "0x106b5200"),
                            ("revision_id", "0x100302"), ("chip_name", "CS4206")):
            self.write(f"{base}/{name}", f"{value}\n")
        self.write("sys/module/snd_hda_intel/parameters/model", "mbp81,(null),(null),(null)\n")
        self.write("sys/module/snd_hda_intel/parameters/power_save", "0\n")

    def test_whole_codec_dumps_with_the_coefficients_and_dump_coef_put_back(self):
        self.codec_machine()
        coef = self.root / "sys/module/snd_hda_codec/parameters/dump_coef"
        seen = []
        real_read = usb_logs.read

        def read(path, limit=usb_logs.READ_LIMIT):
            if "codec#" in str(path):
                seen.append(coef.read_text().strip())
            return real_read(path, limit)

        with patch.object(usb_logs, "read", read):
            dumps = usb_logs.codec_dumps(self.root)
        self.assertEqual(set(dumps), {"card0-codec0.txt", "card0-codec3.txt"})
        self.assertIn("Codec: Cirrus Logic CS4206\nAddress: 0", dumps["card0-codec0.txt"])
        self.assertIn("coefficients: on while read (was -1)", dumps["card0-codec0.txt"])
        self.assertEqual(seen, ["1", "1"], "dump_coef is on while the files are read")
        self.assertEqual(coef.read_text().strip(), "-1", "and put back afterwards")

    def test_codec_dumps_without_the_parameter_or_any_codec(self):
        self.assertEqual(usb_logs.codec_dumps(self.root), {})
        self.codec_machine(dump_coef=None)
        dumps = usb_logs.codec_dumps(self.root)
        self.assertIn("coefficients: no dump_coef parameter", dumps["card0-codec3.txt"])
        self.write("sys/module/snd_hda_codec/parameters/dump_coef", "1\n")
        self.assertIn("coefficients: already on", usb_logs.codec_dumps(self.root)["card0-codec0.txt"])

    def test_codec_ids_and_driver_parameters_in_the_summary_and_report(self):
        self.codec_machine()
        text = usb_logs.summary(self.root)
        self.assertIn("Codec IDs:      hdaudioC0D0 0x10134206 subsystem 0x106b5200 rev 0x100302", text)
        self.assertIn("HDA model:      mbp81", text)
        report = usb_logs.hardware_report(self.root)
        for title in ("hda codecs (sysfs ids)", "snd_hda_intel parameters", "snd_hda_codec parameters"):
            self.assertIn(f"===== {title} =====", report)
        self.assertIn('"power_save": "0"', report)
        self.write("sys/module/snd_hda_intel/parameters/model", "(null),(null)\n")
        self.assertIn("HDA model:      none given (default fixup)", usb_logs.summary(self.root))

    def test_summary_says_whether_this_was_a_cable_or_voice_debug_boot(self):
        self.write("proc/cmdline", "BOOT_IMAGE=/bzImage being.try=1 quiet\n")
        with patch.dict(os.environ, {"BEING_VOICE_DEBUG": ""}):
            text = usb_logs.summary(self.root)
            self.assertIn("Cable link:     off", text)
            self.assertIn("Voice debug:    off", text)
            self.write("proc/cmdline", "being.try=1 being.cable=1 being.voice_debug=1\n")
            text = usb_logs.summary(self.root)
            self.assertIn("(CABLE DEVELOPMENT BOOT)", text.splitlines()[0])
            self.assertIn("Cable link:     ON: a cable development boot", text)
            self.assertIn("Voice debug:    ON", text)
            self.write("proc/cmdline", "being.try=1 being.cable=1 being.cable=0 being.voice_debug=1\n")
            self.assertIn("Cable link:     off", usb_logs.summary(self.root))
            self.write("proc/cmdline", "being.voice_debug=1\n")
            self.assertIn("Voice debug:    off", usb_logs.summary(self.root), "Try boots only")
            self.write("proc/cmdline", "being.try=1 being.voice_debug=1 quiet being.voice_debug=0\n")
            self.assertFalse(usb_logs.boot_flags(self.root, {})["voice_debug"], "the last one counts")
            self.write("proc/cmdline", "being.try=1 being.voice_debug=0 being.voice_debug=1\n")
            self.assertEqual(usb_logs.boot_flags(self.root, {})["voice_debug_from"], ["the kernel command line"])
        self.assertTrue(usb_logs.boot_flags(self.root, {"BEING_VOICE_DEBUG": "1"})["voice_debug"])

    def test_voice_debug_is_read_where_the_voice_gets_it(self):
        """being-usb-logs does not load /etc/being/env, the voice and the mind
        do: the summary reads the variable from there and from their drop-ins,
        and believes the journal if transcripts are already in it."""
        self.write("proc/cmdline", "BOOT_IMAGE=/bzImage being.try=1 being.cable=1 quiet\n")
        with patch.dict(os.environ, {"BEING_VOICE_DEBUG": ""}):
            self.assertIn("Voice debug:    off", usb_logs.summary(self.root))
            self.write("etc/being/env", "# workbench\nBEING_TTS=piper\nexport BEING_VOICE_DEBUG='1'\n")
            text = usb_logs.summary(self.root)
            self.assertIn("Voice debug:    ON: what the voice heard is written in journal.txt (from /etc/being/env)",
                          text)
            self.write("etc/being/env", "BEING_VOICE_DEBUG=1\nBEING_VOICE_DEBUG=0\n")
            self.assertIn("Voice debug:    off", usb_logs.summary(self.root), "the last assignment wins")
            self.write("run/systemd/system/being-voice.service.d/debug.conf",
                       '[Service]\nEnvironment="BEING_VOICE_DEBUG=1" OTHER=2\n')
            self.assertIn("(from being-voice.service.d/debug.conf)", usb_logs.summary(self.root))
            self.write("run/systemd/system/being-voice.service.d/debug.conf",
                       "[Service]\nEnvironment=BEING_VOICE_DEBUG=1\nEnvironment=\n")
            self.assertIn("Voice debug:    off", usb_logs.summary(self.root), "an empty Environment= clears it")
            text = usb_logs.summary(self.root, notable={"transcripts": ["seen"]})
            self.assertIn("(from transcripts seen in the journal)", text)

    def test_the_journal_notices_transcripts_without_keeping_them(self):
        journal = usb_logs.Journal(self.root / "journal.txt", self.root / "cursor")
        try:
            journal.scan(b"being-voice[1]: hello\n")
            self.assertNotIn("transcripts", journal.notable)
            journal.scan(b'being-voice[1]: heard: "open the terminal" (engine x, confidence 0.9)\n')
            self.assertEqual(journal.notable["transcripts"], ["seen"])
            self.assertNotIn("open the terminal", repr(journal.notable))
        finally:
            journal.stream.close()
        for line in ('wake phrase \'hey pip\', speech engine piper (x), transcript text in the log on (from x)',
                     'mind: route {"route": "model", "chars": 5, "text": "hello"}'):
            self.assertTrue(usb_logs.TRANSCRIPT_EVIDENCE.search(line), line)
        self.assertFalse(usb_logs.TRANSCRIPT_EVIDENCE.search("transcript text in the log off"))
        self.assertFalse(usb_logs.TRANSCRIPT_EVIDENCE.search('mind: route {"route": "model", "chars": 5}'))

    def test_kernel_features_without_zlib(self):
        self.write("proc/config.gz", "not really gzip")
        with patch.dict("sys.modules", {"gzip": None}):
            found = usb_logs.kernel_features(self.root)
        self.assertIn("no zlib", found["kernel config"])


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
        self.assertGreater(path.stat().st_size, 1024)
        self.assertEqual(list(folder.glob("*.new")), [], "written in place, not renamed")
        self.assertRegex(usb_logs.saved_line(path), r"^Saved \(\d+ KB\): ")

    def test_saved_is_said_only_once_the_file_has_a_size(self):
        folder = self.root / "stick/boot-0001-x"
        folder.mkdir(parents=True)
        self.write("state/folder", f"{folder}\n")
        synced = []
        with patch.object(usb_logs, "STATE_DIR", self.root / "state"), \
                patch.object(usb_logs, "hardware_report", lambda **_: "report\n"), \
                patch.object(usb_logs.os, "sync", lambda: synced.append(1)):
            path = usb_logs.save_check()
            self.assertEqual(synced, [1], "synced before it is measured")
            self.assertIsNotNone(path)
            with patch.object(usb_logs, "write_new_file", lambda path, text: path.write_bytes(b"") and 0):
                self.assertIsNone(usb_logs.save_check(), "an empty file is not a saved check")
        self.assertEqual(len(list(folder.glob("check-hardware-*.txt"))), 1, "the empty one is removed")
        self.assertTrue(usb_logs.saved_line(None).startswith("Not saved"))
        self.assertTrue(usb_logs.saved_line(folder / "gone.txt").startswith("Not saved"))


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


# What "amixer -c 0 scontents" shows on a MacBookPro9,2 (CS4206, codec SSID 106b:5200),
# from a public alsa-info dump of that model.
MBP92_SCONTENTS = """\
Simple mixer control 'Master',0
  Capabilities: pvolume pvolume-joined pswitch pswitch-joined
  Playback channels: Mono
  Limits: Playback 0 - 127
  Mono: Playback 112 [88%] [-7.50dB] [on]
Simple mixer control 'Headphone',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 127
  Mono:
  Front Left: Playback 0 [0%] [-57.50dB] [off]
  Front Right: Playback 0 [0%] [-57.50dB] [off]
Simple mixer control 'Speaker',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 127
  Mono:
  Front Left: Playback 115 [91%] [0.00dB] [on]
  Front Right: Playback 115 [91%] [0.00dB] [on]
Simple mixer control 'Bass Speaker',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 127
  Mono:
  Front Left: Playback 115 [91%] [0.00dB] [on]
  Front Right: Playback 115 [91%] [0.00dB] [on]
Simple mixer control 'PCM',0
  Capabilities: pvolume
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 255
  Mono:
  Front Left: Playback 255 [100%] [0.00dB]
  Front Right: Playback 255 [100%] [0.00dB]
Simple mixer control 'Mic Boost',0
  Capabilities: volume
  Playback channels: Front Left - Front Right
  Capture channels: Front Left - Front Right
  Limits: 0 - 3
  Front Left: 0 [0%] [0.00dB]
  Front Right: 0 [0%] [0.00dB]
Simple mixer control 'IEC958',0
  Capabilities: pswitch pswitch-joined
  Playback channels: Mono
  Mono: Playback [on]
Simple mixer control 'IEC958',1
  Capabilities: pswitch pswitch-joined
  Playback channels: Mono
  Mono: Playback [on]
Simple mixer control 'Capture',0
  Capabilities: cvolume cswitch
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 63
  Front Left: Capture 0 [0%] [-35.25dB] [off]
  Front Right: Capture 0 [0%] [-35.25dB] [off]
Simple mixer control 'Auto-Mute Mode',0
  Capabilities: enum
  Items: 'Disabled' 'Enabled'
  Item0: 'Enabled'
Simple mixer control 'Digital',0
  Capabilities: cvolume
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 120
  Front Left: Capture 60 [50%] [0.00dB]
  Front Right: Capture 60 [50%] [0.00dB]
"""

# Part of "amixer -c 0 contents" on the same machine: the jack sensors are card controls.
MBP92_CONTENTS = """\
numid=21,iface=CARD,name='Headphone Jack'
  ; type=BOOLEAN,access=r-------,values=1
  : values=on
numid=26,iface=CARD,name='HDMI/DP,pcm=3 Jack'
  ; type=BOOLEAN,access=r-------,values=1
  : values=off
numid=3,iface=MIXER,name='Capture Switch'
  ; type=BOOLEAN,access=rw------,values=2
  : values=on,on
"""

# Another codec: an input selector, and a Mic control with an analog loopback.
SELECTOR_SCONTENTS = """\
Simple mixer control 'Master',0
  Capabilities: pvolume pvolume-joined pswitch pswitch-joined
  Playback channels: Mono
  Limits: Playback 0 - 64
  Mono: Playback 0 [0%] [-64.00dB] [off]
Simple mixer control 'Mic',0
  Capabilities: pvolume pswitch cvolume cswitch
  Playback channels: Front Left - Front Right
  Capture channels: Front Left - Front Right
  Limits: Playback 0 - 31 Capture 0 - 31
  Front Left: Playback 0 [0%] [off] Capture 0 [0%] [off]
  Front Right: Playback 0 [0%] [off] Capture 0 [0%] [off]
Simple mixer control 'Internal Mic Boost',0
  Capabilities: volume
  Playback channels: Front Left - Front Right
  Capture channels: Front Left - Front Right
  Limits: 0 - 3
  Front Left: 0 [0%] [0.00dB]
  Front Right: 0 [0%] [0.00dB]
Simple mixer control 'Input Source',0
  Capabilities: cenum
  Items: 'Mic' 'Internal Mic'
  Item0: 'Mic'
"""

FAKE_AMIXER = r"""#!/usr/bin/env python3
import os, re, sys
text = open(os.environ["FAKE_SCONTENTS"]).read()
args = sys.argv[1:]
if os.environ.get("FAKE_NO_CARD"):
    print("amixer: Mixer attach hw:0 error: No such file or directory", file=sys.stderr)
    sys.exit(1)
while args and args[0] in ("-c", "-q"):
    args = args[2:] if args[0] == "-c" else args[1:]
blocks = re.split(r"(?m)^(?=Simple mixer control )", text)
blocks = [b for b in blocks if b]
def find(ident):
    name, _, index = ident.rpartition(",")
    for block in blocks:
        if block.startswith("Simple mixer control '%s',%s\n" % (name, index)):
            return block
if args[0] == "scontrols":
    for block in blocks:
        print(block.splitlines()[0])
elif args[0] == "scontents":
    sys.stdout.write(text)
elif args[0] == "sget":
    block = find(args[1])
    if block is None:
        sys.exit(1)
    sys.stdout.write(block)
elif args[0] == "sset":
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write("sset\t" + "\t".join(args[1:]) + "\n")
"""

FAKE_LOGGER = """#!/bin/sh
echo "$0 $*" >> "$FAKE_LOG"
"""


class AudioInit(unittest.TestCase):
    """os/package/being/being-audio-init against an amixer that answers like the real laptop."""

    SCRIPT = REPO / "os/package/being/being-audio-init"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("amixer", FAKE_AMIXER), ("alsactl", FAKE_LOGGER), ("mic-test", FAKE_LOGGER)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = self.root / "calls.log"
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("root=PARTUUID=x ro being.try=1 quiet\n")
        self.read_only = self.root / "read-only-root/var/lib/alsa"
        self.read_only.mkdir(parents=True)
        self.read_only.chmod(0o555)
        self.addCleanup(self.read_only.chmod, 0o755)

    def run_script(self, scontents=MBP92_SCONTENTS, cmdline=None, **extra):
        (self.root / "scontents").write_text(scontents)
        if cmdline is not None:
            self.cmdline.write_text(cmdline)
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_SCONTENTS=str(self.root / "scontents"),
                   FAKE_LOG=str(self.log), BEING_AUDIO_RUN=str(self.root / "run"),
                   BEING_AUDIO_CMDLINE=str(self.cmdline), BEING_AUDIO_WAIT="0",
                   BEING_AUDIO_STATE_DIR=str(self.read_only),
                   BEING_AUDIO_MIC_TEST=str(self.bin / "mic-test"))
        env.update(extra)
        shell = "/bin/dash" if Path("/bin/dash").exists() else "/bin/sh"
        done = subprocess.run([shell, str(self.SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return done.stdout, calls

    def sets(self, calls):
        return [tuple(line.split("\t")[1:]) for line in calls if line.startswith("sset\t")]

    def test_sets_the_macbook_pro_9_2_controls(self):
        out, calls = self.run_script()
        sets = self.sets(calls)
        for control in ("Master,0", "Headphone,0", "Speaker,0", "Bass Speaker,0"):
            self.assertIn((control, "playback", "85%"), sets)
            self.assertIn((control, "playback", "unmute"), sets)
        self.assertIn(("PCM,0", "playback", "100%"), sets)
        self.assertIn(("Mic Boost,0", "2"), sets)               # 2 of 3: +20 dB
        self.assertIn(("Capture,0", "capture", "85%"), sets)
        self.assertEqual([s for s in sets if s[0] == "Capture,0"][-1], ("Capture,0", "capture", "cap"))
        self.assertIn(("Digital,0", "capture", "0dB"), sets)
        touched = {s[0] for s in sets}
        self.assertFalse({"IEC958,0", "IEC958,1", "Auto-Mute Mode,0"} & touched)
        self.assertIn("being-audio-init: before: Master = Mono: Playback 112 [88%] [-7.50dB] [on]", out)
        self.assertIn("being-audio-init: after: Capture = FL: Capture 0 [0%]", out)
        self.assertIn("being-audio-init: done:", out)
        self.assertEqual((self.root / "run/scontents").read_text(), MBP92_SCONTENTS)

    def test_never_unmutes_a_microphone_into_the_speakers_and_picks_the_internal_mic(self):
        _, calls = self.run_script(SELECTOR_SCONTENTS)
        sets = self.sets(calls)
        self.assertIn(("Input Source,0", "Internal Mic"), sets)
        self.assertIn(("Internal Mic Boost,0", "2"), sets)
        self.assertIn(("Mic,0", "capture", "85%"), sets)
        self.assertIn(("Mic,0", "capture", "cap"), sets)
        self.assertFalse([s for s in sets if s[0] == "Mic,0" and "playback" in s])

    def test_writes_state_only_under_run_on_a_read_only_root(self):
        _, calls = self.run_script()
        stores = [line for line in calls if "alsactl" in line]
        self.assertEqual(len(stores), 1)
        self.assertIn(str(self.root / "run/asound.state"), stores[0])
        writable = self.root / "writable/var/lib/alsa"
        writable.mkdir(parents=True)
        self.log.unlink()
        out, calls = self.run_script(BEING_AUDIO_STATE_DIR=str(writable))
        self.assertTrue(any(str(writable / "asound.state") in line for line in calls if "alsactl" in line))

    def test_measures_the_mic_on_try_boots_only(self):
        _, calls = self.run_script()
        tests = [line for line in calls if "mic-test" in line]
        self.assertEqual(len(tests), 1)
        self.assertIn(f"mic-test --card 0 --out {self.root / 'run/mic-level.json'}", tests[0])
        self.log.unlink()
        _, calls = self.run_script(cmdline="root=PARTUUID=x ro quiet\n")
        self.assertFalse([line for line in calls if "mic-test" in line])

    def test_keeps_the_card_controls_and_skips_the_mic_test_on_the_loopback_cable(self):
        proc = self.root / "proc-asound"
        (proc / "card0").mkdir(parents=True)
        (proc / "card0/id").write_text("Loopback\n")
        out, calls = self.run_script(BEING_AUDIO_PROC=str(proc))
        self.assertIn("Loopback cable; no microphone to measure", out)
        self.assertFalse([line for line in calls if "mic-test" in line])
        self.assertTrue((self.root / "run/contents").exists())
        (proc / "card0/id").write_text("PCH\n")
        self.log.unlink(missing_ok=True)
        _, calls = self.run_script(BEING_AUDIO_PROC=str(proc))
        self.assertEqual(len([line for line in calls if "mic-test" in line]), 1)

    def test_no_card_or_keep_leaves_everything_and_succeeds(self):
        out, calls = self.run_script(FAKE_NO_CARD="1")
        self.assertIn("no sound card 0", out)
        self.assertEqual(calls, [])
        out, calls = self.run_script(cmdline="ro being.audio=keep being.try=1\n")
        self.assertIn("mixer left as it is", out)
        self.assertEqual(calls, [])

    def test_unit_runs_before_the_voice_and_is_installed(self):
        unit = (REPO / "os/package/being/being-audio-init.service").read_text()
        self.assertIn("Before=being-voice.service", unit)
        self.assertIn("After=alsa-restore.service alsa-state.service sound.target", unit)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ExecStart=/usr/libexec/being/being-audio-init", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("Requires=", unit)
        recipe = (REPO / "os/package/being/being.mk").read_text()
        self.assertIn("$(BEING_PKGDIR)/being-audio-init $(TARGET_DIR)/usr/libexec/being/being-audio-init", recipe)
        self.assertIn("$(BEING_PKGDIR)/being-audio-init.service \\", recipe)
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))


def tone(amplitude, seconds=0.5, channels=1, silent_channel=None, rate=16000):
    import array as array_
    import math as math_
    samples = array_.array("h")
    for n in range(int(rate * seconds)):
        value = int(amplitude * math_.sin(2 * math_.pi * 440 * n / rate))
        for channel in range(channels):
            samples.append(0 if channel == silent_channel else value)
    if os.sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


class MicLevel(FakeMachine):
    def test_levels_in_dbfs(self):
        loud = usb_logs.levels(tone(32767))[0]
        self.assertAlmostEqual(loud["peak_dbfs"], 0.0, delta=0.1)
        self.assertAlmostEqual(loud["rms_dbfs"], -3.0, delta=0.1)
        half = usb_logs.levels(tone(16384))[0]
        self.assertAlmostEqual(half["peak_dbfs"], -6.0, delta=0.1)
        self.assertIsNone(usb_logs.levels(bytes(3200))[0]["peak_dbfs"])
        left, right = usb_logs.levels(tone(8192, channels=2, silent_channel=0), 2)
        self.assertIsNone(left["rms_dbfs"])
        self.assertAlmostEqual(right["peak_dbfs"], -12.0, delta=0.1)

    def test_mic_test_keeps_levels_never_audio(self):
        calls = []

        def recorder(device, channels, seconds):
            calls.append((device, channels, seconds))
            return 0, tone(1000, seconds=seconds, channels=channels, silent_channel=0 if channels == 2 else None), ""
        self.write("proc/uptime", "7.2 3.0\n")
        result = usb_logs.mic_test(0, recorder=recorder, listening=lambda: False, root=self.root)
        self.assertEqual(calls, [("plughw:0,0", 1, 3), ("plughw:0,0", 2, 1)])
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["peak_dbfs"], -30.3, delta=0.2)
        self.assertIsNone(result["channels"][0]["rms_dbfs"])
        path = self.root / "run/being-audio/mic-level.json"
        usb_logs.save_mic_result(result, path)
        self.assertLess(path.stat().st_size, 1024)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["mic-level.json"])
        line = usb_logs.mic_line(usb_logs.mic_result(self.root))
        self.assertTrue(line.startswith("peak -30."), line)
        self.assertIn("dBFS, rms -33.", line)
        self.assertIn("at 7 s after power-on", line)
        self.assertIn("ONE SIDE much weaker", line)

    def test_mic_test_stays_away_while_pip_listens_or_the_device_is_busy(self):
        never = lambda *_: self.fail("recorded while the voice was listening")
        result = usb_logs.mic_test(0, recorder=never, listening=lambda: True, root=self.root)
        self.assertEqual(result["status"], "skipped")
        busy = usb_logs.mic_test(0, recorder=lambda *_: (1, b"", "arecord: main:850: audio open error: Device or resource busy"),
                                 listening=lambda: False, root=self.root)
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(usb_logs.mic_line(busy), "not measured: the capture device was busy")
        missing = usb_logs.mic_test(0, recorder=lambda *_: (None, b"", "arecord is not installed"),
                                    listening=lambda: False, root=self.root)
        self.assertEqual(usb_logs.mic_line(missing), "not measured: arecord is not installed")

    def test_mic_line_names_silence_and_clipping(self):
        self.assertIn("SILENT", usb_logs.mic_line({"status": "ok", "peak_dbfs": None, "rms_dbfs": None}))
        self.assertIn("CLIPPING", usb_logs.mic_line({"status": "ok", "peak_dbfs": -0.2, "rms_dbfs": -9.0}))
        self.assertIn("not measured on this boot", usb_logs.mic_line(None))

    def test_cli_saves_the_result_and_never_fails(self):
        out = self.root / "mic.json"
        with patch.object(usb_logs, "mic_test", return_value={"status": "ok", "peak_dbfs": -20.0, "rms_dbfs": -45.0,
                                                              "seconds": 3, "at": "6 s"}):
            self.assertEqual(usb_logs.main(["mic-test", "--card", "0", "--out", str(out)]), 0)
        self.assertEqual(usb_logs.json.loads(out.read_text())["rms_dbfs"], -45.0)
        with patch.object(usb_logs, "mic_test", side_effect=RuntimeError("no card")):
            self.assertEqual(usb_logs.main(["mic-test", "--out", str(out)]), 0)


class MixerSummary(Reports):
    def test_mixer_line_shows_the_controls_that_matter(self):
        line = usb_logs.mixer_line(MBP92_SCONTENTS)
        self.assertTrue(line.startswith("Master 88% -7.50dB on; Speaker 91% 0.00dB on; Bass Speaker"), line)
        self.assertIn("Headphone 0% -57.50dB off", line)
        self.assertIn("PCM 100% 0.00dB", line)
        self.assertIn("Capture 0% -35.25dB off", line)
        self.assertIn("Digital 50% 0.00dB", line)
        self.assertIn("Mic Boost 0/3 0.00dB", line)
        self.assertNotIn("IEC958", line)
        self.assertTrue(line.endswith("; Mic Boost 0/3 0.00dB; Auto-Mute Mode 'Enabled'"), line)
        self.assertIn("Input Source 'Mic'", usb_logs.mixer_line(SELECTOR_SCONTENTS))
        uneven = MBP92_SCONTENTS.replace("Front Right: Capture 0 [0%] [-35.25dB] [off]",
                                         "Front Right: Capture 54 [85%] [5.25dB] [on]")
        self.assertIn("Capture L 0% -35.25dB off / R 85% 5.25dB on", usb_logs.mixer_line(uneven))
        self.assertEqual(usb_logs.mixer_line(""), "not read (no amixer output for card 0)")

    def test_summary_puts_hearing_and_voice_at_the_top(self):
        self.macbook()
        self.write("run/being-audio/mic-level.json",
                   '{"status": "ok", "peak_dbfs": -18.5, "rms_dbfs": -47.25, "seconds": 3, "at": "6 s"}')
        self.write("run/being-audio/scontents", MBP92_SCONTENTS)
        lines = usb_logs.summary(self.root, boot="#2").splitlines()
        self.assertTrue(lines[3].startswith("Mic level:      peak -18.5 dBFS, rms -47.2 dBFS"), lines[3])
        self.assertTrue(lines[4].startswith("Mixer:          Master 88%"), lines[4])
        self.assertTrue(lines[5].startswith("Machine:"))
        given = usb_logs.summary(self.root, mixer="").splitlines()
        self.assertIn("Mixer:          not read", "\n".join(given))

    def test_mixer_line_names_the_jack_sensors_and_auto_mute(self):
        self.macbook()
        self.write("run/being-audio/scontents", MBP92_SCONTENTS)
        self.write("run/being-audio/contents", MBP92_CONTENTS)
        self.assertEqual(usb_logs.jack_line(MBP92_CONTENTS), "Headphone Jack plugged")
        self.assertEqual(usb_logs.jack_line(""), "")
        line = [l for l in usb_logs.summary(self.root).splitlines() if l.startswith("Mixer:")][0]
        self.assertIn("Auto-Mute Mode 'Enabled'", line)
        self.assertTrue(line.endswith("; Headphone Jack plugged"), line)
        self.assertNotIn("HDMI", line)
        report = usb_logs.hardware_report(self.root)
        self.assertIn("numid=21,iface=CARD,name='Headphone Jack'", report)

    def test_hardware_report_has_the_mixer_and_mic_sections(self):
        self.macbook()
        self.write("run/being-audio/scontents", MBP92_SCONTENTS)
        text = usb_logs.hardware_report(self.root)
        for title in ("alsa mixer (amixer -c 0 scontents)", "alsa mixer controls (amixer -c 0 contents)",
                      "microphone level test", "alsa cards", "alsa pcm"):
            self.assertIn(f"===== {title} =====", text)
        self.assertIn("Simple mixer control 'Mic Boost',0", text)

    def test_audio_init_lines_are_notable(self):
        journal = usb_logs.Journal(self.root / "journal.txt", self.root / "cursor")
        journal.feed(b"[ 6.1] being-audio-init[240]: being-audio-init: mic level: peak -20.0 dBFS\n"
                     b"[ 6.2] being-audio-init[240]: being-audio-init: set Master: playback 85%\n", free=10**9)
        journal.stop()
        self.assertEqual(len(journal.notable["audio"]), 1)


if __name__ == "__main__":
    unittest.main()


class KernelFeatures(unittest.TestCase):
    def test_the_report_says_which_controllers_and_options_are_live(self):
        import gzip

        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "sys/fs/cgroup").mkdir(parents=True)
            (root / "sys/fs/cgroup/cgroup.controllers").write_text("cpuset cpu io memory pids\n")
            (root / "proc/pressure").mkdir(parents=True)
            (root / "proc/pressure/cpu").write_text("some avg10=0.00\n")
            with gzip.open(root / "proc/config.gz", "wt") as config:
                config.write("CONFIG_CGROUP_SCHED=y\nCONFIG_MEMCG=y\n# CONFIG_CFS_BANDWIDTH is not set\n")
            found = usb_logs.kernel_features(root)
            self.assertEqual(found["cgroup controllers"], ["cpuset", "cpu", "io", "memory", "pids"])
            self.assertTrue(found["cpu pressure (/proc/pressure/cpu)"])
            self.assertEqual(found["kernel config"]["CONFIG_CGROUP_SCHED"], "y")
            self.assertEqual(found["kernel config"]["CONFIG_CFS_BANDWIDTH"], "not set")
            self.assertEqual(found["kernel config"]["CONFIG_PSI"], "not set")
            self.assertIn("===== kernel features (cgroups, pressure) =====", usb_logs.hardware_report(root))

    def test_a_kernel_without_its_config_says_so(self):
        with tempfile.TemporaryDirectory() as work:
            found = usb_logs.kernel_features(Path(work))
        self.assertIsNone(found["cgroup controllers"])
        self.assertIn("no /proc/config.gz", found["kernel config"])
