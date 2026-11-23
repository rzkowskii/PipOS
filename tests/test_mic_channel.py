"""Choosing the microphone channel: mix, left, right or automatic."""

import asyncio
import random
import struct
import tempfile
import unittest
from array import array
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from mind import mic_channel
from mind.mic_channel import WINDOW_SECONDS, ChannelPicker, band_level, mono_command, setting
from mind.recognition import FRAME_BYTES, FRAME_SAMPLES, RATE
from mind.voice import STEREO_ATTEMPTS, capture_command, microphone_channel
from tests.test_voice_feedback import Harness, real_sleep

FRAMES_PER_SECOND = RATE // FRAME_SAMPLES


def stereo(left, right):
    """One interleaved frame from two lists of FRAME_SAMPLES samples."""
    samples = array("h")
    for a, b in zip(left, right):
        samples.extend((a, b))
    return samples.tobytes()


class Sounds:
    """Frames of made-up sound, the same every run."""

    def __init__(self, seed=1):
        self.random = random.Random(seed)

    def noise(self, amplitude):
        return [max(-32768, min(32767, round(self.random.gauss(0, amplitude)))) for _ in range(FRAME_SAMPLES)]

    def silence(self):
        return [0] * FRAME_SAMPLES

    def speech(self, seconds, floor=30):
        """Syllables of loud noise, 150 to 300 ms each, between quiet pauses."""
        frames = []
        while len(frames) < seconds * FRAMES_PER_SECOND:
            loud = self.random.uniform(2500, 9000)
            frames += [self.noise(loud) for _ in range(self.random.randint(5, 10))]
            frames += [self.noise(floor) for _ in range(self.random.randint(3, 10))]
        return frames[:round(seconds * FRAMES_PER_SECOND)]

    def static(self, seconds, amplitude=2000):
        return [self.noise(amplitude) for _ in range(round(seconds * FRAMES_PER_SECOND))]

    def clicks(self, seconds, amplitude=200):
        """Steady low static with a sharp click every 300 ms."""
        frames = self.static(seconds, amplitude)
        for index in range(0, len(frames), 10):
            frames[index] = [20000 if i % 2 else -20000 for i in range(FRAME_SAMPLES)]
        return frames


def feed(picker, left, right):
    return [picker.mono(stereo(a, b)) for a, b in zip(left, right)]


class Settings(unittest.TestCase):
    def cmdline(self, text):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "cmdline"
        path.write_text(text)
        return path

    def test_the_boot_entry_wins_over_the_environment_and_mix_is_the_default(self):
        missing = Path("/nonexistent/cmdline")
        self.assertEqual(setting({}, missing), ("mix", "the default"))
        self.assertEqual(setting({"BEING_MIC_CHANNEL": " Left "}, missing), ("left", "BEING_MIC_CHANNEL"))
        line = self.cmdline("ro quiet being.mic_channel=right snd_hda_intel.model=mbp81 being.mic_channel=auto\n")
        self.assertEqual(setting({"BEING_MIC_CHANNEL": "left"}, line), ("auto", "the kernel command line"))
        self.assertEqual(setting({}, self.cmdline("ro quiet")), ("mix", "the default"))

    def test_an_unknown_value_is_logged_and_ignored(self):
        with self.assertLogs("being.voice", "WARNING") as logs:
            self.assertEqual(setting({"BEING_MIC_CHANNEL": "left"}, self.cmdline("being.mic_channel=both")),
                             ("left", "BEING_MIC_CHANNEL"))
            self.assertEqual(setting({"BEING_MIC_CHANNEL": "stereo"}, Path("/nonexistent")), ("mix", "the default"))
        self.assertIn("'both' from the kernel command line", logs.output[0])
        self.assertIn("'stereo' from BEING_MIC_CHANNEL", logs.output[1])

    def test_mix_records_one_channel_and_the_others_two(self):
        self.assertEqual(capture_command("plughw:0,0")[capture_command("plughw:0,0").index("-c") + 1], "1")
        for choice in ("left", "right", "auto"):
            command = capture_command("plughw:0,0", choice)
            self.assertEqual(command[command.index("-c") + 1], "2", choice)
            self.assertEqual(mono_command(command), capture_command("plughw:0,0"))

    def test_the_lab_loopback_and_the_qemu_port_always_mix(self):
        line = self.cmdline("being.mic_channel=auto")
        with self.assertLogs("being.voice", "INFO") as logs:
            self.assertEqual(microphone_channel({}, line), "auto")
            self.assertEqual(microphone_channel({}, line, loopback=True), "mix")
            self.assertEqual(microphone_channel({}, line, port=True), "mix")
        self.assertIn("microphone channel: auto (from the kernel command line)", logs.output[0])
        self.assertIn("does not apply to the lab's loopback body", logs.output[1])


