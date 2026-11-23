"""Tell systemd an organ is ready and still alive (sd_notify, standard library only).

A unit with `WatchdogSec=` and `NotifyAccess=main` restarts the organ when its
pings stop. The ping comes from the organ's own event loop, so a loop that is
stuck (a blocking call, a deadlock) stops pinging and is restarted, while a
loop that is merely busy keeps going.

Outside systemd (no NOTIFY_SOCKET) every call is a quiet no-op, so the organs
run the same way on a developer's machine and in tests.
"""

import asyncio
import os
import socket

PING_SECONDS = 5.0


def notify(message, *, environ=None):
    """Send one sd_notify datagram ("READY=1", "WATCHDOG=1", "STATUS=...").

    Returns True if it was sent. Never raises: a missing or broken socket
    must not stop the organ that is trying to report on itself.
    """
    address = (os.environ if environ is None else environ).get("NOTIFY_SOCKET")
    if not address or not message:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]  # an abstract socket
    elif not address.startswith("/"):
        return False  # only Unix sockets are spoken here
    try:
        # Python's sockets are already close-on-exec (PEP 446).
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
    except OSError:
        return False
    return True


def ready(status=None, *, environ=None):
    message = "READY=1" if status is None else f"READY=1\nSTATUS={status}"
    return notify(message, environ=environ)


def watchdog(*, environ=None):
    return notify("WATCHDOG=1", environ=environ)


def stopping(*, environ=None):
    return notify("STOPPING=1", environ=environ)


def watchdog_interval(*, environ=None, default=PING_SECONDS):
    """How often to ping: every PING_SECONDS, or more often if the unit's
    watchdog is shorter (half of WATCHDOG_USEC, as systemd advises)."""
    env = os.environ if environ is None else environ
    try:
        usec = int(env.get("WATCHDOG_USEC", ""))
    except ValueError:
        return default
    if usec <= 0:
        return default
    return min(default, usec / 2_000_000)


async def keep_alive(*, ready_when=None, interval=None, environ=None):
    """WATCHDOG=1 every `interval` seconds until cancelled, and READY=1 once
    `ready_when` (an asyncio.Event, or None for at once) is set.

    The watchdog pings start at once, before READY: they say the event loop
    is alive, which it is while it waits for the bus. A unit whose watchdog
    is armed from the start (Type=simple) must not be killed every
    WatchdogSec just because the brainstem is slow to come up.
    """
    period = watchdog_interval(environ=environ) if interval is None else interval
    told = False
    try:
        while True:
            if not told and (ready_when is None or ready_when.is_set()):
                ready(environ=environ)
                told = True
            watchdog(environ=environ)
            if told:
                await asyncio.sleep(period)
                continue
            try:
                # READY goes the moment the bus is up, not on the next ping.
                await asyncio.wait_for(ready_when.wait(), period)
            except asyncio.TimeoutError:
                pass
    finally:
        stopping(environ=environ)
