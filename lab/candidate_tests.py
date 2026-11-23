"""Run the integrated candidate's QEMU acceptance suites serially in a frozen checkout."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lab import candidate_manifest

ROOT = Path(__file__).resolve().parent.parent
SUITES = ("l0", "l1", "l2", "l3", "l4", "l5", "l6", "model_pack", "installer", "try", "setup",
          "face_usb", "local_screen", "install_apply", "hardware", "hardware_image", "b43_firmware",
          "hardware_ssh", "network_modes", "wifi", "laptop_smoke", "iso")
# Suites whose media no candidate seals yet: run them by hand (lab/make-iso.sh, then
# python3 -m unittest lab.tests.test_iso) until the candidate lane builds the ISO.
UNSEALED = ("iso",)
BINDINGS = {
    "factory": ("BEING_FACTORY_IMAGE", "BEING_SETUP_IMAGE", "BEING_L6_IMAGE", "BEING_HARDWARE_IMAGE",
                "BEING_TRY_EXISTING_IMAGE", "BEING_INSTALL_OLD_IMAGE", "BEING_NETWORK_IMAGE"),
    "try_image": ("BEING_INSTALLER_IMAGE", "BEING_TRY_IMAGE", "BEING_INSTALL_TRY_IMAGE",
                  "BEING_FACE_USB_IMAGE", "BEING_SCREEN_IMAGE"),
    "factory_bundle": ("BEING_HARDWARE_BUNDLE",),
    "installed_bundle": ("BEING_INSTALL_BUNDLE",),
    "update_fixtures": ("BEING_UPDATE_FIXTURES",),
    "installed_fixtures": ("BEING_INSTALL_UPDATE_IMAGE",),
    "wifi_image": ("BEING_WIFI_IMAGE",),
    # The laptop Try stick from a `candidate NAME --laptop` build: enables laptop_smoke.
    "laptop_try": ("BEING_LAPTOP_TRY_IMAGE",),
}
OPTIONAL_ROLES = ("wifi_image", "laptop_try")
INSTALLED_CASE = ("lab.tests.test_install_apply.InstallerApply."
                  "test_installs_both_slots_then_keeps_identity_and_memory_after_usb_removal")

RELEASE_ROLES = {role: "extra:installed_fixtures" if role == "installed_fixtures" else role
                 for role in BINDINGS if role not in OPTIONAL_ROLES}


def read_release(path):
    with candidate_manifest.regular(path) as (stream, _):
        raw = stream.read()
    release = json.loads(raw)
    if not isinstance(release, dict) or release.get("kind") != "beingos-candidate-v1":
        raise ValueError("expected a sealed PipOS candidate manifest")
    return release, hashlib.sha256(raw).hexdigest()


def bind_release(args):
    overrides = [role for role in RELEASE_ROLES if getattr(args, role) is not None]
    if overrides:
        raise ValueError("--manifest selects candidate files; remove explicit " + ", ".join(overrides))
    release, digest = read_release(args.manifest)
    candidate_manifest.check_source(ROOT, release["source"])
    for role, key in RELEASE_ROLES.items():
        record = release["artifacts"].get(key)
        if not record:
            raise ValueError(f"manifest is missing {key}")
        path = Path(record["path"])
        if path.is_absolute() or str(path) != os.path.normpath(str(path)):
            raise ValueError(f"{key} in manifest must use a normalized relative path")
        setattr(args, role, args.manifest.parent / path)
    laptop = release["artifacts"].get("laptop_try")
    if laptop:
        if getattr(args, "laptop_try", None) is not None:
            raise ValueError("--manifest selects candidate files; remove explicit laptop_try")
        path = Path(laptop["path"])
        if path.is_absolute() or str(path) != os.path.normpath(str(path)):
            raise ValueError("laptop_try in manifest must use a normalized relative path")
        args.laptop_try = args.manifest.parent / path
    return release, digest


def suite_names(with_wifi, with_laptop=False):
    known = {"test_" + name for name in SUITES}
    # Host checks of the lab tools live in lab/tests/host/ and are not guest suites.
    available = {path.stem for path in (ROOT / "lab/tests").glob("test_*.py")}
    if available != known:
        raise ValueError(f"review candidate suite inventory: missing {sorted(known - available)}, "
                         f"unclassified {sorted(available - known)}")
    # laptop_smoke needs the laptop Try stick, sealed only by `candidate NAME --laptop`.
    return ["lab.tests.test_" + name for name in SUITES
            if (name != "wifi" or with_wifi) and (name != "laptop_smoke" or with_laptop)
            and name not in UNSEALED]


def inputs(args):
    env = os.environ.copy()
    env.pop("BEING_FACE_SHA256", None)
    if args.face_sha256:
        env["BEING_FACE_SHA256"] = args.face_sha256
    records = {}
    identities = {}
    for role, names in BINDINGS.items():
        for name in names:
            env.pop(name, None)
        path = getattr(args, role)
        if path is None:
            if role in RELEASE_ROLES:
                raise ValueError(f"provide --manifest or --{role.replace('_', '-')}")
            continue
        path = path.parent.resolve() / path.name
        with candidate_manifest.regular(path) as (stream, info):
            if info.st_size <= 0:
                raise ValueError(f"{role}: empty input {path}")
            identity = (info.st_dev, info.st_ino)
            if identity in identities:
                raise ValueError(f"{role} and {identities[identity]} refer to the same file")
            identities[identity] = role
            record = candidate_manifest.fingerprint(stream, info.st_size)
            records[role] = dict(path=str(path), file_identity=file_identity(info), **record)
        env.update(dict.fromkeys(names, str(path)))
    return env, records


def file_identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def check_inputs(records):
    for role, record in records.items():
        if file_identity(Path(record["path"]).lstat()) != record["file_identity"]:
            raise ValueError(f"{role} changed during qualification: {record['path']}")


def run_suite(name):
    class Result(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            passed.add(test.id())

    passed = set()
    suite = unittest.defaultTestLoader.loadTestsFromName(name)
    expected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    if not expected or result.testsRun != expected:
        print(f"{name}: executed {result.testsRun} of {expected} cases", file=sys.stderr)
        return 1
    if result.skipped or result.expectedFailures:
        print(f"{name}: skipped tests and expected failures cannot qualify a candidate", file=sys.stderr)
        return 1
    if name == "lab.tests.test_install_apply" and INSTALLED_CASE not in passed:
        print(f"Required installed-channel acceptance did not pass: {INSTALLED_CASE}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


def worker(name):
    # Several standalone guest suites request /tmp explicitly to keep QEMU's
    # Unix socket names short. Keep those files inside this worker's short
    # private directory so even a killed guest leaves no multi-GB disk behind.
    original = tempfile.mkdtemp
    def scoped(suffix=None, prefix=None, dir=None):
        if dir is None or os.fspath(dir) == "/tmp":
            dir = os.environ["TMPDIR"]
        return original(suffix, prefix, dir)

    with patch.object(tempfile, "mkdtemp", scoped):
        return run_suite(name)


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))
    return True


def group_has_live_members(group):
    # The unreaped worker pins this session and process-group ID. Even an
    # entirely zombie group is visible in ps and can return EPERM to killpg
    # on Darwin. Only live members can hold sockets or continue guest work.
    result = subprocess.run(["ps", "-g", str(group), "-o", "pgid=,stat="],
                            capture_output=True, text=True, timeout=3, check=True)
    for line in result.stdout.splitlines():
        pgid, state = line.split()
        if int(pgid) == group and not state.startswith("Z"):
            return True
    return False


def stop_group(group):
    if not group_has_live_members(group):
        return False
    for event in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, event)
        except (PermissionError, ProcessLookupError):
            # A member can exit between the status check and the signal.
            if wait_until(lambda: not group_has_live_members(group), 1):
                return True
            raise
        if wait_until(lambda: not group_has_live_members(group), 2):
            return True
    raise RuntimeError(f"process group {group} still has a live member after SIGKILL")


def batch(command, env, log, timeout):
    with log.open("x") as stream:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=True)
        def worker_exited():
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            return info is not None and info.si_pid == proc.pid

        try:
            if not wait_until(worker_exited, timeout):
                return f"timed out after {timeout}s"
        finally:
            try:
                survivors = stop_group(proc.pid)
            except BaseException:
                # Preserve the inspection/cancellation error and still stop
                # this pinned group if its status could not be verified.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                raise
            finally:
                code = proc.wait(timeout=3)
        if survivors:
            return "test left a process running in its process group"
        return "passed" if code == 0 else f"failed with exit {code}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="integrated release mode: sealed manifest from this checkout")
    for role in BINDINGS:
        parser.add_argument("--" + role.replace("_", "-"), type=Path,
                            help={"wifi_image": "separate lab-only managed hwsim image",
                                  "installed_fixtures": "ext4 QA disk containing both named RAUC bundles",
                                  "update_fixtures": "L3 ext4 disk with unsigned, bad and good lab bundles"}.get(role))
    parser.add_argument("--suite-timeout", type=int, default=2400, help="seconds per serial suite (default 2400)")
    parser.add_argument("--face-sha256", help="expected packaged face binary for the Try USB display test")
    parser.add_argument("--output", type=Path, help="new evidence directory (default unique under out/logs)")
    args = parser.parse_args(argv)
    if args.suite_timeout <= 0:
        parser.error("--suite-timeout must be positive")
    if args.face_sha256 and (len(args.face_sha256) != 64 or
                             any(c not in "0123456789abcdefABCDEF" for c in args.face_sha256)):
        parser.error("--face-sha256 must be a 64-character hex digest")
    try:
        release = None
        if args.manifest:
            release, digest = bind_release(args)
        modules = suite_names(args.wifi_image is not None, getattr(args, "laptop_try", None) is not None)
        env, records = inputs(args)
        if release:
            for role, key in RELEASE_ROLES.items():
                expected = release["artifacts"][key]
                if any(records[role][field] != expected[field] for field in ("bytes", "sha256")):
                    raise ValueError(f"{key} does not match the sealed manifest: {records[role]['path']}")
            laptop = release["artifacts"].get("laptop_try")
            if laptop and any(records["laptop_try"][field] != laptop[field] for field in ("bytes", "sha256")):
                raise ValueError(f"laptop_try does not match the sealed manifest: {records['laptop_try']['path']}")
        if args.output:
            output = args.output.resolve()
            if release and any(output.is_relative_to(ROOT / scope)
                               for scope in (*candidate_manifest.SCOPES, *candidate_manifest.OPTIONAL_SCOPES)):
                raise ValueError("save candidate evidence outside the frozen source input scopes")
            output.mkdir(parents=True, exist_ok=False)
        else:
            logs = ROOT / "out/logs"
            logs.mkdir(parents=True, exist_ok=True)
            output = Path(tempfile.mkdtemp(prefix="candidate-", dir=logs))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))

    print(f"Candidate evidence: {output}", flush=True)
    if args.wifi_image is None:
        print("Separate Wi-Fi hwsim qualification was not selected.", flush=True)
    batches = {}
    if release:
        for role in ("factory", "try_image", "laptop_try"):
            if role in records:
                batches["inspect_" + role] = [sys.executable, "-m", "lab.hardware_image", records[role]["path"]]
    for module in modules:
        batches[module] = [sys.executable, "-m", "lab.candidate_tests", "_worker", module]
    report = {"inputs": records, "wifi_selected": args.wifi_image is not None,
              "laptop_selected": "laptop_try" in records, "face_sha256": args.face_sha256,
              "suite_timeout_seconds": args.suite_timeout, "suites": dict.fromkeys(batches, "pending")}
    if release:
        report["manifest"] = {"path": str(args.manifest.absolute()), "sha256": digest,
                              "source_sha256": release["source"]["sha256"], "checked_after_suites": False}
    def save():
        staging = output / ".report.json"
        staging.write_text(json.dumps(report, indent=2) + "\n")
        staging.replace(output / "report.json")

    save()
    env.update(BEING_LAB_OUTPUT=str(output), PYTHONUNBUFFERED="1")
    for module, command in batches.items():
        with tempfile.TemporaryDirectory(prefix="bqa-", dir="/tmp") as scratch:
            log = output / (module.rsplit(".", 1)[-1] + ".log")
            print(f"{module}  {log}", flush=True)
            report["suites"][module] = "running"
            save()
            try:
                check_inputs(records)
                status = batch(command, dict(env, TMPDIR=scratch), log, args.suite_timeout)
                check_inputs(records)
            except (KeyboardInterrupt, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                status = "interrupted" if isinstance(error, KeyboardInterrupt) else str(error)
            report["suites"][module] = status
            save()
            print(f"{module}  {status}", flush=True)
            if status != "passed":
                break
    if release:
        try:
            check_inputs(records)
            if read_release(args.manifest)[1] != digest:
                raise ValueError("the sealed manifest changed during qualification")
            candidate_manifest.check_source(ROOT, release["source"])
            report["manifest"]["checked_after_suites"] = True
        except (OSError, ValueError, TypeError, KeyError) as error:
            report["manifest"]["error"] = str(error)
            print(f"Final release verification failed: {error}", file=sys.stderr)
        save()
        if not report["manifest"]["checked_after_suites"]:
            return 1
    return int(any(status != "passed" for status in report["suites"].values()))


def cli(argv):
    if len(argv) == 2 and argv[0] == "_worker":
        return worker(argv[1])
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    for event in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(event, interrupt)
    return main(argv)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
