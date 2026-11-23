"""Exercise the foreground soak controller without starting QEMU."""

from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from lab import soak


def healthy(guest_seconds):
    return {
        "boot_id": "same-boot", "slot": "A", "confirmed": "A",
        "state": "idle", "unwell": False, "heartbeat": "idle",
        "cortex_ready": True, "guest_seconds": guest_seconds,
        "services": {
            f"being-{organ}.service": {
                "active_state": "active", "sub_state": "running",
                "restarts": 0, "active_since_boot_us": 35, "memory_bytes": 1024,
            } for organ in soak.ORGANS
        },
    }


class Clock:
    def __init__(self):
        self.now = 100.0

    def sleep(self, seconds):
        self.now += seconds


class SoakTests(unittest.TestCase):
    def test_window_starts_after_first_probe_and_samples_through_requested_end(self):
        clock = Clock()
        observed = []
        starts = []

        def probe(vm):
            starts.append(clock.now)
            clock.now += 2
            return healthy(clock.now)

        with patch.object(soak, "sample", side_effect=probe):
            elapsed = soak.observe(None, 23, 8, lambda event, **data: observed.append(data),
                                   clock=lambda: clock.now, sleep=clock.sleep)
        self.assertEqual(starts, [100, 110, 120, 125])
        self.assertEqual([row["observed_seconds"] for row in observed], [0, 10, 20, 25])
        self.assertEqual(elapsed, 25)
        self.assertTrue(all(row["probe_seconds"] == 2 and not row["errors"] for row in observed))

    def test_no_credit_for_missing_observation_after_host_pause(self):
        clock = Clock()
        evidence = []
        with (patch.object(soak, "sample", side_effect=lambda vm: healthy(clock.now)),
              self.assertRaisesRegex(RuntimeError, "missing observations")):
            soak.observe(None, 45, 10, lambda event, **data: evidence.append(data),
                         clock=lambda: clock.now, sleep=lambda n: clock.sleep(n + 60))
        self.assertEqual(len(evidence), 2)
        self.assertTrue(evidence[-1]["errors"])

    def test_slow_guest_clock_cannot_earn_a_long_pass_from_host_time_alone(self):
        clock = Clock()
        evidence = []
        with (patch.object(soak, "sample", side_effect=lambda vm: healthy(clock.now / 100)),
              self.assertRaisesRegex(RuntimeError, "guest never observed")):
            soak.observe(None, 25, 10, lambda event, **data: evidence.append(data),
                         clock=lambda: clock.now, sleep=clock.sleep)
        self.assertGreaterEqual(evidence[-1]["observed_seconds"], 25)
        self.assertLess(evidence[-1]["guest_observed_seconds"], 25)

    def test_health_rejects_reboots_stale_probe_and_service_replacement_or_failure(self):
        before = healthy(42)
        for key, value in (("boot_id", "new-boot"), ("guest_seconds", 42),
                           ("heartbeat", ""), ("unwell", True), ("cortex_ready", False),
                           ("confirmed", "B"), ("slot", "B")):
            with self.subTest(key=key):
                current = healthy(43)
                current[key] = value
                self.assertTrue(soak.health_errors(current, before))
        for organ in soak.ORGANS:
            unit = f"being-{organ}.service"
            for key, bad in (("restarts", 1), ("sub_state", "dead"), ("active_since_boot_us", 73)):
                current = healthy(43)
                current["services"][unit][key] = bad
                with self.subTest(unit=unit, field=key):
                    self.assertTrue(any(unit in why for why in soak.health_errors(current, before)))
            current = deepcopy(before)
            del current["services"][unit]
            self.assertTrue(any(unit in why for why in soak.health_errors(current, None)))

    def test_guest_probe_propagates_nonzero_shell_exit(self):
        vm = Mock()
        vm.run.return_value = (1, "brainstem disconnected")
        with self.assertRaisesRegex(RuntimeError, "brainstem disconnected"):
            soak.sample(vm)

    def test_guest_probe_reads_snapshot_and_live_heartbeat(self):
        events = [
            '{"t":"state.changed","state":"idle","unwell":false}',
            '{"t":"stem.beat","state":"idle"}',
        ]
        for lines in (events[:1], events):
            bus = MagicMock()
            bus.makefile.return_value.__enter__.return_value = lines
            output = StringIO()
            with (self.subTest(lines=lines), redirect_stdout(output),
                  patch("socket.socket") as sock,
                  patch("pathlib.Path.read_text", side_effect=["fixture-boot", "A"]),
                  patch("mind.boot_health.booted_slot", return_value="A"),
                  patch("mind.boot_health.cortex_ready", return_value=True),
                  patch("mind.hardware_metrics.read_services", return_value=healthy(1)["services"])):
                sock.return_value.__enter__.return_value = bus
                if len(lines) == 1:
                    with self.assertRaisesRegex(RuntimeError, "disconnected"):
                        exec(soak.GUEST_PROBE, {})
                else:
                    exec(soak.GUEST_PROBE, {})
                    self.assertFalse(soak.health_errors(json.loads(output.getvalue()), None))
                    payload, = bus.sendall.call_args.args
                    self.assertTrue(payload.endswith(b"\n"))
                    self.assertEqual(json.loads(payload)["sub"], ["state.changed", "stem.beat"])

    def test_real_run_lifecycle_and_partial_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "factory.img"
            image.write_bytes(b"fake image")
            for failure in (None, RuntimeError("bad start"), KeyboardInterrupt()):
                with self.subTest(failure=repr(failure)):
                    vm, report = Mock(), StringIO()
                    vm.dir = Path(directory) / "guest"
                    vm.running.return_value = False
                    vm.run.return_value = (0, "")
                    if isinstance(failure, RuntimeError):
                        vm.start.side_effect = failure
                    with (patch.object(soak, "VM", return_value=vm) as factory,
                          patch.object(soak, "observe", return_value=31,
                                       side_effect=failure if isinstance(failure, KeyboardInterrupt) else None)):
                        if failure is None:
                            self.assertEqual(soak.run(image, 30, 10, report), 31)
                        else:
                            with self.assertRaises(type(failure)):
                                soak.run(image, 30, 10, report)
                    factory.assert_called_once_with(image=image, loopback=True)
                    vm.start.assert_called_once_with()  # No existing writable disk.
                    vm.power_cut.assert_called_once_with()
                    vm.destroy.assert_called_once_with()
                    events = [json.loads(line) for line in report.getvalue().splitlines()]
                    self.assertEqual(events[1]["sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
                    self.assertEqual(events[-2]["event"], "cleaned")
                    self.assertEqual(events[-1]["event"], "failed" if failure else "passed")

    def test_failed_cleanup_never_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "factory.img"
            image.touch()
            vm, report = Mock(), StringIO()
            vm.dir = Path(directory)
            vm.run.return_value = (0, "")
            vm.running.return_value = False
            with (patch.object(soak, "VM", return_value=vm),
                  patch.object(soak, "observe", return_value=10),
                  self.assertRaisesRegex(RuntimeError, "remove")):
                soak.run(image, 10, 10, report)
            events = [json.loads(line)["event"] for line in report.getvalue().splitlines()]
            self.assertEqual(events[-1], "failed")
            self.assertNotIn("cleaned", events)
            self.assertNotIn("passed", events)
