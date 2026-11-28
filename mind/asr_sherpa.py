"""Hearing after the wake: Silero VAD and Moonshine v2 through sherpa-onnx.

`SherpaRecognizer` is a drop-in `RecognizerEngine`. PocketSphinx keeps the
wake phrase ("Hey Pip"), which it already spots on the real laptop. Once the
being is attending, Silero VAD finds the start and end of what is said, in
512-sample windows (threshold 0.5, 0.3 s of silence ends it, 15 s at most),
and Moonshine transcribes the whole segment in one background thread. There
are no partial words: `hear.final` arrives once the decode is done, and an
empty result is an empty final, so the being can say it did not catch that.

`feed()` never waits for a decode. Frames that arrive while one runs are
dropped, buffers are dropped on `stop_capture`, and a stale result (from
before a stop) is never delivered. If Moonshine fails three times running the
engine hands dictation back to PocketSphinx and says so in the log.
"""

import queue
import re
import sys
import threading
from array import array
from collections import deque
from pathlib import Path

from . import sherpa
from .recognition import FRAME_SAMPLES, RATE, RecognizerEngine, strip_name_remnant

ROOT = Path("/models/asr")
VAD_FILE = "silero_vad.onnx"
MOONSHINE_DIRS = {"tiny": "moonshine-tiny", "base": "moonshine-base"}
MOONSHINE_FILES = ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt")
# A short recording the speed check decodes, when the pack ships one.
TEST_WAVS = ("test.wav", "test_wavs/0.wav")

WINDOW = 512
THRESHOLD = 0.5
MIN_SILENCE = 0.3
MIN_SPEECH = 0.25
MAX_SPEECH = 15.0
FRAME_SECONDS = FRAME_SAMPLES / RATE
# Silero reports speech only after MIN_SPEECH of it; keep enough audio from
# before that so the first word is not clipped.
PREROLL_FRAMES = int(0.6 / FRAME_SECONDS) + 1
# While resting, the last few seconds of raw audio are kept in RAM only, so a
# one-breath "Hey Pip, what time is it?" can be transcribed from the end of the
# name (PocketSphinx says where that was; earlier audio is dropped). This
# covers PocketSphinx's 2 s keyword window, its 1 s run-on rule and its 0.72 s
# pre-roll. It is overwritten continuously and never leaves the process.
HISTORY_FRAMES = int(4.0 / FRAME_SECONDS) + 1
# A one-breath command whose rest is shorter than Silero's MIN_SPEECH never
# raises `detected`; end it after this long without speech.
ONESHOT_QUIET_FRAMES = int(0.6 / FRAME_SECONDS) + 1
# "Hey Pip, <pause> <request>": a one-breath window in which Silero hears no
# speech at all, in the replayed audio or the live audio, is not sent to the
# recogniser (it writes "You" or "So" from the noise floor). It stays open for
# the request, up to this long after the wake, and then ends empty. Silero is
# the judge, not a level: the floor rises with the mic gain (-59 dBFS at 2.3x,
# -44 at 6x on the laptop), speech does not stand clear of it at every gain.
ONESHOT_WAIT_FRAMES = int(5.0 / FRAME_SECONDS)
DUMP_KEEP = 20
MAX_FAILURES = 3


def log(message):
    print(message, file=sys.stderr, flush=True)


def moonshine_dir(root=ROOT, size="tiny"):
    return Path(root) / MOONSHINE_DIRS[size]


def missing(root=ROOT, size="tiny", library=None):
    """The Silero and Moonshine pieces that are not installed, as paths."""
    root = Path(root)
    library = Path(library) if library is not None else sherpa.library_path()
    absent = [] if library.is_file() else [str(library)]
    if not (root / VAD_FILE).is_file():
        absent.append(str(root / VAD_FILE))
    folder = moonshine_dir(root, size)
    absent.extend(str(folder / name) for name in MOONSHINE_FILES if not (folder / name).is_file())
    return absent


def test_wav(root=ROOT, size="tiny"):
    folder = moonshine_dir(root, size)
    return next((folder / name for name in TEST_WAVS if (folder / name).is_file()), None)


def verified(root=ROOT, size="tiny"):
    """(ok, reason): Silero (the "vad" pack) and Moonshine (the "asr" pack)
    belong to their signed packs (mind/model_pack.py), when they are on
    /models. Blocking the first time (it may hash them): never on the event
    loop. Moonshine base ships in no pack, so on /models it is refused."""
    try:
        from . import model_pack
    except ImportError:  # an image without pack checks loads what it has
        return True, None
    folder = moonshine_dir(root, size)
    for role, paths in (("vad", [Path(root) / VAD_FILE]), ("asr", [folder / name for name in MOONSHINE_FILES])):
        ok, reason = model_pack.trusted(role, paths)
        if not ok:
            return False, reason
    return True, None


