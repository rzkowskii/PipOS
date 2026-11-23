"""Which microphone channel the voice listens to.

Some MacBook internal microphones deliver speech on one channel of the stereo
capture and steady static on the other (the MacBookPro8,1 and MacBookAir5,2
do; Linux has the snd_hda_intel model=mbp81 fixup for them). A mono capture
through plughw averages both channels, which halves the speech and adds the
static. BEING_MIC_CHANNEL, or being.mic_channel= on the kernel command line,
chooses instead:

  mix    one channel from ALSA, both averaged (the default; as before)
  left   two channels, keep the left
  right  two channels, keep the right
  auto   two channels, averaged until one of them sounds like speech and the
         other does not, then keep that one

The automatic choice measures each channel's level in the speech band
(roughly 0.5 to 5 kHz) every 30 ms frame and smooths it into 150 ms blocks
with a median, so single clicks count for little. Over the last few seconds a
channel that carries speech rises and falls by 15 dB or more between its
pauses and its syllables. Steady static stays within a few dB. A channel is
kept only when it moves at least 8 dB more than the other one, or when the
other one is digital silence. Two channels that both carry the speech never
pass that test, so they stay averaged, unless they carry it in opposite phase:
then the average moves 8 dB less than a channel on its own, because averaging
cancels the speech, and one channel is kept instead. The choice is checked
again every second and every change is logged.
"""

import logging
import math
import os
from array import array
from collections import deque
from pathlib import Path

from .recognition import FRAME_SAMPLES, RATE, _sumprod, dbfs

CHOICES = ("mix", "left", "right", "auto")
DEFAULT = "mix"
CMDLINE = Path("/proc/cmdline")
CMDLINE_KEY = "being.mic_channel="
NAMES = ("left", "right")

FRAME_SECONDS = FRAME_SAMPLES / RATE
# Median of this many frames per block: 150 ms, shorter than a syllable.
BLOCK_FRAMES = 5
BLOCK_SECONDS = BLOCK_FRAMES * FRAME_SECONDS
# The blocks judged together: long enough to hold pauses and words.
WINDOW_SECONDS = 4.0
WINDOW_BLOCKS = round(WINDOW_SECONDS / BLOCK_SECONDS)
# The first judgement comes once the window is full, then once a second.
EVALUATE_BLOCKS = round(1.0 / BLOCK_SECONDS)
LOW_PERCENTILE = 0.10
HIGH_PERCENTILE = 0.90
# Speech moves its channel's level by at least this much (dB, 90th less 10th percentile).
SPEECH_MODULATION_DB = 15.0
# ...and by this much more than the channel left out.
MODULATION_MARGIN_DB = 8.0

LOG = logging.getLogger("being.voice.channel")


def kernel_choice(path=CMDLINE):
    """The being.mic_channel= value on the kernel command line, or None."""
    try:
        words = Path(path).read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return None
    # As with any repeated kernel argument, the last one counts.
    values = [word[len(CMDLINE_KEY):] for word in words if word.startswith(CMDLINE_KEY)]
    return values[-1] if values else None


def setting(environ=os.environ, cmdline=CMDLINE):
    """(choice, where it came from). The boot entry wins over /etc/being/env."""
    for raw, source in ((kernel_choice(cmdline), "the kernel command line"),
                        (environ.get("BEING_MIC_CHANNEL"), "BEING_MIC_CHANNEL")):
        if raw is None or not raw.strip():
            continue
        value = raw.strip().lower()
        if value in CHOICES:
            return value, source
        LOG.warning("microphone channel %r from %s is not one of %s; ignoring it",
                    raw, source, ", ".join(CHOICES))
    return DEFAULT, "the default"


def capture_channels(choice):
    return 1 if choice == "mix" else 2


def mono_command(command):
    """The same arecord command asking ALSA for one channel."""
    command = list(command)
    for index, word in enumerate(command[:-1]):
        if word == "-c":
            command[index + 1] = "1"
    return command


def band(samples):
    """One channel's frame filtered to the speech band, at a quarter of the rate.

    Summing sample pairs is a low-pass filter (null at 8 kHz) and the
    difference of neighbouring sums a high-pass one (null at 0 Hz, so no DC or
    hum). Together they pass roughly 0.5 to 5 kHz at a peak gain of about 4.
    """
    pairs = [a + b for a, b in zip(samples[0::2], samples[1::2])]
    return [b - a for a, b in zip(pairs, pairs[1:])]


def filtered_level(steps):
    """dBFS of a band() output."""
    return dbfs(math.sqrt(_sumprod(steps, steps) / len(steps)) / 4) if steps else dbfs(0)


