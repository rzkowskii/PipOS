"""Which voice and which ears: chosen by measurement on this laptop.

Two engines of each kind are possible:

- speaking: the original Flite voice, or the natural Piper voice (Alba);
- hearing after the wake: PocketSphinx, or Silero VAD plus Moonshine.

In "auto" (the default) the natural voice is used from the first reply, and
kept unless its idle real-time factor, over 3 runs of a fixed 12-word sentence,
is measured above 0.7 (the owner prefers the natural voice even when it starts
a little later; a 2-second clause then takes at most 1.4 s), and Moonshine
only if its files are present and its idle real-time factor on a test
recording is at most 0.3. A real-time factor is compute time over audio time,
so 0.3 means a 2-second clause takes 0.6 s. Measurements are kept in
/mind/audio/engines.json, keyed by build-id and voice or model, and taken
again after a week. Nothing is measured while the being is busy: the CPU
pressure and the voice's own CPU use decide that (cpu_quiet), and the load
average only where the kernel has no pressure figures.

The owner's choice ("auto", "piper" or "flite") is the voice organ's: it is
the "tts" field of /mind/audio/voice.json, which the voice writes when the
mind sends `voice.engine`.

A boot entry or a service drop-in can force a choice: `being.asr=` and
`being.tts=` on the kernel command line, or BEING_ASR and BEING_TTS in the
environment, which win over the kernel argument. "auto" leaves the usual
choice. A forced engine skips the speed rule but is still loaded only if its
signed model pack verifies.

Before the natural voice is chosen, in "auto" or forced, it says one short word
(PROBE_TEXT) within PROBE_TIMEOUT_S. If that raises, gives no samples, is only
silence or takes too long, Flite is used and the note says why; the failure is
kept in RAM for this build, so it is not tried again on every clause or choice.
A natural voice that is broken later silences itself (mind/tts_sherpa.py) and
the failure is kept the same way. The voice organ also writes it to RUN_STATE
(RAM, under /run), with its process id, so the mind's "which voice" answer and
Setup's voice page say the original voice speaks, and why, while that voice
process lives.

Moonshine runs inside the voice process (sherpa-onnx through ctypes), so an
instruction the CPU lacks would kill the voice. "auto" therefore never loads
it unless a passed Ivy Bridge speech smoke test is recorded for the installed
runtime: the QEMU run of lab/sherpa_avx_smoke.py shipped in the image, or a
run on this laptop (`python3 -m mind.voice_engines smoke --record`); see
`sherpa_smoke_passed` and `hardware_smoke`. Saying "use your natural voice", "use your original
voice" or "which voice are you using?" reaches `handle` below through the
everyday registry; the answer is fixed in code.

Nothing here imports the speech library until an engine is used.
"""

import json
import os
import re
import shutil
import statistics
import tempfile
import threading
import time
import wave
from array import array
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from pathlib import Path

ENGINES_FILE = Path("audio") / "engines.json"
# The voice organ's settings; its "tts" field is the owner's engine choice.
VOICE_SETTINGS = Path("audio") / "voice.json"
BUILD_ID = Path("/etc/being/build-id")
TTS_CHOICES = ("auto", "piper", "flite")
ASR_CHOICES = ("auto", "moonshine", "pocketsphinx")
TTS_RTF_LIMIT = 0.7
ASR_RTF_LIMIT = 0.3
RUNS = 3
MAX_AGE_S = 7 * 24 * 3600
# Twelve words, the same every time, so measurements compare across builds.
SENTENCE = "I keep your timers and notes, and I remember what you say."
MAX_FILE_BYTES = 64 * 1024
# Where the kernel has no pressure figures, the 1-minute load average must be
# under this for a measurement to count as idle. On the laptop it never is
# with the face up: the i915 page-flip worker sits in uninterruptible sleep
# about 82% of the time, which adds about 0.8 to the load average while the CPU
# is 93% idle (measured: load 1.51 against CPU pressure 0.16). Pressure, which
# that worker does not touch, is used wherever it exists.
IDLE_LOAD = 0.7
CMDLINE = Path("/proc/cmdline")
# Forced engines: the environment (a drop-in) wins over the kernel argument,
# and both over the older /etc/being/env names.
ASR_ARG, TTS_ARG = "being.asr=", "being.tts="
ASR_ENV, TTS_ENV = "BEING_ASR", "BEING_TTS"
LEGACY_ASR_ENV = "BEING_ASR_ENGINE"
_ALIASES = {"sherpa": "moonshine", "sherpa-onnx": "moonshine"}
# The CPU counts as quiet when tasks waited for it less than this share of the
# last 10 s (/proc/pressure/cpu, "some avg10", in percent; 0.0 to 0.2 at rest
# on the laptop with the face up, tens while the model answers), and this
# process itself used less than OWN_CPU_LIMIT of one core over the last
# OWN_CPU_WINDOW seconds (a dictation decoding, or a reply being synthesized,
# is no time to measure).
CPU_PRESSURE = Path("/proc/pressure/cpu")
QUIET_PRESSURE = 5.0
OWN_CPU_LIMIT = 0.35
OWN_CPU_WINDOW = 0.25
# The Ivy Bridge speech smoke report: the verdict of lab/sherpa_avx_smoke.py's
# QEMU -cpu IvyBridge run, as the image build installs it, or one recorded on
# this laptop (`python3 -m mind.voice_engines smoke --record`, see
# hardware_smoke) in the mind.
SMOKE_VERDICTS = (Path("/usr/share/being/voice/sherpa-avx-smoke.json"),)
SMOKE_STATE = Path("audio") / "sherpa-avx-smoke.json"


# Files ----------------------------------------------------------------------------

def build_id(path=None):
    try:
        value = Path(BUILD_ID if path is None else path).read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,96}", value) else "unknown"


