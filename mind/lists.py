"""Lists and notes, kept in their own file, /mind/lists.db.

"Add eggs and bread to the shopping list", "what's on my shopping list?",
"take milk off my shopping list", "cross off milk", "clear my shopping list"
(asks yes or no), "what lists do you have?", "make a note: the plumber comes
Thursday", "read my notes", "delete my last note".

Lists are managed explicitly. A topic forget in memory.py leaves them alone;
"forget everything" calls `erase_all()`. The file is in WAL mode with
secure_delete on, so a deleted item's bytes are overwritten, not left in
free pages. Guests can neither read nor change them. Nothing here is put in
the conversation memory.
"""

import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .reply import Confirm, Reply, clean, is_guest, keeps_nothing

FILENAME = "lists.db"
READ_BACK = 5           # items said before "and N more"
READ_NOTES = 3
MAX_LISTS = 50
MAX_ITEMS = 500
MAX_NOTES = 1000
MAX_TEXT = 200
GUEST_LINE = "[neutral] In guest mode I don't keep or read lists and notes."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lists(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    text TEXT NOT NULL, folded TEXT NOT NULL, added REAL NOT NULL);
CREATE INDEX IF NOT EXISTS items_list ON items(list_id, id);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, text TEXT NOT NULL, added REAL NOT NULL);
"""


def database(state_dir):
    return Path(state_dir) / FILENAME


def _connect(state_dir):
    path = database(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    connection = sqlite3.connect(path, timeout=5)
    try:
        return _prepare(connection, path, fresh)
    except BaseException:
        connection.close()      # a damaged file must not leave a connection open
        raise


def _prepare(connection, path, fresh):
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    if fresh:
        connection.executescript(_SCHEMA)
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(f"{path}{suffix}", 0o600)
            except OSError:
                pass
    elif connection.execute("PRAGMA user_version").fetchone()[0] < 1:
        connection.executescript(_SCHEMA)
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    return connection


def erase_all(state_dir):
    """Delete every list and note, and the file itself ("forget everything")."""
    path = database(state_dir)
    if path.exists():
        try:
            with closing(_connect(state_dir)) as db:
                db.execute("DELETE FROM items")
                db.execute("DELETE FROM lists")
                db.execute("DELETE FROM notes")
                db.commit()
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("VACUUM")
        except sqlite3.DatabaseError:
            pass
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    _LAST.clear()


def fold(text):
    """How items are compared: 'The Eggs' == 'egg'."""
    said = " ".join(text.casefold().replace("’", "'").split())
    said = re.sub(r"^(?:the|a|an|some|my|more) ", "", said)
    return said[:-1] if len(said) > 3 and said.endswith("s") and not said.endswith("ss") else said


def list_name(spoken):
    """'my shopping' -> 'shopping'; 'to do' -> 'to-do'; 'groceries' -> 'shopping'."""
    said = " ".join((spoken or "").casefold().split())
    said = re.sub(r"^(?:my|the|our|your|a|an)\s+", "", said).strip(" '\"")
    said = re.sub(r"\s+list$", "", said)
    if said in ("my", "the", "our", "your", "list"):
        return ""
    if re.fullmatch(r"to ?-? ?dos?|things to do|todo", said):
        return "to-do"
    if re.fullmatch(r"grocery|groceries|shop|shops|food shopping", said):
        return "shopping"
    return said


class Lists:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)

    def _db(self):
        return closing(_connect(self.state_dir))

    def names(self):
        if not database(self.state_dir).exists():
            return []
        with self._db() as db:
            return [row[0] for row in db.execute("SELECT name FROM lists ORDER BY id")]

    def items(self, name):
        """The items on a list, oldest first, or None if there is no such list."""
        if not database(self.state_dir).exists():
            return None
        with self._db() as db:
            row = db.execute("SELECT id FROM lists WHERE name=?", (name,)).fetchone()
            if row is None:
                return None
            return [r[0] for r in db.execute("SELECT text FROM items WHERE list_id=? ORDER BY id", row)]

    def add(self, name, items):
        """(added, already there). Raises OverflowError past the limits."""
        added, already = [], []
        with self._db() as db:
            row = db.execute("SELECT id FROM lists WHERE name=?", (name,)).fetchone()
            if row is None:
                if db.execute("SELECT count(*) FROM lists").fetchone()[0] >= MAX_LISTS:
                    raise OverflowError("lists")
                row = (db.execute("INSERT INTO lists(name, created) VALUES (?, ?)", (name, time.time())).lastrowid,)
            present = {r[0] for r in db.execute("SELECT folded FROM items WHERE list_id=?", row)}
            count = len(present)
            for item in items:
                key = fold(item)
                if key in present:
                    already.append(item)
                    continue
                if count >= MAX_ITEMS:
                    raise OverflowError("items")
                db.execute("INSERT INTO items(list_id, text, folded, added) VALUES (?, ?, ?, ?)",
                           (row[0], item, key, time.time()))
                present.add(key)
                count += 1
                added.append(item)
            db.commit()
        return added, already

    def find(self, item):
        """Names of the lists that hold `item`."""
        if not database(self.state_dir).exists():
            return []
        with self._db() as db:
            return [r[0] for r in db.execute(
                "SELECT DISTINCT lists.name FROM items JOIN lists ON lists.id = items.list_id "
                "WHERE items.folded=? ORDER BY lists.id", (fold(item),))]

    def remove(self, name, item):
        """The item's text as stored, or None if it was not on that list."""
        if not database(self.state_dir).exists():
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT items.id, items.text FROM items JOIN lists ON lists.id = items.list_id "
                "WHERE lists.name=? AND items.folded=? ORDER BY items.id LIMIT 1", (name, fold(item))).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM items WHERE id=?", (row[0],))
            db.commit()
            return row[1]

    def clear(self, name, *, drop=False):
        """Empty a list (or delete it); the number of items removed, or None if no such list."""
        if not database(self.state_dir).exists():
            return None
        with self._db() as db:
            row = db.execute("SELECT id FROM lists WHERE name=?", (name,)).fetchone()
            if row is None:
                return None
            count = db.execute("DELETE FROM items WHERE list_id=?", row).rowcount
            if drop:
                db.execute("DELETE FROM lists WHERE id=?", row)
            db.commit()
            return count

    def add_note(self, text):
        with self._db() as db:
            if db.execute("SELECT count(*) FROM notes").fetchone()[0] >= MAX_NOTES:
                raise OverflowError("notes")
            db.execute("INSERT INTO notes(text, added) VALUES (?, ?)", (text, time.time()))
            db.commit()

    def notes(self):
        """Every note, newest first."""
        if not database(self.state_dir).exists():
            return []
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT text FROM notes ORDER BY id DESC")]

    def delete_last_note(self):
        if not database(self.state_dir).exists():
            return None
        with self._db() as db:
            row = db.execute("SELECT id, text FROM notes ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM notes WHERE id=?", (row[0],))
            db.commit()
            return row[1]

    def clear_notes(self):
        if not database(self.state_dir).exists():
            return 0
        with self._db() as db:
            count = db.execute("DELETE FROM notes").rowcount
            db.commit()
            return count


