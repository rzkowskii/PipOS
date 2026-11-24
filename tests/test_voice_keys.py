"""The voice's side of the top row's volume keys (mind/keys.py): a key mute
that is never stored, a start that is never silent, and the Mac's click."""

import asyncio
import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mind import turn
from mind.tts import CHIRP_PEAK, CHIRP_RATE, ChirpBank
from mind.voice import (
    CLICK_SECONDS, VOLUME_FLOOR, Voice, click_path, click_samples, load_voice_settings, save_voice_settings,
)


def sent(voice):
    return [(item.args[0], item.kwargs) for item in voice.bus.send.await_args_list]


def make_voice(**options):
    voice = Voice(Mock(), "Richard", [], "plughw:0,0", **options)
    voice.bus.send = AsyncMock()
    voice.bus.connected.set()
    voice.lid_open = True
    voice.muted = False
    voice.state = "idle"
    return voice


def hardware_mixer():
    return Mock(set=AsyncMock(return_value=True), store=AsyncMock(return_value=True),
                get=AsyncMock(return_value=None))


class KeyMute(unittest.IsolatedAsyncioTestCase):
    async def test_a_level_with_persist_false_is_applied_but_never_stored(self):
        with tempfile.TemporaryDirectory() as mind:
            save_voice_settings(mind, volume=62, chirps=True)
            mixer = hardware_mixer()
            voice = make_voice(settings_dir=Path(mind), settings=load_voice_settings(mind), mixer=mixer)
            async with asyncio.TaskGroup() as voice.tasks:
                await voice.handle({"t": "voice.volume", "level": 0, "persist": False})
            mixer.set.assert_awaited_once_with(0)
            mixer.store.assert_not_awaited()
            self.assertEqual(voice.volume, 0)
            self.assertIn(("voice.volume_state", {"level": 0, "mixer": "hardware"}), sent(voice))
            self.assertEqual(load_voice_settings(mind)["volume"], 62)

    async def test_a_later_save_keeps_the_level_from_before_the_mute(self):
        with tempfile.TemporaryDirectory() as mind:
            save_voice_settings(mind, volume=62, chirps=True)
            voice = make_voice(settings_dir=Path(mind), settings=load_voice_settings(mind),
                               mixer=Mock(set=AsyncMock(return_value=False)))
            await voice.handle({"t": "voice.volume", "level": 0, "persist": False})
            # Switching the chirps off saves the settings file: the mute's 0 stays out of it.
            await voice.handle({"t": "voice.chirps", "on": False})
            self.assertEqual(load_voice_settings(mind), {"volume": 62, "chirps": False})
            # A level with no persist field is the owner's again, and is kept.
            await voice.handle({"t": "voice.volume", "level": 44})
            self.assertEqual(load_voice_settings(mind)["volume"], 44)

    async def test_only_false_skips_the_store(self):
        with tempfile.TemporaryDirectory() as mind:
            mixer = hardware_mixer()
            voice = make_voice(settings_dir=Path(mind), mixer=mixer)
            async with asyncio.TaskGroup() as voice.tasks:
                await voice.handle({"t": "voice.volume", "level": 30, "persist": True})
                await voice.handle({"t": "voice.volume", "level": 35, "persist": "no"})
            self.assertEqual(mixer.store.await_count, 2)
            self.assertEqual(load_voice_settings(mind)["volume"], 35)


class StartingLevel(unittest.TestCase):
    def test_the_floor_is_the_minds(self):
        self.assertEqual(VOLUME_FLOOR, turn.VOLUME_FLOOR)

    def test_a_stored_level_under_the_floor_starts_at_the_floor(self):
        voice = make_voice(settings={"volume": 0, "chirps": True})
        with self.assertLogs("being.voice", "INFO") as logs:
            self.assertEqual(voice.starting_volume(0), VOLUME_FLOOR)
        self.assertIn("below 10", logs.output[0])
        self.assertEqual(voice.kept_volume, VOLUME_FLOOR)
        # The volume keys' quietest step is 6: still too quiet to start at.
        with self.assertLogs("being.voice", "INFO"):
            self.assertEqual(make_voice().starting_volume(6), VOLUME_FLOOR)
        loud = make_voice(settings={"volume": 55, "chirps": True})
        self.assertEqual(loud.starting_volume(55), 55)
        self.assertEqual(loud.kept_volume, 55)
        self.assertEqual(loud.starting_volume(VOLUME_FLOOR), VOLUME_FLOOR)


