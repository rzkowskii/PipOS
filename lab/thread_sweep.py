"""Collect the native WP-A5 thread, affinity, polling and tiled-matmul sweep.

Run as `PYTHONPATH=/usr/lib/being python3 -m lab.thread_sweep MODEL OUTPUT`
on the diagnostic Try image, with both cortex units and the boot bench stopped.
The first block warms the machine. Ten shuffled blocks follow, each with
all 32 settings and an additional copy of the reference setting for A/A.
Raw samples are flushed after each arm. The sweep changes no system settings.
"""

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
from itertools import product
import json
import math
import os
from pathlib import Path
import random
import signal
import statistics
import subprocess
import time

from lab.prefill_floor import percentile
from mind.bench import HOT_C, package_temp, power_facts, read, system_facts
from mind.hardware_metrics import power_info

BENCH = Path("/usr/bin/llama-bench")
CPU = Path("/sys/devices/system/cpu")
SYS = Path("/sys")
UNITS = ("being-cortex.service", "being-dream-cortex.service", "being-bench.service")
SIZES = ((8, 0), (32, 0), (128, 0), (512, 0), (0, 64))
BLOCKS = 10
MIN_GAIN = 5.0


@dataclass(frozen=True)
class Arm:
    threads: int = 2
    tiled: int = 1
    pinned: bool = False
    poll: int = 50
    replica: bool = False


REFERENCE = Arm()
REPLICA = replace(REFERENCE, replica=True)
ARMS = tuple(Arm(*values) for values in product(range(1, 5), (0, 1), (False, True), (0, 50))) + (REPLICA,)


def sibling_order(cpus):
    """Place one worker on each physical core before using its SMT sibling."""
    by_cpu = {}
    for cpu in cpus:
        path = CPU / f"cpu{cpu}/topology/thread_siblings_list"
        group = set()
        for span in (read(path, "") or "").split(","):
            first, separator, last = span.partition("-")
            start, end = int(first), int(last if separator else first)
            if not 0 <= start <= end < 64:
                raise ValueError("unexpected thread sibling range")
            group.update(range(start, end + 1))
        by_cpu[cpu] = tuple(sorted(group))
    siblings = sorted(set(by_cpu.values()))
    if (len(siblings) != 2 or any(len(group) != 2 for group in siblings)
            or set(siblings[0] + siblings[1]) != cpus
            or any(cpu not in group or by_cpu[group[0]] != group or by_cpu[group[1]] != group
                   for cpu, group in by_cpu.items())):
        raise ValueError("expected both logical CPUs on each of the two physical cores")
    return [cpu for group in zip(*siblings) for cpu in group]


def preflight():
    facts = system_facts()
    cpu = facts["cpu"]
    if (facts["dmi"] != "MacBookPro9,2" or not cpu["avx"] or cpu["avx2"]
            or "being.try=1" not in (facts["cmdline"] or "").split()
            or not Path("/usr/share/being/diagnostic").is_file()
            or Path("/sys/firmware/qemu_fw_cfg").exists()):
        raise ValueError("the native MacBookPro9,2 diagnostic Try boot is required")
    cpus = set(os.sched_getaffinity(0))
    if len(cpus) != 4 or cpu["threads"] != 4 or cpu["physical_cores"] != 2:
        raise ValueError("the caller must have all four logical CPUs available")
    return facts, sibling_order(cpus)


def quiet_conditions(expected_power):
    power = power_info(SYS)
    if (power["mains_online"] is not True or len(power["batteries"]) != 1
            or any((battery["charge_percent"] or 0) < 95
                   or battery["status"] not in {"Full", "Not charging"} for battery in power["batteries"])):
        raise ValueError("requires AC and a battery at least 95% full with charging settled")
    temp = package_temp()
    if temp is None or temp >= HOT_C:
        raise ValueError("package temperature is unavailable or reached the thermal stop")
    current = power_facts()
    if current != expected_power:
        raise ValueError("CPU or package power settings changed mid-sweep")
    state = subprocess.run(
        ["/usr/bin/systemctl", "show", "--property=ActiveState", "--value", *UNITS],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=True,
    ).stdout.split()
    if state != ["inactive"] * len(UNITS):
        raise ValueError("both cortex units and the boot bench must be inactive")
    return temp


