"""Create or verify retained day-chain evidence for a disposable PipOS archive.

This lab instrument is deliberately outside the installed archive path.  Its
input must be a standalone SQLite copy produced by the authenticated restore
and current deletion-proof replay in ``verify_rotating_backups.py``.  It
refuses ``/mind`` and adjacent WAL/journal files. The database comes from a
separately prepared snapshot. Verification can authenticate the newer
encrypted deletion proof.

The secret key is retained independently from both database and proof.  Every
episode/fact row receives a keyed commitment; commitments are folded into UTC
days and then into an ordered chain.  The proof binds the complete current
deletion ledger and carries its own keyed manifest authenticator.  A strictly
newer ledger found only inside a candidate can describe fact removals and
episode tombstones, but cannot authenticate its own authority: that result is
always nonpassing.  A passing forget result additionally requires the selected
encrypted current deletion proof, native age identity and independently
retained deletion digest alongside its detached checkpoint file. The canonical backup reader authenticates
their checkpoint and life binding, and their ledger must exactly match the
candidate.  Missing
tombstones, surviving facts, unrelated edits, new rows and unlocalisable
deletion identities remain explicitly ambiguous or unexpected.

Examples::

    python3 -m lab.archive_day_chain anchor restored/mind.db \
      --key archive-anchor.key --proof anchor.json
    python3 -m lab.archive_day_chain verify fault-copy.db \
      --key archive-anchor.key --proof anchor.json
    python3 -m lab.archive_day_chain verify forgotten-copy.db \
      --key archive-anchor.key --proof anchor.json \
      --current-deletions pip-deletions.age --identity identity.age \
      --trusted-checkpoint pip-checkpoint.txt --trusted-digest CURRENT_DELETION_DIGEST

The bounded proof contains keyed opaque deletion-identity tokens, but no
plaintext, raw row identity or unkeyed content digest.  Verification emits
only a bounded content-free JSON verdict to stdout.  Archives above the row
index bound need a new design or an exact-match-only instrument; this tool does
not silently weaken classification.
"""

import argparse
from contextlib import closing, contextmanager
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time


KIND = "pipos-archive-day-chain-v2"
LIVE_ROOT = Path("/mind")
TABLES = ("episodes", "facts")
MAX_INPUT_BYTES = 32 * 1024 * 1024 * 1024
MAX_KEY_BYTES = 4096
MIN_KEY_BYTES = 32
MAX_PROOF_BYTES = 64 * 1024 * 1024
MAX_ROWS = 5_000_000
MAX_INDEX_ROWS = 100_000
MAX_DAYS = 20_000
MAX_SCHEMA_OBJECTS = 512
MAX_COLUMNS = 128
MAX_VALUE_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 600.0
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
DELETION_ID = re.compile(r"[0-9a-f]{32}\Z")


class LimitReached(RuntimeError):
    pass


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _sidecars(path):
    return tuple(candidate for suffix in ("-wal", "-shm", "-journal")
                 if os.path.lexists(candidate := Path(str(path) + suffix)))


