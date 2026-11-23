"""The local, half-duplex voice organ. Audio stays outside the event bus."""

import asyncio
import inspect
import json
import logging
import math
import os
import re
import signal
import stat
import tempfile
import time
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from . import mic_channel, sd_notify
from .bus import Bus
from .mic_channel import ChannelPicker
from .mic_port import MicrophonePort
from .profile import DEFAULT_VOICE, FACTORY_CONFIG, VOICES, default_mind_dir, factory_profile, load_profile
from .recognition import (
    DIGITAL_SILENCE_DBFS, FRAME_BYTES, FRAME_SAMPLES, MAX_MIC_GAIN, MIN_MIC_GAIN, RATE, MicLevel, Recognizer,
    amplify, dbfs, levels, pcm,
)
from .tts import (
    CHIRPS, ChirpBank, FliteEngine, available_voices, playback_command, scale_wav, select_engine,
    shorten_leading_pause, synthesis_command, volume_gain,
)

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
    "body.lid", "body.state", "state.changed", "voice.mute", "voice.volume", "voice.chirps",
    "voice.level_request", "voice.engine", "say.request", "say.stop", "stem.chirp",
)
FRAME_SECONDS = FRAME_SAMPLES / RATE
RUN_DIR = "/run"

# After a reply it hears a follow-up without its name only this long (task 3.10).
FOLLOW_UP_SECONDS = 8.0
# At most one onset reaction per this many seconds (task 5.6).
SOUND_INTERVAL = 10.0
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
# Two-channel captures that deliver nothing before one channel is tried as well.
STEREO_ATTEMPTS = 3
# aplay gets the clip's length plus this long before it is judged stuck. A
# speaker that fails at once is tried again, with growing pauses.
PLAYBACK_MARGIN = 5.0
PLAYBACK_ATTEMPTS = 3
PLAYBACK_RETRY_FIRST = 0.2
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
# A frame this far over the noise floor is the owner's voice, for timing.
VOICED_OVER_FLOOR_DB = 10.0
# The microphone's per-frame cost is measured over this many frames, once.
COST_FRAMES = 1000
# While resting, voice_engines may take a due measurement this often.
ENGINE_REFRESH_SECONDS = 900.0

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

LOG = logging.getLogger("being.voice")


