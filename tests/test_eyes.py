"""Verify camera policy through a local bus and a disposable detector child."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import eyes
from mind.eyes import Eyes, detection, private_opt_in, read_frame


DETECTOR = """
import os
import sys
import time

with open(sys.argv[1], 'a') as starts:
    starts.write(f'{os.getpid()}\\n')
options = sys.argv[2:]
if options[-1:] == ['--snapshot']:
    if options[:-1] == ['delay-frame']:
        time.sleep(30)
    sys.stdout.buffer.write(b'P6\\n1 1\\n255\\n' + bytes((16, 32, 48)))
    sys.exit(0)
if options and options != ['delay-frame']:
    sys.exit(1)
while True:
    print('{"present":true,"x":-0.2,"y":0.4}', flush=True)
    time.sleep(0.04)
"""
FRAME = b"P6\n1 1\n255\n\x10\x20\x30"
LOOK_ID = "0123456789abcdef0123456789abcdef"
DESCRIBER = """
import os
import sys
import time

with open(sys.argv[1], 'a') as starts:
    starts.write(f'{os.getpid()}\\n')
frame = sys.stdin.buffer.read()
if frame != b'P6\\n1 1\\n255\\n' + bytes((16, 32, 48)):
    sys.exit(1)
if len(sys.argv) > 2:
    time.sleep(30)
