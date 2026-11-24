"""Words in and out: command routing, Pip's voices, decoding, and hearing a file.

- `predict_route()` runs the mind's own code-phrase routing in this process
  (no bus): the workshop parser, the memory and time commands, the everyday
  skills' gates, greetings, and otherwise the model.
- `Sphinx` decodes 16 kHz audio with PocketSphinx dictation (as the voice
  does) and with a JSGF command grammar built from the workshop phrases.
  Moonshine runs in a child process (`python3 -m mind.diag_speech moonshine
  FILE...`): it loads native code that may not suit this CPU, and a crash
  there must not end the run.
- `check_tts()` renders fixed sentences with each voice, scores them by
  decoding (digital), then plays and records them (acoustic).
- `run_hear()` feeds a WAV to the running voice through the diagnostic
  microphone socket and reads what it heard and where the mind routed it
  from the journal.
- `run_voice()` is the owner-present flow: speech level, the command corpus,
  listening ratings and wake trials.

Owner speech is RAM-only (under /run/being-diag/tmp, removed) unless the
owner said yes to keeping recordings and transcripts for this run.
"""

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from . import diag_audio as audio
from .diag_report import RUN_DIR, check
from .diag_sound import (MIC_SOCKET, MIXER_STATE, VOICE, AudioSession, current_level, play, record_any,
                         record_argv, set_control)
from .diag_system import Journal, menu_has_the_screen, terminal_open

RATE = 16000
FRAME = 480
MIND = "being-mind.service"
CORTEX = "being-cortex.service"
HANDS = "being-hands.service"
# hands.py logs this when the Try menu holds the screen and it refuses the open.
MENU_REFUSAL = re.compile(r"not opened: the face is not running while the Try menu has the screen")
# hands.py when a terminal it began to open failed (it then sends app.refused reason=failed).
TERMINAL_FAILED = re.compile(r"\bterminal did not start\b")
PRIVACY = {"guest_start", "guest_end"}
# (what is said, the route the mind should take). A route ending in "." is a
# prefix; "code" is anything answered without the model.
COMMAND_PHRASES = (
    ("Hey Pip, open a terminal", "workshop.open"),
    ("Open the terminal", "workshop.open"),
    ("Pip, open a terminal", "workshop.open"),
    ("Close the terminal", "workshop."),
    ("What time is it?", "command.time"),
    ("Set a timer for one minute", "command.reminder"),
    ("Use your natural voice", "everyday.voice_engines"),
    ("How well can you hear me?", "everyday.body_talk"),
    ("Are you okay?", "everyday.doctor"),
    ("Remember that my orchid is called Silverleaf", "command."),
    ("What do you remember about my orchid?", "command."),
    ("What is two plus two?", "everyday.calc"),
    ("Tell me a joke", "code"),
    ("Good morning", "code"),
    ("Why is the sky blue?", "model"),
    ("They opened a new terminal at the airport", "model"),
)
# Spoken in every interactive corpus.
CORE_PHRASES = ("Hey Pip, open a terminal", "Open the terminal", "What time is it?", "Set a timer for one minute",
                "Use your natural voice")
# Said to the running voice by `pip-diag commands --e2e`: each wakes it in one
# breath, and none leaves anything behind (no timer, no changed setting).
E2E_PHRASES = (
    ("Hey Pip, open a terminal.", "workshop.open"),
    ("Hey Pip, what time is it?", "command.time"),
    ("Hey Pip, close the terminal.", "workshop."),
)
TTS_SENTENCES = (
    "Here's my terminal.",
    "Sorry, I didn't catch that.",
    "I keep your timers and notes, and I remember what you say.",
    "It is half past seven in the evening.",
    "Hey Pip, open a terminal.",
)
SPEECH_PROMPT = "The quick brown fox jumps over the lazy dog."
PIPER_RTF_LIMIT = 0.7
WAKE_GATES = {"1m": 9, "2m": 8}
KWS_OFFSET = 1500
KWS_DEFAULT_THRESHOLD = -675
HEARD = re.compile(r'heard: "(?P<text>.*)"(?P<rest>.*)$')
# "while speaking"/"while thinking" when the wake comes during a reply; "unknown" when
# the recognizer has no wake score to give.
WAKE = re.compile(r"heard the wake phrase(?P<oneshot> in one breath)?(?: while \w+)?: "
                  r"score (?P<score>-?\d+|unknown)")
NO_WORDS = re.compile(r"dictation ended after [\d.]+ s with no words")
ENDED = re.compile(r"dictation ended")
ROUTE = re.compile(r"\broute (\{.*\})\s*$")
# The voice is ready for the diagnostic microphone only when, after it says it
# connected, it logs its engine choice ("..., speech engine flite (auto), ..." or
# "speech engine: piper (auto)") or that it listens: connected alone is not listening.
CONNECTED = re.compile(r"diagnostic microphone connected")
READY = re.compile(r"listening for the wake phrase|capture open on|speech engine[: ]")


def ready_after_connect():
    """A Journal.wait match: true at the first READY line after the CONNECTED line."""
    connected = [False]

    def match(entry):
        if CONNECTED.search(entry.message):
            connected[0] = True
            return False
        return connected[0] and bool(READY.search(entry.message))

    return match


# Routing ----------------------------------------------------------------------

def _everyday_route(text):
    from . import everyday

    for module in everyday.Registry().modules():
        gate = getattr(module, "may_match", None)
        try:
            if gate is not None and gate(text):
                return f"everyday.{module.__name__.rsplit('.', 1)[-1]}"
        except Exception:
            continue
    return None


