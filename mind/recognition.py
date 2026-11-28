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
# The one-breath replay into dictation starts this many 30 ms frames after the
# end of the name as the keyword search aligns it. That alignment ends the name
# early, and its last sounds led the transcript ("have", "huh", "shape and").
# Measured offline with this recognizer on "Hey Pip, <command>" in three voices
# at -12 dBFS over a -64 dBFS floor: 15 of 39 understood starting 90 ms early,
# 19 at the aligned end, 25 at 90 ms later and 17 at 180 ms later (the first
# word clipped). Owner recordings should retune it.
ONESHOT_SKIP_FRAMES = 3
# Decoded wake-search audio kept for a one-breath replay into dictation.
WAKE_HISTORY_FRAMES = int((WAKE_DECODE_CAP_SECONDS + ONESHOT_AFTER_SECONDS) / FRAME_SECONDS) + WAKE_PREROLL_FRAMES + 1
# Longest utterance, in frames, before dictation closes it.
MAX_UTTERANCE_FRAMES = 600
# WP-E6 research arm: six 30 ms capture frames, selected from the handover's
# 150-200 ms bound.  It is never enabled by this module itself.
HYBRID_TAIL_FRAMES = 6
HYBRID_LEVEL_FRAMES = 4
HYBRID_MIN_LEVEL = 0.001
_UNFINISHED_END = re.compile(
    r"(?:^|\s)(?:and|or|but|because|although|though|if|when|while|that|the|a|an|my|your|our|to|of|with|from|for)\s*$",
    re.I,
)
SKIP = "skip"
# The second search, over a grammar of the commands, that mind/voice.py may
# run on the audio of a dictation the open-vocabulary search did not hear as
# one. A grammar always finds its closest command, so a free phone loop
# decodes the same audio as the garbage model: only a command path that
# scores well above it (per 10 ms frame, in the library's log units) counts.
GRAMMAR_SEARCH = b"commands"
PHONE_SEARCH = b"phones"
PHONE_LM = "en-us-phone.lm.bin"
# The library's err_lvl_t: ERR_DEBUG, ERR_INFO, ERR_WARN, ERR_ERROR, ERR_FATAL.
LOG_FATAL = 4
# Words around a command that change nothing about it.
COURTESY_WORDS = frozenset(("hey", "hi", "ok", "okay", "pip", "please", "now", "thanks", "thank", "you"))


class HybridEndpoint:
    """Content-free WP-E6 tail decision, independently replayable offline.

    ``observe_level`` accepts one normalized PCM RMS value per 30 ms capture
    frame. ``extension`` returns a reason and exactly six frames, or no
    extension.  The semantic arm needs a fresh PocketSphinx partial; the
    acoustic arm needs a monotonic, material decay that remains above digital
    silence.  Neither arm learns words or changes the recognizer's VAD.
    """

    def __init__(self):
        self.levels = deque(maxlen=HYBRID_LEVEL_FRAMES)

    def reset(self):
        self.levels.clear()

    def observe_level(self, level):
        if type(level) not in (int, float) or not math.isfinite(level) or level < 0:
            raise ValueError("endpoint replay levels must be finite and non-negative")
        self.levels.append(float(level))

    def observe_samples(self, samples):
        if len(samples):
            square = sum(int(value) * int(value) for value in samples) / len(samples)
            self.observe_level(math.sqrt(square) / 32768.0)

    def extension(self, partial, *, fresh=False):
        if fresh and isinstance(partial, str) and _UNFINISHED_END.search(partial.strip()):
            return "syntax", HYBRID_TAIL_FRAMES
        if len(self.levels) == HYBRID_LEVEL_FRAMES:
            levels = tuple(self.levels)
            falling = all(left >= right for left, right in zip(levels, levels[1:]))
            if (falling and levels[-1] >= HYBRID_MIN_LEVEL
                    and levels[0] >= max(levels[-1] * 1.5, HYBRID_MIN_LEVEL * 2)):
                return "acoustic", HYBRID_TAIL_FRAMES
        return None, 0

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
# At most this much lift. On the laptop (cable run, 25 Nov) the lift reached
# 5.6 times overnight and 8 in pip-diag runs, and the room's own fan and hum,
# lifted that far, woke Pip about 28 times an hour in a quiet room. Speech
# arrives only about 15 dB over that floor whatever the gain, so more lift
# raises the noise as much as the words.
MAX_AUTO_GAIN = 3.0
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

    def __init__(self, manual=1.0, auto=True, max_lift=MAX_AUTO_GAIN):
        self.manual = manual
        self.auto = auto
        self.max_lift = max_lift
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
            wanted = min(self.max_lift, 10 ** ((TARGET_FLOOR_DBFS - self.floor - manual_db) / 20))
            wanted = min(wanted, 10 ** ((HEADROOM_DBFS - self.recent_peak - manual_db) / 20))
        wanted = max(1.0, wanted)
        if wanted < self.lift:
            self.lift = wanted
        elif not in_speech:
            self.lift = min(wanted, self.lift * AUTO_GAIN_RAISE)


