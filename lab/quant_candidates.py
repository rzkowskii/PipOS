"""Build the quant candidates bench mode compares, from the original weights.

    python3 -m lab.quant_candidates plan
        what would be built, from what, and where it goes (the default)
    python3 -m lab.quant_candidates fetch --model qwen3.5-0.8b --dest DIR [--revision REV]
        download a model's original files at a pinned revision, checking each
        large file against the SHA-256 the hub publishes (optional: needs the
        network, and is never run by the build)
    python3 -m lab.quant_candidates build --model qwen3.5-0.8b --hf-dir DIR --llama-cpp DIR
        convert the original weights to a 16-bit GGUF with llama.cpp's
        convert_hf_to_gguf.py, then quantize that to each candidate type with
        llama-quantize, into out/models/bench with a manifest.json
    python3 -m lab.quant_candidates manifest
        rewrite out/models/bench/manifest.json from the files present

The candidates: Q4_0, IQ4_NL and Q8_0 of Qwen3.5-0.8B (the waking model) and
Q4_0 of Qwen3-1.7B (the night model). These types have llama.cpp's AVX
tinyBLAS path on x86; Q4_K_M, the shipped type, does not, and its activation
packing runs scalar code without AVX2 (docs/PIP-FUNCTIONS.md 3.4).

Every candidate is quantized from a 16-bit conversion of the original
safetensors, never from an already quantized file: re-quantizing Q4_K_M would
stack two rounding errors and measure the wrong model. The build refuses any
input GGUF that is not F32, F16 or BF16, and never passes --allow-requantize.
The shipped default model is not changed by anything here; bench mode on the
laptop decides (`BEING_TRY_BENCH=1` makes the bench stick).
"""

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "out/models/bench"
WORK = REPO / "out/quant-work"
HUB = "https://huggingface.co"

SOURCES = {
    "qwen3.5-0.8b": {"repo": "Qwen/Qwen3.5-0.8B", "license": "Apache-2.0", "role": "conversation",
                     # The revision the shipped pack names (default-model/pack.json).
                     "revision": "2fc06364715b967f1860aea9cf38778875588b17"},
    "qwen3-1.7b": {"repo": "Qwen/Qwen3-1.7B", "license": "Apache-2.0", "role": "dream",
                   # Not pinned yet: fetch records the commit it resolved.
                   "revision": None},
}
CANDIDATES = (
    {"model": "qwen3.5-0.8b", "type": "Q4_0", "file": "qwen3.5-0.8b-q4_0.gguf", "name": "Qwen3.5-0.8B Q4_0"},
    {"model": "qwen3.5-0.8b", "type": "IQ4_NL", "file": "qwen3.5-0.8b-iq4_nl.gguf", "name": "Qwen3.5-0.8B IQ4_NL"},
    {"model": "qwen3.5-0.8b", "type": "Q8_0", "file": "qwen3.5-0.8b-q8_0.gguf", "name": "Qwen3.5-0.8B Q8_0"},
    {"model": "qwen3-1.7b", "type": "Q4_0", "file": "qwen3-1.7b-q4_0.gguf", "name": "Qwen3-1.7B Q4_0"},
)
# GGUF general.file_type values (llama_ftype) that are not quantized.
UNQUANTIZED = {0: "F32", 1: "F16", 32: "BF16"}
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 7: "Q8_0", 15: "Q4_K_M", 25: "IQ4_NL", 32: "BF16"}
# Files a conversion needs; nothing else is downloaded.
WANTED_SUFFIXES = (".safetensors", ".json", ".txt", ".jinja", ".model", ".tiktoken")
WANTED_NAMES = ("LICENSE", "LICENSE.txt", "README.md")
CHUNK = 1 << 20


