"""Replay held-out explicit recall across a normal overnight pass, with no model."""

import argparse
import asyncio
import json
import math
import shutil
import tempfile
import time
from collections import defaultdict
from datetime import datetime, time as wall_time, timedelta
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from unittest.mock import patch

from mind import turn
from mind.dream_store import MAX_NIGHT_BATCHES, journal
from mind.memory import Memory

from . import dream_recall_holdout as fixtures

FRIDAY, SATURDAY, OWNER = fixtures.FRIDAY, fixtures.SATURDAY, fixtures.OWNER


class FixtureBus:
    def __init__(self):
        self.events = []
        self.first_clause_ns = None

    async def send(self, topic, **fields):
        if topic == "say.request" and fields.get("text") and self.first_clause_ns is None:
            self.first_clause_ns = time.perf_counter_ns()
        self.events.append({"t": topic, **fields})


async def deliver(mind, topic, **fields):
    event = {"t": topic, **fields}
    mind.bus.urgent(event)
    await mind._on_event(event)


def fixture_mind(memory, root):
    return turn.Mind(FixtureBus(), SimpleNamespace(), memory, turn.Config(owner=OWNER), root)


async def sleep_pass(memory, root):
    mind = fixture_mind(memory, root)
    mind.skills.now = lambda: datetime.combine(SATURDAY, wall_time(2, 30))
    await deliver(mind, "body.lid", closed=True, seq=1)
    await deliver(mind, "state.changed", state="asleep")
    await deliver(mind, "body.state", hour=2, minute=30, lid_closed=True, on_ac=True, temp=48)
    with patch.object(turn, "DREAM_INTERVAL", 0):
        for _ in range(MAX_NIGHT_BATCHES):
            last = mind._dream_task
            await deliver(mind, "stem.beat", state="asleep")
            if mind._dream_task is last:
                break
            await mind._dream_task
    mind.skills.close()


def keys_from_journal(memory, episodes):
    by_id = {value: key for key, value in episodes.items()}
    return [by_id[entry.source.id] for entry in journal(memory.db, OWNER, limit=100)]


async def prepare(case, root, overnight):
    memory = Memory(root)
    episodes, facts = {}, {}
    def seed(*, after_sleep):
        for index, row in enumerate(case.rows):
            if (row.day > 0) != after_sleep:
                continue
            episode = memory.log(row.kind, row.text, row.speaker)
            episodes[row.key] = episode
            day = FRIDAY + timedelta(days=row.day)
            minutes = index if row.order is None else row.order
            timestamp = datetime.combine(day, wall_time(9)) + timedelta(minutes=minutes)
            with memory.db:
                memory.db.execute("UPDATE episodes SET ts = ? WHERE id = ?", (timestamp.timestamp(), episode))
            if row.fact is not None:
                facts[row.key] = memory.remember(row.fact, row.speaker, source=episode)

    try:
        seed(after_sleep=False)
        if overnight:
            (root / "dreams-enabled").touch()
            await sleep_pass(memory, root)
        pre_edit = keys_from_journal(memory, episodes)
        for key, text in case.edit:
            if not memory.edit(episode_ids=(episodes[key],), replacement=text):
                raise AssertionError(f"{case.key}: fixture edit did not match {key}")
            if key in keys_from_journal(memory, episodes):
                raise AssertionError(f"{case.key}: journal link survived edit {key}")
        if overnight and case.edit:
            await sleep_pass(memory, root)
        seed(after_sleep=True)
        retained = keys_from_journal(memory, episodes)
        if any(text is None and key in retained for key, text in case.edit):
            raise AssertionError(f"{case.key}: an overnight replay restored a forgotten journal link")
        with memory.db:
            memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        memory.close()
    return episodes, facts, pre_edit, retained


async def recall(case, archive, root, episodes, facts, *, audit=False, opted_in=False):
    root.mkdir()
    shutil.copyfile(archive / "mind.db", root / "mind.db")
    if opted_in or (archive / "dreams-enabled").is_file():
        (root / "dreams-enabled").touch()
    memory = Memory(root)
    mind = fixture_mind(memory, root)
    mind.skills.now = lambda: datetime.combine(SATURDAY, wall_time(12))
    statements = []
    try:
        await deliver(mind, "body.lid", closed=False, seq=2)
        await deliver(mind, "state.changed", state="attending")
        if audit:
            memory.db.set_trace_callback(statements.append)
        start = time.perf_counter_ns()
        await deliver(mind, "hear.final", text=case.request, speaker=case.speaker)
        if mind._turn is None:
            raise AssertionError(f"{case.key}: no foreground turn")
        await mind._turn
        if mind.bus.first_clause_ns is None:
            raise AssertionError(f"{case.key}: no speakable clause")
        reply = " ".join(e["text"] for e in mind.bus.events if e["t"] == "say.request" and e["text"])
        saved = memory.db.execute("SELECT text, meta FROM episodes WHERE kind = 'said' ORDER BY id DESC LIMIT 1").fetchone()
        if saved is None or saved[0] != reply:
            raise AssertionError(f"{case.key}: spoken reply and archive disagree")
        meta = json.loads(saved[1] or "{}")
        by_episode = {value: key for key, value in episodes.items()}
        by_fact = {value: key for key, value in facts.items()}
        sources = [by_episode.get(value, f"episode:{value}") for value in meta.get("episodes", [])]
        sources += [by_fact.get(value, f"fact:{value}") for value in meta.get("recalled", [])]
        forgotten_sources = {key for key, text in case.edit if text is None}
        if forgotten_sources.intersection(sources):
            raise AssertionError(f"{case.key}: a spoken reply cited a forgotten source")
        if case.expected_source is None:
            source_correct = correct = not sources and "I don't remember anything about that." in reply
        else:
            source_correct = correct = sources == [case.expected_source]
            if source_correct:
                if meta.get("recalled"):
                    excerpt = memory.fact(facts[case.expected_source]).text
                else:
                    text, = memory.db.execute("SELECT text FROM episodes WHERE id = ?", (episodes[case.expected_source],)).fetchone()
                    excerpt = text.rstrip(".!?")
                correct = " ".join(excerpt.split()) in reply
        result = {
            "sources": sources, "reply": reply, "source_correct": source_correct,
            "correct": correct,
            "first_clause_ms": (mind.bus.first_clause_ns - start) / 1e6,
        }
        if audit:
            result["coverage_checks"] = sum(s.startswith("SELECT 1 FROM episodes_fts WHERE rowid") for s in statements)
            result["journal_lookups"] = sum(s.startswith("SELECT") and "FROM dream_journal" in s for s in statements)
        return result
    finally:
        mind.skills.close()
        memory.close()


