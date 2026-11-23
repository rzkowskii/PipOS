"""Owner-selected encrypted snapshots and deletion-aware offline restore.

A snapshot holds who the being is, how it is set up, its archive, the
owner's lists (lists.db) and its action log (counts only). Backups by voice
and the night shift's automatic one go to a stick whose filesystem label is
one of `STICK_LABELS`, into the same folder the F2 menu uses, keeping the
newest `KEEP` sets there. Each backup made here is recorded in
last-backup.json (a time and how it was made, nothing else).
"""

import argparse
import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
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
# The owner's lists and notes (a database of its own) and the action log
# (kinds and counts only). Both are cleared by "forget everything", which
# counts itself in ERASURES_FILE, so an older snapshot never brings them back.
LISTS_FILE = "lists.db"
ACTIONS_FILE = "log/actions.jsonl"      # in the mind directory
ACTIONS_ARCHIVED = "actions.jsonl"      # in the snapshot
ERASURES_FILE = "erasures.json"
SNAPSHOT_FILES = {*SETTINGS, DB_FILE, LISTS_FILE, ACTIONS_ARCHIVED, ERASURES_FILE}
DATABASES = {DB_FILE, LISTS_FILE}
ACTIONS_LIMIT = 3 * 1024 * 1024
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


def sqlite_copy(source, target):
    """A consistent copy of a live SQLite file, checked, in rollback mode."""
    with regular_file(source):
        pass
    with closing(sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as live, \
            closing(sqlite3.connect(target)) as copy:
        live.backup(copy, pages=256, sleep=0.05)
        copy.execute("PRAGMA journal_mode = DELETE")
        copy.execute("VACUUM")
        if copy.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError(f"{Path(source).name} failed SQLite's integrity check.")
    os.chmod(target, 0o600)


def actions_copy(source, target):
    """Only valid {ts, kind, count} lines are copied: no text can ride along."""
    from . import action_log

    lines = [json.dumps(entry, separators=(",", ":")) + "\n" for entry in action_log.entries(source.parent.parent)]
    if lines:
        atomic_write(target, "".join(lines), mode=0o600)


def extras_snapshot(root, stage, policy):
    """The lists and the action log, beside the archive.

    Lists are things the owner said to keep, so a memory that keeps nothing
    (session-only) backs none up. The action log holds no words at all.
    """
    if policy.mode != "session-only" and (root / LISTS_FILE).exists():
        sqlite_copy(root / LISTS_FILE, stage / LISTS_FILE)
    if (root / ACTIONS_FILE).exists():
        actions_copy(root / ACTIONS_FILE, stage / ACTIONS_ARCHIVED)
    count = erasures(root)
    if count:
        write_erasures(stage, count)


def erasures(mind_dir):
    """How many times "forget everything" has run in this life (0 if never)."""
    try:
        value = read_metadata(Path(mind_dir) / ERASURES_FILE)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        # Unreadable: treat as "something was erased", so nothing old returns.
        return 1 << 30
    count = value.get("count") if isinstance(value, dict) else None
    return count if type(count) is int and count >= 0 else 1 << 30


def write_erasures(mind_dir, count):
    atomic_write(Path(mind_dir) / ERASURES_FILE, json_text({"version": VERSION, "count": count}), mode=0o600)


def note_erase(mind_dir):
    """Called by "forget everything": older snapshots must not restore lists or the log."""
    write_erasures(mind_dir, min(erasures(mind_dir), (1 << 30) - 1) + 1)


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
            "policy": policy.to_dict(), "erasures": erasures(root)}


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
        extras_snapshot(root, stage, policy)
        manifest = {"kind": "snapshot", "version": VERSION, "life_id": life["id"], "created": int(time.time()),
                    "erasures": erasures(root)}
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
                    or entry.size <= 0 or (entry.name not in DATABASES
                                           and entry.size > (ACTIONS_LIMIT if entry.name == ACTIONS_ARCHIVED
                                                             else METADATA_LIMIT))):
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
    allowed = (fields, {*fields, "policy"}, {*fields, "policy", "erasures"})
    if (not isinstance(value, dict) or set(value) not in allowed
            or ("erasures" in value and (type(value["erasures"]) is not int or value["erasures"] < 0))
            or value["kind"] != "deletions" or type(value["version"]) is not int
            or value["version"] != VERSION or value["life_id"] != life_id):
        raise ValueError("The deletion proof is for a different life or format.")
    if not hmac.compare_digest(checkpoint_id(value), expected):
        raise ValueError("The deletion proof does not match the trusted current checkpoint.")
    policy = MemoryPolicy.from_dict(value["policy"]) if "policy" in value else None
    return value["ledger"], policy, proof_hash, value.get("erasures", 0)


