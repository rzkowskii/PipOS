"""Build a blinded, offline Piper expressiveness audition.

This tool only accepts BeingOS's authenticated production Piper pack and
phonemizer frontend.  It synthesizes a fixed set of public product-neutral
phrases with small, documented VITS control arms.  It never changes Voice's
engine or settings and it never plays audio.

Run as ``python3 -m lab.piper_mood_audition OUTPUT`` from the BeingOS tree.
OUTPUT is a new private share directory containing only ``blind.json`` and
``clips/``.  The held-back key is written beside it as ``OUTPUT.key.json`` and
must not be copied to the rater.  Each phrase/arm pair is synthesized once:
ratings are a small preference audition, not evidence of parameter causality,
variance, emotion recognition, identity, or a production improvement.
"""

import argparse
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import stat
import tempfile
import wave

from mind import phonemize, sherpa
from mind.stock_speech_bank import signed_runtime_voice
from mind.tts import WavCache
from mind.tts_sherpa import DEFAULT_VOICE, PiperEngine


SCHEMA = 1
KIND = "pipos-piper-mood-audition-v1"
MAX_CLIPS = 24
MAX_TOTAL = MAX_CLIPS * WavCache.MAX_CLIP
MAX_MANIFEST = 64 * 1024
PHRASES = (
    ("welcome", "Hello. It's good to see you."),
    ("helpful", "I found the note, and I can read it when you're ready."),
    ("uncertain", "I'm not certain yet, but I can check that carefully."),
    ("goodnight", "Good night. I'll be here when you open the lid."),
)
# Small fixed arms around the production controls.  ``length_scale`` uses
# GenerateWithConfig, exactly as Voice does for speaking rate.  Noise controls
# belong to the model-wide VITS config and therefore get a fresh handle for
# each distinct noise pair; the model-wide length default remains 1.0 so pace
# is not accidentally applied twice.
ARMS = (
    ("baseline", 0.667, 0.800, 1.00),
    ("shorter", 0.667, 0.800, 0.94),
    ("longer", 0.667, 0.800, 1.06),
    ("less_variation", 0.560, 0.680, 1.00),
    ("more_variation", 0.740, 0.900, 1.00),
    ("bright_candidate", 0.720, 0.860, 0.96),
)


