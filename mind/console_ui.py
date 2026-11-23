"""Drawing helpers for the installer and setup screens.

being-console runs these screens in a pty and sets BEING_CONSOLE_UI=1. Only
then are colour, box drawing, cursor movement and the other glyphs of the
console contract used. Anywhere else (the serial line, SSH, unit tests) every
helper returns plain ASCII without escape sequences, so logs stay readable and
the exact prompts the lab waits for are unchanged.
"""

import os
import re
import shutil
import sys
import threading
import time


MARGIN = "  "
MAX_WIDTH = 72
ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TONES = {
    "bold": "1", "dim": "2", "reverse": "7",
    "accent": "96", "accent_soft": "36",
    "success": "92", "warning": "93", "danger": "91",
    # Body text keeps the console's default colour (39), which the lab's
    # screen reader decodes; 97 is reserved for emphasis.
    "text": "39", "bright": "97", "plain": "37", "muted": "90",
}
PARTIAL = " ▏▎▍▌▋▊▉"
ICONS = {
    # tone: (styled glyph, ASCII fallback)
    "success": ("✓", "ok"),
    "danger": ("✗", "!!"),
    "warning": ("!", "!"),
    "accent": ("•", "*"),
    "muted": ("·", "-"),
}

_notes = []
_lock = threading.RLock()
_used = [0]  # lines written since the screen was last cleared


def enabled():
    return os.environ.get("BEING_CONSOLE_UI") == "1"


def glyph(fancy, ascii_text):
    return fancy if enabled() else ascii_text


def dot():
    """The separator used between short facts."""
    return glyph(" · ", " - ")


def paint(text, *tones):
    if not enabled() or not tones or not text:
        return text
    codes = ";".join(TONES.get(tone, tone) for tone in tones)
    return f"\x1b[{codes}m{text}\x1b[0m"


def plain(text):
    return ESCAPE.sub("", text)


def visible_len(text):
    return len(plain(text))


def pad(text, width):
    return text + " " * max(0, width - visible_len(text))


def columns():
    return shutil.get_terminal_size((80, 24)).columns


def width():
    """Usable width of a line inside the left margin."""
    return max(36, min(columns() - 2 * len(MARGIN), MAX_WIDTH))


def wrap(text, limit=None, indent=""):
    limit = (limit or width()) - visible_len(indent)
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            if line and visible_len(line) + 1 + visible_len(word) > limit:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        lines.append(line)
    return [indent + line for line in lines]


def write(text):
    with _lock:
        _used[0] += text.count("\n")
        sys.stdout.write(text)
        sys.stdout.flush()


def rows():
    return shutil.get_terminal_size((80, 24)).lines


def room():
    """Lines left on the screen, keeping two for the prompt below."""
    return rows() - _used[0] - 2


def line(text=""):
    """Print one line at the screen margin."""
    write(f"{MARGIN}{text}\n" if text else "\n")


def para(text, *tones, indent=""):
    for part in wrap(text, indent=indent):
        line(paint(part, *tones))


def clear():
    if enabled():
        write("\x1b[2J\x1b[H")
        _used[0] = 0


def cursor(visible):
    if enabled():
        write("\x1b[?25h" if visible else "\x1b[?25l")


def rule(tone="muted"):
    return paint(glyph("─", "-") * width(), tone)


def size(size_bytes):
    """A size as a person reads it on the box: decimal units, three figures."""
    for unit, scale in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if size_bytes >= scale:
            value = size_bytes / scale
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{size_bytes / 1000:.0f} KB"


def note(text, tone="muted", stream=None):
    """A message for the next screen. Without the console UI it prints now."""
    if not enabled():
        print(text, file=stream or sys.stdout, flush=True)
        return
    with _lock:
        _notes.append((text, tone))


def pending_notes():
    with _lock:
        return bool(_notes)


def discard_notes():
    with _lock:
        _notes.clear()


def flush_notes():
    with _lock:
        pending = list(_notes)
        _notes.clear()
    for text, tone in pending:
        icon = paint(ICONS.get(tone, ICONS["muted"])[0], tone)
        first, *rest = wrap(text, indent="  ")
        line(f"{icon} {paint(first[2:], 'text')}")
        for part in rest:
            line(paint(part, "text"))
    if pending:
        line()


def header(title=None, context=None):
    """The title on the left and where you are on the right.

    being-console already draws the being's eyes and the PipOS wordmark
    above the text, so the screens do not repeat the brand.
    """
    left = ""
    if title:
        left = title if ESCAPE.search(title) else paint(title, "bright", "bold")
    if context:
        right = paint(context, "muted")
        gap = width() - visible_len(left) - visible_len(right)
        left = left + " " * max(2, gap) + right
    return left


def screen(title=None, *, context=None, subtitle=None):
    """Start a new screen. Plain output gets a short ASCII heading instead."""
    if not enabled():
        heading = " - ".join(part for part in (context, title) if part)
        if heading:
            print(f"\n{heading}")
        if subtitle:
            for part in wrap(subtitle, MAX_WIDTH):
                print(part)
        return
    with _lock:
        cursor(False)
        clear()
        line(header(title, context))
        line(rule())
        if subtitle:
            para(subtitle, "muted")
        line()
        flush_notes()
        cursor(True)


