"""Did this boot's clock come from systemd's epoch instead of a working clock?

An old laptop's clock battery dies. On the first real boot the hardware clock
(RTC) read 2022-01-03, and systemd moved the system time forward to its
built-in epoch, 2026-10-17, because time may never run before the date the
software was built. The real date was 2026-11-23. A clock like that looks
plausible (it is after 2026), so a check of the year alone misses it.

The evidence stays on the machine: the RTC keeps counting from its wrong
time. `epoch_advanced()` is true when the RTC's `since_epoch` is before
systemd's clock epoch (by more than an hour, so a clock file touched a moment
ago by timesyncd does not count), or, when the epoch is the one compiled into systemd
and so has no file, when the RTC is more than a day behind the system clock.
A time synchronised over the network, an owner's confirmed date this boot,
or a working RTC makes it false. A trusted floor survives power loss in /mind.

Reading it costs one small sysfs read and a few stats.
"""

import fcntl
import math
import os
import stat
import time
from pathlib import Path

from .storage import atomic_write

RTC_SINCE_EPOCH = Path("/sys/class/rtc/rtc0/since_epoch")
# systemd's own clock epoch: the newest of these modification times.
EPOCH_FILES = (Path("/usr/lib/clock-epoch"), Path("/var/lib/systemd/timesync/clock"))
# timesyncd creates this once the clock was set from the network.
SYNCHRONISED = Path("/run/systemd/timesync/synchronized")
# Before this the clock has certainly been reset: this software did not exist.
CLOCK_FLOOR = 1_767_225_600  # 2026-01-01 UTC
MAX_CLOCK = 4_133_980_799  # 2100-12-31 UTC, the date Setup supports
FLOOR_FILE = "clock-floor"
CONFIRMED = Path("/run/being/clock-confirmed")
# An RTC this far behind the system clock was not the clock's source. Wider
# than any time zone offset, for an RTC kept in local time.
BEHIND_SLACK = 86_400.0
# An RTC this close to the epoch files is a working clock: timesyncd touches
# its clock file while it runs, and the RTC counts in whole seconds and drifts.
EPOCH_SLACK = 3_600.0

LOST_CLOCK_LINE = "[concern] I lost track of time. What time is it?"


def floor(state_dir=None):
    """Read the separately stored system clock floor without creating state."""
    if state_dir is not None:
        try:
            fd = os.open(Path(state_dir) / FLOOR_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_uid == os.geteuid():
                    raw = handle.read(16).strip()
                    if raw.isdigit():
                        stamp = int(raw)
                        if CLOCK_FLOOR <= stamp <= MAX_CLOCK:
                            return stamp
        except OSError:
            pass
    return CLOCK_FLOOR


def remember(state_dir, stamp, *, correct_date=False, wait=0):
    """Advance the trusted floor, or replace it after a confirmed date correction."""
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or not CLOCK_FLOOR <= stamp <= MAX_CLOCK):
        raise ValueError("The confirmed time is outside the supported date range.")
    root = Path(state_dir)
    current = int(stamp)
    fd = os.open(root / ".clock-floor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lock = os.fstat(fd)
        if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.geteuid() or lock.st_mode & 0o077:
            raise OSError("The clock-floor lock is not private.")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.025, remaining))
        previous = floor(root)
        if current > previous or (correct_date and current != previous):
            atomic_write(root / FLOOR_FILE, f"{current}\n", mode=0o600)
    finally:
        os.close(fd)


def _boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, "CLOCK_BOOTTIME") else time.monotonic()


def confirm_this_boot(stamp, path=CONFIRMED):
    """Tie an owner-confirmed clock reading to this boot's elapsed time."""
    if Path(path).parent.is_dir():
        atomic_write(path, f"{int(stamp)} {_boottime():.3f}\n", mode=0o600)


def confirmed_now(now=None, path=CONFIRMED):
    if path is None:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return False
            stamp, anchor = map(float, handle.read(64).split())
        elapsed = _boottime() - anchor
        now = time.time() if now is None else now
        return CLOCK_FLOOR <= stamp <= MAX_CLOCK and elapsed >= 0 and abs(now - stamp - elapsed) <= 60
    except (OSError, ValueError):
        return False


def rtc_seconds(path=RTC_SINCE_EPOCH):
    """The hardware clock in Unix seconds, or None when there is none to read."""
    try:
        raw = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(raw) if raw.isdigit() else None


def clock_epoch(files=EPOCH_FILES):
    """systemd's clock epoch from its files, or None when it is only compiled in."""
    stamps = []
    for path in files:
        try:
            stamps.append(os.stat(path).st_mtime)
        except OSError:
            continue
    return max(stamps) if stamps else None


def epoch_advanced(*, rtc=RTC_SINCE_EPOCH, epochs=EPOCH_FILES, synchronised=SYNCHRONISED,
                   confirmed=CONFIRMED, now=None):
    """True when the system clock was moved past a hardware clock that had lost the time."""
    now = time.time() if now is None else now
    if Path(synchronised).exists() or confirmed_now(now, confirmed):
        return False
    reading = rtc_seconds(rtc)
    if reading is None:
        return False  # no hardware clock to judge by is not a reason to doubt
    epoch = clock_epoch(epochs)
    if epoch is not None and reading + EPOCH_SLACK < epoch:
        return True
    return reading + BEHIND_SLACK < now


def doubted(*, now=None, state_dir=None, **sources):
    """True when the wall clock cannot be believed: before 2026, or moved up to the epoch."""
    now = time.time() if now is None else now
    if now < floor(state_dir) or now > MAX_CLOCK:
        return True
    try:
        return epoch_advanced(now=now, **sources)
    except Exception:  # a check that cannot run is not a reason to doubt
        return False
