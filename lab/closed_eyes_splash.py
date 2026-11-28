#!/usr/bin/env python3
"""Prepare and compare the canonical production closed-eyes boot frame."""

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import time
import zlib
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n"
SIZE = (1280, 800)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _png(data):
    if not data.startswith(PNG):
        raise ValueError("not a PNG")
    at, header, packed = len(PNG), None, bytearray()
    while at + 12 <= len(data):
        length = int.from_bytes(data[at:at + 4], "big")
        kind, body = data[at + 4:at + 8], data[at + 8:at + 8 + length]
        if at + 12 + length > len(data):
            raise ValueError("truncated PNG chunk")
        expected = int.from_bytes(data[at + 8 + length:at + 12 + length], "big")
        if zlib.crc32(kind + body) != expected:
            raise ValueError("PNG chunk checksum failed")
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            packed.extend(body)
        elif kind == b"IEND":
            break
        at += 12 + length
    if header is None:
        raise ValueError("PNG has no header")
    width, height, depth, color, compression, filtering, interlace = header
    if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
        raise ValueError("only non-interlaced 8-bit RGB/RGBA PNG is supported")
    channels = 3 if color == 2 else 4
    raw, stride = zlib.decompress(bytes(packed)), width * channels
    if len(raw) != height * (stride + 1):
        raise ValueError("PNG pixel data has the wrong size")
    rows, previous, offset = [], bytearray(stride), 0
    for _ in range(height):
        method, encoded = raw[offset], bytearray(raw[offset + 1:offset + 1 + stride])
        offset += stride + 1
        for index, value in enumerate(encoded):
            left = encoded[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if method == 1:
                encoded[index] = (value + left) & 255
            elif method == 2:
                encoded[index] = (value + up) & 255
            elif method == 3:
                encoded[index] = (value + ((left + up) // 2)) & 255
            elif method == 4:
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                encoded[index] = (value + (left if pa <= pb and pa <= pc else up if pb <= pc else upper_left)) & 255
            elif method != 0:
                raise ValueError(f"unsupported PNG filter {method}")
        rows.append(bytes(encoded))
        previous = encoded
    rgb = b"".join(row if channels == 3 else b"".join(row[x:x + 3] for x in range(0, len(row), 4))
                   for row in rows)
    return width, height, rgb


def _ppm(data):
    match = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\s", data)
    if not match:
        raise ValueError("not a binary RGB PPM")
    width, height = int(match[1]), int(match[2])
    rgb = data[match.end():]
    if len(rgb) != width * height * 3:
        raise ValueError("PPM pixel data has the wrong size")
    return width, height, rgb


def image(path):
    data = Path(path).read_bytes()
    try:
        return _png(data) if data.startswith(PNG) else _ppm(data)
    except (ValueError, zlib.error, struct.error) as error:
        raise ValueError(f"{path}: {error}") from None


def difference(reference, observed, threshold):
    rw, rh, before = reference
    ow, oh, after = observed
    if (rw, rh) != (ow, oh):
        raise ValueError(f"image sizes differ: {rw}x{rh} and {ow}x{oh}")
    changed = total_error = maximum = 0
    box = None
    for pixel in range(rw * rh):
        at = pixel * 3
        error = max(abs(before[at + channel] - after[at + channel]) for channel in range(3))
        total_error += sum(abs(before[at + channel] - after[at + channel]) for channel in range(3))
        maximum = max(maximum, error)
        if error > threshold:
            changed += 1
            x, y = pixel % rw, pixel // rw
            box = [x, y, x, y] if box is None else [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
    pixels = rw * rh
    return {"width": rw, "height": rh, "threshold": threshold, "changed_pixels": changed,
            "changed_fraction": round(changed / pixels, 9), "maximum_channel_delta": maximum,
            "mean_channel_delta": round(total_error / (pixels * 3), 6), "changed_box": box,
            "exact": before == after}


def validate_manifest(path, canonical=None, face=None):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("kind") != "pipos-closed-eyes-splash-v1" \
            or value.get("canonical_expression") != "asleep" or value.get("size") != list(SIZE):
        raise ValueError(f"{path}: not a canonical closed-eyes manifest")
    checks = {}
    for name, supplied, expected in (("png", canonical, value.get("png_sha256")),
                                     ("face", face, value.get("face_sha256"))):
        if supplied is not None:
            actual = sha256(supplied)
            if not isinstance(expected, str) or actual != expected:
                raise ValueError(f"{name} differs from {path}")
            checks[name] = {"path": str(supplied), "sha256": actual, "matches": True}
    return {"path": str(path), "manifest_sha256": sha256(path), "checks": checks}


def prepare(args):
    face, output, manifest = args.face.resolve(strict=True), args.output, args.manifest
    for path in (output, manifest):
        if path.exists():
            raise ValueError(f"refusing to replace {path}")
    command = [str(face), "--snapshot", str(output), "--expr", "asleep", "--size", "1280x800", "--seed", "1"]
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True, timeout=args.timeout, env={**os.environ, "BEING_FACE_EFFECTS": "0"})
    width, height, _ = image(output)
    if (width, height) != SIZE:
        raise ValueError(f"renderer wrote {width}x{height}, expected 1280x800")
    record = {"kind": "pipos-closed-eyes-splash-v1", "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "canonical_expression": "asleep", "size": list(SIZE), "seed": 1,
              "face": str(face), "face_sha256": sha256(face), "png": str(output), "png_sha256": sha256(output),
              "command": command, "production_path": "being-face --snapshot (production shader/catalogue/layout)"}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def compare(args):
    first, last = image(args.first), image(args.last)
    report = {"kind": "pipos-closed-eyes-pixel-compare-v1",
              "first": {"path": str(args.first), "sha256": sha256(args.first)},
              "last": {"path": str(args.last), "sha256": sha256(args.last)},
              "first_to_last": difference(first, last, args.threshold)}
    if args.canonical:
        canonical = image(args.canonical)
        report["canonical"] = {"path": str(args.canonical), "sha256": sha256(args.canonical)}
        report["canonical_to_first"] = difference(canonical, first, args.threshold)
        report["canonical_to_last"] = difference(canonical, last, args.threshold)
    if args.manifest:
        report["build_validation"] = validate_manifest(args.manifest, args.canonical, args.face)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        if args.output.exists():
            raise ValueError(f"refusing to replace {args.output}")
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare", help="render the canonical frame through production Face")
    make.add_argument("--face", type=Path, required=True, help="exact being-face binary to validate")
    make.add_argument("--output", type=Path, required=True, help="new canonical PNG")
    make.add_argument("--manifest", type=Path, required=True, help="new provenance JSON")
    make.add_argument("--timeout", type=float, default=60)
    make.set_defaults(run=prepare)
    check = commands.add_parser("compare", help="compare captured first and last pixels")
    check.add_argument("--first", type=Path, required=True)
    check.add_argument("--last", type=Path, required=True)
    check.add_argument("--canonical", type=Path)
    check.add_argument("--manifest", type=Path, help="validate canonical/renderer hashes from prepare")
    check.add_argument("--face", type=Path, help="renderer binary to validate (requires --manifest)")
    check.add_argument("--threshold", type=int, choices=range(256), default=0,
                       help="per-channel delta ignored for changed-pixel counts (default exact)")
    check.add_argument("--output", type=Path)
    check.set_defaults(run=compare)
    args = parser.parse_args(argv)
    if args.command == "compare" and args.face and not args.manifest:
        parser.error("--face requires --manifest")
    if args.command == "compare" and args.manifest and not args.canonical:
        parser.error("--manifest requires --canonical")
    try:
        args.run(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
