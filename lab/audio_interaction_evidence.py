#!/usr/bin/env python3
"""Bounded, offline, text-free evidence for WP-E15 audio experiments.

This program only reads WAVs explicitly named in a manifest.  It neither opens
an audio device nor emits audio, transcripts, pitch contours, or identity
features. Aggregate results can remain voice-correlated private evidence; they
are not production tags or identity scores.
"""

import argparse
import hashlib
import io
import json
import math
import os
import stat
import wave
from array import array
from pathlib import Path


MAX_CLIPS = 12
MAX_SECONDS_PER_CLIP = 120
MAX_SECONDS_TOTAL = 600
MAX_MANIFEST_BYTES = 64 * 1024
MAX_WAV_BYTES = MAX_SECONDS_PER_CLIP * 16000 * 2 * 2 + 1024 * 1024
FRAME_MS = 30
STEP_MS = 20
MAX_PITCH_FRAMES = 600
MAX_SPECTRAL_FRAMES = 64
PITCH_MIN_HZ = 70
PITCH_MAX_HZ = 350
BANDS = ((50, 170), (170, 1000), (1000, 4000))


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def dbfs(value):
    return round(20 * math.log10(max(value, 1 / 32768)), 2)


def digest(contents):
    return hashlib.sha256(contents).hexdigest()


def read_fd_all(fd, limit):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("input is not a bounded regular file")
    chunks, size = [], 0
    while True:
        block = os.read(fd, min(1024 * 1024, limit + 1 - size))
        if not block:
            break
        chunks.append(block)
        size += len(block)
        if size > limit:
            raise ValueError("input exceeds its byte bound")
    after = os.fstat(fd)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                              value.st_mtime_ns, value.st_ctime_ns)
    if size != info.st_size or identity(after) != identity(info):
        raise ValueError("input changed while it was being read")
    return b"".join(chunks)


