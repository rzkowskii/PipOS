"""L1: eyes open.

Power-on leads to the face and nothing else: no login prompt, no boot text on
the screen. The face is alive (it moves), and it survives its own crashes.
"""

import time
import unittest

from lab.vm import IMAGE, LAB_OUTPUT, VM, eyes

BOOT_PATIENCE = 300
SHOTS = LAB_OUTPUT


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class L1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = VM().start()
        try:
            # The firmware draws its own splash, and that stays up until the
            # kernel takes over the display. From then on, nothing but the
            # face may appear.
            cls.vm.expect(r"Booting `PipOS'", 120)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and any(eyes(*cls.vm.screen())[:2]):
                time.sleep(0.25)
            cls.first_eyes = None
            cls.not_face = []
            deadline = time.monotonic() + BOOT_PATIENCE
            while time.monotonic() < deadline:
                left, right, cyan = eyes(*cls.vm.screen())
                if left or right:
                    if cyan < 0.9:
                        cls.not_face.append(round(time.monotonic() - cls.vm.started, 1))
                    elif left and right:
                        cls.first_eyes = time.monotonic() - cls.vm.started
                        break
                time.sleep(0.5)
            cls.vm.login(BOOT_PATIENCE)
        except Exception:
            cls.vm.destroy()
            raise
        if cls.first_eyes is not None:
            print(f"\n  power-on to eyes: {cls.first_eyes:.1f}s (emulated)")

    @classmethod
    def tearDownClass(cls):
        cls.vm.destroy()

    def sh(self, command, timeout=60):
        status, out = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"`{command}` failed:\n{out}")
        return out

    def wait_for_eyes(self, timeout=60, both=True):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            left, right, cyan = eyes(*self.vm.screen())
            if (left and right) if both else (left or right):
                return left, right, cyan
            time.sleep(0.5)
        self.vm.screenshot(SHOTS / "l1-no-eyes.png")
        self.fail("no eyes on the screen")

    def test_power_on_leads_to_eyes_and_nothing_else(self):
        self.assertIsNotNone(self.first_eyes, "the eyes never appeared")
        self.assertEqual(self.not_face, [], "something other than the face was on the screen while booting")
        self.vm.screenshot(SHOTS / "l1-eyes.png")

    def test_there_are_two_eyes_where_eyes_should_be(self):
        left, right, cyan = self.wait_for_eyes()
        width, height, _ = self.vm.screen()
        self.assertEqual((width, height), (1280, 800), "the virtual screen should match the laptop's")
        self.assertGreater(cyan, 0.95)
        for box in (left, right):
            w, h = box[2] - box[0], box[3] - box[1]
            self.assertGreater(w, 40)
            self.assertGreater(h, 8)
            self.assertLess(w, width // 2)
        # Roughly mirrored about the middle of the screen.
        self.assertLess(abs((left[0] + right[2]) / 2 - width / 2), width * 0.12)

    def test_the_face_is_alive(self):
        self.wait_for_eyes()
        frames = set()
        for _ in range(12):
            frames.add(hash(self.vm.screen()[2]))
            time.sleep(1.0)
        self.assertGreater(len(frames), 3, "the picture did not change: the face is frozen")

    def test_it_is_drawn_in_software_at_reduced_size_here(self):
        log = self.sh("journalctl -b -u being-face --no-pager -o cat | grep 'drawn by' | tail -1")
        self.assertIn("llvmpipe", log)
        self.assertIn("at 1/4 size", log)

    def test_the_brainstem_reaches_idle_with_only_a_face(self):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            log = self.vm.run("journalctl -b -u being-brainstem --no-pager -o cat")[1]
            if "waking -> idle" in log:
                return
            time.sleep(2)
        self.fail(f"the brainstem never reached idle:\n{log}")

    def test_nothing_else_is_running_on_the_screen(self):
        # No login prompt on any screen terminal: the only way in is the serial line.
        units = self.sh("systemctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}'")
        self.assertNotIn("getty@tty1.service", units.split())
        self.assertIn("being-face.service", units.split())
        self.assertIn("being-brainstem.service", units.split())

    def test_a_crashed_face_comes_back(self):
        self.wait_for_eyes()
        before = self.sh("systemctl show -p MainPID --value being-face")
        self.sh("kill -9 " + before)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            after = self.vm.run("systemctl show -p MainPID --value being-face")[1]
            if after not in ("0", before):
                break
            time.sleep(1)
        self.assertNotIn(after, ("0", before), "systemd did not restart the face")
        self.wait_for_eyes(90)

    def test_losing_the_brainstem_shows_on_the_face(self):
        left, right, _ = self.wait_for_eyes()
        self.sh("systemctl stop being-brainstem")
        try:
            # With no heartbeat, the face turns to its unwell look: two eyes
            # of clearly different sizes. Give the slow renderer time.
            deadline = time.monotonic() + 60
            uneven = False
            while time.monotonic() < deadline and not uneven:
                left, right, _ = self.wait_for_eyes()
                lw, rw = left[2] - left[0], right[2] - right[0]
                lh, rh = left[3] - left[1], right[3] - right[1]
                uneven = abs(lw - rw) > 0.15 * max(lw, rw) and abs(lh - rh) > 0.15 * max(lh, rh)
                time.sleep(1)
            self.vm.screenshot(SHOTS / "l1-unwell.png")
            self.assertTrue(uneven, "the face did not show that the brainstem was gone")
        finally:
            self.sh("systemctl start being-brainstem")
        # And it recovers: the eyes match again.
        deadline = time.monotonic() + 60
        even = False
        while time.monotonic() < deadline and not even:
            left, right, _ = self.wait_for_eyes()
            lw, rw = left[2] - left[0], right[2] - right[0]
            even = abs(lw - rw) < 0.12 * max(lw, rw)
            time.sleep(1)
        self.assertTrue(even, "the face did not recover when the brainstem returned")


if __name__ == "__main__":
    unittest.main()
