"""Signing, runtime and legacy provisioning exercise the same pack policy."""

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lab.package_model import DEFAULT_PACK, stage
from mind.model_pack import ENVELOPE, WEIGHTS, main, metadata_bytes, openssl


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
