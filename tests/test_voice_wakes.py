"""False wakes, the self-hearing follow-up, the night apology and the key mute
at start (cable run, 25 Nov), with the name-remnant rules, the n-best rescue,
the capture high-pass and the corrected engine and timing lines."""

import asyncio
import logging
import math
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mind import keys, recognition
from mind.recognition import (
    FRAME_SAMPLES, HIGHPASS_HZ, MAX_AUTO_GAIN, RATE, HighPass, MicLevel, Recognizer, highpass_setting, levels,
    name_ish, pcm, strip_address, strip_name_remnant,
)
from mind.voice import (
    DARK_LUX, FOLLOWUP_HOLD_SECONDS, SPEAKER_MUTED_MARKER, CommandGrammar, Voice, command_phrases,
    journal_prefix, load_voice_settings, save_voice_settings,
)
from tests.test_voice_feedback import START, WAKE, Harness, tone
from tests.test_voice_grammar import GrammarRecognizer

real_sleep = asyncio.sleep
ONESHOT_WAKE = (True, [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})])


def final(text, **flags):
    return (False, [("hear.final", {"text": text, **flags})])


class GainCap(unittest.TestCase):
    def test_the_lift_stops_at_three_times(self):
        self.assertEqual(MAX_AUTO_GAIN, 3.0)
        mic = MicLevel()
        quiet = 10 ** (-80 / 20) * 32768       # an analog floor far under the target
        for _ in range(3000):
            mic.observe(quiet * 2, quiet, False)
        self.assertAlmostEqual(mic.gain, 3.0)

    def test_the_cap_can_be_set_per_level(self):
        mic = MicLevel(max_lift=2.0)
        quiet = 10 ** (-80 / 20) * 32768
        for _ in range(3000):
            mic.observe(quiet * 2, quiet, False)
        self.assertAlmostEqual(mic.gain, 2.0)


class NameRemnantRules(unittest.TestCase):
    """The deep dive's measured rules, with round L's protections kept."""

    def test_a_short_comma_clause_ending_in_the_name_goes(self):
        cases = {
            "Lay pit, what time is it?": "what time is it?",
            "I apep, set a timer for a minute.": "set a timer for a minute.",
            "Hey Peck, what do you remember about my orchard?": "what do you remember about my orchard?",
            "Hey, what time is it?": "what time is it?",
            "Okay, open the terminal.": "open the terminal.",
        }
        for heard, expected in cases.items():
            self.assertEqual(strip_name_remnant(heard), expected, heard)

    def test_a_clause_that_says_something_stays(self):
        for heard in ("In Paris, what time is it", "I think, it is late", "No, close it", "Stop, pep",
                      "Wait, open the terminal", "Lay pit,", "Well, open the terminal", "Not yet, pep"):
            self.assertEqual(strip_name_remnant(heard), heard, heard)

    def test_loose_hearings_only_count_as_a_clause_of_their_own(self):
        for word in ("apep", "apepe", "peck", "Pit,", "pep"):
            self.assertTrue(name_ish(word), word)
        for word in ("put", "keep", "open", "paris", "think", ""):
            self.assertFalse(name_ish(word), word)
        # Without a comma a loose hearing is a word of the request.
        self.assertEqual(strip_name_remnant("peck at the seeds"), "peck at the seeds")

    def test_the_moonshine_wake_strip_uses_the_same_rules(self):
        from mind.asr_sherpa import strip_wake

        self.assertEqual(strip_wake("Lay pit, what time is it?", "Hey Pip"), "what time is it?")
        self.assertEqual(strip_wake("In Paris, what time is it", "Hey Pip"), "In Paris, what time is it")


