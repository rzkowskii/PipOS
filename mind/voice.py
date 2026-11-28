"""The local, half-duplex voice organ. Audio stays outside the event bus."""

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import time
import types
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from . import install_status, mic_channel, night_speech, sd_notify
from .bus import Bus
from .care_routes import guest_active
from .dream_policy import DreamPolicy, telemetry_time
from .mic_channel import ChannelPicker
from .mic_port import (
    CAPTURE_QUEUE_SECONDS, DIAG_ENV, CaptureQueue, DiagnosticMicrophone, MicrophonePort, PipeReader, pump,
)
from .profile import DEFAULT_VOICE, FACTORY_CONFIG, VOICES, default_mind_dir, factory_profile, load_profile
from .recognition import (
    DIGITAL_SILENCE_DBFS, FRAME_BYTES, FRAME_SAMPLES, MAX_MIC_GAIN, MIN_MIC_GAIN, RATE, HighPass, MicLevel,
    Recognizer, amplify, dbfs, highpass_setting, levels, pcm, strip_address,
)
from .speech import EMOTIONS
from .tts import (
    CHIRP_PEAK, CHIRP_RATE, CHIRPS, STOCK_LINES, ChirpBank, FliteEngine, WavCache, available_voices, playback_command, scale_wav,
    select_engine, shorten_leading_pause, synthesis_command, volume_gain, write_wav,
)

_IMPORTED_AT = time.monotonic()
FW = Path("/sys/firmware/qemu_fw_cfg/by_name/opt/beingos")
TALKING = ("thinking", "speaking")
ATTENDING = ("attending", "listening")
# Wake spotting stays on while it thinks (task 5.5, first step). Capture still
# stops before its own voice plays, so the voice remains half-duplex.
HEARING = (*ATTENDING, "idle", "drowsy", "asleep", "thinking")
RESTING = ("idle", "drowsy")
# Every state in which the being is at rest, asleep included.
AT_REST = (*RESTING, "asleep")
PAUSE_TRIAL_VOICES = frozenset(("slt", "rms", "awb"))
PAUSE_TRIAL_MAX_CHARS = 512
SUBSCRIPTIONS = (
    "body.lid", "body.state", "state.changed", "ears.policy", "voice.mute", "voice.volume", "voice.chirps",
    "voice.level_request", "voice.engine", "say.request", "say.stop", "stem.chirp", "mind.ready",
    "app.privacy", "organ.down", "voice.prepare", "voice.clear_prepared", install_status.TOPIC,
    "body.mic_policy",
)
FRAME_SECONDS = FRAME_SAMPLES / RATE
RUN_DIR = "/run"
STOCK_BANK = Path("/usr/share/being/stock-speech")


def process_started_at():
    """Best-effort CLOCK_MONOTONIC process start, including Python imports."""
    try:
        stat_line = Path("/proc/self/stat").read_text(encoding="ascii")
        fields = stat_line[stat_line.rindex(")") + 2:].split()
        return int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return _IMPORTED_AT


def startup_log(stage, started, **numbers):
    """Content-free numeric boot stages for exact-image qualification."""
    timestamp = time.monotonic()
    record = {"stage": stage, "mono_s": round(timestamp, 3),
              "elapsed_s": round(timestamp - started, 3)}
    record.update({key: round(value, 3) if isinstance(value, float) else value
                   for key, value in numbers.items()})
    LOG.info("voice startup %s", json.dumps(record, sort_keys=True, separators=(",", ":")))

# After a reply it hears a follow-up without its name only this long (task 3.10).
FOLLOW_UP_SECONDS = 8.0
# At most one onset reaction per this many seconds (task 5.6).
SOUND_INTERVAL = 10.0
# A one-breath request's final waits this long for the brainstem to take its
# wake before it is dropped: the words after the name can be decoded before
# the brainstem's answer is read (a backlog in the microphone stream decodes
# faster than it arrives).
WAKE_ANSWER_SECONDS = 2.0
# The thinking purr starts this long into a prompted turn, then repeats slowly.
PURR_AFTER = 1.5
PURR_REPEAT = 12.0
# Its own earcons and apologies are not heard: the mic reads silence while one
# plays and for this long after.
CHIRP_DEAF_MARGIN = 0.12
CHIRP_TAIL_SECONDS = CHIRP_DEAF_MARGIN
# Capture retries back off from one second to half a minute. A capture that
# ran this long counts as healthy and resets the pause.
CAPTURE_RETRY_SECONDS = 1.0
CAPTURE_RETRY_FIRST = CAPTURE_RETRY_SECONDS
MAX_CAPTURE_RETRY_SECONDS = 30.0
CAPTURE_HEALTHY_SECONDS = 30.0
# Audio the decoder fell too far behind to hear is logged at most this often,
# and a capture that fell this far behind says so when it closes.
DROP_REPORT_SECONDS = 10.0
BACKLOG_REPORT_SECONDS = 1.0
# arecord's ALSA buffer and period, in microseconds.
CAPTURE_BUFFER_US = 500000
CAPTURE_PERIOD_US = 30000
# Two-channel captures that deliver nothing before one channel is tried as well.
STEREO_ATTEMPTS = 3
# aplay gets the clip's length plus this long before it is judged stuck. A
# speaker that fails at once is tried again, with growing pauses.
PLAYBACK_MARGIN = 5.0
PLAYBACK_ATTEMPTS = 3
PLAYBACK_RETRY_FIRST = 0.2
PLAYBACK_LEVEL_INTERVAL = 0.04
MIXER_TIMEOUT = 3.0
SILENCE = bytes(FRAME_BYTES)
# How often, while resting, the floor and loudest sound go to the log.
FLOOR_FIRST_REPORT_SECONDS = 10.0
FLOOR_REPORT_SECONDS = 60.0
# How often, while resting, measured levels go on the bus (voice.level).
LEVEL_SECONDS = 30.0
# A failed dictation this long asks to be repeated even without a wake phrase.
MISSED_MIN_SECONDS = 0.5
# Said by the voice itself when it sends hear.missed, once per wake. The
# brainstem lets replies play only while thinking or speaking, and a miss
# leaves it attending or resting, so this cannot go through say.request.
APOLOGY = "Sorry, I didn't catch that."
# Not after its attention lapsed into sleep, and never over a reply.
APOLOGY_IN = ("attending", "listening", "idle", "drowsy")
# A second apology this soon after the first adds how to type instead, at most
# once in this long.
HINT_WINDOW_SECONDS = 600.0
HINT_INTERVAL_SECONDS = 24 * 3600.0
# The typing hint has not been worded yet.
_UNKNOWN = object()
# A reply this long after the words it answers is not timed against them.
TIMING_MAX_SECONDS = 60.0
# Content-free per-dictation endpoint evidence is opt-in on Try boots only.
# It changes no recognizer threshold and never records samples or words.
ENDPOINT_EVIDENCE_ENV = "BEING_ENDPOINT_EVIDENCE"
# Default-off WP-E6 PocketSphinx endpoint tail.  It exists only on a Try boot;
# production endpoint timing is unchanged until replay and native WER evidence.
HYBRID_ENDPOINT_ENV = "BEING_HYBRID_ENDPOINT"
# A frame this far over the noise floor is the owner's voice, for timing.
VOICED_OVER_FLOOR_DB = 10.0
# A plain "Hey Pip" followed by nothing is answered once the room has been
# quiet (nothing VOICED_OVER_FLOOR_DB over the floor) for MISS_QUIET_SECONDS,
# and no sooner than MISS_AFTER_WAKE_SECONDS after the wake, while the being is
# still attending: the owner can then say it again without the name. Waiting
# for the brainstem's 20 s window to lapse put the apology 20 s after the wake,
# at idle (real laptop, 28 Nov: 12 times in the owner's free talk, often while
# he was still talking). Raw level is the judge, not Silero, which missed his
# quiet speech there.
MISS_AFTER_WAKE_SECONDS = 4.0
# At most one spoken apology in this long; further misses get the face's cue only.
APOLOGY_MIN_INTERVAL_SECONDS = 60
MISS_QUIET_SECONDS = 2.0
# The microphone's per-frame cost is measured over this many frames, once.
COST_FRAMES = 1000
# While resting, voice_engines may take a due measurement this often.
ENGINE_REFRESH_SECONDS = 900.0
# A Try session is short and its /mind is empty at every boot, so it takes one
# measurement of its own this long after the mind says it is ready (the model
# is then answering and warming), once the CPU is quiet; it gives up waiting
# for quiet after TRY_MEASURE_PATIENCE seconds.
TRY_MEASURE_AFTER = 60.0
# Moonshine alone is measured much sooner (every boot that is still on
# PocketSphinx): this long after the mind is ready, still only at rest and
# once the CPU is quiet. The cortex's start-up warming is what keeps the
# CPU busy just after mind.ready, and the quiet check waits for it, so the
# short delay costs nothing but the poll.
ASR_EARLY_AFTER = 15.0
# mind.ready comes once per mind connection and is not replayed, so a voice
# that starts after it (on a cold boot, a restart for pip-diag, or after a
# crash) learns it from the brainstem having left waking (Voice.learn_ready),
# or else waits this long and then takes the mind as ready.
TRY_READY_PATIENCE = 120.0
# The brainstem states that, on a boot with the lid never seen closed, are
# reached only after mind.ready (machine.rs: waking ends once the mind is
# ready). "asleep" is not among them: waking can turn to asleep with the
# mind still starting (a closed or unknown lid). With the lid seen closed,
# waking may also end as a lid wake without the mind, so nothing is learned.
READY_STATES = ("idle", "attending", "listening", "thinking", "speaking", "drowsy")
TRY_MEASURE_POLL = 10.0
TRY_MEASURE_PATIENCE = 600.0
# Playback this much shorter than its clip, or twice this much longer, is reported.
PLAYBACK_TOLERANCE = 0.4
# The speech filter (mind/speaker_eq.py, when it is installed and says it is
# enabled) shapes each spoken clause for the laptop's speakers before it plays.
# BEING_SPEAKER_EQ=0 skips it. A clause may take SPEAKER_EQ_BASE_SECONDS plus
# SPEAKER_EQ_PER_SECOND per second of audio; anything that goes wrong (an
# error, no result in time, a result that is not a sound of about the clause's
# length) plays the clause as it was, and after SPEAKER_EQ_FAILURES failures in
# a row the filter is left out for the rest of the process.
SPEAKER_EQ_ENV = "BEING_SPEAKER_EQ"
SPEAKER_EQ_BASE_SECONDS = 1.0
SPEAKER_EQ_PER_SECOND = 0.5
SPEAKER_EQ_FAILURES = 3
SPEAKER_EQ_LENGTH_SLACK = 0.25

# The command grammar's second pass. When the open-vocabulary transcript of a
# dictation is not a workshop command, PocketSphinx decodes the same audio
# again with a grammar of the safe commands and judges it against a free
# phone loop (a grammar always finds its nearest command; the posterior among
# commands alone does not tell a command from other speech). Only a whole command whose path beats the phone
# loop by GRAMMAR_MARGIN per 10 ms frame, and whose own words the transcript
# already holds (see agreement), replaces the transcript. The margin alone is
# not enough: one-breath non-commands score at most 3.5, but ordinary
# sentences said while attending reach 10 to 22 against their nearest
# command, and only the word check turns those away. Clean commands score 7 to
# 16; owner recordings should retune it (BEING_COMMAND_GRAMMAR_MARGIN).
# BEING_COMMAND_GRAMMAR=0 turns the pass off.
GRAMMAR_MARGIN = 6.0
# Commands are short: a longer dictation is conversation, never re-read.
GRAMMAR_MAX_SECONDS = 4.0
# Kinds of workshop command a grammar result may become (mind/app_commands.py).
GRAMMAR_SAFE_KINDS = frozenset(("open", "close", "minimise", "restore", "say"))
# The grammar always finds its nearest command, even in "tell me about the
# sea" (it read "show me your body" there, with a margin of 13 in attending
# mode). So the grammar may only tidy a transcript that already holds the
# command's own words: every word of the command outside these must be in the
# open-vocabulary transcript, in order (a one-letter slip allowed, two in long
# words), with at most GRAMMAR_STRAY_WORDS other words besides.
GRAMMAR_FILLER_WORDS = frozenset((
    "a", "an", "the", "new", "your", "my", "me", "up", "back", "to", "away", "how", "you're", "you", "are",
))
GRAMMAR_STRAY_WORDS = 2
# A one-breath transcript that is no command may lose at most this many leading
# words the name left behind, and only to become a safe command (see
# CommandGrammar.without_remnant).
REMNANT_WORDS = 2
# Words that turn a command around. "Can't" is the one exception, and only as
# the first word: in a one-breath "Hey Pip, open a terminal" the n-gram often
# hears the end of the name as "can't".
GRAMMAR_NEGATIONS = frozenset((
    "not", "don't", "dont", "never", "no", "didn't", "won't", "wouldn't", "shouldn't", "stop", "can't", "cannot",
))
# When mind/app_commands.py offers no phrase list.
FALLBACK_PHRASES = (
    "open a terminal", "open the terminal", "open terminal", "open a new terminal", "open your terminal",
    "start a terminal", "launch a terminal", "show me the terminal", "close the terminal", "close your terminal",
    "hide the terminal", "open the clock", "show me the clock", "close the clock", "open the monitor",
    "close the monitor",
)
# Kinds of the mind's own commands (mind/commands.py) a one-breath leftover or
# an n-best alternative may become: they only tell or adjust something and
# store nothing. A reminder, a memory or a time setting is never guessed.
CODE_SAFE_KINDS = frozenset((
    "time", "date", "day", "date_tomorrow", "world_time", "time_difference", "battery", "name", "age",
    "volume_up", "volume_down", "skills", "skills_more", "skills_group", "voice_help", "self_alive", "self_aware",
    "self_feel", "self_what", "dream",
))
# Brightness steps and levels (mind/device.py classify) adjust only the screen.
DEVICE_SAFE_KINDS = frozenset(("step", "level"))
# The n-best alternatives read for a PocketSphinx dictation that is no command.
# The right words can stand fifth ("what kind of shit" ... "what time is it",
# cable run, 25 Nov).
NBEST_LIMIT = 10

# False wakes (cable run, 25 Nov: 8 wakes in 17 minutes in a quiet room
# overnight, their scores no different from real ones; 5 went to the model with
# 2 to 19 characters). After the wake phrase said alone, a dictation of fewer
# than SHORT_WAKE_WORDS words that holds no command is taken for the room and
# not passed on, unless it is one of SHORT_WAKE_KEPT. A one-breath request
# ("Hey Pip, thanks") is never dropped this way.
SHORT_WAKE_WORDS = 3
# What the recognisers wrote for noise or for a window cut off before the
# request, never a request in itself: the whole transcript, punctuation aside.
# Real laptop, 28 Nov 2026: Moonshine wrote "You" for 11 of 14 of the owner's
# model turns, and "So", "It's" and "Oh my" in the replays; PocketSphinx wrote
# the short words. Never passed on, after any wake or in the follow-up window.
NOISE_TRANSCRIPTS = frozenset((
    "you", "so", "it's", "its", "oh", "oh my", "uh", "um", "hmm", "mm", "ah", "of", "but", "and", "if",
    "it", "they", "the", "a",
))
NAME_ALONE = "the name alone, so still listening"
# Said with the name, and nothing else: "Hey, kip.", "Pay-pip", "A tip." (the
# owner saying the name again in the follow-up window, 28 Nov).
NAME_ONLY_FILLER = frozenset(("hey", "hi", "ok", "okay", "a", "pay", "hay", "oh"))
# Name-like words that are answers of their own (recognition.name_like takes them).
NOT_THE_NAME_ALONE = frozenset(("yep", "yes", "yeah", "yup"))
SHORT_WAKE_KEPT = frozenset(("stop", "cancel", "wait", "never mind", "nevermind", "cancel it", "stop it"))
# Its own reply is not heard as a follow-up: the microphone reads silence for
# this long after the last clause ends (five follow-up dictations opened 0.6 to
# 5 s after replies in the cable run). A follow-up must also hold at least this
# much sound clearly over the room's floor (VOICED_OVER_FLOOR_DB).
FOLLOWUP_HOLD_SECONDS = 0.4
FOLLOWUP_MIN_VOICED_SECONDS = 0.1
# No spoken apology at night (quiet hours, or these local hours without them)
# or in the dark (the brainstem's own threshold for a dark room, body.rs).
NIGHT_HOURS = (22, 7)
DARK_LUX = 5.0
# Left in the mind's directory by mind/keys.py while a key mute holds the
# speaker at 0 (keys.MUTED_MARKER): a restarted voice starts muted.
SPEAKER_MUTED_MARKER = "speaker-muted"
# A one-breath request's words may have been said up to this long before its
# dictation began (the replay of the wake search's history).
ONESHOT_LOOKBACK_SECONDS = 4.0
# The capture high-pass (recognition.HighPass) for what the recognizer hears:
# off unless BEING_MIC_HIGHPASS or being.mic_highpass= turns it on.
HIGHPASS_DEFAULT = False
# journald reads a "<N>" prefix on a line as its priority (sd-daemon(3)).
JOURNAL_PRIORITIES = ((logging.ERROR, "<3>"), (logging.WARNING, "<4>"), (logging.INFO, "<6>"))

CMDLINE = Path("/proc/cmdline")
VOICE_DEBUG_KEY = "being.voice_debug="
VOICE_DEBUG_ARG = VOICE_DEBUG_KEY + "1"

SETTINGS_FILE = Path("audio") / "voice.json"
ALSA_STATE_FILE = Path("audio") / "asound.state"
# Version 2 adds the owner's speech engine ("tts"). Version 1 files still read.
SETTINGS_VERSION = 2
READABLE_VERSIONS = (1, 2)
MAX_SETTINGS_BYTES = 1024
TTS_CHOICES = ("auto", "piper", "flite")
# Unscaled samples: the level a relative change starts from when no mixer
# reports one. Assuming less would make "louder" quieter in software volume.
FULL_LEVEL = 100
# A stored level never starts the voice quieter than this, the quietest the
# owner can ask for by voice (mind.turn.VOLUME_FLOOR): an older build kept a
# key mute's 0, and a level the volume keys stepped to may be lower still.
VOLUME_FLOOR = 10
# The volume keys' click (voice.volume with feedback: true), as a Mac makes:
# one short soft tick at the new level, well under the earcons' length.
CLICK_NAME = "click"
CLICK_SECONDS = 0.03
CLICK_HZ = 1350
CLICK_LEVEL = 0.6

LOG = logging.getLogger("being.voice")


@types.coroutine
def _give_way():
    """Let the event loop run its other tasks once, as asyncio.sleep(0) does."""
    yield


def journal_prefix(level, environ=os.environ):
    """The sd-daemon priority prefix for a record of `level` when standard
    output is the journal (systemd sets JOURNAL_STREAM), else "". Warnings
    and errors then reach the journal as such, not all at priority 6."""
    if not environ.get("JOURNAL_STREAM"):
        return ""
    for threshold, prefix in JOURNAL_PRIORITIES:
        if level >= threshold:
            return prefix
    return "<7>"


class _JournalHandler(logging.Handler):
    """One line on standard output per message, as the journal reads it."""

    def emit(self, record):
        try:
            print(journal_prefix(record.levelno) + self.format(record), flush=True)
        except Exception:
            self.handleError(record)


