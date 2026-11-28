"""The speech filter hook: each clause may be shaped for the speakers before it
plays, and nothing the filter does can stop Pip speaking."""

import asyncio
import importlib.util
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import Mock, patch

from mind import voice as voice_module
from mind.voice import SPEAKER_EQ_FAILURES, Voice


def write(path, value=1000, seconds=0.5, rate=22050):
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(array("h", [value] * int(seconds * rate)).tobytes())


def first_sample(path):
    with wave.open(str(path)) as audio:
        return array("h", audio.readframes(1))[0]


class Filter:
    """Stands in for mind/speaker_eq.py."""

    def __init__(self, *, on=True, result=True, value=500, seconds=None, error=None, delay=0.0, rates=None):
        self.on, self.result, self.value, self.seconds = on, result, value, seconds
        self.error, self.delay = error, delay
        self.calls = []
        self.threads = []
        self.rates = rates if rates is not None else []

    def enabled(self):
        return self.on

    def process_wav(self, path_in, path_out, *, rate):
        self.calls.append((path_in, path_out))
        self.rates.append(rate)
        self.threads.append(threading.current_thread())
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.result is True:
            with wave.open(path_in) as source:
                length = source.getnframes() / source.getframerate()
            write(path_out, self.value, self.seconds if self.seconds is not None else length, rate)
        return self.result


class Hook(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clause = self.folder / "clause.wav"
        write(self.clause)

    def voice(self, module):
        voice = Voice(Mock(), "Richard", [], "")
        voice._speaker_eq = module
        return voice

    async def test_an_enabled_filter_replaces_the_clause_off_the_event_loop(self):
        module = Filter()
        voice = self.voice(module)
        self.assertTrue(await voice.speech_filter(self.clause))
        self.assertEqual(first_sample(self.clause), 500)
        self.assertEqual(module.rates, [22050])
        self.assertIsNot(module.threads[0], threading.main_thread())
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["clause.wav"], "nothing left over")

    async def test_a_disabled_filter_leaves_the_clause_quietly(self):
        voice = self.voice(Filter(on=False))
        with patch.object(voice_module.LOG, "warning") as warned:
            self.assertFalse(await voice.speech_filter(self.clause))
        warned.assert_not_called()
        self.assertEqual(first_sample(self.clause), 1000)

    async def test_any_failure_plays_the_clause_as_it_was(self):
        cases = (Filter(result=False), Filter(error=RuntimeError("boom")), Filter(result="yes"),
                 Filter(seconds=0.05), Filter(value=0, seconds=0.0))
        for module in cases:
            voice = self.voice(module)
            with self.assertLogs("being.voice", "WARNING") as logs:
                self.assertFalse(await voice.speech_filter(self.clause))
            self.assertEqual(first_sample(self.clause), 1000)
            self.assertIn("playing the clause unfiltered", logs.output[0])
            self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["clause.wav"])

    async def test_no_room_for_the_filtered_copy_still_plays_the_clause(self):
        voice = self.voice(Filter())
        with patch("mind.voice.tempfile.mkdtemp", side_effect=OSError(28, "No space left on device")), \
                self.assertLogs("being.voice", "WARNING") as logs:
            self.assertFalse(await voice.speech_filter(self.clause))
        self.assertEqual(first_sample(self.clause), 1000)
        self.assertIn("playing the clause unfiltered", logs.output[0])
        self.assertEqual(voice.speaker_eq_failures, 1)

    async def test_a_failure_replacing_the_clause_still_plays_it(self):
        voice = self.voice(Filter())
        with patch("mind.voice.os.replace", side_effect=PermissionError("read-only")), \
                self.assertLogs("being.voice", "WARNING"):
            self.assertFalse(await voice.speech_filter(self.clause))
        self.assertEqual(first_sample(self.clause), 1000)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["clause.wav"])

    async def test_a_slow_filter_is_not_waited_for(self):
        module = Filter(delay=0.5)
        voice = self.voice(module)
        with patch.object(voice_module, "SPEAKER_EQ_BASE_SECONDS", 0.05), \
                patch.object(voice_module, "SPEAKER_EQ_PER_SECOND", 0.0), \
                self.assertLogs("being.voice", "WARNING") as logs:
            began = time.monotonic()
            self.assertFalse(await voice.speech_filter(self.clause))
            self.assertLess(time.monotonic() - began, 0.4)
        self.assertIn("it took more than 0.1 s", logs.output[0])
        self.assertEqual(first_sample(self.clause), 1000)
        await asyncio.sleep(0.7)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["clause.wav"],
                         "the late output is tidied away when the filter ends")

    async def test_repeated_failures_leave_the_filter_out(self):
        module = Filter(result=False)
        voice = self.voice(module)
        with self.assertLogs("being.voice", "WARNING") as logs:
            for _ in range(SPEAKER_EQ_FAILURES + 2):
                await voice.speech_filter(self.clause)
        self.assertEqual(len(module.calls), SPEAKER_EQ_FAILURES)
        self.assertIn("for the rest of this session", logs.output[-1])

    async def test_a_success_resets_the_failure_count(self):
        module = Filter(result=False)
        voice = self.voice(module)
        with self.assertLogs("being.voice", "WARNING"):
            await voice.speech_filter(self.clause)
        module.result = True
        self.assertTrue(await voice.speech_filter(self.clause))
        self.assertEqual(voice.speaker_eq_failures, 0)

    async def test_an_unreadable_clause_is_left_alone(self):
        self.clause.write_bytes(b"not a wav")
        voice = self.voice(Filter())
        with self.assertLogs("being.voice", "WARNING"):
            self.assertFalse(await voice.speech_filter(self.clause))
        self.assertEqual(self.clause.read_bytes(), b"not a wav")

    async def test_each_reply_clause_is_filtered_after_synthesis(self):
        module = Filter()
        voice = self.voice(module)

        async def synthesize(text, audio, first):
            write(audio)

        voice.synthesize = synthesize
        await voice.clauses.put({"text": "Hello there.", "last": True})
        ready, last, _, _, _ = await voice.prepare_clause(self.clause, first=True)
        self.assertEqual((ready, last), (True, True))
        self.assertEqual(first_sample(self.clause), 500)

    async def test_the_apology_is_filtered_too(self):
        module = Filter()
        voice = self.voice(module)
        voice.work_dir = str(self.folder)
        voice.bus.send = Mock(side_effect=lambda *args, **kwargs: asyncio.sleep(0))
        played = []

        async def synthesize(text, audio, first):
            write(audio)

        async def play(audio, first):
            played.append(first_sample(audio))

        voice.synthesize, voice.play = synthesize, play
        with self.assertLogs("being.voice", "INFO"):
            await voice.apologise()
        self.assertEqual(played, [500])


