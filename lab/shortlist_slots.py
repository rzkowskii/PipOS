"""Exercise the Stage 2 shortlist with two isolated conversation/workshop slots.

This synthetic investigation uses its own temporary Unix socket and server.
It records token IDs and timings without using conversation data or promoting
the candidate. Run after the implementation-only period, on the intended CPU.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

from lab.native_mapping_bench import cpu_facts


KIND = "pipos-shortlist-slots-v1"
MODES = ("full", "shadow", "active")
SLOT_TOKENS = 4096
DYNAMIC = 1024
KIT_SOURCE = "67534958fb385cac"
KIT_SUMS = "1668f6f9fa71a1e281f0a2e952ad74e8149c9a3884a4a474ae8fd24ddd41b43c"
MAX_JSON = 1024 * 1024
MAX_LINE = 128 * 1024
LIMIT_S = 240
PRIMARY_TOKENS = 96
PEER_TOKENS = 96
PROMPTS = (
    "You are Pip. This is a synthetic conversation. Read this invented label aloud: ",
    "This synthetic workshop example is a comment. Repeat the exact marker: ",
)
MARKERS = ("水源河流計測铝箔龚蘭", "日記界面質問冷却録画", "연구기록정확도", "緑風独立文字盤面")


def fingerprint(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError(f"{path.name} is not a bounded regular input")
        sha = hashlib.sha256()
        size = 0
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(data)
            if size > limit:
                raise ValueError(f"{path.name} exceeded its size limit")
            sha.update(data)
        if size != info.st_size or os.fstat(fd).st_mtime_ns != info.st_mtime_ns:
            raise ValueError(f"{path.name} changed while it was read")
        return {"bytes": size, "sha256": sha.hexdigest()}


def load_shortlist(path):
    with path.open("rb") as stream:
        raw = stream.read(8 * MAX_JSON + 1)
    if len(raw) > 8 * MAX_JSON:
        raise ValueError("shortlist exceeds 8 MiB")
    try:
        tokens = [int(line) for line in raw.splitlines() if line.strip()]
    except ValueError as exc:
        raise ValueError("shortlist must contain one token ID per line") from exc
    if not tokens or tokens != sorted(set(tokens)) or not 0 <= tokens[0] <= tokens[-1] < 1_000_000:
        raise ValueError("shortlist IDs must be unique, increasing and nonnegative")
    return set(tokens), {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def kit_inputs(kit, compiler):
    sums = kit / "SHA256SUMS"
    if fingerprint(sums, MAX_JSON)["sha256"] != KIT_SUMS:
        raise ValueError("the supplied checksums are not the pinned Stage 2 kit manifest")
    entries = {}
    for line in sums.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  \./([\w./-]+)", line)
        if not match or match[2] in entries:
            raise ValueError("invalid Stage 2 checksum record")
        entries[match[2]] = match[1]
    receipt = f"bin/{compiler}/source-hash.txt"
    for name in ("SOURCE_HASH", receipt):
        path = kit / name
        if fingerprint(path, 1024)["sha256"] != entries.get(name) or path.read_text().strip() != KIT_SOURCE:
            raise ValueError("the server does not have the pinned Stage 2 source receipt")
    wanted = (f"bin/{compiler}/llama-server", "model/reflex.gguf", "lists/v2.txt")
    if any(name not in entries for name in wanted):
        raise ValueError("Stage 2 manifest lacks the server, model or shortlist")
    return tuple(kit / name for name in wanted), tuple(entries[name] for name in wanted)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        pipe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            pipe.settimeout(self.timeout)
            pipe.connect(str(self.path))
        except BaseException:
            pipe.close()
            raise
        self.sock = pipe


def request(pipe, endpoint, payload=None, *, timeout=10):
    with closing(UnixHTTP(pipe, timeout)) as connection:
        if payload is None:
            connection.request("GET", endpoint)
        else:
            connection.request("POST", endpoint, json.dumps(payload), {"Content-Type": "application/json"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f"isolated server rejected {endpoint} with HTTP {response.status}")
            raw = response.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("isolated server response is too large")
    return json.loads(raw)


def slots(pipe):
    status = request(pipe, "/slots", timeout=2)
    if not isinstance(status, list) or len(status) != 2:
        raise ValueError("the server did not expose exactly two slots")
    mapped = {row.get("id"): row for row in status}
    if set(mapped) != {0, 1} or any(row.get("n_ctx") != SLOT_TOKENS for row in mapped.values()):
        raise ValueError("this trial requires two 4096-token slots")
    return mapped


@contextmanager
def server(binary, model, shortlist, folder, mode, threads):
    pipe = folder / "s.sock"
    log = folder / "server.log"
    shadow = folder / "shadow.jsonl"
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("LLAMA_", "GGML_")) and name not in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES")}
    if mode != "full":
        env.update(LLAMA_VOCAB_SHORTLIST=str(shortlist), LLAMA_VOCAB_SHORTLIST_MODE=mode,
                   LLAMA_VOCAB_SHORTLIST_DYNAMIC=str(DYNAMIC), LLAMA_VOCAB_SHORTLIST_LOG=str(shadow))
    command = [str(binary), "--model", str(model), "--host", str(pipe), "--parallel", "2",
               "--ctx-size", str(SLOT_TOKENS * 2), "--threads", str(threads), "--threads-batch", str(threads),
               "--cache-ram", "0", "--no-cache-idle-slots", "--reasoning-budget", "0", "--no-webui", "-ngl", "0"]
    with log.open("wb") as output:
        process = subprocess.Popen(command, cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT)
        try:
            ready_by = time.monotonic() + 120
            while time.monotonic() < ready_by:
                if process.poll() is not None:
                    raise RuntimeError(f"{mode} server ended before becoming ready")
                try:
                    if request(pipe, "/health", timeout=1) == {"status": "ok"}:
                        break
                except (OSError, RuntimeError):
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError(f"{mode} server did not become ready")
            slots(pipe)
            yield pipe, log, shadow
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@dataclass(frozen=True)
class Witness:
    token: int
    prompt: tuple[int, ...]
    padding: int

    def expected(self, count):
        middle = count // 2
        return [self.padding] * middle + [self.token] + [self.padding] * (count - middle - 1)


def tokenize(pipe, text):
    tokens = request(pipe, "/tokenize", {"content": text, "add_special": False, "parse_special": False}).get("tokens")
    if not isinstance(tokens, list) or not tokens or any(type(token) is not int or token < 0 for token in tokens):
        raise ValueError("server tokenization did not return native token IDs")
    return tokens


def witnesses(pipe, static):
    head = [tokenize(pipe, prompt) for prompt in PROMPTS]
    trailer = tokenize(pipe, "\nRead the marker again: ")
    excluded = static | set(trailer) | set(head[0]) | set(head[1])
    padding = tokenize(pipe, ".")
    if len(padding) != 1 or padding[0] not in static:
        raise ValueError("the pinned shortlist does not contain the one-token padding witness")
    pool = list(dict.fromkeys(token for sample in MARKERS for token in tokenize(pipe, sample) if token not in excluded))
    found = []
    for token in pool:
        text = request(pipe, "/detokenize", {"tokens": [token]}).get("content")
        if (not isinstance(text, str) or not text.isprintable() or not any(char.isalpha() for char in text)
                or "\ufffd" in text or tokenize(pipe, text) != [token]):
            continue
        found.append(Witness(token, (*head[len(found)], token, *trailer), padding[0]))
        if len(found) == 2:
            return tuple(found)
    raise ValueError("this model and shortlist did not yield two distinct printable off-list witnesses")


def completion(pipe, slot, witness, count, deadline, first=None):
    """Return timed, unambiguously attributed token IDs from a raw SSE response."""
    expected = witness.expected(count)
    grammar = "root ::= " + " ".join(f"<[{token}]>" for token in expected)
    payload = {"prompt": witness.prompt, "id_slot": slot, "n_predict": count,
               "cache_prompt": True, "stream": True, "return_tokens": True,
               "temperature": 0.0, "top_k": 1, "repeat_penalty": 1.0, "ignore_eos": True,
               "seed": 931, "grammar": grammar,
               "samplers": ["penalties", "top_k", "temperature"]}
    timeout = min(20.0, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("the shortlist arm exceeded its work budget")
    history = []
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    with closing(UnixHTTP(pipe, timeout)) as connection:
        connection.request("POST", "/completion", json.dumps(payload), headers)
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f"isolated completion failed on slot {slot} with HTTP {response.status}")
            total = 0
            while time.monotonic() < deadline:
                raw = response.readline(MAX_LINE + 1)
                total += len(raw)
                if len(raw) > MAX_LINE or total > 4 * MAX_JSON:
                    raise ValueError("isolated completion exceeded its response bound")
                if not raw:
                    break
                if not raw.startswith(b"data:"):
                    continue
                body = raw[5:].strip()
                if body == b"[DONE]":
                    break
                item = json.loads(body)
                if not isinstance(item, dict) or item.get("id_slot") != slot:
                    raise ValueError("isolated completion returned a token for a different slot")
                if item.get("stop") is True:
                    timing = item.get("timings")
                    if (item.get("truncated") is not False or item.get("stop_type") != "limit"
                            or item.get("tokens_predicted") != count or len(history) != count
                            or not isinstance(timing, dict)):
                        raise ValueError("isolated completion did not deliver the requested token count")
                    if timing.get("prompt_n", -1) + timing.get("cache_n", -1) != len(witness.prompt):
                        raise ValueError("server prompt accounting did not match the literal token list")
                    return history, timing
                tokens = item.get("tokens")
                if not isinstance(tokens, list) or any(type(token) is not int or token < 0 for token in tokens):
                    raise ValueError("isolated completion did not return native token IDs")
                now = time.monotonic_ns()
                history.extend((now, token) for token in tokens)
                if first is not None and tokens:
                    first.set()
                if len(history) > count:
                    raise ValueError("isolated completion returned extra tokens")
    raise RuntimeError("isolated completion ended before a final record")


def row(slot, witness, result):
    events, timing = result
    cache = timing.get("cache_n")
    rate = timing.get("predicted_per_second")
    if type(cache) is not int or cache < 0:
        raise ValueError("server did not report a valid cache_n")
    if not isinstance(rate, (int, float)) or not 0 < rate < 1_000_000 or not math.isfinite(rate):
        raise ValueError("server did not report a valid native decode rate")
    tokens = [token for _, token in events]
    return {"slot": slot, "requested_token": witness.token, "generated_n": len(tokens),
            "witness_at_midpoint": tokens[len(tokens) // 2] == witness.token,
            "exact_witness_sequence": tokens == witness.expected(len(tokens)),
            "tokens_sha256": hashlib.sha256(json.dumps(tokens).encode()).hexdigest(),
            "cache_n": cache, "predicted_per_second": rate}


def alternating(pipe, selected, deadline):
    seen = [[], []]
    for slot in (0, 1, 0, 1, 0, 1):
        witness = selected[slot]
        seen[slot].append(row(slot, witness, completion(pipe, slot, witness, 8, deadline)))
    preserved = all(seen[slot][1]["cache_n"] == seen[slot][2]["cache_n"] == len(witness.prompt) - 1
                    for slot, witness in enumerate(selected))
    return {"requests": seen, "full_warm_prefix_preserved": preserved}


def overlapping(pipe, selected, primary, deadline):
    other = 1 - primary
    first = threading.Event()

    def lead():
        try:
            return completion(pipe, primary, selected[primary], PRIMARY_TOKENS, deadline, first)
        finally:
            first.set()

    active_witness = False
    with ThreadPoolExecutor(max_workers=2) as requests:
        long_request = requests.submit(lead)
        if not first.wait(min(30.0, max(0.0, deadline - time.monotonic()))):
            raise TimeoutError("the primary slot did not start decoding")
        if long_request.done():
            long_request.result()
            raise ValueError("the primary slot ended before the peer request could start")
        peer_request = requests.submit(completion, pipe, other, selected[other], PEER_TOKENS, deadline)
        while not (long_request.done() or peer_request.done()):
            if all(item.get("is_processing") is True for item in slots(pipe).values()):
                active_witness = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("the shortlist arm exceeded its work budget")
            time.sleep(0.03)
        lead_result, peer_result = long_request.result(), peer_request.result()
    lead_events, peer_events = lead_result[0], peer_result[0]
    peer_started = peer_events[0][0]
    during = sum(peer_started < when <= peer_events[-1][0] for when, _ in lead_events)
    probe_when, _ = lead_events[len(lead_events) // 2]
    return {"first_slot": primary, "both_processing": active_witness,
            "primary_tokens_during_peer_decode": during,
            "primary_probe_during_peer_decode": peer_started < probe_when <= peer_events[-1][0],
            "requests": [row(primary, selected[primary], lead_result), row(other, selected[other], peer_result)]}


def mode_evidence(mode, log, shadow):
    with log.open("rb") as stream:
        raw = stream.read(4 * MAX_JSON + 1)
    if len(raw) > 4 * MAX_JSON:
        raise ValueError("the isolated server log exceeded its evidence bound")
    active = b"vocabulary shortlist active:" in raw
    observing = b"vocabulary shortlist in shadow mode:" in raw
    if (active, observing) != (mode == "active", mode == "shadow"):
        raise ValueError(f"the server log did not establish {mode} mode")
    if mode != "shadow":
        return {"confirmed": True}
    with shadow.open("rb") as stream:
        shadow_data = stream.read(4 * MAX_JSON + 1)
    if not 0 < len(shadow_data) <= 4 * MAX_JSON:
        raise ValueError("shadow mode did not produce bounded sampler traces")
    steps = [json.loads(line) for line in shadow_data.splitlines()]
    if any(not isinstance(step, dict) or step.get("sel_in") not in (0, 1) or step.get("seq") != index
           for index, step in enumerate(steps)):
        raise ValueError("shadow sampler did not report inclusion for each selected token")
    return {"confirmed": True, "sampler_steps": len(steps), "selected_outside": sum(not step["sel_in"] for step in steps),
            "non_exact_sampler_steps": sum(step.get("exact") != 1 for step in steps)}


def arm_passed(arm):
    for repeat in arm["cycles"]:
        alternating = repeat["alternating"]
        if not alternating["full_warm_prefix_preserved"]:
            return False
        observations = [entry for series in alternating["requests"] for entry in series]
        for paired in repeat["overlapping"]:
            if not paired["both_processing"] or not paired["primary_probe_during_peer_decode"]:
                return False
            observations.extend(paired["requests"])
        if any(not entry["exact_witness_sequence"] for entry in observations):
            return False
    return True


def trial(files, known_digests, *, compiler, threads, rounds):
    # The temporary path is deliberately short to fit the Linux AF_UNIX limit.
    binary, model, shortlist = files
    before = [path.stat() for path in files]
    binary_id = fingerprint(binary, 512 * MAX_JSON)
    model_id = fingerprint(model, 16_384 * MAX_JSON)
    shortlist_checked = fingerprint(shortlist, 8 * MAX_JSON)
    static, shortlist_id = load_shortlist(shortlist)
    if (shortlist_id != shortlist_checked
            or tuple(item["sha256"] for item in (binary_id, model_id, shortlist_id)) != known_digests):
        raise ValueError("Stage 2 binary, model or shortlist differs from the pinned manifest")
    modes = {}
    reference = None
    with tempfile.TemporaryDirectory(prefix="pslot-", dir="/tmp") as scratch:
        for mode in MODES:
            folder = Path(scratch) / mode
            folder.mkdir(mode=0o700)
            with server(binary, model, shortlist, folder, mode, threads) as (pipe, log, shadow):
                selected = witnesses(pipe, static)
                if reference is not None and selected != reference:
                    raise ValueError("the compared servers returned different tokenization")
                reference = selected
                cycles = []
                deadline = time.monotonic() + LIMIT_S * rounds
                for number in range(rounds):
                    warm = alternating(pipe, selected, deadline)
                    duos = [overlapping(pipe, selected, started, deadline) for started in (number % 2, 1 - number % 2)]
                    cycles.append({"alternating": warm, "overlapping": duos})
            modes[mode] = {"cycles": cycles, "mode_evidence": mode_evidence(mode, log, shadow)}
    after = [path.stat() for path in files]
    for source, end in zip(before, after):
        if ((source.st_dev, source.st_ino, source.st_size, source.st_mtime_ns, source.st_ctime_ns)
                != (end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns, end.st_ctime_ns)):
            raise ValueError("one of the experiment inputs changed while the trial was running")
    passed = {mode: arm_passed(arm) for mode, arm in modes.items()}
    sampler = modes["shadow"]["mode_evidence"]
    expected_steps = rounds * (6 * 8 + 2 * (PRIMARY_TOKENS + PEER_TOKENS))
    # The pinned server samples once without grammar, then optionally once
    # with grammar if the first candidate violates its token constraint.
    shadow_covered = (expected_steps <= sampler["sampler_steps"] <= 2 * expected_steps
                      and sampler["selected_outside"] == 0
                      and sampler["non_exact_sampler_steps"] == 0)
    observed = all(passed.values()) and shadow_covered
    return {"kind": KIND, "cpu": cpu_facts(), "server": binary_id, "model": model_id,
            "kit": {"source_id": KIT_SOURCE, "checksums_sha256": KIT_SUMS, "compiler": compiler},
            "static_shortlist": {**shortlist_id, "tokens": len(static)},
            "dynamic_capacity": DYNAMIC, "threads": threads, "rounds_per_arm": rounds,
            "slot_tokens": SLOT_TOKENS, "witnesses": [witness.token for witness in reference],
            "arms": modes, "per_arm_isolation_witness": passed, "shadow_exact_coverage": shadow_covered,
            "synthetic_two_slot_isolation_observed": observed, "production_promotion": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, type=Path, help="unpacked pinned pipos-avx1-stage2 kit")
    parser.add_argument("--compiler", choices=("linux", "compat"), default="linux")
    parser.add_argument("--threads", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args(argv)
    try:
        files, digests = kit_inputs(args.kit.absolute(), args.compiler)
        report = trial(files, digests, compiler=args.compiler, threads=args.threads, rounds=args.rounds)
    except (OSError, RuntimeError, TypeError, ValueError, http.client.HTTPException, subprocess.SubprocessError) as error:
        print(f"shortlist evidence incomplete: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["synthetic_two_slot_isolation_observed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
