"""Candidate selection, strict outcomes and process cleanup without booting a guest."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import shutil
import signal
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from unittest.mock import patch

from lab import candidate_tests as gate, hardware_image
from tests.test_candidate_manifest import disk_image


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.args = argparse.Namespace(face_sha256=None)
        for role in gate.BINDINGS:
            path = self.root / role
            path.write_text(role)
            setattr(self.args, role, path)

    def sealed_fixture(self, *, with_laptop=False, with_laptop_factory=False):
        manifest = gate.candidate_manifest
        source = self.root / "frozen"
        for scope in manifest.SCOPES:
            path = source / scope
            if Path(scope).suffix:
                path.parent.mkdir(exist_ok=True)
                path.write_text(scope)
            else:
                path.mkdir(parents=True)
        slots = Path("os/board/beingos/slots.sh")
        (source / slots).parent.mkdir(parents=True)
        shutil.copyfile(gate.ROOT / slots, source / slots)
        (source / "lab/tests").mkdir()
        for name in gate.SUITES:
            (source / "lab/tests" / f"test_{name}.py").touch()
        ids = {name: uuid.UUID(value) for name, value in manifest.factory_slots(source).items()}
        disk_image(self.args.factory, manifest.FACTORY, ids)
        disk_image(self.args.try_image, manifest.TRY)
        paths = {key: getattr(self.args, role) for role, key in gate.RELEASE_ROLES.items()}
        if with_laptop:
            disk_image(self.args.laptop_try, manifest.LAPTOP_TRY, changed_slot="try-system")
            paths["laptop_try"] = self.args.laptop_try
        if with_laptop_factory:
            laptop_factory = self.root / "laptop_factory"
            disk_image(laptop_factory, manifest.FACTORY, ids, changed_slot="all")
            paths["laptop_factory"] = laptop_factory
        folder = self.root / "sealed"
        folder.mkdir()
        seal = folder / "release.json"
        manifest.publish(seal, {"kind": "beingos-candidate-v1", "source": manifest.source_snapshot(source),
                                "artifacts": manifest.artifact_records(paths, folder, manifest.factory_slots(source))})
        return source, seal

    def test_sealed_run_uses_exact_artifacts_and_checks_source_after_last_suite(self):
        source, seal = self.sealed_fixture()
        out = self.root / "run"
        with (patch.object(gate, "ROOT", source), patch.object(gate, "batch", return_value="passed") as run,
              redirect_stdout(io.StringIO())):
            self.assertEqual(gate.main(["--manifest", str(seal), "--output", str(out)]), 0)
        calls = [call.args for call in run.call_args_list]
        self.assertEqual([args[0][-2:] for args in calls[:2]], [
            ["lab.hardware_image", str(p.resolve())] for p in (self.args.factory, self.args.try_image)])
        report = json.loads((out / "report.json").read_text())
        self.assertTrue(report["manifest"]["checked_after_suites"])
        self.assertEqual(report["manifest"]["source_sha256"], gate.candidate_manifest.source_snapshot(source)["sha256"])
        self.assertTrue(all(s == "passed" for s in report["suites"].values()))
        self.assertTrue(all(args[1]["BEING_INSTALL_UPDATE_IMAGE"] == str(self.args.installed_fixtures.resolve())
                            for args in calls))

    def test_release_rejects_digest_substitution_and_explicit_path_overrides(self):
        source, seal = self.sealed_fixture()
        self.args.try_image.write_bytes(b"substituted media")
        for extra, reason in ((["--factory", str(self.args.factory)], "remove explicit"),
                              (["--laptop-try", str(self.args.laptop_try)], "remove explicit laptop_try"),
                              ([], "try_image does not match")):
            with (patch.object(gate, "ROOT", source), patch.object(gate, "batch") as run,
                  redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit)):
                gate.main(["--manifest", str(seal), *extra])
            self.assertIn(reason, errors.getvalue())
            run.assert_not_called()

    def test_public_alba_gate_binds_every_packaged_image_report_to_the_seal(self):
        source, seal = self.sealed_fixture(with_laptop=True, with_laptop_factory=True)
        release, _ = gate.read_release(seal)
        roles = ("laptop_try", "laptop_factory", "factory", "try_image")

        def audit(command, env, log, timeout):
            if command[2] == "lab.hardware_image":
                role = Path(command[3]).name
                artifact = release["artifacts"][role]
                slots = {part["name"]: {} for part in artifact["partitions"] if "rootfs" in part}
                log.write_text(json.dumps({"image_sha256": artifact["sha256"],
                                           "alba_retrain": {"systems": slots}}))
            return "passed"

        output = self.root / "alba"
        opts = ["--manifest", str(seal), "--require-alba-retrain", "--output", str(output)]
        with (patch.object(gate, "ROOT", source), patch.object(gate, "batch", side_effect=audit) as run,
              redirect_stdout(io.StringIO())):
            self.assertEqual(gate.main(opts), 0)
        commands = [call.args[0] for call in run.call_args_list[:len(roles)]]
        self.assertEqual([Path(command[3]).name for command in commands], list(roles))
        for role, command in zip(roles, commands):
            self.assertEqual(command[2], "lab.hardware_image")
            self.assertIn("--require-alba-retrain", command)
            self.assertEqual("--require-ordinary" in command, role.startswith("laptop_"))
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["alba_retrain_reports"], {role: f"inspect_{role}.log" for role in roles})

    def test_public_alba_gate_stops_before_guest_tests_on_incomplete_evidence(self):
        source, seal = self.sealed_fixture()
        release, _ = gate.read_release(seal)
        factory_digest = release["artifacts"]["factory"]["sha256"]
        cases = [("sealed image digest", "0" * 64, {"system-a": {}, "system-b": {}}),
                 ("every slot", factory_digest, {"system-a": {}})]
        for reason, digest, slots in cases:
            with self.subTest(reason=reason):
                output = self.root / reason.replace(" ", "-")

                def audit(command, env, log, timeout):
                    log.write_text(json.dumps({"image_sha256": digest, "alba_retrain": {"systems": slots}}))
                    return "passed"

                opts = ["--manifest", str(seal), "--require-alba-retrain", "--output", str(output)]
                with (patch.object(gate, "ROOT", source), patch.object(gate, "batch", side_effect=audit) as run,
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(gate.main(opts), 1)
                run.assert_called_once()
                report = json.loads((output / "report.json").read_text())
                self.assertIn(reason, report["suites"]["inspect_factory"])
                self.assertEqual(report["suites"]["lab.tests.test_l0"], "pending")
                self.assertEqual(report["alba_retrain_reports"], {})

    def test_controller_checks_the_hashed_laptop_image_for_cable_files_before_boot(self):
        source, seal = self.sealed_fixture(with_laptop=True)
        path = self.args.laptop_try.resolve()
        original = path.read_bytes()
        output = self.root / "inspection"
        with (patch.object(gate, "ROOT", source), patch.object(gate, "batch", return_value="failed with exit 1") as run,
              redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO())):
            path.write_bytes(b"different")
            with self.assertRaises(SystemExit):
                gate.main(["--manifest", str(seal), "--output", str(output)])
            self.assertIn("laptop_try does not match", errors.getvalue())
            run.assert_not_called()
            self.assertFalse(output.exists())

            path.write_bytes(original)
            self.assertEqual(gate.main(["--manifest", str(seal), "--output", str(output)]), 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "lab.hardware_image",
                                                     str(path), "--require-ordinary"])
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["suites"]["inspect_laptop_try"], "failed with exit 1")
            self.assertEqual(report["suites"]["lab.tests.test_laptop_smoke"], "pending")

    def test_inspection_uses_the_shared_cable_check_only_in_candidate_mode(self):
        entries = [{"Path": "usr/share/being/variant"}, {"Path": "usr/share/being/cable/host_key"}]
        with (patch.object(hardware_image, "entries_for", return_value=entries) as listing,
              patch.object(hardware_image, "member") as extract):
            with self.assertRaisesRegex(ValueError, "try-system contains cable development files"):
                hardware_image.root_report("7zz", self.args.laptop_try, ordinary_role="try-system")
            listing.assert_called_once_with("7zz", "SquashFS", self.args.laptop_try)
            extract.assert_not_called()
            self.assertIn("build_id", hardware_image.root_report("7zz", self.args.laptop_try))

    def test_release_reports_source_or_manifest_changes_during_qualification(self):
        source, seal = self.sealed_fixture()
        for mutation in ("seal", "source"):
            target = seal if mutation == "seal" else source / "Cargo.lock"
            original = target.read_bytes()
            out = self.root / mutation
            def mutate(*args):
                target.write_bytes(original + b" ")
                return "passed"
            with (patch.object(gate, "ROOT", source), patch.object(gate, "batch", side_effect=mutate),
                  redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
                self.assertEqual(gate.main(["--manifest", str(seal), "--output", str(out)]), 1)
            report = json.loads((out / "report.json").read_text())
            self.assertFalse(report["manifest"]["checked_after_suites"])
            self.assertIn("changed", report["manifest"]["error"])
            target.write_bytes(original)

    def test_fresh_imports_bind_every_guest_to_the_selected_inputs(self):
        with patch.dict(os.environ, BEING_LAPTOP_CABLE_TRY_IMAGE=str(self.args.laptop_try)):
            env, records = gate.inputs(self.args)
        self.assertNotIn("BEING_LAPTOP_CABLE_TRY_IMAGE", env)
        script = """
