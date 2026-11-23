"""Candidate manifests bind an uncommitted source tree to named regular files."""

import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from lab import candidate_manifest as candidate
from mind.install_apply import TYPES, gpt_records


def disk_image(path, names, fixed_ids=None, changed_slot=None):
    # Tiny GPT disks with self-consistent partition/slot boundaries. The
    # manifest fingerprints a SquashFS byte span and never mounts these files.
    fixed_ids = fixed_ids or {}
    parts = [{"name": name, "start_lba": 40 + i * 3, "sector_count": 3,
              "gpt_type": TYPES[0 if i == 0 else 1]} for i, name in enumerate(names)]
    ids = {name: fixed_ids.get(name, uuid.uuid4()) for name in names}
    sector, capacity = 512, 128 * 512
    mbr, primary, table, backup, backup_at = gpt_records(sector, capacity, parts, uuid.uuid4(), ids)
    with path.open("wb") as output:
        output.truncate(capacity)
        for offset, data in ((0, mbr), (sector, primary), (2 * sector, table),
                             (backup_at, table), (capacity - sector, backup)):
            output.seek(offset)
            output.write(data)
        for part in parts:
            if part["name"] in ("system-a", "system-b", "try-system"):
                payload = bytearray(110)
                payload[:4] = b"hsqs"
                struct.pack_into("<Q", payload, 40, len(payload))
                if part["name"] == changed_slot:
                    payload[-1] = 1
                output.seek(part["start_lba"] * sector)
                output.write(payload)


