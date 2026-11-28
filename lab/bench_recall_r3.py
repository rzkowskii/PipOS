"""Evaluate the WP-D11 R3 vector recipe without changing production indexes.

The candidate builds ephemeral vectors from the real archive relationships:
an episode's content words plus claims which passed ``dream_verify`` and were
promoted by ``dream_store``; a digest title plus bounded key terms from all of
its live children.  It compares these vectors with today's content-word
recipe on the fixed synthetic recall sets.  Nothing is written outside a
private temporary archive.
"""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import tempfile

from mind import dream_store, dream_verify, embed, memory as memory_module
from mind.dream_verify import Line, check
from mind.embed import dot
from mind.memory import COSINE_FLOOR, COSINE_FLOOR_IMPERSONAL, Memory, search_terms, vector_text
from tests import test_memory_hybrid as fixtures
from tests.test_memory_hybrid import FACTS, HARD, HELD_OUT, MIXED, MODEL, UNRELATED, UNRELATED_PERSONAL


OWNER = "Synthetic Owner"
MAX_TERMS = 64
MAX_CHILDREN = 8
MAX_RESULTS = 3
RECIPE = "verified-claims-content-words-digest-children-lab-1"
ACCEPTANCE_FLOOR = 24
PUBLIC_FALSE_LIMIT = 1
DIGEST_POSITIVE_RATE_FLOOR = 0.5
DIGEST_TITLE_RATE_FLOOR = 1.0
DIGEST_FALSE_LIMIT = 1


@dataclass(frozen=True)
class Digest:
    key: str
    title: str
    children: tuple[int, ...]


def _checked(memory, episode, claim, quote):
    text, = memory.db.execute("SELECT text FROM episodes WHERE id = ?", (episode,)).fetchone()
    verdict = check(
        {"kind": "fact", "claim": claim, "quote": quote, "episode_id": f"E{episode}",
         "speaker": "owner", "modality": "stated", "time_ref": ""},
        {episode: Line(episode, "owner", text)},
    )
    if not verdict.ok:
        raise RuntimeError(f"synthetic verified claim was refused: {verdict.reason}")
    return verdict.item


def _seed(memory):
    """Create promoted claims through the real verifier/store and source graph."""
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    records = {}
    all_children = []
    for index, fact in enumerate(FACTS):
        first_time = start + timedelta(minutes=index)
        second_time = first_time + timedelta(days=2)
        memory.now = lambda value=first_time.timestamp(): value
        first = memory.log("heard", f"Before I forget, {fact}.", OWNER)
        memory.now = lambda value=second_time.timestamp(): value
        second = memory.log("heard", f"I should repeat that {fact}.", OWNER)
        one = _checked(memory, first, fact, fact)
        two = _checked(memory, second, fact, fact)
        dream_store.save_candidates(memory, OWNER, (one,), night="2026-01-01", zone=timezone.utc)
        promoted = dream_store.save_candidates(memory, OWNER, (two,), night="2026-01-03", zone=timezone.utc)
        if len(promoted) != 1:
            raise RuntimeError("synthetic verified claim did not promote exactly once")
        candidate = promoted[0]
        fact_id, = memory.db.execute(
            "SELECT fact_id FROM candidate_facts WHERE id = ? AND status = 'promoted'", (candidate,)).fetchone()
        if fact_id is None:
            raise RuntimeError("synthetic promoted claim has no linked fact")
        records[fact] = {"sources": (first, second), "candidate": candidate, "fact": fact_id}
        all_children.extend((first, second))

    held_start = start + timedelta(days=10)
    for index, (fact, _query) in enumerate(HELD_OUT):
        first_time = held_start + timedelta(minutes=index)
        second_time = first_time + timedelta(days=2)
        memory.now = lambda value=first_time.timestamp(): value
        first = memory.log("heard", f"For this held-out example, {fact}.", OWNER)
        memory.now = lambda value=second_time.timestamp(): value
        second = memory.log("heard", f"A later independent telling says {fact}.", OWNER)
        one = _checked(memory, first, fact, fact)
        two = _checked(memory, second, fact, fact)
        dream_store.save_candidates(memory, OWNER, (one,), night="2026-01-11", zone=timezone.utc)
        promoted = dream_store.save_candidates(memory, OWNER, (two,), night="2026-01-13", zone=timezone.utc)
        if len(promoted) != 1:
            raise RuntimeError("synthetic held-out claim did not promote exactly once")
        candidate = promoted[0]
        fact_id, = memory.db.execute(
            "SELECT fact_id FROM candidate_facts WHERE id = ? AND status = 'promoted'", (candidate,)).fetchone()
        records[fact] = {"sources": (first, second), "candidate": candidate, "fact": fact_id}
        all_children.extend((first, second))
    digests = tuple(
        Digest(f"digest-{offset // MAX_CHILDREN:02d}",
               f"Synthetic digest period {offset // MAX_CHILDREN:02d}",
               tuple(all_children[offset:offset + MAX_CHILDREN]))
        for offset in range(0, len(all_children), MAX_CHILDREN)
    )
    episode_facts = {episode: fact for fact, record in records.items() for episode in record["sources"]}
    return records, episode_facts, digests


