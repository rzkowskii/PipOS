"""Spoken commands for Pip's workshop apps, matched in code.

The mind calls `AppBridge.parse()` before the memory and skill commands in
mind/commands.py (privacy commands keep their meaning and always come first).
Everything said back is a fixed line from this file, built from facts the
hands organ reported (term.status, app.state). Nothing here asks the model,
and no line claims what did not happen: "Here's my terminal" only after
app.state says the panel is shown, "It ran without errors", never "it works".

See docs/design/voice-apps.md sections 3.4, 4 and 6.
"""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SHELF = Path(__file__).resolve().parent / "workshop_shelf"
INSTALLED_SHELF = Path("/usr/share/being/workshop/shelf")
CONFIRM_SECONDS = 15.0
REPLY_WAIT = 2.0
OPEN_WAIT = 4.0     # starting the shell's unit and the renderer takes 1.5-3 s (estimate)
LEASE_WAIT = 0.5

ACTIVE = ("reading", "writing", "held", "paused", "checking", "countdown", "running", "frozen")
GENERATING = ("reading", "writing", "held")
FINISHED = ("waiting", "done", "failed", "cancelled")


@dataclass(frozen=True)
class AppCommand:
    kind: str                 # open, minimise, restore, close, task, control, install, uninstall,
    #                           run_app, shelf_list, report, read_line, answer, forget_script,
    #                           clear_workshop, say
    app: str = "terminal"
    action: str = ""          # for control: pause, resume, cancel, undo, run, fix; for answer: yes, no
    task: str = ""            # for task: recipe, script or command
    text: str = ""            # the request as heard (after normalising)
    recipe: str = ""
    slots: tuple = ()         # (("n", 10),)
    force_model: bool = False
    program: str = ""         # a shelf program's name
    say: str = ""             # a fixed line, for kind "say" (and the offline weather note)
    request: str = ""         # for task: just the thing asked for ("draws a spiral")


SMALL_APPS = ("monitor", "clock")


@dataclass
class AppContext:
    enabled: bool = True
    guest: bool = False
    hands_up: bool = True
    terminal: str = "closed"  # closed, shown, minimised
    job: str | None = None    # the job's phase, or None
    job_source: str = ""
    pending: str = ""         # "close" or "forget" while a yes/no question is open
    shelf: dict = field(default_factory=dict)
    monitor: str = "closed"   # the two small apps: closed or shown (they never minimise)
    clock: str = "closed"

    @property
    def small_app(self):
        """The small app on screen, or ''."""
        return next((app for app in SMALL_APPS if getattr(self, app) == "shown"), "")


# -- normalising -------------------------------------------------------------

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_LEADING = re.compile(
    r"^(?:(?:hey|hi|ok|okay|oh|so|um+|uh+|er+m?|well|right|alright|now)[, ]+)+"
    r"|^(?:please |can you |could you |would you |will you |go ahead and |i want you to |i'd like you to )+"
)
_TRAILING = re.compile(r"(?:[, ]+(?:please|for me|thanks|thank you|now|right now))+$")


def spoken_numbers(text):
    """'count down from twenty five' -> 'count down from 25'."""
    words = text.replace(",", " ,").split(" ")
    out = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in _TENS:
            value = _TENS[word]
            if index + 1 < len(words) and words[index + 1] in _UNITS and 0 < _UNITS[words[index + 1]] < 10:
                value += _UNITS[words[index + 1]]
                index += 1
            out.append(str(value))
        elif word in _UNITS:
            out.append(str(_UNITS[word]))
        elif "-" in word and all(part in _TENS or part in _UNITS for part in word.split("-")):
            out.append(str(sum(_TENS.get(part, _UNITS.get(part, 0)) for part in word.split("-"))))
        else:
            out.append(word)
        index += 1
    return " ".join(out).replace(" ,", ",")


def normalise(text, name="pip"):
    said = text.casefold().replace("’", "'").replace("‘", "'")
    said = re.sub(r"[^\w' ,:-]", " ", said)
    said = " ".join(said.split())
    address = re.escape(name.casefold()) if name else "pip"
    for _ in range(3):
        before = said
        said = re.sub(rf"^(?:(?:hey|hi|ok|okay)[, ]+)?{address}\b[, ]*", "", said)
        said = _LEADING.sub("", said)
        if said == before:
            break
    said = _TRAILING.sub("", said).strip(" ,:-")
    said = re.sub(r"\s*,\s*", ", ", said)
    return spoken_numbers(said).strip()


# -- the shelf ----------------------------------------------------------------

def load_shelf(path=None):
    """{spoken name or alias: program name} from the shelf's app.json files."""
    for root in ([Path(path)] if path else [INSTALLED_SHELF, SHELF]):
        names = {}
        for manifest in sorted(root.glob("*/app.json")):
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = info.get("name")
            if isinstance(name, str) and name == manifest.parent.name:
                names[name] = name
                for alias in info.get("aliases") or ():
                    if isinstance(alias, str):
                        names[" ".join(alias.casefold().split())] = name
        if names:
            return names
    return {}


# -- recipes (the matching half; the scripts themselves are in hands.py) ----------

