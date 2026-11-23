"""Confirm a boot only after the organs and cortex have been healthy together."""

import asyncio
import http.client
import os
import subprocess
import time
from pathlib import Path

from .bus import Bus

BOOT_ENV = "/bootstate/EFI/BOOT/grubenv"
READY = Path("/run/being-slot-good")
STABLE_SECONDS = 60


def booted_slot():
    for arg in Path("/proc/cmdline").read_text().split():
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


async def healthy_for_a_minute(deadline):
    bus = Bus("boot-health", ("state.changed", "stem.beat"))
    await bus.start()
    generation = 0
    ready = False
    last_beat = 0
    healthy_since = None
    next_check = 0
    try:
        while time.monotonic() < deadline:
            try:
                event = await asyncio.wait_for(bus.recv(), 1)
            except asyncio.TimeoutError:
                event = {}
            if generation != bus.generation:
                generation = bus.generation
                ready, last_beat, healthy_since = False, 0, None
            now = time.monotonic()
            if event.get("t") == "state.changed":
                ready = event.get("state") != "waking" and event.get("unwell") is False
            elif event.get("t") == "stem.beat":
                last_beat = now
            if not ready or not bus.connected.is_set() or now - last_beat > 3:
                healthy_since = None
            elif now >= next_check:
                next_check = now + 2
                if not await asyncio.to_thread(cortex_ready):
                    healthy_since = None
                elif healthy_since is None:
                    healthy_since = now
                elif now - healthy_since >= STABLE_SECONDS:
                    return True
        return False
    finally:
        await bus.stop()


def main():
    slot = booted_slot()
    environment = subprocess.check_output(["grub-editenv", BOOT_ENV, "list"], text=True)
    confirmed = f"GOOD={slot}" in environment.splitlines()
    deadline = time.monotonic() + int(os.environ.get("BEING_SLOT_DEADLINE", "180"))
    if asyncio.run(healthy_for_a_minute(deadline)):
        if not confirmed:
            # Use the same OK/TRY values as RAUC's mark-good, in one write.
            subprocess.run(
                ["grub-editenv", BOOT_ENV, "set", f"GOOD={slot}", f"{slot}_OK=1", f"{slot}_TRY=0"],
                check=True,
            )
            with open(BOOT_ENV, "rb") as env_file:
                os.fsync(env_file.fileno())
        READY.write_text(f"{slot}\n")
        print(f"Slot {slot} is healthy", flush=True)
    elif not confirmed:
        print(f"Trial slot {slot} failed its health check, rebooting", flush=True)
        subprocess.run(["systemctl", "--no-block", "reboot"], check=True)
    else:
        print(f"Slot {slot} is degraded, leaving the face available", flush=True)


if __name__ == "__main__":
    main()
