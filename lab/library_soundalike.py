#!/usr/bin/env python3
"""Compare exact Simple-English lookup with an offline sound-alike candidate.

This is a lab-only, ask-before-answer experiment for design 716.  It never
changes ``mind.library`` and never treats generated corruptions as speech
evidence.  Input JSONL rows must come from independently recognised audio::

  {"id":"name-001","asr_transcript":"look up ... in my simple english library",
   "expected_title":"Ada Lovelace","audio_file":"name-001.wav","audio_sha256":"<64 hex>",
   "asr_engine":"moonshine","asr_model_sha256":"<64 hex>"}

No-match evidence uses ``"expected_title": null``.  Recognition metadata is
externally asserted by this supplied manifest; audio bytes are verified, but
the manifest is not presented as a cryptographic ASR transcript attestation.

The signed installed Simple-English pack supplies candidate titles.  Piper's
pinned local espeak-ng frontend supplies pronunciations.  Exact/redirect lookup
is the unchanged baseline.  The candidate scans titles only to make a bounded
orthographic shortlist, then compares phonemes.  A match is merely a proposed
"Did you mean ...?" title; this program never opens or speaks its article.
"""

import argparse
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import heapq
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_AUDIO_BYTES = 32 * 1024 * 1024
MAX_CASES = 250
MIN_QUALIFYING_CASES = 100
MIN_NEGATIVE_CASES = 20
MAX_TITLES = 300_000
MAX_SECONDS = 600.0
SHORTLIST = 32
MAX_PHONEMES = 320
MAX_DISTANCE = 0.35
MIN_MARGIN = 0.08
HEX = frozenset("0123456789abcdef")


