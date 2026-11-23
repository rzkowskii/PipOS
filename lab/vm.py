"""Run PipOS in QEMU and talk to it from the host.

The virtual machine models the laptop where it matters: an Ivy Bridge CPU (AVX
but no AVX2), two cores with four threads, a SATA disk, Intel HD Audio and UEFI
firmware. It is emulated, so it is slow, and its speed says nothing about the
real laptop.

As a program it opens the machine in a window, and your terminal becomes its
ears and voice: type a line to say it.

    python3 lab/vm.py            # keeps its disk (and so its memory) between runs
    python3 lab/vm.py --fresh    # start again from the built image
    python3 lab/vm.py --shell    # a root shell on the terminal instead
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = ROOT / "out" / "images" / "beingos.img"
IMAGE = Path(os.environ.get("BEING_FACTORY_IMAGE", DEFAULT_IMAGE)).resolve()
LAB_OUTPUT = Path(os.environ.get("BEING_LAB_OUTPUT", ROOT / "out" / "lab"))
QEMU = "qemu-system-x86_64"


def firmware():
    """QEMU's UEFI firmware: the code, and a template for its settings."""
    exe = shutil.which(QEMU)
    if exe is None:
        raise RuntimeError(f"{QEMU} is not installed")
    share = Path(exe).resolve().parent.parent / "share" / "qemu"
    code, settings = share / "edk2-x86_64-code.fd", share / "edk2-i386-vars.fd"
    for path in (code, settings):
        if not path.exists():
            raise RuntimeError(f"UEFI firmware not found: {path}")
    return code, settings


def machine_args(
    disk, settings, *, display, memory="4G", cpu="IvyBridge", threads="multi", sound="none",
    maker=None, update_media=None, loopback=False, microphone=None, audio_fixtures=None,
):
    """The virtual laptop. `maker` is (manufacturer, product), to make it claim
    to be some other machine."""
    code, _ = firmware()
    identity = []
    if maker:
        # QEMU separates options with commas and reads doubled commas literally.
        manufacturer, product = (part.replace(",", ",,") for part in maker)
        identity = ["-smbios", f"type=1,manufacturer={manufacturer},product={product}"]
    audio = []
    if microphone:
        audio += [
            "-fw_cfg", "name=opt/beingos/microphone,string=1",
            "-chardev", f"socket,id=mic,path={microphone},server=on,wait=off",
            "-device", "virtio-serial-pci",
            "-device", "virtserialport,chardev=mic,name=org.beingos.microphone",
        ]
    if loopback:
        audio += ["-fw_cfg", "name=opt/beingos/loopback,string=1"]
        if audio_fixtures:
            audio += ["-drive", f"if=virtio,format=raw,readonly=on,file=fat:ro:{audio_fixtures}"]
    elif not microphone:
        audio += ["-fw_cfg", "name=opt/beingos/serial,string=1"]
    return [
        QEMU,
        "-nodefaults",
        *identity,
        "-machine", "q35",
        # x86 on an ARM host is emulated. One host thread per virtual CPU is much
        # faster; QEMU warns that it is not strictly safe for x86 guests.
        "-accel", f"tcg,thread={threads}",
        "-cpu", cpu,
        "-smp", "4,sockets=1,cores=2,threads=2",
        "-m", memory,
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={settings}",
        # The q35 machine's own SATA controller, like the laptop's.
        # discard=unmap: when the guest trims freed blocks, they really go.
        "-drive", f"if=none,id=disk0,format=qcow2,discard=unmap,detect-zeroes=unmap,file={disk}",
        "-device", "ide-hd,drive=disk0,bus=ide.0",
        "-device", "virtio-gpu-pci,xres=1280,yres=800",
        "-display", "cocoa" if display else "none",
        "-device", "qemu-xhci",
        "-device", "usb-kbd",
        "-device", "usb-tablet",
        "-netdev", "user,id=net0",
        "-device", "e1000e,netdev=net0",
        "-audiodev", f"{sound},id=snd0",
        *audio,
        "-device", "intel-hda",
        "-device", "hda-duplex,audiodev=snd0",
        # A watchdog Linux can arm and QEMU can report independently.
        "-device", "i6300esb",
        "-watchdog-action", "reset",
        "-global", "ICH9-LPC.noreboot=off",
        "-rtc", "base=utc",
        *(
            ["-drive", f"if=virtio,format=raw,readonly=on,file={update_media}"]
            if update_media else []
        ),
    ]


def overlay(image, path):
    """A copy-on-write disk on top of the image, so the image is never changed."""
    # A discard must cover whole qcow2 clusters. Match the ext4 block size
    # so discarding even one freed block clears it from the virtual disk.
    subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", "-o", "cluster_size=4k", "-F", "raw", "-b", str(image), str(path)],
        check=True,
    )


