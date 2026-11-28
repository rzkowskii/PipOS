import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab import candidate_manifest, source_id
from tests.test_candidate_manifest import disk_image


class InstalledSourceId(unittest.TestCase):
    def test_only_matching_system_slots_inherit_the_source_id(self):
        with tempfile.TemporaryDirectory() as work, patch.dict("os.environ", {"BEING_IMAGE_SOURCE_ID": ""}):
            try_image = Path(work) / "try.img"
            disk_image(try_image, candidate_manifest.LAPTOP_TRY)
            ident = "a" * 64
            source_id.stamp(try_image, ident=ident)

            for altered in (None, "system-a", "system-b"):
                with self.subTest(altered=altered):
                    installed = Path(work) / f"installed-{altered}.img"
                    disk_image(installed, candidate_manifest.FACTORY, changed_slot=altered)
                    if altered is None:
                        source_id.stamp_verified_install(try_image, installed, role="laptop_try")
                        self.assertEqual(source_id.image_id(installed), ident)
                    else:
                        with self.assertRaisesRegex(ValueError, "both match the Try USB"):
                            source_id.stamp_verified_install(try_image, installed, role="laptop_try")
                        self.assertFalse(source_id.sidecar(installed).exists())
