import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mind.local_screen import serve_panel


class PanelBus:
    def __init__(self, *_args, **_kwargs):
        self.connected = asyncio.Event()
        self.events = asyncio.Queue()
        self.sent = []

    async def start(self):
        self.connected.set()

    async def stop(self):
        self.connected.clear()

    async def recv(self):
        return await self.events.get()

    async def send(self, topic, **payload):
        self.sent.append((topic, payload))


class PanelHandoff(unittest.IsolatedAsyncioTestCase):
    async def test_visible_frame_must_match_lid_sequence(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            stop = threading.Event()
            bus = PanelBus()
            with patch("mind.local_screen.Bus", return_value=bus):
                panel = asyncio.create_task(serve_panel(directory, stop))
                try:
                    await bus.events.put({"t": "body.lid", "closed": False, "seq": 0})
                    await self.until(lambda: (directory / "show").is_file())
                    self.assertEqual((directory / "show").read_text(), "0\n")
                    (directory / "presented").write_text("1\n")
                    await bus.events.put({"t": "probe"})
                    await asyncio.sleep(0.15)
                    self.assertEqual(bus.sent, [])

                    (directory / "presented").write_text("0\n")
                    await self.until(lambda: len(bus.sent) == 1)
                    self.assertEqual(bus.sent, [("panel.ready", {"seq": 0})])

                    (directory / "mute").touch()
                    await self.until(lambda: len(bus.sent) == 2)
                    self.assertEqual(bus.sent[-1], ("key.mute", {}))
                    await bus.events.put({"t": "body.lid", "closed": True, "seq": 1})
                    await self.until(lambda: not (directory / "show").exists())
                finally:
                    stop.set()
                    await panel

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0.01)
