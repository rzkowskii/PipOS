"""The installed preflight excludes its boot media and plans an untouched disk."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.vm import IMAGE, VM, machine_args

INSTALLER_IMAGE = Path(os.environ.get("BEING_INSTALLER_IMAGE", IMAGE)).resolve()
TARGET_BYTES = 8 << 30
TARGET_MODEL = "BEINGOS TARGET"
TARGET_SERIAL = "BEINGOS-TEST-TARGET"

# The second disk belongs to this test. Create a small Linux partition in it
# only after checking that the installer left the blank disk untouched.
PARTITION_FIXTURE = """
import fcntl
import os
import struct
import sys
from pathlib import Path

target, expected_model, expected_size = sys.argv[1:]
block = Path("/sys/class/block") / Path(target).name
model = (block / "device" / "model").read_text().strip()
size = int((block / "size").read_text()) * 512
if model != expected_model or size != int(expected_size):
    raise SystemExit("the fixture target does not match the disposable disk")
sector = bytearray(512)
struct.pack_into("<B3xB3xII", sector, 446, 0, 0x83, 2048, 256 * 1024)
sector[510:] = b"\\x55\\xaa"
with open(target, "r+b", buffering=0) as disk:
    disk.write(sector)
    os.fsync(disk.fileno())
    fcntl.ioctl(disk, 0x125f)  # BLKRRPART
"""


def fingerprint(path):
    stat = path.stat()
    return stat.st_size, stat.st_blocks, stat.st_mtime_ns


@unittest.skipUnless(INSTALLER_IMAGE.exists(), "no image: run lab/build.sh first")
class InstallerPreflight(unittest.TestCase):
    def sh(self, command, timeout=30):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def inventory(self):
        result = json.loads(self.sh("being-installer --list --json"))
        return result["boot_disk"], {row["device"]: row for row in result["disks"]}

    def refused(self, device, reason):
        status, output = self.vm.run(f"being-installer --plan {device} --json", 30)
        self.assertEqual(status, 1, output)
        self.assertIn(reason, output.lower())

    def partition_table(self, device):
        return self.sh(f"dd if={device} bs=512 count=34 2>/dev/null | sha256sum")

    def test_preflight_keeps_running_disk_and_target_unchanged_and_refuses_mounted_child(self):
        files = tempfile.TemporaryDirectory(prefix="being-installer-lab-", dir="/tmp")
        self.addCleanup(files.cleanup)
        target = Path(files.name) / "target.raw"
        with target.open("wb") as disk:
            disk.truncate(TARGET_BYTES)
        blank = fingerprint(target)
        self.assertEqual(blank[1], 0, "the target fixture must begin with no allocated data blocks")

        def usb_boot_with_sata_target(*args, **kwargs):
            command = machine_args(*args, **kwargs)
            boot_device = command.index("ide-hd,drive=disk0,bus=ide.0")
            del command[boot_device - 1:boot_device + 1]
            controller = command.index("qemu-xhci")
            command[controller] = "qemu-xhci,id=usb-controller"
            return command + [
                "-device", "usb-storage,drive=disk0,bus=usb-controller.0,bootindex=1",
                "-drive", f"if=none,id=installer-target,format=raw,file={target}",
                "-device", f"ide-hd,drive=installer-target,bus=ide.0,bootindex=2,model={TARGET_MODEL},serial={TARGET_SERIAL}",
            ]

        self.vm = VM(image=INSTALLER_IMAGE)
        self.addCleanup(self.vm.destroy)
        with patch("lab.vm.machine_args", usb_boot_with_sata_target):
            self.vm.start()
        self.vm.login(120)
        self.vm.select_try_from_screen()
        hardware = json.loads(self.sh("being-installer --check --json"))
        self.assertEqual(hardware["firmware"]["mode"], "UEFI")
        self.assertGreater(hardware["ram_mib"], 0)
        for key in ("capture_pcm_detected", "playback_pcm_detected"):
            self.assertIn(key, hardware["audio"])
        boot_disk, disks = self.inventory()
        self.assertRegex(boot_disk, r"\A/dev/sd[a-z]+\Z")
        self.assertEqual(disks[boot_disk]["transport"], "usb")
        boot_table = self.partition_table(boot_disk)
        candidates = [device for device, row in disks.items()
                      if row["model"] == TARGET_MODEL and row["size_bytes"] == TARGET_BYTES]
        self.assertEqual(len(candidates), 1, disks)
        target_disk = candidates[0]
        self.assertRegex(target_disk, r"\A/dev/sd[a-z]+\Z")
        self.assertNotEqual(target_disk, boot_disk)
        self.assertEqual(disks[target_disk]["transport"], "sata")
        self.assertFalse(disks[boot_disk]["eligible"], disks[boot_disk])
        self.assertTrue(disks[target_disk]["eligible"], disks[target_disk])
        self.refused(boot_disk, "running beingos disk")

        plan = json.loads(self.sh(f"being-installer --plan {target_disk} --json"))
        self.assertEqual(plan["boot_disk"], boot_disk)
        self.assertEqual(plan["target"], disks[target_disk])
        partitions = plan["partitions"]
        self.assertEqual([part["name"] for part in partitions], ["esp", "system-a", "system-b", "models", "mind"])
        self.assertEqual([part["filesystem"] for part in partitions], ["vfat", "squashfs", "squashfs", "ext4", "ext4"])
        self.assertGreater(plan["model_bytes_used"], 0)
        self.assertGreaterEqual(partitions[3]["size_bytes"], 2 * plan["model_bytes_used"])
        end_lba = 34
        for part in partitions:
            self.assertGreaterEqual(part["start_lba"], end_lba)
            self.assertGreater(part["sector_count"], 0)
            self.assertEqual(part["size_bytes"], 512 * part["sector_count"])
            end_lba = part["start_lba"] + part["sector_count"]
        self.assertLess(end_lba, TARGET_BYTES // 512)
        self.sh("sync")
        self.assertEqual(fingerprint(target), blank, "preflight changed the blank target")
        self.assertEqual(self.partition_table(boot_disk), boot_table,
                         "preflight changed the running disk's partition table")

        self.vm.put("/run/installer-partition-fixture.py", PARTITION_FIXTURE)
        self.sh(f"python3 /run/installer-partition-fixture.py {target_disk} '{TARGET_MODEL}' {TARGET_BYTES} && "
                "udevadm settle --timeout=15")
        part = f"{target_disk}1"
        self.sh(f"mke2fs -q -t ext4 {part} && mkdir /run/installer-mounted && "
                f"mount -t ext4 -o ro,noload {part} /run/installer-mounted")
        after_mount, disks = self.inventory()
        self.assertEqual(after_mount, boot_disk)
        mounted = disks[target_disk]
        self.assertFalse(mounted["eligible"], mounted)
        self.refused(target_disk, "mount")
        self.sh("umount /run/installer-mounted")


if __name__ == "__main__":
    unittest.main()
