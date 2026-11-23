"""Role packs (format v2), the verdict cache and the weekly re-hash (tasks L7.4, L7.5)."""

import asyncio
import contextlib
import hashlib
import inspect
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from lab.package_model import describe, produce, stage_bench, stage_role
from mind import model_pack
from mind.model_pack import IMAGE_ROLES, RehashJob, Verdicts, envelope_name, root, verify
from tests.test_model_pack import keypair

REPO = Path(__file__).resolve().parent.parent
PACKS = REPO / "os/package/being/model-packs"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def add(archive, name, data=None, kind=tarfile.REGTYPE):
    info = tarfile.TarInfo(name)
    info.type = kind
    if data is not None:
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    else:
        if kind == tarfile.SYMTYPE:
            info.linkname = "../../etc/passwd"
        archive.addfile(info)


@unittest.skipUnless(shutil.which("openssl"), "needs the OpenSSL CLI")
class RolePacks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.key, cls.cert = keypair(Path(temp.name), "fixture release")
        cls.other_key, cls.other_cert = keypair(Path(temp.name), "other release")

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.out = self.root / "out"
        voice = self.out / "voice/asr/m"
        voice.mkdir(parents=True)
        self.archive = voice / "m.tar.bz2"
        with tarfile.open(self.archive, "w:bz2") as archive:
            add(archive, "m/", kind=tarfile.DIRTYPE)
            add(archive, "m/encoder_model.ort", b"encoder")
            add(archive, "m/decoder_model_merged.ort", b"decoder")
            add(archive, "m/tokens.txt", b"a 1\nb 2\n")
            add(archive, "m/test_wavs/0.wav", b"RIFF unwanted")
            add(archive, "m/data/", kind=tarfile.DIRTYPE)
            add(archive, "m/data/en_dict", b"dictionary")
            add(archive, "m/data/voices/one", b"voice one")
        (self.out / "voice/vad").mkdir(parents=True)
        (self.out / "voice/vad/silero_vad.onnx").write_bytes(b"vad")
        data = self.archive.read_bytes()
        (self.out / "voice/DOWNLOADS.json").write_text(json.dumps({"files": [
            {"name": "asr/m/m.tar.bz2", "size_bytes": len(data), "sha256": sha(data)},
            {"name": "vad/silero_vad.onnx", "size_bytes": 3, "sha256": sha(b"vad")}]}))
        self.packs = self.root / "packs"
        self.draft("asr", [{"file": "voice/asr/m/m.tar.bz2", "bytes": len(data), "sha256": sha(data), "extract": [
            {"member": f"m/{name}", "path": name} for name in ("encoder_model.ort", "decoder_model_merged.ort",
                                                               "tokens.txt")] + [
            {"prefix": "m/data/", "path": "data/"}]}])
        self.draft("vad", [{"file": "voice/vad/silero_vad.onnx", "bytes": 3, "sha256": sha(b"vad"),
                            "path": "silero_vad.onnx"}])
        for role in ("asr", "vad"):
            describe(role, self.out, self.packs)
        self.models = self.root / "models"
        self.models.mkdir()
        for role in ("vad", "asr"):
            stage_role(role, self.out, self.models, self.descriptor(role), self.cert, self.key)
        self.verdict = self.root / "mind/cortex/pack-verdict.json"
        self.verdict.parent.mkdir(parents=True)

    def draft(self, role, sources):
        (self.packs / role).mkdir(parents=True)
        (self.packs / role / "pack.json").write_text(json.dumps({
            "format": model_pack.FORMAT_V2, "role": role, "dir": model_pack.ROLE_DIRS[role],
            "model": f"fixture {role}", "license": "MIT", "sources": sources, "files": []}))

    def descriptor(self, role):
        return self.packs / role / "pack.json"

    def verify(self, role="asr", **options):
        options.setdefault("descriptor", self.descriptor(role))
        options.setdefault("verdict", self.verdict)
        options.setdefault("build_id", "build-1")
        return verify(role, models=self.models, certificate=self.cert, **options)

    def test_archives_are_extracted_by_name_only_and_every_file_is_listed(self):
        pack = json.loads(self.descriptor("asr").read_text())
        self.assertEqual([entry["path"] for entry in pack["files"]],
                         ["data/en_dict", "data/voices/one", "decoder_model_merged.ort", "encoder_model.ort",
                          "tokens.txt"])
        base = root("asr", self.models)
        self.assertEqual(base, self.models / "asr/moonshine-tiny")
        self.assertEqual((base / "data/voices/one").read_bytes(), b"voice one")
        self.assertFalse((base / "test_wavs").exists(), "unlisted members are never written")
        self.assertTrue((self.models / "asr" / envelope_name("vad")).is_file())
        self.assertEqual(self.verify()["role"], "asr")
        self.assertEqual(self.verify("vad")["files"][0]["path"], "silero_vad.onnx")

    def test_an_archive_that_hides_a_link_under_a_prefix_is_refused(self):
        evil = self.out / "voice/asr/m/m.tar.bz2"
        with tarfile.open(evil, "w:bz2") as archive:
            for name in ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"):
                add(archive, f"m/{name}", b"x")
            add(archive, "m/data/link", kind=tarfile.SYMTYPE)
        pack = json.loads(self.descriptor("asr").read_text())
        source = pack["sources"][0]
        source.update(bytes=evil.stat().st_size, sha256=sha(evil.read_bytes()))
        downloads = json.loads((self.out / "voice/DOWNLOADS.json").read_text())
        downloads["files"][0].update(size_bytes=source["bytes"], sha256=source["sha256"])
        (self.out / "voice/DOWNLOADS.json").write_text(json.dumps(downloads))
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            produce(self.out, pack)

    def test_sources_must_match_the_descriptor_and_the_download_record(self):
        downloads = json.loads((self.out / "voice/DOWNLOADS.json").read_text())
        downloads["files"][1]["sha256"] = "0" * 64
        (self.out / "voice/DOWNLOADS.json").write_text(json.dumps(downloads))
        with self.assertRaisesRegex(ValueError, "download record"):
            stage_role("vad", self.out, self.root / "again", self.descriptor("vad"), self.cert, self.key)
        (self.out / "voice/vad/silero_vad.onnx").write_bytes(b"VAD")
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            stage_role("vad", self.out, self.root / "again2", self.descriptor("vad"), self.cert, self.key)

    def test_signature_role_and_system_descriptor_are_all_required(self):
        signed = root("asr", self.models) / envelope_name("asr")
        with self.assertRaisesRegex(ValueError, "cryptographic"):
            verify("asr", models=self.models, certificate=self.other_cert, descriptor=self.descriptor("asr"),
                   verdict=None)
        # The vad envelope moved into the asr folder is the wrong role.
        shutil.copyfile(self.models / "asr" / envelope_name("vad"), signed)
        with self.assertRaisesRegex(ValueError, "qualified system descriptor"):
            self.verify()
        with self.assertRaisesRegex(ValueError, "role, format or file"):
            self.verify(descriptor=None)

    def test_without_a_shipped_descriptor_the_signature_alone_vouches_and_says_so(self):
        self.assertTrue(self.verify()["bound"])
        self.assertFalse(self.verify(descriptor=None)["bound"])

    def test_a_changed_or_missing_file_is_refused(self):
        tokens = root("asr", self.models) / "tokens.txt"
        tokens.write_bytes(b"a 9\nb 2\n")
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.verify(verdict=None)
        tokens.unlink()
        with self.assertRaises(OSError):
            self.verify(verdict=None)
        tokens.symlink_to(self.root / "out/voice/vad/silero_vad.onnx")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.verify(verdict=None)

    # --- the verdict cache (L7.5) ---

    def test_a_matching_verdict_skips_the_hash_and_a_new_build_does_not(self):
        hashed = []
        self.verify(hashed=hashed)
        self.assertEqual(len(hashed), 5)
        entry = Verdicts(self.verdict).role("asr")
        key = entry["files"]["tokens.txt"]
        self.assertEqual(set(key), {"partuuid", "inode", "size", "mtime_ns", "ctime_ns", "sha256", "build_id"})
        hashed.clear()
        self.verify(hashed=hashed)
        self.assertEqual(hashed, [], "a stat match needs no hash")
        self.verify(hashed=hashed, build_id="build-2")
        self.assertEqual(len(hashed), 5, "a new build hashes everything again")
        hashed.clear()
        self.verify(hashed=hashed, build_id=None)
        self.assertEqual(len(hashed), 5, "with no build-id the cache is not used")

    def test_tampering_that_keeps_the_size_is_caught_by_mtime_ctime_or_inode(self):
        path = root("asr", self.models) / "encoder_model.ort"
        for tamper in ("rewrite", "mtime", "inode"):
            with self.subTest(tamper=tamper):
                self.verify()
                info = path.stat()
                if tamper == "rewrite":
                    path.write_bytes(b"ENCODER")
                elif tamper == "mtime":
                    data = bytearray(path.read_bytes())
                    data[0] ^= 0x20
                    path.write_bytes(bytes(data))
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))   # mtime put back; ctime moves
                else:
                    other = path.with_name("swap")
                    other.write_bytes(b"Encoder")
                    os.utime(other, ns=(info.st_atime_ns, info.st_mtime_ns))
                    os.replace(other, path)
                with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                    self.verify()
                path.write_bytes(b"encoder")

    def test_size_change_and_a_new_pack_invalidate(self):
        self.verify()
        path = root("asr", self.models) / "tokens.txt"
        path.write_bytes(b"short")
        with self.assertRaisesRegex(ValueError, "size differs"):
            self.verify()
        path.write_bytes(b"a 1\nb 2\n")
        hashed = []
        self.verify(hashed=hashed)
        self.assertIn("tokens.txt", hashed)
        # A forged verdict for a different descriptor is ignored.
        roles = json.loads(self.verdict.read_text())
        roles["roles"]["asr"]["descriptor"] = "0" * 64
        self.verdict.write_text(json.dumps(roles))
        hashed.clear()
        self.verify(hashed=hashed)
        self.assertEqual(len(hashed), 5)

    def test_an_unwritable_or_corrupt_verdict_only_means_a_full_hash(self):
        self.verdict.write_text("{not json")
        hashed = []
        self.verify(hashed=hashed)
        self.assertEqual(len(hashed), 5)
        hashed.clear()
        self.verify(hashed=hashed, verdict=self.root / "missing/dir/pack-verdict.json")
        self.assertEqual(len(hashed), 5)

    # --- the weekly re-hash ---

    def job(self, now, **options):
        options.setdefault("roles", ("vad", "asr"))
        share = self.root / "share"
        if not share.exists():
            for role in ("vad", "asr"):
                (share / "model-packs" / role).mkdir(parents=True)
                shutil.copyfile(self.descriptor(role), share / "model-packs" / role / "pack.json")
        return RehashJob(self.models, self.verdict, certificate=self.cert, share=share, build_id="build-1",
                         clock=lambda: now[0], **options)

    def test_rehash_runs_weekly_in_slices_and_records_when(self):
        now = [1_000_000.0]
        job = self.job(now)
        self.assertTrue(job.due())
        model_pack.CHUNK, old = 4, model_pack.CHUNK
        self.addCleanup(setattr, model_pack, "CHUNK", old)
        rounds = 0
        while not asyncio.run(job.run_chunk(0)):
            rounds += 1
            self.assertLess(rounds, 100)
        self.assertGreater(rounds, 2, "a zero budget still makes progress, one block at a time")
        self.assertEqual(job.failures, [])
        self.assertEqual(Verdicts(self.verdict).role("asr")["rehashed"], now[0])
        self.assertFalse(job.due())
        hashed = []
        self.verify(hashed=hashed)
        self.assertEqual(hashed, [], "the re-hash also left a valid verdict")
        self.assertEqual(Verdicts(self.verdict).role("asr")["rehashed"], now[0], "a boot check keeps the date")
        now[0] += 7 * 24 * 3600
        self.assertTrue(job.due())
        self.assertTrue(asyncio.run(job().run_chunk(60)))

    def test_rehash_catches_a_file_changed_behind_the_cache_and_drops_its_verdict(self):
        now = [5_000_000.0]
        self.verify()
        path = root("asr", self.models) / "decoder_model_merged.ort"
        info = path.stat()
        data = bytearray(path.read_bytes())
        data[0] ^= 1
        path.write_bytes(bytes(data))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        # Forge the cache to match the new stat, as a stale or copied verdict would.
        forged = json.loads(self.verdict.read_text())
        changed = path.stat()
        forged["roles"]["asr"]["files"]["decoder_model_merged.ort"].update(
            inode=changed.st_ino, mtime_ns=changed.st_mtime_ns, ctime_ns=changed.st_ctime_ns)
        self.verdict.write_text(json.dumps(forged))
        self.verify()      # the forged verdict hides it at boot ...
        job = self.job(now)
        self.assertTrue(asyncio.run(job.run_chunk(60)))
        self.assertEqual([failure["role"] for failure in job.failures], ["asr"])
        self.assertEqual(Verdicts(self.verdict).role("asr"), {})
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.verify()  # ... and the next boot hashes and refuses it.

    def hide_a_damaged_file_behind_the_cache(self):
        """Bit rot under a valid-looking verdict: the stat key matches, the bytes do not."""
        self.verify()
        path = root("asr", self.models) / "decoder_model_merged.ort"
        info = path.stat()
        data = bytearray(path.read_bytes())
        data[0] ^= 1
        path.write_bytes(bytes(data))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        forged = json.loads(self.verdict.read_text())
        changed = path.stat()
        forged["roles"]["asr"]["files"]["decoder_model_merged.ort"].update(
            inode=changed.st_ino, mtime_ns=changed.st_mtime_ns, ctime_ns=changed.st_ctime_ns)
        self.verdict.write_text(json.dumps(forged))
        hashed = []
        self.verify(hashed=hashed)
        self.assertEqual(hashed, [], "the cache hides it at boot")

    def test_rehash_failure_drops_the_verdict_even_when_it_cannot_be_rewritten(self):
        from unittest.mock import patch
        self.hide_a_damaged_file_behind_the_cache()
        real_replace = os.replace

        def full_disk(source, target, *args, **kwargs):
            if Path(target) == self.verdict:
                raise OSError(28, "No space left on device")
            return real_replace(source, target, *args, **kwargs)

        job = self.job([6_000_000.0])
        with patch.object(model_pack.os, "replace", full_disk):
            self.assertTrue(asyncio.run(job.run_chunk(60)))
        self.assertEqual([failure["role"] for failure in job.failures], ["asr"])
        self.assertNotIn("verdict_kept", job.failures[0])
        self.assertFalse(self.verdict.exists(), "a verdict that cannot be rewritten is removed")
        hashed = []
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.verify(hashed=hashed)
        self.assertIn("decoder_model_merged.ort", hashed, "the next start hashes in full")

    def test_rehash_says_so_when_nothing_on_mind_can_change(self):
        from unittest.mock import patch
        self.hide_a_damaged_file_behind_the_cache()
        job = self.job([6_000_000.0])
        with patch.object(Verdicts, "update", return_value=False), \
                patch.object(Path, "unlink", side_effect=OSError(30, "Read-only file system")):
            self.assertTrue(asyncio.run(job.run_chunk(60)))
        self.assertTrue(job.failures[0].get("verdict_kept"))

    def test_a_verdict_write_syncs_its_folder(self):
        from unittest.mock import patch
        synced = []
        real_open, real_fsync = os.open, os.fsync
        folders = {}

        def tracking_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                folders[fd] = Path(path)
            return fd

        def tracking_fsync(fd):
            synced.append(folders.get(fd))
            return real_fsync(fd)

        with patch.object(model_pack.os, "open", tracking_open), patch.object(model_pack.os, "fsync", tracking_fsync):
            self.assertTrue(Verdicts(self.verdict).update("asr", None))
        self.assertIn(self.verdict.parent, synced, "the rename is made durable")

    def test_rehash_job_is_a_night_shift_job(self):
        self.assertTrue(callable(model_pack.rehash_job))
        self.assertIsInstance(model_pack.rehash_job(), RehashJob)
        self.assertTrue(inspect.iscoroutinefunction(RehashJob.run_chunk))

    # --- bench and the CLI ---

    def test_bench_pack_is_self_describing_and_signed(self):
        bench = self.root / "bench-src"
        bench.mkdir()
        (bench / "q4_0.gguf").write_bytes(b"GGUF q4_0")
        (bench / "manifest.json").write_text(json.dumps({"models": [
            {"file": "q4_0.gguf", "name": "Q4_0", "sha256": sha(b"GGUF q4_0")}]}))
        stage_bench(bench, self.models, self.cert, self.key)
        pack = verify("bench", models=self.models, certificate=self.cert, verdict=None)
        self.assertFalse(pack["bound"])
        self.assertEqual([entry["path"] for entry in pack["files"]], ["manifest.json", "q4_0.gguf"])
        (self.models / "bench/q4_0.gguf").write_bytes(b"GGUF q4_1")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            verify("bench", models=self.models, certificate=self.cert, verdict=None)
        (bench / "q4_0.gguf").write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "differs from its manifest"):
            stage_bench(bench, self.root / "fresh", self.cert, self.key)

    def test_cli_root_and_verify_by_role(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(model_pack.main(["root", "--role", "tts", "--models", "/models"]), 0)
        self.assertEqual(output.getvalue().strip(), "/models/voice/piper/default")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = model_pack.main(["verify", "--role", "asr", "--models", str(self.models), "--certificate",
                                    str(self.cert), "--descriptor", str(self.descriptor("asr")), "--no-verdict"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("5 files", output.getvalue())


class ShippedDescriptors(unittest.TestCase):
    def test_every_image_role_has_a_descriptor_at_its_spec_path(self):
        dirs = {"dream": "dream", "vad": "asr", "asr": "asr/moonshine-tiny", "tts": "voice/piper/default",
                "embeddings": "embeddings/blink-8M", "speaker": "speaker"}
        self.assertEqual(set(IMAGE_ROLES), {"conversation", *dirs})
        for role, folder in dirs.items():
            with self.subTest(role=role):
                pack = model_pack.check_descriptor(json.loads((PACKS / role / "pack.json").read_bytes()), role)
                self.assertEqual(pack["dir"], folder)
                self.assertTrue(pack["license"])
                self.assertLessEqual(len((PACKS / role / "pack.json").read_bytes()), model_pack.MAX_DESCRIPTOR)
        paths = {role: {entry["path"] for entry in json.loads((PACKS / role / "pack.json").read_text())["files"]}
                 for role in dirs}
        self.assertIn("dream.gguf", paths["dream"])
        self.assertEqual(paths["vad"], {"silero_vad.onnx"})
        self.assertTrue({"encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"} <= paths["asr"])
        self.assertTrue({"en_GB-alba-medium.onnx", "en_GB-alba-medium.onnx.json", "tokens.txt"} <= paths["tts"])
        self.assertTrue(any(path.startswith("espeak-ng-data/") for path in paths["tts"]))
        self.assertTrue({"model.safetensors", "tokenizer.json"} <= paths["embeddings"])
        self.assertEqual(paths["speaker"], {"campplus.onnx"})

    def test_descriptor_sources_are_the_downloaded_files(self):
        records = {}
        for top in ("models", "voice"):
            path = REPO / "out" / top / "DOWNLOADS.json"
            if not path.is_file():
                self.skipTest("no download records in out/")
            for entry in json.loads(path.read_text())["files"]:
                records[f"{top}/{entry['name']}"] = entry
        for role in IMAGE_ROLES:
            if role == "conversation":
                continue
            for source in json.loads((PACKS / role / "pack.json").read_text())["sources"]:
                with self.subTest(role=role, source=source["file"]):
                    record = records[source["file"]]
                    self.assertEqual((record["sha256"], record["size_bytes"]), (source["sha256"], source["bytes"]))

    def test_small_descriptors_match_the_downloaded_files(self):
        if not (REPO / "out/voice/DOWNLOADS.json").is_file():
            self.skipTest("no out/voice")
        for role in ("vad", "asr", "speaker"):
            with self.subTest(role=role):
                pack = json.loads((PACKS / role / "pack.json").read_text())
                listed = {entry["path"]: (entry["bytes"], entry["sha256"]) for entry in pack["files"]}
                self.assertEqual(produce(REPO / "out", pack), listed)

    def test_post_image_stages_every_role_through_the_packager(self):
        import subprocess
        script = (REPO / "os/board/beingos/post-image.sh").read_text()
        self.assertIn("python3 -m lab.package_model", script)
        option = '${BEING_PACK_ROLES:+--roles "$BEING_PACK_ROLES"}'
        self.assertIn(option, script, "a role list only ever comes from BEING_PACK_ROLES")
        self.assertEqual(script.count("--roles"), 1)
        snippet = f'set -- {option}; printf "[%s]" "$#" "$@"'
        for value, expected in ((None, "[0]"), ("", "[0]"), ("conversation,vad", "[2][--roles][conversation,vad]")):
            with self.subTest(BEING_PACK_ROLES=value):
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
                if value is not None:
                    env["BEING_PACK_ROLES"] = value
                result = subprocess.run(["sh", "-c", snippet], capture_output=True, text=True, check=True, env=env)
                self.assertEqual(result.stdout, expected, "unset: every image role (no --roles)")
        self.assertIn('du -sm "$MODELS"', script, "models.ext4 is sized from what was staged")
        self.assertIn("model-packs", script)

    def test_a_role_list_without_the_conversation_pack_is_refused(self):
        from lab import package_model
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "models"
            with self.assertRaisesRegex(ValueError, "must include conversation"):
                package_model.stage_all(Path(temp) / "out/models", output, "c.pem", "k.pem", roles=("vad", "asr"))
            self.assertFalse(output.exists(), "nothing is staged")
            with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as stopped:
                package_model.main(["--source", str(Path(temp) / "out/models"), "--output", str(output),
                                    "--certificate", "c.pem", "--key", "k.pem", "--roles", "vad,asr,dream"])
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("must include conversation", errors.getvalue())

    def test_models_partition_leaves_room_for_ext4_metadata(self):
        """mkfs.ext4 -d fails when the data and ext4's own metadata do not fit.

        From 2 GB up mke2fs makes a 64 MB journal, a 256-byte inode for every
        16 KB, and backup superblocks with reserved descriptor blocks: about
        110 MB at 2 GB and 160 MB at 5 GB (the bench stick).
        """
        import re
        import subprocess

        def overhead_mb(total_mb):
            journal = 16 if total_mb < 1024 else 32 if total_mb < 2048 else 64 if total_mb < 16384 else 128
            return journal + total_mb * 1024 // 16 * 256 / 2**20 + 8

        for name, variable, folder in (("post-image.sh", "MODELS_MB", '"$MODELS"'),
                                       ("try-image.sh", "models_mb", '"$stage/models-root"')):
            script = (REPO / "os/board/beingos" / name).read_text()
            lines = [line.strip() for line in script.splitlines() if re.match(rf"\s*{variable}=", line)]
            self.assertEqual(len(lines), 2, name)
            self.assertIn(f"du -sm {folder}", lines[0])
            for data_mb in (508, 1910, 4800):
                with self.subTest(script=name, data_mb=data_mb):
                    snippet = "du() { printf '%s\\t.\\n' " + str(data_mb) + "; }; " + "; ".join(lines) \
                        + f'; echo "${variable}"'
                    total = int(subprocess.run(["sh", "-c", snippet], capture_output=True, text=True,
                                               check=True, env={"MODELS": "x", "stage": "y"}).stdout)
                    self.assertGreater(total - data_mb, overhead_mb(total) + 16)

    def test_unit_verifies_the_conversation_pack_with_the_verdict_cache(self):
        conf = (REPO / "os/package/being/30-model-pack.conf").read_text()
        self.assertIn("ExecCondition=/usr/bin/python3 /usr/lib/being/mind/model_pack.py verify", conf)
        self.assertEqual(model_pack.VERDICT, Path("/mind/cortex/pack-verdict.json"))
        self.assertIn("/usr/share/being/model-pack.pem", (REPO / "mind/model_pack.py").read_text())


if __name__ == "__main__":
    unittest.main()
