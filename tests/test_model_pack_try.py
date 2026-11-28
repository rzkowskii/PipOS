"""Provisional trust on Try media and the background full check (cable log, 25 Nov).

A Try stick's verdict cache is empty at every boot, and hashing the 532 MB
conversation pack from its USB 2 stick held the first answer back 23.4 s. On
Try media a pack is trusted once its signature, descriptor and sizes check
out; being-model-check.service hashes every pack in full at idle priority,
and a pack that fails is refused for the rest of the boot and its organs are
restarted. Everywhere else the hash still comes first.
"""

import contextlib
import io
import json
import os
import errno
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lab.package_model import describe, stage_role
from mind import model_pack
from mind.model_pack import CHECK_ORDER, ROLE_UNITS, TRY_LABEL, Verdicts, check_media, provisional_allowed, root
from tests.test_model_pack import keypair
from tests.test_model_pack_v2 import sha

REPO = Path(__file__).resolve().parent.parent
CORTEX = REPO / "os/package/cortex"
TRY_CMDLINE = "BOOT_IMAGE=/bzImage root=PARTUUID=x ro being.try=1 being.cable=1 quiet\n"


def unit(path):
    sections, current = {}, None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), [])
        else:
            key, _, value = line.partition("=")
            current.append((key, value))
    return sections


def values(sections, section, key):
    return [value for name, value in sections.get(section, []) if name == key]


