"""PocketSphinx keyword and streaming speech recognition for 16 kHz mono PCM."""

from array import array
from collections import deque
import ctypes as c
import math
import operator
import re
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
# The second search, over a grammar of the commands, that mind/voice.py may
# run on the audio of a dictation the open-vocabulary search did not hear as
# one. A grammar always finds its closest command, so a free phone loop
# decodes the same audio as the garbage model: only a command path that
# scores well above it (per 10 ms frame, in the library's log units) counts.
GRAMMAR_SEARCH = b"commands"
PHONE_SEARCH = b"phones"
PHONE_LM = "en-us-phone.lm.bin"
# Words around a command that change nothing about it.
COURTESY_WORDS = frozenset(("hey", "hi", "ok", "okay", "pip", "please", "now", "thanks", "thank", "you"))

FULL_SCALE = 32768
SILENT_DBFS = -120.0
# Software microphone gain. The environment value is clamped to this range.
MIN_MIC_GAIN = 0.1
MAX_MIC_GAIN = 16.0
# A floor at or below this is digital silence: a muted mixer or a digital source.
# No analog microphone is that quiet, so it is never lifted.
DIGITAL_SILENCE_DBFS = -90.0
# Lift a quiet analog floor towards this level so the endpointer can hear speech.
TARGET_FLOOR_DBFS = -60.0
MAX_AUTO_GAIN = 8.0
# Keep recent peaks below this level after gain.
HEADROOM_DBFS = -3.0
# Per-frame smoothing of the floor estimate: fall quickly, rise slowly.
FLOOR_FALL = 0.1
FLOOR_RISE = 0.005
PEAK_DECAY_DB = 0.02
AUTO_GAIN_RAISE = 1.01


_sumprod = getattr(math, "sumprod", None) or (lambda a, b: sum(map(operator.mul, a, b)))


def dbfs(value):
    """A linear sample magnitude in dB relative to full scale."""
    return 20 * math.log10(value / FULL_SCALE) if value > 0 else SILENT_DBFS


def pcm(frame):
    samples = array("h")
    samples.frombytes(frame)
    return samples


def levels(samples):
    """Peak magnitude and root mean square of signed 16-bit samples."""
    if not samples:
        return 0, 0.0
    return max(max(samples), -min(samples)), math.sqrt(_sumprod(samples, samples) / len(samples))


def amplify(samples, gain):
    """Scale signed 16-bit samples, clipping at full scale."""
    top, bottom = FULL_SCALE - 1, -FULL_SCALE
    return array("h", [bottom if value < bottom else top if value > top else value
                       for value in (round(sample * gain) for sample in samples)]).tobytes()


class MicLevel:
    """Software gain before recognition.

    The owner's gain is fixed. On top of it, a quiet analog noise floor is
    lifted towards TARGET_FLOOR_DBFS, at most MAX_AUTO_GAIN times, so the
    endpointer's internal speech threshold still triggers on a quiet built-in
    microphone. The lift drops at once when a peak would clip and rises slowly
    and only between speech segments.
    """

    def __init__(self, manual=1.0, auto=True):
        self.manual = manual
        self.auto = auto
        self.lift = 1.0
        self.floor = None
        self.recent_peak = SILENT_DBFS

    @property
    def gain(self):
        return self.manual * self.lift

    def observe(self, peak, rms, in_speech):
        """Take one raw frame's levels and update the gain for the next frame."""
        peak_db, rms_db = dbfs(peak), dbfs(rms)
        if not in_speech:
            if self.floor is None:
                self.floor = rms_db
            else:
                self.floor += (FLOOR_FALL if rms_db < self.floor else FLOOR_RISE) * (rms_db - self.floor)
        self.recent_peak = max(peak_db, self.recent_peak - PEAK_DECAY_DB)
        if not self.auto:
            return
        manual_db = 20 * math.log10(self.manual)
        wanted = 1.0
        if self.floor is not None and DIGITAL_SILENCE_DBFS < self.floor:
            wanted = min(MAX_AUTO_GAIN, 10 ** ((TARGET_FLOOR_DBFS - self.floor - manual_db) / 20))
            wanted = min(wanted, 10 ** ((HEADROOM_DBFS - self.recent_peak - manual_db) / 20))
        wanted = max(1.0, wanted)
        if wanted < self.lift:
            self.lift = wanted
        elif not in_speech:
            self.lift = min(wanted, self.lift * AUTO_GAIN_RAISE)


