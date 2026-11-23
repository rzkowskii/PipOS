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


class Recognizer:
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

    def start_capture(self):
        self.capture_frames = 0
        self.endpoint = self.lib.ps_endpointer_init(0.36, 0.75, 2, RATE, 0.03)
        if not self.endpoint:
            raise RuntimeError("could not initialize speech endpointing")

    def stop_capture(self):
        self.wake_preroll.clear()
        if self.mode is not None:
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

    def spot_keyword(self, samples, in_speech, was_speech):
        self.wake_preroll.append(samples)
        if self.mode is None:
            if not in_speech:
                return []
            self.begin(b"wake")
            # The VAD output can omit the consonants of a short wake phrase.
            # Keep the decoder aligned with the original, buffered samples.
            self.wake_track_from = len(self.wake_preroll)
            self.search_start -= (self.wake_track_from - 1) * FRAME_SAMPLES / RATE

        # The default VAD needs at least eight new frames to close after onset.
        # A six-frame burst closes the backlog before then. Drain on the boundary
        # as well so ending a segment never discards its last audio.
        count = min(len(self.wake_preroll), WAKE_REPLAY_FRAMES) if in_speech else len(self.wake_preroll)
        for _ in range(count):
            self.decode(self.wake_preroll.popleft())
            if self.frames >= self.wake_track_from and (in_speech or was_speech):
                self.track_wake()

        if not in_speech:
            self.lib.ps_end_utt(self.decoder)
            self.mode = None
            if was_speech and self.wake_end is not None:
                tail = self.lib.ps_endpointer_speech_end(self.endpoint) - self.wake_end
                if tail <= WAKE_TAIL_SECONDS:
                    return [("hear.wake", {})]
        return []

    def feed(self, frame, attending):
        self.capture_frames += 1
        samples = (c.c_int16 * FRAME_SAMPLES).from_buffer_copy(frame)
        was_speech = bool(self.lib.ps_endpointer_in_speech(self.endpoint))
        speech = self.lib.ps_endpointer_process(self.endpoint, samples)
        in_speech = bool(self.lib.ps_endpointer_in_speech(self.endpoint))
        if attending:
            self.wake_preroll.clear()
            if self.mode == b"wake":
                self.lib.ps_end_utt(self.decoder)
                self.mode = None
        if not attending and (self.mode is None or self.mode == b"wake"):
            return self.spot_keyword(samples, in_speech, was_speech)
        if not speech:
            return []

        events = []
        if self.mode is None:
            self.begin(self.dictation)
            events.append(("hear.speech_start", {}))

        self.decode(speech)
        if not in_speech or self.frames >= 600:
            self.lib.ps_end_utt(self.decoder)
            self.mode = None
            text = self.hypothesis()
            events.append(("hear.final", {"text": text}) if text else ("hear.cancel", {}))
        elif self.frames % 3 == 0:
            text = self.hypothesis()
            if text and text != self.last_partial:
                self.last_partial = text
                events.append(("hear.partial", {"text": text}))

        return events
