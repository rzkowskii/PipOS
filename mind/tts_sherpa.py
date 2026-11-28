"""The natural voice: a resident Piper (VITS) voice through sherpa-onnx.

The voice is loaded once, on its own single worker thread, and stays loaded:
reloading a 64 MB voice for every clause would cost an estimated 0.3-1 s each
time. Clauses are synthesized one at a time on that worker, with
`threads_setting()` ONNX Runtime threads (two by default): on the laptop's
two-core Ivy Bridge two threads measured a real-time factor of 0.142 against
0.23 with one (0.164 with four, which share cores through hyper-threading),
and slowed the language model's decoding no more than one thread did, while
keeping the voice busy for about half as long.
BEING_PIPER_THREADS (1 to 8) overrides the default.

`synthesize(text, wav, rate)` writes a 16-bit mono WAV with
`length_scale = 1 / rate`. Every failure raises, so the voice organ can say
that clause with Flite instead.

The voice organ's engine is "sticky" (mind/voice_engines.py sets it): a
clause it cannot say is said by Flite, and the voice is then asked to say one
short word (REPROBE_TEXT). Only when that fails too, or the worker is still
stuck on a clause that ran out of time, or STUCK_LIMIT clauses in a row ran out
of time, is the voice silenced for the rest of the process: it says so in one
log line, reports its name as "flite", and hands the voice organ Flite's
command for every later clause, so Pip never goes quiet and never claims the
natural voice while speaking with the original one. One odd clause (only
punctuation, say) or one slow clause while the laptop is busy never silences
it. Any other user (Setup's preview, pip-diag, the bench) is not sticky: a
failure raises and nothing is printed or kept.
"""

import asyncio
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import phonemize, sherpa

VOICES = Path("/models/voice/piper")
DEFAULT_VOICE = VOICES / "default"
# The owner-facing name of the bundled voice, used when its card cannot be read.
DEFAULT_LABEL = "Alba"
MAX_TEXT = 600
# A clause the natural voice takes longer than this to say is said by Flite
# instead (the voice organ's own limit for a clause is 20 s plus 1 s per 25
# characters, so this always leaves Flite time to say it).
CLAUSE_BASE_S = 8.0
CLAUSE_CHARS_PER_S = 20
# After a clause fails, the voice must still say this to stay in use.
REPROBE_TEXT = "Hello."
# Clauses in a row that may run out of time before the voice is silenced.
STUCK_LIMIT = 3
# ONNX Runtime threads for synthesis (see the module notes), never more than
# the CPUs this process may run on.
DEFAULT_THREADS = 2
MAX_THREADS = 8
THREADS_ENV = "BEING_PIPER_THREADS"


