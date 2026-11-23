"""The body monitor: how Pip's body is doing, drawn on the workshop panel.

"Show me how you're feeling" opens it. hands (mind/hands.py) runs it: once a
second it takes a Sampler reading in a worker thread and writes render()'s
ANSI into `being-console --panel monitor`, which draws the frame the face
shows below the eye strip. Everything here is read-only: /proc, /sys and
statvfs, plus the body.state the brainstem already publishes (battery, its
health, the temperature it judges by). Nothing is stored and nothing goes on
the bus.

The picture is 107 columns by 23 rows (hands.GRID_COLUMNS, GRID_ROWS), drawn
row by row with cursor positioning so it never scrolls, and only rows that
changed are redrawn by the renderer. Rendering a frame takes well under a
millisecond on the host (tests/test_app_monitor.py holds it to 5 ms).
"""

import os
import re
import time
from pathlib import Path

from . import hardware_metrics as metrics

COLUMNS, ROWS = 107, 23
TITLE = "How I'm feeling"
DISCOVER_EVERY = 30.0       # seconds between looks for fans and temperature sensors
MAX_CPUS = 8                # rows of the picture are few; the laptop has 4 logical CPUs
LABEL = 12                  # width of the left-hand labels
BAR = 40                    # cells in a full bar

# ANSI, in the 16 colours being-console draws.
RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
GREEN, YELLOW, RED, CYAN, GREY = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[36m", "\x1b[90m"
EIGHTHS = " ▏▎▍▌▋▊▉█"
TRACK = "░"

MODEL_STATES = {
    "idle": "resting", "listening": "listening", "attending": "listening", "thinking": "thinking",
    "speaking": "speaking", "asleep": "asleep", "dreaming": "dreaming",
}


# -- reading the body ----------------------------------------------------------------

def cpu_times(proc):
    """[(busy, total)] ticks for each logical CPU, from /proc/stat."""
    cores = []
    for line in metrics.read_field(proc / "stat").splitlines():
        if not re.match(r"cpu[0-9]+ ", line):
            continue
        values = [metrics.number(value) for value in line.split()[1:9]]
        if len(values) != 8 or any(value is None or value < 0 for value in values):
            continue
        total = sum(values)
        cores.append((total - values[3] - values[4], total))  # idle and iowait are not busy
        if len(cores) >= MAX_CPUS:
            break
    return cores


def cpu_percent(before, after):
    """Busy percent for each CPU between two cpu_times() readings, or None."""
    if not before or not after or len(before) != len(after):
        return None
    out = []
    for (busy0, total0), (busy1, total1) in zip(before, after):
        span = total1 - total0
        out.append(None if span <= 0 else max(0.0, min(100.0, 100.0 * (busy1 - busy0) / span)))
    return out


def _hwmon_chips(sys_root):
    """(driver, directory) for the hwmon chips this monitor reads."""
    for chip in sorted((sys_root / "class/hwmon").glob("hwmon[0-9]*"))[:16]:
        driver = metrics.read_field(chip / "name", 64)
        if not driver and metrics.read_field(chip / "device/name", 64) == "applesmc":
            chip, driver = chip / "device", "applesmc"
        if driver in ("coretemp", "applesmc"):
            yield driver, chip


class Sampler:
    """One reading a second. Which files to read is worked out once and again
    every DISCOVER_EVERY seconds, so a second's reading is about a dozen small
    reads (the SMC is slow to ask, so only the fans are asked of it)."""

    def __init__(self, root=Path("/"), disk="/mind", clock=time.monotonic):
        self.root = Path(root)
        self.disk = disk
        self.clock = clock
        self.fans = []          # [(input path, max rpm or None)]
        self.cores = []         # [(label, input path)] coretemp per core
        self.package = None     # coretemp package input path
        self._found = None

    def discover(self):
        fans, cores, package = [], [], None
        for driver, chip in _hwmon_chips(self.root / "sys"):
            if driver == "applesmc":
                for path in sorted(chip.glob("fan[0-9]_input"))[:4]:
                    maximum = metrics.measure(path.with_name(path.name.replace("_input", "_max")), lower=1, upper=50_000)
                    fans.append((path, maximum))
            else:
                for path in sorted(chip.glob("temp[0-9]*_input"))[:16]:
                    label = metrics.read_field(path.with_name(path.name.replace("_input", "_label")), 64)
                    if label.startswith("Package"):
                        package = package or path
                    elif re.fullmatch(r"Core [0-9]{1,2}", label):
                        cores.append((label, path))
        self.fans, self.cores, self.package = fans, cores[:MAX_CPUS], package
        self._found = self.clock()

    def sample(self):
        if self._found is None or self.clock() - self._found >= DISCOVER_EVERY:
            self.discover()
        proc = self.root / "proc"
        reading = {
            "at": self.clock(),
            "cpus": cpu_times(proc),
            "memory": metrics.memory_info(proc),
            "uptime_s": None,
            "fans": [],
            "cores_c": [],
            "package_c": None,
            "disk": None,
        }
        uptime = metrics.read_field(proc / "uptime", 64).split()
        try:
            reading["uptime_s"] = float(uptime[0]) if uptime else None
        except ValueError:
            pass
        for path, maximum in self.fans:
            rpm = metrics.measure(path, upper=50_000)
            if rpm is not None:
                reading["fans"].append((rpm, maximum))
        for label, path in self.cores:
            value = metrics.measure(path, lower=-40_000, upper=150_000, divisor=1000)
            if value is not None:
                reading["cores_c"].append((label, value))
        if self.package is not None:
            reading["package_c"] = metrics.measure(self.package, lower=-40_000, upper=150_000, divisor=1000)
        try:
            stats = os.statvfs(self.disk)
            reading["disk"] = (stats.f_blocks * stats.f_frsize, stats.f_bavail * stats.f_frsize)
        except OSError:
            pass
        return reading


# -- drawing ----------------------------------------------------------------------------

def bar(fraction, width=BAR, colour=GREEN):
    """A bar in eighths of a cell, on a dotted track."""
    if fraction is None:
        return GREY + TRACK * width + RESET
    eighths = round(max(0.0, min(1.0, fraction)) * width * 8)
    full, part = divmod(eighths, 8)
    cells = "█" * full + (EIGHTHS[part] if part and full < width else "")
    return colour + cells + GREY + TRACK * (width - len(cells)) + RESET


def level(value, warm, hot):
    return RED if value >= hot else YELLOW if value >= warm else GREEN


def gigabytes(count):
    value = count / 1e9
    return f"{value:.1f} GB" if value < 100 else f"{value:.0f} GB"


def duration(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def _row(label, text=""):
    return f"  {BOLD}{label:<{LABEL}}{RESET}{text}"


def _number(value):
    return type(value) in (int, float)


def battery_lines(body):
    percent = body.get("battery")
    if not _number(percent):
        return [_row("Battery", f"{GREY}no reading yet{RESET}")]
    colour = level(100 - percent, 70, 85)
    state = ("charging" if body.get("charging") else "plugged in" if body.get("on_ac") else "on battery")
    text = f"{bar(percent / 100, colour=colour)}  {percent:3.0f} %  {state}"
    left = body.get("minutes_left")
    if body.get("on_ac") is False and _number(left) and left > 0:
        text += f", about {duration(left * 60)} left"
    lines = [_row("Battery", text)]
    health, cycles = body.get("health_pct"), body.get("cycles")
    if _number(health) or _number(cycles):
        parts = []
        if _number(health):
            parts.append(f"holds {health:.0f} % of what it did when new")
        if _number(cycles):
            parts.append(f"{cycles:.0f} charge cycles")
        lines.append(_row("", GREY + ", ".join(parts) + RESET))
    return lines


def render(reading, previous=None, body=None, model=None, *, columns=COLUMNS, rows=ROWS):
    """One whole frame of ANSI: every row positioned and cleared, the cursor
    hidden. `reading` and `previous` are Sampler readings a second apart (no
    previous: CPU shows as not measured yet); `body` is the latest body.state;
    `model` is {"state": brainstem state, "tok_s": writing speed or None}."""
    body = body or {}
    model = model or {}
    lines = []
    # The title is the panel's own (its status bar), so the first row only
    # says how long Pip has been awake.
    up = reading.get("uptime_s")
    awake = f"awake {duration(up)}" if _number(up) else ""
    lines.append(" " * max(1, columns - len(awake) - 2) + GREY + awake + RESET if awake else "")
    lines.append("")

    percents = cpu_percent((previous or {}).get("cpus"), reading.get("cpus"))
    cpus = reading.get("cpus") or []
    for index in range(len(cpus)):
        value = percents[index] if percents else None
        label = "Thinking" if index == 0 else ""
        if value is None:
            text = f"{bar(None)}    …  CPU {index + 1}"
        else:
            text = f"{bar(value / 100, colour=level(value, 60, 90))}  {value:3.0f} %  CPU {index + 1}"
        lines.append(_row(label, text))
    if not cpus:
        lines.append(_row("Thinking", f"{GREY}no reading{RESET}"))
    lines.append("")

    temp = reading.get("package_c")
    if not _number(temp):
        temp = body.get("temp")
    if _number(temp):
        text = f"{bar(max(0.0, temp - 30) / 70, colour=level(temp, 75, 90))}  {temp:3.0f} °C"
        cores = reading.get("cores_c") or []
        if cores:
            text += "  " + GREY + "  ".join(f"core {label.split()[-1]} {value:.0f}°" for label, value in cores) + RESET
    else:
        text = f"{GREY}no reading{RESET}"
    lines.append(_row("Warmth", text))
    fans = reading.get("fans") or []
    for index, (rpm, maximum) in enumerate(fans[:2]):
        fraction = rpm / maximum if maximum else None
        quiet = "quiet" if fraction is not None and fraction < 0.35 else "busy" if fraction is not None else ""
        lines.append(_row("Fan" if index == 0 else "", f"{bar(fraction, colour=CYAN)}  {rpm:,} rpm  {GREY}{quiet}{RESET}"))
    if not fans:
        lines.append(_row("Fan", f"{GREY}no reading{RESET}"))
    lines.append("")

    lines += battery_lines(body)
    memory = reading.get("memory") or {}
    total, available = memory.get("total_kib"), memory.get("available_kib")
    if _number(total) and _number(available) and total > 0:
        used = max(0, total - available)
        text = (f"{bar(used / total, colour=level(100 * used / total, 75, 90))}  "
                f"{gigabytes(used * 1024)} of {gigabytes(total * 1024)} in use")
    else:
        text = f"{GREY}no reading{RESET}"
    lines.append(_row("Memory", text))
    disk = reading.get("disk")
    if disk and disk[0] > 0:
        size, free = disk
        used = max(0, size - free)
        text = (f"{bar(used / size, colour=level(100 * used / size, 85, 95))}  "
                f"{gigabytes(free)} free of {gigabytes(size)}")
    else:
        text = f"{GREY}no reading{RESET}"
    lines.append(_row("Disk", text))
    lines.append("")

    state = MODEL_STATES.get(model.get("state") or "", model.get("state") or "not heard from yet")
    speed = model.get("tok_s")
    if _number(speed) and speed > 0:
        speed_text = f"writes {speed:.1f} tokens a second (my last script)"
    else:
        speed_text = "writing speed not measured yet"
    lines.append(_row("Model", f"{state}  {GREY}·  {speed_text}{RESET}"))

    lines = lines[:rows]
    lines += [""] * (rows - len(lines))
    return "\x1b[?25l" + "".join(f"\x1b[{number};1H{line}\x1b[0m\x1b[K" for number, line in enumerate(lines, 1))
