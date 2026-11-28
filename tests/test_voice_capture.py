"""Capture apart from decoding: a slow decode never makes the microphone overrun."""

import asyncio
import os
import struct
import threading
import time
import unittest
from contextlib import asynccontextmanager
from unittest.mock import Mock, patch

from mind import mic_port
from mind.mic_port import CaptureQueue, DiagnosticMicrophone, PipeReader, pump
from mind.recognition import FRAME_BYTES, FRAME_SAMPLES, MicLevel
from mind.voice import CAPTURE_BUFFER_US, Voice, capture_command

real_sleep = asyncio.sleep


def frame(index):
    """A frame that says which it is in its first sample."""
    return struct.pack("<h", index) + bytes(FRAME_BYTES - 2)


def index_of(data):
    return struct.unpack_from("<h", data)[0]


class Recognizer:
    """Records what it was fed; one frame can hold the event loop, as a long decode does."""

    in_speech = False

    def __init__(self, block_on=None, seconds=0.0):
        self.fed = []
        self.block_on = block_on
        self.seconds = seconds
        self.captures = 0
        self.unblocked_at = None

    def start_capture(self):
        self.captures += 1

    def stop_capture(self):
        pass

    def feed(self, data, attending, oneshot=True):
        self.fed.append(index_of(data))
        if index_of(data) == self.block_on:
            time.sleep(self.seconds)     # blocks the loop, as PocketSphinx's end of an utterance does
            self.unblocked_at = time.monotonic()
        return []


def voice_with(recognizer):
    # No automatic lift: the frames reach the recognizer as they were written.
    voice = Voice(recognizer, "Richard", capture_command("plughw:0,0"), "plughw:0,0", mic=MicLevel(1.0, auto=False))
    voice.work_dir = None
    voice.bus.connected.set()
    voice.bus.send = Mock(side_effect=lambda *args, **kwargs: asyncio.sleep(0))
    voice.lid_open, voice.state, voice.muted = True, "idle", False
    return voice


class Queue(unittest.IsolatedAsyncioTestCase):
    async def test_frames_come_out_in_order_and_the_end_after_them(self):
        frames = CaptureQueue(10)
        for index in range(3):
            frames.put(frame(index))
        frames.finish(asyncio.IncompleteReadError(b"", FRAME_BYTES))
        self.assertEqual([index_of(await frames.get(1)) for _ in range(3)], [0, 1, 2])
        with self.assertRaises(asyncio.IncompleteReadError):
            await frames.get(1)

    async def test_nothing_yet_is_none_after_the_timeout(self):
        self.assertIsNone(await CaptureQueue(10).get(0.01))

    async def test_a_full_queue_drops_its_oldest_frames_and_counts_them(self):
        frames = CaptureQueue(4)
        for index in range(10):
            frames.put(frame(index))
        self.assertEqual(len(frames), 4)
        self.assertEqual((frames.received, frames.dropped, frames.deepest), (10, 6, 4))
        self.assertEqual(frames.take_dropped(), 6)
        self.assertEqual(frames.take_dropped(), 0)
        self.assertEqual([index_of(await frames.get(1)) for _ in range(4)], [6, 7, 8, 9])

    async def test_a_frame_from_another_thread_wakes_the_reader(self):
        frames = CaptureQueue(10)
        waiting = asyncio.ensure_future(frames.get(2))
        await real_sleep(0.01)
        threading.Thread(target=frames.put_threadsafe, args=([frame(7)],)).start()
        self.assertEqual(index_of(await asyncio.wait_for(waiting, 2)), 7)

    async def test_the_end_from_another_thread_wakes_the_reader(self):
        frames = CaptureQueue(10)
        waiting = asyncio.ensure_future(frames.get(2))
        await real_sleep(0.01)
        threading.Thread(target=frames.finish, args=(OSError("gone"),)).start()
        with self.assertRaisesRegex(OSError, "gone"):
            await asyncio.wait_for(waiting, 2)

    async def test_a_lossless_source_waits_for_room_instead(self):
        frames = CaptureQueue(2)
        await frames.put_wait(frame(0))
        await frames.put_wait(frame(1))
        third = asyncio.ensure_future(frames.put_wait(frame(2)))
        await real_sleep(0.01)
        self.assertFalse(third.done(), "a full queue holds the sender")
        self.assertEqual(index_of(await frames.get(1)), 0)
        await asyncio.wait_for(third, 1)
        self.assertEqual(frames.dropped, 0)
        self.assertEqual([index_of(await frames.get(1)) for _ in range(2)], [1, 2])

    async def test_the_pump_reads_a_stream_and_passes_on_its_end(self):
        class Stream:
            def __init__(self):
                self.left = 3

            async def readexactly(self, size):
                if not self.left:
                    raise asyncio.IncompleteReadError(b"", size)
                self.left -= 1
                return frame(3 - self.left)

        frames = CaptureQueue(10)
        await asyncio.wait_for(pump(Stream(), frames, FRAME_BYTES), 1)
        self.assertEqual([index_of(await frames.get(1)) for _ in range(3)], [1, 2, 3])
        with self.assertRaises(asyncio.IncompleteReadError):
            await frames.get(1)

    def test_the_diagnostic_microphone_is_lossless(self):
        self.assertIs(DiagnosticMicrophone.lossless, True)