def available_cpus():
    """The CPUs this process may run on."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def threads_setting(environ=None, cpus=None):
    """Synthesis threads: BEING_PIPER_THREADS when it is a whole number from 1 to
    MAX_THREADS, else DEFAULT_THREADS, at most the CPUs available."""
    environ = os.environ if environ is None else environ
    cpus = available_cpus() if cpus is None else max(1, int(cpus))
    default = min(DEFAULT_THREADS, cpus)
    raw = (environ.get(THREADS_ENV) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_THREADS:
        print(f"voice: {THREADS_ENV}={raw!r} is not a whole number from 1 to {MAX_THREADS}; "
              f"using {default}", file=sys.stderr, flush=True)
        return default
    return value


def length_scale(rate):
    """Piper speaks `rate` times faster with a length scale of 1 / rate."""
    rate = float(rate)
    if not 0.25 <= rate <= 4:
        raise ValueError(f"speaking rate {rate!r} is out of range")
    return 1 / rate


def clause_timeout(text):
    """Seconds the natural voice may take over one clause."""
    return CLAUSE_BASE_S + len(str(text)) / CLAUSE_CHARS_PER_S


def reason(error):
    """A short plain reason for a failure, for a log line."""
    text = " ".join(str(error).split())
    return (text or type(error).__name__)[:200]


def sayable(text):
    """True when a clause has a letter or digit in it: "..." or "?!" has nothing
    a voice can say, so a voice that gives no audio for it is not broken."""
    return any(ch.isalnum() for ch in str(text))


def spoken(text):
    """Tidy a clause for the voice: one line, no leading dash, bounded length."""
    text = " ".join(str(text).split()).lstrip("-").strip()
    if not text:
        raise ValueError("there is nothing to say")
    return text[:MAX_TEXT]


class PiperEngine:
    supports_segment_times = False

    def __init__(self, voice_dir=DEFAULT_VOICE, *, library=None, threads=None, loader=None,
                 phonemizer_library=None):
        self.voice_dir = Path(voice_dir)
        self.library = library
        # sherpa-onnx 2.x needs Piper's phonemizer (mind/phonemize.py) beside it.
        self.phonemizer_library = phonemizer_library
        # ONNX Runtime threads; None means the setting (two by default).
        self.threads = threads_setting() if threads is None else max(1, int(threads))
        self._loader = loader or self._load
        self._tts = None
        self._lock = threading.Lock()
        self._executor = None
        self._error = None
        # Why the voice was silenced for this process (a plain reason), or None.
        self.silenced = None
        self._silent_error = None
        # Told the reason once, when the voice is silenced (voice_engines keeps it for this build).
        self.on_silent = None
        # Set while voice_engines probes the voice: its own note says what happened.
        self.quiet = False
        # True only for the voice organ's engine (voice_engines sets it): a voice
        # that is really broken is then silenced for the process, with one log
        # line. Any other user sees each failure raised and nothing printed.
        self.sticky = False
        # The clause that last ran out of time, while the worker may still be on it.
        self._late = None
        self._timeouts = 0
        # WAV paths of clauses that ran out of time: Flite said them and the
        # voice organ may already have removed their folder, so the worker's
        # late failure on one says nothing about the voice.
        self._abandoned = set()

    @property
    def name(self):
        """"piper", or "flite" once silenced: the engine that really speaks."""
        return "flite" if self.silenced else "piper"

    # Files --------------------------------------------------------------------------

    def files(self):
        models = sorted(self.voice_dir.glob("*.onnx"))
        model = models[0] if len(models) == 1 else None
        return {
            "model": model,
            "card": model.with_name(model.name + ".json") if model else None,
            "tokens": self.voice_dir / "tokens.txt",
            "data": self.voice_dir / "espeak-ng-data",
        }

    def missing(self):
        files = self.files()
        absent = []
        if files["model"] is None:
            absent.append(str(self.voice_dir / "*.onnx"))
        elif not files["card"].is_file():
            absent.append(str(files["card"]))
        if not files["tokens"].is_file():
            absent.append(str(files["tokens"]))
        if not files["data"].is_dir():
            absent.append(str(files["data"]))
        library = (Path(self.library) if isinstance(self.library, (str, Path))
                   else sherpa.library_path() if self.library is None else None)
        if library is not None and not library.is_file():
            absent.append(str(library))
        phonemizer = self.phonemizer_library
        phonemizer = (Path(phonemizer) if isinstance(phonemizer, (str, Path))
                      else phonemize.library_path() if phonemizer is None else None)
        if phonemizer is not None and not phonemizer.is_file():
            absent.append(str(phonemizer))
        return absent

    def available(self):
        return not self.missing()

    def verified(self):
        """(ok, reason): the voice's files belong to its signed pack
        (mind/model_pack.py), when they are on /models. Blocking the first
        time (it may hash the pack), so it runs on a worker thread."""
        try:
            from . import model_pack
        except ImportError:  # an image without pack checks loads what it has
            return True, None
        files = self.files()
        paths = [files[name] for name in ("model", "card", "tokens", "data") if files[name] is not None]
        return model_pack.trusted("tts", paths or [self.voice_dir])

    def label(self):
        """'Alba' for the bundled voice, from its model card when it can be read."""
        card = self.files()["card"]
        try:
            data = json.loads(card.read_text()) if card else {}
            dataset = data.get("dataset") if isinstance(data, dict) else None
        except (OSError, ValueError):
            dataset = None
        if isinstance(dataset, str) and re.fullmatch(r"[A-Za-z][A-Za-z _-]{0,30}", dataset):
            return dataset.replace("_", " ").title()
        return DEFAULT_LABEL

    # The resident voice ---------------------------------------------------------------

    def _load(self):
        files = self.files()
        if files["model"] is None:
            raise sherpa.Unavailable(f"no single Piper voice in {self.voice_dir}")
        ok, reason = self.verified()
        if not ok:
            # Nothing unverified is ever loaded: the voice organ speaks with Flite.
            print(f"voice: the natural voice's model pack did not verify ({reason}); using Flite",
                  file=sys.stderr, flush=True)
            raise sherpa.Unavailable(f"its model pack did not verify ({reason})")
        phonemizer = phonemize.for_voice(files["data"], files["card"], library=self.phonemizer_library)
        return sherpa.OfflineTts(files["model"], files["tokens"], files["data"], threads=self.threads,
                                 library=self.library, phonemizer=phonemizer)

    def resident(self):
        """The loaded voice, loading it on first use. Blocking."""
        with self._lock:
            if self._tts is None:
                if self._error is not None:
                    raise self._error
                try:
                    self._tts = self._loader()
                except sherpa.Unavailable as error:
                    self._error = error     # a missing voice stays missing; do not retry every clause
                    raise
            return self._tts

    @property
    def loaded(self):
        return self._tts is not None

    @property
    def sample_rate(self):
        return int(getattr(self.resident(), "sample_rate", 22050))

    def executor(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="being-piper")
        return self._executor

    def synthesize_blocking(self, text, wav, rate=1.0):
        """Write `wav` and return its length in seconds. Raises on any failure.

        Text with nothing to say, or a rate out of range, is the caller's
        mistake and raises ValueError without silencing the voice. For a
        sticky engine, any other failure of a clause with words in it asks the
        voice to say REPROBE_TEXT, and silences it for the rest of the process
        only if that fails too."""
        said, scale = spoken(text), length_scale(rate)
        if self._silent_error is not None:
            raise self._silent_error
        try:
            return self._write(said, wav, scale)
        except ValueError:
            # A clause the phonemizer finds nothing to say in ("...", "?!", the
            # "." left after an emoji): skip it, and keep the natural voice.
            raise
        except Exception as error:
            if str(wav) in self._abandoned:
                # Nobody waits for this clause any more (Flite said it), and its
                # folder may be gone: a write that fails now is not a broken voice.
                raise
            if self.sticky and not self.quiet and sayable(said) and not self._still_speaks(wav):
                self.silence(error)
            raise
        finally:
            self._abandoned.discard(str(wav))

    def _write(self, said, wav, scale):
        seconds = self.resident().write(said, Path(wav), length_scale=scale)
        if type(seconds) not in (int, float) or seconds <= 0:
            raise RuntimeError("the natural voice produced no audio")
        return seconds

    def _still_speaks(self, wav):
        """Does the voice still say REPROBE_TEXT? Blocking, on the caller's
        (the worker's) thread, next to the clause's own file."""
        probe = Path(wav).with_name(Path(wav).name + ".reprobe.wav")
        try:
            self._write(REPROBE_TEXT, probe, 1.0)
            return True
        except Exception:
            return False
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    async def _synthesize(self, text, wav, rate=1.0):
        late = self._late
        if late is not None:
            if not late.done():
                # The worker is still on a clause that ran out of time: it is stuck.
                stuck = TimeoutError("it is still on a clause that ran out of time")
                if self.sticky:
                    self.silence(stuck)
                raise stuck
            self._late = None
        loop = asyncio.get_running_loop()
        limit = clause_timeout(text)
        job = asyncio.ensure_future(loop.run_in_executor(self.executor(), self.synthesize_blocking, text, wav, rate))
        try:
            seconds = await asyncio.wait_for(asyncio.shield(job), limit)
        except asyncio.CancelledError:
            job.cancel()        # a clause still queued is dropped; one running ends on its own
            raise
        except TimeoutError as error:
            # The worker may still finish this clause; nothing waits for it.
            # One slow clause on a busy laptop is said by Flite and no more.
            if not job.done():
                self._abandoned.add(str(wav))
            self._late = job
            job.add_done_callback(_ignore_result)
            self._timeouts += 1
            late = TimeoutError(f"a clause took more than {limit:.0f} s")
            if self.sticky and self._timeouts >= STUCK_LIMIT:
                self.silence(TimeoutError(f"{self._timeouts} clauses in a row took too long; the last, "
                                          f"more than {limit:.0f} s"))
            raise late from error
        self._timeouts = 0
        return seconds

    @property
    def synthesize(self):
        """The resident voice's async synthesis, or None once silenced.

        The voice organ speaks through `synthesize` when an engine has one,
        and otherwise runs `command`, which then is Flite's."""
        return None if self.silenced else self._synthesize

    def command(self, voice, rate, text, output, *, segment_times=False):
        """Flite's command line, for the clauses after the voice was silenced."""
        from .tts import synthesis_command

        return synthesis_command(voice, rate, text, output, segment_times=segment_times)

    def silence(self, error):
        """Stop using the natural voice for this process, with one log line."""
        if self.silenced or not self.sticky:
            return
        why = reason(error)
        self._silent_error = error if isinstance(error, Exception) else RuntimeError(why)
        self.silenced = why
        if not self.quiet:
            print(f"voice: The natural voice made no sound for a clause ({why}). "
                  "Using the original voice for the rest of this session.", file=sys.stderr, flush=True)
        if callable(self.on_silent):
            try:
                self.on_silent(why)
            except Exception as hook_error:  # the switch to Flite stands either way
                print(f"voice: could not keep the natural voice's failure ({hook_error!r})",
                      file=sys.stderr, flush=True)

    async def warm(self):
        """Load the voice on its worker thread, off the event loop."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor(), self.resident)

    def close(self):
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        with self._lock:
            if self._tts is not None:
                self._tts.close()
                self._tts = None


def _ignore_result(job):
    """A clause nobody waits for any more: read its outcome so asyncio does not
    report it as never retrieved."""
    if not job.cancelled():
        job.exception()
