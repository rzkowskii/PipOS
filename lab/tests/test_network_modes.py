"""Validate the installed network policy with traffic confined to one QEMU guest."""

import json
import os
from pathlib import Path
import re
import shlex
import socket
import time
import unittest
from unittest.mock import patch

from lab import vm


IMAGE = Path(os.environ.get("BEING_NETWORK_IMAGE", vm.ROOT / "out/images/beingos-network.img")).resolve()
GUEST = "10.0.2.15"
CONTROLLER = "10.0.2.2"
OTHER_CONTROLLER = "10.0.2.44"
HOST = "127.0.0.1"
OBSERVE_PORT = 2222
SYNTHETIC_IPV4 = "192.0.2.10/24"
SYNTHETIC_IPV6 = "2001:db8::10/64"
LOCKDOWN = "being-network-lockdown.service"
POLICY = "being-network-policy.service"
TIMESYNC = "systemd-timesyncd.service"
NETWORKD = "systemd-networkd.service"
OBSERVE = "being-lab-observe.service"

# All destinations are on directly connected documentation subnets. SLIRP is
# restricted as well, so the fixture cannot send these to the host's network.
PROBES = {
    "ipv4": ("udp", "192.0.2.80", 18080),
    "ipv6": ("udp", "2001:db8::80", 18080),
    "dns4": ("udp", "192.0.2.53", 53),
    "dns6": ("udp", "2001:db8::53", 53),
    "tcp_dns4": ("tcp", "192.0.2.53", 53),
    "tcp_dns6": ("tcp", "2001:db8::53", 53),
    "time4": ("udp", "192.0.2.20", 123),
    "other_time4": ("udp", "192.0.2.21", 123),
    "time6": ("udp", "2001:db8::20", 123),
    "other_time6": ("udp", "2001:db8::21", 123),
    "tcp_time4": ("tcp", "192.0.2.20", 123),
    "tcp_time6": ("tcp", "2001:db8::20", 123),
}
PROBE_SOURCE = {name: 44000 + index for index, name in enumerate(PROBES)}
GENERAL_TRAFFIC = {"ipv4", "ipv6", "dns4", "dns6", "tcp_dns4", "tcp_dns6"}


