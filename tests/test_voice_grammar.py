"""The command grammar second pass: when it runs, what it may accept, and what the log says."""

import asyncio
import ctypes as c
import math
import unittest
from collections import deque
from unittest.mock import patch

from mind import recognition
from mind.recognition import (
    FRAME_BYTES, GRAMMAR_SEARCH, PHONE_SEARCH, RATE, SKIP, WAKE_PREROLL_FRAMES, Recognizer, command_jsgf,
    find_command, grammar_words,
)
from mind.voice import (
    FALLBACK_PHRASES, GRAMMAR_MARGIN, GRAMMAR_SAFE_KINDS, CommandGrammar, command_grammar, command_phrases,
)
from tests.test_voice_feedback import START, Harness, ScriptedRecognizer, tone

PHRASES = ("open a terminal", "open the terminal", "close the terminal", "show me the clock")


class GrammarText(unittest.TestCase):
    def test_words_are_plain_lowercase(self):
        self.assertEqual(grammar_words("open a terminal"), ["open", "a", "terminal"])
        self.assertEqual(grammar_words("show me how you're feeling"), ["show", "me", "how", "you're", "feeling"])
        for bad in ("Open a terminal", "open; rm", "", "  ", None, "open <x>", "a | b"):
            self.assertIsNone(grammar_words(bad), bad)

    def test_the_grammar_holds_each_command_once_with_optional_address(self):
        text = command_jsgf(["open a terminal", "open a terminal", "close the terminal", "Bad | phrase"])
        self.assertTrue(text.startswith("#JSGF V1.0;"))
        self.assertIn("<command> = close the terminal | open a terminal;", text)
        self.assertIn("public <utterance> = [hey | hi | okay] [pip] <command> [please | now];", text)
        with self.assertRaises(ValueError):
            command_jsgf(["Not Valid"])

    def test_only_a_whole_command_counts(self):
        self.assertEqual(find_command("open a terminal", PHRASES), ("open a terminal", 0))
        self.assertEqual(find_command("hey pip open the terminal please", PHRASES), ("open the terminal", 0))
        self.assertEqual(find_command("um open a terminal", PHRASES), ("open a terminal", 1))
        self.assertIsNone(find_command("hi hide the", PHRASES))
        self.assertIsNone(find_command("open a", PHRASES))
        self.assertIsNone(find_command("", PHRASES))
        self.assertIsNone(find_command(None, PHRASES))
        # The longest command wins when one holds another.
        self.assertEqual(find_command("open a terminal window", PHRASES + ("open a terminal window",)),
                         ("open a terminal window", 0))