MENU_ENTRY = re.compile(r"\A  (\S)  (\S.*)\Z")


class _Indented:
    """A stdout that starts every line at the screen margin.

    For older plain-text screens drawn inside a styled one: "  K  Label" menu
    lines become styled entries and a prompt gets the usual marker, with its
    words unchanged. `skip` is a line to drop once, such as a title the
    styled header already shows.
    """

    def __init__(self, stream, skip=None):
        self.stream, self.skip, self.fresh, self.pending = stream, skip, True, ""

    def _emit(self, text):
        with _lock:
            _used[0] += text.count("\n")
        self.stream.write(text)

    def write(self, text):
        *whole, self.pending = (self.pending + text).split("\n")
        out = []
        for part in whole:
            if self.fresh and self.skip is not None and part.strip() in ("", self.skip):
                # Blank lines before the skipped title are dropped too.
                self.skip = None if part.strip() else self.skip
                continue
            entry = MENU_ENTRY.match(part) if self.fresh else None
            if entry:
                part = option(*entry.groups())
            out.append(f"{MARGIN if self.fresh and part else ''}{part}\n")
            self.fresh = True
        self._emit("".join(out))
        return len(text)

    def flush(self):
        # Text still without a newline is a prompt that is about to be read.
        if self.pending:
            self._emit(prompt(self.pending) if self.fresh else self.pending)
            self.fresh, self.pending = False, ""
        self.stream.flush()

    def isatty(self):
        return self.stream.isatty()


class indented:
    """Run plain-text code with its output at the margin, in the console UI."""

    def __init__(self, skip=None):
        self.skip = skip

    def __enter__(self):
        self.saved = sys.stdout
        if enabled():
            sys.stdout = _Indented(self.saved, self.skip)
        return self

    def __exit__(self, *_):
        sys.stdout.flush()
        sys.stdout = self.saved


def step(number, total, name=None):
    text = f"Step {number} of {total}"
    return f"{name}{dot()}{text}" if name else text


def prompt(text):
    """The string for input(). The words stay exactly as given."""
    if not enabled():
        return text
    return f"\n{MARGIN}{paint('›', 'accent', 'bold')} {text}"


def ask(text):
    return input(prompt(text))


def option(key, label, hint=None, label_width=20):
    """One menu entry. Plain output keeps the historical "  1  Label" form."""
    if not enabled():
        return f"  {key}  {label}"
    entry = f" {paint(key, 'accent', 'bold')}  "
    if not hint:
        return entry + paint(label, "text")
    return f"{entry}{paint(pad(label, label_width), 'text')}{paint(hint, 'muted')}"


def options(entries, label_width=None, *, below=False):
    """Print menu entries given as (key, label[, hint]) tuples.

    With `below`, each hint goes on its own muted line under the label, for
    labels too long to share a line with their hint.
    """
    if not enabled():
        for key, label, *_ in entries:
            print(option(key, label))
        return
    if len(entries) >= 8 and len(entries) > room():
        return option_columns(entries)
    label_width = label_width or max(len(entry[1]) for entry in entries) + 3
    longest_hint = max((len(entry[2]) for entry in entries if len(entry) > 2 and entry[2]), default=0)
    if 4 + label_width + longest_hint > width():
        below = True
    for key, label, *rest in entries:
        hint = rest[0] if rest else None
        if below:
            line(option(key, label))
            if hint:
                for part in wrap(hint, width() - 5):
                    line("    " + paint(part, "muted"))
        else:
            line(option(key, label, hint, label_width))


def option_columns(entries):
    """A long menu in two columns, without hints, so it fits the screen."""
    half = (len(entries) + 1) // 2
    left, right = entries[:half], entries[half:]
    column = max(len(entry[1]) for entry in left) + 8
    for index, (key, label, *_) in enumerate(left):
        text = pad(option(key, label), column)
        if index < len(right):
            text += option(right[index][0], right[index][1]).lstrip(" ")
        line(text)


def card(rows, *, title=None, tone="muted", inner=None):
    """Rows inside a rounded box. Without the console UI, indented rows."""
    if not enabled():
        if title:
            print(f"  {title}")
        for row in rows:
            print(f"  {row}" if title else row)
        return
    inner = inner or width() - 4
    top = paint(f"╭─{'─' * inner}─╮", tone)
    if title:
        label = f" {title if ESCAPE.search(title) else paint(title, 'text')} "
        top = paint("╭──", tone) + label + paint("─" * max(0, inner - 1 - visible_len(label)) + "─╮", tone)
    with _lock:
        line(top)
        for row in rows:
            parts = [row]
            if visible_len(row) > inner:
                # A ticked or bulleted row continues under its text, not its icon.
                hang = "  " if plain(row)[1:2] == " " and not plain(row)[:1].isalnum() else ""
                first, *rest = wrap(row, inner)
                parts = [first, *wrap(" ".join(rest), inner, indent=hang)] if rest else [first]
            for part in parts:
                line(f"{paint('│', tone)} {pad(part, inner)} {paint('│', tone)}")
        line(paint(f"╰─{'─' * inner}─╯", tone))


