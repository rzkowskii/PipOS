"""Owner-selected encrypted snapshots and deletion-aware offline restore."""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .backup_crypto import BackupCryptoError, decrypt, encrypt, generate_key
from .initiative import POLICY_PATH as INITIATIVE_PATH, load_policy as load_initiative, save_policy as save_initiative
from .memory import Memory
from .profile import FACTORY_CONFIG, PROFILE_FILE, load_profile, save_profile
from .retention import POLICY_FILE, MemoryPolicy, load_policy, read_policy, save_policy
from .storage import atomic_write, private_directory, sync_directory


VERSION = 1
LIFE_FILE = "life.json"
DB_FILE = "mind.db"
SKILLS_FILE = "skills.json"
INITIATIVE_FILE = str(INITIATIVE_PATH)
CHECKPOINT_KIND = "pipos-deletion-checkpoint"
MANIFEST = "manifest.json"
RESTORE_MARKER = ".being-restore-in-progress"
RESTORE_STAGE = ".being-restore-stage"
# Reminders, Wi-Fi, lab access, the weather city, caches and anything derived
# (core.md) are left out: a backup holds who the being is, how it is set up,
# and its archive, and forgetting must still reach everything restored.
SETTINGS = (LIFE_FILE, PROFILE_FILE, SKILLS_FILE, POLICY_FILE, INITIATIVE_FILE)
SNAPSHOT_FILES = {*SETTINGS, DB_FILE}
METADATA_LIMIT = 64 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}")
LIFE_ID = re.compile(r"[0-9a-f]{32}")
ZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")


