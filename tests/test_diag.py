"""pip-diag: the report, the exit codes, consent, and the voice always put back.

The fakes here (FakeHost, FakeProcess, FakeJournal) are shared by the other
tests/test_diag_*.py modules. Nothing touches a real sound card, systemd or
journal: every command goes through FakeHost.run, and every path through a
temporary root.
"""

import io
import json
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mind import diag, diag_report, diag_sound, diag_system
from mind.diag_report import Interrupted, Report, check, guarded
from mind.diag_system import Entry, Host, Journal, Result

KEYS = {"id", "section", "status", "value", "evidence", "hint"}


class FakeProcess:
    def __init__(self, argv, host, on_start=None, code=0):
        self.argv, self.host, self.returncode = argv, host, None
        self.code = code
        if on_start is not None:
            on_start(argv)

    def poll(self):
        self.returncode = self.code
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.code

    def communicate(self, timeout=None):
        self.returncode = self.code
        return b"", b""

    def kill(self):
        self.returncode = -9


class FakeHost(Host):
    """A machine made of a temporary root, scripted commands and a clock that only moves on sleep."""

    def __init__(self, root, *, root_user=True, programs=(), handlers=None, active=()):
        super().__init__(root)
        self.root_user = root_user
        self.programs = set(programs)
        self.handlers = dict(handlers or {})
        self.active = set(active)
        self.calls = []
        self.spawned = []
        self.clock = 1000.0

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.programs else None

    def run(self, argv, timeout=30, input=None, env=None):
        self.calls.append(list(argv))
        handler = self.handlers.get(Path(argv[0]).name)
        if handler is not None:
            return handler(list(argv))
        if argv[0] == "systemctl" and "systemctl" in self.programs:
            return self.systemctl(list(argv))
        return None

    def systemctl(self, argv):
        if argv[1] == "is-active":
            return Result(0 if argv[2] in self.active else 3, "active\n" if argv[2] in self.active else "inactive\n")
        if argv[1] == "stop":
            self.active.discard(argv[2])
        elif argv[1] in ("start", "restart"):
            self.active.add(argv[-1])
        elif argv[1] == "--no-block" and argv[2] in ("start", "restart"):
            self.active.add(argv[3])
        elif argv[1] == "list-units":
            return Result(0, "")
        return Result(0, "")

    def spawn(self, argv, **kwargs):
        self.calls.append(list(argv))
        process = FakeProcess(argv, self)
        self.spawned.append(process)
        return process

    def euid(self):
        return 0 if self.root_user else 1000

    def time(self):
        return 1795500000.0

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += max(0.0, seconds)

    def made(self, path, text=""):
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def systemctl_calls(self):
        return [call[1:] for call in self.calls if call and call[0] == "systemctl"]


class FakeJournal(Journal):
    """Entries that appear at given monotonic times; the cursor is an index."""

    def __init__(self, host, timeline=()):
        super().__init__(host)
        self.timeline = list(timeline)  # [(appears_at, Entry)]

    def available(self):
        return True

    def cursor(self):
        visible = [item for item in self.timeline if item[0] <= self.host.monotonic()]
        return str(len(visible))

    def entries(self, after=None, units=(), boot=True):
        start = int(after or 0)
        found = []
        for index, (appears, entry) in enumerate(self.timeline):
            if index < start or appears > self.host.monotonic():
                continue
            if units and entry.unit not in units:
                continue
            found.append(Entry(entry.monotonic, entry.unit, entry.message, str(index + 1)))
        return found


def temporary_root(test):
    folder = Path(test.enterContext(tempfile.TemporaryDirectory()))
    return folder


def quiet():
    return redirect_stdout(io.StringIO())


