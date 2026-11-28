"""Bounded verbatim lookup in plain-text files already in the owner's Keep.

Question callers need installed-owner authorization and an open lid. The
night queue can open only a source already authorized for Extract, bound
to its immutable blob and current erasure generation.
"""

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .keep import Store, document_revision

MAX_TEXT = 128 * 1024
MAX_TOPIC = 80
MAX_EXCERPT = 360
MAX_EXCERPTS = 3
_QUESTION = re.compile(r"what does (?:the )?(.{1,140}?)( from my keep)? say about (.{1,80})[?.!]*\Z",
                       re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class Result:
    document: str
    excerpts: tuple[str, ...]


class MissingDocument(LookupError):
    """The spoken name has no match in the current Keep."""


def parse(text):
    """Return the exact name, topic, and whether the owner named the Keep."""
    match = _QUESTION.fullmatch(" ".join((text or "").strip().split()))
    return (match[1].strip(), match[3].strip(), bool(match[2])) if match else None


def _terms(topic):
    if not isinstance(topic, str) or not 1 <= len(topic) <= MAX_TOPIC or not topic.isprintable():
        raise ValueError("Ask about a short printable topic.")
    terms = tuple(word.casefold() for word in _WORD.findall(topic))
    if not 1 <= len(terms) <= 8:
        raise ValueError("Ask about one to eight topic words.")
    return terms


def passages(text):
    """Yield bounded original slices; no normalization changes quoted words."""
    for line in text.splitlines():
        start = 0
        for match in re.finditer(r"(?<=[.!?])\s+", line):
            yield line[start:match.start()].strip()
            start = match.end()
        yield line[start:].strip()


def _excerpt(passage, terms):
    matched = {}
    for word in _WORD.finditer(passage):
        term = word.group().casefold()
        if term not in terms:
            continue
        matched[term] = word.start()
        if len(matched) < len(terms):
            continue
        first = min(matched.values())
        if word.end() - first > MAX_EXCERPT:
            continue
        if len(passage) <= MAX_EXCERPT:
            return passage
        start = max(0, word.end() - MAX_EXCERPT)
        if start and not passage[start - 1].isspace():
            boundary = passage.find(" ", start, first)
            start = boundary + 1 if boundary != -1 else first
        end = min(len(passage), start + MAX_EXCERPT)
        return passage[start:end].strip()
    return None


async def find(state_dir, document, topic, *, owner_allowed=False):
    """Find direct excerpts from one exact current Keep item."""
    if owner_allowed is not True:
        raise PermissionError("Keep lookup is owner-only.")
    terms = frozenset(_terms(topic))

    def select():
        store = Store(state_dir)
        item = named(store, document)
        return store, item, document_revision(state_dir, item.digest)

    store, item, revision = await asyncio.to_thread(select)
    if revision is None:
        raise FileNotFoundError
    text = await extract_text(state_dir, item, store=store)
    found = await asyncio.to_thread(_match_passages, text, terms)
    current = await asyncio.to_thread(document_revision, state_dir, item.digest)
    if current is None or current[1] != revision[1]:
        raise FileNotFoundError
    return Result(current[0].display_name, found)


async def extract_text(state_dir, item, *, store=None):
    """Return verified text from an admitted Keep document without retaining it."""
    if Path(item.filename).suffix.casefold() == ".pdf":
        from . import keep_pdf

        text = await keep_pdf.extract(state_dir, item)
        validate_text(text)
        return text

    def read():
        return verified_text(store if store is not None else Store(state_dir), item)

    return await asyncio.to_thread(read)


def _match_passages(text, terms):
    excerpts = []
    for passage in passages(text):
        if excerpt := _excerpt(passage, terms):
            excerpts.append(excerpt)
            if len(excerpts) == MAX_EXCERPTS:
                break
    return tuple(excerpts)


def named(store, document):
    matches = store.named(document)
    if not matches:
        raise MissingDocument("I couldn't find that document in my keep.")
    if len(matches) != 1:
        raise ValueError("I couldn't find one exact kept document by that name.")
    return matches[0]


def verified_text(store, item):
    if Path(item.filename).suffix.casefold() not in (".txt", ".md"):
        raise ValueError("That document isn't plain text or Markdown.")
    if item.size > MAX_TEXT:
        raise ValueError("That plain-text document is too large for a bounded spoken lookup.")
    with store.open(item.digest) as source:
        raw = source.read(item.size + 1)
    if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.digest:
        raise OSError("The kept document did not verify.")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ValueError("That kept document is not safe UTF-8 plain text.") from None
    validate_text(text)
    return text


def validate_text(text):
    if any(unicodedata.category(character) == "Cc" and character not in "\n\r\t" for character in text):
        raise ValueError("That kept document contains unsafe control characters.")