class FollowUpAddress(unittest.TestCase):
    def test_a_greeting_and_the_name_go(self):
        cases = {
            "Hey Pip, what time is it": "what time is it",
            "okay pip what time is it": "what time is it",
            "pip, open the terminal": "open the terminal",
            "hey pep close the terminal": "close the terminal",
            "hey bit, open the clock": "open the clock",
        }
        for heard, expected in cases.items():
            self.assertEqual(strip_address(heard), expected, heard)

    def test_anything_else_stays(self):
        for heard in ("hey bob is here", "hey pat what time is it", "a pip in an apple", "hey pip", "pip",
                      "what time is it", "", "they kept open the terminal"):
            self.assertEqual(strip_address(heard), heard, heard)
        self.assertIsNone(strip_address(None))

    def test_another_name(self):
        self.assertEqual(strip_address("hey clover, how are you", "clover"), "how are you")
        self.assertEqual(strip_address("hey pip how are you", "clover"), "hey pip how are you")


class Meaning(unittest.TestCase):
    def setUp(self):
        self.grammar = CommandGrammar(command_phrases())

    def test_what_the_mind_answers_in_code(self):
        for text in ("open the terminal", "what time is it", "louder", "go to sleep", "brighter", "dimmer"):
            self.assertTrue(self.grammar.holds_command(text), text)
        for text in ("oh", "the", "hmm yes", "and then", "Dima.", "hi"):
            self.assertFalse(self.grammar.holds_command(text), text)

    def test_a_failing_parser_keeps_the_words(self):
        with patch.object(CommandGrammar, "code_kind", side_effect=RuntimeError("broken")), \
                self.assertLogs("being.voice", "WARNING"):
            self.assertTrue(self.grammar.holds_command("oh"))

    def test_safe_meanings_store_nothing(self):
        for text in ("what time is it", "open the terminal", "how is your battery", "louder",
                     "make your screen brighter", "dimmer"):
            self.assertTrue(self.grammar.safe_meaning(text), text)
        for text in ("set a timer for one minute", "remember that my orchard is silver leaf", "forget that",
                     "clear the workshop", "start guest mode", "tell me about the sea", "shut down", "restart",
                     "stop listening"):
            self.assertFalse(self.grammar.safe_meaning(text), text)

    def test_the_grammar_may_take_a_safe_command_of_the_minds_own(self):
        # Ready for code phrases in app_commands.grammar_phrases(): the
        # judge's other checks still apply.
        grammar = CommandGrammar(command_phrases() + ["what time is it"])
        result = {"text": "what time is it", "log_posterior": -0.3, "margin": 12.0}
        self.assertEqual(grammar.judge(result, "so what time is it pal"), ("what time is it", None))
        self.assertEqual(grammar.judge(result, "tell me about the sea")[0], None)

    def test_the_names_leftovers_go_before_a_safe_command_of_the_minds_own(self):
        self.assertEqual(self.grammar.without_remnant("hey pat what time is it"), ("what time is it", 2))
        self.assertEqual(self.grammar.without_remnant("they kept open the terminal"), ("open the terminal", 2))
        self.assertIsNone(self.grammar.without_remnant("hey pat set a timer for one minute"))
        self.assertIsNone(self.grammar.without_remnant("don't tell me the time"))

    def test_nbest_alternatives(self):
        grammar = self.grammar
        alternatives = ["what kind of fit", "what kind of hit", "what kind of faith", "what time is it"]
        self.assertEqual(grammar.nbest_command("what kind of shit", alternatives), ("what time is it", 4))
        # A longer sentence never becomes a short command, nor a negated one.
        self.assertIsNone(grammar.nbest_command("what time does the shop close", ["what time is it"]))
        self.assertIsNone(grammar.nbest_command("don't open the terminal", ["open the terminal"]))
        self.assertIsNone(grammar.nbest_command("what kind of shit", ["don't open the terminal"]))
        # Nothing in common with what was heard, or nothing safe.
        self.assertIsNone(grammar.nbest_command("pulled the boat", ["open the terminal"]))
        self.assertIsNone(grammar.nbest_command("set a time for one minute", ["set a timer for one minute"]))


