"""pip-diag's sound card sections with a simulated card: floor, mixer, sweep, speaker, loopback."""

import io
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import diag_audio as audio
from mind import diag_sound as sound
from mind.diag_report import Report
from mind.diag_system import Result
from tests.test_diag import FakeHost, FakeProcess


def noise(seconds, rate, level_dbfs, seed):
    generator = random.Random(seed)
    amplitude = audio.from_dbfs(level_dbfs) * math.sqrt(3)
    return audio.to_pcm([generator.uniform(-amplitude, amplitude) for _ in range(int(seconds * rate))])


def argv_value(argv, flag):
    return argv[argv.index(flag) + 1]


class TimedProcess(FakeProcess):
    """Runs until the fake clock passes its length, then finishes (and may write a file)."""

    def __init__(self, argv, host, seconds, on_finish=None):
        super().__init__(argv, host)
        self.ends = host.clock + seconds
        self.on_finish = on_finish
        self.finished = False

    def poll(self):
        if self.host.clock >= self.ends:
            self._finish()
            return 0
        return None

    def wait(self, timeout=None):
        self.host.clock = max(self.host.clock, self.ends)
        self._finish()
        return 0

    def communicate(self, timeout=None):
        self.wait()
        return b"", self.host.aplay_stderr.encode()

    def _finish(self):
        if not self.finished:
            self.finished = True
            self.returncode = 0
            if self.on_finish:
                self.on_finish()


