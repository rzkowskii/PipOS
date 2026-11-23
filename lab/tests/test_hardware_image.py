import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from lab.hardware_image import copy_partition, entries, kernel_drivers


class HardwareImageTests(unittest.TestCase):
    def test_audit_reads_only_exact_squashfs_payload_and_rejects_bad_size(self):
        with tempfile.TemporaryDirectory() as folder:
            image, scratch = (Path(folder) / name for name in ("disk", "filesystem"))
            header = bytearray(96)
            header[:4] = b"hsqs"
            squashfs = b"payload"
            struct.pack_into("<Q", header, 40, len(header) + len(squashfs))
            data = bytes(header) + squashfs
            image.write_bytes(b"other-contents" + data + b"padding")
            part = {"Path": "1.try-system.img", "Offset": 14, "Size": len(data) + 7}
            result = copy_partition(image, part, scratch, squashfs=True)
            self.assertEqual(scratch.read_bytes(), data)
            self.assertEqual(result, hashlib.sha256(data).hexdigest())
            with self.assertRaisesRegex(ValueError, "byte count"):
                copy_partition(image, {**part, "Size": len(data) - 1}, scratch, squashfs=True)
            with self.assertRaisesRegex(ValueError, "partition extent"):
                copy_partition(image, {**part, "Size": image.stat().st_size}, scratch, squashfs=True)

    def test_audit_distinguishes_shipped_modules_from_kernel_builtins(self):
        raw = b"Path = 1.system-a.img\nSize = 4096\nOffset = 1048576\n\nPath = 2.system-b.img\nSize = 4096\n"
        self.assertEqual([item["Path"] for item in entries(raw)], ["1.system-a.img", "2.system-b.img"])
        result = kernel_drivers({"usr/lib/modules/kernel/b43.ko.xz", "lib/backup/bcma.ko.old"},
                                "kernel/drivers/net/tg3.ko\nkernel/sound/snd-hda-intel.ko\n")
        self.assertEqual(result["b43"], "module")
        self.assertIsNone(result["bcma"])
        self.assertEqual(result["gigabit_ethernet"], "built-in")
        self.assertEqual(result["hda_controller"], "built-in")
