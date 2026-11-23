"""Dreaming with the second model: extraction that code then checks.

At night, with the lid closed and on mains power, the night shift starts the
dream server (being-dream-cortex.service, the 1.7B model on 127.0.0.1:8081),
hands it the day's conversation a chunk at a time and stops it again.

- Every line is tagged with where it came from: `[E1234|owner|2026-11-23 19:02]`.
- A chunk holds 800-1,200 tokens of lines, behind a byte-stable instruction
  prefix of under 300 tokens, so the server reuses that prefix every time.
- The model answers in a flat JSON schema (no `$ref`), at temperature 0 with
  thinking off: up to 8 items, each a claim with its exact quote.
- `dream_verify` checks every item against the stored lines. What passes is
  kept only as a candidate (dream_store), never told as a fact until it
  recurs on a later night or the owner confirms it.

Only the owner's and Pip's own conversation lines are shown: no tombstones,
no read-backs of memory, no canned replies, nothing a guest said.
"""

import asyncio
import http.client
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from . import dream_verify
from .memory import _HISTORY

HOST, PORT = "127.0.0.1", 8081
SERVICE = "being-dream-cortex.service"
CHUNK_MIN_TOKENS = 800
CHUNK_MAX_TOKENS = 1200
# The model's whole context (being-dream-cortex.service --ctx-size): the
# chat template, the prefix, the chunk and the answer must all fit in it.
CONTEXT_TOKENS = 2048
TEMPLATE_TOKENS = 48
MAX_ITEMS = 8
ANSWER_TOKENS = 900
MIN_ANSWER_TOKENS = 256
REQUEST_SECONDS = 600.0
READY_SECONDS = 120.0
LINE_ROWS = 400  # lines read from the archive per chunk, at most

# Byte-stable: the server reuses it across chunks. Keep it under 300 tokens.
PREFIX = (
    "You read a log of conversation between a person (owner) and Pip, a small home computer. "
    "Each line starts with [E<id>|<speaker>|<time>].\n"
    "List up to 8 things the owner said that are worth remembering about the owner's life: facts, "
    "preferences, plans, events, and people they know.\n"
    "Rules:\n"
    "- Only use lines whose speaker is owner or other. Never use Pip's lines.\n"
    "- quote: copy the exact words from one line, at most 200 characters. Do not change them.\n"
    "- episode_id: the E<id> of that line.\n"
    "- claim: one short sentence, at most 160 characters, using only what the quote says. "
    "Every name, number and date in the claim must be in the quote.\n"
    "- modality: stated if said as true; reported if the owner reports what someone else said; "
    "hypothetical, joking or unsure otherwise.\n"
    "- time_ref: when it happens or happened, in the owner's words, or an empty string.\n"
    "- If nothing is worth remembering, return an empty list.\n"
    "Answer with JSON only."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(dream_verify.KINDS)},
                    "claim": {"type": "string", "maxLength": dream_verify.MAX_CLAIM},
                    "quote": {"type": "string", "maxLength": dream_verify.MAX_QUOTE},
                    "episode_id": {"type": "string", "pattern": "^E[1-9][0-9]{0,11}$"},
                    "speaker": {"type": "string", "enum": list(dream_verify.SPEAKERS)},
                    "modality": {"type": "string", "enum": list(dream_verify.MODALITIES)},
                    "time_ref": {"type": "string", "maxLength": dream_verify.MAX_TIME_REF},
                },
                "required": ["kind", "claim", "quote", "episode_id", "speaker", "modality", "time_ref"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def log_line(*parts):
    print(*parts, flush=True)


class DreamUnavailable(Exception):
    """The dream server is not running or did not answer usefully."""


class DreamAnswerInvalid(DreamUnavailable):
    """The server answered, but not with the agreed JSON: the answer was cut
    off at its token limit, or the request did not fit the context. At
    temperature 0 the same chunk would fail the same way again."""


_PUNCTUATION_RUN = re.compile(r"[^\w\s]+|_+")


def estimate_tokens(text):
    """A conservative count for Qwen's tokenizer: every digit is a token of
    its own, as is each run of punctuation, and other text is about four
    characters a token. A line tag like [E1234|owner|2026-11-23 19:02]
    costs about 25 tokens, not the 8 its length suggests."""
    digits = sum(char.isdigit() for char in text)
    punctuation = len(_PUNCTUATION_RUN.findall(text))
    other = len(text) - digits - sum(len(run) for run in _PUNCTUATION_RUN.findall(text))
    return max(1, digits + punctuation + (other + 3) // 4)


def answer_budget(chunk_tokens):
    """Tokens left for the answer once the prefix and a chunk are read: the
    dream server has 2,048 in all, and an answer cut short is no answer."""
    room = CONTEXT_TOKENS - TEMPLATE_TOKENS - PREFIX_TOKENS - chunk_tokens
    return max(MIN_ANSWER_TOKENS, min(ANSWER_TOKENS, room))


PREFIX_TOKENS = estimate_tokens(PREFIX)


@dataclass(frozen=True)
class Chunk:
    text: str
    lines: dict  # episode id -> dream_verify.Line
    through: int  # the highest episode id read, shown or not
    tokens: int
    first: int = 0  # what the first line alone costs


def tag(episode_id, speaker, ts, zone=None):
    when = datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%d %H:%M")
    return f"[E{episode_id}|{speaker}|{when}]"


def clean_line(text):
    """One printable line, so a tag can never be forged from inside a sentence."""
    text = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(text.replace("[E", "(E").split())


def owner_lines(db, owner, being, *, after, limit=LINE_ROWS):
    """(id, ts, speaker role, text) of conversation after `after`, oldest first."""
    rows = db.execute(
        f"SELECT e.id, e.ts, e.kind, e.speaker, e.text FROM episodes e WHERE {_HISTORY} AND e.id > ? "
        "AND ((e.kind = 'heard' AND e.speaker = ?) OR (e.kind = 'said' AND e.speaker = ?)) "
        "ORDER BY e.id LIMIT ?",
        (after, owner, being, limit),
    ).fetchall()
    return [(row_id, ts, "owner" if kind == "heard" else "being", text) for row_id, ts, kind, _, text in rows]


def build_chunk(rows, *, zone=None, min_tokens=CHUNK_MIN_TOKENS, max_tokens=CHUNK_MAX_TOKENS):
    """Pack lines, oldest first, into one chunk of at most `max_tokens`.

    Returns None when there are no rows. A chunk below `min_tokens` is only
    built from the last of the day's lines. A single line too long for a
    chunk is cut, and only its first part can be quoted.
    """
    if not rows:
        return None
    parts, lines, tokens, through = [], {}, 0, rows[0][0] - 1
    for row_id, ts, role, text in rows:
        line = f"{tag(row_id, role, ts, zone)} {clean_line(text)}"
        cost = estimate_tokens(line) + 1
        if parts and tokens + cost > max_tokens:
            break
        if cost > max_tokens:
            keep = max_tokens * 4
            while keep > 1 and estimate_tokens(line[:keep]) + 1 > max_tokens:
                keep = keep * 9 // 10
            line = line[:keep]
            cost = estimate_tokens(line) + 1
        parts.append(line)
        lines[row_id] = dream_verify.Line(row_id, role, text)
        tokens += cost
        through = row_id
    return Chunk("\n".join(parts), lines, through, tokens, estimate_tokens(parts[0]) + 1)


def request_body(chunk):
    return {
        "messages": [
            {"role": "system", "content": PREFIX},
            {"role": "user", "content": chunk.text},
        ],
        "temperature": 0,
        "top_k": 1,
        "max_tokens": answer_budget(chunk.tokens),
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "dream_items", "schema": SCHEMA}},
    }