def _read_json(path):
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return {}
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".engines-", delete=False,
                                         encoding="utf-8") as target:
            pending = Path(target.name)
            os.fchmod(target.fileno(), 0o600)
            json.dump(data, target, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(pending, path)
        pending = None
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink()


def load_preference(state_dir):
    value = _read_json(Path(state_dir) / VOICE_SETTINGS).get("tts")
    return value if value in TTS_CHOICES else "auto"


def save_preference(state_dir, tts):
    """Keep the choice in the voice's own settings, leaving its volume and chirps as they are.

    For Setup, which restarts the voice afterwards. The mind instead sends
    `voice.engine`, and the running voice saves it.
    """
    if tts not in TTS_CHOICES:
        raise ValueError(f"unknown voice choice {tts!r}")
    from .voice import load_voice_settings, save_voice_settings

    current = load_voice_settings(state_dir)
    save_voice_settings(state_dir, volume=current["volume"], chirps=current["chirps"], tts=tts)


def measurements(state_dir):
    data = _read_json(Path(state_dir) / ENGINES_FILE)
    return {kind: data.get(kind) if isinstance(data.get(kind), dict) else {} for kind in ("tts", "asr")}


def key(build, name):
    return f"{build}|{name}"


def cached(state_dir, kind, record_key, now=None, max_age=MAX_AGE_S):
    """A measurement that is still valid, or None."""
    record = measurements(state_dir)[kind].get(record_key)
    if not isinstance(record, dict):
        return None
    rtf, when = record.get("rtf"), record.get("measured")
    if type(rtf) not in (int, float) or type(when) not in (int, float) or not 0 <= rtf < 100:
        return None
    now = time.time() if now is None else now
    if not 0 <= now - when <= max_age:
        return None
    return record


def record(state_dir, kind, record_key, result, now=None):
    data = measurements(state_dir)
    entry = {"rtf": round(float(result["rtf"]), 4), "runs": [round(float(run), 4) for run in result["runs"]],
             "measured": time.time() if now is None else now}
    # One entry per build and engine; older builds' entries are dropped.
    build = record_key.split("|", 1)[0]
    data[kind] = {name: value for name, value in data[kind].items() if name.split("|", 1)[0] == build}
    data[kind][record_key] = entry
    _write_json(Path(state_dir) / ENGINES_FILE, {"version": 1, **data})
    return entry


def system_idle(threshold=IDLE_LOAD):
    try:
        return os.getloadavg()[0] < threshold
    except OSError:
        return False


def cpu_pressure(path=CPU_PRESSURE):
    """The "some avg10" CPU pressure in percent, or None where the kernel has no PSI."""
    try:
        text = Path(path).read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        words = line.split()
        if words[:1] == ["some"]:
            for word in words[1:]:
                if word.startswith("avg10="):
                    try:
                        value = float(word[len("avg10="):])
                    except ValueError:
                        return None
                    return value if 0.0 <= value <= 100.0 else None
    return None


def own_cpu(window=OWN_CPU_WINDOW, clock=time.monotonic, cpu=time.process_time, sleep=time.sleep):
    """This process's CPU use over the next `window` seconds, in cores (every
    thread counts). Blocking for `window`; None when it cannot be told."""
    if window <= 0:
        return None
    try:
        began, used = clock(), cpu()
        sleep(window)
        wall, spent = clock() - began, cpu() - used
    except OSError:
        return None
    return spent / wall if wall > 0 and spent >= 0 else None


def cpu_quiet(threshold=QUIET_PRESSURE, path=CPU_PRESSURE, load_threshold=IDLE_LOAD, own_limit=OWN_CPU_LIMIT,
              window=OWN_CPU_WINDOW, own=None):
    """True when the CPU is not contended now and this process is not busy.

    CPU pressure under `threshold` (the load average under `load_threshold`
    only where the kernel has no pressure figures), and this process's own use
    over `window` seconds under `own_limit` of a core (`own` measures it; a
    window of 0 skips it). Blocking for `window`: call it off the event loop."""
    pressure = cpu_pressure(path)
    if pressure is None:
        if not system_idle(load_threshold):
            return False
    elif pressure >= threshold:
        return False
    used = (own or own_cpu)(window) if window > 0 else None
    return used is None or used < own_limit


# Forced choices -------------------------------------------------------------------

def _kernel_value(key, cmdline=CMDLINE):
    try:
        words = Path(cmdline).read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return None
    values = [word[len(key):] for word in words if word.startswith(key)]
    return values[-1] if values else None


def forced(kind, environ=None, cmdline=CMDLINE):
    """(choice, where it came from) for "asr" or "tts"; ("auto", None) when nothing is set.

    BEING_ASR / BEING_TTS win over being.asr= / being.tts=; for the
    recognizer, the older BEING_ASR_ENGINE counts last. A value that is not
    a known choice is skipped, and the note says so."""
    environ = os.environ if environ is None else environ
    choices = ASR_CHOICES if kind == "asr" else TTS_CHOICES
    sources = [(environ.get(ASR_ENV if kind == "asr" else TTS_ENV), ASR_ENV if kind == "asr" else TTS_ENV),
               (_kernel_value(ASR_ARG if kind == "asr" else TTS_ARG, cmdline),
                "the kernel command line")]
    if kind == "asr":
        sources.append((environ.get(LEGACY_ASR_ENV), LEGACY_ASR_ENV))
    for raw, source in sources:
        if raw is None or not raw.strip():
            continue
        value = raw.strip().lower()
        value = _ALIASES.get(value, value) if kind == "asr" else value
        if value in choices:
            return value, source
    return "auto", None


def asr_setting(environ=None, cmdline=CMDLINE):
    return forced("asr", environ, cmdline)[0]


# The Ivy Bridge speech smoke --------------------------------------------------------

def _sha256(path):
    import hashlib

    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# The libraries the smoke pins: the C API, and the ONNX Runtime beside it
# that holds the AVX kernels which would raise SIGILL.
SMOKE_PINNED = ("libsherpa-onnx-c-api.so", "libonnxruntime.so")


def _smoke_pass(report):
    stages = report.get("stages") if isinstance(report.get("stages"), list) else []
    moonshine = [stage for stage in stages if isinstance(stage, dict) and stage.get("stage") == "moonshine"]
    return report.get("passed") is True and report.get("sigill") is False and bool(moonshine) \
        and all(stage.get("passed") is True for stage in moonshine)


def sherpa_smoke_passed(state_dir=None, paths=SMOKE_VERDICTS, library=None):
    """(ok, reason): may "auto" load Moonshine in this process? Blocking (it hashes the runtime).

    Yes only with a smoke report whose stages all passed with no SIGILL, a
    passed Moonshine stage among them, and a runtime_sha256 pin that the
    installed sherpa-onnx C API and the ONNX Runtime next to it both match
    byte for byte. A report without the pin, or a library missing, is no pass:
    a report kept in /mind outlives updates that change the runtime. A report
    that names the build it was recorded on (one made on this laptop) counts
    only on that build.

    The laptop's own record (state_dir/SMOKE_STATE) is read first. When it
    names this build and does not pin other runtime bytes, it decides on its
    own, pass or fail: a failure on this CPU outweighs a pass shipped in the
    image from QEMU, which only imitates an Ivy Bridge. Only without such a
    record do the shipped verdicts count."""
    reasons = []
    digests = {}

    def installed():
        """The installed bytes' digests, or None (with the reason noted) when they cannot be read."""
        if not digests:
            try:
                from . import sherpa

                api = Path(library) if library is not None else sherpa.library_path()
                # The C API finds its runtime in its own folder (the release's RUNPATH is $ORIGIN).
                digests["libsherpa-onnx-c-api.so"] = _sha256(api)
                digests["libonnxruntime.so"] = _sha256(api.parent / "libonnxruntime.so")
            except (OSError, ImportError) as error:
                reasons.append(f"the installed sherpa-onnx could not be checked ({error})")
                return None
        return digests

    def pins(report):
        pinned = report.get("runtime_sha256")
        if isinstance(pinned, dict) and all(isinstance(pinned.get(name), str) for name in SMOKE_PINNED):
            return pinned
        return None

    if state_dir is not None:
        path = Path(state_dir) / SMOKE_STATE
        report = _read_json(path)
        if report and report.get("build", build_id()) == build_id():
            pinned = pins(report)
            current = installed()
            if current is None:
                return False, "; ".join(reasons)
            if pinned is None or all(current[name] == pinned[name] for name in SMOKE_PINNED):
                if not _smoke_pass(report):
                    return False, f"{path} records a failed smoke on this laptop"
                if pinned is None:
                    return False, f"{path} does not pin the sherpa-onnx and ONNX Runtime bytes it tested"
                return True, None
            reasons.append(f"{path} tested another sherpa-onnx or ONNX Runtime build")
        elif report:
            reasons.append(f"{path} was recorded on another build")
    for path in paths:
        report = _read_json(path)
        if not report:
            continue
        if not _smoke_pass(report):
            reasons.append(f"{path} does not record a pass")
            continue
        if "build" in report and report.get("build") != build_id():
            reasons.append(f"{path} was recorded on another build")
            continue
        pinned = pins(report)
        if pinned is None:
            reasons.append(f"{path} does not pin the sherpa-onnx and ONNX Runtime bytes it tested")
            continue
        current = installed()
        if current is None:
            break
        if any(current[name] != pinned[name] for name in SMOKE_PINNED):
            reasons.append(f"{path} tested another sherpa-onnx or ONNX Runtime build")
            continue
        return True, None
    return False, "; ".join(reasons) or "no Ivy Bridge speech smoke test is recorded on this build"


# Recording the smoke on this laptop -------------------------------------------------
#
# The same three stages as lab/sherpa_avx_smoke.py (Silero VAD, a Moonshine
# decode of the pack's own test recording, one Piper sentence), with the
# installed runtime and models, each in a Python process of its own: an
# instruction this CPU lacks kills only that process (SIGILL), never the voice.
# A pass is pinned to the bytes of the installed sherpa-onnx C API and ONNX
# Runtime, and to this build. With record, the verdict (pass or fail) replaces
# the mind's SMOKE_STATE, so a failure also withdraws an older pass. The voice
# picks a pass up at its next engine choice: a restart, or its next idle
# refresh (refresh_asr switches on the speed the smoke kept). A failure does
# not unload a Moonshine already running; restart being-voice for that.
#
#     PYTHONPATH=/usr/lib/being python3 -m mind.voice_engines smoke --record

SMOKE_STAGES = ("vad", "moonshine", "piper")
SMOKE_SENTENCE = "Hello. I am Pip, and this is my natural voice."
SMOKE_STAGE_TIMEOUT_S = 300
SMOKE_MARK = "SMOKE "
SMOKE_VERSION = 1


def _sigill(status):
    import signal

    return status in (-signal.SIGILL, 128 + signal.SIGILL)


def runtime_pins(library=None):
    """{library name: sha256} of the installed C API and the ONNX Runtime beside it. Raises OSError."""
    from . import sherpa

    api = Path(library) if library is not None else sherpa.library_path()
    return {"libsherpa-onnx-c-api.so": _sha256(api), "libonnxruntime.so": _sha256(api.parent / "libonnxruntime.so")}


def _cpu():
    """The CPU's model name and the instruction sets the smoke cares about."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    model = next((line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("model name")),
                 None)
    flags = next((line.split(":", 1)[1].split() for line in text.splitlines() if line.startswith("flags")), [])
    return {"model": model, **{flag: flag in flags for flag in ("avx", "avx2", "fma", "f16c")}}


def smoke_stage(stage, *, root=None, library=None, sentence=SMOKE_SENTENCE, out=None):
    """Run one stage in this process and print its SMOKE line (the child's side)."""
    import sys

    from . import asr_sherpa, sherpa

    out = out or sys.stdout
    root = Path(root) if root is not None else asr_sherpa.ROOT
    report = {"stage": stage, "versions": sherpa.version(library)}
    started = time.perf_counter()
    if stage == "vad":
        wav = asr_sherpa.test_wav(root, "tiny")
        if wav is None:
            raise FileNotFoundError("the Moonshine pack has no test recording")
        samples, rate = sherpa.read_wav(wav)
        samples = sherpa.resample(samples, rate, 16000)
        vad = asr_sherpa.load_vad(root, library)
        try:
            segments = []
            for index in range(0, len(samples) - asr_sherpa.WINDOW + 1, asr_sherpa.WINDOW):
                vad.accept(samples[index:index + asr_sherpa.WINDOW])
                segments += vad.pop_segments()
            vad.flush()
            segments += vad.pop_segments()
        finally:
            vad.close()
        report.update(segments=len(segments), passed=len(segments) >= 1)
    elif stage == "moonshine":
        wav = asr_sherpa.test_wav(root, "tiny")
        if wav is None:
            raise FileNotFoundError("the Moonshine pack has no test recording")
        samples, rate = sherpa.read_wav(wav)
        recognizer = asr_sherpa.load_moonshine(root, "tiny", library)
        try:
            loaded = time.perf_counter()
            text = recognizer.transcribe(samples, rate)
            report.update(chars=len(text.strip()), rtf=round((time.perf_counter() - loaded) / (len(samples) / rate), 4),
                          passed=bool(text.strip()))
        finally:
            recognizer.close()
    elif stage == "piper":
        from .tts_sherpa import PiperEngine

        engine = PiperEngine(library=library)
        folder = tempfile.mkdtemp(prefix="being-smoke-")
        try:
            seconds = engine.synthesize_blocking(sentence, Path(folder) / "smoke.wav", 1.0)
            report.update(audio_seconds=round(seconds, 3), passed=seconds > 0)
        finally:
            engine.close()
            shutil.rmtree(folder, ignore_errors=True)
    else:
        raise ValueError(f"unknown smoke stage {stage!r}")
    report["seconds"] = round(time.perf_counter() - started, 3)
    with suppress(OSError):
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as maps:
            report["runtime_paths"] = sorted({line.split()[-1] for line in maps
                                              if "libonnxruntime" in line or "libsherpa" in line})
    print(SMOKE_MARK + json.dumps(report), file=out, flush=True)


def judge_stage(stage, status, output, library_dir):
    """A stage's verdict from its exit status and its one SMOKE line."""
    result = {"stage": stage, "status": status, "sigill": _sigill(status), "passed": False}
    lines = [line[len(SMOKE_MARK):] for line in (output or "").splitlines() if line.startswith(SMOKE_MARK)]
    if status != 0 or len(lines) != 1:
        result["error"] = "it was killed by SIGILL" if result["sigill"] else \
            f"exit status {status}" if status != 0 else "it gave no verdict"
        return result
    try:
        report = json.loads(lines[0])
    except ValueError:
        result["error"] = "its verdict could not be read"
        return result
    if not isinstance(report, dict):
        result["error"] = "its verdict could not be read"
        return result
    paths = report.get("runtime_paths")
    here = str(Path(library_dir).resolve())
    loaded_here = isinstance(paths, list) and bool(paths) and all(
        isinstance(path, str) and str(Path(path).resolve().parent) == here for path in paths)
    result.update({key: value for key, value in report.items() if key not in ("stage", "passed")})
    result["passed"] = report.get("passed") is True and loaded_here
    if not loaded_here:
        result["error"] = "it loaded a different sherpa-onnx or ONNX Runtime"
    elif report.get("passed") is not True:
        result["error"] = "its engine gave no result"
    return result


def _stage_environment(environ=None):
    environ = dict(os.environ if environ is None else environ)
    package = str(Path(__file__).resolve().parent.parent)
    environ["PYTHONPATH"] = package + (os.pathsep + environ["PYTHONPATH"] if environ.get("PYTHONPATH") else "")
    environ["PYTHONDONTWRITEBYTECODE"] = "1"
    return environ


def run_stage(stage, *, python=None, timeout=SMOKE_STAGE_TIMEOUT_S, environ=None):
    """(exit status, standard output) of one stage in a Python process of its own."""
    import subprocess
    import sys

    try:
        done = subprocess.run([python or sys.executable, "-m", "mind.voice_engines", "smoke-stage", stage],
                              capture_output=True, text=True, timeout=timeout, env=_stage_environment(environ),
                              check=False)
    except subprocess.TimeoutExpired:
        return None, f"it took more than {timeout} s"
    except OSError as error:
        return None, f"it could not start ({error})"
    return done.returncode, done.stdout + done.stderr[-2000:]


def hardware_smoke(state_dir=None, *, record=False, library=None, runner=run_stage, now=None):
    """Run the speech smoke on this laptop. Returns the report; with `record`,
    also keeps it as the mind's SMOKE_STATE (state_dir), pass or fail. Blocking:
    about a minute."""
    from . import sherpa

    api = Path(library) if library is not None else sherpa.library_path()
    report = {"version": SMOKE_VERSION, "source": "hardware", "build": build_id(), "cpu": _cpu(),
              "measured": time.time() if now is None else now, "stages": [], "passed": False, "sigill": False}
    try:
        report["runtime_sha256"] = runtime_pins(api)
    except OSError as error:
        report["error"] = f"the installed sherpa-onnx could not be read ({error})"
    else:
        for stage in SMOKE_STAGES:
            status, output = runner(stage)
            if status is None:
                result = {"stage": stage, "status": None, "sigill": False, "passed": False, "error": output}
            else:
                result = judge_stage(stage, status, output, api.parent)
            report["stages"].append(result)
        report["sigill"] = any(stage["sigill"] for stage in report["stages"])
        report["passed"] = all(stage["passed"] for stage in report["stages"])
        try:
            if runtime_pins(api) != report["runtime_sha256"]:
                report["passed"] = False
                report["error"] = "the installed sherpa-onnx changed during the test"
        except OSError as error:
            report["passed"] = False
            report["error"] = f"the installed sherpa-onnx could not be read again ({error})"
    if record:
        if state_dir is None:
            raise ValueError("recording a smoke verdict needs the mind folder")
        _write_json(Path(state_dir) / SMOKE_STATE, report)
        _record_smoke_speed(state_dir, report, now)
    return report


def _record_smoke_speed(state_dir, report, now=None):
    """A passed smoke's Moonshine decode is a speed measurement too (one
    thread, as the voice runs it): keep it, so the next choice need not
    measure again."""
    rtf = next((stage.get("rtf") for stage in report.get("stages", []) if stage.get("stage") == "moonshine"), None)
    if report.get("passed") is not True or type(rtf) not in (int, float) or not 0 < rtf < 100:
        return None
    with suppress(OSError):
        return record(state_dir, "asr", asr_key(report.get("build") or build_id(), "tiny"),
                      {"rtf": rtf, "runs": [rtf]}, now)
    return None


def smoke_lines(report):
    """Plain lines describing a smoke report, one per stage and one verdict."""
    lines = []
    for stage in report.get("stages", []):
        verdict = "SIGILL" if stage.get("sigill") else "passed" if stage.get("passed") else "failed"
        detail = f" ({stage['error']})" if stage.get("error") else ""
        lines.append(f"{stage.get('stage')}: {verdict}{detail}")
    if report.get("error"):
        lines.append(report["error"])
    lines.append("The speech smoke passed: Moonshine may be chosen automatically on this build."
                 if report.get("passed") else "The speech smoke did not pass: Moonshine stays off unless forced.")
    return lines


def main(argv=None):
    """`smoke [--record] [--json] [--mind DIR]`, and the internal `smoke-stage NAME`."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python3 -m mind.voice_engines",
                                     description="The voice's engine checks on this laptop.")
    commands = parser.add_subparsers(dest="command", required=True)
    smoke_command = commands.add_parser("smoke", help="run the Ivy Bridge speech smoke here")
    smoke_command.add_argument("--record", action="store_true",
                               help="keep the verdict in the mind, so the automatic choice can use Moonshine")
    smoke_command.add_argument("--mind", type=Path, help="the mind folder (default: the voice's own)")
    smoke_command.add_argument("--json", action="store_true", help="print the report as JSON")
    stage_command = commands.add_parser("smoke-stage", help=argparse.SUPPRESS)
    stage_command.add_argument("stage", choices=SMOKE_STAGES)
    args = parser.parse_args(argv)
    if args.command == "smoke-stage":
        smoke_stage(args.stage)
        return 0
    state_dir = args.mind
    if state_dir is None and args.record:
        from .profile import default_mind_dir

        state_dir = default_mind_dir()
    report = hardware_smoke(state_dir, record=args.record)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for line in smoke_lines(report):
            print(line)
        if args.record:
            print(f"Kept in {Path(state_dir) / SMOKE_STATE}.")
    sys.stdout.flush()
    return 0 if report["passed"] else 1


# Measuring ------------------------------------------------------------------------

def wav_seconds(path):
    with wave.open(str(path)) as audio:
        return audio.getnframes() / audio.getframerate()


def measure_tts(engine, runs=RUNS, clock=time.perf_counter):
    """Real-time factor of `engine.synthesize_blocking` on SENTENCE. Blocking.

    The voice is loaded first, so loading time is not counted.
    """
    engine.resident()
    factors = []
    with tempfile.TemporaryDirectory(prefix="being-rtf-") as folder:
        target = Path(folder) / "rtf.wav"
        for _ in range(runs):
            start = clock()
            seconds = engine.synthesize_blocking(SENTENCE, target, 1.0)
            elapsed = clock() - start
            if not seconds or seconds <= 0:
                raise RuntimeError("the voice produced no audio")
            factors.append(elapsed / seconds)
    return {"rtf": statistics.median(factors), "runs": factors}


def read_wav_16k(path):
    """Float samples of a mono 16-bit WAV, resampled to 16 kHz when it is not.

    The Moonshine packs' own test recordings are 24 kHz.
    """
    from .sherpa import read_wav, resample

    samples, rate = read_wav(path)
    return resample(samples, rate, 16000)


# A cold ONNX Runtime session's first decode allocates its memory arena and is
# slower than every later one. The resident engine pays that once, so the
# speed check decodes this much of the recording first and does not count it.
WARMUP_SECONDS = 1.0


def measure_asr(recognizer, wav, runs=1, clock=time.perf_counter, warmup=True):
    """Real-time factor of `recognizer.transcribe` on a test recording. Blocking.

    The recording is passed at its own rate (the recognizer resamples).
    """
    from .sherpa import read_wav

    samples, rate = read_wav(wav)
    seconds = len(samples) / rate
    if seconds <= 0:
        raise ValueError("the test recording is empty")
    if warmup:
        recognizer.transcribe(samples[:int(WARMUP_SECONDS * rate)], rate)
    factors, text = [], ""
    for _ in range(runs):
        start = clock()
        text = recognizer.transcribe(samples, rate)
        factors.append((clock() - start) / seconds)
    return {"rtf": statistics.median(factors), "runs": factors, "text": text}


# The sound probe ------------------------------------------------------------------

# One short word the natural voice must say before it is chosen.
PROBE_TEXT = "Hello."
# Seconds the probe may take once the voice is loaded (an idle Ivy Bridge
# needs well under one; an emulated laptop several).
PROBE_TIMEOUT_S = 15.0
# A probe whose loudest sample is under this (16-bit) made no sound (-60 dBFS).
PROBE_SILENCE = 32
PROBE_MAX_SECONDS = 10
# Natural voices that made no sound in this process, by tts_key: RAM only, so
# a restart or a new build tests again.
_silent = {}
# The same, for other processes (the mind, Setup): {"key", "reason", "pid"},
# on a RAM filesystem, and believed only while that process lives.
RUN_STATE = Path("/run/being-voice/tts.json")


def silent_reason(build, engine):
    """Why the natural voice made no sound in this process for this build, or None."""
    return _silent.get(tts_key(build, engine))


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reported_silent(build, engine, path=None):
    """Why the voice organ (a live process) says the natural voice made no
    sound for this build, or None."""
    data = _read_json(RUN_STATE if path is None else path)
    why = data.get("reason")
    if data.get("key") != tts_key(build, engine) or not isinstance(why, str) or not why.strip():
        return None
    return why.strip()[:200] if _alive(data.get("pid")) else None


def _keep_silent(build, engine, why):
    record_key = tts_key(build, engine)
    _silent[record_key] = why
    with suppress(OSError):
        _write_json(RUN_STATE, {"key": record_key, "reason": str(why)[:200], "pid": os.getpid()})


def _clear_silent(build, engine):
    """The natural voice spoke here: drop a report this process (or a dead one) left."""
    data = _read_json(RUN_STATE)
    if data.get("key") == tts_key(build, engine) and (data.get("pid") == os.getpid() or not _alive(data.get("pid"))):
        with suppress(OSError):
            RUN_STATE.unlink()


def _peak(path):
    """The loudest sample of a 16-bit WAV, reading at most PROBE_MAX_SECONDS."""
    with wave.open(str(path)) as audio:
        if audio.getsampwidth() != 2:
            raise ValueError(f"its audio is {8 * audio.getsampwidth()}-bit, not 16-bit")
        frames = audio.readframes(min(audio.getnframes(), PROBE_MAX_SECONDS * audio.getframerate()))
    samples = array("h")
    samples.frombytes(frames[:len(frames) - len(frames) % 2])
    if not samples:
        return None
    return max(max(samples), -min(samples))


def _probe(engine, build, timeout):
    """(ok, reason, busy): `busy` is True when the voice's worker is still on the probe."""
    if getattr(engine, "probe_passed", False):
        return True, None, False
    known = silent_reason(build, engine) or getattr(engine, "silenced", None)
    if known:
        return False, known, False

    def failed(why, busy=False):
        _keep_silent(build, engine, why)
        silence = getattr(engine, "silence", None)
        if callable(silence):
            silence(RuntimeError(why))      # a voice in use speaks Flite from now on
        return False, why, busy

    # The probe's note says what happened; the voice need not log it again.
    with suppress(AttributeError):
        engine.quiet = True
    folder = tempfile.mkdtemp(prefix="being-probe-")
    future = None
    try:
        try:
            engine.resident()
        except Exception as error:
            return failed(f"it would not load: {_reason(error)}")
        future = engine.executor().submit(engine.synthesize_blocking, PROBE_TEXT, Path(folder) / "probe.wav", 1.0)
        try:
            seconds = future.result(timeout)
        except FutureTimeout:
            return failed(f"it took more than {timeout:g} s to say one word", busy=True)
        except Exception as error:
            return failed(_reason(error))
        try:
            peak = _peak(Path(folder) / "probe.wav") if type(seconds) in (int, float) and seconds > 0 else None
        except (OSError, EOFError, ValueError, wave.Error) as error:
            return failed(f"its audio could not be read: {_reason(error)}")
        if peak is None:
            return failed("it gave no samples")
        if peak < PROBE_SILENCE:
            return failed("it gave only silence")
        with suppress(AttributeError):
            engine.probe_passed = True
        return True, None, False
    finally:
        if future is not None and not future.done():
            # The worker still holds the folder: tidy it when the probe ends.
            future.add_done_callback(lambda _: shutil.rmtree(folder, ignore_errors=True))
        else:
            shutil.rmtree(folder, ignore_errors=True)
        with suppress(AttributeError):
            engine.quiet = False


def probe_tts(engine, *, build=None, timeout=PROBE_TIMEOUT_S):
    """(ok, reason): does the natural voice make a sound? Blocking: loads the
    voice and says PROBE_TEXT once on its own worker, waiting at most `timeout`
    seconds for it. A failure is kept for this build in RAM and returned again
    without retrying; a pass is kept on the engine."""
    ok, why, _ = _probe(engine, build or build_id(), timeout)
    return ok, why


def _reason(error):
    text = " ".join(str(error).split())
    return (text or type(error).__name__)[:200]


def _watch(engine, build):
    """The voice organ's engine: a voice that breaks later silences itself
    (sticky), and that is kept for this build too."""
    with suppress(AttributeError):
        engine.sticky = True
    with suppress(AttributeError):
        engine.on_silent = lambda why: _keep_silent(build, engine, why)


def _give_back(engine, busy):
    """Unload a voice that failed its probe. One whose worker is still on the
    probe is unloaded on a thread of its own, so nobody waits for it."""
    if busy:
        threading.Thread(target=_unload, args=(engine,), name="being-piper-unload", daemon=True).start()
    else:
        _unload(engine)


def silent_note(why, preference):
    asked = " instead of the natural voice that was asked for" if preference == "piper" else ""
    return f"The natural voice made no sound in a test ({why}). Using the original voice{asked}."


# Choosing the voice ---------------------------------------------------------------

def _flite():
    from .tts import FliteEngine

    return FliteEngine()


def _piper(voice_dir=None, library=None):
    from .tts_sherpa import DEFAULT_VOICE, PiperEngine

    return PiperEngine(voice_dir or DEFAULT_VOICE, library=library)


def verified(engine):
    """(ok, reason) from an engine's signed-pack check (mind/model_pack.py),
    or (True, None) for an engine without one. Blocking the first time."""
    check = getattr(engine, "verified", None)
    if not callable(check):
        return True, None
    try:
        result = check()
    except Exception as error:  # a check that cannot run refuses the pack
        return False, f"the check failed: {error!r}"
    if isinstance(result, tuple) and len(result) == 2:
        return result[0] is True, result[1]
    return result is True, None


def tts_key(build, engine):
    return key(build, f"piper:{engine.voice_dir.name}:{engine.label().lower()}")


def choose_tts(preference=None, *, state_dir, build=None, voice_dir=None, engine=None, measure=False,
               now=None, library=None, probe=True, probe_timeout=PROBE_TIMEOUT_S):
    """Return (engine, note). The note says, in plain words, why Flite was kept.

    `preference` None reads the owner's saved choice. With `measure` set, a
    missing or week-old measurement is taken now (this blocks for a few seconds).
    With `probe` set (the default), the natural voice is chosen only after it
    says one word (see probe_tts); this blocks too, so call it off the event loop.
    """
    preference = load_preference(state_dir) if preference is None else preference
    if preference not in TTS_CHOICES:
        return _flite(), f"Unknown voice choice {preference!r}. Using the original voice."
    if preference == "flite":
        return _flite(), None
    piper = engine or _piper(voice_dir, library)
    # Only a voice made here is unloaded here. One the caller passed in may be
    # the voice in use; the caller gives it back after switching away from it.
    owned = engine is None
    if not piper.available():
        return _flite(), "The natural voice is not installed. Using the original voice."
    build = build or build_id()
    known = silent_reason(build, piper) or getattr(piper, "silenced", None)
    if known:
        if owned:
            _unload(piper)
        return _flite(), silent_note(known, preference)
    trusted, reason = verified(piper)
    if not trusted:
        if owned:
            _unload(piper)
        return _flite(), f"The natural voice's model pack did not verify ({reason}). Using the original voice."
    if preference == "piper":
        return _sounding(piper, None, preference, build, owned, probe, probe_timeout)
    record_key = tts_key(build, piper)
    found = cached(state_dir, "tts", record_key, now)
    if found is None and measure:
        try:
            found = record(state_dir, "tts", record_key, measure_tts(piper), now)
        except Exception as error:
            if owned:
                _unload(piper)
            return _flite(), f"The natural voice could not be measured ({error}). Using the original voice."
    if found is None:
        # Natural first: the owner found the original voice hard to listen to.
        # It is measured later, at rest, and set aside only if it is too slow.
        return _sounding(piper, "Using the natural voice; its speed on this laptop is measured later, at rest.",
                         preference, build, owned, probe, probe_timeout)
    if found["rtf"] <= TTS_RTF_LIMIT:
        return _sounding(piper, f"Using the natural voice: it measured {found['rtf']:.2f} of real time.",
                         preference, build, owned, probe, probe_timeout)
    if owned:
        _unload(piper)
    return _flite(), (f"The natural voice measured {found['rtf']:.2f} of real time, over "
                      f"{TTS_RTF_LIMIT:g}. Using the original voice.")


def _sounding(piper, note, preference, build, owned, probe, timeout):
    """(piper, note) when the natural voice makes a sound (or no probe is asked
    for), else Flite with the reason. Never lets Pip go silent."""
    if probe:
        ok, why, busy = _probe(piper, build, timeout)
        if not ok:
            if owned:
                _give_back(piper, busy)
            return _flite(), silent_note(why, preference)
        _clear_silent(build, piper)
    _watch(piper, build)
    return piper, note


def _unload(engine):
    """Give back a natural voice that was loaded only to be measured."""
    if getattr(engine, "loaded", False) or getattr(engine, "_executor", None) is not None:
        with suppress(Exception):
            engine.close()


# A measurement that failed (the model would not load or decode) is not tried
# again on every refresh: it waits this long. RAM only, so a restart retries.
FAILURE_BACKOFF_S = 6 * 3600
_failures = {}


def _failed(kind, record_key, clock=time.monotonic):
    _failures[(kind, record_key)] = clock()


def _backing_off(kind, record_key, clock=time.monotonic):
    when = _failures.get((kind, record_key))
    return when is not None and clock() - when < FAILURE_BACKOFF_S


async def refresh_tts(current, preference=None, *, state_dir, build=None, voice_dir=None, engine=None,
                      idle=None, now=None, library=None):
    """Measure the natural voice when due and the laptop is idle.

    `idle` (cpu_quiet when None) is asked before each run, on a worker thread.

    Returns (engine, note) when the choice changes, (None, note) when the
    natural voice measured fast enough but made no sound (Flite stays), and
    otherwise None. The measurement runs on the voice's own worker thread.
    """
    import asyncio

    idle = cpu_quiet if idle is None else idle
    preference = load_preference(state_dir) if preference is None else preference
    if preference != "auto":
        return None
    if getattr(current, "silenced", None):
        return None     # the natural voice in use made no sound: Flite for this process
    piper = engine or (current if getattr(current, "name", None) == "piper" else _piper(voice_dir, library))
    if not piper.available():
        return None
    if silent_reason(build or build_id(), piper) is not None:
        return None     # it made no sound in this process: never measured or chosen again here
    # The pack check may hash the voice the first time: never on the event loop.
    if not (await asyncio.to_thread(verified, piper))[0]:
        if piper is not current:
            _unload(piper)
        return None
    build = build or build_id()
    record_key = tts_key(build, piper)
    if cached(state_dir, "tts", record_key, now) is None:
        if _backing_off("tts", record_key) or not await asyncio.to_thread(idle):
            if piper is not current:
                _unload(piper)
            return None
        loop = asyncio.get_running_loop()
        try:
            # One sentence per job on the voice's own thread, so a clause the
            # owner is waiting for queues behind one sentence at most, not three.
            factors = []
            for _ in range(RUNS):
                if factors and not await asyncio.to_thread(idle):
                    if piper is not current:
                        _unload(piper)
                    return None     # busy again: measure another time
                one = await loop.run_in_executor(piper.executor(), measure_tts, piper, 1)
                factors.extend(one["runs"])
            result = {"rtf": statistics.median(factors), "runs": factors}
        except Exception:
            # A voice that will not load or speak: give its memory back and wait
            # a while before trying again, instead of reloading 64 MB every refresh.
            _failed("tts", record_key)
            if piper is not current:
                _unload(piper)
            return None
        record(state_dir, "tts", record_key, result, now)
    chosen, note = choose_tts("auto", state_dir=state_dir, build=build, engine=piper, now=now, probe=False)
    if chosen is piper and piper is not current:
        # A voice switched to must make a sound: probe it off the event loop.
        ok, why, busy = await asyncio.to_thread(_probe, piper, build, PROBE_TIMEOUT_S)
        if not ok:
            _give_back(piper, busy)
            return None, silent_note(why, "auto")     # the voice logs it and keeps Flite
        _clear_silent(build, piper)
    if chosen is not piper and piper is not current:
        _unload(piper)          # loaded only to be measured, and not chosen
    if getattr(chosen, "name", None) == getattr(current, "name", None):
        if piper is not current:
            _unload(piper)
        return None
    # When the voice in use is the one dropped, the voice organ closes it after
    # switching, on the thread that may still be speaking with it.
    return chosen, note


def tts_status(state_dir, *, build=None, voice_dir=None, engine=None, now=None, library=None):
    """What the voice page and "which voice" need: choice, install state, measurement."""
    piper = engine or _piper(voice_dir, library)
    installed = piper.available()
    build = build or build_id()
    found = cached(state_dir, "tts", tts_key(build, piper), now) if installed else None
    silent = (silent_reason(build, piper) or getattr(piper, "silenced", None)
              or reported_silent(build, piper)) if installed else None
    preference = load_preference(state_dir)
    if silent:
        active = "flite"
    elif preference == "auto":
        active = "piper" if installed and (found is None or found["rtf"] <= TTS_RTF_LIMIT) else "flite"
    else:
        active = "piper" if preference == "piper" and installed else "flite"
    return {"preference": preference, "installed": installed, "label": piper.label(),
            "rtf": found["rtf"] if found else None, "active": active, "silent": silent}


# Choosing the ears ----------------------------------------------------------------

# Replaceable in tests: the smoke check the "auto" choice consults.
smoke = sherpa_smoke_passed


def asr_key(build, size):
    return key(build, f"moonshine-{size}")


def choose_asr(wake, preference=None, *, state_dir, build=None, root=None, size="tiny", library=None,
               measure=False, now=None, factory=None, wake_word="Hey Pip"):
    """Return (recognizer, note): a SherpaRecognizer around `wake`, or `wake` itself.

    `wake` is the PocketSphinx recognizer; it keeps the wake phrase either way.
    `preference` comes from BEING_ASR_ENGINE when None: "auto" (default),
    "moonshine" (skip the speed rule) or "pocketsphinx".
    """
    from . import asr_sherpa, sherpa

    if preference is None:
        preference = asr_setting()
    preference = _ALIASES.get(preference, preference)
    if preference not in ASR_CHOICES:
        return wake, f"Unknown speech recognizer {preference!r}. Using PocketSphinx."
    if preference == "pocketsphinx":
        return wake, None
    root = Path(root) if root is not None else asr_sherpa.ROOT
    absent = asr_sherpa.missing(root, size, library)
    if absent:
        return wake, f"Moonshine is not installed ({len(absent)} files missing). Using PocketSphinx."
    if preference == "auto":
        safe, reason = smoke(state_dir, library=library)
        if not safe:
            return wake, f"Moonshine waits for a passed Ivy Bridge speech test ({reason}). Using PocketSphinx."
    build = build or build_id()
    record_key = asr_key(build, size)
    found = cached(state_dir, "asr", record_key, now)
    if preference == "auto" and found is not None and found["rtf"] > ASR_RTF_LIMIT:
        return wake, (f"Moonshine measured {found['rtf']:.2f} of real time, over {ASR_RTF_LIMIT:g}. "
                      "Using PocketSphinx.")
    if preference == "auto" and found is None and not measure:
        return wake, "Moonshine has not been measured on this laptop yet. Using PocketSphinx."
    wav = asr_sherpa.test_wav(root, size) if preference == "auto" and found is None else None
    if preference == "auto" and found is None and wav is None:
        # Checked before loading anything: there is nothing to measure with.
        return wake, "Moonshine has no test recording to measure with. Using PocketSphinx."
    build_engine = factory or (lambda: asr_sherpa.SherpaRecognizer.load(
        wake, root=root, size=size, library=library, wake_word=wake_word))
    try:
        engine = build_engine()
    except sherpa.Unavailable as error:
        return wake, f"Moonshine is unavailable: {error.reason}. Using PocketSphinx."
    if preference == "moonshine":
        return engine, None
    if found is None:
        try:
            found = record(state_dir, "asr", record_key, measure_asr(engine.recognizer, wav), now)
        except Exception as error:
            _release(engine)
            return wake, f"Moonshine could not be measured ({error}). Using PocketSphinx."
        if found["rtf"] > ASR_RTF_LIMIT:
            _release(engine)
            return wake, (f"Moonshine measured {found['rtf']:.2f} of real time, over {ASR_RTF_LIMIT:g}. "
                          "Using PocketSphinx.")
    return engine, f"Using Moonshine after the wake phrase: it measured {found['rtf']:.2f} of real time."


async def refresh_asr(current, wake, *, state_dir, build=None, root=None, size="tiny", library=None,
                      idle=None, now=None, factory=None, wake_word="Hey Pip"):
    """Measure Moonshine when due and the laptop is idle (auto only).

    Returns (recognizer, note) when the voice should switch to Moonshine,
    otherwise None. Loading and measuring run in a worker thread; `wake` is
    only wrapped there, never fed. `idle` (cpu_quiet when None) runs on a
    worker thread too. A speed already kept for this build (the hardware
    smoke records one) is not measured again: when it is within the limit
    and the smoke now passes, the refresh switches to Moonshine, so a smoke
    recorded while the voice runs is taken up without a restart.
    """
    import asyncio

    from . import asr_sherpa

    idle = cpu_quiet if idle is None else idle

    if asr_setting() != "auto" or current is not wake:
        return None
    build = build or build_id()
    record_key = asr_key(build, size)
    root = Path(root) if root is not None else asr_sherpa.ROOT
    found = cached(state_dir, "asr", record_key, now)
    if (found is not None and found["rtf"] > ASR_RTF_LIMIT) or _backing_off("asr", record_key):
        return None
    # Nothing installed, or (with no speed kept) nothing to measure with: do not load the models every refresh.
    if asr_sherpa.missing(root, size, library) or (found is None and asr_sherpa.test_wav(root, size) is None):
        return None
    if not await asyncio.to_thread(idle):
        return None
    # Never load Moonshine to measure it before the CPU is known to run it.
    if not (await asyncio.to_thread(smoke, state_dir, library=library))[0]:
        return None
    engine, note = await asyncio.to_thread(
        choose_asr, wake, "auto", state_dir=state_dir, build=build, root=root, size=size, library=library,
        measure=found is None, now=now, factory=factory, wake_word=wake_word)
    if engine is wake and (found is not None or cached(state_dir, "asr", record_key, now) is None):
        _failed("asr", record_key)     # it would not load or measure; try again later, not every refresh
    return (engine, note) if engine is not wake else None


def _release(engine):
    """Free the sherpa parts of an engine that will not be used, keeping its wake recognizer."""
    for part in (getattr(engine, "recognizer", None), getattr(engine, "vad", None)):
        with suppress(Exception):
            part.close()


# What the owner can say -----------------------------------------------------------

_NATURAL = re.compile(
    r"\b(?:use|switch to|change to|go (?:back )?to|try|speak (?:in|with))\s+(?:your|the)\s+"
    r"(?:natural|nicer|new|human|alba|piper)\s+voice\b", re.I)
_ORIGINAL = re.compile(
    r"\b(?:use|switch to|change to|go back to|back to|speak (?:in|with))\s+(?:your|the)\s+"
    r"(?:original|old|first|usual|robot|robotic|flite)\s+voice\b", re.I)
_AUTO = re.compile(r"\b(?:choose|pick) (?:your|the) (?:own )?voice (?:yourself|automatically)\b", re.I)
_WHICH = re.compile(
    r"\b(?:which|what) voice (?:are you using|is this|are you speaking (?:in|with)|do you use)\b", re.I)


def may_match(text):
    text = text or ""
    return bool(_NATURAL.search(text) or _ORIGINAL.search(text) or _AUTO.search(text) or _WHICH.search(text))


def _status(ctx):
    """tts_status, with the engine the voice organ reports it really speaks with.

    Whether the natural voice made no sound comes from the voice organ's own
    report (RUN_STATE), never from its speaking Flite alone: it also speaks
    Flite while the natural voice loads, and when Flite is forced."""
    status = tts_status(ctx.state_dir)
    active = (ctx.voice or {}).get("tts")
    if active in ("piper", "flite"):
        status["active"] = active
        if active == "piper":
            status["silent"] = None
    return status


def _fell_back(status):
    """True when the original voice speaks because the natural one made no sound,
    and the owner did not choose the original voice."""
    return bool(status["active"] != "piper" and status["installed"] and status["preference"] != "flite"
                and status.get("silent"))


def _which(status):
    if _fell_back(status):
        return ("[neutral] I'm using my original voice. My natural voice made no sound on this "
                "laptop, so I switched.")
    if status["active"] == "piper":
        line = f"[neutral] I'm using my natural voice, {status['label']}."
        if status["preference"] == "auto":
            if status["rtf"] is None:
                line += " I'll check it keeps up on this laptop when I'm resting."
            else:
                line += " I chose it because it runs fast enough on this laptop."
        return line
    line = "[neutral] I'm using my original voice."
    if not status["installed"]:
        return line + " My natural voice isn't installed."
    if status["preference"] == "auto":
        if status["rtf"] is not None and status["rtf"] > TTS_RTF_LIMIT:
            return line + " My natural voice measured too slow on this laptop."
        # Still loading, or Flite forced at boot: not a promise either way.
        return "[neutral] I'm using my original voice for now."
    return line


async def handle(text, ctx):
    from .reply import Reply, clean, is_guest

    said = clean(text, getattr(ctx, "name", "pip"))
    if _WHICH.search(said):
        return Reply(_which(_status(ctx)))
    choice = "piper" if _NATURAL.search(said) else "flite" if _ORIGINAL.search(said) else \
        "auto" if _AUTO.search(said) else None
    if choice is None:
        return None
    if is_guest(ctx):
        return Reply(f"[neutral] Only {ctx.owner} can change my voice.")
    status = _status(ctx)
    if choice == "piper" and not status["installed"]:
        return Reply("[concern] My natural voice isn't installed, so I'll keep this one.")
    # The voice organ switches and keeps the choice in its settings.
    await ctx.send("voice.engine", tts=choice)
    if choice == "piper" and status["active"] != "piper" and status.get("silent"):
        # The choice is kept, but no promise is made: the voice keeps Flite while
        # the natural one is silent, whichever voice the owner chose before.
        return Reply("[concern] My natural voice made no sound on this laptop, so I'll keep this one for now.")
    if choice == "piper":
        slow = status["rtf"] is not None and status["rtf"] > 0.5
        return Reply("[joy] Okay, I'll use my natural voice." +
                     (" It may make me a little slower to answer." if slow else ""))
    if choice == "flite":
        return Reply("[neutral] Okay, I'll use my original voice.")
    return Reply("[neutral] Okay. I'll choose my voice by how fast it runs here.")


if __name__ == "__main__":
    raise SystemExit(main())