def load_vad(root=ROOT, library=None):
    return sherpa.Vad(Path(root) / VAD_FILE, threshold=THRESHOLD, min_silence=MIN_SILENCE,
                      min_speech=MIN_SPEECH, max_speech=MAX_SPEECH, window=WINDOW, library=library)


def load_moonshine(root=ROOT, size="tiny", library=None, threads=1):
    folder = moonshine_dir(root, size)
    return sherpa.OfflineRecognizer(folder / "encoder_model.ort", folder / "decoder_model_merged.ort",
                                    folder / "tokens.txt", threads=threads, library=library)


def strip_wake(text, wake_word):
    """Drop the wake phrase, and anything heard before it, from a one-breath transcript.

    The name itself near the start goes with whatever was heard before it.
    Otherwise a misheard name goes (recognition.strip_name_remnant, the one
    rule set for both recognizers): Moonshine writes it as a short clause of
    its own ("Yep, close the terminal.", "Lay pit, what time is it?") or as a
    name-like word ("Pep open a terminal."). Re-scored on the transcripts
    measured on the laptop (cable run, 25 Nov), that took one-breath requests
    understood from 17 to 31 of 42, and to 35 with the voice's own check of a
    leftover before a safe command (voice.CommandGrammar.without_remnant),
    with none lost."""
    name = (wake_word or "").split()[-1:] or ["pip"]
    match = re.search(rf"\b{re.escape(name[0])}\b[\s,.!:;-]*", text, re.I)
    # Only near the start: "Hey Pip, ..." and not a later "pip" in the request.
    if match and len(text[:match.start()].split()) <= 3:
        return text[match.end():].strip()
    return strip_name_remnant(text, name[0]).strip()


