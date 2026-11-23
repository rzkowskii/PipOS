"""Turning a streamed model reply into things to say: strip any thinking, read
the emotion tag, cut the text into clauses the voice can start on early."""

import re

EMOTIONS = ("neutral", "joy", "curiosity", "surprise", "skepticism", "concern", "sleepy")

# llama.cpp grammar: one emotion tag, then words. The first character after the
# tag must start a word, because a small model left free will give the tag and
# stop, leaving nothing to say.
GRAMMAR = (
    'root ::= "[" emotion "] " [A-Za-z0-9"\'] text\n'
    "emotion ::= " + " | ".join(f'"{e}"' for e in EMOTIONS) + "\n"
    "text ::= [^\\x00]*\n"
)

# The tighter reply shape (task 4.6): the tag, then one or two sentences of
# bounded length that end in . ! or ?, with no new lines, brackets or markup.
# A full stop inside a sentence is allowed only before a digit ("3.5").
# The default since the audition of 24 November 2026 (config/being.json):
# it never lowered the complete-reply rate, and it ended long answers at a
# sentence where the open shape was cut off mid-word.
SENTENCE_CHARS = 160
STRICT_GRAMMAR = (
    'root ::= "[" emotion "] " sentence (" " sentence)?\n'
    "emotion ::= " + " | ".join(f'"{e}"' for e in EMOTIONS) + "\n"
    f"sentence ::= [A-Za-z0-9\"'] part{{0,{SENTENCE_CHARS}}} [.!?] [\"']?\n"
    "part ::= [^\\x00\\n\\r()\\[\\]{}<>*_#`~|.!?] | \".\" [0-9]\n"
)

# Assistant boilerplate a small model falls back on. A reply that opens with
# it is not said and is asked for again, like a parrot. Only the opening
# counts: "Kites are fun, and I'm here to help you fly one" is its own voice.
_BOILERPLATE = re.compile(
    r"(?:(?:well|oh|sure|okay|ok|hi|hello|hey|of course|certainly|great|good question)[,.!: ]+)*"
    r"(?P<phrase>as an ai|as a language model|i(?:'| a)m (?:just )?an ai|i(?:'| a)m (?:just )?a (?:large )?language model"
    r"|i am an artificial intelligence|how (?:can|may) i (?:help|assist)(?: you)?(?: today)?"
    r"|i(?:'m| am) here to (?:help|assist)|i don't have (?:personal )?(?:feelings|emotions|a body)"
    r"|i(?:'m| am) (?:just )?a (?:computer )?program)\b",
    re.I,
)


def _plain_words(text):
    return " ".join(re.findall(r"[a-z']+", text.replace("’", "'").casefold()))


def boilerplate(text, heard=""):
    """True for a reply that opens with assistant boilerplate. Words the owner
    used first are not boilerplate: "How can I help?" answers "how can I help
    a sad friend?" in their own words."""
    match = _BOILERPLATE.match(text.replace("’", "'").lstrip(" \"'"))
    if match is None:
        return False
    phrase = _plain_words(match["phrase"])
    return not (heard and f" {phrase} " in f" {_plain_words(heard)} ")


class ThinkFilter:
    """Drops <think>...</think> from a stream, even when tags arrive in pieces."""

    _BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)

    def __init__(self):
        self._raw = ""
        self._sent = 0

    def feed(self, delta):
        self._raw += delta
        visible = self._BLOCK.sub("", self._raw)
        # Hold back a trailing "<..." that may yet turn into a tag.
        cut = visible.rfind("<")
        if cut != -1 and "<think>".startswith(visible[cut:]):
            visible = visible[:cut]
        if len(visible) <= self._sent:
            return ""
        out = visible[self._sent:]
        self._sent = len(visible)
        return out

    def flush(self):
        visible = self._BLOCK.sub("", self._raw)
        out = visible[self._sent:]
        self._sent = len(visible)
        return out


