"""The cable development build (BEING_LAPTOP_CABLE=1, docs/LAB.md), checked on the Mac.

A development Try stick gives root SSH over an Ethernet cable from the
link-local networks only. These tests check its parts without building an
image: the three units and their conditions, the SSH server configuration
(with the Mac's own sshd), the board script that bakes the keys in and
refuses them everywhere else, lab/build.sh's plumbing and refusals, the
guards that keep such a stick from being installed or made into an ISO, and
that no key material is ever tracked. lab/tests/test_cable_dev.py boots the
built stick in QEMU.
"""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from lab.candidate_parity import laptop_violations
from mind import install_apply
from tests.test_build_try_bench import try_image_options
from tests.test_os_image import _Shim, find_openssl3, unit, values


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "os/package/being"
BOARD = ROOT / "os/board/beingos"
CABLE_UNITS = ("being-cable-link.service", "being-cable-ssh.service", "being-cable-workbench.service")
SSHD_CONFIG = PACKAGE / "cable/sshd_config"
INSTALLED = "/usr/share/being/cable/"
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
SSH_KEYGEN = shutil.which("ssh-keygen")


def keygen(path, *, passphrase="", kind="ed25519"):
    """A throwaway key pair at `path` (and `path`.pub), made by ssh-keygen."""
    subprocess.run([SSH_KEYGEN, "-q", "-t", kind, "-N", passphrase, "-C", "throwaway test key", "-f", str(path)],
                   check=True, capture_output=True, timeout=30)
    return path


# A scoped IPv6 link-local address on the Mac's loopback, so a live login can
# arrive the way the cable's does: as fe80::x%<interface>, with a scope.
LINK_LOCAL = "fe80::1%lo0"


def free_port(address="127.0.0.1"):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family) as probe:
        probe.bind(socket.getaddrinfo(address, 0, family, socket.SOCK_STREAM)[0][4])
        return probe.getsockname()[1]


def link_local_loopback():
    """True when this machine can listen on LINK_LOCAL (macOS's lo0 has it)."""
    try:
        free_port(LINK_LOCAL)
    except OSError:
        return False
    return True


def shipped_allowusers():
    """The networks the shipped sshd_config's AllowUsers names for root."""
    line = re.search(r"(?m)^AllowUsers (.*)$", SSHD_CONFIG.read_text())[1]
    return [entry.removeprefix("root@") for entry in line.split()]


