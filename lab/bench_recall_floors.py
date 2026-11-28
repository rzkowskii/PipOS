"""Measure hybrid recall's cosine floors on the paraphrase sets (task L8.3).

Uses the fixed sets in tests/test_memory_hybrid.py and the real blink-8M
vectors in out/models/embeddings/blink-8M. Reports hit@1 for the acceptance
set (MIXED), the no-shared-word sets (HARD, HELD_OUT), and false recalls on
the unrelated and unrelated-personal questions, for the hybrid search and for
words alone.

    python3 -m lab.bench_recall_floors            the shipped floors
    python3 -m lab.bench_recall_floors --sweep    a grid around them
    python3 -m lab.bench_recall_floors --archive  1,000 and 10,000 synthetic episodes
    python3 -m lab.bench_recall_floors --decade decision
    python3 -m lab.bench_recall_floors --decade 100k --allow-large

The sweep tunes on FACTS (MIXED, HARD and the unrelated sets against FACTS)
and reports HELD_OUT separately, so a floor is not chosen to fit the set it
is judged on. A setting qualifies only if, on both fact sets, its false
recalls do not exceed words alone and the unrelated set gives at most 1.

Archive mode uses the shipped floors on two on-disk corpora spanning ten
synthetic years. It compares the episode search with and without embeddings
on the same labeled queries. Supply --model when the pack is outside the
usual local model directory.

Decade mode replays exactly 3,650 synthetic days through Memory.log and the
registered nightly archive jobs. Its 10,000-row decision profile is the kill
gate. Larger 100k, 365k and 1.8m profiles are deliberately unavailable without
--allow-large. Every profile has its own database-byte abort bound; all files
live in a private temporary directory and contain synthetic fixtures only.
"""

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import platform
import random
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from mind import embed, memory as memory_module
from mind.memory import Memory
from mind.night_shift import CHUNK_BUDGET, Gate, NightShift, standard_jobs
from tests.test_memory_hybrid import (
    FACTS, HARD, HELD_OUT, MIXED, MODEL, UNRELATED, UNRELATED_PERSONAL, false_recalls, scored,
)

KNOBS = ("COSINE_FLOOR", "COSINE_FLOOR_IMPERSONAL", "COSINE_FLOOR_PARTIAL",
         "COSINE_FLOOR_PARTIAL_IMPERSONAL", "COSINE_WEIGHT")
HELD_FACTS = [fact for fact, _ in HELD_OUT]
HELD_QUERIES = [query for _, query in HELD_OUT]
ARCHIVE_SIZES = (1_000, 10_000)
ARCHIVE_SEED = 20261126
ARCHIVE_START = datetime(2016, 11, 26, tzinfo=timezone.utc)
ARCHIVE_END = datetime(2026, 11, 26, tzinfo=timezone.utc)
ITEMS = ("ferry ticket", "desk lamp", "recipe card", "spare key", "bicycle pump", "library book",
         "garden hose", "coffee filter", "tool box", "wool scarf", "shopping bag", "tomato stakes",
         "station map", "porch lantern", "radio charger", "picnic basket", "orchid pot", "paint sample")
PLACES = ("hall cupboard", "community centre", "garden shed", "bus stop", "kitchen drawer",
          "sideboard", "market stall", "window ledge", "library desk", "bike rack")
DESCRIPTIONS = ("blue", "striped", "wooden", "old", "small", "folded", "spare", "heavy", "green", "borrowed")
CONTEXTS = ("on the way home", "before breakfast", "after rehearsal", "while it was raining",
            "just before the shops closed", "after lunch", "before loading the car", "at the weekend")
EPISODE_TEMPLATES = (
    "I left the {detail} {item} by the {place} {context}.",
    "The {item} from the {place} was the {detail} one I picked up {context}.",
    "At the {place} I looked for a {detail} {item} {context}.",
    "I checked whether the {detail} {item} was still at the {place} {context}.",
    "We talked about putting the {detail} {item} near the {place} {context}.",
)
DECADE_DAYS = 3_650
MAX_NIGHT_CHUNKS_PER_DAY = 10_000