def grammar_words(phrase):
    """The words of a grammar phrase, or None if it is not plain lowercase words."""
    if not isinstance(phrase, str):
        return None
    words = phrase.split()
    if not words or not all(re.fullmatch(r"[a-z]+(?:'[a-z]+)?", word) for word in words):
        return None
    return words


def command_jsgf(phrases):
    """A JSGF grammar of the commands, with an optional "hey pip" before and "please" after."""
    commands = sorted({" ".join(grammar_words(phrase)) for phrase in phrases if grammar_words(phrase)})
    if not commands:
        raise ValueError("a command grammar needs commands")
    return ("#JSGF V1.0;\ngrammar pip;\n"
            f"<command> = {' | '.join(commands)};\n"
            "public <utterance> = [hey | hi | okay] [pip] <command> [please | now];\n")


def find_command(hypothesis, phrases):
    """(phrase, extra words) for the one whole command in a grammar hypothesis, or None.

    The extra words are those around it other than courtesy words such as
    "please" or the name (the grammar allows none, so any is a decoding fault).
    """
    words = (hypothesis or "").split()
    best = None
    for phrase in phrases:
        wanted = grammar_words(phrase)
        if not wanted:
            continue
        for start in range(len(words) - len(wanted) + 1):
            if words[start:start + len(wanted)] == wanted:
                if best is None or len(wanted) > len(best[1]):
                    best = (" ".join(wanted), wanted, start)
                break
    if best is None:
        return None
    phrase, wanted, start = best
    rest = words[:start] + words[start + len(wanted):]
    return phrase, sum(word not in COURTESY_WORDS for word in rest)


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
      speech runs on for ONESHOT_AFTER_SECONDS after it (only if `oneshot`;
      otherwise, with `run_on_wake` set, a plain `hear.wake {run_on}`), and while attending
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
        # The files are there: voice_engines.choose_asr decides whether Moonshine
        # transcribes after the wake; PocketSphinx always keeps the wake phrase.
        return "pocketsphinx", None
    if name != "pocketsphinx":
        return "pocketsphinx", f"Unknown speech recognizer {name!r}. Using PocketSphinx."
    return "pocketsphinx", None


