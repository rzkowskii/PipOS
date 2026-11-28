"""Stage and unlock PIN-protected installed memory on the local console."""

import hmac
import os
import resource
import stat
import subprocess
import sys
import termios
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import console_ui as ui
from .install_binding import PARTUUIDS, installed_mapping, verify_partitions
from .install_disks import node_number

CRYPTSETUP = "/usr/sbin/cryptsetup"
MAPPER_NAME = "pipos-mind"
MAPPER_DIR = Path("/dev/mapper")
MIN_DIGITS = 12
MAX_DIGITS = 32


def valid_pin(pin):
    return (isinstance(pin, bytearray) and MIN_DIGITS <= len(pin) <= MAX_DIGITS
            and all(48 <= digit <= 57 for digit in pin))


def wipe(pin):
    if pin is not None:
        pin[:] = b"\0" * len(pin)
        pin.clear()


def read_pin(prompt):
    """Read local digits into mutable storage with TTY echo disabled."""
    descriptor = sys.stdin.fileno()
    if not os.isatty(descriptor):
        raise ValueError("The PIN must be entered on the local keyboard.")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    original = termios.tcgetattr(descriptor)
    hidden = termios.tcgetattr(descriptor)
    hidden[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
    hidden[6][termios.VMIN] = 1
    hidden[6][termios.VTIME] = 0
    pin = bytearray(MAX_DIGITS)
    key = bytearray(1)
    used = 0
    overflow = 0
    invalid = False
    termios.tcsetattr(descriptor, termios.TCSAFLUSH, hidden)
    try:
        print(prompt, end="", flush=True)
        while os.readv(descriptor, [key]):
            code = key[0]
            key[0] = 0
            if code == 27:
                return None
            if code in (10, 13):
                return bytearray() if invalid or overflow else bytearray(memoryview(pin)[:used])
            if code in (8, 127):
                if overflow:
                    overflow -= 1
                elif used:
                    used -= 1
                    pin[used] = 0
            elif 48 <= code <= 57:
                if used == MAX_DIGITS:
                    overflow += 1
                else:
                    pin[used] = code
                    used += 1
            else:
                invalid = True
        return None
    except KeyboardInterrupt:
        return None
    finally:
        wipe(key)
        wipe(pin)
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, original)
        print(flush=True)


def match_pin(first, second):
    return valid_pin(first) and valid_pin(second) and hmac.compare_digest(first, second)


def new_pin():
    first = read_pin(ui.prompt("New PIN (12–32 digits, hidden): "))
    try:
        if not valid_pin(first):
            raise ValueError("PIN cancelled or invalid. The disk was left untouched.")
        second = read_pin(ui.prompt("Enter the PIN again (hidden): "))
        try:
            if not match_pin(first, second):
                raise ValueError("The PINs did not match. The disk was left untouched.")
        finally:
            wipe(second)
        return first
    except BaseException:
        wipe(first)
        raise


def crypt(*args, pin=None, descriptor=None):
    options = {"stdin": subprocess.DEVNULL} if pin is None else {"input": pin}
    return subprocess.run([CRYPTSETUP, *map(str, args)], **options, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=45, check=False,
                          pass_fds=() if descriptor is None else (descriptor,))


def require_success(result, message):
    if result.returncode:
        raise OSError(message)


def check_mapping(mapping):
    if not stat.S_ISBLK(mapping.stat().st_mode):
        raise OSError("The memory mapping is unavailable.")


@contextmanager
def format_image(path, partuuid, sector, pin):
    """Format only a private regular file in the existing RAM install stage."""
    path = Path(path)
    info = path.lstat()
    if (not isinstance(partuuid, uuid.UUID) or not partuuid.int or not valid_pin(pin)
            or sector not in (512, 4096) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or path.parent.parent != Path("/run") or not path.parent.name.startswith("being-install-")
            or info.st_nlink != 1):
        raise ValueError("Invalid protected memory staging input.")
    name = f"pipos-install-{partuuid}"
    mapping = MAPPER_DIR / name
    if os.path.lexists(mapping):
        raise OSError("Protected memory is already being staged.")
    require_success(crypt("--batch-mode", "--type", "luks2", "--uuid", partuuid,
                          "--sector-size", sector, "--pbkdf", "argon2id", "--pbkdf-memory", 131072,
                          "--pbkdf-parallel", 1, "--iter-time", 1500, "--key-file", "-",
                          "luksFormat", path, pin=pin), "Could not format the encrypted memory image.")
    try:
        require_success(crypt("--type", "luks2", "--key-file", "-", "open", path, name, pin=pin),
                        "Could not open the encrypted memory image.")
        check_mapping(mapping)
        yield mapping
    finally:
        if os.path.lexists(mapping):
            require_success(crypt("close", name), "Could not close the encrypted memory image.")


@contextmanager
def installed_partition(partuuid):
    cmdline = Path("/proc/cmdline").read_text()
    guids, active = installed_mapping(cmdline)
    if guids[-1] != partuuid or "being.mind_crypt=1" not in cmdline.split():
        raise ValueError("The encrypted memory selection could not be verified.")
    root = os.stat("/").st_dev
    verify_partitions(guids, active, f"{os.major(root)}:{os.minor(root)}")
    path = PARTUUIDS / partuuid
    number = node_number(str(path))
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        actual = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
        if not stat.S_ISBLK(info.st_mode) or actual != number:
            raise ValueError("The encrypted memory device changed.")
        # Keep the verified kernel device pinned across the cryptsetup request.
        pinned = f"/proc/self/fd/{descriptor}"
        require_success(crypt("--type", "luks2", "isLuks", pinned, descriptor=descriptor),
                        "The installed memory volume has no readable LUKS2 header.")
        identity = crypt("luksUUID", pinned, descriptor=descriptor)
        if identity.returncode or identity.stdout.strip() != partuuid.encode("ascii"):
            raise ValueError("The encrypted memory header did not match the installed drive.")
        yield pinned, descriptor
    finally:
        os.close(descriptor)


def unlock(partuuid):
    failures = 0
    if os.path.lexists(MAPPER_DIR / MAPPER_NAME):
        raise OSError("The installed memory mapping already exists.")
    # Verify the header before asking the owner for a PIN.
    with installed_partition(partuuid):
        pass
    while True:
        ui.screen("Unlock memory", context="PipOS", subtitle="Your installed memory is encrypted.")
        ui.line("Enter your PIN to start PipOS. The digits stay hidden.")
        ui.line("Press Esc to power off.")
        if failures:
            ui.line(ui.mark("warning", "That PIN did not unlock your memory."))
        pin = read_pin(ui.prompt("PIN: "))
        if pin is None:
            subprocess.run(["systemctl", "--no-block", "poweroff"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            return 1
        try:
            if valid_pin(pin):
                with installed_partition(partuuid) as (path, descriptor):
                    opened = crypt("--type", "luks2", "--allow-discards", "--key-file", "-",
                                   "open", path, MAPPER_NAME, pin=pin, descriptor=descriptor)
                if not opened.returncode:
                    check_mapping(MAPPER_DIR / MAPPER_NAME)
                    return 0
                if opened.returncode != 2:
                    raise OSError("Could not open the encrypted memory device.")
        finally:
            wipe(pin)
        failures += 1
        time.sleep(min(failures, 5))


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "unlock" or os.geteuid() != 0:
        return 2
    try:
        return unlock(sys.argv[2])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"PipOS memory remains locked. {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
