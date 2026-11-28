"""Collect WP-A4 DIMM, cache, turbo-ratio and threaded STREAM evidence.

Run as `PYTHONPATH=/usr/lib/being python3 -m lab.memory_bandwidth NEW-EVIDENCE.json`
on a native MacBookPro9,2 diagnostic Try boot. All runs use the same OpenMP
binary. The report includes only allowlisted DMI fields.
"""

import argparse
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import time

from lab.thread_sweep import CPU, file_stamp, fingerprint, preflight, quiet_conditions
from mind.bench import read
from mind.storage import atomic_write

STREAM = Path("/usr/bin/stream")
STREAM_SOURCE = Path("/usr/share/being/stream-5.10.c")
STREAM_SOURCE_SHA256 = "a52bae5e175bea3f7832112af9c085adab47117f7d2ce219165379849231692b"
DIMM_FIELDS = {
    "Size", "Locator", "Bank Locator", "Type", "Speed", "Configured Memory Speed",
    "Configured Clock Speed", "Data Width", "Total Width",
}
OPERATIONS = ("Copy", "Scale", "Add", "Triad")
CAPACITY = re.compile(r"(\d+)([KMG])")
TURBO_RATIO = re.compile(r"[0-9a-fA-F]{1,16}")
TWO_WORKER_THRESHOLD_MBPS = 12_000


def command(argv, *, env=None, timeout=45):
    return subprocess.run(
        argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=True,
    ).stdout


def dimm_inventory(output):
    devices = []
    for section in output.split("\n\n"):
        lines = section.splitlines()
        if not any(line.strip() == "Memory Device" for line in lines):
            continue
        device = {}
        for line in lines:
            field, sep, value = line.strip().partition(":")
            if sep and field in DIMM_FIELDS:
                device[field] = value.strip()[:120]
        if "Locator" in device and "Size" in device:
            devices.append(device)
    if not devices or not any(re.match(r"[1-9]\d* (?:kB|MB|GB|TB)\b", row["Size"]) for row in devices):
        raise ValueError("dmidecode did not identify an installed memory device")
    return devices


def cache_inventory():
    caches = []
    for path in sorted((CPU / "cpu0/cache").glob("index[0-9]*")):
        size = CAPACITY.fullmatch(read(path / "size", "") or "")
        level = read(path / "level")
        kind = read(path / "type")
        if not size or not level or not level.isdecimal() or kind not in {"Data", "Instruction", "Unified"}:
            raise ValueError("unrecognized CPU cache inventory")
        power = "KMG".index(size[2]) + 1
        caches.append({"level": int(level), "type": kind, "bytes": int(size[1]) << (power * 10),
                       "shared_cpu_list": read(path / "shared_cpu_list")})
    if not caches:
        raise ValueError("CPU cache capacity is unavailable")
    return caches


def stream_result(output, workers, largest_cache):
    if "STREAM version $Revision: 5.10 $" not in output.splitlines():
        raise ValueError("the diagnostic binary did not identify itself as STREAM 5.10")
    for field in ("requested", "counted"):
        match = re.search(rf"^Number of Threads {field} = (\d+)$", output, re.M)
        if not match or int(match[1]) != workers:
            raise ValueError(f"STREAM did not report {workers} OpenMP workers {field}")
    if "Solution Validates:" not in output:
        raise ValueError("STREAM array validation failed")
    array = re.search(r"^Array size = (\d+) \(elements\), Offset = 0 \(elements\)$", output, re.M)
    element = re.search(r"^This system uses (\d+) bytes per array element\.", output, re.M)
    if not array or not element:
        raise ValueError("STREAM did not identify its array size")
    array_bytes = int(array[1]) * int(element[1])
    if array_bytes < max(4 * largest_cache, 1_000_000):
        raise ValueError("STREAM arrays do not meet the author's cache-size rule")
    ticks = re.search(r"^ +\(= (\d+) clock ticks\)$", output, re.M)
    if not ticks or int(ticks[1]) < 20:
        raise ValueError("STREAM estimates less than twenty timer ticks per test")
    operations = {}
    for name in OPERATIONS:
        match = re.search(rf"^{name}:\s+([^\n]+)$", output, re.M)
        values = [float(value) for value in match[1].split()] if match else []
        if len(values) != 4 or any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError(f"STREAM {name} metrics are missing or malformed")
        best, average, minimum, maximum = values
        if not minimum <= average <= maximum:
            raise ValueError(f"STREAM {name} timing order is invalid")
        operations[name] = {"best_mb_per_s": best, "average_s": average, "min_s": minimum, "max_s": maximum}
    return {"workers": workers, "array_bytes": array_bytes, "timer_ticks_estimate": int(ticks[1]),
            "operations": operations}


def collect():
    facts, core_order = preflight()
    quiet_conditions(facts["power"])
    deadline = time.monotonic() + 30
    stamp, stream_id = fingerprint(STREAM, 20 << 20, deadline)
    source_stamp, source_id = fingerprint(STREAM_SOURCE, 1024 * 1024, deadline)
    if source_id["sha256"] != STREAM_SOURCE_SHA256:
        raise ValueError("the diagnostic image does not carry the pinned author's STREAM source")
    dimms = dimm_inventory(command(["/usr/sbin/dmidecode", "-t", "memory"], timeout=10))
    caches = cache_inventory()
    largest = max(row["bytes"] for row in caches)
    turbo = command(["/usr/sbin/rdmsr", "-p", "0", "0x1ad"], timeout=10).strip()
    if not TURBO_RATIO.fullmatch(turbo):
        raise ValueError("MSR_TURBO_RATIO_LIMIT did not return a hexadecimal value")
    results = []
    for round_number, schedule in enumerate(((1, 2, 4), (2, 4, 1), (4, 1, 2)), 1):
        for workers in schedule:
            checked_temp = quiet_conditions(facts["power"])
            env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C",
                   "OMP_DYNAMIC": "FALSE", "OMP_NUM_THREADS": str(workers)}
            sample = stream_result(command([str(STREAM)], env=env), workers, largest)
            results.append({"round": round_number, "package_c_before": checked_temp,
                            "package_c_after": quiet_conditions(facts["power"]), **sample})
    if file_stamp(STREAM) != stamp or file_stamp(STREAM_SOURCE) != source_stamp:
        raise ValueError("STREAM inputs changed during measurement")
    triad = statistics.median(sample["operations"]["Triad"]["best_mb_per_s"]
                              for sample in results if sample["workers"] == 2)
    return {"kind": "pipos-wp-a4-memory-bandwidth-v1", "system": facts, "core_order": core_order,
            "stream_binary": stream_id, "stream_source": source_id,
            "dimm_inventory": dimms, "cache_inventory": caches,
            "msr_0x1ad_cpu0": f"0x{int(turbo, 16):016x}", "stream": results,
            "median_two_worker_best_triad_mb_per_s": triad, "two_worker_triad_below_threshold": (
                triad < TWO_WORKER_THRESHOLD_MBPS),
            "threshold_mb_per_s": TWO_WORKER_THRESHOLD_MBPS, "promotion_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new JSON evidence file")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("the evidence path already exists")
    try:
        evidence = collect()
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("the evidence path already exists")
        atomic_write(args.output, json.dumps(evidence, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"memory measurement stopped: {exc}\n")


if __name__ == "__main__":
    main()