class Recognizer(RecognizerEngine):
    def __init__(self, wake_word, model=MODEL, library="libpocketsphinx.so.5"):
        self.lib = c.CDLL(library)
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
            "ps_get_hyp": (string, pointer, c.POINTER(c.c_int32)),
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
        # Confidence is diagnostic only. An older library without these still decodes.
        self._get_prob = self._optional("ps_get_prob", c.c_int32, pointer)
        self._get_logmath = self._optional("ps_get_logmath", pointer, pointer)
        self._logmath_exp = self._optional("logmath_exp", c.c_double, pointer, c.c_int32)
        int_ptr = c.POINTER(c.c_int32)
        self._seg_prob = self._optional("ps_seg_prob", c.c_int32, pointer, int_ptr, int_ptr, int_ptr)
        self._log_to_ln = self._optional("logmath_log_to_ln", c.c_double, pointer, c.c_int32)
        # The command grammar second pass and the n-best list are optional too.
        self._add_jsgf = self._optional("ps_add_jsgf_string", integer, pointer, string, string)
        self._add_allphone = self._optional("ps_add_allphone_file", integer, pointer, string, string)
        self._lookup_word = self._optional("ps_lookup_word", pointer, pointer, string)
        self._ckd_free = self._optional("ckd_free", None, pointer)
        self._nbest = self._optional("ps_nbest", pointer, pointer)
        self._nbest_next = self._optional("ps_nbest_next", pointer, pointer)
        self._nbest_hyp = self._optional("ps_nbest_hyp", string, pointer, int_ptr)
        self._nbest_free = self._optional("ps_nbest_free", None, pointer)

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
        self.wake_phrase = wake_word
        self.model = Path(model)
        self.endpoint = None
        self.mode = None
        self.frames = 0
        self.last_partial = ""
        self.wake_end = None
        self.wake_preroll = deque(maxlen=WAKE_PREROLL_FRAMES + 1)
        self.wake_track_from = 0
        self.in_speech = False
        self.wake_score = None
        self.command_phrases = ()
        self.utterance = []

    def _optional(self, name, result, *args):
        try:
            function = getattr(self.lib, name)
        except AttributeError:
            return None
        function.restype = result
        function.argtypes = args
        return function

    def segment_score(self, segment):
        """The keyword spotter's detection score for one segment, if the library reports it.

        The keyphrase search's own hypothesis score is always 0, so the score
        comes from the detected segment instead (log domain; higher is surer)."""
        seg_prob = getattr(self, "_seg_prob", None)
        if seg_prob is None:
            return None
        ascr, lscr, lback = c.c_int32(), c.c_int32(), c.c_int32()
        try:
            value = seg_prob(segment, c.byref(ascr), c.byref(lscr), c.byref(lback))
        except (c.ArgumentError, OSError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def confidence(self):
        """Posterior probability of the last dictation, 0 to 1, if the library reports it."""
        return self.scores()[0]

    def log_posterior(self):
        """The last search's posterior as a natural logarithm (0 is certain), if the library reports it."""
        return self.scores()[1]

    def scores(self):
        """(probability, natural-log posterior) of the last search; either may be None.

        A probability below 0.005 prints as 0.00, so the logarithm is what
        tells a near miss from noise."""
        get_prob = getattr(self, "_get_prob", None)
        get_logmath = getattr(self, "_get_logmath", None)
        if get_prob is None or get_logmath is None:
            return None, None
        try:
            logmath = get_logmath(self.decoder)
            if not logmath:
                return None, None
            raw = get_prob(self.decoder)
            # The library reports 0 (probability 1) when it computed no posterior.
            if not isinstance(raw, int) or isinstance(raw, bool) or raw == 0:
                return None, None
            return self._probability(logmath, raw), self._natural_log(logmath, raw)
        except (c.ArgumentError, OSError):
            return None, None

    def _probability(self, logmath, raw):
        logmath_exp = getattr(self, "_logmath_exp", None)
        value = logmath_exp(logmath, raw) if logmath_exp is not None else None
        return value if isinstance(value, float) and 0.0 <= value <= 1.0 else None

    def _natural_log(self, logmath, raw):
        log_to_ln = getattr(self, "_log_to_ln", None)
        value = log_to_ln(logmath, raw) if log_to_ln is not None else None
        return value if isinstance(value, float) and math.isfinite(value) and value <= 0.0 else None

    def nbest(self, limit=5):
        """Up to `limit` alternative transcripts of the last dictation, best first (diagnostics only)."""
        functions = [getattr(self, name, None) for name in ("_nbest", "_nbest_next", "_nbest_hyp", "_nbest_free")]
        if None in functions:
            return []
        first, following, hypothesis, release = functions
        found = []
        try:
            iterator = first(self.decoder)
            while iterator and len(found) < limit:
                text = hypothesis(iterator, None)
                if text:
                    found.append(text.decode(errors="replace"))
                nxt = following(iterator)
                if not nxt:
                    # The last step frees the iterator itself.
                    iterator = None
                    break
                iterator = nxt
            if iterator:
                release(iterator)
        except (c.ArgumentError, OSError):
            return found
        return found

    # The command grammar ----------------------------------------------------------

    def knows(self, word):
        """True if the pronouncing dictionary has `word`."""
        lookup = getattr(self, "_lookup_word", None)
        if lookup is None:
            return False
        phones = lookup(self.decoder, word.encode())
        if not phones:
            return False
        release = getattr(self, "_ckd_free", None)
        if release is not None:
            release(phones)
        return True

    def load_command_grammar(self, phrases):
        """Add the command grammar and the phone loop it is judged against.

        Returns the phrases the grammar holds, or () when either search could
        not load (then there is no second pass). Phrases with a word the
        dictionary lacks are left out; the grammar would not load with them."""
        add, allphone = getattr(self, "_add_jsgf", None), getattr(self, "_add_allphone", None)
        if add is None or allphone is None or getattr(self, "_lookup_word", None) is None:
            return ()
        known = tuple(dict.fromkeys(" ".join(words) for words in map(grammar_words, phrases)
                                    if words and all(self.knows(word) for word in words)))
        phones = Path(getattr(self, "model", MODEL)) / PHONE_LM
        if not known or not phones.is_file():
            return ()
        if allphone(self.decoder, PHONE_SEARCH, str(phones).encode()) < 0:
            return ()
        if add(self.decoder, GRAMMAR_SEARCH, command_jsgf(known).encode()) < 0:
            return ()
        self.command_phrases = known
        return known

    def utterance_audio(self):
        """The audio the last dictation search decoded, as 16 kHz mono S16_LE bytes."""
        return b"".join(self.utterance)

    def decode_command(self, audio):
        """The command grammar's reading of `audio`, or None.

        A dict: `text` (empty when the grammar's final state was not reached,
        as PocketSphinx then gives no hypothesis), `log_posterior` (natural
        log, or None) and `margin`: how much better the grammar's path scored
        than the free phone loop, per 10 ms frame (None without a text).
        Only between searches. Blocking: a few hundredths of the audio's length."""
        if not self.command_phrases or self.mode not in (None, SKIP) or len(audio) < 2:
            return None
        count = len(audio) // 2
        samples = (c.c_int16 * count).from_buffer_copy(audio[:count * 2])
        try:
            text, score, log_posterior = self._search(GRAMMAR_SEARCH, samples)
            margin = None
            if text:
                _, garbage, _ = self._search(PHONE_SEARCH, samples)
                margin = (score - garbage) / (count / (RATE / 100))
            return {"text": text, "log_posterior": log_posterior, "margin": margin}
        except RuntimeError:
            return None
        finally:
            self.lib.ps_activate_search(self.decoder, self.dictation)

    def _search(self, name, samples):
        """(hypothesis, path score, natural-log posterior) of one whole-utterance search."""
        if self.lib.ps_activate_search(self.decoder, name) < 0 or self.lib.ps_start_utt(self.decoder) < 0:
            raise RuntimeError("could not start the search")
        status = self.lib.ps_process_raw(self.decoder, samples, len(samples), 0, 1)
        self.lib.ps_end_utt(self.decoder)
        if status < 0:
            raise RuntimeError("could not decode the audio")
        score = c.c_int32()
        text = (self.lib.ps_get_hyp(self.decoder, c.byref(score)) or b"").decode(errors="replace")
        return text, score.value, self.log_posterior() if text else None

    # Read-only fallbacks for instances built without __init__ in tests.
    oneshot = False
    oneshot_queue = ()
    wake_history = ()
    in_speech = False
    wake_score = None
    wake_candidate_score = None
    command_phrases = ()
    utterance = ()
    # Set by the voice while asleep: a wake phrase that runs on into more words
    # still wakes (plainly, without them) instead of being ignored.
    run_on_wake = False

    def start_capture(self):
        self.capture_frames = 0
        self.in_speech = False
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
        self.wake_candidate_score = None
        if mode == getattr(self, "dictation", None):
            # Kept for the command grammar's second look, then replaced by the next dictation.
            self.utterance = []

    def decode(self, samples):
        if self.lib.ps_process_raw(self.decoder, samples, FRAME_SAMPLES, 0, 0) < 0:
            raise RuntimeError("could not decode speech audio")
        self.frames += 1
        if self.mode == getattr(self, "dictation", None) and isinstance(self.utterance, list):
            self.utterance.append(c.string_at(samples, FRAME_BYTES))

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
                    self.wake_candidate_score = self.segment_score(segment)
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
                    self.wake_score = getattr(self, "wake_candidate_score", None)
                    return [("hear.wake", {})]
            return []

        decoded = self.decoded_until()
        if self.wake_end is not None and decoded - self.wake_end > ONESHOT_AFTER_SECONDS:
            # Speech runs on after the name: "Hey Pip, what time is it?"
            if oneshot:
                return self.begin_oneshot()
            self.end_search(SKIP)
            if self.run_on_wake:
                # Asleep, the words cannot be taken in one breath, but the
                # name was said: wake as for the name alone. `run_on` tells
                # the voice why (it is not sent on the bus).
                self.wake_preroll.clear()
                self.wake_history = ()
                self.wake_score = getattr(self, "wake_candidate_score", None)
                return [("hear.wake", {"run_on": True})]
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
        self.wake_score = getattr(self, "wake_candidate_score", None)
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
        self.in_speech = in_speech
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