class NbestRecognizer(GrammarRecognizer):
    def __init__(self, script, alternatives, result=None):
        super().__init__(script, result)
        self.alternatives = alternatives
        self.asked = []

    def nbest(self, limit=5):
        self.asked.append(limit)
        return list(self.alternatives)


class NbestRescue(unittest.IsolatedAsyncioTestCase):
    async def dictate(self, text, alternatives, *, oneshot=False, result=None):
        flags = {"oneshot": True} if oneshot else {}
        script = [(True, [("hear.speech_start", flags)]), (False, [("hear.final", {"text": text, **flags})])]
        harness = Harness(grammar=CommandGrammar(command_phrases()))
        recognizer = NbestRecognizer(script, alternatives, result)
        harness.recognizer = harness.voice.recognizer = recognizer
        harness.voice.state = "attending"
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(tone(4000), tone(4000))
                async with asyncio.timeout(2):
                    while "hear.final" not in harness.topics():
                        await real_sleep(0.005)
                harness.voice.disarm()
        sent = [fields for topic, fields in harness.sent if topic == "hear.final"]
        return sent[-1], logs.output, recognizer

    async def test_an_alternative_that_is_a_command_replaces_the_transcript(self):
        heard, output, recognizer = await self.dictate(
            "what kind of shit", ["what kind of shit", "what kind of fit", "what kind of hit", "what time is it"])
        self.assertEqual(heard["text"], "what time is it")
        self.assertEqual(recognizer.asked, [10])
        self.assertEqual(recognizer.decoded, [], "the grammar pass is not needed")
        self.assertTrue(any("n-best rescue: alternative 4 of 4" in line for line in output))
        self.assertFalse(any("kind of" in line for line in output), "no words in the log")

    async def test_a_recognized_code_or_device_command_skips_nbest_and_the_grammar(self):
        for transcript, alternative in (("what time is it", "what day is it"),
                                        ("make your screen brighter", "make your screen dimmer")):
            with self.subTest(transcript=transcript):
                heard, _, recognizer = await self.dictate(transcript, [transcript, alternative])
                self.assertEqual(heard["text"], transcript)
                self.assertEqual(recognizer.asked, [])
                self.assertEqual(recognizer.decoded, [])

    async def test_otherwise_the_grammar_pass_still_runs(self):
        heard, _, recognizer = await self.dictate("tell me about the sea", ["tell me about the sea", "tell me a bout"])
        self.assertEqual(heard["text"], "tell me about the sea")
        self.assertEqual(len(recognizer.decoded), 1)

    async def test_a_one_breath_alternative_loses_the_names_leftovers_first(self):
        heard, _, _ = await self.dictate("shake it but time is a",
                                         ["shake it but time is a", "hey pat what time is it"], oneshot=True)
        self.assertEqual(heard["text"], "what time is it")

    async def test_a_failing_nbest_keeps_the_transcript(self):
        class Broken(NbestRecognizer):
            def nbest(self, limit=5):
                raise OSError("no lattice")

        harness = Harness(grammar=CommandGrammar(command_phrases()))
        script = [(True, [("hear.speech_start", {})]), (False, [("hear.final", {"text": "but the tamil"})])]
        harness.recognizer = harness.voice.recognizer = Broken(script, [])
        harness.voice.state = "attending"
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(tone(4000), tone(4000))
                async with asyncio.timeout(2):
                    while "hear.final" not in harness.topics():
                        await real_sleep(0.005)
                harness.voice.disarm()
        self.assertEqual([f["text"] for t, f in harness.sent if t == "hear.final"], ["but the tamil"])
        self.assertTrue(any("reading the n-best transcripts failed" in line for line in logs.output))


