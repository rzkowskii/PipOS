"""The local, half-duplex voice organ. Audio stays outside the event bus."""

import asyncio
import json
import math
import os
import re
import signal
import stat
import tempfile
import time
import wave
from array import array
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from .bus import Bus
from .mic_port import MicrophonePort
from .profile import DEFAULT_VOICE, FACTORY_CONFIG, VOICES, default_mind_dir, factory_profile, load_profile
from .recognition import FRAME_BYTES, RATE, Recognizer, engine_choice
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
PAUSE_TRIAL_VOICES = frozenset(("slt", "rms", "awb"))
PAUSE_TRIAL_MAX_CHARS = 512
SUBSCRIPTIONS = (
    "body.lid", "body.state", "state.changed", "voice.mute", "voice.volume", "voice.chirps",
    "say.request", "say.stop", "stem.chirp",
)

# After a reply it hears a follow-up without its name only this long (task 3.10).
FOLLOW_UP_SECONDS = 8.0
# At most one onset reaction per this many seconds (task 5.6).
SOUND_INTERVAL = 10.0
# The thinking purr starts this long into a prompted turn, then repeats slowly.
PURR_AFTER = 1.5
PURR_REPEAT = 12.0
# Its own earcons are not heard: the mic reads silence while one plays.
CHIRP_DEAF_MARGIN = 0.12
# Capture retries back off from one second to half a minute.
CAPTURE_RETRY_SECONDS = 1.0
MAX_CAPTURE_RETRY_SECONDS = 30.0
CAPTURE_HEALTHY_SECONDS = 30.0
# aplay gets the clip's length plus this long before it is judged stuck.
PLAYBACK_MARGIN = 5.0
MIXER_TIMEOUT = 3.0
SILENCE = bytes(FRAME_BYTES)

SETTINGS_FILE = Path("audio") / "voice.json"
ALSA_STATE_FILE = Path("audio") / "asound.state"
SETTINGS_VERSION = 1
MAX_SETTINGS_BYTES = 1024
# Unscaled samples: the level a relative change starts from when no mixer
# reports one. Assuming less would make "louder" quieter in software volume.
FULL_LEVEL = 100


def log(message):
    print(message, flush=True)


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

def load_voice_settings(mind_dir):
    """Read the saved volume and chirp switch. Damage gives defaults, never an error."""
    settings = {"volume": None, "chirps": True}
    if mind_dir is None:
        return settings
    path = Path(mind_dir) / SETTINGS_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("not a private regular file")
            raw = source.read(MAX_SETTINGS_BYTES + 1)
        if len(raw) > MAX_SETTINGS_BYTES:
            raise ValueError("too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != SETTINGS_VERSION:
            raise ValueError("unknown format")
    except FileNotFoundError:
        return settings
    except (OSError, ValueError, RecursionError) as error:
        log(f"Voice settings unreadable ({error}). Using defaults.")
        return settings
    volume = data.get("volume")
    if type(volume) is int and 0 <= volume <= 100:
        settings["volume"] = volume
    if type(data.get("chirps")) is bool:
        settings["chirps"] = data["chirps"]
    return settings


def save_voice_settings(mind_dir, *, volume, chirps):
    """Atomically keep the owner's volume and chirp choice in the mind."""
    from .storage import atomic_write

    path = Path(mind_dir) / SETTINGS_FILE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"version": SETTINGS_VERSION, "volume": volume, "chirps": chirps}
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
        levels = [int(value) for value in re.findall(rb"\[(\d{1,3})%\]", output)]
        levels = [value for value in levels if 0 <= value <= 100]
        return max(levels) if levels else None

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