class Stamps(unittest.IsolatedAsyncioTestCase):
    async def test_each_frame_carries_the_time_it_was_captured(self):
        clock = [10.0]
        frames = CaptureQueue(10, clock=lambda: clock[0], frame_seconds=0.03)
        frames.put(frame(0))
        clock[0] = 11.0
        # Three frames that arrive together were captured one after another, the last just now.
        await asyncio.to_thread(frames.put_threadsafe, [frame(1), frame(2), frame(3)])
        clock[0] = 20.0
        stamps = []
        for _ in range(4):
            await frames.get(1)
            stamps.append(round(frames.taken_at, 3))
        self.assertEqual(stamps, [10.0, 10.94, 10.97, 11.0])

    async def test_a_lossless_frame_held_at_cancellation_is_kept_and_drained(self):
        class Stream:
            def __init__(self):
                self.sent = 0

            async def readexactly(self, size):
                self.sent += 1
                return frame(self.sent - 1)

        frames = CaptureQueue(2)
        task = asyncio.ensure_future(pump(Stream(), frames, FRAME_BYTES, lossless=True))
        await real_sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Two queued, and the third read while it waited for room.
        self.assertEqual([index_of(data) for data in frames.drain()], [0, 1, 2])
        self.assertEqual(len(frames), 0)

    def test_the_diagnostic_microphone_takes_audio_back_in_front(self):
        microphone = DiagnosticMicrophone(Mock(), "/run/test.sock")
        microphone.audio.extend(b"later")
        microphone.unread(b"first ")
        self.assertEqual(bytes(microphone.audio), b"first later")


class OwnSounds(unittest.TestCase):
    def test_frames_are_silenced_by_when_they_were_captured(self):
        clock = [100.0]
        voice = voice_with(Recognizer())
        voice._clock = lambda: clock[0]
        window = voice.deafen(0.5)              # a chirp from 100.0
        clock[0] = 100.2
        # Captured before the chirp, decoded while it plays: the owner's words, heard.
        self.assertFalse(voice.silenced_at(99.9))
        self.assertTrue(voice.silenced_at(100.1))
        window[1] = clock[0] + 0.12             # it ended early
        clock[0] = 103.0
        # Captured while it played, decoded long after: still its own sound.
        self.assertTrue(voice.silenced_at(100.25))
        self.assertFalse(voice.silenced_at(100.4))
        self.assertTrue(voice.condition(frame(7), voice.silenced_at(100.25))[0] == bytes(FRAME_BYTES))
        # Without a capture time, as of now.
        self.assertFalse(voice.silenced_at(None))

    def test_old_windows_are_forgotten(self):
        clock = [0.0]
        voice = voice_with(Recognizer())
        voice._clock = lambda: clock[0]
        voice.deafen(0.1)
        clock[0] = 1000.0
        voice.deafen(0.1)
        self.assertEqual(len(voice.deaf_windows), 1)


