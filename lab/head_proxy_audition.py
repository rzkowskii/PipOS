#!/usr/bin/env python3
"""Off-device WP-E16 signed-int2 output-head candidate-recall audition.

The input is a collector-produced, content-addressed witness.  This tool does
not generate samples, run a model, or change PipOS.  It deterministically
requantises the supplied full output head to group-scaled symmetric signed-int2,
scores every vocabulary row, and asks whether the unrestricted pre-sampler
top 20 are present in K=2,000 and K=4,000 proxy candidates.

The four codes {-3, -1, +1, +3} occupy two packed bits in a prospective
kernel. A 64-value group has one float32 scale. This deliberately reports the honest
~80 MB representation cost; it does not assume the handover's unproven 16 MB.
"""

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import mmap
import os
from pathlib import Path
import stat
import sys


VOCAB = 248_320
HIDDEN = 1_024
TOP = 20
GROUP = 64
KS = (2_000, 4_000)
MAX_STEPS = 2_048
MAX_MANIFEST = 64 * 1024
CHUNK_ROWS = 4_096
HEX = frozenset("0123456789abcdef")
FILES = ("head.f32le", "hidden.f32le", "truth_top20.u32le")


def digest(value):
    return isinstance(value, str) and len(value) == 64 and all(char in HEX for char in value)


def bounded_manifest(path):
    path = Path(path)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) \
            or not 1 <= before.st_size <= MAX_MANIFEST:
        raise ValueError("manifest must be a bounded regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns, item.st_ctime_ns)
        if identity(opened) != identity(before):
            raise ValueError("manifest changed while opening")
        raw = b""
        while len(raw) < opened.st_size:
            block = os.read(descriptor, opened.st_size - len(raw))
            if not block:
                raise ValueError("manifest changed while reading")
            raw += block
        if identity(os.fstat(descriptor)) != identity(opened):
            raise ValueError("manifest changed while reading")
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("manifest is not bounded JSON") from error
    finally:
        os.close(descriptor)


def validate_manifest(value):
    required = {"schema", "vocab", "width", "steps", "model_sha256", "head_tensor_sha256",
                "producer_sha256", "audition_sha256", "engine", "files"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value["schema"]) is not int or value["schema"] != 1):
        raise ValueError("unsupported manifest schema")
    if (type(value["vocab"]) is not int or value["vocab"] != VOCAB
            or type(value["width"]) is not int or value["width"] != HIDDEN):
        raise ValueError("manifest geometry is not the fixed WP-E16 geometry")
    steps = value["steps"]
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= MAX_STEPS:
        raise ValueError("witness step count is outside its bound")
    if (not all(digest(value[name]) for name in ("model_sha256", "head_tensor_sha256",
                                                  "producer_sha256", "audition_sha256"))
            or not isinstance(value["engine"], str) or not 1 <= len(value["engine"]) <= 160):
        raise ValueError("model/witness provenance is incomplete")
    files = value["files"]
    if not isinstance(files, dict) or not set(FILES).issubset(files) \
            or set(files) - set(FILES) - {"d5.u32le"}:
        raise ValueError("manifest file set is invalid")
    expected = {"head.f32le": VOCAB * HIDDEN * 4, "hidden.f32le": steps * HIDDEN * 4,
                "truth_top20.u32le": steps * TOP * 4}
    for name, size in expected.items():
        validate_file_entry(files[name], size)
    if "d5.u32le" in files:
        entry = files["d5.u32le"]
        if (not isinstance(entry, dict) or set(entry) != {"bytes", "sha256"}
                or not isinstance(entry["bytes"], int)
                or isinstance(entry["bytes"], bool) or not 4 <= entry["bytes"] <= VOCAB * 4
                or entry["bytes"] % 4 or not digest(entry["sha256"])):
            raise ValueError("D5 entry is invalid")
    return steps


def validate_file_entry(entry, size):
    if (not isinstance(entry, dict) or set(entry) != {"bytes", "sha256"}
            or type(entry["bytes"]) is not int or entry["bytes"] != size or not digest(entry["sha256"])):
        raise ValueError("witness file entry is invalid")


@contextmanager
def pinned_file(root, relative, entry):
    if Path(relative).is_absolute() or Path(relative).name != relative:
        raise ValueError("witness file name is not a fixed sibling")
    path = root / relative
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) \
            or before.st_size != entry["bytes"]:
        raise ValueError(f"{relative} is not the declared regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns, item.st_ctime_ns)
        if identity(opened) != identity(before):
            raise ValueError(f"{relative} changed while opening")
        measured = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(1 << 20, remaining))
            if not block:
                raise ValueError(f"{relative} ended while hashing")
            measured.update(block)
            remaining -= len(block)
        if measured.hexdigest() != entry["sha256"] or identity(os.fstat(descriptor)) != identity(opened):
            raise ValueError(f"{relative} failed its content/identity check")
        os.lseek(descriptor, 0, os.SEEK_SET)
        mapping = mmap.mmap(descriptor, opened.st_size, access=mmap.ACCESS_READ)
        try:
            yield mapping, opened
            if identity(os.fstat(descriptor)) != identity(opened):
                raise ValueError(f"{relative} changed during scoring")
        finally:
            try:
                mapping.close()
            except BufferError:
                # An exception may still own a diagnostic ndarray view. The
                # mapping is then reclaimed with that view; do not mask the
                # original validation/scoring failure during stack unwind.
                pass
    finally:
        os.close(descriptor)


