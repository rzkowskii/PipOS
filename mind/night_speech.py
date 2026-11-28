"""Prepare a few Piper clauses in a disposable, cancellable night process."""

import json
import os
import stat
import struct
import sys
from pathlib import Path

from .reply import Confirm, Reply, clean, is_guest
from .storage import atomic_write
from .tts import WavCache

MAX_CLAUSES = 8
MAX_CHARS = 180
MAX_JOB_BYTES = 12_288
WORK_SECONDS = 16
CACHE_SECONDS = 16 * 3600
CONSENT_FILE = "night-speech-enabled"
_SIZE = struct.Struct("<I")
_ON = "prepare my speech overnight"
_OFF = "stop preparing my speech overnight"
_STATUS = "is overnight speech preparation on"


def enabled(state_dir):
    if state_dir is None:
        return False
    try:
        info = (Path(state_dir) / CONSENT_FILE).lstat()
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077
    except OSError:
        return False


def may_match(text):
    return clean(text).casefold() in (_ON, _OFF, _STATUS)


async def handle(text, ctx):
    action = clean(text, ctx.name).casefold()
    if action not in (_ON, _OFF, _STATUS):
        return None
    if is_guest(ctx) or ctx.speaker != ctx.owner:
        return Reply("[neutral] Only my owner can change overnight speech preparation.")
    current = enabled(ctx.state_dir)
    if action == _STATUS:
        return Reply("[neutral] Overnight speech preparation is " + ("on." if current else "off."))
    want = action == _ON
    body = ctx.current_body() if ctx.current_body else None
    if not body or (want and body.get("night_speech_available") is not True):
        return Reply("[neutral] I can prepare speech after my persistent installation is ready.")
    if want == current:
        return Reply("[neutral] Overnight speech preparation is already " + ("on." if want else "off."))

    async def confirm():
        body = ctx.current_body() if ctx.current_body else None
        if not body or (want and body.get("night_speech_available") is not True):
            return Reply("[neutral] I couldn't change that in this session.")
        marker = Path(ctx.state_dir) / CONSENT_FILE
        try:
            if want:
                atomic_write(marker, "1\n", mode=0o600)
            else:
                marker.unlink(missing_ok=True)
        except OSError:
            return Reply("[concern] I couldn't save that setting.")
        await ctx.send("voice.clear_prepared")
        return Reply("[neutral] Overnight speech preparation is now " + ("on." if want else "off."))

    text = ("Prepare parts of your morning brief, reminders and night drafts in memory while I sleep?"
            if want else "Turn off overnight speech preparation and clear the prepared audio?")
    return Reply(f"[neutral] {text}", confirm=Confirm(confirm, strict=True))


def eligible_text(text):
    return (isinstance(text, str) and 1 <= len(text) <= MAX_CHARS
            and text == text.strip() and text.isprintable() and any(ch.isalnum() for ch in text))


def _render(engine, text, rate):
    """Give the native WAV writer an anonymous descriptor with no saved path."""
    fd = os.memfd_create("pip-prepared-clause", os.MFD_CLOEXEC)
    try:
        engine.synthesize_blocking(text, f"/proc/self/fd/{fd}", rate)
        size = os.fstat(fd).st_size
        return os.pread(fd, size, 0) if 0 < size <= WavCache.MAX_CLIP else b""
    except Exception:
        return b""
    finally:
        os.close(fd)


def prepare(output, request):
    from .tts_sherpa import PiperEngine

    clauses = request["clauses"]
    if not isinstance(clauses, list) or not 1 <= len(clauses) <= MAX_CLAUSES or not all(map(eligible_text, clauses)):
        return False
    rate = request["rate"]
    if type(rate) not in (int, float) or not 0.25 <= rate <= 4.0:
        return False
    engine = PiperEngine(request["voice_dir"], library=request["library"],
                         phonemizer_library=request["phonemizer_library"], threads=1)
    engine.quiet = True
    for text in clauses:
        clip = _render(engine, text, rate)
        output.write(_SIZE.pack(len(clip)))
        output.write(clip)
    return True


async def read_clips(stream, count):
    clips = []
    for _ in range(count):
        size, = _SIZE.unpack(await stream.readexactly(_SIZE.size))
        if size > WavCache.MAX_CLIP:
            raise ValueError("oversized prepared clip")
        clips.append(await stream.readexactly(size))
    if await stream.read(1):
        raise ValueError("extra prepared audio")
    return clips


def main():
    if len(sys.argv) != 1:
        return 2
    try:
        os.nice(10)
        raw = sys.stdin.buffer.read(MAX_JOB_BYTES + 1)
        if len(raw) > MAX_JOB_BYTES:
            return 2
        # Keep native library output off the private binary pipe.
        with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as output, open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
            return 0 if prepare(output, json.loads(raw)) else 2
    except Exception:
        # The foreground voice can synthesize a miss. The input may be a
        # private reminder or excerpt, so never include it in an error log.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
