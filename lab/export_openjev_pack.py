"""Build a standalone native OpenJev model pack from the pinned academic checkpoint."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REVISION = "7418ae8b6e3b6a6507a8a58729ae491cd1c732d9"
LLAMA_TAG = "b12359"
UPSTREAM = "https://huggingface.co/alanhuangya/OpenJev-1.7B"
SOURCE_SHA256 = {
    "model.safetensors": "a753fd39bc3ff6c9a551cd43d0ae1f2f33207f1dd454ff5d1c028c69f34b5067",
    "head.safetensors": "51d5afaab983491be14e84686acb164185897376174b4d768202c57adcbe7fed",
    "tokenizer.json": "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
}
PACK_FILES = ("model.gguf", "head.safetensors", "tokenizer.json", "wev.json", "LICENSE", "UPSTREAM.md")


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_provenance(pack):
    files = {name: {"bytes": (pack / name).stat().st_size, "sha256": digest(pack / name)} for name in PACK_FILES}
    metadata = {
        "model": "academic OpenJev-1.7B, Q4_K_M backbone and original FP32 pointer head",
        "upstream": f"{UPSTREAM}/tree/{REVISION}",
        "upstream_revision": REVISION,
        "license": "Apache-2.0",
        "source_sha256": SOURCE_SHA256,
        "llama_cpp_tag": LLAMA_TAG,
        "files": files,
    }
    (pack / "PROVENANCE.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return sum(file["bytes"] for file in files.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="local upstream checkpoint directory")
    parser.add_argument("--llama-source", type=Path, required=True, help=f"llama.cpp {LLAMA_TAG} source directory")
    parser.add_argument("--quantize", type=Path, required=True, help=f"llama-quantize from llama.cpp {LLAMA_TAG}")
    parser.add_argument("--license", type=Path, required=True, help="Apache-2.0 license text")
    parser.add_argument("--model-card", type=Path, required=True, help="model card from the pinned revision")
    parser.add_argument("--out", type=Path, required=True, help="new model pack directory")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("choose a new output directory")
    for name, expected in SOURCE_SHA256.items():
        if digest(args.source / name) != expected:
            parser.error(f"{name} does not match the pinned OpenJev checkpoint")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openjev-export-", dir=args.out.parent) as folder:
        temporary = Path(folder)
        provisional = temporary / "pack"
        provisional.mkdir()
        intermediate = temporary / "backbone-bf16.gguf"
        subprocess.run(
            [sys.executable, args.llama_source / "convert_hf_to_gguf.py", args.source,
             "--model-name", "OpenJev-1.7B-native", "--outtype", "bf16", "--outfile", intermediate],
            env=dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false"),
            check=True, timeout=300,
        )
        subprocess.run(
            [args.quantize.resolve(), intermediate, provisional / "model.gguf", "Q4_K_M", "2"],
            check=True, timeout=300,
        )
        for name in ("head.safetensors", "tokenizer.json", "wev.json"):
            shutil.copyfile(args.source / name, provisional / name)
        shutil.copyfile(args.license, provisional / "LICENSE")
        shutil.copyfile(args.model_card, provisional / "UPSTREAM.md")
        size = write_provenance(provisional)
        provisional.rename(args.out)
    print(f"Wrote {size:,} bytes to {args.out}")


if __name__ == "__main__":
    main()
