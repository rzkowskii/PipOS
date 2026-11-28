"""Independently authenticate two PipOS encrypted backup archives.

This lab-only verifier never restores into ``/mind`` and never prints owner
data.  Each archive is decrypted into its own private disposable directory,
checked as SQLite, and replayed through the current deletion ledger.  Both
sets must be paired with the independently retained current checkpoint; a
checkpoint file sitting beside a backup is not treated as trust by itself.

Example::

    python3 -m lab.verify_rotating_backups --identity KEY \
      --trusted-checkpoint 64_HEX_DIGITS \
      --archive first SNAPSHOT.age DELETIONS.age CHECKPOINT.txt \
      --archive second SNAPSHOT.age DELETIONS.age CHECKPOINT.txt

The report establishes archive authentication and logical restore readiness.
It cannot prove that the two paths are on different physical media.
"""

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

from mind import backup
from mind.backup_crypto import BackupCryptoError
from mind.memory import Memory
from mind.retention import load_policy


KIND = "pipos-rotating-backup-verification-v1"
LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}\Z")
MAX_CHECK_SECONDS = 600.0


def _ciphertext_digest(path, limit):
    with backup.regular_file(path) as source:
        before = os.fstat(source.fileno())
        if not 0 < before.st_size <= limit:
            raise ValueError("A backup artifact exceeds its size limit or is empty.")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            block = source.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("A backup artifact changed while it was read.")
            digest.update(block)
            remaining -= len(block)
        after = os.fstat(source.fileno())
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("A backup artifact changed while it was read.")
        return digest.hexdigest()


def _integrity(path):
    """Full, bounded check of one already authenticated disposable database."""
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise ValueError("The authenticated snapshot contains an invalid database.")
    deadline = time.monotonic() + MAX_CHECK_SECONDS
    expired = False

    def progress():
        nonlocal expired
        if time.monotonic() >= deadline:
            expired = True
            return 1
        return 0

    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("PRAGMA trusted_schema=OFF")
            db.set_progress_handler(progress, 10_000)
            result = db.execute("PRAGMA integrity_check(1)").fetchone()
    except sqlite3.OperationalError:
        if expired:
            raise TimeoutError("A database integrity check exceeded ten minutes.") from None
        raise ValueError("An authenticated database could not be checked read-only.") from None
    except sqlite3.DatabaseError:
        raise ValueError("An authenticated database is damaged.") from None
    after = path.lstat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                              value.st_mtime_ns, value.st_ctime_ns)
    if identity(before) != identity(after) or result != ("ok",):
        raise ValueError("An authenticated database failed its integrity check.")


def _keep_catalog(unpacked, proof, destination):
    catalog_path = unpacked / backup.KEEP_CATALOG_FILE
    if not catalog_path.exists():
        return False
    if proof is None:
        raise ValueError("A Keep catalog has no current deletion proof.")
    from mind import keep

    catalog = backup.read_keep_catalog(catalog_path)
    keep.recover_catalog(destination, catalog, proof)
    _integrity(destination / "keep/index.sqlite")
    return True


