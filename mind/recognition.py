"""PocketSphinx keyword and streaming speech recognition for 16 kHz mono PCM."""

from collections import deque
import ctypes as c
from pathlib import Path

RATE = 16000
FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 2
MODEL = Path("/usr/share/pocketsphinx/model/en-us")
# Allow for the keyword alignment and VAD to disagree about trailing speech.
WAKE_TAIL_SECONDS = 0.50
# Keep 720 ms of original audio around the 360 ms VAD onset window.
WAKE_PREROLL_FRAMES = 24
# Catch up without monopolizing the event loop in the first voiced frame.
WAKE_REPLAY_FRAMES = 6
FRAME_SECONDS = FRAME_SAMPLES / RATE
# Stop decoding a segment for the keyword this long after its onset: a wake
# phrase starts within 0.45 s of onset, so longer room speech is ignored early.
WAKE_DECODE_CAP_SECONDS = 2.0
# Speech still running this long after the wake phrase is one breath. By then
# the segment can no longer end within WAKE_TAIL_SECONDS of the name, so the
# conversational wake rule is unchanged.
ONESHOT_AFTER_SECONDS = 1.0
# Decoded wake-search audio kept for a one-breath replay into dictation.
WAKE_HISTORY_FRAMES = int((WAKE_DECODE_CAP_SECONDS + ONESHOT_AFTER_SECONDS) / FRAME_SECONDS) + WAKE_PREROLL_FRAMES + 1
# Longest utterance, in frames, before dictation closes it.
MAX_UTTERANCE_FRAMES = 600
SKIP = "skip"


class RecognizerEngine:
    """What the voice organ needs from a speech recognizer.

    PocketSphinx (`Recognizer`) is the working default. A sherpa-onnx engine
    (task 5.2) must offer the same calls and events so it can be dropped in:

    - `start_capture()` and `stop_capture()` bracket one microphone stream and
      drop any buffered audio on stop.
    - `feed(frame, attending, oneshot=True)` takes FRAME_BYTES of 16 kHz mono
      S16_LE PCM and returns a list of `(topic, fields)` pairs:
      `hear.sound` on a voice onset while not attending (no transcript),
      `hear.wake` when the wake phrase opens a segment and ends within
      WAKE_TAIL_SECONDS, or `hear.wake {oneshot}` plus dictation events when
      speech runs on for ONESHOT_AFTER_SECONDS after it (only if `oneshot`), and while attending
      `hear.speech_start`, `hear.partial {text}`, `hear.final {text}` and
      `hear.cancel`.
    - `close()` frees the engine.
    """

    def start_capture(self):
        raise NotImplementedError

    def stop_capture(self):
        raise NotImplementedError

    def feed(self, frame, attending, oneshot=True):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


# Files a sherpa-onnx engine needs (task 5.2). None ship yet; see
# `sherpa_onnx_missing` for what is absent on this machine.
SHERPA_ROOT = Path("/models/asr")
SHERPA_LIBRARY = Path("/usr/lib/libsherpa-onnx-c-api.so")
SHERPA_FILES = (
    "silero_vad.onnx",
    "zipformer/encoder.int8.onnx",
    "zipformer/decoder.int8.onnx",
    "zipformer/joiner.int8.onnx",
    "zipformer/tokens.txt",
    "kws/keywords.txt",
)


def sherpa_onnx_missing(root=SHERPA_ROOT, library=SHERPA_LIBRARY):
    """List the sherpa-onnx pieces that are not installed, as paths."""
    missing = [str(library)] if not Path(library).is_file() else []
    missing.extend(str(Path(root) / name) for name in SHERPA_FILES if not (Path(root) / name).is_file())
    return missing


def engine_choice(name, root=SHERPA_ROOT, library=SHERPA_LIBRARY):
    """Return the recognizer engine to use and an honest note when it differs."""
    name = (name or "pocketsphinx").strip().lower()
    if name in ("sherpa", "sherpa-onnx"):
        missing = sherpa_onnx_missing(root, library)
        if missing:
            return "pocketsphinx", f"sherpa-onnx is not installed ({len(missing)} files missing). Using PocketSphinx."
        return "pocketsphinx", "The sherpa-onnx recognizer is not built yet. Using PocketSphinx."
    if name != "pocketsphinx":
        return "pocketsphinx", f"Unknown speech recognizer {name!r}. Using PocketSphinx."
    return "pocketsphinx", None