class FalseWakes(unittest.IsolatedAsyncioTestCase):
    async def run_script(self, script, frames, *, state="attending", grammar=True, prepare=None):
        harness = Harness(script, grammar=CommandGrammar(command_phrases()) if grammar else None)
        harness.voice.state = state
        if prepare is not None:
            prepare(harness.voice)
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(*frames)
                harness.voice.disarm()
        return harness, logs.output

    async def test_a_short_sound_after_the_name_alone_is_the_room(self):
        harness, output = await self.run_script([WAKE, START, final("Oh.")], [tone(500), tone(4000), tone(10)])
        self.assertEqual(harness.topics(), ["hear.wake", "hear.speech_start", "hear.cancel"])
        self.assertTrue(any("1 word with no command were taken for the room; not passed on" in line
                            for line in output))
        self.assertFalse(any("Oh" in line for line in output), "no words in the log")
        self.assertFalse(harness.voice.expecting)
        self.assertFalse(harness.voice.apology_owed, "nothing is owed for the room")

    async def test_without_the_grammar_pass_the_rule_still_holds(self):
        harness, _ = await self.run_script([WAKE, START, final("and then")], [tone(500), tone(4000), tone(10)],
                                           grammar=False)
        self.assertNotIn("hear.final", harness.topics())

    async def test_commands_kept_words_and_longer_speech_pass(self):
        for text in ("open terminal", "louder", "stop", "Cancel it.", "tell me a story"):
            harness, _ = await self.run_script([WAKE, START, final(text)], [tone(500), tone(4000), tone(10)])
            self.assertIn("hear.final", harness.topics(), text)

    async def test_a_one_breath_request_is_never_dropped(self):
        def taken(voice):
            voice.wake_taken = AsyncMock(return_value=True)

        harness, _ = await self.run_script([ONESHOT_WAKE, final("Thanks.", oneshot=True)], [tone(500), tone(10)],
                                           state="idle", prepare=taken)
        self.assertIn("hear.final", harness.topics())

    async def test_the_one_breath_window_dump_follows_voice_debugging_and_guests(self):
        for transcripts, guest, expected in ((False, False, False), (True, False, True), (True, True, False)):
            seen = {}

            def prepare(voice, transcripts=transcripts, guest=guest, seen=seen):
                voice.wake_taken = AsyncMock(return_value=True)
                voice.transcripts = transcripts
                voice.guest = guest
                voice.recognizer.window_dump_enabled = None
                seen["voice"] = voice

            await self.run_script([ONESHOT_WAKE, final("What time is it?", oneshot=True)], [tone(500), tone(10)],
                                  state="idle", prepare=prepare)
            self.assertIs(seen["voice"].recognizer.window_dump_enabled, expected, (transcripts, guest))

    async def test_a_key_wake_hears_short_words(self):
        # No wake phrase was heard: a key or a touch opened attention.
        harness, _ = await self.run_script([START, final("yes")], [tone(4000), tone(10)])
        self.assertIn("hear.final", harness.topics())

    async def test_once_words_are_passed_on_short_ones_count_again(self):
        harness, _ = await self.run_script(
            [WAKE, START, final("tell me a story"), START, final("thanks")],
            [tone(500), tone(4000), tone(10), tone(4000), tone(10)])
        self.assertEqual([f["text"] for t, f in harness.sent if t == "hear.final"], ["tell me a story", "thanks"])


