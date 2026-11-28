"""Candidate model checks use frozen metadata and actual streamed member bytes."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab import candidate_models as audit


class CandidateModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.weights = b"GGUF" + bytes(range(256)) * 8193
        self.factory, self.trial = root / "factory.img", root / "try.img"
        for path in (self.factory, self.trial):
            path.write_bytes(self.weights)
        self.model = {"file": audit.WEIGHTS, "from": "upstream.gguf", **audit.digest(self.weights)}
        self.metadata = json.dumps({"reflex": self.model}).encode()
        self.source = {"kind": "beingos-source-v1", "sha256": "a" * 64, "model": self.model,
                       "files": {"out/models/reflex.gguf": audit.digest(self.weights),
                                 "out/models/manifest.json": audit.digest(self.metadata)}}
        # The pipe exercises the real streaming code over several blocks. The
        # listing fixtures expose a models partition occupying each dummy disk.
        # Called as: tool x -so -spd -tExt -- PARTITION MEMBER (lab.hardware_image.member_fingerprint).
        self.tool = root / "fake-extractor"
        self.tool.write_text('#!/bin/sh\ncat "$6"\n')
        self.tool.chmod(0o700)
        self.cms = b"signed envelope fixture"
        self.cms_reads = []
        self.have_cms = True
        self.claimed_size = len(self.weights)
        listings = patch.object(audit, "entries_for", side_effect=self.entries)
        extraction = patch.object(audit, "member", side_effect=self.member)
        listings.start()
        extraction.start()
        self.addCleanup(listings.stop)
        self.addCleanup(extraction.stop)

    def entries(self, tool, kind, path):
        if kind == "GPT":
            label = "models" if path == self.factory else "try-models"
            return [{"Path": f"3.{label}.img", "Offset": "0", "Size": str(path.stat().st_size)}]
        files = {audit.WEIGHTS: self.claimed_size, audit.MANIFEST: len(self.metadata)}
        if self.have_cms:
            files[audit.ENVELOPE] = len(self.cms)
        return [{"Path": name, "Size": str(size), "Mode": "-rw-r--r--"} for name, size in files.items()]

    def member(self, tool, kind, path, name):
        if name == audit.ENVELOPE:
            return self.cms_reads.pop(0) if self.cms_reads else self.cms
        return self.metadata

    def compare(self):
        return audit.compare(self.source, self.factory, self.trial, str(self.tool))

    def test_both_media_have_the_exact_frozen_bytes_and_envelope(self):
        result = self.compare()
        self.assertEqual(result["model"], self.model)
        for role, label in (("factory", "models"), ("try_image", "try-models")):
            record = result["media"][role]
            self.assertEqual(record["partition"], label)
            self.assertEqual(record["weights"], audit.digest(self.weights))
            self.assertEqual(record["signed_envelope"], audit.digest(self.cms))

    def test_equal_size_substitution_on_try_is_detected(self):
        self.trial.write_bytes(self.weights[:-1] + b"z")
        with self.assertRaisesRegex(ValueError, "GGUF digest"):
            self.compare()

    def test_manifest_formatting_and_upstream_metadata_must_match_frozen_source(self):
        self.metadata += b"\n"
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            self.compare()
        self.model["from"] = "different-upstream.gguf"
        self.source["files"]["out/models/manifest.json"] = audit.digest(self.metadata)
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            self.compare()

    def test_missing_empty_and_changed_signed_envelopes_are_rejected(self):
        self.have_cms = False
        with self.assertRaisesRegex(ValueError, audit.ENVELOPE):
            self.compare()
        self.have_cms, self.cms = True, b""
        with self.assertRaisesRegex(ValueError, audit.ENVELOPE):
            self.compare()
        self.cms, self.cms_reads = b"old", [b"old", b"new"]
        with self.assertRaisesRegex(ValueError, "different signed"):
            self.compare()

    def test_size_claims_and_extractor_exit_must_match_stream(self):
        self.claimed_size -= 1
        with self.assertRaisesRegex(ValueError, "GGUF size"):
            self.compare()
        self.claimed_size += 1
        self.factory.write_bytes(self.weights + b"x")
        with self.assertRaisesRegex(ValueError, "Oversized extraction"):
            self.compare()
        self.factory.write_bytes(self.weights)
        self.tool.write_text('#!/bin/sh\ncat "$6"\nexit 2\n')
        with self.assertRaisesRegex(ValueError, "did not finish"):
            self.compare()

    def test_refuses_to_qualify_one_disk_as_both_roles(self):
        with self.assertRaisesRegex(ValueError, "distinct images"):
            audit.compare(self.source, self.factory, self.factory, str(self.tool))

    def test_rejects_inconsistent_frozen_source_before_extraction(self):
        self.source["files"]["out/models/reflex.gguf"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.compare()