def _live_episodes(memory):
    return {row[0]: row[1] for row in memory.db.execute(
        "SELECT id, text FROM episodes WHERE kind = 'heard' AND speaker = ? "
        "AND text NOT IN ('', '[forgotten]') ORDER BY id", (OWNER,))}


def _verified_claims(memory):
    """Claims are admitted only through live promoted candidate-source rows."""
    claims = {}
    for episode, claim in memory.db.execute(
            "SELECT s.episode_id, c.claim FROM candidate_sources s "
            "JOIN candidate_facts c ON c.id = s.candidate_id "
            "JOIN episodes e ON e.id = s.episode_id "
            "WHERE c.status = 'promoted' AND c.owner = ? AND e.kind = 'heard' "
            "AND e.speaker = ? AND e.text NOT IN ('', '[forgotten]') ORDER BY s.episode_id, c.id",
            (OWNER, OWNER)):
        claims.setdefault(episode, []).append(claim)
    return claims


def materialize(memory, digests):
    """Return ephemeral current/R3 keys and source sets; never persist text or vectors."""
    episodes = _live_episodes(memory)
    claims = _verified_claims(memory)
    current, candidate = {}, {}
    for episode, text in episodes.items():
        current[("episode", episode)] = (vector_text(text), (episode,))
        verified = " ".join(vector_text(claim) for claim in claims.get(episode, ()))
        candidate[("episode", episode)] = (
            " ".join(part for part in (verified, vector_text(text)) if part), (episode,))

    for digest in digests:
        if not 1 <= len(digest.children) <= MAX_CHILDREN or len(set(digest.children)) != len(digest.children):
            raise ValueError("digest children must be a bounded unique source list")
        # A missing/forgotten child invalidates the whole digest and therefore
        # any ancestor built from it, matching the existing derived-data rule.
        if any(child not in episodes for child in digest.children):
            continue
        current[("digest", digest.key)] = (vector_text(digest.title), digest.children)
        terms = []
        for child in digest.children:
            for term in search_terms(candidate[("episode", child)][0]):
                if term not in terms:
                    terms.append(term)
                if len(terms) == MAX_TERMS:
                    break
            if len(terms) == MAX_TERMS:
                break
        candidate[("digest", digest.key)] = (
            " ".join((vector_text(digest.title), *terms)), digest.children)
    return current, candidate


def _vectors(model, rows):
    return {key: (model.embed(text), sources) for key, (text, sources) in rows.items()}


