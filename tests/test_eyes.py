"""Verify camera policy through a local bus and a disposable detector child."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind.eyes import Eyes, detection, private_opt_in


DETECTOR = """
import os
import sys
import time

with open(sys.argv[1], 'a') as starts:
    starts.write(f'{os.getpid()}\\n')
if len(sys.argv) > 2:
    sys.exit(1)
while True:
    print('{"present":true,"x":-0.2,"y":0.4}', flush=True)
    time.sleep(0.04)
"""


class CameraMetadata(unittest.TestCase):
    def test_accepts_only_finite_normalized_presence(self):
        self.assertEqual(detection(b'{"present":true,"x":-1,"y":1}'), {"present": True, "x": -1, "y": 1})
        self.assertEqual(detection(b'{"present":false}'), {"present": False})
        for data in (b'{"present":0}', b'{"present":true,"x":NaN,"y":0}', b'{"present":true,"x":2,"y":0}',
                     b'{"present":true,"x":true,"y":0}', b'{"present":false,"image":"anything"}', b'[]'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                detection(data)

    def test_opt_in_requires_a_private_regular_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "camera.enabled"
            self.assertFalse(private_opt_in(marker))
            marker.touch(mode=0o600)
            self.assertTrue(private_opt_in(marker))
            marker.chmod(0o644)
            self.assertFalse(private_opt_in(marker))
            marker.unlink()
            marker.symlink_to(Path(temp) / "absent")
            self.assertFalse(private_opt_in(marker))
            marker.unlink()
            marker.mkdir(mode=0o700)
            self.assertFalse(private_opt_in(marker))


class CameraPolicy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory(prefix="being-eyes-", dir="/tmp"))
        self.marker = Path(work) / "camera.enabled"
        self.starts = Path(work) / "starts"
        self.bus_path = Path(work) / "bus.sock"
        self.clients = asyncio.Queue()
        self.writers = []

        def connect(reader, writer):
            self.writers.append(writer)
            self.clients.put_nowait((reader, writer))

        self.server = await asyncio.start_unix_server(connect, self.bus_path)
        self.enterContext(patch("mind.eyes.POLICY_POLL", 0.02))
        self.eyes_task = None
        self.reader = self.writer = None

    async def asyncTearDown(self):
        if self.eyes_task is not None:
            self.eyes_task.cancel()
            await asyncio.gather(self.eyes_task, return_exceptions=True)
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        for pid in self.started_pids():
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def start(self, *args):
        command = (sys.executable, "-c", DETECTOR, str(self.starts), *args)
        self.eyes = Eyes(self.marker, command, bus_path=self.bus_path)
        self.eyes_task = asyncio.create_task(self.eyes.run())
        await self.accept()

    async def accept(self):
        self.reader, self.writer = await asyncio.wait_for(self.clients.get(), 2)
        hello = await self.until_event("hello")
        self.assertEqual(hello["name"], "eyes")

    async def until_event(self, topic, **fields):
        async with asyncio.timeout(2):
            while line := await self.reader.readline():
                event = json.loads(line)
                if event["t"] == topic and all(event.get(key) == value for key, value in fields.items()):
                    return event
        self.fail(f"no {topic} with {fields!r}")

    async def send(self, *events):
        self.writer.write(b"".join((json.dumps(event) + "\n").encode() for event in events))
        await self.writer.drain()

    async def snapshot(self, *, lid_seq=1, closed=False, camera_seq=1, allowed=True):
        await self.send({"t": "body.lid", "closed": closed, "seq": lid_seq},
                        {"t": "state.changed", "state": "idle"}, {"t": "stem.beat"},
                        {"t": "camera.policy", "allowed": allowed, "seq": camera_seq})

    def started_pids(self):
        return [int(line) for line in self.starts.read_text().splitlines()] if self.starts.exists() else []

    async def wait_for_stop(self, pid):
        async with asyncio.timeout(2):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                await asyncio.sleep(0.02)

    async def test_private_opt_in_reopen_and_revocation(self):
        await self.start()
        await self.until_event("camera.request", enabled=False)
        await self.snapshot()
        await asyncio.sleep(0.06)
        self.assertEqual(self.started_pids(), [])

        self.marker.touch(mode=0o600)
        await self.until_event("camera.request", enabled=True)
        await self.send({"t": "camera.policy", "allowed": True, "seq": 2})
        result = await self.until_event("see.presence", present=True)
        self.assertEqual(result, {"t": "see.presence", "present": True, "x": -0.2, "y": 0.4,
                                  "lid_seq": 1, "camera_seq": 2})
        original, = self.started_pids()

        await self.send({"t": "body.lid", "closed": None, "seq": 2},
                        {"t": "body.lid", "closed": False, "seq": 1},
                        {"t": "camera.policy", "allowed": True, "seq": 1})
        await self.wait_for_stop(original)
        self.assertIsNone(self.eyes.authorization())
        await self.snapshot(lid_seq=3, camera_seq=3)
        await self.until_event("see.presence", lid_seq=3, camera_seq=3, present=True)
        old, reopened = self.started_pids()
        self.assertEqual(old, original)
        self.assertNotEqual(original, reopened)

        await self.send({"t": "camera.policy", "allowed": False, "seq": 4},
                        {"t": "camera.policy", "allowed": True, "seq": 3})
        await self.wait_for_stop(reopened)
        self.assertIsNone(self.eyes.authorization())
        await self.send({"t": "camera.policy", "allowed": True, "seq": 5})
        await self.until_event("see.presence", lid_seq=3, camera_seq=5, present=True)
        latest = self.started_pids()[-1]

        self.marker.unlink()
        await self.until_event("camera.request", enabled=False)
        await self.wait_for_stop(latest)

    async def test_disconnect_and_heartbeat_expiry_stop_the_child(self):
        self.enterContext(patch("mind.eyes.HEARTBEAT_TIMEOUT", 0.35))
        self.marker.touch(mode=0o600)
        await self.start()
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        await self.until_event("see.presence", present=True)
        original, = self.started_pids()
        self.writer.close()
        await self.writer.wait_closed()
        await self.wait_for_stop(original)

        await self.accept()
        await self.until_event("camera.request", enabled=True)
        await self.send({"t": "camera.policy", "allowed": True, "seq": 1},
                        {"t": "state.changed", "state": "idle"}, {"t": "stem.beat"})
        await asyncio.sleep(0.06)
        self.assertEqual(self.started_pids(), [original])
        await self.send({"t": "body.lid", "closed": False, "seq": 1})
        await self.until_event("see.presence", present=True)
        old, reopened = self.started_pids()
        self.assertEqual(old, original)
        self.assertNotEqual(reopened, original)
        await self.wait_for_stop(reopened)
        self.assertIsNone(self.eyes.authorization())

    async def test_detector_failure_clears_presence_and_retries_with_backoff(self):
        self.enterContext(patch("mind.eyes.RETRY_SECONDS", 0.08))
        self.enterContext(patch("mind.eyes.MAX_RETRY_SECONDS", 0.16))
        self.marker.touch(mode=0o600)
        await self.start("fail")
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        for _ in range(3):
            await self.until_event("see.presence", present=False, lid_seq=1, camera_seq=1)
        self.assertEqual(len(self.started_pids()), 3)

        await self.send({"t": "camera.policy", "allowed": False, "seq": 2})
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.started_pids()), 3)
