"""Give the local console the same lid and panel handoff as the face."""

import asyncio
import os
import threading
from pathlib import Path

from . import local_menu
from .bus import Bus


async def serve_panel(directory, stop):
    show = directory / "show"
    presented = directory / "presented"
    current = None
    acknowledged = None
    bus = Bus(
        "face", ("body.lid",),
        path=os.environ.get("BEING_BUS", "/run/being/bus.sock"),
    )

    def close_panel():
        nonlocal current, acknowledged
        current = acknowledged = None
        show.unlink(missing_ok=True)
        presented.unlink(missing_ok=True)

    bus.on_disconnect = close_panel
    close_panel()
    await bus.start()
    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(bus.recv(), 0.1)
            except TimeoutError:
                event = {}
            if event.get("t") == "body.lid":
                sequence = event.get("seq")
                visible = event.get("closed") is False or event.get("show_face") is True
                if visible and type(sequence) is int and sequence >= 0:
                    current = str(sequence)
                    pending = directory / ".show"
                    pending.write_text(current + "\n", encoding="ascii")
                    pending.replace(show)
                else:
                    close_panel()
            if not bus.connected.is_set():
                continue
            for filename, topic in (("mute", "key.mute"), ("recover", "key.panel_recover")):
                try:
                    (directory / filename).unlink()
                except FileNotFoundError:
                    continue
                await bus.send(topic)
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
    stop = threading.Event()
    panel = threading.Thread(target=lambda: asyncio.run(serve_panel(Path(runtime), stop)), daemon=True)
    panel.start()
    try:
        return local_menu.main()
    finally:
        stop.set()
        panel.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
