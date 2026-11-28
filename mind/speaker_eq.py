"""Shape Pip's speech for the MacBookPro9,2's own speakers before playback.

Measured on that laptop (Cirrus Logic CS4206, both speaker pins on, Master
108 as being-audio-init sets it at boot), relative to 1 kHz at the chassis
microphone:

    200 Hz -41 dB   250 Hz -34 dB   317 Hz -26 dB   400 Hz -15.5 dB
    504 Hz -12 dB   635 Hz to 7.2 kHz within about +5/-7 dB

The speakers reproduce almost nothing below about 550 Hz, and what they get
below 400 Hz they turn into distortion: at -1 dBFS the THD at 250, 315 and
400 Hz is 33, 18.5 and 7.6 % at Master 108 (23, 12 and 4.7 % at Master 100),
genuine second and third order growth, while 1 kHz stays under 0.6 % up to
full drive. The harmonics land between 500 Hz and 1.2 kHz, where the speakers
are loudest. Half of the Alba voice's energy lies below 300 Hz (her voice's
fundamental), so on these speakers about half of what Pip sends becomes
distortion and almost none of it becomes sound.

The filter here removes that band before it reaches the amplifier:

- a Butterworth high-pass, 4th order (two biquads) at 350 Hz by default:
  about -19 dB at 200 Hz, -12 dB at 250 Hz, -1.3 dB at 400 Hz and nothing
  from 600 Hz up, so the band the speakers can play is untouched;
- an optional gentle presence lift (a peaking biquad, 2.2 kHz by default),
  off unless asked for, for the 1.6-2.6 kHz dip of a few dB in the response;
- an optional makeup gain;
- a look-ahead peak limiter with a -1 dBFS ceiling. A high-pass changes the
  phase of what it keeps, so peaks can rise (Alba's rose from 29205 to 32239
  with a 300 Hz filter); the limiter turns those few peaks down smoothly over
  a few milliseconds instead of letting them clip, and leaves everything else
  at the level the voice wrote it.

It is the standard library only: an IIR filter in a plain loop over an
array of 16-bit samples, under a tenth of real time on the laptop's i5 for
16-22 kHz mono speech (see process_wav). Each clause is filtered on its own; the filter
starts at rest, and clauses begin and end in silence, so nothing clicks.

It is on by default only on the MacBookPro9,2 (the DMI product name), where
it was measured. The environment variable BEING_SPEAKER_EQ=0|1 wins over the
kernel argument being.speaker_eq=0|1, which wins over the product name.

For listening on the laptop itself (the mind package is in /usr/lib/being,
which is not on Python's path by default, and /usr is read-only, so write
under /run or /tmp):

    PYTHONPATH=/usr/lib/being python3 -m mind.speaker_eq IN.wav /run/OUT.wav \
        [--cutoff HZ] [--order N] [--lift DB]
"""

import argparse
import cmath
import math
import os
import struct
import sys
import tempfile
import time
import wave
from array import array
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

DMI_PRODUCT = Path("/sys/class/dmi/id/product_name")
CMDLINE = Path("/proc/cmdline")
CMDLINE_KEY = "being.speaker_eq="
ENV = "BEING_SPEAKER_EQ"
# Where the speakers were measured and the filter is known to help.
MEASURED_PRODUCTS = ("MacBookPro9,2",)
_ON = ("1", "on", "yes", "true")
_OFF = ("0", "off", "no", "false")

# Sample rates the filter is designed for; speech clauses are 16-22.05 kHz.
MIN_RATE, MAX_RATE = 8000, 96000
# A clause longer than this is refused rather than held in memory.
MAX_SECONDS = 600
FULL_SCALE = 32767

# The look-ahead limiter works on blocks this long: the gain is worked out per
# block from its peak and the next one's, then ramped linearly between block
# edges, so the gain is never above what the block needs.
LIMIT_BLOCK_S = 0.002
# How fast the gain may fall (before a peak) and rise again (after it).
LIMIT_ATTACK_DB_PER_S = 1500.0
LIMIT_RELEASE_DB_PER_S = 100.0


@dataclass(frozen=True)
class Settings:
    """One filter setting. `cutoff` 0 leaves out the high-pass, `lift_db` 0
    the presence lift."""

    cutoff: float = 350.0
    order: int = 4
    lift_db: float = 0.0
    lift_hz: float = 2200.0
    lift_q: float = 1.2
    gain_db: float = 0.0
    ceiling_db: float = -1.0


