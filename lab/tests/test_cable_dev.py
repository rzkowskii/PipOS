"""Boot the cable development Try stick in QEMU and use it over the cable link.

The stick (out/images/beingos-laptop-cable-try.img, from `BEING_LAPTOP_CABLE=1
lab/build.sh laptop-try`, docs/LAB.md) boots over virtual USB beside a blank
internal disk, as on the MacBook. QEMU's user network stands in for the
Ethernet cable: it is given the link-local network itself
(net=169.254.0.0/16, host=169.254.162.20, the Mac's self-assigned address),
so a forwarded connection reaches Pip from a link-local address, as the Mac
does. libslirp accepts that network and a forward to 169.254.77.1:22 (checked
with QEMU 11.1 and no guest); Pip's address is static, and slirp learns its
MAC address from Pip's own ARP traffic.

The machine also has a lid, open, as the MacBook's: a small ACPI table
(an SSDT with a PNP0C0D device whose _LID answers 1) gives Linux the same
"Lid Switch" the MacBook's firmware does. Without one the brainstem reads the
lid as unknown, keeps the being asleep and the microphone shut (by design),
and the voice never hears anything.

    BEING_LAPTOP_CABLE_TRY_IMAGE=out/images/beingos-laptop-cable-try.img \\
    BEING_CABLE_IDENTITY=~/.ssh/pipos_cable_ed25519 \\
        python3 -m unittest lab.tests.test_cable_dev -v

BEING_CABLE_IDENTITY is the login key whose public half `lab/build.sh
cable-key` imported (an ssh-agent may hold its passphrase), and
BEING_CABLE_HOST_KEY the .pub of the host key it imported (by default
~/.config/pipos-cable/ssh_host_ed25519_key.pub), which the connection pins.
BEING_LAPTOP_PATIENCE multiplies every wait (the CPU is emulated).
"""

import json
import os
import shlex
import struct
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from lab.tests.test_laptop_smoke import (
    LAB_DISPLAY, TARGET_BYTES, TARGET_MODEL, boot_folders, gpt_partitions, log_volume, try_menu_lines,
)
from lab.vm import ROOT, VM, machine_args

IMAGE = Path(os.environ.get(
    "BEING_LAPTOP_CABLE_TRY_IMAGE", ROOT / "out/images/beingos-laptop-cable-try.img")).resolve()
IDENTITY = Path(os.environ.get("BEING_CABLE_IDENTITY", Path.home() / ".ssh/pipos_cable_ed25519")).expanduser()
HOST_KEY = Path(os.environ.get(
    "BEING_CABLE_HOST_KEY", Path.home() / ".config/pipos-cable/ssh_host_ed25519_key.pub")).expanduser()
PATIENCE = float(os.environ.get("BEING_LAPTOP_PATIENCE", "1") or 1)

MAC = "169.254.162.20"
PIP4, PIP6 = "169.254.77.1", "fe80::77:1"
# The cable: the link-local network, nothing beyond it (restrict=on), no
# IPv6 router advertisements from slirp.
CABLE_NET = f"user,id=net0,restrict=on,net=169.254.0.0/16,host={MAC},dhcpstart=169.254.200.10,ipv6=off"
HOST = "127.0.0.1"
UNITS = ("being-cable-link.service", "being-cable-ssh.service", "being-cable-workbench.service")
ORGANS = ("being-mind.service", "being-voice.service")
TO_SSH = 1500
POWER_OFF = 240
HEAR_PHRASE = "Hey Pip, open a terminal."
# pip-diag renders the phrase on Pip with Flite. PocketSphinx hears the words
# after the wake in the default voice (slt) as "but then again in l", which
# the command grammar rightly will not replace (none of the command's words
# are in it); in kal16 it hears "kept open a terminal", which the grammar
# tidies to the command, as it would for an owner.
HEAR_VOICE = "kal16"


def patient(seconds):
    return seconds * PATIENCE


