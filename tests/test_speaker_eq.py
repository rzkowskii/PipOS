"""The speaker filter: its response, the limiter, speed, files and the switch."""

import io
import math
import random
import struct
import tempfile
import time
import unittest
import wave
from array import array
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from mind import speaker_eq
from mind.speaker_eq import DEFAULT, Settings, process_samples, process_wav, response_db

CEILING = round(32767 * 10 ** (-1 / 20))


def tone(freq, rate, seconds, amplitude):
    count = round(rate * seconds)
    return array("h", (round(amplitude * math.sin(2 * math.pi * freq * n / rate)) for n in range(count)))


def rms(samples):
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def gain_db(before, after):
    """The level change of the second half (past the filter's start)."""
    half = len(before) // 2
    return 20 * math.log10(rms(after[half:]) / rms(before[half:]))


def write_wav(path, samples, rate=22050, channels=1, width=2):
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(samples if isinstance(samples, bytes) else samples.tobytes())


def damaged_wavs(folder):
    """WAVs the stdlib reader rejects with a bare RuntimeError, not
    wave.Error: a chunk before 'data' that runs past the end of the file."""
    data = array("h", [1000, -1000] * 200).tobytes()
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 22050, 44100, 2, 16)
    info = b"INFO" + b"ISFT" + struct.pack("<I", 14) + b"Lavf58.76.100\x00"
    listing = b"LIST" + struct.pack("<I", len(info)) + info
    tail = b"data" + struct.pack("<I", len(data)) + data
    # A streamed writer's placeholder RIFF size (36) with a LIST chunk before data.
    short_riff = folder / "short-riff.wav"
    short_riff.write_bytes(b"RIFF" + struct.pack("<I", 36) + b"WAVE" + fmt + listing + tail)
    # A fmt chunk that claims 60 bytes, so the reader skips into the samples.
    long_fmt = bytearray(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + fmt + tail)
    long_fmt[16:20] = struct.pack("<I", 0x3C)
    big_fmt = folder / "long-fmt.wav"
    big_fmt.write_bytes(bytes(long_fmt))
    return {"short RIFF size with a LIST chunk": short_riff, "fmt chunk past the end": big_fmt}


def read_wav(path):
    with wave.open(str(path)) as source:
        return source.getparams(), array("h", source.readframes(source.getnframes()))


def speech_like(rate, seconds, seed=3):
    """Alba-like test sound: a 200 Hz voice with harmonics, mostly below
    300 Hz, plus some noise, peaking near full scale."""
    rng = random.Random(seed)
    count = round(rate * seconds)
    raw = []
    for n in range(count):
        t = n / rate
        envelope = 0.6 + 0.4 * math.sin(2 * math.pi * 3 * t)
        value = sum(math.sin(2 * math.pi * 200 * k * t + k) / k for k in range(1, 12))
        raw.append(envelope * value + rng.gauss(0, 0.05))
    peak = max(abs(x) for x in raw)
    return array("h", (round(29205 * x / peak) for x in raw))