class _CountedJob:
    """Count calls to a registered job's real chunk boundary."""

    def __init__(self, job, counter):
        self.job, self.counter = job, counter

    async def run_chunk(self, budget_s):
        self.counter["chunks"] += 1
        if self.counter["chunks"] > self.counter["limit"]:
            raise RuntimeError("synthetic night exceeded its bounded job-chunk limit")
        return await self.job.run_chunk(budget_s)

    def __getattr__(self, name):
        return getattr(self.job, name)


@dataclass(frozen=True)
class ArchiveProfile:
    episodes: int
    max_database_bytes: int
    large: bool = False


DECADE_PROFILES = {
    "smoke": ArchiveProfile(1_000, 256 * 1024 * 1024),
    # The handover's kill gate: do not proceed past this profile unless its
    # result changes a retrieval decision.
    "decision": ArchiveProfile(10_000, 512 * 1024 * 1024),
    "100k": ArchiveProfile(100_000, 2 * 1024 * 1024 * 1024, True),
    "365k": ArchiveProfile(365_000, 6 * 1024 * 1024 * 1024, True),
    "1.8m": ArchiveProfile(1_800_000, 24 * 1024 * 1024 * 1024, True),
}


@dataclass(frozen=True)
class Probe:
    category: str
    query: str
    expected: int | None
    start: float | None = None
    end: float | None = None
    time_basis: str = "told"
    speaker: str = "Owner"


@contextmanager
def floors(**values):
    old = {name: getattr(memory_module, name) for name in values}
    try:
        for name, value in values.items():
            setattr(memory_module, name, value)
        yield
    finally:
        for name, value in old.items():
            setattr(memory_module, name, value)


def measure(model):
    row = {
        "mixed_hit1": scored(model, FACTS, MIXED),
        "hard_hit1": scored(model, FACTS, HARD),
        "held_out_hit1": scored(model, HELD_FACTS, HELD_QUERIES),
    }
    for label, facts in (("facts", FACTS), ("held_out", HELD_FACTS)):
        row[f"unrelated_false_{label}"] = false_recalls(model, facts, UNRELATED)
        row[f"personal_false_{label}"] = false_recalls(model, facts, UNRELATED_PERSONAL)
    return row


def words_only():
    return measure(None)


def qualifies(row, words):
    return all(
        row[f"unrelated_false_{label}"] <= 1
        and row[f"personal_false_{label}"] <= words[f"personal_false_{label}"]
        and row[f"unrelated_false_{label}"] <= words[f"unrelated_false_{label}"]
        for label in ("facts", "held_out")
    ) and row["mixed_hit1"] >= 24 and row["mixed_hit1"] > words["mixed_hit1"]