class Timeout(Exception):
    pass


class Line:
    """One serial line of the machine: what it has printed, and a way to type."""

    def __init__(self, vm):
        self.vm = vm
        self.sock = None
        self._out = bytearray()
        self._lock = threading.Lock()
        self._cursor = 0

    def attach(self, sock):
        self.sock = sock
        with self._lock:
            self._out.clear()
            self._cursor = 0
        threading.Thread(target=self._read, args=(sock,), daemon=True).start()

    def _read(self, sock):
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._out += data

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def output(self):
        with self._lock:
            return self._out.decode("utf-8", "replace")

    def mark(self):
        """A place in the output, to read from later."""
        with self._lock:
            return len(self._out)

    def since(self, mark):
        with self._lock:
            return self._out[mark:].decode("utf-8", "replace")

    def send(self, text):
        self.sock.sendall(text.encode())

    def expect(self, pattern, timeout=60):
        """Wait for `pattern` in the output after the last match."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                text = self._out[self._cursor:].decode("utf-8", "replace")
                match = regex.search(text)
                if match:
                    self._cursor += len(text[:match.end()].encode("utf-8", "replace"))
                    return match
            if time.monotonic() > deadline:
                raise Timeout(f"no {pattern!r} within {timeout}s. Last output:\n{text[-1500:]}")
            if not self.vm.running():
                raise Timeout(f"the machine stopped while waiting for {pattern!r}. Last output:\n{text[-1500:]}")
            time.sleep(0.05)


class VM:
    """A running virtual machine.

    It has two serial lines. `shell` is a root shell, for looking inside.
    `voice` is the lab's stand-in for ears and a mouth: what is typed there is
    heard by the being, and what it says is printed there.
    """

    def __init__(
        self, image=IMAGE, *, memory="4G", cpu="IvyBridge", threads="multi", display=False,
        maker=None, update_media=None, loopback=False, audio_fixtures=None, microphone_port=False,
    ):
        self.image = Path(image)
        self.memory = memory
        self.cpu = cpu
        self.threads = threads
        self.display = display
        self.maker = maker
        self.update_media = update_media
        self.loopback = loopback
        self.audio_fixtures = audio_fixtures
        self.microphone_port = microphone_port
        self.microphone_path = None
        self.dir = None
        self.proc = None
        self.started = None
        self.shell = Line(self)
        self.voice = Line(self)
        self._qmp = None
        self._qmp_file = None
        self._qmp_events = []

    # -- life cycle ------------------------------------------------------

    def start(self, disk=None):
        """Power on. With `disk`, reuse that disk instead of a fresh one."""
        if not self.image.exists():
            raise RuntimeError(f"no image at {self.image}: run lab/build.sh first")
        # Refuse an image built from other source (BEING_ALLOW_STALE=1 overrides;
        # BEING_GATE=1 also refuses one with no recorded source-id).
        from lab import source_id
        source_id.enforce(self.image)
        if self.dir is None:
            # A short path: Unix socket paths are limited to about 100 bytes.
            self.dir = Path(tempfile.mkdtemp(prefix="being-vm-", dir="/tmp"))
        self.disk = Path(disk) if disk else self.dir / "disk.qcow2"
        if not self.disk.exists():
            overlay(self.image, self.disk)
        settings = self.dir / "uefi-settings.fd"
        if not settings.exists():
            shutil.copy(firmware()[1], settings)
        serial, voice, qmp = self.dir / "serial.sock", self.dir / "voice.sock", self.dir / "qmp.sock"
        if self.microphone_port:
            self.microphone_path = self.dir / "mic.sock"
            self.microphone_path.unlink(missing_ok=True)
        for path in (serial, voice, qmp):
            path.unlink(missing_ok=True)
        args = machine_args(
            self.disk, settings, display=self.display, memory=self.memory, cpu=self.cpu, threads=self.threads,
            maker=self.maker, update_media=self.update_media, loopback=self.loopback,
            audio_fixtures=self.audio_fixtures, microphone=self.microphone_path,
        ) + [
            # wait=on: the machine does not start until we are listening, so no
            # early output is lost.
            "-chardev", f"socket,id=serial0,path={serial},server=on,wait=on",
            "-serial", "chardev:serial0",
            "-chardev", f"socket,id=serial1,path={voice},server=on,wait=off",
            "-serial", "chardev:serial1",
            "-qmp", f"unix:{qmp},server=on,wait=off",
        ]
        self._log = open(self.dir / "qemu.log", "ab")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log)
        try:
            # QEMU holds everything, its control channel included, until the
            # first serial line has a listener. So: serial first, then the rest.
            self.shell.attach(self._connect(serial))
            self.started = time.monotonic()
            self.voice.attach(self._connect(voice))
            self._qmp = self._connect(qmp)
            self._qmp_file = self._qmp.makefile("rwb")
            self._qmp_events.clear()
            json.loads(self._qmp_file.readline())  # greeting
            self.qmp("qmp_capabilities")
        except Exception:
            self.power_cut()
            raise
        return self

    def _connect(self, path):
        deadline = time.monotonic() + 15
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("QEMU exited at once:\n" + (self.dir / "qemu.log").read_text()[-2000:])
            try:
                sock = socket.socket(socket.AF_UNIX)
                sock.connect(str(path))
                return sock
            except OSError:
                sock.close()
                if time.monotonic() > deadline:
                    raise RuntimeError(f"could not reach QEMU at {path}")
                time.sleep(0.05)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_off(self, timeout=90):
        """Wait for the machine to power itself off."""
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def power_cut(self):
        """Pull the plug: no warning, nothing flushed."""
        if self.running():
            self.proc.kill()
            self.proc.wait()
        self._close_sockets()

    def stop(self):
        """Power off politely if possible, then make sure it is gone."""
        if self.running():
            try:
                self.qmp("quit")
            except (OSError, ValueError, RuntimeError):
                pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._close_sockets()

    def _close_sockets(self):
        self.shell.close()
        self.voice.close()
        if self._qmp_file is not None:
            try:
                self._qmp_file.close()
            except OSError:
                pass  # the machine is already off; nothing left to flush to
            self._qmp_file = None
        if self._qmp is not None:
            try:
                self._qmp.close()
            except OSError:
                pass
        self._qmp = None
        if getattr(self, "_log", None):
            self._log.close()
            self._log = None

    def destroy(self):
        self.stop()
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.destroy()

    # -- control channel ---------------------------------------------------

    def qmp(self, command, **arguments):
        message = {"execute": command}
        if arguments:
            message["arguments"] = arguments
        self._qmp_file.write(json.dumps(message).encode() + b"\n")
        self._qmp_file.flush()
        while True:
            line = self._qmp_file.readline()
            if not line:
                raise RuntimeError("QEMU closed its control channel")
            reply = json.loads(line)
            if "return" in reply:
                return reply["return"]
            if "error" in reply:
                raise RuntimeError(f"{command}: {reply['error'].get('desc')}")
            if "event" in reply:
                self._qmp_events.append(reply)

    def wait_qmp_event(self, name, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.qmp("query-status")
            for i, event in enumerate(self._qmp_events):
                if event["event"] == name:
                    return self._qmp_events.pop(i)
            time.sleep(0.25)
        raise Timeout(f"QEMU did not report {name} within {timeout}s")

    def reset(self):
        """Press the reset button."""
        self.qmp("system_reset")

    def power_button(self):
        """Press the power button, as a person would."""
        self.qmp("system_powerdown")

    def screen(self):
        """What is on the screen now: (width, height, RGB bytes)."""
        path = self.dir / "screen.ppm"
        path.unlink(missing_ok=True)
        self.qmp("screendump", filename=str(path))
        for _ in range(100):
            if path.exists() and path.stat().st_size > 20:
                break
            time.sleep(0.02)
        width, height, rgb = read_ppm(path.read_bytes())
        if _is_placeholder(width, height, rgb):
            # QEMU's own "Display output is not active." notice: the guest has
            # the screen switched off. On real hardware that is a dark panel.
            rgb = bytes(len(rgb))
        return width, height, rgb

    def screenshot(self, path):
        """Save the screen as a PNG."""
        width, height, rgb = self.screen()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(png(width, height, rgb))
        return path

    # -- the shell line ----------------------------------------------------

    def output(self):
        return self.shell.output()

    def send(self, text):
        self.shell.send(text)

    def expect(self, pattern, timeout=60):
        return self.shell.expect(pattern, timeout)

    def login(self, timeout=180):
        """Log in on the serial line and quieten it for scripted use."""
        self.expect(r"login: ", timeout)
        self.send("root\n")
        self.expect(r"# ", 30)
        self.send("stty -echo cols 250; PS1=''; export TERM=dumb PAGER=cat SYSTEMD_PAGER=cat SYSTEMD_COLORS=0\n")
        time.sleep(0.3)

    def run(self, command, timeout=60):
        """Run a shell command inside the machine. Returns (exit status, output)."""
        # The markers are split in the text we type, so an echo of the command
        # line can never be mistaken for the output.
        self.send(f'echo "<<""begin>>"; {command}; echo "<<""end:$?>>"\n')
        self.expect(r"<<begin>>\r?\n", timeout)
        match = self.expect(r"(?s)(.*?)<<end:(\d+)>>", timeout)
        return int(match.group(2)), match.group(1).replace("\r\n", "\n").strip()

    def select_try_from_screen(self, timeout=90):
        """Dismiss the optional startup screen through the guest keyboard."""
        status, cmdline = self.run("cat /proc/cmdline", 15)
        if status:
            raise RuntimeError(f"could not read the guest's boot flags: {cmdline}")
        if "being.screen=1" not in cmdline.split():
            return

        deadline = time.monotonic() + timeout

        def wait_for(command, description):
            while time.monotonic() < deadline:
                status, _ = self.run(command, 15)
                if status == 0:
                    return
                time.sleep(0.25)
            raise Timeout(f"the guest did not {description} within {timeout}s")

        wait_for("test -s /run/being-screen/presented", "present its startup screen")
        for key in ("1", "ret"):
            self.qmp("send-key", keys=[{"type": "qcode", "data": key}])
        wait_for(
            "systemctl is-active --quiet being-face.service && "
            'test "$(systemctl show -p ActiveState --value being-local-screen.service)" = inactive',
            "return to the face after choosing Try PipOS",
        )

    def put(self, path, text):
        """Write a small text file inside the machine."""
        import base64

        data = base64.b64encode(text.encode()).decode()
        self.run(f": > {path}.b64")
        for i in range(0, len(data), 200):
            self.run(f"printf %s '{data[i:i + 200]}' >> {path}.b64")
        status, out = self.run(f"base64 -d {path}.b64 > {path} && rm {path}.b64")
        if status != 0:
            raise RuntimeError(f"could not write {path}: {out}")

    # -- the voice line ----------------------------------------------------

    def say(self, text):
        """Say something to the being."""
        self.voice.send(text + "\n")

    def wait_state(self, state, timeout=120):
        """Wait until the being reports entering `state`."""
        self.voice.expect(rf"\({re.escape(state)}\)", timeout)

    def spoken(self, mark=0):
        """What the being has said aloud since `mark`, as one string."""
        said = []
        for line in self.voice.since(mark).replace("\r", "").split("\n"):
            # Its speech is indented by two spaces; (state) notes are in brackets;
            # anything else is the echo of what was typed.
            if line.startswith("  ") and not line.strip().startswith("("):
                said.append(line.strip())
        return " ".join(said)

    def converse(self, text, timeout=600):
        """Say `text` and wait for the whole reply. Returns what it said."""
        mark = self.voice.mark()
        self.voice._cursor = mark
        self.say(text)
        self.wait_state("speaking", timeout)
        self.wait_state("attending", timeout)
        return self.spoken(mark)

    # -- the disk, seen from outside ---------------------------------------

    def disk_contains(self, needle):
        """True if `needle` (bytes, any letter case) is anywhere on the raw disk.
        The machine must be off."""
        if self.running():
            raise RuntimeError("power the machine off before reading its disk")
        raw = self.dir / "disk.raw"
        subprocess.run(["qemu-img", "convert", "-O", "raw", str(self.disk), str(raw)], check=True)
        needle = needle.lower()
        try:
            with open(raw, "rb") as f:
                tail = b""
                while True:
                    block = f.read(8 << 20)
                    if not block:
                        return False
                    if needle in (tail + block).lower():
                        return True
                    tail = block[-len(needle):]
        finally:
            raw.unlink(missing_ok=True)


# -- pictures ----------------------------------------------------------------

def _is_placeholder(width, height, rgb):
    """True if the only thing lit is one short line of grey text mid-screen."""
    left, right, cyan = eyes(width, height, rgb)
    boxes = [b for b in (left, right) if b]
    if not boxes or cyan > 0.0:
        return False
    top, bottom = min(b[1] for b in boxes), max(b[3] for b in boxes)
    return bottom - top < 24 and abs((top + bottom) / 2 - height / 2) < 40


def read_ppm(data):
    match = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not match:
        raise ValueError("not a PPM screen dump")
    width, height = int(match.group(1)), int(match.group(2))
    return width, height, data[match.end():match.end() + width * height * 3]


def png(width, height, rgb):
    def chunk(kind, body):
        raw = kind + body
        return len(body).to_bytes(4, "big") + raw + zlib.crc32(raw).to_bytes(4, "big")

    rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")


def lit(width, height, rgb, step=4):
    """Where the screen is lit. Returns (fraction lit, bounding box or None),
    sampling every `step` pixels. Lit means clearly brighter than black."""
    count = total = 0
    box = None
    for y in range(0, height, step):
        row = y * width * 3
        for x in range(0, width, step):
            i = row + x * 3
            total += 1
            if rgb[i] + rgb[i + 1] + rgb[i + 2] > 120:
                count += 1
                box = (x, y, x, y) if box is None else (min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y))
    return count / max(total, 1), box


def eyes(width, height, rgb, step=4):
    """Find the two eyes on the screen.

    Returns (left box, right box, cyan), where each box is (x0, y0, x1, y1) or
    None, and `cyan` is the fraction of lit pixels that are the eyes' colour
    rather than, say, white boot text.
    """
    boxes = [None, None]
    lit_count = cyan = 0
    for y in range(0, height, step):
        row = y * width * 3
        for x in range(0, width, step):
            i = row + x * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if r + g + b <= 120:
                continue
            lit_count += 1
            if b > r + 30 and g > r:
                cyan += 1
            side = 0 if x < width // 2 else 1
            box = boxes[side]
            boxes[side] = (x, y, x, y) if box is None else (min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y))
    return boxes[0], boxes[1], (cyan / lit_count if lit_count else 0.0)


# -- running it by hand ------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Open PipOS in a virtual machine.")
    parser.add_argument("--image", type=Path, default=IMAGE, help="factory image for a separate lab disk")
    parser.add_argument("--fresh", action="store_true", help="discard its disk and start from the built image")
    parser.add_argument("--no-window", action="store_true", help="no screen, serial line only")
    parser.add_argument("--sound", action="store_true", help="connect its sound card to this Mac's speakers and mic")
    parser.add_argument("--mic-input", default="default", help="AVFoundation microphone name or index for --sound")
    parser.add_argument("--shell", action="store_true", help="put a root shell on this terminal, not the conversation")
    parser.add_argument("--memory", default="4G")
    parser.add_argument("--update-media", type=Path, help="attach a read-only raw disk with update bundles")
    args = parser.parse_args()
    if args.sound and not shutil.which("ffmpeg"):
        parser.error("--sound requires ffmpeg for microphone capture")

    home = ROOT / "out" / "vm"
    image = args.image.resolve()
    if image != DEFAULT_IMAGE.resolve():
        home /= hashlib.sha256(os.fsencode(image)).hexdigest()[:12]
    home.mkdir(parents=True, exist_ok=True)
    disk, settings = home / "being.qcow2", home / "uefi-settings.fd"
    if args.fresh or not disk.exists():
        if not image.exists():
            sys.exit(f"no image at {image}: run lab/build.sh first")
        # Interactive memory must outlive rebuilds of the factory image.
        with tempfile.TemporaryDirectory(dir=home) as work:
            new_disk = Path(work) / disk.name
            subprocess.run(
                ["qemu-img", "convert", "-O", "qcow2", "-o", "cluster_size=4k", str(image), str(new_disk)],
                check=True,
            )
            new_disk.replace(disk)
        if args.fresh:
            settings.unlink(missing_ok=True)
    if not settings.exists():
        shutil.copy(firmware()[1], settings)
    shell = home / "shell.sock"
    shell.unlink(missing_ok=True)
    microphone = home / "mic.sock" if args.sound else None
    if microphone:
        microphone.unlink(missing_ok=True)
    command = machine_args(
        disk, settings, display=not args.no_window, memory=args.memory, sound="coreaudio" if args.sound else "none",
        update_media=args.update_media, microphone=microphone,
    )
    if args.shell or args.sound:
        command += ["-serial", "mon:stdio", "-serial", "null"]
        print("A root shell is on this terminal (log in as root). Ctrl-A then X quits.\n")
    else:
        command += [
            "-chardev", f"socket,id=serial0,path={shell},server=on,wait=off", "-serial", "chardev:serial0",
            "-serial", "mon:stdio",
        ]
        print("This terminal is its ears and voice: type a line to say it. Ctrl-A then X quits.")
        print("It takes a minute to wake, and it thinks slowly here: the CPU is emulated.\n")
    if args.sound:
        import asyncio
        if __package__:
            from .mic_bridge import run
        else:
            from mic_bridge import run

        print("When the eyes open, say your wake phrase (Hey Pip by default). M mutes and Esc stops a reply.")
        try:
            sys.exit(asyncio.run(run(command, microphone, args.mic_input)))
        except (KeyboardInterrupt, asyncio.CancelledError):
            sys.exit(130)
        finally:
            microphone.unlink(missing_ok=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))   # `from lab import source_id` when run as lab/vm.py
    main()