def mark(tone, text):
    """A ticked, crossed or bulleted fact."""
    icon = ICONS.get(tone, ICONS["muted"])
    return f"{paint(icon[0], tone)} {text}" if enabled() else f"{icon[1]:>2} {text}"


def meter(level, top=2):
    """A small level gauge, such as 0..2 for a personality trait."""
    if not enabled():
        return f"{level}/{top}"
    return paint("●" * (level + 1), "accent") + paint("○" * (top - level), "muted")


def bar(fraction, length=None):
    fraction = min(1.0, max(0.0, fraction))
    length = length or width() - 6
    if not enabled():
        filled = int(round(fraction * length))
        return "[" + "#" * filled + "-" * (length - filled) + "]"
    eighths = int(fraction * length * 8)
    full, part = divmod(eighths, 8)
    body = "█" * full + (PARTIAL[part] if part and full < length else "")
    return paint(body, "accent") + paint("░" * (length - visible_len(body)), "muted")


class StageProgress:
    """A live list of stages with ticks and a bar, redrawn in place.

    `stages` is a sequence of (message, label, weight). `stage(message)` is
    the progress callback of an operation that reports those messages and
    `advance(done, total)` reports progress inside the current stage. Without
    the console UI the messages print unchanged, with a few percentages.
    """

    FRAMES = ("●", "●", "•", "•")

    def __init__(self, title, stages, *, context=None, subtitle=None, footer=None):
        self.title, self.context, self.subtitle, self.footer = title, context, subtitle, footer
        self.stages = list(stages)
        self.current = -1
        self.within = 0.0
        self.started = time.monotonic()
        self.reported = 0
        self.ticks = 0
        self.stop = threading.Event()
        self.ticker = None
        self.extra = []

    def __enter__(self):
        if enabled():
            cursor(False)
            self.draw(first=True)
            self.ticker = threading.Thread(target=self.tick, daemon=True)
            self.ticker.start()
        return self

    def __exit__(self, kind, *_):
        self.stop.set()
        if self.ticker is not None:
            self.ticker.join(timeout=2)
        if enabled():
            if kind is None:
                self.current, self.within = len(self.stages), 0.0
                self.draw()
            cursor(True)
        return False

    def tick(self):
        while not self.stop.wait(0.4):
            self.ticks += 1
            self.draw()

    def fraction(self):
        total = sum(weight for _, _, weight in self.stages) or 1
        done = sum(weight for _, _, weight in self.stages[:max(0, self.current)])
        if 0 <= self.current < len(self.stages):
            done += self.stages[self.current][2] * self.within
        return min(1.0, done / total)

    def stage(self, message):
        index = next((number for number, (known, _, _) in enumerate(self.stages) if known == message), None)
        if not enabled():
            print(message, flush=True)
        if index is None:
            if enabled():
                self.extra.append(message)
                self.draw()
            return
        self.current, self.within, self.reported = index, 0.0, 0
        if enabled():
            self.draw()

    def advance(self, done, total):
        if total <= 0:
            return
        self.within = min(1.0, done / total)
        if not enabled():
            quarter = int(self.within * 4)
            if quarter > self.reported and quarter < 4:
                self.reported = quarter
                print(f"  {quarter * 25}%", flush=True)
            return
        self.draw()

    def lines(self):
        rows = [header(self.title, self.context), rule()]
        if self.subtitle:
            rows.extend(paint(part, "muted") for part in wrap(self.subtitle))
        rows.append("")
        for index, (_, label, _) in enumerate(self.stages):
            if index < self.current:
                rows.append(f" {paint('✓', 'success')}  {paint(label, 'plain')}")
            elif index == self.current:
                frame = self.FRAMES[self.ticks % len(self.FRAMES)]
                rows.append(f" {paint(frame, 'accent', 'bold')}  {paint(label, 'text', 'bold')}")
            else:
                rows.append(f" {paint('○', 'muted')}  {paint(label, 'muted')}")
        rows.append("")
        percent = int(self.fraction() * 100)
        rows.append(f"{bar(self.fraction())} {paint(f'{percent:>3}%', 'text')}")
        elapsed = int(time.monotonic() - self.started)
        rows.append(paint(f"{elapsed // 60}:{elapsed % 60:02d} elapsed", "muted"))
        rows.append("")
        for message in self.extra[-3:]:
            rows.extend(paint(part, "plain") for part in wrap(message))
        if self.footer:
            rows.extend(paint(part, "muted") for part in wrap(self.footer))
        return rows

    def draw(self, first=False):
        body = "".join(f"{MARGIN if row else ''}{row}\x1b[K\n" for row in self.lines())
        with _lock:
            write(("\x1b[2J" if first else "") + "\x1b[H" + body)
