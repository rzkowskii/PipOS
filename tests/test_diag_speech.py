"""pip-diag's words: routing in-process, voices and their scores, hearing a file, the owner flow."""

import io
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import diag_audio as audio
from mind import diag_speech as speech
from mind.diag_report import Report
from mind.diag_sound import VOICE
from mind.diag_system import Entry, Result
from tests.test_diag import FakeHost, FakeJournal

MIND = speech.MIND


class Routing(unittest.TestCase):
    def test_the_phrase_table_routes_as_the_mind_would(self):
        for text, expected in speech.COMMAND_PHRASES:
            with self.subTest(text=text):
                self.assertTrue(speech.route_matches(speech.predict_route(text), expected),
                                f"{text!r} -> {speech.predict_route(text)}")

    def test_the_end_to_end_phrases_wake_the_voice_and_route_right(self):
        for text, expected in speech.E2E_PHRASES:
            with self.subTest(text=text):
                self.assertTrue(text.startswith("Hey Pip,"))
                self.assertTrue(speech.route_matches(speech.predict_route(text), expected))

    def test_route_matching(self):
        self.assertTrue(speech.route_matches("workshop.open", "workshop.open"))
        self.assertTrue(speech.route_matches("workshop.close", "workshop."))
        self.assertTrue(speech.route_matches("everyday.calc", "code"))
        self.assertFalse(speech.route_matches("model", "code"))
        self.assertFalse(speech.route_matches(None, "workshop."))

    def test_check_commands_reports_a_wrong_route(self):
        items = speech.check_commands(phrases=(("Why is the sky blue?", "workshop.open"),))
        self.assertEqual(items[0]["status"], "fail")
        self.assertIn("expected workshop.open", items[0]["evidence"])

    def test_the_grammar_is_plain_words_without_the_address(self):
        phrases = speech.grammar_phrases()
        self.assertTrue(phrases)
        self.assertTrue(all(phrase == " ".join(audio.words(phrase)) for phrase in phrases))
        self.assertFalse(any(phrase.startswith(("hey pip", "pip ")) for phrase in phrases))
        grammar = speech.jsgf(["open a terminal", "what time is it"])
        self.assertIn("public <command> = [<address>] <phrase>;", grammar)
        self.assertIn("<phrase> = open a terminal | what time is it;", grammar)

    def test_journal_lines(self):
        text, rest = speech.parse_heard('heard: "say \\"hi\\" pip" (engine pocketsphinx, confidence 0.00, '
                                        'log-posterior -9.6)')
        self.assertEqual(text, 'say "hi" pip')
        self.assertIn("engine pocketsphinx", rest)
        self.assertIsNone(speech.parse_heard("heard the wake phrase: score -1805"))
        self.assertEqual(speech.parse_route('mind: route {"route": "workshop.open", "chars": 15}'),
                         {"route": "workshop.open", "chars": 15})
        self.assertIsNone(speech.parse_route("mind: route {broken"))
        self.assertEqual(speech.strip_address("Hey Pip, open a terminal"), "open a terminal")
        self.assertEqual(speech.strip_address("Hey Nova, what time is it?", "Nova"), "what time is it?")
        self.assertEqual(speech.strip_address("open a terminal"), "open a terminal")

    def test_wake_lines_from_every_recognizer_and_while_talking(self):
        for line, score in (("heard the wake phrase: score -1805, peak -30 dBFS", -1805),
                            ("heard the wake phrase in one breath: score -3, ...", -3),
                            ("heard the wake phrase while speaking: score 12, ...", 12),
                            ("heard the wake phrase in one breath while thinking: score -40, ...", -40),
                            ("heard the wake phrase: score unknown, peak -30 dBFS", None)):
            match = speech.WAKE.search(line)
            self.assertIsNotNone(match, line)
            self.assertEqual(speech.wake_score(match), score)


