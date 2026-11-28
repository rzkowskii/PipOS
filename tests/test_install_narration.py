"""Host contracts for optional fixed installation audio."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from mind import install_status
from tests.test_voice import TestVoice


class InstallationAudio(unittest.IsolatedAsyncioTestCase):
    async def voice(self):
        voice = TestVoice()
        voice.try_boot = True
        voice.synthesize_command = AsyncMock()
        voice.play_chirp = AsyncMock()
        voice.bus.send = AsyncMock()
        self.addAsyncCleanup(voice.stop_capture)
        self.addAsyncCleanup(voice.stop_install_status)
        await voice.event("state.changed", state="idle", muted=False)
        await voice.event("body.lid", seq=8, closed=False)
        await asyncio.wait_for(voice.recording_started.wait(), 1)
        return voice

    async def status(self, voice, **changes):
        event = {"stage": "write", "issued": time.monotonic(), "lid_seq": 8, **changes}
        await voice.event(install_status.TOPIC, **event)

    async def test_local_fixed_speech_holds_the_microphone_without_starting_a_turn(self):
        voice = await self.voice()
        playing = asyncio.Event()
        finish = asyncio.Event()

        async def play(_audio):
            self.assertIsNone(voice.capture)
            self.assertTrue(voice.recording_stopped.is_set())
            playing.set()
            await finish.wait()

        voice.play_chirp.side_effect = play
        await self.status(voice, text="Private target and profile text")
        await asyncio.wait_for(playing.wait(), 1)
        command, engine = voice.synthesize_command.call_args.args
        self.assertEqual(engine, "flite")
        self.assertEqual(command[:3], ["flite", "-voice", "slt"])
        self.assertEqual(command[command.index("-t") + 1], install_status.LINES["write"])
        self.assertNotIn("Private", " ".join(command))
        self.assertEqual(voice.state, "idle")
        voice.bus.send.assert_not_awaited()
        voice.recording_started.clear()
        finish.set()
        await asyncio.wait_for(voice.install_audio, 1)
        await asyncio.wait_for(voice.recording_started.wait(), 1)

    async def test_rejects_untrusted_copy_stale_stages_quiet_modes_and_unknown_lids(self):
        voice = await self.voice()
        await self.status(voice, stage="private backup label")
        await self.status(voice, stage=["write"])
        await self.status(voice, issued=time.monotonic() - install_status.FRESH_FOR - 1)
        await self.status(voice, issued=time.monotonic() + 20)
        await self.status(voice, lid_seq=7)
        self.assertIsNone(voice.install_audio)

        voice.try_boot = False
        await self.status(voice)
        self.assertIsNone(voice.install_audio)
        voice.try_boot = True
        with tempfile.TemporaryDirectory() as work:
            voice.settings_dir = Path(work)
            (voice.settings_dir / "speaker-muted").touch()
            await self.status(voice)
            self.assertIsNone(voice.install_audio)
        voice.settings_dir = None
        voice.volume = 0
        await self.status(voice)
        self.assertIsNone(voice.install_audio)
        voice.volume = 70
        await voice.event("voice.mute", on=True)
        await self.status(voice)
        self.assertIsNone(voice.install_audio)
        await voice.event("voice.mute", on=False)
        await voice.event("body.lid", closed=None, seq=9, show_face=True)
        await self.status(voice, lid_seq=9)
        self.assertIsNone(voice.install_audio)
        voice.synthesize_command.assert_not_awaited()

    async def test_active_line_stops_on_lid_mutes_and_conversation(self):
        cases = [
            ("body.lid", {"closed": True, "seq": 9}, False),
            ("voice.mute", {"on": True}, False),
            ("voice.volume", {"level": 0, "persist": False}, True),
            ("state.changed", {"state": "attending", "muted": False, "prev": "idle"}, True),
        ]
        for topic, fields, capture in cases:
            with self.subTest(topic=topic):
                voice = await self.voice()
                playing = asyncio.Event()
                stopped = asyncio.Event()

                async def play(_audio):
                    playing.set()
                    try:
                        await asyncio.Future()
                    finally:
                        stopped.set()

                voice.play_chirp.side_effect = play
                await self.status(voice)
                await asyncio.wait_for(playing.wait(), 1)
                await voice.event(topic, **fields)
                self.assertTrue(stopped.is_set())
                self.assertIsNone(voice.install_audio)
                self.assertEqual(voice.capture is not None, capture)
                await voice.stop_capture()

    async def test_bus_loss_stops_audio_and_reconnect_does_not_replay_it(self):
        voice = await self.voice()
        playing = asyncio.Event()

        async def play(_audio):
            playing.set()
            await asyncio.Future()

        voice.play_chirp.side_effect = play
        await self.status(voice, stage="complete")
        await asyncio.wait_for(playing.wait(), 1)
        voice.bus.connected.clear()
        voice.connection_lost()
        outcome, = await asyncio.wait_for(asyncio.gather(voice.install_audio, return_exceptions=True), 1)
        self.assertIsInstance(outcome, asyncio.CancelledError)
        self.assertIsNone(voice.capture)
        voice.bus.connected.set()
        await voice.event("state.changed", state="idle", muted=False)
        await voice.event("body.lid", closed=False, seq=8)
        voice.play_chirp.assert_awaited_once()
