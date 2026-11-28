"""A voice organ that survives bad audio, keeps its volume, and listens honestly."""

import asyncio
import json
import math
import os
import tempfile
import unittest
import wave
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import mind.voice as voice_module
from mind.initiative import InitiativePolicy, QuietHours, save_policy
from mind.recognition import FRAME_BYTES
from mind.voice import (
    FOLLOW_UP_SECONDS, SILENCE, Mixer, Voice, lab_default, load_voice_settings, mixer_card,
    quiet_hours_check, save_voice_settings,
)


def write_wav(path, frames=1600, rate=16000, amplitude=1000):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes((amplitude).to_bytes(2, "little", signed=True) * frames)


def sent(voice):
    return [(item.args[0], item.kwargs) for item in voice.bus.send.await_args_list]


def make_voice(**options):
    voice = Voice(Mock(), "Richard", [], "plughw:0,0", **options)
    voice.bus.send = AsyncMock()
    voice.bus.connected.set()
    voice.lid_open = True
    voice.muted = False
    return voice


class ReplyFailures(unittest.IsolatedAsyncioTestCase):
    async def speak(self, voice, clauses, fake_child):
        for text, last in clauses:
            voice.clauses.put_nowait({"text": text, "last": last})
        with patch("mind.voice.child", fake_child), \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 3)

    async def test_a_failed_clause_is_skipped_and_the_reply_still_finishes(self):
        voice = make_voice()
        played = []

        async def play(audio, first):
            played.append(first)
            if first:
                await voice.send_audio("say.started")
                voice._start_sent = True

        voice.play = play

        @asynccontextmanager
        async def fake_child(*command, **options):
            yield Mock(wait=AsyncMock(return_value=1 if command[-3] == "Broken." else 0))

        with patch("builtins.print"):
            await self.speak(voice, [("First.", False), ("Broken.", False), ("Third.", False), ("", True)],
                             fake_child)
        self.assertEqual(played, [True, False])
        self.assertIn(("say.done", {"final": True, "failed": True}), sent(voice))

    async def test_a_playback_error_skips_only_that_clause(self):
        voice = make_voice()
        calls = []

        async def play(audio, first):
            calls.append(first)
            if len(calls) == 1:
                raise RuntimeError("ALSA could not play the reply")

        voice.play = play

        @asynccontextmanager
        async def fake_child(*command, **options):
            yield Mock(wait=AsyncMock(return_value=0))

        with patch("builtins.print"):
            await self.speak(voice, [("First.", False), ("Second.", True)], fake_child)
        # Nothing reached the speaker the first time, so the second clause opens the reply.
        self.assertEqual(calls, [True, True])
        done = [fields for topic, fields in sent(voice) if topic == "say.done"]
        self.assertEqual(done, [{"final": True, "failed": True}])

    async def test_nothing_said_plays_the_trouble_sound_so_thinking_can_end(self):
        with tempfile.TemporaryDirectory() as work:
            bank = Mock()
            bank.path.return_value = Path(work) / "trouble.wav"
            write_wav(bank.path.return_value)
            voice = make_voice(chirp_bank=bank)
            voice.play = AsyncMock()
            commands = []

            @asynccontextmanager
            async def fake_child(*command, **options):
                commands.append(command)
                yield Mock(wait=AsyncMock(return_value=1 if command[0] == "flite" else 0))

            with patch("builtins.print"):
                await self.speak(voice, [("Only clause.", True)], fake_child)
        bank.path.assert_called_once_with("trouble", 1.0)
        self.assertEqual(commands[-1][0], "aplay")
        topics = [topic for topic, _ in sent(voice)]
        self.assertLess(topics.index("say.started"), topics.index("say.done"))
        self.assertIn(("say.done", {"final": True, "failed": True}), sent(voice))
        voice.play.assert_not_awaited()

    async def test_without_any_sound_the_reply_still_reports_done(self):
        voice = make_voice()

        @asynccontextmanager
        async def fake_child(*command, **options):
            raise FileNotFoundError("flite")
            yield

        with patch("builtins.print"):
            await self.speak(voice, [("Only clause.", True)], fake_child)
        self.assertNotIn("say.started", [topic for topic, _ in sent(voice)])
        self.assertIn(("say.done", {"final": True, "failed": True}), sent(voice))

    async def test_failed_announcements_are_not_echoed_as_delivered(self):
        voice = make_voice()
        voice.play = AsyncMock()
        voice.clauses.put_nowait({"text": "Reminder.", "last": False})
        voice.clauses.put_nowait({"text": "Broken.", "last": False})
        voice.clauses.put_nowait({"text": "", "last": True, "announcement": "due-1"})

        @asynccontextmanager
        async def fake_child(*command, **options):
            yield Mock(wait=AsyncMock(return_value=1 if command[-3] == "Broken." else 0))

        with patch("mind.voice.child", fake_child), patch("builtins.print"), \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 3)
        done = [fields for topic, fields in sent(voice) if topic == "say.done"]
        self.assertEqual(done, [{"final": True, "failed": True}])

    async def test_a_hung_synthesizer_is_bounded(self):
        voice = make_voice()
        voice.play = AsyncMock()
        voice.clauses.put_nowait({"text": "Hang.", "last": True})
        halted = []

        @asynccontextmanager
        async def fake_child(*command, **options):
            try:
                yield Mock(wait=asyncio.Future)
            finally:
                halted.append(command[0])

        with patch("mind.voice.child", fake_child), patch("mind.voice.synthesis_timeout", return_value=0.05), \
                patch("builtins.print"), patch("mind.voice.tempfile.TemporaryDirectory",
                                               return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 3)
        self.assertEqual(halted, ["flite"])
        self.assertIn(("say.done", {"final": True, "failed": True}), sent(voice))

    async def test_a_stuck_aplay_is_bounded(self):
        with tempfile.TemporaryDirectory() as work:
            audio = Path(work) / "clause.wav"
            write_wav(audio, frames=160)
            voice = make_voice()
            killed = []

            @asynccontextmanager
            async def fake_child(*command, **options):
                try:
                    yield Mock(returncode=None)
                finally:
                    killed.append(command[0])

            with patch("mind.voice.child", fake_child), patch("mind.voice.PLAYBACK_MARGIN", 0.1):
                with self.assertRaisesRegex(RuntimeError, "did not finish"):
                    await asyncio.wait_for(voice.play(audio, first=True), 3)
            self.assertEqual(killed, ["aplay"])


