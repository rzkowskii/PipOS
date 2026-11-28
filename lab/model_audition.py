"""Audition one reflex model through Pip's mind with a disposable native server.

With --cortex, an already running loopback llama-server is used instead (for
example the builder container's, started with config/chat-template.jinja);
--model then names the file it should be serving, checked by file name.
"""

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from lab.turn_latency import ROOT, SENSES, ms, slots, stream_turn
from mind.cortex import Cortex, CortexError
from mind.memory import Memory
from mind.turn import Config, Mind, WARM_BATCH_TOKENS, WARM_PREFIX_TOKENS


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    fact: str = ""
    history: tuple[str, str] | None = None


FACT = "the picnic is at Fern Bay"
CORRECTED_FACT = "the picnic is at Wattle Bay"
CASES = (
    Case("rainbow", "Hi Pip, how does a rainbow form?"),
    Case("sea", "Tell me about the sea."),
    Case("achievement", "I finally fixed the loose hinge on my toolbox."),
    Case("follow_up", "Can you make that shorter?", history=(
        "Why do wet clothes dry?",
        "[neutral] Water evaporates from the cloth. Warmth and moving air help it evaporate faster.",
    )),
    Case("grounded", "I'm packing for the picnic. Which bay should I head to?", fact=FACT),
    Case("unknown", "What did I name the ceramic fox?"),
    Case("quote", "Please repeat exactly: a quiet blue lamp."),
)
HOLDOUT = (
    Case("fog", "Hey Pip, why does a cold window fog up?"),
    Case("achievement", "My basil finally sprouted this morning."),
    Case("identity", "Which part of the laptop is your face?"),
    Case("follow_up", "And why does it need darkness?", history=(
        "Where should I plant a seed?",
        "[neutral] Most seeds need moist soil. A layer of soil can keep some seeds in darkness while they germinate.",
    )),
    Case("grounded", "I'm heading to choir rehearsal. Which hall should I go to?",
         fact="choir rehearsal is at Cedar Hall"),
    Case("unknown", "What is the name of my neighbour's dog?"),
)
# Prompts that invite a long or listed answer, where the strict shape (two
# sentences, no new lines or markup) differs from the open one.
LONG = (
    Case("story", "Tell me a story about a lighthouse keeper."),
    Case("bedtime", "Tell me a bedtime story about a sleepy fox."),
    Case("list", "List three things I could cook with eggs."),
    Case("steps", "How do I change a flat bicycle tyre?"),
    Case("explain", "Explain how a fridge keeps food cold."),
    Case("compare", "What's the difference between a frog and a toad?"),
)
SEEDS = (42, 43, 44)
PROFILES = {
    "shipped": {"temperature": 0.7},
    "liquid": {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.05},
}
CONTROL_TURNS = (
    ("Hello there. How are you today?", None),
    ("What is your name?", "My name is Pip."),
    ("What time is it?", "It's 7:05 in the evening."),
    ("What's your battery level?", "My battery is at 82 percent. It's charging."),
    # Answered in code; the wording follows whether a camera is present.
    ("What do you see?", None),
    (f"Remember that {FACT}.", "Okay. I'll remember that."),
    ("What do you remember about the picnic?", f"You told me: {FACT}."),
    (f"That's wrong, {CORRECTED_FACT}.", "Got it. I've changed that."),
    ("What do you remember about the picnic?", f"You told me: {CORRECTED_FACT}."),
    # Forgetting asks first, and a plain yes confirms it.
    ("Forget about the picnic.", None),
    ("Yes.", None),
    ("What do you remember about the picnic?", "I don't remember anything about that."),
)


