"""Bounded carry-forward from source utterances in the conversation archive."""

import re

from . import commands
from .memory import search_terms

HIGHLIGHT_LIMIT = 3
CANDIDATE_LIMIT = 8
_FILLER = re.compile(r"^(?:hi|hello|hey|thanks|thank you|okay|ok|sure)\b", re.IGNORECASE)
_QUESTION = re.compile(r"^(?:how|when|where|what|who|which|why|can|could|would|did|do|does|is|are)\b", re.IGNORECASE)


def candidate_highlights(memory, episode_ids, *, owner, limit=CANDIDATE_LIMIT):
    """Return original owner statements, with explicit memories first.

    Score only the working set supplied by the caller. An omitted statement
    stays in SQLite and is still available to ordinary history search.
    """
    candidates = []
    seen = set()
    for source in memory.consolidation_sources(episode_ids, owner):
        text = " ".join(source.text.split())
        key = text.casefold().rstrip(".!")
        if key in seen or len(search_terms(text)) < 2 or text.endswith("?"):
            continue
        command = commands.parse(text)
        if command is not None:
            if command.kind != "remember":
                continue
        elif _QUESTION.match(text) or _FILLER.match(text):
            continue
        seen.add(key)
        candidates.append((command is not None, source))
    candidates.sort(key=lambda entry: (entry[0], entry[1].id), reverse=True)
    return tuple(source for _, source in candidates[:limit])


def select_highlights(candidates, *, limit=HIGHLIGHT_LIMIT):
    """Cheap baseline using the candidate priority order."""
    return tuple(source.id for source in candidates[:limit])


def validate_selection(candidates, ids, *, limit=HIGHLIGHT_LIMIT):
    """Validate advisory output. None denotes a malformed or foreign pick."""
    if not isinstance(ids, (list, tuple)) or len(ids) > limit:
        return None
    allowed = {source.id for source in candidates}
    if any(type(source_id) is not int or source_id not in allowed for source_id in ids):
        return None
    if len(set(ids)) != len(ids):
        return None
    return tuple(ids)


def resolve_highlights(memory, ids, *, owner):
    """Render from canonical rows in selection order so edits are respected."""
    sources = {source.id: source for source in memory.consolidation_sources(ids, owner)}
    return tuple(sources[source_id] for source_id in ids if source_id in sources)
