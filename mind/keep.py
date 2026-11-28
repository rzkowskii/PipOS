"""The owner's opt-in, content-addressed file keep."""

import asyncio
import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import unicodedata
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .memory import DISK_LOW
from .reply import Confirm, Reply, clean, is_guest
from .storage import atomic_write, sync_directory

ROOT = "keep"
PRESERVE = "preserve"
ERASE = "erase"
POLICY = "forget-policy"
MAX_FILE = 64 << 20
MAX_BATCH = 256 << 20
MAX_ITEMS = 20_000
MAX_ERASURES = 20_000
MAX_NAME = 140
CHUNK = 1 << 20
ARCHIVE_VERSION = 1
KINDS = frozenset(("photos", "audio files", "videos", "documents", "other files"))
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MEDIA_UUID = re.compile(r"[a-zA-Z0-9-]{1,64}")
_ITEM_COLUMNS = "items.digest, filename, kind, size, modified"
_POLICY_PHRASES = {
    "preserve my keep when i forget everything": PRESERVE,
    "erase my keep when i forget everything": ERASE,
}
_LIST = ("what's in my keep", "what is in my keep")
_EMPTY = ("empty my keep", "empty your keep")


@dataclass(frozen=True)
class Item:
    digest: str
    filename: str
    kind: str
    size: int
    modified: int | None
    title: str | None = None

    @property
    def display_name(self):
        return self.title or self.filename


def name_key(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def valid_filename(name):
    return (isinstance(name, str) and 1 <= len(name) <= MAX_NAME and name.isprintable()
            and name == name.strip() and name not in (".", "..") and "/" not in name and "\\" not in name)


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _modified(value):
    return value is None or type(value) is int and -(1 << 62) <= value < (1 << 62)


def _revision(value):
    return type(value) is int and 0 <= value < (1 << 63)


def _transfer_key(media_uuid, selection_key):
    if (not isinstance(media_uuid, str) or not _MEDIA_UUID.fullmatch(media_uuid)
            or selection_key != "photos" and not _digest(selection_key)):
        raise ValueError("The keep copy source was invalid.")


def _insert_item(db, item):
    db.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?)", (
        item.digest, item.filename, name_key(Path(item.filename).stem), name_key(item.filename),
        item.kind, item.size, item.modified))


def _all_items(db):
    titles = ", title FROM items LEFT JOIN titles USING (digest)" if _table(db, "titles") else " FROM items"
    rows = db.execute(f"SELECT {_ITEM_COLUMNS}{titles} ORDER BY items.digest LIMIT ?", (MAX_ITEMS + 1,)).fetchall()
    if len(rows) > MAX_ITEMS:
        raise ValueError("The keep index exceeds its supported item count.")
    return tuple(Item(*row) for row in rows)


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def document_revision(state_dir, digest):
    """Return an indexed item and the erasure generation the reference belongs to."""
    if not _digest(digest) or retention(state_dir) is None:
        return None
    with _read_catalog_db(state_dir) as db:
        if db is None or not _table(db, "items"):
            return None
        db.execute("BEGIN")
        titles = ", title FROM items LEFT JOIN titles USING (digest)" if _table(db, "titles") else " FROM items"
        row = db.execute(f"SELECT {_ITEM_COLUMNS}{titles} WHERE items.digest = ?", (digest,)).fetchone()
        if row is None:
            return None
        _, floor = _history(db)
        erased = (db.execute("SELECT revision FROM erased WHERE digest = ?", (digest,)).fetchone()
                  if _table(db, "erased") else None)
        return Item(*row), max(floor, erased[0] if erased is not None else 0)


def _history(db):
    values = dict(db.execute("SELECT key, value FROM history")) if _table(db, "history") else {}
    revision, floor = values.get("revision", 0), values.get("floor", 0)
    if not _revision(revision) or not _revision(floor) or floor > revision:
        raise ValueError("The keep erasure history is invalid.")
    return revision, floor


def _write_history(db, revision, floor):
    db.executemany("INSERT OR REPLACE INTO history VALUES (?, ?)", (("revision", revision), ("floor", floor)))


def _transfer_epoch(db):
    row = db.execute("SELECT value FROM history WHERE key = 'transfer_epoch'").fetchone()
    return row[0] if row else 0


def _check_transfer_epoch(db, expected):
    if expected is not None and expected != _transfer_epoch(db):
        raise ValueError("Your keep changed. Ask me to resume the copy again.")


def _limit_erasures(db, revision, floor):
    if db.execute("SELECT count(*) FROM erased").fetchone()[0] > MAX_ERASURES:
        # The proof remains bounded. Retire earlier catalogs entirely when
        # their complete per-file deletion history no longer fits.
        db.execute("DELETE FROM erased")
        floor = revision
    _write_history(db, revision, floor)


