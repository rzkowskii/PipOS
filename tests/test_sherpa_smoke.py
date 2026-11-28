"""Host checks for lab/sherpa_avx_smoke.py: staging, the guest script and the verdicts.

The QEMU run itself needs a lab image and is not run here.
"""

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
import wave
from array import array
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lab import sherpa_avx_smoke as smoke
from mind import phonemize, sherpa
from tests.test_phonemize import FakePhonemizer
from tests.test_sherpa import FakeLibrary

ROOT = Path(__file__).resolve().parents[1]


def add(archive, name, data=b"", directory=False):
    info = tarfile.TarInfo(name)
    if directory:
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
    else:
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def wav_bytes(seconds=1.0, rate=24000):
    """Like the Moonshine pack's test_wavs/0.wav, which is 24 kHz mono 16-bit."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(array("h", ((9000 if (i // 60) % 2 else -9000) if i < int(0.75 * rate) else 0
                                    for i in range(int(seconds * rate)))).tobytes())
    return buffer.getvalue()


def pin(path, entry):
    data = path.read_bytes()
    return {**entry, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def fake_assets(root, *, evil=False):
    root = Path(root)
    (root / "sherpa-onnx/v2.0.3").mkdir(parents=True)
    runtime = root / "sherpa-onnx/v2.0.3/runtime.tar.bz2"
    with tarfile.open(runtime, "w:bz2") as archive:
        add(archive, "rt/lib/libsherpa-onnx-c-api.so", b"api")
        add(archive, "rt/lib/libonnxruntime.so", b"ort")
    (root / "vad").mkdir()
    (root / "vad/silero_vad.onnx").write_bytes(b"vad")
    (root / "asr").mkdir()
    moonshine = root / "asr/moonshine.tar.bz2"
    with tarfile.open(moonshine, "w:bz2") as archive:
        for name in ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"):
            add(archive, f"ms/{name}", name.encode())
        add(archive, "ms/test_wavs/0.wav", wav_bytes())
    (root / "tts").mkdir()
    piper = root / "tts/piper.tar.bz2"
    with tarfile.open(piper, "w:bz2") as archive:
        for name in ("en_GB-alba-medium.onnx", "tokens.txt"):
            add(archive, f"vp/{name}", b"x")
        add(archive, "vp/en_GB-alba-medium.onnx.json", b'{"espeak": {"voice": "en-gb-x-rp"}}')
        add(archive, "vp/espeak-ng-data", directory=True)
        add(archive, "vp/espeak-ng-data/voices", directory=True)
        add(archive, "vp/espeak-ng-data/voices/en", b"voice")
        add(archive, "vp/espeak-ng-data/en_dict", b"dict")
        if evil:
            add(archive, "vp/espeak-ng-data/../../escape", b"evil")
    real = json.loads(smoke.MANIFEST.read_text())["assets"]
    paths = [runtime, root / "vad/silero_vad.onnx", moonshine, piper]
    prefixes = ["rt", None, "ms", "vp"]
    assets = []
    for entry, path, prefix in zip(real, paths, prefixes):
        entry = {**entry, "path": str(path.relative_to(root))}
        if prefix:
            entry["prefix"] = prefix
        assets.append(pin(path, entry))
    return {**json.loads(smoke.MANIFEST.read_text()), "assets": assets}


class ManifestTests(unittest.TestCase):
    def test_manifest_pins_match_the_download_record(self):
        manifest = json.loads(smoke.MANIFEST.read_text())
        self.assertEqual(manifest["runtime_release"], "v2.0.3")
        self.assertEqual(len(manifest["assets"]), 4)
        downloads = ROOT / "out/voice/DOWNLOADS.json"
        if not downloads.is_file():
            self.skipTest("no download record in this checkout")
        smoke.check_manifest(manifest, downloads)

    def test_a_mismatched_pin_is_refused(self):
        manifest = json.loads(smoke.MANIFEST.read_text())
        manifest["assets"][1] = {**manifest["assets"][1], "sha256": "0" * 64}
        downloads = ROOT / "out/voice/DOWNLOADS.json"
        if not downloads.is_file():
            self.skipTest("no download record in this checkout")
        with self.assertRaises(ValueError):
            smoke.check_manifest(manifest, downloads)


class PrepareTests(unittest.TestCase):
    def test_staging_lays_out_runtime_models_bindings_and_recording(self):
        with tempfile.TemporaryDirectory() as assets, tempfile.TemporaryDirectory() as media:
            manifest = fake_assets(assets)
            smoke.prepare_media(assets, media, manifest)
            media = Path(media)
            for relative in ("runtime/libsherpa-onnx-c-api.so", "runtime/libonnxruntime.so", "models/silero_vad.onnx",
                             "models/moonshine-tiny/encoder_model.ort", "models/moonshine-tiny/decoder_model_merged.ort",
                             "models/moonshine-tiny/tokens.txt", "fixtures/0.wav", "models/piper/tokens.txt",
                             "models/piper/en_GB-alba-medium.onnx", "models/piper/en_GB-alba-medium.onnx.json",
                             "models/piper/espeak-ng-data/voices/en", "models/piper/espeak-ng-data/en_dict",
                             "py/mind/sherpa.py", "py/mind/phonemize.py", "py/guest.py"):
                self.assertTrue((media / relative).is_file(), relative)
            self.assertEqual((media / "py/mind/sherpa.py").read_bytes(), smoke.BINDINGS.read_bytes())
            self.assertEqual((media / "py/mind/phonemize.py").read_bytes(), smoke.PHONEMIZER.read_bytes())

    def test_unverified_and_unsafe_assets_are_refused(self):
        with tempfile.TemporaryDirectory() as assets, tempfile.TemporaryDirectory() as media:
            manifest = fake_assets(assets)
            (Path(assets) / "vad/silero_vad.onnx").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "unverified"):
                smoke.prepare_media(assets, media, manifest)
        with tempfile.TemporaryDirectory() as assets, tempfile.TemporaryDirectory() as media:
            manifest = fake_assets(assets, evil=True)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                smoke.prepare_media(assets, media, manifest)
            self.assertFalse((Path(media).parent / "escape").exists())


class GuestScriptTests(unittest.TestCase):
    """The guest script, run on the host against the fake library."""

    def run_stage(self, stage, fake, pronunciation=None):
        with tempfile.TemporaryDirectory() as assets, tempfile.TemporaryDirectory() as media:
            smoke.prepare_media(assets, media, fake_assets(assets))
            maps = Path(media) / "maps"
            maps.write_text(f"7f00 r-xp 0 0:0 1 {smoke.GUEST_MEDIA}/runtime/libonnxruntime.so\n")
            script = smoke.GUEST_SCRIPT.replace('"/proc/self/maps"', repr(str(maps)))
            argv = ["guest.py", media, stage] + ([smoke.SENTENCE] if stage == "piper" else [])
            real_load = sherpa.load
            load_phonemizer = phonemize.load
            with (patch.object(sys, "argv", argv), patch.object(sherpa, "load", lambda library=None: real_load(fake)),
                  patch.object(phonemize, "load", lambda library=None: load_phonemizer(pronunciation)),
                  redirect_stdout(io.StringIO()) as out):
                exec(compile(script, "guest.py", "exec"), {"__name__": "__main__"})
        return smoke.judge(stage, 0, out.getvalue())

    def test_each_stage_reports_and_passes(self):
        vad = self.run_stage("vad", FakeLibrary())
        self.assertTrue(vad["passed"], vad)
        self.assertGreaterEqual(vad["segments"], 1)
        # Silero gets 16 kHz: the 1 s recording is resampled, not misread as 1.5 s.
        self.assertEqual(vad["audio_seconds"], 1.0)
        fake = FakeLibrary(transcript="after early nightfall")
        moonshine = self.run_stage("moonshine", fake)
        self.assertTrue(moonshine["passed"], moonshine)
        self.assertEqual(moonshine["text"], "after early nightfall")
        # The 24 kHz recording goes to the recognizer at its own rate.
        self.assertEqual((fake.accept_rates, moonshine["sample_rate"], moonshine["audio_seconds"]),
                         ([24000], 24000, 1.0))

    def test_piper_renders_phonemes_from_the_bundled_voice_card(self):
        fake, pronunciation = FakeLibrary(), FakePhonemizer()
        piper = self.run_stage("piper", fake, pronunciation)
        self.assertTrue(piper["passed"], piper)
        self.assertEqual(piper["versions"]["onnxruntime"], "1.28.2")
        self.assertEqual(pronunciation.texts[-1], (smoke.SENTENCE.encode(), b"en-gb-x-rp"))
        raw_text, lengths, spoken = fake.configs["phonemes"][0]
        self.assertEqual(raw_text, b"")
        self.assertEqual(len(lengths), 2)
        self.assertEqual(spoken, smoke.SENTENCE.lower())

    def test_empty_transcript_fails(self):
        self.assertFalse(self.run_stage("moonshine", FakeLibrary(transcript=""))["passed"])


class VerdictTests(unittest.TestCase):
    def test_sigill_is_named(self):
        result = smoke.judge("piper", 132, "Illegal instruction (core dumped)\n")
        self.assertEqual((result["sigill"], result["passed"]), (True, False))
        self.assertIn("Illegal instruction", result["log"])

    def test_a_runtime_loaded_from_elsewhere_fails(self):
        line = smoke.MARK + json.dumps({"stage": "vad", "segments": 2, "runtime_paths": ["/usr/lib/libonnxruntime.so"]})
        result = smoke.judge("vad", 0, line)
        self.assertFalse(result["passed"])
        self.assertIn("different", result["error"])

    def test_missing_report_fails(self):
        self.assertFalse(smoke.judge("vad", 0, "Traceback ...")["passed"])

    def report(self, **changes):
        stages = [{"stage": stage, "passed": True, "sigill": False, "status": 0, "log": "/home/someone/x",
                   "versions": {"sherpa_onnx": "2.0.3", "onnxruntime": "1.28.2"}} for stage in smoke.STAGES]
        report = {"image": "/home/someone/out/images/beingos-ivybridge.img", "qemu_cpu": "IvyBridge",
                  "stages": stages, "passed": True, "sigill": False,
                  "runtime_sha256": {"libsherpa-onnx-c-api.so": hashlib.sha256(b"api").hexdigest(),
                                     "libonnxruntime.so": hashlib.sha256(b"ort").hexdigest()}}
        report.update(changes)
        return report

    def test_the_shippable_verdict_has_the_outcome_and_pins_and_no_host_paths(self):
        verdict = smoke.shippable_verdict(self.report())
        self.assertEqual(verdict["source"], "qemu-ivybridge")
        self.assertEqual(verdict["versions"], {"sherpa_onnx": "2.0.3", "onnxruntime": "1.28.2"})
        self.assertEqual([stage["stage"] for stage in verdict["stages"]], list(smoke.STAGES))
        self.assertNotIn("/home", json.dumps(verdict))
        self.assertTrue(verdict["passed"])

    def test_the_voice_accepts_the_verdict_for_the_bytes_it_tested(self):
        from mind import voice_engines

        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "libsherpa-onnx-c-api.so").write_bytes(b"api")
            (folder / "libonnxruntime.so").write_bytes(b"ort")
            path = folder / "sherpa-avx-smoke.json"
            path.write_text(json.dumps(smoke.shippable_verdict(self.report())))
            library = folder / "libsherpa-onnx-c-api.so"
            self.assertEqual(voice_engines.sherpa_smoke_passed(paths=[path], library=library), (True, None))
            # A stripped library is other bytes: no pass.
            (folder / "libonnxruntime.so").write_bytes(b"ort, stripped")
            self.assertFalse(voice_engines.sherpa_smoke_passed(paths=[path], library=library)[0])

    def test_an_incomplete_or_failed_run_is_no_pass(self):
        for report in (self.report(passed=False), self.report(stages=self.report()["stages"][:1]),
                       self.report(stages=[]), {"error": "boot failed"}):
            self.assertFalse(smoke.shippable_verdict(report)["passed"])
        self.assertTrue(smoke.shippable_verdict(self.report(stages=[{**self.report()["stages"][1], "status": 132,
                                                           "sigill": True, "passed": False}]))["sigill"])

    def test_both_images_keep_the_tested_libraries_unstripped(self):
        for name in ("beingos_laptop_defconfig", "beingos_x86_64_defconfig"):
            lines = (ROOT / "os/configs" / name).read_text().splitlines()
            excluded = [line for line in lines if line.startswith("BR2_STRIP_EXCLUDE_FILES=")]
            self.assertEqual(len(excluded), 1, name)
            self.assertEqual(set(excluded[0].split("=", 1)[1].strip('"').split()), set(smoke.RUNTIME_FILES), name)

    def test_other_runtime_bytes_can_be_tested_instead(self):
        with tempfile.TemporaryDirectory() as assets, tempfile.TemporaryDirectory() as media, \
                tempfile.TemporaryDirectory() as runtime:
            smoke.prepare_media(assets, media, fake_assets(assets))
            for name in smoke.RUNTIME_FILES:
                (Path(runtime) / name).write_bytes(b"installed " + name.encode())
            smoke.use_runtime(media, runtime)
            self.assertEqual((Path(media) / "runtime/libonnxruntime.so").read_bytes(), b"installed libonnxruntime.so")
            (Path(runtime) / "libonnxruntime.so").unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                smoke.use_runtime(media, runtime)

    def test_piper_command_quotes_the_sentence(self):
        self.assertTrue(smoke.guest_command("piper").endswith(f"piper '{smoke.SENTENCE}'"))
        self.assertNotIn("'", smoke.SENTENCE)
        self.assertNotIn("'", smoke.guest_command("vad"))


class FakeVM:
    """Answers the commands main() sends the guest, like an Ivy Bridge lab image would."""

    def __init__(self, media, stage_status=None, login_error=None, **_):
        self.media = Path(media.split(":", 2)[2])
        self.stage_status = stage_status or {}
        self.login_error = login_error
        self.destroyed = False

    def start(self):
        pass

    def login(self, timeout):
        if self.login_error:
            raise self.login_error

    def destroy(self):
        self.destroyed = True

    def run(self, command, timeout):
        if command == "uname -m":
            return 0, "x86_64"
        if "/proc/cpuinfo" in command:
            return 0, "flags\t: fpu sse4_2 avx f16c aes"
        if "sha256sum" in command:
            return 0, "\n".join(f"{smoke.file_sha256(self.media / 'runtime' / name)}  {name}"
                                for name in smoke.RUNTIME_FILES)
        for stage in smoke.STAGES:
            if command == smoke.guest_command(stage):
                status = self.stage_status.get(stage, 0)
                if status:
                    return status, "Illegal instruction"
                report = {"stage": stage, "segments": 1, "text": "hello", "audio_seconds": 1.5,
                          "runtime_paths": [f"{smoke.GUEST_MEDIA}/runtime/libonnxruntime.so"],
                          "versions": {"sherpa_onnx": "2.0.3", "onnxruntime": "1.28.2"}}
                return 0, smoke.MARK + json.dumps(report)
        return 0, ""


class MainTests(unittest.TestCase):
    """main() end to end with a fake VM: the shippable verdict is always written."""

    def run_main(self, **vm_options):
        with tempfile.TemporaryDirectory() as assets, tempfile.TemporaryDirectory() as out:
            manifest = Path(out) / "manifest.json"
            manifest.write_text(json.dumps(fake_assets(assets)))
            image = Path(out) / "lab.img"
            image.write_bytes(b"image")
            verdict_path = Path(out) / "verdict.json"
            argv = ["sherpa_avx_smoke", "--assets", assets, "--image", str(image),
                    "--output", str(Path(out) / "report.json"), "--verdict", str(verdict_path)]
            vms = []

            def make_vm(**kwargs):
                vms.append(FakeVM(kwargs["update_media"], **vm_options))
                return vms[-1]

            error = None
            with patch.object(smoke, "MANIFEST", manifest), patch.object(smoke, "VM", make_vm), \
                    patch.object(sys, "argv", argv), patch.object(smoke.signal, "signal"), \
                    patch.object(smoke.signal, "setitimer"), redirect_stdout(io.StringIO()):
                try:
                    smoke.main()
                except BaseException as caught:  # SystemExit or the run's own error
                    error = caught
            self.assertTrue(vms and vms[0].destroyed)
            return error, json.loads(verdict_path.read_text())

    def test_a_passing_run_writes_a_passing_verdict(self):
        error, verdict = self.run_main()
        self.assertIsNone(error)
        self.assertTrue(verdict["passed"])
        self.assertFalse(verdict["sigill"])
        self.assertEqual(set(verdict["runtime_sha256"]), set(smoke.RUNTIME_FILES))
        self.assertNotIn("/tmp", json.dumps(verdict))

    def test_a_sigill_stage_writes_a_failed_verdict_naming_it(self):
        error, verdict = self.run_main(stage_status={"moonshine": smoke.SIGILL_STATUS})
        self.assertIsInstance(error, SystemExit)
        self.assertFalse(verdict["passed"])
        self.assertTrue(verdict["sigill"])

    def test_a_failed_boot_keeps_its_own_error_and_writes_a_failed_verdict(self):
        error, verdict = self.run_main(login_error=TimeoutError("no login prompt"))
        self.assertIsInstance(error, TimeoutError)
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["stages"], [])


if __name__ == "__main__":
    unittest.main()
