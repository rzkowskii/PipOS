"""being-health-watch: reboots an installed Pip whose brainstem or face cannot
come back, and only logs on Try and verbose boots. Run with a fake systemctl
and a fake /proc."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "os/package/being"
SCRIPT = PACKAGE / "being-health-watch"
# Pip's /bin/sh is BusyBox ash; dash is the nearest plain POSIX shell.
SHELL = shutil.which("dash") or shutil.which("sh")

FAKE_SYSTEMCTL = """#!/bin/sh
fake=${FAKE_SYSTEMD:?}
echo "$*" >>"$fake/calls"
case "$1" in
    is-system-running) cat "$fake/running" 2>/dev/null || echo running ;;
    show)
        for unit; do :; done
        [ -f "$fake/show/$unit" ] || exit 1
        cat "$fake/show/$unit" ;;
    --no-block) [ "$2" = reboot ] && echo reboot >>"$fake/reboots" ;;
    *) exit 1 ;;
esac
"""

ACTIVE = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success",
          "MainPID": "214", "ActiveEnterTimestampMonotonic": "5000000"}


@unittest.skipUnless(SHELL, "needs a POSIX shell")
class HealthWatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fake = self.root / "systemd"
        (self.fake / "show").mkdir(parents=True)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.state = self.root / "state"
        self.systemctl = self.root / "systemctl"
        self.systemctl.write_text(FAKE_SYSTEMCTL)
        self.systemctl.chmod(0o755)
        self.cmdline("BOOT_IMAGE=/boot/bzImage root=PARTUUID=x ro rauc.slot=A panic=10")
        self.unit("being-brainstem.service")
        self.unit("being-face.service", MainPID="628")

    def cmdline(self, text):
        (self.proc / "cmdline").write_text(text + "\n")

    def unit(self, name, **changes):
        values = {**ACTIVE, **changes}
        (self.fake / "show" / name).write_text("".join(f"{k}={v}\n" for k, v in values.items()))

    def process(self, pid, state, wchan="0"):
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / "stat").write_text(f"{pid} (being face) {state} 1 {pid} {pid} 0 -1\n")
        (self.proc / str(pid) / "wchan").write_text(wchan)

    def run_pass(self, uptime, **env):
        (self.proc / "uptime").write_text(f"{uptime}.42 100.00\n")
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FAKE_SYSTEMD": str(self.fake),
            "BEING_HEALTH_PROC": str(self.proc),
            "BEING_HEALTH_STATE": str(self.state),
            "BEING_HEALTH_SYSTEMCTL": str(self.systemctl),
            **env,
        }
        result = subprocess.run([SHELL, str(SCRIPT), "--once"], env=environment, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def reboots(self):
        path = self.fake / "reboots"
        return path.read_text().count("reboot") if path.exists() else 0

    def test_healthy_organs_are_left_alone(self):
        for uptime in range(20, 4000, 190):
            self.assertEqual(self.run_pass(uptime), "")
        self.assertEqual(self.reboots(), 0)
        self.assertEqual((self.state / "being-face.service.good").read_text().strip(), "3820")

    def test_a_face_that_cannot_die_reboots_an_installed_pip_once(self):
        self.run_pass(400)
        # Its frame loop stopped; systemd sent SIGABRT and SIGKILL, but the
        # process is stuck in the kernel, so the unit never gets past this.
        self.unit("being-face.service", ActiveState="deactivating", SubState="final-sigkill",
                  Result="watchdog", MainPID="628")
        self.process(628, "D", "i915_gem_wait_for_idle")
        for uptime in (410, 550, 690):
            self.assertEqual(self.run_pass(uptime), "", uptime)
        out = self.run_pass(700)
        self.assertIn("being-face.service has not been up for 300 s (deactivating final-sigkill, "
                      "result watchdog, pid 628 state D in i915_gem_wait_for_idle)", out)
        self.assertIn("Pip restarts now", out)
        self.assertEqual(self.reboots(), 1)
        for uptime in (710, 900):
            self.assertEqual(self.run_pass(uptime), "")
        self.assertEqual(self.reboots(), 1, "asked once")

    def test_try_and_verbose_boots_only_log_and_leave_evidence(self):
        for cmdline in ("ro being.try=1 being.cable=1", "ro panic=0 being.verbose=1 systemd.watchdog_sec=off"):
            with self.subTest(cmdline=cmdline):
                shutil.rmtree(self.state, ignore_errors=True)
                self.cmdline(cmdline)
                self.unit("being-brainstem.service")
                self.run_pass(400)
                self.unit("being-brainstem.service", ActiveState="activating", SubState="start",
                          Result="success", MainPID="901")
                self.process(901, "D")
                self.assertEqual(self.run_pass(990), "")
                out = self.run_pass(1000)
                self.assertIn("being-brainstem.service has not been up for 600 s (activating start, "
                              "result success, pid 901 state D)", out)
                self.assertIn("this is a Try or verbose boot, so Pip is not restarted", out)
                self.assertIn("pid 901 state D", (self.state / "being-brainstem.service.stuck").read_text())
                self.assertEqual(self.run_pass(1010), "", "said once")
                self.assertEqual(self.reboots(), 0)
                # Back up and stable: said, and the evidence is cleared.
                self.unit("being-brainstem.service", ActiveEnterTimestampMonotonic="1012000000")
                self.assertEqual(self.run_pass(1020), "", "not yet stable")
                self.assertIn("being-brainstem.service is back", self.run_pass(1030))
                self.assertFalse((self.state / "being-brainstem.service.stuck").exists())

    def test_a_crash_loop_counts_as_down_even_when_a_pass_sees_it_up(self):
        self.run_pass(400)
        for uptime in range(410, 1100, 15):
            # Up for a second now and then, never for 15 s.
            if (uptime - 410) % 45 == 0:
                self.unit("being-brainstem.service", ActiveEnterTimestampMonotonic=f"{(uptime - 1) * 1000000}")
            else:
                self.unit("being-brainstem.service", ActiveState="activating", SubState="auto-restart",
                          Result="exit-code", MainPID="0")
            out = self.run_pass(uptime)
            if uptime < 1000:
                self.assertEqual(out, "", uptime)
        self.assertEqual(self.reboots(), 1)

    def test_off_on_purpose_or_absent_is_not_stuck(self):
        self.run_pass(400)
        # Skipped by its ExecCondition (the USB menu has the screen), stopped
        # by hand, not on this image, or masked.
        self.unit("being-face.service", ActiveState="inactive", SubState="dead", Result="exec-condition",
                  MainPID="0")
        self.unit("being-brainstem.service", ActiveState="inactive", SubState="dead", Result="success",
                  MainPID="0")
        for uptime in range(410, 3000, 250):
            self.assertEqual(self.run_pass(uptime), "")
        self.unit("being-face.service", LoadState="not-found", ActiveState="inactive", Result="success")
        self.unit("being-brainstem.service", LoadState="masked", ActiveState="failed", Result="exit-code")
        for uptime in range(3000, 5000, 250):
            self.assertEqual(self.run_pass(uptime), "")
        self.assertEqual(self.reboots(), 0)

    def test_never_in_the_first_minutes_and_never_while_stopping(self):
        self.run_pass(0)
        self.unit("being-face.service", ActiveState="failed", SubState="failed", Result="core-dump", MainPID="0")
        out = ""
        for uptime in range(10, 300, 40):
            out += self.run_pass(uptime, BEING_HEALTH_UNITS="being-face.service:60")
        self.assertIn("not restarting in the first 300 s after boot", out)
        self.assertEqual(self.reboots(), 0)
        (self.fake / "running").write_text("stopping\n")
        self.assertEqual(self.run_pass(400, BEING_HEALTH_UNITS="being-face.service:60"), "")
        self.assertEqual(self.reboots(), 0)
        (self.fake / "running").write_text("degraded\n")
        self.assertIn("Pip restarts now", self.run_pass(410, BEING_HEALTH_UNITS="being-face.service:60"))
        self.assertEqual(self.reboots(), 1)

    def test_systemd_not_answering_is_left_to_the_system_watchdog(self):
        self.run_pass(400)
        (self.fake / "show" / "being-face.service").unlink()
        (self.fake / "show" / "being-brainstem.service").unlink()
        for uptime in range(410, 2000, 200):
            self.assertEqual(self.run_pass(uptime), "")
        self.assertEqual(self.reboots(), 0)

    def test_a_watcher_restart_keeps_how_long_an_organ_has_been_down(self):
        self.run_pass(400)
        self.unit("being-face.service", ActiveState="deactivating", SubState="stop-sigterm", Result="timeout")
        self.run_pass(500)
        # The state folder is the unit's preserved runtime directory; a new
        # process reads it.
        self.assertEqual((self.state / "being-face.service.good").read_text().strip(), "400")
        self.assertIn("has not been up for 300 s", self.run_pass(700))

    def stuck_face(self):
        self.run_pass(400)
        self.unit("being-face.service", ActiveState="failed", SubState="failed", Result="core-dump", MainPID="0")

    def persist_env(self, now, **extra):
        return {"BEING_HEALTH_PERSIST": str(self.persist), "BEING_HEALTH_NOW": str(now), **extra}

    def test_reboot_loop_breaker_stops_after_the_limit_and_leaves_pip_up(self):
        self.persist = self.root / "mind-log"
        self.persist.mkdir()
        counter = self.persist / "health-reboots"
        # Each boot is a fresh /run (state) and a fresh uptime; the counter stays.
        for boot in range(3):
            shutil.rmtree(self.state, ignore_errors=True)
            self.stuck_face()
            out = self.run_pass(700, **self.persist_env(1000 + boot * 600))
            self.assertIn("Pip restarts now", out, boot)
            self.assertEqual(self.reboots(), boot + 1)
        self.assertEqual(counter.read_text().split(), ["3", "1000"])
        shutil.rmtree(self.state, ignore_errors=True)
        self.stuck_face()
        out = self.run_pass(700, **self.persist_env(2500))
        self.assertIn("already restarted 3 times", out)
        self.assertIn("stays up", out)
        self.assertEqual(self.reboots(), 3, "no fourth reboot")
        self.assertIn("restart limit reached", (self.state / "being-face.service.stuck").read_text())
        self.assertEqual(self.run_pass(710, **self.persist_env(2510)), "", "said once")
        # The window passes: one more try is allowed, counted afresh.
        shutil.rmtree(self.state, ignore_errors=True)
        self.stuck_face()
        self.assertIn("Pip restarts now", self.run_pass(700, **self.persist_env(1000 + 86400)))
        self.assertEqual(self.reboots(), 4)
        self.assertEqual(counter.read_text().split(), ["1", str(1000 + 86400)])

    def test_reboot_breaker_survives_a_corrupt_or_future_counter(self):
        self.persist = self.root / "mind-log"
        self.persist.mkdir()
        counter = self.persist / "health-reboots"
        for junk in ("garbage\n", "", "3\n", "-1 x\n", "\x00\x01\xff"):
            with self.subTest(junk=junk):
                shutil.rmtree(self.state, ignore_errors=True)
                counter.write_bytes(junk.encode("latin-1"))
                before = self.reboots()
                self.stuck_face()
                self.assertIn("Pip restarts now", self.run_pass(700, **self.persist_env(5000)))
                self.assertEqual(self.reboots(), before + 1)
                self.assertEqual(counter.read_text().split(), ["1", "5000"], "rewritten valid")
        # Stamped in the future (clock not set yet): at the limit, stays up.
        shutil.rmtree(self.state, ignore_errors=True)
        counter.write_text("1 9999999\n")
        before = self.reboots()
        self.stuck_face()
        self.assertIn("stays up", self.run_pass(700, **self.persist_env(5000)))
        self.assertEqual(self.reboots(), before)

    def test_reboot_count_clears_after_sustained_health(self):
        self.persist = self.root / "mind-log"
        self.persist.mkdir()
        counter = self.persist / "health-reboots"
        counter.write_text("2 1000\n")
        self.assertEqual(self.run_pass(1000, **self.persist_env(2000)), "")
        self.assertTrue(counter.exists(), "not healthy long enough yet")
        self.assertEqual(self.run_pass(1800, **self.persist_env(2800)),
                         "all organs have been up for 1800 s; the restart count is cleared\n")
        self.assertFalse(counter.exists())

    def test_without_a_mind_partition_the_old_behaviour_stands(self):
        # Try images and a locked mind volume have no place to count in.
        self.stuck_face()
        out = self.run_pass(700, BEING_HEALTH_PERSIST=str(self.root / "absent"), BEING_HEALTH_NOW="1000")
        self.assertIn("not limiting restarts", out)
        self.assertIn("Pip restarts now", out)
        self.assertEqual(self.reboots(), 1)


class HealthWatchUnitTests(unittest.TestCase):
    def test_the_unit_runs_the_script_and_keeps_its_findings(self):
        unit = (PACKAGE / "being-health-watch.service").read_text()
        self.assertIn("ExecStart=/usr/libexec/being/being-health-watch\n", unit)
        for line in ("Restart=always", "StartLimitIntervalSec=0", "RuntimeDirectory=being-health",
                     "RuntimeDirectoryPreserve=yes", "Conflicts=shutdown.target", "PrivateNetwork=yes",
                     "NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=-/mind/log"):
            self.assertIn(line + "\n", unit)
        self.assertNotIn("WatchdogSec", unit, "a shell loop cannot ping it reliably; Restart= is enough")
        script = SCRIPT.read_text()
        self.assertIn("state=${BEING_HEALTH_STATE:-/run/being-health}", script)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_its_limits_come_after_the_organs_own_recovery(self):
        script = SCRIPT.read_text()
        self.assertIn("being-brainstem.service:600 being-face.service:300", script)
        stem = (PACKAGE / "being-brainstem.service").read_text()
        self.assertIn("RestartMaxDelaySec=30s", stem)
        self.assertIn('-ge 8 ] && [ "$$up" -ge 300 ]', stem, "eight failures in ten minutes, after 300 s")
        self.assertIn("settle=${BEING_HEALTH_SETTLE:-300}", script)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        result = subprocess.run(["shellcheck", "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