if not LOG.handlers:
    LOG.addHandler(_JournalHandler())
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def log(message):
    LOG.info("%s", message)


class Span:
    """Levels of a run of raw microphone frames."""

    def __init__(self):
        self.frames = 0
        self.peak = 0
        self.energy = 0.0

    def add(self, peak, rms):
        self.frames += 1
        self.peak = max(self.peak, peak)
        self.energy += rms * rms

    @property
    def seconds(self):
        return self.frames * FRAME_SECONDS

    def describe(self):
        if not self.frames:
            return "no level measured"
        return f"peak {dbfs(self.peak):.1f} dBFS, rms {dbfs(math.sqrt(self.energy / self.frames)):.1f} dBFS"


class PlaybackStuck(RuntimeError):
    """aplay ran past the clip's length: trying again would only wait again."""


def number(value, form):
    return format(value, form) if isinstance(value, (int, float)) and not isinstance(value, bool) else "unknown"


def rounded(value, digits):
    """A finite number rounded for the bus, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(value, digits)


def engine_name(engine, default="unknown"):
    name = getattr(engine, "name", None)
    return name if isinstance(name, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", name) else default


def clause_request(event):
    """A say.request the reply loop can always finish.

    Text that is not a string becomes a failed clause rather than an error in
    the reply task, and a request without a Boolean `last` ends the reply, so a
    malformed clause can never leave the being stuck speaking.
    """
    text, last = event.get("text"), event.get("last")
    clause = {"text": text if isinstance(text, str) else "", "last": last is not False}
    if not isinstance(text, str):
        clause["bad"] = True
    if type(last) is not bool:
        log("A say.request had no Boolean 'last'. Ending the reply there.")
    if isinstance(event.get("announcement"), str):
        clause["announcement"] = event["announcement"]
    if event.get("face_cue") in EMOTIONS:
        clause["face_cue"] = event["face_cue"]
    if event.get("chirp") == "bell":
        # A timer, alarm or reminder ringing: a bell before its first words.
        clause["chirp"] = "bell"
    # An opaque confirmation receipt is valid only on the empty final marker.
    # Voice echoes it after successful queue completion; it is never spoken.
    receipt = event.get("confirm_ack")
    if (clause["last"] and not clause["text"] and isinstance(receipt, str)
            and re.fullmatch(r"[0-9a-f]{32}", receipt)):
        clause["confirm_ack"] = receipt
    # A model reply's clauses carry the mind's reply token and their place in
    # it, so Voice can report which of them really played (say.played).
    reply, seq = event.get("reply"), event.get("seq")
    if isinstance(reply, str) and re.fullmatch(r"[0-9a-f]{16}", reply):
        clause["reply"] = reply
        if type(seq) is int and seq > 0 and clause["text"].strip():
            clause["seq"] = seq
    return clause


def synthesis_timeout(text):
    """Give Flite generous time on a busy CPU, but never forever."""
    return 20.0 + len(text) / 25


@asynccontextmanager
async def child(*command, **options):
    """Own the child even if interrupted while it is starting."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command, **options))
    try:
        yield await asyncio.shield(spawn)
    finally:
        process = await spawn
        if process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 0.5)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
            except ProcessLookupError:
                await process.wait()


# Owner settings ------------------------------------------------------------------

def _read_settings(mind_dir):
    """The saved settings object, None when there is none, or an error."""
    path = Path(mind_dir) / SETTINGS_FILE
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("not a private regular file")
        raw = source.read(MAX_SETTINGS_BYTES + 1)
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError("too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") not in READABLE_VERSIONS:
        raise ValueError("unknown format")
    return data


def _settings_data(mind_dir):
    if mind_dir is None:
        return {}
    try:
        return _read_settings(mind_dir)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as error:
        log(f"Voice settings unreadable ({error}). Using defaults.")
        return {}


def load_voice_settings(mind_dir):
    """Read the saved volume and chirp switch. Damage gives defaults, never an error."""
    settings = {"volume": None, "chirps": True}
    data = _settings_data(mind_dir)
    volume = data.get("volume")
    if type(volume) is int and 0 <= volume <= 100:
        settings["volume"] = volume
    if type(data.get("chirps")) is bool:
        settings["chirps"] = data["chirps"]
    return settings


def load_engine_choice(mind_dir):
    """The owner's speech engine: "auto" (the default, and for a version 1 file), "piper" or "flite"."""
    choice = _settings_data(mind_dir).get("tts")
    return choice if choice in TTS_CHOICES else "auto"


def save_voice_settings(mind_dir, *, volume, chirps, tts="auto"):
    """Atomically keep the owner's volume, chirp choice and speech engine in the mind."""
    from .storage import atomic_write

    path = Path(mind_dir) / SETTINGS_FILE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"version": SETTINGS_VERSION, "volume": volume, "chirps": chirps,
            "tts": tts if tts in TTS_CHOICES else "auto"}
    atomic_write(path, json.dumps(data) + "\n")


# The volume click ----------------------------------------------------------------

def click_samples(gain=1.0, *, rate=CHIRP_RATE):
    """The volume keys' tick as 16-bit samples: CLICK_SECONDS of a soft tone
    with a two-millisecond attack and a fast decay, so it starts and stops
    without a pop of its own."""
    count = max(1, int(CLICK_SECONDS * rate))
    attack = max(1, int(0.002 * rate))
    peak = CHIRP_PEAK * CLICK_LEVEL * max(0.0, min(1.0, gain))
    samples = array("h")
    for index in range(count):
        position = index / count
        envelope = min(1.0, index / attack) * math.exp(-6.0 * position) * 0.5 * (1 + math.cos(math.pi * position))
        value = math.sin(2 * math.pi * CLICK_HZ * index / rate) + 0.2 * math.sin(4 * math.pi * CLICK_HZ * index / rate)
        samples.append(int(max(-32767, min(32767, peak * envelope * value / 1.2))))
    return samples


def click_path(directory, gain=1.0):
    """The click at `gain` as a WAV in the chirp directory, made on first use.
    One gain is kept at a time, as the chirp bank keeps its sounds."""
    directory = Path(directory)
    level = max(0, min(100, round(gain * 100)))
    target = directory / f"{CLICK_NAME}-g{level}.wav"
    if not target.is_file():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for stale in directory.glob(f"{CLICK_NAME}-g*.wav"):
            if stale != target:
                try:
                    stale.unlink()
                except OSError:
                    pass
        write_wav(target, click_samples(level / 100))
    return target


# Mixer ---------------------------------------------------------------------------

def mixer_card(device):
    """The ALSA card behind a PCM name such as plughw:CARD=PCH,DEV=0 or plughw:0,0."""
    if match := re.search(r"CARD=([^,]+)", device):
        return match.group(1)
    if match := re.match(r"(?:plug)?hw:([^,]+)", device):
        return match.group(1)
    return "0"


class Mixer:
    """Set the playback level on the sound card and keep the card state in the mind."""

    def __init__(self, card, control="Master", state_file=None):
        self.card = card
        self.control = control
        self.state_file = state_file

    async def run(self, *command):
        try:
            async with child(*command, stdout=asyncio.subprocess.DEVNULL,
                             stderr=asyncio.subprocess.DEVNULL) as process:
                return await asyncio.wait_for(process.wait(), MIXER_TIMEOUT) == 0
        except (OSError, asyncio.TimeoutError):
            return False

    async def _read_control(self):
        try:
            async with child("amixer", "-M", "-c", self.card, "sget", self.control,
                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL) as process:
                output, _ = await asyncio.wait_for(process.communicate(), MIXER_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return b""
        return output if process.returncode == 0 else b""

    async def get(self):
        """The control's current level on the same curve `set` uses, or None."""
        output = await self._read_control()
        levels_ = [int(value) for value in re.findall(rb"\[(\d{1,3})%\]", output)]
        levels_ = [value for value in levels_ if 0 <= value <= 100]
        return max(levels_) if levels_ else None

    async def playback_muted(self):
        """Whether every playback switch on this control is off."""
        output = await self._read_control()
        states = re.findall(rb"\bPlayback[^\n]*\[(on|off)\]", output)
        return bool(states) and all(state == b"off" for state in states)

    async def set(self, level, *, switch=True):
        """Return True if the hardware mixer took the level."""
        if not await self.run("amixer", "-q", "-M", "-c", self.card, "sset", self.control, f"{level}%"):
            return False
        # Not every control has a switch, so the mute state is best effort.
        if switch:
            await self.run("amixer", "-q", "-c", self.card, "sset", self.control, "unmute" if level else "mute")
        return True

    async def store(self):
        if self.state_file is None:
            return False
        try:
            Path(self.state_file).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return False
        return await self.run("alsactl", "-f", str(self.state_file), "store", self.card)


# Engines -------------------------------------------------------------------------
#
# mind/voice_engines.py, when it is installed, chooses the speech engines from
# what is on the models partition and what this laptop measured. Without it the
# voice keeps PocketSphinx and Flite.

def load_engines():
    try:
        from . import voice_engines
    except ImportError:
        return None
    return voice_engines


def _engine_result(result):
    engine, note = result if isinstance(result, tuple) and len(result) == 2 else (result, None)
    return engine, note if isinstance(note, str) else None


def pick_tts(engines, choice, state_dir):
    """(engine, note) for the owner's choice. Anything that goes wrong means Flite."""
    if choice == "flite":
        return FliteEngine(), None
    if engines is None or not hasattr(engines, "choose_tts"):
        if choice == "piper":
            return select_engine("piper", voice_name=os.environ.get("BEING_PIPER_VOICE"))
        return FliteEngine(), None
    try:
        engine, note = _engine_result(engines.choose_tts(choice, state_dir=state_dir))
    except Exception as error:
        return FliteEngine(), f"The speech engine choice failed ({error!r}). Using Flite."
    return (engine if engine is not None else FliteEngine()), note


def pick_asr(engines, wake, state_dir):
    """(engine, note): a recognizer built around the wake spotter, or None to keep it."""
    if engines is None or not hasattr(engines, "choose_asr"):
        return None, None
    phrase = getattr(wake, "wake_phrase", None)
    try:
        return _engine_result(engines.choose_asr(wake, state_dir=state_dir,
                                                 wake_word=phrase if isinstance(phrase, str) else "Hey Pip"))
    except Exception as error:
        return None, f"The speech recognizer choice failed ({error!r}). Keeping PocketSphinx."


def asr_name(engine, wake):
    """"moonshine" for an engine wrapped around the wake spotter, else the engine's own name."""
    if engine is wake:
        return "pocketsphinx"
    if getattr(engine, "wake", None) is wake:
        return engine_name(engine, "moonshine") if isinstance(getattr(engine, "name", None), str) else "moonshine"
    return engine_name(engine, "unknown")


def command_phrases():
    """The safe command phrases from mind/app_commands.py, or a small built-in list."""
    try:
        from . import app_commands

        found = app_commands.grammar_phrases()
    except Exception:
        return list(FALLBACK_PHRASES)
    phrases = [phrase for phrase in found if isinstance(phrase, str) and phrase.strip()] \
        if isinstance(found, (list, tuple)) else []
    return phrases or list(FALLBACK_PHRASES)


class CommandGrammar:
    """When the grammar pass runs and whether its result may stand."""

    def __init__(self, phrases, name="pip", margin=GRAMMAR_MARGIN, max_seconds=GRAMMAR_MAX_SECONDS):
        self.phrases = tuple(phrases)
        self.name = name
        self.margin = margin
        self.max_seconds = max_seconds

    _shelf = None

    @classmethod
    def contexts(cls):
        """What may be on screen: nothing, the terminal shown or tucked away, a small
        app up, the workshop shelf (so "play snake" counts as running a program),
        a script being written or finished, and a yes-or-no question open."""
        from .app_commands import SMALL_APPS, AppContext, load_shelf

        if cls._shelf is None:
            try:
                cls._shelf = load_shelf()
            except Exception:
                cls._shelf = {}
        return (AppContext(), AppContext(terminal="shown"), AppContext(terminal="minimised"),
                *(AppContext(**{app: "shown"}) for app in SMALL_APPS), AppContext(shelf=dict(cls._shelf)),
                AppContext(terminal="shown", job="writing"), AppContext(terminal="shown", job="done"),
                AppContext(terminal="shown", job="running", pending="close"))

    def commands(self, text):
        """What `text` means in each ordinary on-screen context (None where nothing)."""
        from .app_commands import parse

        return [parse(text, context, self.name) for context in self.contexts()]

    def is_command(self, text):
        """True if the transcript already is a workshop command (or cannot be checked)."""
        try:
            return any(command is not None for command in self.commands(text))
        except Exception as error:
            LOG.warning("could not check the transcript against the commands: %r", error)
            return True

    def safe(self, phrase):
        """True if `phrase` is a command in some context and never more than a safe one."""
        try:
            kinds = [command.kind for command in self.commands(phrase) if command is not None]
        except Exception:
            return False
        return bool(kinds) and all(kind in GRAMMAR_SAFE_KINDS for kind in kinds)

    def code_kind(self, text):
        """The kind of the mind's own command (mind/commands.py) `text` is, or None.

        Raises when the parser itself fails, so a caller can tell that from no command."""
        from . import commands

        command = commands.parse(text, self.name)
        return command.kind if command is not None else None

    def holds_command(self, text):
        """True if the mind answers `text` in code: a workshop command in some
        context, one of its own commands, or what an everyday skill's gate
        takes ("brighter", "dimmer"). True as well when that cannot be
        checked, so a failure never drops the owner's words. A greeting
        alone does not count: "Hi." is what the room most often sounds like."""
        if self.is_command(text):
            return True
        try:
            if self.code_kind(text) is not None:
                return True
            from . import everyday

            return bool(everyday.may_match(text))
        except Exception as error:
            LOG.warning("could not check the transcript against the mind's commands: %r", error)
            return True

    def safe_meaning(self, text):
        """True if `text` is a safe workshop command, one of the mind's own
        commands that only tells or adjusts something (CODE_SAFE_KINDS), or a
        brightness step or level (mind/device.py). False when it cannot be
        checked: nothing is ever rewritten on a guess."""
        try:
            if any(command is not None for command in self.commands(text)) and self.safe(text):
                return True
            if self.code_kind(text) in CODE_SAFE_KINDS:
                return True
            from . import device

            found = device.classify(text)
            return isinstance(found, tuple) and bool(found) and found[0] in DEVICE_SAFE_KINDS
        except Exception as error:
            LOG.warning("could not check a transcript against the commands: %r", error)
            return False

    def without_remnant(self, text):
        """(command, words left out) when a one-breath transcript that is no
        command becomes a safe one (safe_meaning) without its first
        REMNANT_WORDS words or fewer, else None.

        Those words are what the end of the name left ("have open a
        terminal", "shape and close the terminal", "hey pat what time is
        it"). A negation among them ("don't open the terminal") keeps the
        transcript as it is, as does a remainder that is anything but a safe
        command, or a parse that fails."""
        words = text.split()
        for count in range(1, min(REMNANT_WORDS, len(words) - 1) + 1):
            if any(word in GRAMMAR_NEGATIONS and not (word == "can't" and position == 0)
                   for position, word in enumerate(spoken_words(" ".join(words[:count])))):
                return None
            rest = " ".join(words[count:]).strip(" ,.!?;:")
            try:
                commands = self.commands(rest)
            except Exception as error:
                LOG.warning("could not check a one-breath transcript against the commands: %r", error)
                return None
            if any(command is not None for command in commands) and self.safe(rest):
                return rest, count
            if self.safe_meaning(rest):
                return rest, count
        return None

    def nbest_command(self, transcript, alternatives, readings=None):
        """(reading, rank) for the best-ranked n-best alternative that is a
        safe command (safe_meaning) when `transcript` is none, else None.
        `readings`, when given, are the alternatives as they would be taken
        (for a one-breath request, without what the name left): the checks
        below compare the alternative as heard, and the reading must be safe.

        The alternatives are the same audio read other ways, so one that is a
        command is only taken when nothing in the transcript turns a command
        around, it has about as many words as the transcript (one more or
        fewer: "what time does the shop close" never becomes "what time is
        it"), and it shares at least one word with it beyond filler."""
        heard = spoken_words(transcript)
        if any(word in GRAMMAR_NEGATIONS and not (word == "can't" and position == 0)
               for position, word in enumerate(heard)):
            return None
        readings = list(alternatives) if readings is None else list(readings)
        for rank, (alternative, reading) in enumerate(zip(alternatives, readings), 1):
            if not isinstance(alternative, str) or not isinstance(reading, str) or not reading.strip() \
                    or reading.strip() == transcript.strip():
                continue
            words = spoken_words(alternative)
            if any(word in GRAMMAR_NEGATIONS for word in spoken_words(reading)) \
                    or abs(len(words) - len(heard)) > 1:
                continue
            shared = {word for word in spoken_words(reading) if word not in GRAMMAR_FILLER_WORDS} & set(heard)
            if shared and self.safe_meaning(reading):
                return reading, rank
        return None

    def judge(self, result, transcript=None):
        """(phrase, None) when a grammar result may replace `transcript`, else (None, why)."""
        from .recognition import find_command

        if not isinstance(result, dict) or not result.get("text"):
            return None, "no command fits"
        found = find_command(result["text"], self.phrases)
        if found is None:
            return None, "not a whole command"
        phrase, extra = found
        if extra:
            return None, "words around the command"
        margin = result.get("margin")
        if not isinstance(margin, (int, float)) or not math.isfinite(margin) or margin < self.margin:
            return None, f"margin {number(margin, '.1f')} under {self.margin:g}"
        why = agreement(transcript, phrase)
        if why is not None:
            return None, why
        if not self.safe_meaning(phrase):
            return None, "not a safe command"
        return phrase, None


def spoken_words(text):
    """Lowercase words of a transcript, apostrophes kept, other punctuation dropped."""
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower()) if isinstance(text, str) else []


def edits(first, second):
    """Levenshtein distance between two words."""
    row = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        previous, row[0] = row[0], i
        for j, b in enumerate(second, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (a != b))
    return row[-1]


def near(heard, wanted):
    """`heard` is `wanted` or a slip of it: one letter in words of 4 to 7 letters, two from 8."""
    if heard == wanted:
        return True
    allowed = 2 if len(wanted) >= 8 else 1 if len(wanted) >= 4 else 0
    return allowed > 0 and edits(heard, wanted) <= allowed


def agreement(transcript, phrase):
    """None when the grammar's `phrase` only tidies `transcript`, else why not.

    The command's own words (GRAMMAR_FILLER_WORDS aside) must all be in the
    transcript, in order, each the same word or a slip of it; at most
    GRAMMAR_STRAY_WORDS other words (courtesy words aside) may be left over;
    and nothing may turn the command around ("don't open the terminal")."""
    from .recognition import COURTESY_WORDS

    if transcript is None:
        return "no transcript to compare"
    heard = spoken_words(transcript)
    for position, word in enumerate(heard):
        if word in GRAMMAR_NEGATIONS and not (word == "can't" and position == 0):
            return "the transcript turns the command around"
    used = set()
    start = 0
    for wanted in phrase.split():
        if wanted in GRAMMAR_FILLER_WORDS:
            continue
        index = next((i for i in range(start, len(heard)) if near(heard[i], wanted)), None)
        if index is None:
            return "the transcript does not hold the command's words"
        used.add(index)
        start = index + 1
    fillers = set(phrase.split()) | GRAMMAR_FILLER_WORDS | COURTESY_WORDS
    stray = [word for i, word in enumerate(heard) if i not in used and word not in fillers]
    if len(stray) > GRAMMAR_STRAY_WORDS:
        return "too many other words in the transcript"
    return None


