"""Supervise the local camera detector. Only presence coordinates leave this organ."""

import argparse
import asyncio
import json
import os
import signal
import stat
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .bus import Bus


AWAKE = ("idle", "attending", "listening", "thinking", "speaking")
SUBS = ("body.lid", "state.changed", "camera.policy", "stem.beat")
DETECTOR = "/usr/libexec/being/eyes-capture"
CASCADE = "/usr/share/being/facefinder"
POLICY_POLL = 0.25
HEARTBEAT_TIMEOUT = 2.0
DETECTION_TIMEOUT = 3.0
MAX_RESULT_BYTES = 256
RETRY_SECONDS = 2.0
MAX_RETRY_SECONDS = 30.0


def private_opt_in(marker):
    try:
        info = marker.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def revision(event):
    value = event.get("seq")
    return value if type(value) is int and 0 <= value < 2**64 else None


def detection(line):
    """Accept exactly the small metadata messages the native detector emits."""
    result = json.loads(line)
    if result == {"present": False} and result["present"] is False:
        return result
    if isinstance(result, dict) and result.keys() == {"present", "x", "y"} and result["present"] is True:
        if all(type(value) in (int, float) and -1 <= value <= 1 for value in (result["x"], result["y"])):
            return result
    raise ValueError("invalid detector result")


class Eyes:
    def __init__(self, marker, command, *, bus_path=None):
        self.marker = marker
        self.command = command
        self.bus = Bus("eyes", SUBS, path=bus_path, urgent=self.receive)
        self.bus.on_disconnect = self.connection_lost
        self.capture = None
        self.capture_epoch = None
        self.connection_lost()

    def connection_lost(self):
        self.lid_revision = self.camera_revision = None
        self.lid_open = self.camera_allowed = False
        self.state = None
        self.last_heartbeat = None
        self.request = None
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
            if seq is None:
                self.camera_allowed = False
            elif self.camera_revision is None or seq > self.camera_revision:
                self.camera_revision = seq
                self.camera_allowed = event.get("allowed") is True and self.request == (self.bus.generation, True)
            elif seq == self.camera_revision and event.get("allowed") is not True:
                self.camera_allowed = False
        elif topic == "state.changed":
            self.state = event.get("state")
        elif topic == "stem.beat":
            self.last_heartbeat = time.monotonic()
        self.revoke_stale_capture()

    def authorization(self):
        if (self.bus.connected.is_set() and self.lid_open and self.camera_allowed
                and self.request == (self.bus.generation, True) and self.state in AWAKE
                and self.last_heartbeat is not None and time.monotonic() - self.last_heartbeat <= HEARTBEAT_TIMEOUT
                and private_opt_in(self.marker)):
            return self.bus.generation, self.lid_revision, self.camera_revision
        return None

    def revoke_stale_capture(self):
        task = self.capture
        if task is not None and not task.done() and not task.cancelling():
            if self.authorization() != self.capture_epoch:
                task.cancel()

    async def sync(self):
        if self.bus.connected.is_set():
            request = self.bus.generation, private_opt_in(self.marker)
            if request != self.request:
                self.request = request
                self.camera_allowed = False
                self.revoke_stale_capture()
                await self.bus.send("camera.request", enabled=request[1])

        self.revoke_stale_capture()
        if self.capture is not None and (self.capture.done() or self.capture.cancelling()):
            await self.stop_capture()
        epoch = self.authorization()
        if self.capture is None and epoch is not None:
            self.capture_epoch = epoch
            self.capture = asyncio.create_task(self.watch(epoch))

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

    async def start_detector(self, epoch):
        # The queued spawn might run after a privacy event has revoked its epoch.
        if self.authorization() == epoch:
            return await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_RESULT_BYTES,
            )
        return None

    @asynccontextmanager
    async def detector(self, epoch):
        spawn = asyncio.create_task(self.start_detector(epoch))
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
                await self.stop_capture()
            finally:
                await self.bus.stop()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=os.environ.get("BEING_CAMERA_DEVICE", "/dev/video0"))
    parser.add_argument("--detector", default=DETECTOR)
    parser.add_argument("--cascade", default=CASCADE)
    args = parser.parse_args()
    marker = Path(os.environ.get("BEING_MIND", "/mind")) / "camera.enabled"
    command = (args.detector, "--device", args.device, "--cascade", args.cascade)
    task = asyncio.create_task(Eyes(marker, command).run())
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
