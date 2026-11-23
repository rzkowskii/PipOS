"""Check the installable PipOS ISO (lab/make-iso.sh) as the MacBook would use it.

The ISO is written whole to a USB stick and booted with the Option key, so
every check here treats it as a USB disk: first what is on it (its GPT, the
El Torito entry, the boot menu and the partition GUIDs the kernel and the
Try generator look for), then the same screen-and-keyboard boots as the
laptop Try smoke test (lab/tests/test_laptop_smoke.py), with the ISO
attached as USB storage next to a blank SATA disk: GRUB's menu on the
panel, the Try menu and the eyes, and Install from the screen menu onto
the blank disk, which then boots alone to the eyes.

    lab/make-iso.sh
    python3 -m unittest lab.tests.test_iso -v

BEING_ISO_IMAGE picks another ISO; BEING_LAPTOP_PATIENCE multiplies every
wait. Screenshots land in out/lab/iso-smoke-*.png. Booting the ISO from an
optical drive is not tested: the MacBook boots it from USB.
"""

import os
import re
import struct
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from lab.tests import test_laptop_smoke as smoke
from lab.vm import ROOT, png

ISO = Path(os.environ.get("BEING_ISO_IMAGE", ROOT / "out/iso/pipos-macbook.iso")).resolve()
SECTOR = 512
ISO_BLOCK = 2048
ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX_TYPE = "0fc63daf-8483-4772-8e79-3d69d8477de4"
DATA_TYPE = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
# The Try image's partitions, by GPT entry; entry 1 is the ISO 9660 data.
LAYOUT = {2: ("try-esp", ESP_TYPE), 3: ("try-system", LINUX_TYPE), 4: ("try-models", LINUX_TYPE),
          5: ("try-logs", DATA_TYPE)}
READ_ONLY = 1 << 60


def gpt(disk):
    """(header fields, {entry number: (type, guid, name, first byte, size, attributes)})."""
    disk.seek(SECTOR)
    header = disk.read(92)
    if header[:8] != b"EFI PART":
        return None, {}
    current, backup, first_usable, last_usable = struct.unpack_from("<QQQQ", header, 24)
    disk_guid = str(uuid.UUID(bytes_le=header[56:72]))
    entries_lba, count, size = struct.unpack_from("<QII", header, 72)
    disk.seek(entries_lba * SECTOR)
    table = disk.read(count * size)
    entries = {}
    for index in range(count):
        entry = table[index * size:(index + 1) * size]
        if entry[:16] == bytes(16):
            continue
        first, last, attributes = struct.unpack_from("<QQQ", entry, 32)
        name = entry[56:128].decode("utf-16-le", "replace").split("\0", 1)[0]
        entries[index + 1] = (str(uuid.UUID(bytes_le=entry[:16])), str(uuid.UUID(bytes_le=entry[16:32])), name,
                              first * SECTOR, (last - first + 1) * SECTOR, attributes)
    return {"current": current, "backup": backup, "last_usable": last_usable, "disk_guid": disk_guid}, entries


def el_torito_images(disk):
    """[(platform id, load RBA in 2 KiB blocks, sector count)] from the ISO's boot catalog."""
    disk.seek(17 * ISO_BLOCK)
    record = disk.read(ISO_BLOCK)
    if record[0] != 0 or record[1:6] != b"CD001" or not record[7:30].startswith(b"EL TORITO SPECIFICATION"):
        return []
    catalog = struct.unpack_from("<I", record, 0x47)[0]
    disk.seek(catalog * ISO_BLOCK)
    data = disk.read(ISO_BLOCK)
    if data[0] != 1 or data[30:32] != b"\x55\xaa":
        return []
    images, platform = [], data[1]
    at = 32
    entry = data[at:at + 32]
    if entry[0] == 0x88:
        images.append((platform, struct.unpack_from("<I", entry, 8)[0], struct.unpack_from("<H", entry, 6)[0]))
    at += 32
    while at + 32 <= len(data):
        head = data[at:at + 32]
        if head[0] not in (0x90, 0x91):
            break
        platform, count = head[1], struct.unpack_from("<H", head, 2)[0]
        for number in range(count):
            at += 32
            entry = data[at:at + 32]
            if entry[0] == 0x88:
                images.append((platform, struct.unpack_from("<I", entry, 8)[0],
                               struct.unpack_from("<H", entry, 6)[0]))
        at += 32
        if head[0] == 0x91:
            break
    return images


