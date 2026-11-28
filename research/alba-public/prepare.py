"""Prepare only the pinned public Alba snapshot for a local Sherpa trial."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

MODEL = "en_GB-alba-medium.onnx"
CARD = MODEL + ".json"
REVISION = "cf910fb59f1d79ab3b625a63993a108751c6c25d"
SOURCE = f"https://huggingface.co/rhasspy/piper-voices/resolve/{REVISION}"
VOICE_DIR = "en/en_GB/alba/medium"
# Weights: upstream Git LFS SHA-256. Card and config: upstream Git blob IDs
# and staged SHA-256. The supporting README/base card pins cover local copies.
PINS = {
    MODEL: ("1f9da809b6da1ef4190bb5dd6cce4f4af803274539e9cf9e0693085150bac3d7",
            "e6e9032cbdfa5e92edf3dd6c89a7a259a3486610", f"{VOICE_DIR}/{MODEL}"),
    CARD: ("6214603a0dd60e9dcd05502e23ef07fbe3d9af36d3189c8cc3eb129e1a099d6f",
           "7edffe7c17705b5bf54d02ff7011c77b5ca807ba", f"{VOICE_DIR}/{CARD}"),
    "MODEL_CARD": ("0190e1016a03c46c23f8e920f395a83fc82705ed1b49ba1ded185d7d8357dd6a",
                   "d57e7484aae8d5a38149d6fc59df95d7dc784885", f"{VOICE_DIR}/MODEL_CARD"),
    "UPSTREAM-README.md": ("33e93643fce5180886300f9f0070eeeab8af148efb8e84361f8904def80fd9fb",
                          "e3c946b4ce55f9f4a614599d34e1bca4c4657d07", "README.md"),
    "BASE-VOICE-MODEL_CARD": ("62cafa98605100626d876aadcfd3dfa961cac263fa338b1fe17fe0989545e700",
                             "016d6253754956605be0cad5d4f5edc70215d223",
                             "en/en_US/hifi_tts_92/medium/MODEL_CARD"),
}
CLUSTER_IDS = {"aɪ": 161, "aʊ": 162, "ɔɪ": 163, "eɪ": 164, "oʊ": 165}
ALIASES = dict(zip(CLUSTER_IDS, map(chr, range(0xe000, 0xe000 + len(CLUSTER_IDS)))))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def blob(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def verify_source(source):
    files = {}
    for name, (expected_sha, expected_git, upstream_path) in PINS.items():
        path = source / name
        size = path.stat().st_size
        actual_sha = digest(path)
        if actual_sha != expected_sha:
            raise ValueError(f"{name}: SHA-256 mismatch")
        if name == MODEL:
            payload = ("version https://git-lfs.github.com/spec/v1\n"
                       f"oid sha256:{actual_sha}\nsize {size}\n").encode()
        else:
            payload = path.read_bytes()
        if blob(payload) != expected_git:
            raise ValueError(f"{name}: Git blob mismatch")
        files[name] = {"bytes": size, "sha256": actual_sha, "git_blob": expected_git,
                       "source": f"{SOURCE}/{upstream_path}",
                       "identity": ("staged notice" if name in ("UPSTREAM-README.md", "BASE-VOICE-MODEL_CARD")
                                    else "upstream Git/LFS identity")}
    return files


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def sized(field, data):
    if isinstance(data, str):
        data = data.encode()
    return varint((field << 3) | 2) + varint(len(data)) + data


def metadata_bytes(metadata):
    # ModelProto's repeated metadata_props entries. The pinned graph had no
    # metadata. Append fields while preserving all original graph bytes.
    return b"".join(sized(14, sized(1, key) + sized(2, value)) for key, value in metadata.items())


def compatibility(card):
    original = card["phoneme_id_map"]
    clusters = {"".join(phones) for phones in card["vowel_clusters"]}
    if (clusters != set(CLUSTER_IDS) or {token for token in original if len(token) > 1} != clusters
            or any(original[token] != [index] for token, index in CLUSTER_IDS.items())
            or set(original).intersection(ALIASES.values())):
        raise ValueError("unexpected Piper cluster map or alias collision")
    if any(len(ids) != 1 or type(ids[0]) is not int or "\n" in token for token, ids in original.items()):
        raise ValueError("Sherpa needs one integer ID per printable token row")
    tokens = sorted((ids[0], ALIASES.get(token, token)) for token, ids in original.items())
    if len({token for _, token in tokens}) != len(tokens) or [idx for idx, _ in tokens] != list(range(len(tokens))):
        raise ValueError("duplicate token or a missing ID")
    metadata = {
        "comment": "piper", "language": "English", "model_type": "vits", "voice": card["espeak"]["voice"],
        "n_speakers": str(card["num_speakers"]), "has_espeak": "1", "sample_rate": str(card["audio"]["sample_rate"]),
    }
    return tokens, metadata


def convert(source, target):
    verified = verify_source(source)
    card = json.loads((source / CARD).read_text(encoding="utf-8"))
    tokens, metadata = compatibility(card)
    aliases = {token: {"alias": alias, "id": CLUSTER_IDS[token]} for token, alias in ALIASES.items()}
    card["being_cluster_aliases"] = aliases
    target.mkdir()
    with (source / MODEL).open("rb") as raw, (target / MODEL).open("wb") as prepared:
        shutil.copyfileobj(raw, prepared)
        prepared.write(metadata_bytes(metadata))
    for name in ("MODEL_CARD", "UPSTREAM-README.md", "BASE-VOICE-MODEL_CARD"):
        shutil.copyfile(source / name, target / name)
    shutil.copyfile(source / CARD, target / (CARD + ".upstream"))
    (target / CARD).write_text(json.dumps(card, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (target / "tokens.txt").write_text("".join(f"{token} {idx}\n" for idx, token in tokens), encoding="utf-8")
    (target / "phoneme-aliases.json").write_text(
        json.dumps(aliases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("NOTICES.md"), target / "NOTICES.md")
    outputs = {path.name: digest(path) for path in sorted(target.iterdir())}
    report = {"revision": REVISION, "inputs": verified, "outputs": outputs, "metadata": metadata,
              "weight_or_graph_changes": False, "notice_status": "local source review only"}
    (target / "conversion.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="staged public Alba assets")
    parser.add_argument("target", type=Path, help="new directory for scratch conversion")
    args = parser.parse_args()
    report = convert(args.source, args.target)
    print(json.dumps(report["outputs"], indent=2))
