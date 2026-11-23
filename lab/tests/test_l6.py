"""Exercise camera permission, lid privacy and face tracking inside PipOS."""

import json
import os
from pathlib import Path
import shlex
import time
import unittest

from lab.vm import IMAGE, VM, eyes


BUS_PROBE = r"""
import json, socket, sys

with socket.socket(socket.AF_UNIX) as bus:
    bus.connect('/run/being/bus.sock')
    watch = len(sys.argv) == 1
    topics = ['camera.policy', 'body.lid', 'face.gaze', 'state.changed', 'see.presence'] if watch else []
    name = 'l6-watch' if watch else 'l6-stimulus'
    bus.sendall((json.dumps({'t': 'hello', 'name': name, 'sub': topics}) + '\n').encode())
    if watch:
        for raw in bus.makefile():
            print(raw, end='', flush=True)
    else:
        bus.sendall((sys.argv[1] + '\n').encode())
"""

DETECTOR = """#!/usr/bin/python3
import time

while True:
    print('{"present":true,"x":0.65,"y":-0.2}', flush=True)
    time.sleep(0.2)
"""


class Camera(unittest.TestCase):
    def sh(self, command):
        status, output = self.vm.run(command, 20)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def wait(self, command, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, output = self.vm.run(command, 8)
            if status == 0:
                return output
            time.sleep(0.15)
        self.fail(f"timed out waiting for {command}:\n{output}")

    def events(self):
        output = self.sh("cat /run/l6-events")
        try:
            return [json.loads(line) for line in output.splitlines()]
        except json.JSONDecodeError:
            return [json.loads(line) for line in output.splitlines()[:-1]]

    def event(self, topic, start=0, **fields):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for event in self.events()[start:]:
                if event["t"] == topic and all(event.get(k) == v for k, v in fields.items()):
                    return event
            time.sleep(0.15)
        self.fail(f"no {topic} with {fields} after {start}: {self.events()[start:]}")

    def send(self, topic, **fields):
        payload = shlex.quote(json.dumps({"t": topic, **fields}))
        self.sh(f"python3 /run/l6-bus.py {payload}")

    def screen_gaze(self, matches):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            width, height, rgb = self.vm.screen()
            left, right, _ = eyes(width, height, rgb)
            if left and right:
                offset = (left[0] + left[2] + right[0] + right[2]) / 4 - width / 2
                if matches(offset):
                    return offset
            time.sleep(0.15)
        self.fail("the rendered face did not reach the expected gaze position")

    def test_camera_follows_face_and_enforces_local_privacy(self):
        image = Path(os.environ.get("BEING_L6_IMAGE", IMAGE)).resolve()
        if not image.exists():
            self.skipTest("build the L6 image first")
        self.vm = VM(image=image, loopback=True)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(120)
        self.sh("test -d /sys/bus/usb/drivers/uvcvideo")
        self.sh("test -x /usr/libexec/being/eyes-capture && test -s /usr/share/being/facefinder")
        status, output = self.vm.run("/usr/libexec/being/eyes-capture --device /dev/null "
                                     "--cascade /usr/share/being/facefinder", 15)
        self.assertEqual(status, 1, output)
        self.assertIn("query camera capabilities", output)
        self.assertIn("permission disabled", self.sh("being-camera status"))
        self.vm.put("/run/l6-bus.py", BUS_PROBE)
        self.vm.put("/run/l6-detector", DETECTOR)
        self.sh("chmod 0755 /run/l6-detector && mkdir -p /run/systemd/system/being-eyes.service.d")
        self.vm.put("/run/systemd/system/being-eyes.service.d/l6.conf", """[Service]
ExecStart=
ExecStart=/usr/bin/python3 -m mind.eyes --detector /run/l6-detector --device /dev/null
""")
        self.sh("systemctl daemon-reload && (python3 /run/l6-bus.py >/run/l6-events 2>/run/l6-bus.err &)")
        self.event("state.changed", state="idle", unwell=False)
        opened = self.event("body.lid", closed=False)
        self.send("camera.request", enabled=True)
        self.send("see.presence", present=True, x=0.8, y=0.0, camera_seq=0, lid_seq=opened["seq"])
        self.assertFalse(any(e["t"] in {"face.gaze", "see.presence"} for e in self.events()))

        self.assertIn("permission enabled", self.sh("being-camera enable"))
        self.sh("python3 -c 'import stat; from pathlib import Path; f=Path(\"/mind/camera.enabled\").lstat(); "
                "assert f.st_uid == 0 and stat.S_IMODE(f.st_mode) == 0o600'")
        self.event("camera.policy", allowed=True)
        self.event("face.gaze", x=0.65, y=-0.2)
        tracked = self.screen_gaze(lambda offset: offset > 24)
        self.assertEqual([e["state"] for e in self.events() if e["t"] == "state.changed"][-1], "idle")
        child_list = 'pid=$(systemctl show --property=MainPID --value being-eyes.service); '
        child_list += 'group=$(systemctl show --property=ControlGroup --value being-eyes.service); '
        child_list += 'test -n "$group" && grep -vx "$pid" "/sys/fs/cgroup$group/cgroup.procs"'
        child = self.wait(f'children=$({child_list}); test -n "$children" && echo "$children"').strip()
        self.assertTrue(child.isdecimal(), child)

        mark = len(self.events())
        self.send("sim.body", lid_closed=True)
        revoked = self.event("camera.policy", start=mark, allowed=False)
        self.event("face.gaze", start=mark, present=False)
        self.wait(f"! kill -0 {child} 2>/dev/null")
        events = self.events()
        self.assertFalse(any(e["t"] == "face.gaze" and "x" in e
                             for e in events[events.index(revoked, mark) + 1:]))

        mark = len(self.events())
        self.send("sim.body", lid_closed=False)
        self.event("camera.policy", start=mark, allowed=True)
        self.event("face.gaze", start=mark, x=0.65, y=-0.2)
        mark = len(self.events())
        self.assertIn("permission disabled", self.sh("being-camera disable"))
        self.event("camera.policy", start=mark, allowed=False)
        self.event("face.gaze", start=mark, present=False)
        self.sh("test ! -e /mind/camera.enabled && ! systemctl is-active --quiet being-eyes.service")
        centered = self.screen_gaze(lambda offset: abs(offset) < 16)
        print(f"  rendered gaze moved {tracked:.1f}px right and returned to {centered:.1f}px on opt-out", flush=True)


if __name__ == "__main__":
    unittest.main()