class Finding(unittest.TestCase):
    def test_switched_off_by_the_environment(self):
        voice = Voice(Mock(), "Richard", [], "")
        with patch.dict("os.environ", {"BEING_SPEAKER_EQ": "0"}), self.assertLogs("being.voice", "INFO") as logs:
            self.assertIsNone(voice.speaker_eq())
        self.assertIn("speech filter: off", logs.output[0])

    def test_an_absent_module_is_no_filter_and_no_warning(self):
        voice = Voice(Mock(), "Richard", [], "")
        with patch.dict(sys.modules, {"mind.speaker_eq": None}), patch.dict("os.environ", {}, clear=True), \
                patch.object(voice_module.LOG, "warning") as warned:
            self.assertIsNone(voice.speaker_eq())
        warned.assert_not_called()

    def test_a_module_without_the_calls_is_no_filter(self):
        voice = Voice(Mock(), "Richard", [], "")
        with patch.dict(sys.modules, {"mind.speaker_eq": types.ModuleType("mind.speaker_eq")}), \
                patch.dict("os.environ", {}, clear=True), self.assertLogs("being.voice", "WARNING"):
            self.assertIsNone(voice.speaker_eq())

    @unittest.skipUnless(importlib.util.find_spec("mind.speaker_eq"), "no speech filter in this tree")
    def test_the_real_module_is_found_and_looked_for_once(self):
        voice = Voice(Mock(), "Richard", [], "")
        with patch.dict("os.environ", {}, clear=True):
            module = voice.speaker_eq()
        from mind import speaker_eq

        self.assertIs(module, speaker_eq)
        with patch.dict(sys.modules, {"mind.speaker_eq": None}):
            self.assertIs(voice.speaker_eq(), speaker_eq)


if __name__ == "__main__":
    unittest.main()
