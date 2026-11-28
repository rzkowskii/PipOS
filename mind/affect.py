"""What the person's words say about how they feel, for Pip's face.

read(text) looks at one accepted turn (hear.final text) and returns a
Cue(family, strength, rule) or None. It only ever reacts to a SHORT utterance
(at most 12 tokens) that is the cue plus filler; see the short-utterance rule
below. The family ids are the ten of the emotion director's table (crates/brainstem/data/emotion-director.json);
the brainstem maps family and strength to a pose and an intensity, so this
module never names a pose. emotion_fields() shapes a cue for the existing
mind.emotion topic as optional fields (no "name", so older receivers ignore it).

Design (docs/design/emotion-director.md, section 4):
  * Precision first. A short whitelist: greeting, goodnight, thanks, an apology
    aimed at Pip, plain statements of sadness, loss or worry, laughter and a
    joke marker, surprise, praise. No match is the usual and the right answer.
  * Additive and asymmetric. Text only ever adds a reaction to what was said;
    it does not rewrite a model tag, and nothing here reads anger or contempt.
  * Negation, questions, reported speech, quotes and sarcasm give None.
  * Social families only. Nothing maps a sense or a body state: "I see",
    "I hear", "I looked", "I feel hot" and "I'm tired" can never match.
  * Pure and deterministic: stdlib only, no regular expressions (so nothing
    can backtrack), no clock, no I/O, no state. Work is bounded by the first
    1,000 characters and the first 60 words, whatever the input length.
  * SHORT-UTTERANCE RULE (the structural one, third review round). read() reacts
    only when the WHOLE utterance is the cue plus filler. Filler and vocatives
    (lexicon STRIP: pip, please, so, very, really, just, oh, hey, um, well, okay,
    today ...) do not count. Then:
      - more than MAX_TOKENS (12) tokens, or more than MAX_CHARS characters: None;
      - every sentence must be empty after filler, or be covered by cue matches
        that span at least COVER (60 percent) of its tokens, or hold at most
        SHORT_CORE (6) tokens and contain a cue; a sentence with no cue is None
        for the whole read, so a second sentence can never ride on the first;
      - nothing but filler may come before the cue in a sentence;
      - what the cue does not cover opens with a preposition, a time word or a
        question opener (lexicon GAP_START_OK: "at work", "about it", "how did you
        sleep"), or, as a whole clause, is exactly one phrase of the CLOSED list
        SAFE_CLAUSES ("how are you", "what's up", "how about you"); it never opens
        with a negation, a contrast, a condition, "and", "that", "it", "you", "now",
        "the", a third person or an imperative (TURN_WORDS), and never holds a negation,
        a contrast word or GAP_STOP ("now", "stop", "fix" ...): "thanks, that was not
        what I asked", "nice job, now the
        lights are off", "good job. you deleted my alarm", "if good morning",
        "dinner is late, thanks" and "I'm sad, I'm cutting onions" are all None.
    This is a recall cost on purpose: a long line, or one that says something else
    next to the cue, gets no reaction. A wrong reaction is worse than none.
    "thanks for ..." needs a whitelisted noun or gerund.
  * A positive reaction (warm, thanks, humour, surprise) is dropped when the
    same sentence turns on it with a contrast, a negation or an insult outside
    the matched words ("thanks, but that was wrong", "wow, you broke it").
  * CLOSED TAILS (fourth review round). Loose continuations were replaced by closed
    lists, because each open list grew by one word per review:
      - after a greeting only vocatives or one phrase of GREET_FOLLOW ("how are you",
        "how's it going", "how did you sleep", "how was your day", "how are you
        doing", "what's up", "what's new", "how's everything", ...): "hello, what's
        your problem" and "hello pip can you set a timer" give nothing;
      - "crying" and "loss" (died, passed away, I lost my dog) take only a vocative or
        a time word after them (VTIME: "yesterday", "last week", "this morning"), so
        "crying at this video" and "died in minecraft" have nothing to stand on;
      - "sad", "unhappy" and "upset" followed by a preposition ("with the couch",
        "about the bill") are about a thing and give nothing (only "lonely" and the
        strong words may take one);
      - "oh my god" and "omg" react only with nothing after them, or "really"
        ("oh my god pip" gives NO reaction; the lower "wow" strength was the other
        option, and None was chosen: a wrong gasp is worse than none);
      - a hesitation (well, uh, um, hmm, meh) anywhere before thanks, in its sentence
        or an earlier one, drops the thanks ("well, thanks"). The whitelisted nouns
        and gerunds after "thanks for" stay ("thanks for coming", "thanks for trying");
      - after a thanks, a short closing from CLOSINGS ("that helped", "that worked",
        "that was helpful", optionally "a lot") is allowed, as a clause or at the end.
  * CONFLICT CHECKS run over the WHOLE read (every sentence), not one sentence at a time,
    because speech-to-text puts full stops everywhere (_conflict):
      - sorrow or worry (worry, support) with laughter or a joke anywhere: nothing;
      - a retraction ("just kidding", "jk") with any other cue, in either order: nothing
        (it never turns into a laugh; on its own it is still humour);
      - sorrow or worry with "thanks a lot" anywhere, or any thanks after it: a dig;
      - a positive cue (warm, thanks, surprise) followed by a LATER sentence that is
        laughter or a joke ("thank you pip. lol"): it may be mocking. A laugh BEFORE
        a positive ("haha. thank you pip") is fine.

Scan limits: this reader and the brainstem's clause reader are SEPARATE scans
of different text and do not share a limit. This one reads the person's whole
accepted turn (MAX_WORDS 60 words, MAX_CHARS 1,000 characters). The Rust table
(emotion-director.json "scan": 40 words, 400 bytes) bounds the lexicon it runs
over Pip's own reply clauses in the brainstem. tests/test_affect.py pins that
the two are documented here and that the heard-words limit is the larger. (The
short-utterance rule above answers None long before the 60-word scan ends; the
scan limit is only the ceiling on work for a line that is over the 12-token cap.)
Evidence: host tests in tests/test_affect.py; cost measured there (host).
"""