_DRAW = r"(?:draws?|prints?|makes?|shows?|displays?|does|creates?)(?: me)?"
_SIZE = r"(?:(?: of size| size| that is| with a width of| with a height of)? (?P<n>\d+)(?: wide| high| tall| big| lines?| rows?)?)?"
RECIPE_PATTERNS = [
    ("countdown", r"(?:counts? ?down|does a count ?down|count ?down|a count ?down)(?: from (?P<n>\d+))?(?: to (?:0|1|zero|one))?(?: seconds)?"),
    ("countup", r"counts?(?: up)? (?:to|up to|from 1 to) (?P<n>\d+)"),
    ("times_table", rf"(?:{_DRAW} )?(?:the |a )?(?P<n>\d+) times tables?|(?:{_DRAW} )?(?:the |a )?times tables? (?:for|of) (?P<n2>\d+)"),
    ("note", r"(?:makes?|takes?|writes?|saves?|adds?) a note(?: that says| saying| that|:|,)? (?P<text>.+)"),
    ("list_notes", r"(?:lists?|shows?|reads?|prints?)(?: me| out)? (?:all )?(?:my|the) notes"),
    ("clock", rf"(?:{_DRAW} |is |runs? )?a clock(?: for (?P<n>\d+) seconds)?"),
    ("dice", r"rolls?(?: (?P<n>\d+))? dice|rolls? a die|throws?(?: (?P<n2>\d+))? dice"),
    ("coin", r"(?:flips?|tosses?) a coin|(?:does a )?coin (?:flip|toss)"),
    ("spiral", rf"{_DRAW} an? (?:ascii )?spiral"),
    ("box", rf"{_DRAW} an? (?:ascii )?(?:box|square){_SIZE}"),
    ("triangle", rf"{_DRAW} an? (?:ascii )?triangle{_SIZE}"),
    ("diamond", rf"{_DRAW} an? (?:ascii )?diamond{_SIZE}"),
    ("tree", rf"{_DRAW} an? (?:ascii |christmas )?tree"),
    ("heart", rf"{_DRAW} an? (?:ascii )?heart"),
    ("warmth", r"(?:shows?(?: me)? )?(?:how warm (?:you are|it is inside|you're)|(?:your|the) (?:temperature|temperatures))"),
    ("weather", r"(?:shows?(?: me)? |tells?(?: me)? |what's )?the weather"),
    ("files", r"(?:shows?|lists?)(?: me)?(?: all)? (?:the |my )?files(?: here| in here| in the workshop)?|what files are (?:here|there)"),
    ("disk", r"(?:shows?(?: me)? )?(?:the |how much )?(?:disk|free) space(?: is left)?|how much space is left"),
    ("memory", r"(?:shows?(?: me)? )?(?:the |your |how much )?memory(?: usage| is free| use)?"),
    ("uptime", r"(?:shows?(?: me)? )?(?:your |the )?uptime|how long (?:have you|you've) been (?:up|on|running)"),
]
_RECIPES = [(name, re.compile(rf"^(?:{pattern})$")) for name, pattern in RECIPE_PATTERNS]
WEATHER_NOTE = "I'm offline, so I can't see the weather. Here's how warm I am inside."


def match_recipe(task_text):
    """(recipe name, slots) for a task's words, or (None, ())."""
    text = re.sub(r"^(?:that |which |to |for me |me )+", "", task_text.strip(" ,:"))
    for name, pattern in _RECIPES:
        match = pattern.match(text)
        if not match:
            continue
        groups = {key: value for key, value in match.groupdict().items() if value}
        slots = {}
        for key, value in groups.items():
            slot = key.rstrip("2")
            slots[slot] = value.strip() if slot == "text" else int(value)
        if name == "weather":
            return "warmth", ()
        return name, tuple(sorted(slots.items()))
    return None, ()


# -- the command grammar -------------------------------------------------------------

