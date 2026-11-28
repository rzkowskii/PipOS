"""Run shipped PocketSphinx wake recognition over the synthetic lab corpus.

This is a paired-evidence adapter, not a Voice backend.  It reuses the
production Recognizer, opens only the explicitly named synthetic WAVs, feeds
every input frame, and emits one engine block for wake_candidate_compare.py.
"""

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import wave

from mind.recognition import FRAME_BYTES, FRAME_SAMPLES, RATE, Recognizer
from lab.sherpa_kws_candidate import digest, load_corpus, read_wav


VERSION = "5.0.4"
SOURCE_SHA256 = "9acd63cfdc76bbc85b7b6c6610b7a9cf30c79d5b36a31de7dc2f969e962653d7"
DEFAULT_MODEL = Path("/usr/share/pocketsphinx/model/en-us")
MAX_MODEL_FILES = 64
MAX_MODEL_FILE_BYTES = 64 * 1024 * 1024
MAX_MODEL_BYTES = 192 * 1024 * 1024
TAIL_FRAMES = 25  # 750 ms: enough to close the production 0.36/0.75 endpoint.
WAKE_WORD = re.compile(r"[a-z]+(?:[ '-][a-z]+){0,3}\Z")


def _regular(path, maximum):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path} is not a regular non-symlink file")
    size = path.stat().st_size
    if not 0 < size <= maximum:
        raise ValueError(f"{path} is outside the lab size bound")
    return size


def _model_attestation(model):
    """Digest exactly the acoustic model, dictionary, and LM configured by Recognizer."""
    if model.is_symlink() or not model.is_dir():
        raise ValueError("model must be a non-symlink directory")
    configured = (("dictionary", model / "cmudict-en-us.dict"),
                  ("language_model", model / "en-us.lm.bin"))
    records = []
    total = 0
    for role, path in configured:
        size = _regular(path, MAX_MODEL_FILE_BYTES)
        total += size
        records.append({"role": role, "bytes": size, "sha256": digest(path)})

    acoustic = model / "en-us"
    if acoustic.is_symlink() or not acoustic.is_dir():
        raise ValueError("acoustic model must be a non-symlink directory")
    members = []
    for path in sorted(acoustic.rglob("*")):
        if path.is_symlink():
            raise ValueError("acoustic model contains a symlink")
        if not path.is_file():
            continue
        if len(members) >= MAX_MODEL_FILES:
            raise ValueError("acoustic model has too many files")
        size = _regular(path, MAX_MODEL_FILE_BYTES)
        total += size
        if total > MAX_MODEL_BYTES:
            raise ValueError("configured model exceeds the lab byte bound")
        relative = path.relative_to(acoustic).as_posix()
        members.append({"path": relative, "bytes": size, "sha256": digest(path)})
    if not members:
        raise ValueError("acoustic model is empty")
    manifest = json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
    records.append({"role": "acoustic_model", "bytes": sum(x["bytes"] for x in members),
                    "sha256": hashlib.sha256(manifest).hexdigest()})
    return records


def _pcm_frame(samples):
    pcm = array("h")
    for sample in samples:
        if not math.isfinite(sample) or not -1.0 <= sample <= 1.0:
            raise ValueError("WAV contains a non-finite or out-of-range sample")
        pcm.append(max(-32768, min(32767, round(sample * 32768.0))))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


def run_case(recognizer, samples, frames):
    """Feed all source audio, then bounded silence solely to close endpointing."""
    first_trigger = None
    first_score = None
    consumed = 0
    recognizer.start_capture()
    started = time.monotonic_ns()
    try:
        for offset in range(0, frames, FRAME_SAMPLES):
            block = samples[offset:min(offset + FRAME_SAMPLES, frames)]
            actual = len(block)
            frame = _pcm_frame(block)
            if actual < FRAME_SAMPLES:
                frame += bytes(FRAME_BYTES - len(frame))
            events = recognizer.feed(frame, attending=False, oneshot=False)
            consumed += actual
            if first_trigger is None and any(topic == "hear.wake" for topic, _ in events):
                first_trigger = consumed * 1000.0 / RATE
                first_score = recognizer.wake_score

        silence = bytes(FRAME_BYTES)
        padded_frames = (frames + FRAME_SAMPLES - 1) // FRAME_SAMPLES * FRAME_SAMPLES
        for tail_index in range(1, TAIL_FRAMES + 1):
            events = recognizer.feed(silence, attending=False, oneshot=False)
            if first_trigger is None and any(topic == "hear.wake" for topic, _ in events):
                first_trigger = (padded_frames + tail_index * FRAME_SAMPLES) * 1000.0 / RATE
                first_score = recognizer.wake_score
    finally:
        elapsed = (time.monotonic_ns() - started) / 1e6
        recognizer.stop_capture()
    if consumed != frames:
        raise RuntimeError("Recognizer did not consume the complete WAV")
    return first_trigger, first_score, elapsed, consumed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    # Require the resolved packaged ELF, rather than attesting a loader name or
    # a mutable SONAME symlink while ctypes opens different bytes.
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--wake-word", default="pip")
    parser.add_argument("--corpus", required=True, type=Path)
    args = parser.parse_args(argv)
    recognizer = None
    try:
        wake_word = args.wake_word.strip().casefold()
        if not WAKE_WORD.fullmatch(wake_word):
            raise ValueError("wake word must be one to four bounded lowercase words")
        _regular(args.library, MAX_MODEL_FILE_BYTES)
        model_files = _model_attestation(args.model)
        cases, corpus_sha = load_corpus(args.corpus)
        recognizer = Recognizer(wake_word, model=args.model, library=str(args.library.resolve(strict=True)))
        inputs, results = [], []
        for case, wav, checked_sha in cases:
            samples, frames, wav_sha = read_wav(wav)
            if wav_sha != checked_sha:
                raise ValueError("WAV changed after corpus validation")
            trigger, score, elapsed, consumed = run_case(recognizer, samples, frames)
            inputs.append({"id": case["id"], "frames": frames, "sample_rate": RATE,
                           "wav_sha256": wav_sha})
            results.append({"id": case["id"], "detected": trigger is not None,
                            "trigger_audio_ms": round(trigger, 3) if trigger is not None else None,
                            "processing_ms": round(elapsed, 3), "score": score,
                            "consumed_frames": consumed})
        settings = {"wake_word": wake_word, "sample_rate": RATE,
                    "frame_samples": FRAME_SAMPLES, "kws_threshold": 1e-42,
                    "endpoint_window_seconds": 0.36, "endpoint_ratio": 0.75,
                    "endpoint_vad_mode": 2, "tail_frames": TAIL_FRAMES,
                    "configured_files": model_files}
        output = {
            "provenance": {
                "version": VERSION, "source_sha256": SOURCE_SHA256,
                "binary_sha256": digest(args.library), "corpus_sha256": corpus_sha,
                "api": "being-mind-recognition", "model_trust": "local_digest_only",
                "model_files": model_files,
                "model_pack_sha256": hashlib.sha256(json.dumps(
                    settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "effective_config": settings,
            },
            "inputs": inputs,
            "results": results,
        }
        print(json.dumps(output, sort_keys=True))
        return 0
    except (AttributeError, EOFError, OSError, ValueError, RuntimeError, UnicodeError,
            json.JSONDecodeError, wave.Error) as error:
        print(f"PocketSphinx baseline refused: {error}", file=sys.stderr)
        return 1
    finally:
        if recognizer is not None:
            recognizer.close()


if __name__ == "__main__":
    raise SystemExit(main())