class Voice:
    """A fake voice behind the diagnostic microphone: after each stimulus it logs a line."""

    LEAD = int(0.6 * speech.RATE)

    def __init__(self, host, journal, responses, *, connects=True, listens=True):
        self.host, self.journal = host, journal
        self.responses = list(responses)
        self.connects = connects
        self.listens = listens
        self.until = 0.0
        self.closed = False
        self.heard = []

    def __call__(self, path):  # the server factory
        self.path = path
        return self

    def accept(self, timeout):
        if self.connects:
            self.journal.timeline.append((self.host.clock + 0.5, Entry(self.host.clock + 0.5, VOICE,
                                                                       "diagnostic microphone connected on x")))
            if self.listens:
                self.journal.timeline.append((self.host.clock + 0.7, Entry(
                    self.host.clock + 0.7, VOICE, "wake phrase 'Hey Pip', mic gain 1x, speech engine flite (auto), "
                                                  "transcript text in the log on")))
        return self.connects

    def play(self, samples):
        self.until = max(self.until, self.host.clock) + len(samples) / speech.RATE
        if len(samples) != self.LEAD:
            self.heard.append(len(samples))
            text, route = self.responses.pop(0)
            at = self.until + 1.2
            self.journal.timeline.append((at - 0.8, Entry(at - 0.8, VOICE,
                                                          "heard the wake phrase in one breath: score -1805, ...")))
            if text is None:
                self.journal.timeline.append((at, Entry(at, VOICE, "dictation ended after 2.5 s with no words")))
            else:
                self.journal.timeline.append((at, Entry(at, VOICE, f'heard: "{text}" (engine pocketsphinx, '
                                                                    'confidence 0.00, log-posterior -9.1)')))
                if route:
                    self.journal.timeline.append((at + 0.1, Entry(at + 0.1, MIND,
                                                                  f'mind: route {{"route": "{route}", "chars": 15}}')))
        return self.until

    def close(self):
        self.closed = True


class Session:
    instances = []

    def __init__(self, host, dropin_env=None):
        self.env = dropin_env
        self.started = 0
        self.exited = False
        Session.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def start_voice(self):
        self.started += 1


