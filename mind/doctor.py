"""The check-up: "are you okay?", "run a check-up", "what's wrong?".

Every check reads a measured fact, through a `System` that tests replace:

- organs: each unit's state and NRestarts (`systemctl show`), and failed units;
- the cortex: answering at all, and the last turn's prompt-reading speed
  against the 22.5 tok/s measured on the laptop (23 November);
- hearing: the microphone floor and how long since speech was understood;
- the memory disk's free space, and `PRAGMA quick_check` of each database;
- backup age, battery, temperature, recent overnight power and the clock.

Pip says at most three findings, worst first. The Care Health page shows
every row. Safe repairs only, each logged in the action log:

- restart one stopped organ per check-up (never the mind itself, never the
  cortex while a turn or the workshop uses it), at most once in ten minutes.
  An organ that is off on purpose (the eyes with the camera switched off, a
  unit stopped cleanly) is not stopped and is never started;
- re-run being-audio-init when the microphone floor is at or below -90 dBFS
  (digital silence: the mixer came up muted), never while muted or behind a
  closed lid, at most three times in one run of the mind.

`self_care` is the quiet automatic pass: it reads only the organs and the
voice's levels, so running it every few minutes costs one systemctl call.
"""

import asyncio
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from . import action_log
from .body_talk import Reply, degrees, is_guest, number, said
from .reply import Confirm, clean

BASELINE_TPS = 22.5          # measured prompt reading on the laptop
SLOW_SHARE = 0.6             # below 60% of it is worth saying
SILENT_FLOOR = -90.0
LOW_DISK = 0.10
VERY_LOW_DISK = 0.05
BACKUP_STALE_DAYS = 14
HOT_C = 80.0
VERY_HOT_C = 90.0
WORN_BATTERY = 60
REPAIR_GAP_S = 600
# A mixer that stays silent after this many resets is not fixed by more.
AUDIO_RESETS_PER_START = 3
# A prompt this short is mostly fixed cost, not reading speed: not judged.
MIN_JUDGED_PROMPT = 16
MOST_SPOKEN = 3
AUDIO_INIT = Path("/usr/libexec/being/being-audio-init")
AUDIO_UNIT = "being-audio-init.service"
DATABASES = {"mind.db": "memory", "lists.db": "lists", "reminders.db": "reminders"}
# "Are you okay?" waits for the check-up, and a full quick_check reads the
# whole archive, which grows for years. By voice each database gets this long;
# the Care page's Health check (F2) reads every page.
VOICE_CHECK_S = 1.5
# A check stopped at its time limit: neither good nor damaged.
UNFINISHED = "unfinished"
# Organs, as Pip names them. The mind is checked but never restarted by itself.
ORGANS = {
    "being-brainstem.service": "brainstem",
    "being-face.service": "face",
    "being-ears.service": "microphone capture",
    "being-voice.service": "ears and voice",
    "being-cortex.service": "thinking model",
    "being-eyes.service": "eyes",
    "being-bodycare.service": "body care",
    "being-hands.service": "hands",
    "being-mind.service": "mind",
}
NEVER_RESTART = {"being-mind.service"}
MANUAL_RESTARTS = {
    "face": "being-face.service",
    "ears": "being-ears.service",
    "microphone": "being-ears.service",
    "ears and voice": "being-voice.service",
    "voice": "being-voice.service",
    "thinking model": "being-cortex.service",
    "body care": "being-bodycare.service",
    "hands": "being-hands.service",
}
_MANUAL_NAMES = (*MANUAL_RESTARTS, "mind", "brainstem", "camera", "eyes")
_MANUAL_RESTART = re.compile(r"^restart (?:your |the )?(?P<organ>"
                             + "|".join(re.escape(name) for name in _MANUAL_NAMES) + r")$")
BAD, WARN, NOTE, OK = 3, 2, 1, 0

# Repairs made in this run of the mind (RAM only): unit or "audio" -> time.
_repaired = {}
_restart_lock = threading.Lock()
# The last turn's speed, told by the mind after each model reply (RAM only).
_last_speed = {}


