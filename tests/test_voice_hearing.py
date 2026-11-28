"""What the voice makes of a one-breath request, when it knows the mind is ready,
and when it counts the CPU as quiet enough to measure its engines."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import voice as voice_module
from mind import voice_engines as ve
from mind.recognition import RATE
from mind.voice import CommandGrammar, Voice, command_phrases
from tests.test_voice_feedback import Harness, ScriptedRecognizer, tone
from tests.test_voice_grammar import GrammarRecognizer

real_sleep = asyncio.sleep


async def dictate(text, *, oneshot=True, grammar_result=None, recognizer=None):
    """One dictation through the voice; returns the final it sent and the log."""
    flags = {"oneshot": True} if oneshot else {}
    script = [(True, [("hear.speech_start", flags)]), (False, [("hear.final", {"text": text, **flags})])]
    harness = Harness(grammar=CommandGrammar(command_phrases()))
    if recognizer is None:
        recognizer = GrammarRecognizer(script, grammar_result, b"\x01\x00" * RATE)
    else:
        recognizer.script = script
    harness.recognizer = harness.voice.recognizer = recognizer
    harness.voice.state = "attending"
    with unittest.TestCase().assertLogs("being.voice", "INFO") as logs:
        async with asyncio.TaskGroup() as harness.voice.tasks:
            harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
            await harness.frames(tone(4000), tone(4000))
            async with asyncio.timeout(2):
                while "hear.final" not in harness.topics():
                    await real_sleep(0.005)
            harness.voice.disarm()
    final = [fields for topic, fields in harness.sent if topic == "hear.final"]
    return final[-1], logs.output, recognizer


class Remnant(unittest.TestCase):
    def setUp(self):
        self.grammar = CommandGrammar(command_phrases())

    def test_leading_words_the_name_left_go_when_the_rest_is_a_safe_command(self):
        self.assertEqual(self.grammar.without_remnant("have open a terminal"), ("open a terminal", 1))
        # The fewest words go: app_commands takes a leading "and" in its stride.
        self.assertEqual(self.grammar.without_remnant("shape and close the terminal"), ("and close the terminal", 1))
        self.assertEqual(self.grammar.without_remnant("huh what open the terminal"), ("open the terminal", 2))
        self.assertEqual(self.grammar.without_remnant("can't open the terminal"), ("open the terminal", 1))

    def test_otherwise_the_transcript_stays(self):
        for text in ("don't open a terminal", "not now open the terminal", "tell me about the sea",
                     "three words before open a terminal", "uh clear the workshop", "open", "so then"):
            self.assertIsNone(self.grammar.without_remnant(text), text)

    def test_a_parse_that_fails_keeps_the_transcript(self):
        with patch.object(CommandGrammar, "commands", side_effect=RuntimeError("broken")), \
                self.assertLogs("being.voice", "WARNING"):
            self.assertIsNone(self.grammar.without_remnant("have open a terminal"))


class OneBreath(unittest.IsolatedAsyncioTestCase):
    async def test_a_one_breath_final_loses_the_names_leftovers(self):
        final, output, recognizer = await dictate("have open a terminal")
        self.assertEqual(final, {"text": "open a terminal", "oneshot": True, "speaker": "Richard"})
        self.assertEqual(recognizer.decoded, [], "no grammar pass is needed")
        self.assertTrue(any("left out 1 leading word that the name left behind" in line for line in output))
        self.assertFalse(any("have open" in line for line in output), "no words in the log")

    async def test_only_after_the_name(self):
        final, _, _ = await dictate("have open a terminal", oneshot=False)
        self.assertEqual(final["text"], "have open a terminal")

    async def test_never_through_a_negation_or_into_an_unsafe_command(self):
        for text in ("don't open a terminal", "uh clear the workshop"):
            final, _, _ = await dictate(text)
            self.assertEqual(final["text"], text)

    async def test_moonshine_finals_are_checked_too(self):
        # Moonshine has no grammar pass of its own.
        final, _, _ = await dictate("Huh, close the terminal.", recognizer=ScriptedRecognizer())
        self.assertEqual(final["text"], "close the terminal")


class MindReady(unittest.IsolatedAsyncioTestCase):
    async def test_the_brainstem_past_waking_means_the_mind_is_ready(self):
        harness = Harness()
        voice = harness.voice
        voice.sync_capture = lambda: real_sleep(0)
        await voice.handle({"t": "state.changed", "state": "waking", "muted": False})
        self.assertFalse(voice.mind_ready.is_set())
        with self.assertLogs("being.voice", "INFO") as logs:
            # The snapshot a newly connected voice gets: no `prev`.
            await voice.handle({"t": "state.changed", "state": "idle", "muted": False})
        self.assertTrue(voice.mind_ready.is_set())
        self.assertTrue(any("past waking, so the mind is ready" in line for line in logs.output))

    async def test_asleep_or_a_possible_lid_wake_teaches_nothing(self):
        harness = Harness()
        voice = harness.voice
        voice.sync_capture = lambda: real_sleep(0)
        await voice.handle({"t": "state.changed", "state": "asleep", "muted": False})
        self.assertFalse(voice.mind_ready.is_set(), "waking can turn to asleep before mind.ready")
        await voice.handle({"t": "body.lid", "closed": True})
        await voice.handle({"t": "state.changed", "state": "idle", "prev": "waking", "muted": False})
        self.assertFalse(voice.mind_ready.is_set(), "with the lid seen closed, idle may be a lid wake")
        await voice.handle({"t": "mind.ready", "cortex": True})
        self.assertTrue(voice.mind_ready.is_set())

    async def test_the_try_measurement_then_comes_without_waiting_for_mind_ready(self):
        class Engines:
            asked = []

            def cpu_quiet(self):
                return True

            async def refresh_tts(self, current, choice, *, state_dir, idle=None):
                self.asked.append("tts")

            async def refresh_asr(self, current, wake, *, state_dir, wake_word, idle=None):
                self.asked.append("asr")

        engines = Engines()
        voice = Voice(object(), "Richard", ["arecord"], "plughw:0,0", engines=engines,
                      settings_dir=Path("/nonexistent"), try_boot=True)
        voice.sync_capture = lambda: real_sleep(0)
        with patch.object(voice_module, "TRY_MEASURE_AFTER", 0.01), \
                patch.object(voice_module, "TRY_READY_PATIENCE", 30), self.assertLogs("being.voice", "INFO"):
            task = asyncio.ensure_future(voice.try_measurement(voice.recognizer))
            await real_sleep(0.02)
            await voice.handle({"t": "state.changed", "state": "idle", "prev": "waking", "muted": False})
            await asyncio.wait_for(task, 2)
        self.assertEqual(engines.asked, ["tts", "asr"])


class Quiet(unittest.TestCase):
    def psi(self, folder, avg10):
        path = Path(folder) / "cpu"
        path.write_text(f"some avg10={avg10:.2f} avg60=0.00 avg300=0.00 total=1\n"
                        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        return path

    def test_pressure_decides_where_it_exists_whatever_the_load_average(self):
        # The laptop with the face up: load 1.51 from the i915 flip worker, pressure 0.16.
        with tempfile.TemporaryDirectory() as folder, patch("os.getloadavg", return_value=(1.51, 1.4, 1.3)):
            self.assertTrue(ve.cpu_quiet(path=self.psi(folder, 0.16), window=0))
            self.assertFalse(ve.system_idle(), "the load average alone would never measure")
            self.assertFalse(ve.cpu_quiet(path=self.psi(folder, 12.0), window=0))

    def test_the_load_average_only_without_pressure(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "none"
            with patch("os.getloadavg", return_value=(0.3, 0, 0)):
                self.assertTrue(ve.cpu_quiet(path=missing, window=0))
            with patch("os.getloadavg", return_value=(1.5, 0, 0)):
                self.assertFalse(ve.cpu_quiet(path=missing, window=0))

    def test_a_busy_voice_is_not_quiet(self):
        with tempfile.TemporaryDirectory() as folder:
            psi = self.psi(folder, 0.0)
            self.assertFalse(ve.cpu_quiet(path=psi, own=lambda window: 0.9))
            self.assertTrue(ve.cpu_quiet(path=psi, own=lambda window: 0.05))
            self.assertTrue(ve.cpu_quiet(path=psi, own=lambda window: None), "unknown own use does not block")
            windows = []
            ve.cpu_quiet(path=psi, own=lambda window: windows.append(window))
            self.assertEqual(windows, [ve.OWN_CPU_WINDOW])

    def test_own_cpu_is_this_process_over_a_short_window(self):
        clock, cpu = iter((10.0, 10.25)), iter((3.0, 3.1))
        used = ve.own_cpu(0.25, clock=lambda: next(clock), cpu=lambda: next(cpu), sleep=lambda seconds: None)
        self.assertAlmostEqual(used, 0.4)
        self.assertIsNone(ve.own_cpu(0))
        self.assertLess(ve.own_cpu(0.05), ve.OWN_CPU_LIMIT, "an idle test process is quiet")

    def test_the_defaults(self):
        self.assertEqual(ve.QUIET_PRESSURE, 5.0)
        self.assertLessEqual(ve.OWN_CPU_WINDOW, 0.5)


class RefreshIdle(unittest.IsolatedAsyncioTestCase):
    async def test_the_refresh_asks_cpu_quiet_off_the_event_loop_by_default(self):
        threads = []

        def quiet():
            threads.append(threading.current_thread())
            return False

        with patch.object(ve, "cpu_quiet", quiet), patch("mind.asr_sherpa.missing", return_value=[]), \
                patch("mind.asr_sherpa.test_wav", return_value=Path("/x.wav")), \
                patch.dict("os.environ", {}, clear=True), \
                tempfile.TemporaryDirectory() as folder:
            wake = object()
            self.assertIsNone(await ve.refresh_asr(wake, wake, state_dir=folder, build="b1"))
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.main_thread())

    async def test_the_voices_periodic_refresh_uses_cpu_quiet(self):
        seen = []

        class Engines:
            def cpu_quiet(self):
                return True

            def choose_tts(self, choice, state_dir=None):
                return None, None

            def choose_asr(self, wake, state_dir=None, wake_word="Hey Pip"):
                return wake, None

            async def refresh_tts(self, current, choice, *, state_dir, idle=None):
                seen.append(idle)

            async def refresh_asr(self, current, wake, *, state_dir, wake_word, idle=None):
                seen.append(idle)

        engines = Engines()
        voice = Voice(object(), "Richard", ["arecord"], "plughw:0,0", engines=engines,
                      settings_dir=Path("/nonexistent"))
        voice.state = "idle"
        with patch.object(voice_module, "ENGINE_REFRESH_SECONDS", 0.005):
            task = asyncio.ensure_future(voice.choose_engines())
            async with asyncio.timeout(2):
                while len(seen) < 2:
                    await real_sleep(0.005)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(all(getattr(idle, "__func__", None) is Engines.cpu_quiet for idle in seen), seen)


if __name__ == "__main__":
    unittest.main()
