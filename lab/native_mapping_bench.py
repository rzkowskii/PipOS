#!/usr/bin/env python3
"""Read-only native file-mapping evidence for PipOS Stage 1b/2 runs.

Run immediately after a native boot for a defensible cold arm.  The tool
never drops caches, writes a device, changes a governor, or starts a model.
It records page residency before touching each file, then compares the first
and second pass over the same private file-backed mapping.
"""

import argparse
import ctypes
import json
import mmap
import os
import platform
import resource
import sys
import time
from pathlib import Path

KIND = "pipos-native-mapping-v1"
DEFAULT_MAX_BYTES = 4 * 1024 ** 3
DEFAULT_COLD_RESIDENT = 0.02


def read(path, default=None):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def meminfo(root=Path("/")):
    values = {}
    for line in (read(root / "proc/meminfo", "") or "").splitlines():
        name, separator, rest = line.partition(":")
        if separator:
            words = rest.split()
            if words and words[0].isdigit():
                values[name] = int(words[0]) * (1024 if len(words) > 1 and words[1] == "kB" else 1)
    keep = ("MemTotal", "MemFree", "MemAvailable", "Cached", "Buffers", "SReclaimable", "SwapTotal", "SwapFree")
    return {name: values.get(name) for name in keep}


def cpu_facts():
    text = read("/proc/cpuinfo", "") or ""
    model = next((line.partition(":")[2].strip() for line in text.splitlines()
                  if line.startswith("model name")), None)
    flags_line = next((line.partition(":")[2].split() for line in text.splitlines()
                       if line.startswith("flags")), [])
    flags = set(flags_line)
    return {"model": model, "logical_cpus": os.cpu_count(), "avx": "avx" in flags,
            "avx2": "avx2" in flags, "f16c": "f16c" in flags, "sse4_2": "sse4_2" in flags}


def _block_device(path):
    """Read-only sysfs facts for the filesystem device containing path."""
    stat = path.stat()
    major, minor = os.major(stat.st_dev), os.minor(stat.st_dev)
    link = Path(f"/sys/dev/block/{major}:{minor}")
    facts = {"major": major, "minor": minor, "sysfs": None}
    try:
        resolved = link.resolve(strict=True)
    except OSError:
        return facts
    device = next((item for item in (resolved, *resolved.parents) if (item / "queue").is_dir()), None)
    if device is None:
        return facts
    facts.update({
        "sysfs": str(device), "name": device.name,
        "model": read(device / "device/model"), "revision": read(device / "device/rev"),
        "rotational": read(device / "queue/rotational"),
        "scheduler": read(device / "queue/scheduler"),
        "read_ahead_kb": read(device / "queue/read_ahead_kb"),
        "logical_block_bytes": read(device / "queue/logical_block_size"),
    })
    lowered = str(device).casefold()
    facts["transport_hint"] = next((kind for kind in ("usb", "nvme", "ata") if kind in lowered), None)
    return facts


def _mount(path):
    best = None
    for line in (read("/proc/self/mountinfo", "") or "").splitlines():
        left, separator, right = line.partition(" - ")
        words = left.split()
        if not separator or len(words) < 6:
            continue
        mountpoint = words[4].replace("\\040", " ")
        try:
            path.relative_to(mountpoint)
        except ValueError:
            continue
        if best is None or len(mountpoint) > len(best["mountpoint"]):
            after = right.split()
            best = {"mountpoint": mountpoint, "mount_options": words[5],
                    "filesystem": after[0] if after else None,
                    "source": after[1] if len(after) > 1 else None}
    return best


def _resident(mapping, length, page_size):
    pages = (length + page_size - 1) // page_size
    vector = (ctypes.c_ubyte * pages)()
    address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
    libc = ctypes.CDLL(None, use_errno=True)
    mincore = libc.mincore
    mincore.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte))
    mincore.restype = ctypes.c_int
    if mincore(address, length, vector) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    count = sum(1 for value in vector if value & 1)
    return {"pages": pages, "resident_pages": count, "resident_ratio": round(count / pages, 6)}


def _faults():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_minflt, usage.ru_majflt