DEFAULT = Settings()


def check_settings(settings, rate):
    """A plain-English reason the setting cannot be used at `rate`, or None."""
    if not isinstance(rate, int) or not MIN_RATE <= rate <= MAX_RATE:
        return f"sample rate {rate!r} is outside {MIN_RATE}-{MAX_RATE} Hz"
    nyquist_margin = 0.45 * rate
    if not isinstance(settings.order, int) or not 1 <= settings.order <= 8:
        return f"order {settings.order!r} is not 1 to 8"
    numbers = (settings.cutoff, settings.lift_db, settings.lift_hz, settings.lift_q,
               settings.gain_db, settings.ceiling_db)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in numbers):
        return "a setting is not a finite number"
    if settings.cutoff != 0 and not 20 <= settings.cutoff <= min(4000, nyquist_margin):
        return f"cutoff {settings.cutoff:g} Hz is outside 20-{min(4000, nyquist_margin):g} Hz (0 turns it off)"
    if not -12 <= settings.lift_db <= 12:
        return f"lift {settings.lift_db:g} dB is outside -12 to +12 dB"
    if settings.lift_db and not 200 <= settings.lift_hz <= nyquist_margin:
        return f"lift frequency {settings.lift_hz:g} Hz is outside 200-{nyquist_margin:g} Hz"
    if not 0.3 <= settings.lift_q <= 5:
        return f"lift Q {settings.lift_q:g} is outside 0.3-5"
    if not -24 <= settings.gain_db <= 12:
        return f"gain {settings.gain_db:g} dB is outside -24 to +12 dB"
    if not -12 <= settings.ceiling_db <= 0:
        return f"ceiling {settings.ceiling_db:g} dBFS is outside -12 to 0 dBFS"
    return None


# On or off ------------------------------------------------------------------------

def _switch(value):
    value = (value or "").strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return None


def _read(path, limit=4096):
    try:
        with open(path, "rb") as source:
            return source.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def decide(*, environ=None, cmdline=CMDLINE, product=DMI_PRODUCT):
    """(on, why): whether clause WAVs are filtered, and the reason in a few
    words for the voice's startup line. BEING_SPEAKER_EQ wins over the last
    being.speaker_eq= on the kernel command line, which wins over the laptop
    model; a value that is not 0/1 (or on/off) is ignored."""
    environ = os.environ if environ is None else environ
    forced = _switch(environ.get(ENV))
    if forced is not None:
        return forced, f"{ENV}={environ.get(ENV).strip()}"
    words = (_read(cmdline) or "").split()
    values = [word[len(CMDLINE_KEY):] for word in words if word.startswith(CMDLINE_KEY)]
    if values and _switch(values[-1]) is not None:
        return _switch(values[-1]), f"{CMDLINE_KEY}{values[-1]}"
    name = (_read(product, 256) or "").strip()
    if name in MEASURED_PRODUCTS:
        return True, f"measured speakers ({name})"
    return False, "not the measured laptop"


def enabled(*, environ=None, cmdline=CMDLINE, product=DMI_PRODUCT) -> bool:
    """True when the voice should filter its clause WAVs (see decide)."""
    return decide(environ=environ, cmdline=cmdline, product=product)[0]


# Filter design -----------------------------------------------------------------------
#
# Biquads are (b0, b1, b2, a1, a2), normalised so a0 = 1, from the bilinear
# transform with the frequency prewarped (R. Bristow-Johnson's cookbook forms).
# Every section of the high-pass is prewarped at the same cutoff, so the cascade
# is exactly the analog Butterworth mapped to the clip's rate: -3 dB at the cutoff.

def butterworth_qs(order):
    """The Q of each second-order section of an `order` Butterworth filter,
    and whether a first-order section is needed too."""
    pairs = order // 2
    if order % 2:
        return [1 / (2 * math.cos(k * math.pi / order)) for k in range(1, pairs + 1)], True
    return [1 / (2 * math.cos((2 * k - 1) * math.pi / (2 * order))) for k in range(1, pairs + 1)], False


def highpass_biquad(cutoff, q, rate):
    w0 = 2 * math.pi * cutoff / rate
    cos_w0, alpha = math.cos(w0), math.sin(w0) / (2 * q)
    a0 = 1 + alpha
    return ((1 + cos_w0) / 2 / a0, -(1 + cos_w0) / a0, (1 + cos_w0) / 2 / a0,
            -2 * cos_w0 / a0, (1 - alpha) / a0)


