"""The hands organ: Pip's workshop terminal.  python3 -m mind.hands

See docs/design/voice-apps.md. hands holds the terminal session, types into it,
writes code (from the recipe book or the model's slot 1), runs it when asked and
reports what happened. The mind matches the spoken commands (mind/app_commands.py)
and speaks the short lines; hands never speaks.

What never crosses the bus: keystrokes, script text, program output. The bus
carries only the owner's request text (already on it as hear.final), short
status, and one output line of at most 80 characters when asked.

The shell is caged: a transient systemd unit per session, as the unprivileged
being-shell user, in a RAM-only /workshop, with no network and no view of /run.
The command checks below are for honesty and for the small model's mistakes.
They are not the security boundary: the cage is.

Everything here is stdlib Python. The cage, the renderer and the model sit
behind small classes so the whole organ runs on a Mac with a local shell on a
real pty, a recording renderer and a fake llama-server (tests/test_hands*.py).
"""

import argparse
import ast
import asyncio
import contextvars
import errno
import fcntl
import hashlib
import http.client
import json
import os
import random
import re
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .bus import Bus


# -- places and limits ----------------------------------------------------

WORKSHOP = Path("/workshop")
RUN_DIR = Path("/run/being-hands")
PANEL_DIR = Path("/run/being-panel")
SHELLRC = "/usr/lib/being/workshop/shellrc"
WORKSHOP_BIN = "/usr/lib/being/workshop/bin"
SHELF_DIR = "/usr/share/being/workshop/shelf"
APPS_DISABLED = Path("/mind/apps.disabled")
CONSOLE = "/usr/bin/being-console"
MODEL_LABEL = "Qwen3.5 0.8B"

SUBS = (
    "app.open", "app.close", "app.minimise", "app.restore", "app.privacy",
    "term.task", "term.control", "term.read", "term.forget",
    "state.changed", "body.lid", "body.state", "organ.up", "organ.down",
    "hear.wake", "hear.speech_start", "key.interrupt",
)

APPS = ("terminal", "monitor", "clock")
# The face composites the panels; being-local-screen (the Try menu) conflicts
# with it, so while the menu has the screen no panel can be seen.
FACE_UNIT = "being-face.service"
LOCAL_SCREEN_UNIT = "being-local-screen.service"
SCREEN_UNITS = (FACE_UNIT, LOCAL_SCREEN_UNIT)
FACE_UP = ("active", "activating", "reloading")
# The screen probe sits in front of every open, inside the mind's OPEN_WAIT
# (4 s), and hands runs at a low CPU weight: it must answer well before the
# mind gives up, and a probe that times out lets the open go ahead.
SCREEN_PROBE_SECONDS = 1.0
SMALL_APPS = ("monitor", "clock")   # never minimised: they close, and at most one is open
SMALL_TITLES = {"monitor": "How I'm feeling", "clock": "Pip's clock"}
APP_FRAME = 1.0             # seconds between monitor and clock frames
GRID_COLUMNS, GRID_ROWS = 107, 23
PANEL_SIZE = (1280, 640)

MAX_REQUEST = 4096          # bytes of one request's text
MAX_QUEUE = 16384           # characters waiting to be typed
MAX_LINE = 100              # a model line longer than this cancels the job
MAX_LINES = 30              # lines in a model-written script
MAX_FILE = 16384            # bytes hands reads back when checking a file
RUN_LIMIT = 60.0            # seconds for a command Pip starts
APP_RUN_LIMIT = 15 * 60.0   # a shelf game the owner plays: redraws all the time, so no output limit
OUTPUT_LINES, OUTPUT_BYTES = 200, 16384
COUNTDOWN = 4               # seconds before a one-liner Pip wrote is run
UNDO_SECONDS = 30.0
HOLD_SECONDS = 6.0          # a wake hold releases itself after this much quiet
HUMAN_QUIET = 1.5           # Pip resumes this long after the owner's last key
STATUS_EVERY = 2.0
STALL_SECONDS = 20.0        # no token for this long while writing: pause
KILL_GRACE = 2.0            # Ctrl-C first, then kill all but the shell
STOP_FORGET_SECONDS = 3.5   # the clear on stop, inside being-hands' TimeoutStopSec=5
TICK = 0.04                 # 25 pty writes a second at most
RELAY_RATE = 256 * 1024     # bytes a second relayed from the shell
TRANSCRIPT = 64 * 1024
MINIMISED_IDLE_CLOSE = 20 * 60.0
RECIPE_RATE = 30.0
MODEL_START_DELAY, MODEL_LEAD = 0.8, 1.0
MIN_RATE, MAX_RATE, FLUSH_RATE = 8.0, 80.0, 150.0
LINE_BEAT = 0.12

HOT_C, COOL_C = 90.0, 80.0  # freeze the cage at or above, thaw below (guesses until M10)
LOW_BATTERY = 15            # percent: unplugged at or below this, no new runs

ACTIVE = ("reading", "writing", "held", "paused", "checking", "countdown", "running", "frozen")
REST_REASONS = ("lid", "sleep", "heat")
REST_REPLY = {"lid": "lid_closed", "sleep": "asleep", "heat": "too_warm"}
GENERATING = ("reading", "writing", "held")
BUSY = ("countdown", "running", "frozen")


# The `req` of the bus request being handled, echoed in replies so the mind
# can match them. Tasks started while handling inherit it; the tick does not.
_REQ = contextvars.ContextVar("req", default=None)


def log(*parts):
    # Only ids, phases, exit codes and durations: never typed text, code or output.
    print("hands:", *parts, file=sys.stderr, flush=True)


# -- what Pip types --------------------------------------------------------

def sanitise(text, limit=MAX_REQUEST):
    """Drop every control byte except newline. Tabs become four spaces, so the
    shell's line editing never sees a completion key. ESC, NUL, ^C, ^D, ^Z and
    DEL are gone."""
    out = []
    for ch in str(text).replace("\r\n", "\n").replace("\r", "\n"):
        code = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif ch == "\t":
            out.append("    ")
        elif code < 0x20 or code == 0x7F or 0x80 <= code < 0xA0 or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:
            continue
        else:
            out.append(ch)
    return "".join(out)[:limit]


NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}\.(?:py|sh|txt)\Z")
_STEM_SKIP = {
    "a", "an", "the", "that", "which", "script", "program", "code", "me", "my", "to", "and", "of", "for",
    "write", "make", "it", "some", "little", "small", "python", "shell", "please", "from", "with", "in",
    "on", "out", "up", "is", "be", "you", "can", "will", "would", "yourself", "scratch", "then",
}


def name_stem(request, default="script"):
    words = [w for w in re.findall(r"[a-z]+", request.lower()) if w not in _STEM_SKIP]
    stem = "_".join(words[:2])[:18].strip("_")
    return stem if re.fullmatch(r"[a-z][a-z0-9_]*", stem or "") else default


def unique_name(stem, ext, exists):
    """A new file name: never an existing one. spiral.py, then spiral-2.py."""
    stem = re.sub(r"[^a-z0-9_-]", "", stem.lower())[:18] or "script"
    if not stem[0].isalpha():
        stem = "s" + stem[:17]
    name = f"{stem}.{ext}"
    number = 2
    while exists(name):
        name = f"{stem}-{number}.{ext}"
        number += 1
    if not NAME.match(name):
        raise ValueError("no usable name")
    return name


def end_word(rng=None):
    return "PIP_END_" + "".join((rng or random.SystemRandom()).choice("0123456789abcdef") for _ in range(4))


def heredoc_open(name, word):
    # Quoted delimiter: nothing in the body expands. set -C in the shellrc
    # makes the redirect fail rather than overwrite.
    if not NAME.match(name) or not re.fullmatch(r"PIP_END_[0-9a-f]{4}", word):
        raise ValueError("bad heredoc")
    return f"cat > {name} <<'{word}'\n"


def heredoc(name, body, word):
    """The whole file-writing text, or ValueError if a body line is the end word."""
    lines = body.rstrip("\n").split("\n")
    if any(line.strip() == word for line in lines):
        raise ValueError("end word in body")
    return heredoc_open(name, word) + "\n".join(lines) + f"\n{word}\n"


# -- checks: honesty and the small model's mistakes, not security ------------

ALLOWED_IMPORTS = frozenset({
    "math", "random", "time", "datetime", "string", "itertools", "functools", "collections", "statistics",
    "textwrap", "json", "re", "sys", "os", "pathlib", "calendar", "fractions", "decimal", "colorsys",
    "operator", "dataclasses", "enum", "typing", "heapq", "bisect", "array", "struct", "hashlib",
    "base64", "uuid", "glob", "csv", "zlib", "cmath", "pprint", "copy", "secrets", "shutil",
})
REFUSED_CALLS = frozenset({"eval", "exec", "compile", "__import__", "input", "breakpoint"})
REFUSED_ATTRS = frozenset({
    "system", "popen", "spawn", "spawnl", "spawnv", "spawnlp", "spawnvp", "execl", "execv", "execve",
    "execlp", "execvp", "execvpe", "fork", "forkpty", "kill", "killpg", "setuid", "setgid",
    "remove", "unlink", "rmdir", "removedirs", "rmtree", "chmod", "chown", "symlink", "link",
})


@dataclass
class Check:
    ok: bool
    reason: str = ""
    fixes: tuple = ()        # shell lines Pip types, visibly, to fix the file
    missing: tuple = ()      # modules the free import fix adds
    trimmed: int = 0         # trailing prose lines removed