def command_grammar(recognizer, environ=os.environ):
    """Load the second pass into a PocketSphinx recognizer; None when it is off or cannot load."""
    if environ.get("BEING_COMMAND_GRAMMAR", "1").strip() == "0":
        LOG.info("command grammar second pass: off (BEING_COMMAND_GRAMMAR=0)")
        return None
    load = getattr(recognizer, "load_command_grammar", None)
    if not callable(load):
        return None
    margin = GRAMMAR_MARGIN
    raw = environ.get("BEING_COMMAND_GRAMMAR_MARGIN", "").strip()
    if raw:
        try:
            margin = float(raw)
            if not math.isfinite(margin):
                raise ValueError(raw)
        except ValueError:
            LOG.warning("BEING_COMMAND_GRAMMAR_MARGIN=%r is not a number; using %g", raw, GRAMMAR_MARGIN)
            margin = GRAMMAR_MARGIN
    try:
        phrases = load(command_phrases())
    except Exception as error:
        LOG.warning("command grammar second pass: could not load (%r); off", error)
        return None
    if not phrases:
        LOG.info("command grammar second pass: off (the grammar or the phone loop did not load)")
        return None
    name = (getattr(recognizer, "wake_phrase", None) or "pip").split()[-1].lower()
    LOG.info("command grammar second pass: %d phrases, margin %g per frame", len(phrases), margin)
    return CommandGrammar(phrases, name=name, margin=margin)


def voice_debug(environ=os.environ, cmdline=CMDLINE, try_boot=None):
    """(on, why): transcripts go to the log with BEING_VOICE_DEBUG=1, or on a Try boot with being.voice_debug=1."""
    if environ.get("BEING_VOICE_DEBUG") == "1":
        return True, "BEING_VOICE_DEBUG"
    # As with any repeated kernel argument the last one counts, so
    # being.voice_debug=0 typed at the boot prompt undoes the entry's =1.
    if mic_channel.kernel_arg(VOICE_DEBUG_KEY, cmdline) != "1":
        return False, None
    if try_boot is None:
        from .try_mode import is_try_boot

        try_boot = is_try_boot()
    if not try_boot:
        LOG.info("%s is honoured only on a Try boot; transcript text stays out of the log", VOICE_DEBUG_ARG)
        return False, None
    return True, "the kernel command line"


def debug_environment(environ, diagnostic, connected):
    """The environment voice debugging is read from.

    pip-diag sets BEING_VOICE_DEBUG=1 for its own test audio, beside
    BEING_DIAG_MIC. When that microphone did not answer, the real one listens
    instead, so the owner's words stay out of the log: BEING_VOICE_DEBUG is
    dropped (being.voice_debug=1 on a Try boot still counts)."""
    if not diagnostic or connected or environ.get("BEING_VOICE_DEBUG") != "1":
        return environ
    LOG.info("BEING_VOICE_DEBUG was set for the diagnostic microphone, which did not answer; "
             "it does not apply to the normal microphone")
    return {key: value for key, value in environ.items() if key != "BEING_VOICE_DEBUG"}