class FollowUps(unittest.IsolatedAsyncioTestCase):
    async def follow_up(self, frames, script):
        harness = Harness(script)
        voice = harness.voice
        voice.state = "attending"
        voice.followup = True
        voice.dictation_until = math.inf
        # An analog floor, as on the laptop.
        voice.mic.floor = -60.0
        voice.mic.auto = False
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(*frames)
                voice.disarm()
        return harness, logs.output

    async def test_a_follow_up_with_no_voice_over_the_floor_is_the_room(self):
        # 40 is about -58 dBFS: under the floor plus 10 dB.
        harness, output = await self.follow_up([tone(40), tone(40)], [START, final("yes")])
        self.assertEqual(harness.topics(), ["hear.speech_start", "hear.cancel"])
        self.assertTrue(any("taken for the room or the reply's own tail" in line for line in output))

    async def test_a_spoken_follow_up_passes_without_its_name(self):
        harness, _ = await self.follow_up([tone(4000)] * 6 + [tone(10)],
                                          [START] + [(True, [])] * 5 + [final("Hey Pip, what time is it")])
        heard = [fields for topic, fields in harness.sent if topic == "hear.final"]
        self.assertEqual(heard[0]["text"], "what time is it")
        self.assertTrue(heard[0]["followup"])

    async def test_the_reply_leaves_the_microphone_deaf_a_moment(self):
        voice = Harness().voice
        voice.play = AsyncMock()
        voice.synthesize = AsyncMock()
        voice.clauses.put_nowait({"text": "Hello.", "last": True})
        with patch("mind.voice.tempfile.TemporaryDirectory") as folder:
            folder.return_value.__enter__.return_value = "/unused"
            await voice.speak()
        now = voice.now()
        self.assertTrue(voice.silenced_at(now + FOLLOWUP_HOLD_SECONDS - 0.01))
        self.assertFalse(voice.silenced_at(now + FOLLOWUP_HOLD_SECONDS + 0.01))
        self.assertGreaterEqual(FOLLOWUP_HOLD_SECONDS, 0.3)
        self.assertLessEqual(FOLLOWUP_HOLD_SECONDS, 0.5)

    async def test_nothing_played_means_no_hold(self):
        voice = Harness().voice
        voice.play = AsyncMock()
        voice.clauses.put_nowait({"text": "", "last": True})
        voice.chirp_bank = None
        with patch("mind.voice.tempfile.TemporaryDirectory") as folder:
            folder.return_value.__enter__.return_value = "/unused"
            await voice.speak()
        self.assertEqual(voice.deaf_windows, [])


class NightApology(unittest.IsolatedAsyncioTestCase):
    async def miss(self, *, hour=None, lux=None, quiet=None):
        harness = Harness(quiet_hours=quiet)
        voice = harness.voice
        voice.state = "attending"
        voice.apology_owed = True
        voice.apologise = AsyncMock()
        if hour is not None or lux is not None:
            await harness.event("body.state", hour=hour if hour is not None else 12, minute=0, lux=lux)
        with self.assertLogs("being.voice", "INFO") as logs:
            await voice.missed("unclear")
            await real_sleep(0)
            # assertLogs needs one line even when the miss logs none.
            logging.getLogger("being.voice").info("checked")
        return harness, voice, logs.output

    async def test_by_day_in_a_lit_room_it_says_sorry(self):
        harness, voice, _ = await self.miss(hour=12, lux=150.0)
        self.assertIn(("hear.missed", {"reason": "unclear"}), harness.sent)
        self.assertIsNotNone(voice.apology)

    async def test_at_night_or_in_the_dark_it_stays_quiet(self):
        for options, why in (({"hour": 23, "lux": 150.0}, "at night"), ({"hour": 6, "lux": None}, "at night"),
                             ({"hour": 12, "lux": 0.0}, "in the dark"),
                             ({"hour": 12, "lux": DARK_LUX - 1}, "in the dark"),
                             ({"quiet": lambda clock: True}, "in quiet hours")):
            harness, voice, output = await self.miss(**options)
            self.assertIn(("hear.missed", {"reason": "unclear"}), harness.sent, options)
            self.assertIsNone(voice.apology, options)
            self.assertFalse(voice.apology_owed)
            self.assertTrue(any(f"not saying sorry {why}" in line for line in output), (options, output))

    async def test_without_a_clock_or_a_light_sensor_it_says_sorry(self):
        harness, voice, _ = await self.miss()
        self.assertIsNotNone(voice.apology)

    async def test_a_bad_lux_reading_is_no_reading(self):
        harness = Harness()
        for bad in ("dark", True, float("nan"), -3):
            await harness.event("body.state", hour=12, minute=0, lux=bad)
            self.assertIsNone(harness.voice.lux, bad)
        await harness.event("body.state", hour=12, minute=0, lux=40)
        self.assertEqual(harness.voice.lux, 40.0)

    async def test_hear_missed_goes_out_once_per_wake(self):
        harness = Harness()
        voice = harness.voice
        voice.state = "attending"
        voice.apologise = AsyncMock()
        await voice.hear("hear.wake", {})
        await voice.missed("unclear")
        await voice.missed("unclear")
        self.assertEqual([t for t, _ in harness.sent].count("hear.missed"), 1)
        # A new wake may be missed once more.
        await voice.hear("hear.wake", {})
        await voice.missed("no_speech")
        self.assertEqual([t for t, _ in harness.sent].count("hear.missed"), 2)


