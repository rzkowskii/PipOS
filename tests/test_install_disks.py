import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import install_disks


class DiskInventoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.class_block = self.root / "sys/class/block"
        self.class_block.mkdir(parents=True)
        self.proc = self.root / "proc"
        (self.proc / "self").mkdir(parents=True)
        self.swaps = self.proc / "swaps"
        self.swaps.write_text("Filename\tType\tSize\tUsed\tPriority\n")
        self.numbers = {}
        self.enterContext(patch.object(install_disks, "SYS_BLOCK", self.class_block))
        self.enterContext(patch.object(install_disks, "PROC", self.proc))
        self.enterContext(patch.object(install_disks, "node_number", side_effect=self.node_number))

    def node_number(self, path, *, allow_file=False):
        return self.numbers.get(path)

    def add_block(self, name, number, *, transport="ata1", parent=None, read_only=False, size_gib=8):
        path = parent / name if parent else self.root / "sys/devices" / transport / "block" / name
        path.mkdir(parents=True)
        (path / "dev").write_text(number)
        (path / "ro").write_text(str(int(read_only)))
        (path / "holders").mkdir()
        if parent:
            (path / "partition").write_text("1\n")
        else:
            (path / "slaves").mkdir()
            (path / "device").mkdir()
            (path / "queue").mkdir()
            (path / "queue/logical_block_size").write_text("512")
            (path / "size").write_text(str(size_gib * 1024**3 // 512))
        (self.class_block / name).symlink_to(path)
        self.numbers[f"/dev/{name}"] = number
        return path

    def mounts(self, *rows):
        lines = [f"{index} 0 {number} / {mount} ro - {kind} {source} ro\n"
                 for index, (number, mount, kind, source) in enumerate(rows, 1)]
        (self.proc / "self/mountinfo").write_text("".join(lines))

    def inspect(self, **options):
        result = install_disks.inventory(**options)
        return result, {disk["device"]: disk for disk in result["disks"]}

    def test_usb_boot_and_distinct_sata_and_virtio_targets(self):
        usb = self.add_block("sda", "8:0", transport="usb1/1-1")
        self.add_block("sda2", "8:2", parent=usb)
        self.add_block("sda4", "8:4", parent=usb)
        sata = self.add_block("sdaa", "65:160", transport="ata2")
        self.add_block("vda", "253:0", transport="virtio0")
        (sata / "device/model").write_text("QEMU HARDDISK  \n")
        serial = b"BEINGOS-TEST-TARGET"
        (sata / "device/vpd_pg80").write_bytes(bytes((0, 0x80, 0, len(serial))) + serial)
        (sata / "diskseq").write_text("17\n")
        (sata / "queue/logical_block_size").write_text("4096\n")
        self.mounts(("8:2", "/", "squashfs", "/dev/root"), ("8:4", "/models", "ext4", "/dev/sda4"))

        result, disks = self.inspect()
        self.assertEqual(result["boot_disk"], "/dev/sda")
        self.assertEqual(result["model_disk"], "/dev/sda")
        self.assertEqual(disks["/dev/sda"]["reasons"], ["boot_disk", "mounted"])
        self.assertEqual(disks["/dev/sda"]["transport"], "usb")
        self.assertEqual(disks["/dev/sdaa"], {"device": "/dev/sdaa", "device_number": "65:160",
                                              "model": "QEMU HARDDISK", "size_bytes": 8 * 1024**3,
                                              "logical_sector_bytes": 4096, "eligible": True, "reasons": [],
                                              "transport": "sata", "serial": serial.decode(), "device_sequence": 17})
        self.assertTrue(disks["/dev/vda"]["eligible"])

    def test_root_device_alias_and_duplicate_model_media_are_tracked_independently(self):
        usb = self.add_block("sda", "8:0", transport="usb1")
        self.add_block("sda2", "8:2", parent=usb)
        second = self.add_block("sdb", "8:16")
        self.add_block("sdb5", "8:21", parent=second)
        self.numbers["/dev/root"] = "8:2"
        self.mounts(("0:12", "/", "squashfs", "/dev/root"), ("8:21", "/models", "ext4", "/dev/sdb5"))

        result, disks = self.inspect()
        self.assertEqual(result["boot_disk"], "/dev/sda")
        self.assertEqual(result["model_disk"], "/dev/sdb")
        self.assertEqual(disks["/dev/sdb"]["reasons"], ["mounted"])

    def test_in_use_read_only_small_and_virtual_devices_are_ineligible(self):
        self.add_block("vda", "253:0", transport="virtio0")
        mounted = self.add_block("sdb", "8:16")
        self.add_block("sdb5", "8:21", parent=mounted)
        swap = self.add_block("sdc", "8:32")
        self.add_block("sdc1", "8:33", parent=swap)
        held = self.add_block("sdd", "8:48")
        held_partition = self.add_block("sdd1", "8:49", parent=held)
        mapper = self.add_block("dm-0", "252:0", transport="virtual")
        (held_partition / "holders/dm-0").symlink_to(mapper)
        (mapper / "slaves/sdd1").symlink_to(held_partition)
        self.add_block("sde", "8:64", read_only=True)
        self.add_block("sdf", "8:80", size_gib=5)
        self.add_block("loop0", "7:0", transport="virtual")
        self.mounts(("253:0", "/", "squashfs", "/dev/root"), ("8:21", "/mind", "ext4", "/dev/sdb5"))
        with self.swaps.open("a") as output:
            output.write("/dev/sdc1\tpartition\t4096\t0\t-2\n")

        result, disks = self.inspect(min_size_bytes=6 * 1024**3)
        self.assertEqual(result["boot_disk"], "/dev/vda")
        for name, reason in (("sdb", "mounted"), ("sdc", "active_swap"), ("sdd", "held_or_stacked"),
                             ("sde", "read_only"), ("sdf", "too_small"), ("loop0", "unsupported_device")):
            with self.subTest(name=name):
                self.assertFalse(disks[f"/dev/{name}"]["eligible"])
                self.assertIn(reason, disks[f"/dev/{name}"]["reasons"])

    def test_ambiguous_root_or_device_node_fails_closed(self):
        self.add_block("vda", "253:0", transport="virtio0")
        self.add_block("sdb", "8:16")
        self.numbers["/dev/sdb"] = "253:0"
        self.mounts(("253:0", "/", "squashfs", "/dev/vda"))
        _, disks = self.inspect()
        self.assertIn("inspection_failed", disks["/dev/sdb"]["reasons"])

        self.numbers["/dev/sdb"] = "8:16"
        self.mounts(("253:0", "/", "squashfs", "/dev/vda"), ("8:16", "/", "squashfs", "/dev/sdb"))
        result, disks = self.inspect()
        self.assertIsNone(result["boot_disk"])
        self.assertTrue(all("boot_disk_unknown" in disk["reasons"] for disk in disks.values()))


class TargetContents(unittest.TestCase):
    """What an install would erase, read from the target's GPT without writing."""

    def image(self, parts, *, sector=512, capacity=64 * 1024**2, signature=None):
        import uuid as uuid_module

        from mind.install_apply import gpt_records

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "disk.img"
        mbr, primary, table, _, _ = gpt_records(
            sector, capacity, parts, uuid_module.uuid4(), {part["name"]: uuid_module.uuid4() for part in parts})
        with path.open("wb") as disk:
            disk.truncate(capacity)
            for data, offset in ((mbr, 0), (primary, sector), (table, 2 * sector)):
                disk.seek(offset)
                disk.write(data)
            if signature:
                offset, data = signature
                disk.seek(offset)
                disk.write(data)
        before = path.read_bytes()
        self.addCleanup(lambda: self.assertEqual(path.read_bytes(), before, "inspection wrote to the disk"))
        return str(path)

    @staticmethod
    def part(name, kind, start, count=2048):
        return {"name": name, "gpt_type": kind, "start_lba": start, "sector_count": count}

    def test_apfs_partition_means_macos(self):
        from mind import install_disks

        disk = self.image([self.part("EFI", "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", 2048),
                           self.part("Macintosh HD", install_disks.APFS_TYPE, 4096)])
        found = install_disks.disk_contents(disk)
        self.assertEqual((found["readable"], found["macos"], found["being"], found["partitions"]), (True, True, False, 2))

    def test_hfs_signature_without_an_apple_type_still_means_macos(self):
        from mind import install_disks

        linux = "0fc63daf-8483-4772-8e79-3d69d8477de4"
        disk = self.image([self.part("data", linux, 2048)], signature=(2048 * 512 + 1024, b"H+"))
        self.assertTrue(install_disks.disk_contents(disk)["macos"])

    def test_pipos_layout_is_a_being_and_blank_disk_is_neither(self):
        from mind import install_disks

        linux, esp = "0fc63daf-8483-4772-8e79-3d69d8477de4", "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
        names = install_disks.BEING_NAMES
        disk = self.image([self.part(name, esp if name == "esp" else linux, 2048 + index * 2048)
                           for index, name in enumerate(names)])
        found = install_disks.disk_contents(disk)
        self.assertTrue(found["being"])
        self.assertFalse(found["macos"])
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        blank = directory / "blank.img"
        blank.write_bytes(bytes(1024 * 1024))
        self.assertEqual(install_disks.disk_contents(str(blank)),
                         {"macos": False, "being": False, "partitions": 0, "readable": True})
        self.assertFalse(install_disks.disk_contents(str(directory / "missing"))["readable"])
