"""Lid privacy and speech settings with stand-in audio streams."""

import asyncio
import struct
import tempfile
import unittest
import wave
from collections import deque
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import AsyncMock, Mock, call, patch

from mind.recognition import Recognizer, WAKE_PREROLL_FRAMES, WAKE_REPLAY_FRAMES
from mind.tts import available_voices, synthesis_command
from mind.voice import Voice, wake_recognizer


class TestVoice(Voice):
    def __init__(self):
        super().__init__(Mock(), "Richard", [], "")
        self.recording_started = asyncio.Event()
        self.recording_stopped = asyncio.Event()
        self.playback_started = asyncio.Event()
        self.playback_stopped = asyncio.Event()
        self.bus.connected.set()

    @asynccontextmanager
    async def recording(self):
        self.recording_started.set()
        try:
            yield self
        finally:
            self.recording_stopped.set()

    async def readexactly(self, size):
        await asyncio.Future()

    async def speak(self):
        self.playback_started.set()
        try:
            await asyncio.Future()
        finally:
            self.playback_stopped.set()

    async def event(self, topic, **fields):
        event = {"t": topic, **fields}
        self.on_event_received(event)
        await self.handle(event)


class LidPrivacy(unittest.IsolatedAsyncioTestCase):
    async def test_capture_requires_known_open_lid_and_keeps_mute(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("state.changed", state="idle", muted=False)
            self.assertIsNone(voice.capture)
            await voice.event("body.lid", closed=False)
            await asyncio.wait_for(voice.recording_started.wait(), 1)

            await voice.event("body.lid", closed=True)
            await voice.event("state.changed", state="asleep", muted=False)
            self.assertTrue(voice.recording_stopped.is_set())
            self.assertIsNone(voice.capture)
            await voice.event("voice.mute", on=True)
            await voice.event("body.lid", closed=False)
            self.assertIsNone(voice.capture)

            voice.recording_started.clear()
            await voice.event("voice.mute", on=False)
            await asyncio.wait_for(voice.recording_started.wait(), 1)
            voice.connection_lost()
            await voice.stop_capture()
            await voice.event("state.changed", state="idle", muted=False)
            self.assertIsNone(voice.capture)

    async def test_typed_input_keeps_the_microphone_shut_without_touching_the_mute(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("state.changed", state="idle", muted=False)
            await voice.event("body.lid", closed=False)
            await asyncio.wait_for(voice.recording_started.wait(), 1)
            await voice.event("chat.mode", mode="typed_voice", input="typed", reply="voice")
            self.assertTrue(voice.recording_stopped.is_set())
            self.assertIsNone(voice.capture)
            self.assertFalse(voice.muted, "its own gate, not the saved microphone mute")
            # Still shut after another state change, and while the mode stays typed.
            await voice.event("state.changed", state="idle", muted=False)
            self.assertIsNone(voice.capture)
            voice.recording_started.clear()
            await voice.event("chat.mode", mode="spoken", input="voice", reply="voice")
            await asyncio.wait_for(voice.recording_started.wait(), 1)
            voice.connection_lost()
            await voice.stop_capture()

    async def test_the_mic_mute_stays_on_when_typing_ends(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("state.changed", state="idle", muted=True)
            await voice.event("body.lid", closed=False)
            await voice.event("chat.mode", mode="typed_text", input="typed", reply="text")
            await voice.event("chat.mode", mode="spoken", input="voice", reply="voice")
            self.assertIsNone(voice.capture, "the owner's own mute still holds")

    async def test_a_guest_is_never_left_deaf_by_the_owners_typed_mode(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("state.changed", state="idle", muted=False)
            await voice.event("body.lid", closed=False)
            await asyncio.wait_for(voice.recording_started.wait(), 1)
            voice.guest = True
            await voice.event("chat.mode", mode="typed_voice", input="typed", reply="voice")
            self.assertFalse(voice.typing)
            voice.connection_lost()
            await voice.stop_capture()

    async def test_a_guest_arriving_or_leaving_reopens_or_shuts_the_microphone_while_typing(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("app.privacy", guest=False)
            await voice.event("state.changed", state="idle", muted=False)
            await voice.event("body.lid", closed=False)
            await voice.event("chat.mode", mode="typed_voice", input="typed", reply="voice")
            self.assertIsNone(voice.capture)
            voice.recording_started.clear()
            await voice.event("app.privacy", guest=True)       # a guest arrives: spoken to, heard
            await asyncio.wait_for(voice.recording_started.wait(), 1)
            await voice.event("app.privacy", guest=False)      # leaves: the owner's typed mode shuts it again
            self.assertIsNone(voice.capture)
            voice.connection_lost()
            await voice.stop_capture()

    async def test_a_reconnect_forgets_the_typed_mode_until_it_is_replayed(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("chat.mode", mode="typed_voice", input="typed", reply="voice")
            self.assertTrue(voice.typed_input)
            voice.connection_lost()
            self.assertFalse(voice.typed_input)
            await voice.stop_capture()

    async def test_a_restarted_voice_starts_spoken(self):
        self.assertFalse(TestVoice().typed_input)

    async def test_close_stops_playback_and_drops_pending_clauses(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("body.lid", closed=False)
            await voice.event("state.changed", state="speaking", muted=False)
            await voice.event("say.request", text="A queued clause", last=False)
            await asyncio.wait_for(voice.playback_started.wait(), 1)
            await voice.event("body.lid", closed=True)
            self.assertTrue(voice.playback_stopped.is_set())
            self.assertTrue(voice.clauses.empty())
            await voice.event("say.request", text="A stale event", last=False)
            self.assertTrue(voice.clauses.empty())
            self.assertIsNone(voice.reply)

    async def test_buffered_open_before_close_cannot_start_capture(self):
        voice = TestVoice()
        async with asyncio.TaskGroup() as voice.tasks:
            await voice.event("state.changed", state="idle", muted=False)
            opening = {"t": "body.lid", "closed": False}
            closure = {"t": "body.lid", "closed": True}
            voice.on_event_received(opening)
            voice.on_event_received(closure)
            await voice.handle(opening)
            self.assertIsNone(voice.capture)
            await voice.handle(closure)


class WakeAudio(unittest.TestCase):
    def test_quiet_audio_is_replayed_in_order_without_an_onset_spike(self):
        recognizer = Recognizer.__new__(Recognizer)
        recognizer.lib = Mock()
        recognizer.lib.ps_activate_search.return_value = 0
        recognizer.lib.ps_start_utt.return_value = 0
        recognizer.lib.ps_endpointer_speech_start.return_value = 40 * 0.03
        recognizer.decoder = Mock()
        recognizer.endpoint = None
        recognizer.mode = None
        recognizer.wake_preroll = deque(maxlen=WAKE_PREROLL_FRAMES + 1)
        decoded, tracked = [], []

        def decode(frame):
            decoded.append(frame)
            recognizer.frames += 1

        recognizer.decode = decode
        recognizer.track_wake = lambda: tracked.append(decoded[-1])
        for frame in range(1, 41):
            recognizer.capture_frames = frame
            self.assertEqual(recognizer.spot_keyword(frame, False, False), [])
        self.assertEqual(decoded, [])

        for frame in range(41, 47):
            recognizer.capture_frames = frame
            previous = len(decoded)
            recognizer.spot_keyword(frame, True, frame > 41)
            self.assertLessEqual(len(decoded) - previous, WAKE_REPLAY_FRAMES)

        self.assertEqual(decoded, list(range(41 - WAKE_PREROLL_FRAMES, 47)))
        self.assertEqual(tracked, list(range(41, 47)))
        self.assertAlmostEqual(recognizer.search_start, (40 - WAKE_PREROLL_FRAMES) * 0.03)

    def test_stopping_capture_drops_buffered_audio(self):
        recognizer = Recognizer.__new__(Recognizer)
        recognizer.mode = recognizer.endpoint = None
        recognizer.wake_preroll = deque([b"buffered PCM"])
        recognizer.stop_capture()
        self.assertFalse(recognizer.wake_preroll)


class SpeechSettings(unittest.IsolatedAsyncioTestCase):
    def test_missing_wake_phrase_uses_factory_recognizer(self):
        standby = Mock()
        with patch("mind.voice.Recognizer", side_effect=[ValueError("missing phrase"), standby]) as recognizer, \
                patch("mind.voice.factory_profile", return_value=Mock(wake_word="Hey Pip")), \
                patch("builtins.print"):
            self.assertIs(wake_recognizer("Hey Abc"), standby)
        self.assertEqual(recognizer.call_args_list, [call("Hey Abc"), call("Hey Pip")])

    def test_failed_recognizer_model_keeps_its_error(self):
        with patch("mind.voice.Recognizer", side_effect=RuntimeError("missing model")):
            with self.assertRaisesRegex(RuntimeError, "missing model"):
                wake_recognizer("Hey Pip")

    def test_catalog_contains_only_available_supported_voices(self):
        with patch("mind.tts.subprocess.run", return_value=Mock(stdout="Voices available: rms awb kal")):
            self.assertEqual(available_voices(), {"rms", "awb"})
        with patch("mind.tts.subprocess.run", side_effect=TimeoutExpired("flite", 2)):
            self.assertIsNone(available_voices())

    def test_inventory_failure_keeps_a_known_voice(self):
        from mind.voice import resolve_voice

        with patch("mind.voice.available_voices", return_value=None), patch("builtins.print"):
            self.assertEqual(resolve_voice("awb"), "awb")
            self.assertEqual(resolve_voice("future-voice"), "slt")
        with patch("mind.voice.available_voices", return_value={"slt"}), patch("builtins.print"):
            self.assertEqual(resolve_voice("awb"), "slt")

    def test_rate_keeps_normal_speech_and_tracks_voice_default(self):
        self.assertEqual(synthesis_command("slt", 1.0, "Hello.", "hello.wav"),
                         ["flite", "-voice", "slt", "-t", "Hello.", "-o", "hello.wav"])
        self.assertIn("duration_stretch=0.956522", synthesis_command("kal16", 1.15, "Hello.", "hello.wav"))

    async def test_reply_uses_selected_voice_and_rate(self):
        voice = Voice(Mock(), "Richard", [], "", voice="awb", rate=0.85)
        voice.play = AsyncMock()
        voice.send_audio = AsyncMock()
        voice.clauses.put_nowait({"text": "Hello.", "last": True})
        commands = []

        @asynccontextmanager
        async def fake_child(*command):
            commands.append(command)
            yield Mock(wait=AsyncMock(return_value=0))

        with patch("mind.voice.child", fake_child), \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await voice.speak()

        self.assertEqual(commands, [("flite", "-voice", "awb", "--setf", "duration_stretch=1.17647",
                                     "-t", "Hello.", "-o", "/unused/clause.wav")])
        voice.play.assert_awaited_once_with(Path("/unused/clause.wav"), first=True)
        voice.send_audio.assert_any_await("say.done", final=True)


class PlaybackCadence(unittest.IsolatedAsyncioTestCase):
    async def test_clause_cue_arrives_once_on_audible_playback(self):
        for label, volume, sample, expected in (
            ("audible", 60, 1200, [None, "joy", None]),
            ("silent", 60, 0, [None, None, None]),
            ("muted", 0, 1200, [None, None, None]),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                voice = Voice(Mock(), "Richard", [], "")
                voice.volume = volume
                process = Mock(returncode=None, wait=AsyncMock(return_value=0))
                levels = []

                async def send(topic, **fields):
                    if topic == "say.level":
                        levels.append(fields)
                        if len(levels) == 3:
                            process.returncode = 0

                @asynccontextmanager
                async def fake_child(*_command):
                    yield process

                voice.send_audio = AsyncMock(side_effect=send)
                audio = Path(tmp) / "clause.wav"
                with wave.open(str(audio), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", ""))
                    wav.writeframes(bytes(640 * 2) + struct.pack("<h", sample) * (640 * 2))

                with patch("mind.voice.child", fake_child):
                    await voice.play_once(audio, first=True, cue="joy")
                self.assertEqual(voice.send_audio.await_args_list[0], call("say.started"))
                self.assertEqual(levels[0]["v"], 0.0)
                self.assertEqual([fields.get("face_cue") for fields in levels], expected)

    async def test_player_that_rejects_playback_cannot_start_an_expression(self):
        voice = Voice(Mock(), "Richard", [], "")
        voice.volume = 60
        voice.send_audio = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "clause.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", ""))
                wav.writeframes(struct.pack("<h", 1200) * 1600)

            with patch("mind.voice.playback_command", return_value=["/bin/sh", "-c", "exit 19"]):
                with self.assertRaisesRegex(RuntimeError, "aplay exit status 19"):
                    await voice.play_once(audio, first=True, cue="joy")
        voice.send_audio.assert_not_awaited()

    async def test_pause_trial_only_requests_segments_for_the_first_played_clause(self):
        voice = Voice(Mock(), "Richard", [], "", shorten_first_pause=True)
        voice.play = AsyncMock()
        voice.send_audio = AsyncMock()
        for text, last in (("", False), ("First.", False), ("Second.", False), ("", True)):
            voice.clauses.put_nowait({"text": text, "last": last})
        commands = []

        @asynccontextmanager
        async def fake_child(*command, **options):
            commands.append((command, options))
            yield Mock(wait=AsyncMock(return_value=0), communicate=AsyncMock(return_value=(b"segments", None)))

        with patch("mind.voice.child", fake_child), \
                patch("mind.voice.shorten_leading_pause", return_value=False) as shorten, \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 2)

        self.assertEqual([command[-3] for command, _ in commands], ["First.", "Second."])
        self.assertIn("-psdur", commands[0][0])
        self.assertEqual(commands[0][1], {"stdout": asyncio.subprocess.PIPE})
        self.assertNotIn("-psdur", commands[1][0])
        self.assertEqual(commands[1][1], {})
        shorten.assert_called_once_with(Path("/unused/clause.wav"), b"segments")
        self.assertEqual([item.kwargs["first"] for item in voice.play.await_args_list], [True, False])

    async def test_pause_trial_preserves_stock_synthesis_for_kal16_and_long_clauses(self):
        for name, text in (("kal16", "First."), ("slt", "A" * 513)):
            with self.subTest(voice=name, length=len(text)):
                voice = Voice(Mock(), "Richard", [], "", voice=name, shorten_first_pause=True)
                voice.clauses.put_nowait({"text": text, "last": True})

                @asynccontextmanager
                async def fake_child(*command, **options):
                    self.assertNotIn("-psdur", command)
                    self.assertEqual(options, {})
                    yield Mock(wait=AsyncMock(return_value=0))

                with patch("mind.voice.child", fake_child):
                    self.assertEqual(await voice.prepare_clause(Path("/unused/clause.wav"), first=True),
                                     (True, True, None, None, None))

    async def test_final_announcement_is_echoed_only_after_playback(self):
        @asynccontextmanager
        async def fake_child(*command):
            yield Mock(wait=AsyncMock(return_value=0))

        for clauses, expected in (
            ([{"text": "Reminder. Call Sam.", "last": False},
              {"text": "", "last": True, "announcement": "due-1"}], {"announcement": "due-1"}),
            ([{"text": "Reminder. Call Sam.", "last": False, "announcement": "early"},
              {"text": "", "last": True}], {}),
            ([{"text": "", "last": True, "announcement": "unplayed"}], {}),
        ):
            with self.subTest(clauses=clauses):
                voice = Voice(Mock(), "Richard", [], "")
                voice.play = AsyncMock()
                voice.send_audio = AsyncMock()
                for clause in clauses:
                    voice.clauses.put_nowait(clause)
                with patch("mind.voice.child", fake_child), \
                        patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
                    await asyncio.wait_for(voice.speak(), 2)
                self.assertEqual(voice.play.await_count, int(bool(clauses[0]["text"])))
                self.assertEqual([event for event in voice.send_audio.await_args_list
                                  if event.args[0] == "say.done"], [call("say.done", final=True, **expected)])

    async def test_prepares_only_the_next_clause_during_playback(self):
        voice = Voice(Mock(), "Richard", [], "")
        voice.send_audio = AsyncMock()
        for text in ("First.", "Second.", "Third.", ""):
            voice.clauses.put_nowait({"text": text, "last": not text})
        second_ready = asyncio.Event()
        synthesized = []
        played = []

        @asynccontextmanager
        async def fake_child(*command):
            text, audio = command[-3], command[-1]
            synthesized.append((text, audio))
            yield Mock(wait=AsyncMock(return_value=0))
            if text == "Second.":
                second_ready.set()

        async def play(audio, first):
            played.append((audio.name, first))
            if first:
                self.assertEqual(synthesized, [("First.", "/unused/clause.wav")])
                await asyncio.wait_for(second_ready.wait(), 1)
                self.assertEqual(synthesized, [("First.", "/unused/clause.wav"), ("Second.", "/unused/next.wav")])

        with patch("mind.voice.child", fake_child), patch.object(voice, "play", play), \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 2)

        self.assertEqual(played, [("clause.wav", True), ("next.wav", False), ("clause.wav", False)])
        voice.send_audio.assert_any_await("say.done", final=True)

    async def test_disarming_cancels_playback_and_inflight_synthesis(self):
        voice = Voice(Mock(), "Richard", [], "")
        voice.send_audio = AsyncMock()
        for text in ("First.", "Second.", "Third."):
            voice.clauses.put_nowait({"text": text, "last": False})
        synthesis_started = asyncio.Event()
        halted = []

        async def synthesize_second():
            synthesis_started.set()
            await asyncio.Future()

        @asynccontextmanager
        async def fake_child(*command):
            if command[-3] == "First.":
                yield Mock(wait=AsyncMock(return_value=0))
            else:
                try:
                    yield Mock(wait=synthesize_second)
                finally:
                    halted.append("synthesis")

        async def play(audio, first):
            try:
                await asyncio.Future()
            finally:
                halted.append("playback")

        with patch("mind.voice.child", fake_child), patch.object(voice, "play", play), \
                patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            voice.reply = asyncio.create_task(voice.speak())
            try:
                await asyncio.wait_for(synthesis_started.wait(), 1)
            finally:
                voice.disarm()
                await voice.stop_reply()

        self.assertEqual(halted, ["playback", "synthesis"])
        self.assertIsNone(voice.reply)
        self.assertTrue(voice.clauses.empty())
        self.assertNotIn(call("say.done", final=True), voice.send_audio.await_args_list)


if __name__ == "__main__":
    unittest.main()