class _JournalHandler(logging.Handler):
    """One line on standard output per message, as the journal reads it."""

    def emit(self, record):
        try:
            print(self.format(record), flush=True)
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
    if event.get("chirp") == "bell":
        # A timer, alarm or reminder ringing: a bell before its first words.
        clause["chirp"] = "bell"
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

    async def get(self):
        """The control's current level on the same curve `set` uses, or None."""
        try:
            async with child("amixer", "-M", "-c", self.card, "sget", self.control,
                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL) as process:
                output, _ = await asyncio.wait_for(process.communicate(), MIXER_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return None
        if process.returncode != 0:
            return None
        levels_ = [int(value) for value in re.findall(rb"\[(\d{1,3})%\]", output)]
        levels_ = [value for value in levels_ if 0 <= value <= 100]
        return max(levels_) if levels_ else None

    async def set(self, level):
        """Return True if the hardware mixer took the level."""
        if not await self.run("amixer", "-q", "-M", "-c", self.card, "sset", self.control, f"{level}%"):
            return False
        # Not every control has a switch, so the mute state is best effort.
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


class Voice:
    def __init__(self, recognizer, speaker, capture, playback, mic_port=None, *,
                 voice=DEFAULT_VOICE, rate=1.0, shorten_first_pause=False, tts=None,
                 settings_dir=None, settings=None, mixer=None, chirp_bank=None, chirps=False,
                 hear_sounds=False, quiet_hours=None, mic=None, channel=None, clock=None,
                 tts_choice="auto", engines=None, asr="pocketsphinx", typing_hint=None, tts_forced=False):
        self.recognizer = recognizer
        self.speaker = speaker
        self.capture_command = capture
        self.mic_port = mic_port
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
        self.tts_forced = tts_forced
        self.settings_dir = settings_dir
        settings = settings or {"volume": None, "chirps": True}
        self.volume = settings["volume"]
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
        self._clock = clock
        self.work_dir = RUN_DIR if os.access(RUN_DIR, os.W_OK) else None
        self.bus = Bus("voice", SUBSCRIPTIONS, urgent=self.on_event_received)
        self.bus.on_disconnect = self.connection_lost
        self.tasks = None
        self.state = "waking"
        self.muted = True
        self.lid_open = False
        self.lid_seq = None
        self._latest_lid = None
        self.capture = None
        self.reply = None
        self.chirp_task = None
        self.purr_task = None
        self.apology = None
        self.engine_task = None
        self.refresh_task = None
        # Recognizers replaced and already closed.
        self.retired = []
        self._synthesis_pool = None
        self.clauses = asyncio.Queue()
        self.clock = None
        self.dictation_until = 0.0
        self.followup = False
        self.deaf_until = 0.0
        self.last_sound = -math.inf
        self.reply_failures = 0
        self._prompted = False
        self._oneshot_wake = False
        self._start_sent = False
        self._bell_first = False  # the reply being spoken opens with the ring bell
        self._done_sent = False
        self._in_speech = False
        # A wake phrase was heard and nothing usable has been said since.
        self.expecting = False
        # The wake phrase was heard and no apology has been spoken since.
        self.apology_owed = False
        self.last_apology_at = -math.inf
        self.hint_at = -math.inf
        # Levels of the current dictation and of the last sound segment.
        self.dictation = None
        self.segment = None
        self.last_segment = None
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
        try:
            async with asyncio.TaskGroup() as tasks:
                self.tasks = tasks
                if self.volume is not None:
                    await self.apply_volume(self.volume)
                self.spawn(self.level_loop())
                self.spawn(sd_notify.keep_alive())
                if self.engines is not None:
                    self.refresh_task = self.spawn(self.choose_engines())
                try:
                    while True:
                        event = await self.bus.recv()
                        try:
                            await self.handle(event)
                        except (KeyError, TypeError, ValueError) as error:
                            log(f"Ignored a malformed {event.get('t')!r} event: {error!r}")
                finally:
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
        for task in (self.capture, self.reply, self.chirp_task, self.purr_task, self.apology):
            if task is not None and not task.cancelling():
                task.cancel()
        self.clear_clauses()

    def connection_lost(self):
        self.disarm()
        self.state = "waking"
        self.muted = True
        self.dictation_until = 0.0
        self._prompted = False

    def on_event_received(self, event):
        if event["t"] == "body.lid":
            if event.get("closed") is not False:
                self.disarm()
            self._latest_lid = event

    async def handle(self, event):
        topic = event["t"]
        if topic == "body.lid":
            if event.get("closed") is not False:
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
            await self.follow_attention(before)
            self.follow_state(previous, self.state)
            await self.sync_capture()
            if previous is not None:
                await self.state_chirp(previous, self.state)
        elif topic == "voice.mute":
            self.muted = event["on"]
            if self.muted:
                self.expecting = False
                self.apology_owed = False
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
        elif topic == "stem.chirp":
            await self.chirp(event.get("name"))
        elif topic == "body.state":
            hour, minute = event.get("hour"), event.get("minute")
            valid = type(hour) is int and type(minute) is int and 0 <= hour < 24 and 0 <= minute < 60
            self.clock = (hour, minute) if valid else None
        elif topic == "say.stop":
            await self.stop_apology()
            await self.stop_reply()
        elif topic == "say.request" and self.lid_open and self.state in TALKING:
            await self.clauses.put(clause_request(event))
            if self.reply is None or self.reply.done():
                await self.stop_capture()
                await self.stop_chirp()
                await self.stop_apology()
                if self.lid_open:
                    self.reply = self.spawn(self.speak())

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
            elif previous not in ATTENDING:
                # A wake by name, key or touch from rest.
                self.dictation_until = math.inf
                self.followup = False
        elif state not in (*ATTENDING, *TALKING):
            self.dictation_until = 0.0
            self._prompted = False
        if state != "thinking" and self.purr_task is not None:
            self.purr_task.cancel()
            self.purr_task = None

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
        return self.lid_open and self.bus.connected.is_set() and not self.muted and self.state in HEARING

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
            return "the QEMU microphone port"
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
        if self.mic_port is not None:
            async with self.mic_port.recording() as stream:
                yield stream
        else:
            self.capture_process = None
            command = self.capture_command
            if self.channel is not None and self.mono_trial:
                command = mic_channel.mono_command(command)
            async with child(*command, stdout=asyncio.subprocess.PIPE) as process:
                self.capture_process = process
                yield process.stdout

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

    async def capture_stream(self):
        self.frames_read = 0
        self._in_speech = False
        async with self.recording() as stream:
            LOG.info("capture open on %s, %s, mic gain %.2fx", self.capture_device, self.describe_capture(),
                     self.mic.gain)
            stereo = self.stereo
            size = self.channel.frame_bytes if stereo else FRAME_BYTES
            self.recognizer.start_capture()
            try:
                while self.lid_open and self.bus.connected.is_set():
                    try:
                        frame = await asyncio.wait_for(stream.readexactly(size), 1)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.IncompleteReadError as error:
                        raise RuntimeError("microphone stream closed") from error
                    self.frames_read += 1
                    if self.mono_trial and self.channel is not None:
                        LOG.warning("%s gives one channel but not two; averaging both channels from now on",
                                    self.capture_device)
                        self.channel = None
                        self.mono_trial = False
                    started = time.perf_counter()
                    if stereo and self.channel is not None:
                        frame = self.channel.mono(frame, measure=not self.silenced)
                    frame, level = self.condition(frame)
                    self.account(time.perf_counter() - started)
                    dictating = self.state in ATTENDING and self.dictation_open()
                    oneshot = self.state in RESTING or (self.state == "attending" and not dictating)
                    events = self.recognizer.feed(frame, dictating, oneshot)
                    self._in_speech = getattr(self.recognizer, "in_speech", False) is True
                    self.measure(level, self._in_speech)
                    for topic, fields in events:
                        await self.hear(topic, fields)
            finally:
                self.recognizer.stop_capture()

    @property
    def silenced(self):
        """Its own earcon or apology is playing, or has only just stopped."""
        return self.apologising or self.now() < self.deaf_until

    def condition(self, frame):
        """Apply the software gain, or silence its own sounds. Returns the frame and its raw levels."""
        if self.silenced:
            return bytes(len(frame)), None
        samples = pcm(frame)
        level = levels(samples)
        self.mic.observe(*level, self._in_speech)
        gain = self.mic.gain
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

    def measure(self, level, in_speech):
        if level is not None:
            if in_speech:
                if self.segment is None:
                    self.segment = Span()
                self.segment.add(*level)
            if self.dictation is not None:
                self.dictation.add(*level)
                floor = self.mic.floor
                voiced = (dbfs(level[1]) >= floor + VOICED_OVER_FLOOR_DB if floor is not None
                          and floor > DIGITAL_SILENCE_DBFS else in_speech)
                if voiced:
                    self.voiced_at = self.now()
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

    async def hear(self, topic, fields):
        oneshot = bool(fields.get("oneshot"))
        if oneshot and topic in ("hear.partial", "hear.final", "hear.cancel") and self.state not in ATTENDING:
            # The brainstem did not take the one-breath wake (muted, busy or
            # asleep), so the words are not addressed to it and leave no trace.
            if topic != "hear.partial":
                self.dictation = None
                self.expecting = False
            return
        if topic == "hear.sound":
            now = self.now()
            if not self.hear_sounds or self.state not in RESTING or now - self.last_sound < SOUND_INTERVAL:
                return
            self.last_sound = now
        elif topic == "hear.wake":
            segment = self.last_segment or self.segment or Span()
            LOG.info("heard the wake phrase%s%s: score %s, %s, mic gain %.2fx", " in one breath" if oneshot else "",
                     f" while {self.state}" if self.state in TALKING else "",
                     number(getattr(self.recognizer, "wake_score", None), "d"), segment.describe(), self.mic.gain)
            # The brainstem ignores a wake during its turn, so nothing is
            # expected and no apology is owed for one heard then.
            if self.state not in TALKING:
                self.dictation_until = math.inf
                self.followup = False
                self._oneshot_wake = oneshot
                self.expecting = True
                self.apology_owed = True
                self.pending_timing = None
        elif topic == "hear.speech_start":
            self.dictation = Span()
            self.voiced_at = None
            LOG.info("dictation started while %s", self.state)
        elif topic == "hear.partial":
            text = fields.get("text", "")
            LOG.info("partial transcript: %d characters", len(text) if isinstance(text, str) else 0)
            LOG.debug("partial transcript text: %r", text)
        elif topic == "hear.final":
            fields["speaker"] = self.speaker
            if self.followup and not oneshot:
                # Heard without its name, in the window after a reply.
                fields["followup"] = True
            if fields.get("text"):
                self._prompted = True
        await self.send_audio(topic, **fields)
        if topic in ("hear.final", "hear.cancel"):
            await self.dictation_ended(topic, fields)

    async def dictation_ended(self, topic, fields):
        text = fields.get("text", "") if topic == "hear.final" else ""
        text = text if isinstance(text, str) else ""
        # The confidence is read after the final is on its way.
        dictation, self.dictation = self.dictation or Span(), None
        now = self.now()
        if text.strip():
            confidence = self.recognizer.confidence() if hasattr(self.recognizer, "confidence") else None
            LOG.info("dictation ended after %.1f s: final transcript of %d characters, confidence %s, %s",
                     dictation.seconds, len(text), number(confidence, ".2f"), dictation.describe())
            LOG.debug("final transcript text: %r", text)
            self.expecting = False
            # This wake was answered with words, so nothing is owed for it. A
            # cough in the follow-up window after the reply must not be
            # apologised for (hear.missed still goes out for it).
            self.apology_owed = False
            self.heard_at = now
            ended = self.voiced_at if self.voiced_at is not None and self.voiced_at <= now else now
            self.pending_timing = (ended, now)
        else:
            LOG.info("dictation ended after %.1f s with no words, %s", dictation.seconds, dictation.describe())
            if self.expecting or dictation.seconds >= MISSED_MIN_SECONDS:
                self.expecting = False
                LOG.info("asking to hear that again")
                await self.missed("unclear")
        self.voiced_at = None

    # Missed words ----------------------------------------------------------------

    async def missed(self, reason):
        """Report a miss to the bus, and say sorry aloud once per wake."""
        await self.send_audio("hear.missed", reason=reason)
        if not self.apology_owed:
            return
        if not self.lid_open or self.muted or self.state not in APOLOGY_IN:
            LOG.info("not saying sorry while %s%s", self.state, ", muted" if self.muted else "")
            return
        if (self.reply is not None and not self.reply.done()) or self.apologising:
            return
        self.apology_owed = False
        self.apology = self.spawn(self.apologise())

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

    async def apologise(self):
        """Speak the apology through the reply's own synthesis and playback. The
        microphone is fed silence meanwhile, as for an earcon."""
        try:
            if self.chirp_task is not None:
                await asyncio.gather(self.chirp_task, return_exceptions=True)
            hint = await asyncio.to_thread(self.hint_text) if self.hint_due() else None
            line = self.apology_line(hint)
            with tempfile.TemporaryDirectory(prefix="pip-", dir=self.work_dir) as work:
                audio = Path(work) / "sorry.wav"
                async with asyncio.timeout(synthesis_timeout(line)):
                    await self.synthesize(line, audio, False)
                await self.play(audio, first=False)
            LOG.info("said sorry for the missed words%s", " and how to type" if line != APOLOGY else "")
        except Exception as error:
            LOG.warning("could not say sorry on %s: %s", self.playback, error)
        finally:
            self.deaf_until = self.now() + CHIRP_DEAF_MARGIN
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
        if not self.tts_forced:
            # A voice that will not load must not cost the better ears, so the
            # two choices fail separately.
            try:
                await self.adopt_tts(self.tts_choice)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("could not choose the speech engine: %r", error)
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
        while True:
            await asyncio.sleep(ENGINE_REFRESH_SECONDS)
            if self.state not in AT_REST or self.settings_dir is None:
                continue
            try:
                await self.refresh_engines(wake)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("could not re-measure the speech engines: %r", error)

    async def refresh_engines(self, wake):
        refresh = getattr(self.engines, "refresh_tts", None)
        if refresh is not None and not self.changing_engine() and not self.tts_forced:
            current, choice = self.tts, self.tts_choice
            found = await refresh(current, choice, state_dir=self.settings_dir)
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
                                  wake_word=phrase if isinstance(phrase, str) else "Hey Pip")
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

    async def adopt_tts(self, choice):
        engine, note = await asyncio.to_thread(pick_tts, self.engines, choice, self.settings_dir)
        if note:
            log(note)
        # The owner's latest spoken choice wins over this one if it changed
        # while the voice was being chosen or loaded.
        await self.use_tts(engine, choice, lambda: self.tts_choice == choice and not self.changing_engine())

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
        if self.tts is not old or (wanted is not None and not wanted()):
            await self.drop_engine(engine, old)
            return
        if engine_name(engine) != engine_name(old):
            LOG.info("speech engine: %s (%s)", engine_name(engine), choice)
        self.tts = engine
        await self.release_engine(old)

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

    async def change_engine(self, choice):
        if choice not in TTS_CHOICES:
            log("Ignored a voice.engine without tts of auto, piper or flite.")
            return
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

    async def apply_volume(self, level):
        """Use the card's mixer, or scale samples when no mixer answers."""
        hardware = self.mixer is not None and await self.mixer.set(level)
        self.volume = level
        self.gain = 1.0 if hardware else volume_gain(level)
        return "hardware" if hardware else "software"

    async def change_volume(self, event):
        level, step = event.get("level"), event.get("step")
        if type(level) is int and 0 <= level <= 100:
            target = level
        elif type(step) is int and -100 <= step <= 100:
            target = max(0, min(100, await self.current_volume() + step))
        else:
            log("Ignored a volume change without a level from 0 to 100.")
            return
        method = await self.apply_volume(target)
        self.save_settings()
        if method == "hardware":
            self.spawn(self.store_mixer())
        # The mind keeps the owner's level too; tell it what took effect.
        await self.bus.send("voice.volume_state", level=target, mixer=method)

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
            save_voice_settings(self.settings_dir, volume=self.volume, chirps=self.chirp_choice, tts=self.tts_choice)
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
        self.deaf_until = self.now() + seconds + CHIRP_DEAF_MARGIN
        try:
            async with child(*playback_command(self.playback, path)) as process:
                if announce:
                    await self.send_audio("say.started")
                    self._start_sent = True
                code = await asyncio.wait_for(process.wait(), seconds + PLAYBACK_MARGIN)
        finally:
            self.deaf_until = self.now() + CHIRP_DEAF_MARGIN
        return code == 0

    async def stop_chirp(self):
        task, self.chirp_task = self.chirp_task, None
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Speaking --------------------------------------------------------------------

    async def stop_reply(self):
        if self.reply is not None:
            if not self.reply.cancelling():
                self.reply.cancel()
            await asyncio.gather(self.reply, return_exceptions=True)
            self.reply = None
        self.clear_clauses()

    def clear_clauses(self):
        while not self.clauses.empty():
            self.clauses.get_nowait()

    async def prepare_clause(self, audio, *, first=False):
        """Synthesize the next clause. A failure skips the clause, never the reply."""
        event = await self.clauses.get()
        if first and event.get("chirp") == "bell":
            self._bell_first = True
        if event.get("bad"):
            self.reply_failures += 1
            log("Skipped a clause without text.")
        text = event["text"].strip()
        if text:
            try:
                async with asyncio.timeout(synthesis_timeout(text)):
                    await self.synthesize(text, audio, first)
            except Exception as error:
                self.reply_failures += 1
                LOG.warning("could not synthesize a clause of %d characters with %s: %r; skipping it",
                            len(text), engine_name(self.tts, "the speech engine"), error)
                return False, event["last"], event.get("announcement")
        return bool(text), event["last"], event.get("announcement")

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
                return
        trial = (first and self.shorten_first_pause and self.voice in PAUSE_TRIAL_VOICES
                 and isinstance(engine, FliteEngine) and len(text) <= PAUSE_TRIAL_MAX_CHARS)
        command = synthesis_command(self.voice, self.rate, text, audio, segment_times=trial)
        segments = await self.synthesize_command(command, "flite", segments=trial)
        if segments:
            shorten_leading_pause(audio, segments)
        await self.scale(audio)

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
            if not self._done_sent:
                try:
                    await self.send_audio("say.done", final=True, failed=True)
                except Exception as send_error:
                    log(f"Could not report the failed reply: {send_error!r}")
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
                try:
                    ready, last, announcement = await self.prepare_clause(audio, first=True)
                    while True:
                        if ready:
                            if not last:
                                # One clause can be synthesized while the speaker plays the current WAV.
                                successor = asyncio.create_task(self.prepare_clause(next_audio))
                            if not started and self._bell_first:
                                self._bell_first = False
                                await self.ring_bell()
                            try:
                                await self.play(audio, first=not started)
                                started = True
                            except Exception as error:
                                self.reply_failures += 1
                                started = started or self._start_sent
                                LOG.warning("skipping a clause that could not play: %r", error)
                        if last:
                            if not started and self.reply_failures:
                                # Nothing could be said. A short troubled sound is honest audio,
                                # and lets the brainstem leave thinking at once.
                                started = await self.play_trouble()
                            # Release the capture gate before the brainstem returns to attending.
                            self.reply = None
                            fields = {"final": True}
                            if self.reply_failures:
                                fields["failed"] = True
                            elif started and isinstance(announcement, str) and announcement:
                                fields["announcement"] = announcement
                            await self.send_audio("say.done", **fields)
                            self._done_sent = True
                            return
                        if successor is None:
                            ready, last, announcement = await self.prepare_clause(audio, first=not started)
                        else:
                            ready, last, announcement = await successor
                            successor = None
                            audio, next_audio = next_audio, audio
                finally:
                    if successor is not None:
                        successor.cancel()
                        await asyncio.gather(successor, return_exceptions=True)
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

    async def play(self, audio, first):
        """Play one clause, retrying a speaker that fails at once with growing pauses."""
        delay = PLAYBACK_RETRY_FIRST
        for attempt in range(1, PLAYBACK_ATTEMPTS + 1):
            try:
                await self.play_once(audio, first and attempt == 1)
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

    async def play_once(self, audio, first):
        with wave.open(str(audio)) as wav:
            rate = wav.getframerate()
            block = rate // 25
            limit = wav.getnframes() / rate + PLAYBACK_MARGIN
            async with child(*playback_command(self.playback, audio)) as process:
                if first:
                    await self.send_audio("say.started")
                    self._start_sent = True
                    self.first_audio()
                deadline = time.monotonic() + limit
                while process.returncode is None:
                    if time.monotonic() > deadline:
                        raise PlaybackStuck("ALSA playback did not finish")
                    samples = array("h", wav.readframes(block))
                    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) if samples else 0
                    await self.send_audio("say.level", v=round(min(1.0, rms / 8500), 3))
                    await asyncio.sleep(0.04)
                if status := await process.wait():
                    raise RuntimeError(f"aplay exit status {status}")


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
    from .initiative import load_policy

    def quiet(clock):
        policy = load_policy(mind_dir)
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


