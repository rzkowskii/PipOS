"""The organs' check before loading a pack: the voice, the ears and the embedder.

Nothing on /models is loaded unless its role's signed pack verifies and
lists it. A pack that does not verify means Flite, PocketSphinx or words-only
recall, with a log line; a development copy elsewhere is left alone.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import asr_sherpa, model_pack, sherpa, voice_engines
from mind.tts_sherpa import PiperEngine


def pack(role, *paths):
    return {"format": model_pack.FORMAT_V2, "role": role, "dir": model_pack.ROLE_DIRS[role],
            "files": [{"path": path, "bytes": 1, "sha256": "0" * 64} for path in paths]}


class TrustCase(unittest.TestCase):
    def setUp(self):
        self.models = Path(self.enterContext(tempfile.TemporaryDirectory())) / "models"
        self.models.mkdir()
        model_pack.forget_trust()
        self.addCleanup(model_pack.forget_trust)
        self.calls = []

    def verifier(self, result):
        def verify(role, *, models):
            self.calls.append((role, Path(models)))
            if isinstance(result, Exception):
                raise result
            return result[role] if isinstance(result, dict) and role in result else result
        return verify


class Trusted(TrustCase):
    def test_listed_files_of_a_verified_pack_may_be_loaded(self):
        base = model_pack.root("embeddings", self.models)
        verify = self.verifier(pack("embeddings", "model.safetensors", "tokenizer.json"))
        ok = model_pack.trusted("embeddings", [base / "model.safetensors", base / "tokenizer.json"],
                                models=self.models, verifier=verify)
        self.assertEqual(ok, (True, None))
        model_pack.trusted("embeddings", [base / "tokenizer.json"], models=self.models, verifier=verify)
        self.assertEqual(len(self.calls), 1, "one check per pack per process")

    def test_a_pack_that_does_not_verify_is_refused_and_says_why(self):
        base = model_pack.root("tts", self.models)
        ok, reason = model_pack.trusted("tts", [base / "voice.onnx"], models=self.models,
                                        verifier=self.verifier(ValueError("model SHA-256 differs")))
        self.assertFalse(ok)
        self.assertIn("SHA-256 differs", reason)
        missing = FileNotFoundError(2, "No such file", str(base / "beingos-tts.cms"))
        model_pack.forget_trust()
        ok, reason = model_pack.trusted("tts", [base / "voice.onnx"], models=self.models,
                                        verifier=self.verifier(missing))
        self.assertEqual((ok, reason), (False, f"missing {base / 'beingos-tts.cms'}"))

    def test_a_refusal_is_checked_again_later_but_not_at_once(self):
        base = model_pack.root("tts", self.models)
        results = [ValueError("openssl timed out"), pack("tts", "voice.onnx")]

        def verify(role, *, models):
            self.calls.append(role)
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        clock = [1000.0]
        with patch.object(model_pack.time, "monotonic", lambda: clock[0]):
            self.assertFalse(model_pack.trusted("tts", [base / "voice.onnx"], models=self.models, verifier=verify)[0])
            clock[0] += 5
            self.assertFalse(model_pack.trusted("tts", [base / "voice.onnx"], models=self.models, verifier=verify)[0])
            self.assertEqual(len(self.calls), 1, "a refusal is not re-hashed on every load")
            clock[0] += model_pack.TRUST_RETRY_S
            self.assertEqual(model_pack.trusted("tts", [base / "voice.onnx"], models=self.models, verifier=verify),
                             (True, None))
            clock[0] += 10 * model_pack.TRUST_RETRY_S
            model_pack.trusted("tts", [base / "voice.onnx"], models=self.models, verifier=verify)
        self.assertEqual(len(self.calls), 2, "a verified pack is never checked again in the same process")

    def test_a_file_the_signed_pack_does_not_list_is_refused(self):
        base = model_pack.root("asr", self.models)
        verify = self.verifier(pack("asr", "encoder_model.ort", "tokens.txt"))
        ok, reason = model_pack.trusted("asr", [base / "encoder_model.ort", base / "planted.ort"],
                                        models=self.models, verifier=verify)
        self.assertFalse(ok)
        self.assertIn("planted.ort", reason)

    def test_a_folder_counts_when_the_pack_lists_files_inside_it(self):
        base = model_pack.root("tts", self.models)
        verify = self.verifier(pack("tts", "voice.onnx", "espeak-ng-data/en_dict"))
        self.assertTrue(model_pack.trusted("tts", [base / "espeak-ng-data"], models=self.models,
                                           verifier=verify)[0])
        self.assertFalse(model_pack.trusted("tts", [base / "other-data"], models=self.models,
                                            verifier=verify)[0])

    def test_a_file_outside_the_roles_folder_but_on_models_is_refused_unchecked(self):
        stray = model_pack.root("vad", self.models) / "moonshine-base" / "encoder_model.ort"
        ok, reason = model_pack.trusted("asr", [stray], models=self.models, verifier=self.verifier(ValueError()))
        self.assertFalse(ok)
        self.assertIn("not part of the asr pack", reason)
        self.assertEqual(self.calls, [])

    def test_a_development_copy_off_models_needs_no_pack(self):
        elsewhere = self.models.parent / "checkout" / "model.safetensors"
        self.assertFalse(model_pack.on_models([elsewhere], self.models))
        self.assertTrue(model_pack.on_models([model_pack.root("embeddings", self.models)], self.models))
        self.assertEqual(model_pack.trusted("embeddings", [elsewhere], models=self.models,
                                            verifier=self.verifier(ValueError())), (True, None))
        self.assertEqual(self.calls, [])


class Organs(TrustCase):
    """Each organ asks before loading, and falls back with a log line."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(model_pack, "MODELS", self.models))

    def test_an_unverified_natural_voice_is_never_loaded_and_flite_speaks(self):
        voice = model_pack.root("tts", self.models)
        voice.mkdir(parents=True)
        (voice / "en_GB-alba-medium.onnx").write_bytes(b"x")
        (voice / "tokens.txt").write_text("a 1\n")
        (voice / "espeak-ng-data").mkdir()
        loaded = []
        engine = PiperEngine(voice, library=False, loader=None)
        engine._loader = engine._load
        with patch.object(model_pack, "verify", self.verifier(ValueError("signature does not verify"))), \
                patch.object(sherpa, "OfflineTts", side_effect=lambda *a, **k: loaded.append(a)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(engine.verified(), (False, "signature does not verify"))
            with self.assertRaises(sherpa.Unavailable):
                engine.resident()
            chosen, note = voice_engines.choose_tts("piper", state_dir=self.models.parent, engine=engine)
        self.assertEqual(loaded, [], "nothing unverified reaches the speech library")
        self.assertIn("did not verify", err.getvalue())
        self.assertEqual(chosen.name, "flite")
        self.assertIn("model pack did not verify", note)

    def test_unverified_ears_keep_pocketsphinx(self):
        root = model_pack.root("vad", self.models)
        folder = asr_sherpa.moonshine_dir(root)
        folder.mkdir(parents=True)
        (root / asr_sherpa.VAD_FILE).write_bytes(b"x")
        for name in asr_sherpa.MOONSHINE_FILES:
            (folder / name).write_bytes(b"x")
        library = self.models.parent / "libsherpa.so"
        library.write_bytes(b"x")
        good = {"vad": pack("vad", asr_sherpa.VAD_FILE), "asr": pack("asr", *asr_sherpa.MOONSHINE_FILES)}
        with patch.object(model_pack, "verify", self.verifier(good)):
            self.assertEqual(asr_sherpa.verified(root), (True, None))
        model_pack.forget_trust()
        wake = object()
        with patch.object(model_pack, "verify", self.verifier(ValueError("descriptor differs"))), \
                patch.object(asr_sherpa, "load_vad", side_effect=AssertionError("loaded")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(sherpa.Unavailable):
                asr_sherpa.SherpaRecognizer.load(wake, root=root, library=library)
            engine, note = voice_engines.choose_asr(wake, "moonshine", state_dir=self.models.parent, root=root,
                                                    library=library)
        self.assertIs(engine, wake)
        self.assertIn("did not verify", note)
        self.assertIn("keeping PocketSphinx", err.getvalue())

    def test_moonshine_base_is_in_no_pack_so_it_is_refused_on_models(self):
        root = model_pack.root("vad", self.models)
        with patch.object(model_pack, "verify", self.verifier(pack("vad", asr_sherpa.VAD_FILE))):
            ok, reason = asr_sherpa.verified(root, "base")
        self.assertFalse(ok)
        self.assertIn("not part of the asr pack", reason)


if __name__ == "__main__":
    unittest.main()