class Checks(unittest.TestCase):
    def test_a_check_has_exactly_the_report_fields(self):
        item = check("x.y", "x", "pass", {"a": 1}, "evidence", "")
        self.assertEqual(set(item), KEYS)
        json.dumps(item)

    def test_an_unknown_status_reads_as_warn(self):
        self.assertEqual(check("x", "x", "great")["status"], "warn")

    def test_values_that_json_cannot_hold_become_text(self):
        item = check("x", "x", "pass", {object()})
        json.dumps(item)
        self.assertIsInstance(item["value"], str)

    def test_a_broken_check_becomes_a_warn_and_never_raises(self):
        def broken():
            raise KeyError("boom")

        items = guarded("x.broken", "x", broken)
        self.assertEqual(items[0]["status"], "warn")
        self.assertIn("KeyError", items[0]["evidence"])

    def test_an_interruption_is_not_swallowed(self):
        def stopped():
            raise Interrupted(signal.SIGTERM)

        with self.assertRaises(Interrupted):
            guarded("x", "x", stopped)

    def test_exit_code(self):
        self.assertEqual(diag_report.exit_code([check("a", "a", "pass"), check("b", "b", "skip"),
                                                check("c", "c", "warn")]), 0)
        self.assertEqual(diag_report.exit_code([check("a", "a", "pass"), check("b", "b", "fail")]), 1)


class Reports(unittest.TestCase):
    def setUp(self):
        self.folder = temporary_root(self)

    def test_files_and_summary_put_the_worst_first_with_six_verdicts(self):
        out = io.StringIO()
        report = Report(self.folder, {"command": "quick"}, out=out)
        report.add([check("system.units", "system", "pass", None, "fine"),
                    check("mic.floor", "mic", "warn", None, "left rms -84.0", "Use mic_channel=auto."),
                    check("speaker.play.tone", "speaker", "fail", None, "plays at 0.9x", "Wrong speed.")])
        report.finish()
        data = json.loads((self.folder / "diag.json").read_text())
        self.assertEqual(data["counts"], {"pass": 1, "warn": 1, "fail": 1, "skip": 0})
        for item in data["checks"]:
            self.assertEqual(set(item), KEYS)
            self.assertIn(item["status"], diag_report.STATUSES)
        text = (self.folder / "diag.txt").read_text()
        self.assertLess(text.index("FAIL speaker.play.tone"), text.index("WARN mic.floor"))
        self.assertLess(text.index("Top problems"), text.index("In plain English"))
        plain = text.split("In plain English", 1)[1].split("Every check", 1)[0]
        self.assertIn("The microphone needs a look: left rms -84.0", plain)
        self.assertIn("The speakers have a problem", plain)
        for subject in ("Pip's spoken voice", "The wake word", "Spoken commands", "Opening the terminal by voice"):
            self.assertIn(f"{subject} w", plain)  # was/were not tested this time
        self.assertIn("WARN  mic.floor", out.getvalue())

    def test_json_mode_prints_one_check_per_line_and_nothing_else(self):
        out = io.StringIO()
        report = Report(self.folder, {}, stream_json=True, out=out)
        report.say("not for machines")
        report.add([check("a.b", "a", "pass"), check("c.d", "c", "skip")])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line)["id"] for line in lines], ["a.b", "c.d"])

    def test_a_closed_output_ends_the_run_after_saving(self):
        class Closed(io.StringIO):
            def write(self, text):
                raise BrokenPipeError()

        report = Report(self.folder, {}, out=Closed())
        with self.assertRaises(Interrupted):
            report.add([check("a.b", "a", "pass")])
        self.assertEqual(len(json.loads((self.folder / "diag.json").read_text())["checks"]), 1)

    def test_consent_defaults_to_no(self):
        report = Report(self.folder, {}, out=io.StringIO())
        self.assertFalse(report.ask("Keep recordings?", reader=lambda _: ""))
        self.assertFalse(report.ask("Keep recordings?", reader=lambda _: "n"))
        self.assertFalse(report.ask("Keep recordings?", reader=lambda _: (_ for _ in ()).throw(EOFError())))
        self.assertTrue(report.ask("Keep recordings?", reader=lambda _: "y"))
        machine = Report(self.folder, {}, stream_json=True, out=io.StringIO())
        self.assertFalse(machine.ask("Keep recordings?", reader=lambda _: "y"))

    def test_the_command_line_default_keeps_nothing(self):
        args = diag.parser().parse_args(["voice"])
        self.assertFalse(args.yes_keep_recordings)
        self.assertTrue(diag.parser().parse_args(["voice", "--yes-keep-recordings"]).yes_keep_recordings)

    def test_file_caps_and_unsafe_names(self):
        report = Report(self.folder, {}, out=io.StringIO())
        with self.assertRaises(ValueError):
            report.path("../escape.wav")
        report.files = diag_report.MAX_FILES
        self.assertIsNone(report.write_text("more.txt", "x"))
        spill = report.path("loop-x.wav")
        self.assertNotEqual(spill.parent, self.folder)  # the run goes on; the file is not kept
        spill.write_bytes(b"RIFF")
        report.note_file(spill)
        report.finish()
        self.assertFalse(spill.exists())
        self.assertIn("1 evidence files", json.loads((self.folder / "diag.json").read_text())["files_not_kept"])

    def test_output_folder_prefers_the_logs_stick(self):
        stick = self.folder / "boot-0001-x"
        stick.mkdir()
        state = self.folder / "state" / "folder"
        state.parent.mkdir()
        state.write_text(str(stick))
        folder = diag_report.output_folder(None, now=0, state=state, fallback=self.folder / "run")
        self.assertEqual(folder, stick / "diag-19700101-000000Z")
        (state.parent / "off").write_text("")
        folder = diag_report.output_folder(None, now=0, state=state, fallback=self.folder / "run")
        self.assertEqual(folder, self.folder / "run" / "19700101-000000Z")


