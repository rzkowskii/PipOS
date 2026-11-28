"""Read-only views for saved lists, facts, journal and active route history.

The Mind serves one page at a time over a root-only local socket. The
hands mount of /mind is read-only and cannot create SQLite's WAL and SHM
sidecars when no writer has the lists database open.
"""

import asyncio
import json
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from unicodedata import east_asian_width

from . import app_lookup, lists
from .policy import web_lookup_ready
from .reply import keeps_nothing

TITLES = {"lists": "Lists", "memory": "Memory", "journal": "Journal", "mind": "Mind"}
SECTION_KEYS = {"lists": "Lists", "journal": "Sections"}
PAGE = 16
LIMIT = 16384
TIMEOUT = 0.6
UNAVAILABLE = {"status": "unavailable"}
PRIVATE = {"status": "private"}
RESET = "\x1b[0m"
ACCENT, MUTED, RULE = "\x1b[1;96m", "\x1b[90m", "\x1b[2;94m"
MARKS = {"lists": "○", "memory": "·", "journal": "●"}
PANEL_WIDTH = 102


def socket_path(bus_path):
    return Path(bus_path).with_name("organizer.sock")


def _plain(text, width=94):
    # SQLite is owner data, including control characters pasted in typed chat.
    if not isinstance(text, str):
        return ""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    remaining = width
    for index, char in enumerate(text):
        remaining -= 2 if east_asian_width(char) in ("W", "F") else 1
        if remaining < 1 and index + 1 < len(text):
            return text[:index] + "…"
    return text


def _lists(state_dir, group, offset):
    path = lists.database(state_dir)
    if not path.is_file():
        return _page(0, 0, "", 0, 0, [])
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)) as db:
        # The names, count and current page must come from the same read.
        db.execute("BEGIN")
        names = db.execute("SELECT id, name FROM lists ORDER BY id LIMIT ?", (lists.MAX_LISTS,)).fetchall()
        if not names:
            return _page(0, 0, "", 0, 0, [])
        group = min(group, len(names) - 1)
        list_id, title = names[group]
        count = db.execute("SELECT COUNT(*) FROM items WHERE list_id=?", (list_id,)).fetchone()[0]
        offset = _offset(count, offset)
        rows = db.execute("SELECT text FROM items WHERE list_id=? ORDER BY id LIMIT ? OFFSET ?",
                          (list_id, PAGE, offset)).fetchall()
    return _page(group, len(names), title, offset, count, [text for (text,) in rows])


def _offset(count, offset):
    return min(offset // PAGE, max(0, count - 1) // PAGE) * PAGE


def _page(group, groups, title, offset, count, rows):
    return {"status": "ok", "group": group, "groups": groups, "title": _plain(title),
            "offset": offset, "count": count, "rows": [_plain(row) for row in rows]}


def read_page(state_dir, app, group=0, offset=0, *, owner=None, zone=None):
    if app == "lists":
        return _lists(state_dir, group, offset)
    if app == "journal":
        from .app_journal import SECTIONS, read_section

        group = min(group, len(SECTIONS) - 1)
        title, rows = read_section(state_dir, owner, zone, group)
        offset = _offset(len(rows), offset)
        return _page(group, len(SECTIONS), title, offset, len(rows), rows[offset:offset + PAGE])
    if app != "memory":
        raise ValueError("Unknown organizer panel")
    # Match the existing Care view's scope and ordering. The conversation,
    # its archive, and dream journal have their own controls.
    from .care import remembered
    facts = remembered(state_dir, timeout=0.2)
    offset = _offset(len(facts), offset)
    return _page(0, 1, "Saved facts", offset, len(facts),
                 [text for _, text in facts[offset:offset + PAGE]])


async def start_server(path, state_dir, allowed, *, owner=None, timezone=lambda: None, routes=None,
                       lookup_scope=lambda: None):
    def permitted():
        return allowed() and not keeps_nothing(state_dir)

    lookup_lock = asyncio.Lock()

    async def lookup(line):
        scope = lookup_scope()

        def lookup_permitted():
            return scope is not None and scope == lookup_scope() and permitted() and web_lookup_ready(state_dir)

        if not lookup_permitted():
            return PRIVATE
        if len(line) > app_lookup.MAX_REQUEST or lookup_lock.locked():
            return UNAVAILABLE
        query = json.loads(line.removeprefix(b"lookup ").decode("utf-8"))
        async with lookup_lock:
            response = await app_lookup.search(query, lookup_permitted)
        return response if lookup_permitted() else PRIVATE

    async def page(line):
        if len(line) > 48:
            raise ValueError("Organizer request exceeded limit")
        app, group, offset = line.decode("ascii").split()
        if app not in TITLES or not group.isdecimal() or not offset.isdecimal():
            raise ValueError("Invalid organizer request")
        if not permitted():
            return PRIVATE
        if app == "mind":
            response = routes() if routes else UNAVAILABLE
        else:
            try:
                response = await asyncio.to_thread(read_page, state_dir, app, int(group), int(offset),
                                                   owner=owner, zone=timezone() if app == "journal" else None)
            except sqlite3.Error:
                response = UNAVAILABLE
        return response if permitted() else PRIVATE

    async def serve(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), TIMEOUT)
            response = await (lookup(line) if line.startswith(b"lookup ") else page(line))
            writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            await asyncio.wait_for(writer.drain(), TIMEOUT)
        except (asyncio.TimeoutError, OSError, ValueError):
            pass
        finally:
            writer.close()

    # Only the root-owned Mind and hands services share this socket.
    # Restrict access from bind, before accepting the first connection.
    path = Path(path)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        mask = os.umask(0o177)
        try:
            sock.bind(os.fspath(path))
        finally:
            os.umask(mask)
        return await asyncio.start_unix_server(serve, sock=sock, limit=app_lookup.MAX_REQUEST)
    except BaseException:
        sock.close()
        raise