def restore_extras(unpacked, prepared, policy, erased):
    """Lists and the action log come back only if nothing was erased since.

    A "forget everything" after the snapshot (the proof counts more erasures
    than the snapshot does) leaves both out: forgetting stays forgetting.
    """
    try:
        snapshot_erasures = read_metadata(unpacked / MANIFEST).get("erasures", 0)
    except (OSError, ValueError, AttributeError):
        snapshot_erasures = None
    if erased:
        write_erasures(prepared, erased)
    if type(snapshot_erasures) is not int or snapshot_erasures != erased:
        return
    if policy.mode != "session-only" and (unpacked / LISTS_FILE).exists():
        sqlite_copy(unpacked / LISTS_FILE, prepared / LISTS_FILE)
    if (unpacked / ACTIONS_ARCHIVED).exists():
        (prepared / "log").mkdir(mode=0o700, exist_ok=True)
        actions_copy(_as_log(unpacked), prepared / ACTIONS_FILE)


def _as_log(unpacked):
    """The archived action log, where action_log expects to read it."""
    folder = unpacked / "log"
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / "actions.jsonl"
    if not target.exists():
        os.replace(unpacked / ACTIONS_ARCHIVED, target)
    return target


def _publish_file(source, destination):
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    os.replace(source, destination)
    sync_directory(destination.parent)


