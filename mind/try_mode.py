"""Detect the USB session selected by the bootloader."""

import os
import stat
from pathlib import Path


VOICE_DEBUG_KEY = "being.voice_debug="
CMDLINE = Path("/proc/cmdline")
SLOT_GOOD = Path("/run/being-slot-good")
SLOT_CONTINUITY_WARNING = Path("/run/being-slot-continuity-warning")


def continuity_warning_slot(path=SLOT_CONTINUITY_WARNING):
    try:
        with path.open("rb") as marker:
            value = marker.read(3)
    except OSError:
        return None
    return value[:1].decode("ascii") if value in (b"A\n", b"B\n") else None


def voice_debug_words(words):
    """True when kernel command line `words` put what was heard into the log:
    a Try boot whose last being.voice_debug= is 1. As with any repeated
    kernel argument the last one counts, so being.voice_debug=0 typed at the
    boot prompt undoes the entry's =1 (the voice's own rule)."""
    values = [word[len(VOICE_DEBUG_KEY):] for word in words if word.startswith(VOICE_DEBUG_KEY)]
    return values[-1:] == ["1"] and "being.try=1" in words


def is_try_boot():
    try:
        return "being.try=1" in CMDLINE.read_text().split()
    except OSError:
        return False


def installed_trial_pending(root):
    """Whether this is the shared installed mind of a trial that is still running."""
    if Path(root).resolve() != Path("/mind"):
        return False
    try:
        args = CMDLINE.read_text().split()
    except OSError:
        # An installed mind whose kernel state is unavailable must not migrate.
        return True
    if "being.installed=1" not in args or "being.trial=1" not in args:
        return False
    slots = [arg.removeprefix("rauc.slot=") for arg in args if arg.startswith("rauc.slot=")]
    if len(slots) != 1 or slots[0] not in ("A", "B"):
        return True
    try:
        descriptor = os.open(SLOT_GOOD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as ready:
            info = os.fstat(ready.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return True
            return ready.read(8) != f"{slots[0]}\n".encode("ascii")
    except OSError:
        return True