@unittest.skipUnless(IMAGE.is_file(), "requires beingos-network.img or BEING_NETWORK_IMAGE")
class NetworkModes(unittest.TestCase):
    def sh(self, command, timeout=40):
        code, output = self.guest.run(command, timeout)
        self.assertEqual(code, 0, f"{command}:\n{output}")
        return output

    def status(self, mode, *, time_server=None, paired=False):
        output = self.sh("being-network status --json")
        status = json.loads(output)
        self.assertEqual(status["requested_mode"], mode, output)
        self.assertEqual(status["mode"], mode, output)
        self.assertEqual(status["effective_time_server"], time_server, output)
        self.assertIs(status["paired"], paired, output)

    def service_time(self, unit):
        return int(self.sh(f"systemctl show -p ActiveEnterTimestampMonotonic --value {unit}"))

    def assert_inactive(self, unit):
        self.sh(f"! systemctl is-active --quiet {unit}")

    def assert_link_down(self):
        flags = int(self.sh(f"cat /sys/class/net/{self.interface}/flags"), 16)
        self.assertEqual(flags & 1, 0, "the virtual Ethernet interface is administratively up")
        self.assertEqual(self.sh(f"ip -4 addr show dev {self.interface} | grep 'inet ' || true"), "")
        self.assertEqual(self.sh(f"ip -6 addr show dev {self.interface} | grep 'inet6 ' || true"), "")

    def assert_filter(self):
        table = self.sh("nft list table inet being_network")
        for hook in ("input", "output", "forward"):
            self.assertRegex(table, rf"type filter hook {hook} [^\n]*policy drop;", table)

    def assert_loopback(self):
        for family, address in (("AF_INET", "127.0.0.1"), ("AF_INET6", "::1")):
            code = (f"import socket; s=socket.socket(socket.{family}, socket.SOCK_DGRAM); "
                    f"s.bind(({address!r}, 0)); s.settimeout(2); s.sendto(b'loopback', s.getsockname()); "
                    "assert s.recv(16) == b'loopback'")
            self.sh(shlex.join(("python3", "-c", code)))

    def forward_observer(self):
        for _ in range(4):
            with socket.socket() as probe:
                probe.bind((HOST, 0))
                port = probe.getsockname()[1]
            result = self.guest.qmp("human-monitor-command", **{
                "command-line": f"hostfwd_add net0 tcp:{HOST}:{port}-{GUEST}:{OBSERVE_PORT}",
            })
            if not result.strip():
                return port
        self.fail(f"Could not forward the observer port: {result}")

    def enroll(self, controller):
        self.sh(f"being-lab-access enroll {GUEST} --controller-ip {controller} "
                f"--ethernet {self.interface} --observe-key /run/network-probe.pub")

    def listening_locally(self, timeout=30):
        code = (f"import socket; s=socket.create_connection(('{GUEST}', {OBSERVE_PORT}), 1); "
                "print(s.makefile('rb').readline(100).decode('ascii'))")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, output = self.guest.run(shlex.join(("python3", "-c", code)), 8)
            if status == 0 and output.startswith("SSH-2.0-"):
                return
            time.sleep(0.2)
        _, log = self.guest.run(f"journalctl -b -u {OBSERVE} -n 20 --no-pager -o cat", 10)
        self.fail(f"Observer never became reachable inside the guest:\n{output}\n{log}")

    def host_banner(self, port):
        try:
            with socket.create_connection((HOST, port), timeout=2) as peer, peer.makefile("rb") as stream:
                return stream.readline(100)
        except OSError:
            return b""

    def install_probes(self):
        rules = ["table inet being_network_test {"]
        rules += [f"counter {name} {{ }}" for name in PROBES]
        rules += ["chain observed_egress {", "type filter hook output priority 300; policy accept;"]
        rules += [f"{kind} sport {PROBE_SOURCE[name]} counter name {name}" for name, (kind, _, _) in PROBES.items()]
        rules += ["}", "}"]
        self.guest.put("/run/network-probe.nft", "\n".join(rules) + "\n")
        self.sh("nft -f /run/network-probe.nft")
        self.guest.put("/run/network-probe.py", f"""\
import socket

for name, (kind, destination, port) in {PROBES!r}.items():
    family, source = (socket.AF_INET6, '::') if ':' in destination else (socket.AF_INET, '0.0.0.0')
    with socket.socket(family, socket.SOCK_STREAM if kind == 'tcp' else socket.SOCK_DGRAM) as peer:
        peer.bind((source, {PROBE_SOURCE!r}[name]))
        peer.setblocking(False)
        try:
            if kind == 'tcp':
                peer.connect_ex((destination, port))
            else:
                peer.sendto(b'synthetic network policy fixture', (destination, port))
        except OSError:
            pass
""")

    def tagged_packets(self):
        table = self.sh("nft list table inet being_network_test")
        counters = {name: int(packets) for name, packets in
                    re.findall(r"counter (\w+)\s*\{\s*packets (\d+) bytes", table)}
        self.assertEqual(set(counters), set(PROBES), table)
        return counters

    def assert_egress(self, allowed):
        # Add fixture routes after policy activation. This checks the firewall
        # independently of the normal link, DHCP and IPv6 settings.
        interface = self.interface
        ipv6 = f"/proc/sys/net/ipv6/conf/{interface}"
        flags, disabled, dad = self.sh(f"cat /sys/class/net/{interface}/flags "
                                       f"{ipv6}/disable_ipv6 {ipv6}/dad_transmits").split()
        link = "up" if int(flags, 16) & 1 else "down"
        try:
            # BusyBox ip lacks the per-address nodad option. Restore the two
            # settings after probing so this cannot change the tested mode.
            self.sh(f"echo 0 > {ipv6}/dad_transmits && echo 0 > {ipv6}/disable_ipv6 && "
                    f"ip link set dev {interface} up && ip -4 addr add {SYNTHETIC_IPV4} dev {interface} && "
                    f"ip -6 addr add {SYNTHETIC_IPV6} dev {interface}")
            before = self.tagged_packets()
            self.sh("python3 /run/network-probe.py")
            after = self.tagged_packets()
        finally:
            self.sh(f"ip -6 addr del {SYNTHETIC_IPV6} dev {interface} 2>/dev/null; "
                    f"ip -4 addr del {SYNTHETIC_IPV4} dev {interface} 2>/dev/null; "
                    f"echo {int(disabled)} > {ipv6}/disable_ipv6 && echo {int(dad)} > {ipv6}/dad_transmits && "
                    f"ip link set dev {interface} {link} && "
                    f"! ip -4 addr show dev {interface} | grep -Fq '{SYNTHETIC_IPV4}' && "
                    f"! ip -6 addr show dev {interface} | grep -Fq '{SYNTHETIC_IPV6}'")
        self.assertEqual({name for name, count in after.items() if count > before[name]}, set(allowed), (before, after))

    def test_effective_mode_controls_links_peers_and_network_time(self):
        self.guest = vm.VM(image=IMAGE)
        self.addCleanup(self.guest.destroy)
        original = vm.machine_args

        def isolate(*args, **kwargs):
            command = original(*args, **kwargs)
            command[command.index("user,id=net0")] = "user,id=net0,restrict=on"
            return command

        with patch.object(vm, "machine_args", isolate):
            self.guest.start()
        self.guest.login(140)
        self.interface = self.sh("udevadm settle --timeout=10 && for p in /sys/class/net/*; do "
                                 "if [ -e \"$p/device\" ]; then echo \"${p##*/}\"; fi; done")
        self.assertRegex(self.interface, r"\A[A-Za-z0-9_.-]{1,15}\Z")
        self.status("local-only")
        self.assert_filter()
        self.assert_link_down()
        self.assert_loopback()
        self.assert_inactive(NETWORKD)
        self.assert_inactive(TIMESYNC)
        self.assertGreater(self.service_time(LOCKDOWN), 0)
        self.assertGreaterEqual(self.service_time(POLICY), self.service_time(LOCKDOWN))

        self.sh("ssh-keygen -q -t ed25519 -N '' -f /run/network-probe")
        self.enroll(CONTROLLER)
        self.assert_link_down()
        self.assert_inactive(OBSERVE)
        port = self.forward_observer()
        self.sh("being-network set paired-lan")
        self.status("paired-lan", paired=True)
        self.listening_locally()
        self.assertGreaterEqual(self.service_time(NETWORKD), self.service_time(POLICY))
        self.assertIn(f"inet {GUEST}/24", self.sh(f"ip -4 addr show dev {self.interface}"))
        self.assertEqual(self.sh(f"ip -6 addr show dev {self.interface} | grep 'inet6 ' || true"), "")
        self.assertTrue(self.host_banner(port).startswith(b"SSH-2.0-"), "the paired controller did not reach SSH")
        self.assert_inactive(TIMESYNC)
        self.assert_filter()

        self.enroll(OTHER_CONTROLLER)
        self.sh("being-network set paired-lan")
        self.listening_locally()
        self.assertEqual(self.host_banner(port), b"", "the non-paired source received an SSH banner")
        self.enroll(CONTROLLER)
        self.sh("being-network set paired-lan")
        self.listening_locally()
        self.assertTrue(self.host_banner(port).startswith(b"SSH-2.0-"), "re-pairing did not restore access")
        self.install_probes()
        self.assert_egress(set())

        self.sh("being-network set internet --web-lookups")
        self.status("internet", paired=True)
        self.assert_inactive(TIMESYNC)
        self.assert_filter()
        self.assert_egress(GENERAL_TRAFFIC)

        for name in ("time4", "time6"):
            server = PROBES[name][1]
            self.sh(f"being-network set internet --time-server {server}")
            self.status("internet", time_server=server, paired=True)
            self.sh(f"systemctl is-active --quiet {TIMESYNC}")
            self.assert_egress(GENERAL_TRAFFIC | {name})

        self.sh("being-network set paired-lan")
        self.status("paired-lan", paired=True)
        self.assert_inactive(TIMESYNC)
        self.assert_egress(set())
        self.sh("being-network set local-only")
        self.status("local-only")
        self.assert_filter()
        self.assert_link_down()
        self.assert_loopback()
        self.assert_inactive(TIMESYNC)
        self.assert_inactive(OBSERVE)
        self.assertEqual(self.host_banner(port), b"")
        self.assert_egress(set())
        self.sh("being-network set local-only")
        self.assert_link_down()
        self.guest.send("poweroff\n")
        self.assertTrue(self.guest.wait_off(100), "guest did not shut down cleanly")
        self.guest.stop()
