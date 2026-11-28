"""Bounded, read-only integrity evidence for disposable PipOS archives.

This is a lab instrument, not an installed repair tool.  Point it only at
copies prepared for an integrity experiment; it refuses the live ``/mind``
tree.  It never decrypts a backup and therefore never calls an encrypted
artifact "verified".  A restored backup may be supplied only after the
normal authenticated restore path has produced a disposable database copy.
Each input must be a standalone copy made after a successful SQLite checkpoint;
the tool refuses adjacent WAL, SHM and rollback-journal files rather than
silently ignoring committed WAL state under immutable mode.

Example::

    python3 lab/archive_integrity.py baseline.db \
        --candidate scrubbed=scrubbed.db --candidate flipped=fault.db

The JSON report contains SQLite's full integrity verdict, content digests by
UTC day, and the structural prefix relation between deletion ledgers. It does
not claim that ledger structure alone proves deletion state. Text and deletion
identifiers are hashed into the report, never printed.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path


REPORT_KIND = "pipos-archive-integrity-v1"
DEFAULT_SECONDS = 120.0
DEFAULT_ROWS = 2_000_000
MAX_INPUT_BYTES = 32 * 1024 * 1024 * 1024
MAX_VALUE_BYTES = 16 * 1024 * 1024
MAX_SCHEMA_OBJECTS = 512
MAX_COLUMNS = 128
MAX_CANDIDATES = 8
MAX_SECONDS = 600.0
MAX_ROW_LIMIT = 5_000_000
MAX_ERRORS = 32
MAX_DAYS = 20_000
MAX_LEDGER_EDITS = 100_000
DELETION_ID = re.compile(r"[0-9a-f]{32}")
LIVE_ROOT = Path("/mind")


class LimitReached(RuntimeError):
    pass


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _sidecars(path):
    return tuple(candidate for suffix in ("-wal", "-shm", "-journal")
                 if os.path.lexists(candidate := Path(str(path) + suffix)))


@contextmanager
def regular_copy(path):
    """Pin one unchanged regular file without following a final symlink."""
    supplied = Path(path)
    before_link = supplied.lstat()
    if stat.S_ISLNK(before_link.st_mode):
        raise ValueError(f"final symlink is not an archive copy: {supplied}")
    path = supplied.resolve(strict=True)
    try:
        path.relative_to(LIVE_ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("live /mind paths are not valid lab inputs")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"not a regular archive copy: {path}")
    if not 0 < before.st_size <= MAX_INPUT_BYTES:
        raise ValueError(f"archive copy is empty or exceeds {MAX_INPUT_BYTES} bytes: {path}")
    sidecars = _sidecars(path)
    if sidecars:
        raise ValueError(f"archive copy is not a standalone checkpointed database: {sidecars[0]}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if _signature(opened) != _signature(before):
            raise ValueError(f"archive copy changed while opening: {path}")
        yield path, descriptor, before
        if (_signature(os.fstat(descriptor)) != _signature(opened)
                or _signature(path.lstat()) != _signature(before)
                or _sidecars(path)):
            raise ValueError(f"archive copy changed while it was inspected: {path}")
    finally:
        os.close(descriptor)


def _encode(value):
    if value is None:
        return b"n"
    if isinstance(value, bytes):
        body = value
        kind = b"b"
    elif isinstance(value, float):
        body = value.hex().encode("ascii")
        kind = b"f"
    elif isinstance(value, int):
        body = str(value).encode("ascii")
        kind = b"i"
    else:
        body = str(value).encode("utf-8", "surrogatepass")
        kind = b"s"
    return kind + str(len(body)).encode("ascii") + b":" + body


def _add_row(digest, row):
    for value in row:
        digest.update(_encode(value))
    digest.update(b";")


def _day(value):
    try:
        number = float(value)
        return datetime.datetime.fromtimestamp(number, datetime.timezone.utc).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return "invalid-timestamp"


class Inspector:
    def __init__(self, db, deadline, row_limit):
        self.db = db
        self.deadline = deadline
        self.row_limit = row_limit
        self.rows = 0
        self.timed_out = False
        db.set_progress_handler(self._progress, 10_000)

    def _progress(self):
        if time.monotonic() >= self.deadline:
            self.timed_out = True
            return 1
        return 0

    def row(self):
        self.rows += 1
        if self.rows > self.row_limit:
            raise LimitReached("row limit reached")
        if time.monotonic() >= self.deadline:
            self.timed_out = True
            raise LimitReached("time limit reached")


def _tables(inspector):
    result = set()
    for row in inspector.db.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        inspector.row()
        if len(result) >= MAX_SCHEMA_OBJECTS:
            raise LimitReached("schema object limit reached")
        result.add(row[0])
    return result


def _integrity(inspector):
    messages = []
    truncated = False
    try:
        for row in inspector.db.execute("PRAGMA integrity_check"):
            inspector.row()
            if len(messages) < MAX_ERRORS:
                messages.append(str(row[0])[:512])
            else:
                truncated = True
    except sqlite3.OperationalError:
        if inspector.timed_out:
            return {"status": "limit", "messages": messages, "truncated": True}
        raise
    return {"status": "ok" if messages == ["ok"] else "damaged",
            "messages": messages, "truncated": truncated}


def _daily(inspector, tables, report_key):
    result = {}
    for table in ("episodes", "facts"):
        if table not in tables:
            continue
        columns = set()
        for row in inspector.db.execute(f"PRAGMA table_info({table})"):
            inspector.row()
            if len(columns) >= MAX_COLUMNS:
                raise LimitReached("column limit reached")
            columns.add(row[1])
        wanted = [name for name in ("id", "ts", "kind", "speaker", "text", "meta",
                                      "confidence", "deletion_ref") if name in columns]
        if "ts" not in wanted or "id" not in wanted:
            continue
        ts_index = wanted.index("ts")
        days = {}
        query = f"SELECT {','.join(wanted)} FROM {table} ORDER BY id"
        for row in inspector.db.execute(query):
            inspector.row()
            key = _day(row[ts_index])
            if key not in days:
                if len(days) >= MAX_DAYS:
                    raise LimitReached("day limit reached")
                # A per-run secret key keeps a retained report from becoming
                # an oracle for guessed conversation text.
                days[key] = [hashlib.blake2b(key=report_key, digest_size=32), 0]
            _add_row(days[key][0], row)
            days[key][1] += 1
        result[table] = {key: {"rows": value[1], "digest": value[0].hexdigest()}
                         for key, value in sorted(days.items())}
    return result


def _valid_id(value):
    return isinstance(value, str) and DELETION_ID.fullmatch(value) is not None


def _ledger(inspector, tables, report_key):
    if not {"meta", "deletion_edits"} <= tables:
        return None
    row = inspector.db.execute("SELECT value FROM meta WHERE key = 'deletion_lineage'").fetchone()
    if row is None or not _valid_id(row[0]):
        return {"evidence": "structural-prefix-only", "valid": False}
    digest = hashlib.blake2b(key=report_key, digest_size=32)
    edits = []
    nonces = set()
    valid = True
    for nonce, episodes_raw, facts_raw in inspector.db.execute(
            "SELECT nonce, episodes, facts FROM deletion_edits ORDER BY seq"):
        inspector.row()
        if len(edits) >= MAX_LEDGER_EDITS:
            raise LimitReached("ledger edit limit reached")
        try:
            episodes, facts = json.loads(episodes_raw), json.loads(facts_raw)
        except (TypeError, json.JSONDecodeError, RecursionError):
            valid = False
            episodes, facts = None, None
        refs_valid = all(isinstance(refs, list)
                         and all(_valid_id(ref) for ref in refs)
                         and refs == sorted(set(refs)) for refs in (episodes, facts))
        if (not _valid_id(nonce) or nonce in nonces or not refs_valid
                or not (episodes or facts)):
            valid = False
        nonces.add(nonce)
        canonical = json.dumps([nonce, episodes, facts], separators=(",", ":"),
                               sort_keys=True).encode("utf-8")
        digest.update(len(canonical).to_bytes(8, "big"))
        digest.update(canonical)
        edits.append(hashlib.blake2b(canonical, key=report_key, digest_size=32).hexdigest())
    lineage = hashlib.blake2b(row[0].encode("ascii"), key=report_key, digest_size=32).hexdigest()
    return {"evidence": "structural-prefix-only", "valid": valid, "lineage_digest": lineage,
            "edits": len(edits), "edit_digests": edits, "digest": digest.hexdigest()}


def inspect(path, seconds, row_limit, report_key):
    started = time.monotonic()
    with regular_copy(path) as (resolved, descriptor, info):
        # The descriptor path pins the checked inode. immutable prevents
        # SQLite from opening or creating journals beside the disposable
        # input. Linux supplies /proc; the macOS lab host supplies /dev/fd.
        fd_root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
        uri = f"file:{fd_root}/{descriptor}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
            db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1 * 1024 * 1024)
            db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
            db.execute("PRAGMA query_only = ON")
            db.execute("PRAGMA temp_store = MEMORY")
            inspector = Inspector(db, started + seconds, row_limit)
            try:
                tables = _tables(inspector)
                integrity = _integrity(inspector)
                daily = _daily(inspector, tables, report_key) if integrity["status"] == "ok" else {}
                ledger = _ledger(inspector, tables, report_key) if integrity["status"] == "ok" else None
                limit = None
            except LimitReached as error:
                integrity = locals().get("integrity", {"status": "limit", "messages": [], "truncated": True})
                daily = locals().get("daily", {})
                ledger = None
                limit = str(error)
            except sqlite3.OperationalError as error:
                if not inspector.timed_out:
                    raise
                integrity = {"status": "limit", "messages": [], "truncated": True}
                daily, ledger, limit = {}, None, str(error)[:128]
            except sqlite3.DatabaseError as error:
                integrity = {"status": "damaged", "messages": [str(error)[:512]], "truncated": False}
                daily, ledger, limit = {}, None, None
        return {"bytes": info.st_size, "standalone_without_sidecars": True, "integrity": integrity,
                "daily": daily, "deletion_ledger": ledger, "rows_examined": inspector.rows,
                "limit": limit, "elapsed_s": round(time.monotonic() - started, 3)}


def compare(reference, candidate):
    changed = {}
    for table in sorted(set(reference["daily"]) | set(candidate["daily"])):
        left, right = reference["daily"].get(table, {}), candidate["daily"].get(table, {})
        days = sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))
        if days:
            changed[table] = days
    left, right = reference.get("deletion_ledger"), candidate.get("deletion_ledger")
    relation = "unavailable"
    if left and right and left.get("valid") and right.get("valid"):
        if left["lineage_digest"] != right["lineage_digest"]:
            relation = "different-lineage"
        elif left["edit_digests"] == right["edit_digests"]:
            relation = "equal"
        elif right["edit_digests"][:len(left["edit_digests"])] == left["edit_digests"]:
            relation = "candidate-extends-baseline"
        elif left["edit_digests"][:len(right["edit_digests"])] == right["edit_digests"]:
            relation = "candidate-is-older"
        else:
            relation = "diverged"
    return {"changed_utc_days": changed,
            "deletion_ledger_structural_prefix_relation": relation}


def candidate(value):
    label, separator, path = value.partition("=")
    if not separator or not label or any(char.isspace() for char in label):
        raise argparse.ArgumentTypeError("candidate must be LABEL=PATH with a whitespace-free label")
    return label, Path(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="explicit disposable baseline mind.db copy")
    parser.add_argument("--candidate", action="append", type=candidate, default=[], metavar="LABEL=PATH")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help="maximum seconds per archive (default: %(default)s)")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                        help="maximum result rows per archive (default: %(default)s)")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= MAX_SECONDS or not 1 <= args.rows <= MAX_ROW_LIMIT:
        parser.error(f"bounds must be 1..{MAX_SECONDS:g} seconds and 1..{MAX_ROW_LIMIT} rows")
    if len(args.candidate) > MAX_CANDIDATES:
        parser.error(f"at most {MAX_CANDIDATES} candidates may be inspected in one run")
    labels = [label for label, _ in args.candidate]
    if len(labels) != len(set(labels)):
        parser.error("candidate labels must be unique")

    report_key = os.urandom(32)
    baseline = inspect(args.baseline, args.seconds, args.rows, report_key)
    candidates = {}
    for label, path in args.candidate:
        report = inspect(path, args.seconds, args.rows, report_key)
        report["comparison"] = compare(baseline, report)
        candidates[label] = report
    result = {"kind": REPORT_KIND, "read_only": True,
              "encrypted_backup_authentication": "out-of-scope",
              "baseline": baseline, "candidates": candidates}
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    reports = (baseline, *candidates.values())
    complete = all(report["integrity"]["status"] == "ok" and report["limit"] is None
                   and (report["deletion_ledger"] is None or report["deletion_ledger"]["valid"])
                   for report in reports)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