class Card(FakeHost):
    """A sound card: arecord writes a room floor (left dead); a recording made while
    aplay plays hears the stimulus 40 ms late at -18 dB; the hardware pointer runs at `speed`."""

    def __init__(self, root, *, left_dbfs=-84.0, right_dbfs=-63.0, speed=1.0, underrun=False, delay=0.04,
                 drift_ppm=0.0, divide=8, heard_on=(0, 1)):
        super().__init__(root, programs={"arecord", "aplay", "amixer", "alsactl", "dmesg", "systemctl"},
                         handlers={"arecord": self.arecord, "amixer": self.amixer, "alsactl": self.alsactl,
                                   "dmesg": lambda argv: Result(0, "")})
        self.levels = (left_dbfs, right_dbfs)
        self.speed = speed
        self.aplay_stderr = "Plug PCM: Rate conversion PCM (48000, sformat=S16_LE)\nunderrun!!! (at least 1 ms)\n" \
            if underrun else "Plug PCM: Hardware PCM card 0 'HDA Intel PCH' device 0 subdevice 0\n"
        self.played = []
        self.mixer = {"Mic Boost": 2, "Capture": 54}
        self.started = None
        # Where the stimulus lands after the recording starts (preroll + delay), how fast
        # the playback clock runs against the capture's, and how much quieter it comes back.
        self.delay, self.drift_ppm, self.divide = delay, drift_ppm, divide
        self.heard_on = heard_on  # the microphone channels that hear the speaker

    def arecord(self, argv):
        path = Path(argv[-1])
        rate = int(argv_value(argv, "-r"))
        seconds = int(argv_value(argv, "-d"))
        channels = [noise(seconds, rate, level, seed) for seed, level in enumerate(self.levels)]
        audio.write_wav(path, channels, rate)
        return Result(0, "", "HW Params of device \"hw:0,0\":\nACCESS: RW_INTERLEAVED\nRATE: 16000\n")

    def amixer(self, argv):
        if "sset" in argv:
            self.mixer[argv[-2]] = int(argv[-1]) if argv[-1].isdigit() else argv[-1]
            return Result(0, "")
        if "scontents" in argv:
            return Result(0, "Simple mixer control 'Mic Boost',0\n  Front Left: 2 [67%] [20.00dB]\n"
                             "Simple mixer control 'Capture',0\n  Front Left: Capture 54 [86%] [3.00dB] [on]\n")
        if "sget" in argv:
            name = argv[argv.index("sget") + 1]
            return Result(0, f"  Front Left: {self.mixer.get(name, 0)} [50%]\n")
        return Result(0, "numid=10,iface=MIXER,name='Mic Boost Volume'\n")

    def alsactl(self, argv):
        if argv[1] == "store":
            Path(argv[3]).write_text("saved")
        return Result(0, "")

    def spawn(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "aplay":
            path = argv[-1]
            self.played.append((argv[argv.index("-D") + 1], Path(path).name))
            self.started = self.clock
            self.status_dir().mkdir(parents=True, exist_ok=True)
            (self.status_dir() / "hw_params").write_text("access: RW_INTERLEAVED\nformat: S16_LE\nchannels: 2\n"
                                                           "rate: 48000 (48000/1)\n")
            self.current = Path(path)
            return TimedProcess(argv, self, audio.wav_seconds(path))
        if argv[0] == "arecord":
            path = Path(argv[-1])
            rate = int(argv_value(argv, "-r"))
            seconds = int(argv_value(argv, "-d"))
            return TimedProcess(argv, self, seconds, on_finish=lambda: self.write_loop(path, rate, seconds))
        return super().spawn(argv, **kwargs)

    def status_dir(self):
        return self.path(sound.PCM_PLAYBACK)

    def read(self, path, limit=4 * 1024 * 1024):
        if str(path).endswith("pcm0p/sub0/status") and self.started is not None:
            elapsed = self.clock - self.started
            pointer = int(elapsed * 48000 * self.speed)
            return f"state: RUNNING\ntstamp      : {self.clock:.6f}\nhw_ptr      : {pointer}\n"
        return super().read(path, limit)

    def write_loop(self, path, rate, seconds):
        floor = [noise(seconds, rate, level, 10 + seed) for seed, level in enumerate(self.levels)]
        source_rate, source = audio.read_wav(self.current)
        # A playback clock fast by d plays the file at source_rate x (1 + d).
        stimulus = audio.resample(source[0], source_rate * (1 + self.drift_ppm * 1e-6), rate)
        start = int((0.3 + self.delay) * rate)
        for channel in (floor[number] for number in self.heard_on):
            for index, value in enumerate(stimulus[:len(channel) - start]):
                channel[start + index] = max(-32768, min(32767, channel[start + index] + value // self.divide))
        audio.write_wav(path, floor, rate)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.folder = self.root / "report"
        self.folder.mkdir()
        self.report = Report(self.folder, {}, out=io.StringIO())


class Floor(Base):
    def test_a_dead_left_channel_is_named_with_the_fix(self):
        item = sound.check_mic_floor(Card(self.root), self.report)
        self.assertEqual(item["status"], "warn")
        self.assertIn("left channel is dead", item["evidence"])
        self.assertIn("mic_channel=auto", item["hint"])
        left, right = item["value"]["channels"]
        self.assertAlmostEqual(left["rms_dbfs"], -84.0, delta=0.5)
        self.assertAlmostEqual(right["rms_dbfs"], -63.0, delta=0.5)
        # Room sound may hold voices: not kept without consent.
        self.assertFalse((self.folder / "mic-floor.wav").exists())
        self.assertTrue((self.folder / "hwparams-capture.txt").exists())

    def test_digital_silence_fails_and_a_kept_recording_stays(self):
        item = sound.check_mic_floor(Card(self.root, left_dbfs=-130, right_dbfs=-130), self.report, keep=True)
        self.assertEqual(item["status"], "fail")
        self.assertIn("being-audio-init --defaults", item["hint"])
        self.assertTrue((self.folder / "mic-floor.wav").exists())

    def test_both_channels_alive_pass(self):
        self.assertEqual(sound.check_mic_floor(Card(self.root, left_dbfs=-64), self.report)["status"], "pass")

    def test_no_arecord_fails_plainly(self):
        item = sound.check_mic_floor(FakeHost(self.root), self.report)
        self.assertEqual(item["status"], "fail")

    def scratch_files(self, host):
        folder = host.path(str(sound.SCRATCH))
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []

    def test_the_room_is_recorded_in_ram_never_on_the_stick(self):
        host = Card(self.root, left_dbfs=-64)
        written = []
        original = host.arecord
        host.handlers["arecord"] = lambda argv: written.append(argv[-1]) or original(argv)
        sound.check_mic_floor(host, self.report)
        self.assertTrue(written)
        self.assertTrue(all(str(host.path(str(sound.SCRATCH))) in path for path in written), written)
        self.assertEqual(self.scratch_files(host), [])
        self.assertFalse(any(path.suffix == ".wav" for path in self.folder.iterdir()))

    def test_a_failed_room_recording_is_removed_too(self):
        host = Card(self.root)

        def broken(argv):
            Card.arecord(host, argv)  # writes part of a file, then fails
            return Result(1, "", "arecord: pcm_read:2178: read error: Input/output error\n")

        host.handlers["arecord"] = broken
        item = sound.check_mic_floor(host, self.report)
        self.assertEqual(item["status"], "fail")
        self.assertEqual(self.scratch_files(host), [])
        self.assertFalse(any(path.suffix == ".wav" for path in self.folder.iterdir()))

    def test_the_sweep_floor_never_reaches_the_stick(self):
        host = Card(self.root)
        with sound.AudioSession(host, signals=False) as session:
            sound.check_gain_sweep(host, self.report, session, boosts=(0,), captures=(57,))
        self.assertFalse([path.name for path in self.folder.iterdir() if path.name.endswith("-floor.wav")])
        self.assertEqual(self.scratch_files(host), [])
        host.handlers["arecord"] = lambda argv: (Card.arecord(host, argv), Result(1, "", "overrun"))[1]
        self.assertIsNone(sound.tone_snr(host, self.report, self.folder / "x.wav", -20, "sweep-b0-c51"))
        self.assertEqual(self.scratch_files(host), [])


class Mixer(Base):
    def test_the_v5_mixer_values_and_a_kept_state(self):
        host = Card(self.root)
        item = sound.check_mixer(host, self.report)
        self.assertEqual(item["status"], "pass")
        self.assertIn("Mic Boost 2 (20.0 dB)", item["evidence"])
        host.made(str(sound.KEPT_STATE), "state")
        kept = sound.check_mixer(host, self.report)
        self.assertTrue(kept["value"]["kept_state_overrides_defaults"])
        self.assertIn("being-audio-init --defaults", kept["hint"])

    def test_parse_scontents(self):
        controls = sound.parse_scontents("Simple mixer control 'Capture',0\n"
                                         "  Front Left: Capture 54 [86%] [3.00dB] [on]\n"
                                         "  Front Right: Capture 54 [86%] [3.00dB] [on]\n")
        self.assertEqual(controls["Capture"]["Front Right"], (54, 86, 3.0))

    def test_the_sweep_always_puts_the_mixer_back(self):
        host = Card(self.root)
        host.active.add(sound.VOICE)
        with patch.object(sound, "tone_snr", side_effect=RuntimeError("card vanished")):
            with sound.AudioSession(host, signals=False) as session:
                with self.assertRaises(RuntimeError):
                    sound.check_gain_sweep(host, self.report, session)
        restores = [call for call in host.calls if call[:2] == ["alsactl", "restore"]]
        self.assertGreaterEqual(len(restores), 2)  # by the sweep, and by the session

    def test_a_sweep_where_gain_does_not_change_snr(self):
        host = Card(self.root)
        with sound.AudioSession(host, signals=False) as session:
            item = sound.check_gain_sweep(host, self.report, session, boosts=(0, 3), captures=(57,))
        self.assertEqual(item["status"], "pass")
        self.assertIn("noise sits before the gain", item["hint"])
        self.assertEqual(host.mixer["Mic Boost"], 3)  # set during the sweep; alsactl restore puts it back


class Playback(Base):
    def test_pointer_ratio(self):
        samples = [({"state": "RUNNING", "hw_ptr": "0", "tstamp": "10.0"}, 0),
                   ({"state": "RUNNING", "hw_ptr": "48000", "tstamp": "11.0"}, 0)]
        self.assertEqual(sound.pointer_ratio(samples, 48000), 1.0)
        self.assertIsNone(sound.pointer_ratio(samples[:1], 48000))

    def test_the_battery_at_the_right_speed_passes(self):
        host = Card(self.root)
        checks = sound.check_speaker_battery(host, self.report, full=False)
        self.assertEqual([item["status"] for item in checks], ["pass"])
        self.assertAlmostEqual(checks[0]["value"]["pointer_ratio"], 1.0, delta=0.01)
        self.assertTrue((self.folder / "stim-tone1k-20-16k.wav").exists())  # Pip's own test signals are kept
        self.assertFalse((self.folder / "stim-sweep-48k.wav").exists())  # a quick run makes only its tone
        self.assertTrue((self.folder / "aplay-chains.txt").exists())

    def test_a_slow_card_fails_and_an_underrun_warns(self):
        slow = sound.check_speaker_battery(Card(self.root, speed=0.92), self.report, full=False)
        self.assertEqual(slow[0]["status"], "fail")
        self.assertIn("wrong speed", slow[0]["hint"])
        dry = sound.check_speaker_battery(Card(self.root, underrun=True), self.report, full=False)
        self.assertEqual(dry[0]["status"], "warn")

    def test_the_full_battery_tries_plughw_and_hw(self):
        host = Card(self.root)
        checks = sound.check_speaker_battery(host, self.report, full=True)
        devices = {device for device, _ in host.played}
        self.assertEqual(devices, {"plughw:0,0", "hw:0,0"})
        self.assertTrue(any(item["id"].endswith("4ch.hw") for item in checks))


class Loopback(Base):
    def test_level_latency_thd_and_bands_from_a_known_stimulus(self):
        host = Card(self.root, left_dbfs=-70)
        checks = {item["id"]: item for item in sound.check_loopback(host, self.report)}
        level = checks["loopback.level"]
        self.assertEqual(level["status"], "pass")
        self.assertAlmostEqual(level["value"]["latency_s"], 0.04, delta=0.005)
        self.assertEqual(checks["loopback.thd"]["status"], "pass")
        self.assertEqual(checks["loopback.dropouts"]["value"], 0)
        self.assertIn("1000", checks["loopback.bands"]["value"])
        self.assertTrue(any(path.name.startswith("loop-") for path in self.folder.iterdir()))

    def test_nothing_heard_fails(self):
        host = Card(self.root)
        host.write_loop = lambda path, rate, seconds: audio.write_wav(
            path, [noise(seconds, rate, -80, 1), noise(seconds, rate, -80, 2)], rate)
        level = sound.check_loopback(host, self.report)[0]
        self.assertEqual(level["status"], "fail")
        self.assertFalse(level["value"]["heard"])

    def test_heard_comes_from_the_level_not_from_correlating_a_sine(self):
        # On the laptop the -20 dBFS tone came back 25 dB above the right channel's floor,
        # but a cross-correlation of the steady sine gave match 0.056 and -0.256 s: a false fail.
        host = Card(self.root, left_dbfs=-84, right_dbfs=-65.7, divide=10, heard_on=(1,))
        with patch.object(audio, "latency", return_value=(-0.256, 0.056)):
            checks = {item["id"]: item for item in sound.check_loopback(host, self.report)}
        level = checks["loopback.level"]
        self.assertEqual(level["status"], "pass")
        self.assertEqual(level["value"]["channel"], 1)  # the right channel, where the tone is
        self.assertGreater(level["value"]["rise_db"], 15)
        self.assertGreater(level["value"]["bin_snr_db"], 30)
        self.assertAlmostEqual(level["value"]["latency_s"], 0.04, delta=0.004)  # from the tone's onset
        self.assertTrue(level["value"]["floor_measured"])
        self.assertIn("dB above the room floor", level["evidence"])
        self.assertEqual(checks["loopback.thd"]["status"], "pass")

    def test_a_capture_that_started_late_gives_an_unknown_delay_not_a_negative_one(self):
        host = Card(self.root, delay=-0.2)  # the tone is already playing 0.1 s into the recording
        level = sound.check_loopback(host, self.report)[0]
        self.assertEqual(level["status"], "pass")
        self.assertIsNone(level["value"]["latency_s"])
        self.assertIn("delay unknown", level["evidence"])

    def test_the_floor_is_recorded_in_ram_and_not_kept(self):
        host = Card(self.root)
        sound.check_loopback(host, self.report)
        self.assertFalse((self.folder / "loop-silence-48k.wav").exists())
        self.assertTrue((self.folder / "loop-tone1k-20-16k.wav").exists())
        folder = host.path(str(sound.SCRATCH))
        self.assertEqual(sorted(path.name for path in folder.iterdir()) if folder.is_dir() else [], [])
        # A kept loopback recording is the louder channel alone (half the bytes on the stick).
        rate, channels = audio.read_wav(self.folder / "loop-tone1k-20-16k.wav")
        self.assertEqual((rate, len(channels)), (48000, 1))

    def test_the_natural_voices_rate_and_its_clock_drift(self):
        checks = {item["id"]: item for item in sound.check_loopback(Card(self.root, drift_ppm=180), self.report)}
        rate = checks["loopback.rate_22k"]
        self.assertEqual(rate["status"], "pass")
        self.assertAlmostEqual(rate["value"]["drift_ppm"], 180, delta=15)
        self.assertIn("+", rate["evidence"])
        far = {item["id"]: item for item in sound.check_loopback(Card(self.root, drift_ppm=2500), self.report)}
        self.assertEqual(far["loopback.rate_22k"]["status"], "warn")


class Distortion(unittest.TestCase):
    """THD counts only harmonics that stand above the room in their own bins."""

    RATE = 48000

    def recording(self, harmonic=0.0, tone_dbfs=-38.0, floor_dbfs=-63.0, seconds=2.0):
        floor = noise(seconds, self.RATE, floor_dbfs, 3)
        amplitude = audio.from_dbfs(tone_dbfs) * 32767
        step = 2 * math.pi * 1000 / self.RATE
        return array_of([floor[index] + amplitude * (math.sin(step * index) + harmonic * math.sin(2 * step * index))
                         for index in range(len(floor))])

    def test_noise_in_the_harmonic_bins_is_below_the_floor_not_distortion(self):
        silence = noise(2.0, self.RATE, -63.0, 9)
        result = audio.thd_against_floor(self.recording(), silence, self.RATE, 1000)
        self.assertTrue(result["measurable"])
        self.assertTrue(result["below_floor"])
        self.assertIsNone(result["percent"])
        self.assertLess(result["limit_percent"], 1.0)

    def test_real_second_harmonic_distortion_is_measured(self):
        silence = noise(2.0, self.RATE, -63.0, 9)
        result = audio.thd_against_floor(self.recording(harmonic=0.05), silence, self.RATE, 1000)
        self.assertAlmostEqual(result["percent"], 5.0, delta=0.5)
        self.assertTrue(result["harmonics"][0]["counted"])

    def test_a_tone_barely_above_the_floor_is_not_measurable(self):
        silence = noise(2.0, self.RATE, -40.0, 9)
        result = audio.thd_against_floor(self.recording(tone_dbfs=-75.0, floor_dbfs=-40.0), silence, self.RATE, 1000)
        self.assertFalse(result["measurable"])
        self.assertIn("above the floor", result["reason"])

    def test_the_loopback_says_below_the_floor_instead_of_a_percentage(self):
        rows = [{"stimulus": "tone1k-20-16k", "thd": {"measurable": False, "reason": "the tone is only 9 dB above "
                 "the floor in its bin", "percent": None, "below_floor": False, "limit_percent": None,
                 "snr_db": 9.0, "harmonics": []}},
                {"stimulus": "tone1k-12-16k", "thd": {"measurable": True, "percent": None, "below_floor": True,
                 "limit_percent": 0.4, "snr_db": 45.0, "harmonics": [], "reason": ""}}]
        item = sound.judge_thd(rows)
        self.assertEqual(item["status"], "pass")
        self.assertIn("tone1k-12-16k below the floor (< 0.4%)", item["evidence"])
        self.assertIn("tone1k-20-16k not measurable", item["evidence"])
        self.assertEqual(sound.judge_thd(rows[:1])["status"], "skip")


def array_of(values):
    from array import array

    return array("h", [max(-32768, min(32767, int(round(value)))) for value in values])


class PinCard(Card):
    """The MacBookPro9,2's speakers: Bass Speaker (0x0a) carries 300-1200 Hz from the right
    channel only (its left is dead); Speaker (0x0b) carries 1.2-8 kHz, its right 12 dB down.
    The microphone hears on its right channel."""

    def __init__(self, root, *, speaker_plays=True, **kwargs):
        super().__init__(root, **kwargs)
        self.speaker_plays = speaker_plays
        self.handlers["alsactl"] = self.pins_alsactl

    def pins_alsactl(self, argv):
        if argv[1] == "restore":
            for name in ("Speaker", "Bass Speaker"):
                self.mixer.pop(name, None)
        return Card.alsactl(self, argv)

    def write_loop(self, path, rate, seconds):
        floor = [noise(seconds, rate, level, 10 + seed) for seed, level in enumerate(self.levels)]
        source_rate, source = audio.read_wav(self.current)
        left, right = (source + source)[:2] if len(source) == 1 else source[:2]
        out = [0.0] * len(left)
        if self.mixer.get("Bass Speaker") != "mute":
            for index, value in enumerate(audio.band_filter(right, source_rate, 300, 1200)):
                out[index] += value
        if self.mixer.get("Speaker") != "mute" and self.speaker_plays:
            mixed = [l_value + r_value / 4 for l_value, r_value in zip(left, right)]
            for index, value in enumerate(audio.band_filter(mixed, source_rate, 1200, 8000)):
                out[index] += value
        heard = audio.resample(array_of(out), source_rate, rate)
        start = int((0.3 + self.delay) * rate)
        channel = floor[1]
        for index, value in enumerate(heard[:len(channel) - start]):
            channel[start + index] = max(-32768, min(32767, channel[start + index] + value // self.divide))
        audio.write_wav(path, floor, rate)


class Isolation(Base):
    def run_isolation(self, host):
        files = sound.speaker_stimuli(self.report, -20.0, {"silence-48k", "band300-1200-48k", "band1200-2400-48k",
                                                           "band2400-8000-48k", "band300-8000-48k-left",
                                                           "band300-8000-48k-right"})
        with sound.AudioSession(host, signals=False) as session:
            items = sound.check_speaker_isolation(host, self.report, session, files)
        return {item["id"]: item for item in items}

    def test_each_pin_is_measured_in_its_own_band_and_on_each_channel(self):
        items = self.run_isolation(PinCard(self.root))
        pins = items["speaker.isolation"]
        self.assertEqual(pins["status"], "pass")
        self.assertIn("300-1200 Hz: Bass Speaker", pins["evidence"])
        self.assertIn("1200-2400 Hz: Speaker", pins["evidence"])
        self.assertIn("2400-8000 Hz: Speaker", pins["evidence"])
        self.assertEqual(pins["value"]["mic_channel"], 1)
        bands = pins["value"]["bands"]
        self.assertGreater(bands["300-1200 Hz"]["bass_only"], bands["300-1200 Hz"]["speaker_only"] + 6)
        self.assertGreater(bands["1200-2400 Hz"]["speaker_only"], bands["1200-2400 Hz"]["bass_only"] + 6)
        sides = items["speaker.isolation.channels"]
        self.assertEqual(sides["status"], "pass")
        self.assertIn("Bass Speaker's left channel is at the room floor", sides["evidence"])
        self.assertIn("Speaker's right channel is", sides["evidence"])
        # Each pin carries a band of its own: nothing here may suggest muting one.
        for item in items.values():
            self.assertNotIn("mute", (item["hint"] + item["evidence"]).lower().replace("muted", ""))

    def test_a_pin_that_plays_nothing_warns(self):
        items = self.run_isolation(PinCard(self.root, speaker_plays=False))
        pins = items["speaker.isolation"]
        self.assertEqual(pins["status"], "warn")
        self.assertIn("Speaker gives nothing above the room in any band", pins["evidence"])
        self.assertEqual(items["speaker.isolation.channels"]["status"], "warn")

    def test_the_mixer_is_put_back_after(self):
        host = PinCard(self.root)
        self.run_isolation(host)
        self.assertNotIn("Speaker", host.mixer)
        self.assertNotIn("Bass Speaker", host.mixer)


if __name__ == "__main__":
    unittest.main()


class PeriodCard(Card):
    """A card whose playback misbehaves only at small periods, like a late position report would."""

    def __init__(self, root, **kwargs):
        super().__init__(root, **kwargs)
        self.small = False
        self.bdl = 0
        self.handlers["dmesg"] = lambda argv: Result(0, "snd_hda_intel: IRQ timing workaround is activated "
                                                         "for card #0. Suggest a bigger bdl_pos_adj.\n" * self.bdl)
        self.aplay_argvs = []

    def spawn(self, argv, **kwargs):
        if argv[0] == "aplay":
            self.aplay_argvs.append(list(argv))
            self.small = "--period-time=5000" in argv
            self.aplay_stderr = "underrun!!! (at least 2 ms)\n" if self.small else "Hardware PCM card 0\n"
            if self.small:
                self.bdl += 1
        process = super().spawn(argv, **kwargs)
        if argv[0] == "aplay":
            (self.status_dir() / "hw_params").write_text(
                "access: RW_INTERLEAVED\nformat: S16_LE\nchannels: 2\nrate: 48000 (48000/1)\n"
                f"period_size: {240 if self.small else 1024}\nbuffer_size: {960 if self.small else 4096}\n")
        return process

    def write_loop(self, path, rate, seconds):
        super().write_loop(path, rate, seconds)
        if self.small:  # the chopped buffer: 60 ms of nothing in the middle of the tone
            file_rate, channels = audio.read_wav(path)
            start = int(1.5 * file_rate)
            for channel in channels:
                for index in range(start, start + int(0.06 * file_rate)):
                    channel[index] = 0
            audio.write_wav(path, channels, file_rate)


class Timing(Base):
    def test_every_period_size_clean_passes_and_asks_aplay_for_each_size(self):
        host = PeriodCard(self.root)
        host.spawn = lambda argv, _spawn=Card.spawn.__get__(host), **kw: _spawn(argv, **kw)  # never small faults
        checks = {item["id"]: item for item in sound.check_speaker_timing(host, self.report)}
        self.assertEqual([checks[f"speaker.timing.{name}"]["status"] for name in ("small", "default", "large")],
                         ["pass", "pass", "pass"])
        self.assertEqual(checks["speaker.timing"]["status"], "pass")
        aplays = [argv for argv in host.calls if argv[0] == "aplay"]
        self.assertTrue(any("--period-time=5000" in argv and "--buffer-time=20000" in argv for argv in aplays))
        self.assertTrue(any("--period-time=100000" in argv and "--buffer-time=400000" in argv for argv in aplays))
        self.assertTrue(any(not any(arg.startswith("--period") for arg in argv) for argv in aplays))
        self.assertEqual({argv[argv.index("-D") + 1] for argv in aplays}, {"plughw:0,0", "hw:0,0"})
        # 3 sizes x (16 kHz tone and speech, 22050 Hz tone on plughw; 48 kHz tone and speech on hw)
        self.assertEqual(len(aplays), 15)
        self.assertTrue((self.folder / "speaker-timing.json").exists())
        self.assertTrue((self.folder / "stim-speech-16k.wav").exists())

    def test_a_fault_only_at_small_periods_is_called_a_timing_fault(self):
        host = PeriodCard(self.root)
        checks = {item["id"]: item for item in sound.check_speaker_timing(host, self.report)}
        small = checks["speaker.timing.small"]
        self.assertEqual(small["status"], "warn")
        self.assertIn("underruns", small["evidence"])
        self.assertIn("bdl_pos_adj", small["evidence"])
        self.assertIn("period 240 / buffer 960 frames", small["evidence"])
        tone_rows = [row for row in small["value"] if row["stimulus"] == "tone1k-12-16k"]
        self.assertGreater(tone_rows[0]["dropouts"], 0)
        self.assertEqual(checks["speaker.timing.default"]["status"], "pass")
        self.assertEqual(checks["speaker.timing.large"]["status"], "pass")
        summary = checks["speaker.timing"]
        self.assertEqual(summary["status"], "warn")
        self.assertEqual(summary["value"], {"faulty": ["small"], "clean": ["default", "large"]})
        self.assertIn("period-timing fault", summary["hint"])

    def test_without_arecord_it_still_times_each_playback(self):
        host = PeriodCard(self.root)
        host.programs.discard("arecord")
        checks = {item["id"]: item for item in sound.check_speaker_timing(host, self.report)}
        self.assertEqual(checks["speaker.timing.small"]["status"], "warn")
        self.assertTrue(all(row["dropouts"] is None for row in checks["speaker.timing.small"]["value"]))

    def test_no_aplay_skips(self):
        host = FakeHost(self.root)
        self.assertEqual(sound.check_speaker_timing(host, self.report)[0]["status"], "skip")


class VoiceFlow(Base):
    """`pip-diag voice` with nobody answering: nothing kept, the voice back, every step reported."""

    def test_the_owner_flow_keeps_no_owner_speech_without_a_yes(self):
        import json
        from contextlib import redirect_stderr, redirect_stdout

        from mind import diag, diag_speech

        host = Card(self.root)
        host.active.add(sound.VOICE)
        out = self.root / "voice-run"
        prompts = []
        with patch.object(diag_speech, "open_sphinx", return_value=(None, "no PocketSphinx")), \
                patch.object(diag_speech, "moonshine_ready", return_value=(False, "no Moonshine")), \
                patch.object(diag_speech, "piper_engine", return_value=(None, "no Piper")), \
                patch.object(diag_speech, "_scratch", return_value=str(self.root)), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = diag.main(["voice", "--trials", "2", "--out", str(out)], host=host,
                             reader=lambda text: prompts.append(text) or "")
        data = json.loads((out / "diag.json").read_text())
        ids = {item["id"]: item["status"] for item in data["checks"]}
        self.assertFalse(data["keep_recordings"])
        self.assertTrue(prompts[0].startswith("Keep recordings and transcripts on the stick? [y/N]"))
        self.assertFalse([path.name for path in out.iterdir() if path.name.startswith("owner-")])
        self.assertFalse([path.name for path in self.root.iterdir() if path.name.startswith("pip-diag-owner-")])
        self.assertIn(sound.VOICE, host.active)
        for name in ("voice.mic_speech", "voice.corpus.sphinx", "voice.listening", "voice.wake_1m",
                     "voice.false_wakes"):
            self.assertIn(name, ids)
        self.assertEqual(ids["voice.wake_1m"], "fail")
        self.assertEqual(code, 1)


class Runs(Base):
    """The speaker and mic subcommands end to end on the simulated card."""

    def run_command(self, argv):
        import json
        from contextlib import redirect_stdout

        from mind import diag

        host = Card(self.root, left_dbfs=-70)
        host.active.add(sound.VOICE)
        host.made("/proc/asound/card0/id", "PCH")
        out = self.root / argv[0]
        with redirect_stdout(io.StringIO()):
            code = diag.main(argv + ["--out", str(out)], host=host)
        data = json.loads((out / "diag.json").read_text())
        return code, {item["id"]: item["status"] for item in data["checks"]}, data, host

    def test_speaker(self):
        code, ids, data, host = self.run_command(["speaker"])
        self.assertEqual(code, 0)
        for name in ("speaker.play.sweep-48k.plug", "speaker.play.tone1k-12-48k.hw", "speaker.play.tone1k-12-22k.plug",
                     "speaker.play.speech-22k.plug", "loopback.level", "loopback.thd", "loopback.bands",
                     "loopback.rate_22k", "speaker.isolation", "speaker.isolation.channels"):
            self.assertEqual(ids[name], "pass", name)
        self.assertEqual(data["extra"]["restore"], ["restored the mixer", "restarted the voice"])
        self.assertIn(sound.VOICE, host.active)
        stimuli = [path for path in (self.root / "speaker").iterdir() if path.name.startswith("stim-")]
        self.assertEqual(len(stimuli), 20)  # made once, shared by every check
        for setting in ("small", "default", "large"):
            self.assertEqual(ids[f"speaker.timing.{setting}"], "pass", setting)
        self.assertEqual(ids["speaker.timing"], "pass")

    def test_mic(self):
        code, ids, _, host = self.run_command(["mic"])
        self.assertEqual((code, ids["mic.floor"], ids["mic.gain_sweep"]), (0, "pass", "pass"))
        self.assertIn(sound.VOICE, host.active)