def regular_bytes(path, limit=MAX_INPUT_BYTES):
    path = Path(path)
    before = path.lstat()
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= limit):
        raise ValueError("observations must be a bounded regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if identity(opened) != identity(before):
            raise ValueError("observations changed while opening")
        data = b""
        while len(data) < opened.st_size:
            block = os.read(descriptor, min(1 << 20, opened.st_size - len(data)))
            if not block:
                raise ValueError("observations changed while reading")
            data += block
        if identity(os.fstat(descriptor)) != identity(opened):
            raise ValueError("observations changed while reading")
        return data
    finally:
        os.close(descriptor)


def digest_value(value):
    return isinstance(value, str) and len(value) == 64 and all(char in HEX for char in value)


def audio_digest(root, relative):
    if (not isinstance(relative, str) or not 1 <= len(relative) <= 180
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("audio path is not a portable relative path")
    supplied = root / relative
    supplied_info = supplied.lstat()
    if stat.S_ISLNK(supplied_info.st_mode):
        raise ValueError("audio evidence may not be a symlink")
    path = supplied.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("audio evidence escapes its selected root") from error
    before = path.lstat()
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
            or not 44 <= before.st_size <= MAX_AUDIO_BYTES):
        raise ValueError("audio evidence is not a bounded regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if identity(opened) != identity(before):
            raise ValueError("audio evidence changed while opening")
        validate_pcm_wav(descriptor, opened.st_size)
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(1 << 20, remaining))
            if not block:
                raise ValueError("audio evidence changed while hashing")
            digest.update(block)
            remaining -= len(block)
        if identity(os.fstat(descriptor)) != identity(opened):
            raise ValueError("audio evidence changed while reading")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def validate_pcm_wav(descriptor, size):
    """Reject renamed/non-PCM input and inconsistent RIFF/chunk lengths."""
    header = os.pread(descriptor, min(size, 1 << 20), 0)
    if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("audio evidence is not a RIFF WAVE file")
    if struct.unpack_from("<I", header, 4)[0] + 8 != size:
        raise ValueError("audio evidence has an inconsistent RIFF length")
    offset, fmt_seen, data_seen, pcm_align = 12, False, False, None
    while offset + 8 <= len(header):
        kind = header[offset:offset + 4]
        length = struct.unpack_from("<I", header, offset + 4)[0]
        body, following = offset + 8, offset + 8 + length + (length & 1)
        if following > size:
            raise ValueError("audio evidence has an invalid chunk length")
        if kind == b"fmt ":
            if length < 16 or body + 16 > len(header):
                raise ValueError("audio evidence has an invalid PCM format chunk")
            encoding, channels, rate, byte_rate, block_align, bits = struct.unpack_from(
                "<HHIIHH", header, body)
            if (encoding != 1 or channels not in (1, 2) or not 8_000 <= rate <= 192_000
                    or bits not in (8, 16, 24, 32) or block_align != channels * bits // 8
                    or byte_rate != rate * block_align):
                raise ValueError("audio evidence is not bounded integer PCM")
            fmt_seen, pcm_align = True, block_align
        elif kind == b"data":
            if not length or pcm_align is None or length % pcm_align:
                raise ValueError("audio evidence has invalid PCM sample data")
            data_seen = True
            break
        if following > len(header):
            raise ValueError("audio evidence header exceeds its bound")
        offset = following
    if not fmt_seen or not data_seen:
        raise ValueError("audio evidence lacks bounded PCM format or sample data")


def load_observations(path, audio_root):
    raw = regular_bytes(path)
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("observations are not UTF-8") from error
    rows, ids, audio = [], set(), set()
    fields = {"id", "asr_transcript", "expected_title", "audio_file", "audio_sha256",
              "asr_engine", "asr_model_sha256"}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if len(rows) >= MAX_CASES:
            raise ValueError("observation count exceeds its bound")
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ValueError(f"invalid JSON on line {number}") from error
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(f"invalid observation fields on line {number}")
        identifier, transcript, expected = row["id"], row["asr_transcript"], row["expected_title"]
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 80 or identifier in ids
                or not isinstance(transcript, str) or len(transcript) > 240
                or not (expected is None or isinstance(expected, str) and 1 <= len(expected) <= 256)
                or not isinstance(row["asr_engine"], str) or not 1 <= len(row["asr_engine"]) <= 80
                or not digest_value(row["audio_sha256"]) or row["audio_sha256"] in audio
                or not digest_value(row["asr_model_sha256"])):
            raise ValueError(f"invalid or duplicate observation on line {number}")
        if audio_digest(audio_root, row["audio_file"]) != row["audio_sha256"]:
            raise ValueError(f"audio digest mismatch on line {number}")
        ids.add(identifier)
        audio.add(row["audio_sha256"])
        rows.append(row)
    if not rows:
        raise ValueError("observations are empty")
    return rows, hashlib.sha256(raw).hexdigest()


def key(text):
    return " ".join(str(text).replace("_", " ").casefold().split())


def grams(text):
    padded = f"  {key(text)}  "
    return frozenset(padded[index:index + 3] for index in range(len(padded) - 2))


def shortlist(query, titles):
    wanted = grams(query)
    by_canonical = {}
    for title_key, spoken, canonical in titles:
        candidate = grams(title_key)
        score = len(wanted & candidate) / max(1, len(wanted | candidate))
        item = (score, title_key, spoken, canonical)
        if item > by_canonical.get(canonical, (-1.0, "", "", canonical)):
            by_canonical[canonical] = item
    best = []
    for item in by_canonical.values():
        if len(best) < SHORTLIST:
            heapq.heappush(best, item)
        elif item > best[0]:
            heapq.heapreplace(best, item)
    return [(title_key, spoken, canonical)
            for _score, title_key, spoken, canonical in sorted(best, reverse=True)]


def edit_distance(left, right):
    if len(left) > MAX_PHONEMES or len(right) > MAX_PHONEMES:
        raise ValueError("phoneme sequence exceeds its bound")
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for index, item in enumerate(left, 1):
        current = [index]
        for offset, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[offset] + 1,
                               previous[offset - 1] + (item != other)))
        previous = current
    return previous[-1] / max(1, len(left), len(right))


