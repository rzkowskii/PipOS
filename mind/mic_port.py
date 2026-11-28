"""Microphone transports: the QEMU port, the diagnostic socket, and the queue
that keeps capture apart from decoding."""

import asyncio
import os
import threading
import time
import types
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path


# Capture apart from decoding --------------------------------------------------------
#
# The voice decodes on its event loop, and a decode can take longer than the
# pipe from arecord holds (64 KiB, about one second of 16 kHz stereo): the end
# of a dictation, the command grammar's second pass, or waiting for the
# brainstem to take a one-breath wake. arecord then overran and lost audio (20
# overruns and 12.9 s lost in one boot on the laptop). So the microphone is now
# drained as it arrives, into a bounded CaptureQueue, and the decoder takes
# frames from the queue at its own pace. arecord's pipe is read on a thread of
# its own (PipeReader), so even a decode that holds the event loop cannot stop
# it; other sources (the QEMU port, the diagnostic socket) are read by a task
# on the loop (pump). A decoder that falls the whole queue behind loses the
# oldest frames, counted; a lossless source (the diagnostic socket) waits
# instead, so its sender waits too and a test phrase is heard whole, and
# what it had not decoded when capture closes goes back to the source
# (`unread`) to be heard when capture reopens.
#
# Each frame carries the time it was captured (CaptureQueue.taken_at, on the
# voice's own clock), so the voice silences its own earcons and apology by
# when a frame was heard, not by when a lagging decoder reached it.

# How much audio may wait for the decoder.
CAPTURE_QUEUE_SECONDS = 10.0
# arecord's pipe is enlarged to this where the kernel allows it (about 16 s).
PIPE_BYTES = 1 << 20
# One read from the pipe asks for at most this much.
PIPE_READ_BYTES = 65536


@types.coroutine
def _yield():
    """Let the event loop run its other tasks once (not asyncio.sleep, which tests replace)."""
    yield


class CaptureQueue:
    """Microphone frames waiting for the decoder.

    Filled by a reader thread (`put_threadsafe`) or a task on the event loop
    (`put`, or `put_wait` for a lossless source), and read on the event loop
    (`get`). When full, `put` drops the oldest frame and counts it: a decoder
    that fell this far behind hears the newest sound rather than stale sound.
    `finish(error)` ends the stream; `get` raises the error once every frame
    before it was read. Each frame is stamped with `clock()` when it arrives
    (frames that arrive together are spaced `frame_seconds` apart, the last
    at the arrival); `taken_at` is the stamp of the frame `get` returned last.
    """

    def __init__(self, limit, loop=None, *, clock=None, frame_seconds=0.03):
        self.limit = max(1, int(limit))
        self.loop = loop or asyncio.get_running_loop()
        self.clock = clock or time.monotonic
        self.frame_seconds = frame_seconds
        self.taken_at = None
        self.frames = deque()
        self.lock = threading.Lock()
        self.ready = asyncio.Event()
        self.room = asyncio.Event()
        self.room.set()
        self.waiting = False
        self.error = None
        self.ended = False
        self.received = 0
        self.dropped = 0
        self.unreported = 0
        # The longest the queue grew, in frames: how far decoding fell behind.
        self.deepest = 0

    def __len__(self):
        with self.lock:
            return len(self.frames)

    def _add(self, frames, stamp):
        """Append under the lock; True when a waiting reader must be woken."""
        last = len(frames) - 1
        for index, frame in enumerate(frames):
            self.frames.append((stamp - (last - index) * self.frame_seconds, frame))
            self.received += 1
            if len(self.frames) > self.limit:
                self.frames.popleft()
                self.dropped += 1
                self.unreported += 1
        self.deepest = max(self.deepest, len(self.frames))
        wake, self.waiting = self.waiting, False
        return wake

    def _wake(self):
        self.ready.set()

    def put(self, frame, stamp=None):
        """Add a frame from the event loop's thread."""
        stamp = self.clock() if stamp is None else stamp
        with self.lock:
            wake = self._add((frame,), stamp)
        if wake:
            self._wake()

    async def put_wait(self, frame, stamp=None):
        """Add a frame from the event loop, waiting for room instead of dropping one."""
        stamp = self.clock() if stamp is None else stamp
        while True:
            with self.lock:
                if len(self.frames) < self.limit:
                    break
                self.room.clear()
            await self.room.wait()
        self.put(frame, stamp)

    def keep(self, frame, stamp):
        """Add a frame past the limit, dropping nothing (one a lossless reader
        held when it was cancelled, so `drain` can hand it back)."""
        with self.lock:
            self.frames.append((stamp, frame))
            self.received += 1

    def put_threadsafe(self, frames):
        """Add frames from another thread."""
        stamp = self.clock()
        with self.lock:
            wake = self._add(frames, stamp)
        if wake:
            self._call(self._wake)

    def drain(self):
        """Take every frame still waiting, oldest first (capture is closing)."""
        with self.lock:
            frames = [frame for _, frame in self.frames]
            self.frames.clear()
            self.room.set()
        return frames

    def finish(self, error):
        """The stream ended with `error` (end of file is an IncompleteReadError). Any thread."""
        with self.lock:
            if self.ended:
                return
            self.ended, self.error = True, error
            self.waiting = False
        if self._on_loop():
            self._wake()
        else:
            self._call(self._wake)

    def _on_loop(self):
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def _call(self, callback):
        try:
            self.loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass        # the loop has closed: nobody is reading any more

    async def get(self, timeout):
        """The next frame; None if none came within `timeout` seconds. Raises the stream's error at its end."""
        while True:
            with self.lock:
                if self.frames:
                    self.taken_at, frame = self.frames.popleft()
                    if len(self.frames) < self.limit:
                        self.room.set()
                    return frame
                if self.ended:
                    raise self.error
                self.waiting = True
                self.ready.clear()
            try:
                await asyncio.wait_for(self.ready.wait(), timeout)
            except TimeoutError:
                return None

    def take_dropped(self):
        """Frames dropped since the last call."""
        with self.lock:
            count, self.unreported = self.unreported, 0
        return count