def seed_archive(root, size, model):
    """Log the corpus in chronological order through the real archive API."""
    rng = random.Random(ARCHIVE_SEED)
    positions = range(max(ARCHIVE_SIZES) - 3)
    fixture_positions = dict(zip(sorted(rng.sample(positions, len(FACTS))), FACTS))
    distractor_positions = [position for position in positions if position not in fixture_positions]
    included = set(random.Random(ARCHIVE_SEED).sample(distractor_positions, size - 3 - len(FACTS)))
    fixture_ids = {}
    start = ARCHIVE_START.timestamp()
    elapsed = (ARCHIVE_END - timedelta(days=3)).timestamp() - start
    with closing(Memory(root, embedder=None)) as archive:
        now = start
        archive.now = lambda: now
        for index in positions:
            now = start + elapsed * index / len(positions)
            if index in fixture_positions:
                text = fixture_positions[index]
                fixture_ids[text] = archive.log("heard", text, "Owner")
            else:
                text = rng.choice(EPISODE_TEMPLATES).format(
                    item=rng.choice(ITEMS), place=rng.choice(PLACES), detail=rng.choice(DESCRIPTIONS),
                    context=rng.choice(CONTEXTS),
                )
                if index in included:
                    archive.log("heard", text, "Owner")
        now = (ARCHIVE_END - timedelta(days=2)).timestamp()
        prior = archive.log("heard", "The Willow repair club booking is for the morning.", "Owner")
        now = (ARCHIVE_END - timedelta(days=1)).timestamp()
        latest = archive.log("heard", "The Willow repair club booking is for the afternoon.", "Owner")
        update_time = now
        forgotten = archive.log("heard", "The observatory membership voucher code was Violet Marble.", "Owner")
        if not archive.edit(episode_ids=(forgotten,)):
            raise RuntimeError("The synthetic forgotten episode could not be edited")
        archive.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        words_bytes = (root / "mind.db").stat().st_size
        archive.embedder = model
        while archive.backfill_vectors(512):
            pass
        vectors, _ = archive.vector_counts()
        episodes, = archive.db.execute("SELECT count(*) FROM episodes").fetchone()
        if episodes != size or vectors != size - 1:
            raise RuntimeError(f"Expected {size} episodes and {size - 1} vectors, got {episodes} and {vectors}")
        archive.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        indexed_bytes = (root / "mind.db").stat().st_size
    rows = [Probe(category, query, fixture_ids[fact])
            for category, queries in (("mixed", MIXED), ("paraphrase", HARD))
            for fact, query in zip(FACTS, queries)]
    rows.extend(Probe(category, query, None)
                for category, queries in (("unrelated", UNRELATED), ("unknown-personal", UNRELATED_PERSONAL))
                for query in queries)
    booking = "what did I say about the Willow repair club booking"
    rows.extend([
        Probe("updated", booking, latest),
        Probe("before-update", booking, prior, end=update_time),
        Probe("forgotten", "what was the observatory membership voucher code", None),
    ])
    return rows, {"episodes": episodes, "vectors": vectors,
                  "database_without_vectors_bytes": words_bytes, "database_with_vectors_bytes": indexed_bytes}


def _database_bytes(root):
    return sum((root / name).stat().st_size for name in ("mind.db", "mind.db-wal", "mind.db-shm")
               if (root / name).is_file())


async def _run_synthetic_night(memory, root, day):
    """Run and count the real registered archive-job chunk boundaries."""
    label = day.isoformat()
    shift = standard_jobs(
        NightShift(root / "night-state", chunk_budget=CHUNK_BUDGET), memory,
        owner="Synthetic Owner", night=label, today=day, state_dir=root / "night-state",
    )
    counter = {"chunks": 0, "limit": MAX_NIGHT_CHUNKS_PER_DAY}
    # Registration has restored each real job's cursor already. The wrapper
    # observes execution without replacing cursor/interruption behaviour.
    shift.jobs = {name: _CountedJob(job, counter) for name, job in shift.jobs.items()}
    gate = Gate(night=True, on_ac=True, body_ok=True, dreams=True)
    steps = 0
    while not shift.settled(dreams=True):
        result = await shift.step(gate, night=label)
        steps += 1
        if any(state in ("failed", "interrupted", "cleanup_pending", "waiting") for state in result.values()):
            raise RuntimeError(f"synthetic night {label} did not settle: {result}")
    return counter["chunks"], steps