def _digest(path):
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _regular_digest(path):
    path = Path(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError("Piper input is not a non-empty regular file")
    return {"bytes": info.st_size, "sha256": _digest(path)}


def _identity(path):
    info = Path(path).stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _asset_identities(files, library, phonemizer_library):
    """Content-free pathname receipt before and after the one-shot run.

    The signed production pack remains the authority.  This receipt catches a
    normal replacement or write while the lab tool has it open; it does not
    make an attacker-controlled writable staging tree immutable.
    """
    fixed = {name: _identity(path) for name, path in (
        ("voice_dir", Path(files["model"]).parent),
        ("model", files["model"]), ("card", files["card"]),
        ("tokens", files["tokens"]), ("frontend_root", files["data"]),
        ("sherpa_library", library), ("phonemizer_library", phonemizer_library),
    )}
    root = Path(files["data"])
    tree = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        tree.append((path.relative_to(root).as_posix(), _identity(path)))
    if not tree:
        raise ValueError("Piper frontend data is empty")
    return fixed, tree


def _tree_digest(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Piper frontend data is not a regular directory")
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        item = _regular_digest(path)
        item["path"] = path.relative_to(root).as_posix()
        rows.append(item)
    if not rows:
        raise ValueError("Piper frontend data is empty")
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {"files": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}


def _wav(path):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or not 44 < info.st_size <= WavCache.MAX_CLIP:
        raise ValueError("Piper produced an empty or oversized clip")
    with wave.open(str(path), "rb") as source:
        frames, rate = source.getnframes(), source.getframerate()
        if (source.getnchannels(), source.getsampwidth()) != (1, 2) or not 8000 <= rate <= 48000:
            raise ValueError("Piper output is not bounded mono 16-bit PCM")
        if frames <= 0 or len(source.readframes(frames + 1)) != frames * 2:
            raise ValueError("Piper output has inconsistent PCM length")
    return {"bytes": info.st_size, "frames": frames, "sample_rate": rate,
            "seconds": round(frames / rate, 6), "sha256": _digest(path)}


def _write_json(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    if len(raw) > MAX_MANIFEST:
        raise ValueError("audition manifest exceeds its bound")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("audition manifest write stopped")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seed(value):
    if value is None:
        return secrets.token_hex(32)
    if len(value) != 64:
        raise ValueError("--seed must be exactly 64 hexadecimal characters")
    bytes.fromhex(value)
    return value.lower()


def build(output, voice_dir, library, phonemizer_library, seed):
    output = Path(output)
    parent = output.parent.resolve(strict=True)
    if parent.is_relative_to("/mind"):
        raise ValueError("use a disposable lab output outside /mind")
    if not output.name or output.name in (".", ".."):
        raise ValueError("choose a named output directory")
    output = parent / output.name
    key_path = output.with_name(output.name + ".key.json")
    if output.exists() or output.is_symlink() or key_path.exists() or key_path.is_symlink():
        raise ValueError("choose new share and key output names")
    parent_info = parent.stat()
    if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o077:
        raise ValueError("audition output requires an owner-only parent directory")

    engine = PiperEngine(voice_dir, library=library, phonemizer_library=phonemizer_library, threads=1)
    if engine.missing():
        raise ValueError("the production Piper pack or frontend is incomplete")
    trusted, reason = signed_runtime_voice(engine)
    if not trusted:
        raise ValueError(f"the production Piper pack did not authenticate ({reason})")
    files = engine.files()
    asset_receipt = _asset_identities(files, library, phonemizer_library)
    runtime = {
        "sherpa_library": _regular_digest(library),
        "sherpa_versions": sherpa.version(library),
        "phonemizer_library": _regular_digest(phonemizer_library),
        "phonemizer_version": phonemize.version(phonemizer_library),
        "model": _regular_digest(files["model"]),
        "card": _regular_digest(files["card"]),
        "tokens": _regular_digest(files["tokens"]),
        "frontend_data": _tree_digest(files["data"]),
        "voice_label": engine.label(),
        "trust": "runtime_signed_pack_verified",
    }
    seed = _seed(seed)
    order = [(phrase_id, text, arm) for phrase_id, text in PHRASES for arm in ARMS]
    if len(order) != MAX_CLIPS:
        raise RuntimeError("the reviewed audition matrix changed")
    random.Random(int(seed, 16)).shuffle(order)

    scratch_name = tempfile.mkdtemp(prefix=".piper-mood-", dir=parent)
    scratch = Path(scratch_name)
    os.chmod(scratch, 0o700)
    try:
        clips = scratch / "clips"
        clips.mkdir(mode=0o700)
        blind_items, key_items = [], []
        total = 0
        handles = {}
        try:
            frontend = phonemize.for_voice(files["data"], files["card"], library=phonemizer_library)
            for index, (phrase_id, text, arm) in enumerate(order, 1):
                arm_name, noise, noise_w, length = arm
                handle_key = (noise, noise_w)
                tts = handles.get(handle_key)
                if tts is None:
                    tts = sherpa.OfflineTts(files["model"], files["tokens"], files["data"], threads=1,
                                            library=library, phonemizer=frontend, noise_scale=noise,
                                            noise_scale_w=noise_w, length_scale=1.0)
                    handles[handle_key] = tts
                blind_id = f"clip-{index:02d}"
                relative = f"clips/{blind_id}.wav"
                target = scratch / relative
                tts.write(text, target, length_scale=length)
                facts = _wav(target)
                total += facts["bytes"]
                if total > MAX_TOTAL:
                    raise ValueError("audition clips exceed their aggregate bound")
                # Durations and byte sizes belong only in the held-back key:
                # showing them in the rating packet would unnecessarily hint
                # which pace arm made a clip.
                blind_items.append({"id": blind_id, "phrase_id": phrase_id, "phrase": text,
                                    "file": relative, "sha256": facts["sha256"], "rating": None})
                key_items.append({"id": blind_id, "phrase_id": phrase_id, "arm": arm_name,
                                  "noise_scale": noise, "noise_scale_w": noise_w,
                                  "length_scale": length, **facts})
        finally:
            for tts in handles.values():
                with suppress(Exception):
                    tts.close()
        if _asset_identities(files, library, phonemizer_library) != asset_receipt:
            raise ValueError("authenticated Piper assets changed during the audition")

        common = {"kind": KIND, "schema": SCHEMA, "clips": len(blind_items),
                  "phrases": len(PHRASES), "arms": len(ARMS)}
        _write_json(scratch / "blind.json", {**common, "items": blind_items,
                                              "rating": {"minimum": 1, "maximum": 5,
                                                         "instruction": ("Rate naturalness and fit from 1 to 5 "
                                                                         "without opening key.json.")}})
        _write_json(scratch / "key.json", {**common, "seed": seed, "runtime": runtime,
                                            "arms": [{"name": name, "noise_scale": noise,
                                                      "noise_scale_w": noise_w, "length_scale": length}
                                                     for name, noise, noise_w, length in ARMS],
                                            "items": key_items,
                                            "reproducibility": ("The seed reproduces blind assignment and the fixed recipe. "
                                                                "The sherpa C API exposes no synthesis RNG seed, so clips "
                                                                "are not claimed byte-reproducible."),
                                            "interpretation": ("There is one stochastic draw per phrase and arm. "
                                                               "Ratings measure preference for these clips only; they do "
                                                               "not isolate a control or qualify a live mood mapping."),
                                            "path_trust": ("The exact authenticated production pack and frontend paths "
                                                           "were checked before and after loading. This detects ordinary "
                                                           "changes but does not qualify copied writable staging as immutable."),
                                            "qualification": "owner_blind_a_b_and_native_timing_required"})
        # Reserve the share name with mkdir: unlike rename(2), this cannot
        # replace a concurrently-created empty directory.  The key is then
        # hard-linked beside the share with no-replace semantics; it is never
        # present under the directory a rater receives.
        os.mkdir(output, mode=0o700)
        published = output.stat(follow_symlinks=False)
        key_identity = None
        try:
            os.rename(scratch / "clips", output / "clips")
            os.rename(scratch / "blind.json", output / "blind.json")
            key_identity = _identity(scratch / "key.json")[:2]
            os.link(scratch / "key.json", key_path, follow_symlinks=False)
            share_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(share_fd)
            finally:
                os.close(share_fd)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            if key_identity is not None:
                with suppress(OSError):
                    if _identity(key_path)[:2] == key_identity:
                        key_path.unlink()
            with suppress(OSError):
                current = output.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (published.st_dev, published.st_ino):
                    shutil.rmtree(output)
            raise
        shutil.rmtree(scratch)
        scratch = None
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path,
                        help="new private share directory; held-back key is written beside it as OUTPUT.key.json")
    parser.add_argument("--voice-dir", type=Path, default=DEFAULT_VOICE)
    parser.add_argument("--library", type=Path, default=sherpa.LIBRARY)
    parser.add_argument("--phonemizer-library", type=Path, default=phonemize.LIBRARY)
    parser.add_argument("--seed", help="optional 64-hex shuffle seed; a random seed is recorded in key.json otherwise")
    args = parser.parse_args(argv)
    build(args.output, args.voice_dir, args.library, args.phonemizer_library, args.seed)


if __name__ == "__main__":
    main()
