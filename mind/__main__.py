"""Run the mind:  python3 -m mind [options]"""

import argparse
import asyncio
import os
import re
import signal
from pathlib import Path

from .bus import Bus
from .cortex import Cortex
from .memory import Memory
from .profile import DEFAULT_PERSONALITY, DEFAULT_TEMPERAMENT, default_mind_dir, load_profile, read_factory, temperament
from .retention import load_policy
from .turn import SUBS, Config, Mind, log

ROOT = Path(__file__).resolve().parent.parent


def load_config(config_dir, mind_dir, *, include_core=True):
    settings = read_factory(config_dir)
    profile = load_profile(config_dir, mind_dir, settings=settings, strict=False)
    cfg = Config(name=profile.name, owner=profile.owner, personality=profile.personality)
    for key in ("ctx_tokens", "max_tokens", "temperature", "prefill"):
        if key in settings:
            setattr(cfg, key, settings[key])
    identity = (config_dir / "identity.md").read_text(encoding="utf-8")
    if profile.personality != DEFAULT_PERSONALITY:
        style = temperament(profile.personality)
        if DEFAULT_TEMPERAMENT in identity:
            identity = identity.replace(DEFAULT_TEMPERAMENT, style, 1)
        else:
            identity = f"{identity.rstrip()}\n\n{style}\n"
    names = {"{name}": cfg.name, "{owner}": cfg.owner}
    cfg.identity = re.sub(r"\{(?:name|owner)\}", lambda match: names[match[0]], identity)
    guest_names = {**names, "{owner}": "a person"}
    cfg.guest_identity = re.sub(r"\{(?:name|owner)\}", lambda match: guest_names[match[0]], identity)
    core = mind_dir / "core.md"
    cfg.core = core.read_text() if include_core and core.exists() else ""
    return cfg, settings


async def main():
    parser = argparse.ArgumentParser(prog="mind", description="The being's mind.")
    parser.add_argument("--bus", help="bus socket path (default: see docs/PROTOCOL.md)")
    parser.add_argument("--config", type=Path, default=ROOT / "config", help="folder with being.json and identity.md")
    parser.add_argument("--mind-dir", type=Path, default=None, help="where memory lives")
    parser.add_argument("--cortex", help="model server URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--model", help="model name to ask for")
    parser.add_argument(
        "--plain-openai",
        action="store_true",
        help="the server is not llama-server: send no grammar or cache hints",
    )
    args = parser.parse_args()

    mind_dir = args.mind_dir or default_mind_dir()
    mind_dir.mkdir(parents=True, exist_ok=True)
    policy = load_policy(mind_dir)
    cfg, settings = load_config(args.config, mind_dir, include_core=policy.mode == "persistent")

    url = args.cortex or os.environ.get("BEING_CORTEX") or settings.get("cortex_url") or "http://127.0.0.1:8080"
    cortex = Cortex(url, model=args.model or settings.get("model", "default"), llama=not args.plain_openai)
    memory = Memory(mind_dir) if policy.mode != "session-only" else None
    bus = Bus("mind", SUBS, path=args.bus)
    mind = Mind(bus, cortex, memory, cfg, mind_dir, policy=policy)
    log(f"{cfg.name} | {policy.mode} memory | cortex at {url}")

    task = asyncio.create_task(mind.run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await bus.stop()
        if memory is not None:
            memory.close()


if __name__ == "__main__":
    asyncio.run(main())
