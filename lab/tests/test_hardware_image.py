import hashlib
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lab.hardware_image import (ALBA_RETRAIN_REVISION, ALBA_VOICE_PREFIX, copy_partition, entries, kernel_drivers,
                                verify_alba_retrain, verify_laptop_bundle)
from mind.model_pack import FORMAT_V2


class HardwareImageTests(unittest.TestCase):
    def test_audit_reads_only_exact_squashfs_payload_and_rejects_bad_size(self):
        with tempfile.TemporaryDirectory() as folder:
            image, scratch = (Path(folder) / name for name in ("disk", "filesystem"))
            header = bytearray(96)
            header[:4] = b"hsqs"
            squashfs = b"payload"
            struct.pack_into("<Q", header, 40, len(header) + len(squashfs))
            data = bytes(header) + squashfs
            image.write_bytes(b"other-contents" + data + b"padding")
            part = {"Path": "1.try-system.img", "Offset": 14, "Size": len(data) + 7}
            result = copy_partition(image, part, scratch, squashfs=True)
            self.assertEqual(scratch.read_bytes(), data)
            self.assertEqual(result, hashlib.sha256(data).hexdigest())
            with self.assertRaisesRegex(ValueError, "byte count"):
                copy_partition(image, {**part, "Size": len(data) - 1}, scratch, squashfs=True)
            with self.assertRaisesRegex(ValueError, "partition extent"):
                copy_partition(image, {**part, "Size": image.stat().st_size}, scratch, squashfs=True)

    def test_audit_distinguishes_shipped_modules_from_kernel_builtins(self):
        raw = b"Path = 1.system-a.img\nSize = 4096\nOffset = 1048576\n\nPath = 2.system-b.img\nSize = 4096\n"
        self.assertEqual([item["Path"] for item in entries(raw)], ["1.system-a.img", "2.system-b.img"])
        result = kernel_drivers({"usr/lib/modules/kernel/b43.ko.xz", "lib/backup/bcma.ko.old"},
                                "kernel/drivers/net/tg3.ko\nkernel/sound/snd-hda-intel.ko\n")
        self.assertEqual(result["b43"], "module")
        self.assertIsNone(result["bcma"])
        self.assertEqual(result["gigabit_ethernet"], "built-in")
        self.assertEqual(result["hda_controller"], "built-in")

    def test_signed_bundle_audit_checks_manifest_and_both_payload_digests(self):
        payload = bytearray(96)
        payload[:4] = b"hsqs"
        struct.pack_into("<Q", payload, 40, len(payload))
        padded = bytes(payload) + b"padding"
        provenance = {
            "channel": "beingos-laptop-installed-v1",
            "version": "laptop-fixture",
            "build_id": "b" * 64,
            "rootfs_sha256": hashlib.sha256(padded).hexdigest(),
            "squashfs_bytes_sha256": hashlib.sha256(payload).hexdigest(),
        }
        certificate = b"public release certificate fixture"
        bundle = Path("update.raucb")

        def extract(command, *, stdout, timeout, check):
            rauc, action, keyring, source, destination = command
            self.assertEqual((rauc, action, source), ("rauc", "extract", str(bundle.absolute())))
            self.assertEqual(Path(keyring.removeprefix("--keyring=")).read_bytes(), certificate)
            self.assertTrue(check)
            self.assertEqual(stdout, subprocess.DEVNULL)
            self.assertGreater(timeout, 0)
            target = Path(destination)
            target.mkdir()
            (target / "rootfs.img").write_bytes(padded)
            (target / "manifest.raucm").write_text(
                f"[update]\ncompatible={provenance['channel']}\nversion={provenance['version']}\n"
                f"build={provenance['build_id']}\n[image.rootfs]\nfilename=rootfs.img\n",
                encoding="utf-8",
            )

        with patch("lab.hardware_image.subprocess.run", side_effect=extract):
            verify_laptop_bundle("rauc", bundle, certificate, provenance)
            for field, message in (
                ("version", "manifest does not match update.version"),
                ("rootfs_sha256", "does not match the receipt hash"),
                ("squashfs_bytes_sha256", "does not match the Try SquashFS"),
            ):
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                    verify_laptop_bundle("rauc", bundle, certificate, {**provenance, field: "a" * 64})

    def test_alba_retrain_audit_rejects_wrong_bytes_and_a_legacy_copy(self):
        name = "en_GB-alba-medium.onnx"
        default = ALBA_VOICE_PREFIX + name
        legacy = "voice/old-alba.onnx"
        packaged = {default: b"retrain"}
        new_pin = (7, hashlib.sha256(b"retrain").hexdigest())
        old_pin = (6, hashlib.sha256(b"legacy").hexdigest())

        def listing(*_):
            return [{"Path": path, "Size": str(len(data)), "Mode": "-rw-r--r--"}
                    for path, data in packaged.items()]

        def fingerprint(_tool, _kind, _filesystem, path, size):
            return {"bytes": size, "sha256": hashlib.sha256(packaged[path]).hexdigest()}

        with (patch("lab.hardware_image.ALBA_RETRAIN_FILES", {name: new_pin}),
              patch("lab.hardware_image.ALBA_PRIVATE_MODEL", old_pin),
              patch("lab.hardware_image.entries_for", side_effect=listing),
              patch("lab.hardware_image.member_fingerprint", side_effect=fingerprint)):
            report = verify_alba_retrain("7zz", Path("models.ext4"))
            self.assertEqual(report["files"][name]["sha256"], new_pin[1])
            packaged[default] = b"changed"
            with self.assertRaisesRegex(ValueError, "differs from the pinned voice pack"):
                verify_alba_retrain("7zz", Path("models.ext4"))
            packaged[default] = b"retrain"
            packaged[legacy] = b"legacy"
            with self.assertRaisesRegex(ValueError, "legacy private Alba model is present"):
                verify_alba_retrain("7zz", Path("models.ext4"))

    def test_alba_retrain_audit_checks_all_signed_files_and_both_slot_bindings(self):
        voice, dictionary = "en_GB-alba-medium.onnx", "espeak-ng-data/en_dict"
        parts = {voice: b"weights", dictionary: b"compiled", "NOTICES.md": b"notices"}
        files = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                 for name, data in parts.items()]
        pack = {"format": FORMAT_V2, "files": files, "revision": ALBA_RETRAIN_REVISION}
        expected = {voice: (files[0]["bytes"], files[0]["sha256"])}
        packaged = {ALBA_VOICE_PREFIX + name: data for name, data in parts.items()}
        packaged[ALBA_VOICE_PREFIX + "beingos-tts.cms"] = b"voice envelope"
        binding = b"model certificate", b"matching descriptor"
        bindings = {"system-a": binding, "system-b": binding}

        def listing(*_):
            return [{"Path": path, "Size": str(len(data)), "Mode": "-rw-r--r--"}
                    for path, data in packaged.items()]

        def fingerprint(_tool, _kind, _filesystem, path, size):
            return {"bytes": size, "sha256": hashlib.sha256(packaged[path]).hexdigest()}

        def authenticate(envelope, certificate, descriptor, role):
            self.assertEqual((envelope, certificate.read_bytes(), role), (b"voice envelope", binding[0], "tts"))
            if descriptor.read_bytes() != binding[1]:
                raise ValueError("signed descriptor differs from slot descriptor")
            return pack

        with (patch("lab.hardware_image.ALBA_RETRAIN_FILES", expected),
              patch("lab.hardware_image.ALBA_REQUIRED_ESPEAK_DATA", {"en_dict"}),
              patch("lab.hardware_image.entries_for", side_effect=listing),
              patch("lab.hardware_image.member", side_effect=lambda *args: packaged[args[-1]]),
              patch("lab.hardware_image.member_fingerprint", side_effect=fingerprint),
              patch("lab.hardware_image.certificate_sha256", return_value="c" * 64),
              patch("mind.model_pack.authenticate", side_effect=authenticate)):
            report = verify_alba_retrain("7zz", Path("models.ext4"), bindings=bindings)
            self.assertEqual(set(report["files"]), set(parts))
            self.assertEqual(set(report["systems"]), set(bindings))
            bindings["system-b"] = binding[0], b"other descriptor"
            with self.assertRaisesRegex(ValueError, "signed descriptor differs"):
                verify_alba_retrain("7zz", Path("models.ext4"), bindings=bindings)
            bindings["system-b"] = binding
            packaged[ALBA_VOICE_PREFIX + dictionary] = b"modified"
            with self.assertRaisesRegex(ValueError, "differs from the pinned voice pack"):
                verify_alba_retrain("7zz", Path("models.ext4"), bindings=bindings)
