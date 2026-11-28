"""An ephemeral, text-only reference panel. Mind owns public network I/O."""

import asyncio
import http.client
import json
import textwrap
import unicodedata

from .weather import LookupDenied, fetch_reference

APP = "lookup"
TITLE = "Look it up"
MAX_QUERY = 120
MAX_REQUEST = 640
MAX_TEXT = 900
TILE_LINES = 9
TIMEOUT = 4.0
PRIVATE = {"status": "private"}
UNAVAILABLE = {"status": "unavailable"}
RESET = "\x1b[0m"
ACCENT, MUTED, BORDER = "\x1b[1;96m", "\x1b[90m", "\x1b[2;94m"
FIELD_WIDTH = 84


def query_text(value):
    if not isinstance(value, str) or len(value) > MAX_QUERY * 2 or any(not ch.isprintable() for ch in value):
        return ""
    value = " ".join(unicodedata.normalize("NFC", value).split())
    return value if 1 <= len(value) <= MAX_QUERY and any(ch.isalnum() for ch in value) else ""


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:limit]


def _width(value, columns):
    # Leave room in the console for full-width characters.
    return columns // 2 if any(unicodedata.east_asian_width(ch) in ("W", "F") for ch in value) else columns


async def search(query, permitted):
    """Find one article and return its plaintext introduction and citation."""
    query = query_text(query)
    if not query:
        return {"status": "empty"}
    if not permitted():
        return PRIVATE
    params = {"action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": 1, "gsrnamespace": 0,
              "prop": "extracts", "exintro": 1, "explaintext": 1, "exchars": MAX_TEXT,
              "format": "json", "formatversion": 2}
    try:
        data = await asyncio.wait_for(asyncio.to_thread(fetch_reference, params, permitted=permitted), TIMEOUT)
        if not permitted():
            return PRIVATE
        pages = data.get("query", {}).get("pages", [])
        page = pages[0] if pages else {}
        title, extract = _text(page.get("title"), MAX_QUERY), _text(page.get("extract"), MAX_TEXT)
        page_id = page.get("pageid")
        if not title or not extract or type(page_id) is not int or not 0 < page_id < 2**53:
            return {"status": "empty"}
        uri = f"https://en.wikipedia.org/?curid={page_id}"
        return {"status": "ok", "title": title, "text": extract, "source": uri}
    except LookupDenied:
        return PRIVATE
    except (OSError, http.client.HTTPException, ValueError, AttributeError, TypeError, IndexError):
        return UNAVAILABLE


async def _request(path, query):
    if path is None:
        return UNAVAILABLE

    async def exchange():
        reader, writer = await asyncio.open_unix_connection(path, limit=8192)
        try:
            wire = b"lookup " + json.dumps(query, ensure_ascii=False).encode("utf-8") + b"\n"
            if len(wire) > MAX_REQUEST:
                return {"status": "empty"}
            writer.write(wire)
            await writer.drain()
            response = json.loads(await reader.readline())
            return response if isinstance(response, dict) else UNAVAILABLE
        finally:
            writer.close()

    try:
        return await asyncio.wait_for(exchange(), TIMEOUT + 1.0)
    except (OSError, ValueError):
        return UNAVAILABLE


class Lookup:
    def __init__(self, path):
        self.path = path
        self.changed = asyncio.Event()
        self.entry = ""
        self.result = {"status": "ready"}
        self.pending = None
        self.offset = 0

    def submit(self, value):
        query = query_text(value)
        if query:
            self.entry = self.pending = query
            self.result = {"status": "searching"}
            self.offset = 0
            self.changed.set()

    def _rows(self):
        text = _text(self.result.get("text"), MAX_TEXT)
        return textwrap.wrap(text, width=_width(text, 90))

    def key(self, data):
        if data in (b"\r", b"\n"):
            self.submit(self.entry)
        elif data in (b"\x1b[A", b"\x1b[B", b"\0pgup", b"\0pgdn"):
            step = TILE_LINES if data in (b"\0pgup", b"\0pgdn") else 1
            direction = 1 if data in (b"\x1b[B", b"\0pgdn") else -1
            self.offset = min(max(0, self.offset + direction * step), max(0, len(self._rows()) - TILE_LINES))
            self.changed.set()
        elif data in (b"\x08", b"\x7f"):
            self.entry = self.entry[:-1]
            self.changed.set()
        elif len(data) == 1 and b" " <= data <= b"~" and len(self.entry) < MAX_QUERY:
            self.entry += data.decode("ascii")
            self.changed.set()

    async def refresh(self):
        self.changed.clear()
        query, self.pending = self.pending, None
        if query is not None:
            result = await _request(self.path, query)
            if self.pending is None:
                self.result = result
        return self.render()

    def render(self):
        entry = _text(self.entry, MAX_QUERY)
        width = _width(entry, FIELD_WIDTH) - 1
        if len(entry) > width:
            entry = "…" + entry[1 - width:]
        field = f"{entry + '_':<{FIELD_WIDTH}}"
        edge = "─" * (FIELD_WIDTH + 10)
        rows = [
            "",
            f"  {MUTED}Type a topic and press Enter to search Wikipedia.{RESET}",
            f"  {BORDER}╭{edge}╮{RESET}",
            f"  {BORDER}│{RESET}\x1b[40m {ACCENT}›\x1b[22;39m {field}\x1b[46;1;96m Enter "
            f"{RESET}{BORDER}│{RESET}",
            f"  {BORDER}╰{edge}╯{RESET}",
            "",
        ]
        status = self.result.get("status")
        if status == "ok":
            title = _text(self.result.get("title"), MAX_QUERY)
            headline = textwrap.shorten(title, width=_width(title, 94), placeholder="…")
            rows.extend([f"  {ACCENT}{headline}{RESET}", f"  {BORDER}{edge}{RESET}"])
            body = self._rows()
            rows.extend("    " + line for line in body[self.offset:self.offset + TILE_LINES])
            if self.offset + TILE_LINES < len(body):
                rows.append(f"    {MUTED}More text below.{RESET}")
            rows.extend(["", f"  \x1b[40;90m Source  Wikipedia {RESET}",
                         f"    {MUTED}{_text(self.result.get('source'), 90)}{RESET}"])
        elif status == "searching":
            rows.append(f"  {ACCENT}Looking up {entry}{RESET}")
        elif status == "empty":
            rows.append(f"  {MUTED}No article introduction found. Try a more specific topic.{RESET}")
        elif status == "private":
            rows.append(f"  {MUTED}Lookup is currently unavailable.{RESET}")
        elif status != "ready":
            rows.append(f"  {MUTED}Wikipedia did not return a usable result.{RESET}")
        footer = f"\x1b[23;3H{MUTED}Up / Down or Page Up / Down  Scroll    Esc  Close{RESET}"
        return ("\x1b[?25l\x1b[2J\x1b[H" + "\r\n".join(rows) + footer).encode("utf-8")
