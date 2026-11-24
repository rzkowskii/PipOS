"""Detect the USB session selected by the bootloader."""

from pathlib import Path


VOICE_DEBUG_KEY = "being.voice_debug="


def voice_debug_words(words):
    """True when kernel command line `words` put what was heard into the log:
    a Try boot whose last being.voice_debug= is 1. As with any repeated
    kernel argument the last one counts, so being.voice_debug=0 typed at the
    boot prompt undoes the entry's =1 (the voice's own rule)."""
    values = [word[len(VOICE_DEBUG_KEY):] for word in words if word.startswith(VOICE_DEBUG_KEY)]
    return values[-1:] == ["1"] and "being.try=1" in words


def is_try_boot():
    try:
        return "being.try=1" in Path("/proc/cmdline").read_text().split()
    except OSError:
        return False
