"""Collect and seal an off-device WP-E16 Qwen3.5 full-head witness.

Use a separately built head_witness.cpp executable and the exact pinned
llama.cpp libraries. Audition TSV supplies synthetic cases only: opaque ID,
space-separated complete prompt token IDs, and a fixed continuation token
sequence, separated by tabs. Every continuation position is scored using the
unrestricted head before sampling. Successful output contains arrays and
commitments, without prompt or continuation text.
"""

import argparse
from contextlib import contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile


VOCAB = 248_320
WIDTH = 1024
TOP = 20
MAX_STEPS = 2048
MAX_MODEL = 2 * 1024**3
MAX_CASES = 40 * 1024**2
MAX_COLLECTOR = 256 * 1024**2
HEX = re.compile(r"[0-9a-f]{64}")
ENGINE = "local-collector-digest-qwen35-cpu-pre-sampler"


def digest_argument(value):
    if not HEX.fullmatch(value):
        raise argparse.ArgumentTypeError("supply an exact lowercase SHA-256")
    return value


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def lab_path(path):
    path = Path(path)
    resolved = path.parent.resolve(strict=True) / path.name
    if (not path.name or path.name == ".." or path.is_relative_to("/mind")
            or resolved.is_relative_to("/mind")):
        raise ValueError("use an off-device scratch path outside /mind")
    return resolved


@contextmanager
def regular_input(path, maximum):
    path = lab_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= maximum:
            raise ValueError(f"expected a bounded regular file: {path.name}")
        yield fd, initial
        if identity(os.fstat(fd)) != identity(initial) or identity(path.lstat()) != identity(initial):
            raise ValueError(f"input changed during witness collection: {path.name}")
    finally:
        os.close(fd)


def region_digest(fd, offset, size):
    digest = hashlib.sha256()
    end = offset + size
    while offset < end:
        data = os.pread(fd, min(1 << 20, end - offset), offset)
        if not data:
            raise ValueError("witness input ended while hashing")
        digest.update(data)
        offset += len(data)
    return digest.hexdigest()


def require_digest(source, expected, label):
    fd, info = source
    actual = region_digest(fd, 0, info.st_size)
    if actual != expected:
        raise ValueError(f"{label} SHA-256 differs from the pinned input")
    return actual


def receipt(stage):
    path = stage / "native-witness.json"
    with regular_input(path, 4096) as (fd, info):
        raw = os.pread(fd, info.st_size, 0)
    result = json.loads(raw)
    if (not isinstance(result, dict) or set(result) != {"schema", "steps", "head_offset", "head_bytes"}
            or any(type(value) is not int for value in result.values())
            or result["schema"] != 1 or not 1 <= result["steps"] <= MAX_STEPS
            or result["head_offset"] <= 0):
        raise ValueError("witness collector returned an invalid receipt")
    # Four 256-weight Q6_K blocks per row, each 210 bytes in the pinned format.
    if result["head_bytes"] != VOCAB * (WIDTH // 256) * 210:
        raise ValueError("witness collector did not use the pinned Q6_K head")
    return result


def write_d5(path, stage):
    with regular_input(path, 4 * 1024**2) as (fd, info):
        raw = os.pread(fd, info.st_size, 0)
        if len(raw) != info.st_size:
            raise ValueError("fixed-list input changed while reading")
    identifiers = set()
    for line in raw.splitlines():
        if not 1 <= len(line) <= 6 or not line.isdigit():
            raise ValueError("fixed shortlist must contain one vocabulary ID per line")
        token = int(line)
        if not 0 <= token < VOCAB or token in identifiers:
            raise ValueError("fixed shortlist contains an invalid or repeated ID")
        identifiers.add(token)
    if not identifiers:
        raise ValueError("fixed shortlist is empty")
    return new_file(stage / "d5.u32le", struct.pack(f"<{len(identifiers)}I", *sorted(identifiers)))


def new_file(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return len(data)


def seal_artifacts(stage, sizes):
    files = {}
    for name, expected in sizes.items():
        with regular_input(stage / name, expected) as (fd, info):
            if info.st_size != expected:
                raise ValueError(f"native witness size differs for {name}")
            files[name] = {"bytes": expected, "sha256": region_digest(fd, 0, expected)}
    return files


def publish(stage, target, names):
    target.mkdir(mode=0o700)
    directory = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    placed = {}
    try:
        for name in names:
            source = stage / name
            source_info = source.lstat()
            os.link(source, name, dst_dir_fd=directory, follow_symlinks=False)
            placed[name] = source_info.st_dev, source_info.st_ino
        os.fsync(directory)
    except BaseException:
        for name, expected in placed.items():
            with suppress(OSError):
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == expected:
                    os.unlink(name, dir_fd=directory)
        with suppress(OSError):
            target.rmdir()
        raise
    finally:
        os.close(directory)


def collect(args):
    target = lab_path(args.output)
    if target.exists() or target.is_symlink():
        raise ValueError("choose a new witness output directory")
    with tempfile.TemporaryDirectory(prefix=".head-witness-", dir=target.parent) as work:
        stage = Path(work)
        with (regular_input(args.model, MAX_MODEL) as model,
              regular_input(args.cases, MAX_CASES) as cases,
              regular_input(args.collector, MAX_COLLECTOR) as collector):
            model_hash = require_digest(model, args.model_sha256, "model")
            audition_hash = require_digest(cases, args.audition_sha256, "audition")
            collector_hash = require_digest(collector, args.producer_sha256, "collector")
            if not collector[1].st_mode & stat.S_IXUSR:
                raise ValueError("native collector must be a separately built executable")
            if Path("/proc/self/fd").is_dir():
                fd_path = "/proc/self/fd"
                executable = f"{fd_path}/{collector[0]}"
            else:
                fd_path = "/dev/fd"
                executable = os.fspath(lab_path(args.collector))
            subprocess.run([executable, f"{fd_path}/{model[0]}", f"{fd_path}/{cases[0]}", os.fspath(stage)],
                           cwd=stage, env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
                           pass_fds=(model[0], cases[0], collector[0]), stdout=subprocess.DEVNULL,
                           check=True, timeout=args.seconds)
            result = receipt(stage)
            offset, size = result["head_offset"], result["head_bytes"]
            if offset + size > model[1].st_size:
                raise ValueError("tied-head receipt points outside the GGUF")
            tensor_hash = region_digest(model[0], offset, size)
        sizes = {"head.f32le": VOCAB * WIDTH * 4,
                 "hidden.f32le": result["steps"] * WIDTH * 4,
                 "truth_top20.u32le": result["steps"] * TOP * 4}
        if args.d5_ids is not None:
            sizes["d5.u32le"] = write_d5(args.d5_ids, stage)
        manifest = {"schema": 1, "vocab": VOCAB, "width": WIDTH, "steps": result["steps"],
                    "model_sha256": model_hash, "head_tensor_sha256": tensor_hash,
                    "producer_sha256": collector_hash, "audition_sha256": audition_hash,
                    "engine": ENGINE, "files": seal_artifacts(stage, sizes)}
        new_file(stage / "manifest.json",
                 (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))
        publish(stage, target, (*sizes, "manifest.json"))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collector", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("cases", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--producer-sha256", required=True, type=digest_argument)
    parser.add_argument("--model-sha256", required=True, type=digest_argument)
    parser.add_argument("--audition-sha256", required=True, type=digest_argument)
    parser.add_argument("--d5-ids", type=Path)
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("collector timeout must be between one second and one hour")
    try:
        print(collect(args) / "manifest.json")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