async def seed_decade_archive(root, profile, model):
    """Replay 3,650 synthetic days through Memory.log and nightly archive jobs."""
    rng = random.Random(ARCHIVE_SEED)
    start = ARCHIVE_START
    anchors = {
        180: [("single", "The brass telescope key is in the green sewing tin.")],
        900: [("multi-old", "The Riverside pottery class meets in the west studio.")],
        901: [("multi-new", "The Riverside pottery class moved to the east studio.")],
        1_500: [("temporal", "Yesterday I left the red field notebook at Harbour Library.")],
        2_300: [("update-old", "The Cedar repair appointment is at nine in the morning.")],
        2_900: [("update-new", "The Cedar repair appointment changed to three in the afternoon.")],
        3_200: [("forgotten", "The synthetic observatory passphrase was Copper Comet.")],
    }
    fact_days = {round(index * (DECADE_DAYS - 1) / max(1, len(FACTS) - 1)): fact
                 for index, fact in enumerate(FACTS)}
    reserved = sum(len(rows) for rows in anchors.values()) + len(fact_days)
    if profile.episodes < reserved:
        raise ValueError("archive profile is smaller than its ground-truth fixtures")
    distractors = profile.episodes - reserved
    base, extra = divmod(distractors, DECADE_DAYS)
    ids, fact_ids = {}, {}
    allocation_deltas, night_chunks, night_steps = [], 0, 0
    previous_bytes = 0
    root.mkdir(parents=True, exist_ok=False)
    with closing(Memory(root, embedder=model)) as archive:
        for day_index in range(DECADE_DAYS):
            moment = start + timedelta(days=day_index, hours=12)
            now = moment.timestamp()
            archive.now = lambda value=now: value
            for _ in range(base + int(day_index < extra)):
                text = rng.choice(EPISODE_TEMPLATES).format(
                    item=rng.choice(ITEMS), place=rng.choice(PLACES), detail=rng.choice(DESCRIPTIONS),
                    context=rng.choice(CONTEXTS),
                )
                archive.log("heard", text, "Synthetic Owner")
            if day_index in fact_days:
                fact = fact_days[day_index]
                fact_ids[fact] = archive.log("heard", fact, "Synthetic Owner")
            for name, text in anchors.get(day_index, ()):
                meta = {}
                if name == "temporal":
                    event = (moment - timedelta(days=1)).replace(hour=0)
                    meta = {"event_start": event.timestamp(),
                            "event_end": (event + timedelta(days=1)).timestamp()}
                ids[name] = archive.log("heard", text, "Synthetic Owner", **meta)
            if day_index == 3_201:
                if not archive.edit(episode_ids=(ids["forgotten"],)):
                    raise RuntimeError("synthetic forgotten fixture could not be deleted")
            chunks, steps = await _run_synthetic_night(archive, root, moment.date())
            night_chunks += chunks
            night_steps += steps
            current_bytes = _database_bytes(root)
            allocation_deltas.append(max(0, current_bytes - previous_bytes))
            previous_bytes = current_bytes
            if current_bytes > profile.max_database_bytes:
                raise RuntimeError(
                    f"synthetic archive exceeded profile bound: {current_bytes} > {profile.max_database_bytes}")
        episodes, = archive.db.execute("SELECT count(*) FROM episodes").fetchone()
        vectors, _ = archive.vector_counts()
        if episodes != profile.episodes or vectors != profile.episodes - 1:
            raise RuntimeError(f"profile expected {profile.episodes} episodes and one forgotten vector gap; "
                               f"got {episodes} episodes and {vectors} vectors")
    synthetic = "Synthetic Owner"
    probes = [Probe("single", "where is the brass telescope key", ids["single"], speaker=synthetic)]
    probes.extend(Probe("acceptance", query, fact_ids[fact], speaker=synthetic)
                  for fact, query in zip(FACTS, MIXED))
    probes.extend(Probe("paraphrase", query, fact_ids[fact], speaker=synthetic)
                  for fact, query in zip(FACTS, HARD))
    probes.extend((
        Probe("multi-session", "where does the Riverside pottery class meet", ids["multi-new"], speaker=synthetic),
        Probe("updated", "when is the Cedar repair appointment", ids["update-new"], speaker=synthetic),
        Probe("before-update", "when is the Cedar repair appointment", ids["update-old"],
              end=(start + timedelta(days=2_900)).timestamp(), speaker=synthetic),
        Probe("temporal", "field notebook Harbour Library", ids["temporal"],
              start=(start + timedelta(days=1_499)).replace(hour=0).timestamp(),
              end=(start + timedelta(days=1_500)).replace(hour=0).timestamp(), time_basis="event",
              speaker=synthetic),
        Probe("forgotten", "what was the synthetic observatory passphrase", None, speaker=synthetic),
    ))
    probes.extend(Probe("abstain", query, None, speaker=synthetic)
                  for query in (*UNRELATED, *UNRELATED_PERSONAL))
    return probes, {
        "episodes": profile.episodes,
        "simulated_days": DECADE_DAYS,
        "night_runs": DECADE_DAYS,
        "night_chunks": night_chunks,
        "night_step_calls": night_steps,
        "night_chunk_budget_seconds": CHUNK_BUDGET,
        "night_chunk_limit_per_day": MAX_NIGHT_CHUNKS_PER_DAY,
        "night_job_scope": ["checkpoint", "search", "vectors", "journal", "diary"],
        "night_job_scope_note": "production implementations/order; retention expiry and non-archive organ jobs are intentionally not registered",
        "database_bytes": previous_bytes,
        "database_bound_bytes": profile.max_database_bytes,
        "allocation_delta_bytes_per_day": {
            "p50": round(median(allocation_deltas)),
            "p99": sorted(allocation_deltas)[math.ceil(0.99 * len(allocation_deltas)) - 1],
            "total": sum(allocation_deltas),
            "note": "file allocation growth after each checkpoint, not physical device writes",
        },
    }


