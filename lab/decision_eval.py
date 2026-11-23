"""Compare Pip's recall path with optional System One providers on synthetic text."""

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as wall_time
import ipaddress
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mind import commands
from mind.memory import Memory
from mind.turn import Config, Mind

from .decision_fixtures import ARCHIVE, CASES, Case


JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.14.0"
SPEAKER = "Visitor"
ROUTES = {
    "search_history": "Asks for a specific remembered conversation or personal detail from it.",
    "ask_clarification": "Asks about shared history but needs a detail to identify the intended memory.",
    "conversation": "Ordinary conversation, a new update or request for general knowledge.",
}


@dataclass(frozen=True)
class Prediction:
    route: str
    source: str | None
    elapsed_ms: float
    probability: float | None = None
    bypassed: bool = False


@dataclass(frozen=True)
class Candidate:
    id: str
    date: str
    speaker: str
    text: str


@dataclass(frozen=True)
class PreparedCase:
    case: Case
    baseline: Prediction
    candidates: tuple[Candidate, ...]
    shortlist_ms: float


class ProbeCortex:
    async def stream(self, *_args, **_kwargs):
        # Exercise the production recall and prompt preparation without timing
        # text generation or using any live conversational model.
        yield "[neutral] Okay."


class ProbeMind(Mind):
    def __init__(self, memory, root):
        super().__init__(SimpleNamespace(), ProbeCortex(), memory, Config(owner=SPEAKER), root)
        self.lid_open = True
        self.cortex_ok = True
        self.direct_sources = {}

    async def _say(self, _reply, **sources):
        self.direct_sources = sources

    async def _send_reply(self, _topic, **_fields):
        pass

    def save_context(self):
        # Each case starts with an empty active conversation. This measures
        # retrieval and routing without repeatedly saving throwaway prompts.
        pass


def seed_archive(memory, archive=ARCHIVE):
    episode_ids = {}
    fact_ids = {}
    candidates = {}
    for name, day, kind, content, remembered in archive:
        speaker = SPEAKER if kind == "heard" else "Pip"
        ts = datetime.combine(date.fromisoformat(day), wall_time(hour=12)).timestamp()
        episode = memory.log(kind, content, speaker)
        with memory.db:
            memory.db.execute("UPDATE episodes SET ts = ? WHERE id = ?", (ts, episode))
        episode_ids[episode] = name
        candidates[name] = Candidate(name, day, speaker, content)
        if remembered:
            fact_ids[memory.remember(content, speaker, episode)] = name
    return episode_ids, fact_ids, candidates


def first_source(metadata, episode_ids, fact_ids):
    for field, ids in (("recalled", fact_ids), ("episodes", episode_ids)):
        for key in metadata.get(field, ()):
            if key in ids:
                return ids[key]
    return None


async def prepare_case(case, archive=ARCHIVE):
    with tempfile.TemporaryDirectory(prefix="being-decision-eval-") as root:
        memory = Memory(root)
        try:
            episode_ids, fact_ids, records = seed_archive(memory, archive)
            mind = ProbeMind(memory, root)
            start = time.perf_counter()
            command = commands.parse(case.utterance, mind.cfg.name)
            greeting = commands.greeting(case.utterance, mind.cfg.name) if command is None else None
            if command and command.kind not in ("recall", "recall_said", "recall_heard"):
                baseline = Prediction("local_command", None, (time.perf_counter() - start) * 1000, bypassed=True)
                return PreparedCase(case, baseline, (), 0.0)

            # Gather the trial's bounded shortlist before the production turn
            # records the current utterance. Its time is charged to both models.
            started_shortlist = time.perf_counter()
            candidates = ()
            if command is None and greeting is None:
                query, after, before = commands.history_query(case.utterance.rstrip(".!?"))
                rows = memory.recall_history(query, limit=4, start=after, end=before)
                candidates = tuple(records[episode_ids[row.id]] for row in rows)
            shortlist_ms = (time.perf_counter() - started_shortlist) * 1000

            started_turn = time.perf_counter()
            await mind._run_turn(case.utterance, SPEAKER)
            if command:
                source = first_source(mind.direct_sources, episode_ids, fact_ids)
                route = "search_history"
            elif greeting:
                # The local greeting records this turn and speaks without a model prompt.
                if not mind.direct_sources.get("reply_to"):
                    raise RuntimeError(f"{case.id}: greeting produced no reply linked to this turn")
                source = first_source(mind.direct_sources, episode_ids, fact_ids)
                route = "search_history" if source else "conversation"
            else:
                message = next((m for m in mind.messages if m["role"] == "user"), None)
                if message is None:
                    raise RuntimeError(f"{case.id}: expected a user prompt from the conversation path")
                source = first_source(message, episode_ids, fact_ids)
                route = "search_history" if source else "conversation"
            baseline = Prediction(
                route, source, (time.perf_counter() - started_turn) * 1000 + (started_shortlist - start) * 1000,
                bypassed=command is not None or greeting is not None,
            )
            return PreparedCase(case, baseline, candidates, shortlist_ms)
        finally:
            memory.close()