class FixedChannels(unittest.TestCase):
    def test_left_and_right_keep_their_channel(self):
        left, right = list(range(FRAME_SAMPLES)), [-value for value in range(FRAME_SAMPLES)]
        frame = stereo(left, right)
        self.assertEqual(ChannelPicker("left").mono(frame), array("h", left).tobytes())
        self.assertEqual(ChannelPicker("right").mono(frame), array("h", right).tobytes())
        self.assertEqual(len(ChannelPicker("auto").mono(frame)), FRAME_BYTES)
        self.assertEqual(ChannelPicker("left").frame_bytes, 2 * FRAME_BYTES)
        with self.assertRaises(ValueError):
            ChannelPicker("mix")

    def test_auto_averages_both_channels_until_it_decides(self):
        picker = ChannelPicker("auto")
        out = picker.mono(stereo([1000] * FRAME_SAMPLES, [-3001] * FRAME_SAMPLES))
        self.assertEqual(set(array("h", out)), {-1001})
        self.assertIsNone(picker.kept)

    def test_band_level_ignores_a_steady_offset_and_hears_speech_frequencies(self):
        offset = [8000] * FRAME_SAMPLES
        self.assertLess(band_level(offset), -100)
        import math
        tone = [round(8000 * math.sin(2 * math.pi * 2000 * i / RATE)) for i in range(FRAME_SAMPLES)]
        hum = [round(8000 * math.sin(2 * math.pi * 50 * i / RATE)) for i in range(FRAME_SAMPLES)]
        self.assertGreater(band_level(tone), band_level(hum) + 20)


class AutomaticChoice(unittest.TestCase):
    def setUp(self):
        self.sounds = Sounds()

    def run_picker(self, left, right):
        picker = ChannelPicker("auto")
        with self.assertLogs("being.voice", "INFO") as logs:
            out = feed(picker, left, right)
            mic_channel.LOG.info("end")  # assertLogs needs at least one line
        return picker, out, [line for line in logs.output if not line.endswith(":end")]

    def test_speech_on_one_side_and_static_on_the_other_keeps_the_speech(self):
        for side in (0, 1):
            with self.subTest(side=side):
                speech, static = self.sounds.speech(6), self.sounds.static(6)
                left, right = (speech, static) if side == 0 else (static, speech)
                picker, out, logs = self.run_picker(left, right)
                self.assertEqual(picker.kept, side)
                kept = (left, right)[side]
                self.assertEqual(out[-1], array("h", kept[-1]).tobytes(), "the kept channel is passed on as it is")
                decision = [line for line in logs if "listening to the" in line]
                self.assertEqual(len(decision), 1, logs)
                self.assertIn(f"listening to the {('left', 'right')[side]} channel", decision[0])
                self.assertIn("rises and falls like speech", decision[0])

    def test_the_choice_is_made_within_the_first_seconds(self):
        picker = ChannelPicker("auto")
        speech, static = self.sounds.speech(WINDOW_SECONDS + 0.5), self.sounds.static(WINDOW_SECONDS + 0.5)
        with self.assertLogs("being.voice", "INFO"):
            feed(picker, speech, static)
        self.assertEqual(picker.kept, 0)

    def test_speech_on_both_sides_stays_averaged(self):
        speech = self.sounds.speech(8)
        quieter = [[round(value * 0.7) for value in frame] for frame in speech]
        picker, _, logs = self.run_picker(speech, quieter)
        self.assertIsNone(picker.kept)
        self.assertTrue(any("neither stands out yet" in line for line in logs), logs)

    def test_speech_in_opposite_phase_keeps_one_channel_instead_of_cancelling(self):
        speech, hiss = self.sounds.speech(10), self.sounds.static(10, 20)
        inverted = [[max(-32768, min(32767, -a + b)) for a, b in zip(frame, noise)] for frame, noise in zip(speech, hiss)]
        picker, out, logs = self.run_picker(speech, inverted)
        self.assertIsNotNone(picker.kept)
        decisions = [line for line in logs if "averaging them cancels it" in line]
        self.assertEqual(len(decisions), 1, logs)
        self.assertFalse(any("switching" in line for line in logs), "two equal channels must not swap back and forth")
        kept = (speech, inverted)[picker.kept]
        self.assertEqual(out[-1], array("h", kept[-1]).tobytes())

    def test_static_in_opposite_phase_under_shared_speech_stays_averaged(self):
        # A differential pair: averaging cancels the static and keeps the speech.
        speech, static = self.sounds.speech(8), self.sounds.static(8, 1500)
        left = [[max(-32768, min(32767, a + b)) for a, b in zip(f, n)] for f, n in zip(speech, static)]
        right = [[max(-32768, min(32767, a - b)) for a, b in zip(f, n)] for f, n in zip(speech, static)]
        picker, _, _ = self.run_picker(left, right)
        self.assertIsNone(picker.kept)

    def test_without_speech_static_and_a_quiet_floor_stay_averaged(self):
        picker, _, _ = self.run_picker(self.sounds.static(8, 30), self.sounds.static(8, 3000))
        self.assertIsNone(picker.kept)

    def test_clicks_are_not_mistaken_for_speech(self):
        picker, _, _ = self.run_picker(self.sounds.static(8, 30), self.sounds.clicks(8))
        self.assertIsNone(picker.kept)

    def test_a_dead_channel_gives_way_to_the_live_one(self):
        frames = round(6 * FRAMES_PER_SECOND)
        picker, _, logs = self.run_picker([self.sounds.silence()] * frames, self.sounds.static(6, 40))
        self.assertEqual(picker.kept, 1)
        self.assertTrue(any("the left channel is digital silence" in line for line in logs), logs)

    def test_a_later_change_of_side_is_followed_and_logged(self):
        picker = ChannelPicker("auto")
        with self.assertLogs("being.voice", "INFO") as logs:
            feed(picker, self.sounds.speech(6), self.sounds.static(6))
            self.assertEqual(picker.kept, 0)
            feed(picker, self.sounds.static(6), self.sounds.speech(6))
        self.assertEqual(picker.kept, 1)
        self.assertTrue(any("switching to the right channel" in line for line in logs.output), logs.output)

    def test_frames_marked_unmeasured_do_not_count(self):
        picker = ChannelPicker("auto")
        for a, b in zip(self.sounds.speech(8), self.sounds.static(8)):
            picker.mono(stereo(a, b), measure=False)
        self.assertIsNone(picker.kept)
        self.assertEqual(len(picker.blocks[0]), 0)


