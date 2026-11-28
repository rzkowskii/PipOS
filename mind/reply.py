"""The types every code handler answers with (docs/PIP-FUNCTIONS.md 5.2).

A handler module (calc, units, lists, play, readback and the others listed in
mind/everyday.py) exposes `may_match(text) -> bool` and
`async handle(text, ctx) -> Reply | None`. A Reply is a fixed line said in
code; it never comes from the model.

`clean()` is the shared tidy-up handlers match against: it takes off "hey
Pip", "please", "can you" and the like, and keeps the words' own case so a
note or a list item is stored as it was said or typed.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping


@dataclass(frozen=True)
class Reply:
    line: str                       # "[tag] words", spoken and captioned
    keep: bool = False              # record the exchange in memory (default: no)
    confirm: "Confirm | None" = None
    action: str | None = None       # "poweroff" | "reboot" | None: run by the mind after line
    logged: str | None = None       # action-log kind, e.g. "list.add"
    settings_page: str | None = None
    private: bool = False           # local speech/captions only


@dataclass(frozen=True)
class Confirm:
    on_yes: Callable[[], Awaitable[Reply]]
    on_no: Reply = Reply("[neutral] Okay, I've left it.")
    seconds: float = 15.0
    # True for what cannot be undone (switching off, clearing a list): only a
    # plain yes confirms, never "okay" or "sure".
    strict: bool = False
    plain_yes: bool = False         # Only the word "yes" answers this question.
    after_speech: bool = False      # Begin expiry after Voice completes the question.


@dataclass(frozen=True)
class Context:
    now: datetime | None            # owner's local aware time; None if zone unknown
    clock_trusted: bool
    speaker: str                    # owner name, "guest" or "unknown"
    guest: bool
    state_dir: Path                 # /mind
    owner: str
    last_heard: str | None          # last hear.final this wake (RAM only)
    last_said: str | None           # last spoken line, tag stripped
    body: Mapping[str, Any]         # latest body.state plus body.care fields
    voice: Mapping[str, Any]        # latest voice.level
    send: Callable[..., Awaitable[None]]   # bus send(topic, **fields)
    name: str = "pip"               # the being's own name, taken off the front of what was said
    calc_panel: bool = False        # surface available for the calculator's live result
    ponder: Any = None              # the owner's night queue, when available
    current_body: Callable[[], Mapping[str, Any] | None] | None = None
    library_focus: Any = None       # the current page in the public offline guide


def is_guest(ctx):
    return bool(ctx.guest) or ctx.speaker == "guest"


def keeps_nothing(state_dir):
    """True when the owner chose "nothing after a restart" (session-only
    memory): then nothing an everyday skill does may be written to disk.
    An unreadable policy counts as session-only, as it does for memory."""
    try:
        from .retention import load_policy
    except ImportError:
        return False
    try:
        return load_policy(state_dir).mode == "session-only"
    except Exception:
        return True


_ADDRESS = r"(?:(?:hey|hi|ok|okay|oi)[, ]+)?{name}\b[,.!:]*\s*"
_LEADING = re.compile(
    r"^(?:(?:hey|hi|ok|okay|oh|so|um+|uh+|er+m?|well|right|alright)[, ]+)+"
    r"|^(?:please[, ]+|can you |could you |would you |will you |go ahead and |i want you to |i'd like you to )+",
    re.I,
)
_TRAILING = re.compile(r"(?:[, ]+(?:please|for me|thanks|thank you|right now|now|pip))+$", re.I)
# For words that are kept as said (a note): only politeness comes off the end,
# so "the plumber comes for me now" is not cut short.
_POLITE_TRAILING = re.compile(r"(?:[, ]+(?:please|thanks|thank you|pip))+$", re.I)


@lru_cache(maxsize=8)
def _address(name):
    return re.compile("^" + _ADDRESS.format(name=re.escape(name or "pip")), re.I)


def clean(text, name="pip", *, keep_tail=False):
    """'Hey Pip, could you add milk to my list, please?' -> 'add milk to my list'.

    Case is kept. Letters, digits, apostrophes, commas, colons, hyphens,
    full stops between digits, '%', '+', '*', '/', '^' and quotes survive;
    other punctuation becomes a space. With keep_tail, only "please",
    "thanks" and the name come off the end.
    """
    said = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    said = re.sub(r"(?<=\d),(?=\d{3}\b)", "", said)            # 1,000 -> 1000
    said = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", said)             # full stops, not decimal points
    said = re.sub(r"[^\w' ,:%+*/^\"=.-]", " ", said)
    said = " ".join(said.split())
    address = _address(name or "pip")
    for _ in range(3):
        before = said
        said = address.sub("", said)
        said = _LEADING.sub("", said)
        if said == before:
            break
    said = (_POLITE_TRAILING if keep_tail else _TRAILING).sub("", said).strip(" ,:-")
    return re.sub(r"\s*,\s*", ", ", said)