class Recognizer(RecognizerEngine):
    def __init__(self, wake_word, model=MODEL):
        self.lib = c.CDLL("libpocketsphinx.so.5")
        pointer, string, integer = c.c_void_p, c.c_char_p, c.c_int
        sample_ptr = c.POINTER(c.c_int16)
        signatures = {
            "ps_config_init": (pointer, pointer),
            "ps_config_free": (integer, pointer),
            "ps_config_set_str": (pointer, pointer, string, string),
            "ps_config_set_float": (pointer, pointer, string, c.c_double),
            "ps_init": (pointer, pointer),
            "ps_free": (integer, pointer),
            "ps_current_search": (string, pointer),
            "ps_add_keyphrase": (integer, pointer, string, string),
            "ps_activate_search": (integer, pointer, string),
            "ps_start_utt": (integer, pointer),
            "ps_end_utt": (integer, pointer),
            "ps_process_raw": (integer, pointer, sample_ptr, c.c_size_t, integer, integer),
            "ps_get_hyp": (string, pointer, pointer),
            "ps_seg_iter": (pointer, pointer),
            "ps_seg_frames": (None, pointer, c.POINTER(integer), c.POINTER(integer)),
            "ps_seg_next": (pointer, pointer),
            "ps_endpointer_init": (pointer, c.c_double, c.c_double, integer, integer, c.c_double),
            "ps_endpointer_free": (integer, pointer),
            "ps_endpointer_process": (sample_ptr, pointer, sample_ptr),
            "ps_endpointer_in_speech": (integer, pointer),
            "ps_endpointer_speech_start": (c.c_double, pointer),
            "ps_endpointer_speech_end": (c.c_double, pointer),
        }
        for name, (result, *args) in signatures.items():
            function = getattr(self.lib, name)
            function.restype = result
            function.argtypes = args

        config = self.lib.ps_config_init(None)
        if not config:
            raise RuntimeError("could not initialize speech configuration")
        try:
            for key, filename in (
                (b"hmm", "en-us"),
                (b"dict", "cmudict-en-us.dict"),
                (b"lm", "en-us.lm.bin"),
            ):
                self.lib.ps_config_set_str(config, key, str(model / filename).encode())
            self.lib.ps_config_set_str(config, b"loglevel", b"ERROR")
            self.lib.ps_config_set_float(config, b"kws_threshold", 1e-42)
            self.decoder = self.lib.ps_init(config)
        finally:
            self.lib.ps_config_free(config)
        if not self.decoder:
            raise RuntimeError(f"could not load the speech model from {model}")
        self.dictation = self.lib.ps_current_search(self.decoder)
        if self.lib.ps_add_keyphrase(self.decoder, b"wake", wake_word.lower().encode()) < 0:
            self.lib.ps_free(self.decoder)
            raise ValueError(f"the speech dictionary does not contain {wake_word!r}")
        self.endpoint = None
        self.mode = None
        self.frames = 0
        self.last_partial = ""
        self.wake_end = None
        self.wake_preroll = deque(maxlen=WAKE_PREROLL_FRAMES + 1)
        self.wake_track_from = 0

    # Read-only fallbacks for instances built without __init__ in tests.
    oneshot = False
    oneshot_queue = ()
    wake_history = ()

    def start_capture(self):
        self.capture_frames = 0
        self.endpoint = self.lib.ps_endpointer_init(0.36, 0.75, 2, RATE, 0.03)
        if not self.endpoint:
            raise RuntimeError("could not initialize speech endpointing")

    def stop_capture(self):
        self.wake_preroll.clear()
        self.wake_history = ()
        self.oneshot = False
        self.oneshot_queue = ()
        if self.mode not in (None, SKIP):
            self.lib.ps_end_utt(self.decoder)
        self.mode = None
        if self.endpoint:
            self.lib.ps_endpointer_free(self.endpoint)
            self.endpoint = None

    def close(self):
        self.stop_capture()
        self.lib.ps_free(self.decoder)

    def hypothesis(self):
        return (self.lib.ps_get_hyp(self.decoder, None) or b"").decode()

    def begin(self, mode):
        if self.lib.ps_activate_search(self.decoder, mode) < 0:
            raise RuntimeError("could not switch speech search")
        if self.lib.ps_start_utt(self.decoder) < 0:
            raise RuntimeError("could not start a speech utterance")
        self.mode = mode
        self.search_start = (self.capture_frames - 1) * FRAME_SAMPLES / RATE
        self.frames = 0
        self.last_partial = ""
        self.wake_end = None

    def decode(self, samples):
        if self.lib.ps_process_raw(self.decoder, samples, FRAME_SAMPLES, 0, 0) < 0:
            raise RuntimeError("could not decode speech audio")
        self.frames += 1

    def track_wake(self):
        segment = self.lib.ps_seg_iter(self.decoder)
        if not segment:
            return
        speech_start = self.lib.ps_endpointer_speech_start(self.endpoint)
        start, end = c.c_int(), c.c_int()
        while segment:
            self.lib.ps_seg_frames(segment, c.byref(start), c.byref(end))
            # PocketSphinx reports 100 acoustic frames per second.
            offset = self.search_start + start.value / 100 - speech_start
            if -0.10 <= offset <= 0.45:
                keyword_end = self.search_start + end.value / 100
                if self.wake_end is None or keyword_end > self.wake_end:
                    self.wake_end = keyword_end
            segment = self.lib.ps_seg_next(segment)

    def decoded_until(self):
        """End time of the audio the current search has decoded."""
        return self.search_start + self.frames * FRAME_SECONDS

    def end_search(self, then=None):
        if self.mode not in (None, SKIP):
            self.lib.ps_end_utt(self.decoder)
        self.mode = then

    def spot_keyword(self, samples, in_speech, was_speech, oneshot=False):
        if self.mode == SKIP:
            # The segment outlived the keyword window. Ignore it until it ends.
            if not in_speech:
                self.mode = None
            return []
        self.wake_preroll.append(samples)
        if self.mode is None:
            if not in_speech:
                return []
            self.begin(b"wake")
            self.wake_history = deque(maxlen=WAKE_HISTORY_FRAMES)
            # The VAD output can omit the consonants of a short wake phrase.
            # Keep the decoder aligned with the original, buffered samples.
            self.wake_track_from = len(self.wake_preroll)
            self.search_start -= (self.wake_track_from - 1) * FRAME_SAMPLES / RATE

        # The default VAD needs at least eight new frames to close after onset.
        # A six-frame burst closes the backlog before then. Drain on the boundary
        # as well so ending a segment never discards its last audio.
        count = min(len(self.wake_preroll), WAKE_REPLAY_FRAMES) if in_speech else len(self.wake_preroll)
        for _ in range(count):
            frame = self.wake_preroll.popleft()
            self.decode(frame)
            self.wake_history.append(frame)
            if self.frames >= self.wake_track_from and (in_speech or was_speech):
                self.track_wake()

        if not in_speech:
            self.end_search()
            if was_speech and self.wake_end is not None:
                tail = self.lib.ps_endpointer_speech_end(self.endpoint) - self.wake_end
                if tail <= WAKE_TAIL_SECONDS:
                    return [("hear.wake", {})]
            return []

        decoded = self.decoded_until()
        if self.wake_end is not None and decoded - self.wake_end > ONESHOT_AFTER_SECONDS:
            # Speech runs on after the name: "Hey Pip, what time is it?"
            if oneshot:
                return self.begin_oneshot()
            self.end_search(SKIP)
        elif self.wake_end is None and decoded - self.lib.ps_endpointer_speech_start(self.endpoint) \
                > WAKE_DECODE_CAP_SECONDS:
            self.end_search(SKIP)
        if self.mode == SKIP:
            self.wake_preroll.clear()
            self.wake_history = ()
        return []

    def begin_oneshot(self):
        """Replay the audio after the wake phrase into dictation, then go live."""
        # Start with the frame holding the name's end (the epsilon absorbs float error).
        first = int((self.wake_end - self.search_start) / FRAME_SECONDS + 1e-6)
        # The history is bounded: index it from the oldest frame it still holds.
        oldest = self.frames - len(self.wake_history)
        replay = deque(list(self.wake_history)[max(0, first - oldest):])
        replay.extend(self.wake_preroll)
        self.wake_preroll.clear()
        self.wake_history = ()
        self.end_search()
        self.begin(self.dictation)
        self.oneshot = True
        self.oneshot_queue = replay
        return [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})]

    def continue_oneshot(self, samples, in_speech):
        self.oneshot_queue.append(samples)
        # Catch up a few frames at a time; drain everything when the segment ends.
        count = min(len(self.oneshot_queue), WAKE_REPLAY_FRAMES + 1) if in_speech else len(self.oneshot_queue)
        for _ in range(count):
            self.decode(self.oneshot_queue.popleft())
        if not in_speech or self.frames >= MAX_UTTERANCE_FRAMES:
            self.end_search()
            self.oneshot = False
            self.oneshot_queue = ()
            text = self.hypothesis()
            return [("hear.final", {"text": text, "oneshot": True}) if text else ("hear.cancel", {"oneshot": True})]
        if self.frames % 3 == 0:
            text = self.hypothesis()
            if text and text != self.last_partial:
                self.last_partial = text
                return [("hear.partial", {"text": text, "oneshot": True})]
        return []

    def feed(self, frame, attending, oneshot=True):
        self.capture_frames += 1
        samples = (c.c_int16 * FRAME_SAMPLES).from_buffer_copy(frame)
        was_speech = bool(self.lib.ps_endpointer_in_speech(self.endpoint))
        speech = self.lib.ps_endpointer_process(self.endpoint, samples)
        in_speech = bool(self.lib.ps_endpointer_in_speech(self.endpoint))
        if self.oneshot:
            return self.continue_oneshot(samples, in_speech)
        events = []
        if attending:
            self.wake_preroll.clear()
            if self.mode in (b"wake", SKIP):
                self.end_search()
        elif in_speech and not was_speech:
            # An onset the face can react to. No words are decoded for it.
            events.append(("hear.sound", {}))
        if not attending and self.mode in (None, b"wake", SKIP):
            return events + self.spot_keyword(samples, in_speech, was_speech, oneshot)
        if not speech:
            return events

        if self.mode is None:
            self.begin(self.dictation)
            events.append(("hear.speech_start", {}))

        self.decode(speech)
        if not in_speech or self.frames >= MAX_UTTERANCE_FRAMES:
            self.end_search()
            text = self.hypothesis()
            events.append(("hear.final", {"text": text}) if text else ("hear.cancel", {}))
        elif self.frames % 3 == 0:
            text = self.hypothesis()
            if text and text != self.last_partial:
                self.last_partial = text
                events.append(("hear.partial", {"text": text}))

        return events
