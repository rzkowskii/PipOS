"""Convert a synthetic Stage 2 shadow eval archive to WP-E16 token replay.

The archive supplies recorded reply text and shadow sampling attempts. A
separately started, source-matched loopback llama-server reproduces the chat
template and tokenization. Only unambiguous, text-verified shadow IDs become
recorded-ID trajectories. Other replies are retokenized and round-tripped as
text-derived replay. Supply the identity file used by the original run.
"""

import argparse
from collections import defaultdict, deque
import hashlib
import http.client
import json
import os
from pathlib import Path
import time

from lab.head_witness import VOCAB, MAX_STEPS, digest_argument, lab_path, new_file, regular_input


MAX_LOG = 32 * 1024**2
MAX_MESSAGE = 64 * 1024
MAX_TOKENS = 4096
MAX_CONTINUATION = 64
MAX_CASES = 1024
SENSES = "19:05, battery 82% charging, cool, room bright"


def pinned_bytes(path, maximum):
    with regular_input(path, maximum) as (fd, info):
        with os.fdopen(os.dup(fd), "rb") as source:
            data = source.read(info.st_size)
        if len(data) != info.st_size:
            raise ValueError("input was truncated while reading")
    return data


def token_id(token):
    return type(token) is int and 0 <= token < VOCAB


def archive_cases(run, replies):
    metadata = json.loads(pinned_bytes(run, MAX_MESSAGE))
    if (not isinstance(metadata, dict) or metadata.get("mode") != "shadow" or metadata.get("split") != "eval"
            or not isinstance(metadata.get("seeds"), list) or not metadata["seeds"]
            or any(type(seed) is not int for seed in metadata["seeds"])):
        raise ValueError("expected a Stage 2 shadow eval run receipt")
    contents = pinned_bytes(replies, MAX_LOG)
    groups, seen = defaultdict(list), set()
    for raw in contents.splitlines():
        row = json.loads(raw)
        if (not isinstance(row, dict) or not isinstance(row.get("domain"), str)
                or not 1 <= len(row["domain"]) <= 64 or row.get("shape") not in ("strict", "open")
                or type(row.get("seed")) is not int or row["seed"] not in metadata["seeds"]
                or not isinstance(row.get("prompt"), str) or not 1 <= len(row["prompt"]) <= 4096
                or not isinstance(row.get("reply"), str) or len(row["reply"]) > 8192
                or type(row.get("n_predicted")) is not int or not 0 <= row["n_predicted"] <= 161
                or not isinstance(row.get("steps"), list) or len(row["steps"]) > 322):
            raise ValueError("unexpected Stage 2 archive reply")
        if not row["reply"] or not row["n_predicted"]:
            continue
        chosen = []
        for step in row["steps"]:
            if (not isinstance(step, dict) or not token_id(step.get("sel"))
                    or type(step.get("seq")) is not int or step["seq"] != 0):
                chosen.clear()
                break
            chosen.append(step["sel"])
        label = json.dumps([row["domain"], row["shape"], row["seed"], row["prompt"]],
                           ensure_ascii=False, separators=(",", ":")).encode()
        identifier = "case-" + hashlib.sha256(label).hexdigest()[:24]
        if identifier in seen:
            raise ValueError("duplicate archived Stage 2 case")
        seen.add(identifier)
        if len(chosen) != row["n_predicted"] or any(
                not isinstance(step, dict) or step.get("n_masked", 0) for step in row["steps"]):
            chosen = []
        groups[(row["domain"], row["shape"])].append((identifier, row["prompt"], row["reply"],
                                                     tuple(chosen), row["n_predicted"]))
    if not groups:
        raise ValueError("the held-out archive is empty")
    cohorts = [deque(sorted(group)) for _, group in sorted(groups.items())]
    selected, steps = [], 0
    while cohorts and steps < MAX_STEPS and len(selected) < MAX_CASES:
        following = []
        for group in cohorts:
            if steps >= MAX_STEPS or len(selected) >= MAX_CASES:
                break
            item = group.popleft()
            count = min(item[-1], MAX_CONTINUATION, MAX_STEPS - steps)
            selected.append(item)
            steps += count
            if group:
                following.append(group)
        cohorts = following
    return selected, hashlib.sha256(contents).hexdigest()