def timings(samples):
    """Nearest-rank p95/p99 and median over all measured queries."""
    ordered = sorted(samples)
    return {"samples": len(samples), "p50_ms": round(median(ordered), 3),
            "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
            "p99_ms": round(ordered[math.ceil(0.99 * len(ordered)) - 1], 3)}


def archive_arm(root, model, probes, *, meaning, repeats):
    def search(memory, probe):
        start = time.perf_counter_ns()
        found = memory.recall_history(
            probe.query, limit=3, start=probe.start, end=probe.end, kind="heard",
            speaker=probe.speaker, meaning=meaning, time_basis=probe.time_basis,
        )
        return found, (time.perf_counter_ns() - start) / 1_000_000

    fresh, warm, misses, groups, rankings = [], [], [], {}, []
    for probe in probes:
        with closing(Memory(root, embedder=model)) as memory:
            found, duration = search(memory, probe)
        fresh.append(duration)
        group = groups.setdefault(
            probe.category, {"queries": 0, "hits_at_1": 0, "hits_at_3": 0, "false_recalls": 0}
        )
        group["queries"] += 1
        ids = [episode.id for episode in found]
        rankings.append(ids)
        if probe.expected is None:
            missed = bool(found)
            group["false_recalls"] += missed
        else:
            missed = ids[:1] != [probe.expected]
            group["hits_at_1"] += not missed
            group["hits_at_3"] += probe.expected in ids
        if missed:
            misses.append({"category": probe.category, "query": probe.query,
                           "expected_episode": probe.expected, "returned_episodes": ids,
                           "top_text": found[0].text if found else None})
    with closing(Memory(root, embedder=model)) as memory:
        for probe in probes:
            search(memory, probe)
        for _ in range(repeats):
            for probe in probes:
                _, duration = search(memory, probe)
                warm.append(duration)
    positive_categories = tuple(dict.fromkeys(probe.category for probe in probes if probe.expected is not None))
    negative_categories = tuple(dict.fromkeys(probe.category for probe in probes if probe.expected is None))
    positives = sum(groups[key]["queries"] for key in positive_categories)
    negatives = sum(groups[key]["queries"] for key in negative_categories)
    hit1 = sum(groups[key]["hits_at_1"] for key in positive_categories)
    hit3 = sum(groups[key]["hits_at_3"] for key in positive_categories)
    false = sum(groups[key]["false_recalls"] for key in negative_categories)
    return {
        "positive": {key: {column: groups[key][column] for column in ("queries", "hits_at_1", "hits_at_3")}
                     for key in positive_categories},
        "negative": {key: {column: groups[key][column] for column in ("queries", "false_recalls")}
                     for key in negative_categories},
        "new_sqlite_connection_warm_page_cache": timings(fresh),
        "reused_sqlite_connection_warm_page_cache": timings(warm),
        "totals": {"positive_queries": positives, "hit_at_1": hit1, "hit_at_3": hit3,
                   "negative_queries": negatives, "false_recalls": false,
                   "abstentions": negatives - false,
                   "hit_at_1_rate": round(hit1 / positives, 4) if positives else None,
                   "hit_at_3_rate": round(hit3 / positives, 4) if positives else None,
                   "abstention_rate": round((negatives - false) / negatives, 4) if negatives else None},
        "_rankings": rankings,
        "misses": misses,
    }