# What a misheard "Hey Pip" leaves at the start of a one-breath transcript. The
# replay begins after the name, but the end of the name, or a recognizer that
# heard all of it as other words, can lead the words: "Pep open a terminal.",
# "Yep, close the terminal.", "I ate pip, are you okay?". Words that can stand
# for "hey" before the name:
ADDRESS_WORDS = frozenset((
    "hey", "hay", "hi", "a", "ay", "aye", "i", "they", "he", "ate", "it", "oh", "ok", "okay",
))
# Real words that sound like the name but begin requests: never taken for it.
NOT_THE_NAME = frozenset((
    "put", "puts", "keep", "keeps", "kept", "pick", "book", "books", "boot", "boots", "beat", "beats", "but",
))
# A first clause that says something of its own is never dropped as the name.
MEANINGFUL_HEADS = frozenset((
    "no", "nope", "not", "never", "don't", "dont", "stop", "wait", "cancel", "yes",
))
# How far into the transcript the name's remnant may be.
REMNANT_WORDS = 3
# What "pip" turns into that is hardly a word of its own: dropped even with
# no comma after it. A real word that only sounds like the name (bit, pet,
# pot, beep, yep, hip, ...) goes only as a clause of its own ("Yep, ...").
PIP_REMNANTS = frozenset(("pip", "pips", "pipp", "pep", "peps", "pepp", "bip", "bips", "kip", "kips"))


def _distance(first, second):
    """Levenshtein distance between two words."""
    row = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        previous, row[0] = row[0], i
        for j, b in enumerate(second, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (a != b))
    return row[-1]


def name_like(word, name="pip"):
    """True if `word` sounds enough like the name to be it misheard, and is not
    a request's own first word. For "pip": pip, pep, bip, kip, pet, yep, hip.
    For another name: the name, or a one-letter slip of one of 4 letters or more."""
    word = re.sub(r"[^a-z]", "", (word or "").lower())
    name = re.sub(r"[^a-z]", "", (name or "pip").lower()) or "pip"
    if not word or word in NOT_THE_NAME:
        return False
    if word == name:
        return True
    if name != "pip":
        return len(name) >= 4 and _distance(word, name) <= 1
    if word in ("peep", "pipp", "pepp", "pimp"):
        return True
    return bool(re.fullmatch(r"[pbk][aeiouy]{1,2}[pbtk]{1,2}s?", word) or re.fullmatch(r"[hy][aeiouy]{1,2}p", word))


def name_remnant(word, name="pip"):
    """True if `word` is the name misheard and hardly a word of its own: the
    name itself, or for "pip" one of PIP_REMNANTS. Unlike name_like, never a
    real word such as bit, bet, pot or beep."""
    word = re.sub(r"[^a-z]", "", (word or "").lower())
    name = re.sub(r"[^a-z]", "", (name or "pip").lower()) or "pip"
    if not word or word in NOT_THE_NAME:
        return False
    return word == name or (name == "pip" and word in PIP_REMNANTS)


def name_ish(word, name="pip"):
    """True if `word`, set apart by a comma, can be taken for the name: a
    name-like word (name_like), or for "pip" a looser hearing of it that is
    no English word ("apep", "apepe", "peck"). Only ever used on a clause of
    its own: a word this loose alone is often a real one."""
    word = re.sub(r"[^a-z]", "", (word or "").lower())
    if name_like(word, name):
        return True
    name = re.sub(r"[^a-z]", "", (name or "pip").lower()) or "pip"
    return name == "pip" and word not in NOT_THE_NAME and bool(
        re.fullmatch(r"a?[pbk][aeiouy]{1,2}(?:[pbtk]{1,2}|ck)e?s?", word))