def band_level(samples):
    """dBFS of one channel's frame in the speech band."""
    return filtered_level(band(samples))


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))]


class ChannelPicker:
    """Turns two-channel frames into the mono frames the recognizer expects."""

    def __init__(self, choice):
        if choice not in ("left", "right", "auto"):
            raise ValueError(f"no channel choice for {choice!r}")
        self.auto = choice == "auto"
        # None mixes both channels; 0 or 1 keeps that one.
        self.kept = None if self.auto else NAMES.index(choice)
        # Per channel, then the two averaged as a mono capture would hear them.
        self.frames = ([], [], [])
        self.peaks = [0, 0]
        self.blocks = (deque(maxlen=WINDOW_BLOCKS), deque(maxlen=WINDOW_BLOCKS), deque(maxlen=WINDOW_BLOCKS))
        self.block_peaks = (deque(maxlen=WINDOW_BLOCKS), deque(maxlen=WINDOW_BLOCKS))
        self.since_judged = 0
        self.judgements = 0

    @property
    def frame_bytes(self):
        return FRAME_SAMPLES * 2 * 2

    def describe(self):
        if not self.auto:
            return f"2 channels, keeping the {NAMES[self.kept]}"
        if self.kept is None:
            return "2 channels, averaged until one sounds like speech"
        return f"2 channels, keeping the {NAMES[self.kept]} by automatic choice"

    def mono(self, frame, measure=True):
        """One mono frame from one interleaved stereo frame."""
        samples = array("h")
        samples.frombytes(frame)
        channels = (samples[0::2], samples[1::2])
        if self.auto and measure:
            self.observe(*channels)
        if self.kept is not None:
            return channels[self.kept].tobytes()
        return array("h", [(a + b) >> 1 for a, b in zip(*channels)]).tobytes()

    def observe(self, left, right):
        filtered = (band(left), band(right))
        for channel, samples in enumerate((left, right)):
            self.frames[channel].append(filtered_level(filtered[channel]))
            self.peaks[channel] = max(self.peaks[channel], max(samples, default=0), -min(samples, default=0))
        # The filter is linear, so this is the speech band of the averaged channels.
        self.frames[2].append(filtered_level([(a + b) / 2 for a, b in zip(*filtered)]))
        if len(self.frames[0]) < BLOCK_FRAMES:
            return
        for channel in (0, 1, 2):
            self.blocks[channel].append(percentile(self.frames[channel], 0.5))
            self.frames[channel].clear()
        for channel in (0, 1):
            self.block_peaks[channel].append(self.peaks[channel])
            self.peaks[channel] = 0
        self.since_judged += 1
        full = len(self.blocks[0]) == WINDOW_BLOCKS
        if full and (self.judgements == 0 or self.since_judged >= EVALUATE_BLOCKS):
            self.since_judged = 0
            self.judge()

    def judge(self):
        """Keep a channel only on clear evidence; otherwise leave the choice as it is."""
        self.judgements += 1
        silent = [not any(self.block_peaks[channel]) for channel in (0, 1)]
        spread = [percentile(self.blocks[channel], HIGH_PERCENTILE) - percentile(self.blocks[channel], LOW_PERCENTILE)
                  for channel in (0, 1, 2)]
        best = 0 if spread[0] >= spread[1] else 1
        other = 1 - best
        mixed = spread[2]
        reason = None
        if silent[0] != silent[1]:
            best = silent.index(False)
            other = 1 - best
            reason = f"the {NAMES[other]} channel is digital silence"
        elif not any(silent) and spread[best] >= SPEECH_MODULATION_DB:
            if spread[best] - spread[other] >= MODULATION_MARGIN_DB:
                reason = (f"the {NAMES[best]} channel rises and falls like speech "
                          f"({spread[best]:.1f} dB against {spread[other]:.1f} dB on the {NAMES[other]})")
            elif self.kept is None and spread[best] - mixed >= MODULATION_MARGIN_DB:
                # Both carry the speech, but in opposite phase, so averaging
                # them cancels it. Either one will do; once one is kept, this
                # never swaps it for the other.
                reason = (f"both channels carry the speech but averaging them cancels it "
                          f"({spread[best]:.1f} dB on the {NAMES[best]} against {mixed:.1f} dB averaged)")
        if reason is None:
            if self.judgements == 1:
                LOG.info("microphone channels: neither stands out yet (left moves %.1f dB, right %.1f dB, "
                         "averaged %.1f dB); averaging both", spread[0], spread[1], mixed)
            return
        if best != self.kept:
            LOG.info("microphone channel: %s to the %s channel: %s",
                     "switching" if self.kept is not None else "listening", NAMES[best], reason)
            self.kept = best
