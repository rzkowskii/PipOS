"""Capture voice, display and hardware telemetry during one appliance trial."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from . import hardware_metrics, hardware_timing


def read_environment(root):
    sys_root = root / "sys"
    return {
        "cpu": hardware_metrics.cpu_counters(root / "proc"),
        "cpu_frequency": hardware_metrics.cpu_frequency(sys_root),
        "thermal": hardware_metrics.thermal_info(sys_root),
        "thermal_throttle": hardware_metrics.thermal_throttle_info(sys_root),
        "power": hardware_metrics.power_info(sys_root),
    }


async def sample_environment(seconds, interval, root, started_at):
    clock = asyncio.get_running_loop().time
    milliseconds = hardware_timing.milliseconds
    samples = []
    for offset in (*range(0, seconds, interval), seconds):
        remaining = started_at + offset - clock()
        if remaining > 0:
            await asyncio.sleep(remaining)
        sample_start = clock() - started_at
        if offset < seconds and sample_start >= min(offset + interval, seconds):
            continue
        sensors = await asyncio.to_thread(read_environment, root)
        samples.append({
            "scheduled_at_ms": milliseconds(offset),
            "sample_start_at_ms": milliseconds(sample_start),
            "sample_end_at_ms": milliseconds(clock() - started_at),
            **sensors,
        })
    return samples


async def capture(seconds, interval, *, root=Path("/"), path=hardware_timing.SOCKET):
    if not 1 <= seconds <= hardware_timing.MAX_SECONDS or not 1 <= interval <= hardware_metrics.MAX_INTERVAL:
        raise ValueError("Sampling duration or interval out of range.")
    system = hardware_metrics.system_info(root)
    start_services = hardware_metrics.read_services(root / "proc")
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started_at = asyncio.get_running_loop().time()
    tasks = (
        asyncio.create_task(hardware_timing.capture(seconds, path=path, started_at=started_at)),
        asyncio.create_task(sample_environment(seconds, interval, root, started_at)),
    )
    try:
        timing, samples = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "schema_version": 1,
        "captured_at": captured_at,
        "clock": "monotonic_ms_since_window_start",
        "sampling": {"seconds": seconds, "interval": interval},
        "system": system,
        "services": {"start": start_services, "end": hardware_metrics.read_services(root / "proc")},
        "timing": timing,
        "samples": samples,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--seconds", type=int, default=60,
                        help=f"trial duration, 1–{hardware_timing.MAX_SECONDS} seconds")
    parser.add_argument("--interval", type=int, default=1,
                        help=f"sample interval, 1–{hardware_metrics.MAX_INTERVAL} seconds")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Run on the PipOS device as root in a maintenance session.")
    try:
        print(json.dumps(asyncio.run(capture(args.seconds, args.interval)), allow_nan=False))
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(f"hardware-qualification: {error}")


if __name__ == "__main__":
    main()
