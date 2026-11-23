"""Test the fixed OpenJev second opinion on a fresh local recall archive."""

import argparse
import asyncio
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import statistics

from . import decision_holdout as holdout
from .decision_eval import named_endpoint, positive_seconds, prepare_cases, run_provider, summarize


CUTOFF = 0.80
DEADLINES_MS = (250, 500, 750, 1000)


def consult(baseline):
    return not baseline.bypassed and baseline.route == "search_history"


def apply_opinion(baseline, opinion, *, deadline_ms=math.inf):
    if (
        consult(baseline)
        and opinion is not None
        and opinion.elapsed_ms <= deadline_ms
        and opinion.probability is not None
        and opinion.probability >= CUTOFF
        and opinion.route in ("conversation", "ask_clarification")
    ):
        return replace(baseline, route=opinion.route, source=None)
    return baseline


def evaluate(prepared, opinions, *, deadline_ms=math.inf):
    predictions = {
        row.case.id: apply_opinion(row.baseline, opinions.get(row.case.id), deadline_ms=deadline_ms)
        for row in prepared
    }
    metrics = summarize(prepared, predictions)
    # Replay changes routes and sources only. The provider stage is timed separately.
    for field in ("route_p50_ms", "route_p95_ms"):
        del metrics[field]
    metrics["lost_baseline_sources"] = [
        row.case.id for row in prepared
        if row.case.source is not None and row.baseline.source == row.case.source
        and predictions[row.case.id].source != row.case.source
    ]
    metrics["ordinary_context_prevented"] = [
        row.case.id for row in prepared
        if row.case.route == "conversation" and row.baseline.route == "search_history"
        and predictions[row.case.id].route != "search_history"
    ]
    metrics["applied_routes"] = {
        row.case.id: predictions[row.case.id].route for row in prepared
        if predictions[row.case.id] != row.baseline
    }
    return metrics, predictions


def timing(attempts, opinions):
    samples = sorted(opinion.elapsed_ms for opinion in opinions.values())
    return {
        "attempts": attempts,
        "responses": len(samples),
        "p50_ms": round(statistics.median(samples), 2) if samples else None,
        "p95_ms": round(samples[math.ceil(len(samples) * 0.95) - 1], 2) if samples else None,
        "responses_by_deadline_ms": {ms: sum(elapsed <= ms for elapsed in samples) for ms in DEADLINES_MS},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=named_endpoint, required=True, metavar="ALIAS=LOOPBACK_URL")
    parser.add_argument("--timeout", type=positive_seconds, default=8.0, help="seconds per request")
    parser.add_argument("--budget", type=positive_seconds, default=150.0, help="seconds for the model run")
    parser.add_argument("--json", type=Path, help="write the synthetic per-case results to this file")
    args = parser.parse_args()

    prepared = asyncio.run(prepare_cases(holdout.CASES, holdout.ARCHIVE))
    proposed = [row for row in prepared if consult(row.baseline)]
    alias, url = args.local
    provider = run_provider(args, proposed, url, alias)
    opinions = provider["predictions"]
    baseline, _ = evaluate(prepared, {})
    gated, predictions = evaluate(prepared, opinions)
    deadlines = {ms: evaluate(prepared, opinions, deadline_ms=ms)[0] for ms in DEADLINES_MS}
    elapsed = timing(len(proposed), opinions)
    controls = summarize(prepared, {row.case.id: row.baseline for row in prepared}, bypassed=True)
    output = {
        "cutoff": CUTOFF,
        "provider": {key: value for key, value in provider.items() if key != "predictions"},
        "baseline": baseline,
        "second_opinion": gated,
        "deadlines_ms": deadlines,
        "decision_timing": elapsed,
        "parser_bypass": controls,
        "cases": [
            {
                **asdict(row.case),
                "baseline": asdict(row.baseline),
                "candidate_ids": [record.id for record in row.candidates],
                "opinion": asdict(opinions[row.case.id]) if row.case.id in opinions else None,
                "final_route": predictions[row.case.id].route,
                "final_source": predictions[row.case.id].source,
            }
            for row in prepared
        ],
    }
    if args.json:
        args.json.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

    cases = gated["cases"]
    print(f"Fixed cutoff {CUTOFF:.2f}, {cases} eligible cases, {controls['cases']} parser controls")
    print(f"Compared {elapsed['responses']}/{elapsed['attempts']} proposed archive lookups")
    print(f"Correct routes {baseline['correct_routes']} -> {gated['correct_routes']} of {cases}")
    print(f"Correct archive sources {baseline['correct_sources']} -> {gated['correct_sources']} "
          f"of {baseline['known_recall_cases']}")
    print(f"Ordinary turns with archive context {baseline['ordinary_with_recall']} -> "
          f"{gated['ordinary_with_recall']} of {baseline['ordinary_cases']}")
    print("Lost baseline-correct cases:", ", ".join(gated["lost_baseline_sources"]) or "none")
    print(f"Decision p50/p95 {elapsed['p50_ms']}/{elapsed['p95_ms']} ms")
    for ms in DEADLINES_MS:
        replay = deadlines[ms]
        on_time = elapsed["responses_by_deadline_ms"][ms]
        print(f"{ms} ms budget: {on_time}/{elapsed['attempts']} calls on time, "
              f"{len(replay['ordinary_context_prevented'])} ordinary attachments avoided, "
              f"{len(replay['lost_baseline_sources'])} correct sources lost")
    if provider["errors"]:
        print(f"{len(provider['errors'])} proposals fell back after request errors or the run budget.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