class CaptureRetry(unittest.IsolatedAsyncioTestCase):
    async def test_capture_errors_back_off_and_recover(self):
        voice = make_voice()
        voice.state = "idle"
        attempts = []
        recovered = asyncio.Event()

        @asynccontextmanager
        async def recording():
            attempts.append(asyncio.get_running_loop().time())
            if len(attempts) < 4:
                raise OSError("Input/output error")
            recovered.set()
            yield Mock(readexactly=AsyncMock(side_effect=asyncio.IncompleteReadError(b"", FRAME_BYTES)))

        voice.recording = recording
        with patch("mind.voice.CAPTURE_RETRY_SECONDS", 0.02), patch("mind.voice.MAX_CAPTURE_RETRY_SECONDS", 0.05), \
                patch("builtins.print") as printed:
            task = asyncio.create_task(voice.listen())
            await asyncio.wait_for(recovered.wait(), 3)
            await asyncio.sleep(0)
            voice.lid_open = False
            await asyncio.wait_for(task, 3)
        gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
        self.assertGreaterEqual(gaps[1], gaps[0] * 1.5)
        self.assertLessEqual(max(gaps), 0.5)
        self.assertIn("Microphone trouble", printed.call_args_list[0].args[0])

    async def test_capture_stops_retrying_when_the_lid_closes(self):
        voice = make_voice()
        voice.state = "idle"

        @asynccontextmanager
        async def recording():
            voice.lid_open = False
            raise OSError("gone")
            yield

        voice.recording = recording
        with patch("builtins.print"):
            await asyncio.wait_for(voice.listen(), 3)