# A leading clause, ended by a comma, of at most this many words may be what
# the name left (Moonshine writes "Hey Pip" as a clause of its own: "Lay pit,
# what time is it?", "I apep, set a timer", "Hey Peck, what do you remember").
CLAUSE_WORDS = 2


def _clause_remnant(words, name):
    """How many leading words are a comma-ended clause the name left, or 0.

    The clause has at most CLAUSE_WORDS words and ends with a name-ish word,
    or is only words that stand for "hey" ("Hey, ..."); none of its words says
    something of its own ("No, ...", "Stop, pep"), and something follows. "In
    Paris, ..." and "I think, ..." stay."""
    for count in range(1, min(CLAUSE_WORDS, len(words) - 1) + 1):
        if not words[count - 1].endswith(","):
            continue
        clause = [re.sub(r"[^a-z']", "", word.lower()) for word in words[:count]]
        if any(word in MEANINGFUL_HEADS for word in clause):
            return 0
        if name_ish(clause[-1], name) or all(word in ADDRESS_WORDS for word in clause):
            return count
        return 0
    return 0


def strip_name_remnant(text, name="pip"):
    """A one-breath transcript without a misheard name at its start.

    The one rule set for both recognizers, measured on the laptop's
    transcripts (cable run, 25 Nov: Moonshine one-breath requests understood
    went from 17 to 31 of 42, with none lost). Something must be left, and a
    transcript that is only the name stays.

    - A leading comma-ended clause of at most CLAUSE_WORDS words that the name
      left (_clause_remnant) goes: "Lay pit, what time is it?".
    - Within the first REMNANT_WORDS words, everything up to and including
      the name's remnant (name_remnant), or a name-like or "hey"-like word
      with a comma after it, goes when every word before it could stand for
      "hey" (or is the remnant itself).

    This runs on requests for the model too, so a real word that only sounds
    like the name ("a pot of tea", "I bet you cannot") stays unless a comma
    sets it apart. A first clause that says something of its own ("No, ...",
    "In Paris, ...") is kept. A leftover that is a real word with no comma
    ("hey pat what time is it") is the voice's to judge: it drops it only
    when what remains is a safe command (voice.CommandGrammar.without_remnant)."""
    if not isinstance(text, str):
        return text
    words = text.split()
    clause = _clause_remnant(words, name)
    if clause:
        rest = " ".join(words[clause:]).lstrip(" ,.!:;-")
        if rest.strip():
            return rest
    for index, raw in enumerate(words[:REMNANT_WORDS]):
        word = re.sub(r"[^a-z']", "", raw.lower())
        if word in MEANINGFUL_HEADS:
            return text
        heads = [re.sub(r"[^a-z']", "", earlier.lower()) for earlier in words[:index]]
        addressed = all(head in ADDRESS_WORDS or name_remnant(head, name) for head in heads)
        if not addressed:
            return text
        clause = raw.endswith(",")
        if name_remnant(word, name) or (clause and (name_like(word, name) or word in ADDRESS_WORDS)):
            rest = " ".join(words[index + 1:]).lstrip(" ,.!:;-")
            return rest if rest.strip() else text
        if word not in ADDRESS_WORDS:
            return text
    return text


# Words that open a spoken address before the name ("Hey Pip, ...").
GREETING_WORDS = frozenset(("hey", "hay", "hi", "ok", "okay", "oh"))


def strip_address(text, name="pip"):
    """A dictation heard without the wake phrase (the follow-up after a reply),
    without a "hey/okay + name" said at its start anyway: "Hey Pip, what time
    is it" or "okay pip what time is it". Stricter than strip_name_remnant,
    because nothing says the name was there: only the name itself, or a
    greeting word followed by the name's remnant (name_remnant) or by a
    name-like word set apart by a comma, goes ("hey bob is here" stays).
    Something must be left."""
    if not isinstance(text, str):
        return text
    words = text.split()
    clean = [re.sub(r"[^a-z']", "", word.lower()) for word in words[:2]]
    spoken = re.sub(r"[^a-z]", "", (name or "pip").lower()) or "pip"
    if clean and clean[0] == spoken:
        cut = 1
    elif len(clean) == 2 and clean[0] in GREETING_WORDS and (
            name_remnant(clean[1], name) or (words[1].endswith(",") and name_like(clean[1], name))):
        cut = 2
    else:
        return text
    rest = " ".join(words[cut:]).lstrip(" ,.!:;-")
    return rest if rest.strip() else text


