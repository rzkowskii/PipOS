"""Supervise local presence detection and bounded, epoch-checked frame capture."""

import argparse
import asyncio
import json
import os
import re
import signal
import stat
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .bus import Bus
from .policy import CAMERA_MARKER, camera_allowed


AWAKE = ("idle", "attending", "listening", "thinking", "speaking")
SUBS = ("body.lid", "state.changed", "camera.policy", "stem.beat", "camera.look", "camera.look_cancel")
DETECTOR = "/usr/libexec/being/eyes-capture"
CASCADE = "/usr/share/being/facefinder"
BODY_MAPS = Path("/usr/share/being/body-maps.json")
SYS = Path("/sys")
PRODUCT_NAME = "class/dmi/id/product_name"
MAPPED_LAPTOP = "MacBookPro9,2"
POLICY_POLL = 0.25
HEARTBEAT_TIMEOUT = 2.0
DETECTION_TIMEOUT = 3.0
FRAME_TIMEOUT = 4.0
FRAME_MAX_SIDE = 320
MAX_RESULT_BYTES = 256
MAX_DESCRIPTION_BYTES = 240
LOOK_TIMEOUT = 30.0
LOOK_ID = re.compile(r"[0-9a-f]{32}")
RETRY_SECONDS = 2.0
MAX_RETRY_SECONDS = 30.0


# Compatibility name retained for callers that imported the former local
# predicate.  The policy module is now the single authority and is rechecked
# at each authorization/request boundary below.
private_opt_in = camera_allowed


@dataclass(frozen=True)
class Look:
    token: str
    epoch: tuple[int, int, int]
    deadline: float


def revision(event, key="seq"):
    value = event.get(key)
    return value if type(value) is int and 0 <= value < 2**64 else None


def requires_mapped_camera():
    try:
        model = (SYS / PRODUCT_NAME).read_text().strip()
        # On the target laptop a missing userspace map must still fail closed.
        if model == MAPPED_LAPTOP:
            return True
        return "internal_camera" in json.loads(BODY_MAPS.read_text()).get(model, {})
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        return False


def mapped_usb_camera(node):
    """Resolve the kernel node back to the signed map before opening capture."""
    try:
        model = (SYS / PRODUCT_NAME).read_text().strip()
        identity = json.loads(BODY_MAPS.read_text())[model]["internal_camera"]
        video = SYS / f"class/video4linux/video{node}"
        if (video / "index").read_text().strip() != "0":
            raise ValueError("expected the primary video interface")
        device = (video / "device").resolve(strict=True)
        for parent in (device, *device.parents):
            if not parent.is_relative_to(SYS / "devices"):
                break
            if (parent / "idVendor").is_file():
                if ((parent / "idVendor").read_text().strip() == identity["vendor"]
                        and (parent / "idProduct").read_text().strip() == identity["product"]
                        and (parent / "authorized").read_text().strip() == "1"):
                    return parent
                break
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        pass
    raise OSError("the authorised internal camera is not bound to its video node")


def detection(line):
    """Accept exactly the small metadata messages the native detector emits."""
    result = json.loads(line)
    if result == {"present": False} and result["present"] is False:
        return result
    if isinstance(result, dict) and result.keys() == {"present", "x", "y"} and result["present"] is True:
        if all(type(value) in (int, float) and -1 <= value <= 1 for value in (result["x"], result["y"])):
            return result
    raise ValueError("invalid detector result")


def rgb_payload_size(dimensions):
    match = re.fullmatch(rb"([1-9][0-9]{0,2}) ([1-9][0-9]{0,2})\n", dimensions)
    if match is None:
        raise ValueError("invalid camera frame dimensions")
    width, height = (int(side) for side in match.groups())
    if max(width, height) > FRAME_MAX_SIDE:
        raise ValueError("camera frame exceeded its size limit")
    return width * height * 3


def valid_description(text):
    return (isinstance(text, str) and 0 < len(text) <= MAX_DESCRIPTION_BYTES
            and text.isprintable() and text == text.strip() and any(c.isalnum() for c in text)
            and not any(c in text for c in "[]<>") and len(text.encode("utf-8")) <= MAX_DESCRIPTION_BYTES)