def note_turn(prompt_tps=None, decode_tps=None, prompt_n=None):
    """Record the last model turn's measured speeds, tokens per second.

    `prompt_n` is how many prompt tokens were read. The 22.5 tok/s baseline
    was measured over 32-token batches; a warm turn that reads only a few new
    tokens is mostly fixed cost, so its prompt speed is not kept.
    """
    if type(prompt_n) is int and prompt_n < MIN_JUDGED_PROMPT:
        prompt_tps = None
    for key, value in (("prompt", prompt_tps), ("decode", decode_tps)):
        value = number(value, 0.01, 10_000)
        if value is not None:
            _last_speed[key] = value


@dataclass(frozen=True)
class Finding:
    severity: int
    area: str
    line: str        # spoken: one short sentence, no tag
    row: str         # the Care Health page
    repair: str | None = None   # a unit to restart, or "audio"


class System:
    """The real machine. Each method answers None when it cannot tell."""

    def __init__(self, systemctl="systemctl", cortex_port=8080):
        self.systemctl = shutil.which(systemctl)
        self.cortex_port = cortex_port

    def _run(self, argv, timeout=10):
        try:
            return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return None

    def units(self, names, *, timeout=10):
        if not self.systemctl:
            return None
        result = self._run([self.systemctl, "show",
                            "--property=Id,LoadState,ActiveState,SubState,NRestarts,Result,ConditionResult", *names],
                           timeout=timeout)
        if result is None or result.returncode:
            return None
        found = {}
        for block in result.stdout.strip().split("\n\n"):
            values = dict(line.partition("=")[::2] for line in block.splitlines() if "=" in line)
            if values.get("Id") in names:
                found[values["Id"]] = values
        return found

    def failed_units(self):
        if not self.systemctl:
            return None
        result = self._run([self.systemctl, "list-units", "--failed", "--plain", "--no-legend", "--no-pager"])
        if result is None or result.returncode:
            return None
        return [line.split()[0] for line in result.stdout.splitlines() if line.split()]

    def kernel_faults(self):
        """Count a few fixed, recent kernel messages without publishing their text."""
        result = self._run(("journalctl", "-k", "-b", "-n", "250", "-o", "cat", "--no-pager"),
                           timeout=4)
        if result is None or result.returncode:
            return {}
        patterns = {
            "disk": re.compile(r"^(?:ata[0-9]+: (?:hard resetting link|COMRESET failed)|I/O error, dev )"),
            "filesystem": re.compile(r"^(?:EXT4-fs error \(|BTRFS error \(|FAT-fs \([^)]+\): error)"),
            "machine": re.compile(r"^(?:mce: \[Hardware Error\]|Machine check events logged)"),
        }
        lines = result.stdout.splitlines()
        return {kind: sum(bool(pattern.search(line)) for line in lines)
                for kind, pattern in patterns.items()}

    def microphone_latched(self):
        return Path("/run/being/mic-policy-breach").is_file()

    def restart(self, unit):
        if not self.systemctl or (
            unit in ("being-ears.service", "being-voice.service") and self.microphone_latched()
        ):
            return False
        result = self._run([self.systemctl, "--no-block", "restart", unit], timeout=15)
        return result is not None and result.returncode == 0

    def audio_init(self):
        if not self.systemctl or self.microphone_latched() or not AUDIO_INIT.is_file():
            return False
        # Use the existing device-scoped unit. The Mind never opens ALSA.
        result = self._run([self.systemctl, "restart", AUDIO_UNIT], timeout=20)
        return result is not None and result.returncode == 0

    def disk(self, path):
        try:
            return shutil.disk_usage(path)
        except OSError:
            return None

    def quick_check(self, path, budget_s=None):
        """True, False (damaged) or None (absent or unreadable); UNFINISHED
        when `budget_s` ran out first (the voice's bounded check)."""
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            return None
        stopped = []
        try:
            with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)) as db:
                if budget_s is not None:
                    deadline = time.monotonic() + max(0.0, float(budget_s))

                    def over():
                        if time.monotonic() > deadline:
                            stopped.append(True)
                            return 1
                        return 0
                    db.set_progress_handler(over, 2000)
                return db.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
        except sqlite3.OperationalError:
            if stopped:
                return UNFINISHED
            # Busy, locked or not openable read-only: unknown, never "damaged".
            return None
        except sqlite3.DatabaseError:
            return False

    def uptime(self):
        try:
            return float(Path("/proc/uptime").read_text(encoding="ascii")[:64].split()[0])
        except (OSError, IndexError, ValueError):
            return None

    def cortex_ok(self):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.cortex_port, timeout=1)
        try:
            connection.request("GET", "/health")
            status = connection.getresponse().status
            # 503 is llama-server still loading its model: not an answer yet, not a fault.
            return True if status == 200 else None if status == 503 else False
        except ConnectionRefusedError:
            # Nothing listening: answering is False only where a cortex is meant to run.
            state = (self.units(["being-cortex.service"]) or {}).get("being-cortex.service") or {}
            return False if state.get("LoadState") == "loaded" and state.get("ActiveState") == "active" else None
        except (OSError, http.client.HTTPException):
            return None
        finally:
            connection.close()

    def last_backup(self, state_dir):
        from . import backup

        return backup.last_backup(state_dir)

    def continuity_warning(self):
        from .try_mode import continuity_warning_slot

        return continuity_warning_slot() is not None