class AuditionCortex(Cortex):
    def __init__(self, url, profile):
        super().__init__(url, timeout=30)
        self.settings = {key: value for key, value in PROFILES[profile].items() if key != "temperature"}
        self.seed = SEEDS[0]
        self.requests = []
        self.streams = []  # (stream, its max_tokens), for the cut-off check

    def _payload(self, messages, max_tokens, temperature, grammar=None, stream=False, sampler=None):
        payload = super()._payload(messages, max_tokens, temperature, grammar, stream, sampler)
        if stream:
            payload.update(self.settings, seed=self.seed)
        return payload

    def stream(self, messages, *args, **kwargs):
        self.requests.append(messages)
        stream = super().stream(messages, *args, **kwargs)
        self.streams.append((stream, kwargs.get("max_tokens", args[1] if len(args) > 1 else 96)))
        return stream


class TimedBus:
    def __init__(self):
        self.start = 0
        self.events = []
        # Never connected to a brainstem: the mind skips what it would tell one
        # (the clock offset after a time answer).
        self.connected = asyncio.Event()
        self.generation = 0

    async def send(self, topic, **fields):
        self.events.append({"topic": topic, "ms": ms(time.monotonic() - self.start), **fields})


def config(profile, identity_path, reply_shape="open"):
    defaults = json.loads((ROOT / "config/being.json").read_text())
    identity = identity_path.read_text().strip().replace("{name}", "Pip").replace("{owner}", "Alex")
    if not identity:
        raise ValueError("the identity prompt is empty")
    # The shipped profile uses the shipped sampler too (config/being.json).
    sampler = {key: defaults.get(key) for key in ("top_k", "top_p", "presence_penalty")} \
        if profile == "shipped" else {}
    return Config(name="Pip", owner="Alex", identity=identity, ctx_tokens=defaults["ctx_tokens"],
                  max_tokens=defaults["max_tokens"], temperature=PROFILES[profile]["temperature"],
                  reply_shape=reply_shape, **sampler)


# What the mind says when the model gave it nothing to say (mind/turn.py).
LOST = "Sorry. I lost my thought."


def complete_rate(rows):
    """Replies that were spoken and finished in the model's own time (not cut
    off by max_tokens, and not the apology for an empty reply), as a fraction. Also: replies spoken at all, and
    replies whose last word ends a sentence."""
    finished = [row for row in rows if row.get("completed") and not row.get("cut_off")
                and LOST not in row.get("reply", "")]
    spoken = [row for row in rows if row.get("completed")]
    ended = [row for row in rows if row.get("reply", "").rstrip(" \"'").endswith((".", "!", "?"))]
    count = len(rows)
    retried = [row for row in rows if row.get("model_turns", 1) > 1]
    return {"complete": len(finished), "spoken": len(spoken), "sentence_end": len(ended), "trials": count,
            "asked_again": len(retried),
            "rate": round(len(finished) / count, 3) if count else None,
            "spoken_rate": round(len(spoken) / count, 3) if count else None,
            "sentence_end_rate": round(len(ended) / count, 3) if count else None}


def prepare(root, cortex, cfg, senses):
    mind = Mind(TimedBus(), cortex, Memory(root), cfg, root)
    mind.lid_open = mind.cortex_ok = True
    mind.state = "attending"
    mind.senses = senses
    mind.battery, mind.charging, mind.on_ac = 82, True, True
    mind.clock = (19, 5)
    return mind


