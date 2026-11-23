"""Jokes, fun facts, spelling and small games, all in code.

Jokes and facts come from mind/play_data.py. The ids of the last 100 of each
kind are kept in /mind/play.json, so nothing repeats within the last 100
(and a guest's requests are kept in RAM only, so guest mode writes nothing).
Games are guess the number, rock paper scissors and twenty questions. Their
state is in RAM only and ends after ten quiet minutes, on "stop the game" or
"I give up", or when a different speaker or a guest starts talking.
"""

import asyncio
import itertools
import json
import random
import re
import threading
import time
import zlib
from pathlib import Path

from . import play_data
from .reply import Reply, clean, is_guest, keeps_nothing
from .storage import atomic_write

RECENT = 100
GAME_IDLE = 600.0
ANOTHER_WITHIN = 120.0
NUMBER_TOP = 100
MAX_QUESTIONS = 20

_random = random.SystemRandom()
_GAME = {}      # the one game in progress (RAM only)
_LAST = {}      # the last joke or fact told, for "another one"
_GUEST_RECENT = {"joke": [], "fact": []}


def item_id(text):
    return f"{zlib.crc32(text.encode('utf-8')):08x}"


def _path(state_dir):
    return Path(state_dir) / "play.json"


_CACHE = {}     # state_dir -> what this process last wrote (the file may still be on its way)


def load_recent(state_dir):
    cached = _CACHE.get(str(state_dir))
    if cached is not None:
        return {kind: list(ids) for kind, ids in cached.items()}
    try:
        data = json.loads(_path(state_dir).read_text(encoding="utf-8"))
        recent = data.get("recent", {})
        return {kind: [str(i) for i in recent.get(kind, [])][-RECENT:] for kind in ("joke", "fact")}
    except (OSError, ValueError, AttributeError):
        return {"joke": [], "fact": []}


_SAVE_LOCK = threading.Lock()
_SAVED = {}     # state_dir -> the newest generation written, so an older write never lands last
_GENERATION = itertools.count(1)
_ERASED = {}    # state_dir -> the generation at the last erase; older writes are dropped


def save_recent(state_dir, recent, generation=None):
    path = _path(state_dir)
    key = str(state_dir)
    with _SAVE_LOCK:
        if generation is not None and (generation <= _SAVED.get(key, 0) or generation <= _ERASED.get(key, 0)):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"version": 1, "recent": recent}, sort_keys=True) + "\n")
        if generation is not None:
            _SAVED[key] = generation


def _save_quietly(state_dir, recent, generation):
    try:
        save_recent(state_dir, recent, generation)
    except OSError:
        pass


def erase_all(state_dir):
    """Forget which jokes and facts were told ("forget everything")."""
    with _SAVE_LOCK:
        _ERASED[str(state_dir)] = next(_GENERATION)
        _CACHE.pop(str(state_dir), None)
        _path(state_dir).unlink(missing_ok=True)
    _GAME.clear()
    _LAST.clear()
    for kind in _GUEST_RECENT:
        _GUEST_RECENT[kind].clear()


def pick(kind, state_dir, *, guest=False, rng=None, background=False):
    """A joke or fact not among the last 100 of its kind; the least recent if all were.

    A guest's picks, and everything while memory is session-only, are kept
    in RAM only. With background=True the file is written in a worker
    thread, so a slow disk never holds up the line being said."""
    rng = rng or _random
    bank = play_data.JOKES if kind == "joke" else play_data.FACTS
    ram_only = guest or keeps_nothing(state_dir)
    recent = load_recent(state_dir)
    seen = recent[kind] + (_GUEST_RECENT[kind] if ram_only else [])
    seen = seen[-RECENT:]
    fresh = [text for text in bank if item_id(text) not in set(seen)]
    if fresh:
        text = rng.choice(fresh)
    else:
        order = {identifier: index for index, identifier in enumerate(seen)}
        text = min(bank, key=lambda t: order.get(item_id(t), -1))
    identifier = item_id(text)
    if ram_only:
        _GUEST_RECENT[kind] = [i for i in _GUEST_RECENT[kind] if i != identifier] + [identifier]
        _GUEST_RECENT[kind] = _GUEST_RECENT[kind][-RECENT:]
        return text
    recent[kind] = [i for i in recent[kind] if i != identifier] + [identifier]
    recent[kind] = recent[kind][-RECENT:]
    generation = next(_GENERATION)
    _CACHE[str(state_dir)] = {k: list(v) for k, v in recent.items()}
    if background:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, _save_quietly, state_dir, recent, generation)
            return text
    _save_quietly(state_dir, recent, generation)
    return text


