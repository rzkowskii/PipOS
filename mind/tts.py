"""Local speech commands shared by the voice service and setup."""

import os
import re
import stat
import struct
import subprocess
import tempfile
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
