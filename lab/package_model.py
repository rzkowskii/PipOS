"""Build signed default-pack media from independently pinned weights in a fresh staging directory."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from mind.model_pack import ENVELOPE, WEIGHTS, check_weights, metadata_bytes, openssl, verify


DEFAULT_PACK = Path(__file__).resolve().parents[1] / "os/package/being/default-model"


def stage(source, output, descriptor, certificate, key):
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError("model staging directory must be empty")
    output.mkdir(exist_ok=True)
    info = metadata_bytes(descriptor)
    pack = json.loads(info)
    if hashlib.sha256(metadata_bytes(descriptor.with_name("LICENSE"))).hexdigest() != pack["license_sha256"]:
        raise ValueError("pack licence text differs from its qualified SHA-256")
    check_weights(source, pack)
    shutil.copyfile(source / WEIGHTS, output / WEIGHTS)
    envelope = openssl("cms", "-sign", "-binary", "-nodetach", "-nocerts", "-noattr", "-md", "sha256",
                       "-outform", "DER", "-signer", str(certificate), "-inkey", str(key), data=info)
    (output / ENVELOPE).write_bytes(envelope)
    # Keep working readers of historical inventory metadata on the same model.
    # Runtime trust comes from the authenticated CMS, independently of this file.
    legacy = metadata_bytes(source / "manifest.json")
    inventory = json.loads(legacy)["reflex"]
    if any(inventory[field] != pack[field] for field in ("file", "bytes", "sha256")):
        raise ValueError("staged lab model inventory conflicts with the qualified default pack")
    (output / "manifest.json").write_bytes(legacy)
    verify(output, output / ENVELOPE, certificate, descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--certificate", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    args = parser.parse_args()
    try:
        stage(args.source, args.output, DEFAULT_PACK / "pack.json", args.certificate, args.key)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Could not package default model: {error}\n")


if __name__ == "__main__":
    main()
