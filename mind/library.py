"""Bounded offline guide and opt-in Open English WordNet dictionary."""

import asyncio
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from .reply import Confirm, Reply, clean, is_guest

INSTALLED = Path("/usr/share/being/spoken-guide.json")
DEVELOPMENT = Path(__file__).resolve().parent.parent / "docs/manual/spoken-guide.json"
MAX_PACK = 32 * 1024
FOCUS_SECONDS = 75
DICTIONARY = Path("/models/library/oewn/dictionary.sqlite3")
DICTIONARY_ENABLE = Path("/mind/library/dictionary-enabled")
DICTIONARY_MAX_BYTES = 512 * 1024 * 1024
OEWN_SOURCE = "Open English WordNet"
OEWN_LICENSE = "CC BY 4.0"
ENCYCLOPEDIA = Path("/models/library/simple-en/articles.sqlite3")
ENCYCLOPEDIA_ENABLE = Path("/mind/library/simple-en-enabled")
ENCYCLOPEDIA_MAX_BYTES = 1024 * 1024 * 1024
SIMPLE_SOURCE = "Simple English Wikipedia"
SIMPLE_LICENSE = "CC BY-SA 4.0"
SIMPLE_MORE_MAX = 600
_EXPLICIT = re.compile(r"^(?:ask (?:the|your) (?:guide|manual)(?: about)?|"
                       r"(?:look up|find) in (?:the|your) (?:guide|manual)|"
                       r"(?:what does|what did) (?:the|your) (?:guide|manual) say about)\b[:, ]*(.*)$")
_KEY = re.compile(r"^what does (f ?[0-9]{1,2}|function \w+) do$")
_HOW = re.compile(r"^how (?:do|can) i (.{1,80})$")
_NEXT = frozenset({"next guide step", "next manual step", "next step in the guide"})
_DEFINE = re.compile(r"^(?:define ([a-z][a-z' -]{0,78})|what does ([a-z][a-z' -]{0,78}?) mean)$")
_ENCYCLOPEDIA = re.compile(
    r"^(?:look up (.{1,120}?) in (?:my|the) simple english library|"
    r"what does (?:simple english )?wikipedia say about (.{1,120}))$")
_SIMPLE_CHOICE = re.compile(r"^(?:simple english|wikipedia) option ([1-4]|one|two|three|four)$")
_SIMPLE_NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4"}
_SIMPLE_MORE = frozenset({"more from simple english", "more from wikipedia"})
_OTHER = frozenset({"which others", "what are the other options"})
_OTHER_MEANING = frozenset({"another meaning", "next dictionary meaning", "other dictionary meanings"})
_PRIVATE = re.compile(r"\b(?:my|our|your|you|yours|ours|mine)\b")
_DICT_CONTROL = {
    "enable my dictionary": "enable", "turn on my dictionary": "enable",
    "disable my dictionary": "disable", "turn off my dictionary": "disable",
}
_ENCYCLOPEDIA_CONTROL = {
    "enable my simple english library": "enable_simple",
    "disable my simple english library": "disable_simple",
}


@dataclass(frozen=True)
class Entry:
    summary: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class Definition:
    word: str
    part: str
    text: str


@lru_cache(maxsize=2)
def _load(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_PACK + 1)
        if len(data) > MAX_PACK:
            return {}
        pack = json.loads(data)
    except (OSError, ValueError):
        return {}
    if not isinstance(pack, dict) or pack.get("version") != 1:
        return {}
    entries = pack.get("entries")
    if not isinstance(entries, list) or len(entries) > 200:
        return {}
    result = {}
    for row in entries:
        if not isinstance(row, dict):
            return {}
        summary, steps, topics = row.get("summary"), row.get("steps", []), row.get("topics")
        if (not isinstance(summary, str) or not 1 <= len(summary) <= 360
                or not isinstance(topics, list) or not topics or len(topics) > 16
                or any(not isinstance(topic, str) or not 1 <= len(topic) <= 80 for topic in topics)
                or not isinstance(steps, list) or len(steps) > 12
                or any(not isinstance(step, str) or not 1 <= len(step) <= 360 for step in steps)):
            return {}
        entry = Entry(summary, tuple(steps))
        for topic in topics:
            if topic in result:
                return {}
            result[topic] = entry
    return result