def _touch(mapping, length, page_size):
    before_faults = _faults()
    started = time.monotonic_ns()
    checksum = 0
    for offset in range(0, length, page_size):
        checksum ^= mapping[offset]
    checksum ^= mapping[length - 1]
    elapsed = time.monotonic_ns() - started
    after_faults = _faults()
    return {
        "seconds": round(elapsed / 1e9, 6),
        "mib_per_second": round((length / 1024 ** 2) / (elapsed / 1e9), 3) if elapsed else None,
        "minor_faults": after_faults[0] - before_faults[0],
        "major_faults": after_faults[1] - before_faults[1],
        "page_checksum": checksum,
    }


def measure(label, path, *, max_bytes, cold_limit):
    path = Path(path).resolve(strict=True)
    info = path.stat()
    if not path.is_file() or info.st_size <= 0:
        raise ValueError(f"{path} is not a non-empty regular file")
    if info.st_size > max_bytes:
        raise ValueError(f"{path} is {info.st_size} bytes (limit {max_bytes})")
    page_size = mmap.PAGESIZE
    before_memory = meminfo()
    with path.open("rb", buffering=0) as stream:
        # ACCESS_COPY gives ctypes an address for mincore while remaining a
        # private mapping of an O_RDONLY file. This code never writes it.
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_COPY) as mapping:
            residency_before = _resident(mapping, info.st_size, page_size)
            first = _touch(mapping, info.st_size, page_size)
            residency_after_first = _resident(mapping, info.st_size, page_size)
            second = _touch(mapping, info.st_size, page_size)
            residency_after_second = _resident(mapping, info.st_size, page_size)
    after_memory = meminfo()
    available = before_memory.get("MemAvailable")
    return {
        "label": label, "path": str(path), "bytes": info.st_size,
        "inode": info.st_ino, "device": info.st_dev, "mtime_ns": info.st_mtime_ns,
        "mount": _mount(path), "block_device": _block_device(path),
        "memory_before": before_memory, "memory_after": after_memory,
        "spare_ram_before_bytes": available,
        "file_fraction_of_spare_ram": round(info.st_size / available, 6) if available else None,
        "page_size": page_size, "residency_before": residency_before,
        "cold_eligible": residency_before["resident_ratio"] <= cold_limit,
        "cold_resident_ratio_limit": cold_limit,
        "first_pass": first, "residency_after_first": residency_after_first,
        "second_pass": second, "residency_after_second": residency_after_second,
        "warm_speedup": round(first["seconds"] / second["seconds"], 4) if second["seconds"] else None,
    }


def parse_file(value):
    label, separator, path = value.partition("=")
    if not separator or not label or not path or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in label):
        raise argparse.ArgumentTypeError("use LABEL=/absolute/path (LABEL contains letters, digits, - or _)")
    if not Path(path).is_absolute():
        raise argparse.ArgumentTypeError("measurement paths must be absolute")
    return label, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", action="append", required=True, type=parse_file,
                        help="repeat for q4, q5, q6, shortlist or model: LABEL=/absolute/path")
    parser.add_argument("--output", required=True, type=Path, help="new JSON evidence file")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--cold-resident-limit", type=float, default=DEFAULT_COLD_RESIDENT)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to replace {args.output}")
    if not 0 <= args.cold_resident_limit <= 1 or args.max_bytes <= 0:
        parser.error("limits must be positive and cold residency must be between 0 and 1")
    report = {
        "kind": KIND, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "argv": [Path(sys.argv[0]).name, *sys.argv[1:]],
        "system": {"kernel": platform.release(), "machine": platform.machine(),
                   "cpu": cpu_facts(), "qemu_fw_cfg": Path("/sys/firmware/qemu_fw_cfg").exists(),
                   "product": read("/sys/class/dmi/id/product_name"),
                   "source_id": read("/etc/being/source-id"), "build_id": read("/etc/being/build-id"),
                   "cmdline": read("/proc/cmdline")},
        "files": [], "finished_utc": None,
    }
    try:
        for label, path in args.file:
            report["files"].append(measure(label, path, max_bytes=args.max_bytes,
                                           cold_limit=args.cold_resident_limit))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    report["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
