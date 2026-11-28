#!/usr/bin/env python3
"""Offline evidence for WP-E4 reference-aware barge-in.

This program does not open an ALSA device and cannot change BeingOS audio policy.
It compares mono PCM WAVs already collected by an explicit native experiment, or
deterministically synthesises the public scenarios in audio-echo-scenarios.json.
"""

import argparse
import hashlib
import json
import math
import os
import random
import stat
import wave
from array import array
from pathlib import Path

MAX_SECONDS = 120
ALIGN_SECONDS = 30
MAX_EVENTS_BYTES = 256 * 1024
MAX_LABELS = 100


def regular_bytes(path, limit, description):
    path = Path(path)
    before = path.lstat()
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= limit):
        raise ValueError(f"{description} must be a bounded regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if identity(opened) != identity(before):
            raise ValueError(f"{description} changed while opening")
        data = b""
        while len(data) < opened.st_size:
            block = os.read(descriptor, min(1 << 20, opened.st_size - len(data)))
            if not block:
                raise ValueError(f"{description} changed while reading")
            data += block
        if identity(os.fstat(descriptor)) != identity(opened):
            raise ValueError(f"{description} changed while reading")
        return data
    finally:
        os.close(descriptor)


def bounded_json(path, description):
    try:
        value = json.loads(regular_bytes(path, MAX_EVENTS_BYTES, description))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"{description} is invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} is invalid")
    return value


def validate_windows(items, duration_ms, description, *, limit=MAX_LABELS):
    if not isinstance(items, list) or len(items) > limit:
        raise ValueError(f"{description} has an invalid count")
    result = []
    previous_end = 0.0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"start_ms", "end_ms"}:
            raise ValueError(f"{description} has invalid fields")
        start, end = item["start_ms"], item["end_ms"]
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous_end or start < 0 or end <= start or end > duration_ms):
            raise ValueError(f"{description} has invalid or overlapping times")
        result.append({"start_ms": float(start), "end_ms": float(end)})
        previous_end = end
    return result