class Frames:
    """A microphone stream that serves a fixed number of frames, then waits."""

    def __init__(self, count):
        self.count = count

    async def readexactly(self, size):
        if self.count:
            self.count -= 1
            return b"\x01" * size
        await asyncio.Future()


class Listening(unittest.IsolatedAsyncioTestCase):
    async def run_frames(self, voice, count=1):
        stream = Frames(count)

        @asynccontextmanager
        async def recording():
            yield stream

        voice.recording = recording
        task = asyncio.create_task(voice.capture_stream())
        for _ in range(50):
            await asyncio.sleep(0)
            if not stream.count:
                break
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_wake_spotting_continues_while_thinking(self):
        voice = make_voice()
        voice.recognizer.feed.return_value = []
        await voice.handle({"t": "state.changed", "state": "thinking", "prev": "listening", "muted": False})
        self.assertTrue(voice.capture_wanted())
        await self.run_frames(voice)
        voice.recognizer.feed.assert_called_with(b"\x01" * FRAME_BYTES, False, False)

    async def test_follow_up_window_after_a_prompted_reply(self):
        voice = make_voice()
        clock = [100.0]
        with patch("mind.voice.time.monotonic", lambda: clock[0]):
            voice.follow_state("idle", "attending")
            voice.state = "attending"
            self.assertTrue(voice.dictation_open())
            clock[0] = 1000.0
            self.assertTrue(voice.dictation_open())  # a wake lasts the brainstem's whole window

            await voice.hear("hear.final", {"text": "what time is it"})
            voice.follow_state("listening", "thinking")
            voice.follow_state("speaking", "attending")
            self.assertEqual(voice.dictation_until, 1000.0 + FOLLOW_UP_SECONDS)
            clock[0] += FOLLOW_UP_SECONDS - 0.1
            self.assertTrue(voice.dictation_open())
            clock[0] += 0.2
            self.assertFalse(voice.dictation_open())

    async def test_a_false_start_does_not_renew_the_window(self):
        voice = make_voice()
        with patch("mind.voice.time.monotonic", return_value=50.0):
            voice._prompted = True
            voice.follow_state("speaking", "attending")
            until = voice.dictation_until
            voice.follow_state("attending", "listening")
            voice.follow_state("listening", "attending")  # hear.cancel
            self.assertEqual(voice.dictation_until, until)

    async def test_snapshots_never_open_or_renew_the_window(self):
        voice = make_voice()
        voice.listen = AsyncMock()
        with patch("mind.voice.time.monotonic", return_value=50.0):
            voice.state = "attending"
            voice.dictation_until = 0.0
            # A health or mute change re-sends the state with no `prev`.
            await voice.handle({"t": "state.changed", "state": "attending", "muted": False, "unwell": True})
            self.assertFalse(voice.dictation_open())
            # So does a reconnect snapshot.
            voice.connection_lost()
            voice.bus.connected.set()
            voice.lid_open = True
            await voice.handle({"t": "state.changed", "state": "attending", "muted": False})
            self.assertFalse(voice.dictation_open())
            # A real transition from rest opens it.
            await voice.handle({"t": "state.changed", "state": "idle", "prev": "attending", "muted": False})
            await voice.handle({"t": "state.changed", "state": "attending", "prev": "idle", "muted": False})
            self.assertTrue(voice.dictation_open())
            await voice.stop_capture()

    async def test_one_breath_words_are_dropped_if_the_wake_was_not_taken(self):
        voice = make_voice()
        voice.state = "idle"  # muted, asleep or busy brainstem: it stayed at rest
        await voice.hear("hear.partial", {"text": "what time", "oneshot": True})
        await voice.hear("hear.final", {"text": "what time is it", "oneshot": True})
        voice.bus.send.assert_not_awaited()
        self.assertFalse(voice._prompted)

    async def test_one_breath_words_wait_for_the_brainstem_to_take_the_wake(self):
        # A backlog in the microphone stream can be decoded before the
        # brainstem's state change is read: the final waits for it.
        voice = make_voice()
        voice.state = "idle"
        await voice.hear("hear.wake", {"oneshot": True})
        self.assertTrue(voice.wake_pending)

        async def brainstem_answers():
            await voice.handle({"t": "state.changed", "state": "attending", "prev": "idle", "muted": False})
            await voice.handle({"t": "state.changed", "state": "listening", "prev": "attending", "muted": False})

        voice.sync_capture = AsyncMock()
        voice.state_chirp = AsyncMock()
        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(brainstem_answers()))
        await voice.hear("hear.partial", {"text": "what time", "oneshot": True})
        await asyncio.wait_for(voice.hear("hear.final", {"text": "what time is it", "oneshot": True}), 1)
        self.assertEqual(voice.state, "listening")
        self.assertIn(("hear.final", {"text": "what time is it", "oneshot": True, "speaker": "Richard"}),
                      sent(voice))
        self.assertFalse(voice.wake_pending)

    async def test_a_declined_one_breath_wake_still_drops_its_words(self):
        voice = make_voice()
        voice.state = "idle"
        await voice.hear("hear.wake", {"oneshot": True})
        voice.bus.send.reset_mock()
        loop = asyncio.get_running_loop()
        began = loop.time()
        with patch("mind.voice.WAKE_ANSWER_SECONDS", 0.05):
            await asyncio.wait_for(voice.hear("hear.final", {"text": "what time is it", "oneshot": True}), 1)
        self.assertGreaterEqual(loop.time() - began, 0.04)
        voice.bus.send.assert_not_awaited()
        self.assertFalse(voice.wake_pending)
        self.assertIsNone(voice.dictation)

    async def test_a_backlog_of_frames_gives_the_bus_its_turn(self):
        # readexactly returns at once while frames are queued; the capture
        # loop still yields, so a lid close or a state change is read between
        # frames, not after the backlog.
        voice = make_voice()
        voice.recognizer.feed.return_value = []
        voice.state = "idle"
        seen = []

        async def bus_turn():
            seen.append(voice.frames_read)

        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(bus_turn()))
        await self.run_frames(voice, count=20)
        self.assertEqual(voice.frames_read, 20)
        self.assertTrue(seen and seen[0] < 20, seen)

    async def test_unprompted_turns_close_the_window(self):
        voice = make_voice()
        with patch("mind.voice.time.monotonic", return_value=50.0):
            voice.follow_state("idle", "thinking")  # an announcement
            voice.follow_state("speaking", "attending")
            voice.state = "attending"
            self.assertFalse(voice.dictation_open())

    async def test_closed_window_listens_only_for_the_name(self):
        voice = make_voice()
        voice.recognizer.feed.return_value = [("hear.wake", {})]
        voice.state = "attending"
        voice.dictation_until = 0.0
        await self.run_frames(voice)
        voice.recognizer.feed.assert_called_with(b"\x01" * FRAME_BYTES, False, True)
        self.assertEqual(voice.dictation_until, math.inf)
        self.assertIn(("hear.wake", {}), sent(voice))

    async def test_earcons_are_not_heard_by_its_own_ears(self):
        voice = make_voice()
        voice.recognizer.feed.return_value = []
        voice.state = "idle"
        voice.deaf_until = math.inf
        voice.deaf_windows = [[-math.inf, math.inf]]
        await self.run_frames(voice)
        voice.recognizer.feed.assert_called_with(SILENCE, False, True)

    async def test_onset_sounds_are_rate_limited_and_only_at_rest(self):
        voice = make_voice(hear_sounds=True)
        clock = [10.0]
        with patch("mind.voice.time.monotonic", lambda: clock[0]):
            voice.state = "idle"
            await voice.hear("hear.sound", {})
            clock[0] += 2
            await voice.hear("hear.sound", {})
            clock[0] += 20
            voice.state = "drowsy"
            await voice.hear("hear.sound", {})
            clock[0] += 20
            voice.state = "thinking"
            await voice.hear("hear.sound", {})
        self.assertEqual([topic for topic, _ in sent(voice)], ["hear.sound", "hear.sound"])
        quiet = make_voice()
        quiet.state = "idle"
        await quiet.hear("hear.sound", {})
        quiet.bus.send.assert_not_awaited()

    async def test_follow_up_finals_are_marked_for_the_mind(self):
        voice = make_voice()
        voice._prompted = True
        voice.follow_state("speaking", "attending")
        await voice.hear("hear.final", {"text": "and tomorrow"})
        voice.follow_state("thinking", "speaking")
        await voice.hear("hear.wake", {})
        await voice.hear("hear.final", {"text": "what time is it"})
        finals = [fields for topic, fields in sent(voice) if topic == "hear.final"]
        self.assertEqual(finals[0].get("followup"), True)
        self.assertNotIn("followup", finals[1])

    async def test_finals_carry_the_speaker(self):
        voice = make_voice()
        voice.state = "listening"
        voice.lid_seq = 4
        await voice.hear("hear.final", {"text": "what time is it", "oneshot": True})
        self.assertEqual(sent(voice), [("hear.final", {"text": "what time is it", "oneshot": True,
                                                       "speaker": "Richard", "lid_seq": 4})])
        self.assertTrue(voice._prompted)