def highpass_first_order(cutoff, rate):
    k = math.tan(math.pi * cutoff / rate)
    return (1 / (1 + k), -1 / (1 + k), 0.0, (k - 1) / (k + 1), 0.0)


def peaking_biquad(freq, gain_db, q, rate):
    a = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * freq / rate
    cos_w0, alpha = math.cos(w0), math.sin(w0) / (2 * q)
    a0 = 1 + alpha / a
    return ((1 + alpha * a) / a0, -2 * cos_w0 / a0, (1 - alpha * a) / a0,
            -2 * cos_w0 / a0, (1 - alpha / a) / a0)


def sections(settings, rate):
    """The filter as a list of biquads, the makeup gain folded into the first."""
    result = []
    if settings.cutoff:
        qs, first_order = butterworth_qs(settings.order)
        if first_order:
            result.append(highpass_first_order(settings.cutoff, rate))
        result.extend(highpass_biquad(settings.cutoff, q, rate) for q in qs)
    if settings.lift_db:
        result.append(peaking_biquad(settings.lift_hz, settings.lift_db, settings.lift_q, rate))
    gain = 10 ** (settings.gain_db / 20)
    if gain != 1:
        if not result:
            result.append((1.0, 0.0, 0.0, 0.0, 0.0))
        b0, b1, b2, a1, a2 = result[0]
        result[0] = (b0 * gain, b1 * gain, b2 * gain, a1, a2)
    return result


def response_db(freq, rate, settings=DEFAULT):
    """The filter's gain in dB at `freq` (before the limiter, which only acts
    on peaks above the ceiling)."""
    z = cmath.exp(-2j * math.pi * freq / rate)
    total = 1.0
    for b0, b1, b2, a1, a2 in sections(settings, rate):
        total *= abs((b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z))
    return 20 * math.log10(max(total, 1e-12))


# Filtering ------------------------------------------------------------------------------

def _one(samples, section):
    """One biquad over a sequence of numbers (transposed direct form II)."""
    b0, b1, b2, a1, a2 = section
    out = [0.0] * len(samples)
    z1 = z2 = 0.0
    index = 0
    for x in samples:
        y = b0 * x + z1
        z1 = b1 * x - a1 * y + z2
        z2 = b2 * x - a2 * y
        out[index] = y
        index += 1
    return out


def _two(samples, first, second):
    """Two biquads in one pass: the same as _one twice, with one loop's
    overhead instead of two."""
    b0, b1, b2, a1, a2 = first
    c0, c1, c2, d1, d2 = second
    out = [0.0] * len(samples)
    z1 = z2 = w1 = w2 = 0.0
    index = 0
    for x in samples:
        y = b0 * x + z1
        z1 = b1 * x - a1 * y + z2
        z2 = b2 * x - a2 * y
        u = c0 * y + w1
        w1 = c1 * y - d1 * u + w2
        w2 = c2 * y - d2 * u
        out[index] = u
        index += 1
    return out


def filter_samples(samples, settings, rate):
    """The filtered samples as floats (no limiting, no rounding)."""
    todo = sections(settings, rate)
    signal = samples
    while todo:
        if len(todo) >= 2:
            signal = _two(signal, todo[0], todo[1])
            todo = todo[2:]
        else:
            signal = _one(signal, todo[0])
            todo = []
    return signal if signal is not samples else [float(x) for x in samples]


def limiter_gains(signal, ceiling, rate):
    """Gain at each block edge of `signal` (one more than the blocks) so that
    no sample, ramped linearly between edges, goes above `ceiling`; None when
    no block needs turning down."""
    block = max(1, round(LIMIT_BLOCK_S * rate))
    targets = []
    for start in range(0, len(signal), block):
        chunk = signal[start:start + block]
        peak = max(max(chunk), -min(chunk))
        targets.append(1.0 if peak <= ceiling else ceiling / peak)
    if min(targets, default=1.0) >= 1.0:
        return None
    # An edge serves the block before it and the block after it.
    edges = [min(targets[0], 1.0)]
    edges.extend(min(targets[k - 1], targets[k]) for k in range(1, len(targets)))
    edges.append(targets[-1])
    seconds = block / rate
    release = 10 ** (LIMIT_RELEASE_DB_PER_S * seconds / 20)
    attack = 10 ** (LIMIT_ATTACK_DB_PER_S * seconds / 20)
    # The gain rises back slowly after a peak and starts falling before one;
    # both passes only ever lower an edge, so no block gets more than it needs.
    for k in range(1, len(edges)):
        edges[k] = min(edges[k], edges[k - 1] * release)
    for k in range(len(edges) - 2, -1, -1):
        edges[k] = min(edges[k], edges[k + 1] * attack)
    return block, edges