def predict_route(text, name="Pip", now=None, ctx=None):
    """The route the mind's step-by-step routing gives `text`, without the bus.

    Same order as Mind._run_turn: privacy commands, the workshop, commands,
    everyday skills (their gates), greetings, then the model. Pending
    questions and follow-ups are not modelled: a fresh, addressed turn."""
    from . import app_commands, commands

    command = commands.parse(text, name, now=now or datetime.now())
    if command is not None and command.kind == "ack":
        command = None
    if command is not None and command.kind in PRIVACY:
        return f"command.{command.kind}"
    app = app_commands.parse(text, ctx or app_commands.AppContext(), name.lower())
    if app is not None:
        return f"workshop.{app.kind}"
    if command is not None:
        return f"command.{command.kind}"
    route = _everyday_route(text)
    if route:
        return route
    if commands.greeting(text, name):
        return "greeting"
    return "model"


def route_matches(route, expected):
    if expected == "code":
        return route not in (None, "model")
    if expected.endswith("."):
        return bool(route) and route.startswith(expected)
    return route == expected


def grammar_phrases():
    """The workshop's own non-destructive phrases when it exports them, else the table's."""
    try:
        from . import app_commands

        exported = getattr(app_commands, "grammar_phrases", None)
        phrases = list(exported()) if callable(exported) else []
    except Exception:
        phrases = []
    if not phrases:
        phrases = [text for text, route in COMMAND_PHRASES if route != "model"]
    cleaned = []
    for phrase in phrases:
        words = " ".join(audio.words(phrase))
        words = re.sub(r"^(?:hey )?pip ", "", words)
        if words and words not in cleaned:
            cleaned.append(words)
    return cleaned


def check_commands(name="Pip", phrases=COMMAND_PHRASES):
    """Every phrase of the table through the in-process routing."""
    rows, wrong = [], []
    for text, expected in phrases:
        try:
            route = predict_route(text, name)
        except Exception as error:  # one broken parser must not hide the others
            route = f"error: {type(error).__name__}"
        ok = route_matches(route, expected)
        rows.append({"text": text, "expected": expected, "route": route, "ok": ok})
        if not ok:
            wrong.append(f"{text!r} -> {route} (expected {expected})")
    checks = [check("commands.routes", "commands", "fail" if wrong else "pass", rows,
                    "; ".join(wrong) if wrong else f"all {len(rows)} phrases route as expected",
                    "A command that reaches the model is answered slowly and wrongly." if wrong else "")]
    exported = grammar_phrases()
    from .app_commands import AppContext

    # Some phrases only mean something while an app is on screen ("show me your face").
    contexts = (None, AppContext(terminal="shown"), AppContext(terminal="minimised"),
                AppContext(monitor="shown"), AppContext(clock="shown"))
    reached_model = [phrase for phrase in exported
                     if all(predict_route(phrase, name, ctx=ctx) == "model" for ctx in contexts)]
    checks.append(check("commands.grammar", "commands", "warn" if reached_model else "pass",
                        {"phrases": len(exported), "reach_model": reached_model},
                        f"{len(exported)} grammar phrases" + (f"; these reach the model: {reached_model}"
                                                             if reached_model else ", all answered in code"),
                        "The grammar offers phrases the parser does not accept." if reached_model else ""))
    return checks


# Decoding ---------------------------------------------------------------------

def strip_address(text, name="Pip"):
    """Drop a leading "Hey Pip," (or the being's own name): the voice logs only the words after it."""
    word = re.escape(((name or "").split() or ["pip"])[-1])
    return re.sub(rf"^\s*(?:hey |hi |ok(?:ay)? )?{word}\b[\s,.!]*", "", text or "", flags=re.I).strip()


def wake_score(match):
    """The score of a WAKE match, or None when the voice logged it as unknown."""
    score = match["score"]
    return None if score == "unknown" else int(score)


def jsgf(phrases):
    alternatives = " | ".join(phrases)
    return ("#JSGF V1.0;\ngrammar diag;\n"
            f"public <command> = [<address>] <phrase>;\n<address> = [hey] pip;\n<phrase> = {alternatives};\n")


class Sphinx:
    """PocketSphinx dictation, plus a command grammar search, on whole utterances."""

    def __init__(self, recognizer=None):
        import ctypes

        if recognizer is None:
            from .recognition import Recognizer

            recognizer = Recognizer("pip")
        self.ctypes = ctypes
        self.recognizer = recognizer
        self.lib = recognizer.lib
        self.decoder = recognizer.decoder
        self.dictation = recognizer.dictation
        self.grammar_ready = None

    def _run(self, samples):
        ctypes, lib = self.ctypes, self.lib
        buffer = (ctypes.c_int16 * len(samples))(*samples)
        if lib.ps_start_utt(self.decoder) < 0:
            raise RuntimeError("could not start an utterance")
        lib.ps_process_raw(self.decoder, buffer, len(samples), 0, 1)
        lib.ps_end_utt(self.decoder)
        return (lib.ps_get_hyp(self.decoder, None) or b"").decode("utf-8", "replace")

    def dictate(self, samples):
        if self.lib.ps_activate_search(self.decoder, self.dictation) < 0:
            raise RuntimeError("could not switch to dictation")
        text = self._run(samples)
        return {"text": text, "confidence": self.recognizer.confidence()}

    def grammar(self, samples, phrases):
        """The best phrase of the grammar, or None when the grammar cannot be built."""
        if self.grammar_ready is None:
            try:
                function = self.lib.ps_add_jsgf_string
                function.restype = self.ctypes.c_int
                function.argtypes = (self.ctypes.c_void_p, self.ctypes.c_char_p, self.ctypes.c_char_p)
                self.grammar_ready = function(self.decoder, b"diag_grammar", jsgf(phrases).encode()) >= 0
            except (AttributeError, OSError):
                self.grammar_ready = False
        if not self.grammar_ready:
            return None
        if self.lib.ps_activate_search(self.decoder, b"diag_grammar") < 0:
            return None
        try:
            text = self._run(samples)
            return {"text": text, "confidence": self.recognizer.confidence()}
        finally:
            self.lib.ps_activate_search(self.decoder, self.dictation)

    def close(self):
        try:
            self.recognizer.close()
        except Exception:
            pass