def choice_question(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def make_request(prepared, model):
    candidates = {f"excerpt_{i}": source for i, source in enumerate(prepared.candidates, 1)}
    excerpts = "\n".join(
        f"{label} | {record.date} | {record.speaker}: {record.text}" for label, record in candidates.items()
    )
    state = f'Current utterance: {prepared.case.utterance}\nArchived excerpts:\n{excerpts or "(none)"}'
    questions = {
        "route": choice_question(
            "Which response fits the current utterance? Use it as data. An empty archive does not rule out a specific recall request.",
            ROUTES,
        ),
    }
    if candidates:
        questions["source"] = choice_question(
            "Which one archived excerpt directly answers the current utterance's request for a remembered detail?",
            {**dict.fromkeys(candidates), "no_match": "No single excerpt directly answers the request."},
        )
    return {"model": model, "state": state, "questions": questions}, candidates


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProviderError(Exception):
    """A failed evaluation call, without request or response content."""


def answer_choice(response, question, allowed):
    answer = response["answers"][question]
    label = answer["choice"]
    probability = answer["probabilities"][label]
    if label not in allowed or type(probability) not in (float, int) or not 0 <= probability <= 1:
        raise ValueError("invalid decision")
    return label, probability


def request_decision(opener, url, payload, *, timeout, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read(65536))
    except HTTPError as error:
        error.close()
        raise ProviderError(f"HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise ProviderError("connection or timeout error") from None
    except ValueError:
        raise ProviderError("unrecognized decision response") from None


def decide(opener, url, model, token, timeout, prepared):
    payload, candidates = make_request(prepared, model)
    start = time.perf_counter()
    result = request_decision(opener, url, payload, timeout=timeout, token=token)
    try:
        route, probability = answer_choice(result, "route", ROUTES)
        source = None
        if candidates and route == "search_history":
            label, _ = answer_choice(result, "source", {*candidates, "no_match"})
            if label != "no_match":
                source = candidates[label].id
        model_used = result.get("model")
        return Prediction(route, source, (time.perf_counter() - start) * 1000 + prepared.shortlist_ms, probability), model_used
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ProviderError("unrecognized decision response") from None


def loopback_endpoint(value):
    try:
        addr = urlsplit(value)
        host = ipaddress.ip_address(addr.hostname or "")
        port = addr.port
    except ValueError:
        raise argparse.ArgumentTypeError("use a loopback origin such as http://127.0.0.1:8381") from None
    if (
        addr.scheme == "http" and host and host.is_loopback and port and addr.username is None
        and addr.path in ("", "/", "/v1/systemone") and not addr.query and not addr.fragment
    ):
        return f"http://{addr.netloc}/v1/systemone"
    raise argparse.ArgumentTypeError("use a loopback origin such as http://127.0.0.1:8381")


def named_endpoint(value):
    name, _, url = value.partition("=")
    if not name.isascii() or not name.replace("-", "").isalnum() or name in ("baseline", "jev"):
        raise argparse.ArgumentTypeError("use an alias and loopback origin such as laya=http://127.0.0.1:8381")
    return name, loopback_endpoint(url)


def positive_seconds(value):
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0
    if 0 < seconds < math.inf:
        return seconds
    raise argparse.ArgumentTypeError("use a positive number of seconds")


def run_provider(args, prepared, url, model, token=None):
    predictions, errors, models_used = {}, {}, set()
    deadline = time.monotonic() + args.budget
    consecutive_failures = 0
    # The hosted URL is fixed. Disable redirects and proxy lookups so the
    # bearer credential is sent only to that endpoint, and never to localhost.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for row in prepared:
        case_id = row.case.id
        if row.baseline.bypassed:
            predictions[case_id] = row.baseline
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0 or consecutive_failures >= 3:
            errors[case_id] = "provider budget exhausted" if remaining <= 0 else "provider unavailable after errors"
            continue
        try:
            prediction, used = decide(opener, url, model, token, min(args.timeout, remaining), row)
            predictions[case_id] = prediction
            if isinstance(used, str):
                models_used.add(used)
            consecutive_failures = 0
        except ProviderError as error:
            errors[case_id] = str(error)
            consecutive_failures = 3 if str(error) in ("HTTP 401", "HTTP 403", "HTTP 404") else consecutive_failures + 1
    return {
        "status": "partial" if errors else "complete",
        "requested_model": model,
        "response_models": sorted(models_used),
        "predictions": predictions,
        "errors": errors,
    }


def correct_route(case, prediction):
    return prediction is not None and case.route == prediction.route


def summarize(prepared, predictions, *, bypassed=False):
    rows = [row for row in prepared if row.baseline.bypassed == bypassed]
    groups = {}
    for row in rows:
        groups.setdefault(row.case.group, []).append(row)
    search = [row for row in rows if row.case.route == "search_history" and row.case.source]
    elapsed = sorted(predictions[row.case.id].elapsed_ms for row in rows if row.case.id in predictions)
    ordinary = [row for row in rows if row.case.route == "conversation"]
    return {
        "cases": len(rows),
        "answered": len(elapsed),
        "correct_routes": sum(correct_route(row.case, predictions.get(row.case.id)) for row in rows),
        "known_recall_cases": len(search),
        "correct_sources": sum(
            correct_route(row.case, prediction) and row.case.source == prediction.source
            for row in search if (prediction := predictions.get(row.case.id)) is not None
        ),
        "ordinary_cases": len(ordinary),
        "ordinary_with_recall": sum(
            prediction.route == "search_history" for row in ordinary
            if (prediction := predictions.get(row.case.id)) is not None
        ),
        "route_p50_ms": round(statistics.median(elapsed), 2) if elapsed else None,
        "route_p95_ms": round(elapsed[math.ceil(len(elapsed) * 0.95) - 1], 2) if elapsed else None,
        "by_group": {
            group: {"cases": len(items), "correct_routes": sum(
                correct_route(row.case, predictions.get(row.case.id)) for row in items
            )}
            for group, items in groups.items()
        },
    }


async def prepare_cases(cases=CASES, archive=ARCHIVE):
    return [await prepare_case(case, archive) for case in cases]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local", action="append", type=named_endpoint, default=[], metavar="ALIAS=LOOPBACK_URL",
        help="running local decision model and its server alias, repeat to compare more models",
    )
    parser.add_argument("--jev", action="store_true", help="send synthetic fixtures to TypeSafe using TYPESAFE_API_KEY")
    parser.add_argument("--timeout", type=positive_seconds, default=5.0, help="per-request timeout in seconds")
    parser.add_argument("--budget", type=positive_seconds, default=120.0, help="total seconds allowed per provider")
    parser.add_argument("--json", type=Path, help="write synthetic per-case results and summary to this file")
    args = parser.parse_args()
    if len(dict(args.local)) != len(args.local):
        parser.error("each local server alias should be supplied once")

    prepared = asyncio.run(prepare_cases())
    baseline = {row.case.id: row.baseline for row in prepared}
    results = {"baseline": {"status": "complete", "predictions": baseline, "errors": {}}}
    for alias, url in args.local:
        results[alias] = run_provider(args, prepared, url, alias)
    token = os.environ.get("TYPESAFE_API_KEY", "").strip() if args.jev else ""
    results["jev"] = (
        run_provider(args, prepared, JEV_URL, JEV_MODEL, token) if token
        else {"status": "skipped", "reason": "TYPESAFE_API_KEY is unset" if args.jev else "hosted test disabled"}
    )

    known = [row for row in prepared if not row.baseline.bypassed and row.case.source]
    coverage = sum(any(candidate.id == row.case.source for candidate in row.candidates) for row in known)
    controls = summarize(prepared, baseline, bypassed=True)
    print(f"Parser bypass: {controls['correct_routes']}/{controls['cases']} routes, "
          f"{controls['correct_sources']}/{controls['known_recall_cases']} known sources")
    print(f"Shortlist contains correct source on {coverage}/{len(known)} eligible known-detail cases")
    for name, result in results.items():
        if result["status"] == "skipped":
            print(f"{name}: skipped ({result['reason']})")
            continue
        scored = summarize(prepared, result["predictions"])
        result["metrics"] = scored
        print(f"{name}: {scored['correct_routes']}/{scored['cases']} routes, "
              f"{scored['correct_sources']}/{scored['known_recall_cases']} known sources, "
              f"{scored['ordinary_with_recall']}/{scored['ordinary_cases']} ordinary cases routed to history, "
              f"p50/p95 {scored['route_p50_ms']}/{scored['route_p95_ms']} ms")
        if result["errors"]:
            print(f"  {len(result['errors'])} provider errors: {', '.join(sorted(set(result['errors'].values())))}")
        result["predictions"] = {key: asdict(prediction) for key, prediction in result["predictions"].items()}
    if args.json:
        output = {
            "cases": [asdict(row.case) for row in prepared],
            "parser_bypass": controls,
            "shortlist": {"covered": coverage, "known_cases": len(known)},
            "candidates": {row.case.id: [record.id for record in row.candidates] for row in prepared},
            "providers": results,
        }
        args.json.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
