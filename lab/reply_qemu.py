"""Compare reply scheduling against one dedicated llama-server inside QEMU."""

import argparse
import asyncio
import json
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from lab.turn_latency import CASES, slots, summarize, trial
from mind.cortex import Cortex


MODES = (
    "final-only", "legacy-ahead", "cancel-ahead", "warm-prefix", "bounded-ahead", "restored-prefix",
)
CASE_NAMES = tuple(case.name for case in CASES)
ROOT = Path(__file__).resolve().parent.parent
GUEST_MODULE_PATH = "/mnt/updates"
SERVER = "http://127.0.0.1:8080"
REPORT = "/run/reply-qemu.json"
TRACE = "/run/reply-qemu.stderr"


async def guest_probe(args):
    cortex = Cortex(SERVER, timeout=args.trial_timeout)
    if args.guest == "ready":
        deadline = time.monotonic() + args.trial_timeout
        while time.monotonic() < deadline:
            if await cortex.healthy():
                await slots(cortex)
                properties = await cortex._cancellable_request("GET", "/props", None)
                return {key: properties.get(key) for key in ("build_info", "model_path", "total_slots")}
            await asyncio.sleep(0.5)
        raise TimeoutError("guest llama-server did not become ready")

    profile = Path("/etc/being")
    settings = json.loads((profile / "being.json").read_text())
    system = (profile / "identity.md").read_text()
    system = system.replace("{name}", settings["name"]).replace("{owner}", "Alex")
    case = next(case for case in CASES if case.name == args.cases[0])
    return await trial(cortex, system, case, args.modes[0], "guest")


def stage_fixtures(directory):
    lab = directory / "lab"
    lab.mkdir()
    for name in ("__init__.py", "reply_qemu.py", "turn_latency.py"):
        shutil.copyfile(ROOT / "lab" / name, lab / name)


def checked_shell(vm, command, timeout=30):
    status, output = vm.run(command, timeout=timeout)
    if status:
        raise RuntimeError(f"guest command exited {status}: {output[-2000:]}")
    return output


def guest_call(vm, mode, timeout, options=()):
    command = shlex.join(
        ["python3", "-m", "lab.reply_qemu", "--guest", mode, "--trial-timeout", str(timeout), *options]
    )
    environment = f"PYTHONPATH=/usr/lib/being:{GUEST_MODULE_PATH} PYTHONDONTWRITEBYTECODE=1"
    status, output = vm.run(
        f"{environment} {command} >{REPORT} 2>{TRACE}; "
        f"result=$?; if [ \"$result\" -eq 0 ]; then cat {REPORT}; else tail -30 {TRACE}; fi; (exit \"$result\")",
        timeout=timeout,
    )
    if status:
        raise RuntimeError(f"guest {mode} failed with exit status {status}: {output[-2400:]}")
    return json.loads(output)


def run(args):
    from lab.vm import VM, Timeout as QemuTimeout

    image = args.image.resolve()
    image_stat = image.stat()
    report = {
        "scope": "final synthetic transcript to first speakable clause under emulation",
        "interpretation": "paired QEMU trials; a small sample is exploratory and does not predict native latency",
        "guest": {"cpu": "IvyBridge", "vcpus": 4, "memory": "4G"},
        "image": str(image),
        "image_size_bytes": image_stat.st_size,
        "image_mtime_ns": image_stat.st_mtime_ns,
        "trial_timeout_seconds": args.trial_timeout,
        "budget_seconds": args.budget,
        "trials": [],
    }
    deadline = time.monotonic() + args.budget

    def remaining(max_seconds):
        seconds = min(max_seconds, int(deadline - time.monotonic()))
        if seconds < 1:
            raise TimeoutError("QEMU run budget expired")
        return seconds

    with tempfile.TemporaryDirectory(prefix="being-reply-fixtures-") as directory:
        fixtures = Path(directory)
        stage_fixtures(fixtures)
        vm = VM(image=image, loopback=True, audio_fixtures=fixtures)
        try:
            vm.start()
            vm.login(remaining(120))
            checked_shell(vm, f"mount -t vfat -o ro /dev/vda1 {GUEST_MODULE_PATH}")
            # Prevent the appliance mind from competing for the benchmark slot.
            checked_shell(vm, "systemctl stop being-mind")
            report["server"] = guest_call(vm, "ready", remaining(120))
            for repeat in range(args.repeats):
                for index, case in enumerate(args.cases):
                    shift = (repeat + index) % len(args.modes)
                    modes = args.modes[shift:] + args.modes[:shift]
                    for mode in modes:
                        print(f"QEMU {repeat + 1}/{args.repeats} {case} {mode}", file=sys.stderr, flush=True)
                        row = guest_call(
                            vm, "trial", remaining(args.trial_timeout),
                            ["--cases", case, "--modes", mode],
                        )
                        row["repeat"] = repeat + 1
                        report["trials"].append(row)
                        print(f"  First clause: {row['first_clause_ms']} ms", file=sys.stderr, flush=True)
        except (RuntimeError, TimeoutError, ValueError, QemuTimeout) as error:
            report["error"] = str(error)
        finally:
            vm.destroy()
    cold_cases = {row["case"] for row in report["trials"] if row["mode"] == "final-only"}
    report["summary"] = summarize([row for row in report["trials"] if row["case"] in cold_cases])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=ROOT / "out" / "images" / "beingos.img")
    parser.add_argument("--cases", choices=CASE_NAMES, nargs="+", default=["short"])
    parser.add_argument("--modes", choices=MODES, nargs="+", default=["final-only", "warm-prefix"])
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--trial-timeout", type=int, default=300, help="seconds per guest probe")
    parser.add_argument("--budget", type=int, default=600, help="seconds for the host run")
    parser.add_argument("--guest", choices=("ready", "trial"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.trial_timeout < 1 or args.budget < 1:
        parser.error("timeouts and budgets must be positive")
    if args.guest:
        print(json.dumps(asyncio.run(guest_probe(args))))
        return 0
    if not args.image.is_file():
        parser.error(f"no image at {args.image}")
    result = run(args)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
