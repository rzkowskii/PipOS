"""Run the mind:  python3 -m mind [options]"""

import argparse
import asyncio
import inspect
import os
import re
import signal
from pathlib import Path

from . import sd_notify
from .app_commands import AppBridge
from .bus import Bus
from .cortex import Cortex
from .memory import Memory
from .profile import DEFAULT_PERSONALITY, DEFAULT_TEMPERAMENT, default_mind_dir, load_profile, read_factory, temperament
from .retention import load_policy
from .turn import SUBS, Config, Mind, log, optional_module

ROOT = Path(__file__).resolve().parent.parent
_NUMBER = (int, float)
# Config fields read from being.json, with the types each one accepts.
FACTORY_KEYS = {
    "ctx_tokens": (int,),
    "max_tokens": (int,),
    "temperature": _NUMBER,
    "prefill": (bool,),
    "morning_brief": (bool,),
    "time_zone": (str,),
    "top_k": (int,),
    "top_p": _NUMBER,
    "presence_penalty": _NUMBER,
    "reply_shape": (str,),
    "battery_plea_percent": _NUMBER,
    "battery_plea_minutes": _NUMBER,
    "battery_second_plea_percent": _NUMBER,
    "battery_second_plea_minutes": _NUMBER,
    "battery_critical_percent": _NUMBER,
    "battery_critical_minutes": _NUMBER,
}


def load_config(config_dir, mind_dir, *, include_core=True):
    settings = read_factory(config_dir)
    profile = load_profile(config_dir, mind_dir, settings=settings, strict=False)
    cfg = Config(name=profile.name, owner=profile.owner, personality=profile.personality)
    for key, kinds in FACTORY_KEYS.items():
        value = settings.get(key)
        # A wrong type in being.json keeps the built-in value. bool is an
        # int in Python, so a number setting never takes true or false.
        if value is None or isinstance(value, bool) and bool not in kinds or not isinstance(value, kinds):
            continue
        if key == "reply_shape" and value not in ("open", "strict"):
            continue
        setattr(cfg, key, value)
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


def build_night_shift(mind_dir, memory, cfg):
    """The night shift (mind/night_shift.py) with the other organs' jobs, or
    None when it is not part of this image: then the night is the rule-based
    dream journal alone. The mind registers its own morning re-warm."""
    module = optional_module("night_shift")
    shift_class = getattr(module, "NightShift", None)
    if shift_class is None:
        return None
    offered = {"state_dir": mind_dir, "mind_dir": mind_dir, "memory": memory, "owner": cfg.owner, "name": cfg.name}
    try:
        parameters = inspect.signature(shift_class).parameters
        shift = shift_class(**{key: value for key, value in offered.items() if key in parameters})
    except (TypeError, ValueError) as exc:
        log(f"night shift not started: {exc}")
        return None
    jobs = []
    backup = optional_module("backup")
    if backup is not None and hasattr(backup, "auto_backup_job"):
        jobs.append(("backup", lambda: backup.auto_backup_job(mind_dir)))
    packs = optional_module("model_pack")
    rehash = getattr(packs, "rehash_job", None) or getattr(packs, "RehashJob", None)
    if rehash is not None:
        jobs.append(("rehash", rehash))
    for name, make in jobs:
        try:
            shift.register(name, make())
        except Exception as exc:
            log(f"night shift job {name} not registered: {exc!r}")
    return shift


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
    apps = AppBridge(bus, name=cfg.name)
    night_shift = build_night_shift(mind_dir, memory, cfg) if memory is not None else None
    mind = Mind(bus, cortex, memory, cfg, mind_dir, policy=policy, apps=apps, night_shift=night_shift)
    everyday = optional_module("everyday")
    if everyday is not None and hasattr(everyday, "preload"):
        # Every skill is imported now, not on the first thing said.
        everyday.preload()
    log(f"{cfg.name} | {policy.mode} memory | cortex at {url}")

    task = asyncio.create_task(mind.run())
    # READY once the bus is up (code commands answer from then on, with or
    # without the model), then a watchdog ping every 5 s from this loop.
    alive = asyncio.create_task(sd_notify.keep_alive(ready_when=bus.connected))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        alive.cancel()
        await asyncio.gather(alive, return_exceptions=True)
        await bus.stop()
        if memory is not None:
            memory.close()


if __name__ == "__main__":
    asyncio.run(main())
