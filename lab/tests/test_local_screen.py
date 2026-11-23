"""Drive the Try USB's actual display and keyboard through QMP."""

import json
import os
import re
import tempfile
import time
import unittest
from functools import cache
from pathlib import Path
from unittest.mock import patch

from lab.vm import LAB_OUTPUT, ROOT, VM, eyes, machine_args, png

SCREEN_IMAGE = Path(os.environ.get("BEING_SCREEN_IMAGE", ROOT / "out/images/beingos-try-screen.img")).resolve()
SHOTS = LAB_OUTPUT
# Sparse, so it costs nothing: large enough for the installer's model reserve
# (24 GiB) plus every staged pack, as on the MacBook's 256 GB SSD.
TARGET_BYTES = 64 << 30
CONSOLE_FONT = ROOT / "os/package/being/being-console-font.h"
# being-console's geometry (os/package/being/being-console-draw.h and measure()
# in being-console.c): per face the cell (width, height, cursor band under the
# letters) and the chrome (header, footer, side margin, space above and below
# the text). Change both together.
CONSOLE_FACES = ((8, 16, 2), (11, 24, 2), (16, 34, 3))
CONSOLE_CHROME = ((48, 32, 20, 12, 8), (76, 48, 64, 20, 12), (110, 68, 96, 28, 16))
CONSOLE_COLUMNS = CONSOLE_MAX_ROWS = 200
CONSOLE_FOOTER = bytes((12, 17, 27))  # the footer bar's flat fill
CONSOLE_INK = 64  # green level above which console_rows counts a pixel as drawn
BACKUP_AVAILABLE = "command -v being-backup && command -v age && command -v age-keygen"
RESTORE_NAME = "Aster"
RESTORE_LIFE = "e1" * 16
RESTORE_BORN = 1723000000
RESTORE_FACT = "My atlas is called Blueglow."
RESTORE_SNAPSHOT = "/run/restore/snapshot"
RESTORE_IDENTITY = "/run/restore/right"
WRONG_IDENTITY = "/run/restore/wrong"

BACKUP_FIXTURE = f"""
import json
import subprocess
from pathlib import Path

from mind.memory import Memory
from mind.profile import Profile, save_profile
from mind.retention import MemoryPolicy, save_policy

source = Path('/run/restore/source')
source.parent.mkdir(mode=0o700)
source.mkdir(mode=0o700)
(source / 'life.json').write_text(json.dumps({{'id': {RESTORE_LIFE!r}, 'born': {RESTORE_BORN}, 'wakes': 6}}))
(source / 'skills.json').write_text(json.dumps({{'time_zone': 'Australia/Sydney'}}))
save_profile(source, Profile(name={RESTORE_NAME!r}, owner='Casey', wake_word='Hey Pip', voice='awb'))
save_policy(source, MemoryPolicy('persistent', 18))
archive = Memory(source)
try:
    episode = archive.log('heard', {RESTORE_FACT!r}, 'Casey')
    archive.remember({RESTORE_FACT!r}, 'Casey', episode)
finally:
    archive.close()

def backup(*arguments):
    result = subprocess.run(['being-backup', *arguments], capture_output=True, text=True, timeout=100)
    if result.returncode:
        raise SystemExit(f'Synthetic backup creation failed at {{arguments[0]}}.')
    return result.stdout.strip()

recipient = backup('keygen', '--identity', {RESTORE_IDENTITY!r})
backup('keygen', '--identity', {WRONG_IDENTITY!r})
backup('create', '--mind-dir', str(source), '--output', {RESTORE_SNAPSHOT!r}, '--recipient', recipient)
"""