class Voice:
    def __init__(self, recognizer, speaker, capture, playback, mic_port=None, *,
                 voice=DEFAULT_VOICE, rate=1.0, shorten_first_pause=False, tts=None,
                 settings_dir=None, settings=None, mixer=None, chirp_bank=None, chirps=False,
                 hear_sounds=False, quiet_hours=None):
        self.recognizer = recognizer
        self.speaker = speaker
        self.capture_command = capture
        self.mic_port = mic_port
        self.playback = playback
        self.voice = voice
        self.rate = rate
        self.shorten_first_pause = shorten_first_pause
        self.tts = tts or FliteEngine()
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
        self._done_sent = False

    async def run(self):
        await self.bus.start()
        try:
            async with asyncio.TaskGroup() as tasks:
                self.tasks = tasks
                if self.volume is not None:
                    await self.apply_volume(self.volume)
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
        finally:
            await self.bus.stop()

    def spawn(self, coroutine):
        if self.tasks is not None:
            return self.tasks.create_task(coroutine)
        return asyncio.create_task(coroutine)

    def disarm(self):
        """Close audio immediately, including while a queued event is handled."""
        self.lid_open = False
        self.lid_seq = None
        self._latest_lid = None
        for task in (self.capture, self.reply, self.chirp_task, self.purr_task):
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
            self.state = state
            self.muted = event.get("muted", self.muted)
            self.follow_state(previous, self.state)
            await self.sync_capture()
            if previous is not None:
                await self.state_chirp(previous, self.state)
        elif topic == "voice.mute":
            self.muted = event["on"]
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
        elif topic == "stem.chirp":
            await self.chirp(event.get("name"))
        elif topic == "body.state":
            hour, minute = event.get("hour"), event.get("minute")
            valid = type(hour) is int and type(minute) is int and 0 <= hour < 24 and 0 <= minute < 60
            self.clock = (hour, minute) if valid else None
        elif topic == "say.stop":
            await self.stop_reply()
        elif topic == "say.request" and self.lid_open and self.state in TALKING:
            await self.clauses.put(clause_request(event))
            if self.reply is None or self.reply.done():
                await self.stop_capture()
                await self.stop_chirp()
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
        now = time.monotonic()
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

    def dictation_open(self):
        return self.state == "listening" or (self.state == "attending" and time.monotonic() < self.dictation_until)

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

    @asynccontextmanager
    async def recording(self):
        if self.mic_port is not None:
            async with self.mic_port.recording() as stream:
                yield stream
        else:
            async with child(*self.capture_command, stdout=asyncio.subprocess.PIPE) as process:
                yield process.stdout

    async def listen(self):
        """Keep the ears open through device errors, backing off between tries."""
        delay = CAPTURE_RETRY_SECONDS
        while self.capture_wanted():
            began = time.monotonic()
            try:
                await self.capture_stream()
                return
            except Exception as error:
                log(f"Microphone trouble: {error}. Trying again in {delay:g} s.")
            if time.monotonic() - began >= CAPTURE_HEALTHY_SECONDS:
                delay = CAPTURE_RETRY_SECONDS
            await asyncio.sleep(delay)
            delay = min(MAX_CAPTURE_RETRY_SECONDS, delay * 2)

    async def capture_stream(self):
        async with self.recording() as stream:
            self.recognizer.start_capture()
            try:
                while self.lid_open and self.bus.connected.is_set():
                    try:
                        frame = await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 1)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.IncompleteReadError as error:
                        raise RuntimeError("microphone stream closed") from error
                    if time.monotonic() < self.deaf_until:
                        frame = SILENCE
                    dictating = self.state in ATTENDING and self.dictation_open()
                    oneshot = self.state in RESTING or (self.state == "attending" and not dictating)
                    for topic, fields in self.recognizer.feed(frame, dictating, oneshot):
                        await self.hear(topic, fields)
            finally:
                self.recognizer.stop_capture()

    async def hear(self, topic, fields):
        if fields.get("oneshot") and topic in ("hear.partial", "hear.final") and self.state not in ATTENDING:
            # The brainstem did not take the one-breath wake (muted, busy or
            # asleep), so the words are not addressed to it and leave no trace.
            return
        if topic == "hear.sound":
            now = time.monotonic()
            if not self.hear_sounds or self.state not in RESTING or now - self.last_sound < SOUND_INTERVAL:
                return
            self.last_sound = now
        elif topic == "hear.wake":
            self.dictation_until = math.inf
            self.followup = False
            self._oneshot_wake = bool(fields.get("oneshot"))
        elif topic == "hear.final":
            fields["speaker"] = self.speaker
            if self.followup and not fields.get("oneshot"):
                # Heard without its name, in the window after a reply.
                fields["followup"] = True
            if fields.get("text"):
                self._prompted = True
        await self.send_audio(topic, **fields)

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
            save_voice_settings(self.settings_dir, volume=self.volume, chirps=self.chirp_choice)
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
            log(f"Could not play a chirp: {error}")
            return False

    async def play_chirp(self, path, *, announce=False):
        with wave.open(str(path)) as wav:
            seconds = wav.getnframes() / wav.getframerate()
        self.deaf_until = time.monotonic() + seconds + CHIRP_DEAF_MARGIN
        async with child(*playback_command(self.playback, path)) as process:
            if announce:
                await self.send_audio("say.started")
                self._start_sent = True
            code = await asyncio.wait_for(process.wait(), seconds + PLAYBACK_MARGIN)
        self.deaf_until = time.monotonic() + CHIRP_DEAF_MARGIN
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
                log(f"Skipped a clause that could not be synthesized: {error!r}")
                return False, event["last"], event.get("announcement")
        return bool(text), event["last"], event.get("announcement")

    async def synthesize(self, text, audio, first):
        trial = (first and self.shorten_first_pause and self.voice in PAUSE_TRIAL_VOICES
                 and self.tts.supports_segment_times and len(text) <= PAUSE_TRIAL_MAX_CHARS)
        if isinstance(self.tts, FliteEngine):
            command = synthesis_command(self.voice, self.rate, text, audio, segment_times=trial)
        else:
            command = self.tts.command(self.voice, self.rate, text, audio)
        options = {"stdout": asyncio.subprocess.PIPE} if trial else {}
        segments = b""
        async with child(*command, **options) as synthesis:
            if trial:
                segments, _ = await synthesis.communicate()
            if await synthesis.wait():
                raise RuntimeError(f"{self.tts.name} could not synthesize the reply")
        if segments:
            shorten_leading_pause(audio, segments)
        if self.gain < 0.999:
            await asyncio.to_thread(scale_wav, Path(audio), self.gain)

    async def speak(self):
        """Speak queued clauses. Whatever goes wrong, the turn is closed honestly."""
        self._done_sent = False
        try:
            await self.speak_clauses()
        except Exception as error:
            log(f"The reply failed: {error!r}")
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
                            try:
                                await self.play(audio, first=not started)
                                started = True
                            except Exception as error:
                                self.reply_failures += 1
                                started = started or self._start_sent
                                log(f"Skipped a clause that could not be played: {error!r}")
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

    async def play(self, audio, first):
        with wave.open(str(audio)) as wav:
            rate = wav.getframerate()
            block = rate // 25
            limit = wav.getnframes() / rate + PLAYBACK_MARGIN
            async with child(*playback_command(self.playback, audio)) as process:
                if first:
                    await self.send_audio("say.started")
                    self._start_sent = True
                deadline = time.monotonic() + limit
                while process.returncode is None:
                    if time.monotonic() > deadline:
                        raise RuntimeError("ALSA playback did not finish")
                    samples = array("h", wav.readframes(block))
                    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) if samples else 0
                    await self.send_audio("say.level", v=round(min(1.0, rms / 8500), 3))
                    await asyncio.sleep(0.04)
                if await process.wait():
                    raise RuntimeError("ALSA could not play the reply")


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