class PipeReader:
    """arecord's standard output, drained on a thread of its own into a CaptureQueue.

    The thread owns the pipe's read end and closes it at end of file, which
    comes when arecord exits (the voice's only copy of the write end is closed
    as soon as arecord has started)."""

    def __init__(self, fd):
        self.fd = fd
        self.thread = None

    def enlarge(self, size=PIPE_BYTES):
        """Ask the kernel for a larger pipe (Linux only, best effort)."""
        try:
            import fcntl
        except ImportError:
            return
        setting = getattr(fcntl, "F_SETPIPE_SZ", None)
        if setting is not None:
            with suppress(OSError):
                fcntl.fcntl(self.fd, setting, size)

    def start(self, queue, size):
        """Read frames of `size` bytes into `queue` until arecord's output ends."""
        if self.thread is not None:
            raise RuntimeError("the microphone pipe is already being read")
        self.enlarge()
        self.thread = threading.Thread(target=self._run, args=(queue, size), name="being-mic-reader",
                                       daemon=True)
        self.thread.start()

    def _run(self, queue, size):
        buffer = bytearray()
        try:
            while True:
                block = os.read(self.fd, max(size, PIPE_READ_BYTES))
                if not block:
                    queue.finish(asyncio.IncompleteReadError(bytes(buffer), size))
                    return
                buffer.extend(block)
                whole = len(buffer) // size * size
                if whole:
                    queue.put_threadsafe([bytes(buffer[start:start + size]) for start in range(0, whole, size)])
                    del buffer[:whole]
        except OSError as error:
            queue.finish(error)
        except BaseException as error:  # never leave the decoder waiting on a dead thread
            queue.finish(RuntimeError(f"the microphone reader stopped: {error!r}"))
        finally:
            with suppress(OSError):
                os.close(self.fd)

    def close(self):
        """Close the pipe if no thread ever took it over."""
        if self.thread is None and self.fd is not None:
            with suppress(OSError):
                os.close(self.fd)
            self.fd = None


async def pump(stream, queue, size, *, lossless=False):
    """Read `stream` (anything with an async `readexactly`) into `queue` until it ends.

    A lossless stream waits while the queue is full; any other drops its
    oldest frame. A lossless frame read but not yet queued when the pump is
    cancelled is queued anyway, so the caller can drain and unread it. The
    stream's error, end of file included, ends the queue."""
    try:
        while True:
            frame = await stream.readexactly(size)
            stamp = queue.clock()
            if lossless:
                try:
                    await queue.put_wait(frame, stamp)
                except asyncio.CancelledError:
                    queue.keep(frame, stamp)
                    raise
            else:
                queue.put(frame, stamp)
            # A stream that returns at once must not hold the loop.
            await _yield()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        queue.finish(error)


class MicrophonePort:
    def __init__(self):
        for name in Path("/sys/class/virtio-ports").glob("*/name"):
            if name.read_text().strip() == "org.beingos.microphone":
                self.fd = os.open(Path("/dev") / name.parent.name, os.O_RDWR | os.O_NONBLOCK)
                self.audio = bytearray()
                break
        else:
            raise RuntimeError("QEMU microphone port is missing")

    def close(self):
        os.close(self.fd)

    @asynccontextmanager
    async def recording(self):
        self.audio.clear()
        # Drop any bytes buffered before the previous stop reached the host.
        try:
            os.read(self.fd, 65536)
        except BlockingIOError:
            pass
        os.write(self.fd, b"start\n")
        try:
            yield self
        finally:
            # A vanished host must not hide the error that ended the recording.
            with suppress(OSError):
                os.write(self.fd, b"stop\n")

    async def readexactly(self, size):
        loop = asyncio.get_running_loop()
        while len(self.audio) < size:
            try:
                block = os.read(self.fd, size - len(self.audio))
            except BlockingIOError:
                ready = loop.create_future()

                def readable():
                    if not ready.done():
                        ready.set_result(None)

                loop.add_reader(self.fd, readable)
                try:
                    await ready
                finally:
                    loop.remove_reader(self.fd)
            else:
                if not block:
                    raise asyncio.IncompleteReadError(bytes(self.audio), size)
                self.audio.extend(block)
        result = bytes(self.audio)
        self.audio.clear()
        return result


