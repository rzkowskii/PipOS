"""Check the packaged animation on explicitly selected Try USB media."""

import os
import time
import unittest
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

from lab.vm import VM, eyes, machine_args


def usb_machine_args(*args, **kwargs):
    command = machine_args(*args, **kwargs)
    boot_device = command.index("ide-hd,drive=disk0,bus=ide.0")
    del command[boot_device - 1 : boot_device + 1]
    controller = command.index("qemu-xhci")
    command[controller] = "qemu-xhci,id=usb-controller"
    return command + ["-device", "usb-storage,drive=disk0,bus=usb-controller.0,bootindex=1"]


class VisibleEye(NamedTuple):
    center: float
    width: int
    height: int
    green: int


class FaceUSB(unittest.TestCase):
    def sh(self, command):
        status, output = self.vm.run(command, 15)
        self.assertEqual(status, 0, f"{command}: {output}")
        return output

    def visible_eyes(self):
        width, height, rgb = self.vm.screen()
        left, right, cyan = eyes(width, height, rgb)
        if not left or not right or cyan <= 0.95:
            return ()
        self.assertEqual((width, height), (1280, 800))
        visible = []
        for x0, y0, x1, y1 in (left, right):
            x, y = (x0 + x1) // 2, (y0 + y1) // 2
            green = max(rgb[(py * width + px) * 3 + 1]
                        for py in (y - 6, y, y + 6) for px in (x - 6, x, x + 6))
            visible.append(VisibleEye((x0 + x1) / 2, x1 - x0, y1 - y0, green))
        return tuple(visible)

    def wait_pose(self, description, matches, timeout=5):
        deadline = time.monotonic() + timeout
        consecutive = 0
        visible = ()
        while time.monotonic() < deadline:
            visible = self.visible_eyes()
            consecutive = consecutive + 1 if visible and matches(visible) else 0
            if consecutive == 2:
                return visible
            time.sleep(0.2)
        self.fail(f"the display did not show {description}, last detected eyes: {visible}")

    def test_try_usb_displays_motion_and_body_cues(self):
        selected = os.environ.get("BEING_FACE_USB_IMAGE")
        if not selected:
            self.skipTest("set BEING_FACE_USB_IMAGE to the Try USB image to check")
        image = Path(selected).expanduser().resolve()
        self.assertTrue(image.is_file(), f"no Try USB image at {image}")
        expected_sha = os.environ.get("BEING_FACE_SHA256")
        if expected_sha:
            self.assertRegex(expected_sha, r"\A[0-9a-fA-F]{64}\Z")

        self.vm = VM(image=image)
        self.addCleanup(self.vm.destroy)
        with patch("lab.vm.machine_args", usb_machine_args):
            self.vm.start()
        self.vm.login(120)
        self.assertIn("being.try=1", self.sh("cat /proc/cmdline").split())
        self.vm.select_try_from_screen()
        self.sh("systemctl is-active --quiet being-face")
        actual_sha = self.sh("sha256sum /usr/bin/being-face").split()[0]
        if expected_sha:
            self.assertEqual(actual_sha, expected_sha.lower())
        # The unit is active as soon as the process starts. The renderer line
        # follows SDL and GL initialisation, which can take seconds under TCG.
        started = time.monotonic()
        renderer = ""
        while time.monotonic() - started < 60 and "face: drawn by" not in renderer:
            renderer = self.sh("journalctl -b -u being-face --no-pager -o cat | grep 'face: drawn by' | tail -1 || true")
            if "face: drawn by" not in renderer:
                time.sleep(0.5)
        restarts = self.sh("systemctl show -p NRestarts -p ExecMainPID -p ActiveEnterTimestampMonotonic being-face")
        print(f"  renderer line after {time.monotonic() - started:.1f}s: {renderer!r}; {restarts!r}", flush=True)
        self.assertIn("face: drawn by", renderer)
        self.assertIn("NRestarts=0", restarts)
        self.vm.wait_state("idle", 120)

        # Verify visible gaze motion rather than merely changed pixels.
        centers = []

        def moving(visible):
            if any(eye.width < 200 or eye.height < 230 or eye.green < 170 for eye in visible):
                return False
            centers.append(sum(eye.center for eye in visible) / 2)
            return max(centers) - min(centers) >= 8

        awake = self.wait_pose("awake gaze motion", moving, timeout=15)

        def tired(visible):
            return all(
                eye.width >= 0.85 * initial.width
                and 0.42 * initial.height <= eye.height <= 0.78 * initial.height
                and 0.42 * initial.green <= eye.green <= 0.77 * initial.green
                for eye, initial in zip(visible, awake)
            )

        self.vm.say("/battery 10")
        low = self.wait_pose("a broad, lowered, dimmer low-charge expression", tired)
        self.vm.say("/plug")
        self.wait_pose("the open, bright expression after plugging in", lambda visible: all(
            eye.height >= 0.86 * initial.height and eye.green >= 0.89 * initial.green
            for eye, initial in zip(visible, awake)
        ))

        self.vm.say("/battery 10")
        self.wait_pose("the low-charge expression after unplugging", tired)
        self.vm.qmp("send-key", keys=[{"type": "qcode", "data": "m"}])
        muted = self.wait_pose("narrower and shorter eyes when muted during low charge", lambda visible: all(
            0.55 * initial.width <= eye.width <= 0.86 * initial.width
            and 0.08 * initial.height <= eye.height <= 0.28 * initial.height
            for eye, initial in zip(visible, awake)
        ))

        print(f"\n  {image.name}: {renderer}, face SHA-256 {actual_sha}", flush=True)
        print(f"  idle moved {max(centers) - min(centers):.0f}px. Visible eye heights "
              f"{[eye.height for eye in awake]} → {[eye.height for eye in low]} → "
              f"{[eye.height for eye in muted]}px for awake, low charge and mute.", flush=True)


if __name__ == "__main__":
    unittest.main()
