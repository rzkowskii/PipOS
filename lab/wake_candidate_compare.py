"""Compare paired PocketSphinx and Sherpa wake evidence, without audio access.

The input is produced by separate, local engine runners over the same
synthetic corpus.  This tool deliberately neither records nor decodes audio:
it validates bounded per-clip results, pins the shipped runtimes, and computes
the paired false-accept/false-reject disposition needed before a Sherpa KWS
candidate may be considered.  It cannot promote an engine or alter Voice.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys


KIND = "pipos-wake-paired-evidence-v1"
REPORT_KIND = "pipos-wake-comparison-v1"
MAX_INPUT = 256 * 1024
MAX_CASES = 512
MAX_SECONDS = 10.0
ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
CONDITIONS = frozenset(("wake", "near_miss", "unaddressed", "noise"))
ENGINES = ("pocketsphinx", "sherpa")

# Source archives already pinned by Buildroot. These identify the runtime
# source under comparison; model bytes remain separately mandatory below.
RUNTIME_PINS = {
    "pocketsphinx": {
        "version": "5.0.4",
        "source_sha256": "9acd63cfdc76bbc85b7b6c6610b7a9cf30c79d5b36a31de7dc2f969e962653d7",
    },
    "sherpa": {
        "version": "2.0.3",
        "source_sha256": "615b1bf0140bf09051c15fde6579e48543696352ebd477217d2e69b49d9aec13",
    },
}


def _read_json(path):
    with path.open("rb") as source:
        raw = source.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("evidence packet exceeds 256 KiB")
    return json.loads(raw)


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _provenance(name, value):
    if not isinstance(value, dict):
        raise ValueError(f"{name} provenance is missing")
    pin = RUNTIME_PINS[name]
    if value.get("version") != pin["version"] or value.get("source_sha256") != pin["source_sha256"]:
        raise ValueError(f"{name} runtime does not match the shipped source pin")
    if not _hex(value.get("binary_sha256")):
        raise ValueError(f"{name} binary digest is missing")
    if not _hex(value.get("corpus_sha256")):
        raise ValueError(f"{name} corpus digest is missing")
    if name == "sherpa":
        if value.get("api") != "sherpa-onnx-c-api" or not _hex(value.get("model_pack_sha256")):
            raise ValueError("Sherpa evidence needs the C API and model descriptor digest")
        if value.get("model_trust") not in ("lab_digest_only", "lab_detached_cms"):
            raise ValueError("Sherpa evidence must state its lab descriptor trust")
        files = value.get("model_files")
        if (not isinstance(files, list) or len(files) != 4
                or any(not isinstance(item, dict) or not isinstance(item.get("role"), str)
                       or not _hex(item.get("sha256")) for item in files)
                or {item["role"] for item in files} != {"encoder", "decoder", "joiner", "tokens"}):
            raise ValueError("Sherpa evidence needs digests for the encoder, decoder, joiner and tokens")
    if name == "pocketsphinx":
        config = value.get("effective_config")
        if (value.get("api") != "being-mind-recognition" or value.get("model_trust") != "local_digest_only"
                or not isinstance(config, dict) or config.get("configured_files") != value.get("model_files")):
            raise ValueError("PocketSphinx evidence needs the configured runtime and model identity")
        computed = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if value.get("model_pack_sha256") != computed:
            raise ValueError("PocketSphinx configuration digest differs")
    return {
        key: value[key] for key in (
            "version", "source_sha256", "binary_sha256", "corpus_sha256", "api", "model_pack_sha256",
            "model_trust", "model_files"
        ) if key in value
    }


def _cases(value):
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_CASES:
        raise ValueError(f"corpus needs two to {MAX_CASES} cases")
    found = {}
    for case in value:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not ID.fullmatch(case["id"]):
            raise ValueError("case IDs must be bounded non-private tokens")
        case_id = case["id"]
        if case_id in found:
            raise ValueError("duplicate case ID")
        if case.get("synthetic") is not True:
            raise ValueError("this source-only gate accepts synthetic cases only")
        condition = case.get("condition")
        if condition not in CONDITIONS:
            raise ValueError("unknown wake condition")
        expected = case.get("expected")
        if type(expected) is not bool or expected != (condition == "wake"):
            raise ValueError("expected must be true exactly for wake cases")
        seconds = case.get("seconds")
        if not _number(seconds, 0.05, MAX_SECONDS):
            raise ValueError("case duration is outside 0.05 to 10 seconds")
        found[case_id] = {"expected": expected, "condition": condition, "seconds": float(seconds)}
    if not any(case["expected"] for case in found.values()) or all(case["expected"] for case in found.values()):
        raise ValueError("corpus needs both wake and negative cases")
    return found


def _inputs(name, value, cases):
    if not isinstance(value, list) or len(value) != len(cases):
        raise ValueError(f"{name} needs one verified input record per case")
    found = {}
    for item in value:
        case_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(case_id, str) or case_id not in cases or case_id in found or item.get("sample_rate") != 16000:
            raise ValueError(f"invalid or duplicate {name} input record")
        frames = item.get("frames")
        if type(frames) is not int or not 800 <= frames <= 160000 or not _hex(item.get("wav_sha256")):
            raise ValueError(f"invalid {name} PCM identity")
        if abs(frames / 16000.0 - cases[case_id]["seconds"]) > 1 / 16000.0:
            raise ValueError(f"{name} PCM frames disagree with declared duration")
        found[case_id] = {"frames": frames, "sample_rate": 16000, "wav_sha256": item["wav_sha256"]}
    return found


def _results(name, value, inputs):
    if not isinstance(value, list) or len(value) != len(inputs):
        raise ValueError(f"{name} needs exactly one result per case")
    found = {}
    for result in value:
        case_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(case_id, str) or case_id not in inputs or case_id in found:
            raise ValueError(f"invalid or duplicate {name} result")
        frames = inputs[case_id]["frames"]
        if type(result.get("consumed_frames")) is not int or result["consumed_frames"] != frames:
            raise ValueError(f"{name} did not process the complete clip")
        detected = result.get("detected")
        trigger = result.get("trigger_audio_ms")
        processing = result.get("processing_ms")
        score = result.get("score")
        limit = frames * 1000 / 16000 + (300 if name == "sherpa" else 780)
        if type(detected) is not bool or (detected and not _number(trigger, 0, limit)) or (not detected and trigger is not None):
            raise ValueError(f"invalid {name} detection or timing")
        if not _number(processing, 0, 60_000):
            raise ValueError(f"invalid {name} wall processing time")
        if score is not None and not _number(score, -1e30, 1e30):
            raise ValueError(f"invalid {name} score")
        found[case_id] = {"detected": detected, "trigger_audio_ms": float(trigger) if detected else None,
                          "processing_ms": float(processing), "score": score}
    return found


def _summary(cases, results):
    counts = {key: 0 for key in ("tp", "fn", "fp", "tn")}
    positive = negative = 0.0
    timings = []
    for case_id, case in cases.items():
        detected = results[case_id]["detected"]
        counts[("tp" if detected else "fn") if case["expected"] else ("fp" if detected else "tn")] += 1
        if detected and case["expected"]:
            timings.append(results[case_id]["trigger_audio_ms"])
        if case["expected"]:
            positive += case["seconds"]
        else:
            negative += case["seconds"]
    positives = counts["tp"] + counts["fn"]
    negatives = counts["fp"] + counts["tn"]
    timings.sort()
    processing = sorted(result["processing_ms"] for result in results.values())
    return {
        **counts,
        "false_reject_rate": round(counts["fn"] / positives, 6),
        "false_accept_rate": round(counts["fp"] / negatives, 6),
        "negative_triggered_clips_per_audio_hour": round(counts["fp"] * 3600 / negative, 6),
        "p95_true_positive_trigger_audio_ms": round(timings[math.ceil(0.95 * len(timings)) - 1], 3) if timings else None,
        "p95_processing_ms": round(processing[math.ceil(0.95 * len(processing)) - 1], 3),
        "positive_audio_seconds": round(positive, 3),
        "negative_audio_seconds": round(negative, 3),
    }


def compare(packet):
    if not isinstance(packet, dict) or packet.get("kind") != KIND:
        raise ValueError(f"expected {KIND}")
    cases = _cases(packet.get("cases"))
    engines = packet.get("engines")
    if not isinstance(engines, dict) or set(engines) != set(ENGINES):
        raise ValueError("evidence needs exactly PocketSphinx and Sherpa")
    provenance = {}
    results, inputs = {}, {}
    for name in ENGINES:
        engine = engines[name]
        if not isinstance(engine, dict):
            raise ValueError(f"{name} evidence is missing")
        provenance[name] = _provenance(name, engine.get("provenance"))
        inputs[name] = _inputs(name, engine.get("inputs"), cases)
        results[name] = _results(name, engine.get("results"), inputs[name])
    corpus_sha = packet.get("corpus_sha256")
    if not _hex(corpus_sha) or any(provenance[name]["corpus_sha256"] != corpus_sha for name in ENGINES):
        raise ValueError("engine corpus digests differ; evidence is not paired")
    if inputs["pocketsphinx"] != inputs["sherpa"]:
        raise ValueError("engines did not consume identical PCM frames")
    identity = [{"id": case_id, "condition": case["condition"], "expected": case["expected"],
                 "wav_sha256": inputs["sherpa"][case_id]["wav_sha256"]}
                for case_id, case in cases.items()]
    actual_corpus = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if actual_corpus != corpus_sha:
        raise ValueError("case labels and WAV digests do not match the reported corpus")

    summaries = {name: _summary(cases, results[name]) for name in ENGINES}
    paired = {
        "positive_sherpa_wins": 0,
        "positive_pocketsphinx_wins": 0,
        "negative_sherpa_wins": 0,
        "negative_pocketsphinx_wins": 0,
    }
    for case_id, case in cases.items():
        pocket = results["pocketsphinx"][case_id]["detected"]
        sherpa = results["sherpa"][case_id]["detected"]
        if pocket == sherpa:
            continue
        if case["expected"]:
            paired["positive_sherpa_wins" if sherpa else "positive_pocketsphinx_wins"] += 1
        else:
            paired["negative_sherpa_wins" if not sherpa else "negative_pocketsphinx_wins"] += 1

    pocket, sherpa = summaries["pocketsphinx"], summaries["sherpa"]
    no_worse = sherpa["fn"] <= pocket["fn"] and sherpa["fp"] <= pocket["fp"]
    strictly_better = sherpa["fn"] < pocket["fn"] or sherpa["fp"] < pocket["fp"]
    descriptive_candidate = no_worse and strictly_better
    return {
        "kind": REPORT_KIND,
        "input_sha256": hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "cases": len(cases),
        "provenance": provenance,
        "engines": summaries,
        "paired": paired,
        "synthetic_descriptive_result": "sherpa_no_worse_and_strictly_better" if descriptive_candidate else "not_demonstrated",
        "acceptance_gate": False,
        "production_promotion": False,
        "remaining_gate": "consented native owner and negative-room trials are required by WP-E5",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, nargs="?")
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--pocketsphinx", type=Path)
    parser.add_argument("--sherpa", type=Path)
    args = parser.parse_args(argv)
    separate = (args.corpus, args.pocketsphinx, args.sherpa)
    if (args.evidence is not None and any(separate)) or (args.evidence is None and not all(separate)):
        parser.error("provide a complete evidence packet or all three of --corpus, --pocketsphinx and --sherpa")
    try:
        if args.evidence is not None:
            packet = _read_json(args.evidence)
        else:
            pocket, sherpa = _read_json(args.pocketsphinx), _read_json(args.sherpa)
            corpus = _read_json(args.corpus)
            if corpus["kind"] != "pipos-wake-synthetic-corpus-v1":
                raise ValueError("unsupported synthetic corpus")
            durations = {item["id"]: item["frames"] / 16000 for item in pocket["inputs"]}
            cases = [{key: case[key] for key in ("id", "synthetic", "condition", "expected")} for case in corpus["cases"]]
            for case in cases:
                case["seconds"] = durations[case["id"]]
            packet = {"kind": KIND, "cases": cases, "corpus_sha256": pocket["provenance"]["corpus_sha256"],
                      "engines": {"pocketsphinx": pocket, "sherpa": sherpa}}
        report = compare(packet)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"wake comparison refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