class KeyMuteAtStart(unittest.IsolatedAsyncioTestCase):
    def voice(self, mind, mixer):
        voice = Voice(Mock(), "Richard", [], "plughw:0,0", settings_dir=Path(mind),
                      settings=load_voice_settings(mind), mixer=mixer)
        voice.bus.send = AsyncMock()
        voice.bus.connected.set()
        return voice

    async def test_a_key_mute_survives_a_voice_restart(self):
        self.assertEqual(SPEAKER_MUTED_MARKER, keys.MUTED_MARKER)
        with tempfile.TemporaryDirectory() as mind:
            save_voice_settings(mind, volume=6, chirps=True)
            (Path(mind) / SPEAKER_MUTED_MARKER).write_text("1\n")
            mixer = Mock(set=AsyncMock(return_value=True), store=AsyncMock(return_value=True))
            voice = self.voice(mind, mixer)
            async with asyncio.TaskGroup() as voice.tasks:
                with self.assertLogs("being.voice", "INFO") as logs:
                    await voice.start_volume()
                mixer.set.assert_awaited_once_with(0, switch=True)
                mixer.store.assert_not_awaited()
                self.assertEqual(load_voice_settings(mind)["volume"], 6, "nothing is saved")
                self.assertEqual(voice.volume, 0)
                await voice.report_volume()
                voice.bus.send.assert_not_awaited()
                self.assertTrue(any("muted by a key; starting muted" in line for line in logs.output))
                self.assertFalse(any("starting at" in line for line in logs.output), "no floor lift either")
                # The mind ends the mute with the level to come back to.
                await voice.handle({"t": "voice.volume", "level": 50})
            self.assertEqual(load_voice_settings(mind)["volume"], 50)

    async def test_without_the_marker_the_stored_level_plays(self):
        with tempfile.TemporaryDirectory() as mind:
            save_voice_settings(mind, volume=62, chirps=True)
            mixer = Mock(set=AsyncMock(return_value=True), store=AsyncMock(return_value=True),
                         playback_muted=AsyncMock(return_value=False))
            voice = self.voice(mind, mixer)
            async with asyncio.TaskGroup() as voice.tasks:
                await voice.start_volume()
            mixer.set.assert_awaited_once_with(62, switch=False)


class Timing(unittest.TestCase):
    def test_a_one_breath_request_spoken_before_its_dictation_is_timed_from_its_words(self):
        voice = Harness().voice
        voice.dictation_began_at = 100.0
        voice.voice_heard_at = 99.2         # the words ended before the dictation began
        self.assertEqual(voice.speech_ended_at(101.5, oneshot=True), 99.2)
        # Not for an ordinary dictation: that sound was before it.
        voice.speech_fell_at = 100.9
        self.assertEqual(voice.speech_ended_at(101.5), 100.9)
        # The dictation's own voiced frames come first.
        voice.voiced_at = 100.6
        self.assertEqual(voice.speech_ended_at(101.5), 100.6)
        # Nothing known: now.
        fresh = Harness().voice
        self.assertEqual(fresh.speech_ended_at(5.0), 5.0)

    def test_measure_stamps_the_capture_time(self):
        voice = Harness().voice
        voice.mic.floor = -60.0
        loud = levels(pcm(tone(4000)))
        voice.measure(loud, True, at=42.0)
        self.assertEqual(voice.voice_heard_at, 42.0)
        self.assertIsNone(voice.voiced_at, "no dictation is open")


