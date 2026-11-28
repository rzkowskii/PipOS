"""Factory-off grammar and length audition. Inputs are current body readings."""

from dataclasses import dataclass
from functools import lru_cache

from .speech import EMOTIONS, reply_grammar

MAX_AGE = 15.0


@dataclass(frozen=True)
class Shape:
    grammar: str
    cap: int
    sentences: int | None
    sleepy: bool


@lru_cache(maxsize=6)
def _grammar(sleepy, sentences):
    allowed = EMOTIONS if sleepy else tuple(tag for tag in EMOTIONS if tag != "sleepy")
    return reply_grammar(allowed, sentences=sentences)


def choose(drives, body, *, age, drowsy, hour, cap, strict):
    """The pilot keeps temperature and the prompt untouched."""
    if not 0 <= age <= MAX_AGE:
        return None
    tiredness = drives.get("tiredness")
    if type(tiredness) not in (int, float) or not 0 <= tiredness <= 0.26:
        return None
    temp = body.get("temp")
    hot = type(temp) in (int, float) and 75 <= temp <= 150
    strain = body.get("reflex_strain_pct")
    strained = type(strain) in (int, float) and 5 <= strain <= 100
    night = type(hour) is int and (22 <= hour <= 23 or 0 <= hour < 7)
    short = hot or strained or tiredness > 0 or night or drowsy
    sentences = 1 if short else 2 if strict else None
    if short:
        cap = min(cap, 32 if hot else 40)
    sleepy = drowsy or night
    return Shape(_grammar(sleepy, sentences), cap, sentences, sleepy)