class Voice:
    def __init__(self, recognizer, speaker, capture, playback, mic_port=None, *,
                 voice=DEFAULT_VOICE, rate=1.0, shorten_first_pause=False, tts=None,
                 settings_dir=None, settings=None, mixer=None, chirp_bank=None, chirps=False,
                 hear_sounds=False, quiet_hours=None, mic=None, channel=None, clock=None,
                 tts_choice="auto", engines=None, asr="pocketsphinx", typing_hint=None, tts_forced=False,
                 tts_override=None, transcripts=False, grammar=None, try_boot=False, highpass=None,
                 guest_latch=False):
        self.recognizer = recognizer
        self.speaker = speaker
        self.capture_command = capture
        self.mic_port = mic_port
        self.reply_ladder = try_boot and mic_channel.kernel_arg("being.face_reply_ladder=", CMDLINE) == "1"
        # On the appliance only being-ears owns capture hardware. Tests, the
        # QEMU port and pip-diag's injected RAM-only microphone retain their
        # direct sources by leaving this unset.
        self.ears_socket = os.environ.get("BEING_EARS_SOCKET") if mic_port is None else None
        self.ears_allowed = self.ears_socket is None
        self.playback = playback
        self.voice = voice
        self.rate = rate
        self.shorten_first_pause = shorten_first_pause
        self.tts = tts or FliteEngine()
        self.flite = FliteEngine()
        self.tts_choice = tts_choice if tts_choice in TTS_CHOICES else "auto"
        self.engines = engines
        self.asr = asr
        self.typing_hint = typing_hint
        self._hint = _UNKNOWN
        # The speech filter module (mind/speaker_eq.py), None when there is none, or not yet looked for.
        self._speaker_eq = _UNKNOWN
        self.speaker_eq_failures = 0
        self.tts_forced = tts_forced
        # being.tts= or BEING_TTS: piper or flite, chosen without the speed rule.
        self.tts_override = tts_override if tts_override in ("piper", "flite") else None
        # Transcript text in the log (being.voice_debug=1 on a Try boot, or BEING_VOICE_DEBUG=1).
        self.transcripts = transcripts
        # Guest mode (app.privacy from the mind): a visitor's words never reach the log.
        self._guest_latch = guest_latch
        self.privacy_known = not guest_latch
        self.guest = guest_latch
        self.grammar = grammar
        # What the mind answers in code, for the false-wake and n-best checks;
        # the command grammar's own rules even when its second pass is off.
        wake_name = (getattr(recognizer, "wake_phrase", None) or "pip")
        wake_name = wake_name.split()[-1].lower() if isinstance(wake_name, str) and wake_name.split() else "pip"
        self.meaning = grammar if grammar is not None else CommandGrammar((), name=wake_name)
        self.wake_name = getattr(self.meaning, "name", wake_name) or wake_name
        # The capture high-pass for the recognizer (recognition.HighPass), or None.
        self.highpass = highpass
        self.try_boot = try_boot
        try:
            endpoint_boot = "being.endpoint_evidence=1" in CMDLINE.read_text(encoding="utf-8").split()
        except OSError:
            endpoint_boot = False
        self.endpoint_evidence = try_boot and (os.environ.get(ENDPOINT_EVIDENCE_ENV) == "1" or endpoint_boot)
        try:
            hybrid_boot = "being.hybrid_endpoint=1" in CMDLINE.read_text(encoding="utf-8").split()
        except OSError:
            hybrid_boot = False
        self.hybrid_endpoint = try_boot and (os.environ.get(HYBRID_ENDPOINT_ENV) == "1" or hybrid_boot)
        # An enabled research arm always emits its content-free endpoint
        # receipt; it must not be possible to collect altered finals without
        # knowing whether and why the six-frame tail was used.
        self.endpoint_evidence = self.endpoint_evidence or self.hybrid_endpoint
        self.configure_endpoint(recognizer)
        self.startup_started = process_started_at()
        self._startup_capture_logged = False
        self.mind_ready = asyncio.Event()
        # One measurement at a time: the Try measurement and the refresh may fall due together.
        self.measuring = asyncio.Lock()
        self.try_task = None
        self.asr_task = None
        self.settings_dir = settings_dir
        settings = settings or {"volume": None, "chirps": True}
        self.volume = settings["volume"]
        # The level kept in the settings file: a key mute (voice.volume with
        # persist: false) changes self.volume for now and never this.
        self.kept_volume = self.volume
        # How the level took effect ("hardware" or "software"), for reports.
        self.volume_method = None
        self.chirp_choice = settings["chirps"]
        self.chirps_on = chirps
        self.mixer = mixer
        self.chirp_bank = chirp_bank
        self.hear_sounds = hear_sounds
        self.quiet_hours = quiet_hours
        self.gain = 1.0
        self.mic = mic if mic is not None else MicLevel()
        # A ChannelPicker for a two-channel capture; None reads mono frames.
        self.channel = channel if mic_port is None else None
        # True while a failed two-channel capture is being retried with one channel.
        self.mono_trial = False
        self.frames_read = 0
        self.silent_failures = 0
        self.capture_process = None
        # Microphone frames dropped and not yet logged, and when drops were last logged.
        self.drops_pending = 0
        self.drops_reported_at = -math.inf
        self._clock = clock
        self.work_dir = RUN_DIR if os.access(RUN_DIR, os.W_OK) else None
        self.bus = Bus("voice", SUBSCRIPTIONS, urgent=self.on_event_received)
        self.bus.on_disconnect = self.connection_lost
        self.tasks = None
        self.state = "waking"
        self.muted = True
        self.lid_open = False
        self.lid_closed_seen = False
        self.lid_seq = None
        self._latest_lid = None
        self.capture = None
        self.reply = None
        self.chirp_task = None
        self.install_audio = None
        self.install_playing = False
        self.purr_task = None
        self.apology = None
        self.engine_task = None
        self.refresh_task = None
        # Recognizers replaced and already closed.
        self.retired = []
        self._synthesis_pool = None
        self._stock_wavs = WavCache()
        self._stock_requests = self._stock_hits = 0
        self._stock_bank_hits = 0
        self._stock_bank_job = None
        self._stock_bank_engine = self._stock_bank_rate = None
        self._stock_bank_closed = False
        self._night_wavs = WavCache()
        self._night_policy = DreamPolicy(enabled=True, window="closed")
        self._night_request = self._night_task = None
        self._night_expires = 0.0
        self._night_expiry = None
        self.clauses = asyncio.Queue()
        self.clock = None
        self.dictation_until = 0.0
        self.followup = False
        self.deaf_until = 0.0
        # When its own earcons and apologies played, on the voice's clock:
        # [start, end] each (end infinite while playing), so a frame decoded
        # late is silenced by when it was captured (CaptureQueue.taken_at).
        self.deaf_windows = []
        self.last_sound = -math.inf
        self.reply_failures = 0
        self._prompted = False
        self._oneshot_wake = False
        # A one-breath wake was sent and the brainstem has not answered it yet.
        self.wake_pending = False
        self.state_seen = asyncio.Event()
        self._start_sent = False
        self._bell_first = False  # the reply being spoken opens with the ring bell
        self._done_sent = False
        # Playback receipts (say.played, mind/played.py). Reply tokens this
        # voice accepted and has not yet ended with a final receipt; the
        # token and place of the clause synthesized into each WAV; and the
        # clause now playing, reported once at its first audio.
        self._open_replies = {}
        self._clause_marks = {}
        self._now_playing = None
        self._in_speech = False
        # A wake phrase was heard and nothing usable has been said since.
        self.expecting = False
        # The wake phrase was heard and no apology has been spoken since.
        self.apology_owed = False
        # The wake phrase was heard alone (not in one breath with words) and
        # nothing it heard since was passed on: short room sounds are dropped.
        self.plain_wake = False
        # hear.missed went out since the last wake (or follow-up window): at most once.
        self.missed_sent = False
        self.plain_wake_at = None
        # The room's light (body.state lux), None when there is no sensor.
        self.lux = None
        # Capture times: the newest frame clearly over the room's floor, the
        # recognizer's speech ending (its VAD), and the current dictation's start.
        self.voice_heard_at = None
        self.speech_fell_at = None
        self.dictation_began_at = None
        # When the frame being decoded was captured (None outside capture).
        self.frame_at = None
        self.last_apology_at = -math.inf
        self.hint_at = -math.inf
        # Levels of the current dictation and of the last sound segment.
        self.dictation = None
        # The frames of the current dictation that were clearly voice (above the floor).
        self.voiced = None
        self.segment = None
        self.last_segment = None
        # (confidence, log posterior, engine) of the final being sent.
        self.final_scores = None
        # When words were last understood, and the timing of the newest ones.
        self.heard_at = None
        self.voiced_at = None
        self.pending_timing = None
        self.cost_seconds = 0.0
        self.cost_frames = 0
        self.reset_report()

    def now(self):
        return self._clock() if self._clock is not None else time.monotonic()

    async def run(self):
        await self.bus.start()
        startup_log("bus_connected", self.startup_started)
        try:
            async with asyncio.TaskGroup() as tasks:
                self.tasks = tasks
                if self.volume is not None:
                    await self.start_volume()
                self.spawn(self.level_loop())
                self.spawn(sd_notify.keep_alive())
                self.prepare_stock_bank()
                if self.engines is not None:
                    self.refresh_task = self.spawn(self.choose_engines())
                try:
                    while True:
                        event = await self.bus.recv()
                        if event["t"] == "body.mic_policy" and event.get("breach") is True:
                            log("Microphone safety latch is active; stopping Voice.")
                            raise asyncio.CancelledError
                        try:
                            await self.handle(event)
                        except (KeyError, TypeError, ValueError) as error:
                            log(f"Ignored a malformed {event.get('t')!r} event: {error!r}")
                finally:
                    self._stock_bank_closed = True
                    self.clear_prepared_speech()
                    await self.stop_install_status()
                    await self.stop_reply()
                    await self.stop_capture()
                    await self.stop_chirp()
                    await self.stop_apology()
        finally:
            await self.bus.stop()
            if self._synthesis_pool is not None:
                self._synthesis_pool.shutdown(wait=False, cancel_futures=True)

    def spawn(self, coroutine):
        if self.tasks is not None:
            return self.tasks.create_task(coroutine)
        return asyncio.create_task(coroutine)

    def disarm(self):
        """Close audio immediately, including while a queued event is handled."""
        self.lid_open = False
        self.lid_seq = None
        self._latest_lid = None
        self.expecting = False
        self.apology_owed = False
        for task in (self.capture, self.reply, self.chirp_task, self.purr_task, self.apology, self.install_audio):
            if task is not None and not task.cancelling():
                task.cancel()
        self.clear_clauses()
        # Nothing can be sent with the lid closed or the bus gone: the mind
        # settles these replies from the receipts it already has.
        self._open_replies.clear()

    def connection_lost(self):
        self.disarm()
        self.clear_prepared_speech()
        self._night_policy.reset()
        self.state = "waking"
        self.muted = True
        if self.ears_socket is not None:
            self.ears_allowed = False
        self.dictation_until = 0.0
        self._prompted = False
        # A reconnect is another privacy boundary: do not resume capture or
        # transcript logging until the retained app.privacy snapshot arrives.
        if self._guest_latch:
            self.privacy_known = False
            self.guest = True

    def on_event_received(self, event):
        topic = event["t"]
        if topic == "body.mic_policy" and event.get("breach") is True:
            self.disarm()
            return
        if topic == "voice.prepare":
            self._night_request = event
        if (topic == "voice.clear_prepared" or topic == "app.privacy"
                or (topic == "organ.down" and event.get("name") == "mind")):
            self.clear_prepared_speech()
        if topic in ("state.changed", "body.lid", "body.state"):
            self._night_policy.observe(event)
            if not self._night_policy.eligible():
                self.cancel_night_synthesis()
        if topic == "body.lid":
            if event.get("closed") is not False:
                self.disarm()
            self._latest_lid = event
        elif ((event["t"] == "voice.mute" and event.get("on") is True)
              or (event["t"] == "voice.volume" and event.get("level") == 0)
              or (event["t"] == "state.changed"
                  and (event.get("muted") is True or event.get("state") not in AT_REST))):
            task = self.install_audio
            if task is not None and not task.done() and not task.cancelling():
                task.cancel()

    async def handle(self, event):
        topic = event["t"]
        if topic == "body.lid":
            if event.get("closed") is True:
                self.lid_closed_seen = True
            if event.get("closed") is not False:
                await self.stop_install_status()
                await self.stop_capture()
                await self.stop_reply()
                await self.stop_chirp()
                await self.stop_apology()
            elif event is self._latest_lid and not self.lid_open:
                await self.stop_capture()
                await self.stop_reply()
                if event is self._latest_lid:
                    self.lid_open = True
                    self.lid_seq = event.get("seq")
                    await self.sync_capture()
        elif topic == "state.changed":
            state = event["state"]
            if not isinstance(state, str):
                raise ValueError("state must be a string")
            # Only a real transition names `prev`. A snapshot (a reconnect, or a
            # mute or health change) says where things stand and must never
            # open or renew the dictation window.
            previous = event.get("prev")
            if not isinstance(previous, str) or previous == state:
                previous = None
            before = self.state
            self.state = state
            self.muted = event.get("muted", self.muted)
            if state not in AT_REST or self.muted:
                await self.stop_install_status()
            self.learn_ready(state)
            if previous is not None:
                self.wake_pending = False
            self.state_seen.set()
            await self.follow_attention(before)
            if self.apologising and previous is not None and (
                    state in ("listening", "thinking") or (before in AT_REST and state in ATTENDING)):
                # A new attention: the owner is never talked over.
                await self.stop_apology()
            self.follow_state(previous, self.state)
            await self.sync_capture()
            if previous is not None:
                await self.state_chirp(previous, self.state)
        elif topic == "ears.policy":
            self.ears_allowed = event.get("allowed") is True
            await self.sync_capture()
        elif topic == "voice.mute":
            self.muted = event["on"]
            if self.muted:
                self.expecting = False
                self.apology_owed = False
                await self.stop_install_status()
                await self.stop_apology()
            await self.sync_capture()
            if self.muted:
                await self.stop_chirp()
        elif topic == "voice.volume":
            await self.change_volume(event)
        elif topic == "voice.chirps":
            if type(event.get("on")) is bool:
                self.chirp_choice = self.chirps_on = event["on"]
                self.save_settings()
                if not self.chirps_on:
                    await self.stop_chirp()
        elif topic == "voice.level_request":
            await self.publish_level()
        elif topic == "voice.engine":
            await self.change_engine(event.get("tts"))
        elif topic == "voice.prepare" and event is self._night_request:
            self._night_request = None
            self.start_night_synthesis(event)
        elif topic == install_status.TOPIC:
            await self.start_install_status(event)
        elif topic == "stem.chirp":
            await self.chirp(event.get("name"))
        elif topic == "mind.ready":
            self.mind_ready.set()
            # A mind that connects after this voice started never heard the
            # level it came up at.
            await self.report_volume()
        elif topic == "app.privacy":
            guest = event.get("guest")
            if type(guest) is not bool:
                return
            needs_capture_check = not self.privacy_known
            self.privacy_known = True
            if guest != self.guest:
                self.guest = guest
                if self.transcripts or LOG.isEnabledFor(logging.DEBUG):
                    LOG.info("guest mode %s: transcript text %s the log", "on" if guest else "off",
                             "stays out of" if guest else "goes back into")
            if needs_capture_check:
                await self.sync_capture()
        elif topic == "body.state":
            hour, minute = event.get("hour"), event.get("minute")
            valid = type(hour) is int and type(minute) is int and 0 <= hour < 24 and 0 <= minute < 60
            self.clock = (hour, minute) if valid else None
            lux = event.get("lux")
            self.lux = float(lux) if isinstance(lux, (int, float)) and not isinstance(lux, bool) \
                and math.isfinite(lux) and lux >= 0 else None
        elif topic == "say.stop":
            await self.stop_apology()
            await self.stop_reply()
        elif topic == "say.request" and self.lid_open and self.state in TALKING:
            clause = clause_request(event)
            if "reply" in clause:
                self._open_replies.setdefault(clause["reply"], None)
            await self.clauses.put(clause)
            if self.reply is None or self.reply.done():
                await self.stop_install_status()
                await self.stop_capture()
                await self.stop_chirp()
                await self.stop_apology()
                if self.lid_open:
                    self.reply = self.spawn(self.speak())

    def learn_ready(self, state):
        """The brainstem leaves waking for an awake state only once the mind
        said it is ready, and it tells a newly connected voice where it stands.
        A voice that joined after mind.ready went out (it is not replayed, and
        on a cold boot the voice joins about five seconds after it) learns it
        from that. Not from "asleep", and not once the lid was seen closed (a
        lid wake leaves waking without the mind): then mind.ready itself, or
        TRY_READY_PATIENCE, decides. A brainstem that is idle while a crashed
        mind restarts also reads as ready; the Try measurement still waits
        for a quiet CPU."""
        if state in READY_STATES and not self.lid_closed_seen and not self.mind_ready.is_set():
            self.mind_ready.set()
            LOG.info("the brainstem is %s, past waking, so the mind is ready", state)

    async def send_audio(self, topic, **fields):
        if self.lid_open:
            if self.lid_seq is not None:
                fields["lid_seq"] = self.lid_seq
            await self.bus.send(topic, **fields)

    # Listening -------------------------------------------------------------------

    def follow_state(self, previous, state):
        """Keep the dictation window honest (task 3.10).

        A wake opens dictation for the brainstem's whole attending period. A
        reply to something the owner said leaves FOLLOW_UP_SECONDS for a
        follow-up. A false start (`hear.cancel`) or an empty final does not
        renew the window, and nor does an unprompted announcement.
        """
        now = self.now()
        if previous is None:
            # A snapshot: keep the window as it is, or close it outside a turn.
            if state not in (*ATTENDING, *TALKING):
                self.dictation_until = 0.0
                self._prompted = False
        elif state == "attending":
            if previous in TALKING:
                self.dictation_until = now + FOLLOW_UP_SECONDS if self._prompted else now
                self.followup = True
                self._prompted = False
                # A new chance to be heard: one hear.missed again at most, and
                # the room's short sounds no longer count against a plain wake.
                self.missed_sent = False
                self.plain_wake = False
            elif previous not in ATTENDING:
                # A wake by name, key or touch from rest.
                self.dictation_until = math.inf
                self.followup = False
                if not self.expecting:
                    # A key or touch: a new chance to be heard. (A wake by
                    # name already reset this, and may have used it since.)
                    self.missed_sent = False
        elif state not in (*ATTENDING, *TALKING):
            self.dictation_until = 0.0
            self._prompted = False
            self.plain_wake = False
        if state != "thinking" and self.purr_task is not None:
            self.purr_task.cancel()
            self.purr_task = None

    async def miss_when_quiet(self):
        """Say sorry for a plain wake that nothing followed, once the room is
        quiet and while still attending (MISS_AFTER_WAKE_SECONDS)."""
        wake_at = self.plain_wake_at
        if wake_at is None or not self.expecting or self.state != "attending":
            return
        now = self.now()
        began = self.dictation_began_at
        if began is not None and began >= wake_at:
            self.plain_wake_at = None   # something was dictated: the usual path answers it
            return
        heard = self.voice_heard_at if self.voice_heard_at is not None else -math.inf
        if now - wake_at < MISS_AFTER_WAKE_SECONDS or now - max(heard, wake_at) < MISS_QUIET_SECONDS:
            return
        self.plain_wake_at = None
        self.expecting = False
        LOG.info("nothing followed the wake phrase and the room is quiet; saying so while still attending")
        await self.missed("no_speech")

    async def follow_attention(self, before):
        """Notice a wake that ended with nothing heard (the voice's own record, not `prev`)."""
        if self.state in TALKING:
            # A reply is under way, so nothing is owed for an earlier wake
            # (for instance one the brainstem ignored before an announcement).
            self.expecting = False
        elif self.expecting and before in ATTENDING and self.state not in ATTENDING:
            self.expecting = False
            if self.state in AT_REST and not self.muted:
                # The attention window closed after the wake phrase with nothing heard.
                LOG.info("the wake phrase was heard but no speech followed")
                await self.missed("no_speech")
        if self.state in AT_REST or self.state == "waking":
            self.pending_timing = None

    def dictation_open(self):
        return self.state == "listening" or (self.state == "attending" and self.now() < self.dictation_until)

    def capture_wanted(self):
        return (self.privacy_known and self.ears_allowed and self.lid_open and self.bus.connected.is_set()
                and not self.muted and self.state in HEARING and not self.install_playing)

    async def sync_capture(self):
        if not self.capture_wanted():
            await self.stop_capture()
        elif (self.capture is None or self.capture.done()) and (self.reply is None or self.reply.done()):
            self.capture = self.spawn(self.listen())

    async def stop_capture(self):
        if self.capture is not None:
            if not self.capture.cancelling():
                self.capture.cancel()
            await asyncio.gather(self.capture, return_exceptions=True)
            self.capture = None

    @property
    def capture_device(self):
        if self.mic_port is not None:
            description = getattr(self.mic_port, "description", None)
            return description if isinstance(description, str) else "the QEMU microphone port"
        command = self.capture_command
        return command[command.index("-D") + 1] if "-D" in command[:-1] else "the default device"

    @property
    def stereo(self):
        return self.channel is not None and not self.mono_trial

    def describe_capture(self):
        if self.stereo:
            return f"{RATE} Hz, {self.channel.describe()}"
        return f"{RATE} Hz mono"

    @asynccontextmanager
    async def recording(self):
        """The microphone stream: QEMU/diagnostic, the isolated ears, or
        development-host arecord (read by mic_port.PipeReader)."""
        if self.mic_port is not None:
            async with self.mic_port.recording() as stream:
                yield stream
            return
        if self.ears_socket is not None:
            reader, writer = await asyncio.open_unix_connection(self.ears_socket)
            try:
                writer.write(bytes((2 if self.channel is not None and not self.mono_trial else 1,)))
                await writer.drain()
                yield reader
            finally:
                writer.close()
                await writer.wait_closed()
            return
        self.capture_process = None
        command = self.capture_command
        if self.channel is not None and self.mono_trial:
            command = mic_channel.mono_command(command)
        read_end, write_end = os.pipe()
        reader = None
        try:
            async with child(*command, stdout=write_end) as process:
                # arecord holds the write end now; the voice's copy must go,
                # or the reader would never see the end of the stream.
                os.close(write_end)
                write_end = None
                self.capture_process = process
                stdout = getattr(process, "stdout", None)
                if stdout is not None:
                    # A stand-in that hands back a stream of its own.
                    yield stdout
                else:
                    reader, read_end = PipeReader(read_end), None
                    yield reader
        finally:
            for end in (write_end, read_end):
                if end is not None:
                    try:
                        os.close(end)
                    except OSError:
                        pass
            if reader is not None:
                reader.close()

    async def listen(self):
        """Keep the ears open through device errors, backing off between tries."""
        delay = CAPTURE_RETRY_SECONDS
        while self.capture_wanted():
            began = self.now()
            try:
                await self.capture_stream()
                return
            except Exception as error:
                if self.now() - began >= CAPTURE_HEALTHY_SECONDS:
                    delay = CAPTURE_RETRY_SECONDS
                process = self.capture_process
                status = (f" (arecord exit status {process.returncode})"
                          if process is not None and isinstance(process.returncode, int) else "")
                LOG.warning("Microphone trouble: capture failed on %s%s: %s. Trying again in %g s.",
                            self.capture_device, status, str(error) or type(error).__name__, delay)
                await self.capture_failed()
            await asyncio.sleep(delay)
            delay = min(MAX_CAPTURE_RETRY_SECONDS, delay * 2)

    async def capture_failed(self):
        if self.channel is not None and not self.frames_read:
            # A device that cannot give two channels may still give one. After a
            # few silent failures, try each in turn until one delivers sound. A
            # device that is merely late gets the first attempts with two.
            self.silent_failures += 1
            if self.silent_failures >= STEREO_ATTEMPTS:
                self.mono_trial = not self.mono_trial
                LOG.info("the next attempt asks for %s", "one channel" if self.mono_trial else "two channels")
        else:
            self.silent_failures = 0
        if self.dictation is not None:
            LOG.info("dictation ended by the capture failure, %s", self.dictation.describe())
            self.dictation = None
            # Let the brainstem leave listening now rather than at its timeout.
            try:
                await self.send_audio("hear.cancel")
            except Exception as lost:
                LOG.warning("could not report the cut-off dictation: %s", lost)
        self.segment = None
        self._in_speech = False

    def start_feeding(self, stream, frames, size):
        """Start draining `stream` into the queue `frames`: a thread for
        arecord's pipe, else a task on the loop. Returns the task, or None."""
        if isinstance(stream, PipeReader):
            stream.start(frames, size)
            return None
        return asyncio.create_task(pump(stream, frames, size, lossless=getattr(stream, "lossless", False) is True))

    def note_drops(self, frames, *, closing=False):
        """Log audio the decoder fell too far behind to hear: at most every
        DROP_REPORT_SECONDS while capturing, and once more as capture closes."""
        dropped = frames.take_dropped()
        if dropped:
            self.drops_pending += dropped
        now = self.now()
        if self.drops_pending and (closing or now - self.drops_reported_at >= DROP_REPORT_SECONDS):
            self.drops_reported_at = now
            LOG.warning("microphone audio dropped: decoding fell %.1f s behind, so the oldest %d frames "
                        "(%.1f s) were not heard; %d dropped since capture opened",
                        CAPTURE_QUEUE_SECONDS, self.drops_pending, self.drops_pending * FRAME_SECONDS,
                        frames.dropped)
            self.drops_pending = 0
        if closing and (frames.dropped or frames.deepest * FRAME_SECONDS >= BACKLOG_REPORT_SECONDS):
            LOG.info("capture closed: %d frames from the microphone, decoding fell up to %.1f s behind, "
                     "%d frames dropped",
                     frames.received, frames.deepest * FRAME_SECONDS, frames.dropped)

    async def capture_stream(self):
        self.frames_read = 0
        self._in_speech = False
        async with self.recording() as stream:
            LOG.info("capture open on %s, %s, mic gain %.2fx", self.capture_device, self.describe_capture(),
                     self.mic.gain)
            if not self._startup_capture_logged:
                self._startup_capture_logged = True
                startup_log("capture_open", self.startup_started)
            stereo = self.stereo
            size = self.channel.frame_bytes if stereo else FRAME_BYTES
            # Frames wait here for the decoder, so a slow decode never makes
            # the microphone overrun (mic_port.CaptureQueue).
            frames = CaptureQueue(round(CAPTURE_QUEUE_SECONDS / FRAME_SECONDS), clock=self.now,
                                  frame_seconds=FRAME_SECONDS)
            self.drops_pending = 0
            if self.highpass is not None:
                self.highpass.reset()
            self.recognizer.start_capture()
            feeder = None
            try:
                feeder = self.start_feeding(stream, frames, size)
                while self.lid_open and self.bus.connected.is_set():
                    try:
                        frame = await frames.get(1)
                    except asyncio.IncompleteReadError as error:
                        raise RuntimeError("microphone stream closed") from error
                    if frame is None:
                        continue
                    self.note_drops(frames)
                    self.frames_read += 1
                    if self.mono_trial and self.channel is not None:
                        LOG.warning("%s gives one channel but not two; averaging both channels from now on",
                                    self.capture_device)
                        self.channel = None
                        self.mono_trial = False
                    started = time.perf_counter()
                    # Judged by when the frame was captured: decoding may lag.
                    silenced = self.silenced_at(frames.taken_at)
                    if stereo and self.channel is not None:
                        frame = self.channel.mono(frame, measure=not silenced)
                    frame, level = self.condition(frame, silenced)
                    self.account(time.perf_counter() - started)
                    dictating = self.state in ATTENDING and self.dictation_open()
                    oneshot = self.state in RESTING or (self.state == "attending" and not dictating)
                    # Asleep, a one-breath request still wakes, as the name alone would.
                    self.wake_while_asleep(self.state == "asleep")
                    captured = frames.taken_at if frames.taken_at is not None else self.now()
                    self.frame_at = captured
                    events = self.recognizer.feed(frame, dictating, oneshot)
                    was_speech = self._in_speech
                    self._in_speech = getattr(self.recognizer, "in_speech", False) is True
                    if was_speech and not self._in_speech:
                        # The recognizer's own end of speech, by when the frame was captured.
                        self.speech_fell_at = captured
                    self.measure(level, self._in_speech, captured)
                    for topic, fields in events:
                        await self.hear(topic, fields)
                    await self.miss_when_quiet()
                    # A backlog in the queue is read without suspending, so
                    # give the bus its turn between frames: a lid close, a
                    # mute or the brainstem's answer to a wake is acted on
                    # while the backlog is decoded, not after it.
                    await _give_way()
            finally:
                if feeder is not None:
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)
                self.give_back(stream, frames)
                self.recognizer.stop_capture()
                self.note_drops(frames, closing=True)
                self.frame_at = None

    def give_back(self, stream, frames):
        """A lossless source (the diagnostic microphone) gets back what was read
        but not decoded, to be heard first when capture reopens."""
        unread = getattr(stream, "unread", None)
        if getattr(stream, "lossless", False) is not True or not callable(unread):
            return
        left = frames.drain()
        if left:
            unread(b"".join(left))
            LOG.info("capture closed with %.2f s of %s not yet decoded; it is heard when capture reopens",
                     len(left) * FRAME_SECONDS, getattr(stream, "description", "the microphone stream"))

    def wake_while_asleep(self, on):
        """Tell the recognizer whether a wake phrase that runs on should still wake."""
        if getattr(self.recognizer, "run_on_wake", None) is not on:
            try:
                self.recognizer.run_on_wake = on
            except AttributeError:
                pass

    @property
    def silenced(self):
        """Its own earcon or apology is playing, or has only just stopped."""
        return self.apologising or self.now() < self.deaf_until

    def silenced_at(self, at):
        """Was its own earcon or apology playing (or just stopped) when a frame
        captured at `at` was heard? Without a capture time, as of now."""
        if at is None:
            return self.silenced
        return any(start <= at < end for start, end in self.deaf_windows)

    def deafen(self, seconds):
        """Open a deaf window from now for `seconds` (math.inf until closed by
        setting its end). Windows older than any frame still queued are forgotten."""
        now = self.now()
        oldest = now - CAPTURE_QUEUE_SECONDS - 1.0
        self.deaf_windows = [window for window in self.deaf_windows if window[1] >= oldest]
        window = [now, now + seconds]
        self.deaf_windows.append(window)
        return window

    def condition(self, frame, silenced=None):
        """Apply the software gain, or silence its own sounds. Returns the frame and its raw levels."""
        if self.silenced if silenced is None else silenced:
            if self.highpass is not None:
                # Its own sound was cut out: the filter starts afresh after it.
                self.highpass.reset()
            return bytes(len(frame)), None
        samples = pcm(frame)
        level = levels(samples)
        self.mic.observe(*level, self._in_speech)
        gain = self.mic.gain
        if self.highpass is not None:
            # The levels above stay those of the raw frame: the floor, the
            # lift and every report mean the same with the filter on or off.
            return self.highpass.process(samples, gain), level
        return (frame if gain == 1.0 else amplify(samples, gain)), level

    def account(self, seconds):
        """Measure what the microphone path costs per frame, and log it once per start."""
        if self.cost_frames >= COST_FRAMES:
            return
        self.cost_seconds += seconds
        self.cost_frames += 1
        if self.cost_frames == COST_FRAMES:
            each = self.cost_seconds / COST_FRAMES
            LOG.info("microphone gain and channel cost %.0f us per %.0f ms frame (%.2f%% of one core) over %d frames",
                     each * 1e6, FRAME_SECONDS * 1000, 100 * each / FRAME_SECONDS, COST_FRAMES)

    def measure(self, level, in_speech, at=None):
        """Account one frame's raw levels; `at` is when it was captured (now if unknown)."""
        if level is not None:
            if in_speech:
                if self.segment is None:
                    self.segment = Span()
                self.segment.add(*level)
            floor = self.mic.floor
            voiced = (dbfs(level[1]) >= floor + VOICED_OVER_FLOOR_DB if floor is not None
                      and floor > DIGITAL_SILENCE_DBFS else in_speech)
            if voiced:
                # Kept outside dictations too: a one-breath request is spoken
                # before its dictation begins.
                self.voice_heard_at = at if at is not None else self.now()
            if self.dictation is not None:
                self.dictation.add(*level)
                if voiced:
                    self.voiced_at = at if at is not None else self.now()
                    if self.voiced is None:
                        self.voiced = Span()
                    self.voiced.add(*level)
            self.report_loudest = max(self.report_loudest, level[0])
        if not in_speech and self.segment is not None:
            self.last_segment, self.segment = self.segment, None
            self.report_sounds += 1
        if self.state not in AT_REST:
            self.reset_report()
        elif self.report_at is None:
            self.report_at = self.now() + FLOOR_FIRST_REPORT_SECONDS
        elif self.now() >= self.report_at:
            floor = self.mic.floor
            line = (f"listening for the wake phrase: noise floor {number(floor, '.1f')} dBFS, "
                    f"loudest {dbfs(self.report_loudest):.1f} dBFS, {self.report_sounds} sounds, "
                    f"mic gain {self.mic.gain:.2f}x")
            if floor is not None and floor <= DIGITAL_SILENCE_DBFS:
                line += "; the microphone delivers digital silence, so the capture mixer may be muted"
            LOG.info("%s", line)
            self.reset_report(self.now() + FLOOR_REPORT_SECONDS)

    def reset_report(self, at=None):
        self.report_at = at
        self.report_loudest = 0
        self.report_sounds = 0

    async def wake_taken(self):
        """Whether the brainstem took the one-breath wake, giving it a moment to answer.

        Waiting yields to the event loop, so a state change that arrived
        while a backlog of frames was decoded is read before the words are
        dropped."""
        if self.state in ATTENDING:
            return True
        if not self.wake_pending:
            return False
        self.wake_pending = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAKE_ANSWER_SECONDS
        while self.state not in ATTENDING:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            self.state_seen.clear()
            try:
                await asyncio.wait_for(self.state_seen.wait(), remaining)
            except asyncio.TimeoutError:
                break
        return self.state in ATTENDING

    async def hear(self, topic, fields):
        oneshot = bool(fields.get("oneshot"))
        if (oneshot and topic in ("hear.partial", "hear.final", "hear.cancel") and self.state not in ATTENDING
                and (topic == "hear.partial" or not await self.wake_taken())):
            # The brainstem did not take the one-breath wake (muted, busy or
            # asleep), so the words are not addressed to it and leave no trace
            # (voice debugging says only that it happened).
            if topic != "hear.partial":
                if self.transcripts:
                    LOG.info("a one-breath request ended while %s: the brainstem did not take its wake, "
                             "so it was not passed on", self.state)
                self.dictation = None
                self.expecting = False
            return
        if topic == "hear.sound":
            now = self.now()
            if not self.hear_sounds or self.state not in RESTING or now - self.last_sound < SOUND_INTERVAL:
                return
            self.last_sound = now
        elif topic == "hear.wake":
            run_on = bool(fields.pop("run_on", False))
            if oneshot and hasattr(self.recognizer, "window_dump_enabled"):
                # Voice debugging only, and never with a guest here: keep the
                # one-breath window for the Device lane (asr_sherpa._dump).
                self.recognizer.window_dump_enabled = self.text_logged()
            segment = self.last_segment or self.segment or Span()
            LOG.info("heard the wake phrase%s%s: score %s, %s, mic gain %.2fx", " in one breath" if oneshot else "",
                     f" while {self.state}" if self.state in TALKING else "",
                     number(getattr(self.recognizer, "wake_score", None), "d"), segment.describe(), self.mic.gain)
            if run_on:
                LOG.info("the wake phrase ran on into more words while %s; waking without them, "
                         "so they need saying again", self.state)
            # The brainstem ignores a wake during its turn, so nothing is
            # expected and no apology is owed for one heard then.
            if self.state not in TALKING:
                self.dictation_until = math.inf
                self.followup = False
                self._oneshot_wake = oneshot
                self.wake_pending = oneshot and self.state not in ATTENDING
                self.expecting = True
                self.apology_owed = True
                self.pending_timing = None
                self.plain_wake = not oneshot
                self.missed_sent = False
                self.plain_wake_at = None if oneshot else self.now()
        elif topic == "hear.speech_start":
            self.dictation = Span()
            self.voiced = None
            self.voiced_at = None
            self.speech_fell_at = None
            self.dictation_began_at = self.frame_at if self.frame_at is not None else self.now()
            LOG.info("dictation started while %s", self.state)
        elif topic == "hear.partial":
            text = fields.get("text", "")
            LOG.info("partial transcript: %d characters", len(text) if isinstance(text, str) else 0)
            if LOG.isEnabledFor(logging.DEBUG) and not self.private_audio:
                LOG.debug("partial transcript text: %r", text)
        elif topic == "hear.final":
            if not oneshot:
                self.without_address(fields)
            await self.second_pass(fields)
            alone = self.not_a_request(fields)
            noise = self.room_noise(fields, oneshot) or alone
            if noise is not None:
                # Not passed on: the brainstem hears that the dictation ended
                # without words, and nothing is asked again for it.
                LOG.info("%s; not passed on", noise)
                await self.send_audio("hear.cancel")
                await self.dictation_ended("hear.cancel", {}, noise=True)
                if alone == NAME_ALONE:
                    self.heard_the_name()
                return
            fields["speaker"] = self.speaker
            if self.followup and not oneshot:
                # Heard without its name, in the window after a reply.
                fields["followup"] = True
            if fields.get("text"):
                self._prompted = True
                self.plain_wake = False
                if self.reply_ladder and not self.private_audio:
                    now = self.now()
                    end = self.speech_ended_at(now, oneshot)
                    if end >= 0 and 0 < now - end <= 3.0:
                        fields["speech_end_mono_ms"] = int(end * 1000)
        await self.send_audio(topic, **fields)
        if topic in ("hear.final", "hear.cancel"):
            await self.dictation_ended(topic, fields)

    def without_address(self, fields):
        """A dictation heard without the wake phrase in the same breath may
        still begin with "Hey Pip": that goes (recognition.strip_address)."""
        text = fields.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        rest = strip_address(text, self.wake_name)
        if rest == text:
            rest = self.without_misheard_name(text)
        if rest != text:
            LOG.info("dictation began with the name; it is left out of the %d-word transcript",
                     len(spoken_words(rest)))
            fields["text"] = rest

    def without_misheard_name(self, text):
        """`text` without a misheard name and a greeting word or two at its start
        ("A tip open a terminal", "Pay-pip what time is it"), else `text`.
        Stricter than the name alone: the name word must be one the name
        leaves (recognition.name_remnant) or be set apart by a comma, so "hey
        bob is here" stays."""
        from .recognition import name_remnant

        words = text.split()
        for cut in (1, 2, 3):
            if cut >= len(words):
                break
            head = spoken_words(" ".join(words[:cut]))
            rest = [word for word in head if word not in NAME_ONLY_FILLER]
            if len(rest) != 1 or not self.name_alone(rest):
                continue
            word = rest[0]
            if not (word == self.wake_name or name_remnant(word, self.wake_name) or word in ("tip", "dip")
                    or words[cut - 1].endswith(",")):
                continue
            remainder = " ".join(words[cut:]).lstrip(" ,.!:;-")
            if spoken_words(remainder):
                return remainder
        return text

    def name_alone(self, rest):
        """True if `rest`, the words left once greeting words are dropped, is the name or a mishearing of it."""
        from .recognition import name_remnant

        # Only what the name itself turns into (pip, pep, kip, bip; "tip" and
        # "dip" heard on the laptop), not every name-like word: "Pop.",
        # "Peep." or "Pit." may be an answer (Integration's review, 28 Nov).
        return len(rest) == 1 and rest[0] not in NOT_THE_NAME_ALONE and (
            rest[0] == self.wake_name or name_remnant(rest[0], self.wake_name)
            or (self.wake_name == "pip" and rest[0] in ("tip", "dip")))

    def not_a_request(self, fields):
        """Why a final is noise or the name alone rather than a request, or None.

        A transcript that is all a known noise reading (NOISE_TRANSCRIPTS), or
        only the name with a greeting word or two ("Hey, kip."), is never passed
        on: the dictation ends without words and the being keeps listening.
        Text-free."""
        text = fields.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        words = spoken_words(text)
        if not words:
            return None
        if " ".join(words) in NOISE_TRANSCRIPTS:
            return f"a transcript of {len(words)} word{'' if len(words) == 1 else 's'} that the recogniser writes for noise"
        if self.name_alone([word for word in words if word not in NAME_ONLY_FILLER]):
            return NAME_ALONE
        return None

    def heard_the_name(self):
        """The name was said alone (in the follow-up window, or again after a
        wake): what comes next is addressed, as after a plain wake. The
        dictation stays open for it, past the follow-up window."""
        self.followup = False
        self.dictation_until = math.inf
        self.expecting = True
        self.apology_owed = True
        self.plain_wake = True
        self.missed_sent = False
        self.plain_wake_at = self.now()
        LOG.info("the name alone: listening for the request")

    def room_noise(self, fields, oneshot):
        """Why a final looks like the room rather than the owner, or None.

        After the wake phrase said alone, fewer than SHORT_WAKE_WORDS words
        that hold no command (CommandGrammar.holds_command) are the room
        (false wakes overnight sent "Oh." and the like to the model). In the
        follow-up window after a reply, a final with less than
        FOLLOWUP_MIN_VOICED_SECONDS of sound clearly over the floor is too.
        A one-breath request is never judged here. Text-free."""
        text = fields.get("text")
        if oneshot or not isinstance(text, str) or not text.strip():
            return None
        if self.followup:
            floor = self.mic.floor
            heard = self.voiced.seconds if self.voiced is not None else 0.0
            if floor is not None and floor > DIGITAL_SILENCE_DBFS and heard < FOLLOWUP_MIN_VOICED_SECONDS:
                return (f"a follow-up of {len(spoken_words(text))} words had {heard:.2f} s of sound over the "
                        f"room's floor, so it was taken for the room or the reply's own tail")
            return None
        if not self.plain_wake:
            return None
        words = spoken_words(text)
        if len(words) >= SHORT_WAKE_WORDS or " ".join(words) in SHORT_WAKE_KEPT:
            return None
        if self.meaning.holds_command(text):
            return None
        return (f"after the wake phrase alone, {len(words)} word{'' if len(words) == 1 else 's'} with no "
                f"command were taken for the room")

    @property
    def private_audio(self):
        return self.guest or (self._guest_latch and guest_active())

    def text_logged(self):
        """Transcript text goes into the log: voice debugging is on and no guest is here."""
        return self.transcripts and not self.private_audio

    # The command grammar --------------------------------------------------------

    def read_scores(self):
        """(confidence, log posterior) of the search that just ended, as the recognizer reports them."""
        def finite(value):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            return float(value) if ok else None

        try:
            scores = getattr(self.recognizer, "scores", None)
            found = scores() if callable(scores) else None
            if isinstance(found, tuple) and len(found) == 2:
                return finite(found[0]), finite(found[1])
            confidence = getattr(self.recognizer, "confidence", None)
            return (finite(confidence()) if callable(confidence) else None), None
        except Exception as error:
            LOG.warning("could not read the transcript's confidence: %r", error)
            return None, None

    async def second_pass(self, fields):
        """Read a dictation that is not a command again, with the command grammar.

        The open-vocabulary scores are kept first: the second search replaces
        them in the decoder. A grammar result replaces the text only when
        CommandGrammar.judge accepts it; the log says which pass the words came from."""
        confidence, log_posterior = self.read_scores()
        self.final_scores = (confidence, log_posterior, self.asr)
        text = fields.get("text")
        grammar = self.grammar
        nbest = getattr(self.recognizer, "nbest", None)
        debug = LOG.isEnabledFor(logging.DEBUG) and not self.private_audio
        if grammar is None or not isinstance(text, str) or not text.strip():
            if debug and callable(nbest):
                try:
                    LOG.debug("n-best transcripts: %r", nbest())
                except Exception as error:
                    LOG.debug("no n-best transcripts: %r", error)
            return
        if grammar.holds_command(text):
            return
        oneshot = bool(fields.get("oneshot"))
        if oneshot:
            found = grammar.without_remnant(text)
            if found is not None:
                rest, dropped = found
                LOG.info("one-breath transcript: left out %d leading word%s that the name left behind; "
                         "the rest is a command", dropped, "" if dropped == 1 else "s")
                fields["text"] = rest
                return
        if self.short_command(fields, text):
            return
        audio = self.recognizer.utterance_audio() if callable(getattr(self.recognizer, "utterance_audio", None)) \
            else b""
        seconds = len(audio) / (2 * RATE) if isinstance(audio, (bytes, bytearray)) else 0.0
        if not seconds or seconds > grammar.max_seconds:
            return
        # The other readings first: the grammar search below replaces the lattice.
        if callable(nbest) and await self.nbest_rescue(fields, text, nbest, oneshot, debug):
            return
        decode = getattr(self.recognizer, "decode_command", None)
        if not callable(decode):
            return
        result = await self.decode_off_loop(decode, bytes(audio))
        phrase, why = grammar.judge(result, text)
        margin = number(result.get("margin") if isinstance(result, dict) else None, ".1f")
        if phrase is None:
            LOG.info("command grammar second pass: no match (%s); keeping the open-vocabulary transcript", why)
            if self.text_logged() and isinstance(result, dict) and result.get("text"):
                LOG.info("command grammar heard %s (margin %s)", json.dumps(result["text"]), margin)
            return
        LOG.info("command grammar second pass: matched a command of %d words (margin %s per frame), "
                 "replacing the open-vocabulary transcript", len(phrase.split()), margin)
        fields["text"] = phrase
        posterior = result.get("log_posterior")
        self.final_scores = (math.exp(posterior) if isinstance(posterior, float) else None, posterior,
                             f"{self.asr} grammar")

    def short_command(self, fields, text):
        """Read a one- or two-word transcript that is no command as "brighter"
        or "dimmer" when it sounds like one (mind/short_commands.py; Moonshine
        tiny mishears these said alone). Never in the follow-up window, where
        the reply's own tail is heard. True when it did."""
        if self.followup:
            return False
        try:
            from .short_commands import device_word

            word = device_word(text)
        except Exception as error:
            LOG.warning("could not check a short transcript against the brightness words: %r", error)
            return False
        if word is None:
            return False
        LOG.info("short command: a transcript of %d words sounds like a brightness step; it replaces the transcript",
                 len(spoken_words(text)))
        fields["text"] = word
        self.final_scores = (None, None, f"{self.asr} short command")
        return True

    async def nbest_rescue(self, fields, text, nbest, oneshot, debug=False):
        """Take an n-best alternative that is a safe command (CommandGrammar.
        nbest_command) for a short dictation that is none. True when it did.

        A one-breath alternative first loses what the name left, as the final
        itself did (recognition.strip_name_remnant, then without_remnant)."""
        from .recognition import strip_name_remnant

        found = await self.decode_off_loop(lambda _: nbest(NBEST_LIMIT), b"", what="the n-best transcripts")
        alternatives = [item for item in found if isinstance(item, str)] if isinstance(found, list) else []
        if debug and not self.private_audio:
            LOG.debug("n-best transcripts: %r", alternatives)
        readings = alternatives
        if oneshot:
            readings = []
            for alternative in alternatives:
                alternative = strip_name_remnant(alternative, self.wake_name)
                rest = self.grammar.without_remnant(alternative)
                readings.append(rest[0] if rest is not None else alternative)
        choice = self.grammar.nbest_command(text, alternatives[1:], readings[1:])
        if choice is None:
            return False
        alternative, rank = choice
        LOG.info("n-best rescue: alternative %d of %d is a command of %d words; it replaces the transcript",
                 rank + 1, len(alternatives), len(spoken_words(alternative)))
        fields["text"] = alternative.strip()
        self.final_scores = (None, None, f"{self.asr} n-best")
        return True

    async def decode_off_loop(self, decode, audio, what="command grammar second pass"):
        """Run the grammar decode on a worker thread, but never let the decoder be
        touched again before it ends: a cancelled capture waits for it."""
        job = asyncio.ensure_future(asyncio.to_thread(decode, audio))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await asyncio.wait({job})
            raise
        except Exception as error:
            LOG.warning("%s failed: %r", "command grammar second pass" if what == "command grammar second pass"
                        else f"reading {what}", error)
            return None

    def speech_ended_at(self, now, oneshot=False):
        """When the owner's words ended, by capture time: the newest frame
        clearly over the floor since the dictation began (for a one-breath
        request, since its words began before that), else the recognizer's
        own end of speech, else now. A one-breath request can be over before
        its dictation starts, so the dictation's own frames alone gave 0.0 s."""
        began = self.dictation_began_at
        earliest = None if began is None else began - (ONESHOT_LOOKBACK_SECONDS if oneshot else 0.0)
        for at in (self.voiced_at, self.voice_heard_at, self.speech_fell_at):
            if at is not None and at <= now and (earliest is None or at >= earliest):
                return at
        return now

    async def dictation_ended(self, topic, fields, noise=False):
        text = fields.get("text", "") if topic == "hear.final" else ""
        text = text if isinstance(text, str) else ""
        dictation, self.dictation = self.dictation or Span(), None
        voiced, self.voiced = self.voiced or Span(), None
        scores, self.final_scores = self.final_scores, None
        now = self.now()
        if self.endpoint_evidence:
            owner_end = self.speech_ended_at(now, bool(fields.get("oneshot")))
            endpoint_end = self.speech_fell_at
            began = self.dictation_began_at
            record = {
                "engine": self.asr,
                "hybrid_endpoint": bool(self.hybrid_endpoint and self.asr == "pocketsphinx"),
                "outcome": "room" if noise else "final" if text.strip() else "missed",
                "oneshot": bool(fields.get("oneshot")),
                "dictation_s": round(dictation.seconds, 3),
                "voiced_s": round(voiced.seconds, 3),
                "last_voice_to_final_s": round(max(0.0, now - owner_end), 3),
                "endpoint_to_final_s": (round(max(0.0, now - endpoint_end), 3)
                                        if endpoint_end is not None and endpoint_end <= now else None),
                "speech_span_s": (round(max(0.0, owner_end - began), 3)
                                  if began is not None and began <= owner_end else None),
            }
            extension = getattr(self.recognizer, "endpoint_extension_frames", 0)
            if type(extension) is int and extension > 0:
                record["hybrid_tail_frames"] = extension
                record["hybrid_tail_reason"] = getattr(
                    self.recognizer, "endpoint_extension_reason", None)
            LOG.info("endpoint evidence %s", json.dumps(record, sort_keys=True, separators=(",", ":")))
        if noise:
            LOG.info("dictation ended after %.1f s, taken for the room: %s; voiced %.1f s",
                     dictation.seconds, dictation.describe(), voiced.seconds)
            # Nothing is owed for room noise: no apology, no hear.missed.
            self.expecting = False
            self.apology_owed = False
            self.voiced_at = None
            return
        if text.strip():
            confidence, log_posterior, engine = scores if scores is not None else (*self.read_scores(), self.asr)
            LOG.info("dictation ended after %.1f s: final transcript of %d characters, confidence %s, "
                     "log-posterior %s, %s; voiced %.1f s, %s",
                     dictation.seconds, len(text), number(confidence, ".2f"), number(log_posterior, ".2f"),
                     dictation.describe(), voiced.seconds, voiced.describe())
            if self.text_logged():
                LOG.info("heard: %s (engine %s, confidence %s, log-posterior %s)", json.dumps(text), engine,
                         number(confidence, ".2f"), number(log_posterior, ".2f"))
            if LOG.isEnabledFor(logging.DEBUG) and not self.private_audio:
                LOG.debug("final transcript text: %r", text)
            self.expecting = False
            # This wake was answered with words, so nothing is owed for it. A
            # cough in the follow-up window after the reply must not be
            # apologised for (hear.missed still goes out for it).
            self.apology_owed = False
            self.heard_at = now
            self.pending_timing = (self.speech_ended_at(now, bool(fields.get("oneshot"))), now)
        else:
            LOG.info("dictation ended after %.1f s with no words, %s", dictation.seconds, dictation.describe())
            if self.expecting or dictation.seconds >= MISSED_MIN_SECONDS:
                self.expecting = False
                LOG.info("asking to hear that again")
                await self.missed("unclear")
        self.voiced_at = None

    # Missed words ----------------------------------------------------------------

    def night_or_dark(self):
        """Why no apology may be spoken now (night or a dark room), or None."""
        if self.quiet_now():
            return "in quiet hours"
        if self.clock is not None:
            start, end = NIGHT_HOURS
            if self.clock[0] >= start or self.clock[0] < end:
                return "at night"
        if self.lux is not None and self.lux < DARK_LUX:
            return "in the dark"
        return None

    async def missed(self, reason):
        """Report a miss to the bus at most once per wake (or follow-up
        window), and say sorry aloud once per wake, never at night or in the
        dark: a false wake in an empty room must not talk to it."""
        if self.missed_sent:
            LOG.info("words were missed again since the wake phrase; asking only once")
            return
        self.missed_sent = True
        await self.send_audio("hear.missed", reason=reason)
        if not self.apology_owed:
            return
        if not self.lid_open or self.muted or self.state not in APOLOGY_IN:
            LOG.info("not saying sorry while %s%s", self.state, ", muted" if self.muted else "")
            return
        why = self.night_or_dark()
        if why is not None:
            self.apology_owed = False
            LOG.info("not saying sorry %s", why)
            return
        if (self.reply is not None and not self.reply.done()) or self.apologising or self.install_playing:
            return
        self.apology_owed = False
        if self.now() - self.last_apology_at < APOLOGY_MIN_INTERVAL_SECONDS:
            # Said once already this minute: the face shows it (hear.missed), no more words.
            LOG.info("not saying sorry again within %d s; the face shows it", APOLOGY_MIN_INTERVAL_SECONDS)
            return
        self.apology = self.spawn(self.apologise(reason))

    @property
    def apologising(self):
        return self.apology is not None and not self.apology.done()

    def hint_due(self):
        now = self.now()
        return now - self.last_apology_at <= HINT_WINDOW_SECONDS and now - self.hint_at >= HINT_INTERVAL_SECONDS

    def hint_text(self):
        """How to type instead, or None. Blocking: on an installed being it
        imports the setup module, so the voice calls it off its event loop.
        A worded hint is kept; a failure is tried again next time."""
        if self._hint is not _UNKNOWN:
            return self._hint
        hint = None
        try:
            hint = self.typing_hint() if self.typing_hint is not None else None
        except Exception as error:
            LOG.warning("could not word the typing hint: %r", error)
            return None
        self._hint = hint if isinstance(hint, str) and hint else None
        return self._hint

    def apology_line(self, hint=_UNKNOWN):
        """The apology, with how to type added on a second miss soon after the first, once a day."""
        now = self.now()
        line = APOLOGY
        if self.hint_due():
            if hint is _UNKNOWN:
                hint = self.hint_text()
            if isinstance(hint, str) and hint:
                line = f"{APOLOGY} {hint}"
                self.hint_at = now
        self.last_apology_at = now
        return line

    async def apologise(self, reason="unclear"):
        """Speak the apology through the reply's own synthesis and playback. The
        microphone is fed silence meanwhile, as for an earcon."""
        window = self.deafen(math.inf)
        try:
            if self.chirp_task is not None:
                await asyncio.gather(self.chirp_task, return_exceptions=True)
            # How to type belongs with words heard but not made out, never
            # after a wake with nothing said (coordinator, 28 Nov).
            hint = await asyncio.to_thread(self.hint_text) if reason != "no_speech" and self.hint_due() else None
            line = self.apology_line(hint)
            with tempfile.TemporaryDirectory(prefix="pip-", dir=self.work_dir) as work:
                audio = Path(work) / "sorry.wav"
                async with asyncio.timeout(synthesis_timeout(line)):
                    await self.synthesize(line, audio, False)
                await self.speech_filter(audio)
                await self.play(audio, first=False)
            LOG.info("said sorry for the missed words%s", " and how to type" if line != APOLOGY else "")
        except Exception as error:
            LOG.warning("could not say sorry on %s: %s", self.playback, error)
        finally:
            window[1] = self.deaf_until = self.now() + CHIRP_DEAF_MARGIN
            if self.lid_open:
                try:
                    await self.send_audio("say.level", v=0.0)
                except Exception:
                    pass

    async def stop_apology(self):
        task, self.apology = self.apology, None
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Levels ----------------------------------------------------------------------

    def level_fields(self):
        """What the voice measured, as numbers (and the engines' names). Never words."""
        segment = self.last_segment
        return {
            "floor_dbfs": rounded(self.mic.floor, 1),
            "speech_peak_dbfs": rounded(dbfs(segment.peak), 1) if segment is not None and segment.frames else None,
            "gain": rounded(self.mic.gain, 2),
            "heard_ago_s": rounded(self.now() - self.heard_at, 0) if self.heard_at is not None else None,
            "asr": self.asr if isinstance(self.asr, str) else "unknown",
            "tts": engine_name(self.tts),
        }

    async def publish_level(self):
        if self.bus.connected.is_set():
            await self.bus.send("voice.level", **self.level_fields())

    async def level_loop(self):
        while True:
            await asyncio.sleep(LEVEL_SECONDS)
            if self.state in AT_REST and self.lid_open:
                try:
                    await self.publish_level()
                except Exception as error:
                    LOG.warning("could not publish the microphone levels: %r", error)

    # Engines ---------------------------------------------------------------------

    async def choose_engines(self):
        """Ask voice_engines for the best engines, off the event loop, then adopt
        them. While resting, a due measurement is taken now and then (voice_engines
        measures only when the laptop is idle, on a worker thread)."""
        wake = self.recognizer
        began = time.monotonic()
        startup_log("engine_selection_started", self.startup_started)
        if not self.tts_forced:
            # A voice that will not load must not cost the better ears, so the
            # two choices fail separately. A forced engine skips the speed rule.
            before = self.tts
            try:
                await self.adopt_tts(self.wanted_tts())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("could not choose the speech engine: %r", error)
            if engine_name(self.tts) == engine_name(before):
                # use_tts names only a switch: say what was chosen all the same.
                LOG.info("speech engine: %s (%s)", engine_name(self.tts), self.wanted_tts())
        else:
            LOG.info("speech engine: %s (set by BEING_TTS_ENGINE)", engine_name(self.tts))
        startup_log("tts_ready", self.startup_started, phase_s=time.monotonic() - began)
        began = time.monotonic()
        try:
            engine, note = await asyncio.to_thread(pick_asr, self.engines, wake, self.settings_dir)
            if note:
                log(note)
            if engine is not None and engine is not wake:
                await self.adopt_recognizer(engine, wake)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning("could not choose the speech recognizer: %r", error)
        startup_log("asr_ready", self.startup_started, phase_s=time.monotonic() - began)
        if self.settings_dir is not None and self.recognizer is wake:
            # Moonshine first, soon: until it is on, Pip hears with PocketSphinx, which
            # understands far less. Its own task: waiting for it never holds up listening.
            self.asr_task = self.spawn(self.try_measurement_logged(wake, only_asr=True, after=ASR_EARLY_AFTER))
        if self.try_boot and self.settings_dir is not None:
            # A Try boot also measures the rest (the voice) a little later; the
            # measurements never overlap (refresh_engines).
            self.try_task = self.spawn(self.try_measurement_logged(wake))
        while True:
            await asyncio.sleep(ENGINE_REFRESH_SECONDS)
            if self.state not in AT_REST or self.settings_dir is None:
                continue
            try:
                # CPU pressure, not the load average (voice_engines.cpu_quiet).
                await self.refresh_engines(wake, idle=getattr(self.engines, "cpu_quiet", None))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("could not re-measure the speech engines: %r", error)

    async def try_measurement_logged(self, wake, only_asr=False, after=None):
        try:
            await self.try_measurement(wake, only_asr=only_asr, after=after)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning("could not measure the speech engines in this Try session: %r", error)

    async def try_measurement(self, wake, only_asr=False, after=None):
        """One measurement early in a Try session, which is too short for the 900 s refresh.
        With `only_asr`, a boot that is not a Try session measures Moonshine alone.

        TRY_MEASURE_AFTER seconds after the mind is ready (mind.ready, or the
        brainstem past waking; or TRY_READY_PATIENCE seconds after this voice
        started looking, when neither came), at rest, once the CPU is quiet
        (voice_engines.cpu_quiet). voice_engines measures only what is due
        (auto, nothing cached) and re-checks the quiet between runs."""
        try:
            async with asyncio.timeout(TRY_READY_PATIENCE):
                await self.mind_ready.wait()
        except TimeoutError:
            LOG.info("Try session: no mind.ready in %g s (it may have gone out before this voice started); "
                     "taking the mind as ready", TRY_READY_PATIENCE)
        await asyncio.sleep(TRY_MEASURE_AFTER if after is None else after)
        quiet = getattr(self.engines, "cpu_quiet", None)
        waited = 0.0
        while self.state not in AT_REST or (quiet is not None and not await asyncio.to_thread(quiet)):
            if waited >= TRY_MEASURE_PATIENCE:
                LOG.info("the CPU never went quiet, so the speech engines are not measured")
                return
            await asyncio.sleep(TRY_MEASURE_POLL)
            waited += TRY_MEASURE_POLL
        if only_asr:
            LOG.info("measuring the speech recognizer: Pip hears better after its first minute")
        else:
            LOG.info("Try session: measuring the speech engines that are due")
        await self.refresh_engines(wake, idle=quiet, only_asr=only_asr)

    async def refresh_engines(self, wake, idle=None, only_asr=False):
        if self.measuring.locked():
            LOG.info("a speech engine measurement is already under way; not starting another")
            return
        async with self.measuring:
            await self.measure_engines(wake, idle, only_asr=only_asr)

    async def measure_engines(self, wake, idle=None, only_asr=False):
        # The quiet check for each run, when the caller has a better one than the default.
        options = {"idle": idle} if idle is not None else {}
        refresh = getattr(self.engines, "refresh_tts", None)
        if (refresh is not None and not only_asr and not self.changing_engine() and not self.tts_forced
                and not self.tts_override):
            current, choice = self.tts, self.tts_choice
            found = await refresh(current, choice, state_dir=self.settings_dir, **options)
            if found is not None:
                engine, note = _engine_result(found)
                if note:
                    log(note)
                if engine is not None:
                    # The measurement takes seconds: a voice the owner chose
                    # meanwhile is kept, and the measured one is given back.
                    await self.use_tts(engine, choice, lambda: self.tts is current and self.tts_choice == choice
                                       and not self.changing_engine())
        refresh = getattr(self.engines, "refresh_asr", None)
        if refresh is not None and self.recognizer is wake:
            phrase = getattr(wake, "wake_phrase", None)
            found = await refresh(self.recognizer, wake, state_dir=self.settings_dir,
                                  wake_word=phrase if isinstance(phrase, str) else "Hey Pip", **options)
            if found is not None:
                engine, note = _engine_result(found)
                if note:
                    log(note)
                if engine is not None and engine is not wake:
                    await self.adopt_recognizer(engine, wake)

    def changing_engine(self):
        """A spoken voice change is under way in another task."""
        task = self.engine_task
        return task is not None and not task.done() and task is not asyncio.current_task()

    def wanted_tts(self):
        """The engine choice in force: a forced one (being.tts=, BEING_TTS), else the owner's."""
        return self.tts_override or self.tts_choice

    async def adopt_tts(self, choice):
        engine, note = await asyncio.to_thread(pick_tts, self.engines, choice, self.settings_dir)
        if note:
            log(note)
        # The owner's latest spoken choice wins over this one if it changed
        # while the voice was being chosen or loaded.
        await self.use_tts(engine, choice, lambda: self.wanted_tts() == choice and not self.changing_engine())

    async def use_tts(self, engine, choice, wanted=None):
        """Switch to `engine`. `wanted()` says whether the switch still stands;
        it is asked again after the voice loads, and so is whether another
        switch happened meanwhile. A voice not switched to is given back."""
        old = self.tts
        if engine is old:
            return
        if wanted is not None and not wanted():
            await self.drop_engine(engine, old)
            return
        warm = getattr(engine, "warm", None)
        if warm is not None and inspect.iscoroutinefunction(warm):
            # Load the voice before the first clause needs it, off the event loop.
            try:
                await warm()
            except asyncio.CancelledError:
                # The load goes on on the voice's own thread; its memory must
                # still come back, after the load ends.
                self.discard_engine(engine)
                raise
            except Exception as error:
                # A voice that cannot load is never switched to: the one in use stays.
                LOG.warning("could not load the %s voice (%r); keeping %s", engine_name(engine), error,
                            engine_name(old))
                await self.release_engine(engine)
                return
        # Never in the middle of a reply: its clauses would mix two voices
        # (the first reply of a cable boot was half Flite, half Piper).
        await self.between_replies(engine)
        if self.tts is not old or (wanted is not None and not wanted()):
            await self.drop_engine(engine, old)
            return
        if engine_name(engine) != engine_name(old):
            threads = getattr(engine, "threads", None)
            LOG.info("speech engine: %s (%s)%s", engine_name(engine), choice,
                     f", {threads} synthesis threads" if type(threads) is int and engine_name(engine) == "piper"
                     else "")
        self.tts = engine
        self.prepare_stock_bank()
        self.clear_prepared_speech()
        await self.release_engine(old)

    async def between_replies(self, engine):
        """Wait until no reply or apology is being spoken (a switch must come
        between replies). Returns at once when none is."""
        waited = False
        while True:
            busy = {task for task in (self.reply, self.apology) if task is not None and not task.done()}
            if not busy:
                break
            if not waited:
                waited = True
                LOG.info("the %s voice is ready; switching once the reply being spoken ends", engine_name(engine))
            await asyncio.wait(busy)

    async def drop_engine(self, engine, old):
        LOG.info("not switching to the %s voice: the voice was changed meanwhile; keeping %s",
                 engine_name(engine), engine_name(self.tts))
        if engine is not self.tts and engine is not old:
            await self.release_engine(engine)

    def discard_engine(self, engine):
        """Give back a voice that was never switched to, without waiting:
        its close runs on a worker thread even if this task is cancelled."""
        close = getattr(engine, "close", None)
        if isinstance(engine, FliteEngine) or not callable(close) or engine is self.tts:
            return

        def done(future):
            if not future.cancelled() and future.exception() is not None:
                LOG.warning("could not unload the %s voice: %r", engine_name(engine), future.exception())

        asyncio.get_running_loop().run_in_executor(None, close).add_done_callback(done)

    async def release_engine(self, engine):
        """Give back a resident voice's memory. A voice the reply thread may still
        be using is closed on that same thread, after its clause."""
        close = getattr(engine, "close", None)
        if isinstance(engine, FliteEngine) or not callable(close):
            return
        try:
            if self._synthesis_pool is not None:
                await asyncio.get_running_loop().run_in_executor(self._synthesis_pool, close)
            else:
                await asyncio.to_thread(close)
        except Exception as error:
            LOG.warning("could not unload the %s voice: %r", engine_name(engine), error)

    async def adopt_recognizer(self, engine, wake):
        """Swap the recognizer between utterances, never in the middle of one."""
        while self.state not in AT_REST or self.dictation is not None:
            await asyncio.sleep(1.0)
        await self.stop_capture()
        self.recognizer = engine
        self.configure_endpoint(engine)
        self.asr = asr_name(engine, wake)
        if getattr(engine, "wake", None) is not wake:
            # Not part of the new engine, so its decoder's memory goes back now.
            self.retired.append(wake)
            try:
                wake.close()
            except Exception:
                pass
        LOG.info("speech recognizer: %s", self.asr)
        await self.sync_capture()

    def configure_endpoint(self, engine):
        """Apply the Try-only arm only to an engine that owns dictation.

        Moonshine is offline-only and its wrapper exposes no partials, so its
        embedded PocketSphinx wake spotter is intentionally not configured.
        """
        configure = getattr(engine, "set_hybrid_endpoint", None)
        if callable(configure):
            configure(self.hybrid_endpoint)

    async def change_engine(self, choice):
        if choice not in TTS_CHOICES:
            log("Ignored a voice.engine without tts of auto, piper or flite.")
            return
        if self.tts_override is not None:
            # The owner's spoken choice is the latest word: it ends the boot's
            # forced engine for the rest of this process.
            LOG.info("the owner chose the %s voice; the forced %s voice no longer applies", choice,
                     self.tts_override)
            self.tts_override = None
        self.tts_choice = choice
        self.save_settings()
        if self.engine_task is not None and not self.engine_task.done():
            self.engine_task.cancel()
        self.engine_task = self.spawn(self.engine_change(choice))

    async def engine_change(self, choice):
        try:
            await self.adopt_tts(choice)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning("could not change the speech engine: %r", error)
        await self.publish_level()

    # Volume ----------------------------------------------------------------------

    async def apply_volume(self, level, *, switch=True):
        """Use the card's mixer, or scale samples when no mixer answers."""
        hardware = self.mixer is not None and await self.mixer.set(level, switch=switch)
        self.volume = level
        self.gain = 1.0 if hardware else volume_gain(level)
        self.volume_method = "hardware" if hardware else "software"
        return self.volume_method

    async def start_volume(self):
        """Apply the stored level at start and report it once the bus is up.

        Respect a key mute or an ALSA mute restored by diagnostics. Neither
        changes the saved level. An explicit voice.volume can change the
        playback switch when the owner adjusts the volume."""
        stored = self.volume
        if self.speaker_muted():
            LOG.info("the speaker was muted by a key; starting muted (the stored volume %d is kept for later)",
                     stored)
            await self.apply_volume(0)
            return
        if self.mixer is not None and await self.mixer.playback_muted():
            LOG.info("the sound card is muted; keeping the owner's saved volume %d", stored)
            self.volume = None  # a later adjustment can start from the card's current level
            self.volume_method = "hardware"
            return
        level = self.starting_volume(stored)
        await self.apply_volume(level, switch=False)
        if level != stored:
            self.save_settings()
        self.spawn(self.report_volume_when_connected())

    def speaker_muted(self):
        """True while mind/keys.py's key-mute marker is in the mind's directory."""
        if self.settings_dir is None:
            return False
        try:
            return (Path(self.settings_dir) / SPEAKER_MUTED_MARKER).exists()
        except OSError:
            return False

    async def report_volume_when_connected(self):
        await self.bus.connected.wait()
        await self.report_volume()

    async def report_volume(self):
        """Send `voice.volume_state` for the level playing now. Never a 0: the
        only silence the voice holds unsaved is a key mute, which the mind
        already knows and must not take for the owner's level after its own
        restart."""
        if self.volume is None or self.volume <= 0 or self.volume_method is None:
            return
        await self.bus.send("voice.volume_state", level=self.volume, mixer=self.volume_method)

    def starting_volume(self, level):
        """The stored level the voice starts at: never under VOLUME_FLOOR, so a
        kept 0 (an older build stored a key mute) or a key's quietest step
        cannot start Pip unheard. start_volume saves and reports a raised
        level."""
        if level < VOLUME_FLOOR:
            LOG.info("the stored volume %d is below %d; starting at %d", level, VOLUME_FLOOR, VOLUME_FLOOR)
            self.kept_volume = VOLUME_FLOOR
            return VOLUME_FLOOR
        return level

    async def change_volume(self, event):
        """`voice.volume`: a `level` from 0 to 100 or a relative `step`.
        `persist: false` (a key mute) applies it for now only: neither the
        settings file nor the sound card's stored state changes. `feedback:
        true` (the volume keys) plays the short click at the new level."""
        level, step = event.get("level"), event.get("step")
        if type(level) is int and 0 <= level <= 100:
            target = level
        elif type(step) is int and -100 <= step <= 100:
            target = max(0, min(100, await self.current_volume() + step))
        else:
            log("Ignored a volume change without a level from 0 to 100.")
            return
        if target == 0 and await self.stop_install_status():
            await self.sync_capture()
        method = await self.apply_volume(target)
        if event.get("persist") is not False:
            self.kept_volume = target
            self.save_settings()
            if method == "hardware":
                self.spawn(self.store_mixer())
        # The mind keeps the owner's level too; tell it what took effect.
        await self.bus.send("voice.volume_state", level=target, mixer=method)
        if event.get("feedback") is True and target > 0:
            await self.volume_click()

    def click_allowed(self):
        """The volume click obeys every chirp rule, and never plays while the
        microphone is open for the owner's words."""
        return self.chirp_allowed() and self.dictation is None and not self.dictation_open()

    async def volume_click(self):
        """Play the click at the gain now in use, replacing a chirp or click
        still playing (keys pressed fast give one click per press, never a
        pile). True when it started."""
        if not self.click_allowed():
            return False
        await self.stop_chirp()
        try:
            path = await asyncio.to_thread(click_path, self.chirp_bank.directory, self.gain)
        except Exception as error:
            log(f"Could not make the volume click: {error}")
            return False
        if not self.click_allowed():
            return False
        self.chirp_task = self.spawn(self.sound(path))
        return True

    async def current_volume(self):
        """The level a relative change starts from when none has been set yet."""
        if self.volume is not None:
            return self.volume
        if self.mixer is not None:
            level = await self.mixer.get()
            if level is not None:
                return level
        # No mixer reports a level, so samples play unscaled: that is full level.
        return FULL_LEVEL

    async def store_mixer(self):
        if not await self.mixer.store():
            log("Could not store the sound card state.")

    def save_settings(self):
        if self.settings_dir is None:
            return
        try:
            save_voice_settings(self.settings_dir, volume=self.kept_volume, chirps=self.chirp_choice,
                                tts=self.tts_choice)
        except OSError as error:
            log(f"Could not save voice settings: {error}")

    # Chirps ----------------------------------------------------------------------

    def quiet_now(self):
        if self.quiet_hours is None:
            return False
        try:
            return bool(self.quiet_hours(self.clock))
        except Exception:
            return True

    def chirp_allowed(self):
        return (self.chirps_on and self.chirp_bank is not None and self.lid_open and not self.muted
                and self.bus.connected.is_set() and (self.reply is None or self.reply.done())
                and not self.install_playing
                and not self.quiet_now())

    async def state_chirp(self, previous, state):
        if state == "attending" and previous in (*RESTING, "waking", "asleep"):
            # The ears-open cue. A one-breath request is still being spoken, so stay quiet.
            if not self._oneshot_wake:
                await self.chirp("ears")
            self._oneshot_wake = False
        elif state == "thinking" and previous in ATTENDING and self._prompted:
            await self.chirp("heard")
            self.purr_task = self.spawn(self.purr())
        elif state == "asleep" and previous == "drowsy":
            await self.chirp("sigh")

    async def ring_bell(self):
        """The bell before a ring's first words. The mind has already decided
        the ring may sound (quiet hours allow an alarm); the owner's chirp
        switch, the lid and mute still decide whether the bell plays."""
        if not (self.chirps_on and self.chirp_bank is not None and self.lid_open and not self.muted):
            return False
        # Never two sounds at once: a purr or an ears cue still playing stops.
        await self.stop_chirp()
        try:
            path = await asyncio.to_thread(self.chirp_bank.path, "bell", self.gain)
            if not (self.chirps_on and self.lid_open and not self.muted):
                return False  # muted, switched off or shut while the bell was made
            return await self.play_chirp(path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log(f"Could not ring the bell: {error!r}")
            return False

    async def purr(self):
        await asyncio.sleep(PURR_AFTER)
        while self.state == "thinking":
            await self.chirp("purr")
            await asyncio.sleep(PURR_REPEAT)

    async def chirp(self, name):
        if name not in CHIRPS or not self.chirp_allowed():
            return False
        await self.stop_chirp()
        try:
            path = await asyncio.to_thread(self.chirp_bank.path, name, self.gain)
        except Exception as error:
            log(f"Could not make the {name} chirp: {error}")
            return False
        if not self.chirp_allowed():
            return False
        self.chirp_task = self.spawn(self.sound(path))
        return True

    async def sound(self, path):
        try:
            return await self.play_chirp(path)
        except Exception as error:
            LOG.warning("could not play the %s chirp on %s: %s", Path(path).stem.split("-")[0], self.playback,
                        error)
            return False

    async def play_chirp(self, path, *, announce=False):
        with wave.open(str(path)) as wav:
            seconds = wav.getnframes() / wav.getframerate()
        window = self.deafen(seconds + CHIRP_DEAF_MARGIN)
        self.deaf_until = window[1]
        try:
            async with child(*playback_command(self.playback, path)) as process:
                if announce:
                    await self.send_audio("say.started")
                    self._start_sent = True
                code = await asyncio.wait_for(process.wait(), seconds + PLAYBACK_MARGIN)
        finally:
            window[1] = self.deaf_until = self.now() + CHIRP_DEAF_MARGIN
        return code == 0

    async def stop_chirp(self):
        task, self.chirp_task = self.chirp_task, None
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Installation has its own local status audio, outside the reply protocol.
    def install_status_allowed(self, event):
        issued = event.get("issued")
        sequence = event.get("lid_seq")
        now = time.monotonic()
        return (self.try_boot and self.lid_open and type(sequence) is int and sequence == self.lid_seq
                and self.bus.connected.is_set() and self.state in AT_REST and not self.muted
                and self.volume != 0 and not self.speaker_muted() and not self.quiet_now()
                and (self.reply is None or self.reply.done()) and not self.apologising
                and type(issued) in (int, float)
                and now - install_status.FRESH_FOR <= issued <= now)

    async def start_install_status(self, event):
        stage = event.get("stage")
        if not isinstance(stage, str) or stage not in install_status.LINES or not self.install_status_allowed(event):
            return
        if await self.stop_install_status():
            await self.sync_capture()
        self.install_audio = self.spawn(self.say_install_status(stage, event))

    async def say_install_status(self, stage, event):
        try:
            with tempfile.TemporaryDirectory(prefix="pip-install-", dir=self.work_dir) as work:
                audio = Path(work) / "status.wav"
                async with asyncio.timeout(install_status.FRESH_FOR):
                    await self.synthesize_command(synthesis_command(DEFAULT_VOICE, 1, install_status.LINES[stage], audio),
                                                  "flite")
                    await self.scale(audio)
                if not self.install_status_allowed(event):
                    return
                self.install_playing = True
                await self.stop_capture()
                await self.stop_chirp()
                if self.install_status_allowed(event):
                    await self.play_chirp(audio)
        except (OSError, RuntimeError, TimeoutError, EOFError, wave.Error) as error:
            LOG.debug("installer status audio failed for %s: %r", stage, error)
        finally:
            if self.install_playing:
                self.install_playing = False
                # An interrupting event resynchronizes capture after it applies
                # its new lid, mute or conversation state.
                if not asyncio.current_task().cancelling():
                    await self.sync_capture()

    async def stop_install_status(self):
        task, self.install_audio = self.install_audio, None
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return task is not None

    # Speaking --------------------------------------------------------------------

    async def stop_reply(self):
        if self.reply is not None:
            if not self.reply.cancelling():
                self.reply.cancel()
            await asyncio.gather(self.reply, return_exceptions=True)
            self.reply = None
        self.clear_clauses()
        # Every clause that played has been reported by now; say so.
        await self.settle_replies()

    def clear_clauses(self):
        while not self.clauses.empty():
            self.clauses.get_nowait()

    async def settle_replies(self, tokens=None, *, finished=None):
        """End each reply this voice accepted with one final say.played.

        It follows every clause receipt of that reply on the bus, so the mind
        then knows exactly which clauses played. `finished` reached its own
        last clause; any other stopped short (say.stop, a failure, or a reply
        whose end never came). `tokens` limits this to those replies."""
        tokens = list(self._open_replies) if tokens is None else [t for t in tokens if t in self._open_replies]
        for token in tokens:
            del self._open_replies[token]
        for token in tokens:
            try:
                await self.send_audio("say.played", reply=token, final=True, stopped=token != finished)
            except Exception as error:
                log(f"Could not report the end of a reply: {error!r}")

    async def report_played(self):
        """The clause being played has started: tell the mind, once."""
        playing, self._now_playing = self._now_playing, None
        if playing is not None:
            await self.send_audio("say.played", reply=playing[0], seq=playing[1])

    def cancel_night_synthesis(self):
        self._night_request = None
        task = self._night_task
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()

    def clear_prepared_speech(self):
        self.cancel_night_synthesis()
        self._night_wavs.clear()
        self._night_expires = 0.0
        if self._night_expiry is not None:
            self._night_expiry.cancel()
            self._night_expiry = None

    def may_prepare_speech(self, seq):
        lid = self._latest_lid or {}
        return (not self.private_audio and not self.try_boot and self._night_policy.eligible()
                and night_speech.enabled(self.settings_dir)
                and lid.get("closed") is True and type(seq) is int and lid.get("seq") == seq)

    def start_night_synthesis(self, event):
        if self._night_task is not None and not self._night_task.done():
            return
        seq, clauses = event.get("lid_seq"), event.get("clauses")
        if (not self.may_prepare_speech(seq) or not isinstance(clauses, list)
                or not 1 <= len(clauses) <= night_speech.MAX_CLAUSES
                or not all(map(night_speech.eligible_text, clauses))):
            return
        from .tts_sherpa import PiperEngine

        if type(self.tts) is PiperEngine and not self.tts.silenced:
            self._night_task = self.spawn(self.prepare_night_speech(seq, clauses, self.tts, self.rate))

    async def prepare_night_speech(self, seq, clauses, engine, rate):
        config = {
            "clauses": clauses,
            "rate": rate,
            "voice_dir": os.fspath(engine.voice_dir),
            "library": os.fspath(engine.library) if engine.library is not None else None,
            "phonemizer_library": os.fspath(engine.phonemizer_library) if engine.phonemizer_library is not None else None,
        }
        payload = json.dumps(config, ensure_ascii=False).encode()
        if len(payload) > night_speech.MAX_JOB_BYTES:
            return
        try:
            async with asyncio.timeout(night_speech.WORK_SECONDS):
                async with child(sys.executable, "-m", "mind.night_speech",
                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                 stderr=asyncio.subprocess.DEVNULL) as process:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    process.stdin.close()
                    clips = await night_speech.read_clips(process.stdout, len(clauses))
                    status = await process.wait()
            if status != 0 or not self.may_prepare_speech(seq) or self.tts is not engine:
                return
            self._night_wavs.clear(engine)
            for text, clip in zip(clauses, clips, strict=True):
                self._night_wavs.remember_bytes(engine, self.night_key(text, rate), clip)
        except (OSError, EOFError, ValueError, TimeoutError):
            return
        self._night_expires = telemetry_time() + night_speech.CACHE_SECONDS
        if self._night_expiry is not None:
            self._night_expiry.cancel()
        self._night_expiry = asyncio.get_running_loop().call_later(night_speech.CACHE_SECONDS, self.clear_prepared_speech)

    @staticmethod
    def night_key(text, rate):
        return rate, hashlib.sha256(text.encode()).digest()

    def restore_prepared_speech(self, text, audio):
        if not self._night_wavs.size or self.private_audio or getattr(self.tts, "silenced", False):
            return False
        if telemetry_time() >= self._night_expires or not night_speech.enabled(self.settings_dir):
            self.clear_prepared_speech()
            return False
        return self._night_wavs.restore(self.tts, self.night_key(text, self.rate), audio)

    async def prepare_clause(self, audio, *, first=False):
        """Synthesize the next clause. A failure skips the clause, never the reply."""
        event = await self.clauses.get()
        # Whichever way this clause is synthesized (Piper, Flite, a stock
        # clip, prepared night speech), its receipt follows the WAV.
        self._clause_marks[audio] = (event.get("reply"), event.get("seq"))
        if first and event.get("chirp") == "bell":
            self._bell_first = True
        if event.get("bad"):
            self.reply_failures += 1
            log("Skipped a clause without text.")
        text = event["text"].strip()
        if text:
            try:
                async with asyncio.timeout(synthesis_timeout(text)):
                    if self.restore_prepared_speech(text, audio):
                        await self.scale(audio)
                    elif text in STOCK_LINES:
                        await self.synthesize_stock(text, audio, first)
                    else:
                        await self.synthesize(text, audio, first)
            except Exception as error:
                self.reply_failures += 1
                LOG.warning("could not synthesize a clause of %d characters with %s: %r; skipping it",
                            len(text), engine_name(self.tts, "the speech engine"), error)
                return (False, event["last"], event.get("announcement"), None,
                        event.get("confirm_ack"))
            # Outside the synthesis limit: a slow filter must never cost the clause.
            await self.speech_filter(audio)
        return (bool(text), event["last"], event.get("announcement"),
                event.get("face_cue") if text else None, event.get("confirm_ack"))

    # The speech filter ------------------------------------------------------------

    def speaker_eq(self):
        """mind/speaker_eq.py when it is installed with process_wav() and
        enabled(), else None. Looked for once per process."""
        if self._speaker_eq is not _UNKNOWN:
            return self._speaker_eq
        module = None
        if os.environ.get(SPEAKER_EQ_ENV, "").strip() == "0":
            LOG.info("speech filter: off (%s=0)", SPEAKER_EQ_ENV)
        else:
            try:
                module = importlib.import_module(f"{__package__}.speaker_eq")
            except ImportError as error:
                if getattr(error, "name", None) not in (f"{__package__}.speaker_eq", "speaker_eq"):
                    LOG.warning("the speech filter could not load (%r); speech plays unfiltered", error)
                module = None
            except Exception as error:
                LOG.warning("the speech filter could not load (%r); speech plays unfiltered", error)
                module = None
            if module is not None and not (callable(getattr(module, "process_wav", None))
                                           and callable(getattr(module, "enabled", None))):
                LOG.warning("the speech filter has no process_wav() and enabled(); speech plays unfiltered")
                module = None
        self._speaker_eq = module
        return module

    async def speech_filter(self, audio):
        """Filter one synthesized clause for the speakers, in place, when the
        speech filter is installed and enabled. True when the filtered sound
        replaced it. Never raises (but for cancellation): a failure keeps the
        clause as it was."""
        module = self.speaker_eq()
        if module is None or self.speaker_eq_failures >= SPEAKER_EQ_FAILURES:
            return False
        audio = Path(audio)
        try:
            with wave.open(str(audio)) as wav:
                rate, seconds = wav.getframerate(), wav.getnframes() / wav.getframerate()
        except (OSError, EOFError, wave.Error, ZeroDivisionError) as error:
            LOG.warning("could not read a clause for the speech filter (%r); playing it unfiltered", error)
            return False
        try:
            # /run is a tmpfs: full, or out of inodes, the clause still plays.
            folder = Path(tempfile.mkdtemp(prefix="pip-eq-", dir=audio.parent))
        except OSError as error:
            return self.speech_filter_failed(f"no room for its output ({error!r})")
        target = folder / audio.name

        def run():
            # None: the filter is switched off now; else its own verdict.
            if not module.enabled():
                return None
            return module.process_wav(str(audio), str(target), rate=rate)

        job = asyncio.ensure_future(asyncio.to_thread(run))
        finished = False
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(job),
                                                SPEAKER_EQ_BASE_SECONDS + SPEAKER_EQ_PER_SECOND * seconds)
                finished = True
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return self.speech_filter_failed(f"it took more than "
                                                 f"{SPEAKER_EQ_BASE_SECONDS + SPEAKER_EQ_PER_SECOND * seconds:.1f} s")
            except Exception as error:
                finished = True
                return self.speech_filter_failed(repr(error))
            if result is None:
                return False
            why = self.filtered_problem(result, target, seconds)
            if why is not None:
                return self.speech_filter_failed(why)
            os.replace(target, audio)
            self.speaker_eq_failures = 0
            return True
        except Exception as error:      # OSError above all; never one that silences the reply
            return self.speech_filter_failed(repr(error))
        finally:
            if finished or job.done():
                shutil.rmtree(folder, ignore_errors=True)
            else:
                # The filter still writes there: tidy up once it ends.
                job.add_done_callback(lambda done: (done.cancelled() or done.exception(),
                                                    shutil.rmtree(folder, ignore_errors=True)))

    @staticmethod
    def filtered_problem(result, target, seconds):
        """Why the filter's output cannot be played instead of the clause, or None."""
        if result is not True:
            return "it reported a failure"
        try:
            with wave.open(str(target)) as wav:
                if wav.getsampwidth() != 2 or wav.getnframes() <= 0:
                    return "its output is not a 16-bit sound"
                length = wav.getnframes() / wav.getframerate()
        except (OSError, EOFError, wave.Error, ZeroDivisionError) as error:
            return f"its output could not be read ({error!r})"
        if abs(length - seconds) > SPEAKER_EQ_LENGTH_SLACK + 0.1 * seconds:
            return f"its output lasts {length:.2f} s, not {seconds:.2f} s"
        return None

    def speech_filter_failed(self, why):
        """Log a filter failure, and leave the filter out after too many in a row."""
        self.speaker_eq_failures += 1
        if self.speaker_eq_failures >= SPEAKER_EQ_FAILURES:
            LOG.warning("the speech filter failed %d times in a row (last: %s); speech plays unfiltered "
                        "for the rest of this session", self.speaker_eq_failures, why)
        else:
            LOG.warning("the speech filter failed (%s); playing the clause unfiltered", why)
        return False

    def stock_key(self, text, first):
        pause = first and self.shorten_first_pause and isinstance(self.tts, FliteEngine)
        return self.voice, self.rate, self.gain, bool(pause), text

    def prepare_stock_bank(self):
        """Preflight the immutable donor without holding up the next reply."""
        task = self._stock_bank_job
        if self._stock_bank_closed or (task is not None and not task.done()):
            return
        if isinstance(self.tts, FliteEngine) or not STOCK_BANK.is_dir():
            self._stock_bank_job = None
            self._stock_bank_engine = self._stock_bank_rate = None
            return
        if task is not None and self._stock_bank_engine is self.tts and self._stock_bank_rate == self.rate:
            return
        from .stock_speech_bank import installed_bank

        self._stock_bank_engine, self._stock_bank_rate = self.tts, self.rate
        self._stock_bank_job = self.spawn(asyncio.to_thread(installed_bank, self.tts, self.rate))
        self._stock_bank_job.add_done_callback(self.stock_bank_ready)

    def stock_bank_ready(self, task):
        # An engine switch during a full check starts the current preflight
        # once the old reader settles. Keep at most one full hash in flight.
        if (not task.cancelled() and self._stock_bank_job is task
                and (self._stock_bank_engine is not self.tts or self._stock_bank_rate != self.rate)):
            self.prepare_stock_bank()

    async def restore_stock_donor(self, engine, key, text, audio, first):
        self.prepare_stock_bank()
        task = self._stock_bank_job
        if (task is None or not task.done() or task.cancelled() or self._stock_bank_engine is not engine
                or self._stock_bank_rate != self.rate or (bank := task.result()) is None):
            return False
        try:
            clip = await asyncio.to_thread(bank.read, text)
            if clip is None or self.tts is not engine or self.stock_key(text, first) != key:
                return False
            Path(audio).write_bytes(clip)
            await self.scale(audio)
        except (OSError, ValueError, EOFError, wave.Error):
            return False
        if self.tts is not engine or self.stock_key(text, first) != key:
            return False
        self._stock_wavs.remember(engine, key, audio)
        return True

    async def synthesize_stock(self, text, audio, first):
        engine = self.tts
        key = self.stock_key(text, first)
        hit = self._stock_wavs.restore(engine, key, audio)
        donated = not hit and await self.restore_stock_donor(engine, key, text, audio, first)
        self._stock_requests += 1
        self._stock_hits += int(hit)
        self._stock_bank_hits += int(donated)
        if self._stock_requests % 16 == 0:
            LOG.info("stock speech cache: %d RAM and %d bank hits in %d requests",
                     self._stock_hits, self._stock_bank_hits, self._stock_requests)
        if hit or donated:
            return
        current_engine = await self.synthesize(text, audio, first)
        if current_engine is engine and self.tts is engine and self.stock_key(text, first) == key:
            self._stock_wavs.remember(engine, key, audio)

    async def synthesize(self, text, audio, first):
        """One clause into a WAV. Another engine's failure falls back to Flite for that clause."""
        engine = self.tts
        if not isinstance(engine, FliteEngine):
            try:
                if callable(getattr(engine, "synthesize", None)):
                    await self.synthesize_resident(engine, text, audio)
                else:
                    await self.synthesize_command(engine.command(self.voice, self.rate, text, audio),
                                                  engine_name(engine))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("%s could not say a clause of %d characters (%r); Flite says it instead",
                            engine_name(engine, "the speech engine"), len(text), error)
            else:
                await self.scale(audio)
                return engine
        trial = (first and self.shorten_first_pause and self.voice in PAUSE_TRIAL_VOICES
                 and isinstance(engine, FliteEngine) and len(text) <= PAUSE_TRIAL_MAX_CHARS)
        command = synthesis_command(self.voice, self.rate, text, audio, segment_times=trial)
        segments = await self.synthesize_command(command, "flite", segments=trial)
        if segments:
            shorten_leading_pause(audio, segments)
        await self.scale(audio)
        # A fallback can speak normally but cannot populate another engine's bank.
        return engine if isinstance(engine, FliteEngine) else None

    async def synthesize_command(self, command, name, *, segments=False):
        options = {"stdout": asyncio.subprocess.PIPE} if segments else {}
        output = b""
        async with child(*command, **options) as synthesis:
            if segments:
                output, _ = await synthesis.communicate()
            if status := await synthesis.wait():
                raise RuntimeError(f"{name} exit status {status}")
        return output

    async def synthesize_resident(self, engine, text, audio):
        """A loaded engine (Piper through sherpa-onnx) in one worker thread of its own."""
        if inspect.iscoroutinefunction(engine.synthesize):
            await engine.synthesize(text, audio, self.rate)
            return
        if self._synthesis_pool is None:
            self._synthesis_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-tts")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._synthesis_pool, engine.synthesize, text, Path(audio), self.rate)

    async def scale(self, audio):
        if self.gain < 0.999:
            await asyncio.to_thread(scale_wav, Path(audio), self.gain)

    async def speak(self):
        """Speak queued clauses. Whatever goes wrong, the turn is closed honestly."""
        self._done_sent = False
        try:
            await self.speak_clauses()
        except Exception as error:
            # Never leave the brainstem waiting on a reply that cannot play.
            LOG.error("The reply failed: %r", error)
            self.clear_clauses()
            self.reply = None
            self._now_playing = None
            await self.settle_replies()
            if not self._done_sent:
                try:
                    await self.send_audio("say.done", final=True, failed=True)
                except Exception as send_error:
                    log(f"Could not report the failed reply: {send_error!r}")
        # A reply handed over while the last one was still playing waits in the
        # queue behind its end: speak it now, or its token stays open unheard.
        if self.reply is None and self.lid_open and not self.clauses.empty() and self.state in TALKING:
            self.reply = self.spawn(self.speak())
        # Wake spotting resumes in thinking if the brainstem stays there (task 5.5).
        await self.sync_capture()

    async def speak_clauses(self):
        started = False
        self.reply_failures = 0
        self._start_sent = False
        self._bell_first = False
        try:
            with tempfile.TemporaryDirectory(prefix="pip-", dir="/run") as work:
                audio = Path(work) / "clause.wav"
                next_audio = Path(work) / "next.wav"
                successor = None
                seen = []  # the replies whose clauses this run took from the queue
                try:
                    ready, last, announcement, cue, confirm_ack = await self.prepare_clause(audio, first=True)
                    while True:
                        reply, seq = self._clause_marks.pop(audio, (None, None))
                        if reply is not None and reply not in seen:
                            seen.append(reply)
                        if ready:
                            if not last:
                                # One clause can be synthesized while the speaker plays the current WAV.
                                successor = asyncio.create_task(self.prepare_clause(next_audio))
                            if not started and self._bell_first:
                                self._bell_first = False
                                await self.ring_bell()
                            # play_once reports it (say.played) with its first audio.
                            self._now_playing = (reply, seq) if reply is not None and seq is not None else None
                            try:
                                await self.play(audio, first=not started, **({"cue": cue} if cue else {}))
                                started = True
                            except Exception as error:
                                self.reply_failures += 1
                                started = started or self._start_sent
                                LOG.warning("skipping a clause that could not play: %r", error)
                            finally:
                                self._now_playing = None
                        if last:
                            if not started and self.reply_failures:
                                # Nothing could be said. A short troubled sound is honest audio,
                                # and lets the brainstem leave thinking at once.
                                started = await self.play_trouble()
                            if started:
                                # Its own voice still rings in the room: the
                                # follow-up capture hears silence a moment.
                                window = self.deafen(FOLLOWUP_HOLD_SECONDS)
                                self.deaf_until = max(self.deaf_until, window[1])
                            # Release the capture gate before the brainstem returns to attending.
                            self.reply = None
                            fields = {"final": True}
                            if self.reply_failures:
                                fields["failed"] = True
                            elif started and isinstance(announcement, str) and announcement:
                                fields["announcement"] = announcement
                            if not self.reply_failures and isinstance(confirm_ack, str):
                                fields["confirm_ack"] = confirm_ack
                            # Every clause receipt of these replies is out: end them first.
                            await self.settle_replies(seen, finished=reply)
                            await self.send_audio("say.done", **fields)
                            self._done_sent = True
                            return
                        if successor is None:
                            ready, last, announcement, cue, confirm_ack = await self.prepare_clause(
                                audio, first=not started)
                        else:
                            ready, last, announcement, cue, confirm_ack = await successor
                            successor = None
                            audio, next_audio = next_audio, audio
                finally:
                    if successor is not None:
                        successor.cancel()
                        await asyncio.gather(successor, return_exceptions=True)
                    self._clause_marks.pop(audio, None)
                    self._clause_marks.pop(next_audio, None)
        finally:
            await self.send_audio("say.level", v=0.0)

    async def play_trouble(self):
        if self.chirp_bank is None or not self.lid_open:
            return False
        try:
            path = await asyncio.to_thread(self.chirp_bank.path, "trouble", self.gain)
            return await self.play_chirp(path, announce=True) or self._start_sent
        except Exception as error:
            log(f"Could not play the trouble sound either: {error!r}")
            return self._start_sent

    def first_audio(self):
        """The reply's first sound: log how long it took from the end of the owner's words."""
        pending, self.pending_timing = self.pending_timing, None
        if pending is None:
            return
        ended, final = pending
        now = self.now()
        if now - final > TIMING_MAX_SECONDS:
            return
        timing = {"eos_to_final_s": round(final - ended, 3), "final_to_audio_s": round(now - final, 3),
                  "eos_to_audio_s": round(now - ended, 3)}
        LOG.info("voice timing %s", json.dumps(timing))

    async def play(self, audio, first, cue=None):
        """Play one clause, retrying a speaker that fails at once with growing pauses."""
        delay = PLAYBACK_RETRY_FIRST
        for attempt in range(1, PLAYBACK_ATTEMPTS + 1):
            try:
                await self.play_once(audio, first and not self._start_sent, **({"cue": cue} if cue else {}))
                return
            except PlaybackStuck:
                raise
            except Exception as error:
                if attempt == PLAYBACK_ATTEMPTS:
                    raise RuntimeError(f"ALSA could not play the reply on {self.playback}: {error}") from error
                LOG.warning("playback failed on %s (attempt %d of %d): %s; retrying in %g s",
                            self.playback, attempt, PLAYBACK_ATTEMPTS, error, delay)
                await asyncio.sleep(delay)
                delay *= 2

    async def play_once(self, audio, first, cue=None):
        with wave.open(str(audio)) as wav:
            rate = wav.getframerate()
            block = max(1, round(rate * PLAYBACK_LEVEL_INTERVAL))
            length = wav.getnframes() / rate
            limit = length + PLAYBACK_MARGIN
            began = time.monotonic()
            async with child(*playback_command(self.playback, audio)) as process:
                if cue is not None and process.returncode is None:
                    # A spawned player can still reject the ALSA device.
                    # Let it report an immediate failure before using the WAV envelope.
                    await asyncio.sleep(PLAYBACK_LEVEL_INTERVAL)
                if first and not process.returncode:
                    await self.send_audio("say.started")
                    self._start_sent = True
                    self.first_audio()
                if not process.returncode:
                    # Its first audio is playing: from here the clause counts
                    # as said, even if it is stopped partway (mind/played.py).
                    await self.report_played()
                deadline = time.monotonic() + limit
                while process.returncode is None:
                    if time.monotonic() > deadline:
                        raise PlaybackStuck("ALSA playback did not finish")
                    samples = array("h", wav.readframes(block))
                    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) if samples else 0
                    level = round(min(1.0, rms / 8500), 3)
                    if cue is not None and level >= .015:
                        if self.volume != 0 and not self.speaker_muted():
                            await self.send_audio("say.level", v=level, face_cue=cue)
                        else:
                            await self.send_audio("say.level", v=level)
                        cue = None
                    else:
                        await self.send_audio("say.level", v=level)
                    await asyncio.sleep(PLAYBACK_LEVEL_INTERVAL)
                if status := await process.wait():
                    raise RuntimeError(f"aplay exit status {status}")
            self.playback_timing(length, rate, time.monotonic() - began)

    def playback_timing(self, length, rate, wall):
        """One line per clause: its length, rate and how long aplay took. Never its words."""
        LOG.info("playback %s", json.dumps({"wav_s": round(length, 3), "rate": rate, "wall_s": round(wall, 3)}))
        # Starting aplay takes a moment, so a clip may run a little longer, never shorter.
        if wall < length - PLAYBACK_TOLERANCE or wall > length + 2 * PLAYBACK_TOLERANCE:
            LOG.warning("a %.2f s clip took %.2f s to play on %s", length, wall, self.playback)