def _rank(model, rows, query, *, personal):
    query_vector = model.embed(vector_text(query))
    floor = COSINE_FLOOR if personal else COSINE_FLOOR_IMPERSONAL
    scored = [(dot(query_vector, vector), key, sources)
              for key, (vector, sources) in rows.items() if any(vector)]
    scored.sort(key=lambda row: (-row[0], str(row[1])))
    return [(key, sources, round(score, 6)) for score, key, sources in scored[:MAX_RESULTS] if score >= floor]


def _measure(model, rows, records, episode_facts, digests):
    groups = {}
    cases = []

    def probe(label, query, expected, group, personal=True, kind="episode"):
        eligible = {key: value for key, value in rows.items() if key[0] == kind}
        found = _rank(model, eligible, query, personal=personal)
        ids = [key[1] for key, _, _ in found]
        source_hit = lambda result: bool(set(result[1]).intersection(expected or ()))
        hit1 = bool(expected) and bool(found) and source_hit(found[0])
        hit3 = bool(expected) and any(source_hit(result) for result in found)
        false = expected is None and bool(found)
        row = groups.setdefault(group, {"queries": 0, "hit_at_1": 0, "hit_at_3": 0,
                                        "false_recalls": 0, "abstentions": 0})
        row["queries"] += 1
        row["hit_at_1"] += hit1
        row["hit_at_3"] += hit3
        row["false_recalls"] += false
        row["abstentions"] += not found
        cases.append({"case": label, "returned": ids, "hit_at_1": hit1,
                      "hit_at_3": hit3, "abstained": not found})

    for index, (fact, query) in enumerate(zip(FACTS, MIXED)):
        probe(f"mixed-{index:02d}", query, records[fact]["sources"], "acceptance")
    for index, (fact, query) in enumerate(zip(FACTS, HARD)):
        probe(f"hard-{index:02d}", query, records[fact]["sources"], "hard")
    for index, (fact, query) in enumerate(HELD_OUT):
        probe(f"held-{index:02d}", query, records[fact]["sources"], "held_out")
    for index, query in enumerate(UNRELATED):
        probe(f"unrelated-{index:02d}", query, None, "abstain_public", personal=False)
    for index, query in enumerate(UNRELATED_PERSONAL):
        probe(f"personal-{index:02d}", query, None, "abstain_personal")
    for index, digest in enumerate(digests):
        child = digest.children[0]
        fact = episode_facts[child]
        if fact in FACTS:
            query = HARD[FACTS.index(fact)]
        else:
            query = HELD_OUT[[item[0] for item in HELD_OUT].index(fact)][1]
        probe(f"digest-positive-{index:02d}", query, records[fact]["sources"], "digest_positive",
              kind="digest")
        probe(f"digest-title-{index:02d}", digest.title, digest.children, "digest_title_control",
              personal=False, kind="digest")
    for index, query in enumerate(UNRELATED[:min(10, len(digests))]):
        probe(f"digest-negative-{index:02d}", query, None, "digest_negative", personal=False, kind="digest")
    return {"groups": groups, "cases": cases}