class Policy(unittest.TestCase):
    def grammar(self, **options):
        return CommandGrammar(command_phrases(), **options)

    def test_phrases_come_from_app_commands_or_the_fallback(self):
        from mind import app_commands

        phrases = command_phrases()
        self.assertIn("open a terminal", phrases)
        with patch.object(app_commands, "grammar_phrases", side_effect=RuntimeError("broken"), create=True):
            self.assertEqual(command_phrases(), list(FALLBACK_PHRASES))
        with patch.object(app_commands, "grammar_phrases", return_value=[], create=True):
            self.assertEqual(command_phrases(), list(FALLBACK_PHRASES))
        with patch.object(app_commands, "grammar_phrases", return_value=["open a terminal", 3, ""], create=True):
            self.assertEqual(command_phrases(), ["open a terminal"])

    def test_every_fallback_phrase_is_a_safe_command(self):
        grammar = self.grammar()
        for phrase in FALLBACK_PHRASES:
            self.assertEqual(grammar_words(phrase), phrase.split(), phrase)
            self.assertTrue(grammar.safe(phrase), phrase)

    def test_every_offered_phrase_is_a_safe_command_in_every_context(self):
        grammar = self.grammar()
        self.assertEqual([phrase for phrase in command_phrases() if not grammar.safe(phrase)], [])

    def test_destructive_and_unknown_phrases_are_never_safe(self):
        grammar = self.grammar()
        for phrase in ("install snake", "play snake", "uninstall it", "run it", "clear the workshop", "yes", "cancel that",
                       "write a script that prints hello", "forget that script", "tell me a joke"):
            self.assertFalse(grammar.safe(phrase), phrase)
        self.assertTrue(GRAMMAR_SAFE_KINDS.isdisjoint({"task", "install", "uninstall", "run_app", "control",
                                                       "answer", "forget_script", "clear_workshop"}))

    def test_a_transcript_that_already_is_a_command_is_left_alone(self):
        grammar = self.grammar()
        self.assertTrue(grammar.is_command("open a terminal"))
        self.assertTrue(grammar.is_command("hey pip, show me your face"))   # a command with the terminal up
        self.assertFalse(grammar.is_command("but the tamil"))
        with patch("mind.app_commands.parse", side_effect=RuntimeError("broken")):
            self.assertTrue(grammar.is_command("anything"), "an unreadable check never triggers a re-read")

    def test_judge_rejects_empty_partial_low_and_unsafe_results(self):
        grammar = CommandGrammar(PHRASES + ("install snake",))
        good = {"text": "open a terminal", "log_posterior": -2.4, "margin": 13.2}
        self.assertEqual(grammar.judge(good, "can't open a terminal"), ("open a terminal", None))
        for result, why in (
            (None, "no command fits"),
            ({"text": "", "log_posterior": None, "margin": None}, "no command fits"),
            ({**good, "text": "hi hide the"}, "not a whole command"),
            ({**good, "text": "um open a terminal"}, "words around the command"),
            ({**good, "margin": GRAMMAR_MARGIN - 0.1}, "under"),
            ({**good, "margin": None}, "under"),
            ({**good, "margin": math.nan}, "under"),
            ({**good, "text": "install snake"}, "not a safe command"),
        ):
            phrase, reason = grammar.judge(result, "install snake" if result and result.get("text") == "install snake"
                                           else "can't open a terminal")
            self.assertIsNone(phrase, result)
            self.assertIn(why, reason)

    def test_the_grammar_only_tidies_a_transcript_that_holds_the_command(self):
        grammar = self.grammar()

        def judged(transcript, text, margin=13.0):
            return grammar.judge({"text": text, "log_posterior": -0.5, "margin": margin}, transcript)

        # Readings of the native decoder (L4's fixtures and ordinary sentences,
        # said while attending): the grammar's nearest command, often with a
        # margin well over the threshold, has nothing to do with the words.
        for transcript, text, margin in (
            ("tell me about the sea", "show me your body", 15.2),
            ("tell me about the sea", "show me your body", 11.7),
            ("close the door", "close the clock", 9.8),
            ("show me the photos", "show me the clock now", 22.0),
            ("can you show me something", "open new terminal", 13.3),
            ("hello there how are you today", "hey open your clock please", 14.0),
            ("what time is it", "launch terminal please", 14.0),
            ("open the window", "open the monitor now", 14.0),
            ("start a new game", "start a new terminal", 14.0),
            ("show me what you can do", "show me how you're doing", 14.0),
            ("but the tamil", "open a terminal", 14.0),
            (None, "open a terminal", 14.0),
        ):
            phrase, why = judged(transcript, text, margin)
            self.assertIsNone(phrase, (transcript, text))
            self.assertTrue(why, (transcript, text))
        # Nothing may turn the command around, and a sentence is not a command.
        for transcript in ("don't open a terminal", "please do not open the terminal", "never close the clock",
                           "i can't open a terminal", "no open the terminal",
                           "open a terminal and tell me about the sea", "why would you open a terminal today"):
            self.assertIsNone(judged(transcript, "open a terminal")[0], transcript)
        # The one-breath slips the grammar is for: a fragment of the name, a
        # dropped article, a one-letter slip in the noun, punctuation.
        for transcript, text in (
            ("can't open a terminal", "pip open a terminal"),
            ("get open a terminal", "pip open a terminal"),
            ("can't open the terminal", "open a terminal"),
            ("open germinal", "open a terminal"),
            ("hey pip, close the clocks.", "close the clock"),
            ("show me you're body", "show me your body"),
        ):
            phrase, why = judged(transcript, text)
            self.assertIsNotNone(phrase, (transcript, text, why))

    def test_words_and_slips(self):
        from mind.voice import edits, near, spoken_words

        self.assertEqual(spoken_words("Hey Pip, can't open a TERMINAL."), ["hey", "pip", "can't", "open", "a", "terminal"])
        self.assertEqual(spoken_words(None), [])
        self.assertEqual((edits("terminal", "germinal"), edits("", "abc"), edits("door", "clock")), (1, 3, 4))
        self.assertTrue(near("clocks", "clock"))
        self.assertFalse(near("do", "doing"))
        self.assertFalse(near("in", "on"), "short words must be exact")
        self.assertTrue(near("terminals", "terminal") and near("termnl", "terminal"))
        self.assertFalse(near("tamil", "terminal"))

    def test_the_margin_is_configurable_and_the_pass_can_be_turned_off(self):
        class Loads:
            wake_phrase = "Hey Pip"

            def load_command_grammar(self, phrases):
                return tuple(phrases)[:3]

        loaded = command_grammar(Loads(), {"BEING_COMMAND_GRAMMAR_MARGIN": "9.5"})
        self.assertEqual((loaded.margin, len(loaded.phrases), loaded.name), (9.5, 3, "pip"))
        with self.assertLogs("being.voice", "WARNING"):
            self.assertEqual(command_grammar(Loads(), {"BEING_COMMAND_GRAMMAR_MARGIN": "lots"}).margin,
                             GRAMMAR_MARGIN)
        self.assertIsNone(command_grammar(Loads(), {"BEING_COMMAND_GRAMMAR": "0"}))
        self.assertIsNone(command_grammar(object(), {}), "an engine without the grammar has no second pass")

        class Fails(Loads):
            def load_command_grammar(self, phrases):
                return ()

        self.assertIsNone(command_grammar(Fails(), {}))