def post(port, route, payload, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("tokenize/apply-template budget expired")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=min(remaining, 10))
    try:
        connection.request("POST", route, json.dumps(payload).encode(),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(MAX_MESSAGE + 1)
        if response.status != 200 or len(raw) > MAX_MESSAGE:
            raise ValueError(f"loopback {route} returned an unusable result")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError(f"loopback {route} response is not an object")
        return result
    finally:
        connection.close()


def convert(args):
    identity = pinned_bytes(args.identity, MAX_MESSAGE)
    if hashlib.sha256(identity).hexdigest() != args.identity_sha256:
        raise ValueError("identity differs from the declared source snapshot")
    system = identity.decode("utf-8").strip().replace("{name}", "Pip").replace("{owner}", "Alex")
    if not system:
        raise ValueError("the Stage 2 identity prompt is empty")
    cases, replies_digest = archive_cases(args.run, args.replies)
    deadline = time.monotonic() + args.seconds
    records, steps, truncated, shadow_exact = [], 0, 0, 0
    for identifier, prompt, reply, shadow_ids, _predicted in cases:
        if steps >= MAX_STEPS:
            break
        rendered = post(args.port, "/apply-template", {
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": f"({SENSES})\n{prompt}"}],
            "chat_template_kwargs": {"enable_thinking": False},
        }, deadline).get("prompt")
        if not isinstance(rendered, str) or not rendered:
            raise ValueError("loopback did not render the Stage 2 prompt")
        prefix = post(args.port, "/tokenize", {
            "content": rendered, "add_special": True, "parse_special": True,
        }, deadline).get("tokens")
        if (not isinstance(prefix, list) or not 0 < len(prefix) < MAX_TOKENS
                or any(not token_id(token) for token in prefix)):
            raise ValueError("tokenized Stage 2 prompt is unqualified")
        if shadow_ids and post(args.port, "/detokenize", {"tokens": shadow_ids}, deadline).get("content") == reply:
            chosen = shadow_ids
            shadow_exact += 1
        else:
            chosen = post(args.port, "/tokenize", {
                "content": reply, "add_special": False, "parse_special": False,
            }, deadline).get("tokens")
            if (not isinstance(chosen, list) or not 1 <= len(chosen) <= MAX_TOKENS
                    or any(not token_id(token) for token in chosen)
                    or post(args.port, "/detokenize", {"tokens": chosen}, deadline).get("content") != reply):
                raise ValueError("recorded reply cannot be replayed losslessly as text")
        limit = min(MAX_CONTINUATION, MAX_STEPS - steps, MAX_TOKENS - len(prefix))
        truncated += len(chosen) > limit
        chosen = chosen[:limit]
        records.append("\t".join((identifier, " ".join(map(str, prefix)), " ".join(map(str, chosen)))))
        steps += len(chosen)
    encoded = ("\n".join(records) + "\n").encode("ascii")
    new_file(args.output, encoded)
    return {"source_replies_sha256": replies_digest, "identity_sha256": args.identity_sha256,
            "audition_sha256": hashlib.sha256(encoded).hexdigest(), "cases": len(records),
            "steps": steps, "truncated_replies": truncated, "shadow_id_cases": shadow_exact,
            "retokenized_reply_cases": len(records) - shadow_exact}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Stage 2 run.json")
    parser.add_argument("replies", type=Path, help="the synthetic replies.jsonl from that run")
    parser.add_argument("identity", type=Path, help="original config/identity.md")
    parser.add_argument("output", type=Path, help="new witness TSV outside /mind")
    parser.add_argument("--identity-sha256", required=True, type=digest_argument)
    parser.add_argument("--port", required=True, type=int, help="source-matched off-device llama-server port")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1 <= args.seconds <= 600:
        parser.error("choose a private loopback port and a 1–600 second budget")
    try:
        args.output = lab_path(args.output)
        print(json.dumps(convert(args), sort_keys=True))
    except (OSError, ValueError, http.client.HTTPException) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
