import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lab import hardware


def metrics(cpu_busy=40, **window):
    return {
        "schema_version": 1,
        "captured_at": "2026-11-21T01:00:00Z",
        "system": {"model": "MacBookPro9,2", "build_id": "being-factory-123"},
        "sampling": {"seconds": 30, "interval": 2},
        "services": {},
        "samples": [{"elapsed_seconds": 0}],
        "window": {"cpu_busy_percent": cpu_busy, "elapsed_seconds": 30, **window},
    }


class HardwareController(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="being hardware ")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.key = directory / "reader"
        self.key.touch(mode=0o600)
        self.pins = directory / "known hosts"
        self.pins.touch()
        self.runs = directory / "captures"
        self.connection = ["being.local", "--key", str(self.key), "--known-hosts", str(self.pins)]

    def test_captures_only_through_pinned_observe_ssh_and_writes_private_json(self):
        output = io.StringIO()
        capture = metrics()
        capture["sampling"] = {"seconds": 24, "interval": 4}
        with (patch.object(hardware, "RUNS", self.runs),
              patch.object(hardware, "bounded_output", return_value=json.dumps(capture)) as ssh,
              redirect_stdout(output)):
            result = hardware.main(["collect", *self.connection, "--scenario", "idle-ac",
                                    "--variant", "factory", "--seconds", "24", "--interval", "4"])
        self.assertEqual(result, 0)
        command, timeout = ssh.call_args.args
        self.assertEqual(command[:4], ["ssh", "-F", "/dev/null", "-T"])
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn(f'UserKnownHostsFile="{self.pins}"', command)
        self.assertIn("GlobalKnownHostsFile=/dev/null", command)
        self.assertIn("IdentityAgent=none", command)
        self.assertIn(str(self.key), command)
        self.assertEqual(command[-6:], ["-p", "2222", "-l", "being-observe", "being.local",
                                        "metrics --seconds 24 --interval 4"])
        self.assertEqual(timeout, 44)
        destination = Path(output.getvalue().strip())
        self.assertEqual(destination.parent, self.runs)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.runs.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.runs.iterdir()), [destination])
        run = json.loads(destination.read_text())
        self.assertEqual(run["scenario"], "idle-ac")
        self.assertEqual(run["variant"], "factory")
        self.assertEqual(run["requested_seconds"], 24)
        self.assertEqual(run["metrics"], capture)

    def test_invalid_response_does_not_create_a_capture(self):
        error = io.StringIO()
        with (patch.object(hardware, "RUNS", self.runs),
              patch.object(hardware, "bounded_output", return_value=b'{"schema_version": true}'),
              redirect_stderr(error)):
            result = hardware.main(["collect", *self.connection, "--scenario", "idle-ac"])
        self.assertEqual(result, 1)
        self.assertIn("schema version 1", error.getvalue())
        self.assertFalse(self.runs.exists())

    def test_response_with_different_sampling_does_not_create_a_capture(self):
        with (patch.object(hardware, "RUNS", self.runs),
              patch.object(hardware, "bounded_output", return_value=json.dumps(metrics())),
              redirect_stderr(io.StringIO())):
            result = hardware.main(["collect", *self.connection, "--scenario", "idle-ac", "--seconds", "20"])
        self.assertEqual(result, 1)
        self.assertFalse(self.runs.exists())

    def test_collect_rejects_out_of_bounds_measurements_before_connecting(self):
        with (patch.object(hardware, "bounded_output") as ssh, redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit) as raised):
            hardware.main(["collect", *self.connection, "--scenario", "idle", "--seconds", "301"])
        self.assertEqual(raised.exception.code, 2)
        ssh.assert_not_called()

    def test_compare_matches_scenario_and_reports_missing_readings(self):
        common = dict(target="being.local", scenario="idle-ac", requested_seconds=30,
                      requested_interval_seconds=2)
        baseline_metrics = metrics(14, network={"wifi0": {"rx_bytes": 30_720}}, wifi_signal_mean_dbm=-58)
        candidate_metrics = metrics(18, network={"wifi0": {"rx_bytes": 92_160}},
                                    disk={"sda": {"busy_ms": 3000}}, wifi_signal_mean_dbm=-61.5,
                                    cpu_core_throttle_max_events=3)
        before = hardware.save_run(self.runs, dict(common, metrics=baseline_metrics, variant="factory"))
        after = hardware.save_run(self.runs, dict(common, metrics=candidate_metrics, variant="candidate"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(hardware.main(["compare", str(before), str(after)]), 0)
        self.assertIn("+4.00 pp", output.getvalue())
        self.assertIn("Baseline factory", output.getvalue())
        self.assertIn("being-factory-123", output.getvalue())
        wifi = next(line for line in output.getvalue().splitlines() if "wifi0 receive" in line)
        self.assertIn("+2.00 KiB/s", wifi)
        signal = next(line for line in output.getvalue().splitlines() if "Mean Wi-Fi signal" in line)
        self.assertTrue(signal.endswith("-3.50 dB"), signal)
        self.assertEqual(output.getvalue().count("Mean Wi-Fi signal"), 1)
        self.assertRegex(output.getvalue(), r"Max core throttle events[^\n]+3\.00 events")
        disk = next(line for line in output.getvalue().splitlines() if "sda busy" in line)
        self.assertIn("10.00 %", disk)
        power = next(line for line in output.getvalue().splitlines() if "CPU package power" in line)
        self.assertEqual(power.count("—"), 3)

        different = hardware.save_run(self.runs, dict(common, metrics=metrics(), scenario="idle-battery"))
        with self.assertRaisesRegex(ValueError, "scenario mismatch"):
            hardware.compare(argparse.Namespace(baseline=before, candidate=different))

    def test_dev_uses_its_own_port_and_preserves_remote_arguments(self):
        remote = ["printf", "%s", "space ; $(value)", "single'quote"]
        with patch.object(hardware.subprocess, "run", return_value=subprocess.CompletedProcess([], 17)) as ssh:
            result = hardware.main(["dev", *self.connection, "--", *remote])
        command = ssh.call_args.args[0]
        self.assertEqual(result, 17)
        self.assertIn("-T", command)
        self.assertEqual(command[-6:-1], ["-p", "2223", "-l", "root", "being.local"])
        self.assertEqual(shlex.split(command[-1]), remote)

    def test_dev_control_uses_fixed_commands_on_the_observation_port(self):
        for action, remote in (("open-dev", "dev-on"), ("close-dev", "dev-off")):
            with (self.subTest(action=action), redirect_stdout(io.StringIO()),
                  patch.object(hardware, "bounded_output", return_value=bytearray(b"OK\n")) as ssh):
                self.assertEqual(hardware.main([action, *self.connection]), 0)
                command, timeout = ssh.call_args.args
                self.assertIn("-T", command)
                self.assertEqual(command[-6:], ["-p", "2222", "-l", "root", "being.local", remote])
                self.assertEqual(timeout, 20)

    def test_ssh_read_has_a_time_and_output_limit(self):
        output = hardware.bounded_output([sys.executable, "-c", "print('metrics')"], 2)
        self.assertEqual(output, b"metrics\n")
        with self.assertRaises(TimeoutError):
            hardware.bounded_output([sys.executable, "-c", "import time; time.sleep(8)"], 0.2)
        with self.assertRaisesRegex(ValueError, "output limit"):
            hardware.bounded_output([sys.executable, "-c", "print('x' * (5 * 1024 * 1024))"], 2)
