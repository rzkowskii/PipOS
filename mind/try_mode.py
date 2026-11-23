"""Detect the USB session selected by the bootloader."""

from pathlib import Path


def is_try_boot():
    try:
        return "being.try=1" in Path("/proc/cmdline").read_text().split()
    except OSError:
        return False
