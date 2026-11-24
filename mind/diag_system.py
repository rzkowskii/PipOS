"""The machine as pip-diag sees it, and the checks that need no sound.

`Host` is the only way the diagnostics touch the system: files under a root
(so tests use a temporary folder), commands, the clock. Tests replace it.

Sections here: system (build, kernel command line, units, restarts, every
mind module importing without zlib, the bus), boot timing from the journal,
the cortex (health, properties, fixed prompts, speed), the body (thermal,
battery, clock drift, storage, Ethernet, firewall) and the workshop's state.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .diag_report import check

ORGANS = ("being-brainstem.service", "being-mind.service", "being-voice.service", "being-face.service",
          "being-hands.service", "being-cortex.service", "being-eyes.service", "being-bodycare.service",
          "being-audio-init.service", "being-local-screen.service", "being-usb-logs.service")
BUS_ORGANS = ("mind", "voice", "face", "hands", "bodycare")
CORTEX_PORT = 8080
# Measured on the laptop in the v5 Try boot: prompt and decode tokens a second.
V5_PROMPT_TPS = 17.0
V5_DECODE_TPS = 10.9
SLOW_SHARE = 0.8
DRIFT_WARN_S = 120
CMDLINE_KEYS = ("being.try", "snd_hda_intel.model", "being.mic_channel", "being.cable", "being.voice_debug",
                "being.asr", "being.tts", "being.screen", "being.bench")
PROMPTS = (
    ("What is two plus two? Answer with one number.", ("4", "four")),
    ("What is the capital of France? Answer in one word.", ("paris",)),
    ("Say the word yes and nothing else.", ("yes",)),
    ("What colour is a clear daytime sky? One word.", ("blue",)),
    ("How many legs does a cat have? Answer with one number.", ("4", "four")),
)


@dataclass
class Result:
    code: int
    out: str
    err: str = ""


class Host:
    """Files, commands and clocks. `root` prefixes every absolute path read or written."""

    def __init__(self, root=Path("/")):
        self.root = Path(root)

    def path(self, path):
        path = str(path)
        return self.root / path.lstrip("/") if path.startswith("/") else Path(path)

    def read(self, path, limit=4 * 1024 * 1024):
        try:
            with open(self.path(path), "rb") as handle:
                return handle.read(limit).decode("utf-8", "replace")
        except OSError:
            return None

    def write(self, path, text):
        try:
            self.path(path).write_text(text, encoding="utf-8")
            return True
        except OSError:
            return False

    def exists(self, path):
        return self.path(path).exists()

    def glob(self, path, pattern):
        try:
            return sorted(self.path(path).glob(pattern))
        except OSError:
            return []

    def which(self, name):
        return shutil.which(name)

    def run(self, argv, timeout=30, input=None, env=None):
        """Result(code, out, err), or None when the program is missing or timed out."""
        try:
            done = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, check=False,
                                  stdin=None if input is not None else subprocess.DEVNULL, env=env,
                                  errors="replace")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return Result(done.returncode, done.stdout or "", done.stderr or "")

    def spawn(self, argv, **kwargs):
        """A background process (a recording), or None."""
        try:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, **kwargs)
        except OSError:
            return None

    def euid(self):
        return os.geteuid() if hasattr(os, "geteuid") else -1

    def is_root(self):
        return self.euid() == 0

    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


# systemd ----------------------------------------------------------------------

def systemd_present(host):
    return host.which("systemctl") is not None and host.exists("/run/systemd/system")


def unit_states(host, units):
    """{unit: {ActiveState, SubState, NRestarts, Result, LoadState}}, or None."""
    if host.which("systemctl") is None:
        return None
    result = host.run(["systemctl", "show", "--property=Id,LoadState,ActiveState,SubState,NRestarts,Result",
                       *units], timeout=15)
    if result is None or result.code:
        return None
    found = {}
    for block in result.out.strip().split("\n\n"):
        values = dict(line.partition("=")[::2] for line in block.splitlines() if "=" in line)
        if values.get("Id"):
            found[values["Id"]] = values
    return found


def is_active(host, unit):
    result = host.run(["systemctl", "is-active", unit], timeout=10)
    return result is not None and result.out.strip() == "active"


def systemctl(host, *args, timeout=60):
    result = host.run(["systemctl", *args], timeout=timeout)
    return result is not None and result.code == 0


def failed_units(host):
    result = host.run(["systemctl", "list-units", "--failed", "--plain", "--no-legend", "--no-pager"], timeout=15)
    if result is None or result.code:
        return None
    return [line.split()[0] for line in result.out.splitlines() if line.split()]


def active_units(host, *patterns):
    result = host.run(["systemctl", "list-units", "--plain", "--no-legend", "--no-pager", "--state=active",
                       *patterns], timeout=15)
    if result is None or result.code:
        return []
    return [line.split()[0] for line in result.out.splitlines() if line.split()]


# The journal ------------------------------------------------------------------

@dataclass
class Entry:
    monotonic: float
    unit: str
    message: str
    cursor: str = ""


def _message(value):
    if isinstance(value, list):  # journald gives non-UTF-8 text as a list of bytes
        try:
            return bytes(value).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return value if isinstance(value, str) else ""


class Journal:
    """journalctl read after a cursor, so each wait sees only what came after it."""

    def __init__(self, host):
        self.host = host

    def available(self):
        return self.host.which("journalctl") is not None

    def cursor(self):
        result = self.host.run(["journalctl", "-n", "1", "-o", "json", "--no-pager", "-q"], timeout=15)
        if result is None or result.code:
            return None
        for line in reversed(result.out.splitlines()):
            try:
                return json.loads(line).get("__CURSOR")
            except ValueError:
                continue
        return None

    def entries(self, after=None, units=(), boot=True):
        argv = ["journalctl", "-o", "json", "--no-pager", "-q"]
        argv += [f"--after-cursor={after}"] if after else (["-b"] if boot else [])
        for unit in units:
            argv += ["-u", unit]
        result = self.host.run(argv, timeout=30)
        if result is None or result.code:
            return []
        found = []
        for line in result.out.splitlines():
            try:
                data = json.loads(line)
            except ValueError:
                continue
            try:
                monotonic = int(data.get("__MONOTONIC_TIMESTAMP", 0)) / 1e6
            except (TypeError, ValueError):
                monotonic = 0.0
            unit = data.get("_SYSTEMD_UNIT") or data.get("SYSLOG_IDENTIFIER") or ""
            found.append(Entry(monotonic, unit, _message(data.get("MESSAGE")), data.get("__CURSOR", "")))
        return found

    def wait(self, after, match, *, timeout, units=(), poll=0.25):
        """The first entry after `after` for which match(entry) is true, or None.

        Returns (entry, cursor): the cursor to continue from."""
        deadline = self.host.monotonic() + timeout
        cursor = after
        while True:
            for entry in self.entries(cursor, units):
                cursor = entry.cursor or cursor
                if match(entry):
                    return entry, cursor
            if self.host.monotonic() >= deadline:
                return None, cursor
            self.host.sleep(poll)


# System -----------------------------------------------------------------------

def cmdline(host):
    return (host.read("/proc/cmdline") or "").split()


def cmdline_options(words):
    options = {}
    for word in words:
        key, sep, value = word.partition("=")
        if key in CMDLINE_KEYS:
            options[key] = value if sep else "1"
    return options


def voice_debug_on(host):
    from .try_mode import voice_debug_words

    return voice_debug_words(cmdline(host)) or os.environ.get("BEING_VOICE_DEBUG") == "1"


def boot_id(host):
    return (host.read("/proc/sys/kernel/random/boot_id") or "").strip()


def build_id(host):
    return (host.read("/etc/being/build-id") or "").strip()


def check_build(host):
    words = cmdline(host)
    options = cmdline_options(words)
    build = build_id(host)
    kernel = (host.read("/proc/sys/kernel/osrelease") or "").strip()
    value = {"build": build or None, "kernel": kernel or None, "boot_id": boot_id(host) or None, "cmdline": options}
    if not words:
        return check("system.build", "system", "skip", value, "no /proc/cmdline to read")
    entry = ", ".join(f"{key}={value}" for key, value in options.items()) or "no PipOS options"
    return check("system.build", "system", "pass", value,
                 f"build {build or 'unknown'}, kernel {kernel or 'unknown'}; {entry}")


def check_clock(host, mac_now=None):
    """System clock against the Mac's (--now) and against the RTC."""
    now = host.time()
    rtc = host.read("/sys/class/rtc/rtc0/since_epoch")
    value = {"system_utc": round(now), "monotonic": round(host.monotonic(), 3)}
    notes = []
    status = "pass"
    hint = ""
    if rtc and rtc.strip().isdigit():
        value["rtc_drift_s"] = int(rtc) - round(now)
        notes.append(f"RTC {value['rtc_drift_s']:+d} s from the system clock")
        if abs(value["rtc_drift_s"]) > DRIFT_WARN_S:
            status = "warn"
            hint = "The hardware clock is wrong; time-of-day features (night, routines) may misbehave."
    if mac_now is not None:
        value["mac_drift_s"] = round(now - mac_now)
        notes.append(f"system clock {value['mac_drift_s']:+d} s from the Mac")
        if abs(value["mac_drift_s"]) > DRIFT_WARN_S:
            status = "warn"
            hint = "Pip's clock is far from the Mac's. Folder names and times in this report are off by that much."
    if not notes:
        return check("system.clock", "system", "skip", value, "no RTC and no --now to compare with")
    return check("system.clock", "system", status, value, "; ".join(notes), hint)