import math
import unicodedata
from typing import NamedTuple, Optional

from . import affect_lexicon as lex

FAMILIES = (
    "warm", "humour", "curious", "doubt", "worry",
    "surprise", "thanks", "apology", "support", "calm",
)

MAX_WORDS = 60
MAX_CHARS = 1000
MAX_TOKENS = 12     # an utterance longer than this (after "i am" -> "i'm") gives None
SHORT_CORE = 6      # a sentence this short, with a cue in it, needs no coverage share
COVER = 0.6         # otherwise the cue must span this share of the sentence's tokens
DEFAULT_HOLD = 1.2
HOLD_MIN, HOLD_MAX = 0.5, 2.5   # the wire range the brainstem accepts

# When one read finds several families, the more caring one wins; within a
# family the stronger. A statement of sorrow beats a greeting in the same turn.
PRIORITY = ("worry", "support", "thanks", "humour", "surprise", "warm")


class Cue(NamedTuple):
    family: str
    strength: float
    rule: str


# ---------------------------------------------------------------- compiling

_ANY = object()   # the "_" wildcard item


def _expand(spec):
    """One pattern string -> every concrete sequence of items it stands for."""
    seqs = [()]
    for el in spec.split():
        optional = el.endswith("?")
        if optional:
            el = el[:-1]
        if el.startswith("@"):
            alts = [(frozenset(lex.SETS[el[1:]]),)]
        elif el == "_":
            alts = [(_ANY,)]
        else:
            alts = [tuple(a.split("_")) for a in el.split("|")]
        if optional:
            alts = alts + [()]
        seqs = [s + a for s in seqs for a in alts]
    return seqs


