"""Qualify the experimental WP-D11 facts-before-episodes recall cascade.

This host-only benchmark uses the fixed 30-question recall acceptance set and
the shipped ``Memory.recall`` / ``Memory.recall_history`` APIs.  It does not
change product retrieval order.  All archives are temporary and synthetic.

    python3 -m lab.bench_recall_cascade
    python3 -m lab.bench_recall_cascade --model out/models/embeddings/blink-8M
"""

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time

from mind import embed
from mind import memory as memory_source
from mind.memory import Memory
from tests import test_memory_hybrid as fixture_source
from tests.test_memory_hybrid import (
    FACTS, HARD, HELD_OUT, MIXED, MODEL, UNRELATED, UNRELATED_PERSONAL,
)


OWNER = "Synthetic Owner"
HIT1_FLOOR = 24
FALSE_RECALL_LIMIT = 1
OBVIOUS_FLOOR = len(FACTS)
MAX_RESULTS = 3
TIMING_REPEATS = 3
GENERALISATION_FLOOR = 17
ARMS = ("facts", "episodes", "cascade")


@dataclass(frozen=True)
class Probe:
    label: str
    group: str
    query: str
    expected: int | None
    start: float | None = None
    end: float | None = None
    time_basis: str = "told"
    temporal: bool = False