class VoiceRestore(unittest.TestCase):
    """The voice is always put back: after success, an exception, or a signal."""

    def setUp(self):
        self.root = temporary_root(self)
        self.host = FakeHost(self.root, programs={"systemctl", "alsactl"}, active={diag_sound.VOICE},
                             handlers={"alsactl": lambda argv: Result(0, "")})
        self.host.made("/run/systemd/system/.keep")

    def marker(self):
        return self.host.path(str(diag_sound.MARKER))

    def test_the_voice_is_stopped_inside_and_restarted_after(self):
        with diag_sound.AudioSession(self.host, signals=False):
            self.assertNotIn(diag_sound.VOICE, self.host.active)
            self.assertTrue(self.marker().exists())
            self.assertTrue(json.loads(self.marker().read_text())["voice_was_active"])
        self.assertIn(diag_sound.VOICE, self.host.active)
        self.assertFalse(self.marker().exists())

    def test_an_exception_still_restores(self):
        with self.assertRaises(RuntimeError):
            with diag_sound.AudioSession(self.host, signals=False):
                raise RuntimeError("a check went wrong")
        self.assertIn(diag_sound.VOICE, self.host.active)
        self.assertFalse(self.marker().exists())

    def test_signals_raise_interrupted_and_restore(self):
        for number in diag_sound.SIGNALS:
            with self.subTest(signal=number):
                self.host.active.add(diag_sound.VOICE)
                before = signal.getsignal(number)
                with self.assertRaises(Interrupted) as caught:
                    with diag_sound.AudioSession(self.host):
                        os.kill(os.getpid(), number)
                        for _ in range(100):  # the handler runs between bytecodes
                            pass
                self.assertEqual(caught.exception.signum, number)
                self.assertIn(diag_sound.VOICE, self.host.active)
                self.assertFalse(self.marker().exists())
                self.assertEqual(signal.getsignal(number), before)

    def test_the_diagnostic_mic_drop_in_is_removed_and_the_voice_restarted(self):
        dropin = self.host.path(str(diag_sound.DROPIN))
        with diag_sound.AudioSession(self.host, dropin_env={"BEING_DIAG_MIC": "/run/being-diag/mic.sock"},
                                     signals=False) as session:
            self.assertIn("Environment=BEING_DIAG_MIC=/run/being-diag/mic.sock", dropin.read_text())
            session.start_voice()
        self.assertFalse(dropin.exists())
        calls = self.host.systemctl_calls()
        self.assertIn(["daemon-reload"], calls)
        self.assertEqual(calls[-1], ["--no-block", "restart", diag_sound.VOICE])

    def test_a_voice_that_was_off_stays_off(self):
        self.host.active.clear()
        with diag_sound.AudioSession(self.host, signals=False):
            pass
        self.assertNotIn(diag_sound.VOICE, self.host.active)
        self.assertNotIn(["--no-block", "restart", diag_sound.VOICE], self.host.systemctl_calls())

    def test_the_mixer_is_restored_when_it_was_saved(self):
        with diag_sound.AudioSession(self.host, signals=False) as session:
            self.assertTrue(session.save_mixer())
            self.host.path(str(diag_sound.MIXER_STATE)).write_text("state")
        restores = [call for call in self.host.calls if call[:2] == ["alsactl", "restore"]]
        self.assertEqual(len(restores), 1)

    def test_running_children_are_stopped(self):
        class Running(FakeProcess):
            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        with diag_sound.AudioSession(self.host, signals=False) as session:
            child = session.track(Running(["arecord"], self.host))
        self.assertEqual(child.returncode, -9)

    def test_manual_restore_after_a_killed_run(self):
        self.host.active.clear()
        self.host.made(str(diag_sound.MARKER), json.dumps({"voice_was_active": True, "dropin": True}))
        self.host.made(str(diag_sound.DROPIN), "[Service]\n")
        with quiet() as out:
            code = diag.main(["restore"], host=self.host)
        self.assertEqual(code, 0)
        self.assertIn("restarted the voice", out.getvalue())
        self.assertIn(diag_sound.VOICE, self.host.active)
        self.assertFalse(self.host.path(str(diag_sound.DROPIN)).exists())
        self.assertFalse(self.marker().exists())
        with quiet() as out:
            diag.main(["restore"], host=self.host)
        self.assertIn("nothing to put back", out.getvalue())

    def test_a_voice_the_run_started_on_the_diagnostic_mic_is_stopped_after(self):
        self.host.active.clear()
        dropin = self.host.path(str(diag_sound.DROPIN))
        with diag_sound.AudioSession(self.host, dropin_env={"BEING_DIAG_MIC": "/run/being-diag/mic.sock"},
                                     signals=False) as session:
            session.start_voice()
            self.assertIn(diag_sound.VOICE, self.host.active)
        self.assertFalse(dropin.exists())
        self.assertNotIn(diag_sound.VOICE, self.host.active)  # no voice left on a dead socket
        calls = self.host.systemctl_calls()
        started = calls.index(["restart", diag_sound.VOICE])
        self.assertEqual(calls[started + 1:], [["daemon-reload"], ["stop", diag_sound.VOICE]])

    def test_manual_restore_stops_a_voice_left_on_the_diagnostic_mic(self):
        self.host.made(str(diag_sound.MARKER), json.dumps({"voice_was_active": False, "dropin": True}))
        with quiet():
            diag.main(["restore"], host=self.host)
        self.assertNotIn(diag_sound.VOICE, self.host.active)

    def test_a_voice_between_automatic_restarts_counts_as_wanted(self):
        self.host.active.clear()
        for state in ("activating", "failed"):
            with self.subTest(state=state):
                original = self.host.systemctl

                def systemctl(argv, state=state, original=original):
                    if argv[1] == "is-active" and argv[2] == diag_sound.VOICE and diag_sound.VOICE not in self.host.active:
                        return Result(3, f"{state}\n")
                    return original(argv)

                self.host.systemctl = systemctl
                with diag_sound.AudioSession(self.host, signals=False):
                    self.assertTrue(json.loads(self.marker().read_text())["voice_was_active"])
                self.assertIn(diag_sound.VOICE, self.host.active)
                self.host.active.clear()
                self.host.systemctl = original

    def test_a_stale_marker_from_a_killed_run_is_kept(self):
        # The killed run had stopped the voice and saved the real mixer, then died mid-sweep.
        self.host.active.clear()
        self.host.made(str(diag_sound.MARKER), json.dumps({"voice_was_active": True, "mixer_saved": True,
                                                          "dropin": False, "pid": 999999}))
        state = self.host.made(str(diag_sound.MIXER_STATE), "the mixer before the killed run")
        with diag_sound.AudioSession(self.host, signals=False) as session:
            marker = json.loads(self.marker().read_text())
            self.assertTrue(marker["voice_was_active"])
            self.assertEqual(marker["inherited_from"], 999999)
            self.assertTrue(session.save_mixer())
            self.assertEqual(state.read_text(), "the mixer before the killed run")
        self.assertNotIn(["alsactl", "store"], [call[:2] for call in self.host.calls])
        restores = [call for call in self.host.calls if call[:2] == ["alsactl", "restore"]]
        self.assertEqual(len(restores), 1)
        self.assertIn(diag_sound.VOICE, self.host.active)
        self.assertFalse(self.marker().exists())

    def test_a_signal_during_the_restore_is_honoured_after_it(self):
        original = self.host.systemctl
        sent = []

        def systemctl(argv):
            if argv[1:3] == ["--no-block", "restart"] and not sent:
                sent.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(100):
                    pass
            return original(argv)

        self.host.systemctl = systemctl
        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(Interrupted) as caught:
            with diag_sound.AudioSession(self.host):
                pass
        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertIn(diag_sound.VOICE, self.host.active)  # the restore finished first
        self.assertFalse(self.marker().exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_restore_leaves_a_live_run_alone(self):
        self.host.active.clear()
        self.host.made(str(diag_sound.MARKER), json.dumps({"voice_was_active": True, "dropin": True, "pid": 4242}))
        self.host.made(str(diag_sound.DROPIN), "[Service]\n")
        held = diag.lock(self.host)  # the live run
        try:
            with quiet() as out:
                self.assertEqual(diag.main(["restore"], host=self.host), 0)
            self.assertIn("another pip-diag run (pid 4242)", out.getvalue())
            self.assertTrue(self.host.path(str(diag_sound.DROPIN)).exists())
            self.assertTrue(self.marker().exists())
            self.assertNotIn(diag_sound.VOICE, self.host.active)
            with quiet():
                diag.main(["restore", "--force"], host=self.host)
            self.assertFalse(self.host.path(str(diag_sound.DROPIN)).exists())
            self.assertIn(diag_sound.VOICE, self.host.active)
        finally:
            held.close()

    def test_quiet_restore_prints_nothing_on_stdout(self):
        with quiet() as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(diag.main(["restore", "--quiet"], host=self.host), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.host.made(str(diag_sound.MARKER), json.dumps({"voice_was_active": True}))
        with quiet() as out, redirect_stderr(io.StringIO()) as err:
            diag.main(["restore", "--quiet"], host=self.host)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("restarted the voice", err.getvalue())

    def test_restore_clears_recordings_a_killed_run_left_in_ram(self):
        left = self.host.made(str(diag_sound.SCRATCH / "999999-mic-floor.wav"), "room")
        mine = self.host.made(str(diag_sound.SCRATCH / f"{os.getpid()}-mic-floor.wav"), "room")
        done = diag_sound.restore(self.host)
        self.assertFalse(left.exists())
        self.assertTrue(mine.exists())
        self.assertIn("removed 1 recordings an interrupted run left in RAM", done)

    def test_an_unreadable_marker_still_brings_the_voice_back(self):
        self.host.active.clear()
        self.host.made(str(diag_sound.MARKER), "{not json")
        diag_sound.restore(self.host)
        self.assertIn(diag_sound.VOICE, self.host.active)


class ExitCodes(unittest.TestCase):
    def setUp(self):
        self.root = temporary_root(self)
        self.out = self.root / "out"

    def main(self, argv, host, reader=lambda _: ""):
        with quiet() as out, redirect_stderr(io.StringIO()) as err:
            code = diag.main(argv + ["--out", str(self.out)], host=host, reader=reader)
        return code, out.getvalue(), err.getvalue()

    def test_could_not_run_is_two(self):
        host = FakeHost(self.root, root_user=False)
        for argv in (["mic"], ["hear", "x.wav"], ["speaker"], ["restore"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(argv, host)[0], 2)
        root = FakeHost(self.root)
        self.assertEqual(self.main(["hear"], root)[0], 2)
        self.assertEqual(self.main(["hear", "a.wav", "--phrase", "hello"], root)[0], 2)
        self.assertEqual(self.main(["mic", "--stimulus-level", "0"], root)[0], 2)
        self.assertEqual(self.main(["codec", "--coef", "zz"], root)[0], 2)
        self.assertEqual(self.main(["voice", "--json"], root)[0], 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            diag.main(["bogus"], host=root)
        self.assertEqual(caught.exception.code, 2)

    def test_commands_pass_is_zero_and_writes_the_report(self):
        host = FakeHost(self.root, root_user=False)
        code, out, _ = self.main(["commands"], host)
        self.assertEqual(code, 0)
        data = json.loads((self.out / "diag.json").read_text())
        self.assertEqual(data["command"], "commands")
        self.assertIn("commands.routes", [item["id"] for item in data["checks"]])
        self.assertIn("In plain English", out)

    def test_commands_end_to_end_says_each_phrase_to_the_voice(self):
        from mind import diag_audio

        def flite(argv):
            diag_audio.write_wav(argv[argv.index("-o") + 1], [diag_audio.tone(200, 0.5, 16000)], 16000)
            return Result(0, "")

        host = FakeHost(self.root, programs={"flite"}, handlers={"flite": flite})
        with patch.object(diag.diag_speech, "run_hear", return_value=[]) as hear, \
                patch.object(diag.diag_speech, "_scratch", return_value=str(self.root)):
            code, _, _ = self.main(["commands", "--e2e"], host)
        self.assertEqual(code, 0)
        stimuli = hear.call_args.args[2]
        self.assertEqual([item[0] for item in stimuli], ["p1", "p2", "p3"])
        self.assertEqual([item[3] for item in stimuli], [route for _, route in diag.diag_speech.E2E_PHRASES])

    def test_a_failure_is_one(self):
        host = FakeHost(self.root, root_user=False)
        with patch.object(diag.diag_speech, "check_commands",
                          return_value=[check("commands.routes", "commands", "fail", None, "wrong")]):
            code, _, _ = self.main(["commands"], host)
        self.assertEqual(code, 1)

    def test_an_interrupted_run_is_two_and_still_saves(self):
        host = FakeHost(self.root, root_user=False)
        with patch.object(diag, "commands_section", side_effect=Interrupted(signal.SIGHUP)):
            code, _, _ = self.main(["commands"], host)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((self.out / "diag.json").read_text())["interrupted"], f"signal {signal.SIGHUP}")

    def test_report_shows_the_latest_and_returns_its_code(self):
        host = FakeHost(self.root, root_user=False)
        self.main(["commands"], host)
        with quiet() as out:
            code = diag.main(["report", str(self.out), "--json"], host=host)
        self.assertEqual(code, 0)
        self.assertTrue(all(json.loads(line)["id"] for line in out.getvalue().splitlines()))


class EmptyMachine(unittest.TestCase):
    """No /proc/asound, no sysfs, no tools: every section skips, nothing raises."""

    def test_quick_on_an_empty_machine(self):
        root = temporary_root(self)
        host = FakeHost(root, root_user=True, handlers={
            Path(sys.executable).name: lambda argv: Result(0, json.dumps({name: None for name in argv[6:]}))})

        class NoCortex:
            def request(self, *args):
                raise OSError("connection refused")

        with patch.object(diag_system, "cortex_client", return_value=NoCortex()), \
                patch.object(diag.diag_speech, "open_sphinx", return_value=(None, "no PocketSphinx")):
            with quiet(), redirect_stderr(io.StringIO()):
                code = diag.main(["quick", "--out", str(root / "out")], host=host)
        data = json.loads((root / "out" / "diag.json").read_text())
        statuses = {item["id"]: item["status"] for item in data["checks"]}
        self.assertEqual(code, 1)  # only the cortex, which is really not there
        self.assertEqual([name for name, status in statuses.items() if status == "fail"], ["cortex.health"])
        for name in ("codec.present", "mic.mixer", "body.thermal", "body.battery", "body.network", "system.units",
                     "boot.timing", "system.bus"):
            self.assertEqual(statuses.get(name), "skip", name)
        self.assertFalse(any("the check itself failed" in item["evidence"] for item in data["checks"]))


class SystemdRun(unittest.TestCase):
    def setUp(self):
        self.root = temporary_root(self)
        self.host = FakeHost(self.root, programs={"systemctl", "systemd-run"})
        self.host.made("/run/systemd/system/.keep")
        self.host.made(str(diag.WRAPPER))

    def wrapped(self, argv, env=None):
        args = diag.parser().parse_args(argv)
        with patch.dict(os.environ, env or {}, clear=False):
            if env is None:
                os.environ.pop("PIP_DIAG_UNIT", None)
            return diag.wrap_command(self.host, args, argv)

    def test_audio_runs_go_under_systemd_run_with_a_restore_after(self):
        command = self.wrapped(["speaker", "--json"])
        self.assertEqual(command[0], "systemd-run")
        self.assertIn(f"ExecStopPost={diag.WRAPPER} restore --quiet", command)
        self.assertIn("--setenv=PIP_DIAG_UNIT=1", command)
        self.assertEqual(command[-2:], ["speaker", "--json"])

    def test_not_wrapped_inside_the_unit_or_when_asked_or_without_root(self):
        self.assertIsNone(self.wrapped(["speaker"], env={"PIP_DIAG_UNIT": "1"}))
        self.assertIsNone(self.wrapped(["speaker", "--no-unit"]))
        self.assertIsNone(self.wrapped(["report"]))
        self.host.root_user = False
        self.assertIsNone(self.wrapped(["speaker"]))

    def test_a_second_run_is_refused(self):
        self.host.active.add(f"{diag.UNIT_NAME}.service")
        with self.assertRaises(diag.CannotRun):
            self.wrapped(["mic"])


class NoZlib(unittest.TestCase):
    def test_the_diagnostics_import_without_zlib(self):
        import subprocess

        code = ("import sys\nfor name in ('zlib', 'gzip', 'zipfile'): sys.modules[name] = None\n"
                "import mind.diag, mind.diag_audio, mind.diag_codec, mind.diag_report, mind.diag_sound, "
                "mind.diag_speech, mind.diag_system\nprint('ok')\n")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.stdout.strip(), "ok", result.stderr)

    def test_the_wrapper_runs_the_module(self):
        wrapper = Path(__file__).resolve().parents[1] / "os/package/being/pip-diag"
        text = wrapper.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('PYTHONPATH=/usr/lib/being exec /usr/bin/python3 -m mind.diag "$@"', text)
        self.assertTrue(os.access(wrapper, os.X_OK))


if __name__ == "__main__":
    unittest.main()


class Signals(unittest.TestCase):
    def test_a_signal_outside_the_sound_card_ends_the_run_cleanly_and_handlers_come_back(self):
        root = temporary_root(self)
        host = FakeHost(root, root_user=False)
        before = signal.getsignal(signal.SIGTERM)

        def stopped(*_args, **_kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(100):
                pass
            return []

        with patch.object(diag.diag_speech, "check_commands", side_effect=stopped), quiet(), \
                redirect_stderr(io.StringIO()):
            code = diag.main(["commands", "--out", str(root / "out")], host=host)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((root / "out" / "diag.json").read_text())["interrupted"],
                         f"signal {signal.SIGTERM}")
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
