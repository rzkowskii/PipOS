"""pip-diag's signal maths on synthetic signals: levels, Goertzel, THD, latency, dropouts, WER."""

import math
import random
import tempfile
import unittest
from array import array
from pathlib import Path

from mind import diag_audio as audio

RATE = 16000


def sine(frequency, seconds, rate=RATE, amplitude=0.5, harmonic=0.0):
    return audio.to_pcm([amplitude * (math.sin(2 * math.pi * frequency * n / rate)
                                      + harmonic * math.sin(2 * math.pi * 2 * frequency * n / rate))
                         for n in range(int(rate * seconds))])


def bursts(samples, rate, threshold_dbfs=-40):
    """How many separate loud stretches a signal has."""
    frames = audio.frame_rms(samples, rate, 0.01)
    loud = [value > audio.FULL_SCALE * audio.from_dbfs(threshold_dbfs) for value in frames]
    return sum(1 for index, on in enumerate(loud) if on and (index == 0 or not loud[index - 1]))


class Levels(unittest.TestCase):
    def test_a_sine_at_minus_20_peaks_there_and_has_rms_3_db_lower(self):
        tone = audio.tone(1000, 1.0, RATE, -20.0)
        result = audio.levels(tone)
        self.assertAlmostEqual(result["peak_dbfs"], -20.0, delta=0.1)
        self.assertAlmostEqual(result["rms_dbfs"], -23.0, delta=0.2)
        self.assertEqual(result["clipped"], 0)

    def test_silence_is_minus_120_and_clipping_is_counted(self):
        self.assertEqual(audio.levels(audio.silence(0.1, RATE))["rms_dbfs"], audio.SILENT_DBFS)
        self.assertEqual(audio.clipped(array("h", [32767, -32768, 100, 32767])), 3)

    def test_speech_level_counts_only_frames_above_the_floor(self):
        floor = audio.white_dither(2.0, RATE, -70.0)
        speech = array("h", floor) + audio.tone(300, 0.5, RATE, -26.0) + array("h", floor)
        floor_db = audio.dbfs(audio.rms(floor))
        result = audio.speech_level(speech, RATE, floor_db)
        self.assertAlmostEqual(result["rms_dbfs"], -29.0, delta=1.0)
        self.assertGreater(result["snr_db"], 35)
        self.assertLess(result["share"], 0.2)
        self.assertIsNone(audio.speech_level(floor, RATE, floor_db)["rms_dbfs"])


