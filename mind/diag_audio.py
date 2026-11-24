"""Test signals and their measurement for pip-diag, in the standard library only.

Everything here is pure: lists and `array('h')` of signed 16-bit samples in,
numbers out. WAV files go through the `wave` module (no zlib anywhere). The
levels use the same dBFS scale as mind/recognition.py: full scale is 32768
and digital silence reads -120 dBFS.

- signals: tones, a logarithmic sweep, pink noise, a click train, silence,
  beeps, and the channel-identity file (one beep left, two beeps right);
- levels: peak and RMS per channel, clipping, speech above the floor;
- spectra: a Goertzel filter, total harmonic distortion at one frequency,
  octave-band response of a recorded sweep;
- timing: latency by cross-correlation, dropouts in a steady signal;
- words: a word error rate with the usual edit distance.
"""

import math
import random
import re
import sys
import wave
from array import array
from pathlib import Path

FULL_SCALE = 32768
TOP = 32767
SILENT_DBFS = -120.0
CLIP = 32767
OCTAVES = (63, 125, 250, 500, 1000, 2000, 4000, 8000)


# Levels ---------------------------------------------------------------------

def dbfs(value):
    """A linear magnitude in dB relative to full scale; -120 for nothing."""
    return round(20 * math.log10(value / FULL_SCALE), 2) if value > 0 else SILENT_DBFS


def from_dbfs(level):
    """The linear peak amplitude, as a fraction of full scale, of a dBFS level."""
    return 10 ** (level / 20)


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(math.fsum(value * value for value in samples) / len(samples))


def peak(samples):
    if not samples:
        return 0
    return max(max(samples), -min(samples))


def clipped(samples):
    """How many samples sit at either end of the 16-bit range."""
    return sum(1 for value in samples if value >= CLIP or value <= -FULL_SCALE)


def levels(samples):
    """{peak_dbfs, rms_dbfs, clipped} of one channel."""
    return {"peak_dbfs": dbfs(peak(samples)), "rms_dbfs": dbfs(rms(samples)), "clipped": clipped(samples)}


def channel_levels(channels):
    return [levels(channel) for channel in channels]


def frame_rms(samples, rate, seconds=0.03):
    """RMS of consecutive frames of `seconds` each (the last partial frame is dropped)."""
    size = max(1, int(rate * seconds))
    return [rms(samples[start:start + size]) for start in range(0, len(samples) - size + 1, size)]


def speech_level(samples, rate, floor_dbfs, *, margin_db=6.0, seconds=0.03):
    """The level of the frames that stand `margin_db` above a measured floor.

    A whole recording window includes the pauses, so its RMS understates
    speech. Returns {rms_dbfs, peak_dbfs, frames, share, snr_db}; the RMS is
    of the loud frames only, and None when no frame was loud enough.
    """
    frames = frame_rms(samples, rate, seconds)
    threshold = FULL_SCALE * from_dbfs(floor_dbfs + margin_db)
    loud = [value for value in frames if value > threshold]
    if not loud:
        return {"rms_dbfs": None, "peak_dbfs": dbfs(peak(samples)), "frames": 0, "share": 0.0, "snr_db": None}
    level = dbfs(math.sqrt(math.fsum(value * value for value in loud) / len(loud)))
    return {"rms_dbfs": level, "peak_dbfs": dbfs(peak(samples)), "frames": len(loud),
            "share": round(len(loud) / len(frames), 3), "snr_db": round(level - floor_dbfs, 2)}


# Signals --------------------------------------------------------------------

def to_pcm(values, scale=1.0):
    """Floats in -1..1 (times `scale`) as clipped signed 16-bit samples."""
    out = array("h")
    for value in values:
        sample = int(round(value * scale * TOP))
        out.append(TOP if sample > TOP else -FULL_SCALE if sample < -FULL_SCALE else sample)
    return out