class Hearing(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.panel = []
        self.host = FakeHost(self.root, programs={"systemctl"}, handlers={"systemctl": self.systemctl})
        self.journal = FakeJournal(self.host)
        self.report = Report(self.root, {}, out=io.StringIO())
        Session.instances = []

    def systemctl(self, argv):
        if argv[1] == "list-units":
            return Result(0, "".join(f"{unit} loaded active running x\n" for unit in self.panel))
        return Result(0, "")

    def hear(self, responses, stimuli=None, **kwargs):
        voice = Voice(self.host, self.journal, responses, connects=kwargs.pop("connects", True),
                      listens=kwargs.pop("listens", True))
        stimuli = stimuli or [("phrase", audio.tone(300, 1.0, speech.RATE), "Hey Pip, open a terminal.",
                               "workshop.open")]
        items = speech.run_hear(self.host, self.report, stimuli, server_factory=voice, journal=self.journal,
                                session_factory=Session, **kwargs)
        return {item["id"]: item for item in items}, voice

    def test_heard_routed_and_the_terminal_opened(self):
        self.journal.timeline.append((0.0, Entry(0, "being-hands.service", "old line")))
        original = self.systemctl

        def opening(argv):
            if argv[1] == "list-units" and self.host.clock > 1005:
                self.panel = ["being-panel-terminal.service"]
            return original(argv)

        self.host.handlers["systemctl"] = opening
        items, voice = self.hear([("open a terminal", "workshop.open")], asr="moonshine")
        self.assertEqual(items["hear.connect"]["status"], "pass")
        self.assertEqual(items["hear.heard.phrase"]["status"], "pass")
        self.assertEqual(items["hear.heard.phrase"]["value"]["latency_s"], 1.2)
        self.assertEqual(items["hear.heard.phrase"]["value"]["wake_score"], -1805)
        self.assertEqual(items["hear.route.phrase"]["status"], "pass")
        self.assertEqual(items["hear.terminal.phrase"]["status"], "pass")
        session = Session.instances[0]
        self.assertEqual(session.env, {"BEING_DIAG_MIC": "/run/being-diag/mic.sock", "BEING_VOICE_DEBUG": "1",
                                       "BEING_ASR": "moonshine"})
        self.assertEqual(session.started, 1)
        self.assertTrue(session.exited and voice.closed)

    def test_connected_alone_is_not_listening(self):
        # A listening line from before the connection (the voice that was replaced) does not count either.
        self.journal.timeline.append((0.0, Entry(0, VOICE, "listening for the wake phrase: noise floor -60 dBFS")))
        items, _ = self.hear([("open a terminal", None)], listens=False, ready_timeout=1.0, route_timeout=1.0)
        self.assertEqual((items["hear.connect"]["status"], items["hear.connect"]["value"]), ("warn", False))
        self.assertIn("did not say it was listening", items["hear.connect"]["evidence"])

    def test_the_ready_match_needs_the_engine_or_listening_line_after_connecting(self):
        match = speech.ready_after_connect()
        lines = ["capture open on hw:0, mono, mic gain 1.00x", "diagnostic microphone connected on /run/x",
                 "mic gain 1.00x", "speech engine: piper (auto)"]
        self.assertEqual([match(Entry(0, VOICE, line)) for line in lines], [False, False, False, True])
        for line in ("listening for the wake phrase: noise floor -60.0 dBFS",
                     "capture open on the diagnostic microphone, mono, mic gain 1.00x",
                     "wake phrase 'Hey Pip', mic gain 1x, speech engine flite (auto), transcript text in the log on"):
            match = speech.ready_after_connect()
            self.assertFalse(match(Entry(0, VOICE, "diagnostic microphone connected on /run/x")))
            self.assertTrue(match(Entry(0, VOICE, line)), line)

    def test_a_one_breath_transcript_without_the_address_is_heard_right(self):
        stimuli = [(f"p{n}", audio.tone(300, 1.0, speech.RATE), text, None)
                   for n, text in enumerate(("Hey Pip, open a terminal.", "Hey Pip, close the terminal.",
                                             "Hey Pip, what time is it?"))]
        items, _ = self.hear([("open a terminal", None), ("close the terminal", None), ("what time is it", None)],
                             stimuli=stimuli, route_timeout=1.0)
        for n in range(3):
            self.assertEqual(items[f"hear.heard.p{n}"]["status"], "pass")
            self.assertEqual(items[f"hear.heard.p{n}"]["value"]["wer"], 0.0)

    def test_another_beings_name_is_also_left_out(self):
        stimuli = [("phrase", audio.tone(300, 1.0, speech.RATE), "Hey Nova, open a terminal.", None)]
        items, _ = self.hear([("open a terminal", None)], stimuli=stimuli, route_timeout=1.0, name="Nova")
        self.assertEqual(items["hear.heard.phrase"]["value"]["wer"], 0.0)

    def test_a_wake_with_no_score_still_counts_as_the_wake(self):
        voice = Voice(self.host, self.journal, [(None, None)])
        original_play = voice.play

        def play(samples):
            ends = original_play(samples)
            for index, (at, entry) in enumerate(self.journal.timeline):
                if "in one breath: score -1805" in entry.message:
                    self.journal.timeline[index] = (at, Entry(at, VOICE, "heard the wake phrase in one breath: "
                                                                          "score unknown, peak -30 dBFS"))
            return ends

        voice.play = play
        items = {item["id"]: item for item in speech.run_hear(
            self.host, self.report, [("phrase", audio.tone(300, 1.0, speech.RATE), None, None)],
            server_factory=voice, journal=self.journal, session_factory=Session)}
        self.assertEqual(items["hear.heard.phrase"]["status"], "fail")
        self.assertNotIn("not even the wake phrase", items["hear.heard.phrase"]["evidence"])
        self.assertIsNone(items["hear.heard.phrase"]["value"]["wake_score"])

    def test_open_a_terminal_already_open_routes_to_say_and_only_warns(self):
        self.panel = ["being-panel-terminal.service"]
        items, _ = self.hear([("open a terminal", "workshop.say")])
        self.assertEqual(items["hear.route.phrase"]["status"], "warn")
        self.assertIn("already open", items["hear.route.phrase"]["evidence"])
        self.assertEqual(items["hear.terminal.phrase"]["status"], "warn")

    def test_nothing_heard_fails(self):
        items, _ = self.hear([(None, None)])
        self.assertEqual(items["hear.heard.phrase"]["status"], "fail")
        self.assertIn("no words", items["hear.heard.phrase"]["evidence"])
        self.assertNotIn("hear.route.phrase", items)

    def test_heard_but_routed_to_the_model_fails_and_the_terminal_stays_shut(self):
        items, _ = self.hear([("a good ten in l", "model")])
        self.assertEqual(items["hear.heard.phrase"]["status"], "warn")
        self.assertEqual(items["hear.route.phrase"]["status"], "fail")
        self.assertEqual(items["hear.terminal.phrase"]["status"], "fail")

    def test_the_try_menu_holding_the_screen_skips_the_terminal_instead_of_failing(self):
        original = self.systemctl

        def menu_up(argv):
            if argv[1] == "is-active":
                return Result(0 if argv[2] == "being-local-screen.service" else 3,
                              "active\n" if argv[2] == "being-local-screen.service" else "inactive\n")
            return original(argv)

        self.host.handlers["systemctl"] = menu_up
        items, _ = self.hear([("open a terminal", "workshop.open")])
        self.assertEqual(items["hear.route.phrase"]["status"], "pass")
        self.assertEqual(items["hear.terminal.phrase"]["status"], "skip")
        self.assertIn("Try menu has the screen", items["hear.terminal.phrase"]["evidence"])

    def test_the_hands_refusal_line_also_explains_a_shut_terminal(self):
        voice = Voice(self.host, self.journal, [("open a terminal", "workshop.open")])
        original_play = voice.play

        def play(samples):
            ends = original_play(samples)
            if len(samples) != Voice.LEAD:
                self.journal.timeline.append((ends + 1.5, Entry(ends + 1.5, "being-hands.service",
                    "hands: terminal not opened: the face is not running while the Try menu has the screen")))
            return ends

        voice.play = play
        stimuli = [("phrase", audio.tone(300, 1.0, speech.RATE), "Hey Pip, open a terminal.", "workshop.open")]
        items = {item["id"]: item for item in speech.run_hear(self.host, self.report, stimuli, server_factory=voice,
                                                              journal=self.journal, session_factory=Session)}
        self.assertEqual(items["hear.terminal.phrase"]["status"], "skip")

    def test_a_terminal_that_failed_to_start_is_not_a_pass(self):
        # hands began the terminal (its unit showed for one look), the start
        # timed out and hands stopped it again: that is a failure.
        original = self.systemctl
        seen = []

        def flicker(argv):
            if argv[1] == "list-units":
                seen.append(self.host.clock)
                self.panel = ["being-panel-terminal.service"] if len(seen) == 2 else []
            return original(argv)

        self.host.handlers["systemctl"] = flicker
        voice = Voice(self.host, self.journal, [("open a terminal", "workshop.open")])
        original_play = voice.play

        def play(samples):
            ends = original_play(samples)
            if len(samples) != Voice.LEAD:
                self.journal.timeline.append((ends + 2.0, Entry(ends + 2.0, "being-hands.service",
                                                                "hands: terminal did not start: TimeoutExpired")))
            return ends

        voice.play = play
        stimuli = [("phrase", audio.tone(300, 1.0, speech.RATE), "Hey Pip, open a terminal.", "workshop.open")]
        items = {item["id"]: item for item in speech.run_hear(self.host, self.report, stimuli, server_factory=voice,
                                                              journal=self.journal, session_factory=Session)}
        self.assertEqual(items["hear.route.phrase"]["status"], "pass")
        self.assertEqual(items["hear.terminal.phrase"]["status"], "fail")
        self.assertIn("did not start", items["hear.terminal.phrase"]["evidence"])

    def test_patience_stretches_the_wait_for_a_slow_voice(self):
        # On an emulated CPU the final comes long after the end of speech.
        voice = Voice(self.host, self.journal, [("open a terminal", None)])
        original_play = voice.play

        def slow(samples):
            ends = original_play(samples)
            self.journal.timeline[:] = [(at + 20.0 if at > ends else at, Entry(at + 20.0, entry.unit, entry.message)
                                         if at > ends else entry) for at, entry in self.journal.timeline]
            return ends

        voice.play = slow
        stimuli = [("phrase", audio.tone(300, 1.0, speech.RATE), "Hey Pip, open a terminal.", None)]

        def run(patience):
            voice.responses = [("open a terminal", None)]
            self.journal.timeline.clear()
            return {item["id"]: item for item in speech.run_hear(
                self.host, self.report, stimuli, server_factory=voice, journal=self.journal,
                session_factory=Session, route_timeout=1.0, patience=patience)}

        self.assertEqual(run(1.0)["hear.heard.phrase"]["status"], "fail")
        self.assertEqual(run(3.0)["hear.heard.phrase"]["status"], "pass")

    def test_a_voice_without_the_diagnostic_mic_fails_to_connect_and_still_restores(self):
        items, voice = self.hear([], connects=False)
        self.assertEqual(items["hear.connect"]["status"], "fail")
        self.assertTrue(Session.instances[0].exited and voice.closed)

    def test_several_stimuli_in_one_voice_restart(self):
        stimuli = [(f"p{n}", audio.tone(300, 0.5 + n * 0.2, speech.RATE), None, None) for n in range(3)]
        items, voice = self.hear([("one", None), ("two", None), ("three", None)], stimuli=stimuli,
                                 route_timeout=1.0)
        self.assertEqual(len(voice.heard), 3)
        self.assertEqual(Session.instances[0].started, 1)
        self.assertEqual([items[f"hear.heard.p{n}"]["value"]["text"] for n in range(3)], ["one", "two", "three"])


class MicServer(unittest.TestCase):
    def test_the_socket_streams_queued_samples_between_quiet_frames(self):
        folder = Path(tempfile.mkdtemp(prefix="pdmic"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        server = speech.MicServer(folder / "mic.sock", sleep=lambda seconds: time.sleep(min(seconds, 0.0005)))
        self.addCleanup(server.close)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(folder / "mic.sock"))
        self.assertTrue(server.accept(2.0))
        marker = audio.to_pcm([0.9] * 960)
        server.play(marker)
        received = b""
        deadline = time.monotonic() + 5
        while marker.tobytes() not in received and time.monotonic() < deadline:
            received += client.recv(65536)
        self.assertIn(marker.tobytes(), received)
        self.assertEqual(len(received) % 2, 0)
        self.assertEqual(oct(folder.joinpath("mic.sock").stat().st_mode & 0o777), "0o600")


class Voices(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.report = Report(self.root, {}, out=io.StringIO())

    def flite(self, argv):
        if argv[1] == "-lv":
            return Result(0, "Voices available: kal awb_time kal16 awb rms slt\n")
        path = argv[argv.index("-o") + 1]
        audio.write_wav(path, [audio.tone(200, 1.0, 16000)], 16000)
        return Result(0, "")

    class Sphinx:
        def __init__(self, texts):
            self.texts = list(texts)

        def dictate(self, samples):
            return {"text": self.texts.pop(0), "confidence": None}

    def test_every_voice_is_rendered_kept_timed_and_scored(self):
        host = FakeHost(self.root, programs={"flite"}, handlers={"flite": self.flite})
        sentences = ("Here's my terminal.", "Sorry, I didn't catch that.")
        sphinx = self.Sphinx(["here's my terminal", "sorry i didn't catch that", "here's my", "sorry"])
        with patch.object(speech, "piper_engine", return_value=(None, "the natural voice is not installed")):
            items = {item["id"]: item for item in speech.check_tts(host, self.report, sphinx=sphinx,
                                                                  sentences=sentences)}
        self.assertEqual(items["tts.voices"]["value"], ["slt", "kal16"])
        self.assertEqual(items["tts.digital.slt"]["status"], "pass")
        self.assertEqual(items["tts.digital.kal16"]["status"], "warn")
        self.assertIn("tts.rtf.slt", items)
        self.assertTrue((self.root / "tts-slt-1.wav").exists())  # Pip's own voice is kept

    def test_no_flite_skips(self):
        with patch.object(speech, "piper_engine", return_value=(None, "no")):
            self.assertEqual(speech.check_tts(FakeHost(self.root), self.report)[0]["status"], "skip")

    def test_a_crashing_moonshine_is_reported_not_fatal(self):
        host = FakeHost(self.root, handlers={Path(speech.sys.executable).name: lambda argv: Result(-4, "", "")})
        self.assertIn("signal 4", speech.moonshine(host, ["a.wav"])["error"])

    def test_a_phrase_is_rendered_on_pip(self):
        host = FakeHost(self.root, programs={"flite"}, handlers={"flite": self.flite})
        with patch.object(speech, "_scratch", return_value=str(self.root)):
            samples, wav = speech.stimulus_from_phrase(host, "Hey Pip, open a terminal")
        self.assertEqual(len(samples), 16000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual([path for path in self.root.iterdir() if path.name.startswith("pip-diag-")], [])


class OwnerFlow(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.report = Report(self.root / "report", {}, out=io.StringIO())
        (self.root / "report").mkdir()

    def test_without_consent_owner_speech_never_reaches_the_report(self):
        with patch.object(speech, "_scratch", return_value=str(self.root)):
            keeper = speech.Keeper(self.report, consent=False)
        path = keeper.path("owner-command-1.wav")
        self.assertNotEqual(path.parent, self.report.folder)
        path.write_bytes(b"RIFF")
        keeper.done(path)
        self.assertFalse(path.exists())
        self.assertIsNone(keeper.text("open a terminal"))
        keeper.close()
        self.assertFalse(Path(keeper.scratch).exists())
        kept = speech.Keeper(self.report, consent=True)
        self.assertEqual(kept.path("x.wav").parent, self.report.folder)
        self.assertEqual(kept.text("open a terminal"), "open a terminal")

    def test_wake_trials_against_the_gate(self):
        host = FakeHost(self.root)
        journal = FakeJournal(host)
        trial = {"n": 0}

        def owner_says(_prompt):
            trial["n"] += 1
            if trial["n"] != 4:  # one miss in ten
                at = host.clock + 0.8
                journal.timeline.append((at, Entry(at, VOICE, "heard the wake phrase: score -1700, peak -30 dBFS")))
                journal.timeline.append((at + 2, Entry(at + 2, VOICE, "dictation ended after 1.5 s with no words")))
            return ""

        item = speech.wake_trials(host, self.report, owner_says, journal, trials=10)
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["value"]["heard"], 9)
        self.assertEqual(item["value"]["margins"][0], -200)

    def test_wake_trials_count_wakes_without_a_score(self):
        host = FakeHost(self.root)
        journal = FakeJournal(host)

        def owner_says(_prompt):
            at = host.clock + 0.8
            journal.timeline.append((at, Entry(at, VOICE, "heard the wake phrase: score unknown, peak -30 dBFS")))
            journal.timeline.append((at + 2, Entry(at + 2, VOICE, "dictation ended after 1.5 s with no words")))
            return ""

        item = speech.wake_trials(host, self.report, owner_says, journal, trials=10)
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["value"]["heard"], 10)
        self.assertEqual(item["value"]["scores"], [])

    def test_listening_ratings(self):
        host = FakeHost(self.root, programs={"aplay"})
        path = self.root / "a.wav"
        audio.write_wav(path, [audio.tone(200, 0.2, 16000)], 16000)
        answers = iter(["", "2", "", "y"])
        item = speech.listening(host, self.report, lambda _: next(answers), [("slt", path)], path)
        self.assertEqual(item["status"], "warn")
        self.assertEqual(item["value"], {"ratings": {"slt": 2}, "channels_right": True})


if __name__ == "__main__":
    unittest.main()