class SherpaRecognizer(RecognizerEngine):
    """PocketSphinx for the wake, Silero VAD and Moonshine for the words."""

    name = "moonshine"

    def __init__(self, wake, vad, recognizer, *, wake_word="Hey Pip"):
        self.wake = wake
        self.vad = vad
        self.recognizer = recognizer
        self.wake_word = wake_word
        self.jobs = queue.SimpleQueue()
        self.results = queue.SimpleQueue()
        self.worker = None
        self.generation = 0
        self.decoding = None          # (generation, oneshot) while a decode runs
        self.failures = 0
        self.broken = False
        # Voice debugging only: the voice turns this on while transcripts may be logged.
        self.window_dump_enabled = False
        self.window_dump_dir = None
        self._reset_listening()
        self.history = deque(maxlen=HISTORY_FRAMES)
        self.was_attending = False

    @property
    def wake_score(self):
        """The wake spotter's score for the last wake, so the voice's wake line keeps its number."""
        return getattr(self.wake, "wake_score", None)

    @classmethod
    def load(cls, wake, *, root=ROOT, size="tiny", library=None, threads=1, wake_word="Hey Pip"):
        """Build the engine from installed files. Raises sherpa.Unavailable."""
        absent = missing(root, size, library)
        if absent:
            raise sherpa.Unavailable(f"{len(absent)} speech files are missing ({', '.join(absent)})")
        ok, reason = verified(root, size)
        if not ok:
            # Nothing unverified is ever loaded: PocketSphinx keeps the words.
            log(f"asr: the Moonshine model packs did not verify ({reason}); keeping PocketSphinx")
            raise sherpa.Unavailable(f"its model pack did not verify ({reason})")
        vad = load_vad(root, library)
        try:
            recognizer = load_moonshine(root, size, library, threads)
        except Exception:
            vad.close()
            raise
        return cls(wake, vad, recognizer, wake_word=wake_word)

    # RecognizerEngine ------------------------------------------------------------

    def start_capture(self):
        self.wake.start_capture()

    def stop_capture(self):
        self.generation += 1        # a decode still running is now stale
        self.decoding = None
        self._reset_listening()
        self.history.clear()
        self.was_attending = False
        self.vad.reset()
        self.wake.stop_capture()

    def close(self):
        try:
            self.stop_capture()
        finally:
            worker = self.worker
            if worker is not None:
                self.jobs.put(None)
                worker.join(timeout=5)
            if worker is None or not worker.is_alive():
                self.recognizer.close()
            self.vad.close()
            self.wake.close()

    def feed(self, frame, attending, oneshot=True):
        if self.broken:
            return self.wake.feed(frame, attending, oneshot)
        events = self._collect()
        if self.decoding is not None:
            return events       # one utterance at a time; frames during a decode are dropped
        # An utterance already announced with hear.speech_start is finished
        # even when attending drops under it, as PocketSphinx finishes its own
        # dictation: it ends in a final, never silently.
        finishing = self.was_attending and self._vad_speech
        if not attending and not self.oneshot and not finishing:
            if self.was_attending:
                self._back_to_rest()
            return events + self._rest(frame, oneshot)
        if not self.was_attending and not self.oneshot:
            self._begin_attending()
        return events + self._listen(sherpa.pcm_to_float(frame))

    @property
    def in_speech(self):
        """Whether someone is talking now, as the voice's levels need it.

        While resting PocketSphinx hears every frame, so its endpointer is the
        answer; while attending (or in a one-breath command) it is Silero's."""
        if self.broken or not (self.was_attending or self.oneshot):
            if self.decoding is not None:
                return False    # the wake engine is not fed while a decode runs
            return getattr(self.wake, "in_speech", False) is True
        return self._vad_speech

    @in_speech.setter
    def in_speech(self, value):
        self._vad_speech = bool(value)

    @property
    def run_on_wake(self):
        """Whether a wake phrase that runs on wakes plainly (asleep); PocketSphinx spots it."""
        return getattr(self.wake, "run_on_wake", False) is True

    @run_on_wake.setter
    def run_on_wake(self, value):
        self.wake.run_on_wake = bool(value)

    # Resting: PocketSphinx listens for the name -------------------------------------

    def _rest(self, frame, oneshot):
        self.history.append(bytes(frame))
        events = []
        for topic, fields in self.wake.feed(frame, False, oneshot):
            if topic == "hear.wake" and fields.get("oneshot"):
                return events + self._take_oneshot()
            if topic in ("hear.partial", "hear.final", "hear.cancel", "hear.speech_start"):
                continue    # dictation is ours; the wake engine only wakes
            events.append((topic, fields))
        return events

    def _take_oneshot(self):
        """"Hey Pip, what time is it?": transcribe the breath from the name on, with Moonshine."""
        history = list(self.history)
        self.history.clear()
        # PocketSphinx knows where the name ended: its replay queue holds the
        # frames from there to now. Only those are addressed speech; whatever
        # was said before the name is dropped here, not passed to Moonshine.
        after_name = len(getattr(self.wake, "oneshot_queue", None) or ())
        if 0 < after_name <= len(history):
            history = history[-after_name:]
        # End PocketSphinx's own dictation of the breath; it only spots the name.
        self.wake.stop_capture()
        self.wake.start_capture()
        self.vad.reset()
        self._reset_listening()
        self.oneshot = True
        self.in_speech = True
        self.segment = [sherpa.pcm_to_float(frame) for frame in history]
        self.segment_samples = sum(len(part) for part in self.segment)
        self.replay_frames = len(self.segment)
        # Silero hears the replayed audio too, so the window knows whether it
        # holds speech and how long its own tail has been quiet.
        for samples in self.segment:
            self._watch(samples)
        return [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})]

    def _back_to_rest(self):
        self.was_attending = False
        self._reset_listening()
        self.vad.reset()
        # PocketSphinx saw none of the attending audio: start its endpointer afresh.
        self.wake.stop_capture()
        self.wake.start_capture()

    # Attending: Silero finds the utterance ----------------------------------------

    def _begin_attending(self):
        self.was_attending = True
        self.history.clear()
        self._reset_listening()
        self.vad.reset()

    def _reset_listening(self):
        self.in_speech = False
        self.oneshot = False
        self.vad_seen = False
        self.quiet_frames = 0
        self.segment = []
        self.segment_samples = 0
        self.preroll = deque(maxlen=PREROLL_FRAMES)
        self.window_rest = array("f")
        self.vad_finished = False       # Silero ended a segment inside the replay
        self.peak = 0.0                 # loudest sample of a one-breath window
        self.replay_frames = 0
        self.live_frames = 0
        self.waiting = False

    def _flags(self):
        return {"oneshot": True} if self.oneshot else {}

    def _watch(self, samples):
        """Feed Silero one frame; return (detected, finished) and keep the window's bookkeeping."""
        self.window_rest.extend(samples)
        whole = len(self.window_rest) // WINDOW * WINDOW
        if whole:
            self.vad.accept(self.window_rest[:whole])
            del self.window_rest[:whole]
        detected = self.vad.detected()
        finished = self.vad.pop_segments()
        if self.oneshot:
            self.peak = max(self.peak, max(map(abs, samples), default=0.0))
        if detected:
            self.vad_seen = True
            self.quiet_frames = 0
        else:
            self.quiet_frames += 1
        if finished:
            self.vad_finished = True
        return detected, finished

    def _speechless(self):
        return not self.vad_seen and not self.vad_finished

    def _listen(self, samples):
        if self.in_speech:
            self.segment.append(samples)
            self.segment_samples += len(samples)
        else:
            self.preroll.append(samples)
        if self.oneshot:
            self.live_frames += 1
        detected, finished = self._watch(samples)
        finished = finished or (self.oneshot and self.vad_finished)
        events = []
        if not self.in_speech and (detected or finished):
            self.in_speech = True
            self.segment = list(self.preroll)
            self.segment_samples = sum(len(part) for part in self.segment)
            self.preroll.clear()
            self.waiting = False
            events.append(("hear.speech_start", self._flags()))
        ended = bool(finished) or self.segment_samples >= MAX_SPEECH * RATE
        if self.oneshot and not self.vad_seen and self.quiet_frames >= ONESHOT_QUIET_FRAMES:
            # A command too short for Silero's MIN_SPEECH ends here. A window
            # with no speech in it does not: it waits for the request.
            if self._speechless():
                if not self.waiting:
                    self.waiting = True
                    self._dump("waiting", self.segment)
                    self.in_speech = False
                    self.segment = []
                    self.segment_samples = 0
                    self.preroll.clear()
                if self.live_frames >= ONESHOT_WAIT_FRAMES:
                    return events + self._give_up()
                return events
            ended = True
        if self.in_speech and ended:
            self._submit()
        return events

    def _give_up(self):
        """Nothing was said after the name: an empty one-breath final, as for any empty result."""
        self._dump("gave up", [])
        self._reset_listening()
        self.vad.reset()
        return [("hear.final", {"text": "", "oneshot": True})]

    def _dump(self, decision, parts):
        """Voice debugging only: keep the one-breath window and how it was decided.

        Off unless the voice enables it (transcripts may be logged and no guest
        is here); it never runs by default, and the newest DUMP_KEEP are kept.
        """
        if not self.window_dump_enabled or not self.oneshot:
            return
        try:
            import json
            import math
            import tempfile
            import time
            import wave
            folder = Path(self.window_dump_dir or Path(tempfile.gettempdir()) / "being-oneshot-windows")
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            stem = f"window-{time.strftime('%H%M%S')}-{self.generation:04d}-{decision.replace(' ', '-')}"
            samples = array("f")
            for part in parts:
                samples.extend(part)
            pcm = array("h", (max(-32768, min(32767, int(x * 32768))) for x in samples))
            with wave.open(str(folder / f"{stem}.wav"), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(RATE)
                out.writeframes(pcm.tobytes())
            peak = max(map(abs, samples), default=0.0)
            info = {"decision": decision, "seconds": round(len(samples) / RATE, 3),
                    "replay_frames": self.replay_frames, "live_frames": self.live_frames,
                    "quiet_frames": self.quiet_frames, "vad_seen": self.vad_seen,
                    "vad_finished": self.vad_finished, "peak_dbfs": round(20 * math.log10(peak), 1) if peak else None,
                    "window_peak_dbfs": round(20 * math.log10(self.peak), 1) if self.peak else None}
            (folder / f"{stem}.json").write_text(json.dumps(info) + "\n")
            old = sorted(folder.glob("window-*.wav"))[:-DUMP_KEEP]
            for path in old:
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
        except Exception as error:
            log(f"one-breath window dump failed ({error!r})")

    # The decode thread --------------------------------------------------------------

    def _submit(self):
        samples = array("f")
        for part in self.segment:
            samples.extend(part)
        oneshot = self.oneshot
        if oneshot:
            self._dump("sent", self.segment)
        self._reset_listening()
        self.vad.reset()
        self.generation += 1
        self.decoding = (self.generation, oneshot)
        if self.worker is None:
            self.worker = threading.Thread(target=self._work, name="being-asr-decode", daemon=True)
            self.worker.start()
        self.jobs.put((self.generation, samples))

    def _work(self):
        while (job := self.jobs.get()) is not None:
            generation, samples = job
            try:
                self.results.put((generation, self.recognizer.transcribe(samples, RATE), None))
            except Exception as error:  # reported on the event loop side
                self.results.put((generation, "", error))

    def _collect(self):
        events = []
        while True:
            try:
                generation, text, error = self.results.get_nowait()
            except queue.Empty:
                return events
            if self.decoding is None or generation != self.decoding[0]:
                continue        # stale: capture stopped while it decoded
            oneshot = self.decoding[1]
            self.decoding = None
            if error is not None:
                self.failures += 1
                log(f"Moonshine could not transcribe ({error!r}).")
                if self.failures >= MAX_FAILURES:
                    self.broken = True
                    log("Moonshine failed three times. PocketSphinx takes over dictation.")
                text = ""
            else:
                self.failures = 0
            if oneshot:
                text = strip_wake(text, self.wake_word)
            events.append(("hear.final", {"text": text, **({"oneshot": True} if oneshot else {})}))
