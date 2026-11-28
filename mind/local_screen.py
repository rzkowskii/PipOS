"""Give the local console the same lid and panel handoff as the face."""

import asyncio
import os
import threading
import time
from collections import deque
from pathlib import Path

from . import install_status, local_menu
from .bus import Bus

# The requests being-console.c leaves in the screen gate (one empty file per
# name, made with O_EXCL, so a held key steps no faster than this loop reads)
# and the face's key event each one becomes: the same topics and fields the
# face sends (crates/face/src/keys.rs), which the brainstem passes to the
# mind (mind/keys.py). fn+F10 and fn+F9 are the mic mute and panel recover.
GATE_REQUESTS = (
    ("mute", "key.mute", {}),
    ("recover", "key.panel_recover", {}),
    ("brightness-up", "key.brightness", {"dir": "up"}),
    ("brightness-down", "key.brightness", {"dir": "down"}),
    ("kbd-light-up", "key.kbd_light", {"dir": "up"}),
    ("kbd-light-down", "key.kbd_light", {"dir": "down"}),
    ("kbd-light-toggle", "key.kbd_light", {"dir": "toggle"}),
    ("volume-up", "key.volume", {"dir": "up"}),
    ("volume-down", "key.volume", {"dir": "down"}),
    ("speaker-mute", "key.mute_speaker", {}),
)


async def serve_panel(directory, stop, statuses, *, owner_page=False):
    show = directory / "show"
    presented = directory / "presented"
    current = None
    acknowledged = None
    open_sequence = None
    opened_at = None
    last_lid = None
    guest = False
    hidden_for_guest = False
    if owner_page:
        from .care_routes import guest_active
    bus = Bus(
        "face", ("body.lid", "app.privacy") if owner_page else ("body.lid",),
        path=os.environ.get("BEING_BUS", "/run/being/bus.sock"),
    )

    def close_panel():
        nonlocal current, acknowledged, open_sequence, opened_at
        current = acknowledged = open_sequence = opened_at = None
        statuses.clear()
        show.unlink(missing_ok=True)
        presented.unlink(missing_ok=True)

    def disconnected():
        nonlocal last_lid, guest
        last_lid, guest = None, False
        close_panel()

    bus.on_disconnect = disconnected
    close_panel()
    await bus.start()
    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(bus.recv(), 0.1)
            except TimeoutError:
                event = {}
            if owner_page:
                if event.get("t") == "body.lid":
                    last_lid = event
                if event.get("t") == "app.privacy":
                    guest = event.get("guest") is True
                if guest or guest_active():
                    if not hidden_for_guest:
                        close_panel()
                    hidden_for_guest = True
                    continue
                if hidden_for_guest:
                    event = last_lid or {}
                    hidden_for_guest = False
            if event.get("t") == "body.lid":
                sequence = event.get("seq")
                if event.get("closed") is not False:
                    open_sequence = opened_at = None
                    statuses.clear()
                elif type(sequence) is int and sequence >= 0 and sequence != open_sequence:
                    open_sequence = sequence
                    opened_at = time.monotonic()
                visible = event.get("closed") is False or event.get("show_face") is True
                if visible and type(sequence) is int and sequence >= 0:
                    current = str(sequence)
                    pending = directory / ".show"
                    pending.write_text(current + "\n", encoding="ascii")
                    pending.replace(show)
                else:
                    close_panel()
            if not bus.connected.is_set():
                statuses.clear()
                continue
            for filename, topic, fields in GATE_REQUESTS:
                try:
                    (directory / filename).unlink()
                except FileNotFoundError:
                    continue
                await bus.send(topic, **fields)
            try:
                stage, issued = statuses.popleft()
            except IndexError:
                pass
            else:
                if opened_at is not None and issued >= opened_at and time.monotonic() - issued <= install_status.FRESH_FOR:
                    await bus.send(install_status.TOPIC, stage=stage, issued=issued, lid_seq=open_sequence)
            if current is not None and acknowledged != current:
                try:
                    rendered = presented.read_text(encoding="ascii").strip()
                except FileNotFoundError:
                    continue
                if rendered == current:
                    await bus.send("panel.ready", seq=int(current))
                    acknowledged = current
    finally:
        await bus.stop()
        close_panel()


def main():
    runtime = os.environ.get("BEING_SCREEN_DIR")
    if not runtime:
        return local_menu.main()
    from .care_routes import take_page

    page = take_page()
    stop = threading.Event()
    # The disk writer leaves at most one current status, without waiting for
    # the panel, the bus, or playback. A newer status replaces an unsent one.
    statuses = deque(maxlen=1)
    token = install_status.sink.set(lambda stage: statuses.append((stage, time.monotonic())))
    panel = threading.Thread(
        target=lambda: asyncio.run(serve_panel(Path(runtime), stop, statuses, owner_page=page is not None)), daemon=True)
    panel.start()
    try:
        return local_menu.main(page=page)
    finally:
        install_status.sink.reset(token)
        stop.set()
        panel.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
