import os
import struct
import tempfile
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

from mind import install_apply as writer
from mind.installer import GIB, installation_plan


def approved(sector=512):
    return installation_plan({
        "boot_disk": "/dev/sda",
        "disks": [{
            "device": "/dev/sdb", "device_number": "8:16", "device_sequence": 17,
            "eligible": True, "size_bytes": 8 * GIB, "logical_sector_bytes": sector,
        }],
    }, 512 * writer.MIB, "/dev/sdb")


class PartitionWriterTests(unittest.TestCase):
    def test_both_gpt_headers_cover_fresh_ids_and_checksums(self):
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                plan = approved(sector)
                s, capacity = writer.geometry(plan)
                self.assertEqual(s, sector)
                names = writer.PARTITIONS
                ids = {key: uuid.uuid4() for key in names}
                disk_id = uuid.uuid4()
                mbr, primary, table, backup, backup_offset = writer.gpt_records(
                    s, capacity, plan["partitions"], disk_id, ids
                )
                self.assertEqual(mbr[450], 0xEE)
                self.assertEqual(mbr[510:512], b"\x55\xaa")
                for header, current, alternate, table_lba in (
                    (primary, 1, capacity // sector - 1, 2),
                    (backup, capacity // sector - 1, 1, backup_offset // sector),
                ):
                    self.assertEqual(header[:8], b"EFI PART")
                    length, checksum = struct.unpack_from("<II", header, 12)
                    checked = bytearray(header[:length])
                    checked[16:20] = bytes(4)
                    self.assertEqual(zlib.crc32(checked), checksum)
                    self.assertEqual(struct.unpack_from("<QQ", header, 24), (current, alternate))
                    self.assertEqual(uuid.UUID(bytes_le=bytes(header[56:72])), disk_id)
                    self.assertEqual(struct.unpack_from("<QIII", header, 72),
                                     (table_lba, 128, 128, zlib.crc32(table)))
                for index, partition in enumerate(plan["partitions"]):
                    _, unique, start, end, _, name = struct.unpack_from("<16s16sQQQ72s", table, index * 128)
                    self.assertEqual(uuid.UUID(bytes_le=unique), ids[partition["name"]])
                    self.assertEqual(name.decode("utf-16le").rstrip("\0"), partition["name"])
                    self.assertEqual((start, end), (partition["start_lba"],
                                                  partition["start_lba"] + partition["sector_count"] - 1))

    def test_geometry_rejects_a_partition_overlapping_the_boot_records(self):
        plan = approved()
        plan["partitions"][0]["start_lba"] = 0
        with self.assertRaisesRegex(ValueError, "layout"):
            writer.geometry(plan)

    def test_validation_rejects_a_changed_target_before_opening_it(self):
        plan = approved()
        with patch.object(writer.os, "open") as opened:
            with self.assertRaisesRegex(ValueError, "changed"):
                with writer.claim_target(plan, lambda: {}, *writer.geometry(plan)):
                    self.fail("claimed a changed target")
        opened.assert_not_called()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sector = 512
        self.capacity = 9 * writer.MIB
        self.parts = [{"name": name, "start_lba": (index + 1) * (writer.MIB // self.sector),
                       "size_bytes": writer.MIB, "sector_count": writer.MIB // self.sector,
                       "gpt_type": writer.TYPES[0 if index == 0 else 1]}
                      for index, name in enumerate(writer.PARTITIONS)]
        ids = {name: uuid.uuid4() for name in writer.PARTITIONS}
        self.records = writer.gpt_records(self.sector, self.capacity, self.parts, uuid.uuid4(), ids)
        self.images = {}
        self.payloads = dict(esp=b"an EFI tree", models=b"test models", mind=b"empty mind")
        for name, contents in self.payloads.items():
            image = folder / name
            writer.empty_image(image, writer.MIB)
            with image.open("r+b") as file:
                file.write(contents)
            self.images[name] = image
        root = folder / "root"
        root.write_bytes(bytes(range(256)) * 16)
        self.root_bytes = root.read_bytes()
        self.source = self.enterContext(root.open("rb")).fileno()
        target = folder / "target"
        writer.empty_image(target, self.capacity)
        self.target = self.enterContext(target.open("r+b", buffering=0)).fileno()
        for offset in (0, self.sector, self.capacity - self.sector):
            os.pwrite(self.target, b"old records", offset)
        self.enterContext(patch.object(writer.fcntl, "ioctl"))

    def write(self, progress, meter=None):
        writer.write_validated(self.target, self.capacity, self.sector, self.parts, self.records,
                               self.images, self.source, len(self.root_bytes), progress, meter)

    def check_unbootable(self):
        for offset in (0, self.sector, self.capacity - self.sector):
            self.assertEqual(os.pread(self.target, self.sector, offset), bytes(self.sector))

    def test_interruption_before_commit_keeps_both_boot_records_invalid(self):
        def abort(stage):
            if stage == "Committing the installation boot records.":
                raise InterruptedError("fixture stopped before publication")

        with self.assertRaises(InterruptedError):
            self.write(abort)
        self.check_unbootable()

    def test_corrupt_readback_prevents_gpt_publication(self):
        def corrupt(stage):
            if stage == "Verifying installation data.":
                os.pwrite(self.target, b"broken", writer.MIB * 2)

        with self.assertRaisesRegex(OSError, "failed verification"):
            self.write(corrupt)
        self.check_unbootable()

    def test_both_slots_and_data_are_written_before_new_boot_records(self):
        seen = []
        self.write(seen.append)
        self.assertEqual(seen, ["Writing installation data.", "Verifying installation data.",
                                "Committing the installation boot records."])
        for part in self.parts:
            name = part["name"]
            expected = self.payloads.get(name, self.root_bytes)
            self.assertEqual(os.pread(self.target, len(expected), part["start_lba"] * self.sector), expected)
        mbr, primary, table, backup, offset = self.records
        for position, expected in ((0, mbr), (self.sector, primary), (2 * self.sector, table),
                                   (offset, table), (self.capacity - self.sector, backup)):
            self.assertEqual(os.pread(self.target, len(expected), position), expected)

    def test_meter_reports_whole_write_and_readback_without_changing_the_result(self):
        stages, readings = [], []
        self.write(stages.append, lambda done, total: readings.append((stages[-1], done, total)))
        self.assertEqual(stages, ["Writing installation data.", "Verifying installation data.",
                                  "Committing the installation boot records."])
        total = readings[0][2]
        self.assertGreaterEqual(total, 2 * len(self.root_bytes))
        for stage in stages[:2]:
            seen = [done for name, done, size in readings if name == stage and size == total]
            self.assertTrue(seen, stage)
            self.assertEqual(seen, sorted(seen))
            self.assertEqual(seen[-1], total)
        self.assertFalse([reading for reading in readings if reading[0] == stages[2]])
        for part in self.parts:
            expected = self.payloads.get(part["name"], self.root_bytes)
            self.assertEqual(os.pread(self.target, len(expected), part["start_lba"] * self.sector), expected)