def microphone_channel(environ=os.environ, cmdline=mic_channel.CMDLINE, *, loopback=False, port=False):
    """mix, left, right or auto, from being.mic_channel= or BEING_MIC_CHANNEL."""
    choice, source = mic_channel.setting(environ, cmdline)
    if choice != "mix" and (loopback or port):
        LOG.info("microphone channel %s from %s does not apply to %s; using mix", choice, source,
                 "the lab's loopback body" if loopback else "the QEMU microphone port")
        return "mix"
    LOG.info("microphone channel: %s (from %s)", choice, source)
    return choice


def capture_command(device, channel="mix"):
    return ["arecord", "-q", "-D", device, "-t", "raw", "-f", "S16_LE", "-r", str(RATE),
            "-c", str(mic_channel.capture_channels(channel)), "--buffer-time=120000", "--period-time=30000"]


def typing_hint():
    from .type_chat import typing_hint as hint

    return hint()


async def main():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    if os.environ.get("BEING_VOICE_DEBUG") == "1":
        LOG.setLevel(logging.DEBUG)
    mind_dir = default_mind_dir()
    profile = load_profile(FACTORY_CONFIG, mind_dir, strict=False)
    voice = resolve_voice(profile.voice)
    engines = None
    tts_choice = load_engine_choice(mind_dir)
    forced = os.environ.get("BEING_TTS_ENGINE")
    if forced:
        # An explicit engine in /etc/being/env wins over the automatic choice
        # of voice (the recognizer is still chosen automatically).
        tts, note = select_engine(forced, voice_name=os.environ.get("BEING_PIPER_VOICE"))
    else:
        tts, note = FliteEngine(), None
    engines = load_engines()
    if note:
        print(note, flush=True)
    print(f"Playback voice: {voice} at {profile.rate:g}x", flush=True)
    # The recognizer after the wake (BEING_ASR_ENGINE: auto, moonshine or
    # pocketsphinx) is chosen by voice_engines.choose_asr, which says why.
    in_lab = FW.exists()
    loopback = (FW / "loopback" / "raw").exists()
    if loopback:
        async with child("modprobe", "snd-aloop", "pcm_substreams=2") as module:
            if await module.wait():
                raise RuntimeError("could not load ALSA loopback")
    capture, playback = audio_devices()
    port = (FW / "microphone" / "raw").exists()
    channel = microphone_channel(loopback=loopback, port=port)
    command = capture_command(capture, channel)
    settings = load_voice_settings(mind_dir)
    mixer = Mixer(os.environ.get("BEING_ALSA_CARD") or mixer_card(playback),
                  os.environ.get("BEING_ALSA_MIXER", "Master"), mind_dir / ALSA_STATE_FILE)
    chirp_bank = ChirpBank(Path(os.environ.get("BEING_CHIRP_DIR", "/run/being/chirps")))
    recognizer = wake_recognizer(profile.wake_word)
    auto = os.environ.get("BEING_MIC_AUTO_GAIN", "1") != "0"
    mic = MicLevel(mic_gain(), auto=auto)
    microphone = None
    organ = None
    try:
        if port:
            microphone = MicrophonePort()
        LOG.info("capture from %s, playback to %s through aplay",
                 "the QEMU microphone port" if microphone is not None else f"{capture} through arecord", playback)
        LOG.info("wake phrase %r, mic gain %gx%s, speech engine %s (%s), transcript text in the log %s",
                 getattr(recognizer, "wake_phrase", profile.wake_word), mic.manual,
                 " with automatic lift of a quiet floor" if auto else "", engine_name(tts),
                 forced or tts_choice, "on" if LOG.isEnabledFor(logging.DEBUG) else "off")
        organ = Voice(
            recognizer, profile.owner, command, playback, microphone, voice=voice, rate=profile.rate,
            shorten_first_pause=os.environ.get("BEING_VOICE_SHORTEN_FIRST_PAUSE") == "1", tts=tts,
            settings_dir=mind_dir, settings=settings, mixer=mixer, chirp_bank=chirp_bank,
            chirps=settings["chirps"] and lab_default("BEING_CHIRPS", in_lab=in_lab),
            hear_sounds=lab_default("BEING_HEAR_SOUND", in_lab=in_lab), quiet_hours=quiet_hours_check(mind_dir),
            mic=mic, channel=ChannelPicker(channel) if channel != "mix" else None,
            tts_choice=tts_choice, engines=engines, typing_hint=typing_hint, tts_forced=bool(forced),
        )
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