_T = r"(?:the |your |my |a |that )?(?:terminal|console|shell|command line|command prompt)"
_PATTERNS = [
    # A bare "terminal" or "the console" opens it; a bare "shell" or "a shell"
    # does not (it is too common an answer in conversation).
    ("open", rf"(?:open|start|launch|bring up|pull up|fire up|load)(?: up)? {_T}(?: for me)?|give me a (?:shell|terminal|console)"
             r"|(?:the |your |my )?(?:terminal|console)"),
    ("open_monitor", r"show me how you(?:'re| are) (?:feeling|doing)|show me your (?:temperature|body|vitals|insides)"
                     r"|(?:open|show me|bring up|start|launch) (?:the |your )?(?:body )?monitor"),
    ("open_clock", r"show me (?:the|your) (?:clock|time)|show (?:me )?my timers|(?:open|bring up|start|launch) (?:the |your )?clock"),
    ("close_small", r"(?:close|hide|minimi[sz]e|shut|turn off|get rid of|put away|stop) (?:the |your |my )?(?P<a>(?:body )?monitor|clock|timers)"
                    r"|put (?:the |your )?(?P<a2>clock|monitor) away|close (?:it|that)"),
    ("show_terminal", rf"(?:show(?: me)?|bring back|switch to|go back to|restore|unhide|reopen) {_T}|show (?:it|that) (?:again|to me)|bring it back"),
    ("minimise", rf"(?:minimi[sz]e|hide|put away|tuck away|shrink)(?: it| that| {_T})(?: away)?|minimi[sz]e|show me your face|(?:go|come) back to your face|put it away"),
    ("close", rf"(?:close|quit|exit|shut(?: down)?|get rid of|kill|turn off|stop) {_T}(?: completely)?|close (?:it|that) (?:completely|down)"),
    ("pause", r"stop (?:writing|typing)(?: that| the| this| it)?(?: script| program| code)?(?: for (?:a|one) (?:second|moment|minute|bit|sec))?|pause(?: it| that| the script| writing| typing)?(?: for (?:a|one) (?:second|moment|minute|sec))?|hold on(?: (?:a|one) (?:second|moment|minute|sec))?|hang on(?: (?:a|one) (?:second|moment|minute|sec))?|wait(?: (?:a|one) (?:second|moment|minute|sec))?|stop(?: for (?:a|one) (?:second|moment|sec))?|just (?:a|one) (?:second|moment|sec)"),
    ("resume", r"(?:okay |ok |you can )?(?:carry on|continue|keep going|go on|resume|keep (?:typing|writing)|start again|unpause)(?: writing| typing| with it| please)?"),
    ("cancel", r"cancel(?: that| it| the script| the program| writing)?|scrap (?:it|that)|throw (?:it|that) away|never ?mind the (?:script|program|code)|forget (?:that|the|this) (?:script|program|code)|stop it completely|stop completely|abandon (?:it|that)|bin it"),
    ("undo", r"undo(?: that| it)?|put it back"),
    ("run", r"(?:run|try|start|execute) (?:it|that|this|the script|the program|the code)(?: again)?|run that again|run it now|give it a go"),
    ("fix", r"fix (?:it|that|the script|the error)|try again|have another go"),
    ("shelf_list", r"install (?:an? )?(?:application|app|program|something|game)|what (?:can|could) you install|what(?:'s| is) on your shelf|show me your shelf|what programs do you have|what games do you have"),
    ("uninstall", r"(?:uninstall|remove|delete) (?P<p>[\w' -]+)"),
    ("install", r"(?P<verb>install|download|get|add) (?:me )?(?P<p>[\w' .-]+)"),
    ("run_app", r"(?:run|play|start|launch|open) (?P<p>[\w' -]+)"),
    ("read_line", r"what did it (?:say|print|show|output)|what does it say|read (?:it|that) (?:out|to me)"),
    ("report", r"what happened|did it work|what are you doing|are you (?:done|finished)|is it (?:done|finished|ready)|how(?:'s| is) it going|how far (?:are you|have you got)|what(?:'s| is) it doing"),
    ("clear_workshop", r"clear (?:the|your) workshop|empty (?:the|your) workshop|wipe (?:the|your) workshop"),
    ("forget_file", r"forget (?:that|the|this) (?:script|program|code|file)"),
]
_COMPILED = [(kind, re.compile(rf"^(?:{pattern})$")) for kind, pattern in _PATTERNS]
_CHECK_IN = re.compile(r"^(?:how(?:'s| is) it going|what are you doing)$")
_TASK = [
    # The verb may be left out only after "write it yourself:" ("a script
    # that..."); otherwise "a script for my play is due" would start a job.
    ("script", re.compile(r"^(?:(?P<verb>write|make|create|code|build)(?: me)? )?(?:an? )?(?:little |small |quick |tiny |simple )?(?:python |shell )?(?:script|program|app)(?:,)?(?: (?:that|which|to|for|so it|to make it))? (?P<t>.+)$")),
    ("terminal", re.compile(r"^(?:in (?:the|your) terminal|on the terminal|using the terminal)[, :]+(?P<t>.+)$")),
    ("terminal", re.compile(r"^do the following in (?:the|your) terminal[, :]+(?P<t>.+)$")),
    ("terminal", re.compile(r"^(?:type|run the command|run a command to) (?P<t>.+) in (?:the|your) terminal$")),
]
_FORCE = re.compile(r"(?:[, ]+(?:and )?(?:write it yourself|from scratch|without your recipe book|without a recipe))$|^(?:write it yourself|from scratch)[,: ]+")
# The speculative-read gate (PIP-FUNCTIONS 5.2): words that start one of these
# commands whatever is on screen, so the mind does not start reading the model
# ahead for them. They are prefixes, because the gate may see a partial
# transcript ("open the").
_THING = r"(?:the |your |my |a |that )?(?:body )?(?:terminal|console|shell|command line|command prompt|monitor|clock|timers)\b"
COMMAND_START = re.compile(
    r"^(?:(?:open|close|quit|exit|launch|start|bring up|pull up|fire up|load|show(?: me)?|switch to|go back to|restore"
    rf"|unhide|reopen|minimi[sz]e|hide|put away|tuck away|shrink|get rid of|kill|turn off|shut(?: down)?) {_THING}"
    r"|(?:open|bring up|pull up|show me|show|start|launch|close|hide)(?: (?:the|your|my|a|that))?\s*$"
    r"|show me how you(?:'re| are) (?:feeling|doing)|show me your (?:temperature|body|vitals|insides|clock|time)"
    r"|show me the (?:clock|time)|show (?:me )?my timers"
    r"|give me a (?:shell|terminal|console)|(?:install|uninstall|download)\b"
    r"|what (?:can|could) you install|what(?:'s| is) on your shelf|show me your shelf|what (?:programs|games) do you have"
    r"|(?:clear|empty|wipe) (?:the|your) workshop"
    r"|in (?:the|your) terminal|on the terminal|using the terminal|do the following|write it yourself|from scratch"
    r"|(?:write|make|create|code|build)(?: me)? (?:an? )?(?:little |small |quick |tiny |simple )?(?:python |shell )?"
    r"(?:script|program|app)\b"
    r"|(?:the |your |my )?(?:terminal|console)\s*$)"
)
# Words that are ours only while something is on screen or a job exists
# ("stop", "wait", "carry on", "how's it going", "show me your face"...).
# With nothing open they are conversation, and the read-ahead may start.
CONTEXT_START = re.compile(
    r"^(?:stop|pause|hold on|hang on|wait|just (?:a|one)|carry on|continue|keep going|keep typing|keep writing|go on"
    r"|resume|start again|unpause|you can carry on|okay carry on|ok carry on|cancel|scrap|throw|never ?mind the"
    r"|forget (?:that|the|this) (?:script|program|code|file)|abandon|bin it|undo|put it back|run|try|execute|give it a go"
    r"|fix|have another go|what (?:happened|did it|does it|are you doing|'s it doing|is it doing)|did it work"
    r"|are you (?:done|finished)|is it (?:done|finished|ready)|how(?:'s| is) it going|how far|read (?:it|that)"
    r"|show me your face|(?:go|come) back to your face|put it away|hide it|hide that|minimi[sz]e|close (?:it|that)"
    r"|show (?:it|that)|bring it back)\b"
)
# A job still counts as going on (for the gate and for "how's it going?")
# while it is active or written and waiting to run; a finished one is history.
LIVE = ACTIVE + ("waiting",)
_ANSWER_START = re.compile(r"^(?:yes|yeah|yep|no\b|nope|sure|okay|ok|do it|go ahead|leave it|keep it|close it|forget it all)")
_SHELF_VERB = re.compile(r"^(?:run|play|start|launch|open|install|get|add|uninstall|remove|delete) (?:me )?(?P<p>[\w' -]+)$")
_DEFAULT_SHELF = None


def may_be_app_command(text, ctx=None, name="pip"):
    """True if `text` (perhaps only the start of an utterance) may be one of
    these commands. Without a context, nothing is taken to be open, so a bare
    "stop", "wait", "continue" or "how's it going" says no; AppBridge passes
    its own context (AppBridge.may_be_command)."""
    global _DEFAULT_SHELF
    said = normalise(text, name)
    if not said:
        return False
    if COMMAND_START.match(said):
        return True
    if ctx is None:
        if _DEFAULT_SHELF is None:
            _DEFAULT_SHELF = load_shelf()
        shelf = _DEFAULT_SHELF
    else:
        shelf = ctx.shelf
    match = _SHELF_VERB.match(said)
    if match and " ".join(match["p"].split()).strip(" .") in shelf:
        return True
    if ctx is None:
        return False
    if ctx.pending and _ANSWER_START.match(said):
        return True
    on_screen = ctx.terminal != "closed" or bool(ctx.small_app) or ctx.job in LIVE
    if on_screen and CONTEXT_START.match(said) and not _CHECK_IN.match(said):
        return True
    # Otherwise only a whole command counts: with a finished script and the
    # terminal closed, "did it work?" is still ours, "running late" is not.
    return parse(text, ctx, name) is not None


