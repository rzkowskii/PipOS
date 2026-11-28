#!/usr/bin/env python3
"""Plan and analyse the native WP-A6/WP-D3 paired prefill-floor trial.

This tool never starts a model server and never changes a production setting.
It emits a deterministic acquisition plan, or validates numeric llama-server
timings collected from matched control/candidate servers and estimates the
paired intercept change. Prompt text and generated text are neither accepted
nor emitted.

Plan 10 paired repetitions (the minimum acceptance coverage):

    python3 lab/prefill_floor.py --plan --seed 20261126 --repeats 10 >plan.json

Analyse a filled result document:

    python3 lab/prefill_floor.py --report native-prefill.json >report.json
"""

import argparse
import json
import math
import random
import statistics
from pathlib import Path

DELTAS = (4, 8, 12, 16, 24, 32)
MIN_REPEATS = 10
MIN_ORDER = 4
BOOTSTRAPS = 10000
PASS_MS = 150.0


def plan(seed, repeats):
    rng = random.Random(seed)
    order = ["control-first", "candidate-first"] * ((repeats + 1) // 2)
    rng.shuffle(order)
    return {"schema": 1, "kind": "pipos-prefill-floor-plan", "seed": seed,
            "deltas": list(DELTAS), "repeats": repeats,
            "schedule": [{"repeat": number + 1, "order": order[number], "deltas": list(DELTAS)}
                         for number in range(repeats)]}


def number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


def validate_timing(timing, delta, label, prefix_n):
    if not isinstance(timing, dict):
        raise ValueError(f"{label} timing is not an object")
    prompt_n = timing.get("prompt_n")
    cache_n = timing.get("cache_n")
    if type(prompt_n) is not int or prompt_n != delta:
        raise ValueError(f"{label} prompt_n must equal delta {delta}")
    if type(cache_n) is not int or cache_n != prefix_n or cache_n <= 0:
        raise ValueError(f"{label} cache_n must equal the positive shared prefix_n {prefix_n}")
    prompt_ms = number(timing.get("prompt_ms"), f"{label} prompt_ms", positive=True)
    # Some llama-server builds also return prompt_per_second. When present it
    # must agree with prompt_n/prompt_ms closely enough to catch mixed rows.
    rate = timing.get("prompt_per_second", timing.get("prompt_tps"))
    if rate is not None:
        rate = number(rate, f"{label} prompt rate", positive=True)
        expected = prompt_n * 1000.0 / prompt_ms
        if abs(rate - expected) > max(0.5, expected * 0.03):
            raise ValueError(f"{label} prompt rate disagrees with prompt_n/prompt_ms")
    return prompt_ms


def validate(document):
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError("prefill evidence must use schema 1")
    if document.get("kind") != "pipos-prefill-floor-native":
        raise ValueError("not a native prefill-floor evidence document")
    seed = document.get("seed")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    variants = document.get("variants")
    if not isinstance(variants, dict) or set(variants) != {"control", "candidate"}:
        raise ValueError("variants must name exactly control and candidate")
    for name, value in variants.items():
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
            raise ValueError(f"{name} needs a non-empty id")
        digest = value.get("model_sha256")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise ValueError(f"{name} needs the exact model_sha256")
        if not isinstance(value.get("server_build"), str) or not value["server_build"]:
            raise ValueError(f"{name} needs the exact server_build")
        if not isinstance(value.get("settings"), dict):
            raise ValueError(f"{name} needs its exact server settings")
    if variants["control"]["id"] == variants["candidate"]["id"]:
        raise ValueError("control and candidate ids must differ")
    if variants["control"]["model_sha256"] != variants["candidate"]["model_sha256"]:
        raise ValueError("control and candidate must use the same model bytes")
    if variants["control"]["server_build"] != variants["candidate"]["server_build"]:
        raise ValueError("control and candidate must use the same server build")
    if variants["control"]["settings"] == variants["candidate"]["settings"]:
        raise ValueError("control and candidate settings do not differ")
    rows = document.get("rows")
    if not isinstance(rows, list) or len(rows) > 512:
        raise ValueError("rows must be a bounded list")
    repeats = sorted({row.get("repeat") for row in rows if isinstance(row, dict)})
    if any(type(value) is not int or value < 1 for value in repeats):
        raise ValueError("repeat numbers must be positive integers")
    expected_plan = plan(seed, max(repeats, default=0))["schedule"]
    schedule = {item["repeat"]: item["order"] for item in expected_plan}
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each row must be an object")
        repeat, delta, order = row.get("repeat"), row.get("delta"), row.get("order")
        if type(repeat) is not int or delta not in DELTAS or order != schedule.get(repeat):
            raise ValueError("row repeat/delta/order does not match the seeded schedule")
        key = repeat, delta
        if key in found:
            raise ValueError(f"duplicate repeat/delta row {key}")
        if type(row.get("slot")) is not int or row["slot"] < 0:
            raise ValueError("each pair needs one non-negative slot id")
        prefix_n = row.get("prefix_n")
        if type(prefix_n) is not int or prefix_n <= 0:
            raise ValueError("each pair needs a positive prefix_n")
        control = validate_timing(row.get("control"), delta, f"repeat {repeat} control", prefix_n)
        candidate = validate_timing(row.get("candidate"), delta, f"repeat {repeat} candidate", prefix_n)
        found[key] = {"repeat": repeat, "delta": delta, "order": order, "slot": row["slot"],
                      "prefix_n": prefix_n, "control_ms": control, "candidate_ms": candidate}
    return seed, variants, found


def intercept(points):
    xs = [float(delta) for delta, _ in points]
    ys = [float(value) for _, value in points]
    xbar, ybar = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((x - xbar) ** 2 for x in xs)
    if denominator <= 0:
        raise ValueError("cannot fit an intercept without distinct deltas")
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denominator
    intercept_ = ybar - slope * xbar
    residual = sum((y - (intercept_ + slope * x)) ** 2 for x, y in zip(xs, ys))
    total = sum((y - ybar) ** 2 for y in ys)
    r2 = 1.0 if total == 0 and residual == 0 else 0.0 if total == 0 else 1.0 - residual / total
    return intercept_, slope, r2


def percentile(values, fraction):
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def paired_ci(values, seed):
    rng = random.Random(seed ^ 0xA6D3)
    means = []
    for _ in range(BOOTSTRAPS):
        means.append(statistics.mean(values[rng.randrange(len(values))] for _ in values))
    return percentile(means, 0.025), percentile(means, 0.975)


def report(document):
    seed, variants, rows = validate(document)
    repeats = sorted({repeat for repeat, _ in rows})
    complete = [repeat for repeat in repeats if all((repeat, delta) in rows for delta in DELTAS)]
    curves = {}
    for delta in DELTAS:
        cells = [rows[(repeat, delta)] for repeat in complete]
        curves[str(delta)] = {
            "pairs": len(cells),
            "control_median_ms": statistics.median(row["control_ms"] for row in cells) if cells else None,
            "candidate_median_ms": statistics.median(row["candidate_ms"] for row in cells) if cells else None,
            "paired_saving_median_ms": statistics.median(
                row["control_ms"] - row["candidate_ms"] for row in cells) if cells else None,
        }
    improvements, fits, linear_repeats = [], [], []
    for repeat in complete:
        control_i, control_s, control_r2 = intercept(
            [(delta, rows[(repeat, delta)]["control_ms"]) for delta in DELTAS])
        candidate_i, candidate_s, candidate_r2 = intercept(
            [(delta, rows[(repeat, delta)]["candidate_ms"]) for delta in DELTAS])
        linear = control_r2 >= 0.90 and candidate_r2 >= 0.90
        if linear:
            improvements.append(control_i - candidate_i)
            linear_repeats.append(repeat)
        fits.append({"repeat": repeat, "order": rows[(repeat, DELTAS[0])]["order"],
                     "linear": linear, "control_r2": round(control_r2, 4),
                     "candidate_r2": round(candidate_r2, 4),
                     "control_intercept_ms": round(control_i, 3),
                     "candidate_intercept_ms": round(candidate_i, 3),
                     "saving_ms": round(control_i - candidate_i, 3),
                     "control_slope_ms_per_token": round(control_s, 4),
                     "candidate_slope_ms_per_token": round(candidate_s, 4)})
    order_counts = {name: sum(rows[(repeat, DELTAS[0])]["order"] == name for repeat in linear_repeats)
                    for name in ("control-first", "candidate-first")}
    covered = len(linear_repeats) >= MIN_REPEATS and all(count >= MIN_ORDER for count in order_counts.values())
    low = high = mean = None
    if covered:
        mean = statistics.mean(improvements)
        low, high = paired_ci(improvements, seed)
    accepted = covered and low is not None and low >= PASS_MS
    return {"schema": 1, "kind": "pipos-prefill-floor-report", "seed": seed,
            "variants": variants, "deltas": list(DELTAS), "complete_repeats": len(complete),
            "linear_repeats": len(linear_repeats),
            "order_counts": order_counts, "curves": curves, "paired_fits": fits,
            "intercept_saving_ms": {"mean": round(mean, 3) if mean is not None else None,
                                    "ci95": ([round(low, 3), round(high, 3)] if low is not None else None)},
            "coverage_ok": covered, "accepted": accepted,
            "decision": ("candidate clears the >=150 ms paired intercept gate" if accepted else
                         "insufficient balanced coverage" if not covered else
                         "candidate does not clear the >=150 ms lower-confidence gate")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--report", type=Path, metavar="JSON")
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--repeats", type=int, default=MIN_REPEATS)
    args = parser.parse_args(argv)
    if args.plan:
        if not MIN_REPEATS <= args.repeats <= 64:
            parser.error("--repeats must be 10-64")
        result = plan(args.seed, args.repeats)
    else:
        try:
            result = report(json.loads(args.report.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
