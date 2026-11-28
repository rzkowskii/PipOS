"""The small, source-linked owner profile prepared during a consented night."""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass

from .storage import atomic_write, sync_directory


FILE = "core-profile.md"
MANUAL_FILE = "core.md"
MARKER = "<!-- PipOS generated core v1 -->"
MAX_FILE = 16 * 1024
MAX_FACTS = 4
MAX_FACT_CHARS = 120
_FACT = re.compile(r"- Fact F([1-9][0-9]{0,18}) <!-- ([0-9a-f]{32}) -->")
_OWNER_FACT = (
    "speaker = ? AND confidence >= 1 AND length(text) BETWEEN 1 AND ? "
    "AND (source IS NULL OR EXISTS (SELECT 1 FROM episodes "
    "WHERE id = facts.source AND speaker = facts.speaker AND kind = 'heard' AND text <> '[forgotten]'))"
)


@dataclass(frozen=True)
class Core:
    owner: str
    revision: int
    lineage: str
    facts: tuple[tuple[int, str], ...]

    def markdown(self):
        owner = json.dumps(self.owner, ensure_ascii=False)
        rows = [MARKER, f"# Core profile\n\nOwner: {owner}",
                f"<!-- Archive {self.lineage} revision {self.revision} -->"]
        rows.extend(f"- Fact F{row_id} <!-- {ref} -->" for row_id, ref in self.facts)
        return "\n".join(rows) + "\n"


def _read(path):
    with path.open(encoding="utf-8") as source:
        result = source.read(MAX_FILE + 1)
    if len(result) > MAX_FILE:
        raise ValueError("core profile is too large")
    return result


def manual_text(root):
    """A legacy, explicitly written core continues to be an owner override."""
    try:
        text = (root / MANUAL_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    return "" if text.startswith(MARKER) else text


def load(root):
    try:
        if (root / MANUAL_FILE).exists():
            return None
        marker, title, blank, owner, version, *lines = _read(root / FILE).split("\n")
        if marker != MARKER or title != "# Core profile" or blank or not owner.startswith("Owner: "):
            return None
        owner = json.loads(owner.removeprefix("Owner: "))
        archive = re.fullmatch(r"<!-- Archive ([0-9a-f]{32}) revision ([0-9]+) -->", version)
        if not isinstance(owner, str) or archive is None or lines[-1:] != [""]:
            return None
        facts = []
        for line in lines[:-1]:
            match = _FACT.fullmatch(line)
            if match is None:
                return None
            row_id, ref = match.groups()
            facts.append((int(row_id), ref))
        if len(facts) <= MAX_FACTS and len({fact[0] for fact in facts}) == len(facts):
            return Core(owner, int(archive[2]), archive[1], tuple(facts))
    except (OSError, ValueError):
        pass
    return None


def _recognised(root):
    """Read only the generated marker. Never overwrite a hand-written profile."""
    try:
        with (root / FILE).open("rb") as source:
            return source.read(len(MARKER)) == MARKER.encode("ascii")
    except FileNotFoundError:
        return None


def invalidate(root):
    """Retire the derived file before committing a deletion-history change."""
    try:
        if root is not None and _recognised(root):
            (root / FILE).unlink(missing_ok=True)
            sync_directory(root)
    except OSError:
        # The file carries references only. A failed unlink cannot block
        # forgetting, and any surviving index fails the archive revision check.
        pass


def publish(memory, owner):
    """Refresh the cache under the same SQLite writer lock as fact edits."""
    root = memory.root
    if root is None or not memory.format_ready:
        return None
    db = memory.db
    busy, = db.execute("PRAGMA busy_timeout").fetchone()
    try:
        db.execute("PRAGMA busy_timeout = 0")
        with db:
            db.execute("BEGIN IMMEDIATE")
            if (root / MANUAL_FILE).exists() or _recognised(root) is False:
                return None
            lineage = db.execute("SELECT value FROM meta WHERE key = 'deletion_lineage'").fetchone()
            if lineage is None:
                return None
            rows = db.execute(
                "SELECT id, deletion_ref FROM facts "
                f"WHERE {_OWNER_FACT} AND deletion_ref IS NOT NULL "
                "ORDER BY max(ts, coalesce(confirmed, ts)) DESC, id DESC LIMIT ?",
                (owner, MAX_FACT_CHARS, MAX_FACTS),
            ).fetchall()
            core = Core(owner, memory.revision, lineage[0], tuple(rows))
            text = core.markdown()
            if load(root) != core:
                atomic_write(root / FILE, text)
            return core
    finally:
        db.execute(f"PRAGMA busy_timeout = {busy}")


def resolve(memory, owner, snapshot):
    """Read facts from the live archive and match the exact saved source IDs."""
    if snapshot is None or snapshot.owner != owner or not snapshot.facts:
        return ()
    try:
        ids = json.dumps([fact[0] for fact in snapshot.facts])
        rows = {(row_id, ref): text for row_id, ref, text in memory.db.execute(
            "SELECT id, deletion_ref, text FROM facts "
            f"WHERE {_OWNER_FACT} "
            "AND id IN (SELECT value FROM json_each(?)) "
            "AND coalesce((SELECT value FROM meta WHERE key = 'memory_revision'), '0') = ? "
            "AND (SELECT value FROM meta WHERE key = 'deletion_lineage') = ?",
            (owner, MAX_FACT_CHARS, ids, str(snapshot.revision), snapshot.lineage),
        )}
    except sqlite3.Error:
        return ()
    return tuple((row_id, rows[row_id, ref]) for row_id, ref in snapshot.facts if (row_id, ref) in rows)


def digest(facts):
    """Stamp saved slot/context pairs with the effective live core prefix."""
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest() if facts else ""