@dataclass
class Facts:
    """What the mind knows that the machine does not: from the turn's context."""
    state_dir: Path
    body: dict = field(default_factory=dict)
    voice: dict = field(default_factory=dict)
    clock_trusted: bool = True
    cortex_busy: bool = False
    now: float | None = None
    # Seconds each database check may take; None reads every page (F2).
    check_budget_s: float | None = None

    @property
    def ears_closed(self):
        """Muted or behind a closed lid: a silent microphone is then expected."""
        return self.body.get("muted") is True or self.body.get("lid_closed") is True


def times(n):
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def plural_name(name):
    return name.endswith("s") or " and " in name


def stopped(state):
    """Failed, or ended badly: not a unit that is off on purpose.

    A unit whose start condition was not met (the eyes without the camera
    switched on) or that was stopped cleanly reads inactive with Result
    "success"; that is not a fault.
    """
    active = state.get("ActiveState")
    if active == "failed":
        return True
    if active != "inactive" or state.get("ConditionResult") == "no":
        return False
    return state.get("Result", "success") not in ("success", "")


def check_organs(system, facts):
    found = []
    states = system.units(list(ORGANS))
    if not states:
        return found
    for unit, name in ORGANS.items():
        state = states.get(unit)
        if not state or state.get("LoadState", "loaded") != "loaded":
            continue  # not part of this image (the lab, a Try stick without it)
        active = state.get("ActiveState")
        try:
            restarts = int(state.get("NRestarts") or 0)
        except ValueError:
            restarts = 0
        if stopped(state):
            repair = None if unit in NEVER_RESTART else unit
            verb = "have" if plural_name(name) else "has"
            found.append(Finding(BAD, "organs", f"My {name} {verb} stopped.", f"{name}: stopped ({active})", repair))
        elif active == "inactive":
            # Off on purpose: the camera not switched on (a start condition),
            # or stopped cleanly by the night shift. Never restarted.
            found.append(Finding(OK, "organs", "", f"{name}: off"))
        elif restarts:
            settled = "they're fine now" if plural_name(name) else "it's fine now"
            found.append(Finding(WARN if restarts >= 3 else NOTE, "organs",
                                 f"My {name} restarted {times(restarts)} since I started; {settled}.",
                                 f"{name}: running, {restarts} restarts"))
        else:
            found.append(Finding(OK, "organs", "", f"{name}: running"))
    return found


def check_failed(system, facts):
    failed = system.failed_units()
    if failed is None:
        return []
    others = [unit for unit in failed if unit not in ORGANS]
    if not others:
        return [Finding(OK, "units", "", "failed services: none")]
    short = others[0].removesuffix(".service")
    more = f" and {len(others) - 1} more" if len(others) > 1 else ""
    return [Finding(WARN, "units", f"A background service of mine failed: {short}{more}.",
                    f"failed services: {', '.join(others[:4])}")]