def _note_erasures(db, digests):
    revision, floor = _history(db)
    revision += 1
    if not _revision(revision):
        raise ValueError("The keep erasure history is full.")
    db.executemany("INSERT OR REPLACE INTO erased VALUES (?, ?)", ((digest, revision) for digest in digests))
    _limit_erasures(db, revision, floor)


def _write_title(db, digest, title, revision):
    db.execute("INSERT OR REPLACE INTO titles VALUES (?, ?, ?, ?)", (digest, title, name_key(title), revision))


def _private_dir(path):
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def retention(state_dir):
    root = Path(state_dir) / ROOT
    marker = root / POLICY
    try:
        if not _private_dir(root):
            return None
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
            value = stream.read(20).decode("ascii").strip()
    except (OSError, ValueError):
        return None
    return value if value in (PRESERVE, ERASE) else None


def storage_exists(state_dir):
    root = Path(state_dir) / ROOT
    for name in ("index.sqlite", "objects"):
        try:
            (root / name).lstat()
            return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def set_retention(state_dir, mode):
    if mode not in (PRESERVE, ERASE):
        raise ValueError("unknown keep policy")
    root = Path(state_dir) / ROOT
    root.mkdir(mode=0o700, exist_ok=True)
    if not _private_dir(root):
        raise ValueError("could not open the private keep")
    atomic_write(root / POLICY, mode + "\n", mode=0o600)


