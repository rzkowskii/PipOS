"""Build a bounded Piper WAV bank for Pip's exact product-authored clauses.

This is an offline build tool.  It never discovers text from conversations:
the only accepted clauses are ``mind.tts.STOCK_LINES``.  Output is bound to
the actual runtime, Piper inputs, and checked speaking-rate profile.  A staged
descriptor proves byte identity only; it is not a substitute for the runtime's
signed-pack verification.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import wave

from mind import model_pack, phonemize, sherpa
from mind.model_pack import MAX_DESCRIPTOR, check_descriptor, metadata_bytes
from mind.profile import load_profile
from mind.stock_speech_bank import signed_runtime_voice
from mind.tts import STOCK_LINES, WavCache
from mind.tts_sherpa import PiperEngine


KIND = "pipos-stock-speech-bank-v1"
MAX_BANK_BYTES = len(STOCK_LINES) * WavCache.MAX_CLIP
EXPECTED_CLAUSES = 8


def _regular(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"expected a regular file: {path}")
    return path, info


def _digest(path):
    path, _ = _regular(path)
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _data_tree(path, records=None):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("eSpeak data is not a regular directory tree")
    identity = []
    for source in sorted(path.rglob("*")):
        if source.is_dir() and not source.is_symlink():
            continue
        source, info = _regular(source)
        relative = source.relative_to(path).as_posix()
        actual = _digest(source)
        if records is not None:
            suffix = "/espeak-ng-data/" + relative
            candidates = [item for name, item in records.items()
                          if name == "espeak-ng-data/" + relative or name.endswith(suffix)]
            if len(candidates) != 1 or candidates[0]["bytes"] != info.st_size or candidates[0]["sha256"] != actual:
                raise ValueError("eSpeak data differs from the descriptor")
        identity.append({"path": relative, "bytes": info.st_size, "sha256": actual})
    if not identity:
        raise ValueError("eSpeak data tree is empty")
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return {"files": len(identity), "sha256": hashlib.sha256(raw).hexdigest()}


def _descriptor(path, files):
    """Validate a local descriptor and bind the direct Piper files to it."""
    raw = metadata_bytes(path, MAX_DESCRIPTOR)
    try:
        checked = check_descriptor(json.loads(raw), "tts")
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
        raise ValueError(f"invalid TTS descriptor: {error}") from error
    records = {item["path"]: item for item in checked["files"]}
    bound = {}
    for role in ("model", "card", "tokens"):
        source, info = _regular(files[role])
        candidates = [item for name, item in records.items() if name == source.name or name.endswith("/" + source.name)]
        if len(candidates) != 1:
            raise ValueError(f"descriptor does not identify exactly one {role} file")
        item = candidates[0]
        actual = _digest(source)
        if item["bytes"] != info.st_size or item["sha256"] != actual:
            raise ValueError(f"{role} differs from the descriptor")
        bound[role] = {"bytes": info.st_size, "sha256": actual}
    bound["espeak_data"] = _data_tree(files["data"], records)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}, bound


def _wav(path):
    source, info = _regular(path)
    if not 44 < info.st_size <= WavCache.MAX_CLIP:
        raise ValueError("Piper produced an empty or oversized stock clip")
    with wave.open(str(source), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or not 8000 <= wav.getframerate() <= 48000:
            raise ValueError("Piper stock output is not bounded mono 16-bit PCM")
        frames, rate = wav.getnframes(), wav.getframerate()
        if not frames or len(wav.readframes(frames + 1)) != frames * 2:
            raise ValueError("Piper stock output has inconsistent PCM length")
    return info.st_size, frames, rate, _digest(source)


def _verify_staged_voice(voice_dir, descriptor, certificate):
    parts = Path(model_pack.ROLE_DIRS["tts"]).parts
    voice_dir = voice_dir.resolve(strict=True)
    if voice_dir.parts[-len(parts):] != parts:
        raise ValueError("a signed staged voice must use the normal TTS role directory")
    models = voice_dir.parents[len(parts) - 1]
    model_pack.verify_envelope(models, voice_dir / model_pack.envelope_name("tts"),
                               certificate, descriptor, "tts")


def build(args):
    if len(STOCK_LINES) != EXPECTED_CLAUSES or any(not isinstance(x, str) or not x.strip() for x in STOCK_LINES):
        raise ValueError("the reviewed stock-clause set changed; review and update the bank format first")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("choose a new output directory")
    profile = load_profile(args.factory_config, args.profile_dir, strict=True)
    engine = PiperEngine(args.voice_dir, library=args.library,
                         phonemizer_library=args.phonemizer_library, threads=1)
    missing = engine.missing()
    if missing:
        raise ValueError("shipping Piper inputs are incomplete")
    files = engine.files()
    runtime = {"sherpa_library_sha256": _digest(args.library),
               "sherpa_versions": sherpa.version(args.library),
               "phonemizer_library_sha256": _digest(args.phonemizer_library),
               "phonemizer_version": phonemize.version(args.phonemizer_library)}
    if args.certificate is not None and args.descriptor is None:
        raise ValueError("a staged signature check requires --descriptor and --certificate")
    trusted, reason = signed_runtime_voice(engine)
    descriptor_binding = None
    direct = None
    if args.certificate is not None:
        _verify_staged_voice(args.voice_dir, args.descriptor, args.certificate)
        descriptor_binding, direct = _descriptor(args.descriptor, files)
        trust = "offline_signed_tts_verified"
    elif trusted:
        trust = "runtime_signed_pack_verified"
        direct = {role: {"bytes": _regular(files[role])[1].st_size, "sha256": _digest(files[role])}
                  for role in ("model", "card", "tokens")}
        direct["espeak_data"] = _data_tree(files["data"])
    elif args.descriptor is not None:
        descriptor_binding, direct = _descriptor(args.descriptor, files)
        trust = "local_descriptor_digest_only"
    else:
        raise ValueError(f"voice pack is not runtime-verified ({reason}); pass its checked local descriptor")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stock-speech-", dir=args.output.parent) as scratch_name:
        scratch = Path(scratch_name)
        clips = scratch / "clips"
        clips.mkdir()
        entries = []
        total = 0
        # frozenset has no stable order; the text hash gives reproducible order and names.
        for text in sorted(STOCK_LINES, key=lambda value: hashlib.sha256(value.encode()).digest()):
            text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            name = text_sha + ".wav"
            target = clips / name
            engine.synthesize_blocking(text, target, profile.rate)
            size, frames, sample_rate, wav_sha = _wav(target)
            total += size
            if total > MAX_BANK_BYTES:
                raise ValueError("stock bank exceeds its aggregate bound")
            entries.append({"text_sha256": text_sha, "file": "clips/" + name, "bytes": size,
                            "sha256": wav_sha, "frames": frames, "sample_rate": sample_rate})
        manifest = {
            "kind": KIND,
            "clauses": len(entries),
            "rate": profile.rate,
            "voice_label": engine.label(),
            "trust": trust,
            "descriptor": descriptor_binding,
            "runtime": runtime,
            "voice_files": direct,
            "entries": entries,
        }
        (scratch / "bank.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                                            encoding="utf-8")
        os.chmod(scratch / "bank.json", 0o644)
        os.chmod(clips, 0o755)
        os.chmod(scratch, 0o755)
        # The temporary directory is on the destination filesystem; rename is atomic.
        scratch.rename(args.output)
    return len(entries), total, trust


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-dir", required=True, type=Path)
    parser.add_argument("--library", required=True, type=Path, help="shipping libsherpa-onnx C API")
    parser.add_argument("--phonemizer-library", required=True, type=Path)
    parser.add_argument("--factory-config", required=True, type=Path)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--descriptor", type=Path, help="optional local TTS pack descriptor binding")
    parser.add_argument("--certificate", type=Path, help="trusted signer certificate for a staged TTS pack")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        count, size, trust = build(args)
    except (OSError, ValueError, RuntimeError, wave.Error) as error:
        parser.exit(1, f"Could not build stock speech: {error}\n")
    print(f"Built {count} stock clips ({size} bytes; {trust}) in {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
