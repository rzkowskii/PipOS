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
A time synchronised over the network, or an RTC set right again (setting the
time writes the hardware clock too), makes it false.

Reading it costs one small sysfs read and a few stats.
"""

import os
import time
from pathlib import Path

RTC_SINCE_EPOCH = Path("/sys/class/rtc/rtc0/since_epoch")
# systemd's own clock epoch: the newest of these modification times.
EPOCH_FILES = (Path("/usr/lib/clock-epoch"), Path("/var/lib/systemd/timesync/clock"))
# timesyncd creates this once the clock was set from the network.
SYNCHRONISED = Path("/run/systemd/timesync/synchronized")
# Before this the clock has certainly been reset: this software did not exist.
CLOCK_FLOOR = 1_767_225_600  # 2026-01-01 UTC
# An RTC this far behind the system clock was not the clock's source. Wider
# than any time zone offset, for an RTC kept in local time.
BEHIND_SLACK = 86_400.0
# An RTC this close to the epoch files is a working clock: timesyncd touches
# its clock file while it runs, and the RTC counts in whole seconds and drifts.
EPOCH_SLACK = 3_600.0

LOST_CLOCK_LINE = "[concern] I lost track of time. What time is it?"


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


def epoch_advanced(*, rtc=RTC_SINCE_EPOCH, epochs=EPOCH_FILES, synchronised=SYNCHRONISED, now=None):
    """True when the system clock was moved past a hardware clock that had lost the time."""
    if Path(synchronised).exists():
        return False
    reading = rtc_seconds(rtc)
    if reading is None:
        return False  # no hardware clock to judge by is not a reason to doubt
    epoch = clock_epoch(epochs)
    if epoch is not None and reading + EPOCH_SLACK < epoch:
        return True
    now = time.time() if now is None else now
    return reading + BEHIND_SLACK < now


def doubted(*, now=None, **sources):
    """True when the wall clock cannot be believed: before 2026, or moved up to the epoch."""
    now = time.time() if now is None else now
    if now < CLOCK_FLOOR:
        return True
    try:
        return epoch_advanced(now=now, **sources)
    except Exception:  # a check that cannot run is not a reason to doubt
        return False