class _Rule(NamedTuple):
    name: str
    family: str
    strength: float
    pos: str
    tail: str
    qok: object
    leads: frozenset
    not_next: frozenset
    boost: Optional[tuple]


def _compile():
    index = {}
    for (name, family, strength, pos, tail, qok, leads, not_next, boost, patterns) in lex.RULES:
        if family not in FAMILIES:
            raise ValueError("affect rule %r names unknown family %r" % (name, family))
        rule = _Rule(name, family, strength, pos, tail, qok, frozenset(leads),
                     frozenset(not_next), boost)
        for spec in patterns:
            for pat in _expand(spec):
                if not pat:
                    continue
                first = pat[0]
                keys = first if isinstance(first, frozenset) else (first,)
                for key in keys:
                    index.setdefault(key, []).append((rule, pat))
    return index


_INDEX = _compile()
_SARC_PHRASES = frozenset(lex.SARCASM_PHRASES)
_SARC_MAX = max(len(p) for p in lex.SARCASM_PHRASES)
_VETO = lex.NEGATION | lex.HYPOTHETICAL | lex.REPORTED
_CONTRAST = lex.CONTRAST

# ----------------------------------------------------------------- scanning

_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "′": "'", "`": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "–": " ", "—": " ", "−": " ", "…": ".",
})
_HARD = frozenset(".!?\n;\r")
_SOFT = frozenset(",:()[]{}/\\|")
_BIGRAMS = {
    ("i", "am"): "i'm", ("you", "are"): "you're", ("that", "is"): "that's",
    ("it", "is"): "it's",
}
_ONE = {"im": "i'm", "thats": "that's", "youre": "you're", "ive": "i've"}


def _normalise_tokens(words):
    """Fold "i am" to "i'm", "im" to "i'm", split "thankyou"."""
    out = []
    for w in words:
        if w == "thankyou":
            out.extend(("thank", "you"))
        else:
            out.append(_ONE.get(w, w))
    folded = []
    i = 0
    while i < len(out):
        merged = _BIGRAMS.get((out[i], out[i + 1])) if i + 1 < len(out) else None
        if merged:
            folded.append(merged)
            i += 2
        else:
            folded.append(out[i])
            i += 1
    return folded


def _scan(text):
    """text -> (groups, quoted, flat).

    A group is one sentence: (clauses, terminators). A clause is a token list
    (one comma-separated piece). flat is every token in order. Scanning stops
    at MAX_WORDS words or MAX_CHARS characters, whichever comes first.
    """
    text = unicodedata.normalize("NFKC", text[:MAX_CHARS]).lower().translate(_CHAR_MAP)
    groups, clauses, words, word, term = [], [], [], [], set()
    quoted = False
    count = 0
    n = len(text)
    i = 0
    while i < n and count < MAX_WORDS:
        c = text[i]
        if c.isalnum():
            if len(word) < 40:      # a "word" this long is never a rule's word
                word.append(c)
        elif c == "'" and word and i + 1 < n and text[i + 1].isalnum():
            word.append(c)
        elif c == "." and word and word[-1].isdigit() and i + 1 < n and text[i + 1].isdigit():
            word.append(c)
        else:
            if word:
                words.append("".join(word))
                word = []
                count += 1
            if c == '"':
                quoted = True
            elif c in _HARD or c in _SOFT:
                if words:
                    clauses.append(_normalise_tokens(words))
                    words = []
                if c in _HARD:
                    # a run like "?!" or "..." is one boundary
                    while i + 1 < n and text[i + 1] in ".!?":
                        i += 1
                        term.add(text[i])
                    term.add(c)
                    if clauses:
                        groups.append((clauses, frozenset(term)))
                    clauses, term = [], set()
        i += 1
    if word:
        words.append("".join(word))
    if words:
        clauses.append(_normalise_tokens(words))
    if clauses:
        groups.append((clauses, frozenset(term)))
    flat = [t for g in groups for cl in g[0] for t in cl]
    return groups, quoted, flat