def check_units(host):
    states = unit_states(host, ORGANS)
    if states is None:
        return check("system.units", "system", "skip", None, "systemctl is not available")
    bad, restarted, summary = [], [], {}
    for unit, values in states.items():
        if values.get("LoadState") == "not-found":
            continue
        name = unit.removesuffix(".service")
        summary[name] = f"{values.get('ActiveState')}/{values.get('SubState')}"
        restarts = values.get("NRestarts", "0")
        if values.get("ActiveState") == "failed" or values.get("Result") not in (None, "", "success"):
            bad.append(f"{name} {values.get('ActiveState')} ({values.get('Result')})")
        if restarts.isdigit() and int(restarts) > 0:
            restarted.append(f"{name} x{restarts}")
            summary[name] += f" restarts={restarts}"
    failed = failed_units(host) or []
    value = {"units": summary, "failed": failed}
    if bad or failed:
        return check("system.units", "system", "fail", value, "; ".join(bad + [f"failed: {u}" for u in failed]),
                     "Look at `journalctl -b -u UNIT` for the failed units.")
    if restarted:
        return check("system.units", "system", "warn", value, "restarted: " + ", ".join(restarted),
                     "An organ restarted during this boot; its journal says why.")
    return check("system.units", "system", "pass", value, f"{len(summary)} organs, none failed or restarted")