async def fetch(path, app, group, offset):
    if path is None:
        return UNAVAILABLE

    async def exchange():
        reader, writer = await asyncio.open_unix_connection(path, limit=LIMIT)
        try:
            writer.write(f"{app} {group} {offset}\n".encode("ascii"))
            await writer.drain()
            result = json.loads(await reader.readline())
            return result if isinstance(result, dict) and result.get("status") in ("ok", "private") else UNAVAILABLE
        finally:
            writer.close()

    try:
        return await asyncio.wait_for(exchange(), TIMEOUT)
    except (asyncio.TimeoutError, OSError, ValueError):
        return UNAVAILABLE


class Organizer:
    """The hands-side cursor. Each refresh asks only for the visible page."""

    def __init__(self, app, path):
        self.app = app
        self.path = path
        self.group = self.offset = 0
        self.changed = asyncio.Event()
        self.result = UNAVAILABLE

    def key(self, data):
        if self.app == "mind":
            return
        group, offset = self.group, self.offset
        if data in (b"\x1b[D", b"\x1b[C") and self.app in SECTION_KEYS:
            direction = 1 if data == b"\x1b[C" else -1
            group = max(0, min(group + direction, self.result.get("groups", 0) - 1))
            if group != self.group:
                offset = 0
        elif data in (b"\x1b[A", b"\x1b[B", b"\0pgup", b"\0pgdn"):
            direction = 1 if data in (b"\x1b[B", b"\0pgdn") else -1
            offset = _offset(self.result.get("count", 0), max(0, offset + direction * PAGE))
        if (group, offset) != (self.group, self.offset):
            self.group, self.offset = group, offset
            self.changed.set()

    def slow_refresh(self):
        if self.app != "journal":
            return False
        from .app_journal import ACTIVITY

        return self.group == ACTIVITY

    async def refresh(self):
        self.changed.clear()
        group, offset = self.group, self.offset
        result = await fetch(self.path, self.app, group, offset)
        # A later key has priority if a page read was still in flight.
        if not self.changed.is_set():
            self.result = result
            if result["status"] == "ok" and self.app != "mind":
                self.group, self.offset = result["group"], result["offset"]
        return render(self.app, self.result)


def render(app, result):
    if app == "mind":
        from .app_mind import render as render_mind

        return render_mind(result)
    lines = []
    sections = SECTION_KEYS.get(app)
    if result["status"] == "ok":
        title, count, offset = result["title"], result["count"], result["offset"]
        if title:
            position = f"  [{result['group'] + 1}/{result['groups']}]" if sections else ""
            title = _plain(title, PANEL_WIDTH - len(position) - 2)
            lines.extend([f"  \x1b[46;1;96m {title} {RESET}{MUTED}{position}{RESET}",
                          f"  {RULE}{'─' * PANEL_WIDTH}{RESET}"])
            if count:
                rows = result["rows"][:PAGE]
                for index, row in enumerate(rows):
                    text = _plain(row, PANEL_WIDTH - 4)
                    backing = "\x1b[40m" if index % 2 == 0 and app != "journal" else ""
                    mark = f"{ACCENT}{MARKS[app]}\x1b[22;39m"
                    edge = "  " if app != "journal" else f"{RULE}│{RESET} "
                    lines.append(f"  {backing}{edge}{mark} {text:<{PANEL_WIDTH - 4}}{RESET}")
                lines.extend(["", f"  {MUTED}Showing {offset + 1}–{offset + len(rows)} of {count}{RESET}"])
            else:
                empty = {"lists": "This list is empty.",
                         "memory": "There are no saved facts.",
                         "journal": "No entries yet."}[app]
                lines.extend(["", f"  {MUTED}{empty}{RESET}"])
        else:
            lines.extend(["", f'  {MUTED}You have no lists yet. Try “Add milk to my shopping list”.{RESET}'])
    else:
        unavailable = {"lists": "Lists are unavailable right now.",
                       "memory": "Saved memory is unavailable right now.",
                       "journal": "Journal is unavailable right now."}[app]
        lines.extend(["", f"  {MUTED}{unavailable}{RESET}"])
    keys = f"Left / Right  {sections}    " if sections else ""
    footer = f"\x1b[23;3H{MUTED}{keys}Page Up / Down  Entries    Esc  Close{RESET}"
    return ("\x1b[?25l\x1b[2J\x1b[H" + "\r\n".join(lines) + footer).encode("utf-8")