class Store:
    def __init__(self, state_dir):
        self.root = Path(state_dir) / ROOT
        self.objects = self.root / "objects"
        if retention(state_dir) is None:
            raise ValueError("Choose how Forget everything should treat your keep first.")
        self.objects.mkdir(mode=0o700, exist_ok=True)
        if not _private_dir(self.objects):
            raise ValueError("I couldn't open the private files in your keep.")
        path = self.root / "index.sqlite"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("I couldn't open your private keep index.")
        finally:
            os.close(fd)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS items (
                digest TEXT PRIMARY KEY, filename TEXT NOT NULL, stem TEXT NOT NULL, exact TEXT NOT NULL,
                kind TEXT NOT NULL, size INTEGER NOT NULL, modified INTEGER
            ) WITHOUT ROWID""")
            db.execute("CREATE INDEX IF NOT EXISTS keep_name ON items(exact)")
            db.execute("CREATE INDEX IF NOT EXISTS keep_stem ON items(stem)")
            db.execute("CREATE TABLE IF NOT EXISTS history (key TEXT PRIMARY KEY, value INTEGER NOT NULL) WITHOUT ROWID")
            db.execute("CREATE TABLE IF NOT EXISTS erased (digest TEXT PRIMARY KEY, revision INTEGER NOT NULL) WITHOUT ROWID")
            db.execute("CREATE TABLE IF NOT EXISTS pending_unlink (digest TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute("CREATE TABLE IF NOT EXISTS titles (digest TEXT PRIMARY KEY, title TEXT NOT NULL, "
                       "folded TEXT NOT NULL, revision INTEGER NOT NULL) WITHOUT ROWID")
            db.execute("CREATE INDEX IF NOT EXISTS keep_titles ON titles(folded)")
            db.execute("CREATE TABLE IF NOT EXISTS transfers (media TEXT NOT NULL, selection TEXT NOT NULL, "
                       "position INTEGER NOT NULL, prefix TEXT NOT NULL, PRIMARY KEY (media, selection)) WITHOUT ROWID")

    def connect(self):
        db = sqlite3.connect(self.root / "index.sqlite", timeout=2)
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def changing_files(self):
        fd = os.open(self.root / ".write-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("I couldn't open the keep safely.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def count(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT count(*) FROM items").fetchone()[0]

    @staticmethod
    def _indexed(db):
        return tuple(row[0] for row in db.execute("SELECT digest FROM items ORDER BY digest"))

    def selection(self):
        with closing(self.connect()) as db:
            return self._indexed(db)

    def entries(self):
        with closing(self.connect()) as db:
            return _all_items(db)

    def status_counts(self):
        entries = self.entries()
        return len(entries), sum(not self.contains(item) for item in entries)

    def contains(self, item):
        try:
            with self._open_blob(item.digest) as source:
                return os.fstat(source.fileno()).st_size == item.size
        except (OSError, ValueError):
            return False

    def transfer_progress(self, media_uuid, selection_key):
        _transfer_key(media_uuid, selection_key)
        with closing(self.connect()) as db:
            row = db.execute("SELECT position, prefix FROM transfers WHERE media = ? AND selection = ?",
                             (media_uuid, selection_key)).fetchone()
        return {"position": row[0], "prefix_sha256": row[1]} if row is not None else None

    def transfer_epoch(self):
        with closing(self.connect()) as db:
            return _transfer_epoch(db)

    def save_transfer_progress(self, media_uuid, selection_key, position=None, prefix_sha256=None, *, epoch=None):
        _transfer_key(media_uuid, selection_key)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _check_transfer_epoch(db, epoch)
            if position is None:
                db.execute("DELETE FROM transfers WHERE media = ? AND selection = ?", (media_uuid, selection_key))
            elif type(position) is int and 0 <= position <= 100_000 and _digest(prefix_sha256):
                db.execute("INSERT OR REPLACE INTO transfers VALUES (?, ?, ?, ?)",
                           (media_uuid, selection_key, position, prefix_sha256))
            else:
                raise ValueError("The keep copy progress was invalid.")

    def named(self, name):
        key = name_key(name)
        with closing(self.connect()) as db:
            return tuple(Item(*row) for row in db.execute(
                f"SELECT {_ITEM_COLUMNS}, title FROM items LEFT JOIN titles USING (digest) "
                "WHERE exact = ? OR stem = ? OR folded = ? ORDER BY items.digest LIMIT 2", (key, key, key)))

    def recent(self):
        with closing(self.connect()) as db:
            return tuple(Item(*row) for row in db.execute(
                f"SELECT {_ITEM_COLUMNS}, title FROM items LEFT JOIN titles USING (digest) "
                "ORDER BY modified DESC, exact, items.digest LIMIT 4"))

    def title(self, item, new_name):
        if not valid_filename(new_name) or not name_key(new_name):
            raise ValueError("Use a short name for the file.")
        key = name_key(new_name)
        with self.changing_files(), closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            selected = db.execute(
                "SELECT filename, title FROM items LEFT JOIN titles USING (digest) WHERE items.digest = ?", (item.digest,)
            ).fetchone()
            if selected != (item.filename, item.title):
                raise ValueError("That file changed. Ask me to name it again.")
            if db.execute(
                "SELECT 1 FROM items LEFT JOIN titles USING (digest) WHERE items.digest <> ? "
                "AND (exact = ? OR stem = ? OR folded = ?) LIMIT 1", (item.digest, key, key, key)
            ).fetchone():
                raise ValueError("Another kept file already uses that name.")
            revision, floor = _history(db)
            if not _revision(revision + 1):
                raise ValueError("The keep name history is full.")
            _write_title(db, item.digest, new_name, revision + 1)
            _write_history(db, revision + 1, floor)

    def read_text(self, item):
        if Path(item.filename).suffix.casefold() not in (".txt", ".md") or item.size > 64 * 1024:
            raise ValueError("I can read small plain-text files in your keep.")
        with self.open(item.digest) as stream:
            raw = stream.read(item.size + 1)
        if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.digest:
            raise OSError("I couldn't verify that kept file.")
        try:
            words = " ".join(raw.decode("utf-8").split())
        except UnicodeError as exc:
            raise ValueError("I couldn't read that file as UTF-8 text.") from exc
        if not words or not words.isprintable():
            raise ValueError("I couldn't find readable plain text in that file.")
        if len(words) <= 320:
            return f"[neutral] It says. {words}"
        ending = words.rfind(" ", 0, 320)
        return f"[neutral] The start says. {words[:ending if ending > 0 else 320]}."

    def reserve(self, size):
        info = os.statvfs(self.objects)
        return info.f_frsize * (info.f_bavail - DISK_LOW * info.f_blocks) >= size + CHUNK

    @contextmanager
    def _pending(self):
        if hasattr(os, "O_TMPFILE"):
            # The installed keep is on ext4. An interrupted transfer closes
            # this unlinked inode without leaving a partial named object.
            fd = os.open(self.objects, os.O_TMPFILE | os.O_RDWR, 0o600)
            with os.fdopen(fd, "w+b") as pending:
                yield pending
        else:
            with tempfile.NamedTemporaryFile(mode="w+b", dir=self.objects, prefix=".incoming-") as pending:
                yield pending

    def ingest(self, filename, kind, size, modified, reader, *, epoch=None):
        """Copy exactly the admitted byte count from a confined reader."""
        if (not valid_filename(filename) or type(size) is not int or not 0 <= size <= MAX_FILE
                or not isinstance(kind, str) or kind not in KINDS or not _modified(modified)):
            raise ValueError("That file could not be copied safely.")
        return self._receive(Item("", filename, kind, size, modified), reader, epoch=epoch)

    def restore_blob(self, item, reader):
        """Rehydrate a current index entry. A file backup cannot create entries."""
        if not isinstance(item, Item) or not _digest(item.digest) or type(item.size) is not int or not 0 <= item.size <= MAX_FILE:
            raise ValueError("That kept file cannot be recovered safely.")
        return self._receive(item, reader)

    def _receive(self, item, reader, *, epoch=None):
        if not self.reserve(item.size):
            raise OSError("There isn't enough safe free space in my keep.")
        with self._pending() as pending:
            os.fchmod(pending.fileno(), 0o600)
            remaining = item.size
            hashed = hashlib.sha256()
            while remaining:
                chunk = reader(min(remaining, CHUNK))
                if not chunk or len(chunk) > remaining:
                    raise OSError("The USB file ended during the copy.")
                hashed.update(chunk)
                pending.write(chunk)
                remaining -= len(chunk)
            pending.flush()
            os.fsync(pending.fileno())
            pending.seek(0)
            observed = hashed.hexdigest()
            if item.digest and observed != item.digest:
                raise ValueError("The recovered file did not match the keep catalog.")
            if hashlib.file_digest(pending, "sha256").hexdigest() != observed:
                raise OSError("The copy did not match its contents.")
            verified = Item(observed, item.filename, item.kind, item.size, item.modified)
            result = self._publish(pending, verified, require_index=bool(item.digest), epoch=epoch)
        return result

    def _publish(self, staged, item, *, require_index=False, epoch=None):
        if not _DIGEST.fullmatch(item.digest):
            raise ValueError("invalid keep digest")
        with self.changing_files(), closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _check_transfer_epoch(db, epoch)
            if db.execute("SELECT 1 FROM pending_unlink WHERE digest = ?", (item.digest,)).fetchone():
                raise ValueError("That file is still being removed. Ask me to copy it again afterward.")
            row = db.execute("SELECT size FROM items WHERE digest = ?", (item.digest,)).fetchone()
            indexed = row is not None
            if require_index and (not indexed or row[0] != item.size):
                raise ValueError("That file is no longer in the keep catalog.")
            if indexed and row[0] != item.size:
                raise ValueError("The keep catalog disagrees with this file's size.")
            if not indexed and db.execute("SELECT count(*) FROM items").fetchone()[0] >= MAX_ITEMS:
                raise OSError("Your keep has reached its item limit.")
            # The verified file and its directory entry are durable before
            # an index row can point at them. A retried import can repair a
            # file left by a crash between this point and the SQLite commit.
            obj = self.objects / item.digest
            if obj.exists():
                with self._open_blob(item.digest) as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() != item.digest:
                        raise OSError("I found a damaged file in my keep.")
                if indexed:
                    return False
            elif isinstance(staged.name, int):
                directory = os.open(self.objects, os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.link(f"/proc/self/fd/{staged.fileno()}", item.digest,
                            dst_dir_fd=directory, follow_symlinks=True)
                    os.fsync(directory)
                finally:
                    os.close(directory)
            else:
                os.link(staged.name, obj)
                sync_directory(self.objects)
            if not indexed:
                _insert_item(db, item)
        return True

    def open(self, digest):
        with _read_catalog_db(self.root.parent) as db:
            if db is None or not db.execute("SELECT 1 FROM items WHERE digest = ?", (digest,)).fetchone():
                raise FileNotFoundError("That file is no longer in your keep.")
            return self._open_blob(digest)

    def _open_blob(self, digest, *, writable=False):
        if not _DIGEST.fullmatch(digest):
            raise ValueError("invalid keep digest")
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.objects / digest, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise OSError("The kept object is not a private file.")
            opened = os.fdopen(fd, "r+b" if writable else "rb")
        except BaseException:
            os.close(fd)
            raise
        return opened

    def erase(self, digests=None, *, expected=None):
        """Retire the index entries and record erasures before removing the bytes."""
        with self.changing_files():
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                indexed = self._indexed(db)
                if expected is not None and indexed != expected:
                    return None
                if digests is None:
                    names = {p.name for p in self.objects.iterdir() if _DIGEST.fullmatch(p.name)}
                    names.update(indexed)
                    db.execute("DELETE FROM transfers")
                else:
                    names = set(digests)
                if not all(map(_digest, names)):
                    raise ValueError("invalid keep digest")
                db.execute("INSERT OR REPLACE INTO history VALUES ('transfer_epoch', ?)", (_transfer_epoch(db) + 1,))
                if names:
                    _note_erasures(db, names)
                    db.executemany("DELETE FROM titles WHERE digest = ?", ((digest,) for digest in names))
                    db.executemany("DELETE FROM items WHERE digest = ?", ((digest,) for digest in names))
                    db.executemany("INSERT OR IGNORE INTO pending_unlink VALUES (?)", ((digest,) for digest in names))
            self._remove_pending(sorted(names))
        return len(names)

    def _remove_pending(self, names):
        for digest in names:
            try:
                with self._open_blob(digest, writable=True) as item:
                    remaining = os.fstat(item.fileno()).st_size
                    zeros = b"\0" * min(CHUNK, remaining)
                    while remaining:
                        remaining -= item.write(zeros[:remaining])
                    item.flush()
                    os.fsync(item.fileno())
                (self.objects / digest).unlink()
                sync_directory(self.objects)
            except FileNotFoundError:
                pass
            with closing(self.connect()) as db, db:
                db.execute("DELETE FROM pending_unlink WHERE digest = ?", (digest,))

    def finish_pending_erase(self):
        with self.changing_files(), closing(self.connect()) as db:
            row = db.execute("SELECT digest FROM pending_unlink ORDER BY digest LIMIT 1").fetchone()
            if row is not None:
                self._remove_pending(row)


@contextmanager
def _read_catalog_db(state_dir):
    path = Path(state_dir) / ROOT / "index.sqlite"
    try:
        info = path.lstat()
    except FileNotFoundError:
        if retention(state_dir) is None and storage_exists(state_dir):
            raise ValueError("The keep has files but its retention policy is unreadable.")
        yield None
        return
    if (retention(state_dir) is None or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("The private keep catalog could not be read safely.")
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        yield db


def export_catalog(state_dir):
    """Snapshot the index without opening a writable connection or moving blobs."""
    with _read_catalog_db(state_dir) as db:
        if db is None:
            return None
        revision, _ = _history(db)
        return {"version": ARCHIVE_VERSION, "retention": retention(state_dir), "revision": revision,
                "items": [asdict(item) for item in _all_items(db)]}


def finish_pending_erase(state_dir):
    """Resume one previously confirmed unlink while an installed owner is idle."""
    with _read_catalog_db(state_dir) as db:
        if db is None or not _table(db, "pending_unlink"):
            return
        if db.execute("SELECT 1 FROM pending_unlink LIMIT 1").fetchone() is None:
            return
    Store(state_dir).finish_pending_erase()

def export_erasures(state_dir):
    with _read_catalog_db(state_dir) as db:
        if db is None:
            return None
        revision, floor = _history(db)
        erased = db.execute("SELECT digest, revision FROM erased WHERE revision > ? ORDER BY digest LIMIT ?",
                            (floor, MAX_ERASURES + 1)).fetchall() if _table(db, "erased") else []
        if len(erased) > MAX_ERASURES:
            raise ValueError("The keep deletion proof is too large.")
        titles = db.execute(
            "SELECT digest, revision, title FROM titles WHERE revision > ? ORDER BY digest LIMIT ?", (floor, MAX_ITEMS + 1)
        ).fetchall() if _table(db, "titles") else []
        if len(titles) > MAX_ITEMS:
            raise ValueError("The keep naming proof is too large.")
        return {"version": ARCHIVE_VERSION, "revision": revision, "floor": floor,
                "retention": retention(state_dir),
                "erased": [list(row) for row in erased], "titles": [list(row) for row in titles]}


def _catalog_items(catalog):
    if (not isinstance(catalog, dict) or set(catalog) != {"version", "retention", "revision", "items"}
            or type(catalog["version"]) is not int or catalog["version"] != ARCHIVE_VERSION
            or catalog["retention"] not in (PRESERVE, ERASE) or not _revision(catalog["revision"])
            or not isinstance(catalog["items"], list) or len(catalog["items"]) > MAX_ITEMS):
        raise ValueError("The backup keep catalog is invalid.")
    items = {}
    fields = {"digest", "filename", "kind", "size", "modified"}
    for raw in catalog["items"]:
        if (not isinstance(raw, dict) or set(raw) not in (fields, fields | {"title"})
                or not _digest(raw["digest"]) or not valid_filename(raw["filename"])
                or not isinstance(raw["kind"], str) or raw["kind"] not in KINDS
                or type(raw["size"]) is not int or not 0 <= raw["size"] <= MAX_FILE
                or not _modified(raw["modified"]) or raw["digest"] in items
                or raw.get("title") is not None and not valid_filename(raw["title"])):
            raise ValueError("A count or file record in the keep catalog is invalid.")
        items[raw["digest"]] = Item(**raw)
    return items.values()


def _proof_erasures(proof):
    fields = {"version", "revision", "floor", "retention", "erased"}
    if (not isinstance(proof, dict) or set(proof) not in (fields, fields | {"titles"})
            or type(proof["version"]) is not int or proof["version"] != ARCHIVE_VERSION
            or proof["retention"] not in (PRESERVE, ERASE)
            or not _revision(proof["revision"]) or not _revision(proof["floor"])
            or proof["floor"] > proof["revision"] or not isinstance(proof["erased"], list)
            or len(proof["erased"]) > MAX_ERASURES):
        raise ValueError("The current keep deletion proof is invalid.")
    erased = {}
    for raw in proof["erased"]:
        if (not isinstance(raw, list) or len(raw) != 2 or not _digest(raw[0])
                or not _revision(raw[1]) or not proof["floor"] < raw[1] <= proof["revision"]
                or raw[0] in erased):
            raise ValueError("A record in the current keep deletion proof is invalid.")
        erased[raw[0]] = raw[1]
    return erased


def _proof_titles(proof):
    records = proof.get("titles", [])
    if not isinstance(records, list) or len(records) > MAX_ITEMS:
        raise ValueError("The current keep naming proof is invalid.")
    titles = {}
    for raw in records:
        if (not isinstance(raw, list) or len(raw) != 3 or not _digest(raw[0])
                or not _revision(raw[1]) or not proof["floor"] < raw[1] <= proof["revision"]
                or not valid_filename(raw[2]) or raw[0] in titles):
            raise ValueError("A name in the current keep proof is invalid.")
        titles[raw[0]] = (raw[1], raw[2])
    return titles


def recover_catalog(state_dir, catalog, proof):
    """Restore missing-file index rows only as authorised by the current proof."""
    items = _catalog_items(catalog)
    erased = _proof_erasures(proof)
    titles = _proof_titles(proof)
    if catalog["revision"] > proof["revision"]:
        raise ValueError("The keep catalog is newer than the trusted deletion proof.")
    if retention(state_dir) is None:
        if storage_exists(state_dir):
            raise ValueError("Choose the local keep retention policy before restoring its catalog.")
        set_retention(state_dir, proof["retention"])
    store = Store(state_dir)
    count = 0
    with store.changing_files(), closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        local_revision, local_floor = _history(db)
        revision = max(proof["revision"], local_revision)
        floor = max(proof["floor"], local_floor)
        db.executemany(
            "INSERT INTO erased VALUES (?, ?) ON CONFLICT(digest) DO UPDATE "
            "SET revision=max(erased.revision, excluded.revision)", erased.items())
        indexed = set(store._indexed(db))
        blocked = set(row[0] for row in db.execute("SELECT digest FROM erased WHERE revision > ?", (catalog["revision"],)))
        if catalog["revision"] >= floor:
            for item in items:
                if item.digest not in indexed and item.digest not in blocked:
                    if len(indexed) >= MAX_ITEMS:
                        raise ValueError("The restored keep catalog would exceed its item limit.")
                    _insert_item(db, item)
                    title_revision, title = titles.get(item.digest, (catalog["revision"], item.title))
                    if title_revision < catalog["revision"]:
                        title_revision, title = catalog["revision"], item.title
                    if title is not None:
                        _write_title(db, item.digest, title, title_revision)
                    indexed.add(item.digest)
                    count += 1
        db.execute("DELETE FROM erased WHERE revision <= ?", (floor,))
        _limit_erasures(db, revision, floor)
    return count


def forgotten_name(text):
    match = re.fullmatch(r"kept file (.{1,140})", text.strip().rstrip("?.!"), re.I)
    return match[1].strip() if match else None


def _classify(text):
    folded = text.casefold()
    if folded in _POLICY_PHRASES:
        return "policy", _POLICY_PHRASES[folded]
    if folded in _LIST:
        return "count", None
    if folded in _EMPTY:
        return "empty", None
    if folded == "back up my keep":
        return "backup", None
    if folded == "how far did you get keeping the photos":
        return "photo_progress", None
    if folded in ("keep the photos", "resume copying photos", "resume copying the photos"):
        return "import", None
    if folded == "keep the photos from the sd card":
        return "import_sd", None
    if folded.startswith("what does "):
        from . import library
        from .keep_documents import parse

        if (question := parse(text)) and (question[2] or not library.may_match(text)):
            return "question", question
    if match := re.fullmatch(r"keep the file called (.{1,140}) from the sd card", text, re.I):
        return "import_sd", match[1]
    if match := re.fullmatch(r"keep the file called (.{1,140})", text, re.I):
        return "import", match[1]
    if match := re.fullmatch(r"call kept file (.{1,140}?) by the name (.{1,140})", text, re.I):
        return "title", (match[1], match[2])
    if match := re.fullmatch(r"read (?:me )?(.{1,140}) from my keep", text, re.I):
        return "read", match[1]
    if match := re.fullmatch(r"give me (?:the )?(.{1,140}) from my keep", text, re.I):
        return "export", match[1]
    if match := re.fullmatch(r"forget (.+)", text, re.I):
        if name := forgotten_name(match[1]):
            return "forget", name
    return None


def may_match(text):
    return _classify(clean(text)) is not None


async def handle(text, ctx):
    match = _classify(clean(text, ctx.name))
    if match is None:
        return None

    def authorized():
        body = ctx.current_body() if ctx.current_body is not None else None
        return bool(body and body.get("keep_available") is True)

    action, arg = match
    implicit_document = action == "question" and not arg[2]
    if is_guest(ctx) or ctx.speaker != ctx.owner:
        if implicit_document:
            return None
        return Reply("[neutral] Only the owner can use my keep.")
    if not authorized():
        if implicit_document:
            return None
        return Reply("[neutral] I can use my keep while my lid is open on a confirmed persistent installation.")
    mode = retention(ctx.state_dir)
    if action == "policy":
        go = "preserve your keep" if arg == PRESERVE else "also erase every file in your keep"

        async def change():
            if not authorized():
                return Reply("[neutral] I couldn't change that in this session.")
            try:
                await asyncio.to_thread(set_retention, ctx.state_dir, arg)
            except (OSError, ValueError):
                return Reply("[concern] I couldn't save the keep setting.")
            return Reply(f"[neutral] I'll {go} when you ask me to forget everything.")

        return Reply(f"[neutral] When you say forget everything, should I {go}?", confirm=Confirm(change, strict=True))
    if mode is None:
        if implicit_document:
            return None
        return Reply("[neutral] First say preserve my keep when I forget everything, "
                     "or erase my keep when I forget everything.")
    try:
        if action == "question":
            from .keep_documents import MissingDocument, find

            try:
                result = await find(ctx.state_dir, arg[0], arg[1], owner_allowed=True)
            except MissingDocument as error:
                return None if implicit_document else Reply(f"[neutral] {error}", private=True)
            except ValueError as error:
                return Reply(f"[neutral] {error}", private=True)
            except FileNotFoundError:
                return Reply("[neutral] That kept document's contents are missing on this laptop.", private=True)
            if not authorized():
                return Reply("[neutral] I couldn't read your keep in this session.", private=True)
            if not result.excerpts:
                return Reply("[neutral] I couldn't find that topic in the kept document.", private=True)
            quoted = " ".join(f'“{excerpt}”' for excerpt in result.excerpts)
            return Reply(f"[neutral] From {result.document}. {quoted}", private=True)
        store = await asyncio.to_thread(Store, ctx.state_dir)
        if action == "count":
            count, missing = await asyncio.to_thread(store.status_counts)
            recent = await asyncio.to_thread(store.recent)
            line = f"[neutral] Your keep has {count} file record{'s' if count != 1 else ''}."
            if missing:
                line += f" {missing} {'file is' if missing == 1 else 'files are'} missing on this laptop."
            if recent:
                line += " " + ", ".join(item.display_name for item in recent) + "."
            return Reply(line, private=True)
        if action == "title":
            old_name, new_name = arg
            candidates = await asyncio.to_thread(store.named, old_name)
            if len(candidates) != 1:
                return Reply("[neutral] I couldn't find one exact file by that name in your keep.")
            item, = candidates
            if not valid_filename(new_name):
                return Reply("[neutral] Use a short name for the file.")

            async def name_it():
                if not authorized() or retention(ctx.state_dir) != mode:
                    return Reply("[neutral] I couldn't change your keep in this session.")
                try:
                    await asyncio.to_thread(store.title, item, new_name)
                except ValueError as error:
                    return Reply(f"[concern] {error}", private=True)
                except (OSError, sqlite3.Error):
                    return Reply("[concern] I couldn't save that keep name.")
                return Reply(f"[neutral] I'll call it {new_name}.", private=True)

            return Reply(f"[neutral] Call {item.display_name} {new_name}?",
                         confirm=Confirm(name_it, strict=True), private=True)
        if action == "backup":
            from .keep_file_backup import back_up

            async def save():
                if not authorized() or retention(ctx.state_dir) != mode:
                    return Reply("[neutral] I couldn't use the backup stick in this session.")
                try:
                    return Reply(await back_up(ctx.state_dir), private=True)
                except (OSError, ValueError) as error:
                    return Reply(f"[concern] {error}", private=True)

            return Reply("[neutral] Make an encrypted file backup of your keep on your enrolled backup stick?",
                         confirm=Confirm(save, strict=True))
        if action == "photo_progress":
            from .keep_transfer import photos_progress

            try:
                position = await photos_progress(ctx.state_dir)
            except (OSError, ValueError) as error:
                return Reply(f"[concern] {error}", private=True)
            if position is None:
                return Reply("[neutral] I have no unfinished photo copy recorded for this stick.")
            return Reply(f"[neutral] I checked {position} photo{'s' if position != 1 else ''}. "
                         "Say resume copying the photos to continue.", private=True)
        if action == "read":
            candidates = await asyncio.to_thread(store.named, arg)
            if len(candidates) != 1:
                return Reply("[neutral] I couldn't find one exact file by that name in your keep.")
            if not await asyncio.to_thread(store.contains, candidates[0]):
                return Reply("[neutral] I have that file in the catalog. Its contents are missing on this laptop.")
            return Reply(await asyncio.to_thread(store.read_text, candidates[0]), private=True)
        if action == "export":
            from .keep_export import export, identify

            candidates = await asyncio.to_thread(store.named, arg)
            if len(candidates) != 1:
                return Reply("[neutral] I couldn't find one exact file by that name in your keep.")
            item, = candidates
            if not await asyncio.to_thread(store.contains, item):
                return Reply("[neutral] I have that file in the catalog. Its contents are missing on this laptop.")
            try:
                receipt = await identify(ctx.state_dir)
            except (OSError, ValueError) as error:
                return Reply(f"[concern] {error}", private=True)

            async def give():
                if not authorized() or retention(ctx.state_dir) != mode:
                    return Reply("[neutral] I couldn't use the stick in this session.")
                try:
                    return Reply(await export(ctx.state_dir, item, receipt), private=True)
                except (OSError, ValueError) as error:
                    return Reply(f"[concern] {error}", private=True)

            return Reply(f"[neutral] Copy {item.filename} to {receipt.description}?", private=True,
                         confirm=Confirm(give, strict=True))
        if action in ("import", "import_sd"):
            from .keep_export import identify
            from .keep_transfer import identify_sd, receive

            target = f"the file called {arg}" if arg else "the photos"
            try:
                receipt = await (identify_sd(ctx.state_dir) if action == "import_sd" else identify(ctx.state_dir))
            except (OSError, ValueError) as error:
                return Reply(f"[concern] {error}", private=True)

            async def copy():
                if not authorized() or retention(ctx.state_dir) != mode:
                    return Reply("[neutral] I couldn't use the stick in this session.")
                try:
                    result = await receive(ctx.state_dir, arg, receipt, immediate=action == "import_sd")
                except (OSError, ValueError) as error:
                    return Reply(f"[concern] {error}")
                return Reply(result, private=True)

            return Reply(f"[neutral] Copy {target} from {receipt.description} to your keep?",
                         confirm=Confirm(copy, strict=True), private=arg is not None)
        candidates = await asyncio.to_thread(store.named, arg) if action == "forget" else ()
        if action == "forget" and len(candidates) != 1:
            return Reply("[neutral] I couldn't find one exact file by that name in your keep.")
        snapshot = await asyncio.to_thread(store.selection) if action == "empty" else None
        count = len(snapshot) if snapshot is not None else len(candidates)
        selected = tuple(item.digest for item in candidates) if action == "forget" else None

        async def erase():
            if not authorized() or retention(ctx.state_dir) != mode:
                return Reply("[neutral] I couldn't change your keep in this session.")
            try:
                if await asyncio.to_thread(store.erase, selected, expected=snapshot) is None:
                    return Reply("[neutral] Your keep changed. Ask me to empty it again.")
            except (OSError, sqlite3.Error, ValueError):
                return Reply("[concern] I couldn't finish removing those files from your keep.")
            if ctx.ponder is not None:
                try:
                    ctx.ponder.prune_kept_jobs()
                except (OSError, sqlite3.Error, ValueError):
                    pass  # the source is already invalid and idle upkeep can retire the job
            return Reply("[neutral] I've removed the file." if selected else "[neutral] I've emptied your keep.")

        prompt = f"Remove {candidates[0].display_name} from your keep?" if selected else f"Empty your keep of {count} files?"
        return Reply(f"[concern] {prompt} Say yes to remove them permanently.",
                     confirm=Confirm(erase, strict=True), private=selected is not None)
    except (OSError, sqlite3.Error, ValueError):
        return Reply("[concern] I couldn't open your keep.")
