"""Install from virtual USB and exercise the resulting disk on fresh firmware."""

import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import tempfile
import time
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

from lab.vm import IMAGE, ROOT, VM, Timeout, machine_args, overlay


TRY_IMAGE = Path(os.environ.get("BEING_INSTALL_TRY_IMAGE", ROOT / "out/images/beingos-try-install.img")).resolve()
OLD_IMAGE = Path(os.environ.get("BEING_INSTALL_OLD_IMAGE", IMAGE)).resolve()
UPDATE_IMAGE = Path(os.environ.get("BEING_INSTALL_UPDATE_IMAGE", ROOT / "out/images/beingos-installed-update-qa.img")).resolve()
BUNDLE = Path(os.environ.get("BEING_INSTALL_BUNDLE", ROOT / "out/images/beingos-installed.raucb")).resolve()
HARDWARE_BUNDLE = Path(os.environ.get("BEING_HARDWARE_BUNDLE", ROOT / "out/images/beingos-install-qa-lab.raucb")).resolve()
INSTALLED_BUNDLE = "/mnt/updates/installed.raucb"
LAB_BUNDLE = "/mnt/updates/lab.raucb"
TARGET_BYTES = 8 << 30
TARGET_MODEL = "PipOS target"
TARGET_SERIAL = "BEINGOS-INSTALL-TEST"
SECTOR = 512
PARTITIONS = ("esp", "system-a", "system-b", "models", "mind")
BOOT_ENV = "/bootstate/EFI/BOOT/grubenv"
FACT = "my telescope is called Ultraviolet"
RECALL = "What do you remember about my telescope?"


def read_gpt(disk, lba):
    disk.seek(lba * SECTOR)
    sector = disk.read(SECTOR)
    (signature, revision, header_size, header_crc, reserved, current, alternate, first, last,
     disk_guid, entries_lba, count, entry_size, entries_crc) = struct.unpack_from("<8sIIIIQQQQ16sQIII", sector)
    if (signature != b"EFI PART" or revision != 0x10000 or not 92 <= header_size <= SECTOR
            or current != lba or reserved != 0 or not 1 <= count <= 4096 or entry_size != 128):
        raise ValueError(f"invalid GPT header at LBA {lba}")
    if zlib.crc32(sector[:16] + bytes(4) + sector[20:header_size]) != header_crc:
        raise ValueError(f"incorrect GPT header checksum at LBA {lba}")
    disk.seek(entries_lba * SECTOR)
    entries = disk.read(count * entry_size)
    if zlib.crc32(entries) != entries_crc:
        raise ValueError(f"incorrect GPT entry checksum at LBA {lba}")
    partitions = []
    for offset in range(0, len(entries), entry_size):
        type_guid, part_guid, start, end, _, name = struct.unpack_from("<16s16sQQQ72s", entries, offset)
        if type_guid != bytes(16):
            partitions.append({"name": name.decode("utf-16-le").rstrip("\0"),
                               "id": str(uuid.UUID(bytes_le=part_guid)), "start": start, "end": end})
    return str(uuid.UUID(bytes_le=disk_guid)), partitions, first, last, alternate