def open_sphinx():
    """(Sphinx, None) or (None, why not)."""
    try:
        return Sphinx(), None
    except Exception as error:
        return None, f"PocketSphinx is not available here: {type(error).__name__}: {str(error)[:120]}"


def load_16k(path):
    """Mono 16 kHz samples of a WAV (the louder channel of a stereo one)."""
    rate, channels = audio.read_wav(path)
    channel = max(channels, key=audio.rms) if len(channels) > 1 else channels[0]
    return audio.resample(channel, rate, RATE)


def moonshine_ready():
    """(ready, reason): Moonshine installed and its pack verified."""
    try:
        from . import asr_sherpa

        absent = asr_sherpa.missing()
        if absent:
            return False, f"Moonshine is not installed ({absent[0]})"
        ok, reason = asr_sherpa.verified()
        return (True, None) if ok else (False, f"Moonshine's pack did not verify: {reason}")
    except Exception as error:
        return False, f"Moonshine could not be checked: {error}"


def moonshine(host, files, timeout=600):
    """{file: text} decoded by Moonshine in a child process, or {"error": ...}."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    result = host.run([sys.executable, "-m", "mind.diag_speech", "moonshine", *map(str, files)], timeout=timeout,
                      env=env)
    if result is None:
        return {"error": "the Moonshine child did not finish"}
    if result.code < 0:
        return {"error": f"Moonshine crashed with signal {-result.code} (not safe on this CPU?)"}
    texts = {}
    for line in result.out.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and "file" in item:
            texts[item["file"]] = item.get("text", "")
    if result.code and not texts:
        return {"error": f"Moonshine failed: {result.err.strip()[-200:]}"}
    return texts


def _moonshine_child(files):
    from . import asr_sherpa, sherpa

    recognizer = asr_sherpa.load_moonshine()
    for name in files:
        try:
            samples, rate = sherpa.read_wav(name)
            started = time.perf_counter()
            text = recognizer.transcribe(samples, rate)
            took = time.perf_counter() - started
            print(json.dumps({"file": name, "text": text, "rtf": round(took / max(len(samples) / rate, 1e-6), 3)}),
                  flush=True)
        except Exception as error:
            print(json.dumps({"file": name, "error": str(error)}), flush=True)
    return 0


# Pip's voices -----------------------------------------------------------------

def flite_voices(host):
    if host.which("flite") is None:
        return []
    result = host.run(["flite", "-lv"], timeout=10)
    listed = set(result.out.split()) if result is not None else set()
    return [voice for voice in ("slt", "kal16") if voice in listed or (voice == "slt" and not listed)]


def piper_engine():
    """The natural voice when it is installed and its pack verifies, else (None, why)."""
    try:
        from . import voice_engines

        engine = voice_engines._piper()
        # Diagnostics try every sentence and report each failure; the voice organ
        # instead stops using a natural voice that once made no sound.
        engine.sticky = False
        if not engine.available():
            return None, "the natural voice (Piper) is not installed"
        ok, reason = voice_engines.verified(engine)
        if not ok:
            return None, f"the natural voice did not verify: {reason}"
        return engine, None
    except Exception as error:
        return None, f"the natural voice could not load: {error}"


def synthesize(host, voice, text, path, *, piper=None):
    """Render `text` to `path`. {seconds_audio, seconds_cpu, rtf} or {error}."""
    started = time.perf_counter()
    if voice == "piper":
        try:
            piper.synthesize_blocking(text, Path(path))
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}"}
    else:
        result = host.run(["flite", "-voice", voice, "-t", text, "-o", str(path)], timeout=120)
        if result is None or result.code or not Path(path).exists():
            return {"error": (result.err.strip()[-200:] if result else "flite did not run") or "flite failed"}
    took = time.perf_counter() - started
    try:
        seconds = audio.wav_seconds(path)
    except (OSError, EOFError, ValueError) as error:
        return {"error": f"unreadable output: {error}"}
    return {"seconds_audio": round(seconds, 3), "seconds_cpu": round(took, 3),
            "rtf": round(took / seconds, 3) if seconds else None}


def available_voices(host):
    voices = [(voice, None) for voice in flite_voices(host)]
    engine, why = piper_engine()
    if engine is not None:
        voices.append(("piper", engine))
    return voices, why


def score(results):
    """Mean WER over sentences: {wer, sentences}."""
    values = [item["wer"] for item in results if item.get("wer") is not None]
    return round(sum(values) / len(values), 3) if values else None


def judge_wer(value):
    if value is None:
        return "skip"
    return "pass" if value <= 0.3 else "warn"


def check_tts(host, report, *, sphinx=None, acoustic=False, sentences=TTS_SENTENCES):
    """Every voice: RTF and decoded WER of fixed sentences; acoustic adds speaker -> mic."""
    voices, piper_note = available_voices(host)
    if not voices:
        return [check("tts.voices", "tts", "skip", None, "no speech engine here (no flite)")]
    checks = [check("tts.voices", "tts", "pass", [name for name, _ in voices],
                    "voices: " + ", ".join(name for name, _ in voices) + (f"; {piper_note}" if piper_note else ""))]
    for name, engine in voices:
        rendered, digital = [], []
        for number, sentence in enumerate(sentences, 1):
            path = report.path(f"tts-{name}-{number}.wav")
            result = synthesize(host, name, sentence, path, piper=engine)
            if "error" in result:
                digital.append({"sentence": sentence, "error": result["error"]})
                continue
            report.note_file(path)  # Pip's own voice: always kept
            rendered.append((sentence, path, result))
            entry = {"sentence": sentence, "rtf": result["rtf"]}
            if sphinx is not None:
                heard = sphinx.dictate(load_16k(path))
                entry.update(heard=heard["text"], **audio.wer(sentence, heard["text"]))
            digital.append(entry)
        rtfs = [entry["rtf"] for entry in digital if entry.get("rtf") is not None]
        rtf = round(sorted(rtfs)[len(rtfs) // 2], 3) if rtfs else None
        slow = name == "piper" and rtf is not None and rtf > PIPER_RTF_LIMIT
        checks.append(check(f"tts.rtf.{name}", "tts", "warn" if slow else "pass" if rtf is not None else "fail",
                            rtf, f"{name}: real-time factor {rtf} over {len(rtfs)} sentences",
                            f"Slower than the {PIPER_RTF_LIMIT} limit: Pip would pause before speaking." if slow else
                            ("" if rtf is not None else "This voice could not speak.")))
        if sphinx is not None:
            value = score(digital)
            checks.append(check(f"tts.digital.{name}", "tts", judge_wer(value), digital,
                                f"{name}: decoded word error rate {value} straight from the file",
                                "The words are hard to make out even before the speaker." if judge_wer(value) == "warn"
                                else ""))
        if acoustic and rendered:
            checks.append(acoustic_tts(host, report, name, rendered, sphinx))
    if sphinx is None:
        checks.append(check("tts.digital", "tts", "skip", None, "no PocketSphinx here to score the words"))
    return checks


def acoustic_tts(host, report, name, rendered, sphinx):
    rows = []
    for number, (sentence, path, _) in enumerate(rendered, 1):
        recorded = report.path(f"tts-{name}-{number}-room.wav")
        seconds = audio.wav_seconds(path) + 1.2
        recorder = host.spawn(record_argv(recorded, "plughw:0,0", RATE, 2, seconds))
        host.sleep(0.3)
        played = play(host, path)
        if recorder is not None:
            try:
                recorder.wait(timeout=seconds + 15)
            except Exception:
                recorder.kill()
        try:
            samples = load_16k(recorded)
        except (OSError, EOFError, ValueError):
            rows.append({"sentence": sentence, "error": "no recording"})
            continue
        report.note_file(recorded)  # a known stimulus: kept
        row = {"sentence": sentence, "underruns": played.get("underruns")}
        if sphinx is not None:
            heard = sphinx.dictate(samples)
            row.update(heard=heard["text"], **audio.wer(sentence, heard["text"]))
        rows.append(row)
    value = score(rows)
    status = judge_wer(value) if sphinx is not None else "skip"
    return check(f"tts.acoustic.{name}", "tts", status, rows,
                 f"{name}: word error rate {value} after the speaker and the microphone",
                 "Clear in the file but not in the room: the speaker path or the microphone."
                 if status == "warn" else "")


def check_asr_selftest(host, report, sphinx):
    """Decode known audio: Pip saying the command, and Moonshine's own test file."""
    checks = []
    if sphinx is None or host.which("flite") is None:
        checks.append(check("asr.selftest.sphinx", "asr", "skip", None, "needs PocketSphinx and flite"))
    else:
        path = report.path("asr-selftest-open-terminal.wav")
        result = synthesize(host, "slt", "Hey Pip, open a terminal.", path)
        if "error" in result:
            checks.append(check("asr.selftest.sphinx", "asr", "warn", None, result["error"]))
        else:
            report.note_file(path)
            heard = sphinx.dictate(load_16k(path))["text"]
            route = predict_route(heard)
            ok = route == "workshop.open"
            checks.append(check("asr.selftest.sphinx", "asr", "pass" if ok else "warn",
                                {"heard": heard, "route": route},
                                f"Pip's own 'Hey Pip, open a terminal' decoded as {heard!r} -> {route}",
                                "" if ok else "Clean speech fails here too: the recognizer, not the microphone."))
    ready, why = moonshine_ready()
    if not ready:
        checks.append(check("asr.selftest.moonshine", "asr", "skip", None, why))
        return checks
    from . import asr_sherpa

    wav = asr_sherpa.test_wav()
    if wav is None:
        checks.append(check("asr.selftest.moonshine", "asr", "skip", None, "Moonshine has no test recording"))
        return checks
    texts = moonshine(host, [wav])
    if "error" in texts:
        checks.append(check("asr.selftest.moonshine", "asr", "warn", None, texts["error"],
                            "Do not switch Moonshine on until it runs cleanly here."))
    else:
        text = texts.get(str(wav), "")
        checks.append(check("asr.selftest.moonshine", "asr", "pass" if text.strip() else "warn", text,
                            f"Moonshine's test recording decoded as {text!r}"))
    return checks


