"""Deterministic checks on what the dream model says it found.

The dream model reads a chunk of the day's conversation and proposes items:
a short claim, the exact words it rests on, and which line they came from.
Nothing it proposes is believed until code has checked it against the stored
conversation:

- the line id must be one of the lines it was shown;
- the quote, with case, quotes and spacing folded, must be part of that line;
- the speaker it names must be who really said that line;
- Pip's own lines are never facts about the owner;
- the claim is at most 160 characters;
- every name, number and date in the claim must be in the quote, as whole
  words ("Tom" is not in "tomorrow"), and so must every word saying who
  someone is to the owner ("sister") or which time is meant ("next");
- most of the claim's words of substance must be in the quote, and a "not"
  on one side needs a "not" on the other;
- only things stated (or reported) as true are facts: not jokes, guesses or
  "what if"s.

An item that fails any check is rejected. It is never repaired: a nearly
right item is a wrong item.
"""

import re
import unicodedata
from dataclasses import dataclass

KINDS = ("fact", "preference", "plan", "event", "relation")
SPEAKERS = ("owner", "being", "other")
MODALITIES = ("stated", "reported", "hypothetical", "joking", "unsure")
ASSERTED = ("stated", "reported")
FIELDS = frozenset({"kind", "claim", "quote", "episode_id", "speaker", "modality", "time_ref"})
MAX_CLAIM = 160
MAX_QUOTE = 200
MAX_TIME_REF = 40
MIN_QUOTE_CHARS = 8
EPISODE_ID = re.compile(r"E([1-9][0-9]{0,11})")

_MONTHS = ("january february march april may june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec").split()
_DAYS = ("monday tuesday wednesday thursday friday saturday sunday "
         "mon tue tues wed thu thur thurs fri sat sun today tomorrow yesterday tonight").split()
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "half": 0.5, "dozen": 12,
}
# Words a claim may use about the people in it without having to quote them.
_GENERIC = frozenset(
    """owner owner's user user's person people someone pip being they he she him her his hers
    likes like liked loves love loved prefers prefer wants want needs need has have had having
    is are was were will be been called named name names said says say mentioned told tell
    plans plan planning going goes go went thinks think thought feels feel felt
    their them my me i you your we our us it its the a an and or of to in on at for with""".split()
)
# Who someone is to the owner, and which occurrence of a time: a claim may not
# turn "my sister" into "my brother", or "in June" into "next June". Like
# names, these must be in the quote itself.
_RELATIONS = frozenset(
    """mother mum mom mummy mommy father dad daddy parent sister brother sibling son daughter child children
    kid baby wife husband partner girlfriend boyfriend fiance fiancee friend mate uncle aunt auntie cousin niece
    nephew grandmother grandma gran granny nan nana grandfather grandpa grandad granddad grandson granddaughter
    grandchild stepmother stepfather stepson stepdaughter boss colleague neighbour neighbor landlord landlady
    flatmate housemate roommate ex""".split()
)
_TIME_QUALIFIERS = frozenset("next last every ago before after early late".split())
_NEGATION = re.compile(r"\b(?:not|no|never|nothing|none|nobody|neither|nor|cannot|without)\b|n't\b")
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")


@dataclass(frozen=True)
class Line:
    """One stored line the model was shown."""

    episode_id: int
    speaker: str  # "owner", "being" or "other"
    text: str


@dataclass(frozen=True)
class Checked:
    """An item that passed every check. `quote` is the span as it appears in
    the stored line, so it can be read back as the owner's own words."""

    kind: str
    claim: str
    quote: str
    episode_id: int
    speaker: str
    modality: str
    time_ref: str


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    item: Checked | None = None


def fold(text):
    """Case, accents, quote marks, dashes and spacing folded."""
    text = unicodedata.normalize("NFKC", text)
    text = (text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("—", "-"))
    return " ".join(text.casefold().split())


def _strip_edges(text):
    return text.strip(" \t\n\"'.,;:!?…-“”‘’")


