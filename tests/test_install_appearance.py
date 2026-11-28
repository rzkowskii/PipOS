import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import local_menu, look


class TrialAppearance(unittest.TestCase):
    def test_only_explicitly_kept_trial_appearance_is_seeded(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            trial = look.Store(source)
            with mock.patch.object(local_menu, "MIND_DIR", Path(source)), mock.patch.object(
                local_menu, "temporary_mind_mounted", return_value=True
            ):
                self.assertFalse(local_menu.try_settings_present())
                trial.set("glow", "amber")
                selected = trial.set("mouth", True)
                self.assertTrue(local_menu.try_settings_present())
                with mock.patch("builtins.input", return_value=""):
                    self.assertIsNone(local_menu.appearance_to_keep())
                with mock.patch("builtins.input", return_value="yes"):
                    self.assertEqual(local_menu.appearance_to_keep(), selected)
            look.seed(target, selected)
            installed = look.Store(target).read()
            self.assertEqual(installed.glow, "amber")
            self.assertTrue(installed.mouth)
            self.assertFalse(installed.brows)
            self.assertEqual(installed.revision, 0)

    def test_invalid_appearance_is_rejected_before_staging_a_file(self):
        with tempfile.TemporaryDirectory() as target:
            with self.assertRaisesRegex(ValueError, "Invalid installed appearance"):
                look.seed(target, look.Look(glow="sepia"))
            self.assertFalse((Path(target) / look.FILE).exists())