class Pipe(unittest.IsolatedAsyncioTestCase):
    async def test_the_reader_splits_the_pipe_into_frames_and_closes_it_at_the_end(self):
        read_end, write_end = os.pipe()
        reader = PipeReader(read_end)
        frames = CaptureQueue(100)
        reader.start(frames, FRAME_BYTES)
        data = b"".join(frame(index) for index in range(5))
        # Odd-sized writes: frames are cut from the stream, not from the writes.
        for start in range(0, len(data), 700):
            os.write(write_end, data[start:start + 700])
        os.write(write_end, b"\x01\x02\x03")
        os.close(write_end)
        got = [index_of(await frames.get(2)) for _ in range(5)]
        self.assertEqual(got, [0, 1, 2, 3, 4])
        with self.assertRaises(asyncio.IncompleteReadError) as caught:
            await frames.get(2)
        self.assertEqual(caught.exception.partial, b"\x01\x02\x03")
        reader.thread.join(2)
        with self.assertRaises(OSError):
            os.fstat(read_end)

    def test_an_unused_reader_closes_its_pipe(self):
        read_end, write_end = os.pipe()
        os.close(write_end)
        PipeReader(read_end).close()
        with self.assertRaises(OSError):
            os.fstat(read_end)


class ThroughTheVoice(unittest.IsolatedAsyncioTestCase):
    async def run_arecord(self, recognizer, count, *, rate_hz=None):
        """The voice's real arecord path, with a stand-in arecord writing `count` frames."""
        voice = voice_with(recognizer)
        written = []

        @asynccontextmanager
        async def child(*command, **options):
            out = os.dup(options["stdout"])

            def arecord():
                try:
                    for index in range(count):
                        os.write(out, frame(index))
                        written.append(time.monotonic())
                        if rate_hz:
                            time.sleep(1 / rate_hz)
                finally:
                    os.close(out)

            writer = threading.Thread(target=arecord)
            writer.start()
            try:
                yield Mock(stdout=None, returncode=None)
            finally:
                writer.join(5)

        with patch("mind.voice.child", child), self.assertLogs("being.voice", "INFO") as logs:
            task = asyncio.ensure_future(voice.capture_stream())
            async with asyncio.timeout(10):
                while not recognizer.fed or recognizer.fed[-1] != count - 1:
                    await real_sleep(0.005)
            voice.lid_open = False
            await asyncio.gather(task, return_exceptions=True)
        return voice, written, logs.output

    async def test_a_decode_that_holds_the_loop_loses_no_audio(self):
        # 300 frames (9 s, 288 KB) arrive while the first one is decoded for
        # 1.5 s: far more than a pipe holds. Every one is heard, in order.
        recognizer = Recognizer(block_on=0, seconds=1.5)
        _, written, logs = await self.run_arecord(recognizer, 300)
        self.assertEqual(recognizer.fed, list(range(300)))
        self.assertEqual(len(written), 300)
        self.assertLess(written[-1], recognizer.unblocked_at, "arecord wrote everything while the decode held the loop")
        self.assertFalse(any("microphone audio dropped" in line for line in logs), logs)
        self.assertTrue(any("decoding fell up to" in line for line in logs), logs)

    async def test_a_decoder_too_far_behind_drops_the_oldest_and_says_so(self):
        recognizer = Recognizer(block_on=0, seconds=1.0)
        with patch("mind.voice.CAPTURE_QUEUE_SECONDS", 0.3):  # ten frames
            voice, _, logs = await self.run_arecord(recognizer, 120)
        self.assertLess(len(recognizer.fed), 120)
        self.assertEqual(recognizer.fed[-1], 119, "the newest frames are kept")
        self.assertEqual(recognizer.fed, sorted(set(recognizer.fed)), "in order, none twice")
        # Once while capturing (rate-limited), and what was left as capture closes.
        warnings = [line for line in logs if "microphone audio dropped" in line]
        self.assertIn(len(warnings), (1, 2), logs)
        self.assertIn("decoding fell 0.3 s behind", warnings[0])
        dropped = 120 - len(recognizer.fed)
        self.assertIn(f"{dropped} dropped since capture opened", warnings[-1])
        self.assertTrue(any(f"{dropped} frames dropped" in line for line in logs), logs)

    async def test_a_capture_that_keeps_up_closes_quietly(self):
        recognizer = Recognizer()
        _, _, logs = await self.run_arecord(recognizer, 20, rate_hz=200)
        self.assertEqual(recognizer.fed, list(range(20)))
        self.assertFalse(any("capture closed" in line or "dropped" in line for line in logs), logs)

    async def test_arecord_ending_is_a_capture_failure(self):
        recognizer = Recognizer()
        voice = voice_with(recognizer)

        @asynccontextmanager
        async def child(*command, **options):
            os.close(os.dup(options["stdout"]))
            yield Mock(stdout=None, returncode=1)

        with patch("mind.voice.child", child), self.assertRaisesRegex(RuntimeError, "microphone stream closed"), \
                self.assertLogs("being.voice", "INFO"):
            await asyncio.wait_for(voice.capture_stream(), 5)