class TryMediaCase(unittest.TestCase):
    """A temporary Try boot: a command line, a read-only labelled /models,
    the background check's unit file and its /run folder."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.models = self.root / "models"
        self.models.mkdir()
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text(TRY_CMDLINE)
        self.check_dir = self.root / "run/being-model-check"
        self.check_dir.mkdir(parents=True)
        self.check_unit = self.root / "being-model-check.service"
        self.check_unit.write_text("[Unit]\n")
        self.read_only = True
        self.label = TRY_LABEL
        real_statvfs = os.statvfs

        def statvfs(path):
            result = real_statvfs(path)
            flag = (result.f_flag | os.ST_RDONLY) if self.read_only else (result.f_flag & ~os.ST_RDONLY)
            return SimpleNamespace(f_flag=flag)

        self.enterContext(patch.object(model_pack.os, "statvfs", statvfs))

    def allowed(self, role="vad", **options):
        options.setdefault("cmdline", self.cmdline)
        options.setdefault("label", lambda device: self.label)
        options.setdefault("check_dir", self.check_dir)
        options.setdefault("check_unit", self.check_unit)
        return provisional_allowed(role, self.models, **options)


class WhenProvisionalTrustApplies(TryMediaCase):
    def test_a_try_boot_with_read_only_labelled_media_and_the_check_installed(self):
        self.assertTrue(self.allowed())

    def test_never_off_try_media(self):
        cases = {
            "an installed boot": lambda: self.cmdline.write_text("root=PARTUUID=x ro quiet\n"),
            "a bench boot": lambda: self.cmdline.write_text(TRY_CMDLINE.replace("quiet", "being.bench=1")),
            "the owner asked for a full check": lambda: self.cmdline.write_text(
                TRY_CMDLINE.replace("quiet", "being.model_check=full")),
            "a writable models partition": lambda: setattr(self, "read_only", False),
            "another partition's label": lambda: setattr(self, "label", "models"),
            "no label readable": lambda: setattr(self, "label", None),
            "no background check installed": lambda: self.check_unit.unlink(),
            "the background check has not started": lambda: self.check_dir.rmdir(),
            "an unreadable command line": lambda: self.cmdline.unlink(),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.cmdline.write_text(TRY_CMDLINE)
                self.check_unit.write_text("[Unit]\n")
                self.check_dir.mkdir(exist_ok=True)
                self.read_only, self.label = True, TRY_LABEL
                self.assertTrue(self.allowed())
                change()
                self.assertFalse(self.allowed())

    def test_a_pack_refused_on_this_boot_is_never_trusted_provisionally_again(self):
        model_pack.refused_mark("vad", self.check_dir).write_text("model SHA-256 differs\n")
        self.assertFalse(self.allowed("vad"))
        self.assertTrue(self.allowed("tts"), "only the refused role")

    def test_a_similar_word_on_the_command_line_is_not_a_try_boot(self):
        self.cmdline.write_text("being.try=10 xbeing.try=1\n")
        self.assertFalse(self.allowed())


@unittest.skipUnless(shutil.which("openssl"), "needs the OpenSSL CLI")
class ProvisionalVerify(TryMediaCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.key, cls.cert = keypair(Path(temp.name), "fixture release")

    def setUp(self):
        super().setUp()
        out = self.root / "out"
        (out / "voice/vad").mkdir(parents=True)
        (out / "voice/vad/silero_vad.onnx").write_bytes(b"vad weights")
        (out / "voice/DOWNLOADS.json").write_text(json.dumps({"files": [
            {"name": "vad/silero_vad.onnx", "size_bytes": 11, "sha256": sha(b"vad weights")}]}))
        packs = self.root / "packs"
        (packs / "vad").mkdir(parents=True)
        (packs / "vad/pack.json").write_text(json.dumps({
            "format": model_pack.FORMAT_V2, "role": "vad", "dir": "asr", "model": "fixture vad",
            "license": "MIT", "files": [], "sources": [{"file": "voice/vad/silero_vad.onnx", "bytes": 11,
                                                         "sha256": sha(b"vad weights"), "path": "silero_vad.onnx"}]}))
        describe("vad", out, packs)
        self.descriptor = packs / "vad/pack.json"
        stage_role("vad", out, self.models, self.descriptor, self.cert, self.key)
        self.weights = root("vad", self.models) / "silero_vad.onnx"
        self.verdict = self.root / "mind/cortex/pack-verdict.json"
        self.verdict.parent.mkdir(parents=True)
        self.enterContext(patch.object(model_pack, "provisional_allowed",
                                       lambda role, models: self.allowed(role)))

    def verify(self, **options):
        options.setdefault("descriptor", self.descriptor)
        options.setdefault("verdict", self.verdict)
        options.setdefault("build_id", "build-1")
        return model_pack.verify("vad", models=self.models, certificate=self.cert, **options)

    def test_on_try_media_nothing_is_hashed_and_the_result_says_so(self):
        hashed = []
        pack = self.verify(hashed=hashed)
        self.assertEqual(hashed, [])
        self.assertEqual(pack["checked"], "provisional")
        self.assertEqual([name for name, _, _ in model_pack.files_of(pack)], ["silero_vad.onnx"])
        self.assertFalse(self.verdict.exists(), "a provisional check records no verdict")

    def test_the_signature_descriptor_and_sizes_are_still_checked_first(self):
        self.weights.write_bytes(b"vad weight")
        with self.assertRaisesRegex(ValueError, "size differs"):
            self.verify()
        self.weights.write_bytes(b"vad weights")
        envelope = root("vad", self.models) / model_pack.envelope_name("vad")
        good = envelope.read_bytes()
        envelope.write_bytes(good[:-1] + bytes([good[-1] ^ 1]))
        with self.assertRaises(ValueError):
            self.verify()
        envelope.write_bytes(good)
        self.weights.unlink()
        self.weights.symlink_to(self.root / "out/voice/vad/silero_vad.onnx")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.verify()

    def test_off_try_media_and_with_the_cache_off_the_hash_comes_first(self):
        self.read_only = False
        hashed = []
        self.assertEqual(self.verify(hashed=hashed)["checked"], "full")
        self.assertEqual(hashed, ["silero_vad.onnx"])
        self.read_only = True
        hashed.clear()
        self.assertEqual(self.verify(hashed=hashed, verdict=None)["checked"], "full")
        self.assertEqual(hashed, ["silero_vad.onnx"], "--no-verdict always hashes")
        hashed.clear()
        self.verdict.unlink()
        self.assertEqual(self.verify(hashed=hashed, provisional=False)["checked"], "full")
        self.assertEqual(hashed, ["silero_vad.onnx"], "--full always hashes")

    def test_the_background_check_records_a_full_verdict_that_later_checks_use(self):
        code = check_media(models=self.models, verdict=self.verdict, check_dir=self.check_dir, roles=("vad",),
                           verifier=self.background_verify, run=self.refuse_run, out=self.quiet)
        self.assertEqual(code, 0)
        hashed = []
        pack = self.verify(hashed=hashed)
        self.assertEqual((pack["checked"], hashed), ("full", []), "hashed once, in the background")
        status = model_pack.check_status(self.check_dir)
        self.assertEqual(status["roles"]["vad"]["result"], "ok")
        self.assertTrue(status["done"])

    def test_damage_the_sizes_miss_is_caught_in_the_background_and_refused_from_then_on(self):
        self.weights.write_bytes(b"VAD WEIGHTS")
        self.assertEqual(self.verify()["checked"], "provisional", "same size: trusted until the hash")
        runs = []
        code = check_media(models=self.models, verdict=self.verdict, check_dir=self.check_dir, roles=("vad",),
                           verifier=self.background_verify, run=lambda *args: runs.append(args) or True,
                           out=self.quiet)
        self.assertEqual(code, 1)
        self.assertEqual(runs, [("--no-block", "try-restart", "being-voice.service")])
        self.assertIn("SHA-256 differs", model_pack.refused_mark("vad", self.check_dir).read_text())
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.verify()
        self.assertEqual(model_pack.check_status(self.check_dir)["roles"]["vad"]["result"], "refused")

    def rehash(self):
        share = self.root / "share"
        (share / "model-packs/vad").mkdir(parents=True, exist_ok=True)
        shutil.copy(self.descriptor, share / "model-packs/vad/pack.json")
        job = model_pack.RehashJob(self.models, self.verdict, roles=("vad",), certificate=self.cert, share=share,
                                   build_id="build-1", check_dir=self.check_dir)
        self.assertTrue(job._work(60.0))
        return job

    def test_a_pack_the_night_re_hash_proves_bad_is_never_trusted_provisionally_again(self):
        # Review finding: the re-hash only dropped the verdict, and on Try
        # media a pack with no verdict is trusted on its signature and sizes.
        check_media(models=self.models, verdict=self.verdict, check_dir=self.check_dir, roles=("vad",),
                    verifier=self.background_verify, run=self.refuse_run, out=self.quiet)
        self.weights.write_bytes(b"VAD WEIGHTS")           # the stick now returns other bytes
        job = self.rehash()
        self.assertEqual([(f["role"], f.get("marked")) for f in job.failures], [("vad", True)])
        self.assertIsNone(Verdicts(self.verdict).role("vad").get("files"))
        self.assertIn("night re-hash", model_pack.refused_mark("vad", self.check_dir).read_text())
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.verify()

    def test_a_read_error_in_the_night_re_hash_drops_the_verdict_but_refuses_nothing(self):
        check_media(models=self.models, verdict=self.verdict, check_dir=self.check_dir, roles=("vad",),
                    verifier=self.background_verify, run=self.refuse_run, out=self.quiet)
        real = model_pack.regular_file

        @contextlib.contextmanager
        def failing(path):
            if path.name == "silero_vad.onnx":
                raise OSError(errno.EIO, "Input/output error", str(path))
            with real(path) as stream:
                yield stream

        with patch.object(model_pack, "regular_file", failing):
            job = self.rehash()
        self.assertEqual([(f["role"], f.get("marked")) for f in job.failures], [("vad", None)])
        self.assertFalse(model_pack.refused_mark("vad", self.check_dir).exists())
        self.assertEqual(self.verify()["checked"], "provisional")

    def run_cli(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = model_pack.main(["verify", "--role", "vad", "--models", str(self.models), "--certificate",
                                    str(self.cert), "--descriptor", str(self.descriptor), "--verdict",
                                    str(self.verdict), *extra])
        self.assertEqual(code, 0, output.getvalue())
        return output.getvalue()

    def test_the_gate_says_what_it_checked(self):
        with patch.object(model_pack, "read_build_id", lambda: "build-1"):
            line = self.run_cli()
            self.assertIn("Qualified fixture vad", line)
            self.assertIn("full hash follows in the background", line)
            line = self.run_cli("--full")
            self.assertIn("Qualified fixture vad", line)
            self.assertNotIn("background", line)
        self.assertIn("silero_vad.onnx", Verdicts(self.verdict).role("vad")["files"])

    def background_verify(self, role, **options):
        options.setdefault("descriptor", self.descriptor)
        options.setdefault("build_id", "build-1")
        return model_pack.verify(role, certificate=self.cert, **options)

    def refuse_run(self, *args):
        self.fail(f"nothing should be restarted: {args}")

    @staticmethod
    def quiet(*args, **kwargs):
        pass


class BackgroundCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.models = self.root / "models"
        self.check_dir = self.root / "run"
        self.check_dir.mkdir()
        for role in ("conversation", "tts", "embeddings"):
            base = root(role, self.models)
            base.mkdir(parents=True, exist_ok=True)
            (base / model_pack.envelope_name(role)).write_bytes(b"0")
        self.calls, self.runs, self.lines, self.sleeps, self.timeouts = [], [], [], [], []

    def check(self, failing=(), errors=None, run=None, **options):
        """`errors`: {role: [exception or None per attempt]}, None for a pass."""
        errors = {role: list(queue) for role, queue in (errors or {}).items()}

        def verifier(role, *, models, verdict, provisional):
            self.calls.append((role, provisional))
            self.timeouts.append(model_pack.OPENSSL_TIMEOUT_S)
            if role in failing:
                raise ValueError(f"model SHA-256 differs from the qualified pack ({role})")
            if errors.get(role):
                error = errors[role].pop(0)
                if error is not None:
                    raise error
            return {}

        options.setdefault("check_dir", self.check_dir)
        options.setdefault("sleep", self.sleeps.append)
        return check_media(models=self.models, verdict=self.root / "verdict.json", verifier=verifier,
                           run=run or (lambda *args: self.runs.append(args) or True), clock=lambda: 0.0,
                           out=lambda line, **_: self.lines.append(line), **options)

    def test_every_pack_present_is_hashed_in_full_in_the_order_answers_need_them(self):
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.calls, [("conversation", False), ("tts", False), ("embeddings", False)])
        self.assertEqual(self.runs, [])
        roles = model_pack.check_status(self.check_dir)["roles"]
        self.assertEqual(roles["vad"], {"result": "absent"})
        self.assertEqual(roles["tts"]["result"], "ok")
        self.assertTrue(any("tts pack passed its full check" in line for line in self.lines))

    def test_a_refused_pack_is_marked_and_its_organ_restarted_once(self):
        self.assertEqual(self.check(failing=("conversation",)), 1)
        self.assertEqual(self.runs, [("--no-block", "try-restart", "being-cortex.service")])
        self.assertTrue(model_pack.refused_mark("conversation", self.check_dir).exists())
        self.assertTrue(any(line.startswith("Model pack rejected: conversation") for line in self.lines))
        # The restarted cortex pulls the check in again: it neither re-hashes
        # nor restarts what it already refused on this boot.
        self.calls.clear()
        self.runs.clear()
        self.assertEqual(self.check(), 1)
        self.assertNotIn(("conversation", False), self.calls)
        self.assertEqual(self.runs, [])
        self.assertIn("SHA-256 differs", model_pack.check_status(self.check_dir)["roles"]["conversation"]["reason"])

    def test_without_a_place_for_the_mark_the_organs_are_stopped_not_restarted(self):
        self.assertEqual(self.check(failing=("embeddings",), check_dir=self.root / "missing"), 1)
        self.assertEqual(self.runs, [("--no-block", "stop", "being-mind.service")])

    def test_a_slow_openssl_or_a_read_error_is_tried_again_not_refused(self):
        # Review finding: an idle-priority openssl starved past its timeout,
        # or one EIO from the stick, refused a healthy pack for the whole boot
        # and restarted the cortex cold.
        timeout = subprocess.TimeoutExpired(["openssl", "cms"], 15)
        code = self.check(errors={"conversation": [timeout, None],
                                  "tts": [OSError(errno.EIO, "Input/output error", str(self.models / "x"))]})
        self.assertEqual(code, 0)
        self.assertEqual(self.runs, [], "nothing restarted")
        self.assertFalse(any(self.check_dir.glob("*.refused")), "nothing marked")
        self.assertEqual(self.sleeps, [model_pack.CHECK_RETRY_DELAYS_S[0]], "one pause, then both pass")
        self.assertEqual(self.calls, [("conversation", False), ("tts", False), ("embeddings", False),
                                      ("conversation", False), ("tts", False)])
        roles = model_pack.check_status(self.check_dir)["roles"]
        self.assertEqual({role: roles[role]["result"] for role in ("conversation", "tts")},
                         {"conversation": "ok", "tts": "ok"})
        self.assertFalse(any(line.startswith("Model pack rejected") for line in self.lines))

    def test_a_pack_that_can_never_be_checked_is_reported_unchecked_not_refused(self):
        eio = OSError(errno.EIO, "Input/output error", str(self.models / "reflex.gguf"))
        code = self.check(errors={"conversation": [eio] * 10})
        self.assertEqual(code, 0)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.sleeps, list(model_pack.CHECK_RETRY_DELAYS_S))
        entry = model_pack.check_status(self.check_dir)["roles"]["conversation"]
        self.assertEqual((entry["result"], entry["attempts"]), ("unchecked", len(model_pack.CHECK_RETRY_DELAYS_S) + 1))
        self.assertIn("Input/output error", entry["reason"])
        self.assertFalse(model_pack.refused_mark("conversation", self.check_dir).exists())
        self.assertTrue(any("could not check the conversation pack after 4 tries" in line for line in self.lines))

    def test_the_background_check_allows_openssl_longer_and_puts_the_limit_back(self):
        before = model_pack.OPENSSL_TIMEOUT_S
        self.check()
        self.assertEqual(set(self.timeouts), {model_pack.BACKGROUND_OPENSSL_TIMEOUT_S})
        self.assertGreater(model_pack.BACKGROUND_OPENSSL_TIMEOUT_S, before)
        self.assertEqual(model_pack.OPENSSL_TIMEOUT_S, before)

    def test_a_listed_file_gone_from_the_pack_still_refuses_it(self):
        missing = FileNotFoundError(errno.ENOENT, "No such file or directory", str(self.models / "reflex.gguf"))
        self.assertEqual(self.check(errors={"conversation": [missing]}), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.runs, [("--no-block", "try-restart", "being-cortex.service")])
        self.assertIn("missing", model_pack.refused_mark("conversation", self.check_dir).read_text())

    def test_what_proves_a_pack_bad_and_what_only_failed_to_check_it(self):
        models = self.models
        bad = [ValueError("model SHA-256 differs"), KeyError("files"),
               FileNotFoundError(errno.ENOENT, "gone", str(models / "asr/x.onnx")),
               OSError(errno.ELOOP, "loop", str(models / "voice")), RuntimeError("unexpected")]
        unchecked = [subprocess.TimeoutExpired(["openssl"], 15), MemoryError(),
                     OSError(errno.EIO, "Input/output error", str(models / "reflex.gguf")),
                     FileNotFoundError(errno.ENOENT, "No such file", "openssl"),
                     FileNotFoundError(errno.ENOENT, "No such file", "/usr/share/being/model-pack.pem"),
                     OSError(errno.ENOMEM, "Cannot allocate memory")]
        for error in bad:
            with self.subTest(error=repr(error)):
                self.assertTrue(model_pack.integrity_failure(error, models))
        for error in unchecked:
            with self.subTest(error=repr(error)):
                self.assertFalse(model_pack.integrity_failure(error, models))

    def test_an_organ_that_will_not_restart_is_stopped(self):
        asked = []

        def run(*args):
            asked.append(args)
            return args[1] != "try-restart"

        self.assertEqual(self.check(failing=("tts",), run=run), 1)
        self.assertEqual(asked, [("--no-block", "try-restart", "being-voice.service"),
                                 ("--no-block", "stop", "being-voice.service")])
        self.assertEqual(model_pack.check_status(self.check_dir)["roles"]["tts"]["organs"], "stopped")
        self.assertTrue(any(line.startswith("Model pack rejected: tts") and "Stopped being-voice.service" in line
                            for line in self.lines))

    def test_when_systemd_takes_neither_request_the_check_fails_and_says_so(self):
        asked = []
        code = self.check(failing=("conversation",), run=lambda *args: asked.append(args) and False)
        self.assertEqual(code, 1)
        self.assertEqual([args[1] for args in asked], ["try-restart", "stop"])
        entry = model_pack.check_status(self.check_dir)["roles"]["conversation"]
        self.assertEqual(entry["organs"], "failed")
        line = next(line for line in self.lines if line.startswith("Model pack rejected: conversation"))
        self.assertIn("Could not restart or stop being-cortex.service", line)
        self.assertNotIn("Restarted", line)

    def test_every_role_has_its_organs_named_and_each_unit_exists(self):
        self.assertEqual(set(CHECK_ORDER), set(model_pack.IMAGE_ROLES))
        self.assertEqual(CHECK_ORDER[0], "conversation")
        self.assertEqual(set(ROLE_UNITS), set(model_pack.IMAGE_ROLES))
        installed = {path.name for path in (REPO / "os/package").glob("*/*.service")}
        for role, units in ROLE_UNITS.items():
            for name in units:
                with self.subTest(role=role):
                    self.assertIn(name, installed)

    def test_the_command_line_runs_it_without_restarting_when_asked(self):
        output = io.StringIO()
        with patch.object(model_pack, "verify", lambda role, **_: {}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = model_pack.main(["check-media", "--models", str(self.models), "--verdict",
                                    str(self.root / "verdict.json"), "--status-dir", str(self.check_dir),
                                    "--no-restart"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(model_pack.check_status(self.check_dir)["done"])

    def test_the_command_line_says_it_left_the_organs_alone(self):
        output = io.StringIO()

        def bad(role, **_):
            raise ValueError("model SHA-256 differs from the qualified pack (tts)")

        with patch.object(model_pack, "verify", bad), patch.object(model_pack, "systemctl", self.fail), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = model_pack.main(["check-media", "--models", str(self.models), "--verdict",
                                    str(self.root / "verdict.json"), "--status-dir", str(self.check_dir),
                                    "--no-restart"])
        self.assertEqual(code, 1)
        self.assertIn("Left being-voice.service running, as asked.", output.getvalue())

    def test_a_missing_or_foreign_status_file_reads_as_none(self):
        self.assertIsNone(model_pack.check_status(self.check_dir))
        (self.check_dir / "status.json").write_text(json.dumps({"format": "other", "roles": {}}))
        self.assertIsNone(model_pack.check_status(self.check_dir))


class Units(unittest.TestCase):
    def test_the_check_runs_on_try_boots_only_at_idle_priority(self):
        service = unit(CORTEX / "being-model-check.service")
        conditions = values(service, "Unit", "ConditionKernelCommandLine")
        self.assertIn("being.try=1", conditions)
        self.assertIn("!being.bench=1", conditions)
        self.assertIn("!" + model_pack.FULL_CHECK_ARG, conditions)
        # Early, without waiting for basic.target (and the cable link behind it).
        self.assertEqual(values(service, "Unit", "DefaultDependencies"), ["no"])
        self.assertNotIn("basic.target", " ".join(values(service, "Unit", "After")))
        self.assertNotIn("being-cortex.service", " ".join(values(service, "Unit", "After")))
        self.assertEqual(values(service, "Unit", "RequiresMountsFor"), ["/models /mind"])
        self.assertEqual(values(service, "Unit", "Conflicts"), ["shutdown.target"])
        self.assertEqual(values(service, "Unit", "Before"), ["shutdown.target"])
        self.assertEqual(values(service, "Service", "ExecStart"),
                         ["/usr/bin/python3 /usr/lib/being/mind/model_pack.py check-media"])
        self.assertEqual(values(service, "Service", "CPUSchedulingPolicy"), ["idle"])
        self.assertEqual(values(service, "Service", "IOSchedulingClass"), ["idle"])
        self.assertEqual(values(service, "Service", "Nice"), ["19"])
        self.assertEqual(values(service, "Service", "RuntimeDirectory"), [model_pack.CHECK_DIR.name])
        self.assertEqual(values(service, "Service", "RuntimeDirectoryPreserve"), ["yes"])
        self.assertEqual(values(service, "Service", "ReadWritePaths"), ["-" + str(model_pack.VERDICT.parent)])
        self.assertEqual(values(service, "Service", "PrivateNetwork"), ["yes"])
        self.assertFalse(service.get("Install"), "pulled in by the cortex, never enabled on its own")
        self.assertEqual(model_pack.CHECK_UNIT, Path("/usr/lib/systemd/system/being-model-check.service"))

    def test_the_cortex_pulls_it_in_and_the_recipe_installs_it(self):
        cortex = unit(CORTEX / "being-cortex.service")
        self.assertIn("being-model-check.service", values(cortex, "Unit", "Wants"))
        recipe = (CORTEX / "cortex.mk").read_text()
        self.assertIn("$(CORTEX_PKGDIR)/being-model-check.service", recipe)
        self.assertIn("$(TARGET_DIR)/usr/lib/systemd/system/being-model-check.service", recipe)

    def test_the_cortex_gate_still_verifies_with_the_default_trust(self):
        # No --full: on a Try stick the gate is the quick check, and the
        # background unit finishes it; elsewhere verify hashes as before.
        conf = (REPO / "os/package/being/30-model-pack.conf").read_text()
        self.assertIn("ExecCondition=/usr/bin/python3 /usr/lib/being/mind/model_pack.py verify\n", conf)

    def test_try_media_carries_the_label_provisional_trust_needs(self):
        self.assertIn(f"-L {TRY_LABEL}", (REPO / "os/board/beingos/try-image.sh").read_text())

    def test_the_dream_model_reads_in_the_idle_io_class(self):
        dream = unit(CORTEX / "being-dream-cortex.service")
        self.assertEqual(values(dream, "Service", "IOSchedulingClass"), ["idle"])


if __name__ == "__main__":
    unittest.main()
