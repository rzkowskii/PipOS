"""Smoke-test the laptop Try USB through its screen and keyboard alone.

The laptop media (out/images/beingos-laptop-try.img, from `lab/build.sh
laptop-try`) has no serial console and no root login, exactly like the first
Option-key boot on the MacBook. So nothing here logs in: every check reads the
virtual panel through QMP screendumps and types through QMP send-key, and the
USB image is read from the Mac side after a clean power-off.

    BEING_LAPTOP_TRY_IMAGE=out/images/beingos-laptop-try.img \
        python3 -m unittest lab.tests.test_laptop_smoke -v

BEING_LAPTOP_PATIENCE multiplies every wait (default 1; the CPU is emulated).
Screenshots land in out/lab/laptop-smoke-*.png.

The target design these tests check (other tracks implement it):
  * GRUB shows a menu on the panel with six entries, in this order: Try
    PipOS (the default), one that shows boot messages, one with safe
    graphics, two with alternative microphone set-ups (the default entry plus
    snd_hda_intel.model=mbp81 being.mic_channel=auto, hotkey m, and plus
    snd_hda_intel.model=mbp101, hotkey n) and one that measures speed (the
    default plus being.bench=1, hotkey b). It waits a few seconds, then boots
    the default. No entry reboots on an oops, restarts within seconds of a
    panic, or waits for ever for a missing root.
  * The boot-messages entry puts kernel text on the panel.
  * The safe-graphics entry still reaches the eyes, on the firmware's own
    framebuffer (simpledrm; this test gives it a plain VGA display for that).
  * A FAT partition labelled PIPLOGS on the USB stick gets one folder per
    boot holding summary.txt, a journal and the kernel command line, and
    boots.txt records the clean power-off. The internal disk stays untouched,
    shutdown included.
  * Installing through the screen writes a GPT on the internal disk, and that
    disk boots to the eyes with no login prompt on any serial line.

QEMU's firmware also copies its console (so GRUB's menu) to the serial line;
the serial checks ignore that and look only for a login or shell prompt.
"""

import os
import re
import struct
import subprocess
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from lab.tests.test_local_screen import console_lines, console_screen, visible_eyes
from lab.try_timings import parse_journal
from lab.vm import LAB_OUTPUT, ROOT, VM, machine_args, png

LAPTOP_TRY_IMAGE = Path(os.environ.get(
    "BEING_LAPTOP_TRY_IMAGE", ROOT / "out/images/beingos-laptop-try.img")).resolve()
PATIENCE = float(os.environ.get("BEING_LAPTOP_PATIENCE", "1") or 1)
SHOTS = LAB_OUTPUT
TARGET_BYTES = 8 << 30
TARGET_MODEL = "BEINGOS LAPTOP SSD"
LOG_LABEL = "PIPLOGS"
SECTOR = 512

# Waits, in seconds of wall time on an emulated CPU.
FIRMWARE_TO_GRUB = 300
GRUB_TO_MENU = 600
TO_EYES = 360
POWER_OFF = 240
INSTALL_WRITE = 900
KERNEL_TEXT_LINES = 15  # a screenful of messages, not one GRUB or firmware line

TRY_MENU_TEXT = ("Try PipOS", "Install PipOS", "Restore a being", "Setup", "Check hardware",
                 "by typing", "Return to PipOS")
BOOT_MESSAGES_TITLE = re.compile(r"boot messages|verbose", re.I)
SAFE_GRAPHICS_TITLE = re.compile(r"safe graphics|basic graphics|nomodeset", re.I)
# The extra entries: (title, hotkey, kernel arguments added to the default entry's).
EXTRA_ENTRIES = (
    (re.compile(r"alternative microphone\)", re.I), "m", ["snd_hda_intel.model=mbp81", "being.mic_channel=auto"]),
    (re.compile(r"alternative microphone 2\)", re.I), "n", ["snd_hda_intel.model=mbp101"]),
    (re.compile(r"measure speed", re.I), "b", ["being.bench=1"]),
)
LAPTOP_ENTRIES = 6
LOGIN_PROMPT = re.compile(r"login:|Password:")
SHELL_PROMPT = re.compile(r"^[^\n]{0,40}[#$] ?\r?$", re.M)
# The firmware (OVMF) mirrors its console, and so GRUB's menu, to the serial
# line with terminal escapes; the MacBook has no serial line at all.
TERMINAL_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
# lab/vm.py's display: virtio-gpu, whose firmware screen is Blt-only, so Linux
# gets no firmware framebuffer and only virtio-gpu's own driver can draw.
LAB_DISPLAY = "virtio-gpu-pci,xres=1280,yres=800"
# Plain VGA: the firmware screen is a linear framebuffer, as Apple's EFI gives
# the MacBook. Under nomodeset (bochs-drm, like i915, stands aside) simpledrm
# draws on it: the path the safe-graphics entry takes on the laptop.
FIRMWARE_FRAMEBUFFER_DISPLAY = "VGA,xres=1280,yres=800"


# The organs a healthy Try boot keeps running without a single restart.
ORGANS = ("being-brainstem.service", "being-face.service", "being-mind.service", "being-voice.service",
          "being-cortex.service")


def organ_trouble(journal):
    """Restarts or failures of an organ in a PIPLOGS journal.txt (empty when healthy)."""
    parsed = parse_journal(journal)
    trouble = [f"{unit} restarted {count} times" for unit, count in sorted(parsed["restarts"].items())
               if unit in ORGANS]
    trouble += [f"{failure['unit']} failed: {failure['line']}" for failure in parsed["failed"]
                if failure["unit"] in ORGANS]
    return trouble


