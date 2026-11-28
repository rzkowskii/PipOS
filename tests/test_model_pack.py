"""Signing, runtime and legacy provisioning exercise the same pack policy."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lab import alba_retrain
from lab.package_model import DEFAULT_PACK, produce, stage
from mind import library_pack
from mind.model_pack import ENVELOPE, FORMAT_V2, WEIGHTS, main, metadata_bytes, openssl


class AlbaRetrainInput(unittest.TestCase):
    def test_espeak_data_target_uses_only_the_verified_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "espeak.zip"
            scratch = root / "scratch"
            scratch.mkdir()
            upstream = f"espeak-ng-{alba_retrain.ESPEAK_REVISION}/CMakeLists.txt"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(upstream, "# pinned fixture\n")
            data = source.read_bytes()
            pin = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            with (patch.object(alba_retrain, "ESPEAK_SOURCE", pin),
                  patch.object(alba_retrain.shutil, "which", return_value="cmake"),
                  patch("lab.candidate_tests.batch", return_value="passed") as execute):
                self.assertEqual(alba_retrain.build_espeak_data(source, scratch), scratch / "build/espeak-ng-data")
                self.assertEqual((scratch / upstream).read_text(), "# pinned fixture\n")
                self.assertEqual(execute.call_count, 2)
                build_command = execute.call_args.args[0]
                self.assertEqual(build_command[-4:], ["--target", "data", "--parallel", "4"])
                execute.reset_mock()
                source.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
                with self.assertRaisesRegex(ValueError, "pinned source digest"):
                    alba_retrain.build_espeak_data(source, scratch)
                execute.assert_not_called()

    def test_staged_descriptor_reproduces_the_archive_and_rejects_wrong_voice_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prepared, data = root / "prepared", root / "espeak-ng-data"
            prepared.mkdir()
            data.mkdir()
            name, voice = "en_GB-alba-medium.onnx", b"pinned voice"
            (prepared / name).write_bytes(voice)
            (data / "en_dict").write_bytes(b"dictionary")
            notice = root / "NOTICES.md"
            notice.write_text("Fixture attribution\n", encoding="utf-8")
            pins = {name: (len(voice), hashlib.sha256(voice).hexdigest())}
            with (patch.object(alba_retrain, "ALBA_RETRAIN_FILES", pins),
                  patch.object(alba_retrain, "REQUIRED_DATA", {"en_dict"}),
                  patch.object(alba_retrain, "NOTICE", notice)):
                output = root / "staged"
                pack = alba_retrain.stage(prepared, data, output)
                self.assertEqual(pack, json.loads((output / "pack.json").read_text()))
                self.assertEqual(set(produce(output, pack)), {name, "NOTICES.md", "espeak-ng-data/en_dict"})
                alternate = root / "alternate"
                self.assertEqual(alba_retrain.stage(prepared, data, alternate), pack)

                (prepared / name).write_bytes(b"wrong voice!")
                rejected = root / "rejected"
                with self.assertRaisesRegex(ValueError, "differs from the pinned retrain"):
                    alba_retrain.stage(prepared, data, rejected)
                self.assertFalse(rejected.exists())


def keypair(directory, label):
    key = directory / (label + ".key")
    cert = directory / (label + ".pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN={label}", "-keyout", str(key), "-out", str(cert)],
                   capture_output=True, check=True, timeout=15)
    return key, cert


@unittest.skipUnless(shutil.which("openssl"), "needs the OpenSSL CLI")
class ModelPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.key, cls.cert = keypair(Path(temp.name), "fixture release")
        cls.other_key, cls.other_cert = keypair(Path(temp.name), "other self-signed release")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source = root / "source"
        self.pack = root / "pack"
        self.source.mkdir()
        self.descriptor = root / "pack.json"
        self.weights = b"GGUF fixture for model pack verification"
        (self.source / WEIGHTS).write_bytes(self.weights)
        license_text = b"fixture license"
        (root / "LICENSE").write_bytes(license_text)
        info = json.loads((DEFAULT_PACK / "pack.json").read_bytes())
        info.update(bytes=len(self.weights), sha256=hashlib.sha256(self.weights).hexdigest(),
                    license_sha256=hashlib.sha256(license_text).hexdigest())
        (self.source / "manifest.json").write_text(json.dumps({"reflex": {
            "file": WEIGHTS, "from": "fixture.gguf", "bytes": len(self.weights), "sha256": info["sha256"]}}))
        self.descriptor.write_text(json.dumps(info))
        stage(self.source, self.pack, self.descriptor, self.cert, self.key)

    def invoke(self, action="verify", *, models=None, donor=None, certificate=None, ok=True):
        args = [action, "--models", str(models or self.pack), "--descriptor", str(self.descriptor),
                "--certificate", str(certificate or self.cert)]
        if donor is not None:
            args.extend(["--signed-pack", str(donor)])
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            result = main(args)
        self.assertEqual(result, 0 if ok else 1, output.getvalue())
        return output.getvalue()

    def test_exact_pack_qualifies_and_changed_bytes_are_refused(self):
        self.invoke()
        (self.pack / WEIGHTS).write_bytes(b"X" + self.weights[1:])
        self.assertIn("SHA-256", self.invoke(ok=False))
        (self.pack / WEIGHTS).write_bytes(b"truncated")
        self.assertIn("size", self.invoke(ok=False))

    def test_signature_is_mandatory_even_with_historical_inventory(self):
        signed = self.pack / ENVELOPE
        data = signed.read_bytes()
        signed.unlink()
        self.assertTrue((self.pack / "manifest.json").is_file())
        self.invoke(ok=False)
        signed.write_bytes(data[:-1] + bytes((data[-1] ^ 1,)))
        self.assertIn("cryptographic", self.invoke(ok=False))
        signed.write_bytes(data + b"trailing data")
        self.assertIn("model envelope", self.invoke(ok=False))
        signed.write_bytes(metadata_bytes(self.descriptor))
        self.invoke(ok=False)

    def test_wrong_signer_with_embedded_certificate_and_sibling_descriptor_rejected(self):
        signed = self.pack / ENVELOPE
        self.invoke(certificate=self.other_cert, ok=False)
        embedded = openssl("cms", "-sign", "-binary", "-nodetach", "-md", "sha256",
                           "-outform", "DER", "-signer", str(self.other_cert), "-inkey", str(self.other_key),
                           data=metadata_bytes(self.descriptor))
        signed.write_bytes(embedded)
        self.invoke(ok=False)
        self.invoke(certificate=self.other_cert)
        changed = metadata_bytes(self.descriptor).replace(b"Q4_K_M", b"wrong-quantization")
        signed.write_bytes(openssl("cms", "-sign", "-binary", "-nodetach", "-md", "sha256", "-outform", "DER",
                                   "-signer", str(self.cert), "-inkey", str(self.key), data=changed))
        self.assertIn("qualified system descriptor", self.invoke(ok=False))

    def test_legacy_provision_checks_staged_weights_and_never_replaces_on_failure(self):
        donor = self.pack / ENVELOPE
        self.invoke(models=self.source, ok=False)
        self.invoke("provision", models=self.source, donor=donor)
        self.invoke(models=self.source)
        existing = self.source / ENVELOPE
        self.assertEqual(existing.read_bytes(), donor.read_bytes())
        donor.write_bytes(b"invalid donor")
        self.invoke("provision", models=self.source, donor=donor, ok=False)
        self.invoke(models=self.source)
        (self.source / WEIGHTS).unlink()
        (self.source / WEIGHTS).symlink_to(self.pack / WEIGHTS)
        self.assertIn("regular file", self.invoke(models=self.source, ok=False))

    def test_absent_or_corrupt_legacy_donors_never_reach_the_writer(self):
        absent = self.pack / "absent.cms"
        corrupt = self.pack / "corrupt.cms"
        envelope = (self.pack / ENVELOPE).read_bytes()
        corrupt.write_bytes(envelope[:-1] + bytes((envelope[-1] ^ 1,)))
        with patch("mind.model_pack.provision") as writer:
            self.assertIn(str(absent), self.invoke("provision", models=self.source, donor=absent, ok=False))
            self.assertIn("cryptographic operation failed",
                          self.invoke("provision", models=self.source, donor=corrupt, ok=False))
        writer.assert_not_called()
        self.assertFalse((self.source / ENVELOPE).exists())

    def test_packager_refuses_different_model_or_license_before_signing(self):
        (self.source / WEIGHTS).write_bytes(b"A" + self.weights[1:])
        output = self.pack.parent / "wrong"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            stage(self.source, output, self.descriptor, self.cert, self.key)
        self.assertFalse((output / ENVELOPE).exists())
        self.descriptor.with_name("LICENSE").write_bytes(b"changed text")
        with self.assertRaisesRegex(ValueError, "licence"):
            stage(self.source, output, self.descriptor, self.cert, self.key)

    def test_shipped_descriptor_matches_archived_upstream_licence_and_unit(self):
        info = json.loads((DEFAULT_PACK / "pack.json").read_bytes())
        self.assertEqual(hashlib.sha256((DEFAULT_PACK / "LICENSE").read_bytes()).hexdigest(), info["license_sha256"])
        self.assertEqual((info["license"], info["role"], info["model"]), ("Apache-2.0", "conversation", "Qwen/Qwen3.5-0.8B"))
        self.assertIn("ExecCondition=/usr/bin/python3 /usr/lib/being/mind/model_pack.py verify",
                      (DEFAULT_PACK.parent / "30-model-pack.conf").read_text().splitlines())


class LibraryImportCapacity(unittest.TestCase):
    def test_read_only_check_and_insufficient_space_never_remount(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "donor/library/oewn"
            source.mkdir(parents=True)
            (source / "pack.json").write_bytes(b"manifest")
            models = root / "models"
            models.mkdir()
            pack = {"format": FORMAT_V2, "files": [
                {"path": "dictionary.sqlite3", "bytes": 4097, "sha256": "a" * 64},
                {"path": "LICENSE", "bytes": 80, "sha256": "b" * 64},
            ]}
            space = SimpleNamespace(f_flag=os.ST_RDONLY, f_frsize=4096, f_blocks=40,
                                    f_bfree=20, f_bavail=20, f_favail=8)
            with (patch.object(library_pack.os, "geteuid", return_value=0),
                  patch.object(library_pack.os, "statvfs", return_value=space),
                  patch.object(library_pack, "_models_bound", return_value=True),
                  patch.object(library_pack, "_remount") as remount,
                  patch("mind.model_pack.verify_envelope", return_value=(b"signed", pack))):
                report = library_pack._import_pack(source, "library", models=models, check=True)
                self.assertTrue(report["fits"])
                self.assertEqual(report["estimated_required_bytes"], 9 * 4096)
                self.assertEqual(report["estimated_remaining_bytes"], 11 * 4096)
                self.assertFalse(report["estimated_room_for_current_contents_replacement"])
                for field, count in (("f_bavail", 8), ("f_favail", 7)):
                    with self.subTest(field=field), patch.object(space, field, count):
                        with self.assertRaisesRegex(ValueError, "insufficient models partition space"):
                            library_pack._import_pack(source, "library", models=models)
                remount.assert_not_called()
            self.assertEqual(list(models.iterdir()), [])

    def test_retry_reclaims_stage_and_a_bad_copy_never_publishes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "donor/library/oewn"
            source.mkdir(parents=True)
            (source / "pack.json").write_bytes(b"manifest")
            (source / "dictionary.sqlite3").write_bytes(b"dictionary")
            (source / "beingos-library.cms").write_bytes(b"signed")
            models = root / "models"
            stage_dir = models / library_pack.IMPORT_STAGE
            stage_dir.mkdir(parents=True)
            (stage_dir / "incomplete").write_bytes(b"old bytes")

            pack = {"format": FORMAT_V2, "files": [{"path": "dictionary.sqlite3", "bytes": 10,
                                                   "sha256": "a" * 64}]}
            space = SimpleNamespace(f_flag=os.ST_RDONLY, f_frsize=4096, f_blocks=40,
                                    f_bfree=2, f_bavail=2, f_favail=8)

            def statvfs(_path):
                free = 2 if (stage_dir / "incomplete").exists() else 12
                return SimpleNamespace(**{**vars(space), "f_bfree": free, "f_bavail": free})

            def verify(base, *_args, **_kwargs):
                if base == stage_dir:
                    self.assertFalse((stage_dir / "incomplete").exists())
                    self.assertEqual((stage_dir / "library/oewn/dictionary.sqlite3").read_bytes(), b"dictionary")
                    raise ValueError("copied bytes do not match")
                return b"signed", pack

            with (patch.object(library_pack.os, "geteuid", return_value=0),
                  patch.object(library_pack.os, "statvfs", side_effect=statvfs),
                  patch.object(library_pack, "_models_bound", return_value=True),
                  patch.object(library_pack, "_staging_exists", return_value=True),
                  patch.object(library_pack, "_remount") as remount,
                  patch("mind.model_pack.verify_envelope", side_effect=verify)):
                report = library_pack._import_pack(source, "library", models=models, check=True)
                self.assertTrue(report["unfinished_stage"])
                self.assertFalse(report["fits"])
                self.assertTrue((stage_dir / "incomplete").exists())
                remount.assert_not_called()
                with self.assertRaisesRegex(ValueError, "copied bytes do not match"):
                    library_pack._import_pack(source, "library", models=models)
                self.assertEqual([call.args for call in remount.call_args_list], [(True, models), (False, models)])
            self.assertFalse(stage_dir.exists())
            self.assertFalse((models / "library/oewn").exists())
