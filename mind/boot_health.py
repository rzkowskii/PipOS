"""Confirm a boot only after the organs and cortex have been healthy together."""

import asyncio
from functools import partial
import http.client
import os
import subprocess
import time
from pathlib import Path

from .backup import DB_FILE
from .bus import Bus
from .continuity_baseline import NAME as BASELINE_FILE
from .diag_system import (Host, check_continuity_archive, check_continuity_baseline,
                          check_continuity_recall, check_continuity_slots)
from .retention import read_policy
from .storage import atomic_write
from .try_mode import SLOT_CONTINUITY_WARNING

BOOT_ENV = "/bootstate/EFI/BOOT/grubenv"
CMDLINE = Path("/proc/cmdline")
READY = Path("/run/being-slot-good")
STABLE_SECONDS = 60


def booted_slot():
    for arg in CMDLINE.read_text().split():
        if arg in ("rauc.slot=A", "rauc.slot=B"):
            return arg[-1]
    raise RuntimeError("The bootloader did not identify the booted slot")


def cortex_ready():
    conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=2)
    try:
        conn.request("GET", "/health")
        return conn.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def continuity_failures(host, *, trial=False):
    """Read installed continuity checks without initialising the archive."""
    archive = check_continuity_archive(host)
    path = host.path(f"/mind/{DB_FILE}")
    if not os.path.lexists(path):
        try:
            ephemeral = read_policy(host.path("/mind")).mode == "session-only"
        except (OSError, ValueError):
            ephemeral = False
        if ephemeral and {test["id"] for test in archive} == {"continuity.life", "continuity.archive"}:
            # A session-only owner may have no archive. Keep the life check
            # and do not excuse an interrupted restore or an unreadable DB.
            archive = [test for test in archive if test["id"] != "continuity.archive"]
    checks = [*archive, check_continuity_slots(host)]
    # A first install or a restore can have no baseline yet. Once a baseline
    # exists, a healthy database alone cannot prove that the owner still has
    # the same life and archive. Report comparison failures through the same
    # trial/confirmed-slot policy as other continuity checks.
    if (any(test["id"] == "continuity.archive" and test["status"] == "pass" for test in archive)
            and os.path.lexists(host.path(f"/mind/{BASELINE_FILE}"))):
        checks.append(check_continuity_baseline(host))
    failures = tuple(test["id"] for test in checks if test["status"] != "pass")
    if not trial or failures:
        return failures
    from .diag_speech import check_command_routes

    return tuple(test["id"] for test in (check_continuity_recall(), check_command_routes())
                 if test["status"] != "pass")


def publish_continuity_warning(slot):
    try:
        if slot is None:
            SLOT_CONTINUITY_WARNING.unlink(missing_ok=True)
        else:
            atomic_write(SLOT_CONTINUITY_WARNING, f"{slot}\n")
    except OSError as error:
        print(f"Could not update the runtime continuity warning: {error.strerror or type(error).__name__}", flush=True)


async def healthy_for_a_minute(deadline, *, verify_continuity=None):
    bus = Bus("boot-health", ("state.changed", "stem.beat"))
    generation = bus.generation
    ready = False
    last_beat = None
    healthy_since = None
    epoch = 0
    next_check = 0
    next_continuity_check = 0
    last_failure = None
    attempt = None
    attempt_epoch = None

    def reset_health():
        nonlocal healthy_since, epoch
        healthy_since = None
        epoch += 1

    def disconnect():
        nonlocal ready, last_beat
        ready, last_beat = False, None
        reset_health()

    def observe(event):
        nonlocal generation, ready, last_beat
        if generation != bus.generation:
            disconnect()
            generation = bus.generation
        if event.get("t") == "state.changed":
            ready = event.get("state") != "waking" and event.get("unwell") is False
            if not ready:
                reset_health()
        elif event.get("t") == "stem.beat":
            now = time.monotonic()
            if last_beat is not None and now - last_beat > 3:
                reset_health()
            last_beat = now

    def live(now):
        return (ready and bus.connected.is_set() and generation == bus.generation
                and last_beat is not None and now - last_beat <= 3)

    async def check_cortex():
        nonlocal healthy_since, next_check
        before = epoch
        okay = await asyncio.to_thread(cortex_ready)
        now = time.monotonic()
        next_check = now + 2
        if not okay or not live(now) or epoch != before:
            reset_health()
            return False
        if healthy_since is None:
            healthy_since = now
        return True

    bus.urgent = observe
    bus.on_disconnect = disconnect
    await bus.start()
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                await asyncio.wait_for(bus.recv(), min(1, remaining))
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if now >= deadline:
                break
            if not live(now):
                reset_health()
            elif now >= next_check and await check_cortex():
                now = time.monotonic()
                if verify_continuity is None and now < deadline and now - healthy_since >= STABLE_SECONDS:
                    return True

            if attempt is not None and attempt.done():
                completed, attempt = attempt, None
                failed = completed.result()
                if attempt_epoch == epoch:
                    if not failed and now < deadline and await check_cortex():
                        if attempt_epoch == epoch and time.monotonic() < deadline:
                            return True
                    if failed and failed != last_failure:
                        print(f"Trial continuity checks waiting: {', '.join(failed)}", flush=True)
                    last_failure = failed

            now = time.monotonic()
            if (verify_continuity is not None and attempt is None and healthy_since is not None and live(now)
                    and next_continuity_check <= now < deadline and now - healthy_since >= STABLE_SECONDS):
                # Continue reading health and probing the cortex during the
                # archive and slot checks. Only this uninterrupted span can
                # admit their result. Never run two continuity reads at once.
                next_continuity_check = now + 5
                attempt_epoch = epoch
                attempt = asyncio.create_task(asyncio.to_thread(verify_continuity))
        return False
    finally:
        await bus.stop()
        if attempt is not None:
            await asyncio.gather(attempt, return_exceptions=True)


def main():
    slot = booted_slot()
    environment = subprocess.check_output(["grub-editenv", BOOT_ENV, "list"], text=True)
    confirmed = f"GOOD={slot}" in environment.splitlines()
    deadline = time.monotonic() + int(os.environ.get("BEING_SLOT_DEADLINE", "180"))
    installed = "being.installed=1" in CMDLINE.read_text().split()
    verify = partial(continuity_failures, Host(), trial=not confirmed) if installed else None
    if asyncio.run(healthy_for_a_minute(deadline, verify_continuity=None if confirmed else verify)):
        if confirmed and verify is not None:
            failed = verify()
            if failed:
                publish_continuity_warning(slot)
                print(f"Slot {slot} continuity checks need attention: {', '.join(failed)}; leaving the face available",
                      flush=True)
                return
        if installed:
            from .update_slots import mark_healthy

            mark_healthy(slot)
        elif not confirmed:
            # Use the same OK/TRY values as RAUC's mark-good, in one write.
            subprocess.run(
                ["grub-editenv", BOOT_ENV, "set", f"GOOD={slot}", f"{slot}_OK=1", f"{slot}_TRY=0"],
                check=True,
            )
            with open(BOOT_ENV, "rb") as env_file:
                os.fsync(env_file.fileno())
        if installed:
            publish_continuity_warning(None)
        READY.write_text(f"{slot}\n")
        print(f"Slot {slot} is healthy", flush=True)
    elif not confirmed:
        print(f"Trial slot {slot} failed its health check, rebooting", flush=True)
        subprocess.run(["systemctl", "--no-block", "reboot"], check=True)
    else:
        print(f"Slot {slot} is degraded, leaving the face available", flush=True)


if __name__ == "__main__":
    main()
