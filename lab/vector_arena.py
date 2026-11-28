"""Build and inspect disposable WP-E9 packed vector arenas.

This prototype never reads SQLite or ``/mind``.  It accepts bounded JSONL
exported by a synthetic/qualified harness, writes a fixed little-endian arena,
and emits a fixed binary query for ``pipvec_scan.c``.  Both outputs are new
mode-0600 files.  Model, recipe, source and payload commitments are embedded
in the header; text is neither accepted nor stored.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
import tempfile


MAGIC = b"PIPVEC1\0"
QUERY_MAGIC = b"PIPQRY1\0"
VERSION = 1
HEADER = struct.Struct("<8sIIIIQ32s32s32s32s8s24x")
QUERY_HEADER = struct.Struct("<8sIIf")
RECORD = struct.Struct("<QBBHf")
SOURCE_FORMAT = "pipos-vector-arena-source-v1"
MAX_INPUT = 2 * 1024 * 1024 * 1024
MAX_ARENA = 2 * 1024 * 1024 * 1024
MAX_ROWS = 2_000_000
MAX_DIM = 4096
MAX_META = 256
TOMBSTONE = 1
SHARD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")


def _safe_path(value):
    path = Path(value)
    parent = path.parent.resolve(strict=True)
    if parent.is_relative_to("/mind") or not path.name or path.name == "..":
        raise ValueError("use a disposable lab path outside /mind")
    return parent / path.name


def _record_size(dimension):
    return (RECORD.size + (dimension + 7) // 8 + dimension + 63) & ~63


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _parent(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
    folder = os.open("/", flags)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=folder)
            os.close(folder)
            folder = child
        yield folder
    finally:
        os.close(folder)


def _digest_text(value, name):
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_META:
        raise ValueError(f"{name} is missing or too large")
    return hashlib.sha256(value.encode()).digest()


def _publish(path, blocks):
    path = _safe_path(path)
    scratch = f".pipvec-{secrets.token_hex(16)}"
    with _parent(path) as parent:
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("new arena outputs require an owner-only directory")
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600, dir_fd=parent)
        created = os.fstat(descriptor)
        try:
            for block in blocks:
                view = memoryview(block)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("arena output write stopped")
                    view = view[written:]
            os.fsync(descriptor)
            current = os.stat(scratch, dir_fd=parent, follow_symlinks=False)
            if (created.st_dev, created.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("the staging file was replaced")
            os.link(scratch, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(descriptor)
            try:
                current = os.stat(scratch, dir_fd=parent, follow_symlinks=False)
                if (created.st_dev, created.st_ino) == (current.st_dev, current.st_ino):
                    os.unlink(scratch, dir_fd=parent)
            except FileNotFoundError:
                pass


@contextmanager
def _source(path, maximum):
    path = _safe_path(path)
    with _parent(path) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=parent)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not 0 < opened.st_size <= maximum):
            raise ValueError("lab source must be a bounded regular file with one link")
        yield stream, opened.st_size
        if _identity(os.fstat(descriptor)) != _identity(opened):
            raise ValueError("lab source changed while reading")


def _hex(value, size, name):
    if not isinstance(value, str) or len(value) != size * 2:
        raise ValueError(f"{name} has the wrong length")
    try:
        result = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError(f"{name} is not hexadecimal") from error
    if len(result) != size:
        raise ValueError(f"{name} has the wrong length")
    return result


def _scale(value, *, tombstone=False):
    if type(value) not in (int, float) or value < 0 or (tombstone and value != 0):
        raise ValueError("vector scale is invalid")
    try:
        stored = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as error:
        raise ValueError("vector scale does not fit float32") from error
    if not math.isfinite(stored) or (not tombstone and stored <= 0):
        raise ValueError("active vector scale must be positive finite float32")
    return 0.0 if tombstone else stored


def _line(stream, remaining, maximum):
    data = stream.readline(min(remaining, maximum + 1))
    if remaining and (not data or len(data) > maximum):
        raise ValueError("arena input is truncated or has an oversized row")
    return data


def _pack_payload(source, payload):
    source_digest = hashlib.sha256()
    payload_digest = hashlib.sha256()
    count = 0
    with _source(source, MAX_INPUT) as (stream, size):
        first = _line(stream, size, 4096)
        source_digest.update(first)
        remaining = size - len(first)
        meta = json.loads(first)
        if (not isinstance(meta, dict) or set(meta) != {
                "format", "dimension", "shard", "model", "recipe", "source_sha256"}
                or meta["format"] != SOURCE_FORMAT or type(meta["dimension"]) is not int
                or not 1 <= meta["dimension"] <= MAX_DIM
                or not isinstance(meta["shard"], str) or not SHARD.fullmatch(meta["shard"])):
            raise ValueError("arena source metadata is invalid")
        source_commitment = _hex(meta["source_sha256"], 32, "source provenance digest")
        model = _digest_text(meta["model"], "model")
        recipe = _digest_text(meta["recipe"], "recipe")
        dimension = meta["dimension"]
        bit_bytes = (dimension + 7) // 8
        record_size = _record_size(dimension)
        used = RECORD.size + bit_bytes + dimension
        padding = b"\0" * (record_size - used)
        previous = (-1, -1)
        while remaining:
            if count >= MAX_ROWS or (count + 1) * record_size > MAX_ARENA - HEADER.size:
                raise ValueError("arena row or byte limit reached")
            line = _line(stream, remaining, 128 * 1024)
            source_digest.update(line)
            remaining -= len(line)
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != {"id", "kind", "scale", "q8", "bits", "tombstone"}:
                raise ValueError("arena row schema is invalid")
            row_id, kind, tombstone = row["id"], row["kind"], row["tombstone"]
            kind_code = 1 if kind == "fact" else 0
            if (type(row_id) is not int or not 0 <= row_id < 1 << 63
                    or kind not in ("episode", "fact") or (kind_code, row_id) <= previous
                    or type(tombstone) is not bool):
                raise ValueError("arena row identity, order or tombstone flag is invalid")
            previous = kind_code, row_id
            scale = _scale(row["scale"], tombstone=tombstone)
            bits = _hex(row["bits"], bit_bytes, "sign bits")
            q8 = _hex(row["q8"], dimension, "int8 vector")
            if dimension % 8 and bits[-1] >> (dimension % 8):
                raise ValueError("unused high sign bits must be zero")
            if tombstone and (any(bits) or any(q8)):
                raise ValueError("tombstone vectors must be completely zeroed")
            prefix = RECORD.pack(row_id, kind_code, TOMBSTONE if tombstone else 0, 0, scale)
            packed = prefix + bits + q8 + padding
            payload.write(packed)
            payload_digest.update(packed)
            count += 1
    if not count:
        raise ValueError("arena source contains no rows")
    bound_source = hashlib.sha256(b"pipos-vector-source-v1\0" + source_commitment
                                  + source_digest.digest()).digest()
    shard = meta["shard"]
    header = HEADER.pack(MAGIC, VERSION, dimension, bit_bytes, record_size, count, model, recipe,
                         bound_source, payload_digest.digest(), shard.encode("ascii") + b"\0")
    report = {"format": "pipos-vector-arena-v1", "rows": count, "dimension": dimension,
              "shard": shard, "record_size": record_size, "model_sha256": model.hex(),
              "recipe_sha256": recipe.hex(), "payload_sha256": payload_digest.hexdigest(),
              "source_file_sha256": source_digest.hexdigest(),
              "source_binding_sha256": bound_source.hex()}
    return header, report


def pack(source, output):
    with tempfile.TemporaryFile() as payload:
        header, report = _pack_payload(source, payload)
        payload.seek(0)

        def blocks():
            yield header
            yield from iter(lambda: payload.read(1 << 20), b"")

        _publish(output, blocks())
    return report


def inspect(path):
    with _source(path, MAX_ARENA) as (stream, size):
        raw = stream.read(HEADER.size)
        if len(raw) != HEADER.size:
            raise ValueError("arena header is truncated")
        magic, version, dim, bit_bytes, record_size, count, model, recipe, source, expected, shard = (
            HEADER.unpack(raw))
        shard_text = shard[:7].decode("ascii")
        if (magic != MAGIC or version != VERSION or not 1 <= dim <= MAX_DIM
                or bit_bytes != (dim + 7) // 8 or record_size != _record_size(dim)
                or not 1 <= count <= MAX_ROWS or size != HEADER.size + record_size * count
                or shard[7] or not SHARD.fullmatch(shard_text) or any(raw[-24:])):
            raise ValueError("arena header/layout is invalid")
        digest = hashlib.sha256()
        remaining = record_size * count
        while remaining:
            block = stream.read(min(1 << 20, remaining))
            if not block:
                raise ValueError("arena payload is truncated")
            digest.update(block)
            remaining -= len(block)
        if digest.digest() != expected:
            raise ValueError("arena payload commitment failed")
    return {"format": "pipos-vector-arena-v1", "rows": count, "dimension": dim,
            "shard": shard_text, "record_size": record_size, "model_sha256": model.hex(),
            "recipe_sha256": recipe.hex(), "source_sha256": source.hex(),
            "payload_sha256": expected.hex()}


def query(source, output):
    with _source(source, 1024 * 1024) as (stream, size):
        raw = stream.read(size)
        if len(raw) != size:
            raise ValueError("query source is truncated")
        value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"dimension", "scale", "q8", "bits"}
            or type(value["dimension"]) is not int or not 1 <= value["dimension"] <= MAX_DIM):
        raise ValueError("query schema is invalid")
    dim = value["dimension"]
    bits = _hex(value["bits"], (dim + 7) // 8, "query bits")
    q8 = _hex(value["q8"], dim, "query int8 vector")
    if dim % 8 and bits[-1] >> (dim % 8):
        raise ValueError("unused high query sign bits must be zero")
    data = QUERY_HEADER.pack(QUERY_MAGIC, VERSION, dim, _scale(value["scale"])) + bits + q8
    _publish(output, (data,))
    return {"format": "pipos-vector-query-v1", "dimension": dim,
            "sha256": hashlib.sha256(data).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pack", "query"):
        command = sub.add_parser(name)
        command.add_argument("source", type=Path)
        command.add_argument("output", type=Path)
    check = sub.add_parser("inspect")
    check.add_argument("arena", type=Path)
    args = parser.parse_args(argv)
    report = (pack(args.source, args.output) if args.command == "pack" else
              query(args.source, args.output) if args.command == "query" else inspect(args.arena))
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
