"""Text to phonemes for the natural voice, through piper-phonemize and ctypes.

sherpa-onnx 2.0 and later no longer carry espeak-ng: a Piper (VITS) voice
given plain text fails with "no lexicon, and no frontend available". The
caller now phonemizes the text itself and passes the result as Unicode
codepoints in `GenerationConfig.phoneme_codepoints`, one sub-array per
sentence; sherpa-onnx then adds Piper's start, pad and end symbols and maps
each codepoint through the voice's tokens.txt. A card with the optional
being_cluster_aliases field may assign a single private-use codepoint to a
declared Piper vowel cluster. Its signed token table must use the same ID.

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
`sherpa.Unavailable` with a plain reason, so the voice keeps Flite. A clause
with no phonemes in it ("...", "?!", a lone emoji) raises ValueError: there is
nothing to say, and the voice must not be silenced for it.
"""

import ctypes as c
import json
import os
import re
import threading
from pathlib import Path
from unicodedata import category

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
# Set on a bound library whose first start failed: the Unavailable to raise again.
_FAILED = "_being_espeak_failed"


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
    """Start espeak-ng once per library; return the data folder in use.

    piper-phonemize runs espeak_Initialize inside std::call_once: the first
    call returns the sample rate or -1, and every later call returns 0 without
    trying again, even after a failure. So a failed start is remembered and
    raised again, rather than a later 0 being taken for a working espeak-ng
    whose data folder it never read.
    """
    with _load_lock:
        failed = getattr(lib, _FAILED, None)
        if failed is not None:
            raise failed
        started = getattr(lib, _STARTED, None)
        if started is not None:
            return started
        data_dir = Path(data_dir)
        if not data_dir.is_dir():
            # Checked before the call, so a missing folder spends no start.
            raise Unavailable(f"the pronunciation data {data_dir} is missing")
        if int(lib.PiperPhonemizeInitialize(os.fsencode(str(data_dir)))) < 0:
            failed = Unavailable(f"the pronunciation data in {data_dir} would not load, and espeak-ng "
                                 "cannot be started again in this process; restart the voice")
            setattr(lib, _FAILED, failed)
            raise failed
        # A positive sample rate, or 0 when something else in the process
        # started it first; the probe in Phonemizer then checks it works.
        setattr(lib, _STARTED, data_dir)
        return data_dir