async def read_frame(stream):
    """Read exactly one bounded RGB P6 image from an owned child pipe."""
    if await stream.readline() != b"P6\n":
        raise ValueError("camera did not return an RGB frame")
    dimensions = await stream.readline()
    size = rgb_payload_size(dimensions)
    if await stream.readline() != b"255\n":
        raise ValueError("camera frame exceeded its format or size limit")
    pixels = await stream.readexactly(size)
    if await stream.read(1):
        raise ValueError("camera returned trailing frame data")
    return b"P6\n" + dimensions + b"255\n" + pixels


class Eyes:
    def __init__(self, marker, command, *, bus_path=None, require_node=False, describe_command=None):
        self.marker = marker
        self.command = command
        self.require_node = require_node
        self.describe_command = describe_command
        self.bus = Bus("eyes", SUBS, path=bus_path, urgent=self.receive)
        self.bus.on_disconnect = self.connection_lost
        self.capture = None
        self.capture_epoch = None
        self.frame_task = None
        self.frame_epoch = None
        self.look = None
        self.look_task = None
        self.look_ids = deque(maxlen=32)
        self.connection_lost()

    def connection_lost(self):
        self.lid_revision = self.camera_revision = None
        self.lid_open = self.camera_allowed = False
        self.video_node = None
        self.state = None
        self.last_heartbeat = None
        self.request = None
        self.look_ids.clear()
        self.cancel_look()
        self.revoke_stale_capture()

    def receive(self, event):
        """Apply privacy decisions on arrival, ahead of any queued work."""
        topic = event["t"]
        if topic == "body.lid":
            seq = revision(event)
            if seq is None:
                self.lid_open = False
            elif self.lid_revision is None or seq > self.lid_revision:
                self.lid_revision = seq
                self.lid_open = event.get("closed") is False
            elif seq == self.lid_revision and event.get("closed") is not False:
                self.lid_open = False
        elif topic == "camera.policy":
            seq = revision(event)
            node = event.get("video_node")
            well_formed = node is None or type(node) is int and 0 <= node <= 4094
            if seq is None or not well_formed:
                self.camera_allowed = False
            elif self.camera_revision is None or seq > self.camera_revision:
                self.camera_revision = seq
                self.camera_allowed = event.get("allowed") is True and self.request == (self.bus.generation, True)
                self.video_node = node if self.camera_allowed else None
            elif seq == self.camera_revision and (event.get("allowed") is not True or node != self.video_node):
                self.camera_allowed = False
        elif topic == "state.changed":
            self.state = event.get("state")
        elif topic == "stem.beat":
            self.last_heartbeat = time.monotonic()
        elif topic == "camera.look":
            self.request_look(event)
        elif (topic == "camera.look_cancel" and event.keys() == {"t", "id"}
              and self.look is not None and event.get("id") == self.look.token):
            self.cancel_look()
        self.revoke_stale_capture()
        self.revoke_stale_look()

    def request_look(self, event):
        epoch = self.authorization()
        token = event.get("id")
        if (self.state == "thinking" and epoch is not None and self.look is None
                and event.keys() == {"t", "id", "lid_seq", "camera_seq"}
                and isinstance(token, str) and LOOK_ID.fullmatch(token)
                and token not in self.look_ids
                and (revision(event, "lid_seq"), revision(event, "camera_seq")) == epoch[1:]):
            self.look_ids.append(token)
            self.look = Look(token, epoch, asyncio.get_running_loop().time() + LOOK_TIMEOUT)

    def look_authorized(self, look):
        return self.look is look and self.state == "thinking" and self.authorization() == look.epoch

    def cancel_look(self):
        self.look = None
        if self.look_task is not None and not self.look_task.done() and not self.look_task.cancelling():
            self.look_task.cancel()

    def revoke_stale_look(self):
        if self.look is not None and not self.look_authorized(self.look):
            self.cancel_look()

    def authorization(self):
        if (self.bus.connected.is_set() and self.lid_open and self.camera_allowed
                and (not self.require_node or self.video_node is not None)
                and self.request == (self.bus.generation, True) and self.state in AWAKE
                and self.last_heartbeat is not None and time.monotonic() - self.last_heartbeat <= HEARTBEAT_TIMEOUT
                and camera_allowed(self.marker)):
            return self.bus.generation, self.lid_revision, self.camera_revision
        return None

    def revoke_stale_capture(self):
        for task, epoch in ((self.capture, self.capture_epoch), (self.frame_task, self.frame_epoch)):
            if task is not None and not task.done() and not task.cancelling() and self.authorization() != epoch:
                task.cancel()

    async def sync(self):
        if self.bus.connected.is_set():
            request = self.bus.generation, camera_allowed(self.marker)
            if request != self.request:
                self.request = request
                self.camera_allowed = False
                self.revoke_stale_capture()
                self.revoke_stale_look()
                await self.bus.send("camera.request", enabled=request[1])

        self.revoke_stale_capture()
        self.revoke_stale_look()
        if self.look_task is not None:
            if not self.look_task.done() and not self.look_task.cancelling():
                return
            await self.stop_look()
        if self.look is not None:
            if self.look_authorized(self.look):
                self.look_task = asyncio.create_task(self.describe(self.look))
                return
            self.revoke_stale_look()
        if self.capture is not None and (self.capture.done() or self.capture.cancelling()):
            await self.stop_capture()
        epoch = self.authorization()
        if self.capture is None and self.frame_task is None and epoch is not None:
            self.capture_epoch = epoch
            self.capture = asyncio.create_task(self.watch(epoch))

    async def stop_look(self):
        task = self.look_task
        self.look_task = None
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    await asyncio.gather(task, return_exceptions=True)
                    raise

    async def start_describer(self, look, scratch):
        if self.look_authorized(look) and asyncio.get_running_loop().time() < look.deadline:
            return await asyncio.create_subprocess_exec(
                *self.describe_command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_DESCRIPTION_BYTES + 2, start_new_session=True,
                env={**os.environ, "BEING_VISION_WORKDIR": scratch},
            )
        return None

    async def describe_frame(self, look, frame):
        async def feed(writer):
            try:
                writer.write(frame)
                await writer.drain()
            finally:
                writer.close()

        # Own the socket directory in Eyes so even a killed describer and its
        # server cannot leave a request's socket behind.
        with tempfile.TemporaryDirectory(prefix="pv-eye-", dir="/tmp") as scratch:
            spawn = asyncio.create_task(self.start_describer(look, scratch))
            try:
                process = await asyncio.shield(spawn)
                if process is None:
                    return None
                feeding = asyncio.create_task(feed(process.stdin))
                try:
                    try:
                        await process.stdout.readexactly(MAX_DESCRIPTION_BYTES + 2)
                    except asyncio.IncompleteReadError as error:
                        output = error.partial.removesuffix(b"\n")
                    else:
                        return None
                    await feeding
                    if await process.wait() != 0 or not 0 < len(output) <= MAX_DESCRIPTION_BYTES:
                        return None
                    text = output.decode("utf-8")
                    if valid_description(text):
                        return text
                    return None
                finally:
                    feeding.cancel()
                    await asyncio.gather(feeding, return_exceptions=True)
            finally:
                process = await spawn
                if process is not None:
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.stdin.close()
                    async with asyncio.timeout(1):
                        while await process.stdout.read(512):
                            pass
                        await process.wait()

    async def describe(self, look):
        caption = None
        try:
            if (self.describe_command and self.look_authorized(look)
                    and asyncio.get_running_loop().time() < look.deadline):
                try:
                    async with asyncio.timeout_at(look.deadline):
                        frame = await self.take_frame(look.epoch)
                        if frame is not None and self.look_authorized(look):
                            caption = await self.describe_frame(look, frame)
                except (OSError, ValueError, TimeoutError):
                    pass
            if self.look_authorized(look):
                _, lid, cam = look.epoch
                result = {"status": "unavailable"}
                if caption is not None and asyncio.get_running_loop().time() < look.deadline:
                    result = {"status": "ok", "description": caption}
                await self.bus.send("camera.look_result", id=look.token, lid_seq=lid, camera_seq=cam, **result)
        finally:
            if self.look is look:
                self.look = None

    async def stop_capture(self):
        task = self.capture
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    # Keep the camera child owned while its stop is in flight.
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            finally:
                self.capture = None
                self.capture_epoch = None

    async def start_detector(self, epoch, *, one_frame=False):
        # The queued spawn might run after a privacy event has revoked its epoch.
        if self.authorization() == epoch:
            command = list(self.command)
            if self.video_node is not None:
                usb = mapped_usb_camera(self.video_node)
                command[command.index("--device") + 1] = f"/dev/video{self.video_node}"
                command.extend(("--usb-parent", os.fspath(usb)))
            if one_frame:
                if "--cascade" in command:
                    cascade = command.index("--cascade")
                    del command[cascade:cascade + 2]
                command.append("--snapshot")
            return await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_RESULT_BYTES,
            )
        return None

    @asynccontextmanager
    async def detector(self, epoch, *, one_frame=False):
        spawn = asyncio.create_task(self.start_detector(epoch, one_frame=one_frame))
        try:
            yield await asyncio.shield(spawn)
        finally:
            process = await spawn
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.communicate()

    async def take_frame(self, epoch):
        """Capture for an explicit request holding this Eyes authorization.

        The caller owns the returned in-memory image and must recheck the epoch
        before using or disclosing any result derived from it.
        """
        if self.frame_task is not None or epoch is None or self.authorization() != epoch:
            return None
        self.frame_epoch = epoch
        task = self.frame_task = asyncio.create_task(self._take_frame(epoch))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                if not task.done() and not task.cancelling():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            return None  # capture authorization was revoked
        finally:
            self.frame_task = None
            self.frame_epoch = None

    async def _take_frame(self, epoch):
        try:
            async with asyncio.timeout(FRAME_TIMEOUT):
                await self.stop_capture()
                async with self.detector(epoch, one_frame=True) as process:
                    if process is None:
                        return None
                    frame = await read_frame(process.stdout)
                    if await process.wait() != 0:
                        return None
                return frame if self.authorization() == epoch else None
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
            return None

    async def publish(self, epoch, result):
        if self.authorization() == epoch:
            _, lid_seq, camera_seq = epoch
            await self.bus.send("see.presence", lid_seq=lid_seq, camera_seq=camera_seq, **result)

    async def watch(self, epoch):
        delay = RETRY_SECONDS
        while self.authorization() == epoch:
            try:
                async with self.detector(epoch) as process:
                    if process is None:
                        return
                    while self.authorization() == epoch:
                        line = await asyncio.wait_for(process.stdout.readline(), DETECTION_TIMEOUT)
                        if not line:
                            break
                        await self.publish(epoch, detection(line))
                        delay = RETRY_SECONDS
            except (OSError, ValueError, TimeoutError):
                pass
            await self.publish(epoch, {"present": False})
            await asyncio.sleep(delay)
            delay = min(MAX_RETRY_SECONDS, delay * 2)

    async def run(self):
        await self.bus.start()
        try:
            while True:
                await self.sync()
                try:
                    await asyncio.wait_for(self.bus.recv(), POLICY_POLL)
                except TimeoutError:
                    pass
        finally:
            try:
                await self.stop_look()
                task = self.frame_task
                if task is not None and not task.done():
                    if not task.cancelling():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await self.stop_capture()
            finally:
                await self.bus.stop()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=os.environ.get("BEING_CAMERA_DEVICE", "/dev/video0"))
    parser.add_argument("--detector", default=DETECTOR)
    parser.add_argument("--cascade", default=CASCADE)
    parser.add_argument("--describer")
    args = parser.parse_args()
    marker = Path(os.environ.get("BEING_MIND", "/mind")) / CAMERA_MARKER
    command = (args.detector, "--device", args.device, "--cascade", args.cascade)
    describer = None
    if args.describer:
        path = os.path.abspath(args.describer)
        if not os.path.isabs(args.describer) or not os.path.isfile(path) or not os.access(path, os.X_OK):
            parser.error("--describer must be an executable absolute path")
        describer = (path,)
    task = asyncio.create_task(Eyes(marker, command, require_node=requires_mapped_camera(), describe_command=describer).run())
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