def compared_arms(root, model, probes, repeats):
    words = archive_arm(root, None, probes, meaning=False, repeats=repeats)
    hybrid = archive_arm(root, model, probes, meaning=True, repeats=repeats)
    pairs = list(zip(words.pop("_rankings"), hybrid.pop("_rankings")))
    overlap = [len(set(left) & set(right)) / max(1, len(set(left) | set(right)))
               for left, right in pairs]
    return {"words_only": words, "shipped_hybrid": hybrid,
            "top3_shortlist_jaccard": {"queries": len(overlap),
                                       "mean": round(sum(overlap) / len(overlap), 4) if overlap else None}}


def measure_archive(model, sizes, repeats):
    result = {
        "scope": "host episode retrieval through Memory.recall_history; no reply generation or real data",
        "seed": ARCHIVE_SEED,
        "span": [ARCHIVE_START.date().isoformat(), ARCHIVE_END.date().isoformat()],
        "sqlite": sqlite3.sqlite_version,
        "model": model.model_id,
        "warm_repeats": repeats,
        "method": "the 1,000-episode corpus is a subset of the 10,000-episode corpus; same database and "
                  "probes for both arms; a new SQLite connection is used per first query but the OS page cache "
                  "remains warm; reused-connection queries follow a priming pass",
        "caveats": "templated distractors; the mixed and paraphrase groups overlap; database file sizes "
                   "are not bytes written to disk; this run does not cover night jobs or interruption recovery",
        "archives": {},
    }
    with tempfile.TemporaryDirectory(prefix="pip-recall-archive-") as directory:
        for size in sizes:
            root = Path(directory) / str(size)
            probes, counts = seed_archive(root, size, model)
            result["archives"][size] = {
                **counts,
                **compared_arms(root, model, probes, repeats),
            }
    return result


