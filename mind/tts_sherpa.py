"""The natural voice: a resident Piper (VITS) voice through sherpa-onnx.

The voice is loaded once, on its own single worker thread, and stays loaded:
reloading a 64 MB voice for every clause would cost an estimated 0.3-1 s each
time. Synthesis runs on one ONNX Runtime thread on that worker, so the event
loop, the face and the language model keep their cores.

`synthesize(text, wav, rate)` writes a 16-bit mono WAV with
`length_scale = 1 / rate`. Every failure raises, so the voice organ can say
that clause with Flite instead.

A voice that once fails a clause, or makes no sound, is silenced for the rest
of the process: it says so in one log line, reports its name as "flite", and
hands the voice organ Flite's command for every later clause, so Pip never goes
quiet and never claims the natural voice while speaking with the original one.
"""

import asyncio
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import sherpa

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


def spoken(text):
    """Tidy a clause for the voice: one line, no leading dash, bounded length."""
    text = " ".join(str(text).split()).lstrip("-").strip()
    if not text:
        raise ValueError("there is nothing to say")
    return text[:MAX_TEXT]


class PiperEngine:
    supports_segment_times = False

    def __init__(self, voice_dir=DEFAULT_VOICE, *, library=None, threads=1, loader=None):
        self.voice_dir = Path(voice_dir)
        self.library = library
        self.threads = threads
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
        # False for a measuring tool (pip-diag), which tries every sentence and reports each failure.
        self.sticky = True

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
        if not files["tokens"].is_file():
            absent.append(str(files["tokens"]))
        if not files["data"].is_dir():
            absent.append(str(files["data"]))
        library = (Path(self.library) if isinstance(self.library, (str, Path))
                   else sherpa.library_path() if self.library is None else None)
        if library is not None and not library.is_file():
            absent.append(str(library))
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
        return sherpa.OfflineTts(files["model"], files["tokens"], files["data"], threads=self.threads,
                                 library=self.library)

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
        mistake and raises ValueError without silencing the voice. Any other
        failure silences it for the rest of the process."""
        said, scale = spoken(text), length_scale(rate)
        if self._silent_error is not None:
            raise self._silent_error
        try:
            tts = self.resident()
            seconds = tts.write(said, Path(wav), length_scale=scale)
            if not seconds or seconds <= 0:
                raise RuntimeError("the natural voice produced no audio")
        except Exception as error:
            self.silence(error)
            raise
        return seconds

    async def _synthesize(self, text, wav, rate=1.0):
        loop = asyncio.get_running_loop()
        job = loop.run_in_executor(self.executor(), self.synthesize_blocking, text, wav, rate)
        try:
            return await asyncio.wait_for(job, clause_timeout(text))
        except TimeoutError as error:
            # The worker may still finish this clause; nothing waits for it.
            self.silence(TimeoutError(f"a clause took more than {clause_timeout(text):.0f} s"))
            raise self._silent_error from error

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