# Hearing a file through the running voice -------------------------------------------

class MicServer:
    """The diagnostic microphone: a Unix socket the voice reads S16_LE 16 kHz mono from.

    After `accept()`, a feeder thread writes 30 ms frames in real time, quiet
    noise while nothing is queued, so the voice's clock and levels behave as
    with a real microphone."""

    def __init__(self, path, *, floor_dbfs=-80.0, clock=time.monotonic, sleep=time.sleep):
        self.path = Path(path)
        self.clock, self.sleep = clock, sleep
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen(1)
        self.connection = None
        self.queue = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.disconnected = False
        self.floor = audio.white_dither(1.0, RATE, floor_dbfs).tobytes()
        self.queued_until = 0.0

    def accept(self, timeout):
        self.server.settimeout(timeout)
        try:
            self.connection, _ = self.server.accept()
        except (socket.timeout, OSError):
            return False
        self.thread = threading.Thread(target=self._feed, name="pip-diag-mic", daemon=True)
        self.thread.start()
        return True

    def play(self, samples):
        """Queue samples; returns the monotonic time at which they will have been sent."""
        data = audio.interleave([samples]).tobytes()
        with self.lock:
            backlog = sum(len(chunk) for chunk in self.queue) / 2 / RATE
            self.queue.append(data)
            self.queued_until = self.clock() + backlog + len(samples) / RATE
            return self.queued_until

    def _next(self, position):
        size = FRAME * 2
        with self.lock:
            if self.queue:
                chunk, self.queue[0] = self.queue[0][:size], self.queue[0][size:]
                if not self.queue[0]:
                    self.queue.pop(0)
                if len(chunk) < size:
                    chunk += bytes(size - len(chunk))
                return chunk, position
        start = position % (len(self.floor) - size)
        return self.floor[start:start + size], position + size

    def _feed(self):
        due, position = self.clock(), 0
        while not self.stop.is_set():
            chunk, position = self._next(position)
            try:
                self.connection.sendall(chunk)
            except OSError:
                self.disconnected = True
                return
            due += FRAME / RATE
            delay = due - self.clock()
            if delay > 0:
                self.sleep(delay)
            elif delay < -0.5:
                due = self.clock()  # far behind (a stall): do not burst to catch up

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        for sock in (self.connection, self.server):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        try:
            self.path.unlink()
        except OSError:
            pass