async def measure_decade(model, name, profile, repeats):
    source = Path(__file__)
    provenance = hashlib.sha256(source.read_bytes()).hexdigest()
    result = {
        "format": "being-synthetic-decade-recall-v1",
        "scope": "private synthetic host archive; real Memory.log and registered nightly archive jobs; no owner data",
        "profile": name,
        "seed": ARCHIVE_SEED,
        "requested_episodes": profile.episodes,
        "simulated_days": DECADE_DAYS,
        "span_start": ARCHIVE_START.date().isoformat(),
        "span_end_exclusive": (ARCHIVE_START + timedelta(days=DECADE_DAYS)).date().isoformat(),
        "bounds": {"database_bytes": profile.max_database_bytes,
                   "night_chunks_per_day": MAX_NIGHT_CHUNKS_PER_DAY,
                   "night_chunk_budget_seconds": CHUNK_BUDGET},
        "floors": {name: getattr(memory_module, name) for name in KNOBS},
        "provenance": {"harness": str(source), "harness_sha256": provenance,
                       "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                       "embedding_model": model.model_id},
        "method": "fixed synthetic persona templates and labeled fixtures; one archive; lexical and hybrid arms "
                  "share rows and probes; the first query uses a new SQLite connection with a warm OS page cache; "
                  "reused-connection timings follow priming; every registered job run_chunk call is counted",
        "caveats": "synthetic language is easier than owner speech; allocation deltas are not device write counters; "
                   "cold-cache and native laptop timing, durability fault injection, retention expiry and raw-block "
                   "forgetting require separate harnesses",
    }
    with tempfile.TemporaryDirectory(prefix="pip-private-synthetic-decade-") as directory:
        root = Path(directory) / name
        probes, counts = await seed_decade_archive(root, profile, model)
        result.update(counts)
        result["recall"] = compared_arms(root, model, probes, repeats)
        words = result["recall"]["words_only"]
        hybrid = result["recall"]["shipped_hybrid"]
        acceptance = hybrid["positive"]["acceptance"]
        bespoke = ("single", "multi-session", "updated", "before-update", "temporal")
        bespoke_hit1 = {category: hybrid["positive"][category]["hits_at_1"] for category in bespoke}
        forgotten_false = hybrid["negative"]["forgotten"]["false_recalls"]
        abstain_false = hybrid["negative"]["abstain"]["false_recalls"]
        retrieval_passes = (
            acceptance["hits_at_1"] >= 24
            and all(bespoke_hit1[category] == hybrid["positive"][category]["queries"]
                    for category in bespoke)
            and forgotten_false == 0
            and abstain_false <= 1
            and hybrid["totals"]["false_recalls"] <= words["totals"]["false_recalls"]
        )
        result["acceptance"] = {
            "hit_at_1_floor": 24,
            "hit_at_1": acceptance["hits_at_1"],
            "hit_at_3": acceptance["hits_at_3"],
            "bespoke_hit_at_1": bespoke_hit1,
            "bespoke_required_at_1": list(bespoke),
            "forgotten_false_recalls": forgotten_false,
            "forgotten_false_recall_limit": 0,
            "abstain_false_recalls": abstain_false,
            "abstain_false_recall_limit": 1,
            "hybrid_false_recalls": hybrid["totals"]["false_recalls"],
            "words_false_recalls": words["totals"]["false_recalls"],
            "retrieval_safety_gate_passes": retrieval_passes,
            "native_performance_gate_passes": None,
            "overall_promotion_passes": None,
            "performance_note": "cold/warm native laptop p50 and p99 and physical bytes written were not measured",
            "decision": "do not authorize a larger profile unless this result changes a retrieval decision",
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sweep", action="store_true")
    mode.add_argument("--archive", type=int, nargs="*", choices=ARCHIVE_SIZES,
                      help="episode counts (1,000 and 10,000 by default)")
    mode.add_argument("--decade", choices=tuple(DECADE_PROFILES),
                      help="3,650-day synthetic archive profile")
    parser.add_argument("--allow-large", action="store_true",
                        help="required for decade profiles above the 10,000-row decision gate")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--model", type=Path, default=MODEL, help="path to the blink-8M pack")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3,
                        help="warm passes per archive (default: 3)")
    args = parser.parse_args()
    model = embed.load(args.model)
    if args.decade is not None:
        if model is None:
            parser.error(f"blink-8M unavailable in {args.model}")
        profile = DECADE_PROFILES[args.decade]
        if profile.large and not args.allow_large:
            parser.error("larger decade profiles require --allow-large after reviewing the 10,000-row decision run")
        print(json.dumps(asyncio.run(measure_decade(model, args.decade, profile, args.repeats)), indent=2))
        return
    if args.archive is not None:
        if model is None:
            parser.error(f"blink-8M unavailable in {args.model}")
        sizes = tuple(dict.fromkeys(args.archive)) or ARCHIVE_SIZES
        print(json.dumps(measure_archive(model, sizes, args.repeats), indent=2))
        return
    words = words_only()
    shipped = {name: getattr(memory_module, name) for name in KNOBS}
    result = {"words_only": words, "shipped": dict(shipped, **measure(model))}
    if args.sweep:
        grid = {
            "COSINE_FLOOR": (0.18, 0.20, 0.22, 0.24, 0.26, 0.28),
            "COSINE_FLOOR_IMPERSONAL": (0.28, 0.30, 0.32, 0.34, 0.36),
            "COSINE_FLOOR_PARTIAL": (0.35, 0.40, 0.45, 0.50),
            "COSINE_FLOOR_PARTIAL_IMPERSONAL": (0.45, 0.50, 0.55),
            "COSINE_WEIGHT": (0.05, 0.10, 0.20),
        }
        rows = []
        for values in itertools.product(*grid.values()):
            setting = dict(zip(grid, values))
            if setting["COSINE_FLOOR_IMPERSONAL"] < setting["COSINE_FLOOR"]:
                continue
            with floors(**setting):
                row = measure(model)
            rows.append(dict(setting, **row, qualifies=qualifies(row, words)))
        good = [row for row in rows if row["qualifies"]]
        # Tuned on FACTS: the acceptance set, then the no-shared-word set,
        # then fewer personal false recalls; HELD_OUT is only reported.
        good.sort(key=lambda r: (-r["mixed_hit1"], -r["hard_hit1"],
                                 r["personal_false_facts"] + r["unrelated_false_facts"]))
        result["sweep"] = {"settings": len(rows), "qualifying": len(good), "best": good[:args.top]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