class Candidate:
    def __init__(self, phonemizer, titles):
        self.phonemizer = phonemizer
        self.titles = titles
        self.canonical_titles = frozenset(canonical for _title_key, _spoken, canonical in titles)

    @lru_cache(maxsize=4096)
    def pronunciation(self, text):
        sentences = self.phonemizer.sentences(text)
        value = tuple(point for sentence in sentences for point in sentence)
        if not value or len(value) > MAX_PHONEMES:
            raise ValueError("title has no bounded pronunciation")
        return value

    def propose(self, query):
        heard = self.pronunciation(query)
        ranked = []
        for title_key, spoken, canonical in shortlist(query, self.titles):
            try:
                distance = edit_distance(heard, self.pronunciation(spoken))
            except (RuntimeError, ValueError):
                continue
            ranked.append((distance, title_key, canonical))
        ranked.sort()
        if not ranked:
            return None, None, None
        first = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 1.0
        if first[0] > MAX_DISTANCE or second - first[0] < MIN_MARGIN:
            return None, first[0], second - first[0]
        return first[2], first[0], second - first[0]


@contextmanager
def trusted_titles(library, model_pack):
    path = library.ENCYCLOPEDIA
    if not library.ENCYCLOPEDIA_ENABLE.is_file():
        raise ValueError("Simple-English lookup is not owner-enabled; baseline comparison is invalid")
    trusted, reason = model_pack.trusted("library_simple", [path])
    if not trusted:
        raise ValueError(f"installed Simple-English pack is not trusted: {reason}")
    supplied = path.resolve(strict=True)
    descriptor = os.open(supplied, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    before = os.fstat(descriptor)
    current = supplied.stat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                              value.st_mtime_ns, value.st_ctime_ns)
    if identity(before) != identity(current):
        os.close(descriptor)
        raise ValueError("installed Simple-English pack changed after trust verification")
    digest = hashlib.sha256()
    remaining = before.st_size
    while remaining:
        block = os.read(descriptor, min(1 << 20, remaining))
        if not block:
            os.close(descriptor)
            raise ValueError("installed Simple-English pack changed while hashing")
        digest.update(block)
        remaining -= len(block)
    trusted_again, reason = model_pack.trusted("library_simple", [path])
    if not trusted_again or identity(supplied.stat()) != identity(before):
        os.close(descriptor)
        raise ValueError(f"installed Simple-English pack changed during trust check: {reason}")
    fd_root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    try:
        db = sqlite3.connect(f"file:{fd_root}/{descriptor}?mode=ro&immutable=1", uri=True, timeout=0.2)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        metadata = dict(db.execute(
            "SELECT key,value FROM metadata WHERE key IN ('source','license','snapshot','schema')"))
        if (metadata.get("source") != library.SIMPLE_SOURCE
                or metadata.get("license") != library.SIMPLE_LICENSE or metadata.get("schema") != "2"
                or not metadata.get("snapshot")):
            raise ValueError("installed Simple-English metadata is invalid")
        article_rows = list(db.execute(
            "SELECT title_key,title FROM articles ORDER BY title_key LIMIT ?", (MAX_TITLES + 1,)))
        if not 1 <= len(article_rows) <= MAX_TITLES:
            raise ValueError("installed title count exceeds the candidate bound")
        remaining = MAX_TITLES - len(article_rows)
        redirect_rows = list(db.execute(
            "SELECT r.title_key,r.title,a.title FROM redirects r "
            "JOIN articles a ON a.title_key=r.target_key "
            "ORDER BY r.title_key LIMIT ?", (remaining + 1,)))
        if len(article_rows) + len(redirect_rows) > MAX_TITLES:
            raise ValueError("installed title and redirect count exceeds the candidate bound")
        titles = [(title_key, title, title) for title_key, title in article_rows]
        redirects = [(title_key, redirect, canonical)
                     for title_key, redirect, canonical in redirect_rows]
        def exact(title):
            title_key = key(title)
            row = db.execute("SELECT title FROM articles WHERE title_key=?", (title_key,)).fetchone()
            if row is not None:
                return "direct", row[0], ()
            row = db.execute(
                "SELECT a.title FROM redirects r JOIN articles a ON a.title_key=r.target_key "
                "WHERE r.title_key=?", (title_key,)).fetchone()
            if row is not None:
                return "redirect", row[0], ()
            if metadata.get("schema") == "2":
                alternatives = db.execute(
                    "SELECT a.title FROM alternatives x JOIN articles a ON a.title_key=x.title_key "
                    "WHERE x.query_key=? ORDER BY a.title_key LIMIT 5", (title_key,)).fetchall()
                if len(alternatives) == 1:
                    return "alternate", alternatives[0][0], ()
                if len(alternatives) > 1:
                    return "ambiguous", None, tuple(row[0] for row in alternatives[:4])
            return "miss", None, ()
        yield tuple(titles + redirects), metadata, exact, {
            "bytes": before.st_size, "sha256": digest.hexdigest(),
            "device": f"{os.major(before.st_dev)}:{os.minor(before.st_dev)}", "inode": before.st_ino}
        if identity(os.fstat(descriptor)) != identity(before):
            raise ValueError("installed Simple-English pack changed during evaluation")
    finally:
        db.close()
        os.close(descriptor)