def _merge_vocative(clauses):
    """"sorry, pip" and "pip, I'm sad" are one clause, not two.

    Only a clause that is exactly "pip" is a vocative. A clause that merely
    starts with the name ("sorry, pip is the name of my cat") is a new clause.
    """
    out = []
    for cl in clauses:
        if out and (cl == ["pip"] or out[-1] == ["pip"]):
            out[-1] = out[-1] + cl
        else:
            out.append(cl)
    return out


def _starts_with_question(cl):
    for tok in cl[:4]:
        if tok in lex.QWORDS:
            return True
        if tok not in lex.QLEADS:
            return False
    return False


# ----------------------------------------------------------------- matching

def _hit(item, tok):
    if isinstance(item, str):
        return item == tok
    if item is _ANY:
        return tok not in lex.WILD_STOP and tok not in _VETO
    return tok in item


def _match(pat, toks, i):
    """Match pat at toks[i]; returns (end, skipped intensifiers) or (-1, ())."""
    n = len(toks)
    j = i
    skipped = []
    for k, item in enumerate(pat):
        if k:
            s = 0
            while j < n and s < 2 and toks[j] in lex.INTENS and not _hit(item, toks[j]):
                skipped.append(toks[j])
                j += 1
                s += 1
        if j >= n or not _hit(item, toks[j]):
            return -1, ()
        j += 1
    return j, skipped


def _vetoed(cl, i, window=3):
    for tok in cl[max(0, i - window):i]:
        if tok in _VETO or tok.endswith("n't"):
            return True
    return False


def _thanks_for_ok(after):
    """What may follow "thanks for": a whitelisted noun or gerund, then a few small words."""
    j = 0
    while j < len(after) and after[j] in lex.THANKS_DET:
        j += 1
    if j >= len(after) or after[j] not in lex.THANKS_FOR_OK:
        return False
    tail = after[j + 1:]
    return len(tail) <= 3 and all(t in lex.THANKS_FOR_TAIL for t in tail)


def _phrase_then_tail(tokens, phrases):
    """True when tokens are exactly one phrase of the CLOSED set, then only vocatives or
    time words (lexicon PHRASE_TAIL). Bounded: a phrase is at most five tokens."""
    for ln in range(1, min(len(tokens), 5) + 1):
        if tuple(tokens[:ln]) in phrases and all(t in lex.PHRASE_TAIL for t in tokens[ln:]):
            return True
    return False


def _safe_clause(cl, closings):
    """A whole clause with no cue in it: only filler, or one phrase of the closed list."""
    k = 0
    while k < len(cl) and cl[k] in lex.STRIP:
        k += 1
    if k == len(cl):
        return True
    phrases = lex.SAFE_CLAUSES | lex.CLOSINGS if closings else lex.SAFE_CLAUSES
    return _phrase_then_tail(cl[k:], phrases)


def _tail_ok(mode, rest, nxt, is_last):
    if mode == "any":
        return True
    if mode == "thanks":
        k = 0
        while k < len(rest) and rest[k] in lex.THANKS_PAD:
            k += 1
        if k == len(rest):
            return True
        if _phrase_then_tail(rest[k:], lex.CLOSINGS):
            return True         # "thanks that helped"
        return rest[k] == "for" and _thanks_for_ok(rest[k + 1:])
    if mode == "vtime":
        return len(rest) <= 3 and all(t in lex.VTIME for t in rest)
    if mode == "gasp":
        return not rest or rest == ["really"]
    if mode == "tightlast":
        return is_last and len(rest) <= 3 and all(t in lex.TIGHT for t in rest)
    if mode == "tight":
        return len(rest) <= 3 and all(t in lex.TIGHT for t in rest)
    if mode == "greet":
        k = 0
        while k < len(rest) and k < 3 and rest[k] in lex.VOCATIVES:
            k += 1
        # nothing may follow but vocatives, or one phrase of the CLOSED list
        # ("how are you", "what's up"): "hello pip can you ...", "hello, what's your
        # problem" and "hi pip you are late" all give nothing
        if k == len(rest):
            return True
        return _phrase_then_tail(rest[k:], lex.GREET_FOLLOW)
    if mode == "noway":
        # "no way, really?" / "no way that's true": astonishment. A bare "no way!"
        # (a refusal as often as a gasp) and "no way, put that down!" are not.
        if not rest:
            return nxt in lex.NOWAY_NEXT
        return rest[0] in lex.NOWAY_NEXT
    return False