def console_layout(width, height):
    """(face, x0, y0, columns, rows, cell width, cell height, cursor band) as being-console lays out."""
    face = 2 if width >= 1480 and height >= 1000 else 1 if width >= 750 and height >= 550 else 0
    cell_width, cell_height, band = CONSOLE_FACES[face]
    header, footer, pad_x, pad_top, pad_bottom = CONSOLE_CHROME[face]
    columns = max(1, min(CONSOLE_COLUMNS, (width - 2 * pad_x) // cell_width))
    rows = max(1, min(CONSOLE_MAX_ROWS, (height - header - footer - pad_top - pad_bottom) // cell_height))
    return face, pad_x, header + pad_top, columns, rows, cell_width, cell_height, band


def console_screen(frame):
    """Whether the frame is being-console's: its flat footer bar under a dark blue backdrop."""
    width, height, rgb = frame
    if width < 64 or height < 64:
        return False

    def pixel(x, y):
        return rgb[(y * width + x) * 3:(y * width + x) * 3 + 3]

    footer = all(pixel(x, height - 3) == CONSOLE_FOOTER for x in (2, width // 2, width - 3))
    r, g, b = pixel(2, 2)
    return footer and r < 32 and g < 40 and 12 < b < 64 and b > r


def console_rows(frame):
    """Text raster signature: drawn pixels per line of the text area, without
    the header (whose eyes blink), the footer, or the cursor band."""
    width, height, rgb = frame
    if not console_screen(frame):
        return ()
    _, x0, y0, columns, rows, cell_width, cell_height, band = console_layout(width, height)
    green = rgb[1::3]
    ink = bytes(1 if value > CONSOLE_INK else 0 for value in range(256))
    counts = tuple(green[y * width + x0:y * width + x0 + columns * cell_width].translate(ink).count(1)
                   for row in range(rows) for y in range(y0 + row * cell_height, y0 + (row + 1) * cell_height - band))
    return counts if sum(counts) > 300 else ()


@cache
def console_glyphs():
    """{(cell width, cell height, weight): {character: coverage 0-15 per pixel}} from the console's font."""
    font = CONSOLE_FONT.read_text(encoding="utf-8")
    glyphs = {}
    for match in re.finditer(r'/\* (\d+)x(\d+) (regular|bold) U\+([0-9A-F]+)[^*]*\*/\s*((?:"[0-9a-f]*",?\s*)+)', font):
        coverage = tuple(int(digit, 16) for digit in "".join(re.findall(r'"([0-9a-f]*)"', match[5])))
        glyphs.setdefault((int(match[1]), int(match[2]), match[3]), {})[chr(int(match[4], 16))] = coverage
    return glyphs


def _normal(bits, size):
    """Text pixels are the minority in a cell; reversed text flips that, so flip it back."""
    return bits ^ ((1 << size) - 1) if bits.bit_count() * 2 > size else bits


@cache
def _console_masks(face):
    cell_width, cell_height, band = CONSOLE_FACES[face]
    size = cell_width * (cell_height - band)
    exact, nearest = {}, [(0, " ")]
    for weight in ("bold", "regular"):
        for char, coverage in sorted(console_glyphs()[(cell_width, cell_height, weight)].items(),
                                     key=lambda item: item[0] < "\x7f"):
            window = coverage[:size]
            top = max(window)
            if not top or char == " ":
                continue
            bits = _normal(int("".join("1" if 2 * value > top else "0" for value in window), 2), size)
            exact[bits] = char  # ASCII last, so it wins any tie
            nearest.append((bits, char))
    nearest.sort(key=lambda item: item[1] >= "\x7f")
    return exact, nearest, size


@cache
def _threshold(level):
    return bytes(0x31 if value > level else 0x30 for value in range(256))


def console_lines(frame):
    """The text being-console shows, read off its pixels against its own font:
    any palette colour, bold or not, reversed or on a panel. Blank lines are left out."""
    width, height, rgb = frame
    if not console_screen(frame):
        return []
    face, x0, y0, columns, rows, cell_width, cell_height, band = console_layout(width, height)
    exact, nearest, size = _console_masks(face)
    green = rgb[1::3]
    lines = []
    for row in range(rows):
        top = y0 + row * cell_height
        scan = [green[y * width + x0:y * width + x0 + columns * cell_width]
                for y in range(top, top + cell_height - band)]
        line = []
        for column in range(columns):
            left = column * cell_width
            window = b"".join(part[left:left + cell_width] for part in scan)
            low, high = min(window), max(window)
            if high - low < 28:
                line.append(" ")
                continue
            bits = _normal(int(window.translate(_threshold(low + (high - low) // 2)), 2), size)
            char = exact.get(bits)
            if char is None:
                char = min(nearest, key=lambda item: (item[0] ^ bits).bit_count())[1]
            line.append(char)
        if rendered := "".join(line).rstrip():
            lines.append(rendered)
    return lines


def visible_eyes(frame):
    left, right, cyan = eyes(*frame)
    return left and right and cyan > 0.95


@unittest.skipUnless(SCREEN_IMAGE.exists(), "needs a Try USB image with being.screen=1")
class LocalScreen(unittest.TestCase):
    def sh(self, command):
        status, output = self.vm.run(command, 30)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def start(self, target):
        def usb_boot_with_sata_target(*args, **kwargs):
            command = machine_args(*args, **kwargs)
            boot_device = command.index("ide-hd,drive=disk0,bus=ide.0")
            del command[boot_device - 1:boot_device + 1]
            controller = command.index("qemu-xhci")
            command[controller] = "qemu-xhci,id=usb-controller"
            return command + [
                "-device", "usb-storage,drive=disk0,bus=usb-controller.0,bootindex=1",
                "-drive", f"if=none,id=screen-target,format=raw,file={target}",
                "-device", "ide-hd,drive=screen-target,bus=ide.0,bootindex=2,model=BEINGOS SCREEN",
            ]

        self.vm = VM(image=SCREEN_IMAGE)
        self.addCleanup(self.vm.destroy)
        with patch("lab.vm.machine_args", usb_boot_with_sata_target):
            self.vm.start()
        self.vm.login(120)
        self.sh(
            "PYTHONPATH=/usr/lib/being PYTHONDONTWRITEBYTECODE=1 python3 -c "
            "'import binascii, sqlite3, ssl; "
            "from mind import __main__, initiative_setup, install_apply, local_screen; print(ssl.OPENSSL_VERSION)' "
            "&& command -v mkfs.fat mcopy grub-editenv"
        )

    def key(self, name):
        self.vm.qmp("send-key", keys=[{"type": "qcode", "data": name}])

    def select(self, choice):
        self.key(choice)
        self.key("ret")

    def type_line(self, text):
        for char in text:
            key = {" ": "spc", "/": "slash", ":": "semicolon"}.get(char, char.lower())
            names = ["shift", key] if char.isupper() or char == ":" else [key]
            self.vm.qmp("send-key", keys=[{"type": "qcode", "data": name} for name in names], **{"hold-time": 45})
            time.sleep(0.09)
        self.key("ret")

    def active_screen(self, unit, other):
        self.assertEqual(self.sh(f"systemctl is-active {unit}.service"), "active")
        status, state = self.vm.run(f"systemctl is-active {other}.service", 30)
        self.assertNotEqual(status, 0, f"{other}.service still owns display resources")
        self.assertEqual(state, "inactive")

    def screenshot(self, name, frame):
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / f"local-screen-{name}.png").write_bytes(png(*frame))

    def console(self, name=None, previous=(), timeout=50):
        deadline = time.monotonic() + timeout
        stable = ()
        while time.monotonic() < deadline:
            frame = self.vm.screen()
            rows = console_rows(frame)
            changed = (not previous or len(rows) != len(previous)
                       or sum(abs(old - new) for old, new in zip(previous, rows)) > 350)
            if rows and changed and rows == stable:
                if name:
                    self.screenshot(name, frame)
                return rows
            stable = rows
            time.sleep(0.35)
        self.screenshot(f"{name or 'menu'}-timeout", frame)
        journal = self.sh(
            "ls -la /run/being-screen 2>/dev/null; "
            "journalctl -b -u being-local-screen -u being-face --no-pager -n 50 -o cat"
        )
        self.fail(f"The console did not draw the expected {name or 'menu'} frame:\n{journal}")

    def face(self, name, timeout=55):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.vm.screen()
            if visible_eyes(frame):
                self.screenshot(name, frame)
                return
            time.sleep(0.35)
        self.screenshot(f"{name}-timeout", frame)
        self.fail("The usual PipOS eyes did not return after the console exited")

    def wait_text(self, phrase, *, prompt=False, screenshot=None, timeout=55):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.vm.screen()
            lines = console_lines(frame)
            if any(phrase in line for line in (lines[-1:] if prompt else lines)):
                if screenshot:
                    self.screenshot(screenshot, frame)
                return
            time.sleep(0.35)
        self.screenshot("restore-timeout", frame)
        self.fail(f"The screen did not show {phrase!r}. Last visible lines:\n" + "\n".join(lines[-12:]))

    def test_try_menu_keyboard_navigation_and_f2_return(self):
        files = tempfile.TemporaryDirectory(prefix="being-screen-lab-", dir="/tmp")
        self.addCleanup(files.cleanup)
        target = Path(files.name) / "target.raw"
        with target.open("wb") as disk:
            disk.truncate(TARGET_BYTES)
        before = target.stat()
        self.assertEqual(before.st_blocks, 0)

        self.start(target)
        self.assertIn("being.screen=1", self.sh("cat /proc/cmdline").split())
        first_menu = self.console("01-menu")
        self.active_screen("being-local-screen", "being-face")
        self.sh("pidof being-console")

        self.select("2")
        install = self.console("02-install", first_menu)
        self.key("ret")  # Leave the eligible target unselected.
        menu = self.console(previous=install)
        backup_status, _ = self.vm.run(BACKUP_AVAILABLE, 10)
        self.select("3")
        restore = self.console("03-restore", menu)
        if backup_status == 0:
            self.select("q")
            self.console(previous=restore)
        self.select("4")
        setup = self.console("04-setup", restore)
        self.select("9")
        initiative = self.console("04a-announcement-settings", setup)
        self.select("1")
        self.select("2")
        self.select("3")
        self.type_line("22:00")
        self.type_line("07:00")
        self.select("k")
        setup = self.console("04b-announcements-saved", initiative)
        policy = json.loads(self.sh("cat /mind/initiative.json"))
        self.assertEqual(policy, {"version": 1, "reminders": False, "greetings": True,
                                  "quiet_hours": {"start": "22:00", "end": "07:00"}})
        self.select("q")
        self.console(previous=setup)

        self.select("1")
        self.face("05-eyes")
        self.active_screen("being-face", "being-local-screen")
        self.key("f2")
        self.assertEqual(self.console("06-reopened"), first_menu, "F2 did not return to the original Try menu")
        self.active_screen("being-local-screen", "being-face")
        self.sh("pidof being-console")
        self.select("q")
        self.face("07-returned-eyes")
        self.active_screen("being-face", "being-local-screen")

        self.sh("sync")
        after = target.stat()
        self.assertEqual((after.st_size, after.st_blocks, after.st_mtime_ns),
                         (before.st_size, before.st_blocks, before.st_mtime_ns),
                         "keyboard navigation changed the disposable target")

    def test_install_through_screen_keeps_only_the_accepted_profile(self):
        files = self.enterContext(tempfile.TemporaryDirectory(prefix="being-screen-install-", dir="/tmp"))
        target = Path(files) / "target.raw"
        with target.open("wb") as disk:
            disk.truncate(TARGET_BYTES)
        self.start(target)
        menu = self.console("11-install-start")

        # All installer choices and the erase challenge go through the guest keyboard.
        self.select("4")
        setup = self.console(previous=menu)
        self.select("1")
        name = self.console(previous=setup)
        self.type_line("Fern")
        choices = self.console(previous=name)
        self.select("k")
        self.console(previous=choices)
        self.select("y")
        current = self.console("12-saved-profile", timeout=85)
        self.assertEqual(json.loads(self.sh("being-setup --show"))["name"], "Fern")
        self.sh("touch /mind/try-fixture-sentinel")
        inventory = json.loads(self.sh("being-installer --list --json"))
        eligible = [disk for disk in inventory["disks"] if disk["eligible"]]
        self.assertEqual(len(eligible), 1, eligible)
        approved = eligible[0]
        self.assertEqual((approved["model"], approved["size_bytes"]), ("BEINGOS SCREEN", TARGET_BYTES))
        self.assertRegex(approved["device"], r"^/dev/sd[a-z]+$")

        self.select("2")
        disks = self.console(previous=current)
        self.select("1")
        profile = self.console(previous=disks)
        self.select("y")
        self.console("13-confirm-install", profile)
        self.type_line(f"ERASE {approved['device']}")
        deadline = time.monotonic() + 180
        with target.open("rb", buffering=0) as disk:
            while time.monotonic() < deadline:
                disk.seek(512)
                if disk.read(8) == b"EFI PART":
                    break
                time.sleep(0.5)
            else:
                self.screenshot("14-install-timeout", self.vm.screen())
                self.fail("The screen did not complete a confirmed write on the disposable target")

        completion = self.console("14-installed")
        self.assertTrue(completion)
        self.key("ret")
        self.face("15-after-install")
        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(90))
        self.vm.stop()

        self.vm = VM(image=target)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(120)
        self.assertIn("being.installed=1", self.sh("cat /proc/cmdline").split())
        persisted = json.loads(self.sh("being-setup --show"))
        self.assertEqual(persisted["name"], "Fern")
        self.sh("test ! -e /mind/try-fixture-sentinel")

    def restore_through_screen(self, device, identity):
        self.select("2")
        self.wait_text("Encrypted backup file:", prompt=True)
        self.type_line(RESTORE_SNAPSHOT)
        self.wait_text("Private identity file:", prompt=True)
        self.type_line(identity)
        self.wait_text("Disk number [Enter to go back]:", prompt=True)
        self.select("1")
        self.wait_text(f"Type ERASE {device} to install:", prompt=True)
        self.type_line(f"ERASE {device}")

    def test_restore_identity_through_screen_authenticates_before_writing(self):
        files = self.enterContext(tempfile.TemporaryDirectory(prefix="being-screen-restore-", dir="/tmp"))
        target = Path(files) / "target.raw"
        with target.open("xb") as disk:
            disk.truncate(TARGET_BYTES)
        before = target.stat()
        self.assertEqual(before.st_blocks, 0)
        self.start(target)
        status, _ = self.vm.run(BACKUP_AVAILABLE, 10)
        if status:
            self.skipTest("the screen image does not package being-backup, age and age-keygen")

        self.vm.put("/run/restore-fixture.py", BACKUP_FIXTURE)
        status, output = self.vm.run("PYTHONPATH=/usr/lib/being python3 /run/restore-fixture.py", 110)
        self.assertEqual(status, 0, output)
        inventory = json.loads(self.sh("being-installer --list --json"))
        eligible = [disk for disk in inventory["disks"] if disk["eligible"]]
        self.assertEqual(len(eligible), 1, eligible)
        approved = eligible[0]
        self.assertEqual((approved["model"], approved["size_bytes"]), ("BEINGOS SCREEN", TARGET_BYTES))
        device = approved["device"]
        self.assertRegex(device, r"\A/dev/sd[a-z]+\Z")

        self.wait_text("Choice:", prompt=True)
        self.select("3")
        self.wait_text("Restore identity and settings")
        self.restore_through_screen(device, WRONG_IDENTITY)
        self.wait_text("Backup restore failed.", screenshot="21-restore-wrong-key", timeout=110)
        self.wait_text("Choice:", prompt=True)
        self.sh("sync")
        after = target.stat()
        self.assertEqual((after.st_size, after.st_blocks, after.st_mtime_ns),
                         (before.st_size, before.st_blocks, before.st_mtime_ns),
                         "authentication failure changed the disposable target")

        self.restore_through_screen(device, RESTORE_IDENTITY)
        self.wait_text("Press Enter to return to PipOS.", prompt=True,
                       screenshot="22-restore-complete", timeout=200)
        self.key("ret")
        self.face("23-after-restore")
        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(90))
        self.vm.stop()

        self.vm = VM(image=target)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(120)
        self.assertIn("being.installed=1", self.sh("cat /proc/cmdline").split())
        self.vm.wait_state("idle", 150)
        profile = json.loads(self.sh("being-setup --show"))
        self.assertEqual((profile["name"], profile["owner"], profile["voice"]), (RESTORE_NAME, "Casey", "awb"))
        life = json.loads(self.sh("cat /mind/life.json"))
        self.assertEqual((life["id"], life["born"], life["wakes"]), (RESTORE_LIFE, RESTORE_BORN, 7))
        self.assertEqual(json.loads(self.sh("cat /mind/skills.json")), {"time_zone": "Australia/Sydney"})
        self.assertEqual(json.loads(self.sh("cat /mind/memory-policy.json")),
                         {"version": 1, "mode": "persistent", "expiry_days": 18})
        self.assertEqual(self.vm.converse("What do you remember about my atlas?", 30),
                         "I don't remember anything about that.")


if __name__ == "__main__":
    unittest.main()