class GrammarRecognizer(ScriptedRecognizer):
    """A scripted dictation whose audio the grammar reads as `result`."""

    def __init__(self, script, result, audio=b"\x01\x00" * RATE):
        super().__init__(script)
        self.result = result
        self.audio = audio
        self.decoded = []

    def scores(self):
        return 0.0001, -9.2

    def utterance_audio(self):
        return self.audio

    def decode_command(self, audio):
        self.decoded.append(audio)
        return self.result


class SecondPass(unittest.IsolatedAsyncioTestCase):
    async def dictate(self, text, result, *, transcripts=False, audio=b"\x01\x00" * RATE, oneshot=False):
        flags = {"oneshot": True} if oneshot else {}
        script = [(True, [("hear.speech_start", flags)]), (False, [("hear.final", {"text": text, **flags})])]
        harness = Harness(grammar=CommandGrammar(command_phrases()), transcripts=transcripts)
        harness.recognizer = harness.voice.recognizer = GrammarRecognizer(script, result, audio)
        harness.voice.state = "attending"
        with self.assertLogs("being.voice", "INFO") as logs:
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(tone(4000), tone(4000))
                async with asyncio.timeout(2):
                    while "hear.final" not in harness.topics():
                        await asyncio.sleep(0.005)
                harness.voice.disarm()
        final = [fields for topic, fields in harness.sent if topic == "hear.final"]
        return harness, final[-1] if final else None, logs.output

    async def test_a_misheard_command_is_replaced_by_the_grammar_reading(self):
        harness, final, output = await self.dictate(
            "can't open a terminal", {"text": "open a terminal", "log_posterior": -0.5, "margin": 12.0},
            transcripts=True)
        self.assertEqual(final, {"text": "open a terminal", "speaker": "Richard"})
        self.assertEqual(len(harness.recognizer.decoded), 1)
        self.assertTrue(any("second pass: matched a command of 3 words (margin 12.0" in line for line in output))
        heard = next(line for line in output if "heard: " in line)
        self.assertIn('heard: "open a terminal" (engine pocketsphinx grammar, confidence 0.61, '
                      'log-posterior -0.50)', heard)

    async def test_a_weak_reading_keeps_the_transcript_and_says_why(self):
        harness, final, output = await self.dictate(
            "go somewhere and do something",
            {"text": "close the monitor please", "log_posterior": -4.0, "margin": 3.5})
        self.assertEqual(final["text"], "go somewhere and do something")
        line = next(line for line in output if "second pass: no match" in line)
        self.assertIn("margin 3.5 under 6", line)
        self.assertFalse(any("go somewhere" in line or "monitor" in line for line in output),
                         "no words in the log unless voice debugging is on")

    async def test_an_ordinary_sentence_is_never_turned_into_a_command(self):
        # L4's attending fixture: the open-vocabulary words must reach the mind.
        harness, final, output = await self.dictate(
            "tell me about the sea", {"text": "show me your body", "log_posterior": -0.4, "margin": 13.1})
        self.assertEqual(final["text"], "tell me about the sea")
        self.assertTrue(any("second pass: no match (the transcript does not hold the command's words)" in line
                            for line in output))

    async def test_an_unsafe_reading_is_never_accepted(self):
        harness, final, output = await self.dictate(
            "clear the work shop", {"text": "clear the workshop", "log_posterior": -0.1, "margin": 30.0})
        self.assertEqual(final["text"], "clear the work shop")

    async def test_a_command_transcript_skips_the_second_pass(self):
        harness, final, _ = await self.dictate("open a terminal", {"text": "close the terminal", "margin": 40.0})
        self.assertEqual(final["text"], "open a terminal")
        self.assertEqual(harness.recognizer.decoded, [])

    async def test_long_dictations_and_empty_audio_are_not_read_again(self):
        harness, final, _ = await self.dictate("tell me a long story about the sea",
                                               {"text": "open a terminal", "margin": 40.0},
                                               audio=bytes(2 * RATE * 5))
        self.assertEqual(final["text"], "tell me a long story about the sea")
        self.assertEqual(harness.recognizer.decoded, [])
        harness, final, _ = await self.dictate("but the tamil", {"text": "open a terminal", "margin": 40.0},
                                               audio=b"")
        self.assertEqual(final["text"], "but the tamil")

    async def test_one_breath_commands_get_the_second_pass_too(self):
        harness, final, _ = await self.dictate(
            "get open a terminal", {"text": "open a terminal", "log_posterior": -0.2, "margin": 15.0}, oneshot=True)
        self.assertEqual(final, {"text": "open a terminal", "oneshot": True, "speaker": "Richard"})

    async def test_the_open_vocabulary_scores_are_logged_in_the_log_domain(self):
        _, final, output = await self.dictate("but the tamil", None, transcripts=True)
        self.assertEqual(final["text"], "but the tamil")
        ended = next(line for line in output if "dictation ended" in line)
        self.assertIn("confidence 0.00, log-posterior -9.20", ended)
        heard = next(line for line in output if "heard: " in line)
        self.assertIn('heard: "but the tamil" (engine pocketsphinx, confidence 0.00, log-posterior -9.20)', heard)

    async def test_a_cancelled_capture_waits_for_the_decoder(self):
        voice = Harness().voice
        started, finish = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        done = []

        def decode(audio):
            loop.call_soon_threadsafe(started.set)
            asyncio.run_coroutine_threadsafe(finish.wait(), loop).result(2)
            done.append(audio)
            return None

        task = asyncio.create_task(voice.decode_off_loop(decode, b"ab"))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done(), "still waiting for the decode to end")
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(done, [b"ab"])