def audio_devices():
    capture = os.environ.get("BEING_ALSA_CAPTURE", "plughw:0,0")
    playback = os.environ.get("BEING_ALSA_PLAYBACK", "plughw:0,0")
    if (FW / "loopback" / "raw").exists():
        return "hw:Loopback,1,0", "plughw:Loopback,0,1"
    return capture, playback


def wake_recognizer(wake_word):
    try:
        return Recognizer(wake_word)
    except ValueError:
        factory_phrase = factory_profile(FACTORY_CONFIG).wake_word
        if factory_phrase == wake_word:
            raise
        recognizer = Recognizer(factory_phrase)
        print("Wake phrase unavailable. Using the factory phrase.", flush=True)
        return recognizer


def resolve_voice(voice):
    if voice == DEFAULT_VOICE:
        return voice
    available = available_voices() if voice in VOICES else set()
    if available is None:
        print("Could not read Flite's voice list. Keeping the selected voice.", flush=True)
    elif voice not in available:
        print(f"Voice {voice} unavailable. Using {DEFAULT_VOICE}.", flush=True)
        return DEFAULT_VOICE
    return voice


def lab_default(name, *, in_lab):
    """Sound extras are on for the real body and opt-in in the QEMU lab."""
    value = os.environ.get(name)
    if value in ("0", "1"):
        return value == "1"
    return not in_lab


