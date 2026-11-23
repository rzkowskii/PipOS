"""Measure speculative prompt work against a dedicated single-slot llama-server.

The input is four synthetic transcript timelines. The reported clause timing
stops before speech synthesis. Use a dedicated server: this benchmark clears
slot 0 between trials and can write a synthetic slot file for the restore check.
"""

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
import ipaddress
import json
import math
from pathlib import Path
import re
import statistics
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse

from mind.cortex import Cortex, CortexError
from mind.speech import GRAMMAR, ClauseSplitter, EchoGuard, TagParser, ThinkFilter
from mind.turn import (
    READ_AHEAD_SECONDS, READ_AHEAD_TOKENS, WARM_BATCH_TOKENS, WARM_PREFIX_TOKENS, Config, Mind,
)


ROOT = Path(__file__).resolve().parent.parent
SLOT_FILE = "being-lab-latency.slot"
SENSES = "19:05, battery 82% charging, cool, room bright"
SLOT_POLL_SECONDS = 0.025
REPLY_TOKENS = 48
REPLY_SEED = 42


@dataclass(frozen=True)
class Transcript:
    name: str
    partials: tuple[tuple[float, str], ...]
    final_at: float
    final: str


CASES = (
    Transcript("short", ((0, "hello there how"),), 0.19, "hello there how are you today"),
    Transcript(
        "long",
        (
            (0, "i saw a red"),
            (0.35, "i saw a red kite over the hill"),
            (0.70, "i saw a red kite over the hill and it made me wonder"),
        ),
        1.05,
        "i saw a red kite over the hill and it made me wonder how you would describe the view",
    ),
    Transcript(
        "hesitant",
        ((0, "there is a"), (0.75, "there is a fine mist")),
        1.10,
        "there is a fine mist outside do you like that kind of weather",
    ),
    Transcript(
        "revised",
        ((0, "the noisy cars went"), (0.28, "the noisy cars went past"), (0.56, "the noisy crows went past")),
        0.78,
        "the noisy crows went past my window do you think they are curious",
    ),
)


def prompt(system, user=None):
    messages = [{"role": "system", "content": system}]
    if user is not None:
        messages.append({"role": "user", "content": f"({SENSES})\n{user}"})
    return messages


def ms(seconds):
    return round(1000 * seconds, 1)


def comparison_words(text):
    return re.findall(r"[a-z0-9]+", text.casefold())


class PrefillTrace:
    def __init__(self, cortex):
        self.cortex = cortex
        self.requests = []

    async def prefill(self, messages, **limit):
        start = time.monotonic()
        utterance = messages[-1]["content"].splitlines()[-1]
        sample = {"settled_words": len(utterance.split()), "status": "cancelled"}
        self.requests.append(sample)
        try:
            filled = await self.cortex.prefill(messages, **limit)
            sample["status"] = "completed" if filled else "skipped"
            return filled
        except CortexError:
            sample["status"] = "error"
            raise
        finally:
            sample["elapsed_ms"] = ms(time.monotonic() - start)


class LegacyAhead:
    """Replay the old single-task policy with one replaceable pending partial."""

    def __init__(self, trace, system):
        self.trace = trace
        self.system = system
        self.pending = None
        self.task = None
        self.completed = None

    def partial(self, text):
        settled = " ".join(text.split()[:-1])
        if len(settled.split()) < 2 or settled == self.completed:
            return
        self.pending = settled
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def run(self):
        while self.pending is not None:
            text, self.pending = self.pending, None
            await self.trace.prefill(prompt(self.system, text))
            self.completed = text


def production_ahead(trace, system, prefix):
    # No live memory or bus. Exercise the production partial scheduler
    # against a fresh synthetic conversation with the same verified prefix.
    memory = SimpleNamespace(consolidation_sources=lambda *_: ())
    mind = Mind(SimpleNamespace(), trace, memory, Config(identity=system, prefill=True), ROOT)
    mind.lid_open = mind.cortex_ok = True
    mind.state = "listening"
    mind.senses = SENSES
    mind._warmed_prefix = prefix
    return mind


async def finish_task(task, cancel=False):
    if task is not None:
        if cancel:
            task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, Exception):
            raise result