class FakePocketSphinx:
    """Just enough of libpocketsphinx for the grammar search and the phone loop."""

    def __init__(self, hyps):
        self.hyps = hyps            # search name -> (text, score)
        self.active = None
        self.calls = []

    def ps_activate_search(self, decoder, name):
        self.active = name
        self.calls.append(("activate", name))
        return 0

    def ps_start_utt(self, decoder):
        return 0

    def ps_end_utt(self, decoder):
        return 0

    def ps_process_raw(self, decoder, samples, count, no_search, full):
        self.calls.append(("process", self.active, count, full))
        return 0

    def ps_get_hyp(self, decoder, score):
        text, value = self.hyps[self.active]
        score._obj.value = value
        return text


class DecodeCommand(unittest.TestCase):
    def recognizer(self, hyps):
        recognizer = Recognizer.__new__(Recognizer)
        recognizer.lib = FakePocketSphinx(hyps)
        recognizer.decoder = object()
        recognizer.dictation = b"_default"
        recognizer.mode = None
        recognizer.command_phrases = PHRASES
        recognizer.log_posterior = lambda: -0.25
        return recognizer

    def test_the_margin_is_the_grammar_path_over_the_phone_loop_per_frame(self):
        recognizer = self.recognizer({GRAMMAR_SEARCH: (b"open a terminal", -40000), PHONE_SEARCH: (b"ah", -52000)})
        audio = bytes(2 * RATE)       # one second: 100 frames
        result = recognizer.decode_command(audio)
        self.assertEqual(result, {"text": "open a terminal", "log_posterior": -0.25, "margin": 120.0})
        self.assertEqual(recognizer.lib.calls[-1], ("activate", b"_default"), "dictation is active again")
        self.assertIn(("process", GRAMMAR_SEARCH, RATE, 1), recognizer.lib.calls)

    def test_no_final_state_means_no_text_and_no_phone_loop(self):
        recognizer = self.recognizer({GRAMMAR_SEARCH: (None, 0), PHONE_SEARCH: (b"ah", -1)})
        self.assertEqual(recognizer.decode_command(bytes(3200)), {"text": "", "log_posterior": None, "margin": None})
        self.assertNotIn(("activate", PHONE_SEARCH), recognizer.lib.calls)

    def test_never_during_a_search_or_without_a_grammar(self):
        recognizer = self.recognizer({GRAMMAR_SEARCH: (b"open a terminal", 0), PHONE_SEARCH: (b"", 0)})
        recognizer.mode = b"wake"
        self.assertIsNone(recognizer.decode_command(bytes(3200)))
        recognizer.mode = SKIP
        self.assertIsNotNone(recognizer.decode_command(bytes(3200)))
        recognizer.mode = None
        recognizer.command_phrases = ()
        self.assertIsNone(recognizer.decode_command(bytes(3200)))
        self.assertEqual(recognizer.lib.calls[-1][0], "activate")