def _fingerprint(path, limit=64 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError(f"provenance input {path} is not a bounded regular file")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _provenance(model_root, model):
    model_files = {}
    for name in ("config.json", "tokenizer.json", "model.safetensors"):
        path = Path(model_root) / name
        if path.exists():
            model_files[name] = _fingerprint(path, 64 * 1024 * 1024)
    if "tokenizer.json" not in model_files or "model.safetensors" not in model_files:
        raise ValueError("the embedding model needs attested tokenizer and weight files")
    sources = {
        "harness": _fingerprint(Path(__file__), 1024 * 1024),
        "embed": _fingerprint(embed.__file__, 1024 * 1024),
        "memory": _fingerprint(memory_module.__file__, 4 * 1024 * 1024),
        "dream_store": _fingerprint(dream_store.__file__, 1024 * 1024),
        "dream_verify": _fingerprint(dream_verify.__file__, 1024 * 1024),
        "fixtures": _fingerprint(fixtures.__file__, 1024 * 1024),
    }
    recipe = {
        "id": RECIPE, "base_recipe": memory_module.VECTOR_RECIPE,
        "episode": "verified promoted claims followed by episode content words",
        "digest": "title followed by first-seen deduplicated child key terms",
        "max_children": MAX_CHILDREN, "max_terms": MAX_TERMS,
        "personal_floor": COSINE_FLOOR, "impersonal_floor": COSINE_FLOOR_IMPERSONAL,
    }
    return {"embedding_model": model.model_id, "model_files": model_files, "sources": sources,
            "recipe": recipe,
            "recipe_sha256": hashlib.sha256(json.dumps(
                recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "timezone": "UTC"}


def run(model, model_root):
    with tempfile.TemporaryDirectory(prefix="pip-private-r3-") as directory:
        with closing(Memory(Path(directory), embedder=model)) as memory:
            records, episode_facts, digests = _seed(memory)
            while memory.backfill_vectors(128):
                pass
            current, candidate = materialize(memory, digests)
            before = {
                "current": _measure(model, _vectors(model, current), records, episode_facts, digests),
                "r3_candidate": _measure(
                    model, _vectors(model, candidate), records, episode_facts, digests),
            }
            forgotten = digests[0].children[0]
            forgotten_fact = episode_facts[forgotten]
            record = records[forgotten_fact]
            linked_sources = record["sources"]
            indexed_before = {
                "episode_vectors": memory.db.execute(
                    "SELECT count(*) FROM episode_vec WHERE episode_id IN (?, ?)", linked_sources).fetchone()[0],
                "fact_vectors": memory.db.execute(
                    "SELECT count(*) FROM fact_vec WHERE fact_id = ?", (record["fact"],)).fetchone()[0],
            }
            if not memory.edit(episode_ids=(forgotten,)):
                raise RuntimeError("synthetic source removal did not edit the archive")
            after_current, after_candidate = materialize(memory, digests)
            current_rank = _rank(model, _vectors(model, after_current), HARD[FACTS.index(forgotten_fact)], personal=True)
            candidate_rank = _rank(
                model, _vectors(model, after_candidate), HARD[FACTS.index(forgotten_fact)], personal=True)
            returned_sources = {
                "current": sorted({source for _key, sources, _score in current_rank for source in sources}),
                "r3_candidate": sorted({source for _key, sources, _score in candidate_rank for source in sources}),
            }
            removed = {
                "indexed_before": indexed_before,
                "both_linked_episodes_absent": all(
                    ("episode", source) not in after_current and ("episode", source) not in after_candidate
                    for source in linked_sources),
                "fact_absent": memory.fact(record["fact"]) is None,
                "candidate_absent": memory.db.execute(
                    "SELECT 1 FROM candidate_facts WHERE id = ?", (record["candidate"],)).fetchone() is None,
                "digest_absent_current": ("digest", digests[0].key) not in after_current,
                "digest_absent_candidate": ("digest", digests[0].key) not in after_candidate,
                "candidate_source_rows": memory.db.execute(
                    "SELECT count(*) FROM candidate_sources WHERE episode_id IN (?, ?)", linked_sources).fetchone()[0],
                "linked_episode_live_rows": memory.db.execute(
                    "SELECT count(*) FROM episodes WHERE id IN (?, ?) "
                    "AND text NOT IN ('', '[forgotten]')", linked_sources).fetchone()[0],
                "episode_vector_rows": memory.db.execute(
                    "SELECT count(*) FROM episode_vec WHERE episode_id IN (?, ?)", linked_sources).fetchone()[0],
                "fact_vector_rows": memory.db.execute(
                    "SELECT count(*) FROM fact_vec WHERE fact_id = ?", (record["fact"],)).fetchone()[0],
                "post_removal_ranking_sources": returned_sources,
                "removed_sources_returned": any(
                    source in set(linked_sources) for values in returned_sources.values() for source in values),
            }
    current_groups = before["current"]["groups"]
    candidate_groups = before["r3_candidate"]["groups"]
    paired_improvements = sum(
        current_case["hit_at_1"] is False and candidate_case["hit_at_1"] is True
        for current_case, candidate_case in zip(before["current"]["cases"], before["r3_candidate"]["cases"])
    )
    offline_gate = (
        candidate_groups["acceptance"]["hit_at_1"] >= ACCEPTANCE_FLOOR
        and candidate_groups["acceptance"]["hit_at_1"] >= current_groups["acceptance"]["hit_at_1"]
        and candidate_groups["hard"]["hit_at_1"] >= current_groups["hard"]["hit_at_1"]
        and candidate_groups["held_out"]["hit_at_1"] >= current_groups["held_out"]["hit_at_1"]
        and candidate_groups["abstain_public"]["false_recalls"] <= PUBLIC_FALSE_LIMIT
        and candidate_groups["abstain_public"]["false_recalls"]
            <= current_groups["abstain_public"]["false_recalls"]
        and candidate_groups["abstain_personal"]["false_recalls"]
            <= current_groups["abstain_personal"]["false_recalls"]
        and candidate_groups["abstain_public"]["abstentions"] > 0
        and candidate_groups["abstain_personal"]["abstentions"] > 0
        and candidate_groups["digest_positive"]["hit_at_1"]
            / candidate_groups["digest_positive"]["queries"] >= DIGEST_POSITIVE_RATE_FLOOR
        and candidate_groups["digest_positive"]["hit_at_1"]
            >= current_groups["digest_positive"]["hit_at_1"]
        and candidate_groups["digest_title_control"]["hit_at_1"]
            / candidate_groups["digest_title_control"]["queries"] >= DIGEST_TITLE_RATE_FLOOR
        and candidate_groups["digest_negative"]["false_recalls"] <= DIGEST_FALSE_LIMIT
        and paired_improvements >= 1
    )
    removal_passes = (
        removed["indexed_before"] == {"episode_vectors": 2, "fact_vectors": 1}
        and all(removed[key] is True for key in (
            "both_linked_episodes_absent", "fact_absent", "candidate_absent",
            "digest_absent_current", "digest_absent_candidate"))
        and all(removed[key] == 0 for key in (
            "candidate_source_rows", "linked_episode_live_rows",
            "episode_vector_rows", "fact_vector_rows"))
        and removed["removed_sources_returned"] is False
    )
    return {
        "format": "being-r3-vector-recipe-candidate-v1",
        "scope": "private synthetic archive; ephemeral candidate vectors; production recipe and indexes unchanged; "
                 "source-removal evidence covers logical SQLite rows after real backfill, not raw-block or free-page scrub",
        "recipe": RECIPE,
        "method": "cosine-only recipe comparison at the shipped personal/impersonal floors; this does not "
                  "claim the production hybrid FTS result",
        "provenance": _provenance(model_root, model),
        "arms": before,
        "source_removal": removed,
        "qualification": {"source_removal_passes": removal_passes,
                          "offline_recipe_gate_passes": offline_gate,
                          "paired_hit_at_1_improvements": paired_improvements,
                          "floors": {"acceptance_hit_at_1": ACCEPTANCE_FLOOR,
                                     "public_false_recalls": PUBLIC_FALSE_LIMIT,
                                     "digest_positive_hit_at_1_rate": DIGEST_POSITIVE_RATE_FLOOR,
                                     "digest_title_hit_at_1_rate": DIGEST_TITLE_RATE_FLOOR,
                                     "digest_false_recalls": DIGEST_FALSE_LIMIT},
                          "wp_c6_native_gate_passes": None, "production_promotion": False},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=MODEL)
    args = parser.parse_args(argv)
    model = embed.load(args.model)
    if model is None:
        parser.error(f"blink-8M unavailable in {args.model}")
    print(json.dumps(run(model, args.model), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
