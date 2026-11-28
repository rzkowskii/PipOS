"""Compare the models actually packaged on factory and Try disks with frozen inputs."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from lab.candidate_manifest import fingerprint, read_json, regular
from lab.hardware_image import copy_partition, entries_for, member, member_fingerprint
from mind.model_pack import ENVELOPE, MAX_METADATA, WEIGHTS


MANIFEST = "manifest.json"


def digest(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def expected_model(source):
    if source["kind"] != "beingos-source-v1":
        raise ValueError("expected a frozen PipOS source snapshot")
    model = source["model"]
    files = source["files"]
    weights = files[f"out/models/{WEIGHTS}"]
    if (model["file"] != WEIGHTS or not isinstance(model["from"], str) or not model["from"]
            or type(model["bytes"]) is not int or model["bytes"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", model["sha256"])
            or any(model[key] != weights[key] for key in ("bytes", "sha256"))):
        raise ValueError("frozen model identity is inconsistent with the frozen GGUF")
    manifest = files["out/models/" + MANIFEST]
    return model, {key: manifest[key] for key in ("bytes", "sha256")}


def regular_member(entries, name):
    matches = [entry for entry in entries if entry["Path"] == name]
    if (len(matches) != 1 or not matches[0].get("Mode", "").startswith("-")
            or matches[0].get("Symbolic Link")):
        raise ValueError(f"expected one regular {name} on the model partition")
    return int(matches[0]["Size"])


def metadata(tool, partition, entries, name):
    size = regular_member(entries, name)
    if not 0 < size <= MAX_METADATA:
        raise ValueError(f"missing or oversized {name}")
    data = member(tool, "Ext", partition, name)
    if len(data) != size:
        raise ValueError(f"short or oversized extraction of {name}")
    return data


def inspect(tool, image, label, model, expected_manifest):
    with regular(image) as (stream, info), tempfile.TemporaryDirectory(prefix="being-model-audit-") as folder:
        partitions = [part for part in entries_for(tool, "GPT", image)
                      if re.fullmatch(rf"[0-9]+\.{label}\.img", part["Path"])]
        if len(partitions) != 1:
            raise ValueError(f"{image} must contain exactly one GPT partition named {label}")
        part = partitions[0]
        filesystem = Path(folder) / "models.ext4"
        partition_sha = copy_partition(image, part, filesystem)
        entries = entries_for(tool, "Ext", filesystem)
        legacy = metadata(tool, filesystem, entries, MANIFEST)
        if digest(legacy) != expected_manifest or json.loads(legacy) != {"reflex": model}:
            raise ValueError(f"{image}: packaged manifest differs from the frozen metadata")
        if regular_member(entries, WEIGHTS) != model["bytes"]:
            raise ValueError(f"{image}: packaged GGUF size differs from frozen bytes")
        weights = member_fingerprint(tool, "Ext", filesystem, WEIGHTS, model["bytes"], seconds=180)
        if weights["sha256"] != model["sha256"]:
            raise ValueError(f"{image}: packaged GGUF digest differs from frozen bytes")
        envelope = metadata(tool, filesystem, entries, ENVELOPE)
        return {
            "image": str(image), "image_sha256": fingerprint(stream, info.st_size)["sha256"],
            "partition": label, "partition_sha256": partition_sha, "weights": weights,
            "manifest": digest(legacy), "signed_envelope": digest(envelope),
        }


def compare(source, factory, trial, tool):
    model, legacy = expected_model(source)
    if factory.samefile(trial):
        raise ValueError("factory and Try must be distinct images")
    records = {role: inspect(tool, image, label, model, legacy)
               for role, image, label in (("factory", factory, "models"), ("try_image", trial, "try-models"))}
    if records["factory"]["signed_envelope"] != records["try_image"]["signed_envelope"]:
        raise ValueError("factory and Try contain different signed model envelopes")
    return {"source_sha256": source["sha256"], "model": model, "media": records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--factory", type=Path, required=True)
    parser.add_argument("--try-image", type=Path, required=True)
    args = parser.parse_args(argv)
    tool = shutil.which("7zz") or shutil.which("7z")
    if not tool:
        parser.error("7-Zip with GPT and Ext support is required")
    try:
        result = compare(read_json(args.source), args.factory.absolute(), args.try_image.absolute(), tool)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Candidate model check failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