# -- speaking -----------------------------------------------------------------

_COUNT = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")


def count_words(number):
    if number < len(_COUNT):
        return _COUNT[number]
    from .skills import number_words
    return number_words(number)


def joined(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _cap(text):
    return text[:1].upper() + text[1:]


def spoken_list(name):
    return f"your {name} list"


def read_back(items, limit=READ_BACK):
    """'milk, eggs and bread', or 'a, b, c, d, e, and three more'."""
    if len(items) <= limit:
        return joined(items)
    rest = len(items) - limit
    return ", ".join(items[:limit]) + f", and {count_words(rest)} more"


# -- phrases ------------------------------------------------------------------

_LIST = r"(?:(?:my|the|our|your) )?(?P<list>[\w'-]+(?: [\w'-]+){0,2}?) list"
_ANY_LIST = rf"(?:(?:my|the|our) list|{_LIST})"
_PATTERNS = [
    ("which", re.compile(r"^(?:what|which) lists (?:do you have|have you got|do i have|have i got|are there|do we have)"
                         r"|^(?:read|tell me|name|list) (?:all )?(?:my|the|of my) lists|^what lists$"
                         r"|^(?:do i|do you) have any lists$", re.I)),
    ("note_read", re.compile(r"^(?:(?:read|tell me|show me|say|list)(?: me| out)?(?: all)? (?:my|the|your|all my) notes"
                             r"|what(?:'s| is| are) (?:in )?my notes|what notes (?:do i have|have i got|have you got)"
                             r"|do i have any notes)$", re.I)),
    ("note_delete", re.compile(r"^(?:delete|remove|erase|scrap|get rid of|bin|cancel) (?:my|the|that) "
                               r"(?:last|latest|most recent|newest|previous) note$", re.I)),
    ("note_clear", re.compile(r"^(?:delete|remove|erase|clear|get rid of) (?:all )?(?:of )?(?:my|the) notes$", re.I)),
    ("note_add", re.compile(r"^(?:(?:make|take|write|add|save|jot down|leave) (?:a|me a|myself a) note(?: to self)?"
                            r"(?: that says| saying| that| of| about)?[,:]? "
                            r"|note(?: to self)?(?: that|:|,| down)? |jot down (?:that )?)(?P<text>.+)$", re.I)),
    ("check", re.compile(rf"^(?:is|are) (?P<item>.+?) (?:on|in) {_ANY_LIST}$", re.I)),
    ("read", re.compile(rf"^(?:what(?:'s| is| are| have i got| do i have| have we got| do we have| do i need)"
                        rf"(?: on| in| from)? |(?:read|tell me|show me|say|go through|check)(?: me| out| back)?"
                        rf"(?: what's on)? ){_ANY_LIST}$", re.I)),
    ("clear", re.compile(rf"^(?:clear|empty|wipe|clean out|clear out|delete everything (?:on|from)"
                         rf"|take everything off|remove everything from) {_ANY_LIST}$", re.I)),
    ("drop", re.compile(rf"^(?:delete|remove|get rid of|throw away|scrap|bin) {_ANY_LIST}$", re.I)),
    ("undo", re.compile(r"^(?:actually[, ]+|oops[, ]+|no[, ]+)?(?:take|cross) (?:it|that|those|them) off"
                        r"(?: (?:the|my) list| again)?$|^(?:actually[, ]+)?remove (?:it|that) from (?:the|my) list$", re.I)),
    ("remove", re.compile(rf"^(?:cross|tick|strike|scratch) off (?P<item>.+?)(?: (?:on|from|in|off) {_ANY_LIST})?$", re.I)),
    ("remove", re.compile(rf"^(?:take|cross|tick|strike|scratch|rub|knock) (?P<item>.+?) off(?: (?:of )?{_ANY_LIST})?$", re.I)),
    ("remove", re.compile(rf"^(?:remove|delete|take) (?P<item>.+?) (?:from|off) {_ANY_LIST}$", re.I)),
    ("add", re.compile(rf"^(?:please )?(?:add|put|pop|stick|write|include|chuck|throw) (?P<items>.+?) "
                       rf"(?:on|onto|on to|to|in|into) {_ANY_LIST}$", re.I)),
]
# The gate is also how the mind tells a follow-up meant for it from the room,
# so a bare "off" ("turn the lights off", "I'm off to work") is not enough.
_GATE = re.compile(r"\b(?:lists?|notes?|jot down)\b"
                   r"|\b(?:cross|tick|strike|scratch|take|rub|knock)\b[\w' ,-]{0,60}?\boff\b", re.I)
_SPLIT = re.compile(r"\s*,\s*(?:and\s+|then\s+)?|\s+and\s+|\s+plus\s+", re.I)
_LAST = {}  # the last items added this session (RAM only), for "take it off"
UNDO_WITHIN = 600.0     # "take that off" means something added in the last ten minutes
SESSION_ONLY_LINE = ("[neutral] I'm set to keep nothing after a restart, so I'm not keeping lists or notes. "
                     "You can change that in Care.")


_PATTERNS_BY_KIND = {kind: pattern for kind, pattern in reversed(_PATTERNS)}


def may_match(text):
    return bool(_GATE.search((text or "")[:400]))


def split_items(spoken):
    """'eggs, bread and butter' -> ['eggs', 'bread', 'butter']; a quoted item stays whole."""
    said = spoken.strip()
    quoted = re.fullmatch(r"[\"'](.+)[\"']", said)
    if quoted:
        parts = [quoted[1]]
    else:
        parts = _SPLIT.split(said)
    items = []
    for part in parts:
        part = re.sub(r"^(?:some|a few|more|also|the) ", "", part.strip(" \"'"), flags=re.I).strip()
        if part and len(part) <= MAX_TEXT:
            items.append(part)
    return items[:20]


def _which(store, spoken, *, create=False):
    """The list a phrase means, or (None, line) when it is unclear."""
    if spoken:
        return list_name(spoken), None
    names = store.names()
    if len(names) == 1:
        return names[0], None
    if not names:
        return (None, "[neutral] Which list? For example, say \"add milk to my shopping list\".") if create else \
            (None, "[neutral] You don't have any lists yet.")
    return None, f"[neutral] Which list? You have the {joined(names)} lists."


def _list_of(match):
    groups = match.groupdict()
    return groups.get("list")


async def handle(text, ctx):
    if not may_match(text):
        return None
    said = clean(text, getattr(ctx, "name", "pip")).rstrip("?").strip()
    for kind, pattern in _PATTERNS:
        match = pattern.match(said)
        if match:
            break
    else:
        return None
    if kind == "note_add":
        if len(match["text"].split()) < 2:
            return None
        # The note is kept as said: only politeness comes off its end.
        kept = clean(text, getattr(ctx, "name", "pip"), keep_tail=True).rstrip("?").strip()
        whole = _PATTERNS_BY_KIND["note_add"].match(kept)
        if whole is not None and len(whole["text"]) >= len(match["text"]):
            match = whole
    if is_guest(ctx):
        return Reply(GUEST_LINE)
    if keeps_nothing(ctx.state_dir):
        return Reply(SESSION_ONLY_LINE)
    store = Lists(ctx.state_dir)
    try:
        return await _act(kind, match, store)
    except OverflowError:
        return Reply("[concern] That's more than I can keep. Clear something out first.")
    except sqlite3.DatabaseError:
        return Reply("[concern] I couldn't open my lists just now. Nothing was changed.")


async def _act(kind, match, store):
    if kind == "which":
        names = store.names()
        if not names:
            return Reply("[neutral] You don't have any lists yet.")
        if len(names) == 1:
            return Reply(f"[neutral] You have one list: {spoken_list(names[0])}.")
        return Reply(f"[neutral] You have {count_words(len(names))} lists: {joined(names)}.")

    if kind == "note_add":
        note = " ".join(match["text"].split()).strip(" ,:")[:MAX_TEXT]
        store.add_note(note)
        return Reply(f"[joy] Noted: {note}.", logged="note.add")
    if kind == "note_read":
        notes = store.notes()
        if not notes:
            return Reply("[neutral] You don't have any notes.")
        if len(notes) == 1:
            return Reply(f"[neutral] You have one note: {notes[0]}.")
        shown = "; ".join(notes[:READ_NOTES])
        more = f"; and {count_words(len(notes) - READ_NOTES)} more" if len(notes) > READ_NOTES else ""
        return Reply(f"[neutral] You have {count_words(len(notes))} notes. Newest first: {shown}{more}.")
    if kind == "note_delete":
        deleted = store.delete_last_note()
        if deleted is None:
            return Reply("[neutral] You don't have any notes to delete.")
        return Reply(f"[neutral] I've deleted your last note: {deleted}.", logged="note.delete")
    if kind == "note_clear":
        count = len(store.notes())
        if not count:
            return Reply("[neutral] You don't have any notes to delete.")

        async def clear_notes():
            removed = store.clear_notes()
            return Reply(f"[neutral] Done. I've deleted {count_words(removed)} note{'s' if removed != 1 else ''}.",
                         logged="note.clear")
        return Reply(f"[neutral] Delete all {count_words(count)} of your notes? Say yes or no.",
                     confirm=Confirm(clear_notes, Reply("[neutral] Okay, I've kept your notes."), strict=True))

    if kind == "add":
        name, problem = _which(store, _list_of(match), create=True)
        if problem:
            return Reply(problem)
        items = split_items(match["items"])
        if not items:
            return None
        added, already = store.add(name, items)
        _LAST.clear()
        _LAST.update(state_dir=str(store.state_dir), list=name, items=added, at=time.monotonic())
        where = spoken_list(name)
        if added and already:
            return Reply(f"[joy] I've added {joined(added)} to {where}. {_cap(joined(already))} "
                         f"{'was' if len(already) == 1 else 'were'} already there.", logged="list.add")
        if added:
            return Reply(f"[joy] I've added {joined(added)} to {where}.", logged="list.add")
        return Reply(f"[neutral] {_cap(joined(already))} "
                     f"{'is' if len(already) == 1 else 'are'} already on {where}.")

    if kind == "read":
        name, problem = _which(store, _list_of(match))
        if problem:
            return Reply(problem)
        items = store.items(name)
        where = spoken_list(name)
        if items is None:
            return Reply(f"[neutral] You don't have a {name} list yet.")
        if not items:
            return Reply(f"[neutral] {_cap(where)} is empty.")
        if len(items) > READ_BACK:
            return Reply(f"[neutral] {_cap(where)} has {count_words(len(items))} things: "
                         f"{read_back(items)}.")
        return Reply(f"[neutral] {_cap(where)} has {read_back(items)}.")

    if kind == "check":
        name, problem = _which(store, _list_of(match))
        if problem:
            return Reply(problem)
        item = match["item"]
        if name in store.find(item):
            return Reply(f"[neutral] Yes, {item} is on {spoken_list(name)}.")
        return Reply(f"[neutral] No, {item} isn't on {spoken_list(name)}.")

    if kind == "undo":
        if (not _LAST.get("items") or _LAST.get("state_dir") != str(store.state_dir)
                or time.monotonic() - _LAST.get("at", 0) > UNDO_WITHIN):
            return Reply("[neutral] I'm not sure what to take off. Say the item and the list.")
        name, items = _LAST["list"], _LAST.pop("items")
        removed = [item for item in items if store.remove(name, item) is not None]
        if not removed:
            return Reply(f"[neutral] That's not on {spoken_list(name)} any more.")
        return Reply(f"[neutral] Okay, I've taken {joined(removed)} off {spoken_list(name)}.", logged="list.remove")

    if kind == "remove":
        item = match["item"]
        item = re.sub(r"^(?:the|my) ", "", item.strip(), flags=re.I)
        spoken = _list_of(match)
        if spoken:
            name = list_name(spoken)
            if store.items(name) is None:
                return Reply(f"[neutral] You don't have a {name} list.")
        else:
            places = store.find(item)
            if not places and re.match(r"(?:take|rub|knock)\b", match.string, re.I):
                # "Take the day off", "knock it off": not about a list at all.
                return None
            if not places:
                return Reply(f"[neutral] I can't find {item} on any of your lists.")
            if len(places) > 1:
                return Reply(f"[neutral] {_cap(item)} is on your {joined(places)} lists. "
                             f"Which one should I take it off?")
            name = places[0]
        removed = store.remove(name, item)
        if removed is None:
            return Reply(f"[neutral] {_cap(item)} isn't on {spoken_list(name)}.")
        left = store.items(name) or []
        tail = " That was the last thing on it." if not left else ""
        return Reply(f"[neutral] Okay, I've taken {removed} off {spoken_list(name)}.{tail}", logged="list.remove")

    if kind in ("clear", "drop"):
        name, problem = _which(store, _list_of(match))
        if problem:
            return Reply(problem)
        items = store.items(name)
        if items is None:
            return Reply(f"[neutral] You don't have a {name} list.")
        drop = kind == "drop"
        if not items and not drop:
            return Reply(f"[neutral] {_cap(spoken_list(name))} is already empty.")

        async def confirmed():
            removed = store.clear(name, drop=drop)
            if removed is None:
                return Reply(f"[neutral] {_cap(spoken_list(name))} has already gone.")
            if drop:
                return Reply(f"[neutral] Done. I've deleted {spoken_list(name)}.", logged="list.delete")
            return Reply(f"[neutral] Done. {_cap(spoken_list(name))} is empty.",
                         logged="list.clear")
        count = len(items)
        what = f"{count_words(count)} thing{'s' if count != 1 else ''}"
        everything = "the one thing" if count == 1 else f"all {what}"
        question = (f"[neutral] Delete {spoken_list(name)} and the {what} on it? Say yes or no." if drop
                    else f"[neutral] Clear {everything} off {spoken_list(name)}? Say yes or no.")
        return Reply(question, confirm=Confirm(confirmed, Reply(f"[neutral] Okay, I've left {spoken_list(name)} as it is."),
                                               strict=True))
    return None
