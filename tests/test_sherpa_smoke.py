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
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lab import sherpa_avx_smoke as smoke
from mind import sherpa
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
        for name in ("en_GB-alba-medium.onnx", "en_GB-alba-medium.onnx.json", "tokens.txt"):
            add(archive, f"vp/{name}", b"x")
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
                             "models/piper/en_GB-alba-medium.onnx", "models/piper/espeak-ng-data/voices/en",
                             "models/piper/espeak-ng-data/en_dict", "py/mind/sherpa.py", "py/guest.py"):
                self.assertTrue((media / relative).is_file(), relative)
            self.assertEqual((media / "py/mind/sherpa.py").read_bytes(), smoke.BINDINGS.read_bytes())

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

    def run_stage(self, stage, fake):
        with tempfile.TemporaryDirectory() as assets, tempfile.TemporaryDirectory() as media:
            smoke.prepare_media(assets, media, fake_assets(assets))
            maps = Path(media) / "maps"
            maps.write_text(f"7f00 r-xp 0 0:0 1 {smoke.GUEST_MEDIA}/runtime/libonnxruntime.so\n")
            script = smoke.GUEST_SCRIPT.replace('"/proc/self/maps"', repr(str(maps)))
            argv = ["guest.py", media, stage] + ([smoke.SENTENCE] if stage == "piper" else [])
            real_load = sherpa.load
            with (patch.object(sys, "argv", argv), patch.object(sherpa, "load", lambda library=None: real_load(fake)),
                  patch.object(sherpa.OfflineTts, "write", lambda self, text, path, length_scale=1.0: 1.5)
                  if stage == "piper" else nullcontext(),
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
        piper = self.run_stage("piper", FakeLibrary())
        self.assertTrue(piper["passed"], piper)
        self.assertEqual(piper["versions"]["onnxruntime"], "1.28.2")

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

    def test_piper_command_quotes_the_sentence(self):
        self.assertTrue(smoke.guest_command("piper").endswith(f"piper '{smoke.SENTENCE}'"))
        self.assertNotIn("'", smoke.SENTENCE)
        self.assertNotIn("'", smoke.guest_command("vad"))


if __name__ == "__main__":
    unittest.main()
