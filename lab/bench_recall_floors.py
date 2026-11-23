"""Measure hybrid recall's cosine floors on the paraphrase sets (task L8.3).

Uses the fixed sets in tests/test_memory_hybrid.py and the real blink-8M
vectors in out/models/embeddings/blink-8M. Reports hit@1 for the acceptance
set (MIXED), the no-shared-word sets (HARD, HELD_OUT), and false recalls on
the unrelated and unrelated-personal questions, for the hybrid search and for
words alone.

    python3 -m lab.bench_recall_floors            the shipped floors
    python3 -m lab.bench_recall_floors --sweep    a grid around them

The sweep tunes on FACTS (MIXED, HARD and the unrelated sets against FACTS)
and reports HELD_OUT separately, so a floor is not chosen to fit the set it
is judged on. A setting qualifies only if, on both fact sets, its false
recalls do not exceed words alone and the unrelated set gives at most 1.
"""

import argparse
import itertools
import json
from contextlib import contextmanager

from mind import embed, memory as memory_module
from tests.test_memory_hybrid import (
    FACTS, HARD, HELD_OUT, MIXED, MODEL, UNRELATED, UNRELATED_PERSONAL, false_recalls, scored,
)

KNOBS = ("COSINE_FLOOR", "COSINE_FLOOR_IMPERSONAL", "COSINE_FLOOR_PARTIAL",
         "COSINE_FLOOR_PARTIAL_IMPERSONAL", "COSINE_WEIGHT")
HELD_FACTS = [fact for fact, _ in HELD_OUT]
HELD_QUERIES = [query for _, query in HELD_OUT]


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()
    model = embed.load(MODEL)
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
