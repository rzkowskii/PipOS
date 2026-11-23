"""Boot the Try USB twice with an existing PipOS disk still attached."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.vm import IMAGE, ROOT, VM, machine_args

TRY_IMAGE = Path(os.environ.get("BEING_TRY_IMAGE", ROOT / "out/images/beingos-try.img")).resolve()
EXISTING_IMAGE = Path(os.environ.get("BEING_TRY_EXISTING_IMAGE", IMAGE)).resolve()
EXISTING_MODEL = "BEINGOS EXISTING"
PROFILE_NAME = "Preview Fred"
FACT = "my atlas is called Opal Loom"
RECALL = "What do you remember about my atlas?"


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as disk:
        while data := disk.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


@unittest.skipUnless(TRY_IMAGE.exists() and EXISTING_IMAGE.exists(), "needs a Try USB image and an existing PipOS image")
class TryUSB(unittest.TestCase):
    def sh(self, command, timeout=30):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def start(self, existing):
        def usb_boot_with_sata_disk(*args, **kwargs):
            command = machine_args(*args, **kwargs)
            boot_device = command.index("ide-hd,drive=disk0,bus=ide.0")
            del command[boot_device - 1:boot_device + 1]
            controller = command.index("qemu-xhci")
            command[controller] = "qemu-xhci,id=usb-controller"
            return command + [
                "-device", "usb-storage,drive=disk0,bus=usb-controller.0,bootindex=1",
                "-drive", f"if=none,id=existing,format=raw,file={existing}",
                "-device", f"ide-hd,drive=existing,bus=ide.0,bootindex=2,model={EXISTING_MODEL}",
            ]

        with patch("lab.vm.machine_args", usb_boot_with_sata_disk):
            self.vm.start()
        self.vm.login(120)
        self.vm.select_try_from_screen()

    def stop(self):
        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(90), "Try mode did not power off cleanly")
        self.vm.stop()

    def profile(self):
        return json.loads(self.sh("being-setup --show"))

    def preview_name(self, previous):
        self.vm.voice._cursor = self.vm.voice.mark()
        self.vm.send('being-setup; echo "<<try-setup:$?>>"\n')
        self.vm.expect(r"Try PipOS keeps these choices until you reboot\.", 30)
        self.vm.expect(r"Choice: ", 45)
        self.vm.send("1\n")
        self.vm.expect(rf"Name \[{re.escape(previous)}\]: ", 30)
        self.vm.send(f"{PROFILE_NAME}\n")
        self.vm.expect(r"Choice: ", 30)
        self.vm.send("k\n")
        self.vm.expect(r"Save these choices\? \[y/N\] ", 30)
        self.vm.send("y\n")
        self.vm.expect(r"Profile applied for this Try session\.", 55)
        self.vm.expect(r"<<try-setup:0>>", 15)
        self.vm.voice.expect(r"Type to talk to it\.", 45)
        self.vm.voice.expect(r"\((?:attending|idle|drowsy|asleep)\)", 45)

    def check_try_storage(self):
        arguments = self.sh("cat /proc/cmdline").split()
        self.assertTrue({"being.try=1", "fstab=no", "systemd.gpt_auto=no"} <= set(arguments), arguments)
        self.assertTrue(any(arg.startswith("being.models=") for arg in arguments), arguments)
        mounts = {}
        for line in self.sh("cat /proc/mounts").splitlines():
            source, path, filesystem, options, *_ = line.split()
            mounts[path] = (source, filesystem, set(options.split(",")))
        for path in ("/mind", "/var"):
            self.assertEqual(mounts[path][1], "tmpfs", mounts[path])
            self.assertIn("noswap", mounts[path][2])
        self.assertNotIn("/bootstate", mounts)
        models = mounts["/models"]
        self.assertEqual(models[1], "ext4", models)
        self.assertIn("ro", models[2])
        self.assertTrue({"noload", "norecovery"} & models[2], models)
        self.assertEqual(self.sh("systemctl show -p LoadState --value rauc.service"), "masked")

        inventory = json.loads(self.sh("being-installer --list --json"))
        disks = {disk["device"]: disk for disk in inventory["disks"]}
        boot = inventory["boot_disk"]
        self.assertEqual(inventory["model_disk"], boot, inventory)
        self.assertEqual(disks[boot]["transport"], "usb", inventory)
        self.assertIn("boot_disk", disks[boot]["reasons"], inventory)
        attached = [disk for device, disk in disks.items() if device != boot and disk.get("transport")]
        self.assertEqual(len(attached), 1, inventory)
        existing = attached[0]
        self.assertEqual((existing["transport"], existing["model"]), ("sata", EXISTING_MODEL))
        self.assertNotIn("mounted", existing["reasons"], existing)
        self.assertNotIn("read_only", existing["reasons"], existing)
        return existing["device"]

    def test_usb_trial_resets_profile_and_memory_and_leaves_attached_disk_unchanged(self):
        files = tempfile.TemporaryDirectory(prefix="being-try-lab-", dir="/tmp")
        self.addCleanup(files.cleanup)
        existing = Path(files.name) / "existing.raw"
        shutil.copyfile(EXISTING_IMAGE, existing)
        before = checksum(existing)
        self.vm = VM(image=TRY_IMAGE)
        self.addCleanup(self.vm.destroy)

        self.start(existing)
        existing_disk = self.check_try_storage()
        self.assertRegex(existing_disk, r"\A/dev/sd[a-z]+\Z")
        self.sh("udevadm settle --timeout=15")
        for partition, label in ((4, "models"), (5, "mind")):
            props = self.sh(f"udevadm info --query=property --name={existing_disk}{partition}")
            self.assertIn(f"ID_FS_LABEL={label}", props.splitlines())

        self.vm.wait_state("idle", 150)
        factory = self.profile()
        self.assertEqual(self.vm.converse(f"Remember that {FACT}", 30), "Okay. I'll remember that.")
        self.preview_name(factory["name"])
        self.assertEqual(self.profile()["name"], PROFILE_NAME)
        self.assertEqual(self.vm.converse("What is your name?", 30), f"My name is {PROFILE_NAME}.")
        self.assertIn(FACT, self.vm.converse(RECALL, 30))
        print("\n  Try USB used temporary memory and left the attached PipOS labels unmounted", flush=True)
        self.stop()

        self.start(existing)
        self.check_try_storage()
        self.vm.wait_state("idle", 150)
        self.assertEqual(self.profile(), factory)
        self.assertEqual(self.vm.converse(RECALL, 30), "I don't remember anything about that.")
        self.stop()
        self.assertEqual(checksum(existing), before, "Try mode changed the attached disk's bytes")
        print("  second boot reset the trial profile and memory and preserved every attached disk byte", flush=True)


if __name__ == "__main__":
    unittest.main()