# -- phrases ------------------------------------------------------------------

_JOKE = re.compile(r"^(?:tell me|say|give me|do you know|know|got|have you got|i want|i'd like|tell us|share)?"
                   r"(?: (?:a|another|one more|any|some|your best))? ?(?:funny )?jokes?(?: (?:please|for me|again))?$"
                   r"|^make me (?:laugh|smile)$|^say something funny$|^(?:tell me )?something funny$"
                   r"|^another joke$|^do you know any (?:good )?jokes$", re.I)
_FACT = re.compile(r"^(?:tell me|say|give me|do you know|know|got|have you got|i want|i'd like|tell us|share)?"
                   r"(?: (?:a|another|one more|any|some))? ?(?:fun |interesting |random |cool )?facts?"
                   r"(?: (?:please|for me|again))?$"
                   r"|^tell me something (?:interesting|i don't know|new|cool)$|^(?:another|one more) fact$"
                   r"|^teach me something$", re.I)
_ANOTHER = re.compile(r"^(?:another|another one|one more|more|tell me another(?: one)?|do another(?: one)?)$", re.I)
_MENU = re.compile(r"^(?:let's|lets|shall we|can we|could we|do you want to|want to|i want to|let us) play"
                   r"(?: a| another)? (?:game|something)(?: with me)?$|^play a game(?: with me)?$"
                   r"|^what games (?:can|do) you (?:play|know)$|^(?:i'm|i am) bored$", re.I)
_START = [
    ("number", re.compile(r"^(?:(?:let's|lets|can we|shall we|i want to) )?(?:play )?guess(?:ing)? (?:the|my|a|your) number"
                          r"(?: game)?$|^(?:play )?the number game$|^(?:play )?number guessing(?: game)?$", re.I)),
    ("rps", re.compile(r"^(?:(?:let's|lets|can we|shall we|i want to) )?(?:play )?rock[, ]+paper[, ]+(?:and )?scissors"
                       r"(?: game)?$", re.I)),
    ("questions", re.compile(r"^(?:(?:let's|lets|can we|shall we|i want to) )?(?:play )?(?:twenty|20) questions"
                             r"(?: game)?$|^guess (?:my|the|an|what) animal$|^guess what animal i'm thinking of$", re.I)),
]
_STOP = re.compile(r"^(?:stop|end|quit|finish|leave|cancel)(?: the| this| our)? game$|^(?:i give up|give up|i quit"
                   r"|stop playing|let's stop(?: playing)?|no more games?|i don't want to play(?: any more| anymore)?"
                   r"|that's enough|enough|stop|game over|i'm done(?: playing)?)$", re.I)
_WORD = r"(?:the word )?[\"']?(?P<word>[a-z'-]+(?: [a-z'-]+){0,2}?)[\"']?"
_SPELL = re.compile(r"^(?:how (?:do|would|should) (?:you|i|we) spell|can you spell|could you spell"
                    r"|please spell|spell|what's the spelling of|what is the spelling of|how to spell)"
                    rf" {_WORD}(?: spelt| spelled| spelt out| spelled out)?(?: for me)?$", re.I)
