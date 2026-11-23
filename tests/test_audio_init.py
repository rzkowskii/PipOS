"""being-audio-init: working mixer levels before the voice listens, or the owner's kept ones."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_usb_logs import FAKE_AMIXER, MBP92_SCONTENTS, SELECTOR_SCONTENTS

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "os/package/being/being-audio-init"
UNIT = REPO / "os/package/being/being-audio-init.service"

# An analog input's playback path, as a codec with a line input shows it.
# Unmuting it would play the microphone through the speakers.
LOOPBACK_SCONTENTS = MBP92_SCONTENTS + """\
Simple mixer control 'Mic',0
  Capabilities: pvolume pswitch cvolume cswitch
  Playback channels: Front Left - Front Right
  Capture channels: Front Left - Front Right
  Limits: Playback 0 - 31 Capture 0 - 31
  Front Left: Playback 0 [0%] [off] Capture 0 [0%] [off]
  Front Right: Playback 0 [0%] [off] Capture 0 [0%] [off]
Simple mixer control 'Line',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 31
  Front Left: Playback 0 [0%] [off]
  Front Right: Playback 0 [0%] [off]
"""

FAKE_ALSACTL = """#!/bin/sh
echo "alsactl $*" >> "$FAKE_LOG"
case " $* " in
    *" restore "*) [ -z "$FAKE_RESTORE_FAILS" ] || { echo "bad state file" >&2; exit 1; } ;;
esac
exit 0
"""


class AudioInitScript(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("amixer", FAKE_AMIXER), ("alsactl", FAKE_ALSACTL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = self.root / "calls.log"
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("root=PARTUUID=x ro quiet\n")
        self.kept = self.root / "mind/audio/asound.state"

    def run_script(self, *arguments, scontents=MBP92_SCONTENTS, path=None, **extra):
        (self.root / "scontents").write_text(scontents)
        env = {"PATH": path or f"{self.bin}:/usr/bin:/bin", "FAKE_SCONTENTS": str(self.root / "scontents"),
               "FAKE_LOG": str(self.log), "BEING_AUDIO_RUN": str(self.root / "run"),
               "BEING_AUDIO_CMDLINE": str(self.cmdline), "BEING_AUDIO_WAIT": "0",
               "BEING_AUDIO_STATE_DIR": str(self.root / "no-such-dir"),
               "BEING_AUDIO_MIND_STATE": str(self.kept), **extra}
        if "python3" not in os.listdir(self.bin):
            # The fake amixer is a Python script; keep the host's interpreter reachable.
            (self.bin / "python3").symlink_to(os.path.realpath(os.sys.executable))
        done = subprocess.run(["/bin/sh", str(SCRIPT), *arguments], env=env, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return done.stdout, calls

    @staticmethod
    def sets(calls):
        return [tuple(line.split("\t")[1:]) for line in calls if line.startswith("sset\t")]

    def test_the_cs4206_gets_boost_two_of_three_and_working_levels(self):
        out, calls = self.run_script()
        sets = self.sets(calls)
        self.assertIn(("Mic Boost,0", "2"), sets)
        self.assertIn(("Capture,0", "capture", "85%"), sets)
        self.assertIn(("Master,0", "playback", "85%"), sets)
        self.assertIn(("PCM,0", "playback", "100%"), sets)
        # The capture switch ends on, after the off-and-on that wakes some codecs.
        self.assertEqual([s for s in sets if s[0] == "Capture,0"][-1], ("Capture,0", "capture", "cap"))
        self.assertIn("being-audio-init: done: card 0 playback 85%", out)

    def test_no_analog_input_playback_path_is_unmuted(self):
        for scontents in (LOOPBACK_SCONTENTS, SELECTOR_SCONTENTS):
            with self.subTest(controls=scontents.count("Simple mixer control")):
                _, calls = self.run_script(scontents=scontents)
                sets = self.sets(calls)
                for control in ("Mic,0", "Line,0"):
                    self.assertFalse([s for s in sets if s[0] == control and "playback" in s], sets)
                self.assertIn(("Mic,0", "capture", "cap"), sets)

    def test_a_missing_amixer_or_card_exits_zero_and_touches_nothing(self):
        bare = self.root / "bare"
        bare.mkdir()
        out, calls = self.run_script(path=f"{bare}:/usr/bin:/bin")
        self.assertIn("amixer is not installed", out)
        self.assertEqual(calls, [])
        out, calls = self.run_script(FAKE_NO_CARD="1")
        self.assertIn("no sound card 0", out)
        self.assertEqual(calls, [])

    def test_keep_on_the_kernel_command_line_leaves_the_mixer(self):
        self.cmdline.write_text("ro being.audio=keep quiet\n")
        out, calls = self.run_script()
        self.assertIn("mixer left as it is", out)
        self.assertEqual(calls, [])

    def test_the_owners_kept_levels_are_restored_instead_of_the_defaults(self):
        self.kept.parent.mkdir(parents=True)
        self.kept.write_text("state.PCH {}\n")
        out, calls = self.run_script()
        self.assertIn(f"alsactl -f {self.kept} restore 0", calls)
        self.assertEqual(self.sets(calls), [])
        self.assertIn("restored the kept levels", out)
        self.assertIn("done: card 0 with the kept levels", out)

        # A repair asks for the defaults even when a kept state exists.
        out, calls = self.run_script("--defaults")
        self.assertFalse([line for line in calls if " restore " in line])
        self.assertIn(("Mic Boost,0", "2"), self.sets(calls))

        # A damaged state file falls back to the defaults.
        out, calls = self.run_script(FAKE_RESTORE_FAILS="1")
        self.assertIn("could not restore", out)
        self.assertIn(("Capture,0", "capture", "85%"), self.sets(calls))


class AudioInitUnit(unittest.TestCase):
    def test_runs_once_before_the_voice_and_never_requires_the_mind(self):
        unit = UNIT.read_text()
        self.assertIn("Type=oneshot", unit)
        self.assertIn("Before=being-voice.service", unit)
        self.assertIn("After=alsa-restore.service alsa-state.service sound.target", unit)
        self.assertIn("After=local-fs.target", unit)
        self.assertNotIn("Requires", unit)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
