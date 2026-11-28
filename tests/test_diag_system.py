"""pip-diag's system, boot, cortex, body and workshop checks with an injected machine."""

import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import diag_system as system
from mind.diag_system import Entry, Journal, Result
from tests.test_diag import FakeHost

SHOW = """\
Id=being-mind.service
LoadState=loaded
ActiveState=active
SubState=running
NRestarts=0
Result=success

Id=being-voice.service
LoadState=loaded
ActiveState=active
SubState=running
NRestarts={voice_restarts}
Result=success

Id=being-eyes.service
LoadState=not-found
ActiveState=inactive
SubState=dead
NRestarts=0
Result=success
"""


def systemctl(voice_restarts=0, failed=""):
    def run(argv):
        if argv[1] == "show":
            return Result(0, SHOW.format(voice_restarts=voice_restarts))
        if argv[1] == "list-units" and "--failed" in argv:
            return Result(0, failed)
        if argv[1] == "list-units":
            return Result(0, "")
        if argv[1] == "is-active":
            return Result(0, "active\n")
        return Result(0, "")
    return run


class Units(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def host(self, **kwargs):
        return FakeHost(self.root, programs={"systemctl"}, handlers={"systemctl": systemctl(**kwargs)})

    def test_healthy_organs_pass_and_absent_ones_are_left_out(self):
        item = system.check_units(self.host())
        self.assertEqual(item["status"], "pass")
        self.assertNotIn("being-eyes", item["value"]["units"])

    def test_a_restart_warns_and_a_failed_unit_fails(self):
        self.assertEqual(system.check_units(self.host(voice_restarts=2))["status"], "warn")
        failed = system.check_units(self.host(failed="being-face.service loaded failed failed Being: face\n"))
        self.assertEqual(failed["status"], "fail")
        self.assertIn("being-face.service", failed["evidence"])

    def test_no_systemctl_skips(self):
        self.assertEqual(system.check_units(FakeHost(self.root))["status"], "skip")


class Clock(unittest.TestCase):
    def test_drift_against_the_mac_and_the_rtc(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root)
        self.assertEqual(system.check_clock(host)["status"], "skip")
        self.assertEqual(system.check_clock(host, 1795500000 - 30)["status"], "pass")
        far = system.check_clock(host, 1795500000 - 3600)
        self.assertEqual((far["status"], far["value"]["mac_drift_s"]), ("warn", 3600))
        host.made("/sys/class/rtc/rtc0/since_epoch", "1650000000\n")  # the 2022 RTC
        self.assertEqual(system.check_clock(host, 1795500000)["status"], "warn")

    def test_build_and_entry_options(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root)
        host.made("/proc/cmdline", "BOOT_IMAGE=/bzImage being.try=1 snd_hda_intel.model=mbp81 "
                                   "being.mic_channel=auto being.voice_debug=1 quiet\n")
        host.made("/etc/being/build-id", "laptop-20261124\n")
        item = system.check_build(host)
        self.assertEqual(item["value"]["cmdline"], {"being.try": "1", "snd_hda_intel.model": "mbp81",
                                                    "being.mic_channel": "auto", "being.voice_debug": "1"})
        self.assertTrue(system.voice_debug_on(host))

    def test_a_later_voice_debug_zero_turns_it_off(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root)
        with patch.dict(os.environ, {"BEING_VOICE_DEBUG": ""}):
            host.made("/proc/cmdline", "being.try=1 being.voice_debug=1 being.voice_debug=0\n")
            self.assertFalse(system.voice_debug_on(host))
            host.made("/proc/cmdline", "being.try=1 being.voice_debug=0 being.voice_debug=1\n")
            self.assertTrue(system.voice_debug_on(host))


class Journals(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_entries_are_read_from_json_with_any_message_encoding(self):
        lines = [json.dumps({"__CURSOR": "c1", "__MONOTONIC_TIMESTAMP": "48704392",
                             "_SYSTEMD_UNIT": "being-voice.service", "MESSAGE": "heard the wake phrase: score -1805"}),
                 json.dumps({"__CURSOR": "c2", "__MONOTONIC_TIMESTAMP": "48800000", "SYSLOG_IDENTIFIER": "kernel",
                             "MESSAGE": list(b"bytes \xff message")}),
                 "not json"]
        host = FakeHost(self.root, programs={"journalctl"},
                        handlers={"journalctl": lambda argv: Result(0, "\n".join(lines))})
        entries = Journal(host).entries("c0", units=("being-voice.service",))
        self.assertEqual(entries[0],
                         Entry(48.704392, "being-voice.service", "heard the wake phrase: score -1805", "c1"))
        self.assertTrue(entries[1].message.startswith("bytes "))
        call = host.calls[-1]
        self.assertIn("--after-cursor=c0", call)
        self.assertEqual(call[call.index("-u") + 1], "being-voice.service")
        self.assertEqual(Journal(host).cursor(), "c2")

    def test_boot_markers(self):
        entries = [Entry(5.3, "being-brainstem.service", "brainstem: waking -> idle"),
                   Entry(8.4, "being-mind.service", "mind: Pip | persistent memory | cortex at http://127.0.0.1:8080"),
                   Entry(25.6, "being-voice.service", "listening for the wake phrase: noise floor -70.7 dBFS"),
                   Entry(42.0, "being-mind.service", "mind: warmed 236 verified prompt tokens"),
                   Entry(60.0, "init.scope", "being-local-screen.service: Deactivated successfully."),
                   Entry(130.0, "being-face.service", "face: drawn by GLES 3.0")]
        markers = system.boot_markers(entries)
        self.assertEqual(markers["voice_listening"], 25.6)
        self.assertEqual(markers["face_drawn"], 130.0)

        class Fixed(Journal):
            def available(self):
                return True

            def entries(self, *args, **kwargs):
                return entries

        item = system.check_boot_timing(FakeHost(self.root), Fixed(FakeHost(self.root)))
        self.assertEqual(item["status"], "warn")  # the face came 70 s after the menu closed


class Imports(unittest.TestCase):
    def test_a_module_that_needs_zlib_fails_the_import_check(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        package = root / "mind"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "good.py").write_text("import json\n")
        (package / "bad.py").write_text("import zlib\n")
        (package / "__main__.py").write_text("raise SystemExit('never imported')\n")
        item = system.check_imports(system.Host(), package)
        self.assertEqual(item["status"], "fail")
        self.assertEqual(set(item["value"]["broken"]), {"mind.bad"})
        (package / "bad.py").write_text("try:\n    import zlib\nexcept ImportError:\n    zlib = None\n")
        self.assertEqual(system.check_imports(system.Host(), package)["status"], "pass")


class Bus(unittest.TestCase):
    """The brainstem's beat says how many named peers are up (crates/brainstem/src/main.rs)."""

    def serve(self, beat):
        root = Path(tempfile.mkdtemp(prefix="pdbus"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        path = root / "bus.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        self.addCleanup(server.close)
        hello = []

        def serve():
            connection, _ = server.accept()
            with connection:
                hello.append(connection.recv(4096))
                connection.sendall(b'{"t":"body.lid","closed":false}\n' + beat + b"\n")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        return root, path, hello

    def host(self, root, path, active=None):
        if active is None:
            host = FakeHost(root)
        else:
            def run(argv):
                if argv[1] == "show":
                    units = [arg for arg in argv[2:] if not arg.startswith("--")]
                    return Result(0, "\n\n".join(
                        f"Id={unit}\nLoadState=loaded\nActiveState={'active' if unit in active else 'inactive'}"
                        "\nSubState=running\nNRestarts=0\nResult=success" for unit in units))
                return Result(0, "")
            host = FakeHost(root, programs={"systemctl"}, handlers={"systemctl": run})
        host.made("/etc/being/env", f"BEING_BUS={path}\n")
        host.path = lambda value, _old=host.path: Path(value) if str(value) == str(path) else _old(value)
        return host

    def test_the_real_beat_with_every_running_organ_connected_passes(self):
        root, path, hello = self.serve(b'{"t":"stem.beat","state":"idle","up":6}')
        active = {"being-mind.service", "being-voice.service", "being-face.service", "being-hands.service",
                  "being-bodycare.service"}
        item = system.check_bus(self.host(root, path, active))
        self.assertEqual(item["status"], "pass", item)
        self.assertEqual(item["value"]["organs"], 5)
        self.assertEqual(item["value"]["running"], list(system.BUS_ORGANS))
        self.assertIn(b'"name":"diag"', hello[0])

    def test_fewer_peers_than_running_organs_warns(self):
        root, path, _ = self.serve(b'{"t":"stem.beat","state":"idle","up":3}')
        active = {"being-mind.service", "being-voice.service", "being-local-screen.service",
                  "being-bodycare.service"}
        item = system.check_bus(self.host(root, path, active))
        self.assertEqual(item["status"], "warn")
        self.assertEqual(item["value"]["running"], ["mind", "voice", "face", "bodycare"])
        self.assertEqual(item["value"]["short_by"], 2)

    def test_the_count_alone_passes_when_unit_states_are_unknown(self):
        root, path, _ = self.serve(b'{"t":"stem.beat","state":"idle","up":4}')
        item = system.check_bus(self.host(root, path))
        self.assertEqual(item["status"], "pass")
        self.assertIsNone(item["value"]["running"])

    def test_a_list_of_names_is_still_understood(self):
        root, path, _ = self.serve(b'{"t":"stem.beat","state":"idle","up":["mind","voice","hands"]}')
        item = system.check_bus(self.host(root, path))
        self.assertEqual(item["status"], "warn")
        self.assertEqual(item["value"]["missing"], ["face", "bodycare"])

    def test_no_beat_warns(self):
        root, path, _ = self.serve(b'{"t":"state.changed","state":"idle"}')
        self.assertEqual(system.check_bus(self.host(root, path), timeout=0.5)["status"], "warn")

    def test_no_bus_skips(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertEqual(system.check_bus(FakeHost(root))["status"], "skip")


class Cortex(unittest.TestCase):
    class Client:
        def __init__(self, answers, prompt_tps=19.0, decode_tps=12.0):
            self.answers, self.calls = list(answers), []
            self.prompt_tps, self.decode_tps = prompt_tps, decode_tps

        def request(self, method, path, body=None):
            self.calls.append((method, path, body))
            if path == "/props":
                return {"model_path": "/models/reflex.gguf", "total_slots": 2,
                        "default_generation_settings": {"n_ctx": 4096}}
            if path == "/completion":
                return {"content": self.answers.pop(0),
                        "timings": {"prompt_n": 40, "prompt_per_second": self.prompt_tps,
                                    "predicted_per_second": self.decode_tps}}
            return {}

    def test_right_answers_at_v5_speed_pass_and_leave_the_conversation_slot_alone(self):
        client = self.Client(["4", "Paris."])
        items = {item["id"]: item for item in system.check_cortex(None, prompts=system.PROMPTS[:2], client=client)}
        self.assertEqual(items["cortex.answers"]["status"], "pass")
        self.assertEqual(items["cortex.speed"]["status"], "pass")
        bodies = [body for _, path, body in client.calls if path == "/completion"]
        self.assertTrue(all(body["id_slot"] == 1 and body["temperature"] == 0 and not body["cache_prompt"]
                            for body in bodies))

    def test_slow_or_wrong_answers_warn(self):
        items = {item["id"]: item for item in system.check_cortex(
            None, prompts=system.PROMPTS[:2], client=self.Client(["5", "Paris"], prompt_tps=9.0))}
        self.assertEqual(items["cortex.answers"]["status"], "warn")
        self.assertEqual(items["cortex.speed"]["status"], "warn")

    def test_a_one_slot_server_is_asked_without_a_slot(self):
        class OneSlot(self.Client):
            def request(self, method, path, body=None):
                if path == "/completion" and body.get("id_slot") == 1:
                    raise RuntimeError("POST /completion: HTTP 400 b'invalid slot id'")
                return super().request(method, path, body)

        client = OneSlot(["4", "Paris"])
        items = {item["id"]: item for item in system.check_cortex(None, prompts=system.PROMPTS[:2], client=client)}
        self.assertEqual(items["cortex.answers"]["status"], "pass")

    def test_a_silent_cortex_fails(self):
        class Down:
            def request(self, *args):
                raise OSError("refused")

        self.assertEqual(system.check_cortex(None, client=Down())[0]["status"], "fail")


class Workshop(unittest.TestCase):
    def test_the_terminal_frame_header(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root)
        header = bytearray(128)
        header[:4] = b"BPN1"
        struct.pack_into("<I", header, 32, 1)
        struct.pack_into("<I", header, 44, 17)
        header[96:104] = b"terminal"
        host.made("/run/being-panel/terminal.frame").write_bytes(bytes(header))
        self.assertEqual(system.panel_state(host), {"visible": True, "beat": 17, "title": "terminal"})
        opened, evidence = system.terminal_open(host)
        self.assertTrue(opened)
        self.assertIn("visible", evidence)

    def test_a_shell_alone_is_not_an_open_terminal(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        units = []
        host = FakeHost(root, programs={"systemctl"}, handlers={"systemctl": lambda argv: Result(
            0, "".join(f"{unit} loaded active running x\n" for unit in units))})
        units[:] = ["being-shell-1.service"]
        opened, evidence = system.terminal_open(host)
        self.assertFalse(opened)
        self.assertIn("being-shell-1.service", evidence)
        self.assertIn("a shell without a panel", evidence)
        # A hidden frame does not open it either.
        header = bytearray(128)
        header[:4] = b"BPN1"
        header[96:104] = b"terminal"
        host.made("/run/being-panel/terminal.frame").write_bytes(bytes(header))
        opened, evidence = system.terminal_open(host)
        self.assertFalse(opened)
        self.assertIn("hidden", evidence)
        units[:] = ["being-panel-terminal.service", "being-shell-1.service"]
        self.assertTrue(system.terminal_open(host)[0])

    def test_the_clock_or_monitor_panel_is_not_the_terminal(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        units = []
        host = FakeHost(root, programs={"systemctl"}, handlers={"systemctl": lambda argv: Result(
            0, "".join(f"{unit} loaded active running x\n" for unit in units))})
        for panel in ("being-panel-clock.service", "being-panel-monitor.service"):
            units[:] = [panel]
            opened, evidence = system.terminal_open(host)
            self.assertFalse(opened, panel)
            self.assertIn(panel, evidence)
            self.assertNotIn("a shell without a panel", evidence)
        units[:] = ["being-panel-clock.service", "being-panel-terminal.service"]
        self.assertTrue(system.terminal_open(host)[0])

    def test_the_face_missing_while_the_menu_holds_the_screen(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root, programs={"systemctl"},
                        active={"being-hands.service", "being-local-screen.service"})
        item = system.check_workshop(host)
        self.assertEqual(item["status"], "warn")
        self.assertIn("Try menu has the screen", item["hint"])


class Body(unittest.TestCase):
    def test_wired_link_and_wifi_absent(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root)
        for name, value in (("type", "1"), ("operstate", "up"), ("carrier", "1"), ("speed", "1000"),
                            ("duplex", "full")):
            host.made(f"/sys/class/net/enp1s0f0/{name}", value + "\n")
        host.made("/sys/class/net/enp1s0f0/device/vendor", "0x14e4\n")
        driver = host.path("/sys/bus/pci/drivers/tg3")
        driver.mkdir(parents=True, exist_ok=True)
        host.path("/sys/class/net/enp1s0f0/device").joinpath("driver").symlink_to(driver)
        host.made("/sys/class/net/lo/type", "772\n")
        item = system.check_network(host)
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["value"]["wireless"], "not available on this build")
        self.assertIn("enp1s0f0 driver=tg3 up carrier=1 1000 Mb/s", item["evidence"])

    def test_battery_health(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root)
        base = "/sys/class/power_supply/BAT0"
        for name, value in (("type", "Battery"), ("present", "1"), ("capacity", "88"), ("status", "Discharging"),
                            ("charge_full", "4000000"), ("charge_full_design", "8000000"), ("cycle_count", "512")):
            host.made(f"{base}/{name}", value + "\n")
        item = system.check_battery(host)
        self.assertEqual((item["status"], item["value"]["health_percent"]), ("warn", 50))


class Reading(unittest.TestCase):
    """Host.read and the kernel's /proc/sys, which allocates a buffer the size of the read."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "proc/sys/kernel/random").mkdir(parents=True)
        (self.root / "proc/sys/kernel/osrelease").write_text("6.18.64\n")
        (self.root / "proc/sys/kernel/random/boot_id").write_text("5f0c3a9e-0000-4000-8000-000000000001\n")

    def kernel_open(self, asked):
        """open() as the laptop's kernel does /proc/sys: a read over 2 MiB fails with ENOMEM."""
        real = open

        def fake(path, mode="r", *args, **kwargs):
            handle = real(path, mode, *args, **kwargs)
            if "/proc/sys/" not in str(path):
                return handle
            read = handle.read

            def limited(size=-1):
                asked.append(size)
                if size < 0 or size > 2 * 1024 * 1024:
                    raise OSError(12, "Cannot allocate memory")
                return read(size)

            handle.read = limited
            return handle

        return fake

    def test_the_kernel_and_boot_id_are_read_where_4_mib_reads_fail(self):
        asked = []
        host = system.Host(self.root)
        with patch.object(system, "open", self.kernel_open(asked), create=True):
            item = system.check_build(host)
            boot = system.boot_id(host)
        self.assertEqual(item["value"]["kernel"], "6.18.64")
        self.assertEqual(boot, "5f0c3a9e-0000-4000-8000-000000000001")
        self.assertTrue(asked and max(asked) <= system.PROC_SYS_LIMIT)

    def test_a_read_refused_for_its_size_is_tried_again_smaller(self):
        (self.root / "proc/cmdline").write_text("being.try=1\n")
        real = open
        asked = []

        def fake(path, mode="r", *args, **kwargs):
            handle = real(path, mode, *args, **kwargs)
            read = handle.read

            def limited(size=-1):
                asked.append(size)
                if size > system.PROC_SYS_LIMIT:
                    raise OSError(12, "Cannot allocate memory")
                return read(size)

            handle.read = limited
            return handle

        with patch.object(system, "open", fake, create=True):
            self.assertEqual(system.Host(self.root).read("/proc/cmdline"), "being.try=1\n")
            self.assertIsNone(system.Host(self.root).read("/proc/missing"))
        self.assertEqual(asked, [system.READ_LIMIT, system.PROC_SYS_LIMIT])

    @unittest.skipUnless(os.path.exists("/proc/sys/kernel/osrelease"), "needs a Linux /proc")
    def test_the_real_proc_sys_reads(self):
        import platform

        host = system.Host()
        self.assertEqual((host.read("/proc/sys/kernel/osrelease") or "").strip(), platform.release())
        self.assertTrue(system.boot_id(host))


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def host(self, runtime, *, device=False, dmesg=""):
        def systemctl(argv):
            if "--property=RuntimeWatchdogUSec" in argv:
                return Result(0, f"{runtime}\n")
            return Result(0, "")

        host = FakeHost(self.root, programs={"systemctl", "dmesg"},
                        handlers={"systemctl": systemctl, "dmesg": lambda argv: Result(0, dmesg)})
        if device:
            host.made("/dev/watchdog0")
            host.made("/sys/class/watchdog/watchdog0/identity", "Software Watchdog\n")
        return host

    def test_a_watchdog_set_with_no_device_warns(self):
        # The laptop: RuntimeWatchdogUSec=30s, but the firmware disabled the iTCO watchdog.
        item = system.check_watchdog(self.host("30s", dmesg="iTCO_wdt iTCO_wdt.0.auto: unable to reset NO_REBOOT "
                                                           "flag, device disabled by hardware/BIOS\n"))
        self.assertEqual(item["status"], "warn")
        self.assertIn("every 30s, but there is no /dev/watchdog0", item["evidence"])
        self.assertIn("NO_REBOOT", item["evidence"])
        self.assertIn("CONFIG_SOFT_WATCHDOG", item["hint"])

    def test_a_fed_watchdog_passes_and_none_asked_for_passes(self):
        fed = system.check_watchdog(self.host("30s", device=True))
        self.assertEqual(fed["status"], "pass")
        self.assertIn("Software Watchdog", fed["evidence"])
        self.assertEqual(system.check_watchdog(self.host("0"))["status"], "pass")
        self.assertEqual(system.check_watchdog(FakeHost(self.root))["status"], "skip")


class Devices(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.host = FakeHost(self.root)

    def pci(self, address, vendor, device, driver=None):
        base = f"/sys/bus/pci/devices/{address}"
        self.host.made(f"{base}/vendor", f"0x{vendor:04x}\n")
        self.host.made(f"{base}/device", f"0x{device:04x}\n")
        if driver:
            target = self.host.path(f"/sys/bus/pci/drivers/{driver}")
            target.mkdir(parents=True, exist_ok=True)
            self.host.path(base).joinpath("driver").symlink_to(target)

    def usb(self, name, vendor, product, driver=None):
        base = f"/sys/bus/usb/devices/{name}"
        self.host.made(f"{base}/idVendor", f"{vendor:04x}\n")
        self.host.made(f"{base}/idProduct", f"{product:04x}\n")
        interface = self.host.path(f"/sys/bus/usb/devices/{name}:1.0")
        interface.mkdir(parents=True)
        if driver:
            target = self.host.path(f"/sys/bus/usb/drivers/{driver}")
            target.mkdir(parents=True, exist_ok=True)
            interface.joinpath("driver").symlink_to(target)

    def test_the_macbooks_unsupported_hardware_is_named_not_silently_missing(self):
        self.pci("0000:02:00.0", 0x14E4, 0x16BC)
        self.pci("0000:03:00.0", 0x11C1, 0x5901)
        self.pci("0000:05:00.0", 0x8086, 0x1513)
        self.pci("0000:01:00.0", 0x14E4, 0x4331, driver="b43-pci-bridge")
        self.usb("1-1.8.3", 0x05AC, 0x821D)
        self.usb("1-1.8.2", 0x05AC, 0x8242)
        self.usb("1-1.8.1", 0x05AC, 0x0252, driver="usbhid")
        item = system.check_devices(self.host)
        self.assertEqual(item["status"], "pass")
        for name in ("Bluetooth 05ac:821d", "SD card reader 14e4:16bc", "FireWire 11c1:5901",
                     "Thunderbolt 8086:1513", "infrared receiver 05ac:8242"):
            self.assertIn(name, item["evidence"])
        self.assertIn("not supported on this build", item["evidence"])
        self.assertIn("Wi-Fi (Broadcom BCM4331): driver b43-pci-bridge but no firmware on this build",
                      item["evidence"])
        self.assertNotIn("0252", item["evidence"])  # the keyboard is supported: not listed

    def test_a_bound_driver_is_said_and_nothing_known_skips(self):
        self.assertEqual(system.check_devices(self.host)["status"], "skip")
        self.pci("0000:02:00.0", 0x14E4, 0x16BC, driver="sdhci-pci")
        item = system.check_devices(self.host)
        self.assertIn("SD card reader 14e4:16bc (driver sdhci-pci)", item["evidence"])


if __name__ == "__main__":
    unittest.main()