# "How is X spelled": the ending is needed, or "how is your day" would be spelled out.
_SPELL_HOW_IS = re.compile(rf"^(?:how is|how's) {_WORD} (?:spelt|spelled|spelt out|spelled out)(?: for me)?$", re.I)
_NOT_A_WORD = {"it", "that", "this", "them", "those", "these", "something", "anything", "words", "word"}
_YES = re.compile(r"^(?:yes|yeah|yep|yup|yes it is|yes it does|yes it can|yes they do|it is|it does|it can|correct"
                  r"|right|sure|i think so|probably|mostly|usually|definitely|of course)$", re.I)
_NO = re.compile(r"^(?:no|nope|nah|no it isn't|no it's not|no it doesn't|no it can't|it isn't|it's not|it doesn't"
                 r"|it can't|not really|i don't think so|probably not|never|definitely not|wrong)$", re.I)
_UNSURE = re.compile(r"^(?:maybe|sometimes|i don't know|don't know|dunno|not sure|i'm not sure|sort of|kind of"
                     r"|a bit|it depends|both|pass)$", re.I)
_HAND = re.compile(r"^(?:i (?:choose|pick|go|play|say|throw)|i'll (?:go|pick|choose|play|have)|my turn[, ]*)?"
                   r" ?(?P<hand>rock|paper|scissors|stone)$", re.I)
_GUESS = re.compile(r"^(?:is it |it's |i guess |i think it's |my guess is |how about |what about |maybe |i'll say |try )?"
                    r"(?P<n>[\w -]+?)(?: then)?$", re.I)
# The gate is also how the mind tells a follow-up meant for it from the room,
# so it names the phrases below rather than any word near them ("that was
# funny", "I want to play outside" are not for it).
_GATE = re.compile(r"\b(?:jokes?|facts?|games?|guess(?:ing)?|scissors|(?:twenty|20) questions"
                   r"|spell|spelling|spelt|spelled|bored|make me (?:laugh|smile)|something (?:funny|interesting"
                   r"|i don't know|new|cool)|teach me something|(?:let's|lets|let us) play"
                   r"|play (?:a|another|with me|something|guess|rock|twenty|20|the number|number))\b", re.I)
_MORE_GATE = re.compile(r"\b(?:another|one more|more)\b", re.I)


def _is_move(said):
    """Could this be a move in the game that is on? Cheap, and no state changes."""
    kind = _GAME.get("kind")
    if _STOP.match(said):
        return True
    if kind == "rps":
        return bool(_HAND.match(said))
    if kind == "questions":
        return bool(_YES.match(said) or _NO.match(said) or _UNSURE.match(said))
    if kind == "number":
        match = _GUESS.match(said)
        if not match:
            return False
        from .calc import parse_number
        words = match["n"].casefold().split()
        parsed = parse_number(words) if words else None
        # A partial "twenty" of "twenty three" is a move too.
        return parsed is not None and parsed[1] == len(words)
    return False


def may_match(text):
    text = (text or "")[:400]
    # While a game is on, a move ("forty", "rock", "no") is for it.
    if _GAME and time.monotonic() - _GAME.get("at", 0) < GAME_IDLE:
        said = clean(text).rstrip("?!").replace(",", "").strip()
        if len(said.split()) <= 6 and _is_move(said):
            return True
    if _LAST and time.monotonic() - _LAST.get("at", 0) < ANOTHER_WITHIN and _MORE_GATE.search(text):
        return True
    return bool(_GATE.search(text))


# -- spelling -----------------------------------------------------------------

def spell(word):
    """'necessary' -> 'N, E, C, E, S, S, A, R, Y'; an apostrophe is said."""
    letters = []
    for char in word:
        if char.isalpha():
            letters.append(char.upper())
        elif char == "'":
            letters.append("apostrophe")
        elif char == "-":
            letters.append("hyphen")
    return ", ".join(letters)


def _spelling(word):
    words = word.casefold().split()
    if any(len(w.strip("'-")) < 1 or len(w) > 24 for w in words) or " ".join(words) in _NOT_A_WORD:
        return None
    if len(words) == 1:
        return f"[neutral] {words[0].capitalize()} is spelled {spell(words[0])}."
    parts = "; then ".join(spell(w) for w in words)
    return f"[neutral] That's {_say(len(words))} words: {parts}."


