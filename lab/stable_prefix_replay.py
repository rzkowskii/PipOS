"""Plan WP-E6 stable-prefix commits from bounded synthetic ASR replays.

The planner is tokenizer-independent: every event carries the exact opaque
token IDs and character spans produced by the tokenizer under qualification.
It never calls a model or recognizer and cannot enable production read-ahead.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics


INPUT_KIND = "pipos-stable-prefix-replay-v1"
REPORT_KIND = "pipos-stable-prefix-plan-v1"
MAX_INPUT = 512 * 1024
MAX_TURNS = 100
MAX_EVENTS = 256
MAX_TEXT = 4096
MIN_COMMIT = 8
MAX_CHECKPOINTS = 6
MAX_TOKEN_ID = (1 << 31) - 1
WORD = re.compile(r"\S+")


def _read(path):
    with path.open("rb") as source:
        raw = source.read(MAX_INPUT + 1)
    if not 0 < len(raw) <= MAX_INPUT:
        raise ValueError("replay input is empty or exceeds 512 KiB")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _tokens(event):
    text = event.get("text")
    values = event.get("tokens")
    if not isinstance(text, str) or len(text) > MAX_TEXT or (text and not text.isprintable()):
        raise ValueError("event text must be bounded printable synthetic text")
    if not isinstance(values, list) or len(values) > 2048:
        raise ValueError("event needs bounded original tokenizer spans")
    result = []
    previous = 0
    covered = [False] * len(text)
    for item in values:
        if (not isinstance(item, dict) or set(item) != {"id", "start", "end"}
                or type(item["id"]) is not int or not 0 <= item["id"] <= MAX_TOKEN_ID
                or type(item["start"]) is not int
                or type(item["end"]) is not int or not 0 <= item["start"] < item["end"] <= len(text)
                or item["start"] < previous):
            raise ValueError("invalid or overlapping original token span")
        previous = item["end"]
        for position in range(item["start"], item["end"]):
            covered[position] = True
        result.append((item["id"], item["start"], item["end"]))
    if any(not char.isspace() and not covered[index] for index, char in enumerate(text)):
        raise ValueError("token spans do not cover every non-whitespace character")
    return text, result


def _stable_ends(previous, current):
    left = [(m.group(), m.end()) for m in WORD.finditer(previous)]
    right = [(m.group(), m.end()) for m in WORD.finditer(current)]
    left_end = right_end = 0
    for old, new in zip(left, right):
        if old[0] != new[0]:
            break
        left_end, right_end = old[1], new[1]
    return left_end, right_end


def _prefix(tokens, end):
    return tuple(token[0] for token in tokens if token[2] <= end)


def _compatible(checkpoint, current):
    return len(checkpoint) <= len(current) and tuple(current[:len(checkpoint)]) == checkpoint


def plan_turn(turn, token_rate, overhead, rollback_overhead, evidence):
    if not isinstance(turn, dict) or type(turn.get("synthetic")) is not bool:
        raise ValueError("every replay turn must state whether it is synthetic")
    if turn["synthetic"] != (evidence == "synthetic_fixture"):
        raise ValueError("turn classification differs from input provenance")
    name = turn.get("id")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", name):
        raise ValueError("turn ID must be a bounded non-private token")
    events = turn.get("events")
    if not isinstance(events, list) or not 2 <= len(events) <= MAX_EVENTS:
        raise ValueError("turn needs 2 to 256 events")
    scaffold = turn.get("scaffold_n", 0)
    baseline = turn.get("baseline_eos_to_first_token_ms")
    speech_end = turn.get("speech_end_ms")
    if type(scaffold) is not int or not 0 <= scaffold <= 4096:
        raise ValueError("scaffold_n is invalid")
    if type(baseline) not in (int, float) or not math.isfinite(baseline) or baseline < 0:
        raise ValueError("baseline timing is invalid")
    if type(speech_end) not in (int, float) or not math.isfinite(speech_end) or speech_end < 0:
        raise ValueError("an explicit acoustic end-of-speech time is required")

    checkpoints = [tuple()]
    committed = tuple()
    speculative = rolled_back = 0
    rollbacks = commits = 0
    checkpoint_high_water = 0
    previous_text = None
    previous_tokens = None
    last_revision = -1
    last_at = -1.0
    trace = []
    final_ids = None
    final_at = None
    serial_available_at = 0.0
    attempted_request_ms = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("kind") not in ("partial", "final"):
            raise ValueError("unknown replay event")
        revision, at = event.get("revision"), event.get("at_ms")
        if type(revision) is not int or revision <= last_revision:
            raise ValueError("event revisions must increase strictly")
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            raise ValueError("event time is invalid")
        if at < last_at:
            raise ValueError("event times must be monotonic")
        last_at = float(at)
        last_revision = revision
        text, tokens = _tokens(event)
        ids = tuple(token[0] for token in tokens)

        if not _compatible(committed, ids):
            before = len(committed)
            while checkpoints and not _compatible(checkpoints[-1], ids):
                checkpoints.pop()
            committed = checkpoints[-1] if checkpoints else tuple()
            rolled_back += before - len(committed)
            rollbacks += 1
            serial_available_at = max(float(at), serial_available_at) + rollback_overhead
            trace.append({"at_ms": at, "revision": revision, "action": "rollback",
                          "from_n": before, "to_n": len(committed),
                          "finished_ms": round(serial_available_at, 3)})

        if event["kind"] == "final":
            if index != len(events) - 1:
                raise ValueError("final must be the last event")
            final_ids, final_at = ids, at
            continue
        if previous_text is not None and at < speech_end:
            old_end, new_end = _stable_ends(previous_text, text)
            observed_before = _prefix(previous_tokens, old_end)
            candidate = _prefix(tokens, new_end)
            # Both observations must expose the same exact model-token prefix.
            if (observed_before == candidate and _compatible(committed, candidate)
                    and len(candidate) - len(committed) >= MIN_COMMIT):
                next_event = events[index + 1] if index + 1 < len(events) else None
                next_at = next_event.get("at_ms") if isinstance(next_event, dict) else None
                if type(next_at) not in (int, float) or not math.isfinite(next_at) or next_at < at:
                    raise ValueError("a commit has no valid following revision boundary")
                boundary = min(float(next_at), speech_end)
                delta = len(candidate) - len(committed)
                duration = overhead + delta * 1000.0 / token_rate
                started = max(float(at), serial_available_at)
                finished = started + duration
                attempted_request_ms.append(duration)
                if finished <= boundary:
                    speculative += delta
                    committed = candidate
                    checkpoints.append(committed)
                    serial_available_at = finished
                    # The empty base slot already exists; only committed snapshots
                    # consume the handover's six rollback checkpoints.
                    checkpoint_high_water = max(checkpoint_high_water, len(checkpoints) - 1)
                    commits += 1
                    trace.append({"at_ms": at, "revision": revision, "action": "commit",
                                  "committed_n": len(committed), "finished_ms": round(finished, 3)})
                elif started < boundary:
                    # The request is revoked at the new recognition boundary;
                    # cancellation/restore still occupies the one model lane.
                    serial_available_at = boundary + rollback_overhead
                    trace.append({"at_ms": at, "revision": revision, "action": "did_not_finish",
                                  "candidate_n": len(candidate), "would_finish_ms": round(finished, 3),
                                  "cancel_boundary_ms": boundary,
                                  "cancel_finished_ms": round(serial_available_at, 3)})
        previous_text = text
        previous_tokens = tokens

    if final_ids is None or final_at is None:
        raise ValueError("turn has no final event")
    if final_at < speech_end or baseline < final_at - speech_end:
        raise ValueError("end of speech, final transcript and baseline timings disagree")
    if not _compatible(committed, final_ids):
        raise AssertionError("final prefix was not rolled back")
    tail = len(final_ids) - len(committed)
    saving = len(committed) * 1000.0 / token_rate
    post_final_penalty = max(0.0, serial_available_at - final_at)
    net_gain = saving - post_final_penalty
    return {
        "id": name,
        "speech_end_ms": speech_end,
        "final_transcript_ms": final_at,
        "scaffold_n": scaffold,
        "speculative_n": speculative,
        "stable_final_n": len(committed),
        "rolled_back_n": rolled_back,
        "tail_n": tail,
        "commits": commits,
        "rollbacks": rollbacks,
        "checkpoint_high_water": checkpoint_high_water,
        "checkpoint_capacity_exceeded": checkpoint_high_water > MAX_CHECKPOINTS,
        "attempted_request_ms": [round(value, 3) for value in attempted_request_ms],
        "projected_prefill_avoided_ms": round(saving, 3),
        "projected_rollback_after_final_ms": round(post_final_penalty, 3),
        "projected_net_eos_gain_ms": round(net_gain, 3),
        "projected_eos_to_first_token_ms": round(max(0.0, baseline - net_gain), 3),
        "baseline_eos_to_first_token_ms": float(baseline),
        "trace": trace,
    }


def report(document, source_sha):
    if not isinstance(document, dict) or document.get("kind") != INPUT_KIND:
        raise ValueError(f"expected {INPUT_KIND}")
    rate = document.get("prefill_tokens_per_second")
    overhead = document.get("request_overhead_ms")
    rollback_overhead = document.get("rollback_overhead_ms")
    provenance = document.get("provenance")
    turns = document.get("turns")
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 1 <= rate <= 1000:
        raise ValueError("prefill token rate is invalid")
    if type(overhead) not in (int, float) or not math.isfinite(overhead) or not 0 <= overhead <= 60_000:
        raise ValueError("bounded measured request_overhead_ms is required")
    if (type(rollback_overhead) not in (int, float) or not math.isfinite(rollback_overhead)
            or not 0 <= rollback_overhead <= 60_000):
        raise ValueError("bounded measured rollback_overhead_ms is required")
    if not isinstance(provenance, dict) or provenance.get("evidence") not in (
            "synthetic_fixture", "consented_native_replay"):
        raise ValueError("explicit fixture or consented-native provenance is required")
    evidence = provenance["evidence"]
    if evidence == "synthetic_fixture":
        tokenizer = provenance.get("tokenizer")
        if (set(provenance) != {"evidence", "tokenizer"} or not isinstance(tokenizer, str)
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", tokenizer)):
            raise ValueError("synthetic fixture provenance needs its tokenizer label")
    else:
        digest = provenance.get("tokenizer_sha256")
        if (set(provenance) != {"evidence", "tokenizer_sha256"} or not isinstance(digest, str)
                or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)):
            raise ValueError("native replay provenance needs the exact tokenizer digest")
    if not isinstance(turns, list) or not 1 <= len(turns) <= MAX_TURNS:
        raise ValueError("replay needs 1 to 100 turns")
    rows = [plan_turn(turn, float(rate), float(overhead), float(rollback_overhead), evidence) for turn in turns]
    median_saving = statistics.median(row["projected_net_eos_gain_ms"] for row in rows)
    rollback_rate = sum(row["rollbacks"] for row in rows) / len(rows)
    return {
        "kind": REPORT_KIND,
        "source_sha256": source_sha,
        "turns": len(rows),
        "prefill_tokens_per_second": float(rate),
        "request_overhead_ms": float(overhead),
        "rollback_overhead_ms": float(rollback_overhead),
        "input_provenance": provenance,
        "median_projected_net_eos_gain_ms": round(median_saving, 3),
        "rollbacks_per_turn": round(rollback_rate, 6),
        "checkpoint_capacity_exceeded": any(row["checkpoint_capacity_exceeded"] for row in rows),
        "descriptive_kill_reasons": [reason for failed, reason in (
            (median_saving < 400, "median serially completed prefill saving is under 0.4 seconds"),
            (rollback_rate > 1, "more than one rollback per turn"),
            (any(row["checkpoint_capacity_exceeded"] for row in rows), "six checkpoint slots are insufficient"),
        ) if failed],
        "native_acceptance": False,
        "production_prefill": False,
        "turn_results": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", type=Path)
    args = parser.parse_args(argv)
    try:
        document, source_sha = _read(args.replay)
        result = report(document, source_sha)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, AssertionError) as error:
        parser.exit(1, f"stable-prefix replay refused: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
