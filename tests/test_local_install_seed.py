import tempfile
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from mind import install_apply
from mind.initiative import InitiativePolicy, QuietHours, load_policy
from mind.profile import FACTORY_CONFIG, Profile, load_profile


class StagedMind(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.parts = [{"name": "mind", "size_bytes": 1024 * 1024}]

    def test_only_the_chosen_profile_populates_a_fresh_mind(self):
        profile = Profile(name="Fern", owner="Richard", voice="awb")
        with patch.object(install_apply, "run_tool") as tool:
            install_apply.create_images(self.directory, self.parts, {"mind": uuid.uuid4()}, 512, profile=profile)
        args = tool.call_args_list[0].args
        seed = args[args.index("-d") + 1]
        self.assertEqual({entry.name for entry in seed.iterdir()}, {"profile.json"})
        self.assertEqual(load_profile(FACTORY_CONFIG, seed), profile)

    def test_only_the_chosen_initiative_policy_can_seed_a_fresh_mind(self):
        policy = InitiativePolicy(reminders=False, greetings=True, quiet_hours=QuietHours("22:00", "07:00"))
        with patch.object(install_apply, "run_tool") as tool:
            install_apply.create_images(self.directory, self.parts, {"mind": uuid.uuid4()}, 512, initiative=policy)
        args = tool.call_args_list[0].args
        seed = args[args.index("-d") + 1]
        self.assertEqual({entry.name for entry in seed.iterdir()}, {"initiative.json"})
        self.assertEqual(load_policy(seed), policy)

    def test_failed_restore_does_not_open_the_confirmed_target(self):
        part_size = 2048 * 512
        parts = [{"name": name, "size_bytes": part_size} for name in install_apply.PARTITIONS]
        plan = {"partitions": parts}
        restore = Mock(side_effect=ValueError("Bad deletion proof"))
        with (patch.object(install_apply.sys, "platform", "linux"),
              patch.object(install_apply.os, "geteuid", return_value=0),
              patch.object(install_apply, "is_try_boot", return_value=True),
              patch.object(install_apply, "geometry", return_value=(512, 8 << 30)),
              patch.object(install_apply, "gpt_records"),
              patch.object(install_apply, "source_device", return_value=nullcontext(3)),
              patch.object(install_apply, "squashfs_size", return_value=4096),
              patch.object(install_apply, "model_size", return_value=1),
              patch.object(install_apply, "staging", return_value=nullcontext(self.directory)),
              patch.object(install_apply, "boot_files", side_effect=self.boot_stub),
              patch.object(install_apply, "run_tool", side_effect=self.fake_tool),
              patch.object(install_apply, "claim_target") as claim):
            with self.assertRaisesRegex(ValueError, "Bad deletion proof"):
                install_apply.apply_installation(plan, Mock(), progress=Mock(), restore=restore)
        restore.assert_called_once_with(self.directory / "restore-mind")
        claim.assert_not_called()

    @staticmethod
    def boot_stub(directory, _ids, **_options):
        boot = directory / "EFI" / "BOOT"
        boot.mkdir(parents=True)
        for name in ("bootx64.efi", "grub.cfg", "grubenv"):
            (boot / name).write_bytes(b"fixture")

    @staticmethod
    def fake_tool(*args):
        if args[0] == "mcopy" and str(args[3]).startswith("::/EFI/BOOT/"):
            args[-1].write_bytes(b"fixture")