class JournalLevels(unittest.TestCase):
    def test_warnings_and_errors_carry_their_priority_in_the_journal(self):
        journal = {"JOURNAL_STREAM": "8:1234"}
        self.assertEqual(journal_prefix(logging.ERROR, journal), "<3>")
        self.assertEqual(journal_prefix(logging.WARNING, journal), "<4>")
        self.assertEqual(journal_prefix(logging.INFO, journal), "<6>")
        self.assertEqual(journal_prefix(logging.DEBUG, journal), "<7>")
        self.assertEqual(journal_prefix(logging.WARNING, {}), "", "a terminal gets plain lines")


class EngineLines(unittest.IsolatedAsyncioTestCase):
    async def test_a_voice_ready_mid_reply_waits_for_the_reply_to_end(self):
        voice = Harness().voice
        reply_done = asyncio.Event()

        async def reply():
            await reply_done.wait()

        voice.reply = asyncio.create_task(reply())
        new = Mock(name="piper")
        new.name = "piper"
        new.close = Mock()
        with self.assertLogs("being.voice", "INFO") as logs:
            switch = asyncio.create_task(voice.use_tts(new, "auto"))
            await real_sleep(0.01)
            self.assertIsNot(voice.tts, new, "not while the reply is spoken")
            reply_done.set()
            await switch
        self.assertIs(voice.tts, new)
        self.assertTrue(any("switching once the reply being spoken ends" in line for line in logs.output))

    async def test_the_choice_is_named_even_when_it_keeps_the_engine(self):
        class Engines:
            def choose_tts(self, choice, state_dir=None):
                return None, None

        voice = Harness(engines=Engines()).voice
        with patch("mind.voice.ENGINE_REFRESH_SECONDS", 3600), self.assertLogs("being.voice", "INFO") as logs:
            task = asyncio.create_task(voice.choose_engines())
            async with asyncio.timeout(2):
                while not any("speech engine: flite (auto)" in line for line in logs.output):
                    await real_sleep(0.005)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class CaptureHighPass(unittest.TestCase):
    def sine(self, frequency, seconds=0.5, amplitude=8000):
        count = int(RATE * seconds)
        return array("h", [round(amplitude * math.sin(2 * math.pi * frequency * i / RATE)) for i in range(count)])

    def rms_db(self, samples):
        return 20 * math.log10(math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768)

    def test_the_rooms_hump_goes_and_speech_stays(self):
        hp = HighPass()
        self.assertEqual(hp.cutoff, HIGHPASS_HZ)
        self.assertAlmostEqual(hp.response_db(HIGHPASS_HZ), -3.0, places=1)
        self.assertLess(hp.response_db(63), -15)
        self.assertLess(hp.response_db(125), -5)
        self.assertGreater(hp.response_db(300), -0.5)
        self.assertGreater(hp.response_db(1000), -0.05)

    def test_filtering_real_samples_follows_the_design(self):
        for frequency, expected in ((80, HighPass().response_db(80)), (1000, 0.0)):
            hp = HighPass()
            raw = self.sine(frequency)
            out = pcm(hp.process(raw))
            # Skip the first 0.1 s while the filter settles.
            settle = RATE // 10
            change = self.rms_db(out[settle:]) - self.rms_db(raw[settle:])
            self.assertAlmostEqual(change, expected, delta=0.3)

    def test_state_carries_across_frames_and_gain_and_clipping_apply(self):
        raw = self.sine(150, 0.06)
        whole = HighPass().process(raw, 2.0)
        split = HighPass()
        halves = split.process(raw[:FRAME_SAMPLES], 2.0) + split.process(raw[FRAME_SAMPLES:], 2.0)
        self.assertEqual(whole, halves)
        loud = pcm(HighPass(300).process(array("h", [32000, -32000] * 240), 4.0))
        self.assertLessEqual(max(loud), 32767)
        self.assertGreaterEqual(min(loud), -32768)
        hp = HighPass()
        hp.process(raw)
        hp.reset()
        self.assertEqual((hp.z1, hp.z2), (0.0, 0.0))

    def test_bad_cutoffs_are_refused(self):
        for cutoff in (0, -5, RATE):
            with self.assertRaises(ValueError):
                HighPass(cutoff)

    def test_the_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            cmdline = Path(folder) / "cmdline"
            cmdline.write_text("quiet being.mic_highpass=off being.mic_highpass=180\n")
            self.assertEqual(highpass_setting({}, cmdline), (180.0, "the kernel command line"))
            self.assertEqual(highpass_setting({"BEING_MIC_HIGHPASS": "0"}, cmdline), (None, "BEING_MIC_HIGHPASS"))
            self.assertEqual(highpass_setting({"BEING_MIC_HIGHPASS": "on"}, cmdline),
                             (HIGHPASS_HZ, "BEING_MIC_HIGHPASS"))
            # Out of range or not a number is ignored: the kernel argument decides.
            self.assertEqual(highpass_setting({"BEING_MIC_HIGHPASS": "5000"}, cmdline)[0], 180.0)
            self.assertEqual(highpass_setting({"BEING_MIC_HIGHPASS": "loud"}, cmdline)[0], 180.0)
            cmdline.write_text("quiet\n")
            self.assertEqual(highpass_setting({}, cmdline), (None, "the default"))
            self.assertEqual(highpass_setting({}, cmdline, default=True), (HIGHPASS_HZ, "the default"))
            self.assertEqual(highpass_setting({}, Path(folder) / "missing"), (None, "the default"))

    def test_the_voice_filters_what_the_recognizer_hears_and_measures_the_raw_frame(self):
        frame = tone(3000)
        plain = Harness().voice
        plain.mic.auto = False
        filtered = Harness(highpass=HighPass()).voice
        filtered.mic.auto = False
        out_plain, level_plain = plain.condition(frame, silenced=False)
        out_filtered, level_filtered = filtered.condition(frame, silenced=False)
        self.assertEqual(level_plain, level_filtered, "levels are the raw frame's")
        self.assertEqual(out_plain, frame)
        self.assertNotEqual(out_filtered, frame)
        # Its own sound is silence, and the filter starts afresh after it.
        filtered.highpass.z1 = 5.0
        self.assertEqual(filtered.condition(frame, silenced=True), (bytes(len(frame)), None))
        self.assertEqual(filtered.highpass.z1, 0.0)

    def test_the_voice_default_is_off(self):
        from mind import voice

        self.assertFalse(voice.HIGHPASS_DEFAULT)


class QuietGrammarSearch(unittest.TestCase):
    def test_the_library_is_quiet_only_for_the_grammar_search(self):
        from tests.test_voice_grammar import FakePocketSphinx

        levels_set = []
        recognizer = Recognizer.__new__(Recognizer)
        recognizer.lib = FakePocketSphinx({recognition.GRAMMAR_SEARCH: (b"open a terminal", -40000),
                                           recognition.PHONE_SEARCH: (b"ah", -52000)})
        recognizer.decoder = object()
        recognizer.dictation = b"_default"
        recognizer.mode = None
        recognizer.command_phrases = ("open a terminal",)
        recognizer.log_posterior = lambda: -0.25

        def set_level(level):
            levels_set.append(level)
            return 3

        recognizer._set_loglevel = set_level
        self.assertIsNotNone(recognizer.decode_command(bytes(2 * RATE)))
        self.assertEqual(levels_set, [recognition.LOG_FATAL, 3], "fatal for the grammar, then back")

    def test_without_the_function_nothing_changes(self):
        recognizer = Recognizer.__new__(Recognizer)
        self.assertIsNone(recognizer._quiet())


if __name__ == "__main__":
    unittest.main()