def limit(signal, ceiling, rate):
    """Apply the look-ahead limiter in place; returns how many blocks it
    turned down."""
    plan = limiter_gains(signal, ceiling, rate)
    if plan is None:
        return 0
    block, edges = plan
    lowered = 0
    total = len(signal)
    for k in range(len(edges) - 1):
        g0, g1 = edges[k], edges[k + 1]
        if g0 >= 1.0 and g1 >= 1.0:
            continue
        lowered += 1
        start = k * block
        stop = min(start + block, total)
        step = (g1 - g0) / block
        for offset in range(stop - start):
            signal[start + offset] *= g0 + step * offset
    return lowered


def _to_int16(signal):
    """Round to 16-bit samples; the limiter keeps every value within range,
    and the clamp is only a guard."""
    if signal and (max(signal) > FULL_SCALE or min(signal) < -FULL_SCALE - 1):
        signal = [min(FULL_SCALE, max(-FULL_SCALE - 1, x)) for x in signal]
    return array("h", map(round, signal))


def process_samples(samples, rate, settings=DEFAULT, *, channels=1):
    """Filter interleaved 16-bit samples; returns (array('h'), stats)."""
    problem = check_settings(settings, rate)
    if problem:
        raise ValueError(problem)
    if channels not in (1, 2):
        raise ValueError(f"{channels} channels; only mono or stereo")
    ceiling = FULL_SCALE * 10 ** (settings.ceiling_db / 20)
    lowered = 0
    out = array("h", bytes(2 * len(samples)))
    for channel in range(channels):
        signal = filter_samples(samples[channel::channels], settings, rate)
        lowered += limit(signal, ceiling, rate)
        out[channel::channels] = _to_int16(signal)
    peak_in = max(max(samples), -min(samples)) if samples else 0
    peak_out = max(max(out), -min(out)) if out else 0
    return out, {"peak_in": peak_in, "peak_out": peak_out, "limited_blocks": lowered}


def _write_atomically(path_out, params, data, mode):
    """Write the WAV beside `path_out` and rename it into place, with `mode`
    (the permissions the file had, or the source's), never a half-written file."""
    path_out = Path(path_out)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path_out.parent, prefix=".speaker-eq-", suffix=".wav",
                                         delete=False) as target:
            pending = Path(target.name)
            os.fchmod(target.fileno(), mode)
        with wave.open(str(pending), "wb") as output:
            output.setnchannels(params.nchannels)
            output.setsampwidth(2)
            output.setframerate(params.framerate)
            output.writeframes(data)
        os.replace(pending, path_out)
        pending = None
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink()


# What reading, filtering or writing a clip can raise. The stdlib wave reader
# raises a bare RuntimeError (or struct.error) for a chunk before 'data' that
# runs past the end of the file; _open_wave turns those into wave.Error, and
# they are listed here as well in case another path lets one through.
FILE_ERRORS = (OSError, EOFError, wave.Error, ValueError, OverflowError, MemoryError,
               RuntimeError, struct.error)


def _open_wave(handle):
    try:
        return wave.open(handle)
    except (RuntimeError, struct.error) as error:
        raise wave.Error("damaged chunk headers (a chunk runs past the end of the file)") from error


def process_file(path_in, path_out, *, rate=None, settings=DEFAULT):
    """Filter a 16-bit PCM WAV into `path_out` (which may be `path_in`).
    Returns the stats; raises one of FILE_ERRORS, and leaves `path_out` as it
    was, on any problem."""
    with open(path_in, "rb") as handle, _open_wave(handle) as source:
        try:
            mode = os.stat(path_out).st_mode & 0o777
        except OSError:
            mode = os.fstat(handle.fileno()).st_mode & 0o777
        params = source.getparams()
        if params.sampwidth != 2:
            raise ValueError(f"{8 * params.sampwidth}-bit samples; only 16-bit")
        if params.nchannels not in (1, 2):
            raise ValueError(f"{params.nchannels} channels; only mono or stereo")
        if params.framerate > MAX_RATE or params.nframes > MAX_SECONDS * params.framerate:
            raise ValueError("the clip is too long to filter")
        raw = source.readframes(params.nframes)
    design_rate = params.framerate if rate is None else rate
    samples = array("h")
    samples.frombytes(raw[:len(raw) - len(raw) % (2 * params.nchannels)])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise ValueError("the clip has no samples")
    started = time.process_time()
    out, stats = process_samples(samples, design_rate, settings, channels=params.nchannels)
    stats["cpu_s"] = time.process_time() - started
    stats["seconds"] = len(samples) / params.nchannels / params.framerate
    stats["rate"] = params.framerate
    if sys.byteorder != "little":
        out.byteswap()
    _write_atomically(path_out, params, out.tobytes(), mode)
    return stats


