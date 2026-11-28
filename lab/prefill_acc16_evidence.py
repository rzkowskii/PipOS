#!/usr/bin/env python3
"""Offline evidence comparison for the experimental AVX1 acc16 prefill kernel.

This program never launches a model or benchmark.  It accepts independently
captured raw receipts from one pinned binary, checks the experiment structure,
and emits a bounded summary.  A receipt is evidence, not authentication: the
input SHA-256 printed in the report must itself be retained independently.

The acquisition contract deliberately compares only the accumulator change.
Every arm uses the 256-row window and tracing; the candidate alone enables
GGML_PIPOS_TILED_ACC16.  WINDOW128 experiments belong in a separate record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import stat
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA = 1
KIND = "pipos-wp-e2-prefill-acc16-native"
FORMATS = ("Q4_K", "Q5_K")
BATCHES = (8, 32, 128)
ARMS = ("baseline", "candidate", "baseline_replica")
MIN_REPEATS = 10
MAX_REPEATS = 100
MAX_INPUT = 8 * 1024 * 1024
MAX_ITERATIONS = 1_000_000
MAX_ELAPSED_NS = 3_600_000_000_000
BOOTSTRAPS = 10_000
AA_TOLERANCE = 0.05
KILL_SPEEDUP = 1.5
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


class EvidenceError(ValueError):
    pass


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{where} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    got = set(value)
    if got != expected:
        raise EvidenceError(
            f"{where} fields differ (missing={sorted(expected-got)}, extra={sorted(got-expected)})"
        )


def _integer(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise EvidenceError(f"{where} must be an integer in [{low}, {high}]")
    return value


def _digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise EvidenceError(f"{where} must be a lowercase SHA-256")
    return value


def _read_stable(path: Path) -> tuple[bytes, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT:
            raise EvidenceError("input must be a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, MAX_INPUT + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_INPUT:
                raise EvidenceError("input exceeds 8 MiB")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise EvidenceError("input changed while it was read")
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    return raw, hashlib.sha256(raw).hexdigest()


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _paired_interval(values: list[float], seed: int) -> tuple[float, float, float]:
    centre = sum(values) / len(values)
    rng = random.Random(seed)
    means = []
    for _ in range(BOOTSTRAPS):
        means.append(sum(values[rng.randrange(len(values))] for _ in values) / len(values))
    return centre, _percentile(means, 0.025), _percentile(means, 0.975)


def _check_artifact(value: Any, where: str) -> dict[str, Any]:
    item = _mapping(value, where)
    _exact_keys(item, {"sha256", "bytes"}, where)
    _digest(item["sha256"], f"{where}.sha256")
    _integer(item["bytes"], f"{where}.bytes", 1, 1 << 50)
    return item


def _expected_trace(fmt: str, arm: str) -> list[str]:
    kernel = "acc16-avx1" if arm == "candidate" else "stock-avx1"
    return [
        f"pipos-tiled-prefill: format={fmt.lower()} window=256",
        f"pipos-tiled-prefill: kernel={kernel}",
    ]


def _check_arm(
    value: Any,
    where: str,
    arm: str,
    fmt: str,
    batch: int,
    binary_sha: str,
    model_sha: str,
    seen_runs: set[str],
    seen_capture: set[int],
) -> dict[str, Any]:
    item = _mapping(value, where)
    fields = {
        "run_id", "captured_ns", "binary_sha256", "model_sha256", "format", "batch",
        "environment", "iterations", "elapsed_ns", "output_sha256", "trace",
    }
    _exact_keys(item, fields, where)
    run_id = item["run_id"]
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or run_id in seen_runs:
        raise EvidenceError(f"{where}.run_id must be bounded and globally unique")
    seen_runs.add(run_id)
    captured = _integer(item["captured_ns"], f"{where}.captured_ns", 1, (1 << 63) - 1)
    if captured in seen_capture:
        raise EvidenceError(f"{where}.captured_ns is reused; fresh arms need distinct receipts")
    seen_capture.add(captured)
    if _digest(item["binary_sha256"], f"{where}.binary_sha256") != binary_sha:
        raise EvidenceError(f"{where} did not use the pinned common binary")
    if _digest(item["model_sha256"], f"{where}.model_sha256") != model_sha:
        raise EvidenceError(f"{where} did not use the pinned format model")
    if item["format"] != fmt or item["batch"] != batch:
        raise EvidenceError(f"{where} repeats the wrong cell identity")
    environment = _mapping(item["environment"], f"{where}.environment")
    _exact_keys(
        environment,
        {"GGML_PIPOS_TILED_TRACE", "GGML_PIPOS_TILED_ACC16", "GGML_PIPOS_TILED_WINDOW128"},
        f"{where}.environment",
    )
    expected_env = {
        "GGML_PIPOS_TILED_TRACE": "1",
        "GGML_PIPOS_TILED_ACC16": "1" if arm == "candidate" else "0",
        "GGML_PIPOS_TILED_WINDOW128": "0",
    }
    if environment != expected_env:
        raise EvidenceError(f"{where}.environment does not isolate the accumulator change")
    iterations = _integer(item["iterations"], f"{where}.iterations", 1, MAX_ITERATIONS)
    elapsed = _integer(item["elapsed_ns"], f"{where}.elapsed_ns", 1, MAX_ELAPSED_NS)
    output = _digest(item["output_sha256"], f"{where}.output_sha256")
    trace = item["trace"]
    if trace != _expected_trace(fmt, arm):
        raise EvidenceError(f"{where}.trace does not prove the requested format/kernel dispatch")
    return {"iterations": iterations, "elapsed_ns": elapsed, "output_sha256": output}


def analyse(document: Any, input_sha256: str) -> dict[str, Any]:
    root = _mapping(document, "root")
    _exact_keys(root, {"schema", "kind", "seed", "binary", "models", "provenance", "rows"}, "root")
    if root["schema"] != SCHEMA or root["kind"] != KIND:
        raise EvidenceError("unsupported schema or evidence kind")
    seed = _integer(root["seed"], "seed", 0, (1 << 63) - 1)
    binary = _check_artifact(root["binary"], "binary")
    models = _mapping(root["models"], "models")
    _exact_keys(models, set(FORMATS), "models")
    for fmt in FORMATS:
        _check_artifact(models[fmt], f"models.{fmt}")
    provenance = _mapping(root["provenance"], "provenance")
    _exact_keys(provenance, {"machine", "cpu_sha256", "conditions_sha256", "acquirer_sha256"}, "provenance")
    if provenance["machine"] != "MacBookPro9,2":
        raise EvidenceError("provenance.machine must be MacBookPro9,2")
    for key in ("cpu_sha256", "conditions_sha256", "acquirer_sha256"):
        _digest(provenance[key], f"provenance.{key}")

    rows = root["rows"]
    if not isinstance(rows, list) or not rows:
        raise EvidenceError("rows must be a non-empty array")
    cells: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    seen_keys: set[tuple[str, int, int]] = set()
    seen_runs: set[str] = set()
    seen_capture: set[int] = set()
    for index, raw_row in enumerate(rows):
        where = f"rows[{index}]"
        row = _mapping(raw_row, where)
        _exact_keys(row, {"repeat", "format", "batch", "order", "arms"}, where)
        repeat = _integer(row["repeat"], f"{where}.repeat", 1, MAX_REPEATS)
        fmt = row["format"]
        batch = row["batch"]
        if fmt not in FORMATS or batch not in BATCHES:
            raise EvidenceError(f"{where} has an unsupported format/batch")
        key = (fmt, batch, repeat)
        if key in seen_keys:
            raise EvidenceError(f"duplicate cell/repeat {key}")
        seen_keys.add(key)
        order = row["order"]
        if not isinstance(order, list) or len(order) != len(ARMS) or set(order) != set(ARMS):
            raise EvidenceError(f"{where}.order must be a permutation of all three arms")
        arms = _mapping(row["arms"], f"{where}.arms")
        _exact_keys(arms, set(ARMS), f"{where}.arms")
        checked = {}
        for arm in ARMS:
            checked[arm] = _check_arm(
                arms[arm], f"{where}.arms.{arm}", arm, fmt, batch,
                binary["sha256"], models[fmt]["sha256"], seen_runs, seen_capture,
            )
        if len({checked[arm]["iterations"] for arm in ARMS}) != 1:
            raise EvidenceError(f"{where} arms use different iteration counts")
        if len({checked[arm]["output_sha256"] for arm in ARMS}) != 1:
            raise EvidenceError(f"{where} has a non-exact candidate or A/A output")
        cells[(fmt, batch)].append({"repeat": repeat, "order": order, "arms": checked})

    expected_count: int | None = None
    summaries = []
    invalid_control = False
    any_below = False
    all_clear = True
    for cell_index, (fmt, batch) in enumerate((f, b) for f in FORMATS for b in BATCHES):
        samples = sorted(cells.get((fmt, batch), []), key=lambda item: item["repeat"])
        if not MIN_REPEATS <= len(samples) <= MAX_REPEATS:
            raise EvidenceError(f"{fmt}/{batch} needs {MIN_REPEATS}..{MAX_REPEATS} fresh repeats")
        if [sample["repeat"] for sample in samples] != list(range(1, len(samples) + 1)):
            raise EvidenceError(f"{fmt}/{batch} repeat numbers must be contiguous from 1")
        if expected_count is None:
            expected_count = len(samples)
        elif len(samples) != expected_count:
            raise EvidenceError("all six cells need the same repeat count")
        first_positions = Counter(sample["order"][0] for sample in samples)
        if any(first_positions[arm] < 2 for arm in ARMS):
            raise EvidenceError(f"{fmt}/{batch} does not balance first-position order")
        speedups = []
        aa_ratios = []
        for sample in samples:
            arms = sample["arms"]
            baseline = arms["baseline"]["elapsed_ns"]
            speedups.append(baseline / arms["candidate"]["elapsed_ns"])
            aa_ratios.append(baseline / arms["baseline_replica"]["elapsed_ns"])
        speed, speed_lo, speed_hi = _paired_interval(speedups, seed ^ (cell_index * 2 + 1))
        aa, aa_lo, aa_hi = _paired_interval(aa_ratios, seed ^ (cell_index * 2 + 2))
        aa_stable = aa_lo <= 1.0 <= aa_hi and abs(aa - 1.0) <= AA_TOLERANCE
        clears = speed_lo >= KILL_SPEEDUP
        below = speed_hi < KILL_SPEEDUP
        invalid_control |= not aa_stable
        any_below |= below
        all_clear &= clears
        summaries.append({
            "format": fmt,
            "batch": batch,
            "repeats": len(samples),
            "exact_output_sha256": samples[0]["arms"]["baseline"]["output_sha256"],
            "speedup_mean": round(speed, 6),
            "speedup_ci95": [round(speed_lo, 6), round(speed_hi, 6)],
            "aa_ratio_mean": round(aa, 6),
            "aa_ratio_ci95": [round(aa_lo, 6), round(aa_hi, 6)],
            "aa_stable": aa_stable,
            "clears_1_5x": clears,
        })
    if invalid_control:
        decision = "invalid-control"
    elif any_below:
        decision = "kill-below-1.5x"
    elif all_clear:
        decision = "continue-all-cells-clear-1.5x"
    else:
        decision = "inconclusive-around-1.5x"
    return {
        "schema": SCHEMA,
        "kind": f"{KIND}-comparison",
        "input_sha256": input_sha256,
        "binary_sha256": binary["sha256"],
        "model_sha256": {fmt: models[fmt]["sha256"] for fmt in FORMATS},
        "decision": decision,
        "threshold_speedup": KILL_SPEEDUP,
        "cells": summaries,
        "note": "Receipt validation is not origin authentication; retain the input digest independently.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="bounded raw JSON evidence receipt")
    args = parser.parse_args()
    try:
        raw, input_sha = _read_stable(args.evidence)
        document = json.loads(raw.decode("utf-8"))
        report = analyse(document, input_sha)
    except (EvidenceError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        print(f"prefill_acc16_evidence: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
