"""Measure the staged CAM++ pack on consented, locally supplied utterances."""

import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import time
import wave

from mind import model_pack, sherpa, speaker_id


DESCRIPTOR = (Path(__file__).resolve().parents[1]
              / "os/package/being/model-packs/speaker/pack.json")
KINDS = ("owner", "other", "replay", "silence")
MAX_CLIPS = 128
MAX_WAV_BYTES = 400_000


def bounded_bytes(path, limit):
    if not path.is_file():
        raise ValueError("input is not a regular file")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input exceeds its size limit")
    return data


def load_spec(path):
    spec = json.loads(bounded_bytes(path, 64 * 1024))
    if not isinstance(spec, dict) or spec.get("kind") != "pipos-speaker-clips-v1":
        raise ValueError("expected a pipos-speaker-clips-v1 manifest")
    enrollment = spec.get("enrollment")
    if not isinstance(enrollment, list) or not 5 <= len(enrollment) <= 10:
        raise ValueError("enrollment needs five to ten clips")
    held_out = spec.get("held_out")
    if not isinstance(held_out, list) or not 2 <= len(held_out) <= MAX_CLIPS:
        raise ValueError(f"held_out needs two to {MAX_CLIPS} clips")
    if any(not isinstance(clip, dict) or clip.get("kind") not in KINDS for clip in held_out):
        raise ValueError("invalid held-out condition")
    if not {"owner", "other"} <= {clip["kind"] for clip in held_out}:
        raise ValueError("held_out needs both owner and other clips")
    thresholds = spec.get("thresholds")
    if (not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 32
            or any(type(value) not in (int, float) or not -1 <= value <= 1 for value in thresholds)
            or len(set(thresholds)) != len(thresholds)):
        raise ValueError("thresholds needs up to 32 distinct cosine thresholds in [-1, 1]")
    return enrollment, held_out, sorted(thresholds)


def read_clip(root, filename, seen):
    if (not isinstance(filename, str) or not filename or "\x00" in filename
            or any(part in ("", ".", "..") for part in filename.split("/"))):
        raise ValueError("clip paths must be relative to the manifest directory")
    path = root / filename
    if path.resolve() != path:
        raise ValueError("clip paths cannot contain symbolic links")
    try:
        with wave.open(io.BytesIO(bounded_bytes(path, MAX_WAV_BYTES))) as clip:
            if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate(), clip.getcomptype()) != (
                1, 2, speaker_id.RATE, "NONE",
            ):
                raise ValueError("clips must be mono 16-bit PCM WAV at 16 kHz")
            frames = clip.getnframes()
            duration = frames / speaker_id.RATE
            if not speaker_id.MIN_SECONDS <= duration <= speaker_id.MAX_SECONDS:
                raise ValueError("clips must be one to twelve seconds long")
            pcm = clip.readframes(frames)
    except (wave.Error, EOFError) as error:
        raise ValueError("unreadable PCM WAV") from error
    if len(pcm) != 2 * frames:
        raise ValueError("truncated PCM WAV")
    digest = hashlib.sha256(pcm).digest()
    if digest in seen:
        raise ValueError("duplicate PCM content across audition clips")
    seen.add(digest)
    return sherpa.pcm_to_float(pcm)


def verify_model(path, descriptor):
    pack = model_pack.check_descriptor(json.loads(bounded_bytes(descriptor, 64 * 1024)), "speaker")
    entry = next((item for item in pack["files"] if item["path"] == "campplus.onnx"), None)
    if not entry or not path.is_file() or path.stat().st_size != entry["bytes"]:
        raise ValueError("CAM++ model is missing or its length differs from the pack")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != entry["sha256"]:
        raise ValueError("CAM++ model digest differs from the pack")
    return digest


def compare(extractor, centroid, samples):
    started = time.perf_counter()
    score = None
    outcome = "scored"
    try:
        vector = extractor.embedding(samples)
        score = math.fsum(left * right for left, right in zip(centroid, vector))
        score = min(1.0, max(-1.0, score))
    except speaker_id.InsufficientAudio:
        outcome = "insufficient"
    except (ValueError, sherpa.Unavailable):
        outcome = "failed"
    elapsed_ms = (time.perf_counter() - started) * 1000
    return outcome, score, elapsed_ms


def audition(manifest, model, *, descriptor=DESCRIPTOR, library=None):
    enrollment, held_out, thresholds = load_spec(manifest)
    digest = verify_model(model, descriptor)
    runtime = sherpa.version(library)
    started = time.perf_counter()
    seen = set()
    groups = {kind: [] for kind in KINDS}
    with speaker_id.Extractor(model, library=library) as extractor:
        load_ms = (time.perf_counter() - started) * 1000
        root = manifest.resolve().parent
        embeddings = [extractor.embedding(read_clip(root, filename, seen)) for filename in enrollment]
        means = tuple(math.fsum(column) for column in zip(*embeddings))
        norm = math.hypot(*means)
        if not norm:
            raise ValueError("enrollment did not produce a usable reference")
        centroid = tuple(value / norm for value in means)
        for clip in held_out:
            samples = read_clip(root, clip.get("file"), seen)
            groups[clip["kind"]].append(compare(extractor, centroid, samples))

    conditions = {}
    for kind, results in groups.items():
        scores = [score for outcome, score, _ in results if outcome == "scored"]
        timings = sorted(ms for _, _, ms in results)
        conditions[kind] = {
            "total": len(results),
            "scored": len(scores),
            "insufficient": sum(outcome == "insufficient" for outcome, _, _ in results),
            "failed": sum(outcome == "failed" for outcome, _, _ in results),
            "p95_ms": round(timings[math.ceil(0.95 * len(timings)) - 1], 3) if timings else None,
            "thresholds": [
                {"cosine": threshold, "accepted": sum(score >= threshold for score in scores)}
                for threshold in thresholds
            ],
        }
    return {
        "kind": "pipos-speaker-audition-v1",
        "model_sha256": digest,
        "runtime": runtime,
        "enrollment_count": len(enrollment),
        "model_load_ms": round(load_ms, 3),
        "conditions": conditions,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="pipos-speaker-clips-v1 JSON manifest")
    parser.add_argument("--model", type=Path, required=True, help="staged or installed CAM++ ONNX file")
    parser.add_argument("--descriptor", type=Path, default=DESCRIPTOR,
                        help="speaker pack descriptor from the source under qualification")
    parser.add_argument("--library", type=Path, help="override the default installed Sherpa C library")
    args = parser.parse_args(argv)
    try:
        result = audition(args.manifest, args.model, descriptor=args.descriptor, library=args.library)
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("speaker audition failed to read its inputs", file=sys.stderr)
        return 1
    except sherpa.Unavailable:
        print("the pinned Sherpa speaker extractor is unavailable", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"speaker audition failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
