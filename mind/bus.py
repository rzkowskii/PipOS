"""The mind's connection to the brainstem. See docs/PROTOCOL.md."""

import asyncio
import json
import os


def socket_path() -> str:
    """Where the bus socket lives. Same rule as the Rust side."""
    path = os.environ.get("BEING_BUS")
    if path:
        return path
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "being", "bus.sock")
    return f"/tmp/being-{os.environ.get('USER', 'being')}/bus.sock"


class Bus:
    """One JSON object per line over a Unix socket, reconnecting by itself.

    Events arrive on a queue. `urgent`, if set, is called for every event the
    moment it is read, so a cancel is seen even while a reply is being generated.
    """

    def __init__(self, name, subs, path=None, urgent=None):
        self.name = name
        self.subs = list(subs)
        self.path = path or socket_path()
        self.urgent = urgent
        self.on_disconnect = None
        self.connected = asyncio.Event()
        # Counts connections, so callers can tell a reconnect from the first one.
        self.generation = 0
        self._queue = asyncio.Queue()
        self._writer = None
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._close()

    def _close(self):
        was_connected = self.connected.is_set()
        self.connected.clear()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if was_connected and self.on_disconnect is not None:
            while not self._queue.empty():
                self._queue.get_nowait()
            self.on_disconnect()

    async def _run(self):
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.path)
            except OSError:
                await asyncio.sleep(0.5)
                continue
            self._writer = writer
            try:
                await self._write({"t": "hello", "name": self.name, "sub": self.subs})
                self.generation += 1
                self.connected.set()
                while self.connected.is_set():
                    line = await reader.readline()
                    if not line or not self.connected.is_set():
                        break
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or not isinstance(event.get("t"), str):
                        continue
                    if self.urgent is not None:
                        self.urgent(event)
                    self._queue.put_nowait(event)
            except (OSError, ValueError):
                pass
            finally:
                self._close()
            await asyncio.sleep(0.5)

    async def _write(self, event):
        writer = self._writer
        if writer is None:
            return
        writer.write((json.dumps(event, separators=(",", ":")) + "\n").encode())
        await writer.drain()

    async def send(self, topic, **fields):
        """Send an event. Dropped silently while the bus is down."""
        try:
            await self._write({"t": topic, **fields})
        except OSError:
            self._close()

    async def recv(self):
        return await self._queue.get()