def check_cortex(system, facts):
    alive = system.cortex_ok()
    if alive is False:
        return [Finding(BAD, "cortex", "My thinking model isn't answering.", "thinking model: not answering")]
    speed = _last_speed.get("prompt")
    if speed is None:
        return [Finding(OK, "cortex", "", "thinking model: answering" if alive else "thinking model: not checked")]
    row = f"thinking model: reading {speed:.1f} tok/s (usual {BASELINE_TPS})"
    if speed < BASELINE_TPS * SLOW_SHARE:
        return [Finding(WARN, "cortex", f"My thinking is slow: {round(speed)} tokens a second, against my usual 22.", row)]
    return [Finding(OK, "cortex", "", row)]


def check_hearing(system, facts):
    latched = getattr(system, "microphone_latched", None)
    if latched and latched():
        return [Finding(BAD, "hearing", "My microphone stayed open after I closed it, so I requested an audio shutdown.",
                        "microphone privacy latch: reboot to recover")]
    floor = number(facts.voice.get("floor_dbfs"), -200, 10)
    heard = number(facts.voice.get("heard_ago_s"), 0, 10 * 365 * 86400)
    found = []
    overruns = facts.voice.get("capture_overruns")
    if type(overruns) is int and overruns > 0:
        found.append(Finding(WARN, "hearing", "My ears have been cutting out under load.",
                             f"microphone capture overruns: {overruns}"))
    if floor is None:
        found.append(Finding(OK, "hearing", "", "microphone: no reading yet"))
    elif floor <= SILENT_FLOOR and facts.ears_closed:
        found.append(Finding(OK, "hearing", "", f"microphone: floor {floor:.0f} dBFS (muted or lid closed)"))
    elif floor <= SILENT_FLOOR:
        found.append(Finding(BAD, "hearing", "My microphone is giving me silence.",
                             f"microphone: floor {floor:.0f} dBFS (silent)", "audio"))
    else:
        found.append(Finding(OK, "hearing", "", f"microphone: floor {floor:.0f} dBFS"))
    up = system.uptime()
    if heard is None and up is not None and up > 3600 and floor is not None and floor > SILENT_FLOOR:
        found.append(Finding(NOTE, "hearing", "I haven't understood any speech since I started.",
                             "last understood speech: not since starting"))
    elif heard is not None and heard > 86400:
        found.append(Finding(WARN, "hearing", "I haven't understood any speech for over a day.",
                             f"last understood speech: {heard / 3600:.0f} h ago"))
    elif heard is not None:
        found.append(Finding(OK, "hearing", "", f"last understood speech: {heard / 60:.0f} min ago"))
    return found


def check_disk(system, facts):
    usage = system.disk(facts.state_dir)
    if not usage or usage[0] <= 0:
        return []
    total, _, free = usage
    share = free / total
    row = f"memory disk: {free / 1e9:.1f} of {total / 1e9:.0f} GB free"
    if share < VERY_LOW_DISK:
        return [Finding(BAD, "disk", "My memory disk is nearly full.", row)]
    if share < LOW_DISK:
        return [Finding(WARN, "disk", "My memory disk is getting full.", row)]
    return [Finding(OK, "disk", "", row)]


def check_databases(system, facts):
    found = []
    for filename, name in DATABASES.items():
        path = Path(facts.state_dir) / filename
        budget = getattr(facts, "check_budget_s", None)
        verdict = system.quick_check(path) if budget is None else system.quick_check(path, budget_s=budget)
        if verdict == UNFINISHED:
            found.append(Finding(OK, "databases", "", f"{filename}: not checked in full (the Health page does)"))
        elif verdict is False:
            found.append(Finding(BAD, "databases", f"My {name} file is damaged.", f"{filename}: damaged"))
        elif verdict:
            found.append(Finding(OK, "databases", "", f"{filename}: ok"))
    return found


def check_continuity(system, facts):
    if system.continuity_warning():
        return [Finding(WARN, "continuity", "A startup check for my saved memory or thinking model needs attention.",
                        "startup continuity: check the archive and model slots")]
    return []