class Response(unittest.TestCase):
    def test_default_gain_at_reference_frequencies(self):
        for rate in (16000, 22050):
            with self.subTest(rate=rate):
                self.assertLess(response_db(100, rate), -40)
                self.assertAlmostEqual(response_db(200, rate), -19.5, delta=0.3)
                self.assertAlmostEqual(response_db(300, rate), -6.5, delta=0.3)
                self.assertAlmostEqual(response_db(400, rate), -1.3, delta=0.2)
                self.assertAlmostEqual(response_db(1000, rate), 0.0, delta=0.05)
                self.assertAlmostEqual(response_db(3000, rate), 0.0, delta=0.05)

    def test_butterworth_is_3_db_down_at_the_cutoff_for_every_order(self):
        for order in range(1, 9):
            for cutoff in (300, 450):
                with self.subTest(order=order, cutoff=cutoff):
                    settings = Settings(cutoff=cutoff, order=order)
                    self.assertAlmostEqual(response_db(cutoff, 22050, settings), -3.01, delta=0.02)
                    # The slope below it is 6 dB an octave per order.
                    self.assertAlmostEqual(response_db(cutoff / 4, 22050, settings), -12.04 * order, delta=0.6)

    def test_fourth_order_uses_the_textbook_qs(self):
        qs, first_order = speaker_eq.butterworth_qs(4)
        self.assertFalse(first_order)
        self.assertEqual([round(q, 4) for q in qs], [0.5412, 1.3066])
        self.assertEqual(len(speaker_eq.sections(DEFAULT, 22050)), 2)

    def test_filtered_tones_match_the_design(self):
        for rate in (16000, 22050):
            for freq in (100, 300, 1000, 3000):
                with self.subTest(rate=rate, freq=freq):
                    before = tone(freq, rate, 0.5, 8000)
                    after, stats = process_samples(before, rate)
                    self.assertEqual(stats["limited_blocks"], 0)
                    self.assertAlmostEqual(gain_db(before, after), response_db(freq, rate), delta=0.3)

    def test_presence_lift(self):
        settings = Settings(lift_db=3)
        self.assertAlmostEqual(response_db(2200, 22050, settings), 3.0, delta=0.05)
        self.assertLess(response_db(8000, 22050, settings), 0.3)
        self.assertLess(response_db(100, 22050, settings), -40)
        before = tone(2200, 22050, 0.5, 6000)
        after, _ = process_samples(before, 22050, settings)
        self.assertAlmostEqual(gain_db(before, after), 3.0, delta=0.2)

    def test_nothing_asked_for_leaves_a_quiet_clip_as_it_was(self):
        settings = Settings(cutoff=0)
        before = tone(150, 22050, 0.2, 12000)
        after, stats = process_samples(before, 22050, settings)
        self.assertEqual(after, before)
        self.assertEqual(stats["limited_blocks"], 0)

    def test_makeup_gain(self):
        before = tone(1000, 22050, 0.3, 4000)
        after, _ = process_samples(before, 22050, Settings(gain_db=6))
        self.assertAlmostEqual(gain_db(before, after), 6.0, delta=0.1)