def stream_turn(cortex, messages, start):
    think, guard, tags, splitter = ThinkFilter(), EchoGuard(), TagParser(), ClauseSplitter()
    clauses = []
    body = cortex._payload(messages, REPLY_TOKENS, 0, GRAMMAR, stream=True)
    body.update(seed=REPLY_SEED, stream_options={"include_usage": True})
    first_content = first_clause = None
    timings = {}
    usage = {}

    def speak(text):
        nonlocal first_clause
        ready = splitter.feed(tags.feed(guard.feed(text)))
        if ready and not clauses:
            first_clause = ms(time.monotonic() - start)
        clauses.extend(ready)

    with closing(cortex._connection()) as conn:
        conn.request(
            "POST", cortex.path + "/v1/chat/completions", json.dumps(body).encode(),
            {"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        response = conn.getresponse()
        headers = ms(time.monotonic() - start)
        if response.status != 200:
            raise CortexError(f"chat returned HTTP {response.status}: {response.read(300)!r}")
        for raw in response:
            if not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip()
            if data == b"[DONE]":
                break
            packet = json.loads(data)
            if packet.get("timings"):
                timings = packet["timings"]
            if packet.get("usage"):
                usage = packet["usage"]
            choices = packet.get("choices") or []
            text = (choices[0].get("delta") or {}).get("content") if choices else None
            if text:
                if first_content is None:
                    first_content = ms(time.monotonic() - start)
                speak(think.feed(text))

    speak(think.flush())
    tail = splitter.feed(tags.feed(guard.flush()))
    if tail and not clauses:
        first_clause = ms(time.monotonic() - start)
    clauses.extend(tail)
    rest = (tags.flush() + " " + splitter.flush()).strip()
    if rest:
        if not clauses:
            first_clause = ms(time.monotonic() - start)
        clauses.append(rest)
    return {
        "headers_ms": headers,
        "first_content_ms": first_content,
        "first_clause_ms": first_clause,
        "done_ms": ms(time.monotonic() - start),
        "reply": " ".join(clauses),
        "server_timings": timings,
        "usage": usage,
    }


async def trial(cortex, system, case, mode, progress):
    if not await cortex.slot("erase", SLOT_FILE):
        raise CortexError("slot 0 could not be reset. Use a dedicated server with --slot-save-path.")
    warm_ms = None
    prefix = ()
    if mode in ("warm-prefix", "one-shot-prefix", "restored-prefix", "bounded-ahead"):
        start = time.monotonic()
        limits = {} if mode == "one-shot-prefix" else {
            "batch_tokens": WARM_BATCH_TOKENS,
            "max_tokens": WARM_PREFIX_TOKENS,
        }
        prefix = await cortex.warm_prefix(prompt(system), **limits)
        if not prefix:
            raise CortexError("warm prefix was not verified")
        warm_ms = ms(time.monotonic() - start)
        if mode == "restored-prefix":
            for action in ("save", "erase", "restore"):
                if not await cortex.slot(action, SLOT_FILE):
                    raise CortexError(f"slot {action} failed")
    trace = PrefillTrace(cortex)
    legacy = LegacyAhead(trace, system)
    bounded = production_ahead(trace, system, prefix) if mode == "bounded-ahead" else None
    task = None
    try:
        if mode in ("legacy-ahead", "cancel-ahead", "bounded-ahead"):
            start = time.monotonic()
            for offset, words in case.partials:
                await asyncio.sleep(max(0, start + offset - time.monotonic()))
                if bounded is not None:
                    bounded._on_partial(words)
                    task = bounded._prefill_task
                else:
                    legacy.partial(words)
                    task = legacy.task
            await asyncio.sleep(max(0, start + case.final_at - time.monotonic()))
            legacy.pending = None
        final_at = time.monotonic()
        if mode != "legacy-ahead":
            await finish_task(task, cancel=True)
        result = await asyncio.to_thread(stream_turn, cortex, prompt(system, case.final), final_at)
        await finish_task(task)
    finally:
        await finish_task(task, cancel=True)
    result.update(
        case=case.name,
        mode=mode,
        exact_echo=comparison_words(result["reply"]) == comparison_words(case.final),
        warm_ms=warm_ms,
        warm_prefix_tokens=len(prefix),
        speculative_requests=trace.requests,
    )
    print(f"{progress} {mode} {case.name}: {result['first_clause_ms']} ms to clause", flush=True, file=sys.stderr)
    return result


async def slots(cortex):
    result = await cortex._cancellable_request("GET", "/slots", None)
    if not isinstance(result, list) or len(result) != 1:
        raise CortexError("the benchmark requires exactly one slot and the /slots endpoint")
    return result[0]


async def cancellation_probe(cortex, system, short=False):
    """Watch for a busy cold prefill before disconnecting its real HTTP socket."""
    if not await cortex.slot("erase", SLOT_FILE):
        raise CortexError("slot erase failed")
    if short:
        prefix = await cortex.verified_prefix(prompt(system))
        if len(prefix) < WARM_BATCH_TOKENS:
            raise CortexError("could not derive a full warmup batch")
        background = asyncio.create_task(cortex._prime_cache(list(prefix[:WARM_BATCH_TOKENS])))
    else:
        long_prompt = " ".join(f"Workshop note {n} records a small brown paper lantern." for n in range(180))
        background = asyncio.create_task(cortex.prefill(prompt(system, long_prompt)))
    try:
        async with asyncio.timeout(4):
            while not (observed := await slots(cortex)).get("is_processing"):
                if background.done():
                    await background
                    return {"status": "finished_before_busy_sample"}
                await asyncio.sleep(SLOT_POLL_SECONDS)
        task_id = observed.get("id_task")
        cancel_at = time.monotonic()
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)
        async with asyncio.timeout(12):
            count = 0
            while True:
                observed = await slots(cortex)
                count += 1
                if not observed.get("is_processing") or observed.get("id_task") != task_id:
                    break
                await asyncio.sleep(SLOT_POLL_SECONDS)
        return {
            "status": "released",
            "observed_release_ms": ms(time.monotonic() - cancel_at),
            "polls_after_cancel": count,
        }
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


def summarize(rows):
    groups = {}
    cold_replies = {row["case"]: row["reply"] for row in rows if row["mode"] == "final-only"}
    for mode in dict.fromkeys(row["mode"] for row in rows):
        group = [row for row in rows if row["mode"] == mode]
        metrics = {}
        for key in ("headers_ms", "first_content_ms", "first_clause_ms"):
            values = sorted(row[key] for row in group if row[key] is not None)
            if values:
                metrics[key] = {"p50": round(statistics.median(values), 1), "p95": values[math.ceil(0.95 * len(values)) - 1]}
        cached = [row["server_timings"]["cache_n"] for row in group if "cache_n" in row["server_timings"]]
        matches = sum(row["reply"] == cold_replies[row["case"]] for row in group)
        groups[mode] = {
            "trials": len(group),
            "matching_cold_replies": matches,
            "exact_echoes": sum(row["exact_echo"] for row in group),
            "cached_tokens": {"min": min(cached), "max": max(cached)} if cached else None,
            **metrics,
        }
    return groups


async def run(args):
    cortex = Cortex(args.cortex, timeout=args.request_timeout)
    system = (ROOT / "config" / "identity.md").read_text().strip().replace("{name}", "Pip").replace("{owner}", "Alex")
    modes = ["final-only", "legacy-ahead", "cancel-ahead"]
    cases = CASES
    if args.prefix_only:
        modes = ["final-only", "warm-prefix", "one-shot-prefix", "restored-prefix"]
        cases = CASES[:1]
    elif args.warm:
        modes.extend(("warm-prefix", "bounded-ahead"))
    await slots(cortex)
    metadata = await cortex._cancellable_request("GET", "/props", None)
    rows = []
    for index in range(args.repeats):
        for case in cases:
            for mode in modes:
                rows.append(await trial(cortex, system, case, mode, f"{index + 1}/{args.repeats}"))
    if args.warm and not args.prefix_only:
        rows.append(await trial(cortex, system, CASES[0], "restored-prefix", "restore"))
    cancellations = [await cancellation_probe(cortex, system, short=args.prefix_only) for _ in range(args.repeats)]
    return {
        "scope": "Synthetic transcript to first speakable clause. Hardware, ASR and speaker playback require separate measurements.",
        "server": {key: metadata.get(key) for key in ("build_info", "model_path", "total_slots")},
        "reply": {"temperature": 0, "seed": REPLY_SEED, "max_tokens": REPLY_TOKENS},
        "read_ahead_budget": {"tokens": READ_AHEAD_TOKENS, "seconds": READ_AHEAD_SECONDS} if args.warm else None,
        "warmup_budget": {"tokens_per_request": WARM_BATCH_TOKENS, "max_tokens": WARM_PREFIX_TOKENS}
        if args.warm or args.prefix_only else None,
        "summary": summarize(rows),
        "trials": rows,
        "cancellation": {
            "prefill": "single warmup batch" if args.prefix_only else "long cold prompt",
            "poll_interval_ms": ms(SLOT_POLL_SECONDS),
            "samples": cancellations,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cortex", required=True, help="dedicated loopback llama-server URL")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--warm", action="store_true", help="measure verified cold warmup and synthetic slot restore")
    parser.add_argument("--prefix-only", action="store_true", help="compare one-shot, batched and restored warmup")
    parser.add_argument("--request-timeout", type=float, default=15, help="HTTP socket timeout in seconds")
    args = parser.parse_args()
    url = urlparse(args.cortex)
    if url.scheme != "http" or not url.hostname or (
        url.hostname != "localhost" and not is_loopback(url.hostname)
    ):
        parser.error("--cortex must be a loopback HTTP URL")
    if not 0 < args.request_timeout <= 600:
        parser.error("--request-timeout must be in (0, 600] seconds")
    results = asyncio.run(run(args))
    print(json.dumps(results, indent=2))


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    main()
