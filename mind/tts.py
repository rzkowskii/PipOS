"""Local speech commands shared by the voice service and setup."""

import math
import os
import random
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import wave
from array import array
from contextlib import suppress
from pathlib import Path

from .profile import VOICES

MAX_SEGMENT_METADATA_BYTES = 16 * 1024
_MAX_WAV_BYTES = 2 * 1024 * 1024
_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
_SEGMENT = re.compile(rb"([a-z][a-z0-9]{0,7}):(0|[1-9][0-9]{0,2})\.([0-9]{3})")


def available_voices():
    """Find linked voices, or return None if Flite could not report its inventory."""
    try:
        result = subprocess.run(["flite", "-lv"], capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(result.stdout.split()).intersection(VOICES)


def synthesis_command(voice, rate, text, output, *, segment_times=False):
    command = ["flite", "-voice", voice]
    if rate != 1:
        # Flite measures duration and KAL16 starts with a 1.1 duration stretch.
        base_duration = 1.1 if voice == "kal16" else 1.0
        command.extend(["--setf", f"duration_stretch={base_duration / rate:.6g}"])
    if segment_times:
        command.append("-psdur")
    return command + ["-t", text, "-o", str(output)]


def _modeled_times(segment_metadata):
    if len(segment_metadata) > MAX_SEGMENT_METADATA_BYTES:
        return None
    lines = segment_metadata.strip().splitlines()
    if len(lines) != 1:
        return None
    end_ms = 0
    first_spoken_ms = None
    for index, token in enumerate(lines[0].split()):
        match = _SEGMENT.fullmatch(token)
        if match is None:
            return None
        phone, seconds, milliseconds = match.groups()
        next_end_ms = int(seconds) * 1000 + int(milliseconds)
        if next_end_ms <= end_ms or (index == 0 and phone != b"pau"):
            return None
        if first_spoken_ms is None and phone != b"pau":
            first_spoken_ms = end_ms
        end_ms = next_end_ms
    if first_spoken_ms is None or phone != b"pau":
        return None
    return first_spoken_ms, end_ms


def _pcm_rate(data):
    (riff, riff_size, wave, fmt, fmt_size, encoding, channels, rate, byte_rate,
     align, bits, pcm, pcm_size) = _WAV_HEADER.unpack_from(data)
    if (
        (riff, wave, fmt, pcm) != (b"RIFF", b"WAVE", b"fmt ", b"data")
        or (fmt_size, encoding, channels, align, bits) != (16, 1, 1, 2, 16)
        or riff_size != len(data) - 8
        or pcm_size != len(data) - _WAV_HEADER.size
        or pcm_size % 2
        or not 8000 <= rate <= 48000
        or byte_rate != rate * 2
    ):
        return None
    return rate


def shorten_leading_pause(audio: Path, segment_metadata: bytes) -> bool:
    """Trim a Flite WAV's leading pause using its matching segment end times."""
    times = _modeled_times(segment_metadata)
    if times is None:
        return False
    try:
        info = audio.lstat()
        if not stat.S_ISREG(info.st_mode) or not _WAV_HEADER.size < info.st_size <= _MAX_WAV_BYTES:
            return False
        with audio.open("rb") as source:
            data = source.read(info.st_size + 1)
    except OSError:
        return False
    if len(data) != info.st_size or (rate := _pcm_rate(data)) is None:
        return False

    first_spoken_ms, end_ms = times
    frames = (len(data) - _WAV_HEADER.size) // 2
    if abs(end_ms * rate - frames * 1000) > 6 * rate:
        return False
    first_spoken = first_spoken_ms * rate // 1000
    keep = (40 * rate + 999) // 1000
    first = first_spoken - keep
    if first <= 0 or first_spoken >= frames:
        return False

    # The fade is entirely inside the retained pause. Speech PCM is copied verbatim.
    pcm = bytearray(memoryview(data)[_WAV_HEADER.size + first * 2:])
    fade = 5 * rate // 1000
    for frame in range(fade):
        offset = frame * 2
        sample, = struct.unpack_from("<h", pcm, offset)
        struct.pack_into("<h", pcm, offset, round(sample * frame / (fade - 1)))

    header = bytearray(data[:_WAV_HEADER.size])
    struct.pack_into("<I", header, 4, len(header) + len(pcm) - 8)
    struct.pack_into("<I", header, _WAV_HEADER.size - 4, len(pcm))

    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=audio.parent, prefix=".flite-", delete=False) as target:
            pending = Path(target.name)
            os.fchmod(target.fileno(), stat.S_IMODE(info.st_mode))
            target.write(header)
            target.write(pcm)
        os.replace(pending, audio)
    except OSError:
        return False
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink(missing_ok=True)
    return True


