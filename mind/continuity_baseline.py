"""Compare the installed archive with the last clean owner checkpoint."""

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .backup import DB_FILE, RESTORE_MARKER, life_record, regular_file
from .memory import archive_schema
from .model_pack import ENVELOPE, MODELS, metadata_bytes
from .storage import atomic_write

NAME = "continuity.json"
FORMAT = 1
MAX_RECEIPT = 4096
_WRITE_LOCK = threading.Lock()


@contextmanager
def _archive(root, budget):
    path = root / DB_FILE
    if path.is_symlink() or not path.is_file() or (root / RESTORE_MARKER).exists():
        raise FileNotFoundError("archive unavailable")
    deadline = time.monotonic() + budget
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.15)) as db:
        db.set_progress_handler(lambda: time.monotonic() >= deadline, 2048)
        db.execute("BEGIN")
        yield db


def _meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _counts(db):
    return {
        "episodes": db.execute(
            "SELECT count(*) FROM episodes WHERE text NOT IN ('', '[forgotten]')").fetchone()[0],
        "facts": db.execute("SELECT count(*) FROM facts").fetchone()[0],
    }


def _digest(path):
    try:
        return hashlib.sha256(metadata_bytes(path)).hexdigest()
    except (OSError, ValueError):
        return None


def _receipt(root, etc, share, models, *, identity_salt):
    identity = _digest(etc / "identity.md"), _digest(root / "profile.json")
    saved = json.dumps(identity, separators=(",", ":")).encode()
    return {
        "identity": hmac.digest(identity_salt, saved, "sha256").hex(),
        "model": _digest(share / "default-model/pack.json"),
        "installed_model": _digest(models / ENVELOPE),
        "build": _digest(etc / "build-id"),
    }


def _fact_hash(salt, text):
    return hmac.digest(salt, text.encode(), "sha256").hex()


def write(root, *, etc=Path("/etc/being"), share=Path("/usr/share/being"), models=MODELS, budget=0.6):
    """Read a stable database snapshot. The private receipt has no excerpts."""
    root = Path(root)
    # A slow shutdown snapshot must not overwrite one made after a forget.
    if not _WRITE_LOCK.acquire(timeout=budget):
        return False
    try:
        salt = secrets.token_bytes(32)
        life = life_record(root)["id"]
        with _archive(root, budget) as db:
            lineage = _meta(db, "deletion_lineage")
            schema = archive_schema(db)
            if lineage is None or schema is None:
                return False
            last = db.execute("SELECT seq, nonce FROM deletion_edits ORDER BY seq DESC LIMIT 1").fetchone()
            rows = db.execute("SELECT deletion_ref, text FROM facts ORDER BY id DESC LIMIT 3").fetchall()
            record = {
                "version": FORMAT, "life": life, "schema": schema, "lineage": lineage,
                "revision": int(_meta(db, "memory_revision", 0)), "counts": _counts(db),
                "proof": last or (0, None), "salt": salt.hex(),
                "witnesses": {ref: _fact_hash(salt, text) for ref, text in rows},
                **_receipt(root, etc, share, models, identity_salt=salt),
            }
        atomic_write(root / NAME, json.dumps(record, sort_keys=True) + "\n")
    finally:
        _WRITE_LOCK.release()
    return True


def _read(root):
    with regular_file(root / NAME) as stream:
        raw = stream.read(MAX_RECEIPT + 1)
    if len(raw) > MAX_RECEIPT:
        raise ValueError("oversized continuity receipt")
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get("version") != FORMAT:
        raise ValueError("unsupported continuity receipt")
    return record


def _edition_count(db, kind, after):
    # Called only with one of the archive's fixed deletion-reference columns.
    return db.execute(
        f"SELECT count(DISTINCT edit.value) FROM deletion_edits d, json_each(d.{kind}) edit WHERE d.seq > ?",
        (after,)).fetchone()[0]


def _compare_facts(db, witnesses, salt, seq):
    if not isinstance(witnesses, dict) or len(witnesses) > 3:
        raise ValueError("invalid continuity witnesses")
    for ref, witness in witnesses.items():
        current = db.execute("SELECT text FROM facts WHERE deletion_ref=?", (ref,)).fetchone()
        if current is not None and hmac.compare_digest(_fact_hash(salt, current[0]), witness):
            continue
        edited = db.execute(
            "SELECT 1 FROM deletion_edits d, json_each(d.facts) edit WHERE d.seq > ? AND edit.value = ? LIMIT 1",
            (seq, ref)).fetchone()
        if edited is None:
            return False
    return True


def compare(root, *, etc=Path("/etc/being"), share=Path("/usr/share/being"), models=MODELS, budget=0.75):
    """Return diagnostic flags and counts. Never disclose salt, IDs or hashes."""
    root = Path(root)
    record = _read(root)
    salt = bytes.fromhex(record["salt"])
    seq, nonce = record["proof"]
    if len(salt) != 32 or type(seq) is not int or seq < 0:
        raise ValueError("invalid continuity receipt")
    life = life_record(root)["id"]
    with _archive(root, budget) as db:
        anchor = db.execute("SELECT nonce FROM deletion_edits WHERE seq=?", (seq,)).fetchone() if seq else None
        proof = _meta(db, "deletion_lineage") == record["lineage"] and (
            (seq == 0 and nonce is None) or anchor == (nonce,))
        counts = _counts(db)
        within_proof = True
        for kind, count in counts.items():
            drop = max(0, record["counts"][kind] - count)
            if drop and (not proof or drop > _edition_count(db, kind, seq)):
                within_proof = False
        flags = {
            "life_matches": life == record["life"],
            "schema_matches": archive_schema(db) == record["schema"],
            "revision_retained": int(_meta(db, "memory_revision", 0)) >= record["revision"],
            "ledger_matches": proof,
            "counts_explained": within_proof,
            "sample_matches": proof and _compare_facts(db, record["witnesses"], salt, seq),
        }
    identity = _receipt(root, etc, share, models, identity_salt=salt)
    value = {}
    for key, current in identity.items():
        previous = record.get(key)
        value[key + "_changed"] = None if current is None or previous is None else current != previous
    return {**flags, **value, "counts": counts}