class StartingRun(unittest.IsolatedAsyncioTestCase):
    async def test_run_applies_the_floored_level(self):
        mixer = hardware_mixer()
        voice = make_voice(settings={"volume": 0, "chirps": True}, mixer=mixer)
        voice.bus.start = AsyncMock()
        voice.bus.recv = AsyncMock(side_effect=asyncio.CancelledError)
        voice.level_loop = AsyncMock()
        with patch("mind.voice.sd_notify.keep_alive", AsyncMock()), self.assertLogs("being.voice", "INFO"):
            with self.assertRaises(asyncio.CancelledError):
                await voice.run()
        mixer.set.assert_awaited_once_with(VOLUME_FLOOR)
        self.assertEqual(voice.volume, VOLUME_FLOOR)

    async def run_briefly(self, voice, events=()):
        """Run the voice through its start, hand it `events`, then stop it."""
        queue = list(events)

        async def recv():
            # Let the start's tasks (the level report) run before each event.
            for _ in range(5):
                await asyncio.sleep(0)
            if queue:
                return queue.pop(0)
            raise asyncio.CancelledError

        voice.bus.start = AsyncMock()
        voice.bus.recv = AsyncMock(side_effect=recv)
        voice.level_loop = AsyncMock()
        with patch("mind.voice.sd_notify.keep_alive", AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await voice.run()

    def states(self, voice):
        return [fields for topic, fields in sent(voice) if topic == "voice.volume_state"]

    async def test_the_start_reports_the_level_it_plays_at(self):
        # A key mute is never saved: a voice that restarts during one (the
        # watchdog, pip-diag) plays at the saved 50 and says so, which clears
        # the face's "speaker muted" and the mind's mute.
        with tempfile.TemporaryDirectory() as mind:
            save_voice_settings(mind, volume=50, chirps=True)
            voice = make_voice(settings_dir=Path(mind), settings=load_voice_settings(mind), mixer=hardware_mixer())
            await self.run_briefly(voice)
            self.assertEqual(self.states(voice), [{"level": 50, "mixer": "hardware"}])
            self.assertEqual(load_voice_settings(mind)["volume"], 50)

    async def test_a_level_raised_to_the_floor_is_reported_and_saved(self):
        with tempfile.TemporaryDirectory() as mind:
            save_voice_settings(mind, volume=6, chirps=True)
            voice = make_voice(settings_dir=Path(mind), settings=load_voice_settings(mind),
                               mixer=Mock(set=AsyncMock(return_value=False)))
            with self.assertLogs("being.voice", "INFO"):
                await self.run_briefly(voice)
            self.assertEqual(self.states(voice), [{"level": VOLUME_FLOOR, "mixer": "software"}])
            self.assertEqual(load_voice_settings(mind)["volume"], VOLUME_FLOOR)

    async def test_the_report_waits_for_the_bus(self):
        voice = make_voice(settings={"volume": 40, "chirps": True}, mixer=hardware_mixer())
        voice.bus.connected.clear()
        connect = asyncio.get_running_loop().call_later(0.01, voice.bus.connected.set)
        try:
            await self.run_briefly(voice)
        finally:
            connect.cancel()
        # Stopped before the bus came up: nothing was sent into nowhere.
        self.assertEqual(self.states(voice), [])
        voice = make_voice(settings={"volume": 40, "chirps": True}, mixer=hardware_mixer())
        voice.bus.connected.clear()
        asyncio.get_running_loop().call_soon(voice.bus.connected.set)
        await self.run_briefly(voice)
        self.assertEqual(self.states(voice), [{"level": 40, "mixer": "hardware"}])

    async def test_no_stored_level_reports_nothing(self):
        voice = make_voice(mixer=hardware_mixer())
        await self.run_briefly(voice, [{"t": "mind.ready"}])
        self.assertEqual(self.states(voice), [])

    async def test_a_mind_that_connects_later_hears_the_level(self):
        # At boot the voice may say its level before the mind is on the bus;
        # the mind's ready hears it again.
        voice = make_voice(settings={"volume": 6, "chirps": True}, mixer=hardware_mixer())
        with self.assertLogs("being.voice", "INFO"):
            await self.run_briefly(voice, [{"t": "mind.ready"}])
        self.assertEqual(self.states(voice), [{"level": VOLUME_FLOOR, "mixer": "hardware"}] * 2)

    async def test_a_key_mute_is_never_reported_to_a_new_mind(self):
        # The mind restarted during a key mute: the voice's 0 is not the
        # owner's level, and the mind's marker brings the sound back itself.
        voice = make_voice(settings={"volume": 50, "chirps": True}, mixer=hardware_mixer())
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.handle({"t": "voice.volume", "level": 0, "persist": False})
            voice.bus.send.reset_mock()
            await voice.handle({"t": "mind.ready"})
        self.assertEqual(self.states(voice), [])


class ClickSound(unittest.TestCase):
    def test_the_click_is_short_soft_and_clean(self):
        samples = click_samples()
        self.assertEqual(len(samples), int(CLICK_SECONDS * CHIRP_RATE))
        self.assertLess(len(samples) / CHIRP_RATE, 0.08)
        self.assertLessEqual(max(abs(value) for value in samples), CHIRP_PEAK)
        self.assertGreater(max(abs(value) for value in samples), CHIRP_PEAK // 10)
        # It starts from nothing and dies away, so it makes no pop of its own.
        self.assertEqual(samples[0], 0)
        self.assertLess(abs(samples[-1]), CHIRP_PEAK // 100)
        quiet = click_samples(0.25)
        self.assertLess(max(map(abs, quiet)), max(map(abs, samples)) * 0.3)
        self.assertEqual(max(map(abs, click_samples(0.0))), 0)

    def test_one_gain_is_kept_at_a_time(self):
        with tempfile.TemporaryDirectory() as work:
            first = click_path(Path(work) / "chirps", 0.5)
            self.assertEqual(first.name, "click-g50.wav")
            with wave.open(str(first)) as wav:
                self.assertEqual((wav.getnchannels(), wav.getframerate()), (1, CHIRP_RATE))
                self.assertLess(wav.getnframes() / wav.getframerate(), 0.08)
            self.assertEqual(click_path(Path(work) / "chirps", 0.5), first)
            second = click_path(Path(work) / "chirps", 1.0)
            self.assertFalse(first.exists())
            self.assertEqual(sorted(p.name for p in second.parent.iterdir()), ["click-g100.wav"])


class FeedbackClick(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.played = []

    def clicking_voice(self, **options):
        options.setdefault("mixer", Mock(set=AsyncMock(return_value=False)))
        voice = make_voice(chirp_bank=ChirpBank(Path(self.work.name)), chirps=True, **options)

        async def play_chirp(path, *, announce=False):
            self.played.append(Path(path).name)
            return True

        voice.play_chirp = play_chirp
        return voice

    async def press(self, voice, level, **fields):
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.handle({"t": "voice.volume", "level": level, **fields})
        voice.tasks = None

    async def test_feedback_clicks_at_the_new_gain(self):
        voice = self.clicking_voice()
        await self.press(voice, 50, feedback=True)
        # Software volume: the click is scaled as speech is (volume_gain(50) is a quarter).
        self.assertEqual(self.played, ["click-g25.wav"])
        hardware = self.clicking_voice(mixer=hardware_mixer())
        await self.press(hardware, 50, feedback=True)
        # The card's mixer sets the level, so the samples play unscaled.
        self.assertEqual(self.played[-1], "click-g100.wav")

    async def test_no_click_without_feedback_or_at_zero(self):
        voice = self.clicking_voice()
        await self.press(voice, 50)
        await self.press(voice, 0, feedback=True, persist=False)
        await self.press(voice, 40, feedback=False)
        self.assertEqual(self.played, [])

    async def test_no_click_where_chirps_may_not_play(self):
        for setting, value in (("chirps_on", False), ("muted", True), ("lid_open", False)):
            with self.subTest(setting=setting):
                voice = self.clicking_voice()
                setattr(voice, setting, value)
                await self.press(voice, 50, feedback=True)
        speaking = self.clicking_voice()
        speaking.reply = asyncio.get_running_loop().create_future()
        await self.press(speaking, 50, feedback=True)
        quiet = self.clicking_voice(quiet_hours=lambda clock: True)
        await self.press(quiet, 50, feedback=True)
        self.assertEqual(self.played, [])

    async def test_no_click_while_the_microphone_is_open_for_dictation(self):
        listening = self.clicking_voice()
        listening.state = "listening"
        await self.press(listening, 50, feedback=True)
        attending = self.clicking_voice()
        attending.state = "attending"
        attending.dictation_until = math.inf
        await self.press(attending, 50, feedback=True)
        dictating = self.clicking_voice()
        dictating.dictation = object()
        await self.press(dictating, 50, feedback=True)
        self.assertEqual(self.played, [])
        # The level still took effect each time; only the sound was held back.
        self.assertEqual((listening.volume, attending.volume, dictating.volume), (50, 50, 50))
        # Once the window has closed, the next press clicks.
        attending.dictation_until = 0.0
        await self.press(attending, 56, feedback=True)
        self.assertEqual(len(self.played), 1)

    async def test_a_new_click_replaces_one_still_playing(self):
        voice = self.clicking_voice()
        started, cancelled = asyncio.Event(), []

        async def slow_chirp(path, *, announce=False):
            self.played.append(Path(path).name)
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(Path(path).name)
                raise
            return True

        voice.play_chirp = slow_chirp
        self.assertTrue(await voice.volume_click())
        await asyncio.wait_for(started.wait(), 1)
        first = voice.chirp_task
        voice.gain = 0.5
        self.assertTrue(await voice.volume_click())
        await asyncio.sleep(0)
        self.assertTrue(first.done())
        self.assertEqual(cancelled, ["click-g100.wav"])
        self.assertIsNot(voice.chirp_task, first)
        await voice.stop_chirp()
        self.assertEqual(self.played, ["click-g100.wav", "click-g50.wav"])

    async def test_a_click_that_cannot_be_made_is_logged_not_raised(self):
        voice = self.clicking_voice()
        with patch("mind.voice.click_path", side_effect=OSError("no room")), \
                self.assertLogs("being.voice", "INFO") as logs:
            await self.press(voice, 50, feedback=True)
        self.assertIn("Could not make the volume click: no room", logs.output[-1])
        self.assertEqual(self.played, [])
        self.assertIn(("voice.volume_state", {"level": 50, "mixer": "software"}), sent(voice))


if __name__ == "__main__":
    unittest.main()