class Signals(unittest.TestCase):
    def test_pink_noise_hits_its_rms_and_is_repeatable(self):
        noise = audio.pink_noise(2.0, 48000, -20.0, seed=3)
        self.assertAlmostEqual(audio.dbfs(audio.rms(noise)), -20.0, delta=0.5)
        self.assertEqual(noise, audio.pink_noise(2.0, 48000, -20.0, seed=3))
        # Pink: much more energy at 100 Hz than at 5 kHz.
        self.assertGreater(audio.goertzel(noise, 48000, 100), audio.goertzel(noise, 48000, 5000))

    def test_the_channel_file_beeps_once_left_then_twice_right(self):
        left, right = audio.channel_identity(48000)
        self.assertEqual(len(left), len(right))
        self.assertEqual(bursts(left, 48000), 1)
        self.assertEqual(bursts(right, 48000), 2)
        loud_left = next(i for i, v in enumerate(audio.frame_rms(left, 48000, 0.01)) if v > 1000)
        loud_right = next(i for i, v in enumerate(audio.frame_rms(right, 48000, 0.01)) if v > 1000)
        self.assertLess(loud_left, loud_right)

    def test_click_train_has_a_click_every_100_ms(self):
        clicks = audio.click_train(1.0, 48000, interval=0.1)
        self.assertEqual(bursts(clicks, 48000, -50), 10)

    def test_the_sweep_spends_equal_time_per_octave(self):
        self.assertAlmostEqual(audio.sweep_time(100, 50, 6400, 7.0), 1.0, places=6)
        self.assertAlmostEqual(audio.sweep_time(200, 50, 6400, 7.0), 2.0, places=6)

    def test_flat_sweep_bands_are_flat(self):
        sweep = audio.log_sweep(50, 7900, 3.0, 16000, -12.0)
        bands = audio.sweep_bands(sweep, 16000, 50, 7900, 3.0)
        self.assertIn("1000", bands)
        values = list(bands.values())
        self.assertLess(max(values) - min(values), 1.5)

    def test_wav_round_trip_keeps_channels_and_rate(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        left, right = audio.tone(440, 0.2, 22050), audio.tone(880, 0.2, 22050)
        path = audio.write_wav(folder / "x.wav", [left, right], 22050)
        rate, channels = audio.read_wav(path)
        self.assertEqual(rate, 22050)
        self.assertEqual(channels[0], left)
        self.assertEqual(channels[1], right)
        self.assertAlmostEqual(audio.wav_seconds(path), 0.2, places=3)

    def test_resample_and_mono(self):
        tone = audio.tone(440, 1.0, 48000)
        self.assertAlmostEqual(len(audio.resample(tone, 48000, 16000)), 16000, delta=2)
        self.assertEqual(list(audio.mono([array("h", [100, 0]), array("h", [0, 200])])), [50, 100])


class Spectra(unittest.TestCase):
    def test_goertzel_measures_the_amplitude_of_one_frequency(self):
        tone = sine(1000, 0.5, amplitude=0.5)
        self.assertAlmostEqual(audio.goertzel(tone, RATE, 1000) / 32767, 0.5, delta=0.01)
        self.assertLess(audio.goertzel(tone, RATE, 3000) / 32767, 0.001)
        self.assertEqual(audio.goertzel(tone, RATE, 9000), 0.0)  # above Nyquist

    def test_thd_of_a_pure_tone_is_tiny_and_of_a_distorted_one_is_right(self):
        self.assertLess(audio.thd(sine(1000, 0.5), RATE, 1000)["percent"], 0.1)
        distorted = audio.thd(sine(1000, 0.5, amplitude=0.4, harmonic=0.1), RATE, 1000)
        self.assertAlmostEqual(distorted["percent"], 10.0, delta=0.3)
        self.assertAlmostEqual(distorted["db"], -20.0, delta=0.3)
        self.assertIsNone(audio.thd(audio.silence(0.1, RATE), RATE, 1000)["ratio"])


class Timing(unittest.TestCase):
    def test_latency_finds_a_delayed_noise_burst(self):
        generator = random.Random(5)
        burst = audio.to_pcm([generator.uniform(-0.5, 0.5) for _ in range(int(0.4 * RATE))])
        stimulus = array("h", burst) + audio.silence(0.2, RATE) + burst
        delay = int(0.123 * RATE)
        floor = audio.white_dither(2.0, RATE, -60.0)
        recorded = array("h", floor)
        for index, value in enumerate(stimulus):
            recorded[delay + index] = max(-32768, min(32767, recorded[delay + index] + value // 3))
        lag, score = audio.latency(stimulus, recorded, RATE, max_lag_s=0.5)
        self.assertAlmostEqual(lag, 0.123, delta=0.001)
        self.assertGreater(score, 0.8)

    def test_latency_of_nothing_heard_scores_low(self):
        stimulus = audio.tone(1000, 0.5, RATE)
        lag, score = audio.latency(stimulus, audio.white_dither(1.5, RATE, -70), RATE)
        self.assertLess(score, 0.5)

    def test_dropouts_are_found_where_they_are(self):
        tone = audio.tone(1000, 2.0, RATE, -12.0)
        self.assertEqual(audio.dropouts(tone, RATE, start=0.1, end=1.9)["count"], 0)
        gap = array("h", tone)
        start = int(1.0 * RATE)
        gap[start:start + int(0.05 * RATE)] = array("h", bytes(2 * int(0.05 * RATE)))
        found = audio.dropouts(gap, RATE, start=0.1, end=1.9)
        self.assertGreaterEqual(found["count"], 2)
        self.assertTrue(all(0.98 <= at <= 1.06 for at in found["at_s"]))


class Words(unittest.TestCase):
    def test_word_error_rate(self):
        self.assertEqual(audio.wer("Here's my terminal.", "here's my terminal")["wer"], 0.0)
        self.assertEqual(audio.wer("open a terminal", "open the terminal")["errors"], 1)
        self.assertAlmostEqual(audio.wer("open a terminal", "a good ten in l")["wer"], 5 / 3, places=3)
        self.assertEqual(audio.wer("set a timer for 1 minute", "set a timer for one minute")["wer"], 0.0)
        self.assertEqual(audio.wer("", "")["wer"], 0.0)
        self.assertEqual(audio.wer("", "noise")["wer"], 1.0)

    def test_edit_distance(self):
        self.assertEqual(audio.edit_distance(list("kitten"), list("sitting")), 3)


def floor_noise(seconds, rate, level_dbfs, seed):
    generator = random.Random(seed)
    spread = audio.from_dbfs(level_dbfs) * math.sqrt(3)
    return [generator.uniform(-spread, spread) * audio.TOP for _ in range(int(seconds * rate))]


def recorded_tone(rate=48000, *, start=0.34, seconds=3.0, total=4.0, tone_dbfs=-38.0, floor_dbfs=-63.0,
                  drift_ppm=0.0, click_at=None):
    """A floor with a faded 1 kHz tone from `start`, its clock off by `drift_ppm`."""
    values = floor_noise(total, rate, floor_dbfs, 4)
    tone = audio.tone(1000 * (1 + drift_ppm * 1e-6), seconds, rate, tone_dbfs)
    first = int(start * rate)
    for index, value in enumerate(tone[:len(values) - first]):
        values[first + index] += value
    if click_at is not None:
        values[int(click_at * rate)] += 30000
    return array("h", [max(-32768, min(32767, int(value))) for value in values])


class Onsets(unittest.TestCase):
    def test_the_onset_is_where_the_tone_starts(self):
        found = audio.tone_onset(recorded_tone(), 48000, 1000)
        # The 10 ms fade puts the half-level point 5 ms in.
        self.assertAlmostEqual(found - audio.TONE_FADE / 2, 0.34, delta=0.002)

    def test_a_click_before_the_tone_is_not_its_onset(self):
        found = audio.tone_onset(recorded_tone(click_at=0.29), 48000, 1000)
        self.assertAlmostEqual(found, 0.345, delta=0.003)

    def test_no_tone_has_no_onset(self):
        quiet = array("h", [int(value) for value in floor_noise(2.0, 48000, -63.0, 2)])
        self.assertIsNone(audio.tone_onset(quiet, 48000, 1000))

    def test_a_tone_at_16_khz_too(self):
        found = audio.tone_onset(recorded_tone(16000, start=0.5), 16000, 1000)
        self.assertAlmostEqual(found, 0.505, delta=0.003)


class Drift(unittest.TestCase):
    def test_a_clock_180_ppm_fast_is_measured(self):
        # 44.1 kHz playback against 48 kHz capture on the laptop: about +180 ppm.
        recording = recorded_tone(drift_ppm=180.0)
        self.assertAlmostEqual(audio.drift_ppm(recording, 48000, 1000, start=0.7, end=2.9), 180.0, delta=10.0)

    def test_the_same_clock_is_zero_and_slow_is_negative(self):
        self.assertAlmostEqual(audio.drift_ppm(recorded_tone(), 48000, 1000, start=0.7, end=2.9), 0.0, delta=10.0)
        slow = recorded_tone(drift_ppm=-500.0)
        self.assertAlmostEqual(audio.drift_ppm(slow, 48000, 1000, start=0.7, end=2.9), -500.0, delta=10.0)

    def test_too_short_is_none(self):
        self.assertIsNone(audio.drift_ppm(recorded_tone(), 48000, 1000, start=0.7, end=0.75))


class Bands(unittest.TestCase):
    def test_band_noise_stays_in_its_band(self):
        noise = audio.band_noise(1.5, 48000, 300, 1200, -20.0)
        inside = audio.band_level(noise, 48000, 300, 1200)
        self.assertAlmostEqual(inside, -20.5, delta=1.5)
        self.assertLess(audio.band_level(noise, 48000, 2400, 8000), inside - 20)
        self.assertEqual(noise, audio.band_noise(1.5, 48000, 300, 1200, -20.0))  # seeded

    def test_the_filters_pass_their_band_and_cut_outside(self):
        tone = audio.tone(4000, 0.5, 48000, -12.0)
        self.assertAlmostEqual(audio.band_level(tone, 48000, 2400, 8000), audio.dbfs(audio.rms(tone)), delta=0.5)
        self.assertLess(audio.band_level(tone, 48000, 300, 1200), audio.dbfs(audio.rms(tone)) - 40)
        with self.assertRaises(ValueError):
            audio.biquad([0.0], 48000, "bandstop", 1000)


class AtLevel(unittest.TestCase):
    def test_scaled_to_a_peak_with_a_floor_mixed_in(self):
        tone = audio.tone(500, 1.0, RATE, -4.5)
        quiet = audio.at_level(tone, -30.0, -64.0)
        self.assertAlmostEqual(audio.dbfs(audio.peak(quiet)), -30.0, delta=0.5)
        floor_only = audio.at_level(audio.silence(1.0, RATE), -30.0, -64.0)
        self.assertAlmostEqual(audio.dbfs(audio.rms(floor_only)), -64.0, delta=0.5)
        self.assertEqual(audio.at_level(tone, None, None), tone)


if __name__ == "__main__":
    unittest.main()