def patient(seconds):
    return seconds * PATIENCE


# -- the USB image, read from the Mac side ------------------------------------

def gpt_partitions(disk):
    """[(index, name, first byte, size in bytes)] from a disk image's primary GPT."""
    disk.seek(SECTOR)
    header = disk.read(92)
    if header[:8] != b"EFI PART":
        return []
    entries_lba, count, entry_size = struct.unpack_from("<QII", header, 72)
    disk.seek(entries_lba * SECTOR)
    table = disk.read(count * entry_size)
    found = []
    for index in range(count):
        entry = table[index * entry_size:(index + 1) * entry_size]
        if entry[:16] == bytes(16):
            continue
        first, last = struct.unpack_from("<QQ", entry, 32)
        name = entry[56:128].decode("utf-16-le", "replace").split("\0", 1)[0]
        found.append((index + 1, name, first * SECTOR, (last - first + 1) * SECTOR))
    return found


class FatEntry:
    def __init__(self, name, attributes, cluster, size):
        self.name, self.attributes, self.cluster, self.size = name, attributes, cluster, size

    @property
    def is_dir(self):
        return bool(self.attributes & 0x10)

    def __repr__(self):
        return f"FatEntry({self.name!r}, dir={self.is_dir}, size={self.size})"


class Fat:
    """A minimal read-only FAT12/16/32 reader with long names, enough to list
    folders and read files. mtools is not on a stock Mac."""

    def __init__(self, disk, offset):
        self.disk, self.offset = disk, offset
        disk.seek(offset)
        boot = disk.read(512)
        if boot[510:512] != b"\x55\xaa":
            raise ValueError("no FAT boot sector")
        self.sector = struct.unpack_from("<H", boot, 11)[0]
        self.per_cluster = boot[13]
        reserved, fats, root_entries, total16 = struct.unpack_from("<HBHH", boot, 14)
        fat16_size = struct.unpack_from("<H", boot, 22)[0]
        total32, fat32_size, _, _, self.root_cluster = struct.unpack_from("<IIHHI", boot, 32)
        # A squashfs or ext4 partition can end its first sector in 55 AA by
        # chance: refuse anything whose geometry is not a FAT's before reading
        # a FAT table of arbitrary size.
        if (self.sector not in (512, 1024, 2048, 4096) or self.per_cluster not in (1, 2, 4, 8, 16, 32, 64, 128)
                or not reserved or fats not in (1, 2)):
            raise ValueError("not a FAT file system")
        fat_size = fat16_size or fat32_size
        total = total16 or total32
        if not fat_size or not total or fat_size * self.sector > 64 << 20:
            raise ValueError("not a FAT file system")
        root_sectors = (root_entries * 32 + self.sector - 1) // self.sector
        self.fat_start = reserved
        self.root_start = reserved + fats * fat_size
        self.root_sectors = root_sectors
        self.data_start = self.root_start + root_sectors
        if self.data_start >= total:
            raise ValueError("not a FAT file system")
        clusters = (total - self.data_start) // self.per_cluster
        # FAT32 has no 16-bit FAT size (mkfs.fat -F 32 can make one with few clusters).
        self.bits = 32 if not fat16_size else 12 if clusters < 4085 else 16
        label = boot[71:82] if self.bits == 32 else boot[43:54]
        self.label = label.decode("ascii", "replace").strip()
        self.clusters = clusters
        disk.seek(offset + reserved * self.sector)
        self.fat = disk.read(fat_size * self.sector)
        for entry in self._parse(self._root_bytes(), labels=True):
            if entry.attributes & 0x08 and not entry.is_dir:
                self.label = entry.name.strip()

    def _read_sectors(self, first, count):
        self.disk.seek(self.offset + first * self.sector)
        return self.disk.read(count * self.sector)

    def _next(self, cluster):
        if self.bits == 12:
            value = struct.unpack_from("<H", self.fat, cluster + cluster // 2)[0]
            value = value >> 4 if cluster & 1 else value & 0xFFF
            return None if value >= 0xFF8 else value
        if self.bits == 16:
            value = struct.unpack_from("<H", self.fat, cluster * 2)[0]
            return None if value >= 0xFFF8 else value
        value = struct.unpack_from("<I", self.fat, cluster * 4)[0] & 0x0FFFFFFF
        return None if value >= 0x0FFFFFF8 else value

    def _chain(self, cluster, size=None):
        data = bytearray()
        seen = set()
        while cluster is not None and 2 <= cluster < self.clusters + 2 and cluster not in seen:
            seen.add(cluster)
            data += self._read_sectors(self.data_start + (cluster - 2) * self.per_cluster, self.per_cluster)
            if size is not None and len(data) >= size:
                break
            cluster = self._next(cluster)
        return bytes(data if size is None else data[:size])

    def _root_bytes(self):
        if self.bits == 32:
            return self._chain(self.root_cluster)
        return self._read_sectors(self.root_start, self.root_sectors)

    @staticmethod
    def _parse(data, labels=False):
        entries, long_parts = [], {}
        for at in range(0, len(data) - 31, 32):
            raw = data[at:at + 32]
            if raw[0] == 0:
                break
            if raw[0] == 0xE5:
                long_parts = {}
                continue
            attributes = raw[11]
            if attributes == 0x0F:
                sequence = raw[0] & 0x1F
                chars = raw[1:11] + raw[14:26] + raw[28:32]
                long_parts[sequence] = chars.decode("utf-16-le", "replace")
                continue
            base, extension = raw[0:8].decode("latin-1").rstrip(), raw[8:11].decode("latin-1").rstrip()
            if raw[0] == 0x05:
                base = "\xe5" + base[1:]
            if raw[12] & 0x08:
                base = base.lower()
            if raw[12] & 0x10:
                extension = extension.lower()
            short = base + ("." + extension if extension else "")
            if attributes & 0x08:
                name = (raw[0:11].decode("latin-1"))
            elif long_parts:
                name = "".join(long_parts[key] for key in sorted(long_parts)).split("\0", 1)[0].rstrip("￿")
            else:
                name = short
            long_parts = {}
            if attributes & 0x08 and not labels:
                continue
            if name in (".", ".."):
                continue
            high, low = struct.unpack_from("<H", raw, 20)[0], struct.unpack_from("<H", raw, 26)[0]
            entries.append(FatEntry(name, attributes, (high << 16) | low, struct.unpack_from("<I", raw, 28)[0]))
        return entries

    def _entry(self, path):
        parts = [part for part in str(path).split("/") if part]
        entry = None
        for part in parts:
            listing = self.listdir_entries(entry)
            entry = next((item for item in listing if item.name.lower() == part.lower()), None)
            if entry is None:
                raise FileNotFoundError(path)
        return entry

    def listdir_entries(self, directory=None):
        if directory is None or (directory.is_dir and directory.cluster == 0):
            return self._parse(self._root_bytes())
        if not directory.is_dir:
            raise NotADirectoryError(directory.name)
        return self._parse(self._chain(directory.cluster))

    def listdir(self, path=""):
        return self.listdir_entries(self._entry(path) if path.strip("/") else None)

    def read(self, path):
        entry = self._entry(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        return self._chain(entry.cluster, entry.size) if entry.size else b""

    def walk(self, path="", depth=4):
        """[(path, entry)] for everything under `path`, `depth` folders deep."""
        found = []
        for entry in self.listdir(path):
            child = f"{path.rstrip('/')}/{entry.name}" if path.strip("/") else entry.name
            found.append((child, entry))
            if entry.is_dir and depth > 1:
                found.extend(self.walk(child, depth - 1))
        return found


def fat_volumes(disk):
    """{partition name or FAT label: Fat} for the FAT partitions on a disk image."""
    volumes = {}
    for _, name, offset, _ in gpt_partitions(disk):
        try:
            volume = Fat(disk, offset)
        except (ValueError, struct.error, IndexError):
            continue
        volumes[name or volume.label] = volume
        if volume.label:
            volumes.setdefault(volume.label, volume)
    return volumes


def log_volume(disk):
    volumes = fat_volumes(disk)
    for key, volume in volumes.items():
        if key.upper() == LOG_LABEL or volume.label.upper() == LOG_LABEL:
            return volume
    return None


def boot_folders(volume):
    """{folder path: [(path, entry)]} for each folder that holds a summary.txt."""
    if volume is None:
        return {}
    walked = volume.walk(depth=4)
    folders = {}
    for path, entry in walked:
        if not entry.is_dir and entry.name.lower() == "summary.txt":
            folder = path.rpartition("/")[0]
            folders[folder] = [(child, item) for child, item in walked
                               if child.startswith(folder + "/") or not folder]
    return folders


def grub_config(disk):
    """The text of the boot menu on the image's EFI partition, or None."""
    for volume in fat_volumes(disk).values():
        for path in ("EFI/BOOT/grub.cfg", "EFI/BOOT/GRUB.CFG", "boot/grub/grub.cfg", "grub/grub.cfg"):
            try:
                return volume.read(path).decode("utf-8", "replace")
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                continue
    return None


def menu_entries(config):
    """[(title, body)] for the top-level menu entries of a GRUB config."""
    return [(match[2], match[3]) for match in re.finditer(
        r"^\s*menuentry\s+([\"'])(.+?)\1[^{\n]*\{(.*?)^\s*\}", config, re.M | re.S)]


def grub_setting(config, name):
    """The value a GRUB variable is left with on the laptop, or None.

    The stick's grub.cfg is the lab's with the serial lines taken out, so it
    still opens with the lab's `set timeout=0`; the laptop's own settings come
    later, inside `if [ "$being_lab" != 1 ]`. GRUB runs the file top to
    bottom and the laptop takes that branch, so the last assignment wins."""
    values = re.findall(rf"^\s*set\s+{re.escape(name)}=(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s;]*))", config, re.M)
    return "".join(values[-1]) if values else None


def kernel_args(body):
    """The arguments of the `linux` line in a menu entry's body, or None."""
    match = re.search(r"^\s*(?:if\s+)?linux\s+\S+\s+(.*?)(?:;\s*then)?\s*$", body, re.M)
    return match[1].split() if match else None


def default_entry(config, titles):
    value = grub_setting(config, "default")
    if not value:
        return 0
    return int(value) if value.isdigit() else titles.index(value)


# -- the panel, read off the screen --------------------------------------------

def _background(frame):
    width, height, rgb = frame
    samples = Counter(rgb[i:i + 3] for i in range(0, width * height * 3, 3 * 37))
    return samples.most_common(1)[0][0]


def _differs(pixel, background):
    return abs(pixel[0] - background[0]) + abs(pixel[1] - background[1]) + abs(pixel[2] - background[2]) > 90


def _runs(flags, shortest, longest):
    runs, start = [], None
    for y, flag in enumerate(flags + [False]):
        if flag and start is None:
            start = y
        elif not flag and start is not None:
            if shortest <= y - start <= longest:
                runs.append((start, y))
            start = None
    return runs


def menu_highlight(frame):
    """The vertical centre of a boot-menu highlight bar: one wide band of
    filled character cells. None for any other screen."""
    width, height, rgb = frame
    if width < 64 or height < 64 or console_screen(frame) or visible_eyes(frame):
        return None
    background = _background(frame)
    left, right = width // 10, width * 9 // 10
    samples = range(left, right, 4)
    dense = []
    for y in range(height):
        row = y * width * 3
        filled = sum(1 for x in samples if _differs(rgb[row + x * 3:row + x * 3 + 3], background))
        dense.append(filled > len(samples) // 2)
    bars = _runs(dense, 8, 64)
    if len(bars) != 1:
        return None
    top, bottom = bars[0]
    return (top + bottom) // 2


def text_lines(frame):
    """How many lines of text are on the left half of a plain text screen
    (kernel or systemd messages): not the console, not the eyes, not a menu."""
    width, height, rgb = frame
    if width < 64 or height < 64 or console_screen(frame) or visible_eyes(frame) or menu_highlight(frame):
        return 0
    background = _background(frame)
    inked = []
    for y in range(height):
        row = y * width * 3
        inked.append(any(sum(rgb[row + x * 3:row + x * 3 + 3]) > 200
                         and _differs(rgb[row + x * 3:row + x * 3 + 3], background)
                         for x in range(0, width // 2, 2)))
    return len(_runs(inked, 4, 40))


def try_menu_lines(frame):
    """being-console's lines once the Try menu is fully drawn, else None."""
    lines = console_lines(frame)
    if lines and "Choice:" in lines[-1] and all(any(item in line for line in lines)
                                                    for item in ("Try PipOS", "Install PipOS")):
        return lines
    return None


LAB_TRY_IMAGE = ROOT / "out/images/beingos-try-screen.img"


@unittest.skipUnless(LAB_TRY_IMAGE.exists(), "needs any lab Try image to check the FAT reader against")
class FatReader(unittest.TestCase):
    """The pure-Python reader against a real genimage-built EFI partition."""

    def test_reads_the_lab_try_boot_menu(self):
        with LAB_TRY_IMAGE.open("rb") as disk:
            names = [name for _, name, _, _ in gpt_partitions(disk)]
            self.assertIn("try-esp", names)
            volume = fat_volumes(disk)["try-esp"]
            paths = {path.lower() for path, _ in volume.walk()}
            self.assertTrue({"efi/boot/bootx64.efi", "efi/boot/grub.cfg"} <= paths, paths)
            self.assertGreater(len(volume.read("EFI/BOOT/bootx64.efi")), 100_000)
            config = grub_config(disk)
        self.assertIn("Try PipOS", [title for title, _ in menu_entries(config)])
        self.assertEqual(boot_folders(None), {})


@unittest.skipUnless(LAPTOP_TRY_IMAGE.exists(), "needs the laptop Try USB: run lab/build.sh laptop-try")
class LaptopTryMedia(unittest.TestCase):
    """What is on the stick, before anything boots."""

    def test_boot_menu_is_on_the_panel_with_six_entries_and_a_timeout(self):
        with LAPTOP_TRY_IMAGE.open("rb") as disk:
            config = grub_config(disk)
        self.assertIsNotNone(config, "no grub.cfg on the stick's EFI partition")
        self.assertNotRegex(config, r"(?m)^\s*terminal_output\s+serial\s*$",
                            "GRUB writes only to a serial line the MacBook does not have")
        self.assertNotIn("console=ttyS0", config, "the kernel console points at a missing serial line")
        outputs = re.findall(r"^\s*terminal_output\s+(.+?)\s*$", config, re.M)
        self.assertTrue(not outputs or re.search(r"\b(console|gfxterm)\b", outputs[-1]),
                        f"GRUB's last terminal_output is {outputs[-1] if outputs else None!r}, not the panel")
        timeout = grub_setting(config, "timeout")
        self.assertTrue(timeout and timeout.isdigit() and int(timeout) > 0,
                        f"the menu must wait long enough to be seen (timeout={timeout!r})")
        self.assertIn(grub_setting(config, "timeout_style"), (None, "", "menu"),
                      "the menu is hidden behind a countdown")
        entries = menu_entries(config)
        titles = [title for title, _ in entries]
        self.assertEqual(len(entries), LAPTOP_ENTRIES, titles)
        self.assertRegex(titles[default_entry(config, titles)], r"(?i)^Try PipOS")
        verbose = [body for title, body in entries if BOOT_MESSAGES_TITLE.search(title)]
        safe = [body for title, body in entries if SAFE_GRAPHICS_TITLE.search(title)]
        self.assertEqual((len(verbose), len(safe)), (1, 1), titles)
        self.assertNotRegex(verbose[0], r"\bquiet\b|\bloglevel=[0-3]\b",
                            "the boot-messages entry still hides the kernel's messages")
        default_body = entries[default_entry(config, titles)][1]
        self.assertNotEqual(safe[0], default_body, "the safe-graphics entry boots exactly like the default")
        for pattern, hotkey, extra in EXTRA_ENTRIES:
            found = [body for title, body in entries if pattern.search(title)]
            self.assertEqual(len(found), 1, (pattern.pattern, titles))
            self.assertEqual(kernel_args(found[0]), kernel_args(default_body) + extra,
                             f"the {pattern.pattern} entry is not the default plus {extra}")
            self.assertRegex(config, rf'(?m)^\s*menuentry\s+"[^"]*{pattern.pattern}[^"]*"\s+--hotkey={hotkey}\s*\{{',
                             f"the {pattern.pattern} entry has no hotkey {hotkey}")
        # A failure must stay on the panel: no reboot on an oops, a panic up
        # long enough to photograph, and no endless wait for a missing stick.
        for title, body in entries:
            self.assertNotIn("oops=panic", body, f"{title!r} reboots on any oops")
            panic = re.search(r"\bpanic=(\d+)", body)
            self.assertTrue(not panic or int(panic[1]) == 0 or int(panic[1]) >= 30,
                            f"{title!r} restarts {panic and panic[1]} s after a panic: too fast to read")
            self.assertNotRegex(body, r"\brootwait(?![=\w])",
                                f"{title!r} waits for ever, on a blank screen, if the root never appears")

    def test_stick_has_a_fat_log_partition(self):
        with LAPTOP_TRY_IMAGE.open("rb") as disk:
            volume = log_volume(disk)
            self.assertIsNotNone(volume, f"no FAT partition labelled {LOG_LABEL}: "
                                         f"{[(name, size) for _, name, _, size in gpt_partitions(disk)]}")
            volume.listdir()


@unittest.skipUnless(LAPTOP_TRY_IMAGE.exists(), "needs the laptop Try USB: run lab/build.sh laptop-try")
class LaptopSmoke(unittest.TestCase):
    """Boot the stick over virtual USB with a blank internal disk, eyes and keys only."""

    # -- machine -----------------------------------------------------------

    def blank_target(self):
        files = self.enterContext(tempfile.TemporaryDirectory(prefix="being-laptop-smoke-", dir="/tmp"))
        target = Path(files) / "target.raw"
        with target.open("xb") as disk:
            disk.truncate(TARGET_BYTES)
        return target

    def start_usb(self, target, display=LAB_DISPLAY):
        def usb_boot_with_sata_target(*args, **kwargs):
            command = machine_args(*args, **kwargs)
            boot_device = command.index("ide-hd,drive=disk0,bus=ide.0")
            del command[boot_device - 1:boot_device + 1]
            controller = command.index("qemu-xhci")
            command[controller] = "qemu-xhci,id=usb-controller"
            command[command.index(LAB_DISPLAY)] = display
            return command + [
                "-device", "usb-storage,drive=disk0,bus=usb-controller.0,bootindex=1",
                "-drive", f"if=none,id=laptop-target,format=raw,file={target}",
                "-device", f"ide-hd,drive=laptop-target,bus=ide.0,bootindex=2,model={TARGET_MODEL}",
            ]

        self.vm = VM(image=LAPTOP_TRY_IMAGE)
        self.addCleanup(self.vm.destroy)
        self.last_lit = None
        with patch("lab.vm.machine_args", usb_boot_with_sata_target):
            self.vm.start()

    def guest_resets(self):
        """How often the guest has restarted itself on this machine: QMP RESET
        events (a reboot, or a restart after a panic or by a watchdog; a
        power-off is SHUTDOWN). lab/vm.py keeps the events its commands meet."""
        return sum(1 for event in self.vm._qmp_events if event.get("event") == "RESET")

    def key(self, name):
        self.vm.qmp("send-key", keys=[{"type": "qcode", "data": name}])
        time.sleep(0.15)

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

    def power_off(self):
        """The power button, as the owner would press it; then wait for it to go."""
        self.vm.power_button()
        if not self.vm.wait_off(patient(POWER_OFF)):
            frame = self.vm.screen()
            self.fail_on(frame, "power-off-timeout", "the power button did not power the machine off")
        self.vm.stop()

    # -- evidence ------------------------------------------------------------

    def screenshot(self, name, frame):
        SHOTS.mkdir(parents=True, exist_ok=True)
        path = SHOTS / f"laptop-smoke-{name}.png"
        path.write_bytes(png(*frame))
        return path

    def fail_on(self, frame, name, message):
        details = [message]
        if frame is not None:
            details.append(f"screen: {self.screenshot(name, frame)}")
            lines = console_lines(frame)
            if lines:
                details.append("console lines:\n" + "\n".join(lines[-12:]))
        last = getattr(self, "last_lit", None)
        if last is not None and last is not frame:
            # A panic or an error drawn just before a restart or a dark panel.
            details.append(f"last screen with anything on it: {self.screenshot(f'{name}-last-lit', last)}")
        if self.vm.proc is not None:
            details.append(f"guest restarts so far: {self.guest_resets()}")
        serial = self.vm.output()
        details.append(f"serial line ({len(serial)} bytes): {serial[-800:]!r}")
        log = (self.vm.dir / "qemu.log") if self.vm.dir else None
        if log and log.exists():
            details.append("qemu.log: " + log.read_text(errors="replace")[-800:])
        self.fail("\n".join(details))

    def wait_screen(self, name, test, timeout, *, step=0.5, shot=True):
        """Poll the panel until `test(frame)` is truthy. Returns (result, frame)."""
        deadline = time.monotonic() + patient(timeout)
        frame = None
        while time.monotonic() < deadline:
            if not self.vm.running():
                self.fail_on(None, name, f"the machine stopped (rebooted or crashed) before {name}")
            frame = self.vm.screen()
            if frame[2].strip(b"\0"):
                self.last_lit = frame
            if self.guest_resets():
                # Nothing on these boots restarts on purpose: on the MacBook
                # this would be a reboot loop, with the reason gone from the panel.
                self.fail_on(frame, f"{name}-restarted", f"the machine restarted itself before {name}")
            result = test(frame)
            if result:
                if shot:
                    self.screenshot(name, frame)
                return result, frame
            time.sleep(step)
        self.fail_on(frame, f"{name}-timeout", f"the panel never showed {name} within {patient(timeout):.0f}s")

    def wait_grub(self, name="grub-menu"):
        highlight, _ = self.wait_screen(name, menu_highlight, FIRMWARE_TO_GRUB, step=0.3)
        return highlight

    def wait_try_menu(self, name):
        lines, _ = self.wait_screen(name, try_menu_lines, GRUB_TO_MENU)
        return lines

    def wait_eyes(self, name, timeout=TO_EYES):
        self.wait_screen(name, visible_eyes, timeout)

    def reach_eyes(self, name, *, menu_first=False):
        """Eyes, choosing Try from the console menu if it is shown first.
        With `menu_first`, the Try menu must come before the eyes."""
        def progress(frame):
            if visible_eyes(frame):
                return "eyes"
            if try_menu_lines(frame):
                return "menu"
            return None

        state, frame = self.wait_screen(f"{name}-first", progress, GRUB_TO_MENU)
        if menu_first and state != "menu":
            self.fail_on(frame, f"{name}-no-menu", "the eyes came up without the Try menu: being-console "
                                                   "failed on this boot and the face took its place")
        if state == "menu":
            self.select("1")
            self.wait_eyes(name)

    def highlight_after(self, previous, settle=6):
        """Where the highlight sits after a key: the first new position, or
        `previous` if it does not move within `settle` seconds."""
        deadline = time.monotonic() + patient(settle)
        current = previous
        while time.monotonic() < deadline:
            current = menu_highlight(self.vm.screen())
            if current and abs(current - previous) > 4:
                return current
            time.sleep(0.2)
        if not current:
            self.fail_on(self.vm.screen(), "grub-highlight-lost", "the GRUB menu left the panel while being navigated")
        return previous

    def grub_choose(self, index, name):
        """Stop the countdown, move to entry `index` and boot it."""
        self.wait_grub(f"{name}-grub")
        self.key("home")
        for _ in range(index):
            self.key("down")
        time.sleep(0.5)
        self.screenshot(f"{name}-chosen", self.vm.screen())
        self.key("ret")

    def assert_no_serial_login(self, where):
        # A getty can start well after the eyes: poke the line twice and give
        # it time. A getty or shell answers each poke with a fresh prompt, so
        # a shell prompt is looked for only in what came back after the pokes
        # (before them the line carries the firmware's and GRUB's screens).
        mark = self.vm.shell.mark()
        for _ in range(2):
            self.vm.send("\r\n")
            time.sleep(patient(20))
        serial = TERMINAL_ESCAPE.sub("", self.vm.output())
        answer = TERMINAL_ESCAPE.sub("", self.vm.shell.since(mark))
        self.assertNotRegex(serial, LOGIN_PROMPT, f"{where}: a login prompt appeared on the serial line")
        self.assertNotRegex(answer, SHELL_PROMPT, f"{where}: a shell prompt answered on the serial line")

    def assert_untouched(self, target, before):
        after = target.stat()
        self.assertEqual((after.st_size, after.st_blocks, after.st_mtime_ns),
                         (before.st_size, before.st_blocks, before.st_mtime_ns),
                         "Try changed the internal disk")

    def config(self):
        with LAPTOP_TRY_IMAGE.open("rb") as disk:
            config = grub_config(disk)
        if config is None:
            self.fail("no grub.cfg on the stick's EFI partition")
        entries = menu_entries(config)
        titles = [title for title, _ in entries]
        return config, titles

    def entry_index(self, pattern):
        _, titles = self.config()
        matches = [index for index, title in enumerate(titles) if pattern.search(title)]
        self.assertEqual(len(matches), 1, titles)
        return matches[0]

    def shows_try_menu(self, index):
        """Whether GRUB entry `index` boots with the Try menu (being.screen=1).
        The face then waits for the menu (being-screen-mode), so eyes before
        any menu mean being-console failed and the face came up in its place."""
        config, _ = self.config()
        return "being.screen=1" in menu_entries(config)[index][1].split()

    def usb_raw(self):
        """The stick as the machine left it, as a raw file (the VM ran on an overlay)."""
        self.assertFalse(self.vm.running())
        raw = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="being-laptop-usb-", dir="/tmp"))) / "usb.raw"
        subprocess.run(["qemu-img", "convert", "-O", "raw", str(self.vm.disk), str(raw)], check=True)
        return raw

    # -- the tests -----------------------------------------------------------

    def test_default_boot_shows_try_menu_then_eyes_and_keeps_a_boot_log(self):
        """(1) GRUB on the panel times out into the default; (2) the Try menu
        reads correctly; (3) 1 Try shows the eyes; (6) a clean power-off leaves
        this boot's folder on PIPLOGS. The internal disk stays untouched."""
        with LAPTOP_TRY_IMAGE.open("rb") as disk:
            logged_before = set(boot_folders(log_volume(disk)))
        target = self.blank_target()
        before = target.stat()
        self.start_usb(target)

        self.wait_grub("01-grub-menu")
        shown = time.monotonic()
        # No key: the menu must give way to the default by itself.
        self.wait_screen("02-grub-timed-out", lambda frame: not menu_highlight(frame), GRUB_TO_MENU, shot=False)
        self.assertGreater(time.monotonic() - shown, 0.5, "the GRUB menu was not on the panel long enough to read")

        lines = self.wait_try_menu("03-try-menu")
        text = "\n".join(lines)
        for phrase in TRY_MENU_TEXT:
            self.assertIn(phrase, text, f"the Try menu does not read {phrase!r}:\n{text}")
        self.assertIn("Hello", text, f"the Try menu has no greeting:\n{text}")

        self.select("1")
        self.wait_eyes("04-try-eyes")
        self.assert_no_serial_login("Try")
        self.assert_untouched(target, before)

        self.power_off()
        # Shutdown writes too (the last journal and summary): still only the stick.
        self.assert_untouched(target, before)
        with self.usb_raw().open("rb") as disk:
            self.assert_one_clean_default_boot_logged(disk, logged_before)

    def assert_one_clean_default_boot_logged(self, disk, logged_before):
        """PIPLOGS on `disk` holds one new boot folder with a summary, a
        journal and the default entry's command line, and boots.txt records
        that boot's clean end."""
        volume = log_volume(disk)
        self.assertIsNotNone(volume, f"no {LOG_LABEL} partition on the stick")
        folders = boot_folders(volume)
        new = {folder: files for folder, files in folders.items() if folder not in logged_before}
        self.assertEqual(len(new), 1, f"expected one new boot folder on {LOG_LABEL}, found "
                                      f"{sorted(new)} (all: {sorted(folders)})")
        folder, files = next(iter(new.items()))
        listing = [(path, entry.size) for path, entry in files]
        summary = volume.read(f"{folder}/summary.txt")
        self.assertTrue(summary.strip(), f"{folder}/summary.txt is empty")
        summary.decode("utf-8")
        journals = [(path, entry) for path, entry in files
                    if "journal" in path.lower() and (entry.is_dir or entry.size)]
        journal_bytes = sum(entry.size for path, entry in files
                            if not entry.is_dir and "journal" in path.lower())
        self.assertTrue(journals and journal_bytes > 0, f"{folder} holds no journal: {listing}")

        # The boot the menu timed out into was the default (quiet) entry.
        kernel = b""
        for name in ("hardware-report.txt", "dmesg.txt"):
            try:
                kernel += volume.read(f"{folder}/{name}") + b"\n"
            except (FileNotFoundError, NotADirectoryError):
                pass
        cmdline = re.search(rb"(?:cmdline:|Command line:)([^\n]*being\.try=1[^\n]*)", kernel)
        self.assertIsNotNone(cmdline, f"{folder} records no kernel command line "
                                      f"(hardware-report.txt, dmesg.txt): {listing}")
        for flag in (b"being.verbose=1", b"nomodeset"):
            self.assertNotIn(flag, cmdline[1], "GRUB timed out into a non-default entry: "
                                               + cmdline[1].decode("utf-8", "replace"))

        # boots.txt (see the README on the stick) marks a clean end, so a
        # crash, a power cut or a reboot loop can be told apart on another computer.
        try:
            boots = volume.read("boots.txt").decode("utf-8", "replace")
        except FileNotFoundError:
            self.fail(f"no boots.txt on {LOG_LABEL}: {sorted(path for path, _ in volume.walk(depth=1))}")
        self.assertRegex(boots, rf"(?m)^{re.escape(folder.rpartition('/')[2])} ended cleanly",
                         f"the clean power-off is not recorded in boots.txt:\n{boots[-1500:]}")

        # Every organ stayed up for the whole boot: no restart, no failure.
        journal = b"".join(volume.read(path) for path, entry in files
                           if not entry.is_dir and path.lower().endswith("journal.txt"))
        trouble = organ_trouble(journal.decode("utf-8", "replace"))
        self.assertEqual(trouble, [], "organs restarted or failed during the Try boot")

    def test_hard_power_off_is_recorded_as_an_unclean_end(self):
        """(8) Pulling the plug once the eyes are up leaves a boot folder whose
        journal was already synced, and boots.txt shows a start with no clean
        end: the owner can tell a crash or a held power button from a shutdown."""
        with LAPTOP_TRY_IMAGE.open("rb") as disk:
            logged_before = set(boot_folders(log_volume(disk)))
        target = self.blank_target()
        before = target.stat()
        self.start_usb(target)
        self.wait_grub("31-grub-menu")
        self.wait_try_menu("32-try-menu")
        self.select("1")
        self.wait_eyes("33-try-eyes")
        time.sleep(patient(20))        # the journal copy is synced every few seconds
        self.vm.power_cut()
        self.vm.stop()
        self.assert_untouched(target, before)
        with self.usb_raw().open("rb") as disk:
            volume = log_volume(disk)
            folders = boot_folders(volume)
            new = sorted(folder for folder in folders if folder not in logged_before)
            self.assertEqual(len(new), 1, f"expected one new boot folder, found {new}")
            folder = new[0]
            name = folder.rpartition("/")[2]
            journal = sum(entry.size for path, entry in folders[folder]
                          if not entry.is_dir and "journal" in path.lower())
            self.assertGreater(journal, 0, f"{folder} kept no journal before the power cut")
            boots = volume.read("boots.txt").decode("utf-8", "replace")
        self.assertRegex(boots, rf"(?m)^{re.escape(name)} started", f"boots.txt has no start line:\n{boots[-1500:]}")
        self.assertNotRegex(boots, rf"(?m)^{re.escape(name)} ended cleanly",
                            "a power cut was recorded as a clean end")

    def test_grub_has_six_entries_and_boot_messages_show_kernel_text(self):
        """(1) six entries in the GRUB menu; (4) the boot-messages entry puts
        kernel text on the panel, and the boot still reaches the being."""
        target = self.blank_target()
        before = target.stat()
        self.start_usb(target)
        first = self.wait_grub("11-grub-menu")
        positions = [first]
        for _ in range(LAPTOP_ENTRIES):
            self.key("down")  # the first also stops the countdown
            positions.append(self.highlight_after(positions[-1]))
        self.screenshot("11-grub-last", self.vm.screen())
        # Down from the last entry either stays there or wraps to the first.
        rows = []
        for position in sorted(positions):
            if not rows or position - rows[-1] > 4:
                rows.append(position)
        self.assertEqual(len(rows), LAPTOP_ENTRIES,
                         f"highlight rows {positions}: the menu does not hold exactly {LAPTOP_ENTRIES} entries")
        pitch = [b - a for a, b in zip(rows, rows[1:])]
        self.assertLessEqual(max(pitch) - min(pitch), 6, f"uneven entry rows {positions}")

        verbose = self.entry_index(BOOT_MESSAGES_TITLE)
        self.key("home")
        for _ in range(verbose):
            self.key("down")
        self.key("ret")
        self.wait_screen("12-kernel-text", lambda frame: text_lines(frame) >= KERNEL_TEXT_LINES,
                         FIRMWARE_TO_GRUB, step=0.3)
        # The messages stay up for being.verbose_hold (20 s by default), then the menu or the face.
        self.reach_eyes("13-verbose-eyes", menu_first=self.shows_try_menu(verbose))
        self.assert_no_serial_login("boot messages")
        self.assert_untouched(target, before)

    def test_safe_graphics_still_reaches_the_eyes(self):
        """(5) The safe-graphics entry reaches the Try menu or the eyes, then the eyes.

        On plain VGA, not the lab's virtio-gpu: virtio-gpu's firmware screen is
        Blt-only, so under nomodeset Linux would have nothing at all to draw on
        (a black panel that says nothing about the MacBook). VGA's firmware
        screen is a linear framebuffer like Apple's, so simpledrm draws the
        face exactly as the laptop's safe-graphics boot does."""
        target = self.blank_target()
        before = target.stat()
        self.start_usb(target, display=FIRMWARE_FRAMEBUFFER_DISPLAY)
        safe = self.entry_index(SAFE_GRAPHICS_TITLE)
        self.grub_choose(safe, "21-safe")
        self.reach_eyes("22-safe-eyes", menu_first=self.shows_try_menu(safe))
        self.assert_no_serial_login("safe graphics")
        self.assert_untouched(target, before)

    def test_install_through_screen_then_installed_disk_boots_to_eyes_without_serial_login(self):
        """(7) Install from the Try menu to the blank internal disk with the
        keyboard; it gets a GPT, then boots alone to the eyes with no login on serial."""
        target = self.blank_target()
        self.start_usb(target)
        self.wait_try_menu("31-try-menu")

        self.select("2")

        def disks(frame):
            lines = console_lines(frame)
            return lines if lines and "Disk number" in lines[-1] else None

        disk_lines, _ = self.wait_screen("32-choose-disk", disks, 240)
        self.assertTrue(any(TARGET_MODEL.split()[0] in line.upper() for line in disk_lines),
                        "the blank internal disk is not offered:\n" + "\n".join(disk_lines))
        self.select("1")

        answered = [0.0]

        def challenge(frame):
            lines = console_lines(frame)
            match = lines and re.search(r"Type ERASE (/dev/[a-z0-9]+) to install:", lines[-1])
            if lines and not match and "[y/N]" in lines[-1] and time.monotonic() - answered[0] > patient(5):
                answered[0] = time.monotonic()
                self.select("n")  # a Try profile or settings question: keep nothing
            return match[1] if match else None

        device, _ = self.wait_screen("33-confirm-install", challenge, 240)
        self.type_line(f"ERASE {device}")
        deadline = time.monotonic() + patient(INSTALL_WRITE)
        with target.open("rb", buffering=0) as disk:
            while time.monotonic() < deadline:
                disk.seek(SECTOR)
                if disk.read(8) == b"EFI PART":
                    break
                time.sleep(0.5)
            else:
                self.fail_on(self.vm.screen(), "34-install-timeout", "the screen install wrote no GPT on the target")

        def finished(frame):
            lines = console_lines(frame)
            return bool(lines) and "Press Enter to return to PipOS." in lines[-1]

        self.wait_screen("34-installed", finished, INSTALL_WRITE)
        self.key("ret")
        self.wait_eyes("35-after-install")
        self.power_off()

        with target.open("rb") as disk:
            self.assertTrue(gpt_partitions(disk), "the installed disk has no readable GPT")
            installed = grub_config(disk)
        self.assertIsNotNone(installed, "no EFI/BOOT/grub.cfg on the installed disk's EFI partition")
        self.assertNotIn("console=ttyS0", installed, "the installed boot points its console at serial")
        self.assertNotRegex(installed, r"(?m)^\s*terminal_output\s+serial\s*$")

        self.vm = VM(image=target)
        self.addCleanup(self.vm.destroy)
        self.last_lit = None
        self.vm.start()
        self.reach_eyes("36-installed-eyes")
        self.assert_no_serial_login("installed disk")


if __name__ == "__main__":
    unittest.main()