def json_text(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def read_metadata(path):
    with regular_file(path) as source:
        raw = source.read(METADATA_LIMIT + 1)
    if len(raw) > METADATA_LIMIT:
        raise ValueError(f"{path.name} is too large.")
    return json.loads(raw)


@contextmanager
def regular_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Expected a regular file at {path}.")
        yield source


def working_directory():
    # On the appliance /run is tmpfs. Keep decrypted and unsanitized data there.
    runtime = Path("/run")
    parent = runtime if sys.platform == "linux" and Path("/etc/being/env").is_file() else None
    return tempfile.TemporaryDirectory(dir=parent, prefix="being-backup-")


def source_directory(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or (root / RESTORE_MARKER).exists():
        raise ValueError("Select an available PipOS mind directory.")
    return root


def life_record(root):
    value = read_metadata(root / LIFE_FILE)
    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
            or not LIFE_ID.fullmatch(value["id"]) or type(value.get("born")) is not int
            or value["born"] < 0):
        raise ValueError("The life record is invalid.")
    return value


def copy_eligible_settings(source, target, life):
    atomic_write(target / LIFE_FILE, json_text(life))
    profile = source / PROFILE_FILE
    if profile.exists() or profile.is_symlink():
        read_metadata(profile)
    save_profile(target, load_profile(FACTORY_CONFIG, source))
    initiative = source / INITIATIVE_FILE
    if initiative.exists() or initiative.is_symlink():
        # Strict: a damaged announcement policy fails the backup loudly.
        save_initiative(target, load_initiative(source, strict=True))
    try:
        settings = read_metadata(source / SKILLS_FILE)
    except FileNotFoundError:
        return
    zone = settings.get("time_zone", "") if isinstance(settings, dict) else ""
    # Preserve valid zone keys if the receiving image has a smaller tzdata set.
    if not isinstance(zone, str) or len(zone) > 100 or (zone and not ZONE.fullmatch(zone)):
        raise ValueError("The saved time zone is invalid.")
    atomic_write(target / SKILLS_FILE, json_text({"time_zone": zone}))


def database_snapshot(root, target, policy):
    if policy.mode == "session-only" and not (root / DB_FILE).exists():
        with closing(Memory(target.parent)):
            return
    with regular_file(root / DB_FILE):
        pass
    # Opening through Memory establishes the deletion lineage on an older archive.
    with closing(Memory(root)) as source, closing(sqlite3.connect(target)) as destination:
        deadline = time.monotonic() + 120

        def check_progress(status, remaining, total):
            if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
                raise TimeoutError("The SQLite snapshot did not complete within two minutes.")

        source.db.backup(destination, pages=512, progress=check_progress, sleep=0.1)


def compact_archive(root, policy, proof=None, *, apply_expiry=False):
    with closing(Memory(root)) as memory:
        if proof is not None:
            memory.apply_deletion_ledger(proof)
        # Expiry deletes for good; a clock that cannot be believed expires nothing.
        sure, _ = memory.clock()
        cutoff = policy.cutoff(sure) if sure is not None else None
        if cutoff is not None:
            memory.expire_before(cutoff, snapshot=not apply_expiry)
        # Recreate search tables from current rows before compacting free pages.
        with memory.db:
            if policy.mode != "persistent":
                # Export filtering changes this copy's eligibility, while the
                # deletion proof must still extend its original ledger prefix.
                memory.db.execute("BEGIN IMMEDIATE")
                memory._begin_deletion_batch()
                memory.db.execute("DELETE FROM episodes")
                if policy.mode == "session-only":
                    memory.db.execute("DELETE FROM facts")
                memory._finish_deletion_batch(replayed=())
            memory.db.execute("DELETE FROM dream_journal")
            memory.db.execute("DELETE FROM dream_checkpoints")
            for table in ("facts_fts", "episodes_fts"):
                memory.db.execute(f"INSERT INTO {table} ({table}) VALUES ('rebuild')")
        memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        memory.db.execute("PRAGMA journal_mode = DELETE")
        memory.db.execute("VACUUM")
        if memory.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("The memory database failed SQLite's integrity check.")


def checkpoint_id(proof):
    return hashlib.sha256(json_text(proof).encode("utf-8")).hexdigest()


def deletion_checkpoint(root):
    root = source_directory(root)
    life = life_record(root)
    policy = read_policy(root)
    if policy.mode == "session-only" and not (root / DB_FILE).exists():
        raise ValueError("This life has no durable archive to checkpoint.")
    with regular_file(root / DB_FILE):
        pass
    with closing(Memory(root)) as memory:
        ledger = memory.deletion_ledger()
    return {"kind": "deletions", "version": VERSION, "life_id": life["id"], "ledger": ledger,
            "policy": policy.to_dict()}


def write_encrypted(output, recipient, source, *, outside):
    output = Path(output)
    parent = output.parent.resolve(strict=True)
    if parent == outside or parent.is_relative_to(outside):
        raise ValueError("Choose a backup destination outside the source mind directory.")
    output = parent / output.name
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError("Choose a regular output file.")
    with tempfile.NamedTemporaryFile(dir=parent, prefix=f".{output.name}.", delete=False) as dst:
        pending = Path(dst.name)
        try:
            source.seek(0)
            encrypt(recipient, source, dst)
            dst.flush()
            os.fsync(dst.fileno())
            os.replace(pending, output)
            sync_directory(parent)
        finally:
            pending.unlink(missing_ok=True)


def create(mind_dir, output, recipient):
    root = source_directory(mind_dir)
    life = life_record(root)
    # A damaged policy must fail the backup, not quietly back up nothing.
    policy = read_policy(root)
    with working_directory() as directory:
        stage = Path(directory)
        copy_eligible_settings(root, stage, life)
        if (root / POLICY_FILE).exists() or policy != MemoryPolicy():
            save_policy(stage, policy)
        database_snapshot(root, stage / DB_FILE, policy)
        compact_archive(stage, policy)
        manifest = {"kind": "snapshot", "version": VERSION, "life_id": life["id"], "created": int(time.time())}
        atomic_write(stage / MANIFEST, json_text(manifest))
        with tempfile.TemporaryFile(dir=stage) as plaintext:
            with tarfile.open(fileobj=plaintext, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                for name in sorted({MANIFEST, *SNAPSHOT_FILES}):
                    path = stage / name
                    if path.exists():
                        bundle.add(path, arcname=name, recursive=False)
            plaintext.flush()
            write_encrypted(output, recipient, plaintext, outside=root)
    return {"life_id": life["id"], "created": manifest["created"]}


def export_deletions(mind_dir, output, recipient):
    root = source_directory(mind_dir)
    proof = deletion_checkpoint(root)
    with working_directory() as directory, tempfile.TemporaryFile(dir=directory) as plaintext:
        plaintext.write(json_text(proof).encode("utf-8"))
        plaintext.flush()
        write_encrypted(output, recipient, plaintext, outside=root)
    return {"life_id": proof["life_id"], "checkpoint": checkpoint_id(proof)}


def extract_snapshot(plaintext, stage):
    required = {MANIFEST, LIFE_FILE, PROFILE_FILE, DB_FILE}
    permitted = {MANIFEST, *SNAPSHOT_FILES}
    seen = set()
    with tarfile.open(fileobj=plaintext, mode="r:") as archive:
        for entry in archive:
            if (entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or entry.sparse is not None
                    or entry.name not in permitted or entry.name in seen
                    or entry.size <= 0 or (entry.name != DB_FILE and entry.size > METADATA_LIMIT)):
                raise ValueError("Invalid file in the encrypted snapshot.")
            seen.add(entry.name)
            descriptor = os.open(stage / entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with archive.extractfile(entry) as source, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output)
    if not required <= seen:
        raise ValueError("The encrypted snapshot is incomplete.")
    manifest = read_metadata(stage / MANIFEST)
    life = life_record(stage)
    if (not isinstance(manifest, dict) or manifest.get("kind") != "snapshot"
            or type(manifest.get("version")) is not int or manifest["version"] != VERSION
            or manifest.get("life_id") != life["id"]):
        raise ValueError("The encrypted snapshot format or life ID is invalid.")
    return life


def authenticate(path, identity, plaintext):
    with regular_file(path) as source:
        decrypt(identity, source, plaintext)
    plaintext.seek(0)
    content_hash = hashlib.file_digest(plaintext, "sha256").hexdigest()
    plaintext.seek(0)
    return content_hash


def read_proof(path, identity, stage, life_id, expected):
    with tempfile.TemporaryFile(dir=stage) as plaintext:
        proof_hash = authenticate(path, identity, plaintext)
        value = json.load(plaintext)
    fields = {"kind", "version", "life_id", "ledger"}
    if (not isinstance(value, dict) or set(value) not in (fields, {*fields, "policy"})
            or value["kind"] != "deletions" or type(value["version"]) is not int
            or value["version"] != VERSION or value["life_id"] != life_id):
        raise ValueError("The deletion proof is for a different life or format.")
    if not hmac.compare_digest(checkpoint_id(value), expected):
        raise ValueError("The deletion proof does not match the trusted current checkpoint.")
    policy = MemoryPolicy.from_dict(value["policy"]) if "policy" in value else None
    return value["ledger"], policy, proof_hash


def _publish_file(source, destination):
    os.replace(source, destination)
    sync_directory(destination.parent)


def _copy_staged(source, destination):
    # A previous interrupted write may have left this filename in the private stage.
    destination.unlink(missing_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as data:
        shutil.copyfileobj(data, output)
        output.flush()
        os.fsync(output.fileno())


def publish(target, prepared, token):
    marker = target / RESTORE_MARKER
    scratch = target / RESTORE_STAGE
    names = [name for name in (*SETTINGS, DB_FILE) if (prepared / name).exists()]
    atomic_write(prepared / RESTORE_MARKER, json_text(token))
    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            existing = read_metadata(marker)
        except FileNotFoundError:
            existing = None
        permitted = {"lost+found", RESTORE_STAGE}
        if existing is not None:
            if existing != token:
                raise ValueError("Retry the unfinished restore with the same authenticated inputs.")
            permitted.update((RESTORE_MARKER, *names))
        if any(path.name not in permitted for path in target.iterdir()):
            raise ValueError("Select an empty, inactive mind partition.")
        private_directory(scratch)
        sync_directory(target)
        if existing is None:
            _copy_staged(prepared / RESTORE_MARKER, scratch / RESTORE_MARKER)
            os.replace(scratch / RESTORE_MARKER, marker)
            sync_directory(target)
        for name in names:
            _copy_staged(prepared / name, scratch / name)
            _publish_file(scratch / name, target / name)
        scratch.rmdir()
        sync_directory(target)
        marker.unlink()
        sync_directory(target)
    finally:
        os.close(descriptor)


def restore(mind_dir, input_path, identity, deletions=None, current_checkpoint=None):
    if deletions is not None or current_checkpoint is not None:
        if deletions is None or not current_checkpoint or not DIGEST.fullmatch(current_checkpoint):
            raise ValueError("History restore requires both a deletion proof and the independently known current checkpoint.")
    target = Path(mind_dir).resolve(strict=True)
    live_mind = Path("/mind")
    if not target.is_dir() or (live_mind.is_dir() and target.samefile(live_mind)):
        raise ValueError("Select an empty, inactive mind partition.")
    with working_directory() as directory:
        unpacked = Path(directory) / "unpacked"
        prepared = Path(directory) / "prepared"
        unpacked.mkdir(mode=0o700)
        prepared.mkdir(mode=0o700)
        with tempfile.TemporaryFile(dir=directory) as plaintext:
            snapshot_hash = authenticate(input_path, identity, plaintext)
            life = extract_snapshot(plaintext, unpacked)
        proof_hash = None
        policy = load_policy(unpacked)
        copy_eligible_settings(unpacked, prepared, life)
        if deletions is not None:
            proof, current_policy, proof_hash = read_proof(deletions, identity, directory, life["id"], current_checkpoint)
            policy = current_policy or policy
            compact_archive(unpacked, policy, proof, apply_expiry=True)
            (unpacked / DB_FILE).replace(prepared / DB_FILE)
        else:
            with closing(Memory(prepared)):
                pass
        if (unpacked / POLICY_FILE).exists() or policy != MemoryPolicy():
            save_policy(prepared, policy)
        token = {"snapshot": snapshot_hash, "proof": proof_hash, "version": VERSION}
        publish(target, prepared, token)
    return {"life_id": life["id"], "history_restored": deletions is not None and policy.mode != "session-only"}


# --- the owner's USB stick ---------------------------------------------------

def fingerprint(checkpoint):
    """The first and last 8 characters, for the owner to compare by eye."""
    if not isinstance(checkpoint, str) or not DIGEST.fullmatch(checkpoint):
        raise ValueError("Not a deletion checkpoint.")
    return f"{checkpoint[:8]}…{checkpoint[-8:]}"


def checkpoint_text(life_id, checkpoint, created):
    return json_text({"kind": CHECKPOINT_KIND, "version": VERSION, "life_id": life_id,
                      "checkpoint": checkpoint, "created": created})


def read_checkpoint_file(path):
    """(life_id, checkpoint) from a file written by backup_to_folder."""
    value = read_metadata(Path(path))
    if (not isinstance(value, dict) or value.get("kind") != CHECKPOINT_KIND or value.get("version") != VERSION
            or not isinstance(value.get("life_id"), str) or not LIFE_ID.fullmatch(value["life_id"])
            or not isinstance(value.get("checkpoint"), str) or not DIGEST.fullmatch(value["checkpoint"])):
        raise ValueError("That file is not a PipOS deletion checkpoint.")
    return value["life_id"], value["checkpoint"]


def backup_to_folder(mind_dir, folder, recipient, *, now=None):
    """Snapshot, deletion proof and checkpoint file, side by side in one folder.

    The checkpoint comes from the same proof that was written, so a restore can
    check the pair. Returns the three paths and the checkpoint.
    """
    folder = Path(folder)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now))
    snapshot = folder / f"pip-{stamp}.age"
    proof = folder / f"pip-{stamp}-deletions.age"
    record = folder / f"pip-{stamp}-checkpoint.txt"
    if any(path.exists() or path.is_symlink() for path in (snapshot, proof, record)):
        raise ValueError("A backup with this time already exists. Wait a second and try again.")
    created = create(mind_dir, snapshot, recipient)
    exported = export_deletions(mind_dir, proof, recipient)
    if exported["life_id"] != created["life_id"]:
        raise ValueError("The being changed while it was backed up.")
    atomic_write(record, checkpoint_text(created["life_id"], exported["checkpoint"], created["created"]), mode=0o600)
    return {"snapshot": snapshot, "deletions": proof, "checkpoint_file": record,
            "checkpoint": exported["checkpoint"], "life_id": created["life_id"]}


def has_conversation(mind_dir):
    """Whether an archive holds anything the owner said: facts or heard lines."""
    database = Path(mind_dir) / DB_FILE
    try:
        if not stat.S_ISREG(database.lstat().st_mode):
            return False
        with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as db:
            facts, = db.execute("SELECT EXISTS (SELECT 1 FROM facts)").fetchone()
            heard, = db.execute("SELECT EXISTS (SELECT 1 FROM episodes WHERE kind = 'heard' "
                                "AND text <> '[forgotten]')").fetchone()
        return bool(facts or heard)
    except (OSError, sqlite3.Error):
        return False


def carry(mind_dir, target):
    """Bring a Try being into a fresh install: its life record, settings and archive.

    It follows the restore path (the same eligible settings and publish step)
    without encryption, because nothing leaves this machine's RAM. Expiry and
    the memory policy apply to the carried copy, and the dream journal is not
    carried, exactly as for a backup.
    """
    root = source_directory(mind_dir)
    life = life_record(root)
    policy = read_policy(root)
    target = Path(target).resolve(strict=True)
    with working_directory() as directory:
        prepared = Path(directory) / "prepared"
        prepared.mkdir(mode=0o700)
        copy_eligible_settings(root, prepared, life)
        database_snapshot(root, prepared / DB_FILE, policy)
        if (prepared / DB_FILE).exists():
            compact_archive(prepared, policy, apply_expiry=True)
        if (root / POLICY_FILE).exists() or policy != MemoryPolicy():
            save_policy(prepared, policy)
        publish(target, prepared, {"carry": life["id"], "version": VERSION})
    return {"life_id": life["id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-backup", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    keygen = commands.add_parser("keygen", help="generate an owner identity and print its public recipient")
    keygen.add_argument("--identity", type=Path, required=True)
    for name in ("create", "export-deletions", "checkpoint", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--mind-dir", type=Path, required=True)
        if name in ("create", "export-deletions"):
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--recipient", required=True)
        if name == "restore":
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--identity", type=Path, required=True)
            command.add_argument("--deletions", type=Path)
            command.add_argument("--current-checkpoint")
    args = parser.parse_args(argv)
    try:
        if args.action == "keygen":
            print(generate_key(args.identity))
            return 0
        if args.action == "restore":
            result = restore(args.mind_dir, args.input, args.identity, args.deletions, args.current_checkpoint)
        elif args.action == "checkpoint":
            proof = deletion_checkpoint(args.mind_dir)
            result = {"life_id": proof["life_id"], "checkpoint": checkpoint_id(proof)}
        else:
            operation = create if args.action == "create" else export_deletions
            result = operation(args.mind_dir, args.output, args.recipient)
        print(json_text(result), end="")
        return 0
    except (BackupCryptoError, OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError, RecursionError) as exc:
        print(f"being-backup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
