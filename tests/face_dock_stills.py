"""Stills of the dock from the real face binary, 1280x800, run hidden against the real brainstem.

Not a unit test (it takes a minute and needs both built:  cargo build). It is how the dock is
looked at on a host, and how an image's face can be checked the same way:

    python3 tests/face_dock_stills.py OUTDIR [scene ...]

Scenes: collapsed, faded, written_voice, written_only, menu_open, guest. Each writes
OUTDIR/NAME.png (the face's own render) and, with ImageMagick, NAME-labelled.png with a caption.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mind.bus import Bus  # noqa: E402
FACE = ROOT / "target" / "debug" / "face"
BRAINSTEM = ROOT / "target" / "debug" / "brainstem"
SIZE = "1280x800"
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"  # ImageMagick has no default font here
TOKEN = "r1"

CAPTIONS = {
    "collapsed": "Collapsed dock over the eyes (Spoken). Host render, real face binary, 1280x800.",
    "faded": "The dock faded to a pill after 20 s without use. Host render.",
    "written_voice": "Written + voice: a typed line in the box, Pip's last reply shown as its words were played. Host render.",
    "written_only": "Written only: a text confirmation, answered by typing yes. Host render.",
    "menu_open": "Quick Menu open: the eyes dock to the strip and the dock is not drawn. Host render.",
    "guest": "Guest mode (a written mode was on when the guest arrived): no dock, no captions. Host render.",
}


async def start_stem(directory):
    path = os.path.join(directory, "bus.sock")
    stem = subprocess.Popen([str(BRAINSTEM), "--bus", path, "--require", "mind,voice"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(250):
        if os.path.exists(path):
            break
        await asyncio.sleep(0.02)
    return stem, path


async def scene(name, out):
    with tempfile.TemporaryDirectory(prefix="dstill-", dir="/tmp") as directory:
        stem, path = await start_stem(directory)
        mind = Bus("mind", ["state.changed"], path=path)
        voice = Bus("voice", [], path=path)
        chat = Bus("chat", [], path=path)
        buses = (mind, voice, chat)
        face = None
        try:
            for bus in buses:
                await bus.start()
                await asyncio.wait_for(bus.connected.wait(), 5)
            await mind.send("mind.ready", cortex=True)
            written = name in ("written_voice", "written_only", "guest")
            text = {"written_voice": "what is the weather tomorrow", "written_only": "yes"}.get(name)
            seconds = 24.0 if name == "faded" else 4.5
            command = [str(FACE), "--hidden", "--bus", path, "--exit-after", str(seconds),
                       "--record", str(out / f"{name}.png"), "--size", SIZE]
            if text:
                command += ["--dock-text", text]
            face = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            await asyncio.sleep(1.5)  # connected, awake
            if written:
                await chat.send("chat.mode", mode="typed_text" if name == "written_only" else "typed_voice",
                                input="typed", reply="text" if name == "written_only" else "voice")
                await asyncio.sleep(0.4)
            if name in ("written_voice", "written_only"):
                reply = ("Clear the shopping list? That cannot be undone." if name == "written_only"
                         else "Tomorrow will be sunny, with a high of twenty four.")
                await mind.send("say.request", text=reply, reply=TOKEN, seq=1, last=True)
                await asyncio.sleep(0.2)
                await voice.send("say.played", reply=TOKEN, seq=1)
            if name == "menu_open":
                await mind.send("screen.quick_menu", open=True)
            if name == "guest":
                await mind.send("app.privacy", guest=True)
            output, error = await asyncio.wait_for(face.communicate(), seconds + 25)
            if face.returncode != 0:
                raise RuntimeError(f"{name}: face exited {face.returncode}: {error.decode()[-400:]}")
        finally:
            if face is not None and face.returncode is None:
                face.kill()
            for bus in buses:
                await bus.stop()
            if stem.poll() is None:
                stem.terminate()
            stem.wait(timeout=5)
    label(out, name)


def label(out, name):
    magick = shutil.which("magick")
    if magick and Path(FONT).exists():
        subprocess.run([magick, str(out / f"{name}.png"), "-background", "#101820", "-fill", "#d9f0f7",
                        "-gravity", "south", "-font", FONT, "-pointsize", "20",
                        "-splice", "0x44", "-annotate", "+0+10", CAPTIONS[name],
                        str(out / f"{name}-labelled.png")], capture_output=True)


async def main(argv):
    if not (FACE.exists() and BRAINSTEM.exists()):
        raise SystemExit("not built: run `cargo build`")
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    for name in (argv[1:] or list(CAPTIONS)):
        await scene(name, out)
        print("rendered", out / f"{name}.png", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1:]))
