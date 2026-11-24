"""The piper-phonemize bindings in mind/phonemize.py, against a fake library.

The fake keeps the C API's contract: results are handles that must be freed,
a sentence's phonemes come back as an int32 pointer, an unknown voice gives a
NULL result, and espeak-ng starts once per process. Set
BEING_TEST_PHONEMIZE_LIBRARY (the built libpiper_phonemize_core) and
BEING_TEST_ESPEAK_DATA (a voice pack's espeak-ng-data) to also run the real one.
"""

import ctypes as c
import json
import os
import tempfile
import unittest
from pathlib import Path

from mind import phonemize, sherpa
from tests.test_sherpa import Fn

ENDS = ".?!"


class FakePhonemizer:
    """A stand-in for libpiper_phonemize_core.so.

    "Phonemes" are the lower-cased characters of the text, split into
    sentences after . ? or !, with the punctuation kept, as Piper keeps it.
    Voices in `voices` exist; any other gives a NULL result.
    """

    path = "fake"

    def __init__(self, version=b"1.5.9", *, voices=("en-gb-x-rp", "en-us"), init_result=22050, omit=()):
        self.version = version
        self.voices = set(voices)
        self.init_result = init_result
        self.silent = False
        self.calls = []
        self.texts = []
        self.live = {}
        self._arrays = {}
        self._next = 500
        for name in phonemize.SIGNATURES:
            if name in omit:
                continue
            method = getattr(self, "_" + name.removeprefix("PiperPhonemize"))
            setattr(self, name, Fn(self._recorded(name, method)))

    def _recorded(self, name, method):
        def call(*args):
            self.calls.append(name)
            return method(*args)
        return call

    def _GetVersionStr(self):
        return self.version

    def _Initialize(self, data_dir):
        self.data_dir = data_dir
        result, self.init_result = self.init_result, (0 if self.init_result > 0 else self.init_result)
        return result

    def _Text(self, text, voice):
        self.texts.append((text, voice))
        if voice.decode() not in self.voices:
            return None
        sentences, current = [], []
        for ch in text.decode("utf-8").lower():
            current.append(ord(ch))
            if ch in ENDS:
                sentences.append(current)
                current = []
        if current:
            sentences.append(current)
        if self.silent:
            sentences = []
        sentences.append([])     # the C API can hold an empty sentence
        self._next += 1
        self.live[self._next] = sentences
        return self._next

    def _ResultGetNumSentences(self, handle):
        return len(self.live[handle])

    def _ResultGetNumPhonemes(self, handle, index):
        return len(self.live[handle][index])

    def _ResultGetPhonemes(self, handle, index):
        points = self.live[handle][index]
        if not points:
            return None
        array = (c.c_int32 * len(points))(*points)
        self._arrays[(handle, index)] = array
        return c.cast(array, c.POINTER(c.c_int32))

    def _DestroyResult(self, handle):
        self.live.pop(handle)
        for key in [k for k in self._arrays if k[0] == handle]:
            del self._arrays[key]


def card(folder, **fields):
    data = {"dataset": "alba", "phoneme_type": "espeak", "espeak": {"voice": "en-gb-x-rp"},
            "phoneme_map": {}}
    data.update(fields)
    path = Path(folder) / "en_GB-alba-medium.onnx.json"
    path.write_text(json.dumps(data))
    return path


def text_of(sentences):
    return ["".join(map(chr, s)) for s in sentences]


class LoadTests(unittest.TestCase):
    def test_a_checked_fake_binds(self):
        fake = FakePhonemizer()
        self.assertIs(phonemize.load(fake), fake)
        self.assertEqual(phonemize.version(fake), "1.5.9")
        self.assertEqual(phonemize.available(fake), (True, None))

    def test_an_unchecked_release_is_refused(self):
        with self.assertRaisesRegex(sherpa.Unavailable, "1.6.1 has not been checked"):
            phonemize.load(FakePhonemizer(version=b"1.6.1"))
        ok, reason = phonemize.available(FakePhonemizer(version=b""))
        self.assertFalse(ok)
        self.assertIn("unknown version", reason)

    def test_a_missing_symbol_or_file_is_unavailable(self):
        with self.assertRaisesRegex(sherpa.Unavailable, "has no PiperPhonemizeText"):
            phonemize.load(FakePhonemizer(omit={"PiperPhonemizeText"}))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(sherpa.Unavailable, "is not installed"):
                phonemize.load(Path(root) / "libpiper_phonemize_core.so")

    def test_the_library_path_can_be_overridden(self):
        old = os.environ.pop("BEING_PHONEMIZE_LIBRARY", None)
        try:
            self.assertEqual(phonemize.library_path(), Path("/usr/lib/libpiper_phonemize_core.so"))
            os.environ["BEING_PHONEMIZE_LIBRARY"] = "/run/being-dev/libpiper_phonemize_core.so"
            self.assertEqual(phonemize.library_path(), Path("/run/being-dev/libpiper_phonemize_core.so"))
        finally:
            os.environ.pop("BEING_PHONEMIZE_LIBRARY", None)
            if old is not None:
                os.environ["BEING_PHONEMIZE_LIBRARY"] = old


class CardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_the_espeak_voice_comes_from_the_card(self):
        self.assertEqual(phonemize.card_settings(card(self.root)), ("en-gb-x-rp", {}))

    def test_a_card_without_an_espeak_voice_uses_piper_default(self):
        self.assertEqual(phonemize.card_settings(card(self.root, espeak={}))[0], "en-us")

    def test_cards_this_phonemizer_cannot_serve_are_unavailable(self):
        with self.assertRaisesRegex(sherpa.Unavailable, "'text' phonemes"):
            phonemize.card_settings(card(self.root, phoneme_type="text"))
        with self.assertRaisesRegex(sherpa.Unavailable, "unusual espeak voice"):
            phonemize.card_settings(card(self.root, espeak={"voice": "en; rm -rf /"}))
        with self.assertRaisesRegex(sherpa.Unavailable, "phoneme map"):
            phonemize.card_settings(card(self.root, phoneme_map={"ab": ["c"]}))
        (self.root / "bad.json").write_text("{not json")
        with self.assertRaisesRegex(sherpa.Unavailable, "could not be read"):
            phonemize.card_settings(self.root / "bad.json")
        with self.assertRaisesRegex(sherpa.Unavailable, "could not be read"):
            phonemize.card_settings(self.root / "absent.json")

    def test_a_phoneme_map_turns_one_phoneme_into_several(self):
        _, mapping = phonemize.card_settings(card(self.root, phoneme_map={"a": ["x", "y"], "b": "z"}))
        self.assertEqual(mapping, {ord("a"): [ord("x"), ord("y")], ord("b"): [ord("z")]})


class PhonemizerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.root / "espeak-ng-data"
        self.data.mkdir()

    def test_sentences_are_codepoints_with_punctuation_and_results_are_freed(self):
        fake = FakePhonemizer()
        ph = phonemize.for_voice(self.data, card(self.root), library=fake)
        self.assertEqual(ph.voice, "en-gb-x-rp")
        self.assertEqual(text_of(ph.sentences("Hello. I'm Pip?")), ["hello.", " i'm pip?"])
        self.assertEqual(ph.ipa("Here's my terminal."), ["here's my terminal."])
        self.assertEqual(fake.texts[-1], ("Here's my terminal.".encode(), b"en-gb-x-rp"))
        self.assertEqual(fake.live, {})

    def test_espeak_starts_once_with_the_first_data_folder(self):
        fake = FakePhonemizer()
        first = phonemize.Phonemizer(self.data, "en-gb-x-rp", library=fake)
        other = self.root / "other-data"
        other.mkdir()
        second = phonemize.Phonemizer(other, "en-us", library=fake)
        self.assertEqual(fake.calls.count("PiperPhonemizeInitialize"), 1)
        self.assertEqual(fake.data_dir, os.fsencode(str(self.data)))
        self.assertEqual((first.data_dir, second.data_dir), (self.data, self.data))

    def test_missing_or_unloadable_data_is_unavailable(self):
        with self.assertRaisesRegex(sherpa.Unavailable, "is missing"):
            phonemize.Phonemizer(self.root / "absent", library=FakePhonemizer())
        with self.assertRaisesRegex(sherpa.Unavailable, "would not load"):
            phonemize.Phonemizer(self.data, library=FakePhonemizer(init_result=-1))

    def test_another_initialiser_in_the_process_is_accepted(self):
        ph = phonemize.Phonemizer(self.data, library=FakePhonemizer(init_result=0))
        self.assertTrue(ph.sentences("ok"))

    def test_an_unknown_voice_fails_at_load_not_mid_reply(self):
        fake = FakePhonemizer(voices=())
        with self.assertRaisesRegex(sherpa.Unavailable, "en-gb-x-rp would not phonemize"):
            phonemize.for_voice(self.data, card(self.root), library=fake)
        with self.assertRaisesRegex(sherpa.Unavailable, "not an espeak voice name"):
            phonemize.Phonemizer(self.data, "../../etc", library=FakePhonemizer())

    def test_failures_while_speaking_raise_so_the_voice_can_use_flite(self):
        fake = FakePhonemizer()
        ph = phonemize.Phonemizer(self.data, "en-us", library=fake)
        fake.voices.clear()
        with self.assertRaises(RuntimeError):
            ph.sentences("Hello.")
        with self.assertRaises(ValueError):
            ph.sentences("   ")
        fake.voices.add("en-us")
        with self.assertRaises(ValueError):
            ph.fill(sherpa.GenerationConfig(), "\x00")
        fake.silent = True              # espeak-ng found no phonemes at all
        with self.assertRaisesRegex(RuntimeError, "nothing to say"):
            ph.fill(sherpa.GenerationConfig(), "...")
        self.assertEqual(fake.live, {})

    def test_text_is_cleaned_and_bounded(self):
        fake = FakePhonemizer()
        ph = phonemize.Phonemizer(self.data, "en-us", library=fake)
        ph.sentences("a\x00b")
        self.assertEqual(fake.texts[-1][0], b"a b")
        ph.sentences("x" * (phonemize.MAX_TEXT + 50))
        self.assertEqual(len(fake.texts[-1][0]), phonemize.MAX_TEXT)
        ph.sentences("café")
        self.assertEqual(fake.texts[-1][0], "café".encode("utf-8"))

    def test_the_phoneme_map_is_applied(self):
        ph = phonemize.Phonemizer(self.data, "en-us", mapping={ord("a"): [ord("x"), ord("y")]},
                                  library=FakePhonemizer())
        self.assertEqual(ph.ipa("ab."), ["xyb."])