def _task_command(said):
    force = bool(_FORCE.search(said))
    said = _FORCE.sub("", said).strip(" ,:")
    for where, pattern in _TASK:
        match = pattern.match(said)
        if not match:
            continue
        if where == "script" and not match["verb"] and not force:
            continue
        request = match["t"].strip(" ,:")
        if len(request) < 3:
            return None
        if where == "terminal":
            # "In the terminal, write a script that..." is the script request itself.
            inner = _task_command(request + (", write it yourself" if force else ""))
            if inner is not None and inner.task != "command":
                return AppCommand("task", task=inner.task, text=said, recipe=inner.recipe, slots=inner.slots,
                                  force_model=inner.force_model, say=inner.say, request=inner.request)
        recipe, slots = match_recipe(request)
        say = WEATHER_NOTE if re.match(r"^(?:shows?(?: me)? |tells?(?: me)? |what's )?the weather$", request) else ""
        if recipe and not force:
            return AppCommand("task", task="recipe", text=said, recipe=recipe, slots=slots, say=say, request=request)
        kind = "script" if where == "script" or re.match(r"^(?:write|make|create) ", request) else "command"
        if recipe and force:
            from_recipe = "script" if recipe not in ("files", "disk", "memory", "uptime") else "command"
            kind = from_recipe
        return AppCommand("task", task=kind, text=said, force_model=force, recipe=recipe or "", slots=slots,
                          request=request)
    return None


def parse(text, ctx, name="pip"):
    """The AppCommand in `text` given the current context, or None to let the
    memory commands and the conversation have it. The whole utterance must match."""
    said = normalise(text, name)
    if not said:
        return None
    # Privacy commands always keep their memory meaning.
    if re.match(r"^(?:start|end) guest mode$|^don't remember this\b", said):
        return None
    if ctx.pending:
        if re.match(r"^(?:yes|yeah|yep|yes please|do it|go ahead|sure|okay|ok|close it|forget it all|yes close it)$", said):
            return AppCommand("answer", action="yes")
        if re.match(r"^(?:no|nope|no thanks|don't|leave it|keep it|keep it open|never ?mind)$", said):
            return AppCommand("answer", action="no")
    job_active = ctx.job in ACTIVE
    job_exists = ctx.job is not None
    terminal = ctx.terminal != "closed"
    small = ctx.small_app
    task = _task_command(said)
    if task is not None:
        return _gate(task, ctx) or task
    for kind, pattern in _COMPILED:
        match = pattern.match(said)
        if not match:
            continue
        program = (match.groupdict().get("p") or "").strip(" .")
        if kind == "open":
            return _gate(AppCommand("open"), ctx) or AppCommand("open")
        if kind in ("open_monitor", "open_clock"):
            command = AppCommand("open", app="monitor" if kind == "open_monitor" else "clock")
            return _gate(command, ctx) or command
        if kind == "close_small":
            named = (match["a"] or match["a2"] or "").replace("body ", "")
            app = {"timers": "clock"}.get(named, named)
            if not app:  # "close it": whatever is on screen
                if small:
                    return AppCommand("close", app=small)
                if ctx.terminal == "shown":
                    return AppCommand("close")
                return None
            if getattr(ctx, app) == "shown":
                return AppCommand("close", app=app)
            if said.startswith("stop"):
                continue  # "stop the clock" may be about a script's clock
            return AppCommand("say", say=REPLIES[f"{app}_not_open"])
        if kind == "show_terminal":
            if ctx.terminal == "minimised":
                return AppCommand("restore")
            if ctx.terminal == "shown":
                return AppCommand("say", say="Here it is.")
            return _gate(AppCommand("open"), ctx) or AppCommand("open")
        if kind == "minimise":
            names_terminal = "terminal" in said or "console" in said or "shell" in said
            if small and not names_terminal:
                # "Show me your face" with the monitor or clock up: the small
                # apps close instead of minimising (voice-apps 3.5).
                return AppCommand("minimise", app=small)
            if said in ("show me your face", "go back to your face", "come back to your face") and not terminal:
                return None
            if not terminal:
                return AppCommand("say", say=REPLIES["not_open"]) if names_terminal else None
            if ctx.terminal == "minimised" and not small:
                return AppCommand("say", say=REPLIES["already_hidden"]) if names_terminal else None
            return AppCommand("minimise")
        if kind == "close":
            return AppCommand("close") if terminal else AppCommand("say", say=REPLIES["not_open"])
        if kind in ("pause", "resume", "cancel", "undo", "run", "fix"):
            if kind == "cancel" and said.startswith("forget") and not job_active:
                if ctx.job in ("waiting", "done", "failed"):
                    return AppCommand("forget_script")
                return None
            if kind == "resume" and ctx.job == "cancelled":
                return AppCommand("control", action="undo")
            if job_active or (job_exists and (kind in ("run", "fix", "undo")
                                              or (kind == "cancel" and ctx.job == "waiting"))):
                return AppCommand("control", action=kind)
            if terminal:
                return AppCommand("say", say=REPLIES["nothing"])
            return None  # "continue the story" still works
        if kind == "shelf_list":
            return _gate(AppCommand("shelf_list"), ctx) or AppCommand("shelf_list")
        if kind in ("install", "uninstall", "run_app"):
            known = ctx.shelf.get(" ".join(program.split()))
            if known:
                command = AppCommand(kind, program=known, text=said)
                return _gate(command, ctx) or command
            if kind == "install" and match["verb"] in ("install", "download"):
                # "get me a coffee" or "add milk to the list" is not ours.
                return AppCommand("say", say=REPLIES["no_download"])
            if kind == "uninstall" and program in ("it", "that", "the script"):
                return None
            continue  # "play some music", "open the window": not ours
        if kind == "read_line":
            return AppCommand("read_line") if job_exists else (AppCommand("say", say=REPLIES["nothing"]) if terminal else None)
        if kind == "report":
            if _CHECK_IN.match(said) and ctx.job not in LIVE and not terminal:
                # With no job going on and nothing open, "how's it going?" is
                # a greeting, not a question about a script finished long ago.
                return None
            if job_exists:
                return AppCommand("report")
            if _CHECK_IN.match(said):
                return None  # with no job, "how's it going?" is a greeting
            return AppCommand("say", say=REPLIES["nothing"]) if terminal else None
        if kind == "clear_workshop":
            return AppCommand("clear_workshop")
        if kind == "forget_file":
            if ctx.job in ("waiting", "done", "failed"):
                return AppCommand("forget_script")
            return None
    return None