def ext4_label(disk, offset):
    disk.seek(offset + 1024)
    superblock = disk.read(1024)
    if superblock[56:58] != b"\x53\xef":
        return None
    return superblock[120:136].split(b"\0", 1)[0].decode("ascii", "replace")


@unittest.skipUnless(ISO.exists(), "needs the ISO: run lab/make-iso.sh")
class IsoMedia(unittest.TestCase):
    """What is on the ISO, read from the Mac side, before anything boots."""

    def setUp(self):
        self.disk = self.enterContext(ISO.open("rb"))
        self.header, self.entries = gpt(self.disk)

    def test_is_a_gpt_disk_with_the_try_partitions(self):
        self.assertIsNotNone(self.header, "no GPT: a USB stick written with this would not boot on the Mac")
        size = ISO.stat().st_size
        self.assertEqual(size % ISO_BLOCK, 0)
        self.assertEqual(self.header["backup"], size // SECTOR - 1, "the backup GPT is not at the end of the image")
        self.assertEqual(sorted(self.entries), [1, 2, 3, 4, 5], self.entries)
        for number, (name, kind) in LAYOUT.items():
            entry = self.entries[number]
            self.assertEqual((entry[2], entry[0]), (name, kind), f"GPT entry {number}")
        for number in (3, 4):
            self.assertTrue(self.entries[number][5] & READ_ONLY, f"{self.entries[number][2]} is not marked read-only")
        guids = [entry[1] for entry in self.entries.values()] + [self.header["disk_guid"]]
        self.assertEqual(len(set(guids)), len(guids), "GPT GUIDs repeat")
        # Partitions lie one after another, inside the usable area.
        spans = sorted((entry[3], entry[3] + entry[4]) for entry in self.entries.values())
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end, start, f"partitions overlap: {spans}")
        self.assertLessEqual(spans[-1][1], (self.header["last_usable"] + 1) * SECTOR)

    def test_protective_mbr_only(self):
        # A hybrid MBR could make Apple's firmware offer a legacy (BIOS) boot.
        self.disk.seek(0)
        mbr = self.disk.read(SECTOR)
        self.assertEqual(mbr[510:512], b"\x55\xaa")
        types = [mbr[446 + 16 * index + 4] for index in range(4)]
        self.assertEqual(types.count(0xEE), 1, types)
        self.assertEqual([kind for kind in types if kind not in (0, 0xEE)], [], types)

    def test_partitions_hold_their_file_systems(self):
        _, _, _, offset, size, _ = self.entries[3]
        self.disk.seek(offset)
        superblock = self.disk.read(96)
        self.assertEqual(superblock[:4], b"hsqs", "try-system is not SquashFS")
        self.assertLessEqual(struct.unpack_from("<Q", superblock, 40)[0], size, "the SquashFS is cut short")
        self.assertEqual(ext4_label(self.disk, self.entries[4][3]), "being-try-models",
                         "try-models must not carry the installed LABEL=models")
        volumes = smoke.fat_volumes(self.disk)
        self.assertIn("try-esp", volumes)
        esp = volumes["try-esp"]
        self.assertGreater(len(esp.read("EFI/BOOT/bootx64.efi")), 100_000)
        logs = smoke.log_volume(self.disk)
        self.assertIsNotNone(logs, "no PIPLOGS partition")
        self.assertIn("try-logs", volumes)
        self.assertEqual((logs.offset, volumes["try-logs"].label), (self.entries[5][3], smoke.LOG_LABEL),
                         "PIPLOGS is not the partition named try-logs")
        self.assertIn("README.txt", [entry.name for entry in logs.listdir()])

    def test_el_torito_efi_entry_is_the_efi_partition(self):
        images = el_torito_images(self.disk)
        efi = [(rba, count) for platform, rba, count in images if platform == 0xEF]
        self.assertEqual(len(efi), 1, images)
        rba, count = efi[0]
        _, _, _, offset, size, _ = self.entries[2]
        self.assertEqual(rba * ISO_BLOCK, offset, "El Torito boots something other than try-esp")
        # The exact size, not 0: firmware that boots through El Torito then
        # describes the same start and size as GPT entry 2, so GRUB finds
        # its grub.cfg on gpt2 rather than looking on the whole disk.
        self.assertEqual(count, size // SECTOR, "the El Torito entry does not state try-esp's size")

    def test_partitions_start_on_1_mib_and_the_first_is_an_iso_9660_volume(self):
        for number, entry in self.entries.items():
            if number != 1:
                self.assertEqual(entry[3] % (1 << 20), 0, f"GPT entry {number} is not 1 MiB aligned")
        self.disk.seek(self.entries[1][3] + 16 * ISO_BLOCK)
        self.assertEqual(self.disk.read(6), b"\x01CD001", "GPT entry 1 does not hold an ISO 9660 volume")

    def test_boot_menu_mounts_the_partitions_on_this_iso(self):
        config = smoke.grub_config(self.disk)
        self.assertIsNotNone(config, "no grub.cfg on try-esp")
        entries = smoke.menu_entries(config)
        self.assertEqual(len(entries), 3, [title for title, _ in entries])
        system, models = self.entries[3][1], self.entries[4][1]
        for title, body in entries:
            self.assertIn(f"root=PARTUUID={system}", body, f"{title!r} does not mount try-system")
            self.assertIn(f"being.models={models}", body, f"{title!r} does not mount try-models")
            self.assertRegex(body, r'linux\s+"\(\$pipos_disk,gpt3\)/boot/bzImage"', title)
            for flag in ("being.try=1", "being.screen=1", "hid_apple.fnmode=2", "rootwait=60"):
                self.assertIn(flag, body.split(), f"{title!r} lacks {flag}")
            self.assertNotRegex(body, r"\b(ISO|TRY)_[A-Z]+_UUID\b|BEING_[A-Z]+_ARGS", f"{title!r} kept a placeholder")

    def test_laptop_menu_rules(self):
        """The laptop Try media's own checks on its boot menu, against the ISO."""
        with patch.object(smoke, "LAPTOP_TRY_IMAGE", ISO):
            smoke.LaptopTryMedia.test_boot_menu_is_on_the_panel_with_six_entries_and_a_timeout(self)
            smoke.LaptopTryMedia.test_stick_has_a_fat_log_partition(self)


@unittest.skipUnless(ISO.exists(), "needs the ISO: run lab/make-iso.sh")
class IsoSmoke(smoke.LaptopSmoke):
    """Boot the ISO as a USB stick next to a blank internal disk, eyes and keys only."""

    # test_laptop_smoke skips its class when the laptop Try image is missing;
    # this class needs only the ISO.
    __unittest_skip__ = not ISO.exists()

    # Kept: the default boot (GRUB menu, Try menu, eyes, PIPLOGS) and the
    # install. The other laptop boots exercise the same kernel and menu.
    test_hard_power_off_is_recorded_as_an_unclean_end = None
    test_grub_has_three_entries_and_boot_messages_show_kernel_text = None
    test_safe_graphics_still_reaches_the_eyes = None

    def setUp(self):
        patcher = patch.object(smoke, "LAPTOP_TRY_IMAGE", ISO)
        patcher.start()
        self.addCleanup(patcher.stop)

    def screenshot(self, name, frame):
        smoke.SHOTS.mkdir(parents=True, exist_ok=True)
        path = smoke.SHOTS / f"iso-smoke-{name}.png"
        path.write_bytes(png(*frame))
        return path


if __name__ == "__main__":
    unittest.main()