# The capture high-pass for what the recognizer hears. The laptop's room and
# fan noise has its hump at 63-125 Hz (cable run, 25 Nov), below the voice's
# fundamental; a second-order Butterworth high-pass at HIGHPASS_HZ takes 12 to
# 17 dB off that hump and leaves speech above 300 Hz within 0.5 dB.
HIGHPASS_HZ = 170.0
HIGHPASS_MIN_HZ = 50.0
HIGHPASS_MAX_HZ = 400.0


class HighPass:
    """A second-order Butterworth high-pass (one RBJ biquad, transposed direct
    form II) over 16-bit samples, its state carried from frame to frame.

    `process` applies a gain and the filter in one pass and clips at full
    scale. `reset` forgets the state, for a new capture or after the voice fed
    the recognizer silence of its own."""

    def __init__(self, cutoff=HIGHPASS_HZ, rate=RATE):
        if not (0 < cutoff < rate / 2):
            raise ValueError(f"a high-pass at {cutoff} Hz does not fit {rate} Hz audio")
        self.cutoff = float(cutoff)
        self.rate = rate
        omega = 2 * math.pi * cutoff / rate
        alpha = math.sin(omega) / (2 * math.sqrt(0.5))
        cos = math.cos(omega)
        a0 = 1 + alpha
        self.b0 = (1 + cos) / 2 / a0
        self.b1 = -(1 + cos) / a0
        self.b2 = (1 + cos) / 2 / a0
        self.a1 = -2 * cos / a0
        self.a2 = (1 - alpha) / a0
        self.reset()

    def reset(self):
        self.z1 = 0.0
        self.z2 = 0.0

    def response_db(self, frequency):
        """The filter's gain at `frequency` in dB (for tests and the log)."""
        z = complex(math.cos(2 * math.pi * frequency / self.rate), -math.sin(2 * math.pi * frequency / self.rate))
        value = abs((self.b0 + self.b1 * z + self.b2 * z * z) / (1 + self.a1 * z + self.a2 * z * z))
        return 20 * math.log10(value) if value > 0 else SILENT_DBFS

    def process(self, samples, gain=1.0):
        """Filter signed 16-bit `samples` (an array or list) after `gain`, as bytes."""
        b0, b1, b2, a1, a2 = self.b0 * gain, self.b1 * gain, self.b2 * gain, self.a1, self.a2
        z1, z2 = self.z1, self.z2
        top, bottom = FULL_SCALE - 1, -FULL_SCALE
        out = array("h", bytes(2 * len(samples)))
        for index, sample in enumerate(samples):
            value = b0 * sample + z1
            z1 = b1 * sample - a1 * value + z2
            z2 = b2 * sample - a2 * value
            value = round(value)
            out[index] = bottom if value < bottom else top if value > top else value
        # A denormal state costs time forever after: settle it to zero.
        self.z1 = z1 if abs(z1) > 1e-6 else 0.0
        self.z2 = z2 if abs(z2) > 1e-6 else 0.0
        return out.tobytes()


