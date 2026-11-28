"""The spoken camera revocation path uses the same local helper as Care."""

import asyncio
from pathlib import Path

from .policy import CAMERA_MARKER, camera_allowed


CAMERA_HELPER = "/usr/sbin/being-camera"


async def revoke_camera(state_dir):
    if Path(state_dir) != Path("/mind") or not Path(CAMERA_HELPER).is_file():
        return False
    try:
        process = await asyncio.create_subprocess_exec(CAMERA_HELPER, "disable", "--no-block",
                                                       stdin=asyncio.subprocess.DEVNULL,
                                                       stdout=asyncio.subprocess.DEVNULL,
                                                       stderr=asyncio.subprocess.DEVNULL)
        try:
            if await asyncio.wait_for(process.wait(), 4.0) == 0:
                return not camera_allowed(Path(state_dir) / CAMERA_MARKER)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    except (OSError, TimeoutError):
        pass
    return False