class UtteranceAudio(unittest.TestCase):
    def test_the_dictation_frames_are_kept_and_replaced_by_the_next(self):
        recognizer = Recognizer.__new__(Recognizer)

        class Lib:
            def ps_activate_search(self, decoder, mode):
                return 0

            def ps_start_utt(self, decoder):
                return 0

            def ps_process_raw(self, *args):
                return 0

        recognizer.lib = Lib()
        recognizer.decoder = object()
        recognizer.dictation = b"_default"
        recognizer.capture_frames = 1
        recognizer.wake_preroll = deque(maxlen=WAKE_PREROLL_FRAMES + 1)
        frame = (c.c_int16 * (FRAME_BYTES // 2))(*range(FRAME_BYTES // 2))
        recognizer.begin(b"wake")
        recognizer.decode(frame)
        self.assertEqual(recognizer.utterance_audio(), b"", "wake-search audio is not dictation")
        recognizer.begin(b"_default")
        recognizer.decode(frame)
        recognizer.decode(frame)
        self.assertEqual(recognizer.utterance_audio(), bytes(frame) * 2)
        recognizer.begin(b"_default")
        self.assertEqual(recognizer.utterance_audio(), b"")


class LoadGrammar(unittest.TestCase):
    def test_unknown_words_drop_their_phrase_and_a_missing_phone_model_turns_it_off(self):
        import tempfile
        from pathlib import Path

        model = Path(self.enterContext(tempfile.TemporaryDirectory()))
        added = {}
        recognizer = Recognizer.__new__(Recognizer)
        recognizer.decoder = object()
        recognizer.model = model
        recognizer._lookup_word = lambda decoder, word: 1 if word != b"tamil" else None
        recognizer._ckd_free = lambda pointer: None
        recognizer._add_allphone = lambda decoder, name, path: added.setdefault(name, path) and 0
        recognizer._add_jsgf = lambda decoder, name, text: added.setdefault(name, text) and 0
        self.assertEqual(recognizer.load_command_grammar(["open a terminal"]), (), "no phone loop model")
        (model / recognition.PHONE_LM).write_bytes(b"")
        self.assertEqual(recognizer.load_command_grammar(["open a terminal", "open the tamil", "Bad"]),
                         ("open a terminal",))
        self.assertIn(b"open a terminal;", added[GRAMMAR_SEARCH])
        self.assertNotIn(b"tamil", added[GRAMMAR_SEARCH])
        self.assertTrue(added[PHONE_SEARCH].endswith(recognition.PHONE_LM.encode()))
        recognizer._add_jsgf = lambda decoder, name, text: -1
        recognizer.command_phrases = ()
        self.assertEqual(recognizer.load_command_grammar(["open a terminal"]), ())


if __name__ == "__main__":
    unittest.main()