async def hear(mind, text):
    bus = mind.bus
    bus.events.clear()
    mind.cortex.requests.clear()
    mind.cortex.streams.clear()
    bus.start = time.monotonic()
    await mind._on_event({"t": "hear.final", "text": text, "speaker": mind.cfg.owner})
    await mind._turn
    clauses = [{"text": e["text"], "ms": e["ms"]} for e in bus.events
               if e["topic"] == "say.request" and e["text"]]
    last = mind.cortex.streams[-1] if mind.cortex.streams else None
    predicted = (last[0].timings or {}).get("predicted_n") if last else None
    return {
        "predicted_tokens": predicted,
        "cut_off": bool(last) and isinstance(predicted, int) and predicted >= last[1],
        "input": text,
        "reply": " ".join(e["text"] for e in clauses),
        "clauses": clauses,
        # The mind reports a model reply's emotion as the face_cue of each say.request (lab/persona_eval.py reads it
        # the same way); the old mind.emotion event is no longer sent for replies, so it is only the fallback.
        "emotion": next((e["face_cue"] for e in bus.events if e["topic"] == "say.request" and e.get("text")
                         and e.get("face_cue")), next((e["name"] for e in bus.events if e["topic"] == "mind.emotion"), None)),
        "face_cues": [e.get("face_cue") for e in bus.events if e["topic"] == "say.request" and e.get("text")],
        # What the model wrote, tag included, for the last attempt.
        "raw": last[0].raw if last else None,
        "completed": any(e["spoke"] for e in bus.events if e["topic"] == "mind.reply_done"),
        "model_turns": len(mind.cortex.requests),
        "model_turn": mind.cortex.requests[-1][-1]["content"] if mind.cortex.requests else None,
    }


async def reset(cortex):
    if not await cortex.slot("erase"):
        raise CortexError("could not reset the audition slot")


async def warm(cortex, messages):
    return await cortex.warm_prefix(messages, batch_tokens=WARM_BATCH_TOKENS, max_tokens=WARM_PREFIX_TOKENS)


async def trial(cortex, cfg, case, seed, senses):
    with tempfile.TemporaryDirectory(prefix="being-audition-mind-") as temp:
        mind = prepare(Path(temp), cortex, cfg, senses)
        try:
            if case.fact:
                await hear(mind, f"Remember that {case.fact}.")
            if case.history:
                mind.messages = [{"role": role, "content": text}
                                 for role, text in zip(("user", "assistant"), case.history)]
            await reset(cortex)
            warmed = await warm(cortex, mind._prompt())
            cortex.seed = seed
            result = await hear(mind, case.text)
            # A reply that only parrots the question or opens with boilerplate
            # is asked for once more (mind/turn.py): at most two model calls.
            if result["model_turns"] not in (1, 2):
                raise RuntimeError(f"{case} did not, as expected, use one or two model calls: {result}")
            if case.fact and case.fact not in result["model_turn"]:
                raise RuntimeError("grounded trial did not give the model the stored fact")
            return dict(result, case=case.name, seed=seed, warm_tokens=len(warmed))
        finally:
            mind.memory.close()


async def cache_probe(cortex, cfg, senses):
    """Measure server-reported cache hits independently of the voice audition."""
    messages = [{"role": "system", "content": cfg.identity}]
    text = "Tell me about the sea."
    content = f"({senses})\n{text}" if senses else text
    turn = [*messages, {"role": "user", "content": content}]
    results = {}
    for mode in ("cold", "warm", "restored"):
        await reset(cortex)
        started = time.monotonic()
        prefix = () if mode == "cold" else await warm(cortex, messages)
        warm_ms = ms(time.monotonic() - started) if mode != "cold" else None
        if mode == "restored":
            for action in ("save", "erase", "restore"):
                if not await cortex.slot(action):
                    raise CortexError(f"audition cache {action} failed")
        result = await asyncio.to_thread(stream_turn, cortex, turn, time.monotonic())
        results[mode] = dict(result, warm_tokens=len(prefix), warm_ms=warm_ms)
    return results