def playback_command(playback, audio):
    return ["aplay", "-q", "-D", playback, str(audio)]


# Software volume ---------------------------------------------------------------

def volume_gain(level):
    """Map a 0-100 owner level to a linear sample gain on a rough loudness curve."""
    level = max(0, min(100, int(level)))
    return (level / 100) ** 2


def scale_wav(audio: Path, gain: float) -> bool:
    """Scale a mono 16-bit WAV in place. Used only when no hardware mixer answers."""
    if gain >= 0.999:
        return False
    try:
        with wave.open(str(audio)) as source:
            params = source.getparams()
            if params.sampwidth != 2:
                return False
            samples = array("h", source.readframes(params.nframes))
    except (OSError, EOFError, wave.Error):
        return False
    for index, sample in enumerate(samples):
        samples[index] = int(sample * gain)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=audio.parent, prefix=".gain-", suffix=".wav", delete=False) as target:
            pending = Path(target.name)
        with wave.open(str(pending), "wb") as output:
            output.setparams(params)
            output.writeframes(samples.tobytes())
        os.replace(pending, audio)
    except OSError:
        return False
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink(missing_ok=True)
    return True


# Chirps and earcons ------------------------------------------------------------
#
# Every sound is made here from sine partials with the standard library, so no
# recorded asset ships and each family has a few jittered variants that keep it
# from sounding like a notification tone.

CHIRP_RATE = 22050
CHIRP_VARIANTS = 4
# Peak amplitude at full gain. Earcons sit well under speech loudness.
CHIRP_PEAK = 9000

# name: list of notes (start_hz, end_hz, seconds, gap_after, level)
_CHIRP_SHAPES = {
    "ears": [(420, 720, 0.11, 0.0, 0.7)],
    "heard": [(660, 700, 0.06, 0.03, 0.8), (880, 940, 0.07, 0.0, 0.9)],
    "purr": [(200, 190, 0.45, 0.0, 0.45)],
    "yes": [(523, 530, 0.08, 0.02, 0.8), (659, 670, 0.11, 0.0, 0.9)],
    "no": [(440, 430, 0.09, 0.03, 0.8), (349, 330, 0.14, 0.0, 0.8)],
    "sigh": [(520, 300, 0.62, 0.0, 0.5)],
    "thanks": [(523, 530, 0.07, 0.02, 0.7), (659, 665, 0.07, 0.02, 0.8), (784, 800, 0.14, 0.0, 0.9)],
    "bell": [(880, 880, 0.9, 0.0, 1.0)],
    "trouble": [(330, 320, 0.14, 0.05, 0.8), (247, 233, 0.22, 0.0, 0.8)],
}
CHIRPS = frozenset(_CHIRP_SHAPES)
# Bells ring with partials and a long decay; purrs warble.
_BELL_PARTIALS = ((1.0, 1.0), (2.0, 0.45), (3.01, 0.25), (4.2, 0.12))


def chirp_samples(name, variant=0, *, gain=1.0, rate=CHIRP_RATE):
    """Render one variant of a named earcon as 16-bit samples."""
    if name not in _CHIRP_SHAPES:
        raise ValueError(f"unknown chirp {name!r}")
    jitter = random.Random(f"{name}:{variant}")
    pitch = 2 ** (jitter.uniform(-1.2, 1.2) / 12)  # up to a semitone either way
    tempo = jitter.uniform(0.9, 1.1)
    samples = array("h")
    peak = CHIRP_PEAK * max(0.0, min(1.0, gain))
    for start_hz, end_hz, seconds, gap, level in _CHIRP_SHAPES[name]:
        count = max(1, int(seconds * tempo * rate))
        attack = max(1, int(0.008 * rate))
        phase = 0.0
        for index in range(count):
            position = index / count
            frequency = (start_hz + (end_hz - start_hz) * position) * pitch
            phase += 2 * math.pi * frequency / rate
            if name == "bell":
                envelope = math.exp(-5.0 * index / rate) * min(1.0, index / attack)
                value = sum(weight * math.sin(phase * ratio) for ratio, weight in _BELL_PARTIALS) / 1.8
            else:
                # Raised-cosine release keeps each note click-free.
                envelope = min(1.0, index / attack) * 0.5 * (1 + math.cos(math.pi * position))
                if name == "purr":
                    envelope *= 0.6 + 0.4 * math.sin(2 * math.pi * 22 * index / rate)
                value = math.sin(phase) + 0.18 * math.sin(2 * phase)
            samples.append(int(max(-32767, min(32767, peak * level * envelope * value))))
        samples.extend([0] * int(gap * tempo * rate))
    return samples


