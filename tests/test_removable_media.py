import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mind import removable_media


class RemovableMediaTests(unittest.IsolatedAsyncioTestCase):
    def test_only_explicit_whole_phrases_match(self):
        self.assertEqual(removable_media.classify("what's on this USB stick?"), "inspect")
        self.assertEqual(removable_media.classify("please eject the stick"), "eject")
        self.assertIsNone(removable_media.classify("I put the photos on a USB stick"))
        self.assertIsNone(removable_media.classify("open the stick automatically"))

    def test_summary_is_bounded_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "photo.jpg").write_bytes(b"photo")
            (root / "notes.txt").write_text("notes")
            os.symlink("photo.jpg", root / "copy.jpg")
            found = removable_media.summarize(root)
        self.assertEqual(found["files"], 2)
        self.assertEqual(found["kinds"], {"photos": 1, "documents": 1})

    def test_candidate_fails_closed_on_none_and_ambiguity(self):
        with patch.object(removable_media.install_disks, "inventory", return_value={"disks": []}):
            with self.assertRaisesRegex(ValueError, "can't find"):
                removable_media._candidate()

    def test_eject_selection_does_not_open_the_filesystem(self):
        disk = {"device": "/dev/sdb", "transport": "usb", "reasons": []}
        with patch.object(removable_media.install_disks, "inventory", return_value={"disks": [disk]}), \
                patch.object(removable_media.install_disks, "disk_contents") as contents:
            self.assertEqual(removable_media._candidate(inspect_contents=False)["device"], "/dev/sdb")
            contents.assert_not_called()
        disk = {"device": "/dev/sdb", "transport": "usb", "reasons": [], "logical_sector_bytes": 512}
        with patch.object(removable_media.install_disks, "inventory", return_value={"disks": [disk, disk]}), \
                patch.object(removable_media.install_disks, "disk_contents",
                             return_value={"readable": True, "being": False, "macos": False}):
            with self.assertRaisesRegex(ValueError, "more than one"):
                removable_media._candidate()

    def test_disk_sequence_revalidation_detects_path_reuse(self):
        expected = {"device": "/dev/sdb", "device_number": "8:16",
                    "device_sequence": 41, "transport": "usb"}
        changed = {**expected, "device_sequence": 42}
        with patch.object(removable_media.install_disks, "inventory", return_value={"disks": [changed]}):
            self.assertFalse(removable_media._same_disk(expected))
        with patch.object(removable_media.install_disks, "inventory", return_value={"disks": [expected]}):
            self.assertTrue(removable_media._same_disk(expected))

    def test_root_parent_mounts_and_only_the_confined_child_scans(self):
        disk = {"device": "/dev/sdb", "device_number": "8:16", "device_sequence": 7}

        class Temporary:
            def __enter__(self):
                return "/run/fake-media"

            def __exit__(self, *_):
                return False

        answer = {"files": 2, "kinds": {"photos": 2}}
        with patch.object(removable_media, "_candidate", return_value=disk), \
                patch.object(removable_media, "_same_disk", return_value=True), \
                patch.object(removable_media.install_disks, "volumes", return_value=[{"device": "/dev/sdb1"}]), \
                patch.object(removable_media.tempfile, "TemporaryDirectory", return_value=Temporary()), \
                patch.object(removable_media, "_mount"), \
                patch.object(removable_media, "_mounted_from", return_value=True), \
                patch.object(removable_media, "summarize_confined", return_value=answer) as confined, \
                patch.object(removable_media, "summarize") as root_scan, \
                patch.object(removable_media.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(removable_media.inspect(), answer)
        confined.assert_called_once_with(Path("/run/fake-media"))
        root_scan.assert_not_called()

    async def test_owner_and_lid_gate_precede_helper(self):
        guest = SimpleNamespace(name="Pip", speaker="guest", owner="Richard", lid_open=True, guest=True)
        closed = SimpleNamespace(name="Pip", speaker="Richard", owner="Richard", lid_open=False, guest=False)
        with patch.object(removable_media, "_run", new=AsyncMock()) as run:
            reply = await removable_media.handle("eject the stick", guest)
            self.assertIn("Only my owner", reply.line)
            reply = await removable_media.handle("what's on this stick", closed)
            self.assertIn("Open my lid", reply.line)
            run.assert_not_awaited()