# -- games ----------------------------------------------------------------------

def _owner(ctx):
    return "guest" if is_guest(ctx) else (ctx.speaker or "unknown")


def _start(kind, ctx, rng):
    _GAME.clear()
    _GAME.update(kind=kind, speaker=_owner(ctx), at=time.monotonic())
    if kind == "number":
        _GAME.update(secret=rng.randint(1, NUMBER_TOP), guesses=0)
        return Reply("[joy] I'm thinking of a number between one and a hundred. Have a guess.")
    if kind == "rps":
        _GAME.update(you=0, me=0)
        return Reply("[joy] Rock, paper, scissors! Say rock, paper or scissors.")
    _GAME.update(asked=[], answers={}, wrong=set(), questions=0)
    first = _next_question()
    return Reply(f"[joy] Think of an animal, and I'll try to guess it with yes or no questions. "
                 f"{first}")


def _end(line):
    _GAME.clear()
    return Reply(line)


def _say(number):
    from .calc import say_number
    return say_number(number)


def _number_turn(said, rng):
    match = _GUESS.match(said)
    if not match:
        return None
    from .calc import parse_number
    words = match["n"].casefold().split()
    parsed = parse_number(words)
    if parsed is None or parsed[1] != len(words) or parsed[0].denominator != 1:
        return None
    guess = int(parsed[0])
    if not 1 <= guess <= NUMBER_TOP:
        return Reply("[neutral] It's between one and a hundred.")
    _GAME["guesses"] += 1
    secret, tries = _GAME["secret"], _GAME["guesses"]
    if guess == secret:
        times = "first go" if tries == 1 else f"{_say(tries)} guesses"
        return _end(f"[joy] Yes! It was {_say(secret)}. You got it in {times}.")
    direction = "higher" if secret > guess else "lower"
    close = abs(secret - guess) <= 3
    return Reply(f"[neutral] {'Very close, but ' if close else ''}{direction if close else direction.capitalize()}.")


_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
_HOW = {"rock": "rock blunts scissors", "paper": "paper wraps rock", "scissors": "scissors cut paper"}


def _rps_turn(said, rng):
    match = _HAND.match(said)
    if not match:
        return None
    you = "rock" if match["hand"].casefold() == "stone" else match["hand"].casefold()
    me = rng.choice(tuple(_BEATS))
    if you == me:
        result = "We both chose " + you + ". A draw."
    elif _BEATS[you] == me:
        _GAME["you"] += 1
        result = f"I chose {me}. {_HOW[you].capitalize()}, so you win."
    else:
        _GAME["me"] += 1
        result = f"I chose {me}. {_HOW[me].capitalize()}, so I win."
    score = f"You {_say(_GAME['you'])}, me {_say(_GAME['me'])}."
    return Reply(f"[joy] {result} {score}")


def _score(animal):
    traits = play_data.ANIMALS[animal]
    return sum((key in traits) != value for key, value in _GAME["answers"].items())


def _candidates():
    alive = [a for a in play_data.ANIMALS if a not in _GAME["wrong"]]
    if not alive:
        return []
    best = min(_score(a) for a in alive)
    return [a for a in alive if _score(a) == best]


def _next_question():
    """The question that splits the best candidates most evenly, or a guess."""
    candidates = _candidates()
    if not candidates:
        return None
    if len(candidates) == 1 or _GAME["questions"] >= MAX_QUESTIONS - 1:
        _GAME["guessing"] = candidates[0]
        _GAME["questions"] += 1
        return f"Is it {_article(candidates[0])} {candidates[0]}?"
    open_keys = [k for k in play_data.QUESTIONS if k not in _GAME["answers"] and k not in _GAME["asked"]]
    best, spread = None, None
    for key in open_keys:
        yes = sum(key in play_data.ANIMALS[a] for a in candidates)
        gap = abs(len(candidates) - 2 * yes)
        if 0 < yes < len(candidates) and (spread is None or gap < spread):
            best, spread = key, gap
    if best is None:
        _GAME["guessing"] = candidates[0]
        _GAME["questions"] += 1
        return f"Is it {_article(candidates[0])} {candidates[0]}?"
    _GAME["guessing"] = None
    _GAME["asked"].append(best)
    _GAME["questions"] += 1
    return play_data.QUESTIONS[best]


