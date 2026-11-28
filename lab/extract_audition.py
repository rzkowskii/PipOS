#!/usr/bin/env python3
"""Run the design 665 synthetic Extract audition on the shipped local model."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mind import ponder
from mind.dream_model import DreamAnswerInvalid, DreamClient, DreamUnavailable

CASES = ROOT / "lab/extract-audition-cases.json"
MAX_CASE_SECONDS = 30.0


def load_cases(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = value.get("cases") if isinstance(value, dict) and value.get("version") == 1 else None
    if not isinstance(cases, list) or len(cases) != 10:
        raise ValueError("the Extract audition needs exactly ten version-1 cases")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
                "id", "request", "source_kind", "excerpts", "permissible_picks"}:
            raise ValueError("invalid Extract case fields")
        excerpts, permissible = case["excerpts"], case["permissible_picks"]
        if (not isinstance(case["id"], str) or not case["id"] or case["id"] in seen
                or not isinstance(case["request"], str) or not case["request"]
                or case["source_kind"] not in ("notes", "list", "conversation")
                or not isinstance(excerpts, list) or not 1 <= len(excerpts) <= ponder.MAX_LINES
                or not all(isinstance(line, str) and line and len(line) <= ponder.MAX_LINE for line in excerpts)
                or not ponder._fits(excerpts)
                or not isinstance(permissible, list) or not permissible):
            raise ValueError(f"invalid Extract case {case.get('id')!r}")
        normalized = []
        for picks in permissible:
            checked = ponder._picks(picks, len(excerpts))
            if checked is None or list(checked) != picks:
                raise ValueError(f"invalid permissible picks for {case['id']}")
            normalized.append(tuple(picks))
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"duplicate permissible picks for {case['id']}")
        seen.add(case["id"])
    return cases


def score(case, picks):
    permissible = [tuple(gold) for gold in case["permissible_picks"]]
    allowed = set().union(*(set(gold) for gold in permissible))
    chosen = set(picks)
    unsupported = sorted(chosen - allowed)
    best = max(permissible, key=lambda gold: (
        len(chosen.intersection(gold)) / max(1, len(set(gold))),
        -len(chosen.symmetric_difference(gold)),
    ))
    expected = set(best)
    return {
        "exact_permissible": tuple(picks) in permissible,
        "best_permissible": list(best),
        "covered": sorted(chosen.intersection(expected)),
        "missing": sorted(expected - chosen),
        "unsupported": unsupported,
        "coverage": round(len(chosen.intersection(expected)) / max(1, len(expected)), 6)
                    if expected else (1.0 if not chosen else 0.0),
    }


def run(args):
    if args.host not in ("127.0.0.1", "::1"):
        raise ValueError("Extract audition permits only a numeric loopback endpoint")
    cases = load_cases(args.cases)
    if args.output.exists():
        raise ValueError(f"refusing to replace {args.output}")
    client = DreamClient(args.host, args.port, timeout=args.timeout)
    rows = []
    for case in cases:
        started = time.monotonic()
        answer, usage = client.json_reply(ponder._request(case["excerpts"]))
        elapsed = time.monotonic() - started
        if elapsed > args.max_case_seconds:
            raise TimeoutError(f"{case['id']} exceeded the {args.max_case_seconds:g}s audition bound")
        raw = answer.get("picks") if isinstance(answer, dict) else None
        picks = ponder._picks(raw, len(case["excerpts"]))
        if picks is None:
            raise DreamAnswerInvalid(f"{case['id']} returned invalid Extract indexes")
        rows.append({"id": case["id"], "source_kind": case["source_kind"], "picks": list(picks),
                     "score": score(case, picks), "seconds": round(elapsed, 6), "usage": usage})
    exact = sum(row["score"]["exact_permissible"] for row in rows)
    report = {"kind": "pipos-extract-audition-v1", "cases": rows,
              "summary": {"cases": len(rows), "exact_permissible": exact,
                          "unsupported_picks": sum(len(row["score"]["unsupported"]) for row in rows),
                          "mean_coverage": round(sum(row["score"]["coverage"] for row in rows) / len(rows), 6),
                          "total_seconds": round(sum(row["seconds"] for row in rows), 6),
                          "max_case_seconds": max(row["seconds"] for row in rows),
                          "request_timeout_seconds": args.timeout,
                          "acceptance_bound_seconds": args.max_case_seconds}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--timeout", type=float, default=MAX_CASE_SECONDS)
    parser.add_argument("--max-case-seconds", type=float, default=MAX_CASE_SECONDS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.max_case_seconds <= 0 or args.timeout > args.max_case_seconds:
        parser.error("timeout must be positive and no greater than --max-case-seconds")
    try:
        run(args)
    except (OSError, ValueError, TimeoutError, DreamUnavailable) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