def _gate(command, ctx):
    """The context that turns a request into a fixed reply."""
    if command.kind not in ("open", "task", "install", "uninstall", "run_app", "shelf_list"):
        return None
    if not ctx.enabled:
        return AppCommand("say", say=REPLIES["disabled"])
    if ctx.guest:
        if command.kind == "open" and command.app in SMALL_APPS:
            return AppCommand("say", say=REPLIES[f"guest_{command.app}"])
        return AppCommand("say", say=REPLIES["guest"])
    if command.kind == "shelf_list":
        return None  # the shelf can be listed with my hands down: it is only words
    if not ctx.hands_up:
        if command.kind == "open" and command.app in SMALL_APPS:
            return AppCommand("say", say=REPLIES["no_hands"].replace("the terminal", f"my {command.app}"))
        return AppCommand("say", say=REPLIES["no_hands"])
    if command.kind == "task" or command.kind in ("install", "uninstall", "run_app"):
        if ctx.job in ACTIVE:
            return AppCommand("say", say=REPLIES["busy"])
    if command.kind == "open" and command.app == "terminal" and ctx.terminal == "shown" and not ctx.small_app:
        return AppCommand("say", say=REPLIES["already_open"])
    if command.kind == "open" and command.app in SMALL_APPS and getattr(ctx, command.app) == "shown":
        return AppCommand("say", say=REPLIES["already_open"])
    return None


# -- what Pip says ---------------------------------------------------------------------

REPLIES = {
    "open": "Here's my terminal.",
    "open_monitor": "Here's how I'm doing.",
    "open_clock": "Here's my clock.",
    "monitor_not_open": "My monitor isn't open.",
    "clock_not_open": "My clock isn't open.",
    "already_hidden": "It's already tucked away.",
    "already_open": "It's already open.",
    "open_failed": "My terminal didn't open.",
    "restored": "Here it is.",
    "minimised": "Okay.",
    "minimised_writing": "Okay. I'm still writing.",
    "closed": "Closed.",
    "close_busy": "Something is still running. Close it anyway?",
    "kept_open": "Okay. I'll leave it open.",
    "not_open": "The terminal isn't open.",
    "recipe": "I know this one.",
    "model": "I'll try.",
    "busy": "I'm already writing one. Say cancel first.",
    "nothing": "I'm not writing anything.",
    "paused_first": "Paused. Say carry on when you're ready.",
    "paused": "Paused.",
    "resumed": "Carrying on.",
    "cancelled": "Cancelled.",
    "undone": "Okay, I'll carry on with it.",
    "no_undo": "It's too late to undo that.",
    "ready": "It's ready. Say run it.",
    "running": "Running it.",
    "ran_ok": "It ran without errors.",
    "disabled": "My apps are switched off. You can turn them on in Setup.",
    "guest": "My terminal stays closed while a guest is here.",
    "guest_clock": "My clock stays closed while a guest is here. It shows your reminders.",
    "guest_monitor": "My monitor stays closed while a guest is here.",
    "no_hands": "My hands aren't working right now, so I can't open the terminal.",
    "unavailable": "I don't have that app yet. I can open my terminal.",
    "no_download": ("I can't download programs. I'm offline and my system is read-only. "
                    "But I have a shelf of little programs, or I can try to write you a small one."),
    "stall": "I got stuck, so I paused.",
    "lost": "I lost my thought. Say carry on to try again.",
    "loop": "I got stuck in a loop, so I stopped.",
    "lid": "I paused while the lid was closed. Say carry on.",
    "not_written": "Something went wrong while I was typing, so it wasn't saved.",
    "forget_ask": "Should I forget that script and every copy of it?",
    "forgotten": "Okay. That script is forgotten.",
    "clear_ask": "Should I clear my whole workshop?",
    "cleared": "Okay. My workshop is empty.",
    "kept": "Okay. I've kept it.",
    "no_fix": "I can only try one fix, after it fails.",
    "lid_closed": "My lid is closed, so I can't use my terminal right now.",
    "too_warm": "I'm too warm, so my terminal is resting until I cool down.",
    "low_battery": "My battery is low, so I won't start programs until I'm plugged in.",
    "asleep": "I'm resting, so my terminal is paused.",
    "sleep": "I paused while I was asleep. Say carry on.",
    "heat": "I paused because I got too warm. Say carry on when I've cooled down.",
    "bad_slot": "My recipe can't take that number.",
    "copies_forgotten": "Okay. I removed those copies too.",
    "countdown_stopped": "Okay. I didn't run it.",
}