def _clause_hits(cl, is_last, term, nxt=""):
    question_end = is_last and "?" in term
    q_start = _starts_with_question(cl)
    hits = []
    n = len(cl)
    for i in range(n):
        for rule, pat in _INDEX.get(cl[i], ()):
            if rule.pos == "start" and i > 3:
                continue
            end, skipped = _match(pat, cl, i)
            if end < 0:
                continue
            if "a" in skipped and "little" not in skipped and "bit" not in skipped:
                continue        # "I'm a sad clown": an article, not a softener
            rest = cl[end:]
            start_ok = i <= 3 and all(t in rule.leads for t in cl[:i])
            if rule.pos == "start":
                ok = start_ok
            elif rule.pos == "start_or_end":
                ok = start_ok or (len(rest) <= 2 and all(t in lex.ENDTAIL for t in rest)
                                  and not _vetoed(cl, i, window=n))
            else:
                ok = not _vetoed(cl, i)
            if not ok:
                continue
            if rest and rest[0] in rule.not_next:
                continue
            reject = lex.REJECT_ANY.get(rule.name)
            if reject and any(t in reject for t in cl):
                continue
            if not _tail_ok(rule.tail, rest, nxt, is_last):
                continue
            if q_start and rule.qok is not True:
                continue
            if question_end and not (rule.qok is True or (rule.qok == "rest" and rest)):
                continue
            strength = rule.strength
            span = cl[i:end]
            if any(t in lex.BOOST for t in span):
                strength += 0.15
            if any(t in lex.SOFT for t in span):
                strength -= 0.15
            if rule.boost and any(t in rule.boost[0] for t in cl):
                strength += rule.boost[1]
            cue = Cue(rule.family, min(1.0, max(0.05, round(strength, 2))), rule.name)
            hits.append((cue, i, end))
    return hits


def _sarcastic(flat):
    n = len(flat)
    for i, tok in enumerate(flat):
        if tok in lex.SARCASM_WORDS:
            return True
        for ln in range(2, _SARC_MAX + 1):
            if i + ln > n:
                break
            if tuple(flat[i:i + ln]) in _SARC_PHRASES:
                return True
    return False


def _turns_on_it(clauses, ci, start, end, rule):
    """True when a contrast, negation, insult or condition sits outside the match."""
    greeting = rule in lex.GREETING_ONLY
    for k, cl in enumerate(clauses):
        for j, tok in enumerate(cl):
            if k == ci and start <= j < end:
                continue
            if tok in _CONTRAST or tok.endswith("n't"):
                return True
            if not greeting and tok in lex.CONDITION:
                return True
    return False


def _accused(clauses, ci, rule):
    """A worry, or a greeting, sitting next to an accusation aimed at Pip."""
    for k, cl in enumerate(clauses):
        if k == ci or not cl:
            continue
        if rule in lex.GREETING_RULES and k > ci and cl[0] in lex.ACCUSE_START:
            return True
        if cl[0] in lex.YOU_START and any(t in _CONTRAST for t in cl):
            return True
    return False


