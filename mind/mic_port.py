"""QEMU microphone transport with explicit start and stop commands."""

import asyncio
import os
from contextlib import asynccontextmanager
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