def _find_span(line_text, quote):
    """Where `quote` sits in the stored line, by folded comparison, as the
    line's own characters. None if it is not there."""
    target = _strip_edges(fold(quote))
    if not target:
        return None
    # Map each character of the folded line back to the original.
    folded, origin = [], []
    for index, char in enumerate(line_text):
        piece = fold(char) if not char.isspace() else " "
        for out in piece:
            if out == " " and folded and folded[-1] == " ":
                continue
            folded.append(out)
            origin.append(index)
    flat = "".join(folded)
    # fold() also trims; trim the mapped copy the same way.
    start_trim = len(flat) - len(flat.lstrip(" "))
    flat = flat.strip(" ")
    origin = origin[start_trim:start_trim + len(flat)]
    at = flat.find(target)
    while at != -1:
        before = flat[at - 1] if at > 0 else " "
        after = flat[at + len(target)] if at + len(target) < len(flat) else " "
        # Whole words only: "cat" is not a quote from "concatenate".
        if not (before.isalnum() and target[0].isalnum()) and not (after.isalnum() and target[-1].isalnum()):
            return line_text[origin[at]:origin[at + len(target) - 1] + 1]
        at = flat.find(target, at + 1)
    return None


def _numbers(text):
    found = set()
    for match in _NUMBER.findall(text):
        found.add(match.replace(",", ""))
    for word in _WORD.findall(text.casefold()):
        value = _NUMBER_WORDS.get(word.rstrip("s") if word.endswith("ths") else word)
        if value is not None:
            found.add(str(value))
    for match in re.findall(r"(\d+)(?:st|nd|rd|th)\b", text.casefold()):
        found.add(match)
    return found


