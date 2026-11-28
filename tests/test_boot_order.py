"""Boot ordering on Try sticks: the boot microphone test and the unit keys
that systemd reads (DEEP-DIVE items 12 and 19, 25 Nov 2026).

being-audio-init runs before the voice, so its microphone test (4.4 s on the
MacBookPro9,2) held the ears back. A cable development boot leaves the test
to `pip-diag mic`; being.mic_test=1 and =0 ask for it or skip it on any Try
boot. A skip is recorded where the result goes, so the boot's summary says
why there is no level.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mind import usb_logs
from tests.test_usb_logs import FAKE_AMIXER, FAKE_LOGGER, MBP92_SCONTENTS

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "os/package/being"
SCRIPT = PACKAGE / "being-audio-init"


def sections(path):
    """{section: [(key, value), ...]} of a systemd unit file, comments dropped."""
    found, current = {}, None
    for raw in path.read_text().replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = found.setdefault(line[1:-1], [])
            continue
        key, _, value = line.partition("=")
        current.append((key, value))
    return found


class BootMicTest(unittest.TestCase):
    """os/package/being/being-audio-init's microphone test on the boot path."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("amixer", FAKE_AMIXER), ("alsactl", FAKE_LOGGER), ("mic-test", FAKE_LOGGER)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.root / "scontents").write_text(MBP92_SCONTENTS)
        self.log = self.root / "calls.log"
        self.cmdline = self.root / "cmdline"
        self.run_dir = self.root / usb_logs.AUDIO_RUN

    def run_script(self, cmdline, **extra):
        self.cmdline.write_text(cmdline + "\n")
        self.log.unlink(missing_ok=True)
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_SCONTENTS=str(self.root / "scontents"),
                   FAKE_LOG=str(self.log), BEING_AUDIO_RUN=str(self.run_dir),
                   BEING_AUDIO_CMDLINE=str(self.cmdline), BEING_AUDIO_WAIT="0",
                   BEING_AUDIO_STATE_DIR=str(self.root / "no-state-dir"),
                   BEING_AUDIO_MIND_STATE=str(self.root / "no-kept-state"),
                   BEING_AUDIO_PROC=str(self.root / "no-proc"),
                   BEING_AUDIO_MIC_TEST=str(self.bin / "mic-test"))
        env.update(extra)
        shell = "/bin/dash" if Path("/bin/dash").exists() else "/bin/sh"
        done = subprocess.run([shell, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return done.stdout, calls

    @staticmethod
    def mic_tests(calls):
        return [line for line in calls if "mic-test" in line]

    def recorded(self):
        return usb_logs.mic_result(self.root)

    def test_a_plain_try_boot_skips_active_measurement(self):
        out, calls = self.run_script("ro being.try=1 quiet")
        self.assertEqual(self.mic_tests(calls), [])
        self.assertIn("use a microphone diagnostic boot entry", out)
        self.assertIn("being-audio-init: done:", out)

    def test_a_cable_boot_leaves_the_mic_to_pip_diag_and_says_so(self):
        out, calls = self.run_script("ro being.try=1 being.cable=1 being.voice_debug=1 quiet")
        self.assertEqual(self.mic_tests(calls), [], "the voice must not wait for the boot mic test")
        self.assertIn("being-audio-init: microphone not measured: a cable development boot; run pip-diag mic", out)
        # The mixer is still set before the voice starts.
        sets = [line for line in calls if line.startswith("sset\t")]
        self.assertIn("sset\tMic Boost,0\t2", sets)
        self.assertIn("being-audio-init: done:", out)
        # The boot summary reads the skip and says why there is no level.
        result = self.recorded()
        self.assertEqual(result["status"], "skipped")
        line = usb_logs.mic_line(result)
        self.assertTrue(line.startswith("not measured: a cable development boot"), line)
        self.assertIn("pip-diag mic", line)
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir() if p.name.startswith("mic-level")),
                         ["mic-level.json"], "no half-written file left behind")

    def test_the_kernel_argument_asks_for_or_skips_the_test(self):
        # being-audio-init never opens capture: being.mic_test=1 only defers
        # the level check to being-audio-measure.service, which runs after
        # the voice has started and needs a confirmed open lid.
        out, calls = self.run_script("ro being.try=1 being.cable=1 being.mic_test=1")
        self.assertEqual(self.mic_tests(calls), [], "the boot path must not open the microphone")
        self.assertIn("microphone measurement deferred until after the voice starts", out)
        self.assertIsNone(self.recorded())
        self.assertFalse((self.run_dir / "mic-test-active").exists())
        unit = sections(PACKAGE / "being-audio-measure.service")
        self.assertIn(("ConditionKernelCommandLine", "being.mic_test=1"), unit["Unit"])
        self.assertIn(("ConditionKernelCommandLine", "being.try=1"), unit["Unit"])
        self.assertIn("--require-open-lid", (PACKAGE / "being-audio-measure").read_text())
        out, calls = self.run_script("ro being.try=1 being.mic_test=0")
        self.assertEqual(self.mic_tests(calls), [])
        self.assertIn("microphone not measured: being.mic_test=0 on the kernel command line", out)
        self.assertEqual(usb_logs.mic_line(self.recorded()),
                         "not measured: being.mic_test=0 on the kernel command line")

    def test_the_cable_kill_switch_treats_the_boot_as_any_other_try_boot(self):
        # being.cable=0 wins over being.cable=1, so the boot gets the ordinary
        # Try result (no measurement, a pointer to the diagnostic entries),
        # not the cable development one.
        out, calls = self.run_script("ro being.try=1 being.cable=1 being.cable=0")
        self.assertEqual(self.mic_tests(calls), [])
        self.assertIn("use a microphone diagnostic boot entry", out)
        self.assertNotIn("a cable development boot", out)

    def test_installed_boots_never_measure_or_record_a_skip(self):
        for cmdline in ("ro quiet", "ro being.cable=1 being.mic_test=1"):
            _, calls = self.run_script(cmdline)
            self.assertEqual(self.mic_tests(calls), [], cmdline)
            self.assertIsNone(self.recorded(), cmdline)

    def test_the_voice_is_still_ordered_after_the_mixer(self):
        unit = sections(PACKAGE / "being-audio-init.service")
        before = [word for key, value in unit["Unit"] if key == "Before" for word in value.split()]
        self.assertIn("being-voice.service", before)
        self.assertIn("being-ears.service", before)
        self.assertIn(("Type", "oneshot"), unit["Service"])