@contextmanager
def prepared_archive(label, paths, identity, trusted):
    """Yield one disposable archive prepared by the canonical restore rules."""
    snapshot, deletions, checkpoint_file = map(Path, paths)
    snapshot_ciphertext = _ciphertext_digest(snapshot, backup.RESTORE_ENCRYPTED_LIMIT)
    deletions_ciphertext = _ciphertext_digest(deletions, backup.DELETION_PROOF_LIMIT)
    checkpoint_digest = _ciphertext_digest(checkpoint_file, backup.METADATA_LIMIT)
    checkpoint_life, checkpoint = backup.read_checkpoint_file(checkpoint_file)
    if checkpoint != trusted:
        raise ValueError(f"{label}: checkpoint does not match the independently retained value")

    with backup.working_directory() as directory_name:
        directory = Path(directory_name)
        unpacked = directory / "snapshot"
        prepared = directory / "proof-applied"
        unpacked.mkdir(mode=0o700)
        prepared.mkdir(mode=0o700)
        with tempfile.TemporaryFile(dir=directory) as plaintext:
            snapshot_plaintext = backup.authenticate(
                snapshot, identity, plaintext, limit=backup.RESTORE_ENCRYPTED_LIMIT)
            life = backup.extract_snapshot(plaintext, unpacked)
        if life["id"] != checkpoint_life:
            raise ValueError(f"{label}: snapshot and checkpoint belong to different lives")
        snapshot_policy = load_policy(unpacked)
        backup.copy_eligible_settings(unpacked, prepared, life)
        _integrity(unpacked / backup.DB_FILE)
        if (unpacked / backup.LISTS_FILE).exists():
            _integrity(unpacked / backup.LISTS_FILE)

        ledger, current_policy, proof_plaintext, erased, keep_proof = backup.read_proof(
            deletions, identity, directory, life["id"], trusted)
        Memory._validate_deletion_ledger(ledger)
        with closing(Memory(unpacked, embedder=None)) as memory:
            existing = memory.deletion_ledger()
        if (existing["lineage"] != ledger["lineage"]
                or ledger["edits"][:len(existing["edits"])] != existing["edits"]):
            raise ValueError(f"{label}: current deletion proof does not extend this snapshot")
        policy = current_policy or snapshot_policy
        # This is the same deletion replay, retention filtering, search
        # rebuild and expiry path used by backup.restore(). It touches only
        # the disposable authenticated copy.
        backup.compact_archive(unpacked, policy, ledger, apply_expiry=True)
        with closing(Memory(unpacked, embedder=None)) as memory:
            final_ledger = memory.deletion_ledger()
            if (final_ledger["lineage"] != ledger["lineage"]
                    or final_ledger["edits"][:len(ledger["edits"])] != ledger["edits"]):
                raise ValueError(f"{label}: prepared archive lost the trusted deletion-proof prefix")
            memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        (unpacked / backup.DB_FILE).replace(prepared / backup.DB_FILE)
        backup.restore_extras(unpacked, prepared, policy, erased)
        if (unpacked / backup.POLICY_FILE).exists() or policy != backup.MemoryPolicy():
            backup.save_policy(prepared, policy)
        keep_checked = _keep_catalog(unpacked, keep_proof, prepared)
        if policy.mode == "persistent":
            from mind.ponder import prune_kept_jobs

            with closing(Memory(prepared, embedder=None)) as memory:
                prune_kept_jobs(memory, prepared)
                memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _integrity(prepared / backup.DB_FILE)
        if (prepared / backup.LISTS_FILE).exists():
            _integrity(prepared / backup.LISTS_FILE)

        manifest = backup.read_metadata(unpacked / backup.MANIFEST)
        created = manifest.get("created") if isinstance(manifest, dict) else None
        # A dead RTC is a supported appliance condition. Validate the field's
        # representation, but do not compare it with this host's wall clock.
        if type(created) is not int or not 0 <= created <= (1 << 53):
            raise ValueError(f"{label}: snapshot creation time is invalid")
        if (_ciphertext_digest(snapshot, backup.RESTORE_ENCRYPTED_LIMIT) != snapshot_ciphertext
                or _ciphertext_digest(deletions, backup.DELETION_PROOF_LIMIT) != deletions_ciphertext
                or _ciphertext_digest(checkpoint_file, backup.METADATA_LIMIT) != checkpoint_digest):
            raise ValueError(f"{label}: a backup artifact changed during verification")
        result = {
            "label": label,
            "authenticated": True,
            "checkpoint_matches_trusted": True,
            "sqlite_integrity": "ok",
            "deletion_proof_applied": True,
            "keep_catalog_checked": keep_checked,
            "snapshot_ciphertext_sha256": snapshot_ciphertext,
            "deletions_ciphertext_sha256": deletions_ciphertext,
            # Used only for equality decisions below; removed before reporting.
            "_life": life["id"],
            "_snapshot_plaintext": snapshot_plaintext,
            "_proof_plaintext": proof_plaintext,
            "_created": created,
            "prepared": prepared,
            "policy": policy.mode,
            "history_restored": policy.mode != "session-only",
        }
        yield result


def verify_one(label, paths, identity, trusted):
    with prepared_archive(label, paths, identity, trusted) as result:
        # Paths and policy are useful to an explicit exporter, never to the
        # content-free verification report.
        return {key: value for key, value in result.items() if key not in ("prepared", "policy")}


def verify(archives, identity, trusted):
    if len(archives) != 2:
        raise ValueError("Exactly two rotating backup archives are required.")
    labels = [entry[0] for entry in archives]
    if len(set(labels)) != 2 or any(not LABEL.fullmatch(label) for label in labels):
        raise ValueError("Archive labels must be distinct bounded non-private tokens.")
    all_paths = [Path(path) for _, *paths in archives for path in paths]
    for index, path in enumerate(all_paths):
        for other in all_paths[index + 1:]:
            try:
                if path.samefile(other):
                    raise ValueError("The two archive sets must not reuse an input file.")
            except FileNotFoundError:
                raise ValueError("A selected backup artifact is missing.") from None

    reports = [verify_one(label, paths, identity, trusted) for label, *paths in archives]
    same_life = reports[0]["_life"] == reports[1]["_life"]
    same_current_proof = reports[0]["_proof_plaintext"] == reports[1]["_proof_plaintext"]
    distinct_snapshots = reports[0]["_snapshot_plaintext"] != reports[1]["_snapshot_plaintext"]
    chronological = reports[0]["_created"] != reports[1]["_created"]
    for report in reports:
        for private in ("_life", "_snapshot_plaintext", "_proof_plaintext", "_created"):
            del report[private]
    # Rotation cadence is an owner/media procedure, not an archive-format
    # invariant. Two independently readable copies may intentionally contain
    # the same logical snapshot; report that fact without inventing a failure.
    complete = same_life and same_current_proof
    return {
        "kind": KIND,
        "scope": "authenticated disposable verification; no restore and no plaintext export",
        "trusted_checkpoint": backup.fingerprint(trusted),
        "archives": reports,
        "pair": {"same_life": same_life, "same_current_deletion_proof": same_current_proof,
                 "distinct_snapshot_plaintext": distinct_snapshots,
                 "distinct_creation_times": chronological,
                 "physical_media_independence_verified": False},
        "verification_passes": complete,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--trusted-checkpoint", required=True)
    parser.add_argument("--archive", action="append", nargs=4, metavar=("LABEL", "SNAPSHOT", "DELETIONS", "CHECKPOINT"),
                        required=True)
    args = parser.parse_args(argv)
    if not backup.DIGEST.fullmatch(args.trusted_checkpoint):
        parser.error("--trusted-checkpoint must be the independently retained 64-digit value")
    try:
        report = verify(args.archive, args.identity, args.trusted_checkpoint)
    except (BackupCryptoError, OSError, ValueError, sqlite3.Error, TimeoutError) as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