# The diagnostic microphone ----------------------------------------------------------
#
# pip-diag (mind/diag.py) restarts the voice with BEING_DIAG_MIC naming a Unix
# stream socket it listens on. The voice then reads raw S16_LE 16 kHz mono
# audio from it instead of opening ALSA capture, through the same gain,
# endpointing, wake and recognition as the microphone. Audio is never dropped
# while capture is closed: the socket buffer fills and the sender waits, and
# audio read but not yet decoded when capture closes is handed back (`unread`)
# to be heard first when it reopens, so a recorded test phrase is heard whole. It is delivered at most at real-time
# speed. After the sender closes, digital silence follows, at real-time
# speed, until the voice is restarted.

DIAG_ENV = "BEING_DIAG_MIC"
DIAG_CONNECT_SECONDS = 10.0
DIAG_RETRY_SECONDS = 0.2
DIAG_RATE = 16000
DIAG_SAMPLE_BYTES = 2
# Audio may run ahead of real time by this much before reads wait (a burst
# after capture reopens), and a reader this far behind starts the clock again.
DIAG_LEAD_SECONDS = 0.25
DIAG_RESYNC_SECONDS = 1.0


def _log(message):
    print(message, flush=True)


class DiagnosticMicrophone:
    """A Unix socket that stands in for the microphone, for diagnostics."""

    # Never drop its audio: when the decoder falls behind, the sender waits.
    lossless = True

    def __init__(self, sock, path, clock=None, sleep=None):
        self.sock = sock
        self.path = str(path)
        self.description = f"the diagnostic microphone {self.path}"
        self.audio = bytearray()
        self.ended = False
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep or asyncio.sleep
        self._due = None

    @classmethod
    async def connect(cls, path, timeout=DIAG_CONNECT_SECONDS, interval=DIAG_RETRY_SECONDS):
        """Connect to `path`, trying every `interval` seconds for `timeout`. Returns None if it never answers."""
        import socket

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = None
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                await loop.sock_connect(sock, str(path))
            except OSError as error:
                sock.close()
                last = error
            else:
                _log(f"diagnostic microphone connected on {path}")
                return cls(sock, path)
            if loop.time() + interval > deadline:
                _log(f"The diagnostic microphone {path} did not answer within {timeout:g} s "
                     f"({last}). Using the normal microphone.")
                return None
            await asyncio.sleep(interval)

    def close(self):
        self.sock.close()

    @asynccontextmanager
    async def recording(self):
        # Nothing buffered is dropped: what the sender wrote is still to be heard.
        self._due = None
        yield self

    async def _pace(self, size):
        """Wait until `size` bytes of audio are due at real-time speed."""
        now = self._clock()
        if self._due is None or now - self._due > DIAG_RESYNC_SECONDS:
            self._due = now
        wait = self._due - DIAG_LEAD_SECONDS - now
        if wait > 0:
            await self._sleep(wait)
        self._due += size / (DIAG_RATE * DIAG_SAMPLE_BYTES)

    async def readexactly(self, size):
        await self._pace(size)
        loop = asyncio.get_running_loop()
        fd = self.sock.fileno()
        while len(self.audio) < size and not self.ended:
            try:
                block = self.sock.recv(max(size - len(self.audio), 4096))
            except BlockingIOError:
                ready = loop.create_future()

                def readable():
                    if not ready.done():
                        ready.set_result(None)

                loop.add_reader(fd, readable)
                try:
                    await ready
                finally:
                    loop.remove_reader(fd)
            except OSError as error:
                self._finish(f"failed ({error})")
            else:
                if not block:
                    self._finish("closed")
                else:
                    self.audio.extend(block)
        if len(self.audio) < size:
            # The sender is gone: what is left, then silence, as a muted microphone would give.
            result = bytes(self.audio) + bytes(size - len(self.audio))
            self.audio.clear()
            return result
        result = bytes(self.audio[:size])
        del self.audio[:size]
        return result

    def unread(self, data):
        """Put audio back in front of what is still to be read."""
        if data:
            self.audio[:0] = data

    def _finish(self, how):
        if not self.ended:
            self.ended = True
            _log(f"The diagnostic microphone {how}; hearing silence until the voice restarts.")
