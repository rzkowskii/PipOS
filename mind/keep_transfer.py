"""Invoke the fixed, private-mount USB-to-Keep transfer."""

import asyncio
import json
import time

from .keep_export import RECEIPT_SECONDS, Receipt

UNIT = "being-keep-transfer"
MAX_REPLY = 4096
_armed = None
_active = None
_notify = None


def _identify_sd_sync():
    from . import removable_media

    disk = removable_media._candidate(inspect_contents=False, origin="sd")
    if not removable_media._same_disk(disk):
        raise ValueError("The SD card changed before I could identify it.")
    if not disk.get("device_number") or type(disk.get("device_sequence")) is not int:
        raise ValueError("I couldn't verify the current SD card's block identity.")
    size = disk.get("size_bytes")
    if type(size) is not int or size <= 0:
        raise ValueError("I couldn't verify that SD card's capacity.")
    return Receipt(device=disk["device"], device_number=disk["device_number"],
                   device_sequence=disk["device_sequence"], issued=time.monotonic(),
                   description="the SD card", origin="sd",
                   card_identity=disk["card_identity"])


async def identify_sd(state_dir):
    """Identify the one qualified card without mounting or reading its files."""
    del state_dir
    return await asyncio.to_thread(_identify_sd_sync)


def _photos_progress(state_dir, receipt):
    from . import backup, install_disks, removable_media
    from .keep import Store

    if not removable_media._same_disk(receipt.__dict__):
        raise ValueError("The USB stick changed before I could check the copy.")
    store = Store(state_dir)
    for volume in install_disks.volumes(receipt.device):
        media_uuid = backup.media_identity(volume["device"])
        if media_uuid:
            saved = store.transfer_progress(media_uuid, "photos")
            if saved is not None:
                return saved["position"]
    return None


async def photos_progress(state_dir):
    """Report the last verified cursor using cached device identity only."""
    from .keep_export import identify

    receipt = await identify(state_dir)
    return await asyncio.to_thread(_photos_progress, state_dir, receipt)


def _command():
    return (
        "systemd-run", f"--unit={UNIT}", "--quiet", "--wait", "--pipe", "--collect",
        "--service-type=exec", "--property=Type=exec", "--property=PrivateMounts=yes",
        "--property=PrivateNetwork=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes", "--property=PrivateTmp=yes", "--property=NoNewPrivileges=yes",
        "--property=RestrictAddressFamilies=AF_UNIX", "--property=DevicePolicy=closed",
        "--property=DeviceAllow=block-* r", "--property=ReadWritePaths=/run /mind/keep",
        "--property=CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_CHROOT CAP_SETUID CAP_SETGID CAP_SETPCAP",
        "--property=MemoryMax=320M", "--property=MemorySwapMax=0", "--property=TasksMax=16",
        "--property=Nice=10", "--property=IOSchedulingClass=idle",
        "--property=RuntimeMaxSec=45s", "--property=TimeoutStopSec=5s",
        "/usr/sbin/being-media", "keep",
    )