def _source(memory, fact_id):
    """Resolve a fact through its retained canonical owner episode."""
    row = memory.db.execute("SELECT source FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row or row[0] is None:
        return None
    episodes = memory.consolidation_sources((row[0],), OWNER, limit=1)
    return episodes[0].id if episodes else None


def _facts(memory, probe):
    found = memory.recall(probe.query, limit=MAX_RESULTS)
    results = []
    for row in found:
        source = _source(memory, row.id)
        results.append({"layer": "fact", "fact_id": row.id, "episode_id": source,
                        "source_verified": source is not None})
    return results


def _episodes(memory, probe):
    found = memory.recall_history(
        probe.query, limit=MAX_RESULTS, start=probe.start, end=probe.end,
        kind="heard", speaker=OWNER, time_basis=probe.time_basis,
    )
    return [{"layer": "episode", "fact_id": None, "episode_id": row.id,
             "source_verified": True} for row in found]


def retrieve(memory, probe, arm):
    if arm == "facts":
        return [] if probe.temporal else _facts(memory, probe)
    if arm == "episodes":
        return _episodes(memory, probe)
    if arm != "cascade":
        raise ValueError("unknown retrieval arm")
    # An explicitly classified time range is episode evidence. Facts have a
    # telling/confirmation time, but do not represent an event-range result.
    if probe.temporal:
        return _episodes(memory, probe)
    facts = _facts(memory, probe)
    return facts if facts else _episodes(memory, probe)


def seed(memory):
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    probes = []
    sources = {}
    for index, text in enumerate(FACTS):
        memory.now = lambda moment=now + timedelta(hours=index): moment.timestamp()
        episode = memory.log("heard", text, OWNER)
        memory.remember(text, OWNER, source=episode)
        sources[text] = episode
    for index, (text, _) in enumerate(HELD_OUT):
        memory.now = lambda moment=now + timedelta(days=2, hours=index): moment.timestamp()
        episode = memory.log("heard", text, OWNER)
        memory.remember(text, OWNER, source=episode)
        sources[text] = episode

    probes.extend(Probe(f"mixed-{index:02d}", "acceptance", query, sources[text])
                  for index, (text, query) in enumerate(zip(FACTS, MIXED)))
    probes.extend(Probe(f"hard-{index:02d}", "hard", query, sources[text])
                  for index, (text, query) in enumerate(zip(FACTS, HARD)))
    probes.extend(Probe(f"held-{index:02d}", "held_out", query, sources[text])
                  for index, (text, query) in enumerate(HELD_OUT))
    probes.extend(Probe(f"literal-{index:02d}", "obvious_literal", text, sources[text])
                  for index, text in enumerate(FACTS))
    probes.extend(Probe(f"unrelated-{index:02d}", "abstain_public", query, None)
                  for index, query in enumerate(UNRELATED))
    probes.extend(Probe(f"unknown-{index:02d}", "abstain_personal", query, None)
                  for index, query in enumerate(UNRELATED_PERSONAL))

    memory.now = lambda: datetime(2026, 1, 20, tzinfo=timezone.utc).timestamp()
    forgotten_episode = memory.log("heard", "The synthetic loft code was Amber Kestrel.", OWNER)
    forgotten_fact = memory.remember(
        "The synthetic loft code was Amber Kestrel.", OWNER, source=forgotten_episode)
    probes.append(Probe("forgotten-source", "retention", "what was the synthetic loft code", None))

    old_day = datetime(2026, 2, 1, tzinfo=timezone.utc)
    new_day = datetime(2026, 2, 10, tzinfo=timezone.utc)
    memory.now = lambda: (old_day + timedelta(hours=12)).timestamp()
    old = memory.log("heard", "The boiler pressure reading was 1.2 bar.", OWNER)
    memory.remember("The boiler pressure reading was 1.2 bar.", OWNER, source=old)
    memory.now = lambda: (new_day + timedelta(hours=12)).timestamp()
    new = memory.log("heard", "The boiler pressure reading is now 1.5 bar.", OWNER)
    memory.remember("The boiler pressure reading is now 1.5 bar.", OWNER, source=new)
    probes.extend((
        Probe("temporal-old", "temporal_told", "boiler pressure reading", old,
              old_day.timestamp(), (old_day + timedelta(days=1)).timestamp(), temporal=True),
        Probe("temporal-new", "temporal_told", "boiler pressure reading", new,
              new_day.timestamp(), (new_day + timedelta(days=1)).timestamp(), temporal=True),
    ))

    event_day = datetime(2026, 3, 5, tzinfo=timezone.utc)
    told_day = datetime(2026, 3, 12, tzinfo=timezone.utc)
    memory.now = lambda: told_day.timestamp()
    event = memory.log(
        "heard", "At the March inspection the boiler pressure was 1.4 bar.", OWNER,
        event_start=event_day.timestamp(), event_end=(event_day + timedelta(days=1)).timestamp(),
    )
    memory.remember("At the March inspection the boiler pressure was 1.4 bar.", OWNER, source=event)
    probes.extend((
        Probe("event-range", "temporal_event", "March inspection boiler pressure", event,
              event_day.timestamp(), (event_day + timedelta(days=1)).timestamp(), "event", True),
        Probe("event-not-told-range", "temporal_separation", "March inspection boiler pressure", None,
              event_day.timestamp(), (event_day + timedelta(days=1)).timestamp(), "told", True),
    ))
    return probes, {"fact_id": forgotten_fact, "episode_id": forgotten_episode,
                    "query": "what was the synthetic loft code"}


def evaluate(memory, probes, arm):
    groups = {}
    rows = []
    for probe in probes:
        found = retrieve(memory, probe, arm)
        episode_ids = [row["episode_id"] for row in found if row["episode_id"] is not None]
        hit1 = probe.expected is not None and episode_ids[:1] == [probe.expected]
        hit3 = probe.expected is not None and probe.expected in episode_ids
        false_recall = probe.expected is None and bool(found)
        group = groups.setdefault(probe.group, {"queries": 0, "hit_at_1": 0, "hit_at_3": 0,
                                                "false_recalls": 0, "abstentions": 0})
        group["queries"] += 1
        group["hit_at_1"] += hit1
        group["hit_at_3"] += hit3
        group["false_recalls"] += false_recall
        group["abstentions"] += not found
        rows.append({"case": probe.label, "expected_episode": probe.expected,
                     "provenance": found, "hit_at_1": hit1, "hit_at_3": hit3,
                     "abstained": not found})
    return {"groups": groups, "cases": rows}


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def paired_measure(memory, probes):
    """Warm once, then rotate arm order for every probe and repetition."""
    for index, probe in enumerate(probes):
        rotation = index % len(ARMS)
        for arm in ARMS[rotation:] + ARMS[:rotation]:
            retrieve(memory, probe, arm)

    timings = {arm: [] for arm in ARMS}
    paired = {"cascade_minus_episodes_ms": [], "cascade_minus_facts_ms": []}
    for repeat in range(TIMING_REPEATS):
        for index, probe in enumerate(probes):
            rotation = (repeat + index) % len(ARMS)
            elapsed = {}
            for arm in ARMS[rotation:] + ARMS[:rotation]:
                started = time.perf_counter_ns()
                retrieve(memory, probe, arm)
                elapsed[arm] = (time.perf_counter_ns() - started) / 1_000_000
                timings[arm].append(elapsed[arm])
            paired["cascade_minus_episodes_ms"].append(elapsed["cascade"] - elapsed["episodes"])
            paired["cascade_minus_facts_ms"].append(elapsed["cascade"] - elapsed["facts"])
    return {
        "method": f"one unmeasured warm pass; {TIMING_REPEATS} measured repetitions; "
                  "arm order rotates by repetition and case; perf_counter_ns wall time",
        "host_only_not_native_gate": True,
        "arms": {arm: {"samples": len(values),
                        "median_ms": round(statistics.median(values), 6),
                        "p95_ms": round(_percentile(values, 0.95), 6)}
                 for arm, values in timings.items()},
        "paired": {name: {"samples": len(values),
                           "median_ms": round(statistics.median(values), 6),
                           "p95_ms": round(_percentile(values, 0.95), 6)}
                   for name, values in paired.items()},
    }


def _indexed(memory, control):
    fact, episode = control["fact_id"], control["episode_id"]
    fact_row = memory.db.execute("SELECT source FROM facts WHERE id=?", (fact,)).fetchone()
    episode_row = memory.db.execute("SELECT text, speaker FROM episodes WHERE id=?", (episode,)).fetchone()
    return {
        "fact_exists": fact_row is not None,
        "fact_source_is_episode": bool(fact_row and fact_row[0] == episode),
        "episode_exists": episode_row is not None,
        "episode_is_tombstone": episode_row == ("[forgotten]", None),
        "fact_vector": memory.db.execute(
            "SELECT 1 FROM fact_vec WHERE fact_id=?", (fact,)).fetchone() is not None,
        "episode_vector": memory.db.execute(
            "SELECT 1 FROM episode_vec WHERE episode_id=?", (episode,)).fetchone() is not None,
        "fact_fts": memory.db.execute(
            "SELECT 1 FROM facts_fts WHERE rowid=? AND facts_fts MATCH 'synthetic loft code'", (fact,)
        ).fetchone() is not None,
        "episode_fts": memory.db.execute(
            "SELECT 1 FROM episodes_fts WHERE rowid=? AND episodes_fts MATCH 'synthetic loft code'", (episode,)
        ).fetchone() is not None,
    }


def forgetting_evidence(memory, control):
    before = _indexed(memory, control)
    expected = control["episode_id"]
    probe = Probe("forget-control-before", "retention", control["query"], expected)
    before_retrieval = {arm: expected in [row["episode_id"] for row in retrieve(memory, probe, arm)]
                        for arm in ARMS}
    if not all(before_retrieval.values()) or not all(
            before[key] for key in ("fact_exists", "fact_source_is_episode", "episode_exists",
                                    "fact_vector", "episode_vector", "fact_fts", "episode_fts")):
        raise RuntimeError("synthetic retention control was not indexed and retrievable before forgetting")
    if not memory.edit(fact_ids=(control["fact_id"],)):
        raise RuntimeError("synthetic retention control could not be forgotten")
    after = _indexed(memory, control)
    forgotten = Probe("forget-control-after", "retention", control["query"], None)
    after_retrieval = {arm: bool(retrieve(memory, forgotten, arm)) for arm in ARMS}
    passed = (not any(after_retrieval.values()) and not after["fact_exists"]
              and after["episode_exists"] and after["episode_is_tombstone"]
              and not any(after[key] for key in ("fact_vector", "episode_vector", "fact_fts", "episode_fts")))
    return {"synthetic_fact_id": control["fact_id"], "retained_source_episode_id": expected,
            "before": before, "before_retrievable": before_retrieval,
            "after": after, "after_returned_anything": after_retrieval, "passes": passed}


def _sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def exact_provenance(model, model_dir, probes):
    model_dir = Path(model_dir).resolve()
    model_files = {}
    for name in ("model.safetensors", "tokenizer.json"):
        path = model_dir / name
        model_files[name] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    sources = {}
    for label, path in (("harness", __file__), ("memory", memory_source.__file__),
                        ("embed", embed.__file__), ("acceptance_fixture", fixture_source.__file__)):
        sources[label] = _sha256(path)
    manifest = [asdict(probe) for probe in probes]
    corpus = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {"sources_sha256": sources, "model_id": model.model_id,
            "model_files": model_files, "case_manifest_sha256": hashlib.sha256(corpus).hexdigest(),
            "retrieval": {"max_results": MAX_RESULTS, "timing_repeats": TIMING_REPEATS},
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "machine": platform.machine()}


def measure(model, model_dir):
    with tempfile.TemporaryDirectory(prefix="pip-private-recall-cascade-") as directory:
        with closing(Memory(Path(directory), embedder=model)) as memory:
            probes, control = seed(memory)
            while memory.backfill_vectors(128):
                pass
            forgetting = forgetting_evidence(memory, control)
            arms = {arm: evaluate(memory, probes, arm) for arm in ARMS}
            timing = paired_measure(memory, probes)
            time_bounds = memory.db.execute("SELECT min(ts), max(ts) FROM episodes").fetchone()
    cascade = arms["cascade"]["groups"]
    facts = arms["facts"]["groups"]
    episodes = arms["episodes"]["groups"]
    public_false = cascade["abstain_public"]["false_recalls"]
    temporal = ("temporal_told", "temporal_event", "temporal_separation")
    quality_groups = ("acceptance", "hard", "held_out", "obvious_literal")
    no_quality_regression = all(
        cascade[group]["hit_at_1"] >= max(facts[group]["hit_at_1"], episodes[group]["hit_at_1"])
        for group in quality_groups)
    no_abstention_regression = all(
        cascade[group]["false_recalls"] <= min(facts[group]["false_recalls"], episodes[group]["false_recalls"])
        for group in ("abstain_public", "abstain_personal", "retention"))
    strictly_better = any(
        cascade[group]["hit_at_1"] > episodes[group]["hit_at_1"] for group in quality_groups)
    strictly_better |= any(
        cascade[group]["false_recalls"] < episodes[group]["false_recalls"]
        for group in ("abstain_public", "abstain_personal"))
    retrieval_gate = (
        cascade["acceptance"]["hit_at_1"] >= HIT1_FLOOR
        and cascade["hard"]["hit_at_1"] >= GENERALISATION_FLOOR
        and cascade["held_out"]["hit_at_1"] >= GENERALISATION_FLOOR
        and cascade["obvious_literal"]["hit_at_1"] == OBVIOUS_FLOOR
        and public_false <= FALSE_RECALL_LIMIT
        and cascade["retention"]["false_recalls"] == 0
        and all(cascade[group]["hit_at_1"] == cascade[group]["queries"] for group in temporal[:2])
        and cascade["temporal_separation"]["false_recalls"] == 0
        and no_quality_regression and no_abstention_regression and strictly_better
        and forgetting["passes"]
    )
    return {
        "format": "being-recall-cascade-qualification-v2",
        "scope": "temporary synthetic archive; experimental lab ordering only; no product behavior change",
        "provenance": exact_provenance(model, model_dir, probes),
        "synthetic_time_bounds": {
            "timezone": "UTC", "earliest": datetime.fromtimestamp(time_bounds[0], timezone.utc).isoformat(),
            "latest": datetime.fromtimestamp(time_bounds[1], timezone.utc).isoformat(),
            "range_semantics": "start inclusive, end exclusive"},
        "method": "facts first unless the query seam explicitly supplies a time range; episode fallback only "
                  "after fact abstention; every fact result resolves its retained canonical source episode",
        "floors": {"acceptance_hit_at_1": HIT1_FLOOR, "obvious_literal_hit_at_1": OBVIOUS_FLOOR,
                   "hard_hit_at_1": GENERALISATION_FLOOR,
                   "held_out_hit_at_1": GENERALISATION_FLOOR,
                   "unrelated_false_recall_limit": FALSE_RECALL_LIMIT,
                   "unknown_personal": "must not exceed either paired retrieval baseline",
                   "forgotten_source_false_recall_limit": 0,
                   "temporal_cases_must_all_pass": True,
                   "paired_quality_no_regression": True,
                   "paired_strict_improvement_required": True},
        "arms": arms,
        "timing": timing,
        "forgetting": forgetting,
        "qualification": {"retrieval_gate_passes": retrieval_gate,
                          "paired_quality_no_regression": no_quality_regression,
                          "paired_abstention_no_regression": no_abstention_regression,
                          "paired_strict_improvement": strictly_better,
                          "native_wp_c6_gate_passes": None,
                          "promotion_passes": None,
                          "decision": "experimental only until native WP-C6 and owner acceptance evidence exist"},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--output", type=Path,
                        help="write JSON here instead of stdout (parent must already exist)")
    args = parser.parse_args(argv)
    model = embed.load(args.model)
    if model is None:
        parser.error(f"blink-8M unavailable in {args.model}")
    report = json.dumps(measure(model, args.model), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(report, end="")
    else:
        args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