def file_stamp(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def fingerprint(path, max_bytes, deadline):
    before = file_stamp(path)
    if not path.is_file() or not 0 < before[2] <= max_bytes:
        raise ValueError(f"{path.name} must be a bounded regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while (data := stream.read(1024 * 1024)):
            if time.monotonic() >= deadline:
                raise ValueError("sweep deadline elapsed during input hashing")
            digest.update(data)
    if file_stamp(path) != before:
        raise ValueError(f"{path.name} changed while hashing")
    return before, {"name": path.name, "bytes": before[2], "sha256": digest.hexdigest()}


def measure(arm, *, model, core_order, deadline):
    workers = core_order[:arm.threads] if arm.pinned else []
    mask = hex(sum(1 << cpu for cpu in workers))
    argv = [
        str(BENCH), "-m", str(model), "-o", "json", "-r", "1", "-p", "8,32,128,512", "-n", "64",
        "-ngl", "0", "-t", str(arm.threads), "-C", mask, "--cpu-strict", str(int(arm.pinned)),
        "--poll", str(arm.poll),
    ]
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GGML_CPU_TILED_MM": str(arm.tiled)}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("sweep deadline elapsed")
    original_affinity = os.sched_getaffinity(0)
    try:
        if workers:
            os.sched_setaffinity(0, workers)
        result = subprocess.run(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=min(120, remaining), check=True,
        )
    finally:
        if workers:
            os.sched_setaffinity(0, original_affinity)
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != len(SIZES):
        raise ValueError("llama-bench returned the wrong number of workload results")
    samples, builds = {}, set()
    for row in rows:
        size = row.get("n_prompt"), row.get("n_gen")
        if (size not in SIZES or size in samples or row.get("n_depth") != 0
                or row.get("n_threads") != arm.threads or row.get("cpu_mask") != mask
                or row.get("cpu_strict") != arm.pinned or row.get("poll") != arm.poll
                or row.get("n_gpu_layers") != 0):
            raise ValueError("llama-bench settings differ from the requested arm")
        build = row.get("build_commit")
        if not isinstance(build, str) or not build:
            raise ValueError("llama-bench did not identify its build")
        builds.add(build)
        values = row.get("samples_ts", [])
        if (not isinstance(values, list) or len(values) != 1 or type(values[0]) not in (int, float)
                or not math.isfinite(values[0]) or values[0] <= 0):
            raise ValueError("llama-bench did not return one positive speed per workload")
        samples[size] = values[0]
    if len(builds) != 1:
        raise ValueError("llama-bench changed builds in one arm")
    return samples, builds.pop()


def averages(values):
    return {"mean_tps": statistics.mean(values), "sd_tps": statistics.stdev(values)}


def paired_stats(control, candidate, rng):
    gains = [100 * (b / a - 1) for a, b in zip(control, candidate)]
    estimates = [sum(rng.choices(gains, k=len(gains))) / len(gains) for _ in range(4000)]
    return {"mean_gain_pct": statistics.mean(gains),
            "ci95_pct": [percentile(estimates, tail) for tail in (0.025, 0.975)]}


def zero_in_interval(result):
    lo, hi = result["ci95_pct"]
    return lo <= 0 <= hi


def comparisons():
    yield "a/a", REFERENCE, REPLICA
    for arm in ARMS:
        if arm.replica:
            continue
        if arm.tiled == 1:
            yield "tiled-on", replace(arm, tiled=0), arm
        if arm.pinned:
            yield "pinned", replace(arm, pinned=False), arm
        if arm.poll == 0:
            yield "poll-zero", replace(arm, poll=50), arm
        if arm.threads != 2:
            yield "threads-vs-two", replace(arm, threads=2), arm
        if arm.threads == 2:
            yield "threads-one-to-two", replace(arm, threads=1), arm


def summarize(blocks, seed):
    rng = random.Random(seed ^ 0xA5)
    for size in SIZES:
        rates = {arm: [block[arm][size] for block in blocks] for arm in ARMS}
        aa = paired_stats(rates[REFERENCE], rates[REPLICA], rng)
        stable = zero_in_interval(aa) and abs(aa["mean_gain_pct"]) < MIN_GAIN
        relative = []
        for factor, base, candidate in comparisons():
            result = aa if factor == "a/a" else paired_stats(rates[base], rates[candidate], rng)
            raises_for_review = (factor not in {"a/a", "threads-one-to-two"}
                                 and (factor != "tiled-on" or size == (32, 0))
                                 and stable and result["ci95_pct"][0] > 0
                                 and result["mean_gain_pct"] >= MIN_GAIN)
            relative.append({"factor": factor, "baseline": asdict(base), "candidate": asdict(candidate),
                             **result, "candidate_for_followup": raises_for_review})
        yield {"prompt_n": size[0], "decode_n": size[1], "a_a_stable": stable,
               "absolute": [{"arm": asdict(arm), **averages(rates[arm])} for arm in ARMS],
               "relative": relative}


def collect(model, output, seed, max_minutes):
    facts, order = preflight()
    quiet_conditions(facts["power"])
    deadline = time.monotonic() + max_minutes * 60
    model = model.resolve(strict=True)
    identities = {}
    for label, path, limit in (("benchmark", BENCH, 100 << 20), ("model", model, 4 << 30)):
        identities[label] = fingerprint(path, limit, deadline)
    rng = random.Random(seed)
    blocks = []
    build = None
    with output.open("x", encoding="utf-8") as stream:
        def record(event, **fields):
            stream.write(json.dumps({"type": event, **fields}, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        record("start", kind="pipos-wp-a5-thread-sweep-v1", seed=seed, system=facts,
               core_order=order, inputs={label: proof for label, (_, proof) in identities.items()},
               measured_blocks=BLOCKS, arm_timeout_s=120, max_minutes=max_minutes)
        for index in range(BLOCKS + 1):
            block = {}
            schedule = list(ARMS)
            rng.shuffle(schedule)
            for sequence, arm in enumerate(schedule, 1):
                temp = quiet_conditions(facts["power"])
                samples, commit = measure(arm, model=model, core_order=order, deadline=deadline)
                if build is not None and commit != build:
                    raise ValueError("llama-bench changed builds between arms")
                if any(file_stamp(path) != identities[label][0]
                       for label, path in (("model", model), ("benchmark", BENCH))):
                    raise ValueError("input files changed during the sweep")
                build = commit
                block[arm] = samples
                record("sample", block=index, arm_index=sequence, warmup=index == 0, arm=asdict(arm),
                       package_c_before=temp, build=build,
                       workloads=[{"prompt_n": size[0], "decode_n": size[1], "tps": samples[size]}
                                  for size in SIZES])
            if index:
                blocks.append(block)
        quiet_conditions(facts["power"])
        for summary in summarize(blocks, seed):
            record("workload_summary", **summary)
        record("complete", promotion_authorized=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="one GGUF from the diagnostic model media")
    parser.add_argument("output", type=Path, help="new JSON Lines evidence file")
    parser.add_argument("--seed", type=int, default=20261127)
    parser.add_argument("--max-minutes", type=int, choices=range(1, 181), default=120, metavar="1..180")
    args = parser.parse_args(argv)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        collect(args.model, args.output, args.seed, args.max_minutes)
    except KeyboardInterrupt:
        parser.exit(130, "thread sweep stopped; an incomplete file has no complete marker\n")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"thread sweep stopped: {exc}\n")


if __name__ == "__main__":
    main()