async def _stop_unit():
    process = await asyncio.create_subprocess_exec(
        "systemctl", "stop", f"{UNIT}.service",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(), 7)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _receive(state_dir, selection, receipt):
    """Return one tagged summary; content bytes never enter the Mind process."""
    if str(state_dir) != "/mind":
        raise ValueError("I can only copy into the installed private keep.")
    if not isinstance(receipt, Receipt) or not 0 <= time.monotonic() - receipt.issued <= RECEIPT_SECONDS:
        raise ValueError("That USB confirmation expired. Ask me again.")
    request = json.dumps({"selection": selection, "device": receipt.device,
                          "device_number": receipt.device_number, "device_sequence": receipt.device_sequence,
                          "origin": receipt.origin, "card_identity": receipt.card_identity},
                         ensure_ascii=False).encode("utf-8")
    if len(request) >= 1024:
        raise ValueError("That file name is too long to look for safely.")
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        *_command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL))
    process = None
    try:
        process = await asyncio.shield(launch)
        output, _ = await asyncio.wait_for(process.communicate(request), 50)
    except (asyncio.CancelledError, TimeoutError) as error:
        try:
            if process is None:
                process = await launch
            if process.returncode is None:
                process.kill()
                await process.wait()
            await _stop_unit()
        except (OSError, TimeoutError):
            pass
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ValueError("I stopped because the stick took too long to answer.") from None
    if len(output) > MAX_REPLY:
        raise ValueError("The media helper returned too much information.")
    try:
        answer = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("The media helper didn't answer safely.") from None
    if process.returncode or not isinstance(answer, dict):
        raise ValueError(str(answer.get("error", "I couldn't copy from that stick."))
                         if isinstance(answer, dict) else "I couldn't copy from that stick.")
    imported = int(answer.get("imported", 0))
    duplicates = int(answer.get("duplicates", 0))
    skipped = int(answer.get("skipped", 0))
    noun = "photo" if answer.get("photos") else "file"
    line = f"[neutral] I kept {imported} new {noun}{'' if imported == 1 else 's'}"
    if duplicates:
        line += f" and skipped {duplicates} duplicate{'' if duplicates == 1 else 's'}"
    if answer.get("partial"):
        line += (f". I stopped at the safe limit and skipped {skipped} more" if skipped
                 else ". I stopped at the safe limit, so there may be more")
        line += ". Say keep the photos to continue"
    if imported:
        line += ". These copies are on this laptop only"
    return line + "."


def _finished(task):
    global _active, _notify
    _active = None
    try:
        line = task.result()
    except asyncio.CancelledError:
        line = "[neutral] I paused the photo copy. Say resume copying the photos to continue."
    except (OSError, ValueError):
        line = "[concern] The photo copy stopped. Say resume copying the photos to try again."
    except Exception:
        line = "[concern] The photo copy stopped."
    notify, _notify = _notify, None
    if notify is not None:
        notify(line)


def arm_photos(state_dir, receipt):
    """Arm a confirmed copy; Turn starts it only after its reply finishes."""
    global _armed
    if (str(state_dir) != "/mind" or not isinstance(receipt, Receipt)
            or receipt.origin != "usb"):
        raise ValueError("That USB confirmation was not valid.")
    if not 0 <= time.monotonic() - receipt.issued <= RECEIPT_SECONDS:
        raise ValueError("That USB confirmation expired. Ask me again.")
    _armed = (state_dir, receipt)
    return "[neutral] I'll copy the photos quietly while I'm idle."


def resume_idle(notify=None):
    """Start the one confirmed armed batch; never mounts without that receipt."""
    global _armed, _active, _notify
    if _armed is None or _active is not None:
        return False
    state_dir, receipt = _armed
    _armed = None
    if not 0 <= time.monotonic() - receipt.issued <= RECEIPT_SECONDS:
        if notify is not None:
            notify("[neutral] The photo copy confirmation expired. Say keep the photos to confirm again.")
        return False
    _notify = notify
    _active = asyncio.create_task(_receive(state_dir, None, receipt))
    _active.add_done_callback(_finished)
    return True


def pause(*, clear_armed=False, suppress_feedback=False):
    """Cancel the transient; its cancellation path stops the unit and unmounts."""
    global _armed, _notify
    if clear_armed:
        _armed = None
    if suppress_feedback:
        _notify = None
    task = _active
    if task is not None and not task.done() and not task.cancelling():
        task.cancel()
        return True
    return False


async def receive(state_dir, selection, receipt, *, immediate=False):
    """Named files copy now; photos arm for the next idle state."""
    if isinstance(receipt, Receipt) and receipt.origin == "sd" and not immediate:
        raise ValueError("An SD-card copy must run only from the owner's current request.")
    if selection is None and not immediate:
        return arm_photos(state_dir, receipt)
    return await _receive(state_dir, selection, receipt)