IMPORT_PROBE = r"""
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
for name in ("zlib", "gzip", "zipfile"):
    sys.modules[name] = None
results = {}
for name in sys.argv[2:]:
    try:
        importlib.import_module(name)
        results[name] = None
    except BaseException as error:
        results[name] = f"{type(error).__name__}: {error}"
print(json.dumps(results))
"""


def mind_modules(package_dir):
    """Every importable mind module except __main__ (importing that runs the mind)."""
    return sorted(f"mind.{path.stem}" for path in Path(package_dir).glob("*.py")
                  if path.stem not in ("__main__",) and re.fullmatch(r"[a-z_][a-z0-9_]*", path.stem))


def check_imports(host, package_dir=None, python=None):
    """Import every mind module in a child process with zlib blocked, as on the target."""
    package_dir = Path(package_dir or Path(__file__).resolve().parent)
    modules = mind_modules(package_dir)
    if not modules:
        return check("system.imports", "system", "skip", None, f"no modules found in {package_dir}")
    # Isolated (-I): the package is found where it is, never in the current directory.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = host.run([python or sys.executable, "-I", "-B", "-c", IMPORT_PROBE, str(package_dir.parent), *modules],
                      timeout=180, env=env)
    if result is None:
        return check("system.imports", "system", "warn", None, "the import probe did not finish",
                     "Run `python3 -c 'import mind.turn'` by hand to see what hangs.")
    try:
        found = json.loads(result.out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return check("system.imports", "system", "fail", None, f"the import probe crashed: {result.err[-300:]}",
                     "A module crashed the interpreter on import.")
    broken = {name: error for name, error in found.items() if error}
    if broken:
        return check("system.imports", "system", "fail", {"broken": broken, "modules": len(found)},
                     "; ".join(f"{name}: {error}" for name, error in sorted(broken.items())),
                     "A module that cannot import is a feature that silently disappears (no zlib on the target).")
    return check("system.imports", "system", "pass", {"modules": len(found)},
                 f"all {len(found)} mind modules import with zlib blocked")


def bus_path(host):
    env = host.read("/etc/being/env") or ""
    match = re.search(r"^BEING_BUS=(\S+)", env, re.M)
    return match[1] if match else "/run/being/bus.sock"


# The bus names each organ may use and the units that may use them (as
# crates/brainstem/src/peer.rs PROTECTED); the Try menu draws the face. The
# QEMU lab's simulated body also calls itself "voice"; it is left out, so in
# the lab it only makes the count larger than the running organs, never short.
BUS_ORGAN_UNITS = {
    "mind": ("being-mind.service",),
    "voice": ("being-voice.service",),
    "face": ("being-face.service", "being-local-screen.service"),
    "hands": ("being-hands.service",),
    "bodycare": ("being-bodycare.service",),
}


def organs_running(host):
    """The BUS_ORGANS whose unit is active (they should be on the bus), or None when unknown."""
    units = sorted({unit for names in BUS_ORGAN_UNITS.values() for unit in names})
    states = unit_states(host, units)
    if states is None:
        return None
    return [organ for organ in BUS_ORGANS
            if any((states.get(unit) or {}).get("ActiveState") == "active" for unit in BUS_ORGAN_UNITS[organ])]


def read_beat(host, path, timeout):
    """The `up` field of the first stem.beat, or None. Raises OSError when the bus cannot be read."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(b'{"t":"hello","name":"diag","sub":["stem.beat"]}\n')
        buffer, deadline = b"", time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("t") == "stem.beat" and "up" in event:
                    return event.get("up")
    return None


def check_bus(host, timeout=3.0):
    """Subscribe to stem.beat as a passive 'diag' peer and compare who is up with the running organs.

    The brainstem's beat carries `up` as the count of named peers (this
    'diag' peer included), not their names, so the count is compared with
    the organ units that are active: fewer peers than running organs means
    an organ runs but is not connected."""
    path = host.path(bus_path(host))
    if not path.exists():
        return check("system.bus", "system", "skip", None, f"no bus socket at {bus_path(host)}")
    try:
        up = read_beat(host, path, timeout)
    except OSError as error:
        return check("system.bus", "system", "fail", None, f"could not read the bus: {error}",
                     "The brainstem may be down: `systemctl status being-brainstem`.")
    if isinstance(up, bool) or not isinstance(up, (int, list)):
        return check("system.bus", "system", "warn", None, f"connected, but no heartbeat within {timeout:g} s",
                     "The brainstem accepted the connection but sent no stem.beat.")
    running = organs_running(host)
    if isinstance(up, list):  # names (an older or simulated brainstem)
        names = [str(name) for name in up]
        expected = running if running is not None else list(BUS_ORGANS)
        missing = [organ for organ in expected if organ not in names]
        return check("system.bus", "system", "warn" if missing else "pass", {"up": names, "missing": missing},
                     f"on the bus: {', '.join(names)}" + (f"; missing {', '.join(missing)}" if missing else ""),
                     "An organ runs but is not connected to the brainstem; check its unit." if missing else "")
    organs = max(0, up - 1)  # this diag peer is one of them
    value = {"up": up, "organs": organs, "running": running}
    if running is None:
        return check("system.bus", "system", "pass", value,
                     f"heartbeat received: {organs} organs named on the bus (unit states unknown)")
    evidence = (f"heartbeat received: {organs} organs named on the bus; "
                f"{len(running)} organ units running ({', '.join(running) or 'none'})")
    if organs < len(running):
        value["short_by"] = len(running) - organs
        return check("system.bus", "system", "warn", value, evidence,
                     "An organ runs but is not connected to the brainstem; `journalctl -b -u being-brainstem` "
                     "names refused or dropped peers.")
    return check("system.bus", "system", "pass", value, evidence)


# Boot timing ------------------------------------------------------------------

MARKERS = (
    ("brainstem", re.compile(r"brainstem: \w+ -> ")),
    ("mind_started", re.compile(r"^mind: .* \| cortex at ")),
    ("cortex_warmed", re.compile(r"^mind: warmed \d+")),
    ("voice_listening", re.compile(r"listening for the wake phrase")),
    ("console_opened", re.compile(r"Console opened")),
    ("menu_closed", re.compile(r"being-local-screen\.service: Deactivated")),
    ("face_drawn", re.compile(r"face: drawn by ")),
)


def boot_markers(entries):
    """{marker: monotonic seconds} for the first time each boot milestone appears."""
    found = {}
    for entry in entries:
        for name, pattern in MARKERS:
            if name not in found and pattern.search(entry.message):
                found[name] = round(entry.monotonic, 2)
    return found


def check_boot_timing(host, journal=None):
    journal = journal or Journal(host)
    if not journal.available():
        return check("boot.timing", "boot", "skip", None, "journalctl is not available")
    markers = boot_markers(journal.entries())
    if not markers:
        return check("boot.timing", "boot", "skip", None, "no boot milestones in this boot's journal")
    evidence = ", ".join(f"{name} {seconds:.1f} s" for name, seconds in sorted(markers.items(), key=lambda kv: kv[1]))
    if "face_drawn" in markers and "menu_closed" in markers and markers["face_drawn"] - markers["menu_closed"] > 60:
        return check("boot.timing", "boot", "warn", markers, evidence,
                     "The face took more than 60 s to appear after the Try menu closed.")
    if "voice_listening" not in markers:
        return check("boot.timing", "boot", "warn", markers, evidence + "; the voice never said it was listening",
                     "See `journalctl -b -u being-voice`.")
    return check("boot.timing", "boot", "pass", markers, evidence)


# The cortex -------------------------------------------------------------------

def cortex_client(port=CORTEX_PORT, timeout=60):
    from .bench import Client

    return Client(port, timeout=timeout)


def check_cortex(host, *, prompts=PROMPTS, client=None):
    """Health, properties and fixed prompts at temperature 0, with their speed."""
    client = client or cortex_client()
    checks = []
    try:
        client.request("GET", "/health")
    except (OSError, RuntimeError, ValueError) as error:
        return [check("cortex.health", "cortex", "fail", None, f"llama-server on 127.0.0.1:{CORTEX_PORT}: {error}",
                      "The thinking model is not answering: `systemctl status being-cortex`.")]
    checks.append(check("cortex.health", "cortex", "pass", True, "llama-server answers /health"))
    try:
        props = client.request("GET", "/props")
        settings = props.get("default_generation_settings", {}) if isinstance(props, dict) else {}
        value = {"model": Path(str(props.get("model_path", ""))).name or None,
                 "n_ctx": settings.get("n_ctx"), "slots": props.get("total_slots")}
        checks.append(check("cortex.props", "cortex", "pass", value,
                            f"model {value['model']}, context {value['n_ctx']}, {value['slots']} slots"))
    except (OSError, RuntimeError, ValueError, AttributeError) as error:
        checks.append(check("cortex.props", "cortex", "warn", None, f"/props failed: {error}"))
    from .bench import chat_prompt

    right, prompt_tps, decode_tps, answers = 0, [], [], []
    slot = {"id_slot": 1}
    for question, expected in prompts:
        body = {"prompt": chat_prompt([("user", question)]), "n_predict": 12, "temperature": 0, "seed": 1,
                "cache_prompt": False, "stream": False}
        try:
            # Slot 1 and no prompt cache: the mind's conversation slot (0) is left alone.
            result = client.request("POST", "/completion", {**body, **slot})
        except RuntimeError as error:
            if not slot:
                answers.append({"q": question, "error": str(error)})
                continue
            slot = {}  # a server with one slot refuses id_slot 1: let it choose
            try:
                result = client.request("POST", "/completion", body)
            except (OSError, RuntimeError, ValueError) as again:
                answers.append({"q": question, "error": str(again)})
                continue
        except (OSError, ValueError) as error:
            answers.append({"q": question, "error": str(error)})
            continue
        text = str(result.get("content", "")).strip()
        ok = any(word in text.casefold() for word in expected)
        right += ok
        answers.append({"q": question, "a": text[:80], "ok": ok})
        timings = result.get("timings") or {}
        if isinstance(timings.get("prompt_per_second"), (int, float)) and (timings.get("prompt_n") or 0) >= 16:
            prompt_tps.append(timings["prompt_per_second"])
        if isinstance(timings.get("predicted_per_second"), (int, float)):
            decode_tps.append(timings["predicted_per_second"])
    status = "pass" if right == len(prompts) else "warn" if right else "fail"
    checks.append(check("cortex.answers", "cortex", status, answers,
                        f"{right} of {len(prompts)} fixed questions right",
                        "" if status == "pass" else
                        "The model answered fixed questions wrongly; the pack may be damaged."))
    if prompt_tps or decode_tps:
        prompt = round(sorted(prompt_tps)[len(prompt_tps) // 2], 1) if prompt_tps else None
        decode = round(sorted(decode_tps)[len(decode_tps) // 2], 1) if decode_tps else None
        slow = ((prompt is not None and prompt < V5_PROMPT_TPS * SLOW_SHARE)
                or (decode is not None and decode < V5_DECODE_TPS * SLOW_SHARE))
        checks.append(check("cortex.speed", "cortex", "warn" if slow else "pass",
                            {"prompt_tps": prompt, "decode_tps": decode},
                            f"prompt {prompt} tok/s, decode {decode} tok/s (v5: 17-21 and 10.9-13.6)",
                            "More than 20% slower than the v5 boot: heat, a busy CPU or a power cap." if slow else ""))
    return checks


# The body ---------------------------------------------------------------------

def spin(seconds, threads=4):
    """Busy child processes for a thermal look under load."""
    code = f"import time\nend = time.monotonic() + {float(seconds)}\nwhile time.monotonic() < end: pass\n"
    children = []
    for _ in range(threads):
        try:
            children.append(subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except OSError:
            break
    return children


def check_thermal(host, seconds=5, *, load=False):
    from . import hardware_metrics

    children = spin(seconds + 1) if load and host.root == Path("/") else []
    try:
        data = hardware_metrics.collect(seconds, 1, root=host.root, clock=host.monotonic, sleep=host.sleep)
    finally:
        for child in children:
            child.kill()
            child.wait()
    window = data.get("window", {})
    value = {key: window.get(key) for key in ("temperature_max_c", "cpu_temperature_max_c", "fan_max_rpm",
                                              "cpu_core_throttle_max_events", "cpu_busy_percent",
                                              "package_power_mean_watts", "cpu_frequency_mean_khz")}
    value["under_load"] = bool(children)
    temperature = value["cpu_temperature_max_c"] or value["temperature_max_c"]
    if temperature is None:
        return check("body.thermal", "body", "skip", value, "no temperature sensors")
    throttled = value["cpu_core_throttle_max_events"]
    status = "fail" if temperature >= 95 else "warn" if temperature >= 85 or throttled else "pass"
    return check("body.thermal", "body", status, value,
                 f"hottest {temperature} C{' under a 4-core load' if children else ''}, fan {value['fan_max_rpm']} rpm,"
                 f" throttle events {throttled}",
                 "" if status == "pass" else "Running hot: check the fan and the vents.")


def check_battery(host):
    from . import hardware_metrics

    info = hardware_metrics.power_info(host.path("/sys"))
    if not info["batteries"]:
        return check("body.battery", "body", "skip", info, "no battery reported")
    battery = info["batteries"][0]
    health = battery.get("health_percent")
    status = "warn" if health is not None and health < 60 else "pass"
    limit = (host.read("/sys/class/power_supply/BAT0/charge_control_end_threshold") or "").strip() or None
    value = {**battery, "mains_online": info["mains_online"], "charge_limit": limit}
    return check("body.battery", "body", status, value,
                 f"{battery.get('charge_percent')}% {battery.get('status')}, health {health}%, "
                 f"cycles {battery.get('cycles')}, mains {info['mains_online']}",
                 "The battery is worn below 60% of its design charge." if status == "warn" else "")


def wired_interfaces(host):
    found = []
    for path in host.glob("/sys/class/net", "*"):
        if (path / "wireless").exists() or (path / "phy80211").exists() or not (path / "device").exists():
            continue
        if (host.read(f"/sys/class/net/{path.name}/type") or "").strip() != "1":
            continue
        found.append(path.name)
    return found


def check_network(host):
    wired = wired_interfaces(host)
    wireless = [path.name for path in host.glob("/sys/class/net", "*") if (path / "wireless").exists()]
    links = {}
    for name in wired:
        base = f"/sys/class/net/{name}"
        links[name] = {key: (host.read(f"{base}/{key}") or "").strip() or None
                       for key in ("operstate", "carrier", "speed", "duplex")}
    value = {"wired": links, "wireless": wireless or "not available on this build"}
    if not wired:
        return check("body.network", "body", "skip", value, "no wired network interface")
    evidence = "; ".join(f"{name} {info['operstate']} carrier={info['carrier']} {info['speed'] or '?'} Mb/s"
                         for name, info in links.items())
    return check("body.network", "body", "pass", value, evidence)


def check_firewall(host):
    if host.which("nft") is None:
        return check("body.firewall", "body", "skip", None, "nft is not installed")
    result = host.run(["nft", "list", "tables"], timeout=10)
    if result is None or result.code:
        return check("body.firewall", "body", "skip", None, "nft could not list tables (needs root)")
    tables = [line.strip() for line in result.out.splitlines() if line.strip()]
    status = "pass" if any("being_network" in table for table in tables) else "warn"
    return check("body.firewall", "body", status, tables, "; ".join(tables) or "no tables",
                 "" if status == "pass" else "The PipOS network table is missing.")


def check_storage(host, folder=None):
    value = {}
    if folder is not None:
        try:
            info = os.statvfs(folder)
            value["report_free_mb"] = round(info.f_bavail * info.f_frsize / 1e6)
        except OSError:
            pass
    root_device = None
    for line in (host.read("/proc/self/mountinfo") or "").splitlines():
        before, _, after = line.partition(" - ")
        fields, source = before.split(), after.split()
        if len(fields) >= 5 and fields[4] == "/" and len(source) > 1 and source[1].startswith("/dev/sd"):
            root_device = re.sub(r"\d+$", "", source[1])
    if host.which("hdparm") and root_device and host.is_root():
        result = host.run(["hdparm", "-t", root_device], timeout=60)
        match = re.search(r"=\s*([\d.]+) MB/sec", result.out if result else "")
        if match:
            value["read_mb_s"] = float(match[1])
            value["device"] = root_device
    if not value:
        return check("body.storage", "body", "skip", None, "no free-space or read-speed figure available")
    low = value.get("report_free_mb") is not None and value["report_free_mb"] < 64
    return check("body.storage", "body", "warn" if low else "pass", value,
                 ", ".join(f"{key} {val}" for key, val in value.items()),
                 "The diagnostics stick is nearly full." if low else "")


# The workshop -----------------------------------------------------------------

PANEL_DIR = "/run/being-panel"
PANEL_VISIBLE = 1


def panel_state(host, name="terminal"):
    """{visible, beat, title} from a panel frame header, or None."""
    import struct

    path = host.path(f"{PANEL_DIR}/{name}.frame")
    try:
        with open(path, "rb") as handle:
            header = handle.read(128)
    except OSError:
        return None
    if len(header) < 128 or header[:4] != b"BPN1":
        return None
    flags, = struct.unpack_from("<I", header, 32)
    beat, = struct.unpack_from("<I", header, 44)
    title = header[96:128].split(b"\0", 1)[0].decode("utf-8", "replace")
    return {"visible": bool(flags & PANEL_VISIBLE), "beat": beat, "title": title}


def terminal_open(host):
    """(open?, evidence): open only with a visible terminal frame or an active
    being-panel-* unit (the panel that draws it). A being-shell-* unit alone is
    a shell with nothing on the screen, so it is evidence but not an open terminal."""
    units = active_units(host, "being-panel-*", "being-shell-*")
    panels = [unit for unit in units if unit.startswith("being-panel-")]
    frame = panel_state(host)
    evidence = []
    if units:
        evidence.append("units " + ", ".join(units))
        if not panels:
            evidence.append("a shell without a panel")
    if frame is not None:
        evidence.append(f"terminal frame {'visible' if frame['visible'] else 'hidden'}, beat {frame['beat']}")
    return bool(panels) or bool(frame and frame["visible"]), "; ".join(evidence) or "no terminal unit or frame"


# hands.py FACE_UP: the states in which a unit holds (or is taking) the screen.
SCREEN_UP = ("active", "activating", "reloading")


def unit_state(host, unit):
    """`systemctl is-active` for one unit ('active', 'inactive', 'activating', ...), or None."""
    result = host.run(["systemctl", "is-active", unit], timeout=10)
    if result is None:
        return None
    return (result.out or "").strip().splitlines()[0] if (result.out or "").strip() else None


def menu_has_the_screen(host):
    """True when the Try menu holds the display and the face is not running.

    The same test as hands._menu_has_the_screen: then hands refuses to open
    the terminal (app.refused reason=no_face), by design."""
    if host.which("systemctl") is None:
        return False
    face, menu = unit_state(host, "being-face.service"), unit_state(host, "being-local-screen.service")
    return face is not None and face not in SCREEN_UP and menu in SCREEN_UP


def check_workshop(host):
    if host.which("systemctl") is None:
        return check("workshop.state", "workshop", "skip", None, "systemctl is not available")
    states = {unit.removesuffix(".service"): is_active(host, unit)
              for unit in ("being-face.service", "being-hands.service", "being-local-screen.service")}
    opened, evidence = terminal_open(host)
    value = {"active": states, "terminal_open": opened}
    text = ", ".join(f"{name} {'active' if on else 'inactive'}" for name, on in states.items()) + f"; {evidence}"
    if not states.get("being-hands"):
        return check("workshop.state", "workshop", "warn", value, text, "The workshop (hands) is not running.")
    if not states.get("being-face") and states.get("being-local-screen"):
        return check("workshop.state", "workshop", "warn", value, text,
                     "Face not running: the Try menu has the screen, so an opened terminal cannot be seen.")
    return check("workshop.state", "workshop", "pass", value, text)
