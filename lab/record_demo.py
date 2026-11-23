"""Record a first install and first life of PipOS in the lab VM.

Boots the Try USB image with a blank disk attached, installs through the
on-screen keyboard menu, boots the installed being, talks to it, puts it to
sleep, and wakes it again to show what it kept. The screen is captured the
whole way and joined into one captioned, time-lapse video.

    python3 -m lab.record_demo [output directory]

The being's ears and voice in the lab are a serial line, so the words do not
appear on its screen. The captions show what was said to it and what it said.
"""

import json
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import patch

from lab.tests.test_l2 import BOOT, MODEL_LOAD, REPLY
import textwrap

from lab.tests.test_local_screen import CONSOLE_FACES, console_glyphs, console_lines, visible_eyes
from lab.vm import ROOT, VM, machine_args, png

SCREEN_IMAGE = ROOT / "out/images/beingos-try-screen.img"
TARGET_BYTES = 16 << 30
PLAYBACK_FPS = 8


def captioned(frame, text):
    """The frame with `text` in a band added below it, in the console's own font."""
    width, height, rgb = frame
    face = 2 if width >= 1480 else 1 if width >= 750 else 0
    cell_w, cell_h, _ = CONSOLE_FACES[face]
    font = console_glyphs()[(cell_w, cell_h, "regular")]
    lines = [part for line in text.split("\n")
             for part in textwrap.wrap(line, max(10, (width - 4 * cell_w) // cell_w)) or [""]][:5]
    band = 7 * cell_h
    top = height
    height += band
    background = (8, 11, 18)
    pixels = bytearray(rgb) + bytes(background) * (width * band)
    ink = (214, 222, 233)
    shades = [bytes(round(b + (i - b) * level / 15) for b, i in zip(background, ink)) for level in range(16)]
    for index, line in enumerate(lines):
        y0 = top + cell_h + index * cell_h
        for column, char in enumerate(line):
            coverage = font.get(char) or font["?"]
            x0 = 2 * cell_w + column * cell_w
            for gy in range(cell_h):
                offset = ((y0 + gy) * width + x0) * 3
                for gx, level in enumerate(coverage[gy * cell_w:(gy + 1) * cell_w]):
                    if level and x0 + gx < width:
                        pixels[offset + gx * 3:offset + gx * 3 + 3] = shades[level]
    return width, height, bytes(pixels)


class Recorder:
    """Grabs the screen on a timer, whatever the script is waiting for."""

    def __init__(self, out):
        self.out = out
        self.frames = out / "frames"
        shutil.rmtree(self.frames, ignore_errors=True)
        self.frames.mkdir(parents=True)
        self.count = 0
        self.interval = 0.5
        self.latest = None
        self.captions = []  # (first frame, text)
        self.current = ""
        self.vm = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def watch(self, vm):
        """Record this machine. Its control channel is shared with the script."""
        # Wrap the machine's own method once. Wrapping again after a restart
        # would nest the lock and stop the recording.
        original = type(vm).qmp.__get__(vm)

        def locked(command, **arguments):
            with self._lock:
                return original(command, **arguments)

        vm.qmp = locked
        self.vm = vm

    def unwatch(self):
        self.vm = None
        time.sleep(1.0)

    def caption(self, text):
        print(f"[{self.count:5d}] {text}", flush=True)
        self.captions.append((self.count, text))
        self.current = text

    def shot(self, name):
        if self.latest is not None:
            (self.out / f"{name}.png").write_bytes(png(*self.latest))

    def _run(self):
        while not self._stop.is_set():
            vm = self.vm
            if vm is not None:
                try:
                    frame = vm.screen()
                    self.latest = frame
                    (self.frames / f"{self.count:06d}.png").write_bytes(png(*captioned(frame, self.current)))
                    self.count += 1
                except Exception:  # the machine is switching on or off
                    pass
            self._stop.wait(self.interval)

    def finish(self):
        self._stop.set()
        self._thread.join(timeout=10)
        if not self.count:
            return None
        (self.out / "captions.json").write_text(json.dumps(self.captions, indent=1))
        video = self.out / "beingos-first-life.mp4"
        # The face may run at a lower resolution than the console: fit both.
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(PLAYBACK_FPS),
             "-i", str(self.frames / "%06d.png"),
             "-vf", "scale=1280:968:force_original_aspect_ratio=decrease:flags=neighbor,"
                    "pad=1280:968:(ow-iw)/2:(oh-ih)/2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(video)],
            check=True)
        return video


def key(vm, name):
    vm.qmp("send-key", keys=[{"type": "qcode", "data": name}])


def select(vm, choice):
    key(vm, choice)
    key(vm, "ret")


def type_line(vm, text):
    for char in text:
        name = {" ": "spc", "/": "slash", ":": "semicolon"}.get(char, char.lower())
        names = ["shift", name] if char.isupper() or char == ":" else [name]
        vm.qmp("send-key", keys=[{"type": "qcode", "data": each} for each in names], **{"hold-time": 45})
        time.sleep(0.12)
    key(vm, "ret")


def wait_screen(rec, test, what, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = rec.latest
        if frame is not None and test(frame):
            return frame
        time.sleep(0.4)
    rec.shot("timeout")
    raise TimeoutError(f"the screen never showed {what}")


def wait_text(rec, phrase, timeout=90):
    return wait_screen(rec, lambda frame: any(phrase in line for line in console_lines(frame)), repr(phrase), timeout)


def install(rec, out):
    target = out / "installed-disk.raw"
    target.unlink(missing_ok=True)
    with target.open("wb") as disk:
        disk.truncate(TARGET_BYTES)

    def usb_boot_with_blank_disk(*args, **kwargs):
        command = machine_args(*args, **kwargs)
        boot_device = command.index("ide-hd,drive=disk0,bus=ide.0")
        del command[boot_device - 1:boot_device + 1]
        controller = command.index("qemu-xhci")
        command[controller] = "qemu-xhci,id=usb-controller"
        return command + [
            "-device", "usb-storage,drive=disk0,bus=usb-controller.0,bootindex=1",
            "-drive", f"if=none,id=target,format=raw,file={target}",
            "-device", "ide-hd,drive=target,bus=ide.0,bootindex=2,model=LAPTOP SSD",
        ]

    vm = VM(image=SCREEN_IMAGE)
    try:
        with patch("lab.vm.machine_args", usb_boot_with_blank_disk):
            vm.start()
        rec.watch(vm)
        rec.interval = 1.0
        rec.caption("Power on with the PipOS USB stick in. The laptop's own disk is blank.")
        vm.login(BOOT)
        wait_text(rec, "Install PipOS", 180)
        rec.interval = 0.4
        rec.caption("The USB starts with a keyboard menu: try it, install it, or restore a being from a backup.")
        rec.shot("01-try-menu")
        time.sleep(4)

        inventory = json.loads(vm.run("being-installer --list --json", 60)[1])
        device = next(disk["device"] for disk in inventory["disks"] if disk["eligible"])
        rec.caption("Choosing 2, Install PipOS. It lists the disks it is willing to write to.")
        select(vm, "2")
        wait_text(rec, "Disk number")
        rec.shot("02-choose-disk")
        time.sleep(4)
        select(vm, "1")
        answered = set()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            lines = console_lines(rec.latest)
            last = lines[-1] if lines else ""
            if "Type ERASE" in last:
                break
            if "[y/N]" in last and (len(lines), last) not in answered:
                answered.add((len(lines), last))
                select(vm, "n")
            time.sleep(0.5)
        else:
            raise TimeoutError("the installer never asked for the erase confirmation")
        rec.caption(f"Nothing is written until the exact words ERASE {device} are typed.")
        rec.shot("03-confirm-erase")
        time.sleep(5)
        type_line(vm, f"ERASE {device}")
        rec.interval = 1.0
        rec.caption("Installing: partitions, two system slots, the language model, and an empty mind.")
        wait_text(rec, "Press Enter to return", 900)
        rec.shot("04-installed")
        rec.caption("Installed. Back to the being that is running from the USB stick.")
        time.sleep(4)
        key(vm, "ret")
        wait_screen(rec, visible_eyes, "the eyes", 120)
        time.sleep(6)
        rec.caption("Shutting down so the USB stick can come out.")
        time.sleep(3)
        rec.unwatch()
        vm.send("poweroff\n")
        vm.wait_off(180)
    finally:
        rec.unwatch()
        vm.destroy()
    return target


def say(rec, vm, text, timeout=REPLY):
    rec.caption(f"You: {text}")
    reply = vm.converse(text, timeout)
    rec.caption(f"You: {text}\nBeing: {reply}")
    time.sleep(6)
    return reply


def wake(rec, vm):
    rec.interval = 1.5
    vm.login(BOOT)
    vm.wait_state("idle", MODEL_LOAD)
    deadline = time.monotonic() + MODEL_LOAD
    while time.monotonic() < deadline:
        if "cortex is answering" in vm.run("journalctl -b -u being-mind --no-pager -o cat")[1]:
            break
        time.sleep(5)
    else:
        raise TimeoutError("the model never started answering")
    rec.interval = 0.5


def live(rec, target):
    vm = VM(image=target)
    transcript = []
    try:
        vm.start()
        rec.watch(vm)
        rec.caption("First boot from the laptop's own disk. No login, no desktop: it wakes.")
        wake(rec, vm)
        rec.shot("05-first-wake")
        life = json.loads(vm.run("cat /mind/life.json")[1])
        rec.caption(f"It was born on this boot (life id {life['id'][:8]}), and its language model is running on the emulated 2012 CPU.")
        time.sleep(6)
        for text in (
            "Hello",
            "What is your name?",
            "Remember that my bike is called Zanzibarine",
            "Remember that the safe word is Quixotically",
            "How are you feeling right now?",
            "Forget about the safe word",
            "Yes",
            "What do you remember about the safe word?",
        ):
            transcript.append((text, say(rec, vm, text)))
        rec.shot("06-talking")
        rec.caption("You: Good night")
        vm.say("Good night")
        vm.wait_state("asleep", 120)
        rec.shot("07-asleep")
        rec.caption("Asleep. Now the power goes off completely.")
        time.sleep(8)
        rec.unwatch()
        vm.send("poweroff\n")
        vm.wait_off(180)
        rec.unwatch()
        vm.stop()

        vm.start()
        rec.watch(vm)
        rec.caption("Powered on again. Same disk, same being.")
        wake(rec, vm)
        again = json.loads(vm.run("cat /mind/life.json")[1])
        rec.caption(f"Same life id ({again['id'][:8]}), woken {again.get('wakes', '?')} times.")
        time.sleep(5)
        for text in ("What do you remember about my bike?", "How old are you?"):
            transcript.append((text, say(rec, vm, text)))
        rec.shot("08-remembers")
        vm.send("poweroff\n")
        vm.wait_off(180)
        vm.stop()
        rec.unwatch()
        gone = not vm.disk_contains(b"quixot")
        kept = vm.disk_contains(b"zanzibarin")
        transcript.append(("(raw disk scan)", f"forgotten word absent: {gone}; kept word present: {kept}"))
    finally:
        rec.unwatch()
        vm.destroy()
    return transcript


def main():
    out = (Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out/demo").resolve()
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorder(out)
    transcript, failure = [], None
    try:
        target = install(rec, out)
        transcript = live(rec, target)
    except Exception:
        failure = traceback.format_exc()
        print(failure, flush=True)
        rec.caption("The recording stopped here: see record.log.")
    video = rec.finish()
    (out / "transcript.json").write_text(json.dumps({"exchanges": transcript, "failure": failure}, indent=2))
    print(f"video: {video}", flush=True)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
