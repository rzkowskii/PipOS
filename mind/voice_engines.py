"""Which voice and which ears: chosen by measurement on this laptop.

Two engines of each kind are possible:

- speaking: the original Flite voice, or the natural Piper voice (Alba);
- hearing after the wake: PocketSphinx, or Silero VAD plus Moonshine.

In "auto" (the default) the natural voice is used only if its idle real-time
factor, over 3 runs of a fixed 12-word sentence, is at most 0.35, and Moonshine
only if its files are present and its idle real-time factor on a test
recording is at most 0.3. A real-time factor is compute time over audio time,
so 0.35 means a 2-second clause takes 0.7 s. Measurements are kept in
/mind/audio/engines.json, keyed by build-id and voice or model, and taken
again after a week. Nothing is measured while the being is busy.

The owner's choice ("auto", "piper" or "flite") is the voice organ's: it is
the "tts" field of /mind/audio/voice.json, which the voice writes when the
mind sends `voice.engine`. Saying "use your natural voice", "use your original
voice" or "which voice are you using?" reaches `handle` below through the
everyday registry; the answer is fixed in code.

Nothing here imports the speech library until an engine is used.
"""

import json
import os
import re
import statistics
import tempfile
import time
import wave
from contextlib import suppress
from pathlib import Path

ENGINES_FILE = Path("audio") / "engines.json"
# The voice organ's settings; its "tts" field is the owner's engine choice.
VOICE_SETTINGS = Path("audio") / "voice.json"
BUILD_ID = Path("/etc/being/build-id")
TTS_CHOICES = ("auto", "piper", "flite")
ASR_CHOICES = ("auto", "moonshine", "pocketsphinx")
TTS_RTF_LIMIT = 0.35
ASR_RTF_LIMIT = 0.3
RUNS = 3
MAX_AGE_S = 7 * 24 * 3600
# Twelve words, the same every time, so measurements compare across builds.
SENTENCE = "I keep your timers and notes, and I remember what you say."
MAX_FILE_BYTES = 64 * 1024
# The 1-minute load average must be under this for a measurement to count as idle.
IDLE_LOAD = 0.7


# Files ----------------------------------------------------------------------------