def quiet_hours_check(mind_dir):
    from .policy import announcement_policy

    def quiet(clock):
        policy = announcement_policy(mind_dir)
        if policy.quiet_hours is None:
            return False
        if clock is None:
            # Quiet hours are set but the local time is unknown: stay quiet.
            return True
        return policy.is_quiet(datetime(2000, 1, 1, *clock))

    return quiet


def mic_gain(environ=os.environ):
    """The owner's software microphone gain from BEING_MIC_GAIN, clamped to a safe range."""
    raw = environ.get("BEING_MIC_GAIN", "").strip()
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        LOG.warning("BEING_MIC_GAIN=%r is not a positive number; using 1.0", raw)
        return 1.0
    clamped = min(max(value, MIN_MIC_GAIN), MAX_MIC_GAIN)
    if clamped != value:
        LOG.warning("BEING_MIC_GAIN=%s is outside %g to %g; using %g", raw, MIN_MIC_GAIN, MAX_MIC_GAIN, clamped)
    return clamped


def microphone_channel(environ=os.environ, cmdline=mic_channel.CMDLINE, *, loopback=False, port=False,
                       diagnostic=False):
    """mix, left, right or auto, from being.mic_channel= or BEING_MIC_CHANNEL."""
    choice, source = mic_channel.setting(environ, cmdline)
    if choice != "mix" and (loopback or port or diagnostic):
        LOG.info("microphone channel %s from %s does not apply to %s; using mix", choice, source,
                 "the lab's loopback body" if loopback else
                 "the diagnostic microphone" if diagnostic else "the QEMU microphone port")
        return "mix"
    LOG.info("microphone channel: %s (from %s)", choice, source)
    return choice


