"""Sentence vectors for paraphrase-aware recall, in the standard library.

blink-8M is a static model: every WordPiece token has one 256-number row, and
a sentence is the mean of its tokens' rows, scaled to unit length. Nothing
here runs a neural network, so a sentence costs well under a millisecond of
one core and the face and voice never notice.

The tokenizer reproduces the model's `tokenizer.json` exactly: the BERT
normaliser (clean text, Chinese characters spaced, accents stripped, lower
case), the BERT pre-tokeniser (whitespace, then each punctuation mark on its
own) and greedy longest-match WordPiece with `##` continuations. Unknown
tokens are dropped before pooling, as the model's own encoder does.

The 30 MB table is memory-mapped, not read: only the rows a sentence touches
are paged in, and they are shared with the page cache.
"""

import json
import math
import mmap
import os
import struct
import sys
import threading
import unicodedata
from array import array
from pathlib import Path

MODEL_NAME = "blink-8M"
DEVICE_DIR = Path("/models/embeddings/blink-8M")
DIM = 256
MAX_TOKENS = 512  # the model's encoder truncates here
MAX_WORD_CHARS = 100
UNK = "[UNK]"
PREFIX = "##"
_SUMPROD = getattr(math, "sumprod", None)

# Rust's char::is_whitespace (the Unicode White_Space property).
_RUST_WHITESPACE = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0        "
    "        　"
)
_CJK = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF), (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F), (0x2B920, 0x2CEAF), (0xF900, 0xFAFF), (0x2F800, 0x2FA1F),
)
_ASCII_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


class Unavailable(Exception):
    """The model files are missing or malformed; recall stays word-based."""


def _is_cjk(cp):
    return any(low <= cp <= high for low, high in _CJK)


def _is_punctuation(char):
    return char in _ASCII_PUNCT or unicodedata.category(char).startswith("P")


def normalise(text):
    """The BERT normaliser as blink-8M configures it."""
    out = []
    for char in text:
        cp = ord(char)
        if cp == 0 or cp == 0xFFFD:
            continue
        if char not in "\t\n\r" and unicodedata.category(char).startswith("C"):
            continue
        if char in "\t\n\r" or char in _RUST_WHITESPACE:
            out.append(" ")
        elif _is_cjk(cp):
            out.append(f" {char} ")
        else:
            out.append(char)
    text = "".join(out)
    if not text.isascii():
        text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    return text.lower()


def pre_tokenise(text):
    """Split on whitespace, then put each punctuation mark in a word of its own."""
    words = []
    current = []
    for char in text:
        if char in _RUST_WHITESPACE:
            if current:
                words.append("".join(current))
                current = []
        elif _is_punctuation(char):
            if current:
                words.append("".join(current))
                current = []
            words.append(char)
        else:
            current.append(char)
    if current:
        words.append("".join(current))
    return words


