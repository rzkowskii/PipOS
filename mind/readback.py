"""Read-back and repeat: "what did you hear?" and "say that again".

"What did you hear?" and "what did I just say?" read back the last final
transcript of this wake, word for word, and only if there was one: Pip never
says "I heard" about words it did not hear. "Say that again", "repeat that",
"pardon?", "sorry?", "what did you say?" repeat Pip's last line.

Both come from Context (RAM only, cleared on sleep, lid close and a guest
change); nothing here is stored. "What did you say about X" is a memory
question, which commands.parse answers before this module is asked; the
patterns here are anchored so they never take it.
"""

import re

from .reply import Reply, clean

_HEARD = re.compile(
    r"^(?:what did you (?:just )?hear(?: me say)?|what did i (?:just )?say|what was i saying|what have i just said"
    r"|what did you hear me say|what did you think i said|what do you think i said|what did you catch"
    r"|did you hear (?:me|what i said)|what were my (?:last )?words|tell me what you heard"
    r"|read (?:that|it) back(?: to me)?|read back what i said)$", re.I)
_REPEAT = re.compile(
    r"^(?:(?:can you |could you |would you )?(?:say|repeat) (?:that|it|what you said|yourself)(?: again| one more time"
    r"| please)*|repeat(?: please)?|again please|come again|pardon(?: me)?|i beg your pardon|sorry|excuse me"
    r"|what did you (?:just )?say|what was that|what(?: was that)? again|you said what"
    r"|i didn't (?:catch|hear) (?:that|you|what you said)|i missed that|one more time)$", re.I)
_TAG = re.compile(r"^\s*\[[a-z]+\]\s*", re.I)


def may_match(text):
    if len(text or "") > 120:      # every phrase here is a few words
        return False
    said = clean(text).rstrip("?!").strip()
    return bool(_HEARD.match(said) or _REPEAT.match(said))


def _same(a, b):
    fold = lambda s: " ".join(re.sub(r"[^\w' ]", " ", (s or "").casefold()).split())
    return fold(a) == fold(b)


async def handle(text, ctx):
    if len(text or "") > 120:
        return None
    said = clean(text, getattr(ctx, "name", "pip")).rstrip("?!").strip()
    if _HEARD.match(said):
        heard = (ctx.last_heard or "").strip()
        if not heard or _same(heard, text):
            return Reply("[neutral] I haven't caught anything else from you since I woke up.")
        return Reply(f"[neutral] I heard: '{heard}'.")
    if _REPEAT.match(said):
        line = _TAG.sub("", ctx.last_said or "").strip()
        if not line:
            return Reply("[neutral] I haven't said anything yet since I woke up.")
        return Reply(f"[neutral] {line}")
    return None
