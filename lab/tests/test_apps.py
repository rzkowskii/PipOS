"""The workshop apps inside PipOS, over the real bus (PIP-FUNCTIONS L9.6).

One booted lab image: the terminal opens, minimises, restores and closes over
the bus, and its panel really hides (the frame's visible bit, through
SIGUSR2); closing it kills its whole cgroup, a background program included;
the body monitor and the clock open over it, replace each other and close,
each in its own renderer unit; and the face is back to its two eyes after.

It runs only on an image built from this tree (lab/source_id.py): an older
image would test other code. With BEING_APPS_LOCAL_SCREEN=1 it also runs the
local-screen suite, which must stay green (19 of 19 checks).
"""

import json
import os
import shlex
import struct
import time
import unittest

from lab.vm import IMAGE, VM, eyes

BOOT = 180
FRAME_DIR = "/run/being-panel"
SLICE = "/sys/fs/cgroup/workshop.slice"
VISIBLE = 1

BUS_PROBE = r"""
import json, socket, sys

with socket.socket(socket.AF_UNIX) as bus:
    bus.connect('/run/being/bus.sock')
    watch = len(sys.argv) == 1
    topics = ['app.state', 'app.refused', 'term.status'] if watch else []
    name = 'lab-apps-watch' if watch else 'lab-apps'
    bus.sendall((json.dumps({'t': 'hello', 'name': name, 'sub': topics}) + '\n').encode())
    if watch:
        for raw in bus.makefile():
            print(raw, end='', flush=True)
    else:
        bus.sendall((sys.argv[1] + '\n').encode())
"""

# Reads a frame header as the face does (crates/face/src/panel.rs):
# flags at 32, the title at 96.
FRAME_PROBE = r"""
import struct, sys
with open(sys.argv[1], 'rb') as frame:
    head = frame.read(128)
print(head[:4].decode(), struct.unpack_from('<I', head, 32)[0], head[96:128].rstrip(b'\0').decode())
"""

KEY = r"""
import socket, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as keys:
    for byte in sys.argv[1].encode().decode('unicode_escape').encode():
        keys.sendto(bytes([byte]), '/run/being-hands/keys')
"""


def fresh_image():
    """Why the suite cannot run here, or None."""
    if not IMAGE.exists():
        return "no image: run lab/build.sh first"
    from lab import source_id
    status, message = source_id.check(IMAGE)
    if status != "match" and os.environ.get("BEING_ALLOW_STALE") != "1":
        return f"pending: no image built from this tree ({message})"
    return None