def write_wav(path: Path, samples, rate=CHIRP_RATE):
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".chirp-", suffix=".wav", delete=False) as target:
            pending = Path(target.name)
        with wave.open(str(pending), "wb") as output:
            output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            output.writeframes(samples.tobytes())
        os.replace(pending, path)
        pending = None
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink(missing_ok=True)


class ChirpBank:
    """Earcon WAVs rendered on first use and cached in a private RAM directory."""

    def __init__(self, directory: Path, variants=CHIRP_VARIANTS):
        self.directory = Path(directory)
        self.variants = variants
        self._turn = random.Random()

    def path(self, name, gain=1.0, variant=None):
        if variant is None:
            variant = self._turn.randrange(self.variants)
        level = max(0, min(100, round(gain * 100)))
        target = self.directory / f"{name}-{variant}-g{level}.wav"
        if not target.is_file():
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Keep one gain per sound in RAM: a volume change replaces, never piles up.
            for stale in self.directory.glob(f"{name}-{variant}-g*.wav"):
                if stale != target:
                    with suppress(OSError):
                        stale.unlink()
            write_wav(target, chirp_samples(name, variant, gain=level / 100))
        return target


# Text-to-speech engines ---------------------------------------------------------
#
# Flite is the working default. Piper voices through the sherpa-onnx offline TTS
# program are prepared for task 5.3: the engine is chosen only when its program
# and every model file are present, so a missing download falls back to Flite.

PIPER_PROGRAM = "sherpa-onnx-offline-tts"
PIPER_VOICES = Path("/models/voice/piper")


class FliteEngine:
    name = "flite"
    supports_segment_times = True

    def available(self):
        return shutil.which("flite") is not None

    def command(self, voice, rate, text, output, *, segment_times=False):
        return synthesis_command(voice, rate, text, output, segment_times=segment_times)


class PiperEngine:
    """One-shot Piper (VITS) synthesis through sherpa-onnx, on one thread."""

    name = "piper"
    supports_segment_times = False

    def __init__(self, voice_dir: Path, program=PIPER_PROGRAM):
        self.voice_dir = Path(voice_dir)
        self.program = program

    def files(self):
        models = sorted(self.voice_dir.glob("*.onnx"))
        return {
            "model": models[0] if len(models) == 1 else None,
            "tokens": self.voice_dir / "tokens.txt",
            "data": self.voice_dir / "espeak-ng-data",
        }

    def available(self):
        files = self.files()
        return (shutil.which(self.program) is not None and files["model"] is not None
                and files["tokens"].is_file() and files["data"].is_dir())

    def command(self, voice, rate, text, output, *, segment_times=False):
        files = self.files()
        # A leading dash would be read as an option by the program's parser.
        spoken = text.lstrip("-").strip() or "."
        return [
            self.program, f"--vits-model={files['model']}", f"--vits-tokens={files['tokens']}",
            f"--vits-data-dir={files['data']}", "--num-threads=1",
            f"--vits-length-scale={1 / rate:.4g}", f"--output-filename={output}", spoken,
        ]


def select_engine(name=None, *, voice_name=None, piper_root=PIPER_VOICES):
    """Pick the TTS engine. Unknown or incomplete choices fall back to Flite."""
    name = (name or "flite").strip().lower()
    if name == "piper":
        engine = PiperEngine(Path(piper_root) / (voice_name or "default"))
        if engine.available():
            return engine, None
        return FliteEngine(), "Piper voice files are not installed. Using Flite."
    if name != "flite":
        return FliteEngine(), f"Unknown speech engine {name!r}. Using Flite."
    return FliteEngine(), None
