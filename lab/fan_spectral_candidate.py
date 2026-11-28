#!/usr/bin/env python3
"""Build private paired WAVs for the WP-E15 fan-subtraction WER trial.

This is offline development tooling. It reads only explicit synthetic or
owner-consented snapshots, opens no audio device, and cannot alter Voice.
"""

import argparse
import cmath
import io
import json
import math
import os
import re
import stat
import wave
from array import array
from pathlib import Path

import audio_interaction_evidence as evidence


FFT_SIZE = 512
HOP = FFT_SIZE // 2
MAX_PAIRS = 10
SUBTRACTION = 1.0
SPECTRAL_FLOOR = 0.12
MIN_REFERENCE_SECONDS = 2.0
GROUP = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
CLIP_FIELDS = frozenset(("role", "wav", "fan_rpm", "fan_rpm_source",
                         "expected_sha256", "capture_group", "channel"))
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def fft(values, inverse=False):
    """In-place radix-2 transform; fixed at the bounded 512-sample window."""
    count = len(values)
    output = list(values)
    target = 0
    for source in range(1, count):
        bit = count >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if source < target:
            output[source], output[target] = output[target], output[source]
    size = 2
    sign = 1 if inverse else -1
    while size <= count:
        root = cmath.exp(sign * 2j * math.pi / size)
        half = size // 2
        for start in range(0, count, size):
            factor = 1 + 0j
            for offset in range(half):
                even = output[start + offset]
                odd = factor * output[start + offset + half]
                output[start + offset] = even + odd
                output[start + offset + half] = even - odd
                factor *= root
        size *= 2
    if inverse:
        output = [value / count for value in output]
    return output


def windows(samples):
    padded = array("f", [0.0] * HOP)
    padded.extend(samples)
    padded.extend([0.0] * FFT_SIZE)
    for start in range(0, len(padded) - FFT_SIZE + 1, HOP):
        yield padded[start:start + FFT_SIZE]


def interior_windows(samples):
    """Only complete captured windows: reference training never sees padding."""
    for start in range(0, len(samples) - FFT_SIZE + 1, HOP):
        yield samples[start:start + FFT_SIZE]


def hann():
    return [0.5 - 0.5 * math.cos(2 * math.pi * index / FFT_SIZE)
            for index in range(FFT_SIZE)]