class Apps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = fresh_image()
        if reason:
            raise unittest.SkipTest(reason)
        cls.vm = VM(display=True)
        cls.vm.start()
        try:
            cls.vm.login(BOOT)
            cls.run_sh(f"cat > /run/apps-bus.py <<'EOF'\n{BUS_PROBE}\nEOF")
            cls.run_sh(f"cat > /run/apps-frame.py <<'EOF'\n{FRAME_PROBE}\nEOF")
            cls.run_sh(f"cat > /run/apps-key.py <<'EOF'\n{KEY}\nEOF")
            cls.run_sh("until systemctl is-active -q being-hands.service; do sleep 1; done", 120)
            cls.run_sh("(python3 /run/apps-bus.py > /run/apps-events 2>&1 &)")
            time.sleep(1)
        except Exception:
            cls.vm.destroy()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.vm.destroy()

    @classmethod
    def run_sh(cls, command, timeout=30):
        status, output = cls.vm.run(command, timeout)
        if status != 0:
            raise AssertionError(f"{command}:\n{output}")
        return output

    def sh(self, command, timeout=30):
        return self.run_sh(command, timeout)

    def ok(self, command):
        return self.vm.run(command, 15)[0] == 0

    def wait(self, check, timeout=20, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.25)
        self.fail(f"timed out waiting for {what}")

    def events(self):
        out = []
        for line in self.sh("cat /run/apps-events").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def ask(self, topic, wait_for="app.state", **fields):
        req = f"lab{time.monotonic_ns() % 10**9}"
        payload = shlex.quote(json.dumps({"t": topic, "req": req, **fields}))
        self.sh(f"python3 /run/apps-bus.py {payload}")
        return self.wait(lambda: next((e for e in self.events() if e.get("req") == req and e["t"] in
                                       (wait_for, "app.refused")), None), what=f"{topic} answered")

    def frame(self, app):
        """(magic, flags, title) of an app's frame, or None when it has none."""
        status, output = self.vm.run(f"python3 /run/apps-frame.py {FRAME_DIR}/{app}.frame", 15)
        if status != 0:
            return None
        magic, flags, *title = output.split(" ")
        return magic, int(flags), " ".join(title)

    def visible(self, app):
        frame = self.frame(app)
        return frame is not None and bool(frame[1] & VISIBLE)

    def unit_active(self, unit):
        return self.ok(f"systemctl is-active -q {unit}")

    def shell_unit(self):
        state = [e for e in self.events() if e["t"] == "app.state" and e.get("app") == "terminal"][-1]
        return state.get("session")

    # -- the terminal --------------------------------------------------------------------

    def test_1_open_minimise_restore_close_and_close_kills_the_cgroup(self):
        state = self.ask("app.open", app="terminal")
        self.assertEqual((state["t"], state["view"]), ("app.state", "shown"))
        unit = self.shell_unit()
        self.assertRegex(unit, r"^being-shell-[0-9]+$")
        self.assertTrue(self.unit_active(f"{unit}.service"))
        self.assertTrue(self.unit_active("being-panel-terminal.service"))
        self.wait(lambda: self.visible("terminal"), what="the terminal's frame shown")
        # A program left running in the background: close must take it too.
        self.wait(lambda: self.ok(f"test -d {SLICE}/{unit}.service"), what="the session cgroup")
        self.sh("python3 /run/apps-key.py 'sleep 600 &\\r'")
        self.wait(lambda: len(self.sh(f"cat {SLICE}/{unit}.service/cgroup.procs").split()) >= 2,
                  what="the background sleep in the cgroup")

        state = self.ask("app.minimise", app="terminal")
        self.assertEqual(state["view"], "minimised")
        self.wait(lambda: not self.visible("terminal"), what="the panel really hidden (SIGUSR2)")
        self.assertTrue(self.unit_active(f"{unit}.service"), "minimised: the session keeps running")

        state = self.ask("app.restore", app="terminal")
        self.assertEqual(state["view"], "shown")
        self.wait(lambda: self.visible("terminal"), what="the panel shown again (SIGUSR1)")

        state = self.ask("app.close", app="terminal")
        self.assertEqual(state["view"], "closed")
        self.wait(lambda: not self.unit_active(f"{unit}.service"), what="the session unit stopped")
        self.wait(lambda: not self.ok(f"test -d {SLICE}/{unit}.service"), what="the cgroup gone")
        self.assertFalse(self.ok("pgrep -u being-shell"), "nothing of the session survives")
        self.wait(lambda: self.frame("terminal") is None, what="the frame file removed")
        self.assertFalse(self.unit_active("being-panel-terminal.service"))

    # -- the body monitor and the clock --------------------------------------------------------

    def test_2_monitor_and_clock_replace_each_other_and_close(self):
        self.ask("app.open", app="terminal")
        state = self.ask("app.open", app="monitor")
        self.assertEqual((state["t"], state["view"]), ("app.state", "shown"))
        self.wait(lambda: self.visible("monitor"), what="the monitor's frame shown")
        self.assertEqual(self.frame("monitor")[2], "How I'm feeling")
        self.assertTrue(self.unit_active("being-panel-monitor.service"))
        self.wait(lambda: not self.visible("terminal"), what="the terminal minimised under it")

        state = self.ask("app.open", app="clock")
        self.assertEqual(state["view"], "shown")
        self.wait(lambda: self.frame("monitor") is None, what="the monitor closed")
        self.wait(lambda: self.visible("clock"), what="the clock shown")
        self.assertEqual(self.frame("clock")[2], "Pip's clock")

        state = self.ask("app.minimise", app="clock")
        self.assertEqual(state["view"], "closed", "the small apps close instead of minimising")
        self.wait(lambda: self.frame("clock") is None, what="the clock's frame removed")
        self.assertFalse(self.unit_active("being-panel-clock.service"))

        self.ask("app.close", app="terminal")
        width, height, rgb = self.wait(lambda: self._eyes_back(), timeout=20, what="the two eyes back")
        self.assertGreater(width, 0)

    def _eyes_back(self):
        width, height, rgb = self.vm.screen()
        left, right, cyan = eyes(width, height, rgb)
        return (width, height, rgb) if left and right and cyan > 0.5 else None

    def test_3_guest_mode_refuses_every_app(self):
        payload = shlex.quote(json.dumps({"t": "app.privacy", "guest": True}))
        self.sh(f"python3 /run/apps-bus.py {payload}")
        try:
            for app in ("terminal", "monitor", "clock"):
                reply = self.ask("app.open", app=app)
                self.assertEqual((reply["t"], reply.get("reason")), ("app.refused", "guest"), app)
        finally:
            payload = shlex.quote(json.dumps({"t": "app.privacy", "guest": False}))
            self.sh(f"python3 /run/apps-bus.py {payload}")


@unittest.skipUnless(os.environ.get("BEING_APPS_LOCAL_SCREEN") == "1",
                     "set BEING_APPS_LOCAL_SCREEN=1 to rerun the local-screen suite here")
class LocalScreenStaysGreen(unittest.TestCase):
    def test_local_screen_suite(self):
        reason = fresh_image()
        if reason:
            self.skipTest(reason)
        from lab.tests import test_local_screen
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_local_screen)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        self.assertTrue(result.wasSuccessful(), "the local-screen suite must stay 19 of 19")
        self.assertFalse(result.skipped, "nothing of it may be skipped")


if __name__ == "__main__":
    unittest.main()