# --- small helpers -------------------------------------------------------------

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def gguf_metadata(path, keys=("general.file_type", "general.architecture", "general.name")):
    """Selected scalar or string metadata from a GGUF header (standard library only)."""
    sizes = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
    found = {}
    with open(path, "rb") as f:
        def number(fmt):
            return struct.unpack("<" + fmt, f.read(struct.calcsize(fmt)))[0]

        def string():
            return f.read(number("Q")).decode("utf-8", "replace")

        def skip(kind):
            if kind == 8:
                f.seek(number("Q"), 1)
            elif kind == 9:
                inner, count = number("I"), number("Q")
                if inner in sizes:
                    f.seek(struct.calcsize(sizes[inner]) * count, 1)
                else:
                    for _ in range(count):
                        skip(inner)
            else:
                f.seek(struct.calcsize(sizes[kind]), 1)

        if f.read(4) != b"GGUF":
            raise ValueError(f"{path} is not a GGUF file")
        number("I")
        number("Q")
        for _ in range(number("Q")):
            key, kind = string(), number("I")
            if key in keys and kind != 9:
                found[key] = string() if kind == 8 else number(sizes[kind])
                if len(found) == len(keys):
                    break
            else:
                skip(kind)
    return found


def require_unquantized(path):
    """The 16-bit (or 32-bit) GGUF a candidate is quantized from; anything else is refused."""
    meta = gguf_metadata(path)
    file_type = meta.get("general.file_type")
    if file_type not in UNQUANTIZED:
        kind = FILE_TYPES.get(file_type, f"type {file_type}")
        raise ValueError(f"{Path(path).name} is {kind}: candidates are built only from F32, F16 or BF16 "
                         "conversions of the original weights, never from a quantized file")
    return UNQUANTIZED[file_type]


def candidates(model=None):
    return [entry for entry in CANDIDATES if model in (None, entry["model"])]


def plan(bench=BENCH):
    lines = []
    for model, source in SOURCES.items():
        revision = source["revision"] or "(resolved and recorded by fetch)"
        lines.append(f"{model}: {source['repo']} at {revision}, {source['license']}, for the {source['role']} role")
        lines.append("  original safetensors -> 16-bit GGUF (convert_hf_to_gguf.py --outtype bf16)")
        for entry in candidates(model):
            state = "present" if (bench / entry["file"]).is_file() else "to build"
            lines.append(f"  -> llama-quantize {entry['type']:6} -> {bench / entry['file']} ({state})")
    lines.append("The shipped reflex.gguf (Q4_K_M) is not an input and is not changed.")
    return lines


# --- fetch (optional, network) ---------------------------------------------------

def hub_json(url, opener=urllib.request.urlopen):
    with opener(urllib.request.Request(url, headers={"User-Agent": "pipos-lab"}), timeout=60) as response:
        return json.loads(response.read())


def fetch(model, dest, revision=None, *, opener=urllib.request.urlopen):
    """Download a model's conversion inputs at one commit. Returns the record written to dest/SOURCE.json."""
    source = SOURCES[model]
    revision = revision or source["revision"]
    if revision is None:
        info = hub_json(f"{HUB}/api/models/{source['repo']}", opener)
        revision = info["sha"]
    info = hub_json(f"{HUB}/api/models/{source['repo']}/revision/{revision}?blobs=true", opener)
    if info.get("sha") != revision and len(revision) == 40:
        raise ValueError(f"the hub answered for {info.get('sha')}, not {revision}")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for sibling in info.get("siblings", []):
        name = sibling["rfilename"]
        if "/" in name or name.startswith(".") or not (name.endswith(WANTED_SUFFIXES) or name in WANTED_NAMES):
            continue
        url = f"{HUB}/{source['repo']}/resolve/{info['sha']}/{urllib.parse.quote(name)}"
        target = dest / name
        temp = target.with_name("." + name + ".part")
        digest = hashlib.sha256()
        with opener(urllib.request.Request(url, headers={"User-Agent": "pipos-lab"}), timeout=600) as response, \
                open(temp, "wb") as out:
            for block in iter(lambda: response.read(CHUNK), b""):
                digest.update(block)
                out.write(block)
        expected = (sibling.get("lfs") or {}).get("sha256")
        if expected and digest.hexdigest() != expected:
            temp.unlink()
            raise ValueError(f"{name}: SHA-256 differs from the hub's record")
        os.replace(temp, target)
        files.append({"name": name, "bytes": target.stat().st_size, "sha256": digest.hexdigest(),
                      "hub_sha256": expected})
    if not any(entry["name"].endswith(".safetensors") for entry in files):
        raise ValueError("no safetensors weights at that revision")
    record = {"repo": source["repo"], "revision": info["sha"], "license": source["license"],
              "fetched": time.strftime("%Y-%m-%d", time.gmtime()), "files": files}
    (dest / "SOURCE.json").write_text(json.dumps(record, indent=1) + "\n")
    return record