class VoiceWithTwoChannels(unittest.IsolatedAsyncioTestCase):
    async def test_the_recognizer_hears_only_the_kept_channel(self):
        harness = Harness(channel=ChannelPicker("right"))
        voice = harness.voice
        left, right = [100] * FRAME_SAMPLES, [-700] * FRAME_SAMPLES
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(stereo(left, right))
                voice.disarm()
        self.assertEqual(harness.recognizer.fed[-1][0], array("h", right).tobytes())
        self.assertTrue(any("2 channels, keeping the right" in line for line in logs.output), logs.output)

    async def test_the_qemu_port_ignores_the_channel(self):
        harness = Harness(channel=ChannelPicker("auto"), mic_port=Mock())
        self.assertIsNone(harness.voice.channel)

    async def test_a_device_that_cannot_give_two_channels_falls_back_to_one(self):
        harness = Harness(channel=ChannelPicker("auto"))
        voice = harness.voice
        del voice.recording  # the real arecord path, with a stand-in for spawning it
        voice.capture_command = capture_command("plughw:0,0", "auto")
        commands = []
        stream = harness.stream

        @asynccontextmanager
        async def child(*command, **options):
            commands.append(command)
            if command[command.index("-c") + 1] == "2":
                raise OSError("Invalid argument")
            yield Mock(stdout=stream, returncode=None)

        async def pause(seconds):
            await real_sleep(0)

        with patch("mind.voice.child", child), patch("mind.voice.asyncio.sleep", pause), \
                self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                async with asyncio.timeout(1):
                    while len(commands) <= STEREO_ATTEMPTS:
                        await real_sleep(0)
                await harness.frames(struct.pack(f"<{FRAME_SAMPLES}h", *([300] * FRAME_SAMPLES)))
                voice.disarm()
        channels = [command[command.index("-c") + 1] for command in commands]
        self.assertEqual(channels, ["2"] * STEREO_ATTEMPTS + ["1"])
        self.assertIsNone(voice.channel)
        self.assertTrue(any("averaging both channels from now on" in line for line in logs.output), logs.output)
        self.assertEqual(harness.recognizer.fed[-1][0], struct.pack(f"<{FRAME_SAMPLES}h", *([300] * FRAME_SAMPLES)))


if __name__ == "__main__":
    unittest.main()