def stimulus_from_file(path):
    return load_16k(path)


def stimulus_from_phrase(host, text, voice="slt", folder=None):
    """Pip renders the stimulus itself: Flite, or the natural voice with voice='piper'.

    Returns (16 kHz samples, the rendered WAV's bytes); nothing is left behind."""
    scratch = Path(folder) if folder else Path(tempfile.mkdtemp(prefix="pip-diag-", dir=_scratch()))
    path = scratch / f"phrase-{voice}.wav"
    try:
        piper = None
        if voice == "piper":
            piper, why = piper_engine()
            if piper is None:
                raise ValueError(why)
        result = synthesize(host, voice, text, path, piper=piper)
        if "error" in result:
            raise ValueError(result["error"])
        return load_16k(path), path.read_bytes()
    finally:
        if folder:
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


def _scratch():
    folder = RUN_DIR / "tmp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)
    except OSError:
        return None


def parse_heard(message):
    """(text, the decoder details) from the voice's `heard: "..." (engine X, ...)` line, or None.

    The text is written as a JSON string, so quotes inside it are escaped."""
    marker = (message or "").find('heard: "')
    if marker < 0:
        return None
    rest = message[marker + len("heard: "):]
    try:
        text, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        match = HEARD.search(message)
        return (match["text"], match["rest"].strip()) if match else None
    if not isinstance(text, str):
        return None
    return text, rest[end:].strip().strip("()")