class DreamClient:
    """A small blocking client for the dream server's chat endpoint."""

    def __init__(self, host=HOST, port=PORT, *, timeout=REQUEST_SECONDS):
        self.host, self.port, self.timeout = host, port, timeout

    def _request(self, method, path, body=None, timeout=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            payload = None if body is None else json.dumps(body).encode()
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = response.read(1 << 20)
            return response.status, data
        except (OSError, http.client.HTTPException) as exc:
            raise DreamUnavailable(f"dream server: {type(exc).__name__}") from None
        finally:
            connection.close()

    def healthy(self):
        try:
            status, _ = self._request("GET", "/health", timeout=2.0)
        except DreamUnavailable:
            return False
        return status == 200

    def extract(self, chunk):
        """The model's proposed items for `chunk` (unchecked), and its token counts."""
        status, data = self._request("POST", "/v1/chat/completions", request_body(chunk))
        if status == 400:
            # llama-server's answer to a prompt longer than its context.
            raise DreamAnswerInvalid("the dream server refused the request (400)")
        if status != 200:
            raise DreamUnavailable(f"dream server answered {status}")
        try:
            reply = json.loads(data)
        except ValueError:
            raise DreamUnavailable("the dream server's reply was not JSON") from None
        try:
            choice = reply["choices"][0]
            if choice.get("finish_reason") == "length":
                raise DreamAnswerInvalid("the dream answer was cut off at its token limit")
            items = json.loads(choice["message"]["content"])["items"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise DreamAnswerInvalid("the dream answer was not the agreed JSON") from None
        usage = reply.get("usage") if isinstance(reply, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return items, {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens")}


class DreamService:
    """Start and stop the dream server with systemd; never enabled at boot."""

    def __init__(self, client=None, *, systemctl=None, run=subprocess.run):
        self.client = client or DreamClient()
        self.systemctl = systemctl or shutil.which("systemctl")
        self.run = run

    def _ctl(self, verb, *, wait=True):
        if not self.systemctl:
            return False
        command = [self.systemctl, verb, SERVICE] if wait else [self.systemctl, "--no-block", verb, SERVICE]
        try:
            return self.run(command, capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def start(self, *, ready_seconds=READY_SECONDS, sleep=time.sleep):
        if self.client.healthy():
            return True
        if not self._ctl("start"):
            return False
        deadline = time.monotonic() + ready_seconds
        while time.monotonic() < deadline:
            if self.client.healthy():
                return True
            sleep(1.0)
        return False

    def stop(self, *, wait=True):
        """Stop the server. Without `wait` systemd is only asked to, so the
        event loop never waits while the conversation model needs the CPU."""
        return self._ctl("stop", wait=wait)


@dataclass
class NightTally:
    """Counts only, for the night's log line and the morning's report."""

    chunks: int = 0
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    unanswered: int = 0  # chunks the model could not answer, read again smaller
    passed_over: int = 0  # lines the model could not answer for even alone
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self):
        return dict(self.__dict__)


class DreamJob:
    """The night shift's dream: one chunk per `run_chunk`, resumable.

    The cursor is the last episode id read. A chunk's candidates are saved
    with the cursor in one transaction, so an interrupted chunk is simply
    read again next time. `interrupted()` stops the server at once.

    A chunk the model cannot answer within the context (temperature 0 would
    fail it the same way every night) is read again at half the size; a
    single line that still fails is passed over, so one awkward sentence can
    never stop every later night's dreaming.
    """

    def __init__(self, memory, owner, being, night, *, service=None, zone=None, store=None):
        from . import dream_store

        self.memory, self.owner, self.being, self.night = memory, owner, being, night
        self.service = service or DreamService()
        self.zone = zone
        self.store = store or dream_store
        self.cursor = None
        self.tally = NightTally()
        self._started = False
        self._starting = False
        self._cancel = threading.Event()
        self._limit = CHUNK_MAX_TOKENS

    def resume(self, cursor):
        if isinstance(cursor, int) and cursor >= 0:
            self.cursor = cursor

    def _after(self):
        # The archive's own cursor, saved with the candidates, is the truth:
        # after "forget everything" or a restore it starts again from there,
        # where a copy kept beside the archive would skip the new sentences.
        return self.store.dream_cursor(self.memory, self.owner)

    async def run_chunk(self, budget_s):
        night = self.night() if callable(self.night) else self.night
        rows = owner_lines(self.memory.db, self.owner, self.being, after=self._after())
        chunk = build_chunk(rows, zone=self.zone, max_tokens=self._limit)
        if chunk is None:
            await self._stop()
            return True
        if not any(line.speaker == "owner" for line in chunk.lines.values()):
            await self._advance(chunk.through, [], night)
            return False
        if not self._started:
            self._cancel.clear()
            self._starting = True
            try:
                self._started = await asyncio.to_thread(self._start)
            finally:
                self._starting = False
            if not self._started:
                raise RuntimeError("the dream server did not start")
        revision = self.memory.revision
        try:
            items, usage = await asyncio.to_thread(self.service.client.extract, chunk)
        except DreamAnswerInvalid as exc:
            self.tally.unanswered += 1
            if len(chunk.lines) > 1:
                # Never below the first line, which would cut it.
                self._limit = max(chunk.first, chunk.tokens // 2)
                log_line(f"dream: {exc}; reading {len(chunk.lines)} lines again in smaller pieces")
                return False
            self.tally.passed_over += len(chunk.lines)
            log_line(f"dream: {exc}; passing over {len(chunk.lines)} line(s)")
            await self._advance(chunk.through, [], night)
            self._limit = CHUNK_MAX_TOKENS
            return False
        except DreamUnavailable as exc:
            raise RuntimeError(str(exc)) from None
        accepted, rejected = dream_verify.check_all(items, chunk.lines)
        self.tally.chunks += 1
        self.tally.proposed += len(items) if isinstance(items, list) else 0
        self.tally.accepted += len(accepted)
        self.tally.rejected += len(rejected)
        self.tally.prompt_tokens += usage.get("prompt_tokens") or 0
        self.tally.completion_tokens += usage.get("completion_tokens") or 0
        if self.memory.revision != revision:
            # Something was forgotten or corrected while the model read: read again.
            return False
        await self._advance(chunk.through, accepted, night)
        self._limit = CHUNK_MAX_TOKENS
        return False

    async def _advance(self, through, accepted, night):
        # The save never waits for a lock (the conversation comes first), but
        # minutes of the model's reading are not thrown away for a moment's
        # contention with the vector indexer either.
        for attempt in range(20):
            try:
                self.store.save_candidates(self.memory, self.owner, accepted, night=night, through=through,
                                           zone=self.zone)
                break
            except sqlite3.OperationalError:
                if attempt == 19:
                    raise
                await asyncio.sleep(0.05)
        self.cursor = through

    def _start(self):
        """In a worker thread. An interrupt that lands while the server is
        still starting must not leave it running."""
        started = self.service.start()
        if started and self._cancel.is_set():
            self.service.stop(wait=False)
            return False
        return started

    async def _stop(self):
        if self._started:
            await asyncio.to_thread(self.service.stop)
            self._started = False

    def interrupted(self):
        """Called by the night shift when it stops or pauses this job: the
        server goes now, even if it is still starting."""
        self._cancel.set()
        if self._started or self._starting:
            self._started = False
            self.service.stop(wait=False)
