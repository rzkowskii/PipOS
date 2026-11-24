import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from mind import hardware_metrics as metrics

REPO = Path(__file__).resolve().parents[1]


class HardwareMetricsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_snapshot_keeps_only_approved_inventory_and_network_fields(self):
        self.write("etc/being/build-id", "dev-6f40451\n")
        self.write("models/manifest.json", json.dumps({
            "reflex": {"sha256": "a" * 64, "from": "private model path"},
        }))
        self.write("sys/class/dmi/id/product_name", "MacBookPro9,2")
        self.write("sys/class/dmi/id/product_serial", "SECRET-SERIAL")
        self.write("proc/sys/kernel/osrelease", "6.18.64")
        self.write("proc/cmdline", "root=PARTUUID=SECRET-PARTITION rauc.slot=B secret=SECRET-KERNEL")
        self.write("proc/cpuinfo", "model name : Intel(R) Core(TM) i5-3210M CPU @ 2.50GHz\n"
                   "flags : avx f16c hdw-vendor-specific\nSerial : SECRET-CPU\n")
        self.write("mind/network/wifi.json", '{"ssid":"SECRET-SSID","psk":"SECRET-PSK"}')
        self.write("mind/memory.db", "SECRET-MEMORY")
        wifi = "sys/class/net/wlx001122334455"
        (self.root / wifi / "device").mkdir(parents=True)
        (self.root / wifi / "wireless").mkdir()
        self.write(f"{wifi}/address", "00:11:22:33:44:55")
        self.write(f"{wifi}/carrier", "1\n")
        self.write(f"{wifi}/statistics/rx_bytes", "5000")
        self.write("proc/net/wireless", " wlx001122334455: 0000 70. -45. -101. 0 0 0 0 0 0\n")
        wired = "sys/class/net/enp2s0"
        (self.root / wired / "device").mkdir(parents=True)
        self.write(f"{wired}/carrier", "1\n")
        self.write(f"{wired}/speed", "1000\n")
        self.write(f"{wired}/duplex", "full\n")
        self.write("sys/class/hwmon/hwmon0/name", "coretemp\n")
        self.write("sys/class/hwmon/hwmon0/temp1_label", "Package id 0\n")
        self.write("sys/class/hwmon/hwmon0/temp1_input", "56000\n")
        self.write("sys/class/hwmon/hwmon1/name", "applesmc\n")
        self.write("sys/class/hwmon/hwmon1/fan1_input", "2007\n")
        self.write("sys/class/power_supply/BAT0/type", "Battery\n")
        self.write("sys/class/power_supply/BAT0/voltage_now", "12000000\n")
        self.write("sys/class/power_supply/BAT0/current_now", "-1000000\n")
        self.write("sys/class/power_supply/BAT0/capacity", "67\n")
        self.write("sys/class/power_supply/AC/type", "Mains\n")
        self.write("sys/class/power_supply/AC/online", "0\n")

        data = metrics.collect(0, 5, root=self.root, services=lambda _: {})
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["system"]["model"], "MacBookPro9,2")
        self.assertEqual(data["system"]["build_id"], "dev-6f40451")
        self.assertEqual(data["system"]["slot"], "B")
        self.assertEqual(data["system"]["cpu_flags"], ["avx", "f16c"])
        self.assertEqual(data["system"]["model_sha256"], "a" * 64)
        sample, = data["samples"]
        self.assertEqual(sample["network"]["wifi0"], {
            "connected": True, "signal_dbm": -45, "rx_bytes": 5000,
            "tx_bytes": None, "rx_errors": None, "tx_errors": None,
        })
        self.assertEqual(sample["network"]["ethernet0"]["speed_mbps"], 1000)
        self.assertEqual(sample["network"]["ethernet0"]["duplex"], "full")
        self.assertEqual(sample["thermal"][0], {
            "driver": "coretemp", "channel": "temp1", "label": "Package id 0", "temperature_c": 56.0,
        })
        self.assertIs(sample["power"]["mains_online"], False)
        self.assertEqual(sample["power"]["batteries"], [{"charge_percent": 67, "status": None, "watts": 12.0}])
        self.assertEqual(data["window"]["fan_max_rpm"], 2007)
        self.assertEqual(data["window"]["cpu_temperature_max_c"], 56.0)
        self.assertIsNone(data["window"]["battery_discharge_mean_watts"])
        self.assertEqual(data["window"]["wifi_signal_mean_dbm"], -45)
        self.assertEqual(data["window"]["wifi_connected_percent"], 100)
        self.assertIsNone(data["window"]["cpu_busy_percent"])
        exported = json.dumps(data)
        for secret in ("SECRET-", "hdw-vendor-specific", "private model path", "001122334455", "00:11:22:33:44:55", "enp2s0"):
            self.assertNotIn(secret, exported)

    def test_macbook_inventory_uses_linux_class_paths_without_active_or_private_probes(self):
        card = "sys/class/drm/card0"
        self.write(f"{card}/device/vendor", "0x8086\n")
        self.write(f"{card}/device/device", "0x0166\n")
        (self.root / card / "device/driver").symlink_to("../../../../bus/pci/drivers/i915")
        self.write("sys/class/drm/card0-LVDS-1/status", "connected\n")
        self.write("sys/class/drm/card0-HDMI-A-1/status", "connected\n")
        self.write("proc/asound/pcm", "00-00: SECRET-CODEC : playback 1 : capture 1\n"
                   "00-03: SECRET-HDMI : playback 1\n")
        self.write("sys/class/input/input6/capabilities/sw", "1\n")
        self.write("proc/acpi/button/lid/LID0/state", "state:      closed\n")
        light = "sys/class/backlight/intel_backlight"
        self.write(f"{light}/type", "raw\n")
        self.write(f"{light}/max_brightness", "2400\n")
        self.write(f"{light}/brightness", "600\n")
        self.write(f"{light}/actual_brightness", "0\n")
        self.write(f"{light}/bl_power", "4\n")

        smc = "sys/devices/platform/applesmc.768"
        self.write(f"{smc}/name", "applesmc\n")
        self.write(f"{smc}/fan1_input", "2100\n")
        self.write(f"{smc}/fan1_label", "SECRET-FAN\n")
        self.write(f"{smc}/temp16_label", "TC0P\n")
        self.write(f"{smc}/temp16_input", "48000\n")
        self.write(f"{smc}/temp17_label", "SECRET-SMC\n")
        self.write(f"{smc}/temp17_input", "111000\n")
        hwmon_device = self.root / smc / "hwmon/hwmon1"
        hwmon_device.mkdir(parents=True)
        (hwmon_device / "device").symlink_to("../..")
        hwmon_class = self.root / "sys/class/hwmon"
        hwmon_class.mkdir(parents=True)
        (hwmon_class / "hwmon1").symlink_to(hwmon_device)
        self.write("sys/class/power_supply/BAT0/type", "Battery\n")
        self.write("sys/class/power_supply/BAT0/power_now", "-14000000\n")
        self.write("sys/class/power_supply/BAT0/status", "Discharging\n")

        with patch.object(metrics, "read_field", wraps=metrics.read_field) as reads:
            data = metrics.collect(0, 5, root=self.root, services=lambda _: {})
        self.assertEqual(data["system"]["hardware"], {
            "drm_cards": {"card0": {"driver": "i915", "vendor_id": "0x8086", "device_id": "0x0166",
                                    "internal_connectors": ["LVDS-1"]}},
            "alsa_pcms": {"0:0": {"capture": True, "playback": True},
                          "0:3": {"capture": False, "playback": True}},
            "lid_switch_detected": True,
        })
        sample, = data["samples"]
        self.assertIs(sample["lid_closed"], True)
        self.assertEqual(sample["backlights"], {
            "intel_backlight": {"type": "raw", "requested_percent": 25, "power_state": 4},
        })
        self.assertEqual(data["window"]["fan_max_rpm"], 2100)
        self.assertEqual(data["window"]["temperature_max_c"], 48)
        self.assertIsNone(data["window"]["cpu_temperature_max_c"])
        self.assertEqual(data["window"]["battery_discharge_mean_watts"], 14)
        read_names = {call.args[0].name for call in reads.call_args_list}
        self.assertTrue({"actual_brightness", "fan1_label", "temp17_input"}.isdisjoint(read_names))
        self.assertNotIn("SECRET-", json.dumps(data))

    def test_lid_requires_an_unambiguous_current_acpi_reading(self):
        self.write("proc/acpi/button/lid/LID0/state", "state:      open\n")
        self.write("sys/class/input/input0/capabilities/sw", "2\n")
        self.assertIs(metrics.lid_closed(self.root / "proc"), False)
        self.assertIs(metrics.lid_switch_detected(self.root / "sys"), False)
        self.write("proc/acpi/button/lid/LID1/state", "state:      closed\n")
        self.assertIsNone(metrics.lid_closed(self.root / "proc"))

    def test_wifi_falls_back_to_read_only_station_stats_when_carrier_is_up(self):
        wifi = "sys/class/net/wlx001122334455"
        (self.root / wifi / "device").mkdir(parents=True)
        (self.root / wifi / "phy80211").mkdir()
        self.write(f"{wifi}/operstate", "up\n")
        report = ("Station 66:77:88:99:aa:bb (on wlx001122334455)\n"
                  "\tsignal:  \t-53 [-61, -56] dBm\n\tsignal avg:\t-50 dBm\n")
        with patch.object(metrics.subprocess, "run", return_value=Mock(stdout=report, returncode=0)) as run:
            data = metrics.collect(0, 5, root=self.root, services=lambda _: {})
        args, = run.call_args.args
        self.assertEqual(args, ["/usr/sbin/iw", "dev", "wlx001122334455", "station", "dump"])
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(data["window"]["wifi_signal_mean_dbm"], -53)
        self.assertEqual(data["window"]["wifi_connected_percent"], 100)
        for private in ("001122334455", "66:77:88:99:aa:bb", "-61"):
            self.assertNotIn(private, json.dumps(data))

        self.write(f"{wifi}/operstate", "down\n")
        self.write("proc/net/wireless", "wlx001122334455: 0000 70. -53. -101. 0 0 0 0 0 0\n")
        with patch.object(metrics.subprocess, "run") as run:
            data = metrics.collect(0, 5, root=self.root, services=lambda _: {})
        run.assert_not_called()
        self.assertEqual(data["window"]["wifi_connected_percent"], 0)
        self.assertIsNone(data["samples"][0]["network"]["wifi0"]["signal_dbm"])

    def test_station_signal_requires_one_peer_one_signal_and_success(self):
        for output, status in (
            ("Station private\n\tsignal: -53 dBm\nStation other\n", 0),
            ("Station private\n\tsignal: -53 dBm\n\tsignal: -54 dBm\n", 0),
            ("Station private\n\tsignal avg: -53 dBm\n", 0),
            ("Station private\n\tsignal: -999 dBm\n", 0),
            ("Station private\n\tsignal: -53 dBm\n", 1),
        ):
            with self.subTest(output=output, status=status), patch.object(
                metrics.subprocess, "run", return_value=Mock(stdout=output, returncode=status)
            ):
                self.assertIsNone(metrics.wifi_station_signal("wlan0"))
        with patch.object(metrics.subprocess, "run", side_effect=subprocess.TimeoutExpired("iw", 0.5)):
            self.assertIsNone(metrics.wifi_station_signal("wlan0"))

    def test_window_uses_counter_deltas_and_detects_a_reset(self):
        self.write("proc/stat", "cpu 50 0 20 90 10 0 0 0 100 0\n")
        self.write("proc/meminfo", "MemTotal: 128000 kB\nMemAvailable: 100000 kB\n")
        self.write("proc/diskstats", "8 0 sda 10 0 30 0 10 0 50 0 0 100 0\n"
                   "8 1 sda1 5 0 20 0 5 0 30 0 0 50 0\n")
        rapl = "sys/class/powercap/intel-rapl:0"
        self.write(f"{rapl}/name", "package-0")
        self.write(f"{rapl}/max_energy_range_uj", "10000000")
        self.write(f"{rapl}/energy_uj", "9000000")
        wifi = "sys/class/net/wlan0"
        (self.root / wifi / "device").mkdir(parents=True)
        (self.root / wifi / "phy80211").mkdir()
        self.write(f"{wifi}/statistics/rx_errors", "10")
        self.write(f"{wifi}/statistics/rx_bytes", "100")
        cpufreq = "sys/devices/system/cpu/cpufreq/policy0"
        self.write(f"{cpufreq}/scaling_cur_freq", "1700000")
        for cpu in (0, 1):
            self.write(f"sys/devices/system/cpu/cpu{cpu}/thermal_throttle/core_throttle_count", "9\n")
        now = 0

        def advance(wait):
            nonlocal now
            now += wait
            self.write("proc/stat", "cpu 60 0 30 115 15 0 0 0 900 0\n")
            self.write("proc/meminfo", "MemTotal: 128000 kB\nMemAvailable: 80000 kB\n")
            self.write("proc/diskstats", "8 0 sda 10 0 36 0 10 0 65 0 0 120 0\n")
            self.write(f"{rapl}/energy_uj", "1000000")
            self.write(f"{wifi}/statistics/rx_errors", "1")
            self.write(f"{wifi}/statistics/rx_bytes", "450")
            self.write(f"{cpufreq}/scaling_cur_freq", "2300000")
            for cpu in (0, 1):
                self.write(f"sys/devices/system/cpu/cpu{cpu}/thermal_throttle/core_throttle_count", "11\n")

        services = Mock(return_value={})
        data = metrics.collect(1, 5, root=self.root, clock=lambda: now, sleep=advance, services=services)
        self.assertEqual(data["sampling"], {"seconds": 1, "interval": 5})
        self.assertEqual([sample["elapsed_seconds"] for sample in data["samples"]], [0, 1])
        self.assertEqual(services.call_count, 2)
        self.assertEqual(data["window"], {
            "elapsed_seconds": 1, "cpu_busy_percent": 40, "cpu_iowait_percent": 10,
            "memory_available_min_kib": 80000, "package_power_mean_watts": 2,
            "cpu_frequency_mean_khz": 2000000, "temperature_max_c": None, "fan_max_rpm": None,
            "cpu_temperature_max_c": None, "cpu_core_throttle_max_events": 2,
            "battery_discharge_mean_watts": None, "wifi_signal_mean_dbm": None, "wifi_connected_percent": None,
            "network": {"wifi0": {"rx_bytes": 350, "tx_bytes": None, "rx_errors": None, "tx_errors": None}},
            "disk": {"sda": {"read_bytes": 3072, "write_bytes": 7680, "busy_ms": 20}},
        })

    def test_missing_private_power_counters_and_partial_files_are_safe(self):
        self.write("proc/stat", "cpu 50 0 broken 90\n")
        self.write("models/manifest.json", '{"reflex":{"sha256": 42}}')
        self.write("sys/class/powercap/intel-rapl:0/name", "package-0")
        data = metrics.collect(0, 1, root=self.root, services=lambda _: {})
        self.assertEqual(data["samples"][0]["cpu"], {})
        self.assertIsNone(data["system"]["model_sha256"])
        self.assertEqual(data["samples"][0]["power"]["packages"], {})
        self.assertIsNone(data["window"]["package_power_mean_watts"])

    def test_battery_wear_uses_the_real_laptops_charge_figures(self):
        # The first real boot (23 November): 4,684 of 5,770 mAh after 71 cycles.
        for name, text in (("type", "Battery"), ("capacity", "79"), ("status", "Charging"),
                           ("charge_full", "4684000"), ("charge_full_design", "5770000"), ("cycle_count", "71")):
            self.write(f"sys/class/power_supply/BAT0/{name}", text + "\n")
        battery, = metrics.power_info(self.root / "sys")["batteries"]
        self.assertEqual((battery["health_percent"], battery["cycles"]), (81, 71))
        self.write("sys/class/power_supply/BAT0/charge_full_design", "0\n")
        self.write("sys/class/power_supply/BAT0/cycle_count", "0\n")
        battery, = metrics.power_info(self.root / "sys")["batteries"]
        self.assertNotIn("health_percent", battery)
        self.assertNotIn("cycles", battery, "0 means the controller does not count")

    def test_power_and_wifi_window_use_only_valid_readings(self):
        samples = [metrics.snapshot(self.root, elapsed) for elapsed in range(4)]
        for sample, (watts, status, carrier, signal) in zip(samples, (
            (10, "Discharging", True, -50), (14, "Discharging", False, -80),
            (24, "Charging", True, -60), (None, "Discharging", None, None),
        )):
            sample["power"]["batteries"] = [{"watts": watts, "status": status}]
            sample["network"]["wifi0"] = {"connected": carrier, "signal_dbm": signal}
            sample["network"]["ethernet0"] = {"connected": False, "signal_dbm": None}
        summary = metrics.summarize(samples)
        self.assertEqual(summary["battery_discharge_mean_watts"], 12)
        self.assertEqual(summary["wifi_signal_mean_dbm"], -55)
        self.assertEqual(summary["wifi_connected_percent"], 66.67)

    def test_deadline_is_sampled_with_a_shorter_final_interval(self):
        clock = Mock(return_value=0)
        intervals = []

        def advance(wait):
            intervals.append(wait)
            clock.return_value += wait

        data = metrics.collect(11, 5, root=self.root, clock=clock, sleep=advance, services=lambda _: {})
        self.assertEqual(intervals, [5, 5, 1])
        self.assertEqual([sample["elapsed_seconds"] for sample in data["samples"]], [0, 5, 10, 11])

    def test_systemd_uses_fixed_properties_and_limits_cortex_arguments(self):
        self.write("proc/123/cmdline", "/usr/bin/llama-server\0--threads\0" "4\0--threads-batch=2\0"
                   "--batch-size\0Secret\0--model\0/private/name.gguf\0")
        report = ("Id=being-cortex.service\nMainPID=123\nActiveState=active\nNRestarts=2\n"
                  "MemoryCurrent=18446744073709551615\nDescription=private name\n\n"
                  "Id=unrelated.service\nActiveState=failed\n")
        with patch.object(metrics.subprocess, "run", return_value=Mock(stdout=report)) as run:
            observed = metrics.read_services(self.root / "proc")
        self.assertNotIn("Environment", " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["timeout"], 4)
        self.assertEqual(set(observed), {"being-cortex.service"})
        cortex = observed["being-cortex.service"]
        self.assertEqual(cortex["restarts"], 2)
        self.assertIsNone(cortex["memory_bytes"])
        self.assertEqual(cortex["arguments"], {"threads": 4, "prompt_threads": 2})
        self.assertNotIn("private", json.dumps(observed))

        with patch.object(metrics.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl", 4)):
            self.assertEqual(metrics.read_services(self.root / "proc"), {})

    def test_every_organ_and_the_audio_setup_are_watched(self):
        for unit in ("being-hands.service", "being-eyes.service", "being-bodycare.service",
                     "being-audio-init.service", "being-voice.service", "being-face.service"):
            self.assertIn(unit, metrics.SERVICES)
            self.assertTrue((REPO / "os/package/being" / unit).is_file(), unit)
        report = ("Id=being-audio-init.service\nActiveState=active\nSubState=exited\nNRestarts=0\n\n"
                  "Id=being-hands.service\nActiveState=failed\nSubState=failed\nNRestarts=3\n")
        with patch.object(metrics.subprocess, "run", return_value=Mock(stdout=report)) as run:
            observed = metrics.read_services(self.root / "proc")
        self.assertIn("being-bodycare.service", run.call_args.args[0])
        self.assertEqual(observed["being-audio-init.service"]["sub_state"], "exited")
        self.assertEqual(observed["being-hands.service"]["restarts"], 3)

    def test_sampling_bounds_and_cli_require_linux(self):
        for duration, interval in ((-1, 1), (301, 1), (1, 0), (1, 61)):
            with self.subTest(duration=duration, interval=interval), self.assertRaises(ValueError):
                metrics.collect(duration, interval, root=self.root)
        with patch.object(metrics.sys, "argv", ["being-metrics"]), patch.object(metrics.sys, "platform", "darwin"):
            with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
                metrics.main()
        self.assertIn("must run on the PipOS device", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