def parse_route(message):
    match = ROUTE.search(message or "")
    if not match:
        return None
    try:
        data = json.loads(match[1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def run_hear(host, report, stimuli, *, asr=None, ready_timeout=25.0, heard_timeout=15.0, route_timeout=10.0,
             server_factory=MicServer, journal=None, session_factory=AudioSession, name="Pip", patience=1.0):
    """Feed each stimulus to the voice and report what it heard and what the mind did.

    `stimuli`: [(label, samples16k, expected_text or None, expected_route or None)]. `name` is
    the being's name: the expected text is compared without the address in front of it.
    `patience` multiplies every wait (an emulated CPU, as in the QEMU lab, is many times slower)."""
    patience = max(1.0, float(patience or 1.0))
    ready_timeout, heard_timeout, route_timeout = (ready_timeout * patience, heard_timeout * patience,
                                                   route_timeout * patience)
    journal = journal or Journal(host)
    if not journal.available():
        return [check("hear.connect", "hear", "skip", None, "journalctl is not available to read the result")]
    env = {"BEING_DIAG_MIC": str(MIC_SOCKET), "BEING_VOICE_DEBUG": "1"}
    if asr:
        env["BEING_ASR"] = asr
    checks = []
    cursor = journal.cursor()
    server = server_factory(host.path(str(MIC_SOCKET)))
    try:
        with session_factory(host, dropin_env=env) as session:
            session.start_voice()
            if not server.accept(15.0 * patience):
                return [check("hear.connect", "hear", "fail", None, "the voice never connected to the diagnostic mic",
                              "This image's voice may predate BEING_DIAG_MIC; check `journalctl -u being-voice`.")]
            ready, cursor = journal.wait(cursor, ready_after_connect(), timeout=ready_timeout, units=(VOICE,))
            checks.append(check("hear.connect", "hear", "pass" if ready else "warn", bool(ready),
                                "the voice is listening to the diagnostic microphone" if ready else
                                "connected, but the voice did not say it was listening",
                                "" if ready else "The voice may still be loading; results may be late."))
            host.sleep(2.0)
            for label, samples, expected_text, expected_route in stimuli:
                checks.extend(_hear_one(host, journal, server, cursor, label, samples, expected_text,
                                        expected_route, heard_timeout, route_timeout, name, patience))
                cursor = journal.cursor() or cursor
                host.sleep(4.0)  # let the voice settle back to listening for the wake phrase
    finally:
        server.close()
    return checks


def _hear_one(host, journal, server, cursor, label, samples, expected_text, expected_route, heard_timeout,
              route_timeout, name="Pip", patience=1.0):
    already_open = terminal_open(host)[0] if expected_route == "workshop.open" else False
    lead = audio.white_dither(0.6, RATE)
    server.play(lead)
    ends = server.play(samples)
    host.sleep(max(0.0, ends - host.monotonic()))
    found = {"woke": False, "wake": None, "heard": None, "route": None, "no_words": False}

    def voice_line(entry):
        if entry.unit == VOICE:
            wake = WAKE.search(entry.message)
            if wake and not found["woke"]:
                found["woke"], found["wake"] = True, wake_score(wake)
            if NO_WORDS.search(entry.message):
                found["no_words"] = True
                return True
            heard = parse_heard(entry.message)
            if heard is not None:
                found["heard"] = (heard[0], entry.monotonic, heard[1])
                return True
        return False

    _, after = journal.wait(cursor, voice_line, timeout=heard_timeout, units=(VOICE,))
    checks = []
    if found["heard"] is None:
        why = "no words" if found["no_words"] else "nothing"
        checks.append(check(f"hear.heard.{label}", "hear", "fail",
                            {"wake_score": found["wake"]},
                            f"{label}: the voice heard {why}" + ("" if found["woke"] else
                                                               " (not even the wake phrase)"),
                            "No transcript line: is voice debug on, and did the wake phrase fire?"))
        return checks
    text, at, rest = found["heard"]
    latency = round(at - ends, 2)
    # A one-breath wake replays only the words after the name, so "Hey Pip," is not expected.
    match = audio.wer(strip_address(expected_text, name), strip_address(text, name)) if expected_text else None
    good = match is None or match["wer"] <= 0.34
    checks.append(check(f"hear.heard.{label}", "hear", "pass" if good else "warn",
                        {"text": text, "latency_s": latency, "wake_score": found["wake"], "decoder": rest,
                         **({"wer": match["wer"]} if match else {})},
                        f"{label}: heard {text!r} {latency} s after the end of speech; {rest}",
                        "" if good else "The recognizer misheard a clean recording."))
    route = None

    def mind_line(entry):
        nonlocal route
        if entry.unit == MIND:
            route = parse_route(entry.message)
            return route is not None
        return False

    journal.wait(after, mind_line, timeout=route_timeout, units=(MIND,))
    model_used = any("launch_slot_" in entry.message for entry in journal.entries(after, units=(CORTEX,)))
    if route is not None and model_used and expected_route not in (None, "model"):
        checks.append(check(f"hear.model.{label}", "hear", "warn", True,
                            f"{label}: the thinking model started a reply although this is a code command",
                            "A command answered in code should never wait for the model."))
    if route is None:
        checks.append(check(f"hear.route.{label}", "hear", "warn", None, f"{label}: no route line from the mind",
                            "This mind may predate the route log line."))
    else:
        routed = route.get("route")
        ok = expected_route is None or route_matches(routed, expected_route)
        if not ok and already_open and expected_route == "workshop.open" and routed == "workshop.say":
            # With the terminal up, "open a terminal" is rightly answered "it is already open".
            checks.append(check(f"hear.route.{label}", "hear", "warn", route,
                                f"{label}: the mind routed it to {routed} (the terminal was already open)",
                                "The terminal was already open before the phrase, so this proves nothing. "
                                "Close it and run again."))
        else:
            checks.append(check(f"hear.route.{label}", "hear", "pass" if ok else "fail", route,
                                f"{label}: the mind routed it to {routed}" + (f" (expected {expected_route})"
                                                                              if expected_route else ""),
                                "" if ok else "The words were heard but not understood as that command."))
    if expected_route == "workshop.open":
        opened, evidence = False, ""
        deadline = host.monotonic() + 6.0 * patience
        while host.monotonic() < deadline:
            opened, evidence = terminal_open(host)
            if opened:
                break
            host.sleep(0.5)
        if opened and not already_open:
            # A unit seen while it starts is not yet a terminal: hands stops it
            # again when the start fails. It must still be up a moment later.
            host.sleep(3.0 * patience)
            opened, evidence = terminal_open(host)
            failed = [entry.message for entry in journal.entries(after, units=(HANDS,))
                      if TERMINAL_FAILED.search(entry.message)]
            if failed:
                opened, evidence = False, f"{evidence}; hands: {failed[-1]}"
        refused_for_menu = not opened and (
            menu_has_the_screen(host)
            or any(MENU_REFUSAL.search(entry.message) for entry in journal.entries(after, units=(HANDS,))))
        if already_open:
            checks.append(check(f"hear.terminal.{label}", "hear", "warn", opened, f"{label}: {evidence}",
                                "The terminal was already open before the phrase, so this proves nothing. "
                                "Close it and run again."))
        elif refused_for_menu:
            checks.append(check(f"hear.terminal.{label}", "hear", "skip", False,
                                f"{label}: the Try menu has the screen and the face is not running, so the "
                                f"workshop refused to open the terminal (by design); {evidence}",
                                "Leave the Try menu (start Pip) and run `pip-diag hear --phrase` again to test "
                                "opening the terminal."))
        else:
            checks.append(check(f"hear.terminal.{label}", "hear", "pass" if opened else "fail", opened,
                                f"{label}: {evidence}",
                                "" if opened else "The terminal did not open; see `journalctl -u being-hands`."))
    return checks


# The owner-present flow ---------------------------------------------------------------

class Keeper:
    """Where owner speech goes: the report (with consent) or RAM, removed after."""

    def __init__(self, report, consent):
        self.report = report
        self.consent = consent
        self.scratch = None if consent else Path(tempfile.mkdtemp(prefix="pip-diag-owner-", dir=_scratch()))

    def path(self, name):
        return self.report.path(name) if self.consent else self.scratch / name

    def done(self, path):
        if self.consent:
            self.report.note_file(path)
        else:
            try:
                Path(path).unlink()
            except OSError:
                pass

    def close(self):
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)

    def text(self, value):
        """A transcript of owner speech, or None without consent."""
        return value if self.consent else None


def prompt(report, text, reader):
    report.say(text)
    try:
        return reader("")
    except (EOFError, OSError):
        return ""


def record_owner(host, keeper, name, seconds):
    path = keeper.path(name)
    result = record_any(host, path, seconds=seconds, tries=(("plughw:0,0", RATE),))
    if result["code"] != 0:
        return None, None
    try:
        rate, channels = audio.read_wav(path)
    except (OSError, EOFError, ValueError):
        return None, None
    finally:
        keeper.done(path)
    return rate, channels


def mic_speech(host, report, keeper, session, reader, *, combos=None):
    """Silence then the fox sentence at each gain setting: speech level, SNR, peak per channel."""
    boost, capture = current_level(host, "Mic Boost"), current_level(host, "Capture")
    combos = combos or [(boost, capture)] + [(value, capture) for value in (0, 3) if value != boost]
    saved = session.save_mixer()
    rows = []
    try:
        for boost_value, capture_value in combos:
            if saved and boost_value is not None:
                set_control(host, "Mic Boost", boost_value)
                set_control(host, "Capture", capture_value)
            prompt(report, f"[Mic Boost {boost_value}, Capture {capture_value}] Stay quiet, then press Enter.", reader)
            rate, quiet = record_owner(host, keeper, f"owner-quiet-b{boost_value}.wav", 3)
            prompt(report, f'Press Enter, then say at about 1 m: "{SPEECH_PROMPT}"', reader)
            rate2, speech = record_owner(host, keeper, f"owner-speech-b{boost_value}.wav", 5)
            if quiet is None or speech is None:
                rows.append({"boost": boost_value, "capture": capture_value, "error": "recording failed"})
                continue
            channels = []
            for floor, loud in zip(quiet, speech):
                floor_db = audio.dbfs(audio.rms(floor))
                channels.append({"floor_dbfs": floor_db, **audio.speech_level(loud, rate2, floor_db)})
            rows.append({"boost": boost_value, "capture": capture_value, "channels": channels})
    finally:
        if saved:
            host.run(["alsactl", "restore", "-f", str(host.path(str(MIXER_STATE))), "0"], timeout=20)
    usable = [(row, index, channel) for row in rows for index, channel in enumerate(row.get("channels") or [])
              if channel.get("snr_db") is not None]
    if not usable:
        return check("voice.mic_speech", "voice", "fail", rows, "no speech stood above the room floor",
                     "Speak closer to the keyboard; if that fails the microphone path is broken."), None
    row, index, channel = max(usable, key=lambda item: item[2]["snr_db"])
    status = "pass" if channel["snr_db"] >= 15 else "warn"
    return check("voice.mic_speech", "voice", status, rows,
                 f"best speech SNR {channel['snr_db']} dB on the {('left', 'right')[index] if index < 2 else index} "
                 f"channel at Mic Boost {row['boost']}, Capture {row['capture']} (speech {channel['rms_dbfs']}, "
                 f"floor {channel['floor_dbfs']}, peak {channel['peak_dbfs']} dBFS)",
                 "" if status == "pass" else "Speech is barely above the noise: recognition will be poor."), index


def corpus(host, report, keeper, sphinx, reader, channel, phrases, *, use_moonshine=True):
    """The owner says each phrase; each engine decodes it; WER and route accuracy per engine."""
    engines = {"sphinx": [], "grammar": [], "moonshine": []}
    grammar = grammar_phrases()
    moon_files = []
    for number, (text, expected) in enumerate(phrases, 1):
        prompt(report, f'({number}/{len(phrases)}) Press Enter, then say: "{text}"', reader)
        path = keeper.path(f"owner-command-{number}.wav")
        result = record_any(host, path, seconds=4, tries=(("plughw:0,0", RATE),))
        if result["code"] != 0:
            continue
        rate, channels = audio.read_wav(path)
        samples = channels[min(channel or 0, len(channels) - 1)]
        mono_path = path.with_name(path.stem + "-mono.wav")
        audio.write_wav(mono_path, [samples], rate)
        if sphinx is not None:
            heard = sphinx.dictate(samples)["text"]
            engines["sphinx"].append(_corpus_row(text, expected, heard, keeper))
            guessed = sphinx.grammar(samples, grammar)
            if guessed is not None:
                engines["grammar"].append(_corpus_row(text, expected, guessed["text"], keeper))
        moon_files.append((text, expected, mono_path))
        keeper.done(path)
    if use_moonshine and moon_files and moonshine_ready()[0]:
        texts = moonshine(host, [path for _, _, path in moon_files])
        if "error" not in texts:
            for text, expected, path in moon_files:
                engines["moonshine"].append(_corpus_row(text, expected, texts.get(str(path), ""), keeper))
    for _, _, path in moon_files:
        keeper.done(path)
    checks = []
    for engine, rows in engines.items():
        if not rows:
            checks.append(check(f"voice.corpus.{engine}", "voice", "skip", None, f"{engine} did not run"))
            continue
        routed = sum(row["ok"] for row in rows)
        mean = round(sum(row["wer"] for row in rows) / len(rows), 3)
        status = "pass" if routed >= 0.8 * len(rows) else "warn" if routed else "fail"
        checks.append(check(f"voice.corpus.{engine}", "voice", status, rows,
                            f"{engine}: {routed} of {len(rows)} commands routed right, word error rate {mean}",
                            "" if status == "pass" else "Spoken commands are not recognised reliably."))
    return checks


def _corpus_row(text, expected, heard, keeper):
    route = predict_route(heard)
    return {"said": text, "heard": keeper.text(heard), "route": route, "expected": expected,
            "ok": route_matches(route, expected), **audio.wer(text, heard)}


def listening(host, report, reader, voices_files, channel_file):
    """The owner rates each voice 1-5 and says whether the channel test was right."""
    ratings = {}
    for name, path in voices_files:
        prompt(report, f"Press Enter to hear the {name} voice.", reader)
        play(host, path)
        answer = prompt(report, f"How clear was {name}, from 1 (unintelligible) to 5 (clear)?", reader).strip()
        ratings[name] = int(answer) if answer.isdigit() and 1 <= int(answer) <= 5 else None
    both = None
    if channel_file is not None:
        prompt(report, "Press Enter: one beep should come from the left, then two from the right.", reader)
        play(host, channel_file)
        both = prompt(report, "Did you hear one beep left, then two right, with no crackle? [y/n]",
                      reader).strip().lower().startswith("y")
    rated = [value for value in ratings.values() if value is not None]
    poor = [name for name, value in ratings.items() if value is not None and value <= 2]
    status = "warn" if poor or both is False else "pass" if rated else "skip"
    return check("voice.listening", "voice", status, {"ratings": ratings, "channels_right": both},
                 "ratings " + ", ".join(f"{name} {value}" for name, value in ratings.items())
                 + (f"; channel test {'right' if both else 'wrong'}" if both is not None else ""),
                 ("Hard to understand: " + ", ".join(poor) + "." if poor else "")
                 + (" The speakers' left and right are not as expected." if both is False else ""))


def wake_trials(host, report, reader, journal, *, trials=10, distance="1m"):
    scores, heard = [], 0
    cursor = journal.cursor()
    for number in range(1, trials + 1):
        prompt(report, f'[{distance}] Trial {number} of {trials}: press Enter, then say "Hey Pip".', reader)
        found = {}

        def wake(entry):
            match = WAKE.search(entry.message)
            if match:
                found["score"] = wake_score(match)
                return True
            return False

        entry, cursor = journal.wait(cursor, wake, timeout=6.0, units=(VOICE,))
        if entry is not None:
            heard += 1
            if found["score"] is not None:  # a wake with no score still counts as heard
                scores.append(found["score"])
            _, cursor = journal.wait(cursor, lambda line: bool(ENDED.search(line.message)), timeout=10.0,
                                     units=(VOICE,))
    gate = WAKE_GATES.get(distance, 8)
    margins = [value + KWS_OFFSET for value in scores]
    status = "pass" if heard >= gate * trials / 10 else "fail" if heard < trials / 2 else "warn"
    return check(f"voice.wake_{distance}", "voice", status,
                 {"heard": heard, "trials": trials, "scores": scores, "margins": margins},
                 f"wake phrase heard {heard} of {trials} at {distance}; margins {margins} "
                 f"(default threshold {KWS_DEFAULT_THRESHOLD})",
                 "" if status == "pass" else f"Below the {gate}/10 gate: Pip often will not notice you.")


def false_wakes(host, report, reader, journal, seconds=60):
    prompt(report, f"Press Enter, then talk normally for {seconds} s without saying Pip's name.", reader)
    cursor = journal.cursor()
    host.sleep(seconds)
    wakes = sum(1 for entry in journal.entries(cursor, units=(VOICE,)) if WAKE.search(entry.message))
    return check("voice.false_wakes", "voice", "pass" if wakes == 0 else "warn", wakes,
                 f"{wakes} false wakes in {seconds} s of talk without the name",
                 "Pip wakes on ordinary talk." if wakes else "")


def main(argv=None):
    """The Moonshine child: `python3 -m mind.diag_speech moonshine FILE...`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["moonshine"]:
        return _moonshine_child(argv[1:])
    print("usage: python3 -m mind.diag_speech moonshine FILE...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