class CandidateManifest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.root = self.dir / "source"
        self.root.mkdir()
        for scope in candidate.SCOPES:
            if scope in ("Cargo.toml", "Cargo.lock"):
                (self.root / scope).touch()
            else:
                (self.root / scope).mkdir()
        self.ids = {"system-a": uuid.uuid4(), "system-b": uuid.uuid4()}
        board = self.root / "os/board/beingos"
        board.mkdir(parents=True)
        (board / "slots.sh").write_text("".join(f"slot_{k[-1]}_uuid={v}\n" for k, v in self.ids.items()))
        models = self.root / "out/models"
        models.mkdir(parents=True)
        model = b"local model fixture"
        (models / "reflex.gguf").write_bytes(model)
        (models / "manifest.json").write_text(json.dumps({"reflex": {
            "file": "reflex.gguf", "bytes": len(model), "from": "fixture.gguf",
            "sha256": hashlib.sha256(model).hexdigest()}}))
        self.source = self.dir / "source.json"
        self.release = self.dir / "release.json"
        self.factory = self.dir / "factory.img"
        self.usb = self.dir / "try.img"
        disk_image(self.factory, candidate.FACTORY, self.ids)
        disk_image(self.usb, candidate.TRY)
        self.snapshot = ["snapshot", "--output", self.source]
        self.seal = ["seal", "--source", self.source, "--output", self.release,
                     "--factory", self.factory, "--try-image", self.usb]
        self.verify = ["verify", self.release]

    def run_cli(self, args, *, ok=True):
        message = io.StringIO()
        with redirect_stdout(message), redirect_stderr(message):
            result = candidate.main([str(arg) for arg in [*args, "--root", self.root]])
        self.assertEqual(result, 0 if ok else 1, message.getvalue())
        return message.getvalue()

    def test_relocated_identical_tree_and_unborn_checkout_share_snapshot(self):
        self.run_cli(self.snapshot)
        source = json.loads(self.source.read_bytes())
        self.assertEqual(source["model"]["from"], "fixture.gguf")
        other = self.dir / "copied"
        shutil.copytree(self.root, other)
        os.utime(other / "Cargo.toml", (1, 1))
        self.assertEqual(source, candidate.source_snapshot(other))
        self.assertIn("File exists", self.run_cli(self.snapshot, ok=False))
        self.assertIn("outside the source", self.run_cli(["snapshot", "--output", self.root / "lab/x"], ok=False))

    def test_fresh_install_same_slots_and_distinct_uuids_can_be_sealed(self):
        installed = self.dir / "installed.img"
        disk_image(installed, candidate.FACTORY)
        extra = self.dir / "wifi-fixture.img"
        extra.write_bytes(b"wifi fixture")
        self.run_cli(self.snapshot)
        self.run_cli(self.seal + ["--installed-image", installed, "--extra", f"wifi={extra}"])
        self.run_cli(self.verify)
        artifacts = json.loads(self.release.read_text())["artifacts"]
        self.assertEqual(artifacts["factory"]["partitions"][1]["rootfs"],
                         artifacts["try_image"]["partitions"][1]["rootfs"])
        self.assertEqual(artifacts["extra:wifi"]["sha256"], hashlib.sha256(extra.read_bytes()).hexdigest())
        extra.write_bytes(b"new wifi fixture")
        self.assertIn("candidate artifact changed", self.run_cli(self.verify, ok=False))

    def test_source_drift_and_model_drift_fail(self):
        self.run_cli(self.snapshot)
        organ = self.root / "mind/added.py"
        organ.touch()
        self.assertIn("mind/added.py", self.run_cli(self.seal, ok=False))
        self.assertFalse(self.release.exists())
        organ.unlink()
        self.run_cli(self.seal)
        (self.root / "Cargo.lock").chmod(0o755)
        self.assertIn("Cargo.lock", self.run_cli(self.verify, ok=False))
        (self.root / "out/models/reflex.gguf").write_bytes(b"other model")
        self.assertIn("model identity", self.run_cli(self.verify, ok=False))

    def test_model_inspection_cannot_be_reused_for_a_different_disk(self):
        self.run_cli(self.snapshot)
        source = json.loads(self.source.read_bytes())
        audit = {"source_sha256": source["sha256"], "model": source["model"], "media": {
            role: {"image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for role, path in (("factory", self.factory), ("try_image", self.usb))
        }}
        audit_path = self.dir / "packaged-models.json"
        audit_path.write_text(json.dumps(audit))
        disk_image(self.usb, candidate.TRY)
        self.assertIn("different try_image", self.run_cli(self.seal + ["--model-audit", audit_path], ok=False))
        self.assertFalse(self.release.exists())

    def test_mismatched_factory_b_try_and_disguised_installed_image_fail(self):
        self.run_cli(self.snapshot)
        disk_image(self.factory, candidate.FACTORY, self.ids, changed_slot="system-b")
        self.assertIn("payloads differ", self.run_cli(self.seal, ok=False))
        disk_image(self.factory, candidate.FACTORY, self.ids)
        disk_image(self.usb, candidate.TRY, changed_slot="try-system")
        self.assertIn("payloads differ", self.run_cli(self.seal, ok=False))
        disk_image(self.factory, candidate.FACTORY)
        self.assertIn("factory slot IDs", self.run_cli(self.seal, ok=False))

    def test_duplicate_bundles_substituted_image_and_invalid_gpt_fail(self):
        self.run_cli(self.snapshot)
        bundles = [self.dir / name for name in ("lab.raucb", "installed.raucb")]
        for bundle in bundles:
            bundle.write_bytes(b"lab signed fixture")
        args = self.seal + ["--factory-bundle", bundles[0], "--installed-bundle", bundles[1]]
        self.assertIn("identical bundles", self.run_cli(args, ok=False))
        bundles[1].write_bytes(b"installed signed fixture")
        self.run_cli(args)
        # Outside SquashFS, still part of the boot media fingerprint.
        with self.usb.open("r+b") as stream:
            stream.seek(40 * 512)
            stream.write(b"replacement EFI data")
        self.assertIn("changed: try_image", self.run_cli(self.verify, ok=False))
        with self.usb.open("r+b") as stream:
            stream.seek(-512, os.SEEK_END)
            stream.write(b"broken!!")
        self.assertIn("GPT", self.run_cli(self.verify, ok=False))

    def test_refuses_duplicate_disk_identity_symlink_fifo_and_raw_devices(self):
        self.run_cli(self.snapshot)
        copied = self.dir / "installed.img"
        shutil.copyfile(self.factory, copied)
        self.assertIn("shares GPT identities", self.run_cli(self.seal + ["--installed-image", copied], ok=False))
        linked = self.dir / "linked"
        linked.symlink_to(self.factory)
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        for path in (linked, fifo, Path(os.devnull)):
            with self.subTest(path=path):
                self.assertIn("regular file", self.run_cli(self.seal + ["--update-fixtures", path], ok=False))
