"""Explicitly export one authenticated PipOS backup as portable plain JSONL.

This is an archival read/interchange format, not a PipOS restore format. The
output is plaintext owner memory. It is created only at the explicit absolute
path supplied here, mode 0600, without overwrite, and is never printed. Keep
files, internal logs, credentials, derived jobs, FTS and vector tables are not
exported. Schema 1 has one header followed by allowlisted ``fact``, ``episode``,
``list``, ``list_item`` and ``note`` records; event episodes may carry only
``event_start`` and ``event_end`` from their internal metadata.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from mind import backup
from mind.backup_crypto import BackupCryptoError
from lab.verify_rotating_backups import prepared_archive


KIND = "pipos-portable-memory-jsonl"
VERSION = 1
MAX_RECORDS = 5_000_000
MAX_LINE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024**3
EPISODE_KINDS = ("heard", "said", "note")


class Writer:
    def __init__(self, stream):
        self.stream = stream
        self.records = 0
        self.bytes = 0

    def write(self, value):
        if self.records >= MAX_RECORDS:
            raise ValueError("The portable export exceeds its record limit.")
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_LINE_BYTES or self.bytes + len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError("The portable export exceeds its bounded size.")
        self.stream.write(encoded)
        self.records += 1
        self.bytes += len(encoded)


def _event(meta):
    if not isinstance(meta, str):
        return {}
    try:
        value = json.loads(meta)
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    start, end = value.get("event_start"), value.get("event_end")
    if (type(start) in (int, float) and type(end) in (int, float)
            and start < end):
        return {"event_start": start, "event_end": end}
    return {}


def export(prepared, policy, stream, checkpoint):
    writer = Writer(stream)
    writer.write({"kind": KIND, "version": VERSION, "record": "header",
                  "restore_supported": False, "memory_policy": policy,
                  "deletion_checkpoint": backup.fingerprint(checkpoint)})
    database = Path(prepared) / backup.DB_FILE
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        for row in db.execute(
                "SELECT id, ts, text, speaker, confidence, confirmed FROM facts ORDER BY id"):
            writer.write({"record": "fact", "id": row[0], "told_at": row[1], "text": row[2],
                          "speaker": row[3], "confidence": row[4], "confirmed_at": row[5]})
        placeholders = ",".join("?" for _ in EPISODE_KINDS)
        for row in db.execute(
                f"SELECT id, ts, kind, speaker, text, meta FROM episodes "
                f"WHERE kind IN ({placeholders}) AND text NOT IN ('', '[forgotten]') ORDER BY id",
                EPISODE_KINDS):
            value = {"record": "episode", "id": row[0], "told_at": row[1], "kind": row[2],
                     "speaker": row[3], "text": row[4]}
            value.update(_event(row[5]))
            writer.write(value)

    lists = Path(prepared) / backup.LISTS_FILE
    if lists.exists():
        with closing(sqlite3.connect(lists.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            for row in db.execute("SELECT id, name, created FROM lists ORDER BY id"):
                writer.write({"record": "list", "id": row[0], "name": row[1], "created_at": row[2]})
            for row in db.execute(
                    "SELECT items.id, items.list_id, items.text, items.added FROM items ORDER BY items.id"):
                writer.write({"record": "list_item", "id": row[0], "list_id": row[1],
                              "text": row[2], "added_at": row[3]})
            for row in db.execute("SELECT id, text, added FROM notes ORDER BY id"):
                writer.write({"record": "note", "id": row[0], "text": row[1], "added_at": row[2]})
    return writer.records, writer.bytes


def publish(output, prepared, policy, checkpoint):
    output = Path(output)
    if not output.is_absolute():
        raise ValueError("Choose an explicit absolute output path.")
    parent = output.parent.resolve(strict=True)
    destination = parent / output.name
    try:
        destination.relative_to("/mind")
    except ValueError:
        pass
    else:
        raise ValueError("Portable plaintext exports may not be written into /mind.")
    if os.path.lexists(destination):
        raise FileExistsError("The portable export destination already exists.")
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".pipos-jsonl-", dir=parent, delete=False) as stream:
            pending = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise PermissionError("The destination does not preserve owner-only file permissions.")
            # Check the actual publication primitives using an empty file.
            # FAT-style media must refuse before any plaintext is written.
            probe = pending.with_name(pending.name + ".link-probe")
            os.link(pending, probe)
            probe.unlink()
            os.fsync(descriptor)
            records, size = export(prepared, policy, stream, checkpoint)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, destination)
        try:
            pending.unlink()
            pending = None
            os.fsync(descriptor)
        except BaseException:
            destination.unlink(missing_ok=True)
            os.fsync(descriptor)
            raise
        return records, size
    finally:
        try:
            if pending is not None:
                pending.unlink(missing_ok=True)
        finally:
            os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--trusted-checkpoint", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--deletions", type=Path, required=True)
    parser.add_argument("--checkpoint-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="new absolute plaintext JSONL path; never overwritten")
    args = parser.parse_args(argv)
    if not backup.DIGEST.fullmatch(args.trusted_checkpoint):
        parser.error("--trusted-checkpoint must be the independently retained 64-digit value")
    try:
        paths = (args.snapshot, args.deletions, args.checkpoint_file)
        with prepared_archive("export", paths, args.identity, args.trusted_checkpoint) as result:
            records, size = publish(args.output, result["prepared"], result["policy"],
                                    args.trusted_checkpoint)
    except (BackupCryptoError, OSError, ValueError, sqlite3.Error) as error:
        parser.error(str(error))
    print(f"Wrote {records} records ({size} bytes) to the explicit owner-only JSONL file.")


if __name__ == "__main__":
    main()