class EchoGuard:
    """Stops a reply at the point where the model starts reciting its notes.

    The model sees its senses and memories in parentheses. A new line that
    opens with a parenthesis ends the reply. Inline, only a known note does.
    """

    _LABELS = ("(private note:", "(you remember:", "(past conversation:")

    def __init__(self, senses=""):
        self.stopped = False
        self._held = ""
        self._senses_note = f"({senses})" if senses else ""

    def feed(self, text):
        """Returns the text that is safe to pass on."""
        if self.stopped:
            return ""
        text = self._held + text
        self._held = ""
        newline = re.search(r"\n\s*(\(|\Z)", text)
        end = newline.start() if newline else len(text)
        for opening in re.finditer(r"\(", text[:end]):
            start = opening.start()
            tail = text[start:]
            label = tail.lower()
            if label.startswith(self._LABELS) or (self._senses_note and tail.startswith(self._senses_note)):
                self.stopped = True
                return text[:start]
            if self._senses_note.startswith(tail) or any(marker.startswith(label) for marker in self._LABELS):
                self._held = tail
                return text[:start]
        if newline:
            self.stopped = newline[1] == "("
            if not self.stopped:
                self._held = text[end:]
        return text[:end]

    def flush(self):
        held, self._held = self._held, ""
        return "" if self.stopped else held


class TagParser:
    """Reads the leading [emotion] tag off a streamed reply."""

    def __init__(self):
        self.emotion = None
        self._buf = ""
        self._done = False

    def feed(self, delta):
        """Returns the text to speak, which is empty until the tag is settled."""
        if self._done:
            return delta
        self._buf += delta
        text = self._buf.lstrip()
        if not text:
            return ""
        if text[0] != "[":
            return self._finish(text)
        end = text.find("]")
        if end == -1:
            # Too long to be a tag: it was an ordinary bracket.
            return self._finish(text) if len(text) > 24 else ""
        tag = text[1:end].strip().lower()
        if tag in EMOTIONS:
            self.emotion = tag
        # A tag it invented is dropped, not spoken.
        return self._finish(text[end + 1:].lstrip())

    def _finish(self, text):
        self._done = True
        self._buf = ""
        return text

    def flush(self):
        if self._done:
            return ""
        text = self._buf.strip()
        self._done = True
        self._buf = ""
        return "" if text.startswith("[") else text


_STRAY_TAG = re.compile(r"\[[a-zA-Z]{3,12}\]")
_MARKUP = re.compile(r"[*_#`~|<>]+")
_SPACE = re.compile(r"\s+")
_ABBREVIATION = re.compile(r"(?:^|\s)(?:mr|mrs|ms|dr|st|vs|etc|e\.g|i\.e|[a-z])\.$", re.I)
_BOUNDARY = re.compile(r"([.!?…]+[\"')\]]*|[,;:—–]|\n)\s+")


def clean(text):
    """Remove what a voice should not read out."""
    text = _STRAY_TAG.sub(" ", text)
    text = _MARKUP.sub("", text)
    # Emoji and other symbols beyond the Basic Multilingual Plane.
    text = "".join(ch for ch in text if ord(ch) < 0x10000 and not 0x2600 <= ord(ch) <= 0x27BF)
    return _SPACE.sub(" ", text).strip()


class ClauseSplitter:
    """Cuts streamed text into clauses.

    The first clause is cut early, at a comma if need be, so speech can start
    while the rest is still being generated. After that it cuts at sentence
    ends, or at a comma once a sentence runs long.
    """

    def __init__(self, first_min=10, long_run=90):
        self.first_min = first_min
        self.long_run = long_run
        self._buf = ""
        self._first = True

    def feed(self, text):
        self._buf += text
        out = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            clause = clean(self._buf[:cut])
            self._buf = self._buf[cut:]
            if clause:
                out.append(clause)
                self._first = False
        return out

    def _find_cut(self):
        for match in _BOUNDARY.finditer(self._buf):
            mark = match.group(1)
            head = self._buf[:match.start(1) + len(mark)]
            if mark[0] in ".!?…\n":
                if mark[0] == "." and _ABBREVIATION.search(head):
                    continue
                return match.end()
            if self._first and len(head.strip()) >= self.first_min:
                return match.end()
            if len(head) >= self.long_run:
                return match.end()
        return None

    def flush(self):
        rest = clean(self._buf)
        self._buf = ""
        return rest
