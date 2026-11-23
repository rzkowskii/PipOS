"""Exercise the owner-enrolled SSH workflow on a disposable PipOS disk."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lab import hardware
from lab.vm import ROOT, VM


IMAGE = Path(os.environ.get("BEING_HARDWARE_IMAGE", ROOT / "out/images/beingos-hardware.img")).resolve()
BUNDLE = os.environ.get("BEING_HARDWARE_BUNDLE")
GUEST = "10.0.2.15"
CONTROLLER = "10.0.2.2"  # SLIRP translates the host's loopback forwarding peer.
HOST = "127.0.0.1"
OBSERVE = "being-lab-observe.service"
DEVELOPMENT = "being-lab-dev.service"
PRIVATE = "synthetic private mind fixture"
STAGED = b"not a signed bundle\n"


@unittest.skipUnless(IMAGE.exists(), "requires the LAN-access image or BEING_HARDWARE_IMAGE")
class HardwareSSH(unittest.TestCase):
    def sh(self, command, timeout=40):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def guest_ethernet(self):
        interface = self.sh("udevadm settle --timeout=10 && for p in /sys/class/net/*; do "
                            "if [ -e \"$p/device\" ]; then echo \"${p##*/}\"; fi; done")
        self.assertRegex(interface, r"\A[A-Za-z0-9_.-]{1,15}\Z")
        return interface

    def forward(self, guest_port):
        for _ in range(4):
            with socket.socket() as probe:
                probe.bind((HOST, 0))
                port = probe.getsockname()[1]
            command = f"hostfwd_add net0 tcp:{HOST}:{port}-{GUEST}:{guest_port}"
            result = self.vm.qmp("human-monitor-command", **{"command-line": command})
            if not result.strip():
                return port
        self.fail(f"Could not forward guest port {guest_port}: {result}")

    def auth(self, key, port, *, pins=None):
        return [HOST, "--key", str(key), "--known-hosts", str(pins or self.pins), "--port", str(port)]

    def cli(self, args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = hardware.main(args)
        return status, output.getvalue(), error.getvalue()

    def ssh(self, user, key, port, command=None):
        args = argparse.Namespace(key=key, known_hosts=self.pins, port=port, host=HOST)
        connection = hardware.ssh_command(args, user)
        return connection + ([command] if command is not None else [])

    def run_ssh(self, user, key, port, command, timeout=20):
        return subprocess.run(self.ssh(user, key, port, command), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def wait_active(self, unit, wanted, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = self.sh(f"systemctl show --property=ActiveState --value {unit}", 8)
            if active == wanted:
                return
            time.sleep(0.2)
        self.fail(f"{unit} was {active}, expected {wanted}")

    def collect(self, *, seconds=1, timeout=30):
        args = ["collect", *self.auth(self.observe_key, self.observe_port), "--scenario", "idle-ac",
                "--seconds", str(seconds), "--interval", "1"]
        deadline = time.monotonic() + timeout
        with patch.object(hardware, "RUNS", self.files / "runs"):
            while time.monotonic() < deadline:
                status, output, error = self.cli(args)
                if status == 0:
                    return json.loads(Path(output.strip()).read_text())["metrics"]
                time.sleep(0.25)
        status, log = self.vm.run(f"journalctl -b -u {OBSERVE} -n 20 --no-pager -o cat", 15)
        self.fail(f"Could not collect measurements:\n{error}\nGuest observer journal ({status}):\n{log}")

    def dev_control(self, mode):
        status, output, error = self.cli([mode, *self.auth(self.dev_key, self.observe_port)])
        self.assertEqual(status, 0, error)
        return output

    def wait_for_dev(self, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.run_ssh("root", self.dev_key, self.dev_port, "id -u")
            if result.returncode == 0:
                self.assertEqual(result.stdout.strip(), "0")
                return
            time.sleep(0.25)
        self.fail(f"Could not connect to development:\n{result.stderr}")

    def wait_for_slot(self, expected, timeout=145):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, slot = self.vm.run("cat /run/being-slot-good", 10)
            if status == 0 and slot == expected:
                return
            time.sleep(1)
        _, log = self.vm.run("journalctl -b -u being-boot-health -n 20 --no-pager -o cat", 15)
        self.fail(f"The health gate did not confirm slot {expected}:\n{log}")

    def check_timing_probe(self):
        command = "env PYTHONPATH=/usr/lib/being python3 -m mind.hardware_timing --seconds 1"
        measured = self.run_ssh("root", self.dev_key, self.dev_port, command)
        self.assertEqual(measured.returncode, 0, measured.stderr)
        report = json.loads(measured.stdout)
        self.assertEqual((report["schema_version"], report["window_seconds"]), (1, 1))
        self.assertEqual(set(report["summary"]), {"final_to_first_clause_ms", "final_to_playback_launch_ms",
                                                  "clause_to_playback_launch_ms", "open_to_panel_ready_ms"})
        for values in report["summary"].values():
            self.assertEqual(set(values), {"count", "median_ms", "p95_ms"})
            self.assertIs(type(values["count"]), int)
            for field in ("median_ms", "p95_ms"):
                self.assertTrue(values[field] is None or type(values[field]) in (int, float))

    def install_signed_bundle(self, bundle):
        stage = "/mind/lab-access/lab-candidate.raucb"
        with Path(bundle).open("rb") as source:
            result = subprocess.run(
                self.ssh("root", self.dev_key, self.dev_port, f"umask 077; cat > {stage}"),
                stdin=source, capture_output=True, text=True, timeout=180,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        applied = self.run_ssh("root", self.dev_key, self.dev_port, f"rauc install {stage} && rm {stage}", 240)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

    def test_opt_in_metrics_and_bounded_development_keep_update_boundary(self):
        temp = tempfile.TemporaryDirectory(prefix="being-hardware-lab-")
        self.addCleanup(temp.cleanup)
        self.files = Path(temp.name)
        self.pins = self.files / "verified-hosts"
        self.observe_key, self.dev_key = self.files / "observe", self.files / "develop"
        for key in (self.observe_key, self.dev_key):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=10)

        self.vm = VM(image=IMAGE, maker=("Apple Inc.", "MacBookPro9,2"))
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(140)
        interface = self.guest_ethernet()
        self.sh(f"test ! -f /mind/lab-access/access.json && ! systemctl is-active --quiet {OBSERVE} {DEVELOPMENT} sshd")
        self.observe_port = self.forward(2222)
        self.dev_port = self.forward(2223)

        self.vm.put("/run/observe.pub", self.observe_key.with_suffix(".pub").read_text())
        self.vm.put("/run/develop.pub", self.dev_key.with_suffix(".pub").read_text())
        self.vm.put("/mind/private-hardware-fixture", PRIVATE)
        enrollment = self.sh(f"being-lab-access enroll {GUEST} --controller-ip {CONTROLLER} --ethernet {interface} "
                             "--observe-key /run/observe.pub --dev-key /run/develop.pub")
        if self.vm.run("command -v being-network", 10)[0] == 0:
            self.sh("being-network set paired-lan")
        keys = re.findall(rf"(?m)^\[{re.escape(GUEST)}\]:(222[23]) (ssh-ed25519 [A-Za-z0-9+/=]+)$", enrollment)
        self.assertEqual([port for port, _ in keys], ["2222", "2223"], enrollment)
        self.assertEqual(keys[0][1], keys[1][1], enrollment)
        key = keys[0][1]
        self.pins.write_text(f"[{HOST}]:{self.observe_port} {key}\n[{HOST}]:{self.dev_port} {key}\n")

        report = self.collect()
        self.assertEqual(report["system"]["model"], "MacBookPro9,2", report["system"])
        build_id = self.sh("cat /etc/being/build-id")
        self.assertTrue(build_id)
        self.assertEqual(report["system"]["build_id"], build_id)
        model_sha256 = json.loads(self.sh("cat /models/manifest.json"))["reflex"]["sha256"]
        self.assertRegex(model_sha256, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(report["system"]["model_sha256"], model_sha256)
        self.assertIn(f"inet {GUEST}/24", self.sh(f"ip -4 addr show dev {interface}"))
        self.assertEqual(report["sampling"], {"seconds": 1, "interval": 1})
        self.assertGreaterEqual(report["window"]["elapsed_seconds"], 1)
        self.assertNotIn(PRIVATE, json.dumps(report))
        print("\n  captured installed telemetry over the console-pinned observation connection", flush=True)

        fake_pins = self.files / "wrong-hosts"
        fake_key = " ".join(self.dev_key.with_suffix(".pub").read_text().split()[:2])
        fake_pins.write_text(f"[{HOST}]:{self.observe_port} {fake_key}\n")
        args = ["collect", *self.auth(self.observe_key, self.observe_port, pins=fake_pins),
                "--scenario", "idle-ac", "--seconds", "0"]
        status, _, error = self.cli(args)
        self.assertNotEqual(status, 0)
        self.assertIn("Host key verification failed", error)

        attempts = (("being-observe", self.observe_key, self.observe_port, "cat /mind/private-hardware-fixture"),
                    ("root", self.observe_key, self.observe_port, "dev-on"),
                    ("root", self.dev_key, self.observe_port, "id -u"),
                    ("root", self.dev_key, self.dev_port, "id -u"))
        for user, identity, port, command in attempts:
            with self.subTest(user=user, port=port, command=command):
                denied = self.run_ssh(user, identity, port, command)
                self.assertNotEqual(denied.returncode, 0, denied.stdout)
                self.assertNotIn(PRIVATE, denied.stdout)

        self.assertIn("30 minutes", self.dev_control("open-dev"))
        self.assertEqual(self.sh(f"systemctl show -p RuntimeMaxUSec --value {DEVELOPMENT}"), "30min")
        self.wait_for_dev()
        connection = self.run_ssh("root", self.dev_key, self.dev_port, "printf '%s\\n' \"$SSH_CONNECTION\"")
        self.assertEqual(connection.returncode, 0, connection.stderr)
        self.assertEqual(connection.stdout.split()[0::2], [CONTROLLER, GUEST], connection.stdout)
        self.assertNotEqual(self.run_ssh("root", self.observe_key, self.dev_port, "id -u").returncode, 0)
        if BUNDLE:
            self.check_timing_probe()

        stage = "/run/hardware-probe.raucb"
        result = subprocess.run(
            [sys.executable, "-m", "lab.hardware", "dev", *self.auth(self.dev_key, self.dev_port), "--", "tee", stage],
            input=STAGED, capture_output=True, cwd=ROOT, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, STAGED)
        fetched = self.run_ssh("root", self.dev_key, self.dev_port, f"cat {stage}")
        self.assertEqual((fetched.returncode, fetched.stdout.encode()), (0, STAGED), fetched.stderr)
        self.wait_for_slot(report["system"]["slot"])
        bootenv = "grub-editenv /bootstate/EFI/BOOT/grubenv list"
        before = self.sh(bootenv)
        rejected = self.run_ssh("root", self.dev_key, self.dev_port, f"rauc install {stage}")
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
        self.assertRegex(rejected.stdout + rejected.stderr, r"(?i)(bundle|signature)")
        self.assertEqual(self.sh(bootenv), before)

        heartbeat = self.ssh("root", self.dev_key, self.dev_port, "printf 'ready\\n'; exec sleep 120")
        process = subprocess.Popen(heartbeat, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            readable, _, _ = select.select([process.stdout], [], [], 12)
            self.assertTrue(readable, "the live development command did not start")
            self.assertEqual(process.stdout.readline(), b"ready\n")
            self.assertIn("closed", self.dev_control("close-dev"))
            process.communicate(timeout=12)
            self.assertNotEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.wait_active(DEVELOPMENT, "inactive")
        self.assertNotEqual(self.run_ssh("root", self.dev_key, self.dev_port, "id -u").returncode, 0)

        # Verify expiry promptly using the production unit with a short runtime setting.
        override = f"/run/systemd/system/{DEVELOPMENT}.d"
        self.sh(f"mkdir -p {override}")
        self.vm.put(f"{override}/test-expiry.conf", "[Service]\nRuntimeMaxSec=3s\n")
        self.sh("systemctl daemon-reload")
        self.dev_control("open-dev")
        self.wait_active(DEVELOPMENT, "failed", 15)
        self.assertEqual(self.sh(f"systemctl show -p Result --value {DEVELOPMENT}"), "timeout")
        self.assertNotEqual(self.run_ssh("root", self.dev_key, self.dev_port, "id -u").returncode, 0)
        print("  staged a temporary file, rejected unsigned deployment, and closed and expired dev windows", flush=True)

        if BUNDLE:
            self.sh(f"rm {override}/test-expiry.conf && systemctl daemon-reload")
            self.dev_control("open-dev")
            self.wait_for_dev()
            self.install_signed_bundle(BUNDLE)
            self.assertIn("ORDER=B A", self.sh(bootenv))
            self.dev_control("close-dev")
            print("  streamed the signed candidate and let RAUC install it into the inactive slot", flush=True)

        self.vm.send("reboot\n")
        self.vm.login(140)
        rebooted = self.collect(seconds=0)["system"]
        self.assertEqual(rebooted["model"], "MacBookPro9,2")
        self.assertEqual(rebooted["slot"], "B" if BUNDLE else report["system"]["slot"])
        if BUNDLE:
            self.wait_for_slot("B")
        self.assertEqual(self.sh("cat /mind/private-hardware-fixture"), PRIVATE)
        self.assertNotEqual(self.run_ssh("root", self.dev_key, self.dev_port, "id -u").returncode, 0)
        self.sh(f"test ! -e {stage} && being-lab-access revoke")
        self.wait_active(OBSERVE, "inactive")
        self.assertNotEqual(self.run_ssh("being-observe", self.observe_key, self.observe_port,
                                         "metrics --seconds 0 --interval 1").returncode, 0)
        print("  recovered enrollment after reboot, kept dev closed, and revoked observation", flush=True)
