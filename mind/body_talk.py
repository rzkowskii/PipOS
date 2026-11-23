"""How Pip's body is, in plain words: battery health, warmth, fan, disk,
working memory, uptime and hearing. Code answers only, from measured
numbers: `ctx.body` (body.state from the brainstem plus body.care from
bodycare), `ctx.voice` (voice.level) and the kernel's own files. When a
number is missing, Pip says it cannot tell; it never guesses.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from .reply import Confirm, Reply, clean as _clean
except ImportError:  # the everyday skills' types, as frozen in the spec (5.2)
    _clean = None

    @dataclass(frozen=True)
    class Reply:
        line: str
        keep: bool = False
        confirm: Any = None
        action: str | None = None
        logged: str | None = None

    @dataclass(frozen=True)
    class Confirm:
        on_yes: Callable[[], Awaitable[Reply]]
        on_no: Reply = Reply("[neutral] Okay, I've left it.")
        seconds: float = 15.0


def said(text, ctx=None):
    """What was said, tidied as every code skill sees it ("hey Pip", "please" off)."""
    text = text or ""
    if _clean is not None:
        try:
            text = _clean(text, getattr(ctx, "name", None) or "pip")
        except Exception:  # the raw words still match
            pass
    return text.strip()


def is_guest(ctx):
    return bool(getattr(ctx, "guest", False)) or getattr(ctx, "speaker", None) == "guest"


PROC = Path("/proc")
NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
# Hearing, from voice.level (dBFS). A floor this low is digital silence.
SILENT_FLOOR = -90.0
NOISY_FLOOR = -40.0
QUIET_FLOOR = -55.0
CLEAR_PEAK = -30.0
FAINT_PEAK = -42.0
# Warmth, package or core degrees C.
COOL_BELOW = 55.0
HOT_FROM = 75.0
VERY_HOT_FROM = 90.0
LOW_DISK_SHARE = 0.10

_LEAD = r"^(?:(?:hey|hi|okay|ok)\s+pip\W*\s*)?(?:pip\W*\s*)?(?:so\s+|and\s+|please\s+)?(?:tell\s+me\s+)?"
_END = r"(?:\s+(?:pip|please|now|right\s+now|at\s+the\s+moment|today))*\s*[?.!]*$"
_YOUR = r"(?:your|ur)"

PATTERNS = (
    ("health", _LEAD + r"(?:how(?:['’]s|\s+is)\s+" + _YOUR + r"\s+battery(?:['’]s)?\s+(?:health|wear|condition|holding\s+up)"
     r"|what(?:['’]s|\s+is)\s+" + _YOUR + r"\s+battery(?:['’]s)?\s+(?:health|wear|condition)"
     r"|how\s+(?:healthy|worn(?:\s+out)?|old|good)\s+is\s+" + _YOUR + r"\s+battery"
     r"|(?:check\s+)?" + _YOUR + r"\s+battery\s+health"
     r"|how\s+many\s+(?:charge\s+)?cycles(?:\s+(?:does|has)\s+" + _YOUR + r"\s+battery(?:\s+(?:done|had|got))?"
     r"|\s+(?:have|has)\s+(?:you|it)(?:\s+(?:done|had))?)?"
     r"|battery\s+health)" + _END),
    ("warmth", _LEAD + r"(?:how\s+(?:warm|hot|cold|cool)\s+are\s+you|are\s+you\s+(?:too\s+|getting\s+|running\s+)?(?:hot|warm|overheating|cold)"
     r"|what(?:['’]s|\s+is)\s+" + _YOUR + r"\s+(?:temperature|temp)|how(?:['’]s|\s+is)\s+" + _YOUR + r"\s+(?:temperature|temp)"
     r"|(?:check\s+)?" + _YOUR + r"\s+temperature|are\s+you\s+overheating)" + _END),
    ("fan", _LEAD + r"(?:why\s+is\s+" + _YOUR + r"\s+fan\s+(?:so\s+)?(?:loud|noisy|on|running|spinning|going)"
     r"|why\s+(?:is|are)\s+(?:your|the)\s+fans?\s+(?:so\s+)?(?:loud|noisy)"
     r"|how\s+fast\s+is\s+" + _YOUR + r"\s+fan(?:\s+(?:going|spinning|running))?|is\s+" + _YOUR + r"\s+fan\s+(?:on|running)"
     r"|what(?:['’]s|\s+is)\s+" + _YOUR + r"\s+fan\s+(?:speed|doing)|how(?:['’]s|\s+is)\s+" + _YOUR + r"\s+fan"
     r"|(?:your\s+)?fan\s+speed)" + _END),
    ("disk", _LEAD + r"(?:how\s+much\s+(?:disk\s+|storage\s+|free\s+)?(?:space|storage|room)\s+(?:do\s+you\s+have|have\s+you\s+got|is\s+(?:left|free))(?:\s+left)?"
     r"|how\s+full\s+is\s+" + _YOUR + r"\s+(?:disk|storage|drive|memory\s+disk)|(?:how\s+much\s+)?free\s+(?:disk\s+)?space"
     r"|are\s+you\s+(?:running\s+)?(?:out\s+of|low\s+on)\s+(?:space|storage|disk\s+space)"
     r"|how(?:['’]s|\s+is)\s+" + _YOUR + r"\s+(?:disk|storage))" + _END),
    ("memory", _LEAD + r"(?:how\s+much\s+(?:working\s+)?(?:memory|ram)\s+are\s+you\s+using|how\s+much\s+ram\s+(?:do\s+you\s+have|is\s+(?:free|used|left))"
     r"|how\s+much\s+(?:memory|ram)\s+is\s+(?:free|used|left)|how\s+much\s+working\s+memory(?:\s+do\s+you\s+have)?"
     r"|what(?:['’]s|\s+is)\s+" + _YOUR + r"\s+(?:ram|memory)\s+(?:use|usage)|(?:ram|memory)\s+usage)" + _END),
    ("uptime", _LEAD + r"(?:how\s+long\s+have\s+you\s+been\s+(?:awake|up|on|running|switched\s+on|going)"
     r"|how\s+long\s+since\s+you\s+(?:last\s+)?(?:started|restarted|woke\s+up|booted)|when\s+did\s+you\s+(?:last\s+)?(?:start|restart|boot)"
     r"|what(?:['’]s|\s+is)\s+" + _YOUR + r"\s+uptime|uptime)" + _END),
    ("hearing", _LEAD + r"(?:how\s+well\s+can\s+you\s+hear\s+me|(?:can\s+you\s+)?hear\s+me\s+(?:well|clearly|okay|ok|properly|alright)"
     r"|check\s+" + _YOUR + r"\s+(?:hearing|ears|microphone|mic)|how(?:['’]s|\s+is)\s+" + _YOUR + r"\s+(?:hearing|microphone|mic)"
     r"|how\s+(?:good|clear)\s+is\s+" + _YOUR + r"\s+(?:hearing|microphone|mic)|how\s+loud\s+am\s+i"
     r"|am\s+i\s+(?:too\s+)?(?:loud|quiet)\s+enough|am\s+i\s+(?:too\s+)?(?:quiet|far\s+away))" + _END),
)
COMPILED = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in PATTERNS)
_HINT = re.compile(
    r"\b(?:battery|cycles|warm|hot|cold|cool|overheating|temperature|temp|fans?|space|storage|disk|full|ram|memory|uptime"
    r"|awake|running|started|restarted|booted|hear|hearing|ears|mic|microphone|loud|quiet)\b",
    re.IGNORECASE,
)


def may_match(text):
    return bool(_HINT.search(text or "")) and classify(text) is not None


def classify(text, ctx=None):
    text = said(text, ctx)
    for name, pattern in COMPILED:
        if pattern.match(text):
            return name
    return None


def number(value, low=None, high=None):
    if type(value) not in (int, float) or value != value:
        return None
    if (low is not None and value < low) or (high is not None and value > high):
        return None
    return float(value)


def count_word(n):
    n = int(n)
    return NUMBER_WORDS[n] if 0 <= n < len(NUMBER_WORDS) else f"{n:,}"


def plural(n, word):
    return f"{count_word(n)} {word}{'' if n == 1 else 's'}"


# --- battery health -----------------------------------------------------------

def health_line(body):
    health = number(body.get("health_pct"), 1, 100)
    cycles = number(body.get("cycles"), 1, 100_000)
    if health is None:
        return "[neutral] I can't read my battery's health right now."
    health = round(health)
    line = f"My battery holds {health} percent of what it did when new"
    if cycles is not None:
        line += f", after {int(cycles):,} charge cycle{'' if int(cycles) == 1 else 's'}."
    else:
        line += "."
    limit = number(body.get("charge_limit"), 50, 100)
    if body.get("cap_honoured") is True and limit is not None:
        line += f" I'm keeping it at {round(limit)} percent."
    elif health < 60:
        line += " It may be time for a new one."
    return ("[concern] " if health < 60 else "[neutral] ") + line


# --- warmth and fan -----------------------------------------------------------

def degrees(body):
    """The package temperature from bodycare, or the hottest core."""
    return number(body.get("package_c"), 0, 125) or number(body.get("temp"), 0, 125)


def slowing(body):
    perf = number(body.get("max_perf_pct"), 0, 100)
    return body.get("mode") == "act" and ((perf is not None and perf < 100) or body.get("no_turbo") is True)


def warmth_line(body):
    temp = degrees(body)
    if temp is None:
        return "[neutral] I can't read my temperature right now."
    shown = round(temp)
    if temp < COOL_BELOW:
        return f"[neutral] I'm cool: {shown} degrees inside."
    if temp < HOT_FROM:
        return f"[neutral] I'm warm, {shown} degrees inside, which is fine for me."
    if temp < VERY_HOT_FROM:
        tail = " I'm slowing down a little to cool off." if slowing(body) else " My fan is working on it."
        return f"[concern] I'm hot: {shown} degrees inside.{tail}"
    return f"[concern] I'm very hot: {shown} degrees inside. Please give my vents some room."


def fan_line(body):
    rpm = number(body.get("fan_rpm"), 0, 20_000)
    if rpm is None:
        return "[neutral] I can't read my fan right now."
    rpm = int(round(rpm, -1))
    factory = number(body.get("fan_factory_min"), 0, 20_000)
    floor = number(body.get("fan_floor"), 0, 20_000)
    temp = degrees(body)
    if rpm < 100:
        return "[neutral] My fan is stopped. I'm cool enough not to need it." if temp is not None and temp < HOT_FROM \
            else "[neutral] My fan reads as stopped."
    if body.get("mode") == "act" and floor is not None and factory is not None and floor > factory + 100:
        return f"[neutral] It's at {rpm:,} rpm. I'm holding it faster to keep my chip cool."
    if temp is not None and temp >= 70:
        return f"[neutral] It's at {rpm:,} rpm because I'm {round(temp)} degrees inside. It will slow when I cool."
    if factory is not None and rpm <= factory + 300:
        return f"[neutral] My fan is at its quiet minimum, about {rpm:,} rpm."
    if temp is not None:
        return f"[neutral] It's at {rpm:,} rpm, and I'm {round(temp)} degrees inside, so it should settle soon."
    return f"[neutral] My fan is at {rpm:,} rpm."


# --- disk, memory and uptime ----------------------------------------------------

def size_words(n_bytes):
    gigabytes = n_bytes / 1e9
    if gigabytes >= 10:
        return f"{round(gigabytes)} gigabytes"
    if gigabytes >= 1:
        return f"{gigabytes:.1f} gigabytes".replace(".0 ", " ")
    return f"{max(1, round(n_bytes / 1e6))} megabytes"


def disk_line(state_dir, usage=shutil.disk_usage):
    try:
        total, _used, free = usage(state_dir)
    except OSError:
        return "[neutral] I can't read my disk right now."
    if total <= 0:
        return "[neutral] I can't read my disk right now."
    line = f"I have {size_words(free)} free on my memory disk, out of {size_words(total)}."
    if free / total < LOW_DISK_SHARE:
        return f"[concern] {line} That's getting low; I'll keep only what matters."
    return f"[neutral] {line}"


def meminfo(proc=PROC):
    values = {}
    try:
        text = (Path(proc) / "meminfo").read_text(encoding="ascii", errors="replace")[:16384]
    except OSError:
        return values
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name.strip()] = int(parts[0]) * 1024
    return values


def memory_line(proc=PROC):
    info = meminfo(proc)
    total, available = info.get("MemTotal"), info.get("MemAvailable")
    if not total or available is None or available > total:
        return "[neutral] I can't read my working memory right now."
    used = total - available
    return (f"[neutral] I'm using {size_words(used)} of my {round(total / 2**30)} gigabytes of working memory.")


def uptime_seconds(proc=PROC):
    try:
        first = (Path(proc) / "uptime").read_text(encoding="ascii")[:128].split()[0]
        seconds = float(first)
    except (OSError, IndexError, ValueError):
        return None
    return seconds if 0 <= seconds < 10 * 365 * 86400 else None


def duration_words(seconds):
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return plural(days, "day") + (f" and {plural(hours, 'hour')}" if hours else "")
    if hours:
        return plural(hours, "hour") + (f" and {plural(minutes, 'minute')}" if minutes else "")
    if minutes:
        return plural(minutes, "minute")
    return "less than a minute"


def uptime_line(proc=PROC):
    seconds = uptime_seconds(proc)
    if seconds is None:
        return "[neutral] I can't tell how long I've been running."
    return f"[neutral] I've been running for {duration_words(seconds)}, since I last started."


# --- hearing -------------------------------------------------------------------

def hearing_line(voice):
    floor = number(voice.get("floor_dbfs"), -200, 10)
    peak = number(voice.get("speech_peak_dbfs"), -200, 10)
    if floor is None:
        return "[neutral] I haven't measured my hearing yet. Ask me again in half a minute."
    if floor <= SILENT_FLOOR:
        return ("[concern] My microphone is giving me silence. Say 'run a check-up' and I'll try to fix it, "
                "or type to me: press F2, then 6.")
    if floor > NOISY_FLOOR:
        return "[concern] The room is noisy for me, so I may miss words. Coming a little closer would help."
    if peak is None:
        quiet = "The room is quiet" if floor <= QUIET_FLOOR else "The room is a little noisy"
        return f"[neutral] {quiet}, but I haven't measured your voice yet. Say something and ask me again."
    gap = peak - floor
    if peak >= CLEAR_PEAK and gap >= 20:
        room = "The room is quiet" if floor <= QUIET_FLOOR else "There's some noise"
        return f"[joy] {room} and your voice reaches me clearly."
    if peak < FAINT_PEAK or gap < 10:
        return "[concern] Your voice reaches me faintly. Speaking up or coming closer would help."
    return "[neutral] I can hear you, though not perfectly. A little closer would help."


# --- the handler -----------------------------------------------------------------

async def handle(text, ctx):
    kind = classify(text, ctx)
    if kind is None:
        return None
    body = dict(getattr(ctx, "body", None) or {})
    if kind == "health":
        return Reply(health_line(body))
    if kind == "warmth":
        return Reply(warmth_line(body))
    if kind == "fan":
        return Reply(fan_line(body))
    if kind == "disk":
        return Reply(disk_line(ctx.state_dir))
    if kind == "memory":
        return Reply(memory_line())
    if kind == "uptime":
        return Reply(uptime_line())
    # Ask the voice for fresh levels for next time; answer from what is known now.
    send = getattr(ctx, "send", None)
    if send is not None:
        try:
            await send("voice.level_request")
        except Exception:  # a lost bus must not lose the answer
            pass
    return Reply(hearing_line(dict(getattr(ctx, "voice", None) or {})))