class CableUnits(unittest.TestCase):
    def units(self):
        return {name: unit(PACKAGE / name) for name in CABLE_UNITS}

    def test_every_unit_runs_only_on_a_try_boot_with_the_key_and_without_the_kill_switch(self):
        for name, sections in self.units().items():
            with self.subTest(unit=name):
                self.assertEqual(sorted(values(sections, "Unit", "ConditionKernelCommandLine")),
                                 ["!being.cable=0", "being.try=1"])
                self.assertEqual(values(sections, "Unit", "ConditionPathExists"), [INSTALLED + "authorized_keys"])
                self.assertTrue(values(sections, "Install", "WantedBy"), "the preset enables being-*.service")

    def test_the_link_comes_up_after_the_lockdown_and_before_the_policy(self):
        link = self.units()["being-cable-link.service"]
        self.assertEqual(values(link, "Unit", "DefaultDependencies"), ["no"])
        after = " ".join(values(link, "Unit", "After")).split()
        before = " ".join(values(link, "Unit", "Before")).split()
        self.assertIn("being-network-lockdown.service", after)
        self.assertIn("being-cable-workbench.service", after)
        for name in ("being-network-policy.service", "being-cable-ssh.service", "sysinit.target"):
            self.assertIn(name, before)
        self.assertEqual(values(link, "Service", "ExecStart"), ["/usr/sbin/being-network cable-up"])
        self.assertEqual(values(link, "Service", "Type"), ["oneshot"])
        self.assertEqual(values(link, "Service", "RemainAfterExit"), ["yes"])

    def test_the_ssh_server_is_checked_and_can_reach_only_link_local_and_loopback(self):
        ssh = self.units()["being-cable-ssh.service"]
        self.assertEqual(values(ssh, "Unit", "Requires"), ["being-cable-link.service"])
        self.assertIn("being-cable-link.service", " ".join(values(ssh, "Unit", "After")))
        config = INSTALLED + "sshd_config"
        self.assertEqual(values(ssh, "Service", "ExecStartPre"),
                         ["/bin/mkdir -p -m 0755 /var/empty", f"/usr/sbin/sshd -t -f {config}"])
        self.assertEqual(values(ssh, "Service", "ExecStart"), [f"/usr/sbin/sshd -D -e -f {config}"])
        self.assertEqual(values(ssh, "Service", "IPAddressDeny"), ["any"])
        self.assertEqual(values(ssh, "Service", "IPAddressAllow"), ["localhost link-local"])
        self.assertEqual(values(ssh, "Service", "Restart"), ["on-failure"])

    def test_the_workbench_is_in_place_before_any_python_organ_starts(self):
        bench = self.units()["being-cable-workbench.service"]
        self.assertEqual(values(bench, "Unit", "DefaultDependencies"), ["no"])
        before = " ".join(values(bench, "Unit", "Before")).split()
        for name in ("sysinit.target", "being-usb-logs.service", "being-mind.service", "being-voice.service",
                     "being-eyes.service", "being-hands.service", "being-local-screen.service",
                     "being-network-policy.service", "being-cable-link.service"):
            self.assertIn(name, before)
        (script,) = values(bench, "Service", "ExecStart")
        self.assertTrue(script.startswith("/bin/sh -euc '"), script)
        steps = [step.strip() for step in script.split("'", 1)[1].rstrip("'").split(";")]
        self.assertIn("cp -a /usr/lib/being /run/being-dev/lib", steps)
        self.assertIn("cp -a /etc/being /run/being-dev/etc", steps)
        self.assertIn("/usr/bin/python3 -m compileall -q -f --invalidation-mode timestamp -d /usr/lib/being "
                      "/run/being-dev/lib", steps)
        # Mounted only after the copy is complete and recompiled.
        self.assertEqual(steps[-2:], ["mount --bind /run/being-dev/lib /usr/lib/being",
                                      "mount --bind /run/being-dev/etc /etc/being"])
        self.assertNotIn("overlay", script)

    def test_the_units_and_server_configuration_ship_only_with_the_cable_option(self):
        recipe = (PACKAGE / "being.mk").read_text()
        block = re.search(r"^ifeq \(\$\(BR2_PACKAGE_BEING_CABLE_DEV\),y\)\n(.*?)^endif$", recipe, re.M | re.S)
        self.assertIsNotNone(block)
        outside = recipe.replace(block[0], "")
        for name in (*CABLE_UNITS, "cable/sshd_config"):
            self.assertIn(f"$(BEING_PKGDIR)/{name}", block[1])
            self.assertNotIn(f"$(BEING_PKGDIR)/{name}", outside)
        self.assertIn("$(BEING_INSTALL_CABLE_DEV)", outside)
        self.assertIn("$(BEING_INSTALL_CABLE_DEV_UNITS)", outside)
        option = (PACKAGE / "Config.in").read_text().split("config BR2_PACKAGE_BEING_CABLE_DEV\n", 1)[1]
        option = option.split("\nconfig ", 1)[0]
        self.assertIn("depends on BR2_PACKAGE_OPENSSH_SERVER", option)
        self.assertNotIn("default", option)

    def test_pip_diag_ships_on_every_image(self):
        recipe = (PACKAGE / "being.mk").read_text()
        install = re.search(r"^define BEING_INSTALL_TARGET_CMDS\n(.*?)^endef$", recipe, re.M | re.S)[1]
        self.assertIn("$(INSTALL) -D -m 0755 $(BEING_PKGDIR)/pip-diag $(TARGET_DIR)/usr/sbin/pip-diag", install)

    def test_the_mind_never_names_the_cable_units(self):
        for path in sorted((ROOT / "mind").glob("*.py")):
            with self.subTest(module=path.name):
                self.assertNotRegex(path.read_text(errors="replace"), r"being-cable-[a-z]+\.service")

    def test_the_kernel_fragment_reaches_only_the_cable_build(self):
        fragment = {line.split("=")[0]: line.split("=", 1)[1]
                    for line in (BOARD / "linux-cable.config").read_text().splitlines()
                    if line and not line.startswith("#")}
        for option in ("CONFIG_SND_HDA_HWDEP", "CONFIG_SND_HDA_RECONFIG", "CONFIG_SND_HDA_PATCH_LOADER",
                       "CONFIG_SND_DEBUG", "CONFIG_SND_DEBUG_VERBOSE", "CONFIG_IPV6", "CONFIG_CGROUP_BPF"):
            self.assertEqual(fragment.get(option), "y", option)
        for defconfig in sorted((ROOT / "os/configs").glob("*_defconfig")):
            self.assertNotIn("linux-cable.config", defconfig.read_text(), defconfig.name)
            self.assertNotIn("BEING_CABLE_DEV=y", defconfig.read_text(), defconfig.name)