class Focus:
    """Guide steps and exact library continuations stay in RAM for one speaker/wake."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.generation = 0
        self.lookup = None
        self.clear()

    def clear(self):
        self.generation += 1
        self.speaker = None
        self.until = 0
        self.steps = ()
        self.options = ()
        self.option_query = ""
        self.article_title = ""
        self.article_more = ""
        self.article_snapshot = ""
        self.definition_query = ""
        self.definition_rows = ()
        self.definition_next = 0

    def begin_lookup(self):
        self.lookup = None

    def take_lookup(self):
        result, self.lookup = self.lookup, None
        return result

    def note(self, source, outcome):
        self.lookup = {"source": source, "outcome": outcome}

    def current(self, speaker):
        return speaker == self.speaker and self.clock() < self.until

    def open(self, entry, speaker):
        self.clear()
        self.steps = entry.steps
        self.speaker = speaker
        self.until = self.clock() + FOCUS_SECONDS
        more = " Say next guide step to go through the steps." if self.steps else ""
        return Reply(f"[neutral] My guide says: {entry.summary}{more}")

    def disambiguate(self, titles, speaker, query=""):
        self.clear()
        self.options = tuple(titles[:4])
        self.option_query = query
        self.speaker = speaker
        self.until = self.clock() + FOCUS_SECONDS
        choices = "; ".join(f"option {index}, {title}" for index, title in enumerate(self.options, 1))
        return Reply(f"[neutral] Simple English Wikipedia has these exact entries: {choices}. "
                     "Say Simple English option and its number. [soft] Source: Simple English Wikipedia.")

    def choose(self, number, speaker):
        if speaker != self.speaker or self.clock() >= self.until or not 1 <= number <= len(self.options):
            return None
        title = self.options[number - 1]
        self.clear()
        return title

    def open_article(self, title, spoken, more, speaker, snapshot=""):
        self.clear()
        self.article_title, self.article_more = title, _short_excerpt(more)
        self.article_snapshot = snapshot
        self.speaker = speaker
        self.until = self.clock() + FOCUS_SECONDS
        continuation = " Say more from Simple English for the next excerpt." if self.article_more else ""
        return Reply(f"[neutral] Simple English Wikipedia says: {spoken} {_snapshot_line(snapshot)}{continuation} "
                     f"[soft] Source: {title}.")

    def continuation(self, speaker):
        if speaker != self.speaker or self.clock() >= self.until or not self.article_more:
            return None
        return self.article_title

    def more(self, speaker):
        if speaker != self.speaker or self.clock() >= self.until or not self.article_more:
            return Reply("[neutral] Ask me to look up an exact Simple English title first.")
        title, more, snapshot = self.article_title, self.article_more, self.article_snapshot
        self.clear()
        return Reply(f"[neutral] Simple English Wikipedia continues: {more} {_snapshot_line(snapshot)} "
                     f"[soft] Source: {title}.")

    def open_definition(self, query, rows, speaker):
        self.clear()
        self.definition_query = query
        self.definition_rows = rows
        self.speaker = speaker
        self.until = self.clock() + FOCUS_SECONDS
        return self.next_definition(speaker)

    def next_definition(self, speaker):
        if not self.current(speaker) or not self.definition_rows:
            return Reply("[neutral] Ask me to define a word with more than one meaning first.")
        if self.definition_next >= len(self.definition_rows):
            return Reply("[neutral] That was the last meaning I have for this word.")
        entry = self.definition_rows[self.definition_next]
        self.definition_next += 1
        more = (" Say another meaning to hear the next one."
                if self.definition_next < len(self.definition_rows) else "")
        return Reply(f"[neutral] My Open English WordNet dictionary says: {entry.word}, "
                     f"{entry.part}: {entry.text}{more}")

    def next(self, speaker):
        if speaker != self.speaker or self.clock() >= self.until or not self.steps:
            return Reply("[neutral] Ask a question from my guide first.")
        first, *rest = self.steps
        self.steps = tuple(rest)
        self.until = self.clock() + FOCUS_SECONDS
        more = " Say next guide step to continue." if rest else ""
        return Reply(f"[neutral] My guide says: {first}{more}")


def _mode(text):
    if text in _ENCYCLOPEDIA_CONTROL:
        return _ENCYCLOPEDIA_CONTROL[text], ""
    if text in _DICT_CONTROL:
        return _DICT_CONTROL[text], ""
    if text in _NEXT:
        return "next", ""
    if text in _SIMPLE_MORE:
        return "encyclopedia_more", ""
    if text in _OTHER:
        return "others", ""
    if text in _OTHER_MEANING:
        return "definition_more", ""
    if match := _SIMPLE_CHOICE.fullmatch(text):
        return "encyclopedia_choice", _SIMPLE_NUMBERS.get(match[1], match[1])
    if match := _EXPLICIT.fullmatch(text):
        return "explicit", match[1].removeprefix("the ")
    if match := _KEY.fullmatch(text):
        return "implicit", re.sub(r"^f ", "f", match[1])
    if match := _HOW.fullmatch(text):
        return "implicit", match[1]
    if match := _DEFINE.fullmatch(text):
        topic = " ".join((match[1] or match[2]).split())
        if not _PRIVATE.search(topic):
            return ("define" if match[1] else "define_implicit"), topic
    if match := _ENCYCLOPEDIA.fullmatch(text):
        topic = " ".join((match[1] or match[2]).split())
        if not _PRIVATE.search(topic):
            return "encyclopedia", topic
    return "", ""


def _short_excerpt(text):
    """Keep a continuation to one short spoken block without inventing prose."""
    end = 0
    for match in re.finditer(r"[.!?](?=\s|$)", text):
        if match.end() > SIMPLE_MORE_MAX:
            break
        end = match.end()
    return text[:end]


def _snapshot_line(snapshot):
    try:
        day = date.fromisoformat(snapshot)
        if snapshot == day.isoformat():
            return f"My saved copy is from {day.day} {day:%B %Y}."
    except (ValueError, TypeError):
        pass
    return "As of my saved copy."


def _guide():
    path = INSTALLED if INSTALLED.is_file() else DEVELOPMENT
    return _load(path)


def may_match(text):
    mode, topic = _mode(clean(text).casefold())
    return mode in ("explicit", "next", "define", "define_implicit", "definition_more", "others",
                    "enable", "disable", "encyclopedia",
                    "encyclopedia_choice", "encyclopedia_more",
                    "enable_simple", "disable_simple") or (
        mode == "implicit" and topic in _guide())


def _dictionary(path=DICTIONARY, enabled=DICTIONARY_ENABLE):
    """Open a qualified dictionary read-only, or return (None, reason).

    Presence is not consent: the owner-selected marker is separate from the
    signed pack. Metadata is checked even after pack authentication so a
    differently licensed lexical database cannot silently occupy this route.
    """
    if not Path(enabled).is_file():
        return None, "disabled"
    path = Path(path)
    db = None
    try:
        info = path.stat()
        if not path.is_file() or path.is_symlink() or not 0 < info.st_size <= DICTIONARY_MAX_BYTES:
            return None, "invalid"
        from . import model_pack
        trusted, _ = model_pack.trusted("library", [path])
        if not trusted:
            return None, "untrusted"
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True, timeout=0.2)
        metadata = dict(db.execute("SELECT key, value FROM metadata WHERE key IN ('source', 'license')"))
        columns = {row[1] for row in db.execute("PRAGMA table_info(definitions)")}
        if metadata != {"source": OEWN_SOURCE, "license": OEWN_LICENSE} or not {
                "word", "word_key", "part_of_speech", "definition", "sense_rank"}.issubset(columns):
            db.close()
            return None, "metadata"
        return db, "ready"
    except (OSError, sqlite3.Error, ValueError):
        if db is not None:
            db.close()
        return None, "unavailable"


def definitions(word, *, path=DICTIONARY, enabled=DICTIONARY_ENABLE):
    db, status = _dictionary(path, enabled)
    if db is None:
        return status, ()
    try:
        rows = db.execute("""SELECT word, part_of_speech, definition FROM definitions
                             WHERE word_key=? ORDER BY sense_rank, rowid LIMIT 4""",
                          (word.casefold(),)).fetchall()
    except sqlite3.Error:
        return "invalid", ()
    finally:
        db.close()
    found = tuple(Definition(*row) for row in rows
                  if all(isinstance(value, str) and value for value in row)
                  and len(row[2]) <= 500)
    return "ready", found


def article(title, *, path=ENCYCLOPEDIA, enabled=ENCYCLOPEDIA_ENABLE, include_snapshot=False):
    """An exact title or corpus redirect only; fuzzy matches remain gated."""
    if not Path(enabled).is_file():
        return "disabled", None
    path = Path(path)
    db = None
    try:
        info = path.stat()
        if path.is_symlink() or not path.is_file() or not 0 < info.st_size <= ENCYCLOPEDIA_MAX_BYTES:
            return "invalid", None
        from . import model_pack
        trusted, _ = model_pack.trusted("library_simple", [path])
        if not trusted:
            return "untrusted", None
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True, timeout=0.2)
        metadata = dict(db.execute("SELECT key, value FROM metadata WHERE key IN ('source', 'license', 'snapshot', 'credit', 'schema')"))
        if metadata.get("source") != SIMPLE_SOURCE or metadata.get("license") != SIMPLE_LICENSE \
                or not metadata.get("snapshot") or not metadata.get("credit"):
            return "metadata", None
        key = " ".join(title.replace("_", " ").split()).casefold()
        row = db.execute("""SELECT title, spoken, more FROM articles WHERE title_key=?
                            UNION ALL SELECT a.title, a.spoken, a.more FROM redirects r
                            JOIN articles a ON a.title_key=r.target_key WHERE r.title_key=? LIMIT 1""",
                         (key, key)).fetchone()
        alternatives = ()
        if row is None and metadata.get("schema") == "2":
            columns = {column[1] for column in db.execute("PRAGMA table_info(alternatives)")}
            if not {"query_key", "title_key"}.issubset(columns):
                return "metadata", None
            alternatives = tuple(db.execute(
                """SELECT a.title, a.spoken, a.more FROM alternatives x
                   JOIN articles a ON a.title_key=x.title_key
                   WHERE x.query_key=? ORDER BY a.title_key LIMIT 5""", (key,)).fetchall())
            if len(alternatives) == 1:
                row = alternatives[0]
        if row is None:
            return (("ambiguous", tuple(value[0] for value in alternatives[:4]))
                    if len(alternatives) > 1 else ("ready", None))
        if not all(isinstance(value, str) for value in row) or not 1 <= len(row[1]) <= 600 or len(row[2]) > 1200:
            return "invalid", None
        if include_snapshot:
            row = (*row, metadata["snapshot"])
        return "ready", row
    except (OSError, sqlite3.Error, ValueError):
        return "unavailable", None
    finally:
        if db is not None:
            db.close()


async def handle(text, ctx):
    mode, topic = _mode(clean(text, getattr(ctx, "name", "pip")).casefold())
    if not mode:
        return None
    focus = getattr(ctx, "library_focus", None) or Focus()
    state_dir = Path(getattr(ctx, "state_dir", "/mind"))
    dictionary_enabled = state_dir / "library" / DICTIONARY_ENABLE.name
    encyclopedia_enabled = state_dir / "library" / ENCYCLOPEDIA_ENABLE.name
    if mode in ("enable", "disable", "enable_simple", "disable_simple"):
        if is_guest(ctx) or ctx.speaker != ctx.owner:
            return Reply("[neutral] Only my owner can change my offline library settings.")

        async def change():
            from . import library_pack
            try:
                if mode == "enable":
                    await asyncio.to_thread(library_pack.enable, dictionary_enabled)
                    focus.clear()
                    return Reply("[neutral] My Open English WordNet dictionary is enabled.")
                if mode == "disable":
                    await asyncio.to_thread(library_pack.disable, dictionary_enabled)
                    focus.clear()
                    return Reply("[neutral] My dictionary is disabled. Its signed pack is still installed.")
                if mode == "enable_simple":
                    await asyncio.to_thread(library_pack.enable_simple, encyclopedia_enabled)
                    focus.clear()
                    return Reply("[neutral] My Simple English library is enabled.")
                await asyncio.to_thread(library_pack.disable, encyclopedia_enabled)
                focus.clear()
                return Reply("[neutral] My Simple English library is disabled. Its signed pack is still installed.")
            except (OSError, ValueError):
                return Reply("[neutral] I couldn't verify and change that offline library pack.")

        verb = "enable" if mode.startswith("enable") else "disable"
        thing = "offline dictionary" if mode in ("enable", "disable") else "Simple English library"
        return Reply(f"[neutral] {verb.capitalize()} my {thing}?",
                     confirm=Confirm(change))
    if mode == "next":
        return focus.next(ctx.speaker)
    if mode == "others" and not focus.current(ctx.speaker):
        return None
    if mode == "others" and focus.options:
        query = focus.option_query
        options = focus.options
        generation = focus.generation
        status, found = await asyncio.to_thread(article, query, enabled=encyclopedia_enabled)
        if generation != focus.generation:
            return None
        if status != "ambiguous" or found != options:
            focus.clear()
            return Reply("[neutral] I couldn't verify those Simple English choices anymore.")
        return focus.disambiguate(options, ctx.speaker, query)
    if mode in ("others", "definition_more"):
        if not focus.current(ctx.speaker) or not focus.definition_rows:
            return (None if mode == "others" else
                    Reply("[neutral] Ask me to define a word with more than one meaning first."))
        generation = focus.generation
        status, found = await asyncio.to_thread(definitions, focus.definition_query, enabled=dictionary_enabled)
        if generation != focus.generation:
            return None
        if status != "ready" or found != focus.definition_rows:
            focus.clear()
            return Reply("[neutral] I couldn't continue from my enabled, verified dictionary.")
        return focus.next_definition(ctx.speaker)
    if mode == "encyclopedia_more":
        title = focus.continuation(ctx.speaker)
        if title is None:
            return Reply("[neutral] Ask me to look up an exact Simple English title first.")
        generation = focus.generation
        status, found = await asyncio.to_thread(article, title, enabled=encyclopedia_enabled, include_snapshot=True)
        if generation != focus.generation:
            return None
        if (status != "ready" or found is None or found[0] != title
                or _short_excerpt(found[2]) != focus.article_more or found[3] != focus.article_snapshot):
            focus.clear()
            return Reply("[neutral] I couldn't continue from my enabled, verified Simple English library.")
        return focus.more(ctx.speaker)
    if mode == "encyclopedia_choice":
        selected = focus.choose(int(topic), ctx.speaker)
        if selected is None:
            return Reply("[neutral] Ask me to look up an exact Simple English title first.")
        generation = focus.generation
        status, found = await asyncio.to_thread(article, selected, enabled=encyclopedia_enabled, include_snapshot=True)
        if generation != focus.generation:
            return None
        if status != "ready" or found is None:
            return Reply("[neutral] I couldn't open that verified Simple English entry.")
        title, spoken, more, snapshot = found
        return focus.open_article(title, spoken, more, ctx.speaker, snapshot)
    if mode in ("define", "define_implicit"):
        generation = focus.generation
        status, found = await asyncio.to_thread(definitions, topic, enabled=dictionary_enabled)
        if generation != focus.generation:
            return None
        outcome = ("hit" if found else "miss") if status == "ready" else (
            "disabled" if status == "disabled" else "unavailable")
        focus.note("dictionary", outcome)
        if mode == "define_implicit" and (status != "ready" or not found):
            return None
        if status == "disabled":
            return Reply("[neutral] My optional dictionary is not enabled.")
        if status != "ready":
            return Reply("[neutral] I couldn't open my verified dictionary.")
        if not found:
            return Reply(f"[neutral] I don't have a definition for {topic} in my dictionary.")
        return focus.open_definition(topic, found, ctx.speaker)
    if mode == "encyclopedia":
        generation = focus.generation
        status, found = await asyncio.to_thread(article, topic, enabled=encyclopedia_enabled, include_snapshot=True)
        if generation != focus.generation:
            return None
        if status == "disabled":
            focus.note("simple-en", "disabled")
            return Reply("[neutral] My optional Simple English library is not enabled.")
        if status == "ambiguous" and found:
            focus.note("simple-en", "ambiguous")
            return focus.disambiguate(found, ctx.speaker, topic)
        if status != "ready":
            focus.note("simple-en", "unavailable")
            return Reply("[neutral] I couldn't open my verified Simple English library.")
        if found is None:
            focus.note("simple-en", "miss")
            return Reply(f"[neutral] I don't have {topic} in my Simple English library.")
        focus.note("simple-en", "hit")
        title, spoken, more, snapshot = found
        return focus.open_article(title, spoken, more, ctx.speaker, snapshot)
    guide = _guide()
    if entry := guide.get(topic):
        focus.note("guide", "hit")
        return focus.open(entry, ctx.speaker)
    focus.note("guide", "miss" if guide else "unavailable")
    if mode == "explicit":
        if not guide:
            return Reply("[neutral] I couldn't open my guide.")
        return Reply("[neutral] I don't have that topic in my guide.")
    return None
