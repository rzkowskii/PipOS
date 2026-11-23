"""Audition one reflex model through Pip's mind with a disposable native server."""

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
    ("What do you see?", "I can't see yet. My eyes aren't connected."),
    (f"Remember that {FACT}.", "Okay. I'll remember that."),
    ("What do you remember about the picnic?", f"You told me: {FACT}."),
    (f"That's wrong, {CORRECTED_FACT}.", "Got it. I've changed that."),
    ("What do you remember about the picnic?", f"You told me: {CORRECTED_FACT}."),
    ("Forget about the picnic.", "Okay. That's forgotten."),
    ("What do you remember about the picnic?", "I don't remember anything about that."),
)


class AuditionCortex(Cortex):
    def __init__(self, url, profile):
        super().__init__(url, timeout=30)
        self.settings = {key: value for key, value in PROFILES[profile].items() if key != "temperature"}
        self.seed = SEEDS[0]
        self.requests = []

    def _payload(self, messages, max_tokens, temperature, grammar=None, stream=False):
        payload = super()._payload(messages, max_tokens, temperature, grammar, stream)
        if stream:
            payload.update(self.settings, seed=self.seed)
        return payload

    def stream(self, messages, *args, **kwargs):
        self.requests.append(messages)
        return super().stream(messages, *args, **kwargs)


class TimedBus:
    def __init__(self):
        self.start = 0
        self.events = []

    async def send(self, topic, **fields):
        self.events.append({"topic": topic, "ms": ms(time.monotonic() - self.start), **fields})


def config(profile, identity_path):
    defaults = json.loads((ROOT / "config/being.json").read_text())
    identity = identity_path.read_text().strip().replace("{name}", "Pip").replace("{owner}", "Alex")
    if not identity:
        raise ValueError("the identity prompt is empty")
    return Config(name="Pip", owner="Alex", identity=identity, ctx_tokens=defaults["ctx_tokens"],
                  max_tokens=defaults["max_tokens"], temperature=PROFILES[profile]["temperature"])


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
    bus.start = time.monotonic()
    await mind._on_event({"t": "hear.final", "text": text, "speaker": mind.cfg.owner})
    await mind._turn
    clauses = [{"text": e["text"], "ms": e["ms"]} for e in bus.events
               if e["topic"] == "say.request" and e["text"]]
    return {
        "input": text,
        "reply": " ".join(e["text"] for e in clauses),
        "clauses": clauses,
        "emotion": next((e["name"] for e in bus.events if e["topic"] == "mind.emotion"), None),
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
            if result["model_turns"] != 1:
                raise RuntimeError(f"{case} did not, as expected, use exactly one model call: {result}")
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
        if server.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError(f"server failed to start for {model.name}; check its log")
        await asyncio.sleep(0.1)
    if server.poll() is not None:
        raise RuntimeError("audition server exited")
    await slots(cortex)
    props = await cortex._cancellable_request("GET", "/props", None)
    if Path(props.get("model_path", "")).resolve() != model:
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
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--identity", type=Path, default=ROOT / "config/identity.md")
    parser.add_argument("--holdout", action="store_true", help="use alternate prompts to check a changed identity")
    parser.add_argument("--senses", default=SENSES, help="synthetic body telemetry (empty to omit)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = args.model.resolve(strict=True)
    cfg = config(args.profile, args.identity)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    cortex = AuditionCortex(f"http://127.0.0.1:{port}", args.profile)
    with tempfile.TemporaryDirectory(prefix="being-audition-slot-") as slot_dir, \
            args.output.with_suffix(".server.log").open("w") as log:
        command = [str(args.server.resolve(strict=True)), "--model", str(model), "--host", "127.0.0.1",
                   "--port", str(port), "--ctx-size", str(cfg.ctx_tokens), "--parallel", "1", "--threads", "2",
                   "--reasoning-budget", "0", "--slot-save-path", slot_dir, "--no-webui", "--n-gpu-layers", "0"]
        with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as server:
            try:
                results = asyncio.run(audition(cortex, cfg, server, model, HOLDOUT if args.holdout else CASES,
                                               args.senses))
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
    results.update(model=model.name, profile=args.profile, generation=PROFILES[args.profile], max_tokens=cfg.max_tokens,
                   identity=cfg.identity, senses=args.senses, suite="holdout" if args.holdout else "original",
                   scope="Mac CPU, two threads. Final transcript to speakable clauses through production Mind.")
    args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