class Embedder:
    """One loaded model. Thread-safe: it holds no per-call state."""

    def __init__(self, model_dir):
        model_dir = Path(model_dir)
        try:
            spec = json.loads((model_dir / "tokenizer.json").read_text(encoding="utf-8"))
            model = spec["model"]
            if model.get("type") != "WordPiece":
                raise Unavailable("the tokenizer is not WordPiece")
            self.vocab = {str(token): int(index) for token, index in model["vocab"].items()}
            self.prefix = model.get("continuing_subword_prefix", PREFIX)
            self.max_word = int(model.get("max_input_chars_per_word", MAX_WORD_CHARS))
            unk = model.get("unk_token", UNK)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise Unavailable(f"tokenizer: {exc}") from None
        self.unk = unk
        self.unk_id = self.vocab.get(unk)
        self.special = tuple(
            entry["content"] for entry in spec.get("added_tokens", ())
            if isinstance(entry, dict) and isinstance(entry.get("content"), str) and entry.get("content")
        )
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._open_table(model_dir / "model.safetensors")
        self.model_id = f"{MODEL_NAME}:{self.rows}x{self.dim}"

    def _open_table(self, path):
        try:
            with open(path, "rb") as source:
                size = os.fstat(source.fileno()).st_size
                header_len, = struct.unpack("<Q", source.read(8))
                if not 0 < header_len <= 1 << 20 or 8 + header_len > size:
                    raise Unavailable("safetensors header is out of range")
                header = json.loads(source.read(header_len))
                self._map = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError, struct.error) as exc:
            raise Unavailable(f"weights: {exc}") from None
        tensors = {name: value for name, value in header.items() if name != "__metadata__"}
        if len(tensors) != 1:
            raise Unavailable("expected one embedding tensor")
        (entry,) = tensors.values()
        shape, offsets = entry.get("shape"), entry.get("data_offsets")
        if entry.get("dtype") != "F32" or not isinstance(shape, list) or len(shape) != 2:
            raise Unavailable("expected a two-dimensional F32 tensor")
        rows, dim = int(shape[0]), int(shape[1])
        start, end = 8 + header_len + int(offsets[0]), 8 + header_len + int(offsets[1])
        if end - start != rows * dim * 4 or end > len(self._map) or rows < len(self.vocab):
            raise Unavailable("tensor size does not match its shape")
        if sys.byteorder != "little" or start % 4:
            # Never on x86; copy and fix rather than misread.
            table = array("f", self._map[start:end])
            if sys.byteorder != "little":
                table.byteswap()
            self._table = memoryview(table)
        else:
            self._table = memoryview(self._map)[start:end].cast("f")
        self.rows, self.dim = rows, dim

    def close(self):
        try:
            self._table.release()
        except (AttributeError, ValueError):
            pass
        try:
            self._map.close()
        except (AttributeError, ValueError, BufferError):
            pass

    # -- tokens -----------------------------------------------------------

    def _wordpiece(self, word):
        cached = self._cache.get(word)
        if cached is not None:
            return cached
        if len(word) > self.max_word:
            pieces = (self.unk_id,)
        else:
            pieces = []
            start, length = 0, len(word)
            while start < length:
                end = length
                found = None
                while start < end:
                    piece = word[start:end]
                    if start:
                        piece = self.prefix + piece
                    found = self.vocab.get(piece)
                    if found is not None:
                        break
                    end -= 1
                if found is None:
                    pieces = [self.unk_id]
                    break
                pieces.append(found)
                start = end
            pieces = tuple(pieces)
        if len(self._cache) < 50_000:
            with self._cache_lock:
                self._cache[word] = pieces
        return pieces

    def token_ids(self, text):
        """Token ids exactly as the model's tokenizer gives them, unknowns included."""
        parts = [text]
        for token in self.special:
            parts = [piece for part in parts for piece in _split_keep(part, token)]
        ids = []
        for part in parts:
            if part in self.special:
                special = self.vocab.get(part)
                if special is not None:
                    ids.append(special)
                continue
            for word in pre_tokenise(normalise(part)):
                ids.extend(self._wordpiece(word))
        return ids

    # -- vectors ----------------------------------------------------------

    def embed(self, text):
        """A unit-length vector (a list of floats). Empty when nothing is known."""
        ids = [i for i in self.token_ids(text) if i != self.unk_id][:MAX_TOKENS]
        dim, table = self.dim, self._table
        if not ids:
            return [0.0] * dim
        if len(ids) == 1:
            total = list(table[ids[0] * dim:(ids[0] + 1) * dim])
        else:
            total = [math.fsum(column) for column in zip(*(table[i * dim:(i + 1) * dim] for i in ids))]
        norm = math.sqrt(dot(total, total))
        if norm == 0.0:
            return [0.0] * dim
        return [value / norm for value in total]


def _split_keep(text, token):
    if token not in text:
        return [text]
    out = []
    pieces = text.split(token)
    for index, piece in enumerate(pieces):
        if piece:
            out.append(piece)
        if index < len(pieces) - 1:
            out.append(token)
    return out


def dot(a, b):
    if _SUMPROD is not None:
        return _SUMPROD(a, b)
    return math.fsum(x * y for x, y in zip(a, b))


def cosine(a, b):
    """Cosine of two vectors (unit vectors give their dot product)."""
    na, nb = math.sqrt(dot(a, a)), math.sqrt(dot(b, b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


# -- compact storage: int8 plus one scale ------------------------------------

def quantise(vector):
    """(scale, bytes): each number as a signed byte, the vector's peak at 127."""
    peak = max((abs(value) for value in vector), default=0.0)
    if peak == 0.0:
        return 0.0, bytes(len(vector))
    scale = peak / 127.0
    codes = array("b", (max(-127, min(127, round(value / scale))) for value in vector))
    return scale, codes.tobytes()


def dequantise(scale, blob):
    return [scale * value for value in array("b", blob)]


def codes(blob):
    """The signed bytes of a stored vector, ready for `dot`."""
    return array("b", blob)


# -- one shared model per process --------------------------------------------

_shared = {}
_shared_lock = threading.Lock()


def model_dir():
    """Where the embedding model lives, or None when it is not installed."""
    override = os.environ.get("BEING_EMBED_DIR")
    candidates = []
    if override is not None:
        if not override:
            return None  # set but empty: switched off
        candidates.append(Path(override))
    else:
        try:
            from . import model_pack

            root = getattr(model_pack, "root", None)
            if callable(root):
                found = root("embeddings")
                if found:
                    candidates.append(Path(found) / MODEL_NAME)
                    candidates.append(Path(found))
        except Exception:  # an unverifiable pack is simply not used
            pass
        candidates.append(DEVICE_DIR)
    for candidate in candidates:
        if (candidate / "model.safetensors").is_file() and (candidate / "tokenizer.json").is_file():
            return candidate
    return None


def load(directory=None):
    """The shared Embedder for `directory` (default: the installed model), or None."""
    directory = Path(directory) if directory is not None else model_dir()
    if directory is None:
        return None
    key = str(directory.resolve())
    with _shared_lock:
        if key not in _shared:
            try:
                _shared[key] = Embedder(directory)
            except Unavailable as exc:
                print(f"embed: {MODEL_NAME} unavailable: {exc}", file=sys.stderr, flush=True)
                _shared[key] = None
        return _shared[key]