class Volume(unittest.IsolatedAsyncioTestCase):
    async def test_hardware_volume_is_set_saved_and_stored(self):
        with tempfile.TemporaryDirectory() as mind:
            mixer = Mock(set=AsyncMock(return_value=True), store=AsyncMock(return_value=True))
            voice = make_voice(settings_dir=Path(mind), mixer=mixer)
            async with asyncio.TaskGroup() as voice.tasks:
                await voice.handle({"t": "voice.volume", "level": 40})
            mixer.set.assert_awaited_once_with(40, switch=True)
            mixer.store.assert_awaited_once()
            self.assertEqual(voice.gain, 1.0)
            self.assertIn(("voice.volume_state", {"level": 40, "mixer": "hardware"}), sent(voice))
            self.assertEqual(load_voice_settings(mind), {"volume": 40, "chirps": True})
            info = os.stat(Path(mind) / "audio" / "voice.json")
            self.assertEqual(info.st_mode & 0o077, 0)

    async def test_without_a_mixer_samples_are_scaled(self):
        voice = make_voice(mixer=Mock(set=AsyncMock(return_value=False)))
        await voice.handle({"t": "voice.volume", "level": 50})
        self.assertAlmostEqual(voice.gain, 0.25)
        self.assertIn(("voice.volume_state", {"level": 50, "mixer": "software"}), sent(voice))
        with tempfile.TemporaryDirectory() as work:
            audio = Path(work) / "clause.wav"

            @asynccontextmanager
            async def fake_child(*command, **options):
                write_wav(audio, amplitude=8000)
                yield Mock(wait=AsyncMock(return_value=0))

            with patch("mind.voice.child", fake_child):
                await voice.synthesize("Hello.", audio, False)
            with wave.open(str(audio)) as wav:
                self.assertEqual(int.from_bytes(wav.readframes(1), "little", signed=True), 2000)

    async def test_relative_steps_and_bad_levels(self):
        voice = make_voice()
        with patch("builtins.print"):
            # No level known and no mixer: samples play unscaled, which is full level.
            await voice.handle({"t": "voice.volume", "step": -10})
            self.assertEqual(voice.volume, 90)
            await voice.handle({"t": "voice.volume", "step": 50})
            self.assertEqual(voice.volume, 100)
            for bad in ({"level": 101}, {"level": True}, {"level": "50"}, {}):
                await voice.handle({"t": "voice.volume", **bad})
        self.assertEqual(voice.volume, 100)
        self.assertEqual(len(sent(voice)), 2)

    async def test_a_first_relative_step_starts_from_the_mixer_level(self):
        mixer = Mock(get=AsyncMock(return_value=40), set=AsyncMock(return_value=True),
                     store=AsyncMock(return_value=True))
        voice = make_voice(mixer=mixer)
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.handle({"t": "voice.volume", "step": 10})
        mixer.set.assert_awaited_once_with(50, switch=True)
        silent = make_voice(mixer=Mock(get=AsyncMock(return_value=None), set=AsyncMock(return_value=False)))
        await silent.handle({"t": "voice.volume", "step": 10})
        # Software volume from unscaled samples: "louder" can never make it quieter.
        self.assertEqual(silent.volume, 100)
        self.assertEqual(silent.gain, 1.0)

    async def test_mixer_reads_its_level(self):
        output = b"Simple mixer control 'Master',0\n  Front Left: Playback 40 [63%] [on]\n" \
                 b"  Front Right: Playback 40 [61%] [on]\n"

        @asynccontextmanager
        async def fake_child(*command, **options):
            self.assertEqual(command, ("amixer", "-M", "-c", "PCH", "sget", "Master"))
            yield Mock(communicate=AsyncMock(return_value=(output, b"")), returncode=0)

        with patch("mind.voice.child", fake_child):
            self.assertEqual(await Mixer("PCH").get(), 63)

        @asynccontextmanager
        async def failing(*command, **options):
            yield Mock(communicate=AsyncMock(return_value=(b"", b"")), returncode=1)

        with patch("mind.voice.child", failing):
            self.assertIsNone(await Mixer("PCH").get())

    async def test_saved_level_is_applied_at_start(self):
        mixer = Mock(set=AsyncMock(return_value=False), playback_muted=AsyncMock(return_value=False))
        voice = make_voice(settings={"volume": 30, "chirps": True}, mixer=mixer)
        voice.bus.start = AsyncMock()
        voice.bus.stop = AsyncMock()
        voice.bus.recv = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await voice.run()
        mixer.set.assert_awaited_once_with(30, switch=False)
        self.assertAlmostEqual(voice.gain, 0.09)

    async def test_mixer_commands(self):
        commands = []

        @asynccontextmanager
        async def fake_child(*command, **options):
            commands.append(command)
            yield Mock(wait=AsyncMock(return_value=0))

        with tempfile.TemporaryDirectory() as mind:
            mixer = Mixer("PCH", "Master", Path(mind) / "audio" / "asound.state")
            with patch("mind.voice.child", fake_child):
                self.assertTrue(await mixer.set(0))
                self.assertTrue(await mixer.store())
            self.assertEqual(commands, [
                ("amixer", "-q", "-M", "-c", "PCH", "sset", "Master", "0%"),
                ("amixer", "-q", "-c", "PCH", "sset", "Master", "mute"),
                ("alsactl", "-f", str(Path(mind) / "audio" / "asound.state"), "store", "PCH"),
            ])

    async def test_a_missing_amixer_means_software_volume(self):
        @asynccontextmanager
        async def missing(*command, **options):
            raise FileNotFoundError(command[0])
            yield

        with patch("mind.voice.child", missing):
            self.assertFalse(await Mixer("0").set(50))
            self.assertFalse(await Mixer("0").store())

    def test_card_names(self):
        self.assertEqual(mixer_card("plughw:CARD=PCH,DEV=0"), "PCH")
        self.assertEqual(mixer_card("plughw:0,0"), "0")
        self.assertEqual(mixer_card("plughw:Loopback,0,1"), "Loopback")
        self.assertEqual(mixer_card("default"), "0")

    def test_damaged_settings_give_defaults(self):
        with tempfile.TemporaryDirectory() as mind:
            path = Path(mind) / "audio" / "voice.json"
            self.assertEqual(load_voice_settings(mind), {"volume": None, "chirps": True})
            save_voice_settings(mind, volume=55, chirps=False)
            self.assertEqual(load_voice_settings(mind), {"volume": 55, "chirps": False})
            for damage in (b"{", b"[]", json.dumps({"version": 9}).encode(), b"x" * 2000):
                path.write_bytes(damage)
                with patch("builtins.print"):
                    self.assertEqual(load_voice_settings(mind), {"volume": None, "chirps": True})
            path.write_text(json.dumps({"version": 1, "volume": 300, "chirps": "yes"}))
            self.assertEqual(load_voice_settings(mind), {"volume": None, "chirps": True})