@contextmanager
def regular_copy(value):
    supplied = Path(value)
    link = supplied.lstat()
    if stat.S_ISLNK(link.st_mode):
        raise ValueError("the disposable database may not be a final symlink")
    path = supplied.resolve(strict=True)
    try:
        path.relative_to(LIVE_ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("live /mind paths are not valid lab inputs")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_INPUT_BYTES:
        raise ValueError("the disposable database is empty, non-regular, or too large")
    if _sidecars(path):
        raise ValueError("the disposable database is not a standalone checkpointed copy")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise ValueError("the disposable database changed while opening")
        yield descriptor
        if (_identity(os.fstat(descriptor)) != _identity(opened)
                or _identity(path.lstat()) != _identity(before) or _sidecars(path)):
            raise ValueError("the disposable database changed while it was inspected")
    finally:
        os.close(descriptor)


def _read_regular(value, limit, description):
    path = Path(value)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{description} must be a regular non-symlink file")
    if not 0 < before.st_size <= limit:
        raise ValueError(f"{description} has an invalid size")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise ValueError(f"{description} changed while it was opened")
        data = b""
        while len(data) < opened.st_size:
            block = os.read(descriptor, min(1024 * 1024, opened.st_size - len(data)))
            if not block:
                raise ValueError(f"{description} changed while it was read")
            data += block
        if _identity(os.fstat(descriptor)) != _identity(opened):
            raise ValueError(f"{description} changed while it was read")
        return data
    finally:
        os.close(descriptor)


def _key(path):
    value = _read_regular(path, MAX_KEY_BYTES, "anchor key")
    if len(value) < MIN_KEY_BYTES:
        raise ValueError(f"anchor key must contain at least {MIN_KEY_BYTES} bytes")
    return value


def _field(value):
    if value is None:
        kind, body = b"n", b""
    elif isinstance(value, bytes):
        kind, body = b"b", value
    elif isinstance(value, int):
        kind, body = b"i", str(value).encode("ascii")
    elif isinstance(value, float):
        kind, body = b"f", value.hex().encode("ascii")
    else:
        kind, body = b"s", str(value).encode("utf-8", "surrogatepass")
    if len(body) > MAX_VALUE_BYTES:
        raise LimitReached("a database value exceeds the evidence bound")
    return kind + len(body).to_bytes(8, "big") + body


def _mac(key, domain, *values):
    digest = hmac.new(key, domain, hashlib.sha256)
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.digest()


def _utc_day(value):
    try:
        return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return "invalid-timestamp"


def _keyed_ledger(value, key):
    """Key the canonical, production-validated semantic deletion proof."""
    from mind.memory import Memory

    Memory._validate_deletion_ledger(value)
    lineage = _mac(key, b"deletion-lineage-v2", _field(value["lineage"])).hex()
    edits = []
    for nonce, episodes, facts in value["edits"]:
        canonical = json.dumps([nonce, episodes, facts], sort_keys=True,
                               separators=(",", ":")).encode("ascii")
        edits.append(_mac(key, b"deletion-edit-v2", canonical).hex())
    return {"present": True, "lineage": lineage, "edits": edits}


def _ledger(db, key, tick, tables):
    if not {"meta", "deletion_edits"} <= tables:
        return ({"present": False, "lineage": None, "edits": []},
                {"episodes": set(), "facts": set()}, [])
    lineage = db.execute("SELECT value FROM meta WHERE key='deletion_lineage'").fetchone()
    if lineage is None or not isinstance(lineage[0], str) or not DELETION_ID.fullmatch(lineage[0]):
        raise ValueError("the deletion ledger has an invalid lineage")
    semantic_edits = []
    erased = {"episodes": set(), "facts": set()}
    erased_by_edit = []
    nonces = set()
    previous_seq = 0
    for seq, nonce, episodes_raw, facts_raw in db.execute(
            "SELECT seq,nonce,episodes,facts FROM deletion_edits ORDER BY seq"):
        tick()
        try:
            episode_refs, fact_refs = json.loads(episodes_raw), json.loads(facts_raw)
        except (TypeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("the deletion ledger is invalid") from error
        if (type(seq) is not int or seq <= previous_seq
                or not isinstance(nonce, str) or not DELETION_ID.fullmatch(nonce) or nonce in nonces
                or not isinstance(episode_refs, list) or not isinstance(fact_refs, list)
                or any(not isinstance(ref, str) or not DELETION_ID.fullmatch(ref)
                       for ref in episode_refs + fact_refs)
                or episode_refs != sorted(set(episode_refs))
                or fact_refs != sorted(set(fact_refs))
                or not (episode_refs or fact_refs)):
            raise ValueError("the deletion ledger is invalid")
        previous_seq = seq
        nonces.add(nonce)
        semantic_edits.append([nonce, episode_refs, fact_refs])
        edit_erased = {
            "episodes": {_mac(key, b"deletion-ref-v2", ref.encode("ascii")).hex()
                         for ref in episode_refs},
            "facts": {_mac(key, b"deletion-ref-v2", ref.encode("ascii")).hex()
                      for ref in fact_refs},
        }
        erased["episodes"].update(edit_erased["episodes"])
        erased["facts"].update(edit_erased["facts"])
        erased_by_edit.append(edit_erased)
    return (_keyed_ledger({"lineage": lineage[0], "edits": semantic_edits}, key),
            erased, erased_by_edit)


def _proof_mac(key, proof):
    authenticated = {name: value for name, value in proof.items()
                     if name != "proof_mac" and not name.startswith("_")}
    canonical = json.dumps(authenticated, sort_keys=True, separators=(",", ":")).encode("ascii")
    return _mac(key, b"archive-proof-v2", canonical).hex()


def measure(database, key, seconds, row_limit):
    started = time.monotonic()
    deadline = started + seconds
    rows = 0

    def tick():
        nonlocal rows
        rows += 1
        if rows > row_limit:
            raise LimitReached("row limit reached")
        if time.monotonic() >= deadline:
            raise LimitReached("time limit reached")

    with regular_copy(database) as descriptor:
        fd_root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
        uri = f"file:{fd_root}/{descriptor}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            def progress():
                return 1 if time.monotonic() >= deadline else 0

            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("PRAGMA trusted_schema=OFF")
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
            db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
            db.set_progress_handler(progress, 10_000)
            days = {}
            schema = {}
            row_index = []
            available = set()
            for row in db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                tick()
                if len(available) >= MAX_SCHEMA_OBJECTS:
                    raise LimitReached("schema object limit reached")
                available.add(row[0])
            for table in TABLES:
                if table not in available:
                    continue
                column_info = list(db.execute(f"PRAGMA table_info({table})"))
                columns = [row[1] for row in column_info]
                if not columns or len(columns) > MAX_COLUMNS or "id" not in columns or "ts" not in columns:
                    raise ValueError(f"{table} has an unsupported schema")
                if any(not isinstance(column, str) or not IDENTIFIER.fullmatch(column)
                       for column in columns):
                    raise ValueError(f"{table} has an unsupported column name")
                # Bind declared types, defaults, nullability and primary-key
                # shape too, rather than only the names used by SELECT.
                schema[table] = column_info
                ts_index = columns.index("ts")
                ref_index = columns.index("deletion_ref") if "deletion_ref" in columns else None
                query = f"SELECT {','.join(columns)} FROM {table} ORDER BY id"
                for row in db.execute(query):
                    tick()
                    day = _utc_day(row[ts_index])
                    if day not in days and len(days) >= MAX_DAYS:
                        raise LimitReached("day limit reached")
                    commitment = _mac(key, b"archive-row-v1", table.encode("ascii"),
                                      *(_field(value) for value in row))
                    if day not in days:
                        days[day] = [hmac.new(key, b"archive-day-rows-v1", hashlib.sha256), 0]
                    days[day][0].update(commitment)
                    days[day][1] += 1
                    if ref_index is None or not isinstance(row[ref_index], str):
                        raise ValueError(f"{table} lacks an opaque deletion identity")
                    if len(row_index) >= MAX_INDEX_ROWS:
                        raise LimitReached("forget-classification index limit reached")
                    token = _mac(key, b"deletion-ref-v2", row[ref_index].encode("ascii")).hex()
                    invariant = None
                    tombstone = False
                    if table == "episodes":
                        mutable = {"ts", "speaker", "text", "meta"}
                        invariant = _mac(key, b"episode-invariant-v2", *(
                            _field(value) for name, value in zip(columns, row) if name not in mutable)).hex()
                        values = dict(zip(columns, row))
                        timestamp = values.get("ts")
                        tombstone = (values.get("text") == "[forgotten]"
                                     and values.get("speaker") is None and values.get("meta") is None
                                     and isinstance(timestamp, (int, float))
                                     and float(timestamp) % 86400.0 == 0.0)
                    row_index.append({"token": token, "table": table, "day": day,
                                      "commitment": commitment.hex(), "invariant": invariant,
                                      "tombstone": tombstone})
            ledger, erased, erased_by_edit = _ledger(db, key, tick, available)

    schema_bytes = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    schema_commitment = _mac(key, b"archive-schema-v1", schema_bytes).hex()
    ledger_commitment = _mac(key, b"deletion-ledger-state-v2",
                             json.dumps(ledger, sort_keys=True,
                                        separators=(",", ":")).encode("ascii")).hex()
    previous = _mac(key, b"archive-chain-start-v1", bytes.fromhex(ledger_commitment),
                    bytes.fromhex(schema_commitment))
    anchors = []
    for day in sorted(days):
        row_root, count = days[day]
        commitment = row_root.digest()
        previous = _mac(key, b"archive-day-link-v1", previous, day.encode("ascii"),
                        count.to_bytes(8, "big"), commitment)
        # The independent day commitment localises a changed row to its day;
        # the cumulative anchor proves ordering and detects removed suffixes.
        anchors.append({"day": day, "rows": count,
                        "commitment": commitment.hex(), "anchor": previous.hex()})
    proof = {"kind": KIND, "algorithm": "HMAC-SHA-256", "key_id": _mac(key, b"key-id-v1").hex(),
            "schema_commitment": schema_commitment, "deletion_ledger_commitment": ledger,
            "days": anchors, "row_index": row_index, "final_anchor": previous.hex(), "rows": rows,
            "elapsed_s": round(time.monotonic() - started, 3)}
    proof["proof_mac"] = _proof_mac(key, proof)
    proof["_erased"] = erased
    proof["_erased_by_edit"] = erased_by_edit
    return proof


def _write_new(path, proof):
    data = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(data) > MAX_PROOF_BYTES:
        raise LimitReached("proof exceeds its output bound")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(Path(path), flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not write complete proof")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _proof(path):
    try:
        value = json.loads(_read_regular(path, MAX_PROOF_BYTES, "anchor proof"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("anchor proof is invalid") from error
    if not isinstance(value, dict) or value.get("kind") != KIND:
        raise ValueError("anchor proof has the wrong format")
    return value


def _outside_live(path, description):
    resolved = Path(path).resolve(strict=True)
    try:
        resolved.relative_to(LIVE_ROOT)
    except ValueError:
        return resolved
    raise ValueError(f"{description} must be retained outside /mind")


def _authenticate_current(deletions, identity, checkpoint_file, trusted_digest, key, candidate_ledger):
    """Authenticate one independently retained current proof canonically."""
    from mind import backup
    from mind.memory import Memory

    deletions = _outside_live(deletions, "encrypted deletion proof")
    identity = _outside_live(identity, "age identity")
    checkpoint_file = _outside_live(checkpoint_file, "trusted checkpoint")
    life_id, checkpoint = backup.read_checkpoint_file(checkpoint_file)
    if not backup.DIGEST.fullmatch(trusted_digest) or not hmac.compare_digest(checkpoint, trusted_digest):
        raise ValueError("checkpoint file does not match the independently retained current digest")
    with backup.working_directory() as directory:
        ledger, _policy, _proof_hash, _erased, _keep = backup.read_proof(
            deletions, identity, Path(directory), life_id, checkpoint)
    Memory._validate_deletion_ledger(ledger)
    authenticated = _keyed_ledger(ledger, key)
    if not hmac.compare_digest(
            json.dumps(authenticated, sort_keys=True, separators=(",", ":")),
            json.dumps(candidate_ledger, sort_keys=True, separators=(",", ":"))):
        raise ValueError("authenticated deletion proof does not match the candidate archive")
    return {"authenticated": True, "proof_life_matched_checkpoint": True,
            "checkpoint_matched": True, "candidate_ledger_matched": True,
            "proof_schema": "pipos deletions v1",
            "independent_physical_retention_verified": False}


def _row_map(proof):
    rows = proof.get("row_index")
    if not isinstance(rows, list) or len(rows) > MAX_INDEX_ROWS:
        raise ValueError("anchor proof has an invalid row index")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {
                "token", "table", "day", "commitment", "invariant", "tombstone"}
                or row.get("table") not in TABLES or not isinstance(row.get("token"), str)
                or row["token"] in result):
            raise ValueError("anchor proof has an invalid row index")
        result[row["token"]] = row
    return result


def _forget_comparison(expected, actual, expected_rows, actual_rows):
    old_ledger = expected.get("deletion_ledger_commitment")
    new_ledger = actual.get("deletion_ledger_commitment")
    if not isinstance(old_ledger, dict) or not isinstance(new_ledger, dict):
        return None
    old_edits, new_edits = old_ledger.get("edits"), new_ledger.get("edits")
    if (old_ledger.get("lineage") != new_ledger.get("lineage")
            or not isinstance(old_edits, list) or not isinstance(new_edits, list)
            or new_edits[:len(old_edits)] != old_edits or len(new_edits) <= len(old_edits)):
        return None
    extension = {"episodes": set(), "facts": set()}
    for edit in actual["_erased_by_edit"][len(old_edits):]:
        extension["episodes"].update(edit["episodes"])
        extension["facts"].update(edit["facts"])
    summary = {}

    def note(day, kind):
        counts = summary.setdefault(day, {"candidate_claimed_removed": 0,
                                          "candidate_claimed_tombstone": 0,
                                          "unexpected": 0, "ambiguous": 0})
        counts[kind] += 1

    for token, old in expected_rows.items():
        new = actual_rows.get(token)
        erased = token in extension[old["table"]]
        if new == old:
            if erased:
                note(old["day"], "ambiguous")
            continue
        if old["table"] == "facts" and erased and new is None:
            note(old["day"], "candidate_claimed_removed")
        elif (old["table"] == "episodes" and erased and new is not None
              and new.get("tombstone") is True
              and new.get("invariant") == old.get("invariant")):
            note(old["day"], "candidate_claimed_tombstone")
        elif erased:
            note(old["day"], "ambiguous")
        else:
            note(old["day"], "unexpected")
    for token, new in actual_rows.items():
        if token not in expected_rows:
            # A later archive may contain legitimate new memories, but without
            # another authenticated baseline they are not evidence of a clean
            # redaction-only transition.
            note(new["day"], "ambiguous")
    for table in TABLES:
        indexed = {token for token, row in expected_rows.items() if row["table"] == table}
        for _token in extension[table] - indexed:
            # The authenticated extension refers to an identity absent from
            # this baseline. It may be a valid older deletion, but this proof
            # cannot localise or certify its effect.
            note("unlocalized", "ambiguous")
    totals = {name: sum(day[name] for day in summary.values())
              for name in ("candidate_claimed_removed", "candidate_claimed_tombstone",
                           "unexpected", "ambiguous")}
    # These rows are descriptive only. The candidate supplied the extension
    # that labels its own changes, so even an otherwise exact transition lacks
    # independent authority and must never pass verification.
    result = ("forget-extension-untrusted" if totals["unexpected"] == totals["ambiguous"] == 0
              else "forget-ambiguous-untrusted")
    return result, [{"day": day, **summary[day]} for day in sorted(summary)], totals


def _comparison(expected, actual, key, externally_authenticated=False):
    supplied_mac = expected.get("proof_mac")
    if not isinstance(supplied_mac, str) or not hmac.compare_digest(supplied_mac, _proof_mac(key, expected)):
        return "unauthenticated-proof", [], None
    if expected.get("algorithm") != "HMAC-SHA-256":
        return "invalid-proof", [], None
    if expected.get("key_id") != actual["key_id"]:
        return "wrong-key", [], None
    if expected.get("schema_commitment") != actual["schema_commitment"]:
        return "schema-changed", [], None
    expected_rows, actual_rows = _row_map(expected), _row_map(actual)
    if expected.get("deletion_ledger_commitment") != actual["deletion_ledger_commitment"]:
        forgotten = _forget_comparison(expected, actual, expected_rows, actual_rows)
        if forgotten is None:
            return "deletion-context-changed", [], None
        result, days, totals = forgotten
        if result == "forget-extension-untrusted" and externally_authenticated:
            result = "forget-applied"
        return result, days, totals
    wanted = expected.get("days")
    if not isinstance(wanted, list):
        return "invalid-proof", [], None
    actual_by_day = {entry["day"]: entry for entry in actual["days"]}
    expected_by_day = {entry.get("day"): entry for entry in wanted if isinstance(entry, dict)}
    if len(expected_by_day) != len(wanted) or None in expected_by_day:
        return "invalid-proof", [], None
    changed = sorted(day for day in set(expected_by_day) | set(actual_by_day)
                     if (expected_by_day.get(day, {}).get("rows"),
                         expected_by_day.get(day, {}).get("commitment"))
                     != (actual_by_day.get(day, {}).get("rows"),
                         actual_by_day.get(day, {}).get("commitment")))
    chain_changed = any(expected_by_day.get(day, {}).get("anchor")
                        != actual_by_day.get(day, {}).get("anchor")
                        for day in set(expected_by_day) | set(actual_by_day))
    if not changed and not chain_changed and expected.get("final_anchor") == actual["final_anchor"]:
        return "match", [], None
    if not changed:
        return "invalid-proof", [], None
    return "content-changed", changed, None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("anchor", "verify"))
    parser.add_argument("database", type=Path)
    parser.add_argument("--key", required=True, type=Path,
                        help="independently retained secret key file (at least 32 bytes)")
    parser.add_argument("--proof", required=True, type=Path)
    parser.add_argument("--current-deletions", type=Path,
                        help="current encrypted deletion proof, independently authenticated for forget verification")
    parser.add_argument("--identity", type=Path,
                        help="age identity for --current-deletions")
    parser.add_argument("--trusted-checkpoint", type=Path,
                        help="detached checkpoint record with the life ID")
    parser.add_argument("--trusted-digest",
                        help="independently retained 64-digit current deletion checkpoint digest")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--rows", type=int, default=2_000_000)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= MAX_SECONDS or not 1 <= args.rows <= MAX_ROWS:
        parser.error(f"bounds must be 1..{MAX_SECONDS:g} seconds and 1..{MAX_ROWS} rows")
    external_values = (args.current_deletions, args.identity, args.trusted_checkpoint, args.trusted_digest)
    if any(value is not None for value in external_values) and not all(
            value is not None for value in external_values):
        parser.error("--current-deletions, --identity, --trusted-checkpoint and --trusted-digest are all-or-none")
    if args.mode != "verify" and any(value is not None for value in external_values):
        parser.error("external current-proof authentication is valid only in verify mode")
    key = _key(args.key)
    measured = measure(args.database, key, args.seconds, args.rows)
    external = None
    if args.mode == "anchor":
        retained = {name: value for name, value in measured.items() if not name.startswith("_")}
        _write_new(args.proof, retained)
        result, changed, classification, ok = "anchored", [], None, True
        deletion_context_equal = True
    else:
        expected = _proof(args.proof)
        anchor_authenticated = (isinstance(expected.get("proof_mac"), str)
                                and hmac.compare_digest(expected["proof_mac"], _proof_mac(key, expected)))
        if args.current_deletions is not None and anchor_authenticated:
            try:
                external = _authenticate_current(
                    args.current_deletions, args.identity, args.trusted_checkpoint, args.trusted_digest, key,
                    measured["deletion_ledger_commitment"])
            except Exception as error:
                parser.error(f"current deletion proof authentication failed: {type(error).__name__}")
        result, changed, classification = _comparison(
            expected, measured, key, externally_authenticated=external is not None)
        ok = result in ("match", "forget-applied")
        deletion_context_equal = (expected.get("deletion_ledger_commitment") == measured["deletion_ledger_commitment"]
                                  if anchor_authenticated else None)
    day_classification = changed if changed and isinstance(changed[0], dict) else None
    changed_days = ([entry["day"] for entry in changed] if day_classification is not None else changed)
    report = {"kind": KIND, "result": result, "changed_utc_days": changed_days,
              "deletion_context_equal": deletion_context_equal}
    if day_classification is not None:
        report["day_classification"] = day_classification
    if classification is not None:
        report["classification"] = classification
    report["external_current_proof"] = external or {"authenticated": False}
    if result == "forget-applied":
        report["external_current_proof"]["baseline_lineage_matched"] = True
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