def finite(array, label, np):
    if not bool(np.isfinite(array).all()):
        raise ValueError(f"{label} contains a non-finite value")


def quantize(group, np):
    scale = (np.max(np.abs(group), axis=1) / np.float32(3.0)).astype(np.float32, copy=False)
    safe = np.where(scale == 0, np.float32(1.0), scale)
    normal = group / safe[:, None]
    magnitude = np.where(np.abs(normal) < 2, 1, 3)
    values = np.where(normal < 0, -magnitude, magnitude)
    return values.astype(np.int8), scale


def merge_top(scores, ids, new_scores, first_id, keep, np):
    rows = new_scores.shape[0]
    new_ids = np.broadcast_to(np.arange(first_id, first_id + rows, dtype=np.uint32),
                              (new_scores.shape[1], rows))
    joined_scores = np.concatenate((scores, new_scores.T), axis=1)
    joined_ids = np.concatenate((ids, new_ids), axis=1)
    split = joined_scores.shape[1] - keep
    cutoffs = np.partition(joined_scores, split, axis=1)[:, split]
    selected_scores = np.empty_like(scores)
    selected_ids = np.empty_like(ids)
    for step, cutoff in enumerate(cutoffs):
        above = np.flatnonzero(joined_scores[step] > cutoff)
        equal = np.flatnonzero(joined_scores[step] == cutoff)
        space = keep - above.size
        if equal.size > space:
            tie_break = np.argpartition(joined_ids[step, equal], space - 1)[:space]
            equal = equal[tie_break]
        selected = np.concatenate((above, equal))
        values = joined_scores[step, selected]
        identifiers = joined_ids[step, selected]
        order = np.lexsort((identifiers, -values))
        selected_scores[step] = values[order]
        selected_ids[step] = identifiers[order]
    return selected_scores, selected_ids


def proxy_candidates(head, hidden, steps, np):
    keep = max(KS)
    best_scores = np.full((steps, keep), -np.inf, dtype=np.float32)
    best_ids = np.full((steps, keep), np.iinfo(np.uint32).max, dtype=np.uint32)
    for first in range(0, VOCAB, CHUNK_ROWS):
        last = min(first + CHUNK_ROWS, VOCAB)
        rows = head[first:last]
        finite(rows, "head chunk", np)
        scores = np.zeros((last - first, steps), dtype=np.float32)
        for column in range(0, HIDDEN, GROUP):
            codes, scales = quantize(rows[:, column:column + GROUP], np)
            scores += (codes.astype(np.float32) @ hidden[:, column:column + GROUP].T) * scales[:, None]
        finite(scores, "proxy scores", np)
        best_scores, best_ids = merge_top(best_scores, best_ids, scores, first, keep, np)
    return best_ids


def inclusion(candidate_ids, truth, k, np):
    chosen = candidate_ids[:, :k]
    present = np.empty((truth.shape[0], TOP), dtype=np.bool_)
    for step in range(truth.shape[0]):
        present[step] = np.isin(truth[step], chosen[step], assume_unique=True)
    counts = present.sum(axis=1)
    top_one = (truth[:, 0, None] == chosen).any(axis=1)
    return present, counts, top_one


def d5_report(values, truth, np):
    offsets = np.searchsorted(values, truth)
    valid = offsets < values.size
    present = np.zeros(truth.shape, dtype=np.bool_)
    present[valid] = values[offsets[valid]] == truth[valid]
    counts = present.sum(axis=1)
    return {"tokens": int(values.size), "token_ids_bytes": int(values.nbytes),
            "vocabulary_fraction": float(values.size / VOCAB),
            "top20_included": int(present.sum()),
            "top20_total": int(present.size), "recall": float(present.mean()),
            "top1_included": int(present[:, 0].sum()),
            "top1_total": int(truth.shape[0]),
            "per_step_included": [int(value) for value in counts]}