import json, os
from lab import vm
from lab.tests import test_hardware_ssh as h, test_install_apply as a, test_installer as i
from lab.tests import test_local_screen, test_network_modes
from lab.tests import test_l3 as l, test_l4, test_setup as s, test_try as t, test_wifi as w
print(json.dumps([str(path) for path in [
    vm.VM().image, test_l4.AudioHarness.start_audio.__kwdefaults__['image'],
    s.SETUP_IMAGE, h.IMAGE, os.environ['BEING_L6_IMAGE'], a.OLD_IMAGE, t.EXISTING_IMAGE, test_network_modes.IMAGE,
    a.TRY_IMAGE, t.TRY_IMAGE, i.INSTALLER_IMAGE, test_local_screen.SCREEN_IMAGE,
    os.environ['BEING_FACE_USB_IMAGE'], h.BUNDLE, a.HARDWARE_BUNDLE, a.BUNDLE,
    l.UPDATE_MEDIA, a.UPDATE_IMAGE, w.IMAGE]]))
"""
        result = subprocess.run([sys.executable, "-c", script], env=env, cwd=gate.ROOT, check=True,
                                capture_output=True, text=True, timeout=10)
        roles = ["factory"] * 8 + ["try_image"] * 5 + ["factory_bundle"] * 2 + [
            "installed_bundle", "update_fixtures", "installed_fixtures", "wifi_image"]
        self.assertEqual(json.loads(result.stdout), [records[role]["path"] for role in roles])
        self.assertEqual(len(records["factory"]["sha256"]), 64)

    def test_omitted_wifi_cannot_inherit_earlier_media(self):
        self.args.wifi_image = None
        with patch.dict(os.environ, BEING_WIFI_IMAGE="stale"):
            env, records = gate.inputs(self.args)
        self.assertNotIn("BEING_WIFI_IMAGE", env)
        self.assertNotIn("wifi_image", records)
        self.assertNotIn("lab.tests.test_wifi", gate.suite_names(False))
        self.assertIn("lab.tests.test_wifi", gate.suite_names(True))

    def test_replaced_input_blocks_a_continuing_run(self):
        _, records = gate.inputs(self.args)
        gate.check_inputs(records)
        self.args.factory.unlink()
        self.args.factory.write_text("substitute")
        with self.assertRaisesRegex(ValueError, "factory changed"):
            gate.check_inputs(records)

    def test_invalid_inputs_are_refused_before_any_suite(self):
        for bad in ("empty", "missing", "same", "directory"):
            with self.subTest(bad=bad):
                path = self.root / bad
                if bad == "empty":
                    path.touch()
                elif bad == "same":
                    os.link(self.args.factory, path)
                elif bad == "directory":
                    path.mkdir()
                self.args.try_image = path
                with self.assertRaises((OSError, ValueError)):
                    gate.inputs(self.args)

    def test_strict_worker_rejects_skips_errors_failures_and_empty_suites(self):
        module = types.ModuleType("candidate_fixture")
        class Fixture(unittest.TestCase):
            def test_ok(self):
                self.assertTrue(True)
        module.Fixture = Fixture
        def fail(self):
            self.fail("fixture")
        def error(self):
            raise ValueError("fixture")
        with patch.dict(sys.modules, {module.__name__: module}), redirect_stderr(io.StringIO()):
            self.assertEqual(gate.run_suite("candidate_fixture"), 0)
            for method in (unittest.skip("fixture")(Fixture.test_ok), fail, error, unittest.expectedFailure(fail)):
                with patch.object(Fixture, "test_ok", method):
                    self.assertEqual(gate.run_suite("candidate_fixture"), 1)
            del module.Fixture
            self.assertEqual(gate.run_suite("candidate_fixture"), 1)

    def test_timeout_stops_the_whole_batch_including_its_guest_process(self):
        address = self.root / "port"
        child = ("import socket,sys,time; from pathlib import Path; "
                 "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
                 "Path(sys.argv[1]).write_text(str(s.getsockname()[1])); "
                 "print('child ready', flush=True); time.sleep(10)")
        parent = ("import subprocess,sys; "
                  "p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); p.wait()")
        status = gate.batch([sys.executable, "-c", parent, child, str(address)], os.environ.copy(),
                            self.root / "timeout.log", 1)
        self.assertIn("timed out", status)
        self.assertIn("child ready", (self.root / "timeout.log").read_text())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", int(address.read_text())))

    def test_failure_records_unrun_suites_and_never_reuses_evidence(self):
        out = self.root / "evidence"
        argv = ["--output", str(out)]
        for role in gate.BINDINGS.keys() - {"laptop_try"}:
            argv.extend(["--" + role.replace("_", "-"), str(getattr(self.args, role))])
        with patch.object(gate, "batch", return_value="fixture failure") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(argv), 1)
            run.assert_called_once()
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["suites"]["lab.tests.test_l0"], "fixture failure")
        self.assertEqual(report["suites"]["lab.tests.test_l1"], "pending")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()), patch.object(gate, "batch") as run:
            gate.main(argv)
        run.assert_not_called()

    def test_denied_group_signal_requires_positive_evidence_the_group_is_quiescent(self):
        with patch.object(gate.os, "killpg", side_effect=PermissionError):
            with patch.object(gate, "group_has_live_members", side_effect=[True, False]):
                self.assertTrue(gate.stop_group(123))
            with (patch.object(gate, "group_has_live_members", return_value=True),
                  patch.object(gate, "wait_until", return_value=False), self.assertRaises(PermissionError)):
                gate.stop_group(123)

    def test_finished_worker_with_term_resistant_child_fails_and_closes_the_child(self):
        ready = self.root / "ready"
        release = self.root / "release"
        child = """
