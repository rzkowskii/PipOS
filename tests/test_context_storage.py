"""Boot-local context routing with fscrypt standing in for the Linux filesystem."""

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import context_storage as storage


class WorkingContext(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mind = self.root / "mind"
        self.mind.mkdir()
        self.runtime = self.root / "run"
        self.boot = self.root / "boot-id"
        self.boot.write_text("01234567-1111-2222-3333-444444444444\n")
        self.enterContext(patch.object(storage, "BOOT_ID", self.boot))

    def test_try_uses_ram_and_an_in_boot_restart_keeps_both_paths(self):
        with patch.object(storage, "installed_ready", return_value=False):
            storage.prepare(self.mind, self.runtime)
        selection = self.runtime / storage.PATHS_FILE
        original = selection.read_text()
        self.assertEqual(storage.read_mode(self.runtime, self.mind), "ram")
        (self.runtime / "text/context.json").write_text("same boot")
        with patch.object(storage, "encrypted_files") as encrypted:
            storage.prepare(self.mind, self.runtime)
        encrypted.assert_not_called()
        self.assertEqual(selection.read_text(), original)
        self.assertEqual((self.runtime / "text/context.json").read_text(), "same boot")
        self.assertEqual(list(self.mind.iterdir()), [])

    def test_filesystem_without_fscrypt_never_publishes_disk_working_paths(self):
        with (patch.object(storage, "installed_ready", return_value=True),
              patch.object(storage.fscrypt, "policy_key", side_effect=OSError(errno.EOPNOTSUPP, "fscrypt"))):
            storage.prepare(self.mind, self.runtime)
        self.assertEqual(storage.read_mode(self.runtime, self.mind), "ram")
        self.assertEqual(list((self.mind / "context-cache").iterdir()), [])
        self.assertEqual(list((self.mind / "cortex/boot-cache").iterdir()), [])

    def test_existing_plaintext_is_not_used_or_removed_during_setup(self):
        paths = storage.WorkingFiles.for_boot("encrypted", self.mind, self.runtime)
        paths.text.mkdir(mode=0o700, parents=True)
        paths.text.parent.chmod(0o700)
        legacy = paths.text / "context.json"
        legacy.write_text("unrelated data")
        with (patch.object(storage, "installed_ready", return_value=True),
              patch.object(storage.fscrypt, "policy_key", return_value=None),
              patch.object(storage.fscrypt, "add_key") as add):
            storage.prepare(self.mind, self.runtime)
        add.assert_not_called()
        self.assertEqual(legacy.read_text(), "unrelated data")
        self.assertEqual(storage.read_mode(self.runtime, self.mind), "ram")

    def test_encrypted_selection_is_bound_to_one_kernel_boot_id(self):
        paths = storage.WorkingFiles.for_boot("encrypted", self.mind, self.runtime)
        for folder in (paths.text, paths.slots):
            folder.mkdir(mode=0o700, parents=True)
        with (patch.object(storage, "installed_ready", return_value=True),
              patch.object(storage, "encrypted_files", return_value=paths)):
            storage.prepare(self.mind, self.runtime)
        self.assertEqual(storage.read_mode(self.runtime, self.mind), "encrypted")
        self.boot.write_text("abcdef01-1111-2222-3333-444444444444\n")
        self.assertIsNone(storage.read_mode(self.runtime, self.mind))
        with self.assertRaises(ValueError):
            storage.prepare(self.mind, self.runtime)

    def test_directory_cannot_be_selected_from_a_partial_environment(self):
        with self.assertRaises(ValueError):
            storage.WorkingFiles.for_process(self.mind, {"BEING_SLOT_DIR": "/mind/cortex"})