def _stem(word):
    word = word.casefold().replace("’", "'")
    if word.endswith("'s"):
        word = word[:-2]
    for suffix in ("ing", "ies", "ied", "ed", "es", "s", "ly"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _names(claim):
    """Capitalised words, months and days: what a claim must not invent."""
    names = set()
    words = _WORD.findall(claim)
    for index, word in enumerate(words):
        low = word.casefold().replace("’", "'")
        bare = low[:-2] if low.endswith("'s") else low
        if bare in _MONTHS or bare in _DAYS:
            names.add(bare)
        elif word[:1].isupper() and bare not in _GENERIC and not (index == 0 and bare in _COMMON_STARTS):
            names.add(bare)
    return names


_COMMON_STARTS = frozenset("the a an this that these those my his her their our its there here".split())


def _has_word(text, word):
    return re.search(rf"(?<![^\W_]){re.escape(word)}(?![^\W_])", text) is not None


def claim_signature(claim):
    """What makes two checked claims the same claim: their words of
    substance, stemmed and sorted, so "The owner's dog is called Biscuit" and
    "Biscuit is the name of the owner's dog" agree. A negated claim never
    agrees with an unnegated one."""
    folded = fold(claim)
    words = sorted({_signature_stem(word) for word in _WORD.findall(folded)
                    if word not in _SIGNATURE_SKIP and not _NEGATION.fullmatch(word)} - {""})
    if not words:
        return " ".join(_strip_edges(folded).split())
    return ("not " if _NEGATION.search(folded) else "") + " ".join(words)


# Only words that carry no meaning of their own are left out of a signature:
# "wants a dog" and "has a dog" are different claims.
_SIGNATURE_SKIP = frozenset(
    """owner owner's user user's person pip being the a an and or of to in on at for with by from is are was
    were be been am being it its it's they them their he she him her his hers my me i i'm you your we our us
    this that these those there here called named name names""".split()
)
_IRREGULAR = {"has": "have", "had": "have", "having": "have", "does": "do", "did": "do", "doing": "do",
              "goes": "go", "went": "go", "going": "go", "gone": "go"}


def _signature_stem(word):
    word = word[:-2] if word.endswith("'s") else word
    if word.endswith("n't"):  # the negation itself is kept apart
        word = {"ca": "can", "wo": "will", "sha": "shall"}.get(word[:-3], word[:-3])
        if word in _SIGNATURE_SKIP:
            return ""
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    word = _stem(word)
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def _content(text):
    return {_stem(word) for word in _WORD.findall(text) if word.casefold() not in _GENERIC and len(word) > 1}


def check(item, lines):
    """Check one proposed item against the lines of its chunk.

    `lines` maps episode id to Line. Returns a Verdict; only `ok` verdicts
    carry an item, and the item's fields are the model's own, unchanged,
    except that the quote is replaced by the stored words it matched.
    """
    if not isinstance(item, dict):
        return Verdict(False, "not_an_object")
    if set(item) != FIELDS:
        return Verdict(False, "wrong_fields")
    kind, claim, quote = item["kind"], item["claim"], item["quote"]
    episode, speaker, modality, time_ref = item["episode_id"], item["speaker"], item["modality"], item["time_ref"]
    if not all(isinstance(value, str) for value in (kind, claim, quote, episode, speaker, modality, time_ref)):
        return Verdict(False, "wrong_types")
    if kind not in KINDS:
        return Verdict(False, "unknown_kind")
    if speaker not in SPEAKERS:
        return Verdict(False, "unknown_speaker")
    if modality not in MODALITIES:
        return Verdict(False, "unknown_modality")
    if modality not in ASSERTED:
        return Verdict(False, "not_asserted")
    if not claim.strip() or len(claim) > MAX_CLAIM:
        return Verdict(False, "claim_length")
    if any(not char.isprintable() for char in claim + quote + time_ref):
        return Verdict(False, "control_characters")
    if len(quote) > MAX_QUOTE or len(_strip_edges(fold(quote))) < MIN_QUOTE_CHARS:
        return Verdict(False, "quote_length")
    if len(time_ref) > MAX_TIME_REF:
        return Verdict(False, "time_ref_length")
    match = EPISODE_ID.fullmatch(episode)
    if match is None:
        return Verdict(False, "bad_episode_id")
    episode_id = int(match.group(1))
    line = lines.get(episode_id)
    if line is None:
        return Verdict(False, "episode_not_in_chunk")
    if line.speaker == "being" or speaker == "being":
        return Verdict(False, "being_line")
    if speaker != line.speaker:
        return Verdict(False, "speaker_mismatch")
    span = _find_span(line.text, quote)
    if span is None:
        return Verdict(False, "quote_not_in_episode")
    claim_numbers, quote_numbers = _numbers(claim), _numbers(span)
    if not claim_numbers <= quote_numbers:
        return Verdict(False, "number_not_in_quote")
    folded_quote = fold(span)
    quote_words = {_stem(word) for word in _WORD.findall(folded_quote)}
    for name in _names(claim):
        # Whole words only: "Tom" is not in "tomorrow", nor "Ann" in "Annabel".
        if _stem(name) not in quote_words and not _has_word(folded_quote, name):
            return Verdict(False, "name_not_in_quote")
    if bool(_NEGATION.search(fold(claim))) != bool(_NEGATION.search(folded_quote)):
        return Verdict(False, "negation_mismatch")
    content = _content(claim)
    if content:
        supported = sum(1 for word in content if word in quote_words
                        or any(other.startswith(word[:5]) for other in quote_words if len(word) >= 5))
        if supported * 2 < len(content):
            return Verdict(False, "claim_unsupported")
    for word in _WORD.findall(fold(claim)):
        bare = word[:-2] if word.endswith("'s") else word
        if (bare in _RELATIONS or _stem(bare) in _RELATIONS) and _stem(bare) not in quote_words:
            return Verdict(False, "relation_not_in_quote")
        if bare in _TIME_QUALIFIERS and bare not in quote_words:
            return Verdict(False, "time_not_in_quote")
    return Verdict(True, "ok", Checked(kind, claim, span, episode_id, speaker, modality, time_ref))


def check_all(items, lines):
    """(accepted items, rejection reasons): never more than the schema's 8 items are read."""
    accepted, rejected = [], []
    if not isinstance(items, list):
        return [], ["not_a_list"]
    for item in items[:8]:
        verdict = check(item, lines)
        if verdict.ok:
            accepted.append(verdict.item)
        else:
            rejected.append(verdict.reason)
    if len(items) > 8:
        rejected.extend(["too_many_items"] * (len(items) - 8))
    return accepted, rejected