def lid_table(path):
    """Write an ACPI SSDT with an open lid: Scope(\\_SB) { Device(LID0) {
    Name(_HID, EisaId("PNP0C0D")) Method(_LID) { Return(One) } } }, in AML."""
    method = b"\x14\x08_LID\x00\xa4\x01"                      # Method(_LID, 0) { Return(One) }
    name = b"\x08_HID\x0c" + bytes((0x41, 0xD0, 0x0C, 0x0D))   # Name(_HID, EisaId("PNP0C0D"))
    body = b"LID0" + name + method
    device = b"\x5b\x82" + bytes((len(body) + 1,)) + body
    scope = b"\x10" + bytes((len(b"\\_SB_" + device) + 1,)) + b"\\_SB_" + device
    header = (b"SSDT" + struct.pack("<I", 36 + len(scope)) + bytes((2, 0)) + b"BEINGL" + b"LIDSWTCH"
              + struct.pack("<I", 1) + b"BLAB" + struct.pack("<I", 1))
    table = bytearray(header + scope)
    table[9] = -sum(table) & 0xFF
    Path(path).write_bytes(table)
    return path


def cable_machine(target, display=LAB_DISPLAY, lid=None):
    """lab.vm.machine_args for the stick over USB, a blank SATA disk, and the cable."""
    def build(*args, **kwargs):
        command = machine_args(*args, **kwargs)
        boot_device = command.index("ide-hd,drive=disk0,bus=ide.0")
        del command[boot_device - 1:boot_device + 1]
        command[command.index("qemu-xhci")] = "qemu-xhci,id=usb-controller"
        command[command.index(LAB_DISPLAY)] = display
        command[command.index("user,id=net0")] = CABLE_NET
        return command + [
            "-device", "usb-storage,drive=disk0,bus=usb-controller.0,bootindex=1",
            "-drive", f"if=none,id=laptop-target,format=raw,file={target}",
            "-device", f"ide-hd,drive=laptop-target,bus=ide.0,bootindex=2,model={TARGET_MODEL}",
        ] + (["-acpitable", f"file={lid}"] if lid else [])
    return build


def host_key_line():
    words = HOST_KEY.read_text().split()
    if len(words) < 2 or words[0] != "ssh-ed25519":
        raise ValueError(f"{HOST_KEY} is not an Ed25519 public key")
    return " ".join(words[:2])


class CableMachine:
    """One boot of a cable stick and the forwarded SSH port that reaches it."""

    def __init__(self, image, files):
        self.files = Path(files)
        self.target = self.files / "target.raw"
        with self.target.open("xb") as disk:
            disk.truncate(TARGET_BYTES)
        self.before = self.target.stat()
        self.lid = lid_table(self.files / "lid.aml")
        self.vm = VM(image=image)
        self.port = None
        self.pins = self.files / "known_hosts"

    def start(self):
        with patch("lab.vm.machine_args", cable_machine(self.target, lid=self.lid)):
            self.vm.start()
        self.port = self.forward(22)
        self.pins.write_text(f"[{HOST}]:{self.port} {host_key_line()}\n")

    def forward(self, guest_port):
        result = ""
        for _ in range(4):
            with socket.socket() as probe:
                probe.bind((HOST, 0))
                port = probe.getsockname()[1]
            result = self.vm.qmp("human-monitor-command",
                                 **{"command-line": f"hostfwd_add net0 tcp:{HOST}:{port}-{PIP4}:{guest_port}"})
            if not result.strip():
                return port
        raise RuntimeError(f"could not forward the cable's port {guest_port}: {result}")

    def ssh_command(self, command=None, *, identity=None, options=()):
        args = ["ssh", "-F", "/dev/null", "-i", str(identity or IDENTITY), "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.pins}",
                "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR",
                *options, "-p", str(self.port), f"root@{HOST}"]
        return args + ([command] if command is not None else [])

    def ssh(self, command, *, timeout=90, **kwargs):
        return subprocess.run(self.ssh_command(command, **kwargs), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=patient(timeout))

    def wait_ssh(self, timeout=TO_SSH):
        deadline = time.monotonic() + patient(timeout)
        result = None
        while time.monotonic() < deadline:
            if not self.vm.running():
                raise RuntimeError("the machine stopped before the cable link came up")
            try:
                result = self.ssh("true", timeout=60)
                if result.returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                pass
            time.sleep(5)
        raise RuntimeError(f"no SSH over the cable within {patient(timeout):.0f}s: "
                           f"{result.stderr if result else 'no answer'}")

    def untouched(self):
        after = self.target.stat()
        return ((after.st_size, after.st_blocks, after.st_mtime_ns)
                == (self.before.st_size, self.before.st_blocks, self.before.st_mtime_ns))