def _bound_names(tree):
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def python_policy(tree):
    """Return a reason the script is refused, or ''."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    return f"it imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                return f"it imports {node.module or 'a relative module'}"
            if any(alias.name in REFUSED_ATTRS or alias.name in REFUSED_CALLS for alias in node.names):
                return "it imports something I won't run"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in REFUSED_CALLS:
            return f"it uses {node.func.id}()"
        elif isinstance(node, ast.Attribute):
            if node.attr in REFUSED_ATTRS:
                return f"it uses {node.attr}"
            if node.attr.startswith("__") and node.attr != "__name__":
                return "it reaches into Python's insides"
        elif isinstance(node, ast.Name) and node.id in ("subprocess", "socket", "ctypes", "__builtins__"):
            return f"it uses {node.id}"
    return ""


def missing_imports(tree):
    """Allowed modules used as `module.thing` but never imported or assigned."""
    bound = _bound_names(tree)
    used = {
        node.value.id for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and isinstance(node.value.ctx, ast.Load)
    }
    return tuple(sorted(name for name in used - bound if name in ALLOWED_IMPORTS))


def check_python(source, name="script.py"):
    """Parse, trim up to three trailing prose lines, apply the policy and find
    missing imports. Fixes are shell lines Pip types on screen."""
    lines = source.rstrip("\n").split("\n")
    trimmed, tree, error = 0, None, None
    while lines and trimmed <= 3:
        try:
            tree = ast.parse("\n".join(lines) + "\n")
            break
        except SyntaxError as exc:
            error = exc
            if trimmed == 3 or len(lines) == 1:
                break
            lines.pop()
            trimmed += 1
    if tree is None:
        line = getattr(error, "lineno", None)
        return Check(False, f"syntax error on line {line}" if line else "syntax error")
    reason = python_policy(tree)
    if reason:
        return Check(False, reason)
    fixes = []
    if trimmed:
        fixes.append(f"sed -i '{len(lines) + 1},$d' {name}")
    missing = missing_imports(tree)
    for module in reversed(missing):
        fixes.append(f"sed -i '1i import {module}' {name}")
    return Check(True, fixes=tuple(fixes), missing=missing, trimmed=trimmed)


APPLETS = (
    "ls", "cat", "echo", "date", "cal", "df", "du", "free", "uptime", "uname", "head", "tail", "wc",
    "sort", "uniq", "grep", "find", "mkdir", "touch", "cp", "mv", "seq", "printf", "tr", "cut",
    "basename", "dirname", "sleep", "python3",
)
_SHELL_BAD = re.compile(r"[;&`$(){}<\\!#\n\r]|\|\|")


def check_shell_line(line, exists):
    """One BusyBox line Pip wrote. Returns '' if it may be typed, else a reason."""
    line = line.strip()
    if not line or len(line) > 200:
        return "it is empty or too long"
    if _SHELL_BAD.search(line):
        return "it uses shell features I don't type"
    redirect = None
    match = re.search(r"\s(>>?)\s*(\S+)$", line)
    if match:
        redirect = (match[1], match[2])
        line = line[:match.start()].rstrip()
        if not NAME.match(redirect[1]):
            return "it writes to a name I don't use"
        if redirect[0] == ">" and exists(redirect[1]):
            return "it would overwrite a file"
    if ">" in line:
        return "it redirects in the middle"
    segments = [segment.strip() for segment in line.split("|")]
    if len(segments) > 3 or any(not segment for segment in segments):
        return "it is too long a pipeline"
    for segment in segments:
        try:
            words = shlex.split(segment)
        except ValueError:
            return "its quotes don't match"
        if not words or words[0] not in APPLETS:
            return f"{words[0] if words else 'it'} isn't a command I use"
        applet, args = words[0], words[1:]
        if any(".." in arg for arg in args):
            return "it climbs out of the workshop"
        if applet == "find" and any(arg in ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint") for arg in args):
            return "find may only look"
        if applet in ("cp", "mv") and "-n" not in args:
            return f"{applet} must not overwrite"
        if applet in ("mkdir", "touch", "cp", "mv") and any(arg.startswith("/") for arg in args):
            return "it writes outside the workshop"
        if applet == "python3" and (len(args) != 1 or not NAME.match(args[0]) or not exists(args[0])):
            return "python3 runs only a file in the workshop"
    return ""


# -- grammars (GBNF) -------------------------------------------------------

def python_grammar(max_lines=MAX_LINES, done=0, mid_line=False):
    """1..max_lines lines and then the closing fence. Nested rules give exactly
    two choices at each line end, on any llama.cpp GBNF version. For a resume,
    `done` lines are already written, and `mid_line` means the current one is
    unfinished."""
    rules = [r'line ::= [^`\n]* "\n"', 'fence ::= "```"']
    start = max(done, 0) + (1 if mid_line else 0)
    last = max(max_lines, start)
    for index in range(start, last + 1):
        if index == last:
            rules.append(f"r{index} ::= fence")
        elif index == 0:
            rules.append(f"r{index} ::= line r{index + 1}")
        else:
            rules.append(f"r{index} ::= fence | line r{index + 1}")
    root = f'root ::= [^`\\n]* "\\n" r{start}' if mid_line else f"root ::= r{start}"
    return "\n".join([root, *rules]) + "\n"


def shell_grammar():
    applets = " | ".join(f'"{name}"' for name in APPLETS)
    return "\n".join([
        'root ::= cmd pipe? pipe? redir? "\\n"',
        'pipe ::= " | " cmd',
        'cmd ::= applet (" " arg)*',
        f"applet ::= {applets}",
        r"""arg ::= word | "'" [^'\n]* "'" | "\"" [^"\n$`\\]* "\"" """.rstrip(),
        "word ::= [a-zA-Z0-9_./=:,+%*?@-]+",
        'redir ::= (" > " | " >> ") [a-z] [a-z0-9_-]* "." ("txt" | "py" | "sh")',
    ]) + "\n"


# -- the recipe book -------------------------------------------------------

@dataclass(frozen=True)
class Recipe:
    name: str
    kind: str            # script or command
    ext: str             # py, or "" for a command
    slots: tuple         # (slot name, low, high, default) for numbers, (name, "word", None, default) for text
    template: str
    describe: str        # for the model note and reports: "a spiral"

    def build(self, slots):
        values = {}
        for slot, low, high, default in self.slots:
            value = slots.get(slot, default) if slots else default
            if low == "word":
                text = sanitise(str(value), 60).replace("\n", " ").strip()
                if not text:
                    raise ValueError(f"{slot} is empty")
                values[slot] = repr(text)
            else:
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"{slot} must be a whole number from {low} to {high}")
                values[slot] = str(value)
        text = self.template
        for slot, value in values.items():
            text = text.replace("{" + slot + "}", value)
        return text


def _r(name, kind, template, describe, slots=(), ext="py"):
    return Recipe(name, kind, ext if kind == "script" else "", tuple(slots), template.strip("\n") + "\n", describe)


RECIPES = {recipe.name: recipe for recipe in (
    _r("countdown", "script", """
import time

for number in range({n}, 0, -1):
    print(number)
    time.sleep(1)
print("Lift off!")
""", "a countdown", [("n", 1, 60, 10)]),
    _r("countup", "script", """
import time

for number in range(1, {n} + 1):
    print(number)
    time.sleep(0.5)
print("Done counting.")
""", "a count up", [("n", 1, 60, 10)]),
    _r("times_table", "script", """
for row in range(1, 13):
    print(f"{row:2} x {n} = {row * {n}:3}")
""", "a times table", [("n", 1, 60, 7)]),
    _r("note", "script", """
import datetime
import pathlib

note = {text}
notes = pathlib.Path("notes")
notes.mkdir(exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
with open(notes / "notes.txt", "a") as book:
    book.write(f"{stamp}  {note}\\n")
print("Noted:", note)
""", "a note", [("text", "word", None, "")]),
    _r("list_notes", "script", """
import pathlib

book = pathlib.Path("notes") / "notes.txt"
lines = book.read_text().splitlines() if book.exists() else []
for number, line in enumerate(lines, 1):
    print(number, line)
print(f"{len(lines)} notes in the workshop.")
""", "your notes"),
    _r("clock", "script", """
import time

for _ in range({n}):
    print(time.strftime("%H:%M:%S"), end="\\r", flush=True)
    time.sleep(1)
print()
""", "a clock", [("n", 1, 60, 10)]),
    _r("dice", "script", """
import random

rolls = [random.randint(1, 6) for _ in range({n})]
print("You rolled:", *rolls)
print("Total:", sum(rolls))
""", "dice", [("n", 1, 6, 2)]),
    _r("coin", "script", """
import random

print("Heads!" if random.random() < 0.5 else "Tails!")
""", "a coin toss"),
    _r("spiral", "script", """
import time

size = 21
grid = [[" "] * size for _ in range(size)]
x = y = size // 2
dx, dy = 0, -1
length = 1
while 0 <= x < size and 0 <= y < size:
    for _ in range(length):
        if 0 <= x < size and 0 <= y < size:
            grid[y][x] = "#"
        x, y = x + dx, y + dy
    dx, dy = -dy, dx
    length += 1
for row in grid:
    print(" ".join(row))
    time.sleep(0.05)
""", "a spiral"),
    _r("box", "script", """
size = {n}
print("+" + "-" * size + "+")
for _ in range(size // 2):
    print("|" + " " * size + "|")
print("+" + "-" * size + "+")
""", "a box", [("n", 2, 60, 10)]),
    _r("triangle", "script", """
height = {n}
for row in range(1, height + 1):
    print(" " * (height - row) + "*" * (2 * row - 1))
""", "a triangle", [("n", 1, 30, 6)]),
    _r("diamond", "script", """
size = {n}
for row in list(range(1, size + 1)) + list(range(size - 1, 0, -1)):
    print(" " * (size - row) + "*" * (2 * row - 1))
""", "a diamond", [("n", 1, 30, 5)]),
    _r("tree", "script", """
height = 7
for row in range(height):
    print(" " * (height - row) + "*" * (2 * row + 1))
print(" " * height + "|")
""", "a tree"),
    _r("heart", "script", """
for y in range(6, -6, -1):
    line = ""
    for x in range(-12, 13):
        a, b = x * 0.1, y * 0.2
        line += "*" if (a * a + b * b - 1) ** 3 - a * a * b ** 3 <= 0 else " "
    print(line)
""", "a heart"),
    _r("warmth", "script", """
import glob

for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
    with open(zone) as reading:
        print(f"{zone.split('/')[4]}: {int(reading.read()) / 1000:.1f} C")
for battery in glob.glob("/sys/class/power_supply/BAT*/capacity"):
    with open(battery) as reading:
        print("battery:", reading.read().strip() + "%")
""", "how warm I am"),
    _r("files", "command", "ls -l", "the files here"),
    _r("disk", "command", "df -h .", "the disk space"),
    _r("memory", "command", "free -m", "the memory"),
    _r("uptime", "command", "uptime", "how long I've been up"),
)}


# -- the typist ------------------------------------------------------------

class Typist:
    """Paces text into the shell. Pure: feed() arrivals, take(now) what to type.

    Model text is drawn at its own smoothed arrival rate (8-80 characters a
    second) behind a jitter buffer that starts 0.8 s after the first token and
    aims for 1 s of lead. Known text (recipes, headers, the end word) goes at
    about 30 a second. Indentation costs nothing and a line end costs a short
    beat. Once the stream ends, model text flushes at 150 a second. While
    minimised, text goes straight in.
    """

    def __init__(self, *, fixed_rate=None, now=0.0):
        self.fixed_rate = fixed_rate
        self._segments = []          # [text, known]
        self.typed = ""
        self.ended = False
        self.held = False
        self.paused = False
        self.human_until = None
        self.instant = False
        self._first = None
        self._arrived = 0
        self._credit = 0.0
        self._last = now
        self._beat_until = 0.0
        self._retype = None

    @property
    def queued(self):
        return sum(len(text) for text, _ in self._segments)

    @property
    def done(self):
        return self.ended and not self._segments and self._retype is None

    def feed(self, text, now, *, known=None):
        if not text:
            return
        known = (self.fixed_rate is not None) if known is None else known
        if not known:
            if self._first is None:
                self._first = now
            self._arrived += len(text)
        if self._segments and self._segments[-1][1] == known:
            self._segments[-1][0] += text
        else:
            self._segments.append([text, known])
        if self.queued > MAX_QUEUE:
            raise OverflowError("too much queued")

    def end(self):
        self.ended = True

    def hold(self):
        self.held = True

    def release(self):
        self.held = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        self.held = False

    def current_line(self):
        return self.typed.rpartition("\n")[2]

    def human(self, now):
        """The owner typed: stop at once. When Pip carries on it clears the
        line (Ctrl-U) and retypes what it had typed of that line."""
        if self.human_until is None:
            self._retype = self.current_line()
        self.human_until = now + HUMAN_QUIET

    def stopped(self, now):
        if self.human_until is not None:
            if now < self.human_until:
                return True
            self.human_until = None
        return self.held or self.paused

    def _rate(self, now, known):
        if known:
            return self.fixed_rate or RECIPE_RATE
        if self.ended:
            return FLUSH_RATE
        elapsed = max(now - self._first, 0.25)
        arrival = self._arrived / elapsed
        waiting = sum(len(text) for text, k in self._segments if not k)
        lead = waiting / max(arrival, 1.0)
        return min(MAX_RATE, max(MIN_RATE, arrival * (1.0 + 0.5 * (lead - MODEL_LEAD))))

    def take(self, now):
        dt, self._last = max(0.0, now - self._last), now
        if self.stopped(now):
            self._credit = 0.0
            return ""
        out = ""
        if self._retype is not None:
            out, self._retype = "\x15" + self._retype, None
        if not self._segments:
            self._credit = 0.0
            return out
        if self.instant:
            chunk = "".join(text for text, _ in self._segments)
            self._segments.clear()
            self.typed += chunk
            return out + chunk
        if now < self._beat_until:
            return out
        known = self._segments[0][1]
        if not known and not self.ended and now - self._first < MODEL_START_DELAY:
            return out
        self._credit = min(self._credit + dt * self._rate(now, known), 40.0)
        taken = []
        line = self.current_line()
        while self._segments and self._segments[0][1] == known:
            text = self._segments[0][0]
            ch = text[0]
            free = ch == " " and not line.strip()
            if not free and self._credit < 1.0:
                break
            if len(text) == 1:
                self._segments.pop(0)
            else:
                self._segments[0][0] = text[1:]
            taken.append(ch)
            if not free:
                self._credit -= 1.0
            if ch == "\n":
                self._beat_until = now + LINE_BEAT
                break
            line += ch
        chunk = "".join(taken)
        self.typed += chunk
        return out + chunk


# -- the slot-1 code client --------------------------------------------------

class CodeStreamError(Exception):
    pass


class CodeStream:
    """Raw /completion on slot 1 with token IDs. Iterate (text, tokens); cancel()
    cuts the connection, which stops the server within about a token."""

    def __init__(self, client, payload):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._sock = None
        self.cancelled = False
        self.error = None
        self.stop_reason = None
        self.timings = None         # the server's numbers for this request, at its end
        threading.Thread(target=self._run, args=(client, payload), daemon=True).start()

    def _run(self, client, payload):
        conn = None
        try:
            conn = http.client.HTTPConnection(client.host, client.port, timeout=client.timeout)
            conn.connect()
            self._sock = conn.sock
            if self.cancelled:
                return
            conn.request("POST", client.path + "/completion", json.dumps(payload).encode(),
                         {"Content-Type": "application/json", "Accept": "text/event-stream"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise CodeStreamError(f"HTTP {resp.status}")
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:].strip())
                except ValueError:
                    continue
                tokens = [t for t in data.get("tokens") or () if type(t) is int]
                text = data.get("content") or ""
                if text or tokens:
                    self._put((text, tokens))
                if data.get("stop"):
                    self.stop_reason = data.get("stop_type") or "stop"
                    timings = data.get("timings")
                    if isinstance(timings, dict):
                        self.timings = {key: value for key, value in timings.items()
                                        if type(value) in (int, float)}
                    break
        except Exception as exc:  # the thread always hands back an ending
            if not self.cancelled:
                self.error = exc
        finally:
            if conn is not None:
                conn.close()
            self._put(None)

    def _put(self, item):
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            if self.error is not None:
                raise CodeStreamError(str(self.error))
            raise StopAsyncIteration
        return item

    def cancel(self):
        self.cancelled = True
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


CODE_SYSTEM = (
    "You write tiny programs for a terminal. Reply with code only, in one fenced block. "
    "For a script: Python 3 standard library only, at most 25 lines, no input(), no internet, "
    "files only in the current folder. For a command: one BusyBox shell line."
)
CODE_EXAMPLES = (
    ("script: print the numbers from 1 to 5", "```python\nfor number in range(1, 6):\n    print(number)\n```"),
    ("script: wait two seconds then say hello", "```python\nimport time\n\ntime.sleep(2)\nprint(\"hello\")\n```"),
    ("script: draw a line of 20 stars", "```python\nprint(\"*\" * 20)\n```"),
    ("script: print today's date", "```python\nimport datetime\n\nprint(datetime.date.today())\n```"),
    ("command: count the lines in notes.txt", "```sh\nwc -l notes.txt\n```"),
    ("command: show the biggest files here", "```sh\nls -lS | head -5\n```"),
)


def code_messages(kind, request, *, previous=None, error=None):
    messages = [{"role": "system", "content": CODE_SYSTEM}]
    for asked, answer in CODE_EXAMPLES:
        messages += [{"role": "user", "content": asked}, {"role": "assistant", "content": answer}]
    messages.append({"role": "user", "content": f"{'script' if kind == 'script' else 'command'}: {request}"})
    if previous is not None:
        messages.append({"role": "assistant", "content": f"```python\n{previous.rstrip()}\n```"})
        messages.append({"role": "user", "content": f"fix: it failed with: {error}"[:240]})
    return messages


def request_seed(text, bump=0):
    return (int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) + bump) % (2 ** 31)


class CodeCortex:
    """llama-server slot 1, for code only. Never saved; erased on close, forget
    and any guest change. Slot 0 stays the conversation's."""

    def __init__(self, base_url="http://127.0.0.1:8080", slot=1, timeout=120.0):
        url = urlparse(base_url)
        if url.scheme != "http" or not url.hostname:
            raise ValueError("cortex URL must be http://host:port")
        self.host, self.port, self.path = url.hostname, url.port or 80, url.path.rstrip("/")
        self.slot = slot
        self.timeout = timeout

    def _post(self, path, body, timeout=10.0):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request("POST", self.path + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise CodeStreamError(f"HTTP {resp.status} from {path}")
            return json.loads(raw) if raw else {}
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise CodeStreamError(f"{path}: {exc}") from exc
        finally:
            conn.close()

    async def prompt_tokens(self, messages, opening):
        rendered = await asyncio.to_thread(
            self._post, "/apply-template", {"messages": messages, "chat_template_kwargs": {"enable_thinking": False}}
        )
        prompt = rendered.get("prompt") if isinstance(rendered, dict) else None
        if not isinstance(prompt, str) or not prompt:
            raise CodeStreamError("no prompt")
        tokens = await asyncio.to_thread(
            self._post, "/tokenize", {"content": prompt + opening, "add_special": True, "parse_special": True}
        )
        ids = tokens.get("tokens") if isinstance(tokens, dict) else None
        if not isinstance(ids, list) or not ids or any(type(t) is not int for t in ids):
            raise CodeStreamError("no tokens")
        return ids

    def stream(self, prompt_ids, *, grammar, seed, temperature=0.2, n_predict=400):
        payload = {
            "prompt": list(prompt_ids), "id_slot": self.slot, "cache_prompt": True, "stream": True,
            "return_tokens": True, "n_predict": n_predict, "grammar": grammar, "seed": seed,
            "temperature": temperature, "top_k": 20, "top_p": 0.9, "repeat_penalty": 1.0,
        }
        return CodeStream(self, payload)

    async def erase(self):
        try:
            await asyncio.to_thread(self._post, f"/slots/{self.slot}?action=erase", {}, 5.0)
            return True
        except CodeStreamError:
            return False


# -- the shell side: OSC filter and transcript ---------------------------------

SPLICE = b"\x18"  # CAN: put where OscFilter strips a sequence


class OscFilter:
    """Strip OSC 7770 and 7771 from everything the shell side writes, so no
    program can forge Pip's flags or the prompt marker. Returns the clean
    bytes and the text of any 7771 markers, in order. Other OSCs pass through
    (the renderer ignores them). A sequence split across reads is held back."""

    PREFIXES = (b"\x1b]7770", b"\x1b]7771")

    def __init__(self):
        self._held = b""

    def feed(self, data):
        data = self._held + data
        self._held = b""
        out, markers, index = bytearray(), [], 0
        while True:
            start = data.find(b"\x1b]777", index)
            if start < 0:
                tail = data[index:]
                keep = 0
                for size in range(1, 6):
                    if len(tail) >= size and any(p.startswith(tail[-size:]) for p in self.PREFIXES):
                        keep = size
                out += tail[:len(tail) - keep]
                self._held = tail[len(tail) - keep:]
                break
            out += data[index:start]
            if len(data) < start + 6:
                self._held = data[start:]
                break
            if data[start:start + 6] not in self.PREFIXES:
                out += data[start:start + 1]
                index = start + 1
                continue
            ends = [(pos, 1) for pos in (data.find(b"\x07", start),) if pos >= 0]
            ends += [(pos, 2) for pos in (data.find(b"\x1b\\", start + 1),) if pos >= 0]
            if not ends:
                if len(data) - start <= 512:
                    self._held = data[start:]
                break  # an unterminated flood is dropped
            stop, width = min(ends)
            if data[start + 5:start + 6] == b"1":
                body = data[start + 6:stop]
                markers.append(body[1:].decode("ascii", "replace") if body.startswith(b";") else "")
            # CAN where the sequence was: a lone ESC ] the program wrote just
            # before it can never join what follows into a forged 7770 (the
            # panel ignores CAN; inside an OSC the body no longer starts 7770;).
            out += SPLICE
            index = stop + width
        return bytes(out), markers


class Ring:
    """hands' 64 KB transcript of the session, RAM only, wiped for real."""

    def __init__(self, size=TRANSCRIPT):
        self.size = size
        self.data = bytearray()

    def add(self, chunk):
        self.data += chunk
        if len(self.data) > self.size:
            del self.data[: len(self.data) - self.size]

    def wipe(self):
        for index in range(len(self.data)):
            self.data[index] = 0
        self.data = bytearray()

    def lines(self):
        text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.|\x18", "",
                      self.data.decode("utf-8", "replace"))
        return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


# -- the cage ----------------------------------------------------------------

# One transient unit per session. The owner's words and the model's text reach
# the shell only as pty bytes, never as arguments here. Properties marked (*)
# are inert until the next image adds libseccomp or the memory and pids
# controllers (voice-apps.md section 5); they are passed so they take effect
# the day the kernel and systemd support them. ProcSubset=pid is left out on
# purpose: the uptime and free recipes read /proc/uptime and /proc/meminfo.
CAGE_PROPERTIES = (
    "WorkingDirectory=/workshop",
    "StandardInput=tty", "StandardOutput=tty", "StandardError=tty",
    "TTYReset=yes", "TTYVHangup=yes",
    "NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
    "ProtectSystem=strict", "ProtectHome=yes", "ReadWritePaths=/workshop",
    "TemporaryFileSystem=/run:size=1M,mode=0755", "TemporaryFileSystem=/var:size=1M,mode=0755",
    "TemporaryFileSystem=/tmp:size=16M,mode=1777", "TemporaryFileSystem=/dev/shm:size=8M,mode=1777",
    "InaccessiblePaths=-/mind -/models -/bootstate -/etc/being",
    "PrivateDevices=yes", "PrivateIPC=yes", "RemoveIPC=yes", "KeyringMode=private", "UMask=0077",
    "PrivateNetwork=yes",
    "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes", "ProtectClock=yes", "ProtectHostname=yes", "ProtectProc=invisible",
    "SystemCallFilter=@system-service", "RestrictAddressFamilies=AF_UNIX", "RestrictNamespaces=yes",  # (*)
    "MemoryDenyWriteExecute=yes", "LockPersonality=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes",  # (*)
    "CPUSchedulingPolicy=idle", "IOSchedulingClass=idle", "CPUQuota=50%",
    "MemoryHigh=192M", "MemoryMax=256M", "MemorySwapMax=0", "TasksMax=48",  # (*)
    "LimitNPROC=48", "OOMScoreAdjust=1000", "LimitCPU=900", "LimitFSIZE=16M", "LimitNOFILE=128",
    "LimitCORE=0", "RuntimeMaxSec=4h",
)
# HISTFILE=/dev/null: the typed scripts never land in /workshop/.ash_history,
# where forgetting one script would miss them. The in-memory history goes
# with the shell, which hands restarts when a script is forgotten.
SHELL_ENV = ("HOME=/workshop", "TERM=dumb", f"ENV={SHELLRC}", "HISTFILE=/dev/null")


def cage_command(unit, tty):
    """The fixed systemd-run line. Nothing in it comes from speech or the model."""
    if not re.fullmatch(r"being-shell-[0-9]{1,6}", unit) or not re.fullmatch(r"/dev/pts/[0-9]{1,6}", tty):
        raise ValueError("bad unit or tty")
    argv = ["systemd-run", f"--unit={unit}", "--slice=workshop.slice", "--service-type=exec",
            "--collect", "--quiet", "--uid=being-shell", "--gid=being-shell", f"--property=TTYPath={tty}"]
    argv += [f"--property={item}" for item in CAGE_PROPERTIES]
    argv += [f"--setenv={item}" for item in SHELL_ENV]
    return argv + ["/bin/sh", "-i"]


def renderer_command(app, size=PANEL_SIZE):
    """being-console in panel mode, as being-panel, in its own hardened unit,
    reading the app's bytes on stdin (systemd-run --pipe)."""
    if app not in APPS:
        raise ValueError("bad app")
    props = (
        "NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes",
        f"ReadWritePaths={PANEL_DIR}", "PrivateNetwork=yes", "PrivateDevices=yes", "PrivateTmp=yes",
        "InaccessiblePaths=-/mind -/models -/workshop", "ProtectKernelTunables=yes", "ProtectControlGroups=yes",
        "Nice=5", "CPUQuota=30%", "MemoryMax=32M", "RuntimeMaxSec=4h",
    )
    argv = ["systemd-run", f"--unit=being-panel-{app}", "--pipe", "--collect", "--quiet",
            "--uid=being-panel", "--gid=being-panel"]
    argv += [f"--property={item}" for item in props]
    return argv + [CONSOLE, "--panel", app, "--size", f"{size[0]}x{size[1]}", "--io", "-"]


def _run(argv, timeout=10.0):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    return result.returncode, result.stdout.decode("utf-8", "replace").strip()


class SystemdCage:
    """The real cage, on the laptop: systemd-run and systemctl."""

    real = True

    def __init__(self, run=_run, cgroup_root="/sys/fs/cgroup/workshop.slice"):
        self.run = run
        self.cgroup_root = cgroup_root

    def stop_leftovers(self):
        self.run(["systemctl", "stop", "being-shell-*", "being-panel-*"])

    def screen_states(self):
        """{unit: ActiveState} of the face and the local screen (the Try
        menu), which conflict: only one of them has the display. {} if
        systemctl did not answer for both (a timeout raises, and the caller
        then lets the open go ahead)."""
        code, out = self.run(["systemctl", "is-active", *SCREEN_UNITS], timeout=SCREEN_PROBE_SECONDS)
        states = out.split()
        return dict(zip(SCREEN_UNITS, states)) if len(states) == len(SCREEN_UNITS) else {}

    def start(self, unit, tty, slave_fd):
        code, out = self.run(cage_command(unit, tty))
        if code != 0:
            raise OSError(errno.EIO, "the cage did not start")
        code, out = self.run(["systemctl", "show", "-p", "MainPID", "--value", unit])
        pid = int(out) if code == 0 and out.isdigit() else 0
        if pid <= 0:
            raise OSError(errno.ESRCH, "no shell")
        return pid

    def freeze(self, unit):
        return self.run(["systemctl", "freeze", unit])[0] == 0

    def thaw(self, unit):
        return self.run(["systemctl", "thaw", unit])[0] == 0

    def stop(self, unit):
        self.run(["systemctl", "stop", unit])

    def kill_all_but(self, unit, shell_pid):
        """Kill every process in the session's cgroup except the shell."""
        killed = 0
        try:
            with open(f"{self.cgroup_root}/{unit}.service/cgroup.procs") as procs:
                pids = [int(line) for line in procs if line.strip().isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            if pid != shell_pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
        return killed


class LocalCage:
    """NOT a cage: a plain local /bin/sh on a real pty, for the Mac and tests.
    It records what the real cage would be asked to do. Never used on the
    laptop (only `--local-shell` selects it)."""

    real = False

    def __init__(self, workshop, shellrc, path=None, extra_env=None):
        self.workshop = str(Path(workshop).resolve())
        self.shellrc = str(Path(shellrc).resolve())
        self.path = path
        self.extra_env = dict(extra_env or {})
        self.calls = []
        self.pids = {}

    def stop_leftovers(self):
        self.calls.append(("stop_leftovers",))

    def start(self, unit, tty, slave_fd):
        self.calls.append(("start", unit))
        env = {
            "HOME": self.workshop, "TERM": "dumb", "ENV": self.shellrc, "LANG": "C.UTF-8", "HISTFILE": "/dev/null",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "BASH_SILENCE_DEPRECATION_WARNING": "1",
            **({"PIP_PATH": self.path} if self.path else {}), **self.extra_env,
        }
        with warnings.catch_warnings():
            # The child only calls login_tty, chdir and execve before exec.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - the child
            try:
                os.login_tty(slave_fd)
                os.chdir(self.workshop)
                os.execve("/bin/sh", ["/bin/sh", "-i"], env)
            finally:
                os._exit(127)
        self.pids[unit] = pid
        return pid

    def freeze(self, unit):
        # The real cage uses the cgroup v2 freezer, which the shell never
        # notices. SIGSTOP would be seen by the shell's job control, so the
        # stand-in only records the request.
        self.calls.append(("freeze", unit))
        return True

    def thaw(self, unit):
        self.calls.append(("thaw", unit))
        return True

    def stop(self, unit):
        self.calls.append(("stop", unit))
        pid = self.pids.pop(unit, None)
        if pid:
            for target in self._children(pid) + [pid]:
                try:
                    os.kill(target, signal.SIGKILL)
                except OSError:
                    pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def kill_all_but(self, unit, shell_pid):
        self.calls.append(("kill_all_but", unit))
        killed = 0
        for target in self._children(shell_pid):
            try:
                os.kill(target, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
        return killed

    @staticmethod
    def _children(pid):
        try:
            out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, timeout=3).stdout.decode()
        except (OSError, subprocess.SubprocessError):
            return []
        found = [int(word) for word in out.split() if word.isdigit()]
        for child in list(found):
            found += LocalCage._children(child)
        return found


class Session:
    """One caged shell on a pty that hands holds. Pip types by writing to the
    pty master; uinput is not in the laptop kernel and is not needed."""

    def __init__(self, cage, number):
        self.cage = cage
        self.unit = f"being-shell-{number}"
        self.master = None
        self.shell_pid = None
        self.frozen = False

    def start(self):
        master, slave = os.openpty()
        try:
            attrs = termios.tcgetattr(slave)
            attrs[6][termios.VERASE] = 0x7F
            if hasattr(termios, "IUTF8"):
                attrs[0] |= termios.IUTF8
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", GRID_ROWS, GRID_COLUMNS, 0, 0))
            tty = os.ttyname(slave)
            if self.cage.real and not re.fullmatch(r"/dev/pts/[0-9]+", tty):
                raise OSError(errno.ENOTTY, "unexpected tty")
            self.shell_pid = self.cage.start(self.unit, tty, slave)
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        os.set_blocking(master, False)
        self.master = master
        return self

    def foreground_is_shell(self):
        if self.master is None or not self.shell_pid:
            return False
        try:
            return os.tcgetpgrp(self.master) == self.shell_pid
        except OSError:
            return False

    def write(self, data):
        if self.master is None or not data:
            return 0
        try:
            return os.write(self.master, data)
        except BlockingIOError:
            return 0
        except OSError:
            return -1

    def read(self, limit):
        if self.master is None:
            return b""
        try:
            return os.read(self.master, limit)
        except BlockingIOError:
            return None
        except OSError:
            return b""

    def freeze(self):
        if not self.frozen:
            self.frozen = self.cage.freeze(self.unit)
        return self.frozen

    def thaw(self):
        if self.frozen:
            self.cage.thaw(self.unit)
            self.frozen = False

    def kill_children(self):
        return self.cage.kill_all_but(self.unit, self.shell_pid)

    def close(self):
        if self.master is not None:
            self.thaw()
            # Master first: the shell gets its hangup, and an exiting process is
            # never left waiting to drain output nobody will read.
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
            self.cage.stop(self.unit)


# -- the renderer ------------------------------------------------------------

def osc_flags(**flags):
    """In-band flags for being-console --panel (OSC 7770). Values never carry
    ';', control bytes or more than 48 characters."""
    parts = []
    for key, value in flags.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = int(value)
        text = sanitise(str(value), 48).replace("\n", " ").replace(";", ",")
        parts.append(f"{key}={text}")
    return ("\x1b]7770;" + ";".join(parts) + "\x07").encode() if parts else b""


class PanelRenderer:
    """being-console --panel, writing /run/being-panel/<app>.frame for the face."""

    def __init__(self, app, command=renderer_command, run=_run):
        self.app = app
        self.command = command
        self.run = run
        self.proc = None
        self.visible = True

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.command(self.app), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def alive(self):
        """False once systemd-run has exited: the renderer's unit did not
        start, or it died. Pip then never says the terminal is shown."""
        return self.proc is not None and self.proc.returncode is None

    def backlog(self):
        transport = getattr(self.proc.stdin, "transport", None) if self.proc else None
        return transport.get_write_buffer_size() if transport else 0

    def write(self, data):
        if self.proc is not None and self.proc.stdin is not None and data:
            try:
                self.proc.stdin.write(data)
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass

    def flags(self, **flags):
        self.write(osc_flags(**flags))

    def wipe(self):
        self.flags(wipe=1)

    async def show(self):
        return await self._signal("SIGUSR1", True)

    async def hide(self):
        return await self._signal("SIGUSR2", False)

    async def ready(self, timeout=1.0, step=0.02):
        """True once the renderer's frame file exists (it appears by rename,
        so with a whole first frame), waiting up to `timeout`. hands hides
        the last panel only after the next one is there, so the face goes
        straight from one to the other."""
        path = PANEL_DIR / f"{self.app}.frame"
        deadline = time.monotonic() + timeout
        while self.alive():
            if path.exists():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(step)
        return False

    async def _signal(self, name, visible):
        """Show or hide the panel: being-console takes SIGUSR1 and SIGUSR2
        (being-console-panel.h). The renderer is the main process of its
        own unit, so systemd delivers the signal and hands never signals a
        pid it read from a file the renderer writes."""
        if not self.alive():
            return False
        code, _ = await asyncio.to_thread(
            self.run, ["systemctl", "kill", "--kill-whom=main", f"--signal={name}", f"being-panel-{self.app}.service"])
        if code == 0:
            self.visible = visible
        else:
            log(f"panel {self.app} did not take {name}")
        return code == 0

    async def stop(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3.0)
            except (asyncio.TimeoutError, OSError, RuntimeError):
                self.proc.kill()
            self.proc = None


class RecordingRenderer:
    """A renderer for the Mac and tests: keeps the bytes it would draw."""

    def __init__(self, app):
        self.app = app
        self.data = bytearray()
        self.flag_log = []
        self.started = False
        self.wiped = 0
        self.visible = True
        self.shown_log = []

    async def start(self):
        self.started = True

    def alive(self):
        return self.started

    def backlog(self):
        return 0

    def write(self, data):
        self.data += data

    def flags(self, **flags):
        self.flag_log.append(flags)

    def wipe(self):
        self.wiped += 1
        self.data = bytearray()

    async def show(self):
        self.visible = True
        self.shown_log.append(True)
        return self.started

    async def ready(self, timeout=1.0, step=0.02):
        return self.started

    async def hide(self):
        self.visible = False
        self.shown_log.append(False)
        return self.started

    async def stop(self):
        self.started = False


# -- forgetting ----------------------------------------------------------------

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))


def _zero_and_unlink(dir_fd, name):
    """Overwrite with zeros, then unlink through the directory handle. Root
    never follows a link the cage planted."""
    fd = os.open(name, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            remaining = info.st_size
            block = b"\0" * 65536
            while remaining > 0:
                remaining -= os.write(fd, block[:min(remaining, len(block))])
            os.fsync(fd)
    finally:
        os.close(fd)
    os.unlink(name, dir_fd=dir_fd)


def read_workshop_file(workshop, name, limit=MAX_FILE):
    if not NAME.match(name):
        return None
    dir_fd = _open_dir(workshop)
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
    except OSError:
        os.close(dir_fd)
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit + 1)[: limit + 1]
    finally:
        os.close(fd)
        os.close(dir_fd)


def workshop_exists(workshop, name):
    try:
        os.lstat(os.path.join(workshop, name))
        return True
    except OSError:
        return False


def forget_file(workshop, name, *, also=()):
    """Remove a workshop file, its same-hash copies and its hard links, by
    walking the workshop without following links. Returns (removed, changed):
    removed names, and names that look like copies but differ (Pip asks
    before removing those; pass them in `also` once the owner says yes)."""
    removed, changed = [], []
    root_fd = _open_dir(workshop)
    try:
        target, digest = None, None
        try:
            # O_NONBLOCK: a FIFO the cage planted under this name must never
            # block root; only a regular file is read.
            fd = os.open(name, _READ_FLAGS, dir_fd=root_fd)
            try:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode):
                    target = (info.st_dev, info.st_ino)
                    digest = hashlib.sha256(os.read(fd, MAX_FILE * 4)).hexdigest()
            finally:
                os.close(fd)
        except OSError:
            pass
        stem = name.rsplit(".", 1)[0]
        copy_name = re.compile(rf"{re.escape(stem)}(?:-[0-9]+)?(?: \(copy\)|_copy)?\.[a-z]+\Z")
        for top, dirs, files, top_fd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False):
            for entry in files:
                relative = os.path.normpath(os.path.join(top, entry))
                try:
                    entry_info = os.stat(entry, dir_fd=top_fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(entry_info.st_mode):
                    if relative in also:
                        os.unlink(entry, dir_fd=top_fd)
                        removed.append(relative)
                    continue
                same = target is not None and (entry_info.st_dev, entry_info.st_ino) == target
                if not same and digest is not None and entry_info.st_size <= MAX_FILE * 4:
                    try:
                        fd = os.open(entry, _READ_FLAGS, dir_fd=top_fd)
                        try:
                            same = (stat.S_ISREG(os.fstat(fd).st_mode)
                                    and hashlib.sha256(os.read(fd, MAX_FILE * 4)).hexdigest() == digest)
                        finally:
                            os.close(fd)
                    except OSError:
                        same = False
                if same or relative == name or relative in also:
                    _zero_and_unlink(top_fd, entry)
                    removed.append(relative)
                elif copy_name.match(entry):
                    changed.append(relative)
    finally:
        os.close(root_fd)
    return sorted(removed), sorted(changed)


def remove_files(workshop, names):
    """Zero and remove the named workshop files (relative paths the owner said
    yes to), through directory handles and without following links. Returns
    the names removed."""
    wanted = {os.path.normpath(name) for name in names
              if isinstance(name, str) and name and not os.path.isabs(name) and ".." not in name.split("/")}
    removed = []
    if not wanted:
        return removed
    root_fd = _open_dir(workshop)
    try:
        for top, dirs, files, top_fd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False):
            for entry in files:
                relative = os.path.normpath(os.path.join(top, entry))
                if relative not in wanted:
                    continue
                try:
                    info = os.stat(entry, dir_fd=top_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        _zero_and_unlink(top_fd, entry)
                    else:
                        os.unlink(entry, dir_fd=top_fd)
                    removed.append(relative)
                except OSError:
                    pass
    finally:
        os.close(root_fd)
    return sorted(removed)


def clear_workshop(workshop):
    """Zero and remove everything in the workshop, through directory handles."""
    count = 0
    root_fd = _open_dir(workshop)
    try:
        for top, dirs, files, top_fd in os.fwalk(".", dir_fd=root_fd, topdown=False, follow_symlinks=False):
            for entry in files:
                try:
                    info = os.stat(entry, dir_fd=top_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        _zero_and_unlink(top_fd, entry)
                    else:
                        os.unlink(entry, dir_fd=top_fd)
                    count += 1
                except OSError:
                    pass
            for entry in dirs:
                try:
                    if stat.S_ISLNK(os.stat(entry, dir_fd=top_fd, follow_symlinks=False).st_mode):
                        os.unlink(entry, dir_fd=top_fd)
                        count += 1
                    else:
                        os.rmdir(entry, dir_fd=top_fd)
                except OSError:
                    pass
    finally:
        os.close(root_fd)
    return count


# -- jobs ------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    kind: str                     # recipe, script, command, install, uninstall, run_app
    text: str                     # what the owner asked, shown as "# you asked:"
    source: str                   # recipe, model, command, shelf
    recipe: str = ""
    slots: dict = field(default_factory=dict)
    force_model: bool = False
    phase: str = "reading"
    file: str = ""
    body: str = ""                # the file body, as written so far
    line: str = ""                # a one-line command
    word: str = ""                # heredoc end word
    prompt_ids: list = field(default_factory=list)
    tokens: list = field(default_factory=list)
    generation_done: bool = False
    parked: bool = False
    started: float = 0.0
    deadline: float = 0.0
    exit: object = None
    reason: str = ""
    fixed: bool = False
    sha256: str = ""
    output_lines: int = 0
    output_bytes: int = 0
    run_line: str = ""
    run_started: float = 0.0
    killed_at: float = 0.0
    last_token: float = 0.0
    last_status: float = 0.0
    countdown_left: int = 0
    undo_until: float = 0.0
    seed: int = 0
    temperature: float = 0.2
    previous: str = ""            # the code a fix starts from
    error: str = ""
    ext: str = "py"
    request: str = ""             # the thing asked for, for the model and the file name
    stream: object = None
    seconds: float = 0.0
    finished: bool = False        # the end of the text has been queued
    check_deadline: float = 0.0
    fix_round: int = 0
    parent_file: str = ""         # for a "fix it" job: the file it fixes (forgotten with it)
    frozen_at: float = 0.0
    app: str = ""                 # for a shelf job: the program's name

    @property
    def lines(self):
        return self.body.count("\n") + (1 if self.body and not self.body.endswith("\n") else 0)

    def describe(self):
        recipe = RECIPES.get(self.recipe) if self.source == "recipe" else None
        return recipe.describe if recipe else (self.request or self.text)[:60]


class Hands:
    """The organ. Everything is driven from the bus, the key socket, the pty and
    a 40 ms tick; nothing here blocks the event loop."""

    def __init__(self, bus, *, cage, cortex, workshop=WORKSHOP, renderer=PanelRenderer,
                 clock=time.monotonic, disabled=APPS_DISABLED, key_socket=None, rng=None,
                 model_label=MODEL_LABEL, body_root=Path("/"), state_dir=Path("/mind"), wall=time.time):
        self.bus = bus
        self.cage = cage
        self.cortex = cortex
        self.workshop = Path(workshop)
        self.make_renderer = renderer
        self.clock = clock
        self.disabled = Path(disabled) if disabled else None
        self.key_socket_path = key_socket
        self.rng = rng or random.SystemRandom()
        self.model_label = model_label
        self.view = {app: "closed" for app in APPS}
        self.session = None
        self.renderer = None
        self.sessions = 0
        self.job = None
        self.jobs_made = 0
        self.osc = OscFilter()
        self.transcript = Ring()
        self.state = None
        self.lid_closed = None
        self.guest = False
        self.mind_up = False
        self._gen_task = None
        self._typist = None
        self._relay_budget = RELAY_RATE
        self._relay_at = clock()
        self._reading = False
        self._minimised_at = None
        self._lid_paused = False
        self._hot = False
        self.on_ac = None
        self.battery = None
        self._hold_until = None
        self._out_since_run = b""
        self._kill_at = None
        self._typing = b""          # typed bytes the pty has not taken yet
        self._leftovers_stopped = False
        self._key_sock = None
        self._tick_task = None
        self._loop = None
        # The body monitor and the clock (mind/app_monitor.py, app_clock.py).
        self.body_root = Path(body_root)
        self.state_dir = Path(state_dir)
        self.wall = wall
        self.apps = {}              # small app -> its renderer
        self._app_tasks = {}
        self._app_sources = {}      # small app -> (sampler or reader, previous reading)
        self.body = {}              # the latest body.state, for the monitor
        self.code_tok_s = None      # tokens a second of the last finished code generation
        self.frame_ms = {}          # small app -> the last frame's draw time, for the log and tests
        if hasattr(bus, "urgent"):
            bus.urgent = self.urgent

    # -- bus out ------------------------------------------------------------------

    async def send(self, topic, **fields):
        await self.bus.send(topic, **fields)

    async def reply(self, topic, **fields):
        req = _REQ.get()
        if req is not None and "req" not in fields:
            fields["req"] = req
        await self.bus.send(topic, **fields)

    async def announce_state(self, app="terminal", req=None):
        job = self.job.phase if self.job and self.job.phase in ACTIVE + ("waiting",) else None
        terminal = app == "terminal"
        await self.send("app.state", app=app, view=self.view[app],
                        session=self.session.unit if self.session and terminal else None,
                        job=job if terminal else None, req=req)

    async def status(self, speakable=False, **extra):
        job = self.job
        if job is None:
            return
        job.last_status = self.clock()
        fields = dict(id=job.id, phase=job.phase, source=job.source, file=job.file or None, lines=job.lines,
                      seconds=round(self.clock() - job.started, 1), exit=job.exit, reason=job.reason or None,
                      speakable=speakable, describe=job.describe(), kind=job.kind)
        if job.sha256:
            fields["sha256"] = job.sha256
        if job.fixed:
            fields["fixed"] = True
        fields.update(extra)
        await self.reply("term.status", **fields)
        self._panel_flags()

    # -- the urgent path (seen on arrival, ahead of queued work) ----------------------

    def urgent(self, event):
        topic = event.get("t")
        if topic == "hear.wake" and self.job is not None and self.job.phase in ("reading", "writing") and self._typist:
            # Typing stops the moment the name is heard; generation carries on.
            self._typist.hold()
            if self.job.phase == "writing":
                self.job.phase = "held"
            self._hold_until = self.clock() + HOLD_SECONDS
            self._panel_flags()

    # -- events ---------------------------------------------------------------------

    async def on_event(self, event):
        topic = event.get("t")
        req = event.get("req")
        _REQ.set(str(req)[:32] if isinstance(req, (str, int)) else None)
        try:
            if topic == "app.open":
                await self.open(event.get("app"), event.get("req"))
            elif topic == "app.close":
                await self.close(event.get("app"), bool(event.get("force")), event.get("req"))
            elif topic == "app.minimise":
                await self.minimise(event.get("app"), event.get("req"))
            elif topic == "app.restore":
                await self.restore(event.get("app"), event.get("req"))
            elif topic == "app.privacy":
                await self.privacy(bool(event.get("guest")))
            elif topic == "term.task":
                await self.task(event)
            elif topic == "term.control":
                await self.control(event.get("action"), event.get("reason") or "owner", event.get("id"))
            elif topic == "term.read":
                await self.read_back(event.get("id"))
            elif topic == "term.forget":
                await self.forget(event)
            elif topic == "state.changed":
                await self.on_state(event.get("state"))
            elif topic == "body.lid":
                await self.on_lid(event.get("closed"))
            elif topic == "body.state":
                await self.on_body(event)
            elif topic == "hear.speech_start":
                if getattr(self, "_hold_until", None):
                    self._hold_until = self.clock() + HOLD_SECONDS
            elif topic == "key.interrupt":
                await self.on_escape()
            elif topic == "organ.up" and event.get("name") == "mind":
                self.mind_up = True
                for app in APPS:
                    if self.view[app] != "closed" or app == "terminal":
                        await self.announce_state(app)
            elif topic == "organ.down" and event.get("name") == "mind":
                self.mind_up = False
        except Exception as exc:  # one bad request must not stop the organ
            log(f"{topic} failed: {type(exc).__name__}")

    # -- apps -----------------------------------------------------------------------

    def _refusal(self):
        if self.guest:
            return "guest"
        if self.disabled is not None and self.disabled.exists():
            return "disabled"
        if self.lid_closed is True:
            return "lid_closed"
        return None

    async def _menu_has_the_screen(self):
        """True while being-face.service is not running because the Try menu
        (being-local-screen) holds the display: a panel opened now would be
        drawn where nobody can see it. Only the real cage can ask systemd;
        if it cannot tell, the open goes ahead as before. The menu is never
        closed from here: it may be in the middle of an install."""
        probe = getattr(self.cage, "screen_states", None)
        if probe is None:
            return False
        try:
            states = await asyncio.to_thread(probe)
        except Exception as exc:
            log(f"could not ask which unit has the screen: {type(exc).__name__}")
            return False
        face, menu = states.get(FACE_UNIT), states.get(LOCAL_SCREEN_UNIT)
        return face is not None and face not in FACE_UP and menu in FACE_UP

    async def open(self, app, req=None):
        """Show an app. True when it went ahead, False when it was refused
        (the app.refused has been sent), so a task that brings the terminal
        forward can stop instead of working where nobody can see."""
        if app not in APPS:
            await self.send("app.refused", req=req, reason="unknown", app=app)
            return False
        reason = self._refusal()
        if reason:
            await self.send("app.refused", req=req, reason=reason, app=app)
            return False
        if await self._menu_has_the_screen():
            log(f"{app} not opened: the face is not running while the Try menu has the screen")
            await self.send("app.refused", req=req, reason="no_face", app=app)
            return False
        if app in SMALL_APPS:
            await self._open_small(app, req)
            return self.view[app] != "closed"
        was = self.view[app]
        if was == "closed":
            try:
                await self._start_terminal()
            except Exception as exc:
                log(f"terminal did not start: {type(exc).__name__}")
                await self._stop_terminal()
                await self.send("app.refused", req=req, reason="failed", app=app)
                return False
        elif was == "minimised" and self.renderer is not None:
            await self.renderer.show()
        self.view[app] = "shown"
        self._minimised_at = None
        if self._typist:
            self._typist.instant = False
        self._panel_flags()
        # At most one app is shown: the terminal is up, so a small app goes
        # (after, so the face goes straight from one panel to the other).
        for small in SMALL_APPS:
            if self.view[small] != "closed":
                await self._close_small(small)
        await self.announce_state(app, req)
        return True

    def _small_shown(self):
        return next((app for app in SMALL_APPS if self.view[app] != "closed"), None)

    async def _open_small(self, app, req=None):
        """The monitor or the clock: its own renderer, fed a frame a second.
        The terminal (if open) is minimised and the other small app closes,
        once this one's first frame is on its way."""
        if self.view[app] == "closed":
            renderer = self.make_renderer(app)
            try:
                await renderer.start()
                self._app_sources[app] = (self._small_source(app), None)
                frame = await asyncio.to_thread(self._small_frame, app)
            except Exception as exc:
                log(f"{app} did not start: {type(exc).__name__}")
                self._app_sources.pop(app, None)
                await renderer.stop()
                await self.send("app.refused", req=req, reason="failed", app=app)
                return
            renderer.flags(title=SMALL_TITLES[app], status="Live · once a second", focus=0, pip="idle", job="idle")
            renderer.write(frame)
            # Its first frame on screen before the last panel is hidden: the
            # face then goes straight from one to the other.
            ready = getattr(renderer, "ready", None)
            if ready is not None:
                await ready()
            if not renderer.alive():
                self._app_sources.pop(app, None)
                await renderer.stop()
                await self.send("app.refused", req=req, reason="failed", app=app)
                return
            self.apps[app] = renderer
            self.view[app] = "shown"
            self._app_tasks[app] = asyncio.get_running_loop().create_task(self._run_small(app, renderer))
            log(f"{app} opened, first frame {self.frame_ms.get(app, 0):.2f} ms")
        for other in SMALL_APPS:
            if other != app and self.view[other] != "closed":
                await self._close_small(other)
        if self.view["terminal"] == "shown":
            if self.renderer is not None:
                await self.renderer.hide()
            self.view["terminal"] = "minimised"
            self._minimised_at = self.clock()
            if self._typist:
                self._typist.instant = True
            self._panel_flags()
            await self.announce_state("terminal")
        await self.announce_state(app, req)

    async def _close_small(self, app, req=None, announce=True):
        task = self._app_tasks.pop(app, None)
        if task is not None:
            task.cancel()
        renderer = self.apps.pop(app, None)
        self._app_sources.pop(app, None)
        if renderer is not None:
            # Hidden first, so the face lets go of it at once; then its
            # picture goes for real (the clock shows reminders' words).
            await renderer.hide()
            renderer.wipe()
            await renderer.stop()
        self.view[app] = "closed"
        if announce:
            await self.announce_state(app, req)

    def _small_source(self, app):
        if app == "monitor":
            from .app_monitor import Sampler
            return Sampler(root=self.body_root, disk=str(self.state_dir))
        from .app_clock import Reader
        return Reader(state_dir=str(self.state_dir))

    def _small_frame(self, app):
        """One frame of a small app's ANSI. Runs in a worker thread: the
        readings are small file reads, but an SMC or sqlite read must never
        hold up typing or the bus."""
        source, previous = self._app_sources[app]
        started = time.perf_counter()
        if app == "monitor":
            from .app_monitor import render
            reading = source.sample()
            frame = render(reading, previous, dict(self.body), {"state": self.state, "tok_s": self.code_tok_s})
            # Only while this app is still open with this sampler: a close that
            # came while this thread ran must not be undone.
            if self._app_sources.get(app, (None,))[0] is source:
                self._app_sources[app] = (source, reading)
        else:
            from .app_clock import render
            now = self.wall()
            frame = render(now, source.read(now))
        self.frame_ms[app] = (time.perf_counter() - started) * 1000
        return frame.encode("utf-8")

    async def _run_small(self, app, renderer):
        """A frame just after each second turns, so the clock ticks on time.
        Nothing is read or drawn while the lid is closed or Pip sleeps."""
        while self.apps.get(app) is renderer:
            now = self.wall()
            await asyncio.sleep(APP_FRAME - (now % APP_FRAME) + 0.02 if APP_FRAME >= 1 else APP_FRAME)
            if self.apps.get(app) is not renderer:
                continue
            if not renderer.alive():
                # Its renderer ended (it died, or reached its unit's run-time
                # limit): the app is closed, and the mind is told so, so "show
                # me the clock" opens a fresh one instead of "It's already open".
                log(f"{app} renderer ended; closing it")
                self._app_tasks.pop(app, None)
                await self._close_small(app)
                return
            if self._resting() in ("lid", "sleep"):
                continue
            try:
                frame = await asyncio.to_thread(self._small_frame, app)
            except Exception as exc:  # one bad reading must not end the app
                log(f"{app} frame failed: {type(exc).__name__}")
                continue
            if self.apps.get(app) is renderer:
                renderer.write(frame)

    async def _start_terminal(self):
        self.sessions += 1
        if not self._leftovers_stopped:
            await asyncio.to_thread(self.cage.stop_leftovers)
            self._leftovers_stopped = True
        if not self.cage.real:
            self.workshop.mkdir(parents=True, exist_ok=True)
        self.renderer = self.make_renderer("terminal")
        await self.renderer.start()
        session = Session(self.cage, self.sessions)
        self.session = await asyncio.to_thread(session.start)
        self.osc = OscFilter()
        self._watch_pty()
        if not self.renderer.alive():
            # Starting the shell took long enough for a failed renderer unit
            # to have exited: the panel would never appear, so say so.
            raise OSError(errno.EIO, "the renderer did not start")
        log(f"session {self.session.unit} started")

    def _watch_pty(self):
        if self.session is not None and not self._reading:
            asyncio.get_running_loop().add_reader(self.session.master, self._on_pty)
            self._reading = True

    def _unwatch_pty(self):
        if self.session is not None and self._reading:
            try:
                asyncio.get_running_loop().remove_reader(self.session.master)
            except (ValueError, OSError):
                pass
        self._reading = False

    async def _stop_terminal(self):
        await self._end_generation()
        self._unwatch_pty()
        session, self.session = self.session, None
        renderer, self.renderer = self.renderer, None
        if session is not None:
            await asyncio.to_thread(session.close)
            log(f"session {session.unit} closed")
        if renderer is not None:
            await renderer.stop()
        self.transcript.wipe()
        self._kill_at = None
        self._typing = b""
        self.view["terminal"] = "closed"

    async def _restart_shell(self):
        """Swap the session's shell for a fresh one, keeping the panel. Used
        when a script is forgotten: the old shell's in-memory history held
        every line Pip typed. If the new shell does not start, the terminal
        closes and says so."""
        if self.session is None:
            return
        self._unwatch_pty()
        old, self.session = self.session, None
        self._kill_at = None
        self._typing = b""
        await asyncio.to_thread(old.close)
        self.sessions += 1
        try:
            self.session = await asyncio.to_thread(Session(self.cage, self.sessions).start)
        except Exception as exc:
            log(f"shell did not restart: {type(exc).__name__}")
            await self._stop_terminal()
            await self.announce_state("terminal")
            return
        self.osc = OscFilter()
        self._watch_pty()
        log(f"session {old.unit} replaced by {self.session.unit}")
        await self.announce_state("terminal")

    async def minimise(self, app, req=None):
        app = app or "terminal"
        if app not in APPS or self.view[app] == "closed":
            await self.send("app.refused", req=req, reason="no_session", app=app)
            return
        if app in SMALL_APPS:
            await self._close_small(app, req)  # the small apps close instead (voice-apps 3.5)
            return
        if self.view[app] == "shown" and self.renderer is not None:
            await self.renderer.hide()
        self.view[app] = "minimised"
        self._minimised_at = self.clock()
        if self._typist:
            self._typist.instant = True
        self._panel_flags()
        await self.announce_state(app, req)

    async def restore(self, app, req=None):
        app = app or "terminal"
        if app not in APPS or self.view[app] == "closed":
            await self.send("app.refused", req=req, reason="no_session", app=app)
            return
        if app in SMALL_APPS:
            await self.announce_state(app, req)  # never minimised: it is already shown
            return
        await self.open(app, req)

    async def close(self, app, force=False, req=None):
        app = app or "terminal"
        if app not in APPS or self.view[app] == "closed":
            await self.send("app.refused", req=req, reason="no_session", app=app)
            return
        if app in SMALL_APPS:
            await self._close_small(app, req)
            return
        if self.job is not None and self.job.phase in ACTIVE and not force:
            await self.send("app.refused", req=req, reason="busy", app=app, job=self.job.phase)
            return
        if self.job is not None and self.job.phase in ACTIVE:
            await self._cancel_job("closed")
        await self._stop_terminal()
        await self.announce_state(app, req)

    async def privacy(self, guest):
        """Guest mode changed: close the session and clear the workshop.
        No terminal while a guest is here (decision 7)."""
        changed = guest != self.guest
        self.guest = guest
        if changed or guest:
            await self.forget({"all": True, "quiet": True})

    def _panel_flags(self):
        if self.renderer is None:
            return
        job = self.job
        typing = bool(job and job.phase in ("writing", "reading") and self._typist and not self._typist.stopped(self.clock()))
        paused = bool(job and job.phase in ("paused", "held", "frozen"))
        busy = bool(job and job.phase in BUSY)
        human = bool(self._typist and self._typist.human_until)
        self.renderer.flags(visible=self.view["terminal"] == "shown", focus=self.view["terminal"] == "shown",
                            pip="typing" if typing else "paused" if paused else "idle",
                            job="busy" if busy else "idle", human=human, title="Pip's terminal",
                            status=self.status_text())

    def status_text(self):
        job = self.job
        if job is None:
            return "Ready"
        phase = job.phase
        elapsed = int(self.clock() - (job.run_started or job.started))
        if phase == "reading":
            return f"reading your request… {elapsed} s"
        if phase in ("writing", "held"):
            return "● Pip typing"
        if phase == "paused":
            return "Paused · say carry on"
        if phase == "checking":
            return "Checking"
        if phase == "waiting":
            return "Written · say run it"
        if phase == "countdown":
            return f"Running in {job.countdown_left} · Esc or say stop"
        if phase == "running":
            return f"Running {elapsed // 60}:{elapsed % 60:02d} · Esc stops"
        if phase == "frozen":
            return "Frozen · say carry on"
        if phase == "done":
            return f"Done · exit {job.exit}" if job.exit is not None else "Done"
        if phase == "failed":
            return f"Failed · exit {job.exit}" if job.exit is not None else "Failed"
        return "Cancelled · say carry on to undo" if self.clock() < job.undo_until else "Ready"

    # -- the pty ------------------------------------------------------------------------

    def _on_pty(self):
        now = self.clock()
        self._relay_budget = min(RELAY_RATE, self._relay_budget + (now - self._relay_at) * RELAY_RATE)
        self._relay_at = now
        if self._relay_budget <= 0 or (self.renderer is not None and self.renderer.backlog() > RELAY_RATE):
            # A flooding program blocks on its tty instead of burning CPU.
            self._unwatch_pty()
            asyncio.get_running_loop().call_later(0.1, self._watch_pty)
            return
        data = self.session.read(min(8192, max(int(self._relay_budget), 1)))
        if data is None:
            return
        if not data:
            self._unwatch_pty()
            asyncio.get_running_loop().create_task(self._shell_gone())
            return
        self._relay_budget -= len(data)
        clean, markers = self.osc.feed(data)
        if clean:
            self.transcript.add(clean)
            if self.renderer is not None:
                self.renderer.write(clean)
            self._on_output(clean)
        if markers and self.session.foreground_is_shell():
            asyncio.get_running_loop().create_task(self._on_prompt(markers[-1]))

    async def _shell_gone(self):
        if self.session is None:
            return
        log("shell ended")
        if self.job is not None and self.job.phase in ACTIVE:
            self.job.phase, self.job.reason = "failed", "shell_ended"
            await self.status(speakable=True)
        await self._stop_terminal()
        await self.announce_state("terminal")

    def _on_output(self, clean):
        job = self.job
        if job is None or job.phase not in ("running", "frozen"):
            return
        job.output_bytes += len(clean)
        job.output_lines += clean.count(b"\n")
        self._out_since_run = (self._out_since_run + clean)[-4096:]

    async def _on_prompt(self, marker):
        job = self.job
        if job is None:
            return
        status = int(marker) if marker.isdigit() else None
        if job.phase == "checking":
            await self._check_file()
        elif job.phase == "running":
            job.exit = status
            job.phase = "done" if status == 0 else "failed"
            if status is None:
                job.phase, job.reason = "done", "exit_unknown"
            job.seconds = self.clock() - job.run_started
            log(f"job {job.id} {job.phase} exit {status} after {job.seconds:.1f} s")
            await self.status(speakable=True)

    # -- typing ---------------------------------------------------------------------------

    def _type(self, text, *, urgent=False):
        """Write typed text, with Enter as CR as a keyboard sends it.

        The pty takes only so much at once (about 4 KB on Linux, less while
        the shell is busy echoing): what it does not take waits, in order, for
        the next tick, and is never silently dropped. An urgent key (Ctrl-C or
        Ctrl-U for a cancel) first drops whatever of the cancelled text is
        still waiting, so a heredoc's end word queued behind it can never
        complete the file."""
        if self.session is None or not text:
            return
        if urgent:
            self._typing = b""
        self._typing += text.replace("\n", "\r").encode()
        self._flush_typing()

    def _flush_typing(self):
        if self.session is None:
            self._typing = b""
            return
        while self._typing:
            written = self.session.write(self._typing)
            if written < 0:
                self._typing = b""  # the pty is gone
            if written <= 0:
                return
            self._typing = self._typing[written:]

    async def tick(self):
        now = self.clock()
        job = self.job
        if self._typing:
            self._flush_typing()
        if self._kill_at is not None and now >= self._kill_at:
            # Ctrl-C went KILL_GRACE ago. Whatever the job's phase is now
            # (a cancel moves it on at once), anything still in front of the
            # shell is killed: every process in the session but the shell.
            self._kill_at = None
            if job is not None:
                job.killed_at = 0.0
            if self.session is not None and not self.session.foreground_is_shell():
                await asyncio.to_thread(self.session.kill_children)
        if job is None or job.phase not in ACTIVE:
            if (self._minimised_at is not None and now - self._minimised_at > MINIMISED_IDLE_CLOSE
                    and self.view["terminal"] == "minimised"):
                self._minimised_at = None
                await self.close("terminal")
                return
            if job is None:
                return
        hold_until = getattr(self, "_hold_until", None)
        if hold_until is not None and now >= hold_until:
            await self.control("release", "quiet")
        typist = self._typist
        if typist is not None and job.phase in ("reading", "writing", "held", "paused") and self.session:
            if job.phase in ("reading", "writing") and self.session.foreground_is_shell():
                # Text is typed only while the shell itself is in the
                # foreground: never into a running program.
                out = typist.take(now)
                if out:
                    self._type(out)
                if typist.done and job.generation_done and job.phase == "writing":
                    await self._typed_all()
            if job.phase == "writing" and job.source == "model" and not job.generation_done and not job.parked:
                if now - (job.last_token or job.started) > STALL_SECONDS:
                    await self._pause_job("stall", speak=True)
            if job.phase in ("reading", "writing") and now - job.last_status >= STATUS_EVERY:
                await self.status()
        elif job.phase == "countdown":
            left = max(0, int(job.deadline - now + 0.999))
            if left != job.countdown_left:
                job.countdown_left = left
                self._panel_flags()
            if now >= job.deadline:
                self._type("\n")
                job.phase, job.run_started = "running", now
                job.deadline = now + RUN_LIMIT
                await self.status()
        elif job.phase == "checking":
            if now >= job.check_deadline:
                await self._check_file(final=True)
        elif job.phase == "running":
            if now >= job.deadline and not job.killed_at:
                job.reason = "time_limit"
                await self._interrupt_run()
            elif (job.kind != "run_app" and not job.killed_at
                  and (job.output_lines > OUTPUT_LINES or job.output_bytes > OUTPUT_BYTES)):
                # A shelf game redraws its screen many times a second; the
                # relay rate and the cage's CPU quota bound it instead.
                job.reason = "output_limit"
                await self._interrupt_run()
            elif now - job.last_status >= STATUS_EVERY:
                await self.status()

    async def _interrupt_run(self):
        self._type("\x03", urgent=True)
        self.job.killed_at = self.clock()
        self._kill_at = self.job.killed_at + KILL_GRACE

    async def _typed_all(self):
        job = self.job
        if job.kind in ("script", "recipe") and job.file:
            job.phase = "checking"
            job.check_deadline = self.clock() + 5.0
            await self.status()
        elif job.kind == "command" or (job.kind == "recipe" and not job.file):
            job.phase, job.deadline = "countdown", self.clock() + COUNTDOWN
            job.countdown_left = COUNTDOWN
            await self.status(speakable=False)
        else:  # install, uninstall, run_app: Enter was part of the text
            job.phase, job.run_started = "running", self.clock()
            job.deadline = job.run_started + (APP_RUN_LIMIT if job.kind == "run_app" else RUN_LIMIT)
            await self.status()

    async def _check_file(self, final=False):
        """Called on each prompt marker while checking, and once at the
        deadline. A marker from an earlier line can arrive first, so a file
        that is not there yet (or not yet as typed) is waited for."""
        job = self.job
        if job is None or job.phase != "checking":
            return
        raw = read_workshop_file(self.workshop, job.file)
        expected = job.body if job.body.endswith("\n") else job.body + "\n"
        if raw is None or (not job.fix_round and raw.decode("utf-8", "replace") != expected):
            if not final:
                return
            job.phase, job.reason = "failed", "not_written" if raw is None else "changed_while_typing"
            await self.status(speakable=True)
            return
        if len(raw) > MAX_FILE:
            job.phase, job.reason = "failed", "too_big"
            await self.status(speakable=True)
            return
        text = raw.decode("utf-8", "replace")
        if job.ext == "py":
            result = check_python(text, job.file)
            if not result.ok:
                job.phase, job.reason = "failed", "policy:" + result.reason
                job.sha256 = hashlib.sha256(raw).hexdigest()
                log(f"job {job.id} refused by the checks")
                await self.status(speakable=True)
                return
            if result.fixes:
                if job.fix_round:
                    if not final:
                        return
                    job.phase, job.reason = "failed", "fix_failed"
                    await self.status(speakable=True)
                    return
                # The free fix is typed visibly, then checked again.
                fix = "".join(f"{line}\n" for line in result.fixes)
                self._typist = Typist(fixed_rate=RECIPE_RATE, now=self.clock())
                self._typist.instant = self.view["terminal"] == "minimised"
                self._typist.feed(fix, self.clock())
                self._typist.end()
                job.phase, job.reason = "writing", "import_fix" if result.missing else "trimmed"
                job.generation_done = True
                job.fix_round = 1
                return
        job.sha256 = hashlib.sha256(raw).hexdigest()
        job.phase = "waiting"
        log(f"job {job.id} written, {job.lines} lines")
        await self.status(speakable=True)

    # -- tasks -----------------------------------------------------------------------------

    async def task(self, event):
        kind = event.get("kind")
        text = sanitise(event.get("text") or "", MAX_REQUEST).replace("\n", " ").strip()
        job_id = str(event.get("id") or "")[:32] or None
        if self._refusal():
            await self.send("app.refused", req=job_id, reason=self._refusal(), app="terminal")
            return
        if self.job is not None and self.job.phase in ACTIVE:
            await self.send("app.refused", req=job_id, reason="busy", app="terminal", job=self.job.phase)
            return
        if kind not in ("recipe", "script", "command", "install", "uninstall", "run_app"):
            await self.send("app.refused", req=job_id, reason="unknown", app="terminal")
            return
        recipe = RECIPES.get(str(event.get("recipe") or ""))
        runs = kind in ("command", "run_app") or (kind == "recipe" and recipe is not None and recipe.kind == "command")
        if runs and self._low_battery():
            # Unplugged and low: nothing new runs (writing a script still may).
            await self.send("app.refused", req=job_id, reason="low_battery", app="terminal")
            return
        if self._hot:
            await self.send("app.refused", req=job_id, reason="too_warm", app="terminal")
            return
        if self.view["terminal"] == "closed":
            await self.open("terminal", job_id)
            if self.view["terminal"] == "closed":
                return
        elif self._small_shown() or (kind == "run_app" and self.view["terminal"] == "minimised"):
            # Work in the terminal is shown: a game needs its player, and the
            # monitor or the clock would otherwise hide what Pip types. If it
            # cannot be brought forward (no_face: the Try menu has the screen)
            # the refusal already answered this req, so nothing starts.
            if not await self.open("terminal", job_id):
                return
        self.jobs_made += 1
        job = Job(id=job_id or f"j{self.jobs_made}", kind=kind, text=text, source="model",
                  recipe=str(event.get("recipe") or ""), slots=dict(event.get("slots") or {}),
                  force_model=bool(event.get("force_model")), started=self.clock())
        job.request = sanitise(event.get("request") or "", 400).replace("\n", " ").strip() or text
        job.seed = request_seed(job.request)
        if kind == "recipe" and job.recipe not in RECIPES:
            kind = job.kind = "script"
        if kind == "recipe" and not job.force_model:
            await self._start_recipe(job)
        elif kind in ("install", "uninstall", "run_app"):
            await self._start_shelf(job, event.get("app") or "")
        else:
            if kind == "recipe":
                job.kind = "script" if RECIPES[job.recipe].kind == "script" else "command"
            await self._start_model(job)

    def _intro(self, job, label):
        asked = job.text[:GRID_COLUMNS - 16].replace("\n", " ")
        return f"# you asked: {asked}\n# {label}\n"

    def _exists(self, name):
        return workshop_exists(self.workshop, name)

    def _installed(self, word):
        try:
            return stat.S_ISDIR((self.workshop / "apps" / word).lstat().st_mode)
        except OSError:
            return False

    async def _start_recipe(self, job):
        recipe = RECIPES[job.recipe]
        try:
            body = recipe.build(job.slots)
        except ValueError as exc:
            ranges = [(low, high) for _, low, high, _ in recipe.slots if low != "word"]
            low, high = ranges[0] if ranges else (None, None)
            await self.send("app.refused", req=job.id, reason="bad_slot", app="terminal", detail=str(exc),
                            low=low, high=high)
            return
        job.source = "recipe"
        intro = self._intro(job, f"recipe: {recipe.name.replace('_', ' ')} (from Pip's recipe book)")
        if recipe.kind == "script":
            job.ext = recipe.ext
            job.file = unique_name(recipe.name, recipe.ext, self._exists)
            job.word = end_word(self.rng)
            text = intro + heredoc(job.file, body, job.word)
            job.body = body
        else:
            job.line = body.strip()
            text = intro + job.line
            job.body = ""
        self._begin(job, text, fixed_rate=RECIPE_RATE)
        job.generation_done = True
        job.phase = "writing"
        log(f"job {job.id} recipe {recipe.name}")
        await self.status()

    async def _start_shelf(self, job, app):
        job.source = "shelf"
        word = re.sub(r"[^a-z0-9-]", "", str(app).lower())[:24]
        if not word:
            await self.send("app.refused", req=job.id, reason="unknown", app="terminal")
            return
        verb = {"install": "install", "uninstall": "uninstall", "run_app": "run"}[job.kind]
        job.app = word
        job.line = f"{verb} {word}"
        if job.kind == "run_app" and not await asyncio.to_thread(self._installed, word):
            # "Play snake" before "install snake": install it first, from the
            # shelf (offline), then play.
            job.line = f"install {word} && run {word}"
        self._begin(job, job.line + "\n", fixed_rate=RECIPE_RATE)
        job.generation_done = True
        job.phase = "writing"
        await self.status()

    def _begin(self, job, text, fixed_rate=None):
        self.job = job
        self._typist = Typist(fixed_rate=fixed_rate, now=self.clock())
        self._typist.instant = self.view["terminal"] == "minimised"
        self._typist.feed(text, self.clock(), known=True)
        if fixed_rate:
            self._typist.end()
        self._hold_until = None
        self._out_since_run = b""

    async def _start_model(self, job, *, retype=""):
        job.source = "model"
        intro = self._intro(job, f"written by my language model ({self.model_label})")
        if job.kind == "script":
            job.ext = "py"
            if not job.file:
                job.file = unique_name(name_stem(job.request or job.text), "py", self._exists)
            job.word = job.word or end_word(self.rng)
            text = intro + heredoc_open(job.file, job.word) + retype
        else:
            text = intro + retype
        self._begin(job, text)
        job.phase = "reading"
        job.last_token = 0.0
        await self.status()
        self._gen_task = asyncio.get_running_loop().create_task(self._generate(job))

    async def _generate(self, job):
        try:
            if not job.prompt_ids:
                opening = "```python\n" if job.kind == "script" else "```sh\n"
                messages = code_messages(job.kind, job.request or job.text, previous=job.previous or None,
                                         error=job.error or None)
                job.prompt_ids = await self.cortex.prompt_tokens(messages, opening)
            if job.kind == "script":
                grammar = python_grammar(MAX_LINES, done=job.body.count("\n"),
                                         mid_line=bool(job.body) and not job.body.endswith("\n"))
            else:
                grammar = shell_grammar()
            stream = self.cortex.stream(job.prompt_ids + job.tokens, grammar=grammar, seed=job.seed,
                                        temperature=job.temperature)
            job.stream = stream
            async for text, tokens in stream:
                if self.job is not job or job.parked:
                    break
                job.tokens.extend(tokens)
                job.last_token = self.clock()
                if job.phase == "reading":
                    # A wake word heard while the prompt was read holds the typist.
                    job.phase = "held" if self._typist is not None and self._typist.held else "writing"
                if not self._accept(job, text):
                    stream.cancel()
                    return
            else:
                speed = (stream.timings or {}).get("predicted_per_second")
                if type(speed) in (int, float) and 0 < speed < 10_000:
                    self.code_tok_s = float(speed)  # the body monitor shows it
                if self.job is job and not job.parked:
                    await self._generation_finished(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.job is job and not job.parked and job.phase in GENERATING:
                log(f"job {job.id} lost its thought: {type(exc).__name__}")
                await self._pause_job("lost", speak=True)

    def _accept(self, job, text):
        """Take model text into the file body (or the command line). False ends the job."""
        text = sanitise(text)
        if job.kind == "script":
            # The grammar allows a backtick only as the start of the closing
            # fence, so the first one ends the body.
            fence = text.find("`")
            if fence >= 0:
                text = text[:fence]
                job.generation_done = True
            job.body += text
            lines = job.body.split("\n")
            if any(len(line) > MAX_LINE for line in lines):
                return self._fail_later(job, "long_line")
            if any(line.strip() == job.word for line in lines):
                return self._fail_later(job, "end_word")
            complete = [line.strip() for line in lines[:-1] if len(line.strip()) >= 4]
            if len(complete) >= 3 and complete[-1] == complete[-2] == complete[-3]:
                return self._fail_later(job, "loop")
            if len(lines) > MAX_LINES + 1:
                return self._fail_later(job, "too_long")
            try:
                self._typist.feed(text, self.clock(), known=False)
            except OverflowError:
                return self._fail_later(job, "too_long")
            if job.generation_done:
                asyncio.get_running_loop().create_task(self._generation_finished(job))
                return False
        else:
            cut = text.find("\n")
            job.line += text if cut < 0 else text[:cut]
            if "`" in job.line:
                job.line = job.line.split("`")[0]
                job.generation_done = True
            if cut >= 0:
                job.generation_done = True
            if len(job.line) > 200:
                return self._fail_later(job, "long_line")
            if job.generation_done:
                asyncio.get_running_loop().create_task(self._generation_finished(job))
                return False
        return True

    def _fail_later(self, job, reason):
        asyncio.get_running_loop().create_task(self._cancel_job(reason, speak=True))
        return False

    async def _generation_finished(self, job):
        if self.job is not job or job.phase not in GENERATING + ("paused",) or job.finished:
            return
        job.finished = True
        job.generation_done = True
        if job.phase == "reading":
            job.phase = "held" if self._typist is not None and self._typist.held else "writing"
        if job.kind == "script":
            if not job.body.strip():
                await self._cancel_job("empty", speak=True)
                return
            tail = "" if job.body.endswith("\n") else "\n"
            if not job.body.endswith("\n"):
                job.body += "\n"
            self._typist.feed(tail + job.word + "\n", self.clock(), known=True)
        else:
            reason = check_shell_line(job.line, self._exists)
            if reason:
                job.reason = "policy:" + reason
                await self._cancel_job(job.reason, speak=True, wipe_line=False)
                return
            self._typist.feed(job.line.strip(), self.clock(), known=True)
        self._typist.end()

    async def _end_generation(self):
        job, task = self.job, self._gen_task
        if job is not None and getattr(job, "stream", None) is not None:
            job.stream.cancel()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._gen_task = None

    # -- control ------------------------------------------------------------------------------

    async def control(self, action, reason="owner", job_id=None):
        job = self.job
        if job is None or (job_id and job.id != job_id):
            if action not in ("hold", "release"):
                await self.reply("term.status", id=job_id, phase=None, reason="no_job", speakable=False)
            return
        if action == "hold":
            if job.phase in ("reading", "writing") and self._typist:
                self._typist.hold()
                job.phase = "held" if job.phase == "writing" else job.phase
                self._hold_until = self.clock() + HOLD_SECONDS
        elif action == "release":
            self._hold_until = None
            # Whatever the phase now (a hold taken while reading may have
            # moved on to writing), the wake hold always lets go.
            if self._typist is not None:
                self._typist.release()
            if job.phase == "held":
                job.phase = "writing"
        elif action == "pause":
            if job.phase in GENERATING:
                await self._pause_job(reason, speak=False)
            elif job.phase == "running":
                if self.session and self.session.freeze():
                    self._frozen(job, reason)
                await self.status()
            elif job.phase == "countdown":
                self._type("\x15", urgent=True)
                await self._cancel_job(reason)
            else:
                await self.status()
        elif action == "resume":
            await self._resume(reason)
        elif action in ("cancel", "stop_run"):
            if job.phase in ("running", "frozen"):
                if self.session:
                    self.session.thaw()
                self._thawed(job)
                job.reason = "stopped" if action == "stop_run" else "cancelled"
                await self._interrupt_run()
                job.phase = "cancelled" if action == "cancel" else "running"
                if action == "cancel":
                    job.undo_until = 0.0
                    await self.status(speakable=False)
            elif job.phase in ACTIVE or job.phase == "waiting":
                await self._cancel_job(reason if reason != "owner" else "cancelled")
            else:
                await self.status()
        elif action == "undo":
            await self._undo()
        elif action == "run":
            await self._run_file()
        elif action == "fix":
            await self._fix()
        else:
            await self.reply("term.status", id=job.id, phase=job.phase, reason="unknown_action", speakable=False)

    def _frozen(self, job, reason):
        job.phase, job.reason = "frozen", reason
        job.frozen_at = self.clock()

    def _thawed(self, job):
        """Time spent frozen does not count against the run's limit."""
        if job.frozen_at:
            job.deadline += self.clock() - job.frozen_at
            job.frozen_at = 0.0

    async def _pause_job(self, reason, speak=False):
        job = self.job
        if self._typist:
            self._typist.pause()
        if job.source == "model" and not job.generation_done:
            # Park: cut the stream, keep the token IDs.
            job.parked = True
            await self._end_generation()
        job.phase, job.reason = "paused", reason
        self._hold_until = None
        await self.status(speakable=speak)

    async def _resume(self, reason):
        job = self.job
        resting = self._resting()
        if job.phase == "frozen":
            if resting:
                await self.status(reason=REST_REPLY[resting])
                return
            if self.session:
                self.session.thaw()
            self._thawed(job)
            job.phase, job.reason = "running", ""
            await self.status()
        elif job.phase in ("paused", "held"):
            if resting:
                # Always answer, so the mind never says "My hands didn't answer."
                await self.status(reason=REST_REPLY[resting])
                return
            self._typist.resume()
            job.reason = ""
            self._hold_until = None
            if job.parked and not job.generation_done:
                job.parked = False
                job.phase = "reading"
                job.last_token = self.clock()
                self._gen_task = asyncio.get_running_loop().create_task(self._generate(job))
            else:
                job.phase = "writing"
            await self.status()
        elif job.phase == "cancelled" and self.clock() < job.undo_until:
            await self._undo()
        else:
            await self.status()

    async def _cancel_job(self, reason, speak=False, wipe_line=True):
        job = self.job
        if job is None:
            return
        was = job.phase
        await self._end_generation()
        if was == "frozen" and self.session is not None:
            # Ctrl-C and the kill must reach a thawed cage.
            await asyncio.to_thread(self.session.thaw)
            self._thawed(job)
        if was in ("reading", "writing", "held", "paused", "countdown", "checking"):
            # Ctrl-C at the prompt or inside the heredoc: ash drops the
            # unfinished command, so no half file is written.
            if self._typist is not None and (self._typist.typed or was == "countdown"):
                self._type("\x03" if was != "countdown" else "\x15", urgent=True)
        if was in ("running", "frozen"):
            await self._interrupt_run()
        job.phase, job.reason = "cancelled", reason
        job.undo_until = self.clock() + UNDO_SECONDS if reason in ("cancelled", "owner") else 0.0
        if self._typist:
            self._typist.pause()
        log(f"job {job.id} cancelled: {reason.split(':')[0]}")
        await self.status(speakable=speak)

    async def _undo(self):
        job = self.job
        if job is None or job.phase != "cancelled" or self.clock() >= job.undo_until:
            await self.reply("term.status", id=job.id if job else None, phase=job.phase if job else None,
                            reason="no_undo", speakable=False)
            return
        if job.source == "recipe":
            fresh = Job(id=job.id, kind="recipe", text=job.text, source="recipe", recipe=job.recipe,
                        slots=job.slots, started=self.clock())
            await self._start_recipe(fresh)
        elif job.source == "shelf":
            # The helper line again; never the model.
            fresh = Job(id=job.id, kind=job.kind, text=job.text, source="shelf", started=self.clock())
            await self._start_shelf(fresh, job.app)
        else:
            fresh = Job(id=job.id, kind=job.kind, text=job.text, request=job.request, source="model", started=self.clock(),
                        prompt_ids=job.prompt_ids, tokens=list(job.tokens), body=job.body, line=job.line,
                        seed=job.seed, temperature=job.temperature, previous=job.previous, error=job.error,
                        fixed=job.fixed, parent_file=job.parent_file)
            fresh.file = unique_name(name_stem(job.request), "py", self._exists) if job.kind == "script" else ""
            fresh.generation_done = job.generation_done
            retype = job.body if job.kind == "script" else job.line
            if fresh.generation_done:
                self.job = fresh
                await self._start_model_done(fresh, retype)
            else:
                await self._start_model(fresh, retype=retype)

    async def _start_model_done(self, job, retype):
        intro = self._intro(job, f"written by my language model ({self.model_label})")
        if job.kind == "script":
            job.word = end_word(self.rng)
            text = intro + heredoc(job.file, retype, job.word)
        else:
            text = intro + retype.strip()
        self._begin(job, text, fixed_rate=RECIPE_RATE)
        job.generation_done = True
        job.phase = "writing"
        await self.status()

    async def _run_file(self):
        job = self.job
        if self.session is not None and self.session.frozen:
            # The cage is frozen while the lid is closed, Pip sleeps or is too warm.
            await self.reply("term.status", id=job.id, phase=job.phase, reason=REST_REPLY[self._resting() or "lid"],
                             speakable=False)
            return
        if self._low_battery():
            await self.reply("term.status", id=job.id, phase=job.phase, reason="low_battery", speakable=False)
            return
        if job.phase in ("waiting", "done", "failed") and job.file and not job.reason.startswith("policy"):
            if not self.session or not self.session.foreground_is_shell():
                await self.reply("term.status", id=job.id, phase=job.phase, reason="not_at_prompt", speakable=False)
                return
            runner = "python3" if job.file.endswith(".py") else "sh"
            job.run_line = f"{runner} {job.file}"
            job.exit, job.reason, job.output_lines, job.output_bytes = None, "", 0, 0
            self._out_since_run = b""
            self._type(job.run_line + "\n")
            job.phase, job.run_started = "running", self.clock()
            job.deadline = job.run_started + RUN_LIMIT
            log(f"job {job.id} running")
            await self.status()
        elif job.phase in ("done", "failed") and job.line and job.kind == "command":
            self._type(job.line + "\n")
            job.phase, job.run_started, job.exit = "running", self.clock(), None
            job.deadline = job.run_started + RUN_LIMIT
            await self.status()
        else:
            await self.reply("term.status", id=job.id, phase=job.phase, reason="nothing_to_run", speakable=False)

    def error_tail(self):
        """The last three error lines of the last run, at most 200 characters."""
        text = self._out_since_run.decode("utf-8", "replace")
        text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        lines = [line for line in lines if not line.startswith("pip:") and "python3 " not in line]
        return " | ".join(lines[-3:])[-200:]

    async def _fix(self):
        job = self.job
        if job.phase != "failed" or job.source != "model" or job.kind != "script" or job.fixed or job.exit in (None, 0):
            await self.reply("term.status", id=job.id, phase=job.phase, reason="no_fix", speakable=False)
            return
        fresh = Job(id=job.id + "f", kind="script", text=job.text, request=job.request, source="model", started=self.clock(),
                    seed=job.seed + 1, temperature=0.4, previous=job.body, error=self.error_tail(), fixed=True,
                    parent_file=job.file)
        fresh.file = unique_name(job.file.rsplit(".", 1)[0].split("-")[0], "py", self._exists)
        await self._start_model(fresh)

    async def read_back(self, job_id=None):
        lines = [line for line in self.transcript.lines() if line.strip() and not line.startswith("pip:")]
        last = lines[-1] if lines else ""
        wordy = sum(ch.isalpha() or ch == " " for ch in last) >= 0.6 * max(len(last), 1)
        text = last if len(last) <= 80 and wordy else ""
        count = self.job.output_lines if self.job else 0
        await self.reply("term.text", id=job_id or (self.job.id if self.job else None), text=text, lines=count)

    # -- body and brainstem --------------------------------------------------------------------

    async def on_state(self, state):
        previous, self.state = self.state, state
        job = self.job
        if state == "asleep":
            await self._rest("sleep")
        elif previous == "asleep" and state != "asleep":
            await self._wake("sleep")
        if job is not None and state == "idle" and getattr(self, "_hold_until", None):
            await self.control("release", "idle")

    async def on_lid(self, closed):
        previous, self.lid_closed = self.lid_closed, closed
        if closed is True:
            await self._rest("lid")
        elif closed is False and previous is True:
            await self._wake("lid")

    async def _rest(self, reason):
        """Lid closed or asleep: stop typing, park the model, freeze the cage."""
        job = self.job
        if job is not None and job.phase in GENERATING:
            await self._pause_job(reason)
            self._lid_paused = True
        elif job is not None and job.phase == "paused" and self._lid_paused:
            job.reason = reason  # the latest reason is the one said on waking
        if job is not None and job.phase == "countdown":
            await self._cancel_job(reason)
        if self.session is not None:
            await asyncio.to_thread(self.session.freeze)
            if job is not None and job.phase == "running":
                self._frozen(job, reason)
                await self.status()

    def _resting(self):
        """Why the cage must stay frozen now, or None."""
        if self.lid_closed is True:
            return "lid"
        if self.state == "asleep":
            return "sleep"
        if self._hot:
            return "heat"
        return None

    async def _wake(self, reason):
        if self._resting():
            return  # still closed, asleep or too warm: nothing thaws yet
        job = self.job
        owner_froze = job is not None and job.phase == "frozen" and job.reason not in REST_REASONS
        if self.session is not None and not owner_froze:
            # A program the owner froze ("stop for a second") stays frozen
            # until they say carry on.
            await asyncio.to_thread(self.session.thaw)
            if job is not None and job.phase == "frozen" and job.reason in REST_REASONS:
                self._thawed(job)
                job.phase, job.reason = "running", ""
                await self.status()
        if self._lid_paused and job is not None and job.phase == "paused":
            # "I paused while the lid was closed. Say carry on." (or while I
            # slept, or got too warm): said once everything is clear.
            self._lid_paused = False
            await self.status(speakable=True)

    async def on_body(self, event):
        """body.state: heat and battery (voice-apps 3.6; thresholds are guesses
        until measured). Freeze at HOT_C on the hottest core, thaw below COOL_C."""
        self.body = {key: event.get(key) for key in (
            "battery", "charging", "on_ac", "minutes_left", "health_pct", "cycles", "temp") if key in event}
        temp, battery = event.get("temp"), event.get("battery")
        self.on_ac = event.get("on_ac") if isinstance(event.get("on_ac"), bool) else self.on_ac
        self.battery = battery if type(battery) in (int, float) and 0 <= battery <= 100 else self.battery
        if type(temp) not in (int, float):
            return
        if temp >= HOT_C and not self._hot:
            self._hot = True
            log(f"too warm ({temp:.0f} C): resting the workshop")
            await self._rest("heat")
        elif temp < COOL_C and self._hot:
            self._hot = False
            await self._wake("heat")

    def _low_battery(self):
        return self.on_ac is False and self.battery is not None and self.battery <= LOW_BATTERY

    async def on_escape(self):
        """Esc: innermost first. Speech belongs to the brainstem."""
        if self.state in ("thinking", "speaking"):
            return
        job = self.job
        if job is None or job.phase not in ACTIVE:
            small = self._small_shown()
            if small:
                await self._close_small(small)
            return
        if job.phase == "countdown":
            await self.control("pause", "key")
        elif job.phase in GENERATING:
            await self.control("pause", "key")
        elif job.phase in ("running", "frozen"):
            await self.control("stop_run", "key")

    # -- keys from the face -----------------------------------------------------------------------

    def on_key(self, data):
        """One datagram per key from the face. Raw bytes go to the shell; a
        leading NUL marks a control word (pgup, pgdn, f3)."""
        if not data or len(data) > 16:
            return
        loop = asyncio.get_running_loop()
        if data.startswith(b"\0"):
            word = data[1:].decode("ascii", "ignore")
            if word == "f3":
                action = self.minimise if self.view["terminal"] == "shown" else self.restore
                if self.view["terminal"] != "closed":
                    loop.create_task(action("terminal"))
            elif word in ("pgup", "pgdn") and self.renderer is not None:
                self.renderer.flags(scroll="up" if word == "pgup" else "down")
            return
        if self.view["terminal"] != "shown" or self.session is None:
            return
        job = self.job
        if job is not None and job.phase == "countdown":
            loop.create_task(self.control("pause", "key"))  # any key cancels the countdown
            return
        if job is not None and job.phase in ("reading", "writing", "held") and self._typist is not None:
            self._typist.human(self.clock())
            self._panel_flags()
        self.session.write(data)

    def _open_key_socket(self):
        if not self.key_socket_path:
            return
        path = Path(self.key_socket_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.setblocking(False)
        old = os.umask(0o177)
        try:
            sock.bind(str(path))
        finally:
            os.umask(old)
        self._key_sock = sock
        asyncio.get_running_loop().add_reader(sock.fileno(), self._read_keys)

    def _read_keys(self):
        for _ in range(64):
            try:
                data = self._key_sock.recv(64)
            except BlockingIOError:
                return
            except OSError:
                return
            self.on_key(data)

    # -- forgetting --------------------------------------------------------------------------------

    async def forget(self, event):
        """term.forget: a job's file and every copy, or everything.

        The reply ('forgotten', under the request's req) goes out as soon as
        the files are gone, before the slower shell restart and slot erase, so
        the mind hears it in time. A job cancelled on the way sends its
        'cancelled' status without the req: that is not the answer."""
        everything = bool(event.get("all"))
        job = self.job
        removed, changed = [], []
        wanted = str(event.get("id") or "")
        parent = job.id[:-1] if job is not None and job.fixed and job.id.endswith("f") else None
        also = tuple(name for name in event.get("files") or () if isinstance(name, str))[:64]

        async def answer():
            if not event.get("quiet"):
                await self.reply("term.status", id=wanted or None, phase="forgotten",
                                 removed=len(removed), changed=changed, speakable=False)

        if everything:
            if job is not None and job.phase in ACTIVE:
                await self._cancel_unanswered("forget")
            for small in SMALL_APPS:
                if self.view[small] != "closed":
                    await self._close_small(small)
            await self._stop_terminal()
            # Never restart workshop.mount from here: being-hands.service
            # needs the mount (RequiresMountsFor=/workshop), so systemd would
            # restart hands itself mid-forget. clear_workshop() zeroes and
            # unlinks every file once the cage units are stopped.
            if self.workshop.is_dir():
                count = await asyncio.to_thread(clear_workshop, self.workshop)
                removed = ["?"] * count
            self.job = None
            self._typist = None
            await answer()
            await self.announce_state("terminal")
        elif also and (job is None or (wanted and wanted not in (job.id, parent))):
            # The changed copies the owner said yes to, after the script
            # itself was forgotten (so there is no job any more).
            if self.workshop.is_dir():
                removed = await asyncio.to_thread(remove_files, self.workshop, also)
            await answer()
            await self._restart_shell()
            if self.renderer is not None:
                self.renderer.wipe()
            self.transcript.wipe()
            self._out_since_run = b""
        else:
            if job is None or (wanted and wanted not in (job.id, parent)):
                await self.reply("term.status", id=wanted or None, phase=None, reason="no_job", speakable=False)
                return
            if job.phase in ACTIVE:
                await self._cancel_unanswered("forget")
            if self.workshop.is_dir():
                # A fix job forgets the file it fixed as well.
                for name in dict.fromkeys(n for n in (job.file, job.parent_file) if n):
                    gone, differ = await asyncio.to_thread(forget_file, self.workshop, name, also=also)
                    removed += [n for n in gone if n not in removed]
                    changed += [n for n in differ if n not in changed and n not in removed]
                changed = [n for n in changed if n not in removed]
            self.job = None
            self._typist = None
            await answer()
            # A fresh shell: the old one's in-memory history held every
            # line Pip typed, the script included.
            await self._restart_shell()
            if self.renderer is not None:
                self.renderer.wipe()
            self.transcript.wipe()
            self._out_since_run = b""
        await self.cortex.erase()
        log(f"forgot {len(removed)} files")

    async def _cancel_unanswered(self, reason):
        token = _REQ.set(None)
        try:
            await self._cancel_job(reason)
        finally:
            _REQ.reset(token)

    # -- running -------------------------------------------------------------------------------------

    async def run(self):
        # After a crash and restart, a caged shell or renderer from the last
        # run may still be there: stop it before anything else (section 3.5).
        try:
            await asyncio.to_thread(self.cage.stop_leftovers)
            self._leftovers_stopped = True
        except Exception as exc:
            log(f"leftovers not stopped: {type(exc).__name__}")
        self._open_key_socket()
        await self.bus.start()
        self._tick_task = asyncio.get_running_loop().create_task(self._ticker())
        try:
            while True:
                await self.on_event(await self.bus.recv())
        finally:
            self._tick_task.cancel()
            # Clear the workshop on the way out, but never past systemd's
            # TimeoutStopSec=5: a slow unit stop or slot erase must not get
            # hands SIGKILLed.
            try:
                await asyncio.wait_for(self.forget({"all": True, "quiet": True}), STOP_FORGET_SECONDS)
            except (asyncio.TimeoutError, Exception) as exc:
                log(f"clear on stop incomplete: {type(exc).__name__}")
            await self.bus.stop()

    async def _ticker(self):
        while True:
            await asyncio.sleep(TICK)
            try:
                await self.tick()
            except Exception as exc:
                log(f"tick failed: {type(exc).__name__}")


async def main():
    parser = argparse.ArgumentParser(prog="mind.hands", description=__doc__.splitlines()[0])
    parser.add_argument("--bus", help="bus socket path")
    parser.add_argument("--cortex", default=os.environ.get("BEING_CORTEX_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--workshop", default=str(WORKSHOP))
    parser.add_argument("--keys", default=str(RUN_DIR / "keys"))
    parser.add_argument("--local-shell", action="store_true",
                        help="Mac and lab only: a plain local shell, NOT caged, and a recording renderer")
    parser.add_argument("--shellrc", default=SHELLRC)
    args = parser.parse_args()
    if args.local_shell:
        cage = LocalCage(args.workshop, args.shellrc)
        renderer = RecordingRenderer
    else:
        if os.geteuid() != 0:
            parser.error("the real cage needs root; use --local-shell on a Mac")
        cage = SystemdCage()
        renderer = PanelRenderer
    bus = Bus("hands", SUBS, path=args.bus)
    hands = Hands(bus, cage=cage, cortex=CodeCortex(args.cortex), workshop=args.workshop, renderer=renderer,
                  key_socket=args.keys)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runner = loop.create_task(hands.run())
    await stop.wait()
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
