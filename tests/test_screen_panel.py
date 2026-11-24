import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mind.local_screen import GATE_REQUESTS, serve_panel


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

    async def test_setup_screen_keys_reach_the_bus_as_the_faces(self):
        # The names being-console.c writes for the top row (media_request, and
        # F9 / F10), each sent on once as the face would send it.
        expected = [
            ("mute", ("key.mute", {})),
            ("recover", ("key.panel_recover", {})),
            ("brightness-up", ("key.brightness", {"dir": "up"})),
            ("brightness-down", ("key.brightness", {"dir": "down"})),
            ("kbd-light-up", ("key.kbd_light", {"dir": "up"})),
            ("kbd-light-down", ("key.kbd_light", {"dir": "down"})),
            ("kbd-light-toggle", ("key.kbd_light", {"dir": "toggle"})),
            ("volume-up", ("key.volume", {"dir": "up"})),
            ("volume-down", ("key.volume", {"dir": "down"})),
            ("speaker-mute", ("key.mute_speaker", {})),
        ]
        self.assertEqual([name for name, _, _ in GATE_REQUESTS], [name for name, _ in expected])
        console = (Path(__file__).resolve().parents[1] / "os/package/being/being-console.c").read_text()
        for name, _ in expected:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', console)
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            stop = threading.Event()
            bus = PanelBus()
            with patch("mind.local_screen.Bus", return_value=bus):
                panel = asyncio.create_task(serve_panel(directory, stop))
                try:
                    await self.until(bus.connected.is_set)
                    for name, event in expected:
                        (directory / name).touch()
                        await self.until(lambda: len(bus.sent) and bus.sent[-1] == event)
                        self.assertFalse((directory / name).exists())
                    await asyncio.sleep(0.15)
                    self.assertEqual(bus.sent, [event for _, event in expected])
                    # A request left while the bus is away waits for it.
                    bus.connected.clear()
                    (directory / "volume-up").touch()
                    await asyncio.sleep(0.15)
                    self.assertTrue((directory / "volume-up").exists())
                    bus.connected.set()
                    await self.until(lambda: not (directory / "volume-up").exists())
                    self.assertEqual(bus.sent[-1], ("key.volume", {"dir": "up"}))
                finally:
                    stop.set()
                    await panel

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0.01)
