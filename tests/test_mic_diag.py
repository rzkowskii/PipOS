"""The diagnostic microphone: a Unix socket that stands in for the microphone (BEING_DIAG_MIC)."""

import asyncio
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import mic_port
from mind.mic_port import DIAG_ENV, DiagnosticMicrophone
from mind.recognition import FRAME_BYTES
from mind.voice import Voice, microphone_channel
from tests.test_voice_feedback import ScriptedRecognizer, real_sleep


class FakeClock:
    """A clock that only moves when the microphone sleeps."""

    def __init__(self):
        self.now = 100.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(round(seconds, 4))
        self.now += seconds
        await real_sleep(0)


def short_socket_dir():
    # Unix socket paths are short (about 104 bytes on macOS): not the long test temporary folder.
    return tempfile.TemporaryDirectory(prefix="pd-", dir="/tmp")


class Connect(unittest.IsolatedAsyncioTestCase):
    async def test_it_waits_for_the_listener_and_says_when_connected(self):
        with short_socket_dir() as folder:
            path = Path(folder) / "mic.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)

            async def listen_later():
                await asyncio.sleep(0.3)
                listener.bind(str(path))
                listener.listen(1)

            later = asyncio.create_task(listen_later())
            with patch("builtins.print") as said:
                microphone = await DiagnosticMicrophone.connect(path, timeout=3, interval=0.05)
            await later
            self.assertIsNotNone(microphone)
            self.addCleanup(microphone.close)
            self.assertIn("diagnostic microphone connected", said.call_args[0][0])
            self.assertEqual(microphone.description, f"the diagnostic microphone {path}")

    async def test_nobody_listening_falls_back_with_a_line(self):
        with short_socket_dir() as folder:
            with patch("builtins.print") as said:
                microphone = await DiagnosticMicrophone.connect(Path(folder) / "none.sock", timeout=0.3,
                                                                interval=0.05)
        self.assertIsNone(microphone)
        self.assertIn("Using the normal microphone", said.call_args[0][0])


class Reading(unittest.IsolatedAsyncioTestCase):
    def pair(self):
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        ours.setblocking(False)
        clock = FakeClock()
        microphone = DiagnosticMicrophone(ours, "/run/being-diag/mic.sock", clock=clock, sleep=clock.sleep)
        self.addCleanup(microphone.close)
        self.addCleanup(theirs.close)
        return microphone, theirs, clock

    async def test_frames_arrive_whole_and_in_order(self):
        microphone, sender, _ = self.pair()
        audio = bytes(range(256)) * 15     # 3840 bytes: four frames in odd pieces
        async with microphone.recording() as stream:
            sender.sendall(audio[:1000])
            first = asyncio.create_task(stream.readexactly(FRAME_BYTES))
            await real_sleep(0.01)
            sender.sendall(audio[1000:])
            frames = [await asyncio.wait_for(first, 1)]
            for _ in range(3):
                frames.append(await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 1))
        self.assertEqual(b"".join(frames), audio)

    async def test_audio_written_while_capture_was_closed_is_kept(self):
        microphone, sender, _ = self.pair()
        sender.sendall(b"\x05\x00" * (FRAME_BYTES // 2))
        async with microphone.recording() as stream:
            self.assertEqual(await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 1),
                             b"\x05\x00" * (FRAME_BYTES // 2))

    async def test_after_the_sender_closes_it_hears_silence_and_says_so(self):
        microphone, sender, _ = self.pair()
        sender.sendall(b"\x07" * 100)
        sender.close()
        with patch("builtins.print") as said:
            async with microphone.recording() as stream:
                first = await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 1)
                second = await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 1)
        self.assertEqual(first, b"\x07" * 100 + bytes(FRAME_BYTES - 100))
        self.assertEqual(second, bytes(FRAME_BYTES))
        self.assertEqual(said.call_count, 1)
        self.assertIn("hearing silence until the voice restarts", said.call_args[0][0])

    async def test_it_never_runs_ahead_of_real_time(self):
        microphone, sender, clock = self.pair()
        # 1.2 s of audio at once: more than a socket buffer holds, so from a thread.
        writer = threading.Thread(target=sender.sendall, args=(bytes(FRAME_BYTES * 40),))
        writer.start()
        self.addCleanup(writer.join, 5)
        async with microphone.recording() as stream:
            for _ in range(40):
                await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 2)
        # A quarter second may come at once; the rest at 30 ms a frame.
        self.assertAlmostEqual(clock.now - 100.0, 1.2 - mic_port.DIAG_LEAD_SECONDS - 0.03, delta=0.031)
        self.assertTrue(all(0 < wait <= 0.0301 for wait in clock.slept))

    async def test_silence_after_the_end_is_paced_too(self):
        microphone, sender, clock = self.pair()
        sender.close()
        with patch("builtins.print"):
            async with microphone.recording() as stream:
                for _ in range(100):
                    self.assertEqual(await stream.readexactly(FRAME_BYTES), bytes(FRAME_BYTES))
        self.assertGreater(clock.now - 100.0, 2.5, "3 s of silence take about 3 s, not a busy loop")

    async def test_a_cancelled_read_loses_nothing(self):
        microphone, sender, _ = self.pair()
        async with microphone.recording() as stream:
            sender.sendall(b"\x01" * 500)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 0.05)
            sender.sendall(b"\x02" * (FRAME_BYTES - 500))
            self.assertEqual(await asyncio.wait_for(stream.readexactly(FRAME_BYTES), 1),
                             b"\x01" * 500 + b"\x02" * (FRAME_BYTES - 500))


class ThroughTheVoice(unittest.IsolatedAsyncioTestCase):
    async def test_the_voice_hears_the_socket_through_its_gain_and_recognizer(self):
        ours, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        ours.setblocking(False)
        self.addCleanup(sender.close)
        microphone = DiagnosticMicrophone(ours, "/run/being-diag/mic.sock")
        self.addCleanup(microphone.close)
        recognizer = ScriptedRecognizer()
        voice = Voice(recognizer, "Richard", ["arecord"], "plughw:0,0", microphone)
        voice.work_dir = None
        voice.bus.connected.set()
        voice.lid_open, voice.state, voice.muted = True, "idle", False
        sent = []

        async def send(topic, **fields):
            sent.append(topic)

        voice.bus.send = send
        frame = b"\x10\x00" * (FRAME_BYTES // 2)
        sender.sendall(frame * 3)
        self.assertIsNone(voice.channel, "the socket is mono")
        self.assertEqual(voice.capture_device, "the diagnostic microphone /run/being-diag/mic.sock")
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                async with asyncio.timeout(2):
                    while len(recognizer.fed) < 3:
                        await real_sleep(0.005)
                voice.disarm()
        self.assertEqual([fed for fed, _ in recognizer.fed[:3]], [frame] * 3)
        self.assertTrue(any("capture open on the diagnostic microphone" in line for line in logs.output))

    def test_the_diagnostic_microphone_always_mixes(self):
        with tempfile.TemporaryDirectory() as folder:
            line = Path(folder) / "cmdline"
            line.write_text("being.mic_channel=auto")
            with self.assertLogs("being.voice", "INFO") as logs:
                self.assertEqual(microphone_channel({}, line, diagnostic=True), "mix")
        self.assertIn("does not apply to the diagnostic microphone", logs.output[0])

    def test_the_environment_name_is_the_contract_one(self):
        self.assertEqual(DIAG_ENV, "BEING_DIAG_MIC")


if __name__ == "__main__":
    unittest.main()