def build_id(path=BUILD_ID):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
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
               now=None, library=None):
    """Return (engine, note). The note says, in plain words, why Flite was kept.

    `preference` None reads the owner's saved choice. With `measure` set, a
    missing or week-old measurement is taken now (this blocks for a few seconds).
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
    trusted, reason = verified(piper)
    if not trusted:
        if owned:
            _unload(piper)
        return _flite(), f"The natural voice's model pack did not verify ({reason}). Using the original voice."
    if preference == "piper":
        return piper, None
    build = build or build_id()
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
        return _flite(), "The natural voice has not been measured on this laptop yet. Using the original voice."
    if found["rtf"] <= TTS_RTF_LIMIT:
        return piper, f"Using the natural voice: it measured {found['rtf']:.2f} of real time."
    if owned:
        _unload(piper)
    return _flite(), (f"The natural voice measured {found['rtf']:.2f} of real time, over "
                      f"{TTS_RTF_LIMIT:g}. Using the original voice.")


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
                      idle=system_idle, now=None, library=None):
    """Measure the natural voice when due and the laptop is idle.

    Returns (engine, note) when the choice changes, otherwise None. The
    measurement runs on the voice's own worker thread.
    """
    import asyncio

    preference = load_preference(state_dir) if preference is None else preference
    if preference != "auto":
        return None
    piper = engine or (current if getattr(current, "name", None) == "piper" else _piper(voice_dir, library))
    if not piper.available():
        return None
    # The pack check may hash the voice the first time: never on the event loop.
    if not (await asyncio.to_thread(verified, piper))[0]:
        if piper is not current:
            _unload(piper)
        return None
    build = build or build_id()
    record_key = tts_key(build, piper)
    if cached(state_dir, "tts", record_key, now) is None:
        if _backing_off("tts", record_key) or not idle():
            if piper is not current:
                _unload(piper)
            return None
        loop = asyncio.get_running_loop()
        try:
            # One sentence per job on the voice's own thread, so a clause the
            # owner is waiting for queues behind one sentence at most, not three.
            factors = []
            for _ in range(RUNS):
                if factors and not idle():
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
    chosen, note = choose_tts("auto", state_dir=state_dir, build=build, engine=piper, now=now)
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
    found = cached(state_dir, "tts", tts_key(build or build_id(), piper), now) if installed else None
    preference = load_preference(state_dir)
    if preference == "auto":
        active = "piper" if found and found["rtf"] <= TTS_RTF_LIMIT else "flite"
    else:
        active = "piper" if preference == "piper" and installed else "flite"
    return {"preference": preference, "installed": installed, "label": piper.label(),
            "rtf": found["rtf"] if found else None, "active": active}


# Choosing the ears ----------------------------------------------------------------

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
        preference = (os.environ.get("BEING_ASR_ENGINE") or "auto").strip().lower()
    preference = {"sherpa": "moonshine", "sherpa-onnx": "moonshine"}.get(preference, preference)
    if preference not in ASR_CHOICES:
        return wake, f"Unknown speech recognizer {preference!r}. Using PocketSphinx."
    if preference == "pocketsphinx":
        return wake, None
    root = Path(root) if root is not None else asr_sherpa.ROOT
    absent = asr_sherpa.missing(root, size, library)
    if absent:
        return wake, f"Moonshine is not installed ({len(absent)} files missing). Using PocketSphinx."
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
                      idle=system_idle, now=None, factory=None, wake_word="Hey Pip"):
    """Measure Moonshine when due and the laptop is idle (auto only).

    Returns (recognizer, note) when the voice should switch to Moonshine,
    otherwise None. Loading and measuring run in a worker thread; `wake` is
    only wrapped there, never fed.
    """
    import asyncio

    from . import asr_sherpa

    preference = (os.environ.get("BEING_ASR_ENGINE") or "auto").strip().lower()
    if preference != "auto" or current is not wake:
        return None
    build = build or build_id()
    record_key = asr_key(build, size)
    root = Path(root) if root is not None else asr_sherpa.ROOT
    if cached(state_dir, "asr", record_key, now) is not None or _backing_off("asr", record_key):
        return None
    # Nothing to measure with, or nothing installed: do not load the models every refresh.
    if asr_sherpa.missing(root, size, library) or asr_sherpa.test_wav(root, size) is None or not idle():
        return None
    engine, note = await asyncio.to_thread(
        choose_asr, wake, "auto", state_dir=state_dir, build=build, root=root, size=size, library=library,
        measure=True, now=now, factory=factory, wake_word=wake_word)
    if engine is wake and cached(state_dir, "asr", record_key, now) is None:
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
    status = tts_status(ctx.state_dir)
    active = (ctx.voice or {}).get("tts")
    if active in ("piper", "flite"):
        status["active"] = active
    return status


def _which(status):
    if status["active"] == "piper":
        line = f"[neutral] I'm using my natural voice, {status['label']}."
        if status["preference"] == "auto":
            line += " I chose it because it runs fast enough on this laptop."
        return line
    line = "[neutral] I'm using my original voice."
    if not status["installed"]:
        return line + " My natural voice isn't installed."
    if status["preference"] == "auto":
        if status["rtf"] is None:
            return line + " I haven't measured my natural voice here yet."
        return line + " My natural voice measured too slow on this laptop."
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
    if choice == "piper":
        slow = status["rtf"] is not None and status["rtf"] > 0.5
        return Reply("[joy] Okay, I'll use my natural voice." +
                     (" It may make me a little slower to answer." if slow else ""))
    if choice == "flite":
        return Reply("[neutral] Okay, I'll use my original voice.")
    return Reply("[neutral] Okay. I'll choose my voice by how fast it runs here.")
