"""A one-word brightness step that Moonshine tiny misheard, read back by its sound.

Moonshine tiny is weak on a single word said alone. On the real laptop
(28 Nov 2026, synthetic speech replayed through the diagnostic mic, Moonshine
given the word on its own) "dimmer" came out right 0 of 6 times ("The map",
"The map", "The ma", "Dima", "The more", "The more"; live once "Demo") and
"brighter" 1 of 6 ("Righter" four times, "Breiter"). The PocketSphinx command
grammar does not rescue them: replayed on the host through PocketSphinx with
"brighter" and "dimmer" in the grammar, "dimmer" alone beat the phone loop by
1.3 per frame and the one-breath "brighter" windows by 1.1 and 3.7, while
non-commands in the same recordings scored up to 18.6, so no margin tells them
apart.

So a transcript of one or two words that is no command (courtesy words such as
"please" aside) is compared with the two brightness words by a rough sound key:
the spelling's consonants in order, voicing kept, "th" read as "d" (Moonshine
writes a soft "d" as "the"), the first vowel kept as front, open or back, later
vowels folded together and an "-er", "-a" or "-o" ending taken as the same
ending. "Righter" is taken for "brighter" only when it is spelled "righ...".
Real words that share a key and that the owner may well say on their own
("theme", "dime", "retry", "righto") are never rewritten.

A wrong rewrite only steps the screen brightness once, which the owner can undo
with one word; longer speech is never touched.
"""

import re

# What each brightness word may be heard as, by key (see sound_key).
TARGETS = {
    "brighter": ("brItR",),
    "dimmer": ("dImR",),
}
# "Righter" without its "b": only words spelled with this onset.
DROPPED_ONSETS = {"brighter": ("rItR", "righ")}
# Words with a target's key that mean something of their own.
NEVER = frozenset((
    "theme", "themes", "thyme", "dime", "dimes", "demi", "demure", "demur", "deemer", "dimer",
    "righto", "righty", "right-o", "righty-o", "brite", "brita", "brito", "retry", "retire", "retiree", "retro", "rite",
    "rita",
))
# Said around a command without changing it (recognition.COURTESY_WORDS less
# "thank" and "you", which make a reply of their own).
COURTESY = frozenset(("hey", "hi", "ok", "okay", "pip", "please", "now"))
MAX_WORDS = 2


def words(text):
    """Lowercase words of a transcript, apostrophes and hyphens kept, other punctuation dropped."""
    return re.findall(r"[a-z]+(?:['-][a-z]+)?", text.lower()) if isinstance(text, str) else []


def sound_key(text):
    """A rough key for how `text` sounds (see the module notes); '' for no letters."""
    s = re.sub(r"[^a-z]", "", text.lower()) if isinstance(text, str) else ""
    for spelled, said in (("th", "d"), ("gh", ""), ("ph", "f"), ("ck", "k")):
        s = s.replace(spelled, said)
    s = re.sub(r"(.)\1+", r"\1", s)
    found = re.match(r"([^aeiouy]*)([aeiouy]+)(.*)$", s)
    if not found:
        return s
    onset, vowel, rest = found.groups()
    vowel = "I" if re.search(r"[iey]", vowel) else "A" if "a" in vowel else "O"
    rest = re.sub(r"[aeiouy]+", "V", rest)
    return re.sub(r"(?:Vr|rV|VrV|V|r)$", "R", onset + vowel + rest)


def device_word(text):
    """"brighter" or "dimmer" when `text` is one or two words that sound like it, else None.

    Only for a transcript that is no command already: the caller checks that
    first, so "brighter" itself never comes here."""
    heard = [word for word in words(text) if word not in COURTESY]
    if not heard or len(heard) > MAX_WORDS:
        return None
    joined = " ".join(heard)
    if joined in NEVER or any(word in NEVER for word in heard):
        return None
    key = sound_key("".join(heard))
    for command, keys in TARGETS.items():
        if key in keys:
            return command
        dropped = DROPPED_ONSETS.get(command)
        if dropped and key == dropped[0] and joined.startswith(dropped[1]):
            return command
    return None