def _read_card(card):
    try:
        data = json.loads(Path(card).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Unavailable(f"the voice card {card} could not be read ({error})") from error
    if not isinstance(data, dict):
        raise Unavailable(f"the voice card {card} is not a JSON object")
    return data


def card_settings(card):
    """(espeak voice, scalar phoneme map) from a Piper model card."""
    return _card_settings(_read_card(card))


def _card_settings(data):
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


def _phoneme_scalar(text):
    return isinstance(text, str) and len(text) == 1 and (text == " " or text.isprintable())


def _cluster_aliases(data, tokens):
    """The card and tokens.txt must describe one identical vocabulary."""
    if "being_cluster_aliases" not in data:
        return {}
    selected = data["being_cluster_aliases"]
    clusters = data.get("vowel_clusters")
    original = data.get("phoneme_id_map")
    symbols = data.get("num_symbols")
    invalid = "the voice card has invalid cluster aliases"
    if (not isinstance(selected, dict) or not selected or not isinstance(clusters, list)
            or not isinstance(original, dict) or type(symbols) is not int or not 0 < symbols <= 4096):
        raise Unavailable(invalid)
    declared = set()
    for parts in clusters:
        if (not isinstance(parts, list) or not 2 <= len(parts) <= 8
                or not all(_phoneme_scalar(part) and category(part)[0] in "LM" for part in parts)):
            raise Unavailable(invalid)
        declared.add("".join(parts))
    if (len(declared) != len(clusters) or set(selected) != declared
            or {token for token in original if len(token) > 1} != declared):
        raise Unavailable(invalid)
    expected, aliases, ids = {}, {}, set()
    for token, indices in original.items():
        if (not isinstance(indices, list) or len(indices) != 1 or type(indices[0]) is not int
                or not 0 <= indices[0] < symbols or indices[0] in ids):
            raise Unavailable(invalid)
        index = indices[0]
        unit = token
        if token in declared:
            pair = selected[token]
            if not isinstance(pair, dict) or set(pair) != {"alias", "id"}:
                raise Unavailable(invalid)
            unit = pair["alias"]
            if (not isinstance(unit, str) or len(unit) != 1 or not 0xE000 <= ord(unit) <= 0xF8FF
                    or unit in original or type(pair["id"]) is not int or pair["id"] != index):
                raise Unavailable(invalid)
            aliases[tuple(map(ord, token))] = ord(unit)
        elif not _phoneme_scalar(unit):
            raise Unavailable(invalid)
        if unit in expected:
            raise Unavailable(invalid)
        expected[unit] = index
        ids.add(index)
    if not {"_", "^", "$"} <= expected.keys():
        raise Unavailable(invalid)
    if _token_ids(tokens) != expected:
        raise Unavailable("tokens.txt does not match the voice card's cluster aliases")
    return aliases


def _token_ids(tokens):
    """Sherpa rows are a scalar and an ID, with two spaces for its space token."""
    result = {}
    try:
        with Path(tokens).open("rb") as stream:
            table = stream.read(128 * 1024 + 1)
        if len(table) > 128 * 1024:
            raise ValueError("too large")
        for row in table.decode("utf-8").splitlines():
            unit, index = row.rsplit(" ", 1)
            if len(unit) != 1 or unit in result or not index.isascii() or not index.isdecimal():
                raise ValueError("invalid token row")
            result[unit] = int(index)
    except (OSError, ValueError) as error:
        raise Unavailable(f"tokens.txt for the voice's cluster aliases could not be read ({error})") from error
    return result


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

    def __init__(self, data_dir, voice=DEFAULT_VOICE, *, mapping=None, clusters=None, library=None):
        if not isinstance(voice, str) or not VOICE_NAME.fullmatch(voice):
            raise Unavailable(f"{voice!r} is not an espeak voice name")
        self.lib = load(library)
        self.voice = voice
        self.mapping = dict(mapping or {})
        self.clusters = dict(clusters or {})
        self._cluster_lengths = sorted({len(parts) for parts in self.clusters}, reverse=True)
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
        Text with nothing espeak-ng can say ("...", "?!", an emoji) gives [].
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
                if self.clusters:
                    sentence = self._clustered(sentence)
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

    def _clustered(self, points):
        # Apply the longest match within this sentence. Punctuation and spaces
        # are real phonemes and cannot be dropped to complete a match.
        out = []
        start = 0
        while start < len(points):
            for length in self._cluster_lengths:
                if start + length > len(points):
                    continue
                match = self.clusters.get(tuple(points[start:start + length]))
                if match is not None:
                    out.append(match)
                    start += length
                    break
            else:
                out.append(points[start])
                start += 1
        return out

    def ipa(self, text):
        """The phonemes as strings, one per sentence (for logs and the diagnostics)."""
        sentences = self.sentences(text)
        if not self.clusters:
            return ["".join(map(chr, sentence)) for sentence in sentences]
        spellings = {alias: "".join(map(chr, parts)) for parts, alias in self.clusters.items()}
        return ["".join(spellings.get(point, chr(point)) for point in sentence) for sentence in sentences]

    def fill(self, options, text):
        """Phonemize `text` into a sherpa GenerationConfig; return what to keep alive.

        The caller passes an empty text to the generate call; the arrays
        returned here must stay referenced until that call returns. Text
        with no phonemes ("...", "?!", a lone emoji) raises ValueError: it is
        a clause with nothing to say, not a voice that has stopped working.
        """
        sentences = self.sentences(text)
        if not sentences:
            raise ValueError("there is nothing to say in this text")
        return fill(options, sentences)


def for_voice(data_dir, card, *, library=None):
    """Read the voice card, and the sibling token table when it opts into aliases."""
    data = _read_card(card)
    voice, mapping = _card_settings(data)
    clusters = _cluster_aliases(data, Path(card).with_name("tokens.txt"))
    if clusters and set(clusters.values()).intersection(point for expansion in mapping.values() for point in expansion):
        raise Unavailable("the voice card's scalar phoneme map emits a cluster alias")
    return Phonemizer(data_dir, voice, mapping=mapping, clusters=clusters, library=library)


def available(library=None):
    """(ok, reason) without starting espeak-ng: the library is present and checked."""
    try:
        load(library)
    except Unavailable as error:
        return False, error.reason
    return True, None