class DiagnosticBacklog(unittest.IsolatedAsyncioTestCase):
    async def test_audio_not_yet_decoded_when_capture_closes_is_heard_when_it_reopens(self):
        import socket

        ours, theirs = socket.socketpair()
        self.addCleanup(theirs.close)
        ours.setblocking(False)

        async def no_wait(_seconds):
            await real_sleep(0)

        microphone = DiagnosticMicrophone(ours, "/run/test.sock", clock=lambda: 0.0, sleep=no_wait)
        self.addCleanup(microphone.close)
        # The sender waits while the socket is full, as pip-diag does.
        sender = threading.Thread(target=theirs.sendall, args=(b"".join(frame(index) for index in range(60)),),
                                  daemon=True)
        sender.start()
        self.addCleanup(sender.join, 5)
        recognizer = Recognizer()
        voice = voice_with(recognizer)
        voice.mic_port = microphone
        voice.channel = None

        original = recognizer.feed

        def feed(data, attending, oneshot=True):
            events = original(data, attending, oneshot)
            if index_of(data) == 5:
                voice.lid_open = False          # a reply, a mute or the lid closes capture here
            return events

        recognizer.feed = feed
        with self.assertLogs("being.voice", "INFO") as logs:
            await asyncio.wait_for(voice.capture_stream(), 5)
            self.assertEqual(recognizer.fed, list(range(6)))
            self.assertTrue(any("not yet decoded" in line for line in logs.output), logs.output)
            voice.lid_open = True
            task = asyncio.ensure_future(voice.capture_stream())
            async with asyncio.timeout(5):
                while recognizer.fed[-1] != 59:
                    await real_sleep(0.005)
            voice.lid_open = False
            await asyncio.wait_for(task, 5)
        self.assertEqual(recognizer.fed, list(range(60)), "every frame heard once, in order")


class Arecord(unittest.TestCase):
    def test_the_alsa_buffer_is_half_a_second_with_30_ms_periods(self):
        command = capture_command("plughw:0,0", "auto")
        self.assertIn(f"--buffer-time={CAPTURE_BUFFER_US}", command)
        self.assertEqual(CAPTURE_BUFFER_US, 500000)
        self.assertIn("--period-time=30000", command)
        self.assertNotIn("-q", command)

    def test_the_queue_holds_ten_seconds(self):
        self.assertEqual(mic_port.CAPTURE_QUEUE_SECONDS, 10.0)
        self.assertEqual(FRAME_SAMPLES, 480)


if __name__ == "__main__":
    unittest.main()
