"""Open existing on-device settings from a spoken request."""

import asyncio
import os
import stat
import time
from pathlib import Path

from .reply import Reply, clean, is_guest
from .storage import atomic_write
from .try_mode import is_try_boot

REQUEST = Path("/run/being/settings-page")
GUEST = Path("/run/being/guest")
SCREEN = Path("/run/being-screen")
FRESH_FOR = 15

PAGES = {
    "care": "Care",
    "look": "appearance settings",
    "camera": "camera settings",
    "network": "network settings",
    "backup": "Backup",
    "update": "Updates",
    "permissions": "Permissions",
    "camera-enable": "camera permission",
    "dream-tonight": "tonight's dream permission",
}
NAMES = {
    "care": "care", "care settings": "care",
    "options": "look", "appearance settings": "look", "look settings": "look",
    "camera settings": "camera", "camera controls": "camera",
    "network settings": "network", "network controls": "network",
    "backup settings": "backup", "backup menu": "backup",
    "update settings": "update", "updates": "update", "update menu": "update",
    "permissions": "permissions", "permission settings": "permissions",
    "capabilities": "permissions", "capability settings": "permissions",
}


def _page(text, name):
    said = clean(text, name).casefold()
    for verb in ("open ", "show me ", "show "):
        if said.startswith(verb):
            return NAMES.get(said[len(verb):].removeprefix("the ").removeprefix("your "))
    return None


def may_match(text):
    return _page(text, "pip") is not None


async def handle(text, ctx):
    page = _page(text, ctx.name)
    if page is None:
        return None
    if is_guest(ctx) or ctx.speaker != ctx.owner:
        return Reply("[neutral] My owner can open those settings.")
    if not REQUEST.parent.is_dir() or not Path("/etc/being").is_dir():
        return Reply("[neutral] Those settings are available on the appliance.")
    if SCREEN.is_dir():
        return Reply("[neutral] Setup is already on screen. Close it before using a spoken shortcut.")
    if page in ("network", "update") and is_try_boot():
        return Reply(f"[neutral] {PAGES[page].capitalize()} are available after installation.")
    return Reply(f"[neutral] I'll open {PAGES[page]}.", settings_page=page)


async def request_page(page):
    if page not in PAGES or guest_active() or SCREEN.is_dir():
        return False
    pending = None
    success = False
    try:
        atomic_write(REQUEST, page + "\n")
        pending = REQUEST.stat().st_ino
        process = await asyncio.create_subprocess_exec(
            "systemctl", "--no-block", "start", "being-local-screen.service",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            success = await asyncio.wait_for(process.wait(), 2.0) == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    except (OSError, TimeoutError):
        return False
    finally:
        if pending is not None and not success:
            try:
                if REQUEST.stat().st_ino == pending:
                    REQUEST.unlink(missing_ok=True)
            except OSError:
                pass
    return success


def guest_active():
    """Read the boot-local privacy latch. Uncertainty keeps owner access closed."""
    try:
        GUEST.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def mark_guest(active):
    """Keep the admission mode across process restarts within the same boot."""
    if active:
        REQUEST.unlink(missing_ok=True)
        atomic_write(GUEST, "guest\n")
    else:
        GUEST.unlink(missing_ok=True)


def take_page():
    """Consume a current root-owned request once, including when it is stale."""
    try:
        descriptor = os.open(REQUEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if REQUEST.lstat().st_ino == info.st_ino:
            REQUEST.unlink(missing_ok=True)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                or not 0 <= time.time() - info.st_mtime <= FRESH_FOR or guest_active()):
            return None
        page = os.read(descriptor, 24).decode("ascii").strip()
        return page if page in PAGES else None
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    # The local Quick Menu's Options key links straight to the Look page.
    import sys

    raise SystemExit(0 if sys.argv[1:] == ["look"] and asyncio.run(request_page("look")) else 1)
