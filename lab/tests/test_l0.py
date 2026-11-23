"""L0: the lab works.

The image boots in the virtual laptop through UEFI and GRUB, the machine it
finds itself in has the laptop's kind of CPU, and it shuts down cleanly.
"""

import time
import unittest

from lab.vm import IMAGE, VM

# Emulated x86 is slow; these are patience limits, not targets.
BOOT_PATIENCE = 300


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class L0(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image_before = (IMAGE.stat().st_size, IMAGE.stat().st_mtime_ns)
        cls.vm = VM().start()
        try:
            cls.vm.login(BOOT_PATIENCE)
        except Exception:
            cls.vm.destroy()
            raise
        cls.boot_seconds = time.monotonic() - cls.vm.started
        print(f"\n  power-on to login prompt: {cls.boot_seconds:.1f}s (emulated)")

    @classmethod
    def tearDownClass(cls):
        cls.vm.destroy()

    def sh(self, command, timeout=60):
        status, out = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"`{command}` failed:\n{out}")
        return out

    def test_it_is_the_right_kind_of_machine(self):
        self.assertEqual(self.sh("uname -m"), "x86_64")
        self.assertEqual(self.sh("nproc"), "4")
        self.assertEqual(self.sh("hostname"), "being")

    def test_cpu_has_avx_but_not_avx2_like_the_laptop(self):
        flags = self.sh("grep -m1 '^flags' /proc/cpuinfo").split()
        self.assertIn("avx", flags)
        self.assertIn("f16c", flags)
        for missing in ("avx2", "fma", "bmi2"):
            self.assertNotIn(missing, flags, f"the virtual CPU has {missing}, the laptop does not")

    def test_kernel_is_the_long_term_one(self):
        self.assertRegex(self.sh("uname -r"), r"^6\.18\.")

    def test_booted_through_uefi_from_the_sata_disk(self):
        self.sh("test -d /sys/firmware/efi")
        disks = self.sh("ls /sys/block").split()
        self.assertIn("sda", disks)
        path = self.sh("readlink -f /sys/block/sda")
        self.assertIn("/ata", path, "the disk should be SATA, as on the laptop")
        self.assertIn("root=PARTUUID=", self.sh("cat /proc/cmdline"))

    def test_every_service_started(self):
        status, state = self.vm.run("systemctl is-system-running --wait", 180)
        failed = self.vm.run("systemctl --failed --no-legend --plain")[1]
        self.assertEqual(state, "running", f"failed units:\n{failed}")

    def test_the_built_image_is_never_modified(self):
        self.sh("! touch /root/scribble && echo scribble > /mind/scribble && sync")
        self.assertEqual((IMAGE.stat().st_size, IMAGE.stat().st_mtime_ns), self.image_before)


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class PowerOff(unittest.TestCase):
    def test_it_powers_off_when_told(self):
        with VM() as vm:
            vm.login(BOOT_PATIENCE)
            vm.send("poweroff\n")
            self.assertTrue(vm.wait_off(120), "it did not power off")

    def test_the_power_button_shuts_it_down(self):
        with VM() as vm:
            vm.login(BOOT_PATIENCE)
            vm.power_button()
            self.assertTrue(vm.wait_off(120), "the power button did nothing")


if __name__ == "__main__":
    unittest.main()