async def compare(case, repeats):
    with tempfile.TemporaryDirectory(prefix="being-dream-recall-") as folder:
        root = Path(folder)
        variants = ("before", "after")
        prepared = {name: await prepare(case, root / name, name == "after") for name in variants}
        results = defaultdict(list)
        for repeat in range(repeats):
            order = variants if repeat % 2 == 0 else variants[::-1]
            for name in order:
                episodes, facts, *_ = prepared[name]
                results[name].append(await recall(case, root / name, root / f"{name}-{repeat}", episodes, facts))
        row = {"key": case.key, "group": case.group, "request": case.request,
               "speaker": case.speaker, "expected_source": case.expected_source,
               "journal_before_edit": prepared["after"][2], "journal_at_recall": prepared["after"][3]}
        for name in variants:
            first, *rest = results[name]
            stable = {k: v for k, v in first.items() if k != "first_clause_ms"}
            if any({k: v for k, v in other.items() if k != "first_clause_ms"} != stable for other in rest):
                raise AssertionError(f"{case.key}: unstable {name} recall")
            row[name] = dict(stable, first_clause_ms=[item["first_clause_ms"] for item in results[name]])
        episodes, facts, *_ = prepared["after"]
        audit = await recall(case, root / "after", root / "after-audit", episodes, facts, audit=True)
        if audit["reply"] != row["after"]["reply"] or audit["sources"] != row["after"]["sources"]:
            raise AssertionError(f"{case.key}: trace replay selected a different answer")
        row["coverage_checks"] = audit["coverage_checks"]
        row["journal_lookups"] = audit["journal_lookups"]
        episodes, facts, *_ = prepared["before"]
        armed = await recall(case, root / "before", root / "before-opted-in", episodes, facts, opted_in=True)
        if armed["reply"] != row["before"]["reply"] or armed["sources"] != row["before"]["sources"]:
            raise AssertionError(f"{case.key}: opting in changed recall before overnight work ran")
        return row


def summarize(rows):
    result = {"cases": len(rows)}
    for name in ("before", "after"):
        timings = sorted(value for row in rows for value in row[name]["first_clause_ms"])
        result[name] = {
            "correct": sum(row[name]["correct"] for row in rows),
            "source_correct": sum(row[name]["source_correct"] for row in rows),
            "first_clause_p50_ms": round(median(timings), 3),
            "first_clause_p95_ms": round(timings[math.ceil(.95 * len(timings)) - 1], 3),
        }
    result["gains"] = [row["key"] for row in rows if not row["before"]["correct"] and row["after"]["correct"]]
    result["regressions"] = [row["key"] for row in rows if row["before"]["correct"] and not row["after"]["correct"]]
    result["source_regressions"] = [row["key"] for row in rows
                                    if row["before"]["source_correct"] and not row["after"]["source_correct"]]
    result["changed_sources"] = [row["key"] for row in rows if row["before"]["sources"] != row["after"]["sources"]]
    return result


async def run(args):
    cohorts = {"compact": fixtures.CASES, "archive": fixtures.ARCHIVE_CASES}
    rows = [dict(await compare(case, args.repeats), cohort=cohort)
            for cohort, cases in cohorts.items() for case in cases]
    groups = {name: summarize([row for row in rows if row["group"] == name]) for name in sorted({r["group"] for r in rows})}
    by_cohort = {name: summarize([row for row in rows if row["cohort"] == name]) for name in cohorts}
    strict_rows = [row for row in rows if row["journal_lookups"]]
    report = {
        "scope": "synthetic archives, real dream worker and hear.final to first say.request on the host",
        "armed_before_sleep_control": "an extra untimed reply per case verifies opt-in alone leaves reply and source identical",
        "friday": FRIDAY.isoformat(), "saturday": SATURDAY.isoformat(), "repeats": args.repeats,
        "overall": summarize(rows), "cohorts": by_cohort, "groups": groups,
        "journal_lookup_cases": summarize(strict_rows) if strict_rows else None, "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("overall", "cohorts", "journal_lookup_cases")}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, choices=range(1, 21), default=7)
    parser.add_argument("--output", type=Path, default=Path("out/logs/dream-recall-holdout.json"))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
