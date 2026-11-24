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
        host.made("/sys/class/net/lo/type", "772\n")
        item = system.check_network(host)
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["value"]["wireless"], "not available on this build")
        self.assertIn("enp1s0f0 up carrier=1 1000 Mb/s", item["evidence"])

    def test_battery_health(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root)
        base = "/sys/class/power_supply/BAT0"
        for name, value in (("type", "Battery"), ("present", "1"), ("capacity", "88"), ("status", "Discharging"),
                            ("charge_full", "4000000"), ("charge_full_design", "8000000"), ("cycle_count", "512")):
            host.made(f"{base}/{name}", value + "\n")
        item = system.check_battery(host)
        self.assertEqual((item["status"], item["value"]["health_percent"]), ("warn", 50))


if __name__ == "__main__":
    unittest.main()
