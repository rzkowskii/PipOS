"""Spoken time zone names must resolve with the device's tzdata layout."""

import os
import shutil
import tempfile
import unittest
import zoneinfo
from pathlib import Path
from unittest.mock import patch

from mind import skills


class DeviceTzdataLayout(unittest.TestCase):
    def setUp(self):
        source = next((Path(root) / "Australia/Sydney" for root in zoneinfo.TZPATH
                       if (Path(root) / "Australia/Sydney").is_file()), None)
        if source is None:
            self.skipTest("the host has no compiled Australia/Sydney zone to copy")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # Buildroot's tzdata: real zones under posix/ and right/, regions linked
        # from the top level, and a few plain files at the root.
        for tree in ("posix", "right"):
            (self.root / tree / "Australia").mkdir(parents=True)
            shutil.copyfile(source, self.root / tree / "Australia/Sydney")
        os.symlink("posix/Australia", self.root / "Australia")
        shutil.copyfile(source, self.root / "UTC")
        (self.root / "zone1970.tab").write_text("AU\t-3352+15113\tAustralia/Sydney\n")
        zoneinfo.reset_tzpath([str(self.root)])
        self.addCleanup(zoneinfo.reset_tzpath)
        zoneinfo.ZoneInfo.clear_cache()
        self.addCleanup(zoneinfo.ZoneInfo.clear_cache)
        skills.zone_keys.cache_clear()
        self.addCleanup(skills.zone_keys.cache_clear)
        # On the device the standard list is only the root files: it neither
        # follows the region links nor enters posix/ and right/.
        self.enterContext(patch.object(skills, "available_timezones", return_value={"UTC"}))

    def test_spoken_region_and_city_resolve_through_the_region_link(self):
        for spoken in ("Australia Sydney", "australia slash sydney", "Australia/Sydney"):
            with self.subTest(spoken=spoken):
                self.assertEqual(skills.find_zone(spoken).key, "Australia/Sydney")

    def test_posix_right_and_data_files_are_not_zone_names(self):
        keys = skills.zone_keys()
        self.assertIn("Australia/Sydney", keys)
        self.assertIn("UTC", keys)
        self.assertFalse(any(key.startswith(("posix/", "right/")) for key in keys), keys)
        self.assertNotIn("zone1970.tab", keys)
        self.assertIsNone(skills.find_zone("Posix Australia Sydney"))
        # A city on its own resolves when exactly one zone ends with it (task 1.10).
        self.assertEqual(skills.find_zone("Sydney").key, "Australia/Sydney")


if __name__ == "__main__":
    unittest.main()