def evaluate(rows, candidate, library, exact_lookup, deadline):
    exact = proposed = false_baseline = false_final = false_added = 0
    results = []
    engines = set()
    models = set()
    soft_exceeded = False
    for row in rows:
        if time.monotonic() >= deadline:
            soft_exceeded = True
            break
        if row["expected_title"] is not None and row["expected_title"] not in candidate.canonical_titles:
            raise ValueError("an externally labelled expected title is absent from the signed pack")
        mode, query = library._mode(library.clean(row["asr_transcript"]).casefold())
        if mode != "encyclopedia":
            baseline_outcome = "not-encyclopedia"
            baseline_title = None
            baseline_alternatives = ()
            title = distance = margin = None
        else:
            baseline_outcome, baseline_title, baseline_alternatives = exact_lookup(query)
            title, distance, margin = ((None, None, None) if baseline_outcome != "miss"
                                       else candidate.propose(query))
        expected = row["expected_title"]
        baseline_ok = expected is not None and baseline_title == expected
        candidate_ok = expected is not None and (baseline_ok or title == expected)
        negative = expected is None
        baseline_false = baseline_title is not None and baseline_title != expected
        final_title = baseline_title if baseline_title is not None else title
        final_false = final_title is not None and final_title != expected
        added_false = baseline_title is None and title is not None and title != expected
        exact += baseline_ok
        proposed += candidate_ok
        false_baseline += baseline_false
        false_final += final_false
        false_added += added_false
        engines.add(row["asr_engine"])
        models.add(row["asr_model_sha256"])
        results.append({"id": row["id"], "negative": negative, "baseline": baseline_ok,
                        "baseline_outcome": baseline_outcome,
                        "baseline_alternatives": list(baseline_alternatives),
                        "candidate": candidate_ok, "baseline_false_offer": baseline_false,
                        "final_false_offer": final_false, "candidate_added_false_offer": added_false,
                        "proposed": title is not None, "distance": round(distance, 4) if distance is not None else None,
                        "margin": round(margin, 4) if margin is not None else None})
        if time.monotonic() >= deadline:
            soft_exceeded = True
            break
    positives = sum(row["expected_title"] is not None for row in rows)
    negatives = sum(row["expected_title"] is None for row in rows)
    evaluated_positives = sum(not row["negative"] for row in results)
    evaluated_negatives = sum(row["negative"] for row in results)
    gain = 100.0 * (proposed - exact) / max(1, evaluated_positives)
    consistent = len(engines) == 1 and len(models) == 1
    enough = (not soft_exceeded and len(results) == len(rows) and positives >= MIN_QUALIFYING_CASES
              and negatives >= MIN_NEGATIVE_CASES and consistent)
    false_gate = false_added == 0 and false_final <= false_baseline
    clause = "met" if gain >= 10 and false_gate else "drop"
    return {"cases_supplied": len(rows), "cases_evaluated": len(results),
            "positive_cases": evaluated_positives, "negative_cases": evaluated_negatives,
            "baseline_correct": exact, "candidate_correct": proposed,
            "baseline_false_offers": false_baseline, "final_false_offers": false_final,
            "candidate_added_false_offers": false_added,
            "false_offer_gate": "met" if false_gate else "drop",
            "gain_percentage_points": round(gain, 3),
            "ten_point_clause": clause if enough else "insufficient",
            "qualification": ("externally asserted single-recognizer evidence: at least 100 labelled positives, "
                              "20 no-match negatives, and no added false offers" if enough else
                              "needs complete soft-budget evaluation, 100 positives and 20 negatives from one "
                              "externally asserted recognizer/model; no ten-point decision"),
            "timing": {"kind": "soft-budget", "budget_exceeded": soft_exceeded,
                       "native_call_is_killable": False},
            "asr_engines": sorted(engines), "asr_model_sha256": sorted(models), "results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--audio-root", type=Path, required=True,
                        help="directory containing the independently recognised WAV evidence")
    parser.add_argument("--phonemize-library", type=Path, required=True,
                        help="installed pinned piper-phonemize shared library")
    parser.add_argument("--espeak-data", type=Path, required=True,
                        help="installed signed voice pack's espeak-ng-data directory")
    parser.add_argument("--voice", default="en-us", help="pinned espeak voice name")
    parser.add_argument("--seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= MAX_SECONDS:
        parser.error(f"--seconds must be between 1 and {MAX_SECONDS:g}")
    try:
        supplied_root = args.audio_root
        root_info = supplied_root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError("--audio-root must be a real directory")
        audio_root = supplied_root.resolve(strict=True)
        rows, observations_sha256 = load_observations(args.observations, audio_root)
        from mind import library, model_pack, phonemize
        phonemize_info = args.phonemize_library.lstat()
        if (stat.S_ISLNK(phonemize_info.st_mode) or not stat.S_ISREG(phonemize_info.st_mode)
                or args.phonemize_library.resolve(strict=True) != phonemize.LIBRARY.resolve(strict=True)):
            raise ValueError("phonemizer must be the installed pinned system library")
        voice_trusted, voice_reason = model_pack.trusted("tts", [args.espeak_data])
        if not voice_trusted:
            raise ValueError(f"espeak data is not in the trusted TTS pack: {voice_reason}")
        engine = phonemize.Phonemizer(args.espeak_data, args.voice, library=args.phonemize_library)
        with trusted_titles(library, model_pack) as (titles, metadata, exact_lookup, library_identity):
            result = evaluate(rows, Candidate(engine, titles), library, exact_lookup,
                              time.monotonic() + args.seconds)
            result.update({"schema": 2, "mode": "offline-ask-only", "live_library_changed": False,
                           "observations_sha256": observations_sha256,
                           "recognition_provenance": "externally asserted by supplied JSONL; audio bytes verified, "
                                                     "transcript/model relationship not cryptographically certified",
                           "library_snapshot": metadata["snapshot"], "library_identity": library_identity,
                           "candidate_titles": len(titles),
                           "thresholds": {"max_phoneme_distance": MAX_DISTANCE,
                                          "minimum_margin": MIN_MARGIN, "shortlist": SHORTLIST,
                                          "minimum_positive_cases": MIN_QUALIFYING_CASES,
                                          "minimum_negative_cases": MIN_NEGATIVE_CASES,
                                          "candidate_added_false_offer_limit": 0}})
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result["ten_point_clause"] == "met" else 1


if __name__ == "__main__":
    raise SystemExit(main())
