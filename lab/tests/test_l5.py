"""Exercise the lid privacy policy, visible wake, and Linux input adapter."""

import json
import shlex
import time
import unittest

from lab.vm import IMAGE, LAB_OUTPUT, VM, eyes

EVENTS = "/run/l5-events"
BUS = "/run/being/bus.sock"
NATIVE_BUS = "/run/l5-native.sock"
PROBE = "/run/l5-bus.py"

BUS_PROBE = r"""
import json, socket, sys

with socket.socket(socket.AF_UNIX) as bus:
    bus.connect(sys.argv[1])
    hello = {"t": "hello", "name": "l5-" + sys.argv[2], "sub": []}
    if sys.argv[2] == "watch":
        hello["sub"] = ["body.lid", "body.state", "state.changed", "panel.ready", "face.gaze", "say.*", "hear.*"]
    bus.sendall((json.dumps(hello) + "\n").encode())
    if sys.argv[2] == "watch":
        for line in bus.makefile():
            if json.loads(line)["t"] != "say.level":
                print(line, end="", flush=True)
    else:
        bus.sendall((sys.argv[3] + "\n").encode())
"""

VIRTUAL_LID = r"""
import fcntl, os, struct

fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, 0x40045564, 5)  # UI_SET_EVBIT, EV_SW
fcntl.ioctl(fd, 0x4004556d, 0)  # UI_SET_SWBIT, SW_LID
fcntl.ioctl(fd, 0x405c5503, struct.pack("@4H80sI", 0x03, 1, 1, 1, b"L5 test lid", 0))
fcntl.ioctl(fd, 0x5501)  # UI_DEV_CREATE
packet = struct.Struct("@llHHi")

def report(closed):
    os.write(fd, packet.pack(0, 0, 5, 0, int(closed)) + packet.pack(0, 0, 0, 0, 0))

try:
    report(True)
    command_fd = os.open("/run/l5-switch", os.O_RDWR)
    with os.fdopen(command_fd) as commands:
        for command in commands:
            command = command.strip()
            if command == "remove":
                break
            report(command == "close")
finally:
    fcntl.ioctl(fd, 0x5502)  # UI_DEV_DESTROY
    os.close(fd)
"""


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class Lid(unittest.TestCase):
    def setUp(self):
        self.vm = VM(loopback=True)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(120)
        self.vm.put(PROBE, BUS_PROBE)

    def sh(self, command):
        status, out = self.vm.run(command, 15)
        self.assertEqual(status, 0, f"{command}: {out}")
        return out

    def watch(self, bus=BUS):
        self.sh(f"(python3 {PROBE} {bus} watch >{EVENTS} 2>/run/l5-bus.err &)")

    def send(self, topic, *, bus=BUS, **fields):
        message = shlex.quote(json.dumps({"t": topic, **fields}))
        self.sh(f"python3 {PROBE} {bus} send {message}")

    def events(self):
        events = []
        for line in self.sh(f"cat {EVENTS}").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def event(self, topic, after=0, timeout=12, **fields):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events()[after:]:
                if event["t"] == topic and all(key in event and event[key] == value for key, value in fields.items()):
                    return event
            time.sleep(0.1)
        self.fail(f"missing {topic} {fields} after event {after}: {self.events()[after:]}")

    def wait_shell(self, command, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.vm.run(command, 4)[0] == 0:
                return
            time.sleep(0.1)
        self.fail(f"did not become true: {command}")

    def wait_screen(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate(self.vm.screen()):
                return
            time.sleep(0.1)
        self.fail("the face did not reach the expected screen state")

    def key(self, name):
        self.vm.qmp("send-key", keys=[{"type": "qcode", "data": name}])

    def set_lid(self, closed):
        mark = len(self.events())
        self.send("sim.body", lid_closed=closed)
        return self.event("body.lid", after=mark, closed=closed)

    def test_close_stops_audio_and_reopen_wakes_without_the_cortex(self):
        self.watch()
        self.event("state.changed", timeout=25, state="idle")
        self.wait_shell("pidof arecord")
        self.sh("systemctl stop --no-block being-cortex")
        # QEMU can take longer than the ordinary serial command timeout to
        # let llama.cpp finish the batch it was decoding on shutdown.
        self.wait_shell('test "$(systemctl show -p MainPID --value being-cortex)" = 0', timeout=110)
        mark = len(self.events())
        self.send("sim.body", battery=37, on_ac=False, charging=False)
        self.event("body.state", after=mark, battery=37, on_ac=False)
        self.send("hear.wake")
        self.event("state.changed", after=mark, state="attending")
        self.send("hear.final", text="how much charge do you have")
        self.event("say.started", after=mark)
        self.assertTrue(any("37 percent" in e["text"] for e in self.events()[mark:] if e["t"] == "say.request"))

        closed_mark = len(self.events())
        self.set_lid(True)
        self.event("say.stop", after=closed_mark)
        self.event("state.changed", after=closed_mark, state="asleep")
        self.wait_shell("! pidof aplay && ! pidof arecord")
        self.wait_screen(lambda frame: not any(frame[2]))
        blocked_mark = len(self.events())
        self.send("hear.wake")
        self.send("say.request", text="late reply", seq=99, last=True)
        self.assertFalse(any(e["t"].startswith(("say.", "hear.")) for e in self.events()[blocked_mark:]))

        # The preferred mute state survives close and recovery. Lose the mind
        # as well to prove the face does not depend on either model process.
        self.key("m")
        self.event("state.changed", after=closed_mark, state="asleep", muted=True)
        self.sh("systemctl stop being-mind")
        opened = self.set_lid(False)
        self.event("panel.ready", after=closed_mark, seq=opened["seq"])
        self.event("state.changed", after=closed_mark, state="idle", muted=True, unwell=True)
        self.wait_screen(lambda frame: all(eyes(*frame)[:2]))
        self.sh("! pidof arecord && ! pidof aplay")
        self.vm.screenshot(LAB_OUTPUT / "l5-lid-awake.png")

        mark = len(self.events())
        self.send("sim.body", lid_closed=False)
        self.assertFalse(any(e["t"] in ("body.lid", "panel.ready") for e in self.events()[mark:]))
        self.vm.qmp("input-send-event", events=[
            {"type": "abs", "data": {"axis": "x", "value": 16384}},
            {"type": "abs", "data": {"axis": "y", "value": 16384}},
            {"type": "btn", "data": {"down": True, "button": "left"}},
        ])
        time.sleep(0.15)
        self.vm.qmp("input-send-event", events=[
            {"type": "btn", "data": {"down": False, "button": "left"}},
        ])
        self.event("face.gaze", after=mark, x=0, y=-0.75)

        self.set_lid(True)
        self.set_lid(False)
        self.set_lid(True)
        self.wait_screen(lambda frame: not any(frame[2]))
        missing = self.set_lid(None)
        self.wait_screen(lambda frame: not any(frame[2]))
        mark = len(self.events())
        self.key("f9")
        recovery = self.event("body.lid", after=mark, closed=None, show_face=True)
        self.assertNotEqual(recovery["seq"], missing["seq"])
        self.event("panel.ready", after=mark, seq=recovery["seq"])
        self.key("m")
        self.event("state.changed", after=mark, state="asleep", muted=False)
        self.sh("! pidof arecord")
        mark = len(self.events())
        self.set_lid(False)
        self.event("state.changed", after=mark, state="idle", muted=False)
        self.wait_shell("pidof arecord")
        mark = len(self.events())
        self.send("hear.wake", lid_seq=opened["seq"])
        self.send("being.sleep", lid_seq=opened["seq"])
        self.assertFalse(any(e["t"] in ("hear.wake", "state.changed") for e in self.events()[mark:]))

    def test_linux_reads_switch_state_and_fails_closed_on_device_loss(self):
        self.wait_shell(
            "journalctl -b -u being-brainstem --no-pager -o cat | "
            "grep -Fq 'brainstem: QEMU lab timeouts, think 1800s, speak 900s'"
        )
        # Give a second brainstem the native hardware view of this same guest.
        self.sh(
            "systemd-run --unit=l5-native "
            "-p InaccessiblePaths=/sys/firmware/qemu_fw_cfg "
            f"/usr/bin/being-brainstem --appliance-body --bus {NATIVE_BUS} --require ''"
        )
        self.wait_shell(f"test -S {NATIVE_BUS}")
        self.wait_shell(
            "journalctl -b -u l5-native --no-pager -o cat | "
            "grep -Fq 'brainstem: standard timeouts, think '"
        )
        self.watch(NATIVE_BUS)
        self.event("body.lid", closed=None, snapshot=True)
        self.send("sim.body", bus=NATIVE_BUS, lid_closed=False)
        self.assertFalse(any(event.get("closed") is False for event in self.events()))
        self.vm.put("/run/l5-switch.py", VIRTUAL_LID)
        self.sh("mkfifo /run/l5-switch && (python3 /run/l5-switch.py >/run/l5-switch.log 2>&1 &)")
        self.event("body.lid", closed=True)
        mark = len(self.events())
        self.sh("echo open > /run/l5-switch")
        self.event("body.lid", after=mark, closed=False)
        self.event("state.changed", after=mark, state="idle")
        mark = len(self.events())
        self.sh("echo remove > /run/l5-switch")
        self.event("body.lid", after=mark, closed=None)
        self.event("state.changed", after=mark, state="asleep")