class Limiter(unittest.TestCase):
    def test_peaks_the_high_pass_raises_never_clip(self):
        # A high-passed square wave overshoots at every edge, far above the input.
        rate = 22050
        before = array("h", (29205 if (n // 100) % 2 else -29205 for n in range(rate)))
        unlimited = speaker_eq.filter_samples(before, Settings(cutoff=300), rate)
        self.assertGreater(max(abs(x) for x in unlimited), 32767)
        after, stats = process_samples(before, rate, Settings(cutoff=300))
        self.assertLessEqual(stats["peak_out"], CEILING)
        self.assertGreaterEqual(min(after), -CEILING)
        self.assertGreater(stats["limited_blocks"], 0)

    def test_speech_like_clip_stays_under_the_ceiling(self):
        before = speech_like(22050, 1.5)
        _, stats = process_samples(before, 22050, Settings(cutoff=300, lift_db=6))
        self.assertLessEqual(stats["peak_out"], CEILING)
        # Most of this voice is its 200 Hz fundamental, as Alba's is below
        # 300 Hz: the filter takes that out and keeps the harmonics.
        after, _ = process_samples(before, 22050, Settings(cutoff=300))
        self.assertLess(rms(after), 0.7 * rms(before))
        self.assertGreater(rms(after), 0.5 * rms(before))

    def test_full_scale_and_extra_gain_stay_under_the_ceiling(self):
        rate = 16000
        square = array("h", (32767 if (n // 40) % 2 else -32768 for n in range(rate)))
        for settings in (DEFAULT, Settings(gain_db=12, lift_db=6), Settings(cutoff=0, gain_db=12)):
            with self.subTest(settings=settings):
                after, stats = process_samples(square, rate, settings)
                self.assertLessEqual(max(after), CEILING)
                self.assertGreaterEqual(min(after), -CEILING)
                self.assertLessEqual(stats["peak_out"], CEILING)

    def test_the_gain_moves_smoothly(self):
        # A loud burst in a quiet tone: the gain ramps, it does not jump.
        rate = 22050
        quiet = tone(1000, rate, 0.4, 3000)
        loud = tone(1000, rate, 0.05, 32000)
        before = quiet[:4000] + loud + quiet[4000:]
        signal = speaker_eq.filter_samples(before, Settings(cutoff=0, gain_db=6), rate)
        ceiling = 32767 * 10 ** (-1 / 20)
        block, edges = speaker_eq.limiter_gains(signal, ceiling, rate)
        steps = [abs(b - a) for a, b in zip(edges, edges[1:])]
        self.assertLess(max(steps), 0.5)
        self.assertEqual(edges[0], 1.0)
        self.assertEqual(edges[-1], 1.0)
        speaker_eq.limit(signal, ceiling, rate)
        self.assertLessEqual(max(abs(x) for x in signal), ceiling + 1e-6)

    def test_silence_stays_silent(self):
        after, stats = process_samples(array("h", bytes(2 * 4000)), 16000)
        self.assertEqual(set(after), {0})
        self.assertEqual(stats["peak_out"], 0)


class Speed(unittest.TestCase):
    def test_ten_seconds_well_within_real_time(self):
        rng = random.Random(5)
        rate = 22050
        samples = array("h", (rng.randint(-20000, 20000) for _ in range(10 * rate)))
        started = time.process_time()
        process_samples(samples, rate, Settings(lift_db=3))
        elapsed = time.process_time() - started
        # About 0.06 s on a current Mac; the laptop's i5 is about 15 times slower.
        self.assertLess(elapsed, 1.0, f"{elapsed:.3f} s CPU for 10 s of audio")


class Files(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def leftovers(self):
        return sorted(p.name for p in self.folder.iterdir() if p.name.startswith(".speaker-eq-"))

    def test_mono_round_trip_keeps_the_header(self):
        source, target = self.folder / "in.wav", self.folder / "out.wav"
        write_wav(source, tone(1000, 22050, 0.3, 9000))
        source.chmod(0o644)
        self.assertTrue(process_wav(source, target))
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        params, samples = read_wav(target)
        self.assertEqual((params.nchannels, params.sampwidth, params.framerate, params.nframes),
                         (1, 2, 22050, round(22050 * 0.3)))
        self.assertAlmostEqual(gain_db(read_wav(source)[1], samples), 0.0, delta=0.1)
        self.assertEqual(self.leftovers(), [])

    def test_in_place(self):
        path = self.folder / "clause.wav"
        before = tone(150, 16000, 0.4, 9000)
        write_wav(path, before, rate=16000)
        path.chmod(0o640)
        self.assertTrue(process_wav(path, path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        _, after = read_wav(path)
        self.assertAlmostEqual(gain_db(before, after), response_db(150, 16000), delta=0.5)

    def test_stereo_channels_are_filtered_apart(self):
        source, target = self.folder / "in.wav", self.folder / "out.wav"
        left = tone(1000, 22050, 0.3, 9000)
        frames = array("h")
        for value in left:
            frames.extend((value, 0))
        write_wav(source, frames, channels=2)
        self.assertTrue(process_wav(source, target))
        params, samples = read_wav(target)
        self.assertEqual(params.nchannels, 2)
        self.assertEqual(set(samples[1::2]), {0})
        self.assertAlmostEqual(gain_db(left, samples[0::2]), 0.0, delta=0.1)

    def test_design_rate_override(self):
        source, target = self.folder / "in.wav", self.folder / "out.wav"
        before = tone(300, 22050, 0.4, 9000)
        write_wav(source, before)
        self.assertTrue(process_wav(source, target, rate=44100))
        _, after = read_wav(target)
        # Designed for 44.1 kHz, a 300 Hz tone in a 22.05 kHz file is treated as 600 Hz.
        self.assertAlmostEqual(gain_db(before, after), response_db(600, 44100), delta=0.3)
        self.assertLess(response_db(300, 22050), -6)
        self.assertEqual(read_wav(target)[0].framerate, 22050)

    def test_errors_return_false_and_write_nothing(self):
        good = self.folder / "good.wav"
        write_wav(good, tone(1000, 22050, 0.1, 9000))
        garbage = self.folder / "garbage.wav"
        garbage.write_bytes(b"not a wave file at all" * 10)
        eight_bit = self.folder / "eight.wav"
        write_wav(eight_bit, bytes(200), width=1)
        three = self.folder / "three.wav"
        write_wav(three, bytes(600), channels=3)
        empty = self.folder / "empty.wav"
        write_wav(empty, b"")
        slow = self.folder / "slow.wav"
        write_wav(slow, tone(500, 4000, 0.1, 9000), rate=4000)
        cases = {
            "missing": (self.folder / "missing.wav", {}),
            "garbage": (garbage, {}),
            "8-bit": (eight_bit, {}),
            "3 channels": (three, {}),
            "empty": (empty, {}),
            "rate too low": (slow, {}),
            "design rate too high": (good, {"rate": 200000}),
            "cutoff above the band": (good, {"settings": Settings(cutoff=9000)}),
            "order 9": (good, {"settings": Settings(order=9)}),
            "lift too strong": (good, {"settings": Settings(lift_db=20)}),
            "not a number": (good, {"settings": Settings(cutoff=float("nan"))}),
        }
        cases.update({name: (path, {}) for name, path in damaged_wavs(self.folder).items()})
        for name, (source, options) in cases.items():
            with self.subTest(name):
                target = self.folder / f"out-{name}.wav"
                self.assertFalse(process_wav(source, target, **options))
                self.assertFalse(target.exists())
        with self.subTest("missing folder"):
            self.assertFalse(process_wav(good, self.folder / "nowhere" / "out.wav"))
        self.assertEqual(self.leftovers(), [])

    def test_damaged_headers_never_raise(self):
        source = self.folder / "source.wav"
        write_wav(source, speech_like(22050, 0.05))
        original = source.read_bytes()
        rng = random.Random(7)
        mutated = self.folder / "mutated.wav"
        target = self.folder / "fuzz-out.wav"
        for _ in range(400):
            header = bytearray(original[:64])
            for _ in range(rng.randint(1, 4)):
                header[rng.randrange(len(header))] = rng.randrange(256)
            mutated.write_bytes(bytes(header) + original[64:])
            result = process_wav(mutated, target)  # must return, never raise
            self.assertIsInstance(result, bool)
            if not result:
                self.assertFalse(target.exists())
            target.unlink(missing_ok=True)
        self.assertEqual(self.leftovers(), [])

    def test_an_unexpected_error_is_false_too(self):
        good = self.folder / "good.wav"
        write_wav(good, tone(1000, 22050, 0.1, 9000))
        original = speaker_eq.process_file

        def broken(*args, **kwargs):
            raise KeyError("something nobody expected")

        speaker_eq.process_file = broken
        try:
            self.assertFalse(process_wav(good, self.folder / "out.wav"))
        finally:
            speaker_eq.process_file = original
        self.assertFalse((self.folder / "out.wav").exists())

    def test_an_error_leaves_an_existing_output_alone(self):
        target = self.folder / "out.wav"
        target.write_bytes(b"previous")
        self.assertFalse(process_wav(self.folder / "missing.wav", target))
        self.assertEqual(target.read_bytes(), b"previous")

    def test_a_bad_setting_raises_in_memory(self):
        with self.assertRaises(ValueError):
            process_samples(array("h", [0, 1]), 22050, Settings(cutoff=-5))
        with self.assertRaises(ValueError):
            process_samples(array("h", [0, 1, 2]), 22050, channels=3)


class Switch(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def decide(self, product=None, cmdline="", environ=None):
        product_file = self.folder / "product_name"
        if product is None:
            product_file.unlink(missing_ok=True)
        else:
            product_file.write_text(product + "\n")
        cmdline_file = self.folder / "cmdline"
        cmdline_file.write_text(cmdline + "\n")
        return speaker_eq.decide(environ=environ or {}, cmdline=cmdline_file, product=product_file)

    def test_on_only_on_the_measured_laptop(self):
        self.assertEqual(self.decide("MacBookPro9,2"), (True, "measured speakers (MacBookPro9,2)"))
        self.assertFalse(self.decide("MacBookPro10,1")[0])
        self.assertFalse(self.decide("Standard PC (Q35 + ICH9, 2009)")[0])
        self.assertFalse(self.decide(None)[0])

    def test_kernel_argument_overrides_the_model_and_the_last_one_counts(self):
        self.assertEqual(self.decide("MacBookPro9,2", "quiet being.speaker_eq=0"),
                         (False, "being.speaker_eq=0"))
        self.assertTrue(self.decide("Other", "being.speaker_eq=1")[0])
        self.assertTrue(self.decide("Other", "being.speaker_eq=0 being.speaker_eq=1")[0])
        # A value that is not a switch is ignored.
        self.assertTrue(self.decide("MacBookPro9,2", "being.speaker_eq=maybe")[0])

    def test_environment_wins(self):
        self.assertEqual(self.decide("MacBookPro9,2", "being.speaker_eq=1", {"BEING_SPEAKER_EQ": "0"}),
                         (False, "BEING_SPEAKER_EQ=0"))
        self.assertTrue(self.decide("Other", "being.speaker_eq=0", {"BEING_SPEAKER_EQ": "1"})[0])
        self.assertFalse(self.decide("MacBookPro9,2", "", {"BEING_SPEAKER_EQ": "off"})[0])
        self.assertTrue(self.decide("MacBookPro9,2", "", {"BEING_SPEAKER_EQ": ""})[0])

    def test_enabled_matches_decide(self):
        product = self.folder / "product_name"
        product.write_text("MacBookPro9,2\n")
        self.assertTrue(speaker_eq.enabled(environ={}, cmdline=self.folder / "none", product=product))
        self.assertFalse(speaker_eq.enabled(environ={}, cmdline=self.folder / "none",
                                            product=self.folder / "none"))


class CommandLine(unittest.TestCase):
    def test_writes_and_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "in.wav", Path(folder) / "out.wav"
            write_wav(source, speech_like(22050, 0.5))
            out = io.StringIO()
            with redirect_stdout(out):
                code = speaker_eq.main([str(source), str(target), "--cutoff", "400", "--order", "2",
                                        "--lift", "3"])
            self.assertEqual(code, 0)
            self.assertTrue(target.exists())
            settings = Settings(cutoff=400, order=2, lift_db=3)
            self.assertIn(f"400 Hz {response_db(400, 22050, settings):+.1f} dB", out.getvalue())
            self.assertIn("1000 Hz +0.4 dB", out.getvalue())
            self.assertIn("of real time", out.getvalue())

    def test_failure_exits_1_with_a_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            err = io.StringIO()
            with redirect_stderr(err):
                code = speaker_eq.main([str(Path(folder) / "missing.wav"), str(Path(folder) / "out.wav")])
            self.assertEqual(code, 1)
            self.assertIn("could not filter", err.getvalue())
            self.assertFalse((Path(folder) / "out.wav").exists())

    def test_damaged_headers_exit_1_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, source in damaged_wavs(Path(folder)).items():
                with self.subTest(name):
                    err = io.StringIO()
                    with redirect_stderr(err):
                        code = speaker_eq.main([str(source), str(Path(folder) / "out.wav")])
                    self.assertEqual(code, 1)
                    self.assertIn("could not filter", err.getvalue())
                    self.assertIn("damaged chunk headers", err.getvalue())
                    self.assertFalse((Path(folder) / "out.wav").exists())

    def test_help_names_the_path_on_pip(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            speaker_eq.main(["--help"])
        self.assertIn("PYTHONPATH=/usr/lib/being", out.getvalue())


if __name__ == "__main__":
    unittest.main()
