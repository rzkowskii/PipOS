"""Text to phonemes for the natural voice, through piper-phonemize and ctypes.

sherpa-onnx 2.0 and later no longer carry espeak-ng: a Piper (VITS) voice
given plain text fails with "no lexicon, and no frontend available". The
caller now phonemizes the text itself and passes the result as Unicode
codepoints in `GenerationConfig.phoneme_codepoints`, one sub-array per
sentence; sherpa-onnx then adds Piper's start, pad and end symbols and maps
each codepoint through the voice's tokens.txt.

This module binds the small C API of piper-phonemize
(`/usr/lib/libpiper_phonemize_core.so`, espeak-ng linked in statically). Its
`PiperPhonemizeText` is Piper's own phonemizer: espeak-ng IPA per clause, in
the voice named by the model card's `espeak.voice`, with the clause
punctuation (. , ? ! : ;) kept as phonemes, language-switch flags removed and
the IPA decomposed (NFD), sentence by sentence. The pronunciation data is the
`espeak-ng-data` folder of the voice pack on /models, which is read only.

espeak-ng is process-wide state: the library is initialised once with one
data folder, and every later phonemizer in the process uses that folder. The
library serialises its own calls, so a phonemizer may be used from the voice's
worker thread while another thread loads.

Standard library only. A missing or unchecked library raises
`sherpa.Unavailable` with a plain reason, so the voice keeps Flite.
"""

import ctypes as c
import json
import os
import re
import threading
from pathlib import Path

from .sherpa import Unavailable

LIBRARY = Path("/usr/lib/libpiper_phonemize_core.so")
# Releases whose C API and phonemization were checked against the voice.
CHECKED_VERSIONS = ("1.5.9",)
# Piper's own default when a model card names no espeak voice.
DEFAULT_VOICE = "en-us"
# espeak-ng voice names: "en-gb-x-rp", "en-us", "en-GB-scotland", "cmn".
VOICE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,39}")
# The longest clause text handed to espeak-ng at once (the voice trims to 600).
MAX_TEXT = 4000

_V = c.c_void_p
_I = c.c_int32
_IP = c.POINTER(c.c_int32)
# name: (restype, argtypes)
SIGNATURES = {
    "PiperPhonemizeGetVersionStr": (c.c_char_p, ()),
    "PiperPhonemizeInitialize": (_I, (c.c_char_p,)),
    "PiperPhonemizeText": (_V, (c.c_char_p, c.c_char_p)),
    "PiperPhonemizeResultGetNumSentences": (_I, (_V,)),
    "PiperPhonemizeResultGetNumPhonemes": (_I, (_V, _I)),
    "PiperPhonemizeResultGetPhonemes": (_IP, (_V, _I)),
    "PiperPhonemizeDestroyResult": (None, (_V,)),
}

_loaded = {}
_load_lock = threading.Lock()
# Set on a bound library once espeak-ng in it has been started: the data folder.
_STARTED = "_being_espeak_data"


def library_path():
    return Path(os.environ.get("BEING_PHONEMIZE_LIBRARY") or LIBRARY)


def load(library=None):
    """Return the bound library. `library` may be a path or an already loaded object.

    Raises Unavailable when the file is missing, will not load, lacks a symbol
    or reports a release that was not checked.
    """
    if library is not None and not isinstance(library, (str, os.PathLike)):
        return _bind(library, getattr(library, "path", "fake"))
    path = Path(library) if library is not None else library_path()
    with _load_lock:
        if str(path) in _loaded:
            return _loaded[str(path)]
        if not path.is_file():
            raise Unavailable(f"the pronunciation library {path} is not installed")
        try:
            handle = c.CDLL(str(path))
        except OSError as error:
            raise Unavailable(f"the pronunciation library would not load ({error})") from error
        lib = _bind(handle, path)
        _loaded[str(path)] = lib
        return lib


def _bind(lib, path):
    for name, (result, args) in SIGNATURES.items():
        try:
            function = getattr(lib, name)
        except AttributeError as error:
            raise Unavailable(f"the pronunciation library at {path} has no {name}") from error
        function.restype = result
        function.argtypes = args
    found = (lib.PiperPhonemizeGetVersionStr() or b"").decode(errors="replace").strip()
    if found.lstrip("v") not in CHECKED_VERSIONS:
        raise Unavailable(f"piper-phonemize {found or 'of unknown version'} has not been checked")
    return lib


def version(library=None):
    return (load(library).PiperPhonemizeGetVersionStr() or b"").decode(errors="replace").strip()


def _initialise(lib, data_dir):
    """Start espeak-ng once per library; return the data folder in use."""
    with _load_lock:
        started = getattr(lib, _STARTED, None)
        if started is not None:
            return started
        data_dir = Path(data_dir)
        if not data_dir.is_dir():
            raise Unavailable(f"the pronunciation data {data_dir} is missing")
        # The first call returns the sample rate, a later one 0, a failure -1.
        if int(lib.PiperPhonemizeInitialize(os.fsencode(str(data_dir)))) < 0:
            raise Unavailable(f"the pronunciation data in {data_dir} would not load")
        setattr(lib, _STARTED, data_dir)
        return data_dir