@unittest.skipUnless(Path(SSHD).exists() and SSH_KEYGEN, "needs OpenSSH's sshd and ssh-keygen on this Mac")
class CableSshdConfig(unittest.TestCase):
    """os/package/being/cable/sshd_config against the Mac's own OpenSSH."""

    def setUp(self):
        # sshd applies StrictModes to the key's parent directories. Keep
        # this fixture under the user's home even when TMPDIR is shared.
        scratch = self.enterContext(tempfile.TemporaryDirectory(prefix=".pipos-sshd-", dir=Path.home()))
        self.folder = Path(scratch).resolve()
        keygen(self.folder / "ssh_host_ed25519_key")
        self.client = keygen(self.folder / "client")
        self.write_keys(peers=self.peers())

    @staticmethod
    def peers():
        """The from= networks post-build.sh gives the authorized key."""
        return re.search(r'^PEERS = "([^"]+)"$', (BOARD / "post-build.sh").read_text(), re.M)[1]

    def write_keys(self, *, peers):
        public = " ".join((self.folder / "client.pub").read_text().split()[:2])
        keys = self.folder / "authorized_keys"
        keys.write_text(f'from="{peers}",restrict,pty {public}\n')
        keys.chmod(0o644)

    def config(self, text=None):
        """The shipped file with its paths moved into the scratch folder."""
        text = (text or SSHD_CONFIG.read_text()).replace(INSTALLED, f"{self.folder}/")
        text = re.sub(r"(?m)^PidFile .*$", f"PidFile {self.folder}/sshd.pid", text)
        path = self.folder / "sshd_config"
        path.write_text(text)
        return path

    def test_the_shipped_file_passes_sshd_t(self):
        result = subprocess.run([SSHD, "-t", "-f", str(self.config())], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_effective_settings_allow_only_root_by_key_from_link_local(self):
        for address in ("169.254.10.10", "10.0.2.2", "fe80::1", "fe80::1%1", LINK_LOCAL):
            with self.subTest(address=address):
                result = subprocess.run([SSHD, "-T", "-f", str(self.config()), "-C",
                                         f"user=root,host=mac,addr={address}"],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                settings = [line.split(" ", 1) for line in result.stdout.splitlines() if " " in line]
                found = {}
                for key, value in settings:
                    found.setdefault(key, []).append(value)
                self.assertEqual(found["allowusers"], ["root@169.254.0.0/16", "root@fe80:*"])
                for key, value in (("passwordauthentication", "no"), ("kbdinteractiveauthentication", "no"),
                                   ("pubkeyacceptedalgorithms", "ssh-ed25519"), ("hostkeyalgorithms", "ssh-ed25519"),
                                   ("permitrootlogin", "prohibit-password"), ("disableforwarding", "yes"),
                                   ("permittunnel", "no"), ("authenticationmethods", "publickey"),
                                   ("usepam", "no"), ("strictmodes", "yes"), ("port", "22")):
                    self.assertEqual(found.get(key), [value], key)
                self.assertEqual(found["subsystem"], ["sftp internal-sftp"])

    def live(self, allow=None, *, address="127.0.0.1"):
        """Run the shipped configuration as this user on `address` (IPv4 loopback,
        or LINK_LOCAL for a scoped IPv6 peer), with AllowUsers naming `allow`
        (default: the shipped networks) for this user, and try one login with
        the client key. sshd sees the client at the same address."""
        user = os.environ.get("USER") or subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        port = free_port(address)
        six = ":" in address
        text = SSHD_CONFIG.read_text()
        text = text.replace("Port 22\n", f"Port {port}\n")
        text = re.sub(r"(?m)^AddressFamily any\n", f"AddressFamily {'inet6' if six else 'inet'}\n", text)
        text = re.sub(r"(?m)^ListenAddress .*\n", "", text) + f"ListenAddress {address}\n"
        allowed = " ".join(f"{user}@{network}" for network in (shipped_allowusers() if allow is None else allow))
        text = re.sub(r"(?m)^AllowUsers .*$", f"AllowUsers {allowed}", text)
        # Not root here, so the account is this user's own.
        text = text.replace("PermitRootLogin prohibit-password", "PermitRootLogin no")
        log = self.folder / "sshd.log"
        with log.open("w") as output:
            server = subprocess.Popen([SSHD, "-D", "-e", "-f", str(self.config(text))], stdout=output,
                                      stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while "Server listening" not in log.read_text() and time.monotonic() < deadline:
                if server.poll() is not None:
                    self.fail(f"sshd stopped: {log.read_text()}")
                time.sleep(0.05)
            login = subprocess.run(
                ["ssh", "-F", "/dev/null", "-i", str(self.client), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={self.folder}/known_hosts",
                 "-o", "ConnectTimeout=10", "-p", str(port), f"{user}@{address}", "echo logged-in"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
        finally:
            server.terminate()
            server.wait(timeout=10)
        return login, log.read_text()

    def test_allowusers_refuses_a_source_outside_the_link_local_networks(self):
        # The key's from= allows loopback here, so only AllowUsers can refuse.
        self.write_keys(peers="127.0.0.1")
        login, log = self.live()
        self.assertNotEqual(login.returncode, 0)
        self.assertNotIn("logged-in", login.stdout)
        self.assertIn("not allowed because not listed in AllowUsers", log)

    @unittest.skipUnless(link_local_loopback(), f"needs {LINK_LOCAL} to listen on")
    def test_the_shipped_rules_admit_a_scoped_ipv6_link_local_peer(self):
        # The cable's first path (Host pip, fe80::77:1%en0): sshd sees the Mac
        # as fe80::x%<port>. Both the shipped AllowUsers and the key's from=
        # (the PEERS that post-build.sh writes, from setUp) must admit it.
        login, log = self.live(address=LINK_LOCAL)
        self.assertEqual(login.returncode, 0, login.stderr + log)
        self.assertEqual(login.stdout.strip(), "logged-in")
        self.assertIn(f"from {LINK_LOCAL}", log)

    @unittest.skipUnless(link_local_loopback(), f"needs {LINK_LOCAL} to listen on")
    def test_a_link_local_cidr_would_refuse_that_peer(self):
        # Why the shipped files use the fe80:* pattern: OpenSSH compares the
        # peer's scope with the CIDR's, so fe80::/10 never matches it.
        login, log = self.live(["169.254.0.0/16", "fe80::/10"], address=LINK_LOCAL)
        self.assertNotEqual(login.returncode, 0)
        self.assertIn("not allowed because not listed in AllowUsers", log)
        self.write_keys(peers="169.254.0.0/16,fe80::/10")
        login, log = self.live(address=LINK_LOCAL)
        self.assertNotEqual(login.returncode, 0)
        self.assertIn("not from a permitted host", log)

    def test_the_keys_from_option_refuses_a_source_outside_the_link_local_networks(self):
        login, log = self.live(["127.0.0.1"])
        self.assertNotEqual(login.returncode, 0)
        self.assertNotIn("logged-in", login.stdout)
        self.assertRegex(log, r"not permitted to use this key|is not permitted|Authentication tried")

    def test_the_same_configuration_admits_the_key_from_an_allowed_source(self):
        # The control: with loopback allowed in both places, the login works.
        self.write_keys(peers="127.0.0.1")
        login, log = self.live(["127.0.0.1"])
        self.assertEqual(login.returncode, 0, login.stderr + log)
        self.assertEqual(login.stdout.strip(), "logged-in")


@unittest.skipUnless(find_openssl3() and SSH_KEYGEN, "needs OpenSSL 3 and ssh-keygen on the Mac")
class CablePostBuild(unittest.TestCase):
    """os/board/beingos/post-build.sh: the keys in the cable build, and nowhere else."""

    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        (self.bin / "install").write_text(_Shim.INSTALL)
        (self.bin / "install").chmod(0o755)
        (self.bin / "openssl").symlink_to(find_openssl3())
        self.lab = self.work / "lab"
        self.lab.mkdir()
        subprocess.run([find_openssl3(), "req", "-new", "-x509", "-newkey", "rsa:2048", "-noenc", "-days", "30",
                        "-subj", "/CN=PipOS Lab Updates", "-keyout", str(self.lab / "key.pem"),
                        "-out", str(self.lab / "cert.pem")], check=True, capture_output=True, timeout=60)
        self.keys = self.work / "beingos-cable"
        self.keys.mkdir(mode=0o700)
        self.client = keygen(self.work / "client")
        keygen(self.work / "host")
        (self.keys / "authorized_key").write_text(" ".join((self.work / "client.pub").read_text().split()[:2]) + "\n")
        shutil.copy(self.work / "host", self.keys / "ssh_host_ed25519_key")

    def tree(self, *, cable=True, sshd=True):
        target = Path(tempfile.mkdtemp(dir=self.work))
        dynload = target / "usr/lib/python3.14/lib-dynload"
        dynload.mkdir(parents=True)
        (dynload / "_ssl.cpython-314-x86_64-linux-gnu.so").write_bytes(b"\0")
        (target / "etc/systemd/system").mkdir(parents=True)
        (target / "etc/being").mkdir()
        (target / "etc/fstab").write_text("/dev/root / ext2 rw 0 1\n")
        mind = target / "usr/lib/being/mind"
        mind.mkdir(parents=True)
        (mind / "model_pack.py").write_text('CERT = "/usr/share/being/model-pack.pem"\n')
        if sshd:
            (target / "usr/sbin").mkdir(parents=True)
            (target / "usr/sbin/sshd").write_text("#!/bin/sh\n")
            (target / "usr/sbin/sshd").chmod(0o755)
        if cable:
            # What BR2_PACKAGE_BEING_CABLE_DEV installs (being.mk).
            (target / "usr/share/being/cable").mkdir(parents=True)
            shutil.copy(SSHD_CONFIG, target / "usr/share/being/cable/sshd_config")
            units = target / "usr/lib/systemd/system"
            units.mkdir(parents=True, exist_ok=True)
            for name in CABLE_UNITS:
                shutil.copy(PACKAGE / name, units / name)
        binaries = Path(tempfile.mkdtemp(dir=self.work))
        (binaries / "bzImage").write_bytes(b"kernel")
        (binaries / "efi-part/EFI/BOOT").mkdir(parents=True)
        (binaries / "efi-part/EFI/BOOT/bootx64.efi").write_bytes(b"efi")
        return target, binaries

    def post_build(self, *args, cable=True, sshd=True, target=None):
        if target is None:
            target, binaries = self.tree(cable=cable, sshd=sshd)
        else:
            target, binaries = target
        env = dict(os.environ, TARGET_DIR=str(target), BINARIES_DIR=str(binaries), BEINGOS_LAB_KEYS=str(self.lab),
                   PATH=f"{self.bin}:{os.environ['PATH']}", BEINGOS_RELEASE_CERT="",
                   BEINGOS_REQUIRE_RELEASE_KEY="0", BEINGOS_CABLE_KEYS=str(self.keys))
        result = subprocess.run(["/bin/sh", str(BOARD / "post-build.sh"), str(target), *args],
                                env=env, text=True, capture_output=True, timeout=60)
        return result, target

    def test_the_cable_build_carries_the_keys_with_safe_modes(self):
        result, target = self.post_build("laptop", "cable")
        self.assertEqual(result.returncode, 0, result.stderr)
        cable = target / "usr/share/being/cable"
        public = " ".join((self.work / "client.pub").read_text().split()[:2])
        self.assertEqual((cable / "authorized_keys").read_text(),
                         f'from="169.254.0.0/16,fe80:*",restrict,pty {public}\n')
        self.assertNotIn("throwaway test key", (cable / "authorized_keys").read_text())
        self.assertEqual((cable / "ssh_host_ed25519_key").read_text().strip(), (self.work / "host").read_text().strip())
        derived = subprocess.run([SSH_KEYGEN, "-y", "-f", str(self.work / "host")], capture_output=True, text=True,
                                 check=True).stdout.split()[:2]
        self.assertEqual((cable / "ssh_host_ed25519_key.pub").read_text(), " ".join(derived) + "\n")
        for name, mode in (("authorized_keys", 0o644), ("ssh_host_ed25519_key", 0o600),
                           ("ssh_host_ed25519_key.pub", 0o644), ("sshd_config", 0o644)):
            self.assertEqual((cable / name).stat().st_mode & 0o777, mode, name)
        self.assertEqual(cable.stat().st_mode & 0o777, 0o755)
        self.assertEqual((target / "usr/share/being/variant").read_text(), "laptop\n")
        self.assertIn("keep it private", result.stderr)

    def test_the_cable_build_fails_without_its_keys_or_parts(self):
        (self.keys / "ssh_host_ed25519_key").unlink()
        result, _ = self.post_build("laptop", "cable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lab/build.sh cable-key", result.stderr)
        shutil.copy(self.work / "host", self.keys / "ssh_host_ed25519_key")
        result, _ = self.post_build("laptop", "cable", sshd=False)
        self.assertIn("OpenSSH's server", result.stderr)
        result, _ = self.post_build("laptop", "cable", cable=False)
        self.assertIn("BR2_PACKAGE_BEING_CABLE_DEV", result.stderr)
        result, _ = self.post_build("lab", "cable")
        self.assertIn("is a laptop build", result.stderr)
        result, _ = self.post_build("laptop", "maintenance")
        self.assertIn("unknown build option", result.stderr)

    def test_the_cable_build_refuses_bad_keys(self):
        encrypted = keygen(self.work / "encrypted", passphrase="correct horse battery")
        rsa = keygen(self.work / "rsa", kind="rsa")
        cases = (
            ("authorized_key", (self.work / "host").read_text(), "public key"),
            ("authorized_key", (self.work / "client.pub").read_text() * 2, "exactly one"),
            ("authorized_key", (self.work / "rsa.pub").read_text(), "ssh-ed25519"),
            ("ssh_host_ed25519_key", encrypted.read_text(), "unencrypted"),
            ("ssh_host_ed25519_key", rsa.read_text(), "Ed25519"),
            ("ssh_host_ed25519_key", "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n",
             "not an OpenSSH private key"),
            ("ssh_host_ed25519_key", (self.work / "client").read_text(), "different keys"),
        )
        for name, contents, message in cases:
            with self.subTest(name=name, message=message):
                original = (self.keys / name).read_text()
                (self.keys / name).write_text(contents)
                try:
                    result, target = self.post_build("laptop", "cable")
                finally:
                    (self.keys / name).write_text(original)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_every_other_image_is_refused_if_it_holds_any_cable_file(self):
        for variant in ("laptop", "lab"):
            with self.subTest(variant=variant):
                result, _ = self.post_build(variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only BEING_LAPTOP_CABLE=1 builds may", result.stderr)
        # A unit alone is refused too.
        target, binaries = self.tree(cable=False)
        units = target / "usr/lib/systemd/system"
        units.mkdir(parents=True, exist_ok=True)
        shutil.copy(PACKAGE / "being-cable-ssh.service", units / "being-cable-ssh.service")
        result, _ = self.post_build("laptop", target=(target, binaries))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/usr/lib/systemd/system/being-cable-ssh.service", result.stderr)
        # And an ordinary laptop tree, keys or not in the builder, carries none.
        result, target = self.post_build("laptop", cable=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((target / "usr/share/being/cable").exists())


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
root = os.environ["FAKE_ROOT"]
args = sys.argv[1:]
with open(os.path.join(root, "docker-calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:1] == ["--context"]:
    args = args[2:]
if args[:1] != ["run"]:
    sys.exit(0)
args = args[1:]
env = {}
while args and args[0] != "beingos-builder":
    option = args.pop(0)
    if option == "-e":
        key, _, value = args.pop(0).partition("=")
        env[key] = value
    elif option in ("-v", "--context"):
        args.pop(0)
command = args[1:]
runs = ("try-image.sh", "cable-key")
if not any(any(word in part for word in runs) for part in command):
    sys.exit(0)
def mapped(part):
    return part.replace("/src/", root + "/").replace("/work/", root + "/work/")
command = [mapped(part) for part in command]
child = {key: value for key, value in os.environ.items() if not key.startswith("BEING_TRY_")}
child.update(env)
os.execvpe(command[0], command, child)
'''


class CableBuildPlumbing(unittest.TestCase):
    """lab/build.sh with BEING_LAPTOP_CABLE=1, against stand-in colima and docker."""

    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.scratch = scratch
        self.root = scratch / "tree"
        (self.root / "lab").mkdir(parents=True)
        shutil.copy2(ROOT / "lab/build.sh", self.root / "lab/build.sh")
        (self.root / "os/configs").mkdir(parents=True)
        shutil.copy2(ROOT / "os/configs/beingos_laptop_defconfig", self.root / "os/configs/beingos_laptop_defconfig")
        (self.root / "mind").symlink_to(ROOT / "mind")
        board = self.root / "os/board/beingos"
        board.mkdir(parents=True)
        (board / "try-image.sh").write_text(try_image_options())
        for tree in ("x86_64", "laptop", "laptop-maintenance", "laptop-cable"):
            images = self.root / "work/output" / tree / "images"
            (images / "efi-part/EFI/BOOT").mkdir(parents=True)
            (self.root / "work/output" / tree / "host").mkdir()
            for name in ("efi-part/EFI/BOOT/bootx64.efi", "rootfs.squashfs", "models.ext4"):
                (images / name).write_text("fixture")
        commands = scratch / "bin"
        commands.mkdir()
        (commands / "colima").write_text("#!/bin/sh\nexit 0\n")
        (commands / "docker").write_text(FAKE_DOCKER)
        for command in commands.iterdir():
            command.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("BEING_", "BEINGOS_", "PYTHONPATH"))}
        self.env.update(PATH=str(commands) + os.pathsep + os.environ["PATH"], FAKE_ROOT=str(self.root),
                        PYTHONDONTWRITEBYTECODE="1")

    def build(self, *argv, **env):
        return subprocess.run(["bash", "lab/build.sh", *argv], cwd=self.root, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        log = self.root / "docker-calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def images(self):
        folder = self.root / "out/images"
        return {path.name: path.read_text().strip() for path in folder.glob("*.img")} if folder.exists() else {}

    def test_the_cable_stick_has_its_own_tree_name_options_and_configuration(self):
        result = self.build("laptop-try", BEING_LAPTOP_CABLE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.images(), {"beingos-laptop-cable-try.img": "being.screen=1 being.cable=1 being.voice_debug=1"})
        calls = self.calls()
        makes = [call for call in calls if "make" in call]
        self.assertTrue(makes)
        for call in makes:
            self.assertIn("O=/work/output/laptop-cable", call)
        settings = [call for call in calls if "laptop-set" in call]
        self.assertEqual(len(settings), 1, settings)
        values = settings[0][settings[0].index("laptop-set") + 2:]
        self.assertIn("BR2_PACKAGE_BEING_CABLE_DEV=y", values)
        self.assertIn("BR2_PACKAGE_OPENSSH_SERVER=y", values)
        self.assertIn("BR2_PACKAGE_BEING_LAN_MAINTENANCE=n", values)
        self.assertIn('BR2_ROOTFS_POST_SCRIPT_ARGS="laptop cable"', values)
        self.assertIn('BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="$(BR2_EXTERNAL_BEINGOS_PATH)/board/beingos/'
                      'linux-laptop.config $(BR2_EXTERNAL_BEINGOS_PATH)/board/beingos/linux-cable.config"', values)
        checks = [call for call in calls if "laptop-check" in call or "laptop-verify" in call]
        self.assertEqual(len(checks), 2)
        for call in checks:
            # Arguments: tree, maintenance, cable, diagnostic.
            self.assertEqual(call[-4:], ["/work/output/laptop-cable", "0", "1", "0"])
        for call in calls:
            if call[:3] == ["--context", "colima-beingos", "run"]:
                self.assertIn("BEINGOS_CABLE_KEYS=/work/beingos-cable", call)

    def test_ordinary_laptop_builds_never_set_the_cable_options(self):
        for env in ({}, {"BEING_LAPTOP_MAINTENANCE": "1"}, {"BEING_LAPTOP_CABLE": "0"}):
            with self.subTest(env=env):
                (self.root / "docker-calls.jsonl").unlink(missing_ok=True)
                self.assertEqual(self.build("laptop-try", **env).returncode, 0)
                calls = self.calls()
                settings = [call[call.index("laptop-set") + 2:] for call in calls if "laptop-set" in call]
                for values in settings:
                    self.assertFalse([value for value in values if "CABLE" in value or "linux-cable" in value
                                      or "laptop cable" in value], values)
                for call in calls:
                    self.assertNotIn("O=/work/output/laptop-cable", call)
                    if "laptop-check" in call or "laptop-verify" in call:
                        self.assertEqual(call[-1], "0")
        self.assertNotIn("beingos-laptop-cable-try.img", self.images())

    def test_cable_builds_refuse_maintenance_release_and_bad_values_before_building(self):
        for env, message in (({"BEING_LAPTOP_CABLE": "1", "BEING_LAPTOP_MAINTENANCE": "1"}, "separate builds"),
                             ({"BEING_LAPTOP_CABLE": "1", "BEINGOS_REQUIRE_RELEASE_KEY": "1"}, "never installed"),
                             ({"BEING_LAPTOP_CABLE": "yes"}, "BEING_LAPTOP_CABLE must be 0 or 1")):
            for command in ("laptop-try", "laptop", "laptop-config"):
                with self.subTest(env=env, command=command):
                    (self.root / "docker-calls.jsonl").unlink(missing_ok=True)
                    result = self.build(command, **env)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(self.calls(), [])

    def test_a_cable_build_is_never_signed_or_made_a_candidate(self):
        key = self.scratch / "release-key.pem"
        key.write_text("not a key\n")
        result = self.build("laptop-bundle", str(key), BEING_LAPTOP_CABLE="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("never signed as an update", result.stderr)
        self.assertFalse([call for call in self.calls() if "laptop-bundle" in call])
        result = self.build("candidate", "cable-test", BEING_LAPTOP_CABLE="1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("never carries cable or diagnostic development tooling", result.stderr)
        self.assertFalse((self.root / "out/candidates").exists())

    def test_try_image_refuses_a_cable_stick_that_is_not_a_laptop_stick(self):
        inputs = [self.scratch / name for name in ("host", "bootx64.efi", "rootfs.squashfs", "models.ext4")]
        inputs[0].mkdir()
        for path in inputs[1:]:
            path.write_text("fixture")
        result = subprocess.run(["sh", str(self.root / "os/board/beingos/try-image.sh"), *map(str, inputs),
                                 str(self.scratch / "out")], env=dict(self.env, BEING_TRY_CABLE="1"),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("needs BEING_TRY_LAPTOP=1", result.stderr)

    def test_the_real_try_image_checks_the_rootfs_matches_the_stick(self):
        script = (BOARD / "try-image.sh").read_text()
        self.assertIn("unsquashfs -l -no-progress \"$rootfs\"", script)
        self.assertIn("^squashfs-root/usr/share/being/cable(/|$)", script)

    # -- cable-key ------------------------------------------------------------

    @unittest.skipUnless(SSH_KEYGEN, "needs ssh-keygen")
    def test_cable_key_imports_the_two_keys_privately_and_prints_the_pin(self):
        keys = self.scratch / "keys"
        keys.mkdir()
        client, host = keygen(keys / "login"), keygen(keys / "host")
        result = self.build("cable-key", f"{client}.pub", str(host))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stored = self.root / "work/beingos-cable"
        self.assertEqual(stored.stat().st_mode & 0o777, 0o700)
        self.assertEqual((stored / "authorized_key").read_text(),
                         " ".join(Path(f"{client}.pub").read_text().split()[:2]) + "\n")
        self.assertEqual((stored / "ssh_host_ed25519_key").read_text(), host.read_text())
        for name in ("authorized_key", "ssh_host_ed25519_key"):
            self.assertEqual((stored / name).stat().st_mode & 0o777, 0o600, name)
        self.assertEqual(sorted(path.name for path in stored.iterdir()), ["authorized_key", "ssh_host_ed25519_key"])
        pin = " ".join(Path(f"{host}.pub").read_text().split()[:2])
        self.assertIn(f"pip,169.254.77.1,fe80::77:1 {pin}\n", result.stdout)
        self.assertNotIn("PRIVATE", result.stdout + result.stderr)

    @unittest.skipUnless(SSH_KEYGEN, "needs ssh-keygen")
    def test_cable_key_refuses_keys_in_the_repository_private_logins_and_unusable_host_keys(self):
        keys = self.scratch / "keys"
        keys.mkdir()
        client, host = keygen(keys / "login"), keygen(keys / "host")
        inside = keygen(self.root / "inside")
        (self.root / "linked").symlink_to(keys)
        (keys / "back").symlink_to(self.root / "inside")
        encrypted = keygen(keys / "encrypted", passphrase="correct horse battery")
        rsa = keygen(keys / "rsa", kind="rsa")
        for args, message in (
                ((f"{inside}.pub", str(host)), "inside the repository"),
                ((f"{client}.pub", str(inside)), "inside the repository"),
                ((f"{client}.pub", str(keys / "back")), "inside the repository"),
                ((str(client), str(host)), "holds a private key"),
                ((f"{rsa}.pub", str(host)), "Ed25519"),
                ((f"{client}.pub", str(encrypted)), "encrypted or unreadable"),
                ((f"{client}.pub", f"{host}.pub"), "not an OpenSSH private key"),
                ((f"{client}.pub", str(rsa)), "not an Ed25519 key"),
                ((f"{host}.pub", str(host)), "the same key"),
                ((f"{client}.pub",), "usage"),
                ((f"{client}.pub", str(keys / "missing")), "No such file")):
            with self.subTest(args=args):
                result = self.build("cable-key", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.root / "work/beingos-cable").exists())
        # A key reached through a link from inside the repository is still outside it.
        result = self.build("cable-key", str(self.root / "linked/login.pub"), str(host))
        self.assertEqual(result.returncode, 0, result.stderr)


class CableNeverInstalls(unittest.TestCase):
    def test_install_apply_refuses_a_cable_stick_before_touching_any_disk(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "cable").mkdir()
        with (patch.object(install_apply, "CABLE_MEDIA", folder / "cable"),
              patch.object(install_apply.sys, "platform", "linux"),
              patch.object(install_apply.os, "geteuid", return_value=0),
              patch.object(install_apply, "is_try_boot", return_value=True),
              patch.object(install_apply, "geometry") as geometry,
              patch.object(install_apply, "source_device") as source):
            with self.assertRaisesRegex(ValueError, "cable development stick"):
                install_apply.apply_installation({"target": {}, "partitions": []}, lambda: None)
        geometry.assert_not_called()
        source.assert_not_called()

    def test_make_iso_refuses_the_cable_tree_by_name(self):
        result = subprocess.run(["bash", str(ROOT / "lab/make-iso.sh"),
                                 str(Path(self.enterContext(tempfile.TemporaryDirectory())))],
                                env=dict(os.environ, BEING_ISO_IMAGES="/work/output/laptop-cable/images"),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("never becomes an ISO", result.stderr)

    def test_every_media_path_checks_the_rootfs_for_the_cable(self):
        pattern = "^squashfs-root/usr/share/being/cable(/|"
        self.assertIn(pattern, (ROOT / "lab/make-iso.sh").read_text())
        self.assertIn(pattern, (ROOT / "lab/build.sh").read_text().split("laptop_bundle() {", 1)[1])
        candidate = (ROOT / "lab/candidate-build.sh").read_text()
        self.assertIn("BR2_PACKAGE_BEING_CABLE_DEV", candidate)
        self.assertIn('candidate_parity.py" laptop-check "$t"', candidate.split("candidate_laptop_verify() {", 1)[1])
        self.assertTrue(laptop_violations(["usr/share/being/cable"]))


@unittest.skipUnless(SSH_KEYGEN and shutil.which("bash"), "needs ssh-keygen and bash on the Mac")
class CableMacHelper(unittest.TestCase):
    """lab/cable.sh with a stand-in ssh: its exit status and its temporary file."""

    FAKE_SSH = """#!/bin/sh
# Records the -F file's contents and the arguments, then exits as told.
while [ $# -gt 0 ]; do
    case "$1" in
        -F) printf '%s\\n' "$2" > "$CABLE_TEST_DIR/config-path"; cp "$2" "$CABLE_TEST_DIR/config"; shift 2 ;;
        *) printf '%s\\n' "$1" >> "$CABLE_TEST_DIR/args"; shift ;;
    esac
done
cat > "$CABLE_TEST_DIR/stdin"
exit "${CABLE_TEST_STATUS:-0}"
"""

    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        (self.bin / "ssh").write_text(self.FAKE_SSH)
        (self.bin / "ssh").chmod(0o755)
        self.tmp = self.work / "tmp"
        self.tmp.mkdir()
        keygen(self.work / "login")
        keygen(self.work / "host")

    def cable(self, *args, status=0):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", TMPDIR=str(self.tmp),
                   CABLE_TEST_DIR=str(self.work), CABLE_TEST_STATUS=str(status),
                   PIPOS_CABLE_IDENTITY=str(self.work / "login"), PIPOS_CABLE_HOST_KEY=str(self.work / "host.pub"),
                   PIPOS_CABLE_KNOWN_HOSTS=str(self.work / "known_hosts"))
        # /bin/bash (3.2 on the Mac) is what the script's #! line runs.
        bash = shutil.which("bash", path="/bin") or "bash"
        return subprocess.run([bash, str(ROOT / "lab/cable.sh"), *args], env=env, cwd=ROOT,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def test_push_restarts_every_running_organ_that_runs_mind_code(self):
        result = self.cable("push")
        self.assertEqual(result.returncode, 0, result.stderr)
        remote = (self.work / "args").read_text()
        organs = ("being-mind.service being-voice.service being-hands.service being-eyes.service "
                  "being-local-screen.service being-wifi.service")
        self.assertIn(f"systemctl try-restart {organs}\n", remote)
        self.assertIn(f"systemctl is-active {organs}", remote)
        self.assertNotIn("being-usb-logs", remote.split("tar -x", 1)[1], "it holds the PIPLOGS mount")
        names = subprocess.run(["tar", "-t", "-f", str(self.work / "stdin")], capture_output=True, text=True,
                               check=True).stdout.split()
        self.assertIn("hands.py", names)
        self.assertIn("local_menu.py", names)
        quiet = self.cable("push", "--no-restart")
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual((self.work / "args").read_text().count("systemctl"), 3, "only the first push restarts")

    def test_ssh_returns_the_remote_status_and_leaves_no_temporary_file(self):
        for status in (0, 1, 3):
            with self.subTest(status=status):
                result = self.cable("ssh", "pip-diag", "quick", "--json", status=status)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertNotIn("unbound variable", result.stderr)
                self.assertEqual(list(self.tmp.iterdir()), [])
        used = (self.work / "config-path").read_text().strip()
        self.assertTrue(used.startswith(str(self.tmp)), used)
        config = (self.work / "config").read_text()
        self.assertIn("Host pip\n  HostName fe80::77:1%%en0\n", config)
        self.assertIn(f"IdentityFile {self.work / 'login'}\n", config)
        self.assertEqual((self.work / "args").read_text().split("\n")[:4], ["pip", "pip-diag", "quick", "--json"])

    def test_the_pin_file_holds_only_the_host_keys_public_half(self):
        result = self.cable("ssh-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        public = " ".join((self.work / "host.pub").read_text().split()[:2])
        self.assertEqual((self.work / "known_hosts").read_text(), f"pip,169.254.77.1,fe80::77:1 {public}\n")
        self.assertIn("Host pip4\n  HostName 169.254.77.1\n  BindInterface en0\n", result.stdout)
        self.assertEqual(list(self.tmp.iterdir()), [])


class NoKeyMaterialTracked(unittest.TestCase):
    PRIVATE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n[A-Za-z0-9+/=]{40,}")
    # An Ed25519 public key: the algorithm name's encoding, then 32 bytes.
    PUBLIC = re.compile(r"AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}")

    def test_no_tracked_file_holds_a_key(self):
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=60)
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        for name in listing.stdout.decode().split("\0"):
            if not name:
                continue
            base = Path(name).name
            with self.subTest(path=name):
                self.assertNotRegex(base, r"^(authorized_keys?|ssh_host_.*_key(\.pub)?|id_(ed25519|rsa|ecdsa).*"
                                          r"|pipos_cable.*)$")
                self.assertFalse(name.startswith("os/package/being/cable/") and base != "sshd_config", name)
                path = ROOT / name
                if not path.is_file() or path.stat().st_size > 4 << 20:
                    continue
                text = path.read_bytes().decode("utf-8", "replace")
                self.assertIsNone(self.PRIVATE.search(text))
                self.assertIsNone(self.PUBLIC.search(text))


if __name__ == "__main__":
    unittest.main()