class GenerationConfigTests(unittest.TestCase):
    def test_fill_flattens_sentences_into_the_sherpa_struct(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "espeak-ng-data").mkdir()
        ph = phonemize.Phonemizer(root / "espeak-ng-data", "en-us", library=FakePhonemizer())
        options = sherpa.GenerationConfig()
        keep = ph.fill(options, "Hi. Yo!")
        count = options.phoneme_codepoints_num_sentences
        self.assertEqual(count, 2)
        lengths = options.phoneme_codepoints_lens[:count]
        self.assertEqual(lengths, [3, 4])
        flat = options.phoneme_codepoints[:sum(lengths)]
        self.assertEqual("".join(map(chr, flat)), "hi. yo!")
        self.assertEqual(len(keep), 2)
        # Tokens stay unset: sherpa-onnx refuses both at once.
        self.assertFalse(options.tokens)
        self.assertEqual(options.tokens_num_sentences, 0)

    def test_pack_leaves_out_empty_sentences_and_refuses_nothing(self):
        codepoints, lengths, count = phonemize.pack([[104, 105], [], [46]])
        self.assertEqual((list(codepoints), list(lengths), count), ([104, 105, 46], [2, 1], 2))
        with self.assertRaises(ValueError):
            phonemize.pack([[], []])


@unittest.skipUnless(os.environ.get("BEING_TEST_PHONEMIZE_LIBRARY") and os.environ.get("BEING_TEST_ESPEAK_DATA"),
                     "set BEING_TEST_PHONEMIZE_LIBRARY and BEING_TEST_ESPEAK_DATA to run the real library")
class RealLibraryTests(unittest.TestCase):
    def test_alba_settings_give_piper_ipa(self):
        ph = phonemize.Phonemizer(os.environ["BEING_TEST_ESPEAK_DATA"], "en-gb-x-rp",
                                  library=os.environ["BEING_TEST_PHONEMIZE_LIBRARY"])
        self.assertEqual(ph.ipa("Here's my terminal."), ["hˈiəz maɪ tˈɜːmɪnəl."])
        self.assertEqual(ph.ipa("Sorry, I didn't catch that. Could you say it again?"),
                         ["sˈɒɹɪ, aɪ dˈɪdnt kˈætʃ ðˈæt.", "kʊd juː sˈeɪ ɪt ɐɡˈɛn?"])


if __name__ == "__main__":
    unittest.main()
