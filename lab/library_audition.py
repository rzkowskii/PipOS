#!/usr/bin/env python3
"""Score an externally labelled typed/spoken audition of the shipped library.

The JSONL corpus is supplied separately.  This runner never manufactures
questions or expected answers from the installed databases: that would make
the evaluation circular.  It uses mind.library's real routing and qualified
lookup helpers, and emits no question, transcript, definition or article text.

Each non-blank input line is one object::

  {"id":"simple-001","source":"simple","typed":"...",
   "expected":{"mode":"encyclopedia","outcome":"hit","entry":"Ada Lovelace"},
   "spoken_transcript":"..."}

``source`` is ``simple``, ``dictionary``, ``guide`` or ``persona-negative``.
For dictionary and guide hits, ``entry`` is the lowercase SHA-256 of the
canonical first entry described below.  Simple-English hits use the externally
labelled canonical title.  Misses use ``entry:null`` and outcome ``miss``.
Persona negatives require mode ``none``, outcome ``not-captured`` and null
entry.  ``spoken_transcript`` must be the later recognizer output for the same
prompt. A separate ``--spoken-transcripts`` file binds those strings to the
exact frozen corpus SHA-256 without editing the expected answers.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


MAX_CORPUS_BYTES = 4 * 1024 * 1024
MAX_CASES = 1000
MAX_TEXT = 240
MINIMUMS = {"simple": 100, "dictionary": 50, "guide": 50,
            "persona-negative": 1}
SOURCES = frozenset(MINIMUMS)
OUTCOMES = frozenset(("hit", "miss", "ambiguous", "not-captured"))
MODES = frozenset(("encyclopedia", "define", "define_implicit", "explicit",
                   "implicit", "none"))


def _regular_bytes(path):
    path = Path(path)
    before = path.lstat()
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= MAX_CORPUS_BYTES):
        raise ValueError("audition input must be a bounded regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if identity(opened) != identity(before):
            raise ValueError("audition input changed while opening")
        data = bytearray()
        while len(data) < opened.st_size:
            block = os.read(descriptor, min(1 << 20, opened.st_size - len(data)))
            if not block:
                raise ValueError("audition input changed while reading")
            data.extend(block)
        if identity(os.fstat(descriptor)) != identity(opened):
            raise ValueError("audition input changed while reading")
        return bytes(data)
    finally:
        os.close(descriptor)


def _digest(*parts):
    value = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _expected(value, source):
    if not isinstance(value, dict) or set(value) != {"mode", "outcome", "entry"}:
        raise ValueError("expected must contain exactly mode, outcome and entry")
    mode, outcome, entry = value["mode"], value["outcome"], value["entry"]
    if not isinstance(mode, str) or not isinstance(outcome, str) or mode not in MODES or outcome not in OUTCOMES:
        raise ValueError("expected mode or outcome is invalid")
    if entry is not None and (not isinstance(entry, str) or not 1 <= len(entry) <= 160):
        raise ValueError("expected entry is invalid")
    if outcome in ("miss", "ambiguous", "not-captured") and entry is not None:
        raise ValueError("a non-hit may not carry an expected entry")
    if outcome == "hit" and entry is None:
        raise ValueError("a hit requires an independently labelled entry")
    if source == "persona-negative" and (mode, outcome, entry) != ("none", "not-captured", None):
        raise ValueError("persona negatives must be labelled not-captured")
    if source in ("dictionary", "guide") and outcome == "hit" and (
            entry is None or len(entry) != 64 or any(c not in "0123456789abcdef" for c in entry)):
        raise ValueError("dictionary and guide hits require a lowercase SHA-256 entry label")
    return value


def load_cases(path):
    raw = _regular_bytes(path)
    cases, identifiers, queries = [], set(), set()
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("corpus is not UTF-8") from error
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if len(cases) >= MAX_CASES:
            raise ValueError("corpus exceeds the case bound")
        try:
            case = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ValueError(f"invalid JSON on line {number}") from error
        allowed = {"id", "source", "typed", "expected", "spoken_transcript"}
        if not isinstance(case, dict) or not set(case) <= allowed or not {
                "id", "source", "typed", "expected"} <= set(case):
            raise ValueError(f"invalid fields on line {number}")
        identifier, source, typed = case["id"], case["source"], case["typed"]
        spoken = case.get("spoken_transcript")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", identifier)
                or identifier in identifiers or not isinstance(source, str) or source not in SOURCES
                or not isinstance(typed, str) or not 1 <= len(typed) <= MAX_TEXT
                or spoken is not None and (not isinstance(spoken, str) or len(spoken) > MAX_TEXT)):
            raise ValueError(f"invalid case on line {number}")
        _expected(case["expected"], source)
        query = " ".join(typed.casefold().split())
        if not query or (source, query) in queries:
            raise ValueError(f"duplicate or empty typed query on line {number}")
        identifiers.add(identifier)
        queries.add((source, query))
        cases.append(case)
    if not cases:
        raise ValueError("corpus is empty")
    return cases, hashlib.sha256(raw).hexdigest()


def load_transcripts(path, cases, corpus_sha256):
    raw = _regular_bytes(path)
    try:
        source = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("invalid spoken transcript JSON") from error
    if (not isinstance(source, dict)
            or set(source) != {"schema", "corpus_sha256", "recognizer", "transcripts"}
            or type(source["schema"]) is not int or source["schema"] != 1
            or source["corpus_sha256"] != corpus_sha256):
        raise ValueError("spoken transcripts must identify the exact audition corpus")
    recognizer = source["recognizer"]
    transcripts = source["transcripts"]
    case_ids = {case["id"] for case in cases}
    if not isinstance(recognizer, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", recognizer):
        raise ValueError("spoken transcripts need a recognizer identifier")
    if (not isinstance(transcripts, dict) or not 1 <= len(transcripts) <= len(case_ids)
            or not transcripts.keys() <= case_ids
            or any(not isinstance(value, str) or len(value) > MAX_TEXT for value in transcripts.values())):
        raise ValueError("spoken transcripts must use known IDs and bounded text")
    if any(case.get("spoken_transcript") is not None for case in cases):
        raise ValueError("use either inline or separate spoken transcripts")
    return transcripts, hashlib.sha256(raw).hexdigest(), recognizer


def lookup(text, source, library):
    """Return only route/outcome/opaque expected-entry identity."""
    cleaned = library.clean(text, "pip").casefold()
    mode, topic = library._mode(cleaned)
    if source == "persona-negative":
        status = "no-library-route"
        if mode == "implicit":
            guide = library._guide()
            captured = topic in guide
            status = "ready" if guide else "unavailable"
        elif mode == "define_implicit":
            status, rows = library.definitions(topic)
            captured = status == "ready" and bool(rows)
        else:
            captured = bool(mode) and mode != "others"
        return {"mode": mode if captured else "none", "outcome": "captured" if captured else "not-captured",
                "entry": None, "status": status}
    if source == "simple":
        if mode != "encyclopedia":
            return {"mode": mode or "none", "outcome": "miss", "entry": None,
                    "status": "route-miss"}
        status, found = library.article(topic, include_snapshot=True)
        if status == "ambiguous":
            return {"mode": mode, "outcome": "ambiguous", "entry": None, "status": status}
        return {"mode": mode, "outcome": "hit" if status == "ready" and found else "miss",
                "entry": found[0] if status == "ready" and found else None, "status": status}
    if source == "dictionary":
        if mode not in ("define", "define_implicit"):
            return {"mode": mode or "none", "outcome": "miss", "entry": None,
                    "status": "route-miss"}
        status, rows = library.definitions(topic)
        entry = (_digest(rows[0].word, rows[0].part, rows[0].text)
                 if status == "ready" and rows else None)
        return {"mode": mode, "outcome": "hit" if entry else "miss", "entry": entry,
                "status": status}
    guide = library._guide()
    entry = guide.get(topic) if mode in ("explicit", "implicit") else None
    return {"mode": mode or "none", "outcome": "hit" if entry else "miss",
            "entry": _digest(entry.summary, *entry.steps) if entry else None,
            "status": "ready" if guide else "unavailable"}


def _matches(observed, expected):
    return (observed["status"] in ("ready", "ambiguous", "no-library-route")
            and all(observed[name] == expected[name] for name in ("mode", "outcome", "entry")))


def score(cases, library, transcripts=None):
    counts = {source: 0 for source in SOURCES}
    expected_outcomes = {source: {} for source in SOURCES}
    guide_questions = 0
    labeled_entries = {source: set() for source in SOURCES if source != "persona-negative"}
    typed_ok = spoken_ok = spoken_supplied = spoken_empty = 0
    failures = []
    statuses = {}
    for case in cases:
        source, expected = case["source"], case["expected"]
        counts[source] += 1
        outcomes = expected_outcomes[source]
        outcome = expected["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if source in labeled_entries and expected["outcome"] == "hit":
            labeled_entries[source].add(expected["entry"])
            guide_questions += source == "guide"
        typed = lookup(case["typed"], source, library)
        statuses[typed["status"]] = statuses.get(typed["status"], 0) + 1
        typed_match = _matches(typed, expected)
        typed_ok += typed_match
        spoken_match = None
        transcript = (transcripts.get(case["id"]) if transcripts is not None
                      else case.get("spoken_transcript"))
        if transcript is not None:
            spoken_supplied += 1
            if transcript.strip():
                heard = lookup(transcript, source, library)
                statuses[heard["status"]] = statuses.get(heard["status"], 0) + 1
                spoken_match = _matches(heard, expected)
            else:
                spoken_empty += 1
                spoken_match = False
            spoken_ok += spoken_match
        if (not typed_match or spoken_match is False) and len(failures) < 100:
            failures.append({"id": case["id"], "typed": typed_match, "spoken": spoken_match,
                             "typed_outcome": typed["outcome"]})
    distinct = {source: len(entries) for source, entries in labeled_entries.items()}
    coverage = {**distinct, "guide": guide_questions,
                "persona-negative": counts["persona-negative"]}
    quotas = {
        source: {"count": counts[source], "qualifying_cases": coverage[source],
                 "distinct_labels": distinct.get(source), "minimum": minimum,
                 "met": coverage[source] >= minimum}
        for source, minimum in MINIMUMS.items()
    }
    complete = all(item["met"] for item in quotas.values()) and spoken_supplied == len(cases)
    return {"status": "coverage-complete" if complete else "incomplete",
            "all_cases_matched": complete and typed_ok == len(cases) and spoken_ok == len(cases),
            "promotion": "not-evaluated: this harness has no benchmark pass threshold",
            "cases": len(cases), "quotas": quotas, "expected_outcomes": expected_outcomes,
            "typed": {"matched": typed_ok, "total": len(cases)},
            "spoken": {"matched": spoken_ok, "supplied": spoken_supplied, "empty": spoken_empty,
                       "required": len(cases)},
            "lookup_statuses": statuses, "failures": failures}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="externally labelled UTF-8 JSONL corpus")
    parser.add_argument("--spoken-transcripts", type=Path, help="ASR JSON bound to the exact corpus")
    args = parser.parse_args(argv)
    try:
        cases, corpus_sha256 = load_cases(args.corpus)
        transcripts, transcript_sha256, recognizer = (None, None, None)
        if args.spoken_transcripts is not None:
            transcripts, transcript_sha256, recognizer = load_transcripts(
                args.spoken_transcripts, cases, corpus_sha256)
        from mind import library
        result = score(cases, library, transcripts)
        result.update({"schema": 2, "corpus_sha256": corpus_sha256,
                       "transcript_sha256": transcript_sha256, "recognizer": recognizer,
                       "entry_labels": "external-title-or-sha256", "contains_text": False})
    except (OSError, ValueError) as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result["all_cases_matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
