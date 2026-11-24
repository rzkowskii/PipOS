"""QEMU microphone transport with explicit start and stop commands."""

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path


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
# while capture is closed: the socket buffer fills and the sender waits, so a
# recorded test phrase is heard whole. It is delivered at most at real-time
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

    def _finish(self, how):
        if not self.ended:
            self.ended = True
            _log(f"The diagnostic microphone {how}; hearing silence until the voice restarts.")