async def main():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    mind_dir = default_mind_dir()
    profile = load_profile(FACTORY_CONFIG, mind_dir, strict=False)
    voice = resolve_voice(profile.voice)
    tts, note = select_engine(os.environ.get("BEING_TTS_ENGINE"), voice_name=os.environ.get("BEING_PIPER_VOICE"))
    if note:
        print(note, flush=True)
    print(f"Playback voice: {voice} at {profile.rate:g}x", flush=True)
    _, note = engine_choice(os.environ.get("BEING_ASR_ENGINE"))
    if note:
        print(note, flush=True)
    in_lab = FW.exists()
    if (FW / "loopback" / "raw").exists():
        async with child("modprobe", "snd-aloop", "pcm_substreams=2") as module:
            if await module.wait():
                raise RuntimeError("could not load ALSA loopback")
    capture, playback = audio_devices()
    command = ["arecord", "-q", "-D", capture, "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1",
               "--buffer-time=120000", "--period-time=30000"]
    settings = load_voice_settings(mind_dir)
    mixer = Mixer(os.environ.get("BEING_ALSA_CARD") or mixer_card(playback),
                  os.environ.get("BEING_ALSA_MIXER", "Master"), mind_dir / ALSA_STATE_FILE)
    chirp_bank = ChirpBank(Path(os.environ.get("BEING_CHIRP_DIR", "/run/being/chirps")))
    recognizer = wake_recognizer(profile.wake_word)
    microphone = None
    try:
        if (FW / "microphone" / "raw").exists():
            microphone = MicrophonePort()
        await Voice(
            recognizer, profile.owner, command, playback, microphone, voice=voice, rate=profile.rate,
            shorten_first_pause=os.environ.get("BEING_VOICE_SHORTEN_FIRST_PAUSE") == "1", tts=tts,
            settings_dir=mind_dir, settings=settings, mixer=mixer, chirp_bank=chirp_bank,
            chirps=settings["chirps"] and lab_default("BEING_CHIRPS", in_lab=in_lab),
            hear_sounds=lab_default("BEING_HEAR_SOUND", in_lab=in_lab), quiet_hours=quiet_hours_check(mind_dir),
        ).run()
    finally:
        if microphone is not None:
            microphone.close()
        recognizer.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        pass