def noise_profile(samples, window):
    sums = [0.0] * (FFT_SIZE // 2 + 1)
    count = 0
    for frame in interior_windows(samples):
        spectrum = fft([sample * weight for sample, weight in zip(frame, window)])
        for index in range(len(sums)):
            sums[index] += abs(spectrum[index])
        count += 1
    if not count:
        raise ValueError("fan reference is too short for one spectral window")
    return [value / count for value in sums], count


def subtract(samples, profile, window):
    padded_count = len(samples) + HOP + FFT_SIZE
    output = [0.0] * padded_count
    weights = [0.0] * padded_count
    frame_count = 0
    for start, frame in zip(range(0, padded_count - FFT_SIZE + 1, HOP), windows(samples)):
        spectrum = fft([sample * weight for sample, weight in zip(frame, window)])
        for index in range(FFT_SIZE // 2 + 1):
            magnitude = abs(spectrum[index])
            retained = max(SPECTRAL_FLOOR * magnitude, magnitude - SUBTRACTION * profile[index])
            spectrum[index] *= retained / magnitude if magnitude else 0.0
            if index not in (0, FFT_SIZE // 2):
                spectrum[-index] = spectrum[index].conjugate()
        restored = fft(spectrum, inverse=True)
        for offset, (sample, weight) in enumerate(zip(restored, window)):
            output[start + offset] += sample.real * weight
            weights[start + offset] += weight * weight
        frame_count += 1
    begin = HOP
    result = array("f")
    for index in range(begin, begin + len(samples)):
        value = output[index] / weights[index] if weights[index] > 1e-9 else 0.0
        result.append(max(-1.0, min(1.0, value)))
    return result, frame_count


def wav_bytes(samples, rate):
    pcm = array("h", (round(max(-1.0, min(32767 / 32768, value)) * 32768) for value in samples))
    destination = io.BytesIO()
    with wave.open(destination, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        sink.writeframes(pcm.tobytes())
    return destination.getvalue()


def private_write(directory_fd, name, contents):
    verify_private_directory(directory_fd)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("candidate output file is not a private new regular file")
        view = memoryview(contents)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("private candidate write made no progress")
            view = view[written:]
    finally:
        os.close(fd)
    verify_private_directory(directory_fd)


def verify_private_directory(directory_fd):
    info = os.fstat(directory_fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("candidate output directory lost its private owner/mode binding")


def create_private_directory(path):
    absolute = path.absolute()
    if len(absolute.parts) < 2 or any(part in ("", ".", "..") for part in absolute.parts[1:]):
        raise ValueError("output directory needs a portable path without dot components")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            os.close(parent)
            parent = child
        os.mkdir(absolute.parts[-1], 0o700, dir_fd=parent)
        directory = os.open(absolute.parts[-1], os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        verify_private_directory(directory)
        return directory
    finally:
        os.close(parent)


def load_clips(manifest, root_fd):
    items = manifest.get("clips")
    if not isinstance(items, list) or not 1 <= len(items) <= evidence.MAX_CLIPS:
        raise ValueError(f"manifest requires 1--{evidence.MAX_CLIPS} bounded clips")
    loaded = []
    total_seconds = 0.0
    speech_hashes = set()
    speech_pcm_hashes = set()
    for item in items:
        if not isinstance(item, dict) or set(item) - CLIP_FIELDS:
            raise ValueError("candidate clips contain an unknown field")
        if item.get("role") not in ("speech", "fan-silence"):
            raise ValueError("candidate clips must be speech or fan-silence")
        rpm = item.get("fan_rpm")
        source = item.get("fan_rpm_source")
        if type(rpm) is not int or not 0 <= rpm <= 10000 \
                or source not in ("hardware-sampled", "synthetic-fixture"):
            raise ValueError("every paired clip needs bounded fan_rpm and its evidence source")
        if not isinstance(item.get("wav"), str) or not item["wav"]:
            raise ValueError("every candidate clip needs a relative WAV path")
        expected = item.get("expected_sha256")
        group = item.get("capture_group")
        channel = item.get("channel")
        if not isinstance(expected, str) or SHA256.fullmatch(expected) is None:
            raise ValueError("every candidate clip needs its expected lowercase SHA-256")
        if not isinstance(group, str) or GROUP.fullmatch(group) is None:
            raise ValueError("every candidate clip needs a bounded opaque capture_group")
        if type(channel) is not int or channel not in (0, 1):
            raise ValueError("every candidate clip needs explicit channel 0 or 1")
        contents = evidence.read_relative_once(root_fd, item["wav"], evidence.MAX_WAV_BYTES)
        actual = evidence.digest(contents)
        if actual != expected:
            raise ValueError("candidate clip bytes do not match expected_sha256")
        rate, samples, channels, selected = evidence.read_wav(contents)
        if channel >= channels or selected != channel:
            raise ValueError("explicit channel does not match the recorded selected channel")
        total_seconds += len(samples) / rate
        if total_seconds > evidence.MAX_SECONDS_TOTAL:
            raise ValueError(f"manifest exceeds {evidence.MAX_SECONDS_TOTAL} aggregate seconds")
        if item["role"] == "fan-silence" and len(samples) < max(rate * MIN_REFERENCE_SECONDS, FFT_SIZE):
            raise ValueError(f"fan reference must contain at least {MIN_REFERENCE_SECONDS:g} seconds")
        if item["role"] == "speech" and actual in speech_hashes:
            raise ValueError("duplicate speech clip in candidate manifest")
        if item["role"] == "speech":
            selected_hash = evidence.digest(wav_bytes(samples, rate))
            if selected_hash in speech_pcm_hashes:
                raise ValueError("duplicate selected-channel speech PCM in candidate manifest")
            speech_hashes.add(actual)
            speech_pcm_hashes.add(selected_hash)
        loaded.append({"role": item["role"], "rpm": rpm, "source": source, "group": group,
                       "input_sha256": actual, "rate": rate, "samples": samples,
                       "channels": channels, "selected": selected})
    return loaded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="new private directory for paired WAVs and pairs.json")
    args = parser.parse_args()
    manifest_bytes, root_fd = evidence.open_manifest(args.manifest)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != 1 \
                or manifest.get("provenance") not in ("synthetic", "owner-consented"):
            raise ValueError("manifest requires explicit synthetic or owner-consented provenance")
        loaded = load_clips(manifest, root_fd)
    finally:
        os.close(root_fd)
    references = [clip for clip in loaded if clip["role"] == "fan-silence"]
    speeches = [clip for clip in loaded if clip["role"] == "speech"]
    if not references or not 1 <= len(speeches) <= MAX_PAIRS:
        raise ValueError(f"need a fan reference and 1--{MAX_PAIRS} speech clips")

    pairings = []
    for speech in speeches:
        matches = [reference for reference in references
                   if reference["rpm"] == speech["rpm"] and reference["rate"] == speech["rate"]
                   and reference["source"] == speech["source"]
                   and reference["group"] == speech["group"]
                   and reference["selected"] == speech["selected"]]
        if len(matches) != 1:
            raise ValueError("each speech clip needs exactly one same-source, same-RPM, same-rate fan reference")
        pairings.append((speech, matches[0]))

    output_fd = create_private_directory(args.output_dir)
    records = []
    profiles = {}
    window = hann()
    try:
        for number, (speech, reference) in enumerate(pairings, 1):
            key = (reference["input_sha256"], reference["rate"])
            if key not in profiles:
                profiles[key] = noise_profile(reference["samples"], window)
            profile, profile_frames = profiles[key]
            filtered, processed_frames = subtract(speech["samples"], profile, window)
            original = wav_bytes(speech["samples"], speech["rate"])
            candidate = wav_bytes(filtered, speech["rate"])
            original_name = f"pair-{number:02d}-original.wav"
            candidate_name = f"pair-{number:02d}-candidate.wav"
            private_write(output_fd, original_name, original)
            private_write(output_fd, candidate_name, candidate)
            records.append({
                "pair": number, "fan_rpm": speech["rpm"], "fan_rpm_source": speech["source"],
                "capture_group": speech["group"],
                "rate_hz": speech["rate"], "frames": len(speech["samples"]),
                "selected_input_channel": speech["selected"], "input_channels": speech["channels"],
                "source_sha256": speech["input_sha256"], "fan_reference_sha256": reference["input_sha256"],
                "fan_reference": {"capture_group": reference["group"], "channel": reference["selected"],
                                  "rate_hz": reference["rate"], "frames": len(reference["samples"]),
                                  "duration_s": round(len(reference["samples"]) / reference["rate"], 3),
                                  "rpm": reference["rpm"], "rpm_source": reference["source"],
                                  "interior_profile_frames": profile_frames},
                "original": original_name, "original_sha256": evidence.digest(original),
                "candidate": candidate_name, "candidate_sha256": evidence.digest(candidate),
                "profile_frames": profile_frames, "processed_frames": processed_frames,
            })
        report = {
            "schema": 1, "experiment": "wp-e15-fan-spectral-subtraction-pairs",
            "provenance": manifest["provenance"], "manifest_sha256": evidence.digest(manifest_bytes),
            "algorithm": {"fft_size": FFT_SIZE, "hop": HOP, "window": "hann",
                          "subtraction": SUBTRACTION, "spectral_floor": SPECTRAL_FLOOR},
            "pairs": records,
            "acceptance": {"status": "not-run", "required": "paired native WER and wake false-trigger comparison",
                           "kill": "any WER increase versus the paired original"},
            "production_promotion": False,
        }
        private_write(output_fd, "pairs.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
    finally:
        os.close(output_fd)


if __name__ == "__main__":
    main()
