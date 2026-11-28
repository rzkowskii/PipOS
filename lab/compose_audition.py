"""Prepare and score the design 665 Compose blind audition.

Generation is deliberately lab-only: ten synthetic requests, five bounded
samples each, a loopback-only shipped dream-model endpoint, and no archive.
The blind packet never contains case names, prompts, seeds or checker results.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mind import dream_verify
from mind.dream_model import DreamAnswerInvalid, DreamClient, DreamUnavailable

CASES = ROOT / "lab/compose-audition-cases.json"
SAMPLES = 5
MAX_TOKENS = 220
MAX_DRAFT_CHARS = 1800
SEEDS = (66501, 66502, 66503, 66504, 66505)
SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string", "maxLength": MAX_DRAFT_CHARS}},
    "required": ["draft"],
    "additionalProperties": False,
}
SYSTEM = (
    "Write one concise letter using only the supplied request. Cover every requested point. "
    "Do not invent a name, number, date, address, promise or fact. Do not add placeholders, "
    "commentary, a subject line, or alternatives. End the letter cleanly. Return JSON only."
)
BOILERPLATE = re.compile(r"\b(?:as an ai|language model|insert name|your name|to whom it may concern)\b|\[[^]]+\]", re.I)
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
RELATIONS = dream_verify._RELATIONS
TIME_WORDS = dream_verify._TIME_QUALIFIERS
LETTER_WORDS = frozenset({
    # Fixed letter scaffolding and sentence-opening function words are not
    # entities. Proper nouns still come from the shared verifier and must be
    # present in the request.
    "best", "can", "could", "dear", "hello", "kind", "may", "please", "regards", "sincerely",
    "thank", "thanks", "warm", "would", "yours",
})
CLOSINGS = frozenset({"best regards", "kind regards", "regards", "sincerely", "thank you", "yours sincerely"})


def load_cases(path=CASES):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = value.get("cases") if isinstance(value, dict) and value.get("version") == 1 else None
    if not isinstance(cases, list) or len(cases) != 10:
        raise ValueError("the audition needs exactly ten version-1 cases")
    seen = set()
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"id", "request", "points"}
                or not isinstance(case["id"], str) or not isinstance(case["request"], str)
                or not isinstance(case["points"], list) or not case["points"] or case["id"] in seen):
            raise ValueError("invalid audition case")
        seen.add(case["id"])
        if any(not isinstance(point, list) or not point or any(not isinstance(term, str) or not term for term in point)
               for point in case["points"]):
            raise ValueError(f"invalid coverage points for {case['id']}")
    return cases


def request(case, seed):
    return {
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": case["request"]}],
        "temperature": 0.7,
        "top_k": 40,
        "seed": seed,
        "max_tokens": MAX_TOKENS,
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "compose_audition", "schema": SCHEMA}},
    }


def _terms(text):
    folded = dream_verify.fold(text)
    return {dream_verify._stem(word) for word in WORD.findall(folded)}


def _entities(text):
    # A neutral leading word prevents the shared verifier's sentence-start
    # exemption from hiding the first invented capitalised entity.
    return dream_verify._names("context " + text) - LETTER_WORDS


def checks(case, draft):
    """Deterministic gates; shared verifier logic supplies entity parsing."""
    source = case["request"]
    folded_draft = dream_verify.fold(draft)
    source_words = _terms(source)
    new_names = sorted(_entities(draft) - _entities(source))
    new_numbers = sorted(dream_verify._numbers(draft) - dream_verify._numbers(source))
    strict_words = {word for word in WORD.findall(folded_draft)
                    if word in RELATIONS or word in TIME_WORDS}
    new_strict = sorted(word for word in strict_words if dream_verify._stem(word) not in source_words)
    coverage = [all(dream_verify.fold(term) in folded_draft for term in point) for point in case["points"]]
    stripped = draft.rstrip(" \t\n\"'")
    tail = dream_verify.fold(stripped.rsplit("\n", 1)[-1]).rstrip(",")
    # A signature-less conventional closing is a complete letter too. The
    # client separately rejects a token-limit finish before this checker.
    ended = stripped.endswith((".", "!", "?")) or tail in CLOSINGS
    result = {
        "printable": bool(draft) and len(draft) <= MAX_DRAFT_CHARS and all(c.isprintable() or c in "\n\t" for c in draft),
        "no_new_names": not new_names,
        "no_new_numbers_dates": not new_numbers,
        "no_new_relations_times": not new_strict,
        "coverage": all(coverage),
        "clean_ending": ended,
        "no_boilerplate": BOILERPLATE.search(draft) is None,
    }
    result["pass"] = all(result.values())
    return result, {"coverage": coverage, "new_names": new_names,
                    "new_numbers_dates": new_numbers, "new_relations_times": new_strict}


def _rank(sample):
    gates = sample["checks"]
    return (gates["pass"], gates["coverage"], sum(value is True for key, value in gates.items() if key != "pass"),
            -len(sample["draft"]), -sample["seed"])


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _packet_identity(packet):
    """The immutable blind content; ratings are filled after sealing it."""
    if not isinstance(packet, dict):
        raise ValueError("blind packet is not an object")
    entries = packet.get("entries")
    if not isinstance(entries, list):
        raise ValueError("blind packet has no entries")
    return {
        "kind": packet.get("kind"),
        "instructions": packet.get("instructions"),
        "entries": [{"id": entry.get("id"), "draft": entry.get("draft")}
                    if isinstance(entry, dict) else None for entry in entries],
    }


def generate(args):
    if args.host not in ("127.0.0.1", "::1"):
        raise ValueError("Compose audition permits only a loopback model endpoint")
    if len(SEEDS) != SAMPLES:
        raise ValueError("the audition seed set must contain exactly five samples")
    cases = load_cases(args.cases)
    client = DreamClient(args.host, args.port, timeout=args.timeout)
    results = []
    for case in cases:
        samples = []
        for seed in SEEDS:
            answer, usage = client.json_reply(request(case, seed))
            draft = answer.get("draft") if isinstance(answer, dict) else None
            if not isinstance(draft, str):
                raise DreamAnswerInvalid("Compose answer had no draft")
            gates, detail = checks(case, draft)
            samples.append({"seed": seed, "draft": draft, "checks": gates,
                            "detail": detail, "usage": usage})
        best = max(samples, key=_rank)
        results.append({"case": case, "samples": samples, "selected_seed": best["seed"],
                        "selected": best})
    ordered = sorted(results, key=lambda row: hashlib.sha256(("compose-665:" + row["case"]["id"]).encode()).digest())
    entries = [{"id": chr(65 + index), "draft": row["selected"]["draft"], "rating": None}
               for index, row in enumerate(ordered)]
    packet_core = {"kind": "pipos-compose-blind-v1", "instructions": "Rate each draft from 1 to 5.",
                   "entries": entries}
    packet_digest = _digest(_packet_identity(packet_core))
    packet = {**packet_core, "packet_sha256": packet_digest}
    key = {"kind": "pipos-compose-key-v1", "packet_sha256": packet_digest,
           "threshold": {"rating_at_least": 3, "cases_at_least": 5, "cases_total": 10},
           "cases": [{"id": chr(65 + index), **row} for index, row in enumerate(ordered)]}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    outputs = ((out / "compose-blind.json", packet), (out / "compose-key.json", key))
    existing = [path for path, _ in outputs if path.exists()]
    if existing and not args.force:
        raise FileExistsError(f"refusing to replace {existing[0]}; pass --force")
    for path, value in outputs:
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def score(args):
    ratings = json.loads(Path(args.ratings).read_text(encoding="utf-8"))
    key = json.loads(Path(args.key).read_text(encoding="utf-8"))
    identity = _packet_identity(ratings)
    if not isinstance(key, dict) or ratings.get("packet_sha256") != _digest(identity) \
            or key.get("packet_sha256") != ratings.get("packet_sha256"):
        raise ValueError("ratings and answer key are not the same unchanged blind packet")
    entries = ratings.get("entries")
    if not isinstance(entries, list) or len(entries) != 10:
        raise ValueError("exactly ten ratings are required")
    keyed = key.get("cases")
    if not isinstance(keyed, list) or len(keyed) != 10 or any(
            not isinstance(entry, dict) or not isinstance(answer, dict)
            or not isinstance(answer.get("selected"), dict)
            or entry.get("id") != answer.get("id")
            or entry.get("draft") != answer["selected"].get("draft")
            for entry, answer in zip(entries, keyed)):
        raise ValueError("answer key does not describe the sealed blind packet")
    values = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "draft", "rating"} or type(entry["rating"]) is not int \
                or not 1 <= entry["rating"] <= 5:
            raise ValueError("every unchanged packet entry needs an integer rating from 1 to 5")
        values.append(entry["rating"])
    passed = sum(value >= 3 for value in values)
    report = {"kind": "pipos-compose-score-v1", "packet_sha256": ratings["packet_sha256"],
              "ratings": values, "rated_3_or_more": passed, "required": 5,
              "compose_gate": "pass" if passed >= 5 else "wait_for_writer_pack"}
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("generate", help="make the blind packet and separate answer key")
    make.add_argument("--cases", type=Path, default=CASES)
    make.add_argument("--host", default="127.0.0.1")
    make.add_argument("--port", type=int, default=8081)
    make.add_argument("--timeout", type=float, default=150)
    make.add_argument("--out", required=True)
    make.add_argument("--force", action="store_true")
    make.set_defaults(run=generate)
    judge = commands.add_parser("score", help="apply the separately reproducible 5-of-10 gate")
    judge.add_argument("--ratings", required=True)
    judge.add_argument("--key", required=True)
    judge.add_argument("--out")
    judge.set_defaults(run=score)
    args = parser.parse_args()
    try:
        args.run(args)
    except (OSError, ValueError, DreamUnavailable) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