def labelled_metadata(metadata, labels, mic_path, reference_path, events_raw, duration_ms):
    """Bind separately supplied ground truth to this exact three-file trace."""
    result = dict(metadata)
    if labels is None:
        result["interruptions"] = []
        result["label_provenance"] = "unqualified"
        return result
    expected = {"schema", "source", "trace_sha256", "interruptions"}
    if set(labels) != expected or labels.get("schema") != 1 or not isinstance(labels.get("source"), str) \
            or not 1 <= len(labels["source"]) <= 120 or labels["source"] == "collector":
        raise ValueError("external labels have an invalid schema or source")
    digests = labels.get("trace_sha256")
    if not isinstance(digests, dict) or set(digests) != {"mic", "reference", "events"}:
        raise ValueError("external labels do not bind the trace")
    actual = {
        "mic": hashlib.sha256(regular_bytes(mic_path, 32 * 1024 * 1024, "microphone WAV")).hexdigest(),
        "reference": hashlib.sha256(regular_bytes(reference_path, 32 * 1024 * 1024,
                                                   "reference WAV")).hexdigest(),
        "events": hashlib.sha256(events_raw).hexdigest(),
    }
    if any(not isinstance(digests.get(key), str) or digests[key] != actual[key] for key in actual):
        raise ValueError("external labels name a different trace")
    result["interruptions"] = validate_windows(labels["interruptions"], duration_ms,
                                                 "external interruption labels")
    # Separation and hashes bind the labels to these bytes, but do not
    # authenticate who authored them. Native review remains outside this tool.
    result["label_provenance"] = "supplied-digest-bound-unverified-author"
    result["label_set_sha256"] = hashlib.sha256(
        json.dumps(labels, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return result


def trace_provenance(metadata, rate, mic_frames, reference_frames):
    """Validate timing claims and return only non-audio acquisition provenance."""
    basis = metadata.get("timing_basis", "unspecified")
    if basis in ("unspecified", "host_scheduled_unmatched"):
        return {"timing_basis": basis, "native_linked": False,
                "alignment_required": True}
    if basis != "alsa-linked-trigger-monotonic-raw":
        raise ValueError(f"unsupported trace timing basis: {basis}")
    native = metadata.get("native")
    schema = metadata.get("schema")
    if schema not in (2, 3, 4) or not isinstance(native, dict) or native.get("linked") is not True:
        raise ValueError("native trace does not attest an ALSA-linked stream group")
    required = ("rate", "period_frames", "buffer_frames", "capture_channel",
                "capture_trigger_ns", "playback_trigger_ns", "trigger_delta_ns",
                "captured_frames", "written_frames", "xruns")
    if schema in (3, 4):
        required += ("reference_first_signal_frame", "stimulus_pulse_frames",
                     "stimulus_interval_frames", "stimulus_pulses")
    if schema == 4:
        required += ("playback_start_status_ns", "playback_start_trigger_ns",
                     "playback_start_delay_frames", "playback_start_avail_frames",
                     "initial_queued_frames")
    if any(type(native.get(key)) is not int for key in required):
        raise ValueError("native trace is missing integer frame/timestamp evidence")
    if rate != 48000 or native["rate"] != rate or native["period_frames"] != rate // 100:
        raise ValueError("native trace rate or period disagrees with its WAVs")
    if native["buffer_frames"] != native["period_frames"] * 4:
        raise ValueError("native trace does not have the declared four-period buffer")
    if native["capture_channel"] not in (0, 1):
        raise ValueError("native trace selected an invalid capture channel")
    if native["captured_frames"] != mic_frames or native["written_frames"] != reference_frames:
        raise ValueError("native frame evidence disagrees with its WAVs")
    if (mic_frames, reference_frames) != (4 * rate, 4 * rate):
        raise ValueError("native trace did not preserve the full four-second window")
    capture_trigger, playback_trigger = native["capture_trigger_ns"], native["playback_trigger_ns"]
    if capture_trigger <= 0 or playback_trigger <= 0:
        raise ValueError("native ALSA trigger timestamp is absent")
    if native["trigger_delta_ns"] != abs(capture_trigger - playback_trigger):
        raise ValueError("native trigger delta is inconsistent")
    if native["xruns"] != 0:
        raise ValueError("native evidence contains an xrun")
    result = {"timing_basis": basis, "native_linked": True, "alignment_required": True,
              "rate": rate, "period_frames": native["period_frames"],
              "buffer_frames": native["buffer_frames"],
              "capture_channel": native["capture_channel"],
              "trigger_delta_ns": native["trigger_delta_ns"], "xruns": native["xruns"]}
    if schema in (3, 4):
        first = native["reference_first_signal_frame"]
        pulse = native["stimulus_pulse_frames"]
        interval = native["stimulus_interval_frames"]
        pulses = native["stimulus_pulses"]
        if (first != rate // 2 or pulse != rate // 200 or interval != rate or pulses != 4
                or first + (pulses - 1) * interval + pulse > reference_frames):
            raise ValueError("native stimulus frame evidence is invalid")
        result.update({
            "reference_first_signal_frame": first,
            "reference_first_signal_monotonic_raw_ns": (
                native["playback_trigger_ns"] + first * 1_000_000_000 // rate),
            "stimulus_pulse_frames": pulse, "stimulus_interval_frames": interval,
            "stimulus_pulses": pulses,
        })
    if schema == 4:
        status_ns = native["playback_start_status_ns"]
        trigger_ns = native["playback_start_trigger_ns"]
        delay = native["playback_start_delay_frames"]
        available = native["playback_start_avail_frames"]
        queued = native["initial_queued_frames"]
        if (status_ns <= 0 or trigger_ns <= 0 or trigger_ns != native["playback_trigger_ns"]
                or status_ns < trigger_ns
                or status_ns - trigger_ns > 1_000_000_000
                or queued != native["buffer_frames"] or not 0 <= delay <= queued
                or not 0 <= available <= native["buffer_frames"]):
            raise ValueError("native playback queue observation is inconsistent")
        result["kernel_queue_observation"] = {
            "status_monotonic_raw_ns": status_ns,
            "trigger_monotonic_raw_ns": trigger_ns,
            "observed_after_trigger_ms": round((status_ns - trigger_ns) / 1_000_000, 3),
            "initial_queued_frames": queued,
            "delay_frames_at_observation": delay,
            "delay_ms_at_observation": round(delay * 1000 / rate, 3),
            "available_frames_at_observation": available,
            "meaning": "kernel queue estimate, not physical amplitude onset",
        }
    return result


def acoustic_onset(metadata, rate, lag, score):
    """Describe an acoustic delay estimate without claiming DAC/ADC timestamps."""
    native = metadata.get("native")
    if metadata.get("schema") not in (3, 4) or not isinstance(native, dict):
        return None
    first = native["reference_first_signal_frame"]
    observed = first + lag
    playback_ns = native["playback_trigger_ns"] + first * 1_000_000_000 // rate
    capture_ns = native["capture_trigger_ns"] + observed * 1_000_000_000 // rate
    detected = score >= 0.2
    return {
        "status": "estimated" if detected else "not-detected",
        "method": "bounded-reference-correlation-1ms-grid",
        "reference_frame": first,
        "estimated_capture_frame": observed if detected else None,
        "delay_ms": round((capture_ns - playback_ns) / 1_000_000, 3) if detected else None,
        "correlation": round(score, 5),
        "clock_basis": "alsa-linked-trigger-monotonic-raw-plus-stream-frame",
        "qualification": "frame-derived estimate; not a measured DAC or ADC physical timestamp",
    }


def validate_native_reference(metadata, reference):
    """Bind declared stimulus windows to the actual bounded reference PCM."""
    native = metadata.get("native")
    if metadata.get("schema") not in (3, 4) or not isinstance(native, dict):
        return
    first = native["reference_first_signal_frame"]
    width = native["stimulus_pulse_frames"]
    interval = native["stimulus_interval_frames"]
    windows = tuple((first + index * interval, first + index * interval + width)
                    for index in range(native["stimulus_pulses"]))
    for begin, end in windows:
        if not any(reference[frame] != 0.0 for frame in range(begin, end)):
            raise ValueError("native reference lacks a declared stimulus pulse")
    for frame, sample in enumerate(reference):
        if sample != 0.0 and not any(begin <= frame < end for begin, end in windows):
            raise ValueError("native reference has signal outside its declared stimulus windows")


def read_mono(path):
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError(f"{path}: expected mono 16-bit PCM WAV")
        rate = source.getframerate()
        if source.getnframes() > rate * MAX_SECONDS:
            raise ValueError(f"{path}: trace exceeds the {MAX_SECONDS} second evidence bound")
        samples = array("h", source.readframes(source.getnframes()))
    return rate, [sample / 32768.0 for sample in samples]


def frames(samples, size):
    return [samples[start:start + size] for start in range(0, len(samples) - size + 1, size)]


def rms(values):
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def correlation(left, right):
    count = min(len(left), len(right))
    if not count:
        return 0.0
    left, right = left[:count], right[:count]
    lm, rm = sum(left) / count, sum(right) / count
    numerator = sum((a - lm) * (b - rm) for a, b in zip(left, right))
    l2 = sum((a - lm) ** 2 for a in left)
    r2 = sum((b - rm) ** 2 for b in right)
    return numerator / math.sqrt(l2 * r2) if l2 and r2 else 0.0


def align(reference, microphone, rate, limit_ms):
    """Return the non-negative speaker-to-microphone lag with best correlation."""
    maximum = min(int(rate * limit_ms / 1000), max(0, len(microphone) - 1))
    # Search one millisecond apart: fine enough for the gate, bounded for long files.
    step = max(1, rate // 1000)
    best = (float("-inf"), 0)
    for lag in range(0, maximum + 1, step):
        score = correlation(reference[:len(microphone) - lag], microphone[lag:])
        if score > best[0]:
            best = score, lag
    return best[1], best[0]


def in_interruption(at_ms, interruptions):
    return any(item["start_ms"] <= at_ms < item["end_ms"] for item in interruptions)


def analyse(reference, microphone, rate, metadata, processed=None):
    frame_ms = int(metadata.get("frame_ms", 10))
    if frame_ms != 10:
        raise ValueError("evidence frames must be the E4 10 ms period")
    size = rate * frame_ms // 1000
    if size <= 0:
        raise ValueError("sample rate is too low")
    if max(len(reference), len(microphone)) > rate * MAX_SECONDS:
        raise ValueError(f"trace exceeds the {MAX_SECONDS} second evidence bound")
    if processed is not None and len(processed) < min(len(reference), len(microphone)):
        raise ValueError("processed WAV is shorter than the evidence interval")
    duration_ms = min(len(reference), len(microphone)) * 1000 / rate
    maximum_lag = metadata.get("max_lag_ms", 250)
    hold_ms = metadata.get("hold_ms", 150)
    threshold = metadata.get("residual_rms_threshold", 0.025)
    if (type(maximum_lag) not in (int, float) or not 0 <= maximum_lag <= 1000
            or type(hold_ms) not in (int, float) or not 10 <= hold_ms <= 1000
            or type(threshold) not in (int, float) or not math.isfinite(threshold)
            or not 0.001 <= threshold <= 1.0):
        raise ValueError("evidence gate parameters are outside their bounds")
    playback = validate_windows(metadata.get("playback", []), duration_ms, "playback windows", limit=32)
    interruptions = validate_windows(metadata.get("interruptions", []), duration_ms,
                                     "interruption labels")
    validate_native_reference(metadata, reference)
    alignment_count = min(len(reference), len(microphone), rate * ALIGN_SECONDS)
    lag, alignment = align(reference[:alignment_count], microphone[:alignment_count], rate,
                           int(maximum_lag))
    shifted = [0.0] * lag + reference
    count = min(len(shifted), len(microphone))
    ref_frames = frames(shifted[:count], size)
    mic_frames = frames(microphone[:count], size)
    processed_frames = frames(processed[:count], size) if processed is not None else None
    def playing(at):
        return any(window["start_ms"] <= at < window["end_ms"] for window in playback)

    calibration = []
    for index, (ref, mic) in enumerate(zip(ref_frames, mic_frames)):
        at = index * frame_ms
        ref_level = rms(ref)
        if playing(at) and not in_interruption(at, interruptions) and ref_level > 1e-5:
            calibration.append(rms(mic) / ref_level)
    if not calibration:
        raise ValueError("no playback-only frames available to calibrate echo gain")
    calibration.sort()
    gain = calibration[len(calibration) // 2]
    threshold = float(threshold)
    hold_frames = math.ceil(int(hold_ms) / frame_ms)
    active = 0
    candidates = []
    echo_in = echo_out = 0.0
    echo_count = 0
    for index, (ref, mic) in enumerate(zip(ref_frames, mic_frames)):
        at = index * frame_ms
        if not playing(at):
            active = 0
            continue
        residual = rms([m - gain * r for m, r in zip(mic, ref)])
        active = active + 1 if residual >= threshold else 0
        if active == hold_frames:
            candidates.append(at - (hold_frames - 1) * frame_ms)
        if processed_frames is not None and not in_interruption(at, interruptions):
            echo_in += sum(value * value for value in mic)
            echo_out += sum(value * value for value in processed_frames[index])
            echo_count += len(mic)

    matched = set()
    latencies = []
    false_candidates = 0
    for candidate in candidates:
        match = next((i for i, item in enumerate(interruptions)
                      if i not in matched and item["start_ms"] <= candidate < item["end_ms"]), None)
        if match is None:
            false_candidates += 1
        else:
            matched.add(match)
            latencies.append(candidate - interruptions[match]["start_ms"])
    result = {
        "alignment": {"lag_ms": round(lag * 1000 / rate, 3), "correlation": round(alignment, 5)},
        "calibration": {"echo_gain": round(gain, 5), "playback_only_frames": len(calibration)},
        "acoustic_gate": {
            "hold_ms": hold_frames * frame_ms,
            "candidate_count": len(candidates),
            "false_candidates": false_candidates,
            "labelled_interruptions": len(interruptions),
            "detected_interruptions": len(matched),
            "missed_interruptions": len(interruptions) - len(matched),
            "latency_ms": latencies,
        },
        "promotion": "forbidden: acoustic candidates still require a wake/command spotter and 50 native interruptions",
    }
    onset = acoustic_onset(metadata, rate, lag, alignment)
    if onset is not None:
        result["acoustic_onset"] = onset
    qualified = metadata.get("label_provenance") == "supplied-digest-bound-unverified-author"
    enough = len(interruptions) >= 50 and len(matched) >= 49 and len(interruptions) - len(matched) <= 1
    result["e4_kill_gate"] = {
        "status": ("candidate" if false_candidates <= 1 else "kill") if qualified and enough else "insufficient",
        "reason": ("requires digest-bound external labels, at least 50 interruptions, at least 49 matched "
                   "detections, no more than one miss, and no more than one false candidate; supplied labels "
                   "are not author-authenticated, so this tool never reports a final pass"),
        "label_provenance": metadata.get("label_provenance", "synthetic-or-unqualified"),
    }
    if processed_frames is not None:
        result["suppression"] = {
            "erle_db": round(10 * math.log10(echo_in / echo_out), 3) if echo_in and echo_out else None,
            "samples": echo_count,
        }
    return result


def synthetic(scenario):
    rate = int(scenario.get("rate", 16000))
    duration_ms = int(scenario["duration_ms"])
    if rate <= 0 or duration_ms <= 0 or duration_ms > MAX_SECONDS * 1000:
        raise ValueError("synthetic rate/duration is invalid or exceeds the evidence bound")
    count = rate * duration_ms // 1000
    rng = random.Random(int(scenario.get("seed", 1)))
    reference = [rng.uniform(-0.25, 0.25) for _ in range(count)]
    lag = rate * int(scenario.get("echo_lag_ms", 45)) // 1000
    gain = float(scenario.get("echo_gain", 0.35))
    microphone = [rng.uniform(-0.002, 0.002) for _ in range(count)]
    for index in range(lag, count):
        microphone[index] += gain * reference[index - lag]
    for item in scenario.get("interruptions", []) + scenario.get("disturbances", []):
        begin = rate * int(item["start_ms"]) // 1000
        end = rate * int(item["end_ms"]) // 1000
        near_gain = float(item.get("gain", 0.12))
        for index in range(begin, min(end, count)):
            microphone[index] += near_gain * rng.uniform(-1.0, 1.0)
    metadata = dict(scenario)
    metadata.setdefault("playback", [{"start_ms": 0, "end_ms": duration_ms}])
    return reference, microphone, rate, metadata


def check_expectation(result, expected):
    gate = result["acoustic_gate"]
    checks = {
        "false_candidates_max": gate["false_candidates"] <= expected.get("false_candidates_max", math.inf),
        "detected_interruptions_min": gate["detected_interruptions"] >= expected.get("detected_interruptions_min", 0),
        "missed_interruptions_max": gate["missed_interruptions"] <= expected.get("missed_interruptions_max", math.inf),
    }
    return {"pass": all(checks.values()), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--scenarios", type=Path, help="deterministic public scenario definitions")
    source.add_argument("--mic", type=Path, help="offline mono PCM16 microphone WAV")
    parser.add_argument("--reference", type=Path, help="matching offline playback-reference WAV")
    parser.add_argument("--events", type=Path, help="monotonic playback and labelled-interruption windows")
    parser.add_argument("--labels", type=Path,
                        help="separate externally authored, trace-digest-bound interruption truth")
    parser.add_argument("--processed", type=Path, help="optional candidate-AEC output WAV for ERLE only")
    args = parser.parse_args()
    reports = []
    if args.scenarios:
        document = json.loads(args.scenarios.read_text())
        for scenario in document["scenarios"]:
            reference, microphone, rate, metadata = synthetic(scenario)
            report = {"name": scenario["name"], **analyse(reference, microphone, rate, metadata)}
            if "expect" in scenario:
                report["fixture_expectation"] = check_expectation(report, scenario["expect"])
            reports.append(report)
    else:
        if args.reference is None or args.events is None:
            parser.error("--mic requires --reference and --events")
        mic_rate, microphone = read_mono(args.mic)
        ref_rate, reference = read_mono(args.reference)
        if mic_rate != ref_rate:
            raise ValueError("microphone and reference rates differ")
        processed = None
        if args.processed:
            processed_rate, processed = read_mono(args.processed)
            if processed_rate != mic_rate:
                raise ValueError("processed and microphone rates differ")
        events_raw = regular_bytes(args.events, MAX_EVENTS_BYTES, "event manifest")
        try:
            metadata = json.loads(events_raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("event manifest is invalid") from error
        if not isinstance(metadata, dict):
            raise ValueError("event manifest is invalid")
        labels = bounded_json(args.labels, "external labels") if args.labels else None
        metadata = labelled_metadata(metadata, labels, args.mic, args.reference, events_raw,
                                     min(len(microphone), len(reference)) * 1000 / mic_rate)
        provenance = trace_provenance(metadata, mic_rate, len(microphone), len(reference))
        reports.append({"name": metadata.get("name", "native-trace"), "provenance": provenance,
                        **analyse(reference, microphone, mic_rate, metadata, processed)})
    print(json.dumps({"schema": 1, "reports": reports}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