def highpass_setting(environ, cmdline=Path("/proc/cmdline"), default=False):
    """(cutoff in Hz or None for off, where it came from).

    BEING_MIC_HIGHPASS wins over the last being.mic_highpass= kernel argument;
    either takes 0/off/no/false, 1/on/yes/true (HIGHPASS_HZ), or a cutoff from
    HIGHPASS_MIN_HZ to HIGHPASS_MAX_HZ. Anything else is ignored. Without
    either, `default` decides."""
    found = []
    raw = environ.get("BEING_MIC_HIGHPASS")
    if raw is not None and raw.strip():
        found.append((raw.strip().lower(), "BEING_MIC_HIGHPASS"))
    try:
        args = Path(cmdline).read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        args = []
    values = [arg.split("=", 1)[1] for arg in args if arg.startswith("being.mic_highpass=")]
    if values:
        found.append((values[-1].strip().lower(), "the kernel command line"))
    for value, source in found:
        if value in ("0", "off", "no", "false"):
            return None, source
        if value in ("1", "on", "yes", "true"):
            return HIGHPASS_HZ, source
        try:
            cutoff = float(value)
        except ValueError:
            continue
        if math.isfinite(cutoff) and HIGHPASS_MIN_HZ <= cutoff <= HIGHPASS_MAX_HZ:
            return cutoff, source
    return (HIGHPASS_HZ if default else None), "the default"


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
        # The library's log level, to quiet the grammar search's expected
        # "Final result does not match the grammar" (fsg_search.c) ERROR lines.
        self._set_loglevel = self._optional("err_set_loglevel", integer, integer)

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
        self.last_partial_frame = -MAX_UTTERANCE_FRAMES
        self.hybrid_endpoint = False
        self.endpoint_policy = HybridEndpoint()
        self.endpoint_tail = 0
        self.endpoint_extension_frames = 0
        self.endpoint_extension_reason = None
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
        quiet = self._quiet() if name == GRAMMAR_SEARCH else None
        try:
            if self.lib.ps_activate_search(self.decoder, name) < 0 or self.lib.ps_start_utt(self.decoder) < 0:
                raise RuntimeError("could not start the search")
            status = self.lib.ps_process_raw(self.decoder, samples, len(samples), 0, 1)
            self.lib.ps_end_utt(self.decoder)
            if status < 0:
                raise RuntimeError("could not decode the audio")
            score = c.c_int32()
            text = (self.lib.ps_get_hyp(self.decoder, c.byref(score)) or b"").decode(errors="replace")
            return text, score.value, self.log_posterior() if text else None
        finally:
            if quiet is not None:
                self._loud(quiet)

    def _quiet(self):
        """Raise the library's log level to fatal; returns the old level, or None.

        Speech that is no command never reaches the grammar's final state,
        and the library reports each such search as an ERROR on standard
        error (18 lines in one cable session). That is the expected answer
        "no command", not a fault, so the grammar search runs quiet."""
        set_level = getattr(self, "_set_loglevel", None)
        if set_level is None:
            return None
        try:
            previous = set_level(LOG_FATAL)
        except (c.ArgumentError, OSError):
            return None
        return previous if isinstance(previous, int) and 0 <= previous < LOG_FATAL else None

    def _loud(self, level):
        try:
            self._set_loglevel(level)
        except (c.ArgumentError, OSError):
            pass

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
    hybrid_endpoint = False
    endpoint_tail = 0
    endpoint_extension_frames = 0
    endpoint_extension_reason = None

    @property
    def endpoint_policy(self):
        # Created on first use so a recognizer built without __init__ still works.
        policy = self.__dict__.get("_endpoint_policy")
        if not isinstance(policy, HybridEndpoint):
            policy = self.__dict__["_endpoint_policy"] = HybridEndpoint()
        return policy

    @endpoint_policy.setter
    def endpoint_policy(self, value):
        self.__dict__["_endpoint_policy"] = value

    def start_capture(self):
        self.capture_frames = 0
        self.in_speech = False
        self.endpoint_tail = 0
        if not isinstance(getattr(self, "endpoint_policy", None), HybridEndpoint):
            self.endpoint_policy = HybridEndpoint()
        self.endpoint_policy.reset()
        self.endpoint = self.lib.ps_endpointer_init(0.36, 0.75, 2, RATE, 0.03)
        if not self.endpoint:
            raise RuntimeError("could not initialize speech endpointing")

    def stop_capture(self):
        self.wake_preroll.clear()
        self.wake_history = ()
        self.oneshot = False
        self.oneshot_queue = ()
        self.endpoint_tail = 0
        policy = getattr(self, "endpoint_policy", None)
        if isinstance(policy, HybridEndpoint):
            policy.reset()
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
        self.last_partial_frame = -MAX_UTTERANCE_FRAMES
        self.endpoint_tail = 0
        self.endpoint_extension_frames = 0
        self.endpoint_extension_reason = None
        self.endpoint_policy.reset()
        self.wake_end = None
        self.wake_candidate_score = None
        if mode == getattr(self, "dictation", None):
            # Kept for the command grammar's second look, then replaced by the next dictation.
            self.utterance = []

    def set_hybrid_endpoint(self, enabled):
        """Enable only the explicit Try experiment, between utterances."""
        self.hybrid_endpoint = enabled is True
        self.endpoint_tail = 0
        if not isinstance(getattr(self, "endpoint_policy", None), HybridEndpoint):
            self.endpoint_policy = HybridEndpoint()
        self.endpoint_policy.reset()

    def _extend_endpoint(self):
        if (not self.hybrid_endpoint or self.endpoint_extension_frames > 0
                or self.frames >= MAX_UTTERANCE_FRAMES):
            return False
        text = self.hypothesis()
        fresh = text != self.last_partial or self.frames - self.last_partial_frame <= HYBRID_TAIL_FRAMES
        reason, frames = self.endpoint_policy.extension(text, fresh=fresh)
        if not frames:
            return False
        self.endpoint_tail = min(frames, MAX_UTTERANCE_FRAMES - self.frames)
        self.endpoint_extension_frames += self.endpoint_tail
        self.endpoint_extension_reason = reason
        return self.endpoint_tail > 0

    def _finish_dictation(self, *, oneshot=False):
        self.end_search()
        self.endpoint_tail = 0
        if oneshot:
            self.oneshot = False
            self.oneshot_queue = ()
            name = (getattr(self, "wake_phrase", None) or "pip").split()[-1:] or ["pip"]
            text = strip_name_remnant(self.hypothesis(), name[0])
            return [("hear.final", {"text": text, "oneshot": True}) if text
                    else ("hear.cancel", {"oneshot": True})]
        text = self.hypothesis()
        return [("hear.final", {"text": text}) if text else ("hear.cancel", {})]

    def _partial(self, *, oneshot=False):
        if self.frames % 3:
            return []
        text = self.hypothesis()
        if not text or text == self.last_partial:
            return []
        self.last_partial = text
        self.last_partial_frame = self.frames
        return [("hear.partial", {"text": text, **({"oneshot": True} if oneshot else {})})]

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
        # Start ONESHOT_SKIP_FRAMES after the frame holding the name's end, as
        # aligned (the epsilon absorbs float error).
        first = int((self.wake_end - self.search_start) / FRAME_SECONDS + 1e-6) + ONESHOT_SKIP_FRAMES
        # The history is bounded: index it from the oldest frame it still holds.
        oldest = self.frames - len(self.wake_history)
        history = list(self.wake_history)
        skip = max(0, first - oldest)
        replay = deque(history[skip:])
        # Frames not yet decoded follow; a skip past the history goes on into them.
        replay.extend(list(self.wake_preroll)[max(0, skip - len(history)):])
        self.wake_preroll.clear()
        self.wake_history = ()
        self.wake_score = getattr(self, "wake_candidate_score", None)
        self.end_search()
        self.begin(self.dictation)
        self.oneshot = True
        self.oneshot_queue = replay
        return [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})]

    def continue_oneshot(self, samples, in_speech):
        self.endpoint_policy.observe_samples(samples)
        extending = self.endpoint_tail > 0
        if in_speech:
            self.endpoint_tail = 0
        self.oneshot_queue.append(samples)
        # Catch up a few frames at a time; drain everything when the segment ends.
        count = min(len(self.oneshot_queue), WAKE_REPLAY_FRAMES + 1) if in_speech else len(self.oneshot_queue)
        count = min(count, MAX_UTTERANCE_FRAMES - self.frames)
        for _ in range(count):
            self.decode(self.oneshot_queue.popleft())
        if self.frames >= MAX_UTTERANCE_FRAMES:
            return self._finish_dictation(oneshot=True)
        if not in_speech:
            if extending:
                self.endpoint_tail -= 1
                if self.endpoint_tail == 0:
                    return self._finish_dictation(oneshot=True)
            elif not self._extend_endpoint():
                return self._finish_dictation(oneshot=True)
        return self._partial(oneshot=True)

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
        if self.mode == getattr(self, "dictation", None):
            self.endpoint_policy.observe_samples(samples)
        if self.endpoint_tail and not in_speech:
            # The VAD has already closed.  The opt-in arm deliberately gives
            # these exact raw capture frames to the still-open decoder.
            self.decode(samples)
            self.endpoint_tail -= 1
            if self.frames >= MAX_UTTERANCE_FRAMES or self.endpoint_tail == 0:
                return events + self._finish_dictation()
            return events + self._partial()
        if in_speech:
            self.endpoint_tail = 0
        if not speech:
            return events

        if self.mode is None:
            self.begin(self.dictation)
            self.endpoint_policy.observe_samples(samples)
            events.append(("hear.speech_start", {}))

        self.decode(speech)
        if self.frames >= MAX_UTTERANCE_FRAMES:
            events.extend(self._finish_dictation())
        elif not in_speech:
            if not self._extend_endpoint():
                events.extend(self._finish_dictation())
        else:
            events.extend(self._partial())

        return events