def squashfs_digest(path, partition):
    with path.open("rb") as disk:
        disk.seek(partition["start"] * SECTOR)
        header = disk.read(96)
        used, = struct.unpack_from("<Q", header, 40)
        capacity = (partition["end"] + 1 - partition["start"]) * SECTOR
        if header[:4] != b"hsqs" or not len(header) <= used <= capacity:
            raise ValueError(f"invalid SquashFS on {path.name}, partition {partition['name']}")
        digest = hashlib.sha256(header)
        remaining = used - len(header)
        while remaining:
            chunk = disk.read(min(remaining, 1 << 20))
            if not chunk:
                raise ValueError(f"short read from {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest()


MOUNT_PROBE = """
import json
from pathlib import Path

mounts = {}
for line in Path('/proc/self/mountinfo').read_text().splitlines():
    left, right = line.split(' - ', 1)
    _, _, number, _, path, options, *_ = left.split()
    if path in ('/', '/bootstate', '/models', '/mind'):
        mounts[path] = {'device': number, 'filesystem': right.split()[0], 'options': options.split(',')}
print(json.dumps(mounts))
"""


@unittest.skipUnless(TRY_IMAGE.is_file() and OLD_IMAGE.is_file(), "needs the staged install USB and old image")
class InstallerApply(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory(prefix="being-install-apply-", dir="/tmp")
        self.addCleanup(files.cleanup)
        self.files = Path(files.name)
        self.target = self.files / "target.raw"
        with self.target.open("xb") as disk:
            disk.truncate(TARGET_BYTES)
        self.old_disk = self.files / "existing.qcow2"
        overlay(OLD_IMAGE, self.old_disk)
        self.usb_disk = self.files / "usb.qcow2"
        self.installed_disk = self.files / "installed.qcow2"

    def start(self, *, try_boot=False, usb_attached=False, updates=False):
        image, disk = (TRY_IMAGE, self.usb_disk) if try_boot else (self.target, self.installed_disk)

        def configure(*args, **kwargs):
            command = machine_args(*args, **kwargs)
            controller = command.index("qemu-xhci")
            command[controller] = "qemu-xhci,id=installer-usb"
            base = command.index("ide-hd,drive=disk0,bus=ide.0")
            if try_boot:
                del command[base - 1:base + 1]
                command.extend([
                    "-device", "usb-storage,drive=disk0,bus=installer-usb.0,bootindex=1",
                    "-drive", f"if=none,id=install-target,format=raw,file={self.target}",
                    "-device", ("ide-hd,drive=install-target,bus=ide.0,bootindex=2,"
                                f"model={TARGET_MODEL},serial={TARGET_SERIAL}"),
                ])
            else:
                command[base] += f",bootindex=1,model={TARGET_MODEL},serial={TARGET_SERIAL}"
                if usb_attached:
                    command.extend([
                        "-drive", f"if=none,id=installer-media,format=qcow2,file={self.usb_disk}",
                        "-device", "usb-storage,drive=installer-media,bus=installer-usb.0,bootindex=2",
                    ])
            command.extend([
                "-drive", f"if=none,id=existing,format=qcow2,file={self.old_disk}",
                "-device", "ide-hd,drive=existing,bus=ide.1,bootindex=3,model=Existing PipOS",
            ])
            return command

        self.vm = VM(image=image, update_media=UPDATE_IMAGE if updates else None)
        self.addCleanup(self.vm.destroy)
        with patch("lab.vm.machine_args", configure):
            self.vm.start(disk)
        try:
            self.vm.login(120)
        except Timeout as error:
            self.fail(f"UEFI guest did not reach login:\n{self.vm.output()[-6000:]}\n{error}")
        self.vm.select_try_from_screen()

    def sh(self, command, timeout=30):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def poweroff(self):
        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(100), "guest did not shut down")
        self.vm.destroy()

    def bootenv(self):
        return dict(line.split("=", 1) for line in self.sh(f"grub-editenv {BOOT_ENV} list").splitlines())

    def wait_good(self, slot):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status, result = self.vm.run("cat /run/being-slot-good", 15)
            if status == 0 and result == slot:
                self.assertEqual(self.bootenv()["GOOD"], slot)
                return
            time.sleep(0.8)
        journal = self.sh("journalctl -b -u being-boot-health -u being-cortex -n 25 --no-pager -o cat")
        self.fail(f"slot {slot} was not confirmed: {result}\n{journal}")

    def wait_ready(self):
        try:
            self.vm.wait_state("idle", 150)
        except Timeout as error:
            status = self.sh("systemctl --failed --no-pager; "
                             "systemctl show -p Id,ActiveState,SubState,Result being-face being-mind being-cortex; "
                             "journalctl -b -u being-brainstem -u being-face -u being-mind -u being-cortex "
                             "-n 100 --no-pager -o cat")
            self.fail(f"the installed being did not reach idle:\n{status}\n{error}")

    def update_channel(self, bundle):
        info = self.sh(f"rauc info --keyring=/etc/rauc/keyring.pem {shlex.quote(bundle)}", 90)
        info = re.sub(r"\x1b\[[0-9;]*m", "", info)
        compatible = re.search(r"(?im)^\s*Compatible:\s+['\"]?([^'\"\s]+)", info)
        self.assertIsNotNone(compatible, info)
        return compatible[1]

    def exercise_updates(self, parts):
        self.wait_good("A")
        self.sh("mount -t ext4 -o ro,noload /dev/vda /mnt/updates")
        for artifact, loaded in ((BUNDLE, INSTALLED_BUNDLE), (HARDWARE_BUNDLE, LAB_BUNDLE)):
            with artifact.open("rb") as bundle:
                expected = hashlib.file_digest(bundle, "sha256").hexdigest()
            self.assertEqual(self.sh(f"sha256sum {loaded}", 90).split()[0], expected, loaded)
        self.assertEqual(self.update_channel(LAB_BUNDLE), "beingos-lab-v1")
        runtime = self.sh("cat /run/systemd/generator.early/being-installed/rauc.conf")
        installed_channel = self.update_channel(INSTALLED_BUNDLE)
        self.assertEqual(installed_channel, "beingos-installed-v1")
        self.assertIn(f"compatible={installed_channel}", runtime.splitlines())
        before = self.bootenv()
        slot_b = shlex.quote(f"/dev/disk/by-partuuid/{parts['system-b']}")
        sha_b = self.sh(f"sha256sum {slot_b}", 100)
        for fixture in ("lab.raucb", "unsigned.raucb"):
            status, output = self.vm.run(f"rauc install /mnt/updates/{fixture}", 90)
            self.assertNotEqual(status, 0, output)
            if fixture == "lab.raucb":
                self.assertIn("compatib", output.lower())
            self.assertEqual(self.bootenv(), before, output)
        self.assertEqual(self.sh(f"sha256sum {slot_b}", 100), sha_b)
        self.sh(f"rauc install {INSTALLED_BUNDLE}", 240)
        env = self.bootenv()
        self.assertEqual((env["ORDER"], env["GOOD"], env["B_OK"]), ("B A", "A", "1"))
        print("  rejected the trusted lab channel and unsigned media, then staged the signed installed update", flush=True)

    def check_runtime(self):
        program = (
            "import binascii, hashlib, sqlite3, ssl, mind.__main__, mind.installer, mind.install_apply; "
            "assert binascii.crc32(b'abc') == 891568578; "
            "assert hashlib.pbkdf2_hmac('sha256', b'password', b'salt', 1).hex() == "
            "'120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b'; "
            "context = ssl.create_default_context(); "
            "assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname; "
            "assert ssl.get_default_verify_paths().capath == '/etc/ssl/certs'; "
            "db = sqlite3.connect(':memory:'); "
            "assert db.execute('select 6 * 7').fetchone() == (42,); "
            "db.close()"
        )
        self.sh("PYTHONPATH=/usr/lib/being python3 -c " + shlex.quote(program))
        # A hashed CA directory may populate the context on first verification.
        anchor = "/usr/share/ca-certificates/mozilla/ISRG_Root_X1.crt"
        output = self.sh("openssl verify -check_ss_sig " + shlex.quote(anchor))
        self.assertEqual(output, anchor + ": OK")

    def target_in_guest(self, *, verify_source_refusal=False):
        self.check_runtime()
        report = json.loads(self.sh("being-installer --list --json"))
        self.assertEqual(report["boot_disk"], report["model_disk"], report)
        disks = {row["device"]: row for row in report["disks"]}
        self.assertEqual(disks[report["boot_disk"]]["transport"], "usb", report)
        matched = [row for row in report["disks"] if row.get("serial") == TARGET_SERIAL]
        self.assertEqual(len(matched), 1, report)
        target = matched[0]
        self.assertEqual((target["model"], target["size_bytes"], target["transport"]),
                         (TARGET_MODEL, TARGET_BYTES, "sata"), target)
        self.assertTrue(target["eligible"], target)
        self.assertRegex(target["device"], r"\A/dev/sd[a-z]+\Z")
        self.assertEqual(sum(row["model"] == "Existing PipOS" for row in report["disks"]), 1, report)
        if verify_source_refusal:
            status, output = self.vm.run(f"being-installer --install {shlex.quote(report['boot_disk'])}", 30)
            self.assertEqual(status, 1, output)
            self.assertIn("running PipOS disk", output)
        return target["device"]

    def begin_install(self, target, confirmation):
        self.vm.send(f'being-installer --install {shlex.quote(target)}; echo "<<""apply-end:$?>>"\n')
        self.await_install_text(f"Type ERASE {target} to install: ", 30)
        self.vm.send(confirmation + "\n")

    def await_install_text(self, text, timeout):
        match = self.vm.expect(re.escape(text) + r"|<<apply-end:(\d+)>>", timeout)
        if match[1] is not None:
            self.fail(f"installer exited with status {match[1]} before {text!r}\n{self.vm.output()[-4000:]}")

    def install_result(self, status, timeout=360):
        match = self.vm.expect(r"(?s)(.*?)<<apply-end:(\d+)>>", timeout)
        result = match[1].replace("\r", "").strip()
        self.assertEqual(int(match[2]), status, result)
        return result

    def unchanged_media(self):
        for disk, base in ((self.usb_disk, TRY_IMAGE), (self.old_disk, OLD_IMAGE)):
            check = subprocess.run(["qemu-img", "compare", "-f", "qcow2", "-F", "raw", str(disk), str(base)],
                                   capture_output=True, text=True, timeout=90)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def layout(self):
        with self.target.open("rb") as disk:
            primary = read_gpt(disk, 1)
            backup = read_gpt(disk, TARGET_BYTES // SECTOR - 1)
        self.assertEqual(primary[:-1], backup[:-1])
        self.assertEqual((primary[-1], backup[-1]), (TARGET_BYTES // SECTOR - 1, 1))
        disk_id, partitions, first, last, _ = primary
        self.assertEqual([part["name"] for part in partitions], list(PARTITIONS))
        ids = {disk_id, *(part["id"] for part in partitions)}
        self.assertEqual(len(ids), len(PARTITIONS) + 1)
        for image in (TRY_IMAGE, OLD_IMAGE):
            with image.open("rb") as disk:
                source_id, source_parts, *_ = read_gpt(disk, 1)
            self.assertTrue(ids.isdisjoint({source_id, *(part["id"] for part in source_parts)}))
        previous = first - 1
        for part in partitions:
            self.assertTrue(previous < part["start"] <= part["end"] <= last, part)
            self.assertEqual(part["start"] % ((1 << 20) // SECTOR), 0)
            previous = part["end"]
        with TRY_IMAGE.open("rb") as disk:
            source_root = read_gpt(disk, 1)[1][1]
        copied = [squashfs_digest(self.target, part) for part in partitions[1:3]]
        self.assertEqual(copied, [squashfs_digest(TRY_IMAGE, source_root)] * 2)
        return {part["name"]: part["id"] for part in partitions}

    def assert_binding(self, parts, active):
        identifiers = {"being." + name.replace("system-", "slot_"): value for name, value in parts.items()}
        root = "system-" + active.lower()
        cmdline = self.sh("cat /proc/cmdline").split()
        required = {"being.installed=1", "fstab=no", "systemd.gpt_auto=no", f"rauc.slot={active}",
                    f"root=PARTUUID={parts[root]}", *(f"{key}={value}" for key, value in identifiers.items())}
        self.assertTrue(required <= set(cmdline), (required, cmdline))
        self.assertNotIn("being.try=1", cmdline)
        self.sh("systemctl is-active being-installed-bind.service || "
                "{ journalctl -b -u being-installed-bind.service -n 30 --no-pager -o cat; exit 1; }")
        self.assertEqual(self.sh("systemctl show -p Result --value being-installed-bind.service"), "success")

        mounts = json.loads(self.sh("python3 -c " + shlex.quote(MOUNT_PROBE)))
        inventory = json.loads(self.sh("being-installer --list --json"))
        target = inventory["boot_disk"]
        self.assertEqual(target, inventory["model_disk"], inventory)
        self.assertIsInstance(target, str, inventory)
        expected = (("/", root, "squashfs", "ro"), ("/bootstate", "esp", "vfat", "rw"),
                    ("/models", "models", "ext4", "ro"), ("/mind", "mind", "ext4", "rw"))
        for mount, part, fs, mode in expected:
            device = shlex.quote(f"/dev/disk/by-partuuid/{parts[part]}")
            path = self.sh(f"readlink -f {device}")
            self.assertRegex(path, rf"\A{re.escape(target)}p?[1-5]\Z")
            number = self.sh(f"cat /sys/class/block/{shlex.quote(Path(path).name)}/dev")
            self.assertEqual((mounts[mount]["device"], mounts[mount]["filesystem"]), (number, fs))
            self.assertIn(mode, mounts[mount]["options"])

        conf = self.sh("cat /run/systemd/generator.early/being-installed/rauc.conf")
        for part in ("system-a", "system-b"):
            self.assertIn(f"device=/dev/disk/by-partuuid/{parts[part]}", conf)
        self.sh("systemctl is-active rauc.service")
        status = re.sub(r"\x1b\[[0-9;]*m", "", self.sh("rauc status"))
        self.assertIn(f"Booted from: rootfs.{0 if active == 'A' else 1} ({active})", status)
        for part in ("system-a", "system-b"):
            self.assertIn(f"/dev/disk/by-partuuid/{parts[part]}", status)
        self.sh("test -s /models/reflex.gguf")

    def written_target_bytes(self):
        for row in self.vm.qmp("query-blockstats"):
            if row.get("device") == "install-target":
                return row["stats"]["wr_bytes"]
        self.fail("QEMU did not report block statistics for the disposable target")

    def test_power_cut_during_install_leaves_no_published_partition_table(self):
        self.start(try_boot=True)
        target = self.target_in_guest()
        self.vm.qmp("block_set_io_throttle", device="install-target", bps=0, bps_rd=0, bps_wr=4 << 20,
                    iops=0, iops_rd=0, iops_wr=0)
        before = self.written_target_bytes()
        self.begin_install(target, f"ERASE {target}")
        self.await_install_text("Writing installation data.", 175)
        deadline = time.monotonic() + 180
        # Two MiB alone would be the old GPT invalidation, before copying began.
        while self.written_target_bytes() - before < 4 << 20:
            if time.monotonic() >= deadline:
                self.fail("installer never reached payload writes on the disposable target")
            time.sleep(0.1)
        self.vm.power_cut()
        self.vm.destroy()
        with self.target.open("rb") as disk:
            head = disk.read(2 * SECTOR)
            disk.seek(-SECTOR, os.SEEK_END)
            tail = disk.read(SECTOR)
        self.assertNotEqual(head[510:512], b"\x55\xaa", "interrupted install exposed a boot MBR")
        self.assertNotEqual(head[SECTOR:SECTOR + 8], b"EFI PART", "interrupted install exposed primary GPT")
        self.assertNotEqual(tail[:8], b"EFI PART", "interrupted install exposed backup GPT")
        self.unchanged_media()
        print("  power cut during payload writes left the target unpublished and both other disks unchanged", flush=True)

    @unittest.skipUnless(all(path.is_file() for path in (UPDATE_IMAGE, BUNDLE, HARDWARE_BUNDLE)),
                         "needs the staged update fixture disk and both signed bundle files")
    def test_installs_both_slots_then_keeps_identity_and_memory_after_usb_removal(self):
        self.start(try_boot=True)
        target = self.target_in_guest(verify_source_refusal=True)
        manifest = json.loads(self.sh("cat /models/manifest.json"))
        blank = self.target.stat()
        self.begin_install(target, f"erase {target}")
        self.install_result(1, timeout=30)
        now = self.target.stat()
        self.assertEqual((now.st_mtime_ns, now.st_blocks), (blank.st_mtime_ns, blank.st_blocks))
        self.begin_install(target, f"ERASE {target}")
        self.assertIn("Installation complete.", self.install_result(0))
        self.poweroff()
        parts = self.layout()
        self.unchanged_media()
        print("  confirmed install wrote matching SquashFS slots with fresh GPT identities", flush=True)

        self.start(updates=True)
        self.assert_binding(parts, "A")
        self.check_runtime()
        self.wait_ready()
        self.assertEqual(json.loads(self.sh("cat /models/manifest.json")), manifest)
        self.assertEqual(self.sh("sha256sum /models/reflex.gguf", 90).split()[0], manifest["reflex"]["sha256"])
        self.sh("python3 -c 'from urllib.request import urlopen; urlopen(\"http://127.0.0.1:8080/health\", timeout=5)'")
        self.assertEqual(self.vm.converse("What is your name?", 30), "My name is Pip.")
        self.assertEqual(self.vm.converse(f"Remember that {FACT}", 30), "Okay. I'll remember that.")
        self.assertIn(FACT, self.vm.converse(RECALL, 30))
        life = json.loads(self.sh("cat /mind/life.json"))["id"]
        self.exercise_updates(parts)
        self.poweroff()

        self.start()
        self.assert_binding(parts, "B")
        self.wait_ready()
        self.assertEqual(json.loads(self.sh("cat /mind/life.json"))["id"], life)
        self.assertIn(FACT, self.vm.converse(RECALL, 30))
        self.wait_good("B")
        self.poweroff()
        print("  the installed disk booted both slots without USB and kept Pip's identity and memory", flush=True)

        self.start(usb_attached=True)
        self.assert_binding(parts, "B")
        self.poweroff()
        self.unchanged_media()
        print("  the confirmed installed slot still bound its own devices when USB was reattached", flush=True)


if __name__ == "__main__":
    unittest.main()
