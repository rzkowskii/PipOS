"""The local, half-duplex voice organ. Audio stays outside the event bus."""

import asyncio
import math
import os
import signal
import tempfile
import wave
from array import array
from contextlib import asynccontextmanager
from pathlib import Path

from .bus import Bus
from .mic_port import MicrophonePort
from .profile import DEFAULT_VOICE, FACTORY_CONFIG, VOICES, default_mind_dir, factory_profile, load_profile
from .recognition import FRAME_BYTES, RATE, Recognizer
from .tts import available_voices, playback_command, shorten_leading_pause, synthesis_command

FW = Path("/sys/firmware/qemu_fw_cfg/by_name/opt/beingos")
TALKING = ("thinking", "speaking")
ATTENDING = ("attending", "listening")
HEARING = (*ATTENDING, "idle", "drowsy", "asleep")
PAUSE_TRIAL_VOICES = frozenset(("slt", "rms", "awb"))
PAUSE_TRIAL_MAX_CHARS = 512


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


class Voice:
    def __init__(self, recognizer, speaker, capture, playback, mic_port=None, *,
                 voice=DEFAULT_VOICE, rate=1.0, shorten_first_pause=False):
        self.recognizer = recognizer
        self.speaker = speaker
        self.capture_command = capture
        self.mic_port = mic_port
        self.playback = playback
        self.voice = voice
        self.rate = rate
        self.shorten_first_pause = shorten_first_pause
        self.bus = Bus("voice", ("body.lid", "state.changed", "voice.mute", "say.request", "say.stop"),
                       urgent=self.on_event_received)
        self.bus.on_disconnect = self.connection_lost
        self.state = "waking"
        self.muted = True
        self.lid_open = False
        self.lid_seq = None
        self._latest_lid = None
        self.capture = None
        self.reply = None
        self.clauses = asyncio.Queue()

    async def run(self):
        await self.bus.start()
        try:
            async with asyncio.TaskGroup() as tasks:
                self.tasks = tasks
                try:
                    while True:
                        await self.handle(await self.bus.recv())
                finally:
                    await self.stop_reply()
                    await self.stop_capture()
        finally:
            await self.bus.stop()

    def disarm(self):
        """Close audio immediately, including while a queued event is handled."""
        self.lid_open = False
        self.lid_seq = None
        self._latest_lid = None
        for task in (self.capture, self.reply):
            if task is not None and not task.cancelling():
                task.cancel()
        self.clear_clauses()

    def connection_lost(self):
        self.disarm()
        self.state = "waking"
        self.muted = True

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
            elif event is self._latest_lid and not self.lid_open:
                await self.stop_capture()
                await self.stop_reply()
                if event is self._latest_lid:
                    self.lid_open = True
                    self.lid_seq = event.get("seq")
                    await self.sync_capture()
        elif topic == "state.changed":
            self.state = event["state"]
            self.muted = event["muted"]
            await self.sync_capture()
        elif topic == "voice.mute":
            self.muted = event["on"]
            await self.sync_capture()
        elif topic == "say.stop":
            await self.stop_reply()
        elif topic == "say.request" and self.lid_open and self.state in TALKING:
            await self.clauses.put(event)
            if self.reply is None or self.reply.done():
                await self.stop_capture()
                if self.lid_open:
                    self.reply = self.tasks.create_task(self.speak())

    async def send_audio(self, topic, **fields):
        if self.lid_open:
            if self.lid_seq is not None:
                fields["lid_seq"] = self.lid_seq
            await self.bus.send(topic, **fields)

    async def sync_capture(self):
        if not self.lid_open or self.muted or self.state not in HEARING:
            await self.stop_capture()
        elif (self.capture is None or self.capture.done()) and (self.reply is None or self.reply.done()):
            self.capture = self.tasks.create_task(self.listen())

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
                    for topic, fields in self.recognizer.feed(frame, self.state in ATTENDING):
                        if topic == "hear.final":
                            fields["speaker"] = self.speaker
                        await self.send_audio(topic, **fields)
            finally:
                self.recognizer.stop_capture()

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
        event = await self.clauses.get()
        if text := event["text"].strip():
            trial = (first and self.shorten_first_pause and self.voice in PAUSE_TRIAL_VOICES
                     and len(text) <= PAUSE_TRIAL_MAX_CHARS)
            command = synthesis_command(self.voice, self.rate, text, audio, segment_times=trial)
            options = {"stdout": asyncio.subprocess.PIPE} if trial else {}
            segments = b""
            async with child(*command, **options) as synthesis:
                if trial:
                    segments, _ = await synthesis.communicate()
                if await synthesis.wait():
                    raise RuntimeError("Flite could not synthesize the reply")
            if segments:
                shorten_leading_pause(audio, segments)
        return bool(text), event["last"], event.get("announcement")

    async def speak(self):
        started = False
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
                            await self.play(audio, first=not started)
                            started = True
                        if last:
                            # Release the capture gate before the brainstem returns to attending.
                            self.reply = None
                            fields = {"final": True}
                            if started and isinstance(announcement, str) and announcement:
                                fields["announcement"] = announcement
                            await self.send_audio("say.done", **fields)
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

    async def play(self, audio, first):
        with wave.open(str(audio)) as wav:
            block = wav.getframerate() // 25
            async with child(*playback_command(self.playback, audio)) as process:
                if first:
                    await self.send_audio("say.started")
                while process.returncode is None:
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


async def main():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    profile = load_profile(FACTORY_CONFIG, default_mind_dir(), strict=False)
    voice = resolve_voice(profile.voice)
    print(f"Playback voice: {voice} at {profile.rate:g}x", flush=True)
    if (FW / "loopback" / "raw").exists():
        async with child("modprobe", "snd-aloop", "pcm_substreams=2") as module:
            if await module.wait():
                raise RuntimeError("could not load ALSA loopback")
    capture, playback = audio_devices()
    command = ["arecord", "-q", "-D", capture, "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1",
               "--buffer-time=120000", "--period-time=30000"]
    recognizer = wake_recognizer(profile.wake_word)
    microphone = None
    try:
        if (FW / "microphone" / "raw").exists():
            microphone = MicrophonePort()
        await Voice(
            recognizer, profile.owner, command, playback, microphone, voice=voice, rate=profile.rate,
            shorten_first_pause=os.environ.get("BEING_VOICE_SHORTEN_FIRST_PAUSE") == "1",
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