def run(manifest_path, qualifying_audition_sha256=None, qualifying_producer_sha256=None,
        qualifying_model_sha256=None):
    try:
        import numpy as np
    except ImportError as error:
        raise SystemExit("head proxy audition needs the off-device numpy package") from error
    manifest_path = Path(manifest_path).absolute()
    manifest = bounded_manifest(manifest_path)
    steps = validate_manifest(manifest)
    qualifying = {"audition_sha256": qualifying_audition_sha256,
                  "producer_sha256": qualifying_producer_sha256,
                  "model_sha256": qualifying_model_sha256}
    if any(qualifying.values()) and (not all(qualifying.values())
                                     or any(manifest[field] != value for field, value in qualifying.items())):
        raise ValueError("witness must match the declared held-out audition, reviewed collector and model")
    # At least 1,000 truth IDs are needed to resolve a one-in-a-thousand miss.
    qualified = qualifying_audition_sha256 is not None and steps * TOP >= 1000
    root = manifest_path.parent
    with ExitStack() as stack:
        opened = {name: stack.enter_context(pinned_file(root, name, entry))[0]
                  for name, entry in manifest["files"].items()}
        head = np.frombuffer(opened["head.f32le"], dtype="<f4").reshape(VOCAB, HIDDEN)
        hidden = np.frombuffer(opened["hidden.f32le"], dtype="<f4").reshape(steps, HIDDEN)
        truth = np.frombuffer(opened["truth_top20.u32le"], dtype="<u4").reshape(steps, TOP)
        finite(hidden, "hidden witnesses", np)
        if bool((truth >= VOCAB).any()) or any(len(set(map(int, row))) != TOP for row in truth):
            raise ValueError("unrestricted top-20 IDs are invalid or duplicated")
        candidates = proxy_candidates(head, hidden, steps, np)
        reports = {}
        for k in KS:
            present, counts, top_one = inclusion(candidates, truth, k, np)
            matched = int(present.sum())
            total = int(present.size)
            meets_recall = matched * 1000 >= total * 999
            reports[str(k)] = {"top20_included": matched,
                               "top20_total": total, "recall": matched / total,
                               "top1_included": int(top_one.sum()), "top1_total": steps,
                               "per_step_included": [int(value) for value in counts],
                               "per_step_missing": [[int(truth[s, i]) for i in range(TOP)
                                                     if not present[s, i]] for s in range(steps)],
                               "gate_99_9": ("met" if meets_recall else "kill") if qualified
                               else "unqualified-witness"}
        d5 = None
        if "d5.u32le" in manifest["files"]:
            values = np.frombuffer(opened["d5.u32le"], dtype="<u4")
            if (bool((values >= VOCAB).any()) or values.size != np.unique(values).size
                    or not bool((values[:-1] < values[1:]).all())):
                raise ValueError("D5 token list must be sorted, unique and in range")
            d5 = d5_report(values, truth, np)
            del values
        del candidates, truth, hidden, head
    groups = HIDDEN // GROUP
    provenance = {name: manifest[name] for name in ("model_sha256", "head_tensor_sha256",
                                                     "producer_sha256", "audition_sha256", "engine")}
    qualifying_widths = [width for width in KS if reports[str(width)]["gate_99_9"] == "met"]
    return {"schema": 1, "method": {"kind": "symmetric-sint2", "codes": [-3, -1, 1, 3],
                                     "group": GROUP, "ties": "away-from-zero",
                                     "scale": "max-abs/3-f32le"},
            "provenance": provenance, "steps": steps, "candidate_recall": reports,
            "d5_control": d5,
            "d5_sufficiency": "requires separate reply and native paired timing evidence",
            "recall_qualifying_widths": qualifying_widths,
            "raw_cost": {"packed_weight_bytes": VOCAB * HIDDEN // 4,
                         "group_scale_bytes": VOCAB * groups * 4,
                         "proxy_total_bytes": VOCAB * HIDDEN // 4 + VOCAB * groups * 4,
                         "proxy_multiply_accumulates_per_step": VOCAB * HIDDEN,
                         "full_head_input_bytes": VOCAB * HIDDEN * 4,
                         "candidate_exact_rows_per_step": list(KS)},
            "decision": ("research-only: provide a qualifying held-out witness" if not qualified else
                         "candidate-widths-found: fixed-list sufficiency and native costs remain open"
                         if qualifying_widths else "kill-proxy; true-top20 recall below 99.9%")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="collector-produced strict witness manifest")
    parser.add_argument("--qualifying-audition-sha256",
                        help="independently selected held-out audition TSV SHA-256")
    parser.add_argument("--qualifying-producer-sha256",
                        help="independently built and reviewed collector executable SHA-256")
    parser.add_argument("--qualifying-model-sha256",
                        help="model SHA-256 from the independently approved reflex descriptor")
    args = parser.parse_args(argv)
    try:
        supplied = (args.qualifying_audition_sha256, args.qualifying_producer_sha256,
                    args.qualifying_model_sha256)
        if any(value is not None and not digest(value) for value in supplied):
            raise ValueError("qualifying digests must be lowercase SHA-256")
        report = run(args.manifest, *supplied)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