class Chirps(unittest.IsolatedAsyncioTestCase):
    def chirpy(self, **options):
        bank = Mock()
        bank.path.side_effect = lambda name, gain: Path(f"/run/{name}.wav")
        voice = make_voice(chirp_bank=bank, chirps=True, **options)
        voice.sound = AsyncMock(return_value=True)
        return voice

    async def test_state_changes_choose_the_earcon(self):
        voice = self.chirpy()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.state_chirp("idle", "attending")
            voice._prompted = True
            with patch("mind.voice.PURR_AFTER", 0.01):
                await voice.state_chirp("listening", "thinking")
                voice.state = "thinking"
                await asyncio.sleep(0.05)
                voice.state = "speaking"
                voice.purr_task.cancel()
            await voice.state_chirp("drowsy", "asleep")
            await voice.state_chirp("speaking", "attending")
        names = [item.args[0] for item in voice.chirp_bank.path.call_args_list]
        self.assertEqual(names, ["ears", "heard", "purr", "sigh"])

    async def test_one_breath_wakes_skip_the_ears_cue(self):
        voice = self.chirpy()
        voice._oneshot_wake = True
        await voice.state_chirp("idle", "attending")
        voice.chirp_bank.path.assert_not_called()
        self.assertFalse(voice._oneshot_wake)

    async def test_chirps_obey_mute_lid_switch_quiet_hours_and_replies(self):
        for change in ("muted", "lid", "switch", "quiet", "reply", "unknown"):
            with self.subTest(change=change):
                voice = self.chirpy(quiet_hours=lambda clock: change == "quiet")
                if change == "muted":
                    voice.muted = True
                elif change == "lid":
                    voice.lid_open = False
                elif change == "switch":
                    voice.chirps_on = False
                elif change == "reply":
                    voice.reply = asyncio.get_running_loop().create_future()
                name = "nonsense" if change == "unknown" else "yes"
                self.assertFalse(await voice.chirp(name))
                voice.chirp_bank.path.assert_not_called()

    async def test_stem_chirp_and_the_switch(self):
        with tempfile.TemporaryDirectory() as mind:
            voice = self.chirpy(settings_dir=Path(mind))
            async with asyncio.TaskGroup() as voice.tasks:
                await voice.handle({"t": "stem.chirp", "name": "thanks"})
                await voice.handle({"t": "voice.chirps", "on": False})
                await voice.handle({"t": "stem.chirp", "name": "bell"})
            self.assertEqual([item.args[0] for item in voice.chirp_bank.path.call_args_list], ["thanks"])
            self.assertEqual(load_voice_settings(mind)["chirps"], False)

    async def test_a_reply_stops_a_playing_chirp(self):
        voice = make_voice(chirps=True)
        voice.state = "thinking"
        voice.speak = AsyncMock()
        playing = asyncio.create_task(asyncio.sleep(10))
        voice.chirp_task = playing
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.handle({"t": "say.request", "text": "Hi.", "last": True})
        self.assertTrue(playing.cancelled())
        self.assertIsNone(voice.chirp_task)

    async def test_play_chirp_makes_the_ears_deaf_while_it_plays(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "ears.wav"
            write_wav(path, frames=1600)
            voice = make_voice()
            seen = []

            @asynccontextmanager
            async def fake_child(*command, **options):
                seen.append((command, voice.deaf_until - voice_module.time.monotonic()))
                yield Mock(wait=AsyncMock(return_value=0))

            with patch("mind.voice.child", fake_child):
                self.assertTrue(await voice.play_chirp(path))
            self.assertEqual(seen[0][0], ("aplay", "-q", "-D", "plughw:0,0", str(path)))
            self.assertGreater(seen[0][1], 0.1)

    def test_quiet_hours_follow_the_owner_policy(self):
        with tempfile.TemporaryDirectory() as mind:
            quiet = quiet_hours_check(mind)
            self.assertFalse(quiet((23, 0)))
            save_policy(mind, InitiativePolicy(quiet_hours=QuietHours("22:00", "07:00")))
            self.assertTrue(quiet((23, 0)))
            self.assertFalse(quiet((12, 0)))
            self.assertTrue(quiet(None))

    async def test_body_state_sets_the_clock(self):
        voice = make_voice()
        await voice.handle({"t": "body.state", "hour": 7, "minute": 5})
        self.assertEqual(voice.clock, (7, 5))
        await voice.handle({"t": "body.state", "hour": None, "minute": None})
        self.assertIsNone(voice.clock)

    def test_lab_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(lab_default("BEING_CHIRPS", in_lab=False))
            self.assertFalse(lab_default("BEING_CHIRPS", in_lab=True))
        with patch.dict(os.environ, {"BEING_CHIRPS": "1"}):
            self.assertTrue(lab_default("BEING_CHIRPS", in_lab=True))
        with patch.dict(os.environ, {"BEING_CHIRPS": "0"}):
            self.assertFalse(lab_default("BEING_CHIRPS", in_lab=False))


class Robustness(unittest.IsolatedAsyncioTestCase):
    async def test_a_malformed_clause_still_ends_the_reply(self):
        voice = make_voice()
        voice.state = "thinking"
        voice.play = AsyncMock()
        voice.listen = AsyncMock()

        @asynccontextmanager
        async def fake_child(*command, **options):
            yield Mock(wait=AsyncMock(return_value=0))

        with patch("builtins.print"), patch("mind.voice.child", fake_child), \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            async with asyncio.TaskGroup() as voice.tasks:
                await voice.handle({"t": "say.request", "text": "Fine.", "last": False})
                await voice.handle({"t": "say.request", "text": None})
        self.assertIn(("say.done", {"final": True, "failed": True}), sent(voice))

    async def test_an_unexpected_reply_error_still_reports_done(self):
        voice = make_voice()
        voice.clauses.put_nowait({"text": "Hi.", "last": True})
        with patch("builtins.print"), \
                patch("mind.voice.tempfile.TemporaryDirectory", side_effect=PermissionError("/run")):
            await asyncio.wait_for(voice.speak(), 3)
        self.assertEqual(sent(voice)[-1], ("say.done", {"final": True, "failed": True}))

    async def test_wake_spotting_resumes_after_a_reply_in_thinking(self):
        voice = make_voice()
        voice.state = "thinking"
        voice.speak_clauses = AsyncMock()
        voice.listen = AsyncMock()
        await voice.speak()
        self.assertIsNotNone(voice.capture)
        await voice.stop_capture()


    async def test_a_malformed_event_does_not_stop_the_organ(self):
        voice = make_voice()
        voice.bus.start = AsyncMock()
        voice.bus.stop = AsyncMock()
        voice.bus.recv = AsyncMock(side_effect=[{"t": "state.changed"}, {"t": "voice.mute", "on": True},
                                                asyncio.CancelledError])
        with patch("builtins.print"), self.assertRaises(asyncio.CancelledError):
            await voice.run()
        self.assertTrue(voice.muted)


if __name__ == "__main__":
    unittest.main()