import signal, socket, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.listen()
Path(sys.argv[1]).write_text(str(s.getsockname()[1]))
print("ready", flush=True)
deadline = time.monotonic() + 15
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(.05)
"""
        parent = ("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',*sys.argv[1:]],"
                  "stdout=subprocess.PIPE); assert p.stdout.readline() == b'ready\\n'")
        try:
            status = gate.batch([sys.executable, "-c", parent, child, str(ready), str(release)],
                                os.environ.copy(), self.root / "unexpected-child.log", 5)
            self.assertIn("left a process", status)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", int(ready.read_text())))
        finally:
            release.touch()

    def test_hangup_cleans_up_a_running_batch_and_records_interruption(self):
        out = self.root / "cancelled"
        ready = self.root / "ready"
        argv = ["--output", str(out)]
        for role in gate.BINDINGS.keys() - {"laptop_try"}:
            argv.extend(["--" + role.replace("_", "-"), str(getattr(self.args, role))])
        fake_guest = """
import json, os, socket, sys, time
from pathlib import Path
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.listen()
ready = Path(sys.argv[1])
staging = ready.with_suffix('.new')
staging.write_text(json.dumps([s.getsockname()[1], os.environ['TMPDIR']]))
staging.replace(ready)
deadline = time.monotonic() + 15
while not ready.with_suffix('.quit').exists() and time.monotonic() < deadline:
    time.sleep(.05)
"""
        wrapper = """
import sys
from lab import candidate_tests as gate
batch = gate.batch
gate.batch = lambda command, *args: batch([sys.executable, '-c', sys.argv[1], sys.argv[2]], *args)
sys.exit(gate.cli(sys.argv[3:]))
"""
        proc = subprocess.Popen([sys.executable, "-c", wrapper, fake_guest, str(ready), *argv], cwd=gate.ROOT,
                                stdout=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.025)
            self.assertTrue(ready.is_file(), "the fixture worker never started")
            port, scratch = json.loads(ready.read_text())
            proc.send_signal(signal.SIGHUP)
            self.assertEqual(proc.wait(timeout=5), 1)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            self.assertFalse(Path(scratch).exists())
            self.assertEqual(json.loads((out / "report.json").read_text())["suites"]["lab.tests.test_l0"],
                             "interrupted")
        finally:
            ready.with_suffix(".quit").touch()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
