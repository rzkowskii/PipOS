"""Turning a streamed model reply into things to say: strip any thinking, read
the emotion tag, cut the text into clauses the voice can start on early."""

import re

EMOTIONS = ("neutral", "joy", "curiosity", "surprise", "skepticism", "concern", "sleepy")
SENTENCE_CHARS = 160


# A few explicit admissions can change the tone within a reply. The
# leading model tag still supplies the general tone. Require an unquoted
# statement from Pip, rather than matching words anywhere in the text.
_CLAUSE_CONCERN = re.compile(
    r"^(?:i (?:couldn't|could not|can't|cannot) (?:find|open|finish|save|remember)\b"
    r"|one(?: of them)? (?:is|was) missing[.!?]?$)", re.I,
)


def clause_cue(text, emotion):
    if _CLAUSE_CONCERN.search(text.strip().replace("’", "'")):
        return "concern"
    return emotion if emotion in EMOTIONS else "neutral"


def reply_grammar(emotions=EMOTIONS, sentences=None):
    choices = "emotion ::= " + " | ".join(f'"{emotion}"' for emotion in emotions) + "\n"
    if sentences is None:
        return ('root ::= "[" emotion "] " [A-Za-z0-9"\'] text\n'
                + choices + "text ::= [^\\x00]*\n")
    tail = ' (" " sentence)?' if sentences == 2 else ""
    return ('root ::= "[" emotion "] " sentence' + tail + "\n" + choices
            + f"sentence ::= [A-Za-z0-9\"'] part{{0,{SENTENCE_CHARS}}} [.!?] [\"']?\n"
            + 'part ::= [^\\x00\\n\\r()\\[\\]{}<>*_#`~|.!?] | "." [0-9]\n')

# llama.cpp grammar: one emotion tag, then words. The first character after the
# tag must start a word, because a small model left free will give the tag and
# stop, leaving nothing to say.
GRAMMAR = reply_grammar()

# The tighter reply shape (task 4.6): the tag, then one or two sentences of
# bounded length that end in . ! or ?, with no new lines, brackets or markup.
# A full stop inside a sentence is allowed only before a digit ("3.5").
# The default since the audition of 24 November 2026 (config/being.json):
# it never lowered the complete-reply rate, and it ended long answers at a
# sentence where the open shape was cut off mid-word.
STRICT_GRAMMAR = reply_grammar(sentences=2)

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


# A small model sometimes describes what it supposedly sees or hears ("I see a
# quiet blue lamp."), when nothing in its notes says it can: it has no eyes or
# ears of its own and only code tells it what the camera or microphone found.
# Only a claim with something seen or heard counts; "I see." and "I hear you."
# are ordinary talk, and a refusal ("I can't see the sea") is honest.
_PERCEPTION = re.compile(
    r"\bI(?:'m| am)?\s+(?:(?:can|could|just|now)\s+)?(?:see|seeing|hear|hearing|smell|smelling|watch|watching)"
    r"\s+(?:a|an|some|something|the|those|these|this)\b"
    r"(?!\s+(?:point|difference|idea|problem|reason|answer|pattern|connection|logic|appeal|issue)\b)",
    re.I,
)
_REFUSAL = re.compile(r"\b(?:can't|cannot|can not|couldn't|don't|do not|not|never|unable to)\b", re.I)


_INTERJECTION = {"well", "oh", "sure", "okay", "ok", "hi", "hello", "hey", "of", "course", "certainly", "great",
                 "good", "question", "hmm", "um", "uh", "yes", "right", "alright", "so", "now", "ah"}
_OPENINGS = tuple(tuple(opening.split()) for opening in (
    "as an ai", "as a language model", "i am an ai", "i am an artificial intelligence", "i am just an ai",
    "i am a language model", "i am just a language model", "i am a large language model",
    "i am a program", "i am just a program", "i am a computer program", "how can i help you today",
    "how may i assist you today", "how can i assist you", "i am here to help", "i am here to assist",
    "i do not have personal feelings", "i do not have feelings", "i do not have a body",
    "i can see a", "i can hear a", "i see a", "i hear a", "i am seeing", "i am hearing", "i smell a",
))


def reply_open(text):
    """True while a reply's opening could still become boilerplate or a claim to see
    or hear something, so a short first clause is held until the next words say
    which: only interjections so far ("Of course,"), or the first words of such an opening."""
    words = re.findall(r"[a-z']+", text.replace("\u2019", "'").casefold())
    words = [w.replace("i'm", "i am").replace("don't", "do not") for w in words]
    words = " ".join(words).split()
    if not words:
        return False
    while words and words[0] in _INTERJECTION:
        words.pop(0)
        if not words:
            return True
    if not words:
        return True
    return any(opening[:len(words)] == tuple(words) and len(words) < len(opening) for opening in _OPENINGS)


def perception_claim(text):
    """True when a reply says it sees, hears or smells something. A sentence that
    also says it cannot ("I can't see the sea, but I hear the waves") is split
    at the comma: the claim after the refusal still counts."""
    for sentence in re.split(r"(?<=[.!?])\s+", text.replace("\u2019", "'")):
        for part in re.split(r",\s*(?:but|and|yet)?\s*", sentence):
            match = _PERCEPTION.search(part)
            if match and not _REFUSAL.search(part[:match.start()]):
                return True
    return False


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

    def __init__(self, first_min=10, long_run=90, short_first=False, first_words=3):
        self.first_min = first_min
        self.long_run = long_run
        # A short first clause (WP-D6): cut at the first mark the moment it
        # arrives (no waiting for the space after it), or after `first_words`
        # words with no mark. Later clauses are cut as before.
        self.short_first = short_first
        self.first_words = first_words
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

    _FIRST_MARK = re.compile(r"[.!?…,;:—–]")

    def _find_short_first(self):
        buf = self._buf
        for match in self._FIRST_MARK.finditer(buf):
            i = match.start()
            head = buf[:i + 1]
            if not re.search(r"[A-Za-z0-9]", head):
                continue
            prev = buf[i - 1] if i else ""
            nxt = buf[i + 1] if i + 1 < len(buf) else None
            if buf[i] in ",." and prev.isdigit() and (nxt is None or nxt.isdigit()):
                if nxt is None:
                    return None     # "3." may become "3.5": wait for the next character
                continue
            if buf[i] == "." and (_ABBREVIATION.search(head) or (nxt is not None and not nxt.isspace()
                                                                 and nxt not in "\"')]")):
                continue
            end = i + 1
            while end < len(buf) and buf[end] in "\"')]" and buf[i] in ".!?…":
                end += 1
            return end
        words = list(re.finditer(r"\S+\s", buf))
        if len(words) >= self.first_words:
            return words[self.first_words - 1].end()
        return None

    def _find_cut(self):
        if self._first and self.short_first:
            cut = self._find_short_first()
            if cut is not None:
                return cut
            return None
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
