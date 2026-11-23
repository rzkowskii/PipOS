"""Cancellation of first-clause timing collection and playback."""

import asyncio
import signal
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import Mock, patch

from mind.voice import Voice


SYNTHESIZE = """
import signal
import struct
import sys
import wave
from pathlib import Path

audio, amplitude, marker, action = sys.argv[1:]
with wave.open(audio, 'wb') as output:
    output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
    output.writeframes(struct.pack('<h', int(amplitude)) * 4800)
if action == 'drain':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # Exceed the pipe and asyncio reader buffers before signalling readiness.
    sys.stdout.buffer.write(b'pau:0.100 ' * 250000)
    sys.stdout.flush()
if action == 'timed':
    print('pau:0.100 aa:0.230 pau:0.300', flush=True)
if action in ('drain', 'prefetch'):
    Path(marker).touch()
    while True:
        signal.pause()
"""

PLAY = """
import signal
import sys
from pathlib import Path

if sys.argv[1] == 'hold':
    Path(sys.argv[2]).touch()
    while True:
        signal.pause()
"""

FIRST = "First reply."
SECOND = "Prefetched reply."
STALE = "Stale reply."
FRESH = "Fresh reply."
AMPLITUDE = {FIRST: 1000, SECOND: 2000, STALE: 3000, FRESH: 4000}


class FirstPauseCancellation(unittest.IsolatedAsyncioTestCase):
    async def wait_for_marker(self, marker):
        async with asyncio.timeout(3):
            while not marker.exists():
                await asyncio.sleep(0.01)

    async def test_cancel_during_first_clause_timing_collection(self):
        for closing_lid in (True, False):
            with self.subTest(closing_lid=closing_lid):
                await self.cancel_reply(closing_lid, during_synthesis=True)

    async def test_cancel_during_first_playback_and_next_clause_synthesis(self):
        for closing_lid in (True, False):
            with self.subTest(closing_lid=closing_lid):
                await self.cancel_reply(closing_lid, during_synthesis=False)

    async def cancel_reply(self, closing_lid, *, during_synthesis):
        voice = Voice(Mock(), "Richard", [], "test", shorten_first_pause=True)
        events = []
        finished = asyncio.Event()
        syntheses, playbacks = {}, []
        create_process = asyncio.create_subprocess_exec
        work_directory = tempfile.TemporaryDirectory

        async def event(topic, **fields):
            incoming = {"t": topic, **fields}
            voice.on_event_received(incoming)
            await voice.handle(incoming)

        async def send(topic, **fields):
            events.append((topic, fields))
            if topic == "say.done":
                finished.set()

        voice.bus.send = send
        with work_directory() as directory:
            marker = Path(directory)

            async def spawn(program, *args, **options):
                if program == "flite":
                    text = args[args.index("-t") + 1]
                    audio = args[args.index("-o") + 1]
                    self.assertNotIn(text, syntheses)
                    timed = "-psdur" in args
                    action = "timed" if timed else "plain"
                    if text == FIRST and during_synthesis:
                        self.assertTrue(timed)
                        self.assertEqual(options.get("stdout"), asyncio.subprocess.PIPE)
                        action = "drain"
                    elif text == SECOND:
                        action = "prefetch"
                    process = await create_process(
                        sys.executable, "-c", SYNTHESIZE, audio, str(AMPLITUDE[text]),
                        str(marker / action), action, **options,
                    )
                    syntheses[text] = (process, timed)
                    return process
                self.assertEqual(program, "aplay")
                with wave.open(args[-1]) as audio:
                    level = max(array("h", audio.readframes(audio.getnframes())))
                action = "hold" if level == AMPLITUDE[FIRST] else "play"
                process = await create_process(sys.executable, "-c", PLAY, action,
                                               str(marker / "playing"), **options)
                playbacks.append((process, level))
                return process

            def temporary_work(*, prefix, dir):
                return work_directory(prefix=prefix, dir=directory)

            with patch("mind.voice.asyncio.create_subprocess_exec", side_effect=spawn), \
                    patch("mind.voice.tempfile.TemporaryDirectory", side_effect=temporary_work):
                async with asyncio.TaskGroup() as voice.tasks:
                    try:
                        await event("body.lid", closed=False, seq=1)
                        await event("state.changed", state="speaking", muted=False)
                        for text in (FIRST, SECOND, STALE):
                            await event("say.request", text=text, last=False)

                        if during_synthesis:
                            await self.wait_for_marker(marker / "drain")
                        else:
                            await self.wait_for_marker(marker / "playing")
                            await self.wait_for_marker(marker / "prefetch")

                        if closing_lid:
                            closing = {"t": "body.lid", "closed": True, "seq": 2}
                            reply = voice.reply
                            voice.on_event_received(closing)
                            self.assertTrue(reply.cancelling())
                            await voice.handle(closing)
                            await event("say.request", text=STALE, last=True)
                        else:
                            await event("say.stop")

                        self.assertIsNone(voice.reply)
                        self.assertTrue(voice.clauses.empty())
                        self.assertFalse(finished.is_set())
                        self.assertEqual(voice.lid_open, not closing_lid)
                        self.assertTrue(syntheses[FIRST][1])
                        if during_synthesis:
                            self.assertEqual(syntheses[FIRST][0].returncode, -signal.SIGKILL)
                            self.assertEqual(set(syntheses), {FIRST})
                            self.assertFalse(playbacks)
                            self.assertFalse(any(topic == "say.started" for topic, _ in events))
                        else:
                            self.assertEqual(set(syntheses), {FIRST, SECOND})
                            self.assertEqual(syntheses[SECOND][0].returncode, -signal.SIGTERM)
                            self.assertFalse(syntheses[SECOND][1])
                            self.assertEqual([(process.returncode, level) for process, level in playbacks],
                                             [(-signal.SIGTERM, AMPLITUDE[FIRST])])

                        if closing_lid:
                            await event("body.lid", closed=False, seq=3)
                        await event("say.request", text=FRESH, last=True)
                        await asyncio.wait_for(finished.wait(), 3)
                        self.assertNotIn(STALE, syntheses)
                        self.assertEqual(syntheses[FRESH][0].returncode, 0)
                        self.assertTrue(syntheses[FRESH][1])
                        self.assertEqual([(process.returncode, level) for process, level in playbacks[-1:]],
                                         [(0, AMPLITUDE[FRESH])])
                        self.assertEqual(len(playbacks), 1 if during_synthesis else 2)
                        self.assertEqual([fields for topic, fields in events if topic == "say.done"],
                                         [{"final": True, "lid_seq": 3 if closing_lid else 1}])
                    finally:
                        await voice.stop_reply()


if __name__ == "__main__":
    unittest.main()