def number_words(value):
    small = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
             "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
    if not isinstance(value, int) or value < 0:
        return str(value)
    if value < 20:
        return small[value]
    if value < 100:
        return tens[value // 10] + ("" if value % 10 == 0 else "-" + small[value % 10])
    return str(value)


def _plural(count, word):
    return f"{number_words(count)} {word}{'' if count == 1 else 's'}"


def status_line(status):
    """The line to announce for a term.status the hands marked speakable, or ''."""
    if not status or not status.get("speakable"):
        return ""
    phase, reason, exit_code = status.get("phase"), status.get("reason") or "", status.get("exit")
    if phase == "waiting":
        return REPLIES["ready"]
    if phase == "done":
        if reason == "exit_unknown":
            return "It finished. I couldn't tell whether it had errors."
        return REPLIES["ran_ok"]
    if phase == "failed":
        if reason == "time_limit":
            return "It ran for a minute, so I stopped it."
        if reason == "output_limit":
            return "It printed too much, so I stopped it."
        if reason.startswith("policy:"):
            return f"I wrote it, but I won't run it: {reason.split(':', 1)[1]}."
        if reason in ("not_written", "changed_while_typing"):
            return REPLIES["not_written"]
        if reason == "fix_failed":
            return "I tried a small fix, but it still isn't right."
        if reason == "shell_ended":
            return "My terminal's shell stopped."
        if reason == "stopped":
            return "Stopped."
        if isinstance(exit_code, int):
            line = f"It failed with exit code {exit_code}."
            if status.get("source") == "model" and status.get("kind") == "script" and not status.get("fixed"):
                line += " Say fix it if you'd like me to try once more."
            return line
        return "It didn't work."
    if phase == "paused":
        return REPLIES.get(reason, "")
    if phase == "cancelled":
        if reason == "loop":
            return REPLIES["loop"]
        if reason in ("long_line", "too_long", "end_word", "empty"):
            return "What I was writing went wrong, so I stopped."
        if reason.startswith("policy:"):
            return f"I won't type that command: {reason.split(':', 1)[1]}."
    return ""


def report(status, text=None):
    """A sentence about the job, built in code from its last term.status."""
    if not status or not status.get("phase"):
        return REPLIES["nothing"]
    phase = status["phase"]
    lines = status.get("lines") or 0
    source = status.get("source")
    made = " from my recipe book" if source == "recipe" else " with my language model" if source == "model" else ""
    if phase == "reading":
        return "I'm reading your request."
    if phase in ("writing", "held"):
        return f"I'm writing it{made}. {_plural(lines, 'line').capitalize()} so far." if lines else f"I'm writing it{made}."
    if phase == "paused":
        return f"It's paused at line {number_words(lines)}." if lines else "It's paused."
    if phase == "checking":
        return "I'm checking what I wrote."
    if phase == "waiting":
        return f"It's written{made}. Say run it."
    if phase == "countdown":
        return "It's about to run. Say stop if you don't want it to."
    if phase == "running":
        return "It's running."
    if phase == "frozen":
        return "It's frozen. Say carry on to let it run."
    if phase == "cancelled":
        return "I cancelled it."
    line = status_line({**status, "speakable": True})
    if text is not None:
        printed = text.get("lines") or 0
        if printed:
            line += f" It printed {_plural(printed, 'line')}. They're on the screen."
    return line or "It's finished."


def read_line(text):
    """Answer "what did it say?" from term.text: a line is read aloud only if
    it is short and mostly words."""
    line = (text or {}).get("text") or ""
    count = (text or {}).get("lines") or 0
    if line:
        return f'It said: "{line}"'
    if count:
        return f"It printed {_plural(count, 'line')}. They're on the screen."
    return "It didn't print anything I can read out."


# -- the mind's side of the bus ------------------------------------------------------------

class AppBridge:
    """Everything the mind needs for the apps, so mind/turn.py gains only a few
    hooks: observe() each event, parse() a final, handle() a command, lease()
    around model turns, announcement() on each beat, privacy() on guest change.

    `say` is an async callable taking a fixed line ("[neutral] ..." style tags
    are added here)."""

    def __init__(self, bus, *, clock=time.monotonic, shelf=None, enabled=None, name="pip"):
        self.bus = bus
        self.clock = clock
        self.name = name
        self.shelf = load_shelf(shelf) if not isinstance(shelf, dict) else shelf
        self.enabled = enabled or (lambda: not Path("/mind/apps.disabled").exists())
        self.hands_up = False
        self.view = {"terminal": "closed", "monitor": "closed", "clock": "closed"}
        self.status = None          # the last term.status
        self.text = None            # the last term.text
        self.pending = ""
        self.pending_until = 0.0
        self.pending_files = ()
        self.paused_once = False
        self._waiters = {}
        self._announce = []
        self._leased = False
        self.guest = False
        self._episoded = set()      # (file, sha256) already handed out by episode()
        self._episode_ids = []      # (episode id, job id, file) the mind recorded
        self._to_forget = []

    # -- incoming -------------------------------------------------------------------

    SUBS = ("app.state", "app.refused", "term.status", "term.text")

    def observe(self, event):
        topic = event.get("t")
        if topic == "app.state":
            app = event.get("app")
            if app in self.view:
                self.view[app] = event.get("view") or "closed"
            if app == "terminal" and self.view["terminal"] == "closed" and event.get("job") is None:
                self.status = None if not self.status or self.status.get("phase") in ACTIVE else self.status
            self.hands_up = True
        elif topic == "term.status":
            if event.get("phase") == "forgotten":
                self.status = None
                self.text = None
            elif event.get("phase") is not None:
                self.status = dict(event)
                if event.get("speakable"):
                    line = status_line(event)
                    if line:
                        self._announce.append(line)
        elif topic == "term.text":
            self.text = dict(event)
        elif topic in ("organ.up", "organ.down") and event.get("name") == "hands":
            self.hands_up = topic == "organ.up"
            if not self.hands_up:
                self.view = {app: "closed" for app in self.view}
                self.status = None
        key = event.get("req")
        waiter = self._waiters.get((key, topic)) if key is not None else None
        if waiter is not None and not waiter.done():
            waiter.set_result(dict(event))

    def context(self, guest=None):
        if self.pending and self.clock() > self.pending_until:
            self.pending = ""
        status = self.status or {}
        return AppContext(
            enabled=bool(self.enabled()), guest=self.guest if guest is None else guest, hands_up=self.hands_up,
            terminal=self.view["terminal"], job=status.get("phase"), job_source=status.get("source") or "",
            pending=self.pending, shelf=self.shelf, monitor=self.view["monitor"], clock=self.view["clock"],
        )

    def parse(self, text, guest=None):
        return parse(text, self.context(guest), self.name)

    def may_be_command(self, text, guest=None):
        """The speculative-read gate with what is really on screen: a bare
        "stop" is ours only while the terminal is open or a job exists."""
        return may_be_app_command(text, self.context(guest), self.name)

    @property
    def job_holds_model(self):
        return bool(self.status) and self.status.get("phase") in GENERATING and self.status.get("source") == "model"

    def model_note(self):
        """A short note for a model turn during a job, or ''."""
        status = self.status or {}
        if status.get("phase") in ACTIVE:
            what = status.get("describe") or "a script"
            return f"(You are writing a script in the terminal: {what}.)"
        return ""

    def announcement(self):
        """The next line to announce (through mind.announce), or None."""
        return self._announce.pop(0) if self._announce else None

    def episode(self):
        """The one-line episode for memory when a script was written: text and
        metadata. Never the code or the output. None in guest mode, and None
        once it has been handed out for this file and hash (record it once).

        After recording, the mind calls note_episode(episode_id, meta) so a
        later "forget that script" can redact it (forgotten_episodes())."""
        status = self.status or {}
        if self.guest or status.get("phase") != "waiting" or not status.get("file"):
            return None
        key = (status["file"], status.get("sha256"))
        if key in self._episoded:
            return None
        self._episoded.add(key)
        meta = {"file": status["file"], "sha256": status.get("sha256"), "job": status.get("id")}
        return f"I wrote {status['file']} in the terminal", meta

    def note_episode(self, episode_id, meta):
        """The mind recorded the episode under this id (memory.log's return)."""
        if episode_id is not None and isinstance(meta, dict):
            self._episode_ids.append((episode_id, meta.get("job"), meta.get("file")))

    def _forget_episodes(self, status):
        job = (status or {}).get("id")
        if not job:
            return
        jobs = {job, job[:-1] if status.get("fixed") and job.endswith("f") else job}
        keep = []
        for episode_id, episode_job, file in self._episode_ids:
            (self._to_forget if episode_job in jobs else keep).append((episode_id, episode_job, file))
        self._episode_ids = keep

    def forgotten_episodes(self):
        """Episode ids the mind must redact (memory.edit(episode_ids=...)),
        after the owner confirmed "forget that script". Each id is given once."""
        ids = [episode_id for episode_id, _, _ in self._to_forget]
        self._to_forget = []
        return ids

    # -- outgoing --------------------------------------------------------------------------

    async def _ask(self, topic, wait_for, wait=REPLY_WAIT, **fields):
        req = uuid.uuid4().hex[:12]
        futures = {}
        loop = asyncio.get_running_loop()
        for name in wait_for:
            futures[name] = self._waiters[(req, name)] = loop.create_future()
        if topic == "term.task":
            fields["id"] = req      # the job is named after its request
        await self.bus.send(topic, req=req, **fields)
        try:
            done, _ = await asyncio.wait(list(futures.values()), timeout=wait,
                                         return_when=asyncio.FIRST_COMPLETED)
            return next(iter(done)).result() if done else None
        finally:
            for name in wait_for:
                self._waiters.pop((req, name), None)

    async def handle(self, command, say):
        kind = command.kind
        if kind == "say":
            await say(command.say)
            return
        if kind == "answer":
            await self._answer(command.action == "yes", say)
            return
        self.pending = ""
        if kind == "open":
            was = self.view.get(command.app)
            reply = await self._ask("app.open", ("app.state", "app.refused"), OPEN_WAIT, app=command.app)
            await say(self._open_line(reply, was))
        elif kind == "restore":
            reply = await self._ask("app.restore", ("app.state", "app.refused"), app=command.app)
            await say(REPLIES["restored"] if reply and reply.get("view") == "shown" else REPLIES["open_failed"])
        elif kind == "minimise":
            reply = await self._ask("app.minimise", ("app.state", "app.refused"), app=command.app)
            writing = (self.status or {}).get("phase") in GENERATING
            small = command.app in SMALL_APPS
            if reply and (reply.get("view") == "minimised" or (small and reply.get("view") == "closed")):
                await say(REPLIES["minimised_writing"] if writing and not small else REPLIES["minimised"])
            else:
                await say(REPLIES[f"{command.app}_not_open"] if small else REPLIES["not_open"])
        elif kind == "close":
            reply = await self._ask("app.close", ("app.state", "app.refused"), app=command.app)
            await self._close_line(reply, say, command.app)
        elif kind == "task":
            fields = dict(kind=command.task, text=command.text, request=command.request, recipe=command.recipe or None,
                          slots=dict(command.slots), force_model=command.force_model)
            reply = await self._ask("term.task", ("term.status", "app.refused"), OPEN_WAIT, **fields)
            if reply is None:
                await say(REPLIES["open_failed"])
            elif reply.get("t") == "app.refused":
                await say(self._refused_line(reply))
            elif command.say:
                # The offline weather note, said only once the job has started:
                # "Here's how warm I am inside" must be true.
                await say(command.say)
            else:
                await say(REPLIES["recipe"] if reply.get("source") == "recipe" else REPLIES["model"])
        elif kind in ("install", "uninstall", "run_app"):
            reply = await self._ask("term.task", ("term.status", "app.refused"), OPEN_WAIT, kind=command.kind,
                                    text=command.text, app=command.program)
            if reply is None:
                await say(REPLIES["open_failed"])
            elif reply.get("t") == "app.refused":
                await say(self._refused_line(reply))
            else:
                verb = {"install": "Installing", "uninstall": "Uninstalling", "run_app": "Starting"}[kind]
                await say(f"{verb} {command.program}." + (" It's from my built-in shelf." if kind == "install" else ""))
        elif kind == "shelf_list":
            names = sorted(set(self.shelf.values()))
            if names:
                listed = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]
                await say(f"I can't download anything, but my shelf has {listed}. Say install and a name.")
            else:
                await say(REPLIES["no_download"])
        elif kind == "control":
            await self._control(command.action, say)
        elif kind == "report":
            await say(report(self.status, self.text))
        elif kind == "read_line":
            reply = await self._ask("term.read", ("term.text",), id=(self.status or {}).get("id"), what="last_line")
            await say(read_line(reply))
        elif kind == "forget_script":
            self.pending, self.pending_until = "forget", self.clock() + CONFIRM_SECONDS
            await say(REPLIES["forget_ask"])
        elif kind == "clear_workshop":
            self.pending, self.pending_until = "clear", self.clock() + CONFIRM_SECONDS
            await say(REPLIES["clear_ask"])

    def _open_line(self, reply, was):
        if reply is None:
            return REPLIES["open_failed"]
        if reply.get("t") == "app.state" and reply.get("view") == "shown":
            app = reply.get("app")
            if app in SMALL_APPS:
                return REPLIES[f"open_{app}"]
            return REPLIES["restored"] if was == "minimised" else REPLIES["open"]
        return self._refused_line(reply)

    def _refused_line(self, reply):
        reason = reply.get("reason")
        if reason == "bad_slot":
            low, high = reply.get("low"), reply.get("high")
            if isinstance(low, int) and isinstance(high, int):
                return f"My recipe for that takes a number from {number_words(low)} to {number_words(high)}."
            return REPLIES["bad_slot"]
        if reason == "guest" and reply.get("app") in SMALL_APPS:
            return REPLIES[f"guest_{reply['app']}"]
        return {
            "disabled": REPLIES["disabled"], "guest": REPLIES["guest"], "busy": REPLIES["busy"],
            "unavailable": REPLIES["unavailable"], "lid_closed": REPLIES["lid_closed"],
            "too_warm": REPLIES["too_warm"], "low_battery": REPLIES["low_battery"],
            "no_session": REPLIES["not_open"],
        }.get(reason, REPLIES["open_failed"])

    async def _close_line(self, reply, say, app="terminal"):
        if reply is not None and reply.get("reason") == "no_session" and app in SMALL_APPS:
            await say(REPLIES[f"{app}_not_open"])
        elif reply is None:
            await say(REPLIES["open_failed"].replace("open", "close"))
        elif reply.get("t") == "app.state" and reply.get("view") == "closed":
            await say(REPLIES["closed"])
        elif reply.get("reason") == "busy":
            self.pending, self.pending_until = "close", self.clock() + CONFIRM_SECONDS
            await say(REPLIES["close_busy"])
        else:
            await say(self._refused_line(reply))

    async def _answer(self, yes, say):
        pending, self.pending = self.pending, ""
        if pending == "close":
            if not yes:
                await say(REPLIES["kept_open"])
                return
            reply = await self._ask("app.close", ("app.state", "app.refused"), app="terminal", force=True)
            await self._close_line(reply, say)
        elif pending in ("forget", "forget_copies", "clear"):
            if not yes:
                await say(REPLIES["kept"])
                return
            status = self.status or {}
            if pending == "clear":
                fields = {"all": True}
            elif pending == "forget_copies":
                fields = {"files": list(self.pending_files)}
            else:
                fields = {}
            job = status.get("id") if pending != "clear" else None
            req = uuid.uuid4().hex[:12]
            loop = asyncio.get_running_loop()
            waiter = self._waiters[(req, "term.status")] = loop.create_future()
            await self.bus.send("term.forget", id=job or req, req=req, **fields)
            try:
                reply = await asyncio.wait_for(waiter, REPLY_WAIT)
            except asyncio.TimeoutError:
                reply = None
            finally:
                self._waiters.pop((req, "term.status"), None)
            changed = (reply or {}).get("changed") or []
            forgotten = reply is not None and reply.get("phase") == "forgotten"
            if forgotten and pending == "forget":
                # The one-line episode about this script goes too (real
                # forgetting): the mind redacts these episode ids.
                self._forget_episodes(status)
            if pending == "clear":
                await say(REPLIES["cleared"] if forgotten else REPLIES["open_failed"].replace("open", "clear"))
            elif pending == "forget_copies":
                await say(REPLIES["copies_forgotten"] if forgotten and reply.get("removed")
                          else "I couldn't remove those copies." if forgotten
                          else "I couldn't reach my hands to forget them.")
            elif not forgotten:
                await say("I don't have a script to forget." if reply is not None
                          else "I couldn't reach my hands to forget it.")
            elif changed and pending == "forget":
                self.pending, self.pending_until = "forget_copies", self.clock() + CONFIRM_SECONDS
                self.pending_files = tuple(changed)
                names = ", ".join(changed[:3])
                await say(f"That script is forgotten. I also found changed copies: {names}. Should I remove those too?")
            else:
                await say(REPLIES["forgotten"])

    async def _control(self, action, say):
        status = self.status or {}
        reply = await self._ask("term.control", ("term.status",), id=status.get("id"), action=action, reason="owner")
        phase = (reply or {}).get("phase")
        if reply is None:
            await say("My hands didn't answer.")
        elif action == "pause":
            if phase == "cancelled":
                # "Stop" during the countdown: the line was cleared, nothing ran.
                await say(REPLIES["countdown_stopped"])
            elif phase in ("paused", "frozen"):
                first = not self.paused_once
                self.paused_once = True
                await say(REPLIES["paused_first"] if first else REPLIES["paused"])
            else:
                await say(report(reply, self.text))
        elif action == "resume":
            if phase in ("paused", "frozen") and reply.get("reason") in ("lid_closed", "asleep", "too_warm"):
                await say(REPLIES[reply["reason"]])
            else:
                await say(REPLIES["resumed"] if phase in ("reading", "writing", "running") else report(reply, self.text))
        elif action == "cancel":
            await say(REPLIES["cancelled"] if phase == "cancelled" else report(reply, self.text))
        elif action == "undo":
            await say(REPLIES["undone"] if reply and reply.get("reason") != "no_undo" else REPLIES["no_undo"])
        elif action == "run":
            if reply and reply.get("phase") == "running":
                await say(REPLIES["running"])
            elif reply and reply.get("reason") in ("lid_closed", "asleep", "too_warm", "low_battery"):
                await say(REPLIES[reply["reason"]])
            elif reply and reply.get("reason") == "not_at_prompt":
                await say("Something else is running in my terminal, so I didn't start it.")
            elif status.get("phase") in ("reading", "writing", "held", "checking"):
                await say("It isn't finished yet.")
            else:
                await say("There's nothing ready to run.")
        elif action == "fix":
            if reply and reply.get("reason") == "no_fix":
                await say(REPLIES["no_fix"])
            else:
                await say(REPLIES["model"])

    # -- the model and privacy -------------------------------------------------------------

    async def lease(self):
        """Before a conversation turn that needs the model: park the code job
        (it must never take the model from the conversation). Waits up to 0.5 s."""
        if not self.job_holds_model:
            return False
        self._leased = True
        req = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        waiter = self._waiters[(req, "term.status")] = loop.create_future()
        await self.bus.send("term.control", id=self.status.get("id"), req=req, action="pause", reason="turn")
        try:
            await asyncio.wait_for(asyncio.shield(waiter), LEASE_WAIT)
        except asyncio.TimeoutError:
            pass
        finally:
            self._waiters.pop((req, "term.status"), None)
        return True

    async def unlease(self):
        if self._leased:
            self._leased = False
            await self.bus.send("term.control", id=(self.status or {}).get("id"), action="resume", reason="turn")

    async def privacy(self, guest):
        """Guest mode changed: hands closes the session and clears the workshop."""
        self.guest = guest
        self.status = None
        self.text = None
        self.pending = ""
        self._announce.clear()
        await self.bus.send("app.privacy", guest=guest)
