"""Read-only donor for an installed, build-time Piper stock-speech bank.

The donor never accepts arbitrary text.  Construction revalidates the active
signed voice pack and all bank provenance; ``read`` then serves bytes only for
the exact product-authored ``STOCK_LINES`` set.  Any mismatch should be treated
by Voice as an ordinary cache miss.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import wave

from . import model_pack, phonemize, sherpa
from .tts import STOCK_LINES, WavCache


KIND = "pipos-stock-speech-bank-v1"
INSTALLED = Path("/usr/share/being/stock-speech")
MAX_MANIFEST = 64 * 1024
EXPECTED_CLAUSES = 8


def signed_runtime_voice(engine):
    files = engine.files()
    try:
        root = model_pack.root("tts")
        if root.resolve(strict=True) != root or not all(
                files[role] is not None and files[role].resolve(strict=True).is_relative_to(root)
                   for role in ("model", "card", "tokens", "data")):
            return False, "voice files are outside the installed signed role"
    except OSError:
        return False, "voice files are unavailable"
    return engine.verified()


def installed_bank(engine, rate):
    """Only the current read-only system slot supplies clips to Voice."""
    try:
        info = INSTALLED.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_dev != Path("/").stat().st_dev or info.st_uid != 0
                or not os.statvfs(INSTALLED).f_flag & getattr(os, "ST_RDONLY", 1)):
            return None
        trusted, _ = signed_runtime_voice(engine)
        return StockSpeechBank(INSTALLED, engine, rate) if trusted else None
    except (AttributeError, EOFError, OSError, RuntimeError, TypeError, ValueError, wave.Error):
        return None


def _regular(path, maximum=None):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (maximum is not None and not 0 < info.st_size <= maximum):
        raise ValueError("stock speech input is not a bounded regular file")
    return path, info


def _read(path, maximum):
    path, info = _regular(path, maximum)
    with path.open("rb") as source:
        raw = source.read(maximum + 1)
    if len(raw) != info.st_size:
        raise ValueError("stock speech input changed while reading")
    return raw


def _digest(path):
    path, _ = _regular(path)
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _tree(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("stock speech eSpeak data is not a regular directory")
    identity = []
    for source in sorted(path.rglob("*")):
        if source.is_dir() and not source.is_symlink():
            continue
        source, info = _regular(source)
        identity.append({"path": source.relative_to(path).as_posix(), "bytes": info.st_size,
                         "sha256": _digest(source)})
    if not identity:
        raise ValueError("stock speech eSpeak data is empty")
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return {"files": len(identity), "sha256": hashlib.sha256(raw).hexdigest()}


def _wav(raw, entry):
    if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ValueError("stock speech clip differs from its manifest")
    with wave.open(io.BytesIO(raw), "rb") as source:
        frames = source.getnframes()
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), frames) != (
                1, 2, entry["sample_rate"], entry["frames"]):
            raise ValueError("stock speech clip PCM differs from its manifest")
        if len(source.readframes(frames + 1)) != frames * 2:
            raise ValueError("stock speech clip has inconsistent PCM length")


def _manifest(root):
    if root.is_symlink() or not root.is_dir() or (root / "clips").is_symlink():
        raise ValueError("stock speech bank is not a regular directory tree")
    raw = _read(root / "bank.json", MAX_MANIFEST)
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
        raise ValueError("unsupported stock speech bank")
    if manifest.get("clauses") != EXPECTED_CLAUSES:
        raise ValueError("stock speech bank has the wrong number of clauses")
    if manifest.get("trust") not in ("runtime_signed_pack_verified", "offline_signed_tts_verified"):
        raise ValueError("stock speech bank was not made from an authenticated TTS pack")
    return raw, manifest


def _clips(root, entries):
    allowed = {hashlib.sha256(text.encode("utf-8")).hexdigest(): text for text in STOCK_LINES}
    if len(STOCK_LINES) != EXPECTED_CLAUSES or not isinstance(entries, list) or len(entries) != EXPECTED_CLAUSES:
        raise ValueError("stock speech clause set differs")
    found = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
                "text_sha256", "file", "bytes", "sha256", "frames", "sample_rate"}:
            raise ValueError("invalid stock speech entry")
        key = entry["text_sha256"]
        if not isinstance(key, str) or key not in allowed or allowed[key] in found or entry["file"] != f"clips/{key}.wav":
            raise ValueError("stock speech entry is not a canonical clause")
        if (type(entry["bytes"]) is not int or not 44 < entry["bytes"] <= WavCache.MAX_CLIP
                or type(entry["frames"]) is not int or entry["frames"] <= 0
                or type(entry["sample_rate"]) is not int or not 8000 <= entry["sample_rate"] <= 48000
                or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64
                or any(ch not in "0123456789abcdef" for ch in entry["sha256"])):
            raise ValueError("stock speech entry is outside bounds")
        clip = root / entry["file"]
        _wav(_read(clip, WavCache.MAX_CLIP), entry)
        found[allowed[key]] = (clip, entry)
    return found


class StockSpeechBank:
    """A fully checked immutable donor for the active Piper engine and rate."""

    def __init__(self, root, engine, rate):
        self.root = Path(root)
        _, manifest = _manifest(self.root)
        if manifest.get("rate") != rate:
            raise ValueError("stock speech bank does not match the active rate")

        library = Path(engine.library) if isinstance(engine.library, (str, Path)) else sherpa.library_path()
        phonemizer = (Path(engine.phonemizer_library)
                      if isinstance(engine.phonemizer_library, (str, Path)) else phonemize.library_path())
        expected_runtime = {"sherpa_library_sha256": _digest(library),
                            "sherpa_versions": sherpa.version(library),
                            "phonemizer_library_sha256": _digest(phonemizer),
                            "phonemizer_version": phonemize.version(phonemizer)}
        if manifest.get("runtime") != expected_runtime:
            raise ValueError("stock speech bank runtime provenance differs")

        files = engine.files()
        expected_voice = {role: {"bytes": _regular(files[role])[1].st_size,
                                 "sha256": _digest(files[role])}
                          for role in ("model", "card", "tokens")}
        expected_voice["espeak_data"] = _tree(files["data"])
        if manifest.get("voice_files") != expected_voice or manifest.get("voice_label") != engine.label():
            raise ValueError("stock speech bank voice provenance differs")

        self._clips = _clips(self.root, manifest.get("entries"))

    def read(self, text):
        """Return immutable WAV bytes for one exact stock clause, else None."""
        item = self._clips.get(text) if text in STOCK_LINES else None
        if item is None:
            return None
        path, entry = item
        raw = _read(path, WavCache.MAX_CLIP)
        _wav(raw, entry)
        return raw


def stage(source, destination):
    """Copy the eight validated clips and manifest into a new image stage."""
    if os.path.lexists(destination):
        raise ValueError("expected a new stock speech image stage")
    raw, manifest = _manifest(source)
    files = _clips(source, manifest.get("entries"))
    with tempfile.TemporaryDirectory(prefix=".stock-copy-", dir=destination.parent) as directory:
        staged = Path(directory) / "bank"
        (staged / "clips").mkdir(parents=True)
        staged.chmod(0o755)
        (staged / "clips").chmod(0o755)
        for clip, entry in files.values():
            content = _read(clip, WavCache.MAX_CLIP)
            _wav(content, entry)
            target = staged / entry["file"]
            target.write_bytes(content)
            target.chmod(0o644)
        target = staged / "bank.json"
        target.write_bytes(raw)
        target.chmod(0o644)
        staged.rename(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stage an authenticated stock speech bank for a signed system image.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        stage(args.source, args.destination)
    except (EOFError, OSError, ValueError, wave.Error) as error:
        parser.exit(1, f"stock speech image stage refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
