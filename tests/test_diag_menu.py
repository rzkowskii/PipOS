"""The Try menu's "Run diagnostics" item: appended, Quick/Full/Voice, summary on screen."""

import io
import json
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mind import local_menu

SUMMARY = """\
PipOS diagnostics (pip-diag)

command: full
started utc: 20261124-181331Z
folder: /run/beinglogs/boot-0001/diag-20261124-181331Z
result: 30 pass, 2 warn, 0 fail, 5 skip

Top problems
  WARN mic.floor: plughw:0,0 at 16000 Hz: left rms -84.0 dBFS; the left channel is dead

In plain English
  The microphone needs a look: the left channel is dead.

Every check
  [pass] system.units
"""


class MenuItem(unittest.TestCase):
    def test_diagnostics_is_appended_and_the_old_numbers_stay(self):
        keys = [entry[0] for entry in local_menu.MENU]
        self.assertEqual(keys, ["1", "2", "3", "4", "5", "6", "7", "8", "Q"])
        self.assertEqual(local_menu.MENU[6][1], "Back up to a USB stick")
        self.assertEqual(local_menu.MENU[7][:2], ("8", "Run diagnostics"))
        self.assertEqual((local_menu.TYPE_CHAT_KEY, local_menu.BACKUP_KEY, local_menu.DIAG_KEY), ("6", "7", "8"))
        self.assertEqual([choice[1] for choice in local_menu.DIAG_CHOICES[:3]], ["Quick", "Full", "Voice"])

    def test_the_try_menu_dispatches_eight(self):
        answers = iter(["8", "q"])
        with patch.object(local_menu, "welcome"), \
                patch.object(local_menu, "idle_input", side_effect=lambda *_: next(answers)), \
                patch.object(local_menu, "diagnostics_menu") as menu, redirect_stdout(io.StringIO()):
            self.assertEqual(local_menu.try_menu(), 0)
        menu.assert_called_once_with()


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run = self.folder / "diag-20261124-181331Z"
        self.run.mkdir()
        self.last = self.folder / "last-run.json"
        self.enterContext(patch.object(local_menu, "DIAG_LAST_RUN", self.last))
        self.enterContext(patch.dict(local_menu.os.environ, {"BEING_CONSOLE_UI": "0"}))

    def finish(self, *_):
        (self.run / "diag.txt").write_text(SUMMARY)
        self.last.write_text(json.dumps({"folder": str(self.run)}))
        return 1

    def test_full_runs_and_its_summary_is_shown(self):
        answers = iter(["2", ""])
        with patch.object(local_menu, "run_diagnostic", side_effect=self.finish) as run, \
                patch("builtins.input", side_effect=lambda *_: next(answers)), redirect_stdout(io.StringIO()) as out:
            local_menu.diagnostics_menu()
        run.assert_called_once_with("full")
        text = out.getvalue()
        self.assertIn("The microphone needs a look", text)
        self.assertIn("WARN mic.floor", text)
        self.assertNotIn("started utc", text)
        self.assertNotIn("[pass] system.units", text)

    def test_back_runs_nothing_and_a_wrong_key_asks_again(self):
        answers = iter(["9", "q"])
        with patch.object(local_menu, "run_diagnostic") as run, \
                patch("builtins.input", side_effect=lambda *_: next(answers)), redirect_stdout(io.StringIO()):
            local_menu.diagnostics_menu()
        run.assert_not_called()

    def test_a_run_that_wrote_nothing_says_so(self):
        answers = iter(["1", ""])
        with patch.object(local_menu, "run_diagnostic", return_value=2), \
                patch("builtins.input", side_effect=lambda *_: next(answers)), redirect_stdout(io.StringIO()) as out:
            local_menu.diagnostics_menu()
        self.assertIn("No diagnostics report was written.", out.getvalue())

    def test_the_child_gets_ctrl_c_and_the_menu_waits(self):
        seen = {}

        class Child:
            def wait(self):
                seen["handler"] = signal.getsignal(signal.SIGINT)
                return 0

        before = signal.getsignal(signal.SIGINT)
        with patch.object(local_menu.subprocess, "Popen", return_value=Child()) as popen:
            self.assertEqual(local_menu.run_diagnostic("voice"), 0)
        self.assertEqual(popen.call_args.args[0][1:], ["-m", "mind.diag", "voice"])
        self.assertNotIn(seen["handler"], (signal.SIG_IGN, signal.default_int_handler))
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    def test_summary_reading(self):
        self.assertEqual(local_menu.diagnostic_summary(self.last), (None, []))
        self.finish()
        folder, lines = local_menu.diagnostic_summary(self.last)
        self.assertEqual(folder, self.run)
        self.assertEqual(lines[0], "PipOS diagnostics (pip-diag)")
        self.assertIn("result: 30 pass, 2 warn, 0 fail, 5 skip", lines)


if __name__ == "__main__":
    unittest.main()