def _fade(values, rate, seconds):
    """Raised-cosine fade in and out, so a tone starts and stops without a click."""
    count = min(len(values) // 2, int(rate * seconds))
    for index in range(count):
        gain = 0.5 - 0.5 * math.cos(math.pi * index / count)
        values[index] *= gain
        values[-1 - index] *= gain
    return values


def tone(frequency, seconds, rate, level_dbfs=-20.0, *, fade=0.01):
    """A sine at `level_dbfs` peak."""
    amplitude = from_dbfs(level_dbfs)
    count = int(rate * seconds)
    step = 2 * math.pi * frequency / rate
    return to_pcm(_fade([amplitude * math.sin(step * index) for index in range(count)], rate, fade))


def log_sweep(start_hz, end_hz, seconds, rate, level_dbfs=-20.0, *, fade=0.02):
    """An exponential sine sweep: equal time per octave."""
    amplitude = from_dbfs(level_dbfs)
    count = int(rate * seconds)
    ratio = math.log(end_hz / start_hz)
    k = seconds * start_hz * 2 * math.pi / ratio
    values = [amplitude * math.sin(k * (math.exp(ratio * (index / rate) / seconds) - 1)) for index in range(count)]
    return to_pcm(_fade(values, rate, fade))


def sweep_time(frequency, start_hz, end_hz, seconds):
    """When an exponential sweep passes `frequency`, in seconds from its start."""
    return seconds * math.log(frequency / start_hz) / math.log(end_hz / start_hz)


def pink_noise(seconds, rate, level_dbfs=-20.0, *, seed=1, fade=0.02):
    """Pink noise (Paul Kellet's filter on seeded white noise) at `level_dbfs` RMS."""
    generator = random.Random(seed)
    b0 = b1 = b2 = 0.0
    values = []
    for _ in range(int(rate * seconds)):
        white = generator.uniform(-1.0, 1.0)
        b0 = 0.99765 * b0 + white * 0.0990460
        b1 = 0.96300 * b1 + white * 0.2965164
        b2 = 0.57000 * b2 + white * 1.0526913
        values.append(b0 + b1 + b2 + white * 0.1848)
    level = math.sqrt(math.fsum(value * value for value in values) / len(values)) if values else 0.0
    scale = from_dbfs(level_dbfs) / level if level else 0.0
    # Pink noise has a crest factor near 4: keep the peaks inside full scale.
    return to_pcm(_fade([max(-1.0, min(1.0, value * scale)) for value in values], rate, fade))


def click_train(seconds, rate, *, interval=0.1, level_dbfs=-6.0, click=0.001):
    """Short half-sine clicks every `interval` seconds."""
    amplitude = from_dbfs(level_dbfs)
    count = int(rate * seconds)
    width = max(1, int(rate * click))
    every = max(width + 1, int(rate * interval))
    values = [0.0] * count
    for start in range(0, count - width, every):
        for index in range(width):
            values[start + index] = amplitude * math.sin(math.pi * index / width)
    return to_pcm(values)


def speech_like(seconds, rate, level_dbfs=-12.0, *, f0=120.0, syllables=4.0, fade=0.02):
    """A voiced, syllabic signal shaped like speech: what period-timing faults chop audibly.

    A harmonic series on a gliding fundamental (f0 +- 15%), weighted like
    three vowel formants (500, 1500, 2500 Hz), switched on and off at the
    syllable rate with short gaps between syllables. `level_dbfs` is the peak."""
    count = int(rate * seconds)
    nyquist = rate / 2
    harmonics = []
    for number in range(1, int(4000 / f0) + 1):
        weight = sum(math.exp(-((number * f0 - formant) / width) ** 2)
                     for formant, width in ((500, 250), (1500, 350), (2500, 450))) / number ** 0.5
        if number * f0 * 1.15 < nyquist and weight > 1e-3:
            harmonics.append((number, weight))
    phase, values = 0.0, []
    for index in range(count):
        t = index / rate
        frequency = f0 * (1 + 0.15 * math.sin(2 * math.pi * 0.7 * t))
        phase += 2 * math.pi * frequency / rate
        syllable = (t * syllables) % 1.0
        gate = math.sin(math.pi * syllable / 0.8) ** 2 if syllable < 0.8 else 0.0
        values.append(gate * sum(weight * math.sin(number * phase) for number, weight in harmonics))
    top = max((abs(value) for value in values), default=0.0)
    scale = from_dbfs(level_dbfs) / top if top else 0.0
    return to_pcm(_fade([value * scale for value in values], rate, fade))


def silence(seconds, rate):
    return array("h", bytes(2 * int(rate * seconds)))


def beeps(count, rate, *, frequency=880.0, beep=0.15, gap=0.15, level_dbfs=-12.0):
    out = array("h")
    for index in range(count):
        out.extend(tone(frequency, beep, rate, level_dbfs))
        if index < count - 1:
            out.extend(silence(gap, rate))
    return out


def channel_identity(rate, *, level_dbfs=-12.0):
    """(left, right): one beep on the left, a pause, then two beeps on the right."""
    one, two = beeps(1, rate, level_dbfs=level_dbfs), beeps(2, rate, level_dbfs=level_dbfs)
    pause = silence(0.6, rate)
    left = array("h", one) + pause + silence(len(two) / rate, rate)
    right = silence(len(one) / rate, rate) + pause + array("h", two)
    return left, right


def white_dither(seconds, rate, level_dbfs=-80.0, *, seed=7):
    """Very quiet noise: a room-like floor instead of digital silence."""
    generator = random.Random(seed)
    amplitude = from_dbfs(level_dbfs) * math.sqrt(3)
    return to_pcm([generator.uniform(-amplitude, amplitude) for _ in range(int(rate * seconds))])


# WAV files ------------------------------------------------------------------

def interleave(channels):
    if len(channels) == 1:
        return array("h", channels[0])
    length = min(len(channel) for channel in channels)
    out = array("h", bytes(2 * length * len(channels)))
    for number, channel in enumerate(channels):
        out[number::len(channels)] = array("h", channel[:length])
    return out


def deinterleave(samples, count):
    if count == 1:
        return [array("h", samples)]
    return [array("h", samples[number::count]) for number in range(count)]


def write_wav(path, channels, rate):
    """Write signed 16-bit little-endian PCM; `channels` is a list of sample sequences."""
    data = interleave([array("h", channel) for channel in channels])
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(len(channels))
        audio.setsampwidth(2)
        audio.setframerate(int(rate))
        audio.writeframes(data.tobytes())
    return Path(path)


def read_wav(path):
    """(rate, [channel samples]) of a 16-bit PCM WAV file."""
    with wave.open(str(path), "rb") as audio:
        rate, count, width = audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
        frames = audio.readframes(audio.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit samples, found {8 * width}-bit")
    samples = array("h")
    samples.frombytes(frames[:len(frames) - len(frames) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return rate, deinterleave(samples, count)


def wav_seconds(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def mono(channels):
    """The average of the channels, as the ALSA plug plugin mixes them."""
    if len(channels) == 1:
        return array("h", channels[0])
    length = min(len(channel) for channel in channels)
    return array("h", [int(sum(channel[index] for channel in channels) / len(channels)) for index in range(length)])


def resample(samples, from_rate, to_rate):
    """Linear interpolation to another rate (good enough for decoding and levels)."""
    if from_rate == to_rate or not samples:
        return array("h", samples)
    step = from_rate / to_rate
    out = array("h")
    position, last = 0.0, len(samples) - 1
    while position < last:
        low = int(position)
        fraction = position - low
        out.append(int(samples[low] * (1 - fraction) + samples[low + 1] * fraction))
        position += step
    return out


# Spectra --------------------------------------------------------------------

def goertzel(samples, rate, frequency, *, window=True):
    """The amplitude (peak, in sample units) of one frequency in `samples`.

    A Hann window keeps a strong fundamental from leaking into the bins of
    its harmonics; its coherent gain of one half is divided back out.
    """
    count = len(samples)
    if count < 2 or not 0 < frequency < rate / 2:
        return 0.0
    coefficient = 2 * math.cos(2 * math.pi * frequency / rate)
    first = second = 0.0
    scale = 2 * math.pi / (count - 1)
    for index, value in enumerate(samples):
        if window:
            value = value * (0.5 - 0.5 * math.cos(scale * index))
        first, second = value + coefficient * first - second, first
    power = first * first + second * second - coefficient * first * second
    gain = 0.5 if window else 1.0
    return math.sqrt(max(power, 0.0)) * 2 / (count * gain)


def thd(samples, rate, fundamental, *, harmonics=5):
    """Total harmonic distortion at `fundamental`: {ratio, percent, db, fundamental_dbfs}."""
    base = goertzel(samples, rate, fundamental)
    if base <= 0:
        return {"ratio": None, "percent": None, "db": None, "fundamental_dbfs": SILENT_DBFS}
    upper = [goertzel(samples, rate, fundamental * order) for order in range(2, harmonics + 1)
             if fundamental * order < rate / 2]
    ratio = math.sqrt(math.fsum(value * value for value in upper)) / base
    return {"ratio": round(ratio, 5), "percent": round(100 * ratio, 3),
            "db": round(20 * math.log10(ratio), 2) if ratio > 0 else SILENT_DBFS,
            "fundamental_dbfs": dbfs(base)}


def sweep_bands(recorded, rate, start_hz, end_hz, seconds, *, offset=0.0, bands=OCTAVES):
    """Octave-band levels (dBFS RMS) of a recorded exponential sweep.

    `offset` is where the sweep starts in the recording (its latency). Each
    band is the stretch of time the sweep spent between the band's edges.
    """
    result = {}
    for centre in bands:
        low, high = centre / math.sqrt(2), centre * math.sqrt(2)
        if low < start_hz or high > end_hz:
            continue
        begin = int((offset + sweep_time(low, start_hz, end_hz, seconds)) * rate)
        end = int((offset + sweep_time(high, start_hz, end_hz, seconds)) * rate)
        part = recorded[max(0, begin):max(0, end)]
        if part:
            result[str(centre)] = dbfs(rms(part))
    return result


# Timing ---------------------------------------------------------------------

def envelope(samples, rate, to_rate=1000):
    """Mean magnitude in blocks: a coarse envelope at `to_rate` points a second."""
    size = max(1, int(rate / to_rate))
    return [math.fsum(abs(value) for value in samples[start:start + size]) / size
            for start in range(0, len(samples) - size + 1, size)]


def _correlate(reference, recorded, lag, length):
    return math.fsum(reference[index] * recorded[index + lag] for index in range(length))


def latency(reference, recorded, rate, *, max_lag_s=1.0, env_rate=1000):
    """(seconds, score): where `reference` best lines up inside `recorded`.

    A coarse search over envelopes, then a fine search on the samples around
    it. The score is the normalised envelope correlation, 0 to 1; below about
    0.3 the stimulus was probably not heard at all.
    """
    ref_env, rec_env = envelope(reference, rate, env_rate), envelope(recorded, rate, env_rate)
    if not ref_env or len(rec_env) < len(ref_env):
        return None, 0.0
    ref_mean = math.fsum(ref_env) / len(ref_env)
    ref_env = [value - ref_mean for value in ref_env]
    ref_norm = math.sqrt(math.fsum(value * value for value in ref_env))
    best_lag, best_score = 0, -1.0
    max_lag = min(int(max_lag_s * env_rate), len(rec_env) - len(ref_env))
    for lag in range(max_lag + 1):
        window = rec_env[lag:lag + len(ref_env)]
        mean = math.fsum(window) / len(window)
        centred = [value - mean for value in window]
        norm = math.sqrt(math.fsum(value * value for value in centred))
        if not norm or not ref_norm:
            continue
        score = math.fsum(a * b for a, b in zip(ref_env, centred)) / (ref_norm * norm)
        if score > best_score:
            best_lag, best_score = lag, score
    if best_score <= 0:
        return None, 0.0
    block = max(1, int(rate / env_rate))
    coarse = best_lag * block
    length = min(len(reference), int(0.25 * rate), len(recorded) - coarse - 2 * block)
    fine = coarse
    if length > 16:
        candidates = range(max(0, coarse - 2 * block), min(len(recorded) - length, coarse + 2 * block) + 1)
        fine = max(candidates, key=lambda lag: abs(_correlate(reference, recorded, lag, length)), default=coarse)
    return round(fine / rate, 4), round(best_score, 3)


def dropouts(samples, rate, *, start=0.0, end=None, window=0.02, drop_db=20.0):
    """Windows inside a steady stretch whose level falls `drop_db` below its median.

    Returns {count, at_s: [...first ten...], median_dbfs}.
    """
    first = int(start * rate)
    last = len(samples) if end is None else min(len(samples), int(end * rate))
    frames = frame_rms(samples[first:last], rate, window)
    if not frames:
        return {"count": 0, "at_s": [], "median_dbfs": SILENT_DBFS}
    median = sorted(frames)[len(frames) // 2]
    threshold = median * from_dbfs(-drop_db)
    found = [round(start + index * window, 3) for index, value in enumerate(frames) if value < threshold]
    return {"count": len(found), "at_s": found[:10], "median_dbfs": dbfs(median)}


# Words ----------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9']+")
_NUMBERS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
            "7": "seven", "8": "eight", "9": "nine", "10": "ten"}


def words(text):
    """Lowercase words without punctuation; single digits spelled out."""
    found = _WORD.findall((text or "").casefold().replace("’", "'"))
    return [_NUMBERS.get(word, word) for word in found]


def edit_distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, 1):
        current = [row]
        for column, heard in enumerate(hypothesis, 1):
            current.append(min(previous[column] + 1, current[column - 1] + 1,
                               previous[column - 1] + (expected != heard)))
        previous = current
    return previous[-1]


def wer(reference, hypothesis):
    """{wer, errors, words}: word errors over the reference's word count."""
    expected, heard = words(reference), words(hypothesis)
    errors = edit_distance(expected, heard)
    return {"wer": round(errors / len(expected), 3) if expected else (0.0 if not heard else 1.0),
            "errors": errors, "words": len(expected)}
