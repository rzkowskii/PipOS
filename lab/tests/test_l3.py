"""L3 appliance tests. Each case uses its own disposable QEMU overlay."""

import json
import os
import random
import shlex
import time
import unittest
from pathlib import Path

from lab.vm import IMAGE, ROOT, VM

UPDATE_MEDIA = Path(os.environ.get("BEING_UPDATE_FIXTURES", ROOT / "out/images/beingos-update-fixtures.img")).resolve()
BOOT_ENV = "/bootstate/EFI/BOOT/grubenv"
BOOT = 120
MARKER = "an appliance remembers its own history"


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class Appliance(unittest.TestCase):
    def open_vm(self, **kwargs):
        self.vm = VM(**kwargs)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(BOOT)

    def sh(self, command, timeout=60):
        status, result = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{result}")
        return result

    def python(self, code, timeout=60):
        return self.sh("PYTHONPATH=/usr/lib/being python3 -c " + shlex.quote(code), timeout)

    def poll(self, command, check, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, result = self.vm.run(command, 15)
            if status == 0 and check(result):
                return result
            time.sleep(1)
        self.fail(f"timed out waiting for {command}: {result}")

    def life(self):
        record = self.poll(
            'test -f /mind/life.json && grep -F "$(cat /proc/sys/kernel/random/boot_id)" /mind/life.json',
            bool,
            20,
        )
        return json.loads(record)

    def bootenv(self):
        return dict(line.split("=", 1) for line in self.sh(f"grub-editenv {BOOT_ENV} list").splitlines())

    def ready(self, slot):
        self.poll("cat /run/being-slot-good", lambda result: result == slot, 150)
        self.assertEqual(self.bootenv()["GOOD"], slot)

    def slot(self, slot):
        self.assertIn(f"rauc.slot={slot}", self.sh("cat /proc/cmdline").split())
        self.sh("mount -o remount,rw / 2>/dev/null || true")
        filesystem, options = self.sh("awk '$2 == \"/\" {print $3, $4}' /proc/mounts").split()
        self.assertEqual(filesystem, "squashfs")
        self.assertIn("ro", options.split(","))
        self.sh("! touch /etc/write-probe")

    def mount_updates(self):
        self.sh("mount -t ext4 -o ro,noload /dev/vda /mnt/updates")

    def remember_marker(self):
        self.python(f"from mind.memory import Memory; Memory('/mind').remember({MARKER!r})")

    def check_marker(self):
        self.assertEqual(
            self.python("from mind.memory import Memory; print(Memory('/mind').facts()[0].text)"),
            MARKER,
        )

    @unittest.skipUnless(UPDATE_MEDIA.exists(), "run lab/build.sh test-media first")
    def test_signed_update_rolls_back_then_confirms_a_healthy_slot(self):
        self.open_vm(update_media=UPDATE_MEDIA)
        self.slot("A")
        born = self.life()
        self.remember_marker()
        self.mount_updates()
        self.ready("A")

        before = self.bootenv()
        status, result = self.vm.run("rauc install /mnt/updates/unsigned.raucb")
        self.assertNotEqual(status, 0, result)
        self.assertEqual(self.bootenv(), before)

        self.sh("rauc install /mnt/updates/bad.raucb", 240)
        env = self.bootenv()
        self.assertEqual((env["ORDER"], env["GOOD"]), ("B A", "A"))
        self.vm.send("reboot\n")
        for _ in range(3):
            self.vm.expect(r"PipOS slot B", BOOT)
        self.vm.expect(r"PipOS slot A", BOOT)
        self.vm.login(BOOT)
        self.slot("A")
        env = self.bootenv()
        self.assertEqual((env["GOOD"], env["B_TRY"], env["B_LEFT"]), ("A", "1", "0"))
        self.assertEqual(self.life()["id"], born["id"])
        self.check_marker()

        self.mount_updates()
        self.ready("A")
        self.sh("rauc install /mnt/updates/good.raucb", 240)
        self.vm.send("reboot\n")
        self.vm.expect(r"PipOS slot B", BOOT)
        self.vm.login(BOOT)
        self.slot("B")
        self.assertEqual(self.bootenv()["GOOD"], "A")
        self.sh("test ! -e /run/being-slot-good")
        self.mount_updates()
        status, result = self.vm.run("rauc install /mnt/updates/good.raucb", 30)
        self.assertNotEqual(status, 0, result)
        self.assertEqual(self.bootenv()["GOOD"], "A")
        self.ready("B")
        if os.environ.get("BEING_FACTORY_IMAGE"):
            # The good fixture is re-signed with its own version. Compare
            # the exact filesystem payload written by RAUC with factory A.
            self.assertEqual(self.python("""
import hashlib, struct
def digest(slot):
    with open('/dev/disk/by-partlabel/system-' + slot, 'rb') as disk:
        header = disk.read(96)
        used, = struct.unpack_from('<Q', header, 40)
        assert header[:4] == b'hsqs' and 96 <= used <= disk.seek(0, 2)
        disk.seek(0)
        result = hashlib.sha256()
        while used:
            block = disk.read(min(used, 1024 * 1024))
            if not block:
                raise ValueError('truncated SquashFS')
            result.update(block)
            used -= len(block)
        return result.hexdigest()
print(digest('a') == digest('b'))
""", timeout=120), "True", "healthy update fixture does not contain the candidate root filesystem")
        self.assertEqual(self.bootenv()["B_TRY"], "0")
        self.assertEqual(self.life()["id"], born["id"])
        self.check_marker()

    def test_brainstem_watchdog_restarts_a_stalled_main_loop(self):
        self.open_vm()
        before = self.sh("systemctl show -p MainPID --value being-brainstem")
        self.assertGreater(int(before), 1)
        self.sh(f"kill -STOP {before}")
        self.poll(
            "systemctl show -p MainPID --value being-brainstem",
            lambda result: result not in ("0", before),
            35,
        )
        self.assertIn(
            "watchdog",
            self.sh("journalctl -b -u being-brainstem --no-pager -o cat").lower(),
        )

    @unittest.skipUnless(UPDATE_MEDIA.exists(), "run lab/build.sh test-media first")
    def test_cutting_power_during_an_update_preserves_the_confirmed_slot(self):
        self.open_vm(update_media=UPDATE_MEDIA)
        born = self.life()
        self.remember_marker()
        self.mount_updates()
        self.ready("A")

        prefix = "head -c 65536 /dev/disk/by-partlabel/system-b | sha256sum"
        before = self.sh(prefix)
        # Make the write window reproducible even on a fast host disk.
        self.vm.qmp(
            "block_set_io_throttle", device="disk0", bps=0, bps_rd=0, bps_wr=2 << 20,
            iops=0, iops_rd=0, iops_wr=0,
        )
        self.sh("(rauc install /mnt/updates/bad.raucb > /run/interrupted-install.log 2>&1 &)")
        self.poll(prefix, lambda result: result != before, 45)
        self.assertEqual(self.bootenv()["B_OK"], "0")
        self.vm.power_cut()
        self.vm.start()
        self.vm.login(BOOT)
        self.slot("A")
        env = self.bootenv()
        self.assertEqual((env["GOOD"], env["B_OK"]), ("A", "0"))
        self.assertEqual(self.life()["id"], born["id"])
        self.check_marker()

    def test_hardware_watchdog_expiry_resets_the_vm(self):
        self.open_vm()
        born = self.life()
        device = self.sh("ls -l /proc/1/fd | awk '$NF ~ /^\\/dev\\/watchdog/ {print $NF}'")
        self.assertRegex(device, r"^/dev/watchdog\d*$")
        # Hand the watchdog from PID 1 to a fixture that arms it and stops
        # feeding it. QEMU must report the hardware reset.
        self.vm.put("/run/trip-watchdog.py", f"""
import array, fcntl, os, time
fd = os.open({device!r}, os.O_WRONLY)
fcntl.ioctl(fd, 0xc0045706, array.array("i", [6]), True)
print("WATCHDOG ARMED", flush=True)
time.sleep(90)
""")
        self.sh(
            "mkdir -p /run/systemd/system.conf.d && "
            "printf '[Manager]\\nRuntimeWatchdogSec=0\\n' > /run/systemd/system.conf.d/99-lab.conf && "
            "systemctl daemon-reexec",
        )
        self.poll("ls -l /proc/1/fd", lambda result: device not in result, 20)
        self.vm.send("python3 /run/trip-watchdog.py\n")
        self.vm.expect("WATCHDOG ARMED", 25)
        event = self.vm.wait_qmp_event("WATCHDOG", 40)
        self.assertEqual(event["data"]["action"], "reset")
        self.vm.login(BOOT)
        rebooted = self.life()
        self.assertEqual(rebooted["id"], born["id"])
        self.assertTrue(rebooted["this_wake"]["lost_power"])

    def test_twenty_seeded_power_cuts_preserve_committed_memories(self):
        self.open_vm()
        born = self.life()["id"]
        rng = random.Random(2012)
        phases = ["boot"] * 4 + ["commit"] * 8 + ["transaction"] * 8
        rng.shuffle(phases)
        committed = 0
        for i, phase in enumerate(phases, 1):
            if phase != "boot":
                committed += 1
                code = f"""
import time
from mind.memory import Memory
memory = Memory("/mind")
memory.remember("durable-{i}")
print("COMMITTED", flush=True)
if {phase == 'transaction'}:
    memory.db.execute("PRAGMA cache_size=8")
    memory.db.execute("BEGIN")
    memory.db.execute("INSERT INTO facts(ts, text) VALUES (1, ?)", ("pending-{i}-" + "x" * 98304,))
    print("PENDING", flush=True)
time.sleep(30)
"""
                self.vm.send("PYTHONPATH=/usr/lib/being python3 -c " + shlex.quote(code) + "\n")
                self.vm.expect("PENDING" if phase == "transaction" else "COMMITTED", 30)
            else:
                # Cut the next boot between the bootloader and login.
                self.vm.send("reboot\n")
                self.vm.expect("PipOS slot A", BOOT)
            time.sleep(rng.uniform(0.05, 0.35))
            self.vm.power_cut()
            self.vm.start()
            self.vm.login(BOOT)
            record = self.life()
            self.assertEqual(record["id"], born, f"cut {i}")
            if phase != "boot":
                self.assertTrue(record["this_wake"]["lost_power"], f"cut {i}")
            facts = self.python(
                "import json; from mind.memory import Memory; "
                "m = Memory('/mind'); print(json.dumps([f.text for f in m.facts()])); "
                "print(m.db.execute('PRAGMA integrity_check').fetchone()[0])"
            ).splitlines()
            expected = [f"durable-{j}" for j in range(1, i + 1) if phases[j - 1] != "boot"]
            self.assertEqual((json.loads(facts[0]), facts[1]), (expected, "ok"))
            self.assertEqual(self.bootenv()["GOOD"], "A")
            print(f"  cut {i}/20 during {phase}, {committed} committed facts retained", flush=True)
        self.slot("A")


if __name__ == "__main__":
    unittest.main()
