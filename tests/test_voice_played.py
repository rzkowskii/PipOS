"""Voice reports which clauses of a reply really played (say.played, mind/played.py).

The real reply path runs: say.request through `handle`, synthesis in
`prepare_clause` (Flite, a stock clip, prepared night speech, or Flite after
another engine fails) and `play_once`. Only the processes are stand-ins.
"""

import asyncio
import struct
import tempfile
import unittest
import wave
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mind import voice as voice_module
from mind.voice import Voice, clause_request

TOKEN = "0123456789abcdef"


def write_wav(path, seconds=0.5):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(struct.pack("<h", 1200) * int(16000 * seconds))


class Player:
    """A stand-in aplay: it plays until the voice has sent one level, unless held."""

    def __init__(self, status=None):
        self.returncode = status
        self.status = status or 0

    async def wait(self):
        return self.status


class Speaking(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.voice = Voice(Mock(), "Richard", [], "")
        self.voice.lid_open = True
        self.voice.state = "speaking"
        self.voice.muted = True  # no capture in these tests
        self.voice.speech_filter = AsyncMock(return_value=False)
        self.events = []
        self.synthesized = {}  # WAV path -> the clause last synthesized into it
        self.played = []
        self.hold = {}  # clause text -> an Event its playback waits for
        self.rejected = set()  # clause texts the player refuses at once
        self.player = None
        self.voice.bus.send = AsyncMock(side_effect=self.send)
        patches = (
            patch("mind.voice.child", self.child),
            patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext(self.tmp.name)),
            patch("mind.voice.PLAYBACK_RETRY_FIRST", 0.0),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        await self.voice.stop_reply()
        self.tmp.cleanup()

    async def send(self, topic, **fields):
        self.events.append({"t": topic, **fields})
        player = self.player
        if topic == "say.level" and player is not None and player.returncode is None:
            text = self.synthesized.get(player.path)
            held = self.hold.get(text)
            if held is None or held.is_set():
                player.returncode = 0

    @asynccontextmanager
    async def child(self, *command, **options):
        if command[0] == "flite":
            self.synthesized[command[-1]] = command[command.index("-t") + 1]
            write_wav(command[-1])
            yield Mock(wait=AsyncMock(return_value=0))
        elif command[0] == "aplay":
            path = command[-1]
            text = self.synthesized.get(path)
            player = self.player = Player(1 if text in self.rejected else None)
            player.path = path
            self.played.append(text)
            try:
                yield player
            finally:
                self.player = None
        else:
            raise AssertionError(command)

    async def say(self, text, seq=None, *, last=False, reply=TOKEN, **fields):
        event = {"t": "say.request", "text": text, "last": last, **fields}
        if seq is not None:
            event["seq"] = seq
        if reply is not None:
            event["reply"] = reply
        self.voice.on_event_received(event)
        await self.voice.handle(event)

    async def reply_ends(self):
        async with asyncio.timeout(3):
            while not any(event["t"] == "say.done" for event in self.events):
                await asyncio.sleep(0.01)

    def receipts(self):
        """say.started, say.played and say.done, in the order they went out."""
        shown = []
        for event in self.events:
            if event["t"] == "say.played":
                shown.append(("final", event["stopped"]) if event.get("final") else ("seq", event["seq"]))
            elif event["t"] in ("say.started", "say.done"):
                shown.append((event["t"], event.get("failed", False)) if event["t"] == "say.done" else event["t"])
        return shown

    async def test_each_clause_is_reported_at_its_first_audio_and_the_end_comes_before_say_done(self):
        await self.say("One thing.", 1, face_cue="joy")
        await self.say("Another thing.", 2)
        await self.say("", 3, last=True)
        await self.reply_ends()
        self.assertEqual(self.played, ["One thing.", "Another thing."])
        self.assertEqual(self.receipts(), ["say.started", ("seq", 1), ("seq", 2), ("final", False),
                                           ("say.done", False)])
        self.assertTrue(all(e["reply"] == TOKEN for e in self.events if e["t"] == "say.played"))
        self.assertEqual(self.voice._open_replies, {})

    async def test_stopping_mid_reply_reports_what_started_and_then_the_stop(self):
        self.hold["Two."] = asyncio.Event()
        for seq, text in enumerate(("One.", "Two.", "Three."), 1):
            await self.say(text, seq)
        async with asyncio.timeout(3):
            while ("seq", 2) not in self.receipts():
                await asyncio.sleep(0.01)
        # Esc: the brainstem's say.stop, partway through the second clause.
        await self.voice.handle({"t": "say.stop"})
        self.assertEqual(self.receipts(), ["say.started", ("seq", 1), ("seq", 2), ("final", True)])
        self.assertNotIn("Three.", self.played)
        self.assertEqual(self.voice._open_replies, {})

    async def test_a_clause_the_speaker_rejects_is_not_reported(self):
        self.rejected.add("One.")
        await self.say("One.", 1)
        await self.say("Two.", 2)
        await self.say("", 3, last=True)
        await self.reply_ends()
        self.assertEqual(self.receipts(), ["say.started", ("seq", 2), ("final", False), ("say.done", True)])

    async def test_stock_clips_and_prepared_night_speech_are_reported_like_any_clause(self):
        stock = AsyncMock(side_effect=self.voice.synthesize_stock)
        self.voice.synthesize_stock = stock
        self.voice.restore_stock_donor = AsyncMock(return_value=False)

        def prepared(text, audio):
            if text != "Good morning.":
                return False
            self.synthesized[str(audio)] = text
            write_wav(audio)
            return True

        self.voice.restore_prepared_speech = Mock(side_effect=prepared)
        await self.say("Okay.", 1)
        await self.say("Good morning.", 2)
        await self.say("", 3, last=True)
        await self.reply_ends()
        self.assertEqual(stock.await_args.args[0], "Okay.")
        self.assertEqual(self.played, ["Okay.", "Good morning."])
        self.assertEqual(self.receipts(), ["say.started", ("seq", 1), ("seq", 2), ("final", False),
                                           ("say.done", False)])

    async def test_a_clause_flite_says_for_a_failed_engine_is_reported(self):
        class Broken:
            name = "piper"

            async def synthesize(self, text, audio, rate):
                raise RuntimeError("no model")

        self.voice.tts = Broken()
        await self.say("Said by Flite.", 1)
        await self.say("", 2, last=True)
        await self.reply_ends()
        self.assertEqual(self.played, ["Said by Flite."])
        self.assertEqual(self.receipts(), ["say.started", ("seq", 1), ("final", False), ("say.done", False)])

    async def test_a_failed_reply_still_ends_with_its_receipt(self):
        self.voice.speak_clauses = AsyncMock(side_effect=RuntimeError("broken"))
        await self.say("One.", 1)
        await self.reply_ends()
        self.assertEqual(self.receipts(), [("final", True), ("say.done", True)])

    async def test_a_closed_lid_sends_nothing_and_forgets_the_reply(self):
        self.hold["One."] = asyncio.Event()
        await self.say("One.", 1)
        async with asyncio.timeout(3):
            while ("seq", 1) not in self.receipts():
                await asyncio.sleep(0.01)
        closed = {"t": "body.lid", "closed": True}
        self.voice.on_event_received(closed)
        await self.voice.handle(closed)
        self.assertEqual(self.receipts(), ["say.started", ("seq", 1)], "the mind settles from its own receipts")
        self.assertEqual(self.voice._open_replies, {})

    async def test_clauses_without_a_reply_token_send_no_receipts(self):
        await self.say("Reminder. Call Sam.", 1, reply=None)
        await self.say("", 2, last=True, reply=None)
        await self.reply_ends()
        self.assertEqual(self.receipts(), ["say.started", ("say.done", False)])

    async def test_a_stop_with_no_reply_open_sends_nothing(self):
        await self.voice.handle({"t": "say.stop"})
        self.assertEqual(self.events, [])


    async def test_a_reply_queued_behind_the_one_playing_is_spoken_too(self):
        other = "fedcba9876543210"
        await self.say("Alpha.", 1)
        await self.say("", 2, last=True)
        await self.say("Beta.", 1, reply=other)
        await self.say("", 2, last=True, reply=other)
        async with asyncio.timeout(3):
            while sum(e["t"] == "say.done" for e in self.events) < 2:
                await asyncio.sleep(0.01)
        self.assertEqual(self.played, ["Alpha.", "Beta."])
        finals = [e["reply"] for e in self.events if e["t"] == "say.played" and e.get("final")]
        self.assertEqual(finals, [TOKEN, other])
        self.assertEqual(self.voice._open_replies, {})


class Requests(unittest.TestCase):
    def test_only_a_well_formed_token_and_a_spoken_clause_carry_receipt_marks(self):
        self.assertEqual(clause_request({"text": "Hi.", "last": False, "seq": 1, "reply": TOKEN})["seq"], 1)
        final = clause_request({"text": "", "last": True, "seq": 2, "reply": TOKEN})
        self.assertEqual((final["reply"], "seq" in final), (TOKEN, False), "the end marker plays nothing")
        for reply in (TOKEN.upper(), TOKEN[:-1], 7, None, TOKEN + "0"):
            self.assertNotIn("reply", clause_request({"text": "Hi.", "last": False, "seq": 1, "reply": reply}))
        for seq in (0, -1, True, 1.0, "1"):
            self.assertNotIn("seq", clause_request({"text": "Hi.", "last": False, "seq": seq, "reply": TOKEN}))


if __name__ == "__main__":
    unittest.main()