def _grudging_thanks(clauses, ci, start, shrugged):
    """A hesitation or a shrug before thanks ("well, thanks", "uh, thank you", "hmm thanks"),
    in this sentence or an earlier one ("hmm. thanks."). shrugged: an earlier sentence had one."""
    if shrugged:
        return True
    for k in range(ci + 1):
        for j, tok in enumerate(clauses[k]):
            if k == ci and j >= start:
                break
            if tok in lex.HESITATION:
                return True
    return False


def _sentence_is_cue_plus_filler(clauses, raw):
    """The short-utterance rule for one sentence.

    raw is [(cue, clause index, start, end)] for the cues that survived the vetoes.
    True when the sentence is empty after filler, or the cues cover most of it and
    nothing outside them turns it (see the module docstring):
      * nothing but filler may come before the first cue;
      * a stretch the cues do not cover opens with a preposition or a question word
        (lexicon GAP_START_OK); a whole clause the cues do not touch must be exactly one
        phrase of the CLOSED list SAFE_CLAUSES ("how about you"), or, after a thanks, a
        closing from CLOSINGS ("that helped");
      * no such stretch opens with a TURN_WORDS word, and none holds a negation, a
        contrast word or one of GAP_STOP ("now", "stop", "fix", ...);
      * the cues span COVER of the sentence's tokens, or the sentence has at most
        SHORT_CORE tokens (filler not counted).
    """
    covered = set()
    for _, ci, start, end in raw:
        covered.update((ci, j) for j in range(start, end))
    thanked = any(r[0].family == "thanks" for r in raw)
    greeted = any(r[0].rule in lex.GREETING_RULES for r in raw)
    core = cov = 0
    seen_cue = False
    for ci, cl in enumerate(clauses):
        if not any((ci, j) in covered for j in range(len(cl))):
            # a clause the cues do not touch: filler, or one phrase of the closed list
            rest_core = sum(1 for t in cl if t not in lex.STRIP)
            if rest_core == 0:
                continue
            if not seen_cue:
                return False            # something other than filler before the cue
            if any(t in _CONTRAST or t in lex.GAP_STOP or t.endswith("n't") for t in cl):
                return False
            if not _safe_clause(cl, thanked):
                return False
            core += rest_core
            continue
        in_gap = False                  # inside a stretch the cues do not cover
        for j, tok in enumerate(cl):
            if (ci, j) in covered:
                core += 1
                cov += 1
                in_gap = False
                seen_cue = True
                continue
            if tok in lex.STRIP:
                continue
            core += 1
            if not seen_cue:
                return False            # something other than filler before the cue
            if tok in _CONTRAST or tok in lex.GAP_STOP or tok.endswith("n't"):
                return False
            if not in_gap:
                in_gap = True
                if thanked and tok == "that" and _phrase_then_tail(cl[j:], lex.CLOSINGS):
                    core += sum(1 for t in cl[j + 1:] if t not in lex.STRIP)
                    break               # "thanks that helped": a closed closing, then done
                if tok in lex.TURN_WORDS:
                    return False
                if tok not in lex.GAP_START_OK and not (
                        greeted and _phrase_then_tail(cl[j:], lex.GREET_FOLLOW)):
                    return False
                if (tok, cl[j + 1] if j + 1 < len(cl) else "") in lex.GAP_BAD_PAIRS:
                    return False        # "what a joke" is an exclamation, not a question
    if core == 0:
        return True
    if not raw:
        return False
    return core <= SHORT_CORE or cov >= COVER * core


