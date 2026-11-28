"""Boundaries between the previous signer, replacement trust and model trust."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab import hardware_image


class LaptopRotation(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bundle = root / "bridge.raucb"
        self.bundle.write_bytes(b"synthetic bridge")
        self.old, self.new = "a" * 64, "b" * 64
        self.receipt = {
            "channel": "beingos-laptop-installed-v1", "version": "rotation-fixture", "build_id": "c" * 64,
            "source_id": "d" * 64, "squashfs_bytes_sha256": "e" * 64, "rootfs_sha256": "f" * 64,
            "bundle_sha256": hardware_image.artifact_sha256(self.bundle), "signer_der_sha256": self.old,
            "rotation": {"from_der_sha256": self.old, "to_der_sha256": self.new},
            "payload_verified_at_signing": True,
        }
        self.report = {"system_partitions": {"try-system": self.receipt.copy()}}
        self.write_receipt()

    def write_receipt(self):
        self.bundle.with_name(self.bundle.name + ".provenance.json").write_text(json.dumps(self.receipt))

    def test_bridge_audit_uses_old_signature_and_rejects_cross_trust(self):
        with (patch.object(hardware_image, "certificate_sha256", side_effect=(self.old, self.new)),
              patch.object(hardware_image, "certificate_public_key", side_effect=(b"old key", b"new key")),
              patch.object(hardware_image, "verify_laptop_bundle") as verify):
            result = hardware_image.match_laptop_bundle(self.report, self.bundle, release_signer=self.new,
                                                        previous_certificate=b"old certificate",
                                                        verify=("rauc", b"new certificate"))
        self.assertEqual(result["rotation"], self.receipt["rotation"])
        verify.assert_called_once_with("rauc", self.bundle, b"old certificate", result,
                                       reject_certificate=b"new certificate")

    def test_rotation_requires_both_explicit_trust_roles_and_signed_extraction(self):
        with self.assertRaisesRegex(ValueError, "requires --previous-release-certificate"):
            hardware_image.match_laptop_bundle(self.report, self.bundle, release_signer=self.new)
        with self.assertRaisesRegex(ValueError, "RAUC verification"):
            hardware_image.match_laptop_bundle(self.report, self.bundle, release_signer=self.new,
                                               previous_certificate=b"old certificate")

    def test_new_certificate_with_the_same_key_is_not_rotation(self):
        with (patch.object(hardware_image, "certificate_sha256", side_effect=(self.old, self.new)),
              patch.object(hardware_image, "certificate_public_key", return_value=b"same key"),
              self.assertRaisesRegex(ValueError, "distinct signing keys")):
            hardware_image.match_laptop_bundle(self.report, self.bundle, release_signer=self.new,
                                               previous_certificate=b"old certificate",
                                               verify=("rauc", b"new certificate"))

    def test_base_must_trust_the_model_pack_the_replacement_will_boot(self):
        with patch.object(hardware_image, "inspect", return_value={"system_partitions": {"try-system": {
            "model_pack_certificate_sha256": "1" * 64,
        }}}), self.assertRaisesRegex(ValueError, "installed model-pack certificate"):
            hardware_image.rotation_base("7zz", self.bundle, self.old,
                                         {"model_pack_certificate_sha256": "2" * 64})
