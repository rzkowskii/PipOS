"""One bounded read of the installed conversation model's leading pages."""

import json
import os
import stat
import time
from pathlib import Path

MIB = 1024 * 1024
MODEL = Path("/models/reflex.gguf")
SIZES = {"64", "128", "256"}


def spare_memory():
    with Path("/proc/meminfo").open(encoding="ascii") as source:
        for line in source:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return 0


def warm(limit):
    started = time.monotonic()
    offset = 0
    fd = os.open(MODEL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != MODEL.parent.stat().st_dev:
            raise ValueError("model is not a regular file on the models volume")
        limit = min(limit, info.st_size)
        while offset < limit and time.monotonic() - started < 6:
            block = os.pread(fd, min(4 * MIB, limit - offset), offset)
            if not block:
                break
            offset += len(block)
    finally:
        os.close(fd)
    return {"bytes_read": offset, "target_bytes": limit,
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


def main():
    value = os.environ.get("BEING_MODEL_WARM_MIB")
    if value not in SIZES:
        return 0
    try:
        limit = int(value) * MIB
        if spare_memory() < 2 * 1024 * MIB + limit:
            result = {"status": "memory_reserve"}
        else:
            result = {"status": "read", **warm(limit)}
    except (OSError, ValueError) as error:
        result = {"status": type(error).__name__}
    print("model warm " + json.dumps(result, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