# --- build -------------------------------------------------------------------------

def source_record(hf_dir):
    """Hashes of the original files a conversion reads (SOURCE.json from fetch, else computed)."""
    hf_dir = Path(hf_dir)
    if not (hf_dir / "config.json").is_file() or not list(hf_dir.glob("*.safetensors")):
        raise ValueError(f"{hf_dir} does not hold original weights (config.json and *.safetensors)")
    recorded = hf_dir / "SOURCE.json"
    if recorded.is_file():
        record = json.loads(recorded.read_text())
        for entry in record["files"]:
            if entry["name"].endswith(".safetensors") and sha256(hf_dir / entry["name"]) != entry["sha256"]:
                raise ValueError(f"{entry['name']} changed since it was fetched")
        return record
    return {"repo": None, "revision": None, "files": [
        {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(hf_dir.iterdir()) if path.is_file() and path.name.endswith(".safetensors")]}


def tool_version(llama_cpp):
    for name in ("VERSION", ".version"):
        path = Path(llama_cpp) / name
        if path.is_file():
            return path.read_text().strip()[:80]
    return Path(llama_cpp).resolve().name


def find_quantize(llama_cpp):
    for candidate in ("build/bin/llama-quantize", "bin/llama-quantize", "llama-quantize"):
        path = Path(llama_cpp) / candidate
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise ValueError(f"no llama-quantize under {llama_cpp} (build its llama-quantize target first)")


def build(model, hf_dir, llama_cpp, *, bench=BENCH, work=WORK, python=sys.executable, threads=None,
          imatrix=None, types=None, run=subprocess.run):
    """Convert once, quantize to each candidate type. Returns the new manifest entries."""
    if model not in SOURCES:
        raise ValueError(f"unknown model {model!r}; choose from {', '.join(SOURCES)}")
    record = source_record(hf_dir)
    convert = Path(llama_cpp) / "convert_hf_to_gguf.py"
    if not convert.is_file():
        raise ValueError(f"no convert_hf_to_gguf.py in {llama_cpp}")
    quantize = find_quantize(llama_cpp)
    work, bench = Path(work), Path(bench)
    work.mkdir(parents=True, exist_ok=True)
    bench.mkdir(parents=True, exist_ok=True)
    wide = work / f"{model}-bf16.gguf"
    if not wide.is_file():
        run([python, str(convert), str(hf_dir), "--outtype", "bf16", "--outfile", str(wide)], check=True)
    width = require_unquantized(wide)
    entries = []
    for entry in candidates(model):
        if types and entry["type"] not in types:
            continue
        output = bench / entry["file"]
        temp = output.with_name("." + output.name + ".part")
        command = [str(quantize)]
        if imatrix and entry["type"] == "IQ4_NL":
            command += ["--imatrix", str(imatrix)]
        command += [str(wide), str(temp), entry["type"]]
        if threads:
            command.append(str(threads))
        assert "--allow-requantize" not in command
        run(command, check=True)
        got = gguf_metadata(temp).get("general.file_type")
        if FILE_TYPES.get(got) != entry["type"]:
            temp.unlink(missing_ok=True)
            raise ValueError(f"llama-quantize wrote file type {got} for {entry['type']}")
        os.replace(temp, output)
        source = SOURCES[model]
        license_path = Path(hf_dir) / "LICENSE"
        entries.append({
            "file": entry["file"], "name": entry["name"], "quantization": entry["type"],
            "role": source["role"], "bytes": output.stat().st_size, "sha256": sha256(output),
            "built": time.strftime("%Y-%m-%d", time.gmtime()),
            "from": {"repo": record.get("repo") or source["repo"], "revision": record.get("revision"),
                     "weights": [f for f in record["files"] if f["name"].endswith(".safetensors")],
                     "intermediate": width},
            "license": source["license"],
            "license_sha256": sha256(license_path) if license_path.is_file() else None,
            "tool": {"llama.cpp": tool_version(llama_cpp), "imatrix": bool(imatrix and entry["type"] == "IQ4_NL")},
        })
    write_manifest(bench, entries)
    return entries


def write_manifest(bench=BENCH, new=()):
    """out/models/bench/manifest.json: every candidate present, newest record per file."""
    bench = Path(bench)
    path = bench / "manifest.json"
    known = {}
    if path.is_file():
        for entry in json.loads(path.read_text()).get("models", []):
            known[entry["file"]] = entry
    for entry in new:
        known[entry["file"]] = entry
    models = []
    for entry in CANDIDATES:
        file = bench / entry["file"]
        if not file.is_file():
            continue
        record = dict(known.get(entry["file"]) or {"file": entry["file"], "name": entry["name"],
                                                   "quantization": entry["type"]})
        size = file.stat().st_size
        if record.get("bytes") != size or "sha256" not in record:
            record.update(bytes=size, sha256=sha256(file))
        models.append(record)
    data = {"kind": "pipos-bench-models-v1",
            "note": "Quant candidates for bench mode (mind/bench.py); built by lab/quant_candidates.py.",
            "models": models}
    temp = path.with_name(".manifest.json.part")
    temp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(temp, path)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("plan")
    get = sub.add_parser("fetch")
    get.add_argument("--model", required=True, choices=sorted(SOURCES))
    get.add_argument("--dest", required=True, type=Path)
    get.add_argument("--revision")
    make = sub.add_parser("build")
    make.add_argument("--model", required=True, choices=sorted(SOURCES))
    make.add_argument("--hf-dir", required=True, type=Path, help="the original weights (from fetch)")
    make.add_argument("--llama-cpp", required=True, type=Path, help="llama.cpp source with llama-quantize built")
    make.add_argument("--bench", type=Path, default=BENCH)
    make.add_argument("--work", type=Path, default=WORK)
    make.add_argument("--threads", type=int)
    make.add_argument("--imatrix", type=Path, help="importance matrix for IQ4_NL (optional)")
    make.add_argument("--types", help="comma-separated subset, e.g. Q4_0,Q8_0")
    again = sub.add_parser("manifest")
    again.add_argument("--bench", type=Path, default=BENCH)
    args = parser.parse_args(argv)
    try:
        if args.action in (None, "plan"):
            print("\n".join(plan()))
        elif args.action == "fetch":
            record = fetch(args.model, args.dest, args.revision)
            print(f"{record['repo']} at {record['revision']}: {len(record['files'])} files")
        elif args.action == "build":
            types = set(args.types.split(",")) if args.types else None
            for entry in build(args.model, args.hf_dir, args.llama_cpp, bench=args.bench, work=args.work,
                               threads=args.threads, imatrix=args.imatrix, types=types):
                print(f"{entry['file']}: {entry['bytes']} bytes, {entry['sha256']}")
        else:
            print(f"{len(write_manifest(args.bench)['models'])} candidates in {args.bench / 'manifest.json'}")
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"quant candidates: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