def card_settings(card):
    """(espeak voice, phoneme map) from a Piper model card (the .onnx.json).

    Raises Unavailable for a card that cannot be read or a voice that does not
    use espeak phonemes, since this phonemizer could not speak it correctly.
    """
    try:
        data = json.loads(Path(card).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Unavailable(f"the voice card {card} could not be read ({error})") from error
    if not isinstance(data, dict):
        raise Unavailable(f"the voice card {card} is not a JSON object")
    kind = data.get("phoneme_type", "espeak")
    if kind != "espeak":
        raise Unavailable(f"the voice uses {kind!r} phonemes, which this voice cannot read")
    espeak = data.get("espeak")
    voice = espeak.get("voice") if isinstance(espeak, dict) else None
    if voice is None:
        voice = DEFAULT_VOICE
    if not isinstance(voice, str) or not VOICE_NAME.fullmatch(voice):
        raise Unavailable(f"the voice card names an unusual espeak voice ({voice!r})")
    return voice, phoneme_map(data.get("phoneme_map"))


def phoneme_map(raw):
    """Piper's optional phoneme map: one codepoint to a list of codepoints."""
    mapping = {}
    if not raw:
        return mapping
    if not isinstance(raw, dict):
        raise Unavailable("the voice card's phoneme map is not a mapping")
    for source, targets in raw.items():
        if isinstance(targets, str):
            targets = [targets]
        if (not isinstance(source, str) or len(source) != 1 or not isinstance(targets, list)
                or not all(isinstance(t, str) and len(t) == 1 for t in targets)):
            raise Unavailable("the voice card's phoneme map has an entry that is not single phonemes")
        mapping[ord(source)] = [ord(t) for t in targets]
    return mapping


def pack(sentences):
    """Flatten sentences of codepoints for GenerationConfig.

    Returns (codepoints, lengths, count): two ctypes int32 arrays and the
    number of sentences. Empty sentences are left out, because sherpa-onnx
    refuses a zero length. The arrays must outlive the generate call.
    """
    kept = [list(sentence) for sentence in sentences if sentence]
    flat = [point for sentence in kept for point in sentence]
    if not flat:
        raise ValueError("there are no phonemes to say")
    codepoints = (c.c_int32 * len(flat))(*flat)
    lengths = (c.c_int32 * len(kept))(*map(len, kept))
    return codepoints, lengths, len(kept)


def fill(options, sentences):
    """Point a sherpa GenerationConfig at the sentences; return what to keep alive."""
    codepoints, lengths, count = pack(sentences)
    options.phoneme_codepoints = c.cast(codepoints, _IP)
    options.phoneme_codepoints_lens = c.cast(lengths, _IP)
    options.phoneme_codepoints_num_sentences = count
    return codepoints, lengths


class Phonemizer:
    """Piper's espeak-ng phonemization for one voice."""

    def __init__(self, data_dir, voice=DEFAULT_VOICE, *, mapping=None, library=None):
        if not isinstance(voice, str) or not VOICE_NAME.fullmatch(voice):
            raise Unavailable(f"{voice!r} is not an espeak voice name")
        self.lib = load(library)
        self.voice = voice
        self.mapping = dict(mapping or {})
        self.data_dir = _initialise(self.lib, data_dir)
        # A voice the data does not have fails here, at load, not mid-reply.
        try:
            probe = self.sentences("Hello.")
        except RuntimeError as error:
            raise Unavailable(f"the espeak voice {voice} would not phonemize ({error})") from error
        if not probe:
            raise Unavailable(f"the espeak voice {voice} gave no phonemes")

    def sentences(self, text):
        """Codepoint lists, one per sentence, for `text`. Blocking but quick.

        Raises ValueError for empty text and RuntimeError when espeak-ng fails.
        """
        text = str(text).replace("\x00", " ").strip()
        if not text:
            raise ValueError("there is nothing to say")
        raw = text[:MAX_TEXT].encode("utf-8", errors="replace")
        result = self.lib.PiperPhonemizeText(raw, self.voice.encode("ascii"))
        if not result:
            raise RuntimeError(f"espeak-ng could not phonemize this text in {self.voice}")
        try:
            out = []
            for index in range(int(self.lib.PiperPhonemizeResultGetNumSentences(result))):
                count = int(self.lib.PiperPhonemizeResultGetNumPhonemes(result, index))
                points = self.lib.PiperPhonemizeResultGetPhonemes(result, index) if count > 0 else None
                if not points:
                    continue
                sentence = self._mapped(points[:count])
                if sentence:
                    out.append(sentence)
            return out
        finally:
            self.lib.PiperPhonemizeDestroyResult(result)

    def _mapped(self, points):
        if not self.mapping:
            return [int(p) for p in points]
        out = []
        for point in points:
            out.extend(self.mapping.get(int(point), (int(point),)))
        return out

    def ipa(self, text):
        """The phonemes as strings, one per sentence (for logs and the diagnostics)."""
        return ["".join(map(chr, sentence)) for sentence in self.sentences(text)]

    def fill(self, options, text):
        """Phonemize `text` into a sherpa GenerationConfig; return what to keep alive.

        The caller passes an empty text to the generate call; the arrays
        returned here must stay referenced until that call returns.
        """
        sentences = self.sentences(text)
        if not sentences:
            raise RuntimeError("espeak-ng found nothing to say in this text")
        return fill(options, sentences)


def for_voice(data_dir, card, *, library=None):
    """The phonemizer a Piper voice needs, from its data folder and model card."""
    voice, mapping = card_settings(card)
    return Phonemizer(data_dir, voice, mapping=mapping, library=library)


def available(library=None):
    """(ok, reason) without starting espeak-ng: the library is present and checked."""
    try:
        load(library)
    except Unavailable as error:
        return False, error.reason
    return True, None