class UnitKeySections(unittest.TestCase):
    def test_usb_logs_start_limits_sit_in_unit(self):
        # systemd warned "Unknown key 'StartLimitIntervalSec' in section [Service]"
        # and ignored it (audit-logs/warn.txt on the MacBookPro9,2).
        unit = sections(PACKAGE / "being-usb-logs.service")
        unit_keys = dict(unit["Unit"])
        self.assertEqual((unit_keys.get("StartLimitBurst"), unit_keys.get("StartLimitIntervalSec")), ("3", "60"))
        service_keys = {key for key, _ in unit["Service"]}
        self.assertFalse({"StartLimitBurst", "StartLimitIntervalSec", "StartLimitInterval"} & service_keys)
        self.assertIn(("Restart", "on-failure"), unit["Service"])

    def test_no_being_unit_puts_a_start_limit_under_service(self):
        units = sorted(PACKAGE.glob("*.service")) + sorted((REPO / "os/package/cortex").glob("*.service"))
        self.assertTrue(units)
        for path in units:
            service_keys = {key for key, _ in sections(path).get("Service", [])}
            self.assertFalse({"StartLimitBurst", "StartLimitIntervalSec", "StartLimitInterval"} & service_keys,
                             path.name)


if __name__ == "__main__":
    unittest.main()