def _article(word):
    return "an" if word[0] in "aeiou" else "a"


def _questions_turn(said, rng):
    yes, no = bool(_YES.match(said)), bool(_NO.match(said))
    unsure = bool(_UNSURE.match(said))
    if not (yes or no or unsure):
        return None
    guessing = _GAME.get("guessing")
    if guessing:
        if yes:
            return _end(f"[joy] I got it! It was {_article(guessing)} {guessing}, in {_say(_GAME['questions'])} "
                        f"question{'s' if _GAME['questions'] != 1 else ''}.")
        _GAME["wrong"].add(guessing)
    elif _GAME["asked"]:
        key = _GAME["asked"][-1]
        if not unsure:
            _GAME["answers"][key] = yes
    if _GAME["questions"] >= MAX_QUESTIONS:
        return _end("[joy] That's twenty questions, and I still don't know. You win! Well played.")
    question = _next_question()
    if question is None:
        return _end("[joy] You've beaten me. I don't know that animal. You win!")
    lead = "Hmm, not that then. " if guessing else ""
    return Reply(f"[curiosity] {lead}{question}")


def _game_turn(said, ctx, rng):
    if not _GAME:
        return None
    if time.monotonic() - _GAME["at"] > GAME_IDLE or _GAME["speaker"] != _owner(ctx):
        _GAME.clear()
        return None
    kind = _GAME["kind"]
    if _STOP.match(said):
        if kind == "number":
            return _end(f"[neutral] Okay. My number was {_say(_GAME['secret'])}.")
        if kind == "rps":
            you, me = _GAME["you"], _GAME["me"]
            verdict = "You won" if you > me else "I won" if me > you else "It was a draw"
            return _end(f"[neutral] Okay, game over. {verdict}, {_say(max(you, me))} to {_say(min(you, me))}.")
        return _end("[neutral] Okay, we'll stop. You can tell me what it was, if you like.")
    turn = {"number": _number_turn, "rps": _rps_turn, "questions": _questions_turn}[kind](said, rng)
    if turn is not None and _GAME:
        _GAME["at"] = time.monotonic()
    return turn


# -- the handler ----------------------------------------------------------------

async def handle(text, ctx, *, rng=None):
    rng = rng or _random
    if len(text or "") > 200:      # every phrase and move here is short
        return None
    said = clean(text, getattr(ctx, "name", "pip")).rstrip("?!").strip()
    said = re.sub(r"\s+", " ", said.replace(",", ", ")).replace(" ,", ",")
    folded = re.sub(r"[,]", "", said).strip()
    if (turn := _game_turn(folded, ctx, rng)) is not None:
        return turn
    for kind, pattern in _START:
        if pattern.match(folded):
            return _start(kind, ctx, rng)
    if _MENU.match(folded):
        return Reply("[joy] We could play guess the number, rock paper scissors or twenty questions. Which one?")
    if match := (_SPELL.match(folded) or _SPELL_HOW_IS.match(folded)):
        line = _spelling(match["word"])
        if line:
            return Reply(line)
    kind = None
    if _JOKE.match(folded):
        kind = "joke"
    elif _FACT.match(folded):
        kind = "fact"
    elif _ANOTHER.match(folded) and _LAST and time.monotonic() - _LAST["at"] < ANOTHER_WITHIN:
        kind = _LAST["kind"]
    if kind is None:
        return None
    text = pick(kind, ctx.state_dir, guest=is_guest(ctx), rng=rng, background=True)
    _LAST.update(kind=kind, at=time.monotonic())
    return Reply(("[joy] " if kind == "joke" else "[curiosity] ") + text)
