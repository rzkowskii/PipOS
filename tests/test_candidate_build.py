"""Exercise the candidate build's host entry point without starting Colima."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from lab import candidate_manifest
from tests.test_candidate_manifest import disk_image


ROOT = Path(__file__).resolve().parents[1]


class CandidateBuild(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "working tree"
        self.root.mkdir()
        (self.root / "docs").mkdir()
        for note in ("DESIGN-AND-PLAN.md", "docs/DESIGN-COVERAGE.md", "docs/USB-READINESS.md"):
            (self.root / note).write_text(note)
        for scope in candidate_manifest.SCOPES:
            path = self.root / scope
            if scope.startswith("Cargo."):
                path.touch()
            else:
                path.mkdir()
                # The frozen snapshot copies files. Real input directories are populated.
                (path / "fixture").touch()
        for name in ("build.sh", "candidate-build.sh", "candidate_manifest.py", "__init__.py"):
            shutil.copy2(ROOT / "lab" / name, self.root / "lab" / name)
        # Model inspection has its own ext4 extraction tests.
        (self.root / "lab/candidate_models.py").write_text('''import hashlib, json, sys
from pathlib import Path
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
source = json.loads(Path(args["--source"]).read_bytes())
media = {role: {"image_sha256": hashlib.sha256(Path(args[option]).read_bytes()).hexdigest()}
        for role, option in (("factory", "--factory"), ("try_image", "--try-image"))}
print(json.dumps({"source_sha256": source["sha256"], "model": source["model"], "media": media}))
''')
        (self.root / "os/board/beingos").mkdir(parents=True)
        board = self.root / "os/board/beingos/slots.sh"
        shutil.copyfile(ROOT / "os/board/beingos/slots.sh", board)
        models = self.root / "out/models"
        models.mkdir(parents=True)
        weights = models / "reflex.gguf"
        weights.write_bytes(b"model fixture")
        with weights.open("rb") as stream:
            metadata = candidate_manifest.fingerprint(stream, weights.stat().st_size)
        (models / "manifest.json").write_text(json.dumps({"reflex": {
            **metadata, "file": "reflex.gguf", "from": "fixture.gguf"}}))

        # The fake container emits disposable disk fixtures into the bind
        # mount selected by the real host driver.
        media = self.root / "out/mock-media"
        media.mkdir()
        disk_image(media / "beingos.img", candidate_manifest.FACTORY,
                   {k: uuid.UUID(v) for k, v in candidate_manifest.factory_slots(self.root).items()})
        disk_image(media / "beingos-try-screen.img", candidate_manifest.TRY)
        for name in ("buildroot.config", "beingos.raucb", "beingos-installed.raucb",
                     "beingos-update-fixtures.img", "beingos-installed-update-qa.img"):
            (media / name).write_text(name)
        commands = self.root / "out/commands"
        commands.mkdir()
        (commands / "colima").write_text('#!/bin/sh\nexit 0\n')
        (commands / "7zz").write_text('#!/bin/sh\nexit 0\n')
        (commands / "docker").write_text('''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
if "--iidfile" in args:
    Path(args[args.index("--iidfile") + 1]).write_text("fixture-builder")
if "--cidfile" in args:
    Path(args[args.index("--cidfile") + 1]).write_text("fixture-container")
    if os.environ.get("FAIL_TEST_BUILD"):
        sys.exit(17)
    output = Path(next(a[:-9] for a in args if a.endswith(":/release")))
    for artifact in Path(os.environ["FIXTURE_MEDIA"]).iterdir():
        shutil.copyfile(artifact, output / artifact.name)
''')
        for command in commands.iterdir():
            command.chmod(0o755)
        self.env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                        FIXTURE_MEDIA=str(media))

    def build(self, name, **env):
        return subprocess.run(["bash", "lab/build.sh", "candidate", name], cwd=self.root,
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def test_normal_entry_freezes_source_and_seals_all_six_artifacts_once(self):
        result = self.build("smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory = self.root / "out/candidates/smoke"
        manifest = json.loads((directory / "release.json").read_text())
        self.assertEqual(set(manifest["artifacts"]), {
            "factory", "try_image", "factory_bundle", "installed_bundle",
            "update_fixtures", "extra:installed_fixtures", "extra:design", "extra:coverage", "extra:readiness",
            "extra:buildroot_config"})
        self.assertTrue(manifest["artifacts"]["try_image"]["path"].endswith("beingos-try-screen.img"))
        self.assertEqual(manifest["model_audit"]["media"]["try_image"]["image_sha256"],
                         manifest["artifacts"]["try_image"]["sha256"])
        self.assertEqual(manifest["source"], candidate_manifest.source_snapshot(directory / "source"))
        self.assertNotEqual(self.build("smoke").returncode, 0)

    def test_invalid_name_or_failed_container_cannot_publish_release(self):
        self.assertEqual(self.build("../escape").returncode, 2)
        result = self.build("partial", FAIL_TEST_BUILD="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "out/candidates/partial/release.json").exists())