@unittest.skipUnless(IMAGE.exists(), "needs the cable Try stick: BEING_LAPTOP_CABLE=1 lab/build.sh laptop-try")
@unittest.skipUnless(IDENTITY.exists() and HOST_KEY.exists(),
                     "needs BEING_CABLE_IDENTITY and BEING_CABLE_HOST_KEY (docs/LAB.md, Cable development build)")
class CableDevelopment(unittest.TestCase):
    """One default boot of the stick, used over the cable by every test here."""

    @classmethod
    def setUpClass(cls):
        files = tempfile.mkdtemp(prefix="being-cable-", dir="/tmp")
        cls.addClassCleanup(shutil.rmtree, files, ignore_errors=True)
        cls.machine = CableMachine(IMAGE, files)
        cls.addClassCleanup(cls.machine.vm.destroy)
        cls.machine.start()
        cls.machine.wait_ssh()

    def sh(self, command, timeout=90):
        result = self.machine.ssh(command, timeout=timeout)
        self.assertEqual(result.returncode, 0, f"{command}:\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def wait_active(self, units, timeout=300):
        deadline = time.monotonic() + patient(timeout)
        states = ""
        while time.monotonic() < deadline:
            result = self.machine.ssh("systemctl is-active " + " ".join(units), timeout=30)
            states = result.stdout.split()
            if states == ["active"] * len(units):
                return
            time.sleep(3)
        self.fail(f"{units} were {states}, expected active")

    @staticmethod
    def second_sshd(then, *, from_networks="shipped"):
        """A shell script: a second sshd on port 2222 with the shipped file and
        a throwaway key in $d/id, then `then`. The key carries the shipped
        from= networks, or none with from_networks=None."""
        if from_networks == "shipped":
            options = """peers=$(sed -n 's/^from="\\([^"]*\\)".*/\\1/p' /usr/share/being/cable/authorized_keys)
[ -n "$peers" ]
printf 'from="%s",restrict,pty %s\\n' "$peers" "$(cut -d' ' -f1,2 "$d/id.pub")" > "$d/authorized_keys"
"""
        else:
            options = """printf 'restrict,pty %s\\n' "$(cut -d' ' -f1,2 "$d/id.pub")" > "$d/authorized_keys"
"""
        return """set -eu
d=$(mktemp -d /run/being-cable-v6.XXXXXX)
trap 'kill "$(cat "$d/pid" 2>/dev/null)" 2>/dev/null || true; rm -rf "$d"' EXIT
ssh-keygen -q -t ed25519 -N '' -C '' -f "$d/id"
""" + options + """chmod 0644 "$d/authorized_keys"
/usr/sbin/sshd -f /usr/share/being/cable/sshd_config -o Port=2222 -o PidFile="$d/pid" \\
    -o AuthorizedKeysFile="$d/authorized_keys"
i=0
while [ ! -s "$d/pid" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
""" + then

    def main_pid(self, unit):
        return int(self.sh(f"systemctl show -p MainPID --value {unit}").strip() or 0)

    def journal_says(self, match, text, timeout=60):
        """Whether this boot's journal entries matching `match` come to contain `text`."""
        deadline = time.monotonic() + patient(timeout)
        while True:
            found = self.machine.ssh(f"journalctl -b {match} -o cat --no-pager | grep -qF '{text}'", timeout=30)
            if found.returncode == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(2)

    def interface(self):
        status = self.sh("being-network status")
        match = re.search(rf"^Cable development on \(([A-Za-z0-9_.-]+), {re.escape(PIP4)}, {re.escape(PIP6)}\)$",
                          status, re.M)
        self.assertIsNotNone(match, status)
        return match[1]

    # -- getting in ------------------------------------------------------

    def test_root_logs_in_from_the_macs_link_local_address(self):
        self.assertEqual(self.sh("id -u").strip(), "0")
        connection = self.sh('echo "$SSH_CONNECTION"').split()
        self.assertEqual(connection[0], MAC)
        self.assertEqual(connection[2], PIP4)
        cmdline = self.sh("cat /proc/cmdline").split()
        for word in ("being.try=1", "being.cable=1", "being.voice_debug=1"):
            self.assertIn(word, cmdline)
        self.assertNotIn("being.cable=0", cmdline)

    def test_the_wired_port_has_the_static_link_local_addresses(self):
        interface = self.interface()
        four = [line for line in self.sh(f"ip -4 -o addr show dev {interface}").splitlines() if PIP4 in line]
        self.assertEqual(len(four), 1, four)
        self.assertIn(f"inet {PIP4}/16 ", four[0])
        self.assertNotIn("dynamic", four[0])
        self.assertIn(f"inet6 {PIP6}/64", self.sh(f"ip -6 -o addr show dev {interface}"))
        network = self.sh("cat /run/systemd/network/00-being-cable.network")
        self.assertIn(f"Name={interface}\n", network)
        self.assertIn("Unmanaged=yes", network)
        self.assertEqual(json.loads(self.sh("cat /run/being-network/cable.json"))["interface"], interface)

    def test_the_firewall_admits_ssh_on_the_wired_port_from_link_local_only(self):
        interface = self.interface()
        incoming = self.sh("nft list chain inet being_network input")
        outgoing = self.sh("nft list chain inet being_network output")
        self.assertIn("policy drop", incoming)
        ssh_in = [line.strip() for line in incoming.splitlines() if "dport 22" in line]
        self.assertTrue(ssh_in, incoming)
        for line in ssh_in:
            self.assertIn(f'iifname "{interface}"', line)
            self.assertRegex(line, r"saddr (169\.254\.0\.0/16|fe80::/10) ")
        for line in (line.strip() for line in outgoing.splitlines() if "sport 22" in line):
            self.assertIn(f'oifname "{interface}"', line)
            self.assertIn("ct state established", line)

    def test_the_server_refuses_other_keys_passwords_and_other_networks(self):
        keys = Path(tempfile.mkdtemp(prefix="being-cable-key-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, keys, ignore_errors=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keys / "other")], check=True)
        other = self.machine.ssh("true", identity=keys / "other", timeout=60)
        self.assertEqual(other.returncode, 255)
        self.assertIn("Permission denied", other.stderr)
        password = self.machine.ssh("true", timeout=60, options=(
            "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive"))
        self.assertEqual(password.returncode, 255)
        self.assertIn("Permission denied (publickey)", password.stderr)
        # sshd's own reading of the shipped file for a connection from the
        # default QEMU network (10.0.2.2). OpenSSH 10 prints the keywords in
        # their documented case (`AllowUsers`), older releases in lower case;
        # one line per AllowUsers pattern either way.
        test = self.machine.ssh("sshd -T -f /usr/share/being/cable/sshd_config -C user=root,host=lab,addr=10.0.2.2",
                                timeout=60)
        self.assertEqual(test.returncode, 0, test.stderr)
        # Every line of the shipped file is one this OpenSSH knows.
        self.assertNotIn("Unsupported option", test.stderr)
        self.assertNotIn("Deprecated option", test.stderr)
        effective = test.stdout
        settings = [line.split(None, 1) for line in effective.splitlines() if line.strip()]
        values = {}
        for words in settings:
            values.setdefault(words[0].lower(), []).append(words[1] if len(words) > 1 else "")
        self.assertEqual(values.get("allowusers"), ["root@169.254.0.0/16", "root@fe80:*"], effective)
        for keyword, value in (("passwordauthentication", "no"), ("kbdinteractiveauthentication", "no"),
                               ("permitrootlogin", "prohibit-password"), ("authenticationmethods", "publickey"),
                               ("pubkeyacceptedalgorithms", "ssh-ed25519"), ("disableforwarding", "yes"),
                               ("permitemptypasswords", "no"),
                               ("authorizedkeysfile", "/usr/share/being/cable/authorized_keys")):
            self.assertEqual(values.get(keyword), [value], f"{keyword}:\n{effective}")
        # AllowUsers is applied at login, not by -T: a second sshd with the
        # shipped file and a throwaway key without from= (so only AllowUsers
        # decides) refuses root from loopback, an address outside both
        # networks, and admits the same key from the scoped link-local one.
        interface = self.interface()
        output = self.sh(self.second_sshd(f"""
try() {{ ssh -F /dev/null -i "$d/id" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no \\
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20 -p 2222 "root@$1" 'echo admitted' 2>&1 \\
    || true; }}
echo "loopback: $(try 127.0.0.1)"
echo "link-local: $(try '{PIP6}%{interface}')"
""", from_networks=None), timeout=120)
        self.assertRegex(output, r"(?m)^loopback: .*Permission denied", output)
        self.assertRegex(output, r"(?m)^link-local: admitted$", output)
        keys_line = self.sh("cat /usr/share/being/cable/authorized_keys")
        self.assertTrue(keys_line.startswith('from="169.254.0.0/16,fe80:*",restrict,pty ssh-ed25519 '), keys_line)
        # The target's busybox has no stat(1).
        modes = self.sh("python3 -c 'import os, sys; [print(f\"{os.stat(p).st_mode & 0o777:o} "
                        "{os.stat(p).st_uid} {p}\") for p in sys.argv[1:]]' "
                        "/usr/share/being/cable/ssh_host_ed25519_key /usr/share/being/cable/authorized_keys")
        self.assertIn("600 0 /usr/share/being/cable/ssh_host_ed25519_key", modes)
        self.assertIn("644 0 /usr/share/being/cable/authorized_keys", modes)

    def test_the_shipped_rules_admit_a_scoped_link_local_peer(self):
        # slirp cannot bring a link-local IPv6 peer onto the cable, so the
        # guest logs in to itself at fe80::77:1%<port>: the target's own sshd
        # and libc then see a scoped peer, as with the Mac's `ssh pip`. A
        # second sshd runs the shipped file on port 2222 with a throwaway key
        # that carries the shipped from= networks.
        interface = self.interface()
        script = self.second_sshd(f"""
ssh -F /dev/null -i "$d/id" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no \\
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20 -p 2222 \\
    'root@{PIP6}%{interface}' 'echo "connection=$SSH_CONNECTION"'
""")
        output = self.sh(script, timeout=120)
        connection = re.search(r"^connection=(\S+) ", output, re.M)
        self.assertIsNotNone(connection, output)
        self.assertEqual(connection[1], f"{PIP6}%{interface}")

    # -- the boot itself -------------------------------------------------

    def test_the_organs_and_the_cable_units_are_active(self):
        self.wait_active((*ORGANS, *UNITS))

    def test_the_code_is_a_writable_workbench_copy(self):
        mounts = self.sh("cat /proc/self/mountinfo").splitlines()
        for where, source in (("/usr/lib/being", "/being-dev/lib"), ("/etc/being", "/being-dev/etc")):
            lines = [line for line in mounts if line.split()[4] == where]
            self.assertEqual(len(lines), 1, f"{where}: {lines}")
            self.assertTrue(lines[0].split()[3].endswith(source), lines[0])
        self.sh("test -w /usr/lib/being/mind/turn.py && test -w /etc/being/env")
        # The image's unchecked-hash .pyc files were replaced by timestamp ones.
        flags = self.sh("python3 -c \"import glob, struct; "
                        "print(sorted({struct.unpack('<I', open(p, 'rb').read(8)[4:8])[0] "
                        "for p in glob.glob('/usr/lib/being/mind/__pycache__/*.pyc')}))\"")
        self.assertEqual(flags.strip(), "[0]")

    def test_an_edited_module_takes_effect_after_a_restart(self):
        module = "/usr/lib/being/mind/sd_notify.py"
        self.sh(f"cp -p {module} /run/being-dev/sd_notify.py.orig")
        self.addCleanup(self.machine.ssh, f"cp -p /run/being-dev/sd_notify.py.orig {module} && "
                                          "systemctl restart being-mind.service", timeout=120)
        self.sh(f"printf '\\nimport sys as _workbench_sys\\nprint(\"cable workbench probe\", "
                f"file=_workbench_sys.stderr, flush=True)\\n' >> {module}")
        loaded = self.sh("cd / && PYTHONPATH=/usr/lib/being python3 -c 'import mind.sd_notify' 2>&1")
        self.assertIn("cable workbench probe", loaded)
        before = self.main_pid("being-mind.service")
        self.sh("systemctl restart being-mind.service", timeout=180)
        self.wait_active(("being-mind.service",))
        after = self.main_pid("being-mind.service")
        self.assertNotIn(after, (0, before), "systemctl restart did not start a new mind")
        # The mind is Type=simple: `restart` returns once it is forked, and on
        # the emulated CPU its imports take seconds more. Its own stderr line
        # (by its PID) shows the edited module ran in the restarted process.
        self.assertTrue(self.journal_says(f"_PID={after}", "cable workbench probe", timeout=120),
                        f"the restarted mind (PID {after}) did not load the edit")

    def test_a_reconcile_keeps_the_link_and_the_session(self):
        interface = self.interface()
        output = self.sh(f"being-network reconcile && ip -4 -o addr show dev {interface} && "
                         f"nft list chain inet being_network input && echo session-survived", timeout=180)
        self.assertIn(f"inet {PIP4}/16", output)
        self.assertIn('tcp dport 22 accept', output)
        self.assertTrue(output.rstrip().endswith("session-survived"), output[-500:])
        self.assertEqual(self.sh("id -u").strip(), "0")

    def test_pip_diag_quick_reports_and_never_fails_to_run(self):
        self.sh("command -v pip-diag")
        result = self.machine.ssh(f"pip-diag quick --json --now {int(time.time())}", timeout=900)
        self.assertIn(result.returncode, (0, 1), f"pip-diag could not run:\n{result.stdout}\n{result.stderr}")
        checks = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(checks, result.stdout[-2000:])
        for check in checks:
            if "id" in check:
                self.assertIn(check.get("status"), ("pass", "warn", "fail", "skip"), check)

    def leave_the_try_menu(self, timeout=600):
        """Choose "Try PipOS" on the Try menu, as the owner would, so the face
        has the screen: while the menu has it, hands refuses the terminal (by
        design). The menu also gives the screen back by itself after a few idle
        minutes."""
        deadline = time.monotonic() + patient(timeout)
        pressed = None
        while time.monotonic() < deadline:
            states = self.machine.ssh("systemctl is-active being-face.service being-local-screen.service",
                                      timeout=30).stdout.split()
            if states[:1] == ["active"] and states[1:] != ["active"]:
                return
            if (states[1:] == ["active"] and (pressed is None or time.monotonic() - pressed > patient(60))
                    and try_menu_lines(self.machine.vm.screen())):
                for key in ("1", "ret"):
                    self.machine.vm.qmp("send-key", keys=[{"type": "qcode", "data": key}])
                    time.sleep(0.15)
                pressed = time.monotonic()
            time.sleep(2)
        self.fail("the face never took the screen from the Try menu")

    def test_pip_diag_hears_the_terminal_phrase(self):
        # The whole path on Pip: the phrase rendered by Flite, fed to the
        # running voice through the diagnostic microphone (BEING_DIAG_MIC),
        # wake and dictation by PocketSphinx with the grammar's second pass,
        # the voice's `heard:` line, the mind's `route` line, and the
        # terminal on the panel.
        self.wait_active(ORGANS)
        self.leave_the_try_menu()
        patience = min(20, max(3, round(3 * PATIENCE)))
        result = self.machine.ssh(f"pip-diag hear --phrase {shlex.quote(HEAR_PHRASE)} --voice {HEAR_VOICE} "
                                  f"--patience {patience} --json", timeout=900)
        self.assertIn(result.returncode, (0, 1), f"pip-diag could not run:\n{result.stdout}\n{result.stderr}")
        checks = {check["id"]: check for check in (json.loads(line) for line in result.stdout.splitlines()
                                                    if line.strip().startswith("{")) if "id" in check}
        for name in ("hear.connect", "hear.heard.phrase", "hear.route.phrase", "hear.terminal.phrase"):
            self.assertEqual(checks.get(name, {}).get("status"), "pass", f"{name}:\n{result.stdout[-3000:]}")
        self.assertEqual(checks["hear.heard.phrase"]["value"]["text"], "open a terminal")
        self.assertEqual(checks["hear.route.phrase"]["value"]["route"], "workshop.open")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(self.sh("ls -n /run/being-panel/terminal.frame | awk '{print $3}'").strip(),
                         self.sh("id -u being-panel").strip(), "the renderer does not own its frame")

    def test_z_power_off_leaves_the_internal_disk_untouched(self):
        # Last (tests run in name order): the boot ends here.
        try:
            self.machine.ssh("systemctl poweroff", timeout=60)
        except subprocess.TimeoutExpired:
            pass  # the session ends with the machine
        self.assertTrue(self.machine.vm.wait_off(patient(POWER_OFF)), "the machine did not power off")
        self.machine.vm.stop()
        self.assertTrue(self.machine.untouched(), "the cable boot changed the internal disk")


@unittest.skipUnless(IMAGE.exists(), "needs the cable Try stick: BEING_LAPTOP_CABLE=1 lab/build.sh laptop-try")
@unittest.skipUnless(HOST_KEY.exists(), "needs BEING_CABLE_HOST_KEY (docs/LAB.md, Cable development build)")
class CableKillSwitch(unittest.TestCase):
    """being.cable=0 on the command line: nothing answers on the cable."""

    def stick_with_the_kill_switch(self, files):
        """A copy of the stick whose GRUB entries say being.cable=0 (same length, in place)."""
        copy = files / "cable-off.img"
        if subprocess.run(["cp", "-c", str(IMAGE), str(copy)], capture_output=True).returncode != 0:
            shutil.copyfile(IMAGE, copy)
        stamp = IMAGE.with_name(IMAGE.name + ".source-id")
        if stamp.exists():
            shutil.copyfile(stamp, copy.with_name(copy.name + ".source-id"))
        with copy.open("r+b") as disk:
            esp = [part for part in gpt_partitions(disk) if part[1] == "try-esp"]
            self.assertEqual(len(esp), 1, gpt_partitions(disk))
            _, _, offset, size = esp[0]
            disk.seek(offset)
            data = disk.read(size)
            switched = data.replace(b"being.cable=1", b"being.cable=0")
            self.assertGreaterEqual(data.count(b"being.cable=1"), 6, "the stick's GRUB entries lack being.cable=1")
            disk.seek(offset)
            disk.write(switched)
        return copy

    def test_the_kill_switch_keeps_the_cable_closed(self):
        files = Path(tempfile.mkdtemp(prefix="being-cable-off-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, files, ignore_errors=True)
        machine = CableMachine(self.stick_with_the_kill_switch(files), files)
        self.addCleanup(machine.vm.destroy)
        machine.start()
        # The Try menu on the panel: the boot is up.
        deadline = time.monotonic() + patient(TO_SSH)
        while not try_menu_lines(machine.vm.screen()):
            self.assertTrue(machine.vm.running(), "the machine stopped before the Try menu")
            self.assertLess(time.monotonic(), deadline, "the Try menu never came up")
            time.sleep(1)
        deadline = time.monotonic() + patient(90)
        while time.monotonic() < deadline:
            attempt = machine.ssh("true", timeout=40, identity=IDENTITY if IDENTITY.exists() else None)
            self.assertNotEqual(attempt.returncode, 0, "the cable answered with being.cable=0")
            time.sleep(5)
        machine.vm.power_button()
        self.assertTrue(machine.vm.wait_off(patient(POWER_OFF)), "the power button did not power the machine off")
        machine.vm.stop()
        raw = files / "usb.raw"
        subprocess.run(["qemu-img", "convert", "-O", "raw", str(machine.vm.disk), str(raw)], check=True)
        with raw.open("rb") as disk:
            volume = log_volume(disk)
            self.assertIsNotNone(volume, "no PIPLOGS partition on the stick")
            folders = boot_folders(volume)
            self.assertTrue(folders, "no boot folder on PIPLOGS")
            folder = sorted(folders)[-1]
            journal = b"".join(volume.read(path) for path, entry in folders[folder]
                               if not entry.is_dir and path.lower().endswith("journal.txt")).decode("utf-8", "replace")
        self.assertTrue(journal, f"{folder} holds no journal")
        self.assertNotIn("Cable development on", journal)
        self.assertNotRegex(journal, r"Start(ed|ing) PipOS cable development")
        self.assertTrue(machine.untouched(), "the boot changed the internal disk")


if __name__ == "__main__":
    unittest.main()
