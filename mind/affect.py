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
        sleep"), or, as a whole clause, with SAFE_START ("how", "see", "who's");
        it never opens with a negation, a contrast, a condition, "and", "that",
        "it", "you", "now", "the", a third person or an imperative (TURN_WORDS),
        and never holds a negation, a contrast word or GAP_STOP ("now", "stop",
        "fix" ...): "thanks, that was not what I asked", "nice job, now the
        lights are off", "good job. you deleted my alarm", "if good morning",
        "dinner is late, thanks" and "I'm sad, I'm cutting onions" are all None.
    This is a recall cost on purpose: a long line, or one that says something else
    next to the cue, gets no reaction. A wrong reaction is worse than none.
    A joke marker never overrides worry or support: "my dog died, just kidding"
    is None, not a laugh. "thanks for ..." needs a whitelisted noun or gerund.
  * A positive reaction (warm, thanks, humour, surprise) is dropped when the
    same sentence turns on it with a contrast, a negation or an insult outside
    the matched words ("thanks, but that was wrong", "wow, you broke it").
    Sorrow or worry followed by thanks in the same sentence is irony: nothing.

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


def _tail_ok(mode, rest, nxt, is_last, addressed):
    if mode == "any":
        return True
    if mode == "thanks":
        k = 0
        while k < len(rest) and rest[k] in lex.THANKS_PAD:
            k += 1
        if k == len(rest):
            return True
        return rest[k] == "for" and _thanks_for_ok(rest[k + 1:])
    if mode == "tightlast":
        return is_last and len(rest) <= 3 and all(t in lex.TIGHT for t in rest)
    if mode == "tight":
        return len(rest) <= 3 and all(t in lex.TIGHT for t in rest)
    if mode == "greet":
        k = 0
        while k < len(rest) and k < 3 and rest[k] in lex.VOCATIVES:
            k += 1
        # once Pip is named, anything may follow ("hello Pip can you ...")
        # except an accusation or a command ("hello pip you are late")
        if k == len(rest):
            return True
        if addressed:
            return rest[k] not in lex.ACCUSE_START
        return rest[k] in lex.FOLLOW
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
            if not _tail_ok(rule.tail, rest, nxt, is_last, "pip" in cl[:end + 1]):
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


def _ironic_thanks_after(clauses, raw):
    """Worry or fear followed, in the same sentence, by thanks: irony, say nothing.

    "I'm anxious, thanks a lot" and "I'm anxious, thank you so much" are digs, not
    gratitude. Thanks BEFORE the worry ("thanks, I had a rough day") is fine.
    """
    first_worry = None
    for cue, ci, start, _ in raw:
        if cue.family in ("worry", "support") and (first_worry is None or (ci, start) < first_worry):
            first_worry = (ci, start)
    if first_worry is None:
        return False
    if any(cue.family == "thanks" and (ci, start) > first_worry for cue, ci, start, _ in raw):
        return True
    flat = [t for cl in clauses for t in cl]
    return any(tuple(flat[i:i + 3]) in lex.IRONIC_THANKS for i in range(len(flat) - 2))


def _sentence_is_cue_plus_filler(clauses, raw):
    """The short-utterance rule for one sentence.

    raw is [(cue, clause index, start, end)] for the cues that survived the vetoes.
    True when the sentence is empty after filler, or the cues cover most of it and
    nothing outside them turns it (see the module docstring):
      * nothing but filler may come before the first cue;
      * a stretch the cues do not cover opens with a preposition or a question word
        (lexicon GAP_START_OK), or, when it is a whole clause, with SAFE_START;
      * no such stretch opens with a TURN_WORDS word, and none holds a negation, a
        contrast word or one of GAP_STOP ("now", "stop", "fix", ...);
      * the cues span COVER of the sentence's tokens, or the sentence has at most
        SHORT_CORE tokens (filler not counted).
    """
    covered = set()
    for _, ci, start, end in raw:
        covered.update((ci, j) for j in range(start, end))
    core = cov = 0
    seen_cue = False
    for ci, cl in enumerate(clauses):
        in_gap = False                  # inside a stretch the cues do not cover
        clause_core = 0                 # tokens of this clause counted so far
        for j, tok in enumerate(cl):
            if (ci, j) in covered:
                core += 1
                clause_core += 1
                cov += 1
                in_gap = False
                seen_cue = True
                continue
            if tok in lex.STRIP:
                continue
            core += 1
            clause_core += 1
            if not seen_cue:
                return False            # something other than filler before the cue
            if tok in _CONTRAST or tok in lex.GAP_STOP or tok.endswith("n't"):
                return False
            if not in_gap:
                in_gap = True
                if tok in lex.TURN_WORDS:
                    return False
                allowed = lex.SAFE_START if clause_core == 1 else lex.GAP_START_OK
                if tok not in allowed:
                    return False
                if (tok, cl[j + 1] if j + 1 < len(cl) else "") in lex.GAP_BAD_PAIRS:
                    return False        # "what a joke" is an exclamation, not a question
    if core == 0:
        return True
    if not raw:
        return False
    return core <= SHORT_CORE or cov >= COVER * core


def read(text):
    """The person's words -> Cue or None. Deterministic; bounded work."""
    if not isinstance(text, str) or not text or len(text) > MAX_CHARS:
        return None
    groups, quoted, flat = _scan(text)
    if quoted or not flat or len(flat) > MAX_TOKENS or _sarcastic(flat):
        return None
    hits = []
    for clauses, term in groups:
        clauses = _merge_vocative(clauses)
        raw = []
        for ci, cl in enumerate(clauses):
            nxt = clauses[ci + 1][0] if ci + 1 < len(clauses) else ""
            for cue, start, end in _clause_hits(cl, ci == len(clauses) - 1, term, nxt):
                raw.append((cue, ci, start, end))
        if _ironic_thanks_after(clauses, raw):
            raw = []
        kept = [
            r for r in raw
            if not (r[0].family in lex.POSITIVE_FAMILIES
                    and r[0].rule not in lex.NO_CONTRAST_VETO
                    and _turns_on_it(clauses, r[1], r[2], r[3], r[0].rule))
            and not _accused(clauses, r[1], r[0].rule)
        ]
        if kept and any(t in lex.LAUGH_TOKENS for cl in clauses for t in cl):
            # sorrow and laughter in one sentence is a joke or irony: say nothing,
            # unless it is plainly laughter on its own
            if any(r[0].family in ("worry", "support") for r in kept):
                kept = []
        # every sentence must be the cue plus filler, or the whole read is None
        if not _sentence_is_cue_plus_filler(clauses, kept):
            return None
        hits.extend(r[0] for r in kept)
    if not hits:
        return None
    kidding = [h for h in hits if h.rule == "kidding"]
    if kidding:
        # a joke marker never overrides a worry or a support cue ("I'm sad, just
        # kidding" is a retraction, not a laugh): say nothing
        if any(h.family in ("worry", "support") for h in hits):
            return None
        hits = kidding
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