def check_backup(system, facts):
    stamp = system.last_backup(facts.state_dir)
    now = time.time() if facts.now is None else facts.now
    if stamp is None:
        return [Finding(NOTE, "backup", "I've never been backed up.", "last backup: never")]
    days = int((now - stamp) // 86400)
    row = f"last backup: {days} days ago" if days else "last backup: today"
    if days > BACKUP_STALE_DAYS:
        return [Finding(NOTE, "backup", f"My last backup was {days} days ago.", row)]
    return [Finding(OK, "backup", "", row)]


def check_battery(system, facts):
    body = facts.body
    found = []
    health = number(body.get("health_pct"), 1, 100)
    if health is not None:
        row = f"battery health: {round(health)}%"
        cycles = number(body.get("cycles"), 1, 100_000)
        if cycles is not None:
            row += f", {int(cycles)} cycles"
        if health < WORN_BATTERY:
            found.append(Finding(WARN, "battery", f"My battery is worn: it holds {round(health)} percent of new.", row))
        else:
            found.append(Finding(OK, "battery", "", row))
    if body.get("battery_low") is True:
        found.append(Finding(WARN, "battery", "My battery is low; please plug me in.", "battery: low"))
    if body.get("cap_honoured") is False:
        found.append(Finding(NOTE, "battery", "My battery keeps charging past its cap.", "charge cap: not holding"))
    return found


def check_temperature(system, facts):
    temp = degrees(facts.body)
    if temp is None:
        return []
    row = f"temperature: {round(temp)} C"
    if temp >= VERY_HOT_C:
        return [Finding(BAD, "temperature", f"I'm very hot: {round(temp)} degrees.", row)]
    if temp >= HOT_C:
        return [Finding(WARN, "temperature", f"I'm running hot: {round(temp)} degrees.", row)]
    return [Finding(OK, "temperature", "", row)]


def check_kernel_health(system, facts):
    found = []
    strain = number(facts.body.get("reflex_strain_pct"), 5, 100)
    if strain is not None:
        found.append(Finding(WARN, "reflex_pressure", "My face, hearing or speech has been waiting for the processor.",
                             f"reflex organs: sustained CPU stalls up to {strain:.1f}%"))
    throttles = facts.body.get("thermal_throttles")
    if type(throttles) is int and throttles > 0:
        found.append(Finding(WARN, "thermal_throttle", "I got hot enough to slow down since I started.",
                             f"processor package throttle events this boot: {throttles}"))
    killed = facts.body.get("oom_organs")
    if type(killed) is int and 0 < killed < 16:
        organs = ", ".join(label for index, label in enumerate(("face", "voice", "brainstem", "mind"))
                           if killed & (1 << index))
        found.append(Finding(BAD, "memory_pressure", f"I ran out of memory while running my {organs}.",
                             f"organs with kernel out-of-memory kills: {organs}"))
    reader = getattr(system, "kernel_faults", None)
    faults = reader() if reader else {}
    for kind, text in (("disk", "My kernel reported a disk reset or input-output error."),
                       ("filesystem", "My kernel reported a filesystem error."),
                       ("machine", "My processor reported a hardware error.")):
        if faults.get(kind):
            found.append(Finding(BAD, f"kernel_{kind}", text, f"recent kernel {kind} reports: {faults[kind]}"))
    return found


def check_overnight_power(system, facts):
    from .vitals import hardware_epoch, latest, package_power_trend

    if not facts.clock_trusted:
        return []
    vital = latest(facts.state_dir)
    if vital is None:
        return []
    night, _ = vital
    # Night labels use the owner's local date. Allow for the date boundary
    # between that zone and UTC when deciding whether the reading is recent.
    now = time.time() if facts.now is None else facts.now
    age = (datetime.fromtimestamp(now, UTC).date() - date.fromisoformat(night)).days
    if not -1 <= age <= 2:
        return []
    change = package_power_trend(facts.state_dir, night, expected_epoch=hardware_epoch())
    if change is None:
        return []
    direction = "higher" if change > 0 else "lower"
    watts = abs(change)
    return [Finding(NOTE, "overnight_power",
                    f"My median overnight processor power was {watts:.1f} watts {direction} "
                    "over the last seven measured nights than over the previous twenty-eight.",
                    f"overnight package power: {watts:.1f} W {direction} ({night})")]


def check_active_vitals(system, facts):
    from .vitals import active_trend, hardware_epoch, latest

    if not facts.clock_trusted:
        return []
    vital = latest(facts.state_dir)
    if vital is None:
        return []
    night, _ = vital
    now = time.time() if facts.now is None else facts.now
    age = (datetime.fromtimestamp(now, UTC).date() - date.fromisoformat(night)).days
    if not -1 <= age <= 2:
        return []
    changes = active_trend(facts.state_dir, night, expected_epoch=hardware_epoch())
    if not changes:
        return []
    if changes.get("peak_c", 0) >= 6:
        amount = round(changes["peak_c"])
        return [Finding(NOTE, "active_vitals",
                        f"For the same measured work I ran {amount} degrees hotter than a month ago; my vents may need cleaning.",
                        f"active workload: peak temperature {amount} C higher ({night})")]
    labels = (("read_tps", "reading", True), ("write_tps", "writing", True),
              ("disk_sync_ms", "disk writes", False), ("joules_per_token", "energy per token", False))
    for key, label, higher_is_better in labels:
        change = changes.get(key)
        if change is None:
            continue
        slower = change < 0 if higher_is_better else change > 0
        direction = "slower" if slower else "faster" if higher_is_better else "lower"
        return [Finding(NOTE, "active_vitals",
                        f"My measured {label} was {abs(change) * 100:.0f} percent {direction} than a month ago.",
                        f"active workload: {label} {abs(change) * 100:.0f}% {direction} ({night})")]
    return []


def check_smart(system, facts):
    """The latest weekly, no-wake SMART evidence for the disk holding the archive."""
    from .vitals import latest_smart

    if not facts.clock_trusted:
        return []
    sample = latest_smart(facts.state_dir)
    if sample is None:
        return []
    night, smart, delta = sample
    now = time.time() if facts.now is None else facts.now
    age = (datetime.fromtimestamp(now, UTC).date() - date.fromisoformat(night)).days
    if not -1 <= age <= 10:
        return []
    attributes = smart.get("attributes", {})
    if not isinstance(attributes, dict):
        return []
    changes = delta.get("attributes", {}) if isinstance(delta, dict) else {}

    def increase(key):
        change = changes.get(str(key)) if isinstance(changes, dict) else None
        return change.get("raw") if isinstance(change, dict) and type(change.get("raw")) is int else 0

    row = f"archive disk SMART ({night}): " + ", ".join(
        f"{key}={value['raw']}" for key, value in attributes.items()
        if (isinstance(value, dict) and type(value.get("raw")) is int
            and (key in {"5", "187", "199", "241"} or value.get("wear") is True)))
    if smart.get("passed") is False or increase(5) or increase(187):
        return [Finding(BAD, "smart", "My archive disk reported new damaged or uncorrectable sectors.", row)]
    if increase(199):
        return [Finding(WARN, "smart", "My archive disk reported new link errors.", row)]
    return [Finding(OK, "smart", "", row)]


def check_clock(system, facts):
    if facts.clock_trusted:
        return [Finding(OK, "clock", "", "clock: trusted")]
    return [Finding(WARN, "clock", "I'm not sure of the time; tell me what time it is.", "clock: not trusted")]


CHECKS = (check_organs, check_failed, check_cortex, check_hearing, check_disk, check_databases, check_continuity, check_backup,
          check_battery, check_temperature, check_kernel_health, check_clock, check_overnight_power,
          check_active_vitals, check_smart)
# The automatic pass reads only what a safe repair can act on: a systemctl
# call and the voice's last levels, no database reads every ten minutes.
REPAIR_CHECKS = (check_organs, check_hearing)


def check(system, facts, checks=CHECKS):
    """Every finding, worst first (stable within a severity)."""
    found = []
    for step in checks:
        try:
            found.extend(step(system, facts))
        except Exception as error:  # one broken reading never hides the rest
            found.append(Finding(NOTE, step.__name__.removeprefix("check_"), "",
                                 f"{step.__name__.removeprefix('check_')}: could not check ({type(error).__name__})"))
    return sorted(found, key=lambda finding: -finding.severity)


def _reserve_restart(unit, now):
    with _restart_lock:
        if now - _repaired.get(unit, -REPAIR_GAP_S) < REPAIR_GAP_S:
            return False
        _repaired[unit] = now
        return True


def repair(system, facts, findings, *, now=None, done=None):
    """The safe repairs, at most one organ and one audio reset. Returns lines said.

    `done`, if given, collects the repair keys that worked, so their findings
    are not told as still broken.
    """
    now = time.monotonic() if now is None else now
    said = []
    done = [] if done is None else done

    def due(key):
        return now - _repaired.get(key, -REPAIR_GAP_S) >= REPAIR_GAP_S

    organ = next((f for f in findings if f.repair and f.repair != "audio" and due(f.repair)
                  and not (f.repair == "being-cortex.service" and facts.cortex_busy)), None)
    if organ is not None and _reserve_restart(organ.repair, now):
        if system.restart(organ.repair):
            action_log.record(facts.state_dir, "repair.organ")
            said.append(f"I restarted my {ORGANS[organ.repair]}.")
            done.append(organ.repair)
    resets = _repaired.get("audio_resets", 0)
    if (any(f.repair == "audio" for f in findings) and not facts.ears_closed and due("audio")
            and resets < AUDIO_RESETS_PER_START):
        _repaired["audio"] = now
        _repaired["audio_resets"] = resets + 1
        if system.audio_init():
            action_log.record(facts.state_dir, "repair.audio")
            said.append("I reset my microphone; ask me how well I can hear you.")
            done.append("audio")
    return said


def spoken(findings, repairs=()):
    """At most three findings, worst first, after any repair just made."""
    told = [f.line for f in findings if f.severity > OK and f.line][:MOST_SPOKEN]
    if not told and not repairs:
        return "[joy] I'm well. Everything I can check looks fine."
    worst = max((f.severity for f in findings), default=OK)
    tag = "[concern]" if worst >= WARN else "[neutral]"
    parts = list(repairs) + told
    return f"{tag} {' '.join(parts[:MOST_SPOKEN + len(repairs)])}"


def health_rows(system, facts):
    """(mark, text) rows for the Care Health page."""
    marks = {BAD: "danger", WARN: "warning", NOTE: "accent", OK: "success"}
    return [(marks[f.severity], f.row) for f in check(system, facts)]


# --- the handler -----------------------------------------------------------------

_LEAD = r"^(?:(?:hey|hi|okay|ok)\s+pip\W*\s*)?(?:pip\W*\s*)?(?:so\s+|and\s+|please\s+|can\s+you\s+)?"
_END = r"(?:\s+(?:pip|please|now|today))*\s*[?.!]*$"
PATTERN = re.compile(
    _LEAD + r"(?:are\s+you\s+(?:okay|ok|alright|all\s+right|well|feeling\s+(?:okay|ok|alright|well))"
    r"|(?:run|do|give\s+yourself)\s+(?:a\s+)?(?:check[\s-]?up|health\s+check|self[\s-]check|diagnostic)s?(?:\s+on\s+yourself)?"
    r"|check\s+yourself(?:\s+over)?|what(?:['’]s|\s+is)\s+wrong(?:\s+with\s+you)?|is\s+(?:anything|something)\s+wrong(?:\s+with\s+you)?"
    r"|is\s+everything\s+(?:okay|ok|alright|all\s+right)(?:\s+with\s+you)?|how\s+are\s+your\s+(?:parts|systems|organs)"
    r"|are\s+all\s+your\s+parts\s+(?:okay|ok|working))" + _END, re.IGNORECASE)
_HINT = re.compile(r"\b(?:okay|ok|alright|right|well|check|checkup|diagnostics?|wrong|parts|systems|organs)\b", re.IGNORECASE)
_FIREWALL = ("firewall drops", "how many packets did you block", "how many packets did your firewall drop")


def may_match(text):
    candidate = text or ""
    cleaned = clean(candidate).casefold()
    return (bool(_MANUAL_RESTART.fullmatch(cleaned)) or cleaned in _FIREWALL
            or (bool(_HINT.search(candidate)) and bool(PATTERN.match(said(candidate)))))


def facts_from(ctx):
    turn = getattr(ctx, "body", None) or {}
    return Facts(state_dir=Path(ctx.state_dir), body=dict(turn), voice=dict(getattr(ctx, "voice", None) or {}),
                 clock_trusted=bool(getattr(ctx, "clock_trusted", True)),
                 cortex_busy=bool(turn.get("cortex_busy", False)), now=time.time(),
                 check_budget_s=VOICE_CHECK_S)


def examine(system, facts, checks=CHECKS):
    """Check, make the safe repairs, and drop the findings a repair just fixed."""
    findings = check(system, facts, checks)
    done = []
    repairs = repair(system, facts, findings, done=done)
    return [f for f in findings if f.repair not in done], repairs


def _manual_restart(ctx, system, unit):
    if is_guest(ctx) or ctx.speaker != ctx.owner:
        return Reply("[neutral] Only the person I live with can ask me to restart an organ.")
    if unit is None:
        return Reply("[neutral] I can restart my face, microphone, voice, thinking model, body care, or hands.")
    if unit in ("being-ears.service", "being-voice.service") and getattr(
        system, "microphone_latched", lambda: False
    )():
        return Reply("[concern] My microphone safety latch is active. It stays on until I reboot.")
    organ = ORGANS[unit]

    async def yes():
        read_body = getattr(ctx, "current_body", None)
        if read_body is None:
            return Reply("[concern] I couldn't check whether it's safe to restart.")
        states = await asyncio.to_thread(system.units, (unit,))
        state = (states or {}).get(unit, {})
        if (state.get("LoadState") != "loaded" or state.get("ConditionResult") == "no"
                or (state.get("ActiveState") != "active" and not stopped(state))):
            return Reply(f"[neutral] My {organ} isn't running or in a failed state.")
        body = read_body()
        if body is None or body.get("lid_closed") is not False:
            return Reply("[neutral] I couldn't restart it in this session.")
        if unit in ("being-cortex.service", "being-hands.service") and body.get("cortex_busy"):
            return Reply(f"[neutral] My {organ} is busy. Ask again after the work finishes.")
        if not _reserve_restart(unit, time.monotonic()):
            return Reply(f"[neutral] I tried restarting my {organ} recently. Give it a few minutes.")
        if not await asyncio.to_thread(system.restart, unit):
            return Reply(f"[concern] I couldn't request a restart for my {organ}.")
        action_log.record(ctx.state_dir, "repair.organ")
        return Reply(f"[neutral] I requested a restart for my {organ}.")

    return Reply(f"[neutral] Restart my {organ}? Say yes to go ahead.", confirm=Confirm(yes, strict=True))


async def handle(text, ctx, system=None):
    cleaned = clean(text, getattr(ctx, "name", "pip")).casefold()
    if requested := _MANUAL_RESTART.fullmatch(cleaned):
        return _manual_restart(ctx, system or System(), MANUAL_RESTARTS.get(requested["organ"]))
    if cleaned in _FIREWALL:
        if is_guest(ctx) or ctx.speaker != ctx.owner:
            return Reply("[neutral] My owner can ask about my firewall.")
        from .network_enforce import dropped_packets

        count = await asyncio.to_thread(dropped_packets)
        if count is None:
            return Reply("[neutral] I can't read my firewall counter right now.")
        packets = "packet" if count == 1 else "packets"
        return Reply(f"[neutral] I blocked {count:,} {packets} since I loaded my current network policy.")
    if not PATTERN.match(said(text, ctx)):
        return None
    if is_guest(ctx):
        return Reply("[neutral] I'm fine, thank you. My owner can ask me for a full check-up.")
    system = system or System()
    findings, repairs = await asyncio.to_thread(examine, system, facts_from(ctx))
    return Reply(spoken(findings, repairs))


async def self_care(ctx, system=None):
    """The quiet, automatic pass: check, make the safe repairs, say nothing.

    Returns the repair lines (for the log or a later "what did you do").
    """
    system = system or System()
    _, repairs = await asyncio.to_thread(examine, system, facts_from(ctx), REPAIR_CHECKS)
    return repairs