def _copy_staged(source, destination):
    # A previous interrupted write may have left this filename in the private stage.
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.unlink(missing_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as data:
        shutil.copyfileobj(data, output)
        output.flush()
        os.fsync(output.fileno())


def publish(target, prepared, token):
    marker = target / RESTORE_MARKER
    scratch = target / RESTORE_STAGE
    names = [name for name in (*SETTINGS, DB_FILE, LISTS_FILE, ERASURES_FILE, ACTIONS_FILE)
             if (prepared / name).exists()]
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
            permitted.update((RESTORE_MARKER, *(Path(name).parts[0] for name in names)))
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
        for folder in sorted({(scratch / name).parent for name in names if "/" in name}, reverse=True):
            folder.rmdir()
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
            proof, current_policy, proof_hash, erased = read_proof(deletions, identity, directory, life["id"],
                                                                   current_checkpoint)
            policy = current_policy or policy
            compact_archive(unpacked, policy, proof, apply_expiry=True)
            (unpacked / DB_FILE).replace(prepared / DB_FILE)
            restore_extras(unpacked, prepared, policy, erased)
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
        if policy.mode != "session-only" and (root / LISTS_FILE).exists():
            sqlite_copy(root / LISTS_FILE, prepared / LISTS_FILE)
        if (root / ACTIONS_FILE).exists():
            (prepared / "log").mkdir(mode=0o700, exist_ok=True)
            actions_copy(root / ACTIONS_FILE, prepared / ACTIONS_FILE)
        if erasures(root):
            write_erasures(prepared, erasures(root))
        publish(target, prepared, {"carry": life["id"], "version": VERSION})
    return {"life_id": life["id"]}


# --- the labelled stick, by voice and at night ---------------------------------

LAST_RECORD = "last-backup.json"        # read by the mind (turn.py BACKUP_RECORD)
RECIPIENT_FILE = "backup-recipient.txt"
BACKUP_FOLDER = "PipOS-backups"
# A FAT label holds 11 characters, so the short forms are accepted too.
STICK_LABELS = ("being-backup", "pip-backup", "beingbackup", "pipbackup")
BY_LABEL = Path("/dev/disk/by-label")
KEEP = 5
SET = re.compile(r"pip-(\d{8}-\d{6})(?:\.age|-deletions\.age|-checkpoint\.txt)")
HOW = ("menu", "voice", "night")
# The night shift backs up at most once in this long.
NIGHT_GAP_S = 20 * 3600


class Unavailable(Exception):
    """Why a backup cannot be made now: `reason` is one of the codes below."""

    REASONS = ("no_tool", "no_key", "no_stick", "busy", "failed")

    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason


def record_backup(mind_dir, how, *, now=None):
    """Note a finished backup: when, and how it was made. Nothing else."""
    if how not in HOW:
        raise ValueError(f"Unknown backup kind {how!r}.")
    stamp = int(time.time() if now is None else now)
    atomic_write(Path(mind_dir) / LAST_RECORD, json_text({"version": VERSION, "ts": stamp, "how": how}), mode=0o600)


def last_backup(mind_dir):
    """The time of the last backup made here (unix seconds), or None."""
    try:
        value = read_metadata(Path(mind_dir) / LAST_RECORD)
    except (OSError, ValueError):
        return None
    stamp = value.get("ts") if isinstance(value, dict) else None
    return stamp if type(stamp) is int and stamp > 0 else None


def recipient_on_file(mind_dir):
    from .backup_crypto import RECIPIENT

    try:
        value = (Path(mind_dir) / RECIPIENT_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if RECIPIENT.fullmatch(value) else None


def find_stick(by_label=BY_LABEL):
    """The device of a partition labelled for backups, or None."""
    try:
        entries = sorted(os.scandir(by_label), key=lambda entry: entry.name)
    except OSError:
        return None
    for entry in entries:
        # udev escapes characters in labels as \xNN; a backup label has none.
        if entry.name.lower() in STICK_LABELS:
            try:
                return str(Path(entry.path).resolve(strict=True))
            except OSError:
                continue
    return None


def prune(folder, keep=KEEP):
    """Keep the newest `keep` backup sets in the folder; remove the rest.

    Only files this tool names (pip-<time>.age, its proof and checkpoint) are
    touched. Older sets can hold what was forgotten since, so going is good.
    """
    sets = {}
    for path in Path(folder).iterdir():
        match = SET.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            sets.setdefault(match[1], []).append(path)
    removed = 0
    for stamp in sorted(sets, reverse=True)[keep:]:
        for path in sets[stamp]:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        sync_directory(Path(folder))
    return removed


def readiness(mind_dir, *, which=shutil.which, by_label=BY_LABEL):
    """(device, recipient), or raise Unavailable with the reason."""
    if not (which("age") and which("age-keygen")):
        raise Unavailable("no_tool")
    recipient = recipient_on_file(mind_dir)
    if recipient is None:
        raise Unavailable("no_key")
    device = find_stick(by_label)
    if device is None:
        raise Unavailable("no_stick")
    return device, recipient


# Voice and night backups mount the stick here, never at the F2 menu's
# media folder, so neither can unmount the other's stick mid-read.
STICK_DIR = Path("/run/being-backup-stick")
# One backup at a time across processes: the mind (voice, night) and the F2
# menu (care.py) both take this lock.
BACKUP_LOCK = Path("/run/being-backup.lock")


def lock_path():
    runtime = BACKUP_LOCK.parent
    if runtime.is_dir() and os.access(runtime, os.W_OK):
        return BACKUP_LOCK
    return Path(tempfile.gettempdir()) / f"being-backup-{os.getuid()}.lock"


@contextmanager
def backup_lock(path=None):
    """Hold the one-backup-at-a-time lock, or raise Unavailable("busy")."""
    path = Path(path) if path is not None else lock_path()
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Unavailable("busy") from None
        yield
    finally:
        os.close(descriptor)


def unmount_stick(mount_dir=STICK_DIR):
    if os.path.ismount(mount_dir):
        subprocess.run(["umount", str(mount_dir)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False)
    try:
        Path(mount_dir).rmdir()
    except OSError:
        pass


def mount_stick(device, mount_dir=STICK_DIR):
    """Mount the labelled stick writable (FAT, exFAT or ext4; nothing on it runs)."""
    from .local_menu import WRITABLE_MOUNTS

    mount_dir = Path(mount_dir)
    unmount_stick(mount_dir)
    mount_dir.mkdir(mode=0o700, exist_ok=True)
    for filesystem, options in WRITABLE_MOUNTS:
        try:
            result = subprocess.run(["mount", "-t", filesystem, "-o", options, device, str(mount_dir)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return mount_dir
    unmount_stick(mount_dir)
    raise ValueError("The backup stick could not be mounted. PipOS writes FAT, exFAT and ext4 sticks.")


def stick_backup(mind_dir, how, *, mount=None, unmount=None, which=shutil.which, by_label=BY_LABEL, now=None,
                 keep=KEEP, lock=None):
    """Back up to the labelled stick (voice or night). Returns the result dict.

    Raises Unavailable. The menu's backup (care.py) keeps its own stick picker
    but takes the same lock.
    """
    with backup_lock(lock):
        device, recipient = readiness(mind_dir, which=which, by_label=by_label)
        mount = mount or mount_stick
        unmount = unmount or unmount_stick
        try:
            root = Path(mount(device))
            folder = root / BACKUP_FOLDER
            folder.mkdir(mode=0o700, exist_ok=True)
            result = backup_to_folder(Path(mind_dir), folder, recipient, now=now)
            result["pruned"] = prune(folder, keep)
            os.sync()
        except Unavailable:
            raise
        except (BackupCryptoError, OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError,
                subprocess.SubprocessError) as error:
            raise Unavailable("failed", str(error)[:200]) from None
        finally:
            try:
                unmount()
            except (OSError, subprocess.SubprocessError):
                pass
        record_backup(mind_dir, how, now=now)
        return result


class AutoBackupJob:
    """The night shift's backup (plan 8.6): once a night, to the labelled stick.

    The night shift gives every chunk about a second, and a backup is one
    piece of disk work of seconds to a minute that cannot be split. So the
    first chunk starts it in a worker thread and each chunk waits for it for
    at most its budget: False while it is still running (the shift asks again
    after letting wake and lid events through), True when there is nothing
    more to do tonight: done, done recently, or not possible (no stick, no
    key). An interrupt stops the waiting at once; the backup itself finishes
    in the background, whole, and the next chunk collects its outcome.
    """

    name = "backup"
    POLL_S = 0.1

    def __init__(self, mind_dir, *, backup=None, clock=time.time, gap_s=NIGHT_GAP_S):
        self.mind_dir = Path(mind_dir)
        self.backup = backup or (lambda: stick_backup(self.mind_dir, "night"))
        self.clock = clock
        self.gap_s = gap_s
        self.last_reason = None
        self._worker = None
        self._finished = None
        self._outcome = None

    def _attempt(self):
        try:
            self.backup()
            self._outcome = "done"
        except Unavailable as error:
            self._outcome = error.reason if error.reason in Unavailable.REASONS else "failed"
        except Exception:  # whatever went wrong, the night carries on
            self._outcome = "failed"
        finally:
            self._finished.set()

    @property
    def running(self):
        return self._worker is not None and not self._finished.is_set()

    async def run_chunk(self, budget_s):
        import threading

        from . import action_log

        if self._worker is None:
            last = last_backup(self.mind_dir)
            if last is not None and self.clock() - last < self.gap_s:
                self.last_reason = "recent"
                return True
            self._outcome = None
            self._finished = threading.Event()
            self._worker = threading.Thread(target=self._attempt, name="being-night-backup", daemon=True)
            self._worker.start()
        deadline = time.monotonic() + max(0.0, float(budget_s))
        while not self._finished.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                self.last_reason = "running"
                return False
            await asyncio.sleep(min(self.POLL_S, left))
        self._worker = None
        self.last_reason = self._outcome
        if self._outcome == "done":
            action_log.record(self.mind_dir, "backup.auto")
        elif self._outcome == "failed":
            action_log.record(self.mind_dir, "backup.failed")
        return True


def auto_backup_job(mind_dir, **options):
    """The job the night shift registers: `NightShift.register("backup", auto_backup_job(state_dir))`."""
    return AutoBackupJob(mind_dir, **options)


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