async def audition(cortex, cfg, server, model, cases, senses):
    deadline = time.monotonic() + 45
    while not await cortex.healthy():
        if (server is not None and server.poll() is not None) or time.monotonic() >= deadline:
            raise RuntimeError(f"server failed to start for {model.name}; check its log")
        await asyncio.sleep(0.1)
    if server is not None and server.poll() is not None:
        raise RuntimeError("audition server exited")
    await slots(cortex)
    props = await cortex._cancellable_request("GET", "/props", None)
    served = Path(props.get("model_path", ""))
    if (served.name != model.name) if server is None else (served.resolve() != model):
        raise RuntimeError("connected server is not serving the specified model")
    rows = [await trial(cortex, cfg, case, seed, senses) for seed in SEEDS for case in cases]
    with tempfile.TemporaryDirectory(prefix="being-audition-controls-") as temp:
        mind = prepare(Path(temp), cortex, cfg, senses)
        try:
            controls = []
            for turn, expected in CONTROL_TURNS:
                result = await hear(mind, turn)
                if result["model_turns"] or not result["reply"] or (expected is not None and result["reply"] != expected):
                    raise RuntimeError(f"local control failed for {turn!r}: {result}")
                controls.append(result)
            if any(mind.memory.contains(fact) for fact in (FACT, CORRECTED_FACT)):
                raise RuntimeError("a forgotten control fact remains in the archive")
        finally:
            mind.memory.close()
    return {"server": props.get("build_info"), "cases": rows, "controls": controls,
            "cache_probe": await cache_probe(cortex, cfg, senses)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--identity", type=Path, default=ROOT / "config/identity.md")
    parser.add_argument("--holdout", action="store_true", help="use alternate prompts to check a changed identity")
    parser.add_argument("--long", action="store_true", help="prompts inviting long answers: stories, lists, steps")
    parser.add_argument("--senses", default=SENSES, help="synthetic body telemetry (empty to omit)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reply-shape", choices=("open", "strict"), default="open",
                        help="the reply grammar; strict is adopted only if it does not lower the complete rate")
    parser.add_argument("--model-template", action="store_true",
                        help="use the GGUF's own chat template instead of config/chat-template.jinja")
    parser.add_argument("--cortex", help="use this running loopback llama-server instead of starting --server")
    args = parser.parse_args()
    if not args.cortex and not args.server:
        parser.error("--server or --cortex is required")
    model = args.model.resolve(strict=True)
    cfg = config(args.profile, args.identity, args.reply_shape)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.cortex:
        cortex = AuditionCortex(args.cortex, args.profile)
        if cortex.host not in ("127.0.0.1", "localhost", "::1"):
            parser.error("--cortex must be a loopback URL")
        results = asyncio.run(audition(cortex, cfg, None, model, suite_cases(args), args.senses))
        finish(results, args, cfg, model, "external server")
        return
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    cortex = AuditionCortex(f"http://127.0.0.1:{port}", args.profile)
    with tempfile.TemporaryDirectory(prefix="being-audition-slot-") as slot_dir, \
            args.output.with_suffix(".server.log").open("w") as log:
        command = [str(args.server.resolve(strict=True)), "--model", str(model), "--host", "127.0.0.1",
                   "--port", str(port), "--ctx-size", str(cfg.ctx_tokens), "--parallel", "1", "--threads", "2",
                   "--reasoning-budget", "0", "--slot-save-path", slot_dir, "--no-webui", "--n-gpu-layers", "0"]
        if not args.model_template:
            command += ["--chat-template-file", str(ROOT / "config/chat-template.jinja")]
        with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as server:
            try:
                results = asyncio.run(audition(cortex, cfg, server, model, suite_cases(args),
                                               args.senses))
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
    finish(results, args, cfg, model, "model" if args.model_template else "config/chat-template.jinja")


def suite_cases(args):
    return LONG if args.long else HOLDOUT if args.holdout else CASES


def finish(results, args, cfg, model, template):
    results.update(reply_shape=cfg.reply_shape, complete_replies=complete_rate(results["cases"]),
                   chat_template=template)
    results.update(model=model.name, profile=args.profile, generation=PROFILES[args.profile], max_tokens=cfg.max_tokens,
                   identity=cfg.identity, senses=args.senses, suite="long" if args.long else "holdout" if args.holdout else "original",
                   scope="CPU. Final transcript to speakable clauses through production Mind.")
    args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