print('A blue mug.')
"""


class CameraMetadata(unittest.TestCase):
    def test_mapped_node_requires_primary_interface_and_live_usb_authorisation(self):
        # resolved: on macOS the temp dir is under a symlink (/var -> /private/var)
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        sys_root = root / "sys"
        name = sys_root / eyes.PRODUCT_NAME
        name.parent.mkdir(parents=True)
        name.write_text("MacBookPro9,2\n")
        usb = sys_root / "devices/camera"
        interface = usb / "capture"
        interface.mkdir(parents=True)
        (usb / "idVendor").write_text("05ac\n")
        (usb / "idProduct").write_text("8509\n")
        grant = usb / "authorized"
        grant.write_text("1\n")
        video = sys_root / "class/video4linux/video3"
        video.mkdir(parents=True)
        (video / "device").symlink_to(interface, target_is_directory=True)
        index = video / "index"
        index.write_text("0\n")
        body_maps = root / "body-maps.json"
        body_maps.write_text('{"MacBookPro9,2":{"internal_camera":{"vendor":"05ac","product":"8509"}}}')
        with patch.object(eyes, "SYS", sys_root), patch.object(eyes, "BODY_MAPS", body_maps):
            self.assertEqual(eyes.mapped_usb_camera(3), usb)
            index.write_text("1\n")
            with self.assertRaises(OSError):
                eyes.mapped_usb_camera(3)
            index.write_text("0\n")
            grant.write_text("0\n")
            with self.assertRaises(OSError):
                eyes.mapped_usb_camera(3)

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


class CameraFrames(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_complete_bounded_rgb_frame_is_accepted(self):
        def buffered(data):
            stream = asyncio.StreamReader(limit=eyes.MAX_RESULT_BYTES)
            stream.feed_data(data)
            stream.feed_eof()
            return stream

        self.assertEqual(await read_frame(buffered(FRAME)), FRAME)
        for malformed in (b"P6\n321 1\n255\n", b"P5\n1 1\n255\n", b"P6\n1 1\n256\n",
                          FRAME[:-1], FRAME + b"x"):
            with self.subTest(data=malformed), self.assertRaises((ValueError, asyncio.IncompleteReadError)):
                await read_frame(buffered(malformed))


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

    async def start(self, *args, require_node=False, describe_command=None):
        command = (sys.executable, "-c", DETECTOR, str(self.starts), *args)
        self.eyes = Eyes(self.marker, command, bus_path=self.bus_path, require_node=require_node,
                         describe_command=describe_command)
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

    async def test_mapped_camera_does_not_fall_back_to_default_video_node(self):
        self.marker.touch(mode=0o600)
        await self.start(require_node=True)
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        async with asyncio.timeout(2):
            while self.eyes.camera_revision != 1:
                await asyncio.sleep(0.01)
        self.assertIsNone(self.eyes.authorization())
        self.assertEqual(self.started_pids(), [])

    async def wait_for_stop(self, pid):
        async with asyncio.timeout(2):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                await asyncio.sleep(0.02)

    async def wait_for_starts(self, count):
        async with asyncio.timeout(2):
            while len(self.started_pids()) < count:
                await asyncio.sleep(0.01)
        return self.started_pids()

    async def begin_look(self):
        await self.send({"t": "state.changed", "state": "thinking"},
                        {"t": "camera.look", "id": LOOK_ID, "lid_seq": 1, "camera_seq": 1})

    async def test_a_look_without_a_describer_preserves_the_presence_child(self):
        self.marker.touch(mode=0o600)
        await self.start()
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        await self.until_event("see.presence", present=True)
        original, = self.started_pids()
        await self.begin_look()
        result = await self.until_event("camera.look_result", id=LOOK_ID)
        self.assertEqual(result, {"t": "camera.look_result", "id": LOOK_ID, "lid_seq": 1,
                                  "camera_seq": 1, "status": "unavailable"})
        self.assertEqual(self.started_pids(), [original])

    async def test_configured_describer_receives_one_frame_and_sends_only_text(self):
        self.marker.touch(mode=0o600)
        await self.start(describe_command=(sys.executable, "-c", DESCRIBER, str(self.starts)))
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        await self.until_event("see.presence", present=True)
        await self.begin_look()
        result = await self.until_event("camera.look_result", id=LOOK_ID)
        self.assertEqual(result, {"t": "camera.look_result", "id": LOOK_ID, "lid_seq": 1, "camera_seq": 1,
                                  "status": "ok", "description": "A blue mug."})
        original, snapshot, describer, resumed = await self.wait_for_starts(4)
        for pid in (original, snapshot, describer):
            await self.wait_for_stop(pid)
        self.assertNotIn(resumed, (original, snapshot, describer))

    async def test_camera_revoke_reaps_the_pending_describer(self):
        self.marker.touch(mode=0o600)
        await self.start(describe_command=(sys.executable, "-c", DESCRIBER, str(self.starts), "delay"))
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        await self.until_event("see.presence", present=True)
        await self.begin_look()
        original, snapshot, describer = await self.wait_for_starts(3)
        await self.send({"t": "camera.policy", "allowed": False, "seq": 2})
        for pid in (original, snapshot, describer):
            await self.wait_for_stop(pid)
        self.assertIsNone(self.eyes.authorization())
        self.assertEqual(self.started_pids(), [original, snapshot, describer])

    async def test_explicit_frame_uses_the_current_grant_and_then_resumes_presence(self):
        self.marker.touch(mode=0o600)
        await self.start()
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        await self.until_event("see.presence", present=True)
        original, = self.started_pids()
        generation, lid, camera = self.eyes.authorization()
        self.assertIsNone(await self.eyes.take_frame((generation, lid, camera + 1)))
        self.assertEqual(self.started_pids(), [original])
        self.assertEqual(await self.eyes.take_frame((generation, lid, camera)), FRAME)

        first, frame, resumed = await self.wait_for_starts(3)
        self.assertEqual(first, original)
        self.assertEqual(len({original, frame, resumed}), 3)
        await self.wait_for_stop(original)
        await self.wait_for_stop(frame)

    async def test_lid_close_reaps_a_pending_frame_and_discards_its_result(self):
        self.marker.touch(mode=0o600)
        await self.start("delay-frame")
        await self.until_event("camera.request", enabled=True)
        await self.snapshot()
        await self.until_event("see.presence", present=True)
        pending = asyncio.create_task(self.eyes.take_frame(self.eyes.authorization()))
        original, frame = await self.wait_for_starts(2)
        self.assertIsNone(await self.eyes.take_frame(self.eyes.authorization()))
        await self.send({"t": "body.lid", "closed": True, "seq": 2})
        self.assertIsNone(await asyncio.wait_for(pending, timeout=2))
        await self.wait_for_stop(original)
        await self.wait_for_stop(frame)
        self.assertEqual(self.started_pids(), [original, frame])

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