def capture_command(device, channel="mix"):
    # Not quiet: arecord's overrun reports reach the journal on its standard
    # error. The voice drains the pipe on a thread of its own; the half-second
    # ALSA buffer covers arecord itself waiting for a busy CPU. Frames still
    # arrive every 30 ms period, so a larger buffer adds no delay.
    return ["arecord", "-D", device, "-t", "raw", "-f", "S16_LE", "-r", str(RATE),
            "-c", str(mic_channel.capture_channels(channel)), f"--buffer-time={CAPTURE_BUFFER_US}",
            f"--period-time={CAPTURE_PERIOD_US}"]


def typing_hint():
    from .type_chat import typing_hint as hint

    return hint()


async def main():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    startup_started = process_started_at()
    startup_log("main_entered", startup_started)
    level = LOG.level
    if os.environ.get("BEING_VOICE_DEBUG") == "1":
        LOG.setLevel(logging.DEBUG)
    from .try_mode import is_try_boot

    try_boot = is_try_boot()
    mind_dir = default_mind_dir()
    profile = load_profile(FACTORY_CONFIG, mind_dir, strict=False)
    startup_log("profile_ready", startup_started)
    wake_began = time.monotonic()
    wake_task = asyncio.create_task(asyncio.to_thread(wake_recognizer, profile.wake_word))
    startup_log("wake_load_started", startup_started)
    voice = resolve_voice(profile.voice)
    engines = load_engines()
    tts_choice = load_engine_choice(mind_dir)
    # being.tts= or BEING_TTS: a forced engine, still only if its pack verifies.
    override, override_source = engines.forced("tts") if engines is not None and hasattr(engines, "forced") \
        else ("auto", None)
    override = override if override in ("piper", "flite") else None
    forced = None if override else os.environ.get("BEING_TTS_ENGINE")
    if override:
        print(f"Speech engine {override} forced by {override_source}.", flush=True)
    if forced:
        # An explicit engine in /etc/being/env wins over the automatic choice
        # of voice (the recognizer is still chosen automatically).
        tts, note = select_engine(forced, voice_name=os.environ.get("BEING_PIPER_VOICE"))
    else:
        tts, note = FliteEngine(), None
    if note:
        print(note, flush=True)
    print(f"Playback voice: {voice} at {profile.rate:g}x", flush=True)
    # The recognizer after the wake (BEING_ASR_ENGINE: auto, moonshine or
    # pocketsphinx) is chosen by voice_engines.choose_asr, which says why.
    in_lab = FW.exists()
    loopback = (FW / "loopback" / "raw").exists()
    capture, playback = audio_devices()
    port = (FW / "microphone" / "raw").exists()
    microphone = None
    diagnostic = os.environ.get(DIAG_ENV, "").strip()
    if diagnostic:
        microphone = await DiagnosticMicrophone.connect(diagnostic)
    debug_environ = debug_environment(os.environ, diagnostic, microphone is not None)
    if debug_environ is not os.environ:
        LOG.setLevel(level)
    transcripts, debug_source = voice_debug(debug_environ)
    channel = microphone_channel(loopback=loopback, port=port, diagnostic=microphone is not None)
    command = capture_command(capture, channel)
    startup_log("audio_route_ready", startup_started)
    settings = load_voice_settings(mind_dir)
    mixer = Mixer(os.environ.get("BEING_ALSA_CARD") or mixer_card(playback),
                  os.environ.get("BEING_ALSA_MIXER", "Master"), mind_dir / ALSA_STATE_FILE)
    chirp_bank = ChirpBank(Path(os.environ.get("BEING_CHIRP_DIR", "/run/being/chirps")))
    recognizer = await wake_task
    startup_log("wake_ready", startup_started, phase_s=time.monotonic() - wake_began)
    auto = os.environ.get("BEING_MIC_AUTO_GAIN", "1") != "0"
    mic = MicLevel(mic_gain(), auto=auto)
    organ = None
    try:
        if port and microphone is None:
            microphone = MicrophonePort()
        grammar = command_grammar(recognizer)
        LOG.info("capture from %s, playback to %s through aplay",
                 getattr(microphone, "description", "the QEMU microphone port") if microphone is not None
                 else f"{capture} through arecord", playback)
        transcripts = transcripts or LOG.isEnabledFor(logging.DEBUG)
        # The engine is named once chosen ("speech engine: ..."): until then
        # Flite stands in, and naming it here read as the choice.
        LOG.info("wake phrase %r, mic gain %gx%s, speech engine %s (%s), transcript text in the log %s%s",
                 getattr(recognizer, "wake_phrase", profile.wake_word), mic.manual,
                 f" with automatic lift of a quiet floor up to {mic.max_lift:g}x" if auto else "",
                 engine_name(tts) if forced or engines is None else "to be chosen",
                 override or forced or tts_choice, "on" if transcripts else "off",
                 f" (from {debug_source})" if transcripts and debug_source else "")
        cutoff, cutoff_source = highpass_setting(os.environ, CMDLINE, default=HIGHPASS_DEFAULT)
        highpass = HighPass(cutoff) if cutoff is not None else None
        LOG.info("capture high-pass for the recognizer: %s (from %s)",
                 f"on at {cutoff:g} Hz" if highpass is not None else "off", cutoff_source)
        organ = Voice(
            recognizer, profile.owner, command, playback, microphone, voice=voice, rate=profile.rate,
            shorten_first_pause=os.environ.get("BEING_VOICE_SHORTEN_FIRST_PAUSE") == "1", tts=tts,
            settings_dir=mind_dir, settings=settings, mixer=mixer, chirp_bank=chirp_bank,
            chirps=settings["chirps"] and lab_default("BEING_CHIRPS", in_lab=in_lab),
            hear_sounds=lab_default("BEING_HEAR_SOUND", in_lab=in_lab), quiet_hours=quiet_hours_check(mind_dir),
            mic=mic, channel=ChannelPicker(channel) if channel != "mix" else None,
            tts_choice=tts_choice, engines=engines, typing_hint=typing_hint, tts_forced=bool(forced),
            tts_override=override, transcripts=transcripts, grammar=grammar, try_boot=try_boot,
            highpass=highpass, guest_latch=Path("/etc/being/env").is_file(),
        )
        startup_log("organ_ready", startup_started)
        await organ.run()
    finally:
        if microphone is not None:
            microphone.close()
        current = organ.recognizer if organ is not None else recognizer
        current.close()
        if current is not recognizer and organ is not None and recognizer not in organ.retired \
                and getattr(current, "wake", None) is not recognizer:
            recognizer.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        pass
