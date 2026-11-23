"""Record which model file was put in out/models/, with its checksum."""

import hashlib
import json
import os
import sys

source = sys.argv[1]
path = "out/models/reflex.gguf"
digest = hashlib.sha256()
with open(path, "rb") as f:
    for block in iter(lambda: f.read(1 << 20), b""):
        digest.update(block)
entry = {"file": "reflex.gguf", "from": os.path.basename(source), "bytes": os.path.getsize(path), "sha256": digest.hexdigest()}
with open("out/models/manifest.json", "w") as f:
    json.dump({"reflex": entry}, f, indent=2)
print(f"reflex model: {entry['from']} ({entry['bytes'] / 1e6:.0f} MB)")