def _conflict(sentences, flat):
    """The conflict checks, over the WHOLE read (every sentence), not one sentence at a time.

    sentences is [(kept hits [(cue, clause index, start, end)], sentence has laughter)].
    True when the read contradicts itself, so it gives nothing:
      * sorrow or worry (worry, support) with any laughter anywhere: a joke or irony,
        whichever sentence the laugh is in ("my dog died. haha.");
      * a retraction ("just kidding", "jk") with any other cue, in either order;
      * sorrow or worry with a "thanks a lot" anywhere, or any thanks after it: a dig;
      * a positive cue (warm, thanks, surprise) followed by a LATER sentence that is
        laughter ("thank you pip. lol"): it may be mocking.
    A laugh BEFORE a positive ("haha. thank you pip") is fine.
    """
    hits = [(si, h) for si, (kept, _) in enumerate(sentences) for h in kept]
    sorrow = [(si, h[1], h[2]) for si, h in hits if h[0].family in ("worry", "support")]
    if sorrow:
        if any(t in lex.LAUGH_TOKENS for t in flat) or any(h[0].family == "humour" for _, h in hits):
            return True
        first = min(sorrow)
        if any(h[0].family == "thanks" and (si, h[1], h[2]) > first for si, h in hits):
            return True
        if any(tuple(flat[i:i + 3]) in lex.IRONIC_THANKS for i in range(len(flat) - 2)):
            return True
    if any(h[0].rule == "kidding" for _, h in hits) and any(h[0].family != "humour" for _, h in hits):
        return True
    for si, h in hits:
        if h[0].family in ("warm", "thanks", "surprise") and any(
                laugh for _, laugh in sentences[si + 1:]):
            return True
    return False


def read(text):
    """The person's words -> Cue or None. Deterministic; bounded work."""
    if not isinstance(text, str) or not text or len(text) > MAX_CHARS:
        return None
    groups, quoted, flat = _scan(text)
    if quoted or not flat or len(flat) > MAX_TOKENS or _sarcastic(flat):
        return None
    sentences = []
    shrugged = False
    for clauses, term in groups:
        clauses = _merge_vocative(clauses)
        raw = []
        for ci, cl in enumerate(clauses):
            nxt = clauses[ci + 1][0] if ci + 1 < len(clauses) else ""
            for cue, start, end in _clause_hits(cl, ci == len(clauses) - 1, term, nxt):
                raw.append((cue, ci, start, end))
        kept = [
            r for r in raw
            if not (r[0].family in lex.POSITIVE_FAMILIES
                    and r[0].rule not in lex.NO_CONTRAST_VETO
                    and _turns_on_it(clauses, r[1], r[2], r[3], r[0].rule))
            and not _accused(clauses, r[1], r[0].rule)
            and not (r[0].family == "thanks" and _grudging_thanks(clauses, r[1], r[2], shrugged))
        ]
        # every sentence must be the cue plus filler, or the whole read is None
        if not _sentence_is_cue_plus_filler(clauses, kept):
            return None
        laugh = (any(r[0].family == "humour" for r in kept)
                 or any(t in lex.LAUGH_TOKENS for cl in clauses for t in cl))
        sentences.append((kept, laugh))
        shrugged = shrugged or any(t in lex.HESITATION for cl in clauses for t in cl)
    if _conflict(sentences, flat):
        return None
    hits = [r[0] for kept, _ in sentences for r in kept]
    if not hits:
        return None
    kidding = [h for h in hits if h.rule == "kidding"]
    if kidding:
        hits = kidding              # only humour is left here (see _conflict)
    best = min(hits, key=lambda h: (PRIORITY.index(h.family), -h.strength))
    return best


def emotion_fields(cue, hold=DEFAULT_HOLD):
    """The optional fields for a mind.emotion event: {family, strength, hold}.

    No "name" key: the brainstem maps family and strength through its ladder.
    Returns {} when there is no valid cue, so a caller can splat it safely.
    """
    if not isinstance(cue, Cue) or cue.family not in FAMILIES:
        return {}
    try:
        strength = float(cue.strength)
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(strength):
        return {}
    try:
        hold = float(hold)
    except (TypeError, ValueError):
        hold = DEFAULT_HOLD
    if not math.isfinite(hold):
        hold = DEFAULT_HOLD
    return {
        "family": cue.family,
        "strength": round(min(1.0, max(0.0, strength)), 2),
        "hold": round(min(HOLD_MAX, max(HOLD_MIN, hold)), 2),
    }