def process_wav(path_in, path_out, *, rate=None, settings=DEFAULT) -> bool:
    """Filter a 16-bit mono (or stereo) PCM WAV for the measured speakers and
    write it to `path_out`, which may be `path_in` (replaced atomically).

    `rate` is the sample rate the filter is designed for; None (the normal
    case) uses the WAV's own rate. False, and `path_out` untouched, on any
    error: a missing or unreadable file, not 16-bit PCM, more than two
    channels, no samples, a rate outside 8-96 kHz, or a bad setting. The
    caller then plays the clip as it was.

    Cost, measured with the default filter on 12.2 s of 22.05 kHz Alba: 0.0047
    of real time on an Apple-silicon Mac. A plain two-biquad loop runs 15
    times slower on the MacBookPro9,2's i5 (0.037 of real time there against
    0.0024 on the Mac), so expect about 0.07 of real time on the laptop:
    about 0.2 s of CPU for a 3 s clause.

    It never raises (except KeyboardInterrupt and SystemExit): the voice calls
    it on every clause and must fall back to the unfiltered clip, so anything
    beyond the expected FILE_ERRORS is caught here too."""
    try:
        process_file(path_in, path_out, rate=rate, settings=settings)
    except Exception:  # the contract is False on any error, never an exception
        return False
    return True


# Listening on the laptop ---------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m mind.speaker_eq",
        description="Filter a speech WAV for the MacBookPro9,2 speakers, to compare by ear.",
        epilog="On Pip: PYTHONPATH=/usr/lib/being python3 -m mind.speaker_eq IN /run/OUT.wav")
    parser.add_argument("source", metavar="IN", help="16-bit PCM WAV, mono or stereo")
    parser.add_argument("target", metavar="OUT", help="where to write the filtered WAV (may be IN)")
    parser.add_argument("--cutoff", type=float, default=DEFAULT.cutoff,
                        help=f"high-pass cutoff in Hz, 0 for none (default {DEFAULT.cutoff:g})")
    parser.add_argument("--order", type=int, default=DEFAULT.order,
                        help=f"high-pass order 1-8, 4 is two biquads (default {DEFAULT.order})")
    parser.add_argument("--lift", type=float, default=DEFAULT.lift_db,
                        help="presence lift in dB, 0 for none (default 0)")
    parser.add_argument("--lift-hz", type=float, default=DEFAULT.lift_hz,
                        help=f"centre of the presence lift (default {DEFAULT.lift_hz:g})")
    parser.add_argument("--gain", type=float, default=DEFAULT.gain_db,
                        help="makeup gain in dB before the limiter (default 0)")
    parser.add_argument("--ceiling", type=float, default=DEFAULT.ceiling_db,
                        help=f"limiter ceiling in dBFS (default {DEFAULT.ceiling_db:g})")
    args = parser.parse_args(argv)
    settings = replace(DEFAULT, cutoff=args.cutoff, order=args.order, lift_db=args.lift,
                       lift_hz=args.lift_hz, gain_db=args.gain, ceiling_db=args.ceiling)
    try:
        stats = process_file(args.source, args.target, settings=settings)
    except FILE_ERRORS as error:
        reason = str(error) or type(error).__name__
        print(f"speaker_eq: could not filter {args.source}: {reason}", file=sys.stderr)
        return 1
    rate = stats["rate"]
    shape = ", ".join(f"{freq} Hz {response_db(freq, rate, settings):+.1f} dB"
                      for freq in (200, 300, 400, 1000, 3000) if freq < rate / 2)
    print(f"speaker_eq: wrote {args.target}: {stats['seconds']:.2f} s at {rate} Hz, "
          f"filter {shape}; peak {stats['peak_in']} -> {stats['peak_out']}, "
          f"{stats['limited_blocks']} limiter blocks; "
          f"{stats['cpu_s']:.3f} s CPU ({stats['cpu_s'] / stats['seconds']:.4f} of real time)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