def open_manifest(path):
    absolute = path.absolute()
    if any(part in ("", ".", "..") for part in absolute.parts[1:]):
        raise ValueError("manifest path contains a non-portable component")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(absolute.parts[-1], os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                     | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        try:
            return read_fd_all(fd, MAX_MANIFEST_BYTES), directory
        finally:
            os.close(fd)
    except BaseException:
        os.close(directory)
        raise


def read_relative_once(root_fd, relative, limit):
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("WAV path must be portable and relative beneath the manifest")
    directory = os.dup(root_fd)
    try:
        for component in path.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.parts[-1], os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                     | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        try:
            return read_fd_all(fd, limit)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def read_wav(contents):
    with wave.open(io.BytesIO(contents), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        count = source.getnframes()
        if channels not in (1, 2) or width != 2 or rate not in (8000, 16000):
            raise ValueError("input must be mono/stereo PCM16 WAV at 8 or 16 kHz")
        if count > rate * MAX_SECONDS_PER_CLIP:
            raise ValueError(f"input exceeds {MAX_SECONDS_PER_CLIP} seconds")
        if count * 1000 < FRAME_MS * rate:
            raise ValueError("WAV must contain at least one complete analysis frame")
        raw = source.readframes(count)
        if len(raw) != count * channels * width:
            raise ValueError("WAV PCM is truncated relative to its declared frame count")
        values = array("h")
        values.frombytes(raw)
    tracks = [values[index::channels] for index in range(channels)]
    energies = [math.fsum(sample * sample for sample in track) for track in tracks]
    chosen = max(range(channels), key=energies.__getitem__)
    return rate, array("f", (sample / 32768 for sample in tracks[chosen])), channels, chosen


def frame_starts(samples, size, step):
    return range(0, max(0, len(samples) - size + 1), step)


def bounded_indices(indices, limit):
    if len(indices) <= limit:
        return list(indices)
    return [indices[index * (len(indices) - 1) // (limit - 1)] for index in range(limit)]


def rms(values):
    return math.sqrt(math.fsum(value * value for value in values) / max(1, len(values)))


def band_power(values, rate, low, high):
    """Bounded Goertzel bank; ratios are diagnostic, not calibrated spectra."""
    length = len(values)
    if not length:
        return 0.0
    first = max(1, math.ceil(low * length / rate))
    # Bands are [low, high): an edge bin belongs to exactly one band.
    last = min(length // 2, math.ceil(high * length / rate) - 1)
    total = 0.0
    for index in range(first, last + 1):
        coefficient = 2 * math.cos(2 * math.pi * index / length)
        previous = before = 0.0
        for sample in values:
            current = sample + coefficient * previous - before
            before, previous = previous, current
        total += previous * previous + before * before - coefficient * previous * before
    return max(0.0, total)


def pitch(frame, rate):
    mean = sum(frame) / len(frame)
    centred = [value - mean for value in frame]
    power = math.fsum(value * value for value in centred)
    if power <= 1e-8:
        return None
    candidates = []
    for lag in range(max(1, rate // PITCH_MAX_HZ), min(len(frame) - 1, rate // PITCH_MIN_HZ) + 1):
        numerator = math.fsum(a * b for a, b in zip(centred[:-lag], centred[lag:]))
        left = math.fsum(value * value for value in centred[:-lag])
        right = math.fsum(value * value for value in centred[lag:])
        score = numerator / math.sqrt(left * right) if left and right else 0.0
        candidates.append((lag, score))
    # The first credible local periodic peak avoids selecting a stronger
    # integer multiple of the fundamental (the usual octave/sub-octave alias).
    for index in range(1, len(candidates) - 1):
        lag, score = candidates[index]
        if score >= 0.6 and score >= candidates[index - 1][1] and score > candidates[index + 1][1]:
            return rate / lag
    return None


def analyse(contents, pcm, declared_syllables=None):
    rate, samples, channels, chosen = pcm
    size = max(1, rate * FRAME_MS // 1000)
    step = max(1, rate * STEP_MS // 1000)
    starts = frame_starts(samples, size, step)
    levels = [rms(samples[start:start + size]) for start in starts]
    floor = percentile(levels, 0.2) or 0.0
    threshold = max(floor * 3.16, 10 ** (-45 / 20))
    active = [level >= threshold for level in levels]
    bursts = sum(on and (index == 0 or not active[index - 1]) for index, on in enumerate(active))
    voiced_starts = [start for start, on in zip(starts, active) if on]
    pitch_starts = bounded_indices(voiced_starts, MAX_PITCH_FRAMES)
    pitches = [value for start in pitch_starts
               if (value := pitch(samples[start:start + size], rate)) is not None]
    spectral_starts = bounded_indices(starts, MAX_SPECTRAL_FRAMES)
    powers = [math.fsum(band_power(samples[start:start + size], rate, low,
                                    min(high, rate / 2 - 1))
                           for start in spectral_starts)
              if low < rate / 2 - 1 else 0.0 for low, high in BANDS]
    power_total = sum(powers) or 1.0
    duration = len(samples) / rate
    result = {
        "sha256": digest(contents),
        "pcm": {"rate_hz": rate, "frames": len(samples), "source_channels": channels,
                "analysed_channel": chosen},
        "duration_s": round(duration, 3),
        "energy_dbfs": {"p20": dbfs(percentile(levels, 0.2) or 0),
                        "median": dbfs(percentile(levels, 0.5) or 0),
                        "p95": dbfs(percentile(levels, 0.95) or 0)},
        "pitch_hz": {"median": round(percentile(pitches, 0.5), 1) if pitches else None,
                     "p25": round(percentile(pitches, 0.25), 1) if pitches else None,
                     "p75": round(percentile(pitches, 0.75), 1) if pitches else None,
                     "voiced_frame_fraction": round(len(pitches) / max(1, len(pitch_starts)), 4),
                     "sampled_active_frames": len(pitch_starts)},
        "activity": {"threshold_dbfs": dbfs(threshold), "active_fraction": round(sum(active) / max(1, len(active)), 4),
                     "burst_count": bursts,
                     "bursts_per_minute": round(bursts * 60 / duration, 2) if duration else None,
                     "is_not_speech_rate": True},
        "band_power_fraction": {f"{low}-{high}_hz": round(value / power_total, 6)
                                for (low, high), value in zip(BANDS, powers)},
        "spectral_frames_sampled": len(spectral_starts),
    }
    if declared_syllables is not None:
        if type(declared_syllables) is not int or not 0 <= declared_syllables <= 2000:
            raise ValueError("syllable_count must be a bounded non-negative integer")
        active_seconds = sum(active) * STEP_MS / 1000
        result["declared_cadence"] = {
            "syllable_count": declared_syllables,
            "syllables_per_active_second": round(declared_syllables / active_seconds, 3) if active_seconds else None,
            "count_source": "manifest; audio was not transcribed",
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path,
                        help="new private JSON result (created mode 0600; never stdout)")
    args = parser.parse_args()
    manifest_bytes, root_fd = open_manifest(args.manifest)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != 1 \
                or manifest.get("provenance") not in ("synthetic", "owner-consented"):
            raise ValueError("manifest requires schema 1 and synthetic or owner-consented provenance")
        clips = manifest.get("clips")
        if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
            raise ValueError(f"manifest requires 1--{MAX_CLIPS} clips")
    except Exception:
        os.close(root_fd)
        raise
    results = []
    total = 0.0
    try:
        for item in clips:
            if not isinstance(item, dict) or item.get("role") not in ("speech", "fan-silence", "control"):
                raise ValueError("each clip needs a speech, fan-silence, or control role")
            if not isinstance(item.get("wav"), str) or not item["wav"]:
                raise ValueError("each clip needs a WAV pathname")
            contents = read_relative_once(root_fd, item["wav"], MAX_WAV_BYTES)
            pcm = read_wav(contents)
            total += len(pcm[1]) / pcm[0]
            if total > MAX_SECONDS_TOTAL:
                raise ValueError(f"manifest exceeds {MAX_SECONDS_TOTAL} aggregate seconds")
            result = analyse(contents, pcm, item.get("syllable_count"))
            result["role"] = item["role"]
            if "fan_rpm" in item:
                rpm = item["fan_rpm"]
                source = item.get("fan_rpm_source")
                if type(rpm) is not int or not 0 <= rpm <= 10000 or source not in ("hardware-sampled", "synthetic-fixture"):
                    raise ValueError("fan_rpm requires 0--10000 and a descriptive evidence source")
                result["fan_reference"] = {"rpm": rpm, "source": source, "descriptive_only": True}
            results.append(result)
    finally:
        os.close(root_fd)
    fan = [item["band_power_fraction"]["50-170_hz"] for item in results if item["role"] == "fan-silence"]
    report = {
        "schema": 1,
        "experiment": "wp-e15-text-free-offline",
        "provenance": manifest["provenance"],
        "input_manifest_sha256": digest(manifest_bytes),
        "clips": results,
        "fan_reference": {"available": bool(fan), "median_low_band_fraction": round(percentile(fan, 0.5), 6) if fan else None,
                          "interpretation": "comparison only; no subtraction or production filter"},
        "claims": {"text_free": True, "voice_correlated_aggregates": True,
                   "identity_scoring_or_enrolment": False, "live_capture": False,
                   "production_change": False, "native_acceptance": "not-run"},
    }
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(args.output, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("the destination does not enforce owner-only permissions")
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("private result write made no progress")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        args.output.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
