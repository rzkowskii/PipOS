import contextlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mind import network_enforce as network
from mind.network_policy import INTERNET, LOCAL_ONLY, PAIRED_LAN, NetworkPolicy, save_policy
from mind.skills import Skills


PAIR = {"address": "192.168.0.20", "controller": "192.168.0.10", "ethernet": "eth0", "dev_enabled": True}
CABLE = {"interface": "enp1s0f0"}
# The local-only table exactly as the release images load it.
LOCAL_ONLY_TABLE = (
    "destroy table inet being_network\n"
    "table inet being_network {\n"
    "  chain input {\n"
    "    type filter hook input priority filter; policy drop;\n"
    '    iifname "lo" accept\n'
    "  }\n"
    "  chain output {\n"
    "    type filter hook output priority filter; policy drop;\n"
    '    oifname "lo" accept\n'
    "  }\n"
    "  chain forward { type filter hook forward priority filter; policy drop; }\n"
    "}\n"
)


class NetworkRulesTests(unittest.TestCase):
    def test_offline_is_loopback_only_regardless_of_pair_and_old_connections(self):
        rules = network.render_rules(LOCAL_ONLY, PAIR)
        self.assertTrue(rules.startswith("destroy table inet being_network\ntable inet being_network {"))
        self.assertEqual(rules.count("policy drop;"), 3)
        self.assertEqual(rules.count(" accept"), 2)
        self.assertIn('iifname "lo" accept', rules)
        self.assertIn('oifname "lo" accept', rules)
        self.assertNotIn("ct state", rules)
        self.assertNotIn(PAIR["controller"], rules)

    def test_paired_mode_exceptions_cover_only_ipv4_dhcp_and_selected_ssh(self):
        rules = network.render_rules(PAIRED_LAN, PAIR)
        self.assertIn("255.255.255.255 } udp sport 68 udp dport 67 accept", rules)
        self.assertIn("tcp dport { 2222, 2223 } ct state new,established accept", rules)
        self.assertIn("ip saddr 192.168.0.20 ip daddr 192.168.0.10 tcp sport", rules)
        self.assertNotIn("ct state established,related accept", rules)
        self.assertNotIn("ip6", rules)
        self.assertNotIn(" dport 53", rules)
        self.assertNotIn(" dport 123", rules)
        self.assertNotIn("2223", network.render_rules(PAIRED_LAN, dict(PAIR, dev_enabled=False)))
        self.assertNotIn("2222", network.render_rules(PAIRED_LAN))

    def test_internet_reserves_network_time_to_one_exact_approved_ip(self):
        default = network.render_rules(INTERNET, PAIR)
        self.assertIn("meta nfproto ipv6 udp sport 547 udp dport 546 accept", default)
        self.assertIn("nd-neighbor-advert", default)
        self.assertIn("ct state established,related accept", default)
        self.assertIn("ip saddr 192.168.0.10 ip daddr 192.168.0.20 tcp dport", default)
        self.assertNotIn("dport 123 accept", default)
        for address, expected in (("192.0.2.60", "ip daddr 192.0.2.60"),
                                  ("2001:db8::60", "ip6 daddr 2001:db8::60")):
            with self.subTest(address=address):
                rules = network.render_rules(INTERNET, time_server=address)
                self.assertIn(f"{expected} udp dport 123 accept", rules)
                self.assertLess(rules.index("dport 123 accept"), rules.index("udp dport 123 drop"))
                self.assertIn("udp dport 123 drop\n    tcp dport 123 drop\n    accept", rules)

    def test_apply_installs_only_its_own_table_in_one_nft_transaction(self):
        with patch.object(network.subprocess, "run") as invoke:
            network.install_rules(PAIRED_LAN, PAIR)
        self.assertEqual(invoke.call_args.args[0], ["/usr/sbin/nft", "--file", "-"])
        self.assertEqual(invoke.call_args.kwargs["input"], network.render_rules(PAIRED_LAN, PAIR))
        self.assertNotIn("flush ruleset", invoke.call_args.kwargs["input"])


class NetworkTransitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run = self.directory / "run"
        self.time_conf = self.directory / "timesyncd.conf.d/99-being-network.conf"
        self.enterContext(patch.object(network, "RUN", self.run))
        self.enterContext(patch.object(network, "TIME_CONF", self.time_conf))
        self.ethernet_file = self.directory / "02-being-ethernet.network"
        self.enterContext(patch.object(network, "ETHERNET_FILE", self.ethernet_file))
        # The lab image ships every unit; tests that model the laptop remove some.
        self.units = self.directory / "units"
        self.units.mkdir()
        for unit in network.NETWORK_UNITS:
            (self.units / unit).write_text("[Unit]\n")
        self.enterContext(patch.object(network, "UNIT_DIRS", (self.units,)))

    def test_legacy_skill_authorization_boots_offline_with_no_time_fallback(self):
        save_policy(self.directory, NetworkPolicy(INTERNET, True))
        self.assertTrue(network.load_policy(self.directory).permits_web_lookup)
        with (patch.object(network, "install_rules") as nft,
              patch("mind.lab_access.load_pair") as pair,
              patch("mind.lab_access.configure_ethernet") as ethernet):
            network.apply(self.directory)
        pair.assert_not_called()
        ethernet.assert_called_once_with(None, LOCAL_ONLY)
        nft.assert_called_once_with(LOCAL_ONLY, None, None, None)
        self.assertEqual(network.runtime_state(), network.OFFLINE)
        self.assertFalse((self.run / "mode").exists())
        self.assertEqual(self.time_conf.read_text(), "[Time]\nNTP=\nFallbackNTP=\n")

    def test_socket_activation_follows_the_applied_mode(self):
        for mode in (PAIRED_LAN, INTERNET, LOCAL_ONLY):
            with self.subTest(mode=mode):
                save_policy(self.directory, NetworkPolicy(mode, system_network=mode != LOCAL_ONLY))
                with (patch.object(network, "install_rules"),
                      patch("mind.lab_access.load_pair", return_value=PAIR),
                      patch("mind.lab_access.configure_ethernet")):
                    network.apply(self.directory)
                marker = self.run / "mode"
                self.assertEqual(marker.exists(), mode != LOCAL_ONLY)
                if marker.exists():
                    self.assertEqual(marker.read_text().strip(), mode)

    def test_reconcile_blocks_old_mode_before_stopping_and_starting_services(self):
        save_policy(self.directory, NetworkPolicy(INTERNET, True, True, "2001:db8::60"))
        calls = []

        def nft(mode, *_):
            calls.append(("firewall", mode))

        def manage(action, *units, **_):
            calls.append((action, network.runtime_state()["mode"], units))

        with (patch.object(network, "install_rules", side_effect=nft),
              patch.object(network, "lower_external_links") as lower,
              patch.object(network, "systemctl", side_effect=manage),
              patch("mind.lab_access.load_pair", return_value=PAIR),
              patch("mind.lab_access.configure_ethernet") as ethernet):
            network.reconcile(self.directory)
        lower.assert_called_once()
        self.assertEqual(calls, [("firewall", LOCAL_ONLY), ("stop", LOCAL_ONLY, network.NETWORK_UNITS),
                                ("firewall", INTERNET), ("start", INTERNET, (
                                    network.NETWORKD, network.WIFI, network.OBSERVE,
                                    network.RESOLVED, network.TIMESYNC,
                                ))])
        ethernet.assert_called_once_with(PAIR, INTERNET)
        self.assertIn("NTP=2001:db8::60\nFallbackNTP=\n", self.time_conf.read_text())
        self.assertTrue(network.allows("require-time", network.runtime_state()))
        self.assertTrue(network.allows("require-pair", network.runtime_state()))

    def test_laptop_reconcile_names_only_units_that_exist(self):
        # The laptop image has no lab maintenance units: stop or start of a
        # missing unit fails, so they are never named.
        for unit in (network.DEV, network.OBSERVE):
            (self.units / unit).unlink()
        save_policy(self.directory, NetworkPolicy(INTERNET, True, True))
        calls = []
        with (patch.object(network, "install_rules"), patch.object(network, "lower_external_links"),
              patch.object(network, "systemctl", side_effect=lambda action, *units, **_: calls.append((action, units))),
              patch("mind.lab_access.load_pair", return_value=PAIR),
              patch("mind.lab_access.configure_ethernet")):
            network.reconcile(self.directory)
        for _, units in calls:
            self.assertNotIn(network.DEV, units)
            self.assertNotIn(network.OBSERVE, units)
        self.assertEqual(calls[-1], ("start", (network.NETWORKD, network.WIFI, network.RESOLVED)))

    def test_systemctl_with_no_units_runs_nothing(self):
        with patch.object(network.subprocess, "run") as invoke:
            network.systemctl("stop")
        invoke.assert_not_called()

    def test_owner_ethernet_comes_up_only_in_internet_mode(self):
        from mind import wifi

        wifi.save_ethernet(self.directory, True)
        for mode, expected in ((LOCAL_ONLY, False), (PAIRED_LAN, False), (INTERNET, True)):
            with self.subTest(mode=mode):
                save_policy(self.directory, NetworkPolicy(mode, system_network=mode != LOCAL_ONLY))
                with (patch.object(network, "install_rules"), patch("mind.lab_access.load_pair", return_value=None),
                      patch("mind.lab_access.configure_ethernet")):
                    network.apply(self.directory)
                self.assertEqual(self.ethernet_file.exists(), expected)
        text = self.ethernet_file.read_text()
        self.assertIn("Type=ether", text)
        self.assertIn("SendHostname=no", text)
        self.assertIn("IPv6PrivacyExtensions=yes", text)
        wifi.save_ethernet(self.directory, False)
        with (patch.object(network, "install_rules"), patch("mind.lab_access.load_pair", return_value=None),
              patch("mind.lab_access.configure_ethernet")):
            network.apply(self.directory)
        self.assertFalse(self.ethernet_file.exists())

    def test_unreadable_ethernet_switch_never_opens_a_link(self):
        (self.directory / "network").mkdir(mode=0o700)
        damaged = self.directory / "network/ethernet.json"
        damaged.write_text("{not json")
        damaged.chmod(0o600)
        self.assertFalse(network.configure_owner_ethernet(self.directory, INTERNET))
        self.assertFalse(self.ethernet_file.exists())

    def test_online_rule_failure_leaves_mode_offline_and_services_stopped(self):
        save_policy(self.directory, NetworkPolicy(PAIRED_LAN, False, True))
        failure = subprocess.CalledProcessError(1, "nft")
        with (patch.object(network, "install_rules", side_effect=(None, failure, None)),
              patch.object(network, "lower_external_links"),
              patch.object(network, "systemctl") as service,
              patch("mind.lab_access.load_pair", return_value=PAIR),
              patch("mind.lab_access.configure_ethernet")):
            with self.assertRaises(subprocess.CalledProcessError):
                network.reconcile(self.directory)
        self.assertEqual(network.runtime_state(), network.OFFLINE)
        for call in service.call_args_list:
            self.assertEqual(call.args[0], "stop")
            self.assertIn("systemd-networkd.socket", call.args)
            self.assertIn("systemd-networkd-varlink.socket", call.args)
        self.assertEqual(service.call_count, 2)
        self.assertFalse(network.allows("require-link", network.runtime_state()))

    def test_state_gate_closes_if_runtime_publication_is_incomplete(self):
        network.write_state({"mode": INTERNET, "effective_time_server": "192.0.2.60", "paired": True})
        self.assertTrue(network.allows("require-internet", network.runtime_state()))
        (self.run / "mode").unlink()
        self.assertEqual(network.runtime_state(), network.OFFLINE)

    def test_lockdown_disables_links_even_if_the_firewall_cannot_load(self):
        network.write_state({"mode": INTERNET, "effective_time_server": None, "paired": True})
        links = self.directory / "sys_net"
        for name, flags in (("eth0", "0x1003"), ("lo", "0x9"), ("wlan0", "0x1003"), ("eth1", "0x1002")):
            device = links / name
            device.mkdir(parents=True)
            (device / "flags").write_text(flags)
        failure = subprocess.CalledProcessError(1, "nft")
        with (patch.object(network, "SYS_NET", links),
              patch.object(network, "install_rules", side_effect=failure),
              patch.object(network.subprocess, "run") as links_command):
            with self.assertRaises(subprocess.CalledProcessError):
                network.lockdown()
        self.assertEqual(network.runtime_state(), network.OFFLINE)
        self.assertFalse((self.run / "mode").exists())
        self.assertEqual([call.args[0][-2] for call in links_command.call_args_list], ["eth0", "wlan0"])

    def test_appliance_skill_requires_requested_web_and_applied_internet(self):
        # The appliance path is checked without accessing the local machine's /mind.
        with (patch("mind.skills.load_policy", return_value=NetworkPolicy(INTERNET, True, True)),
              patch.object(network, "runtime_state", return_value=network.OFFLINE),
              patch.object(Path, "read_text", side_effect=FileNotFoundError)):
            skills = Skills(Path("/mind"))
            self.assertFalse(skills._web_lookups_permitted())
        with (patch("mind.skills.load_policy", return_value=NetworkPolicy(INTERNET, True, True)),
              patch.object(network, "runtime_state", return_value={"mode": INTERNET})):
            self.assertTrue(skills._web_lookups_permitted())
        with (patch("mind.skills.load_policy", return_value=NetworkPolicy(INTERNET, True)),
              patch.object(network, "runtime_state", return_value={"mode": INTERNET})):
            self.assertFalse(skills._web_lookups_permitted())

    def test_status_json_reports_requested_and_effective_modes(self):
        save_policy(self.directory, NetworkPolicy(INTERNET, True, True))
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            network.status(self.directory, True)
        self.assertEqual(json.loads(capture.getvalue()), {"requested_mode": INTERNET, **network.OFFLINE})
class CableRulesTests(unittest.TestCase):
    """The cable development exception inside render_rules (development Try sticks only)."""

    ADDRESSES = {"169.254.0.0/16", "169.254.77.1", "fe80::/10", "fe80::77:1"}

    def cable_lines(self, mode, *args):
        plain = network.render_rules(mode, *args).splitlines()
        cabled = network.render_rules(mode, *args, cable=CABLE).splitlines()
        added = [line.strip() for line in cabled if line not in plain]
        # Nothing of the release table is lost or reordered.
        self.assertEqual([line for line in cabled if line in plain], plain)
        return added

    def test_without_cable_state_the_release_tables_are_byte_identical(self):
        self.assertEqual(network.render_rules(LOCAL_ONLY), LOCAL_ONLY_TABLE)
        self.assertEqual(network.render_rules(LOCAL_ONLY, PAIR, None, None), LOCAL_ONLY_TABLE)
        for mode, args in ((PAIRED_LAN, (PAIR,)), (INTERNET, (PAIR, "192.0.2.60")), (INTERNET, ())):
            with self.subTest(mode=mode):
                self.assertEqual(network.render_rules(mode, *args, cable=None), network.render_rules(mode, *args))

    def test_cable_rules_name_only_the_port_the_link_local_networks_and_ssh(self):
        for mode, args in ((LOCAL_ONLY, ()), (PAIRED_LAN, (PAIR,)), (INTERNET, (PAIR, "192.0.2.60"))):
            with self.subTest(mode=mode):
                added = self.cable_lines(mode, *args)
                self.assertEqual(len(added), 10)
                for line in added:
                    self.assertRegex(line, r'^(iifname|oifname) "enp1s0f0" ')
                    self.assertTrue(line.endswith(" accept"), line)
                    addresses = set(re.findall(r"[0-9a-f]+[.:][0-9a-f.:]*(?:/\d+)?", line.split('" ', 1)[1]))
                    self.assertLessEqual(addresses, self.ADDRESSES, line)
                    self.assertEqual(set(re.findall(r"[sd]port (\S+)", line)) - {"22"}, set(), line)
                    self.assertNotIn("{ 22", line)
                inbound = [line for line in added if line.startswith("iifname")]
                self.assertIn('iifname "enp1s0f0" ip saddr 169.254.0.0/16 ip daddr 169.254.77.1 '
                              'tcp dport 22 accept', inbound)
                self.assertIn('iifname "enp1s0f0" ip6 saddr fe80::/10 ip6 daddr fe80::77:1 tcp dport 22 accept',
                              inbound)
                # Replies leave only as part of a connection the Mac opened.
                for line in added:
                    if "sport 22" in line:
                        self.assertIn("ct state established accept", line)
                self.assertFalse([line for line in added if "dport 22" in line and line.startswith("oifname")])

    def test_local_only_with_cable_opens_nothing_else(self):
        rules = network.render_rules(LOCAL_ONLY, PAIR, cable=CABLE)
        self.assertNotIn(PAIR["controller"], rules)
        self.assertNotIn("ct state established,related", rules)
        self.assertNotIn("udp", rules)
        self.assertEqual(rules.count("policy drop;"), 3)
        # Loopback first, then the cable, in each chain.
        self.assertLess(rules.index('iifname "lo" accept'), rules.index('iifname "enp1s0f0"'))
        self.assertLess(rules.index('oifname "lo" accept'), rules.index('oifname "enp1s0f0"'))

    def test_an_unsafe_interface_name_never_reaches_nft(self):
        for name in ('eth0" accept', "", "x" * 16, "-eth0", None, 7):
            with self.subTest(name=name), self.assertRaises(ValueError):
                network.render_rules(LOCAL_ONLY, cable={"interface": name})


class CableFixture(unittest.TestCase):
    """A Try boot of a cable image: the key, the command line and /run, in a scratch folder."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run = self.directory / "run"
        self.run.mkdir()
        self.keys = self.directory / "cable/authorized_keys"
        self.keys.parent.mkdir()
        self.keys.write_text('from="169.254.0.0/16,fe80:*",restrict,pty ssh-ed25519 AAAA\n')
        self.cmdline = self.directory / "cmdline"
        self.cmdline.write_text("root=PARTUUID=x ro being.try=1 being.screen=1 being.cable=1 quiet\n")
        self.enterContext(patch.object(network, "RUN", self.run))
        self.enterContext(patch.object(network, "CABLE_KEYS", self.keys))
        self.enterContext(patch.object(network, "CMDLINE", self.cmdline))

    def write(self, state=None, mode=0o644, raw=None):
        path = self.run / network.CABLE_FILE
        path.write_bytes(raw if raw is not None else json.dumps(state or {
            "interface": "enp1s0f0", "ipv4": "169.254.77.1/16", "ipv6": "fe80::77:1/64"}).encode())
        path.chmod(mode)
        return path


class CableStateTests(CableFixture):
    def test_reads_the_state_cable_up_writes(self):
        self.write()
        self.assertEqual(network.load_cable(), CABLE)

    def test_missing_key_try_boot_or_kill_switch_means_no_cable(self):
        self.write()
        for cmdline in ("root=x ro being.cable=1\n", "being.try=1 being.cable=1 being.cable=0\n",
                        "being.try=1 being.cable=0\n", "being.try=10\n"):
            with self.subTest(cmdline=cmdline):
                self.cmdline.write_text(cmdline)
                self.assertIsNone(network.load_cable())
                self.assertFalse(network.cable_enabled())
        self.cmdline.write_text("being.try=1\n")
        self.assertEqual(network.load_cable(), CABLE)
        self.keys.unlink()
        self.assertIsNone(network.load_cable())
        self.cmdline.unlink()
        self.keys.write_text("key\n")
        self.assertIsNone(network.load_cable())

    def test_rejects_a_file_others_can_change_or_another_owner(self):
        self.write(mode=0o664)
        self.assertIsNone(network.load_cable())
        self.write(mode=0o646)
        self.assertIsNone(network.load_cable())
        self.write(mode=0o600)
        self.assertEqual(network.load_cable(), CABLE)
        with patch.object(network.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertIsNone(network.load_cable())

    def test_rejects_links_and_bad_schemas(self):
        good = {"interface": "enp1s0f0", "ipv4": "169.254.77.1/16", "ipv6": "fe80::77:1/64"}
        for state in (dict(good, interface='eth0" accept'), dict(good, interface=3), dict(good, ipv4="10.0.0.1/8"),
                      dict(good, ipv6="fe80::1/64"), dict(good, extra=1), {"interface": "eth0"}, ["eth0"]):
            with self.subTest(state=state):
                self.write(raw=json.dumps(state).encode())
                self.assertIsNone(network.load_cable())
        for raw in (b"{not json", b'{"interface": "a", "interface": "b", "ipv4": "", "ipv6": ""}',
                    b" " * 5000, b"[" * 100000):
            with self.subTest(raw=raw[:20]):
                self.write(raw=raw)
                self.assertIsNone(network.load_cable())
        real = self.directory / "elsewhere.json"
        real.write_text(json.dumps(good))
        (self.run / network.CABLE_FILE).unlink()
        (self.run / network.CABLE_FILE).symlink_to(real)
        self.assertIsNone(network.load_cable())

    def test_status_reports_the_cable_line(self):
        self.write()
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            network.status(self.directory, False)
        self.assertIn("Cable development on (enp1s0f0, 169.254.77.1, fe80::77:1)\n", capture.getvalue())
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            network.status(self.directory, True)
        self.assertEqual(json.loads(capture.getvalue())["cable"],
                         {"interface": "enp1s0f0", "ipv4": "169.254.77.1", "ipv6": "fe80::77:1"})
        (self.run / network.CABLE_FILE).unlink()
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            network.status(self.directory, False)
        self.assertNotIn("Cable", capture.getvalue())


class CableReconcileTests(CableFixture):
    """Every reconcile keeps the cable link up and its rules loaded."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(network, "TIME_CONF", self.directory / "timesyncd/99.conf"))
        self.enterContext(patch.object(network, "ETHERNET_FILE", self.directory / "02-being-ethernet.network"))
        units = self.directory / "units"
        units.mkdir()
        for unit in network.NETWORK_UNITS:
            (units / unit).write_text("[Unit]\n")
        self.enterContext(patch.object(network, "UNIT_DIRS", (units,)))
        self.links = self.directory / "sys_net"
        for name in ("enp1s0f0", "lo", "wlan0"):
            (self.links / name).mkdir(parents=True)
            (self.links / name / "flags").write_text("0x1003")
        self.enterContext(patch.object(network, "SYS_NET", self.links))
        self.write()

    def reconcile(self, mode):
        save_policy(self.directory, NetworkPolicy(mode, system_network=mode != LOCAL_ONLY))
        tables, lowered = [], []

        def run(command, **kwargs):
            if command[:1] == ["/usr/sbin/nft"]:
                tables.append(kwargs["input"])
            elif command[-1] == "down":
                lowered.append(command[-2])
            return subprocess.CompletedProcess(command, 0, "", "")

        with (patch.object(network.subprocess, "run", side_effect=run),
              patch.object(network, "systemctl"),
              patch("mind.lab_access.load_pair", return_value=None),
              patch("mind.lab_access.configure_ethernet")):
            network.reconcile(self.directory)
        return tables, lowered

    def test_local_only_and_internet_reconciles_keep_the_link_and_its_rules(self):
        for mode in (LOCAL_ONLY, INTERNET):
            with self.subTest(mode=mode):
                tables, lowered = self.reconcile(mode)
                self.assertEqual(lowered, ["wlan0"])
                self.assertEqual(len(tables), 2)  # the lockdown, then the mode
                for table in tables:
                    self.assertIn('iifname "enp1s0f0" ip saddr 169.254.0.0/16 ip daddr 169.254.77.1 '
                                  'tcp dport 22 accept', table)
                self.assertEqual(tables[-1], network.render_rules(mode, None, None, CABLE))

    def test_without_cable_state_reconcile_lowers_every_link(self):
        (self.run / network.CABLE_FILE).unlink()
        tables, lowered = self.reconcile(LOCAL_ONLY)
        self.assertEqual(lowered, ["enp1s0f0", "wlan0"])
        self.assertEqual(tables, [LOCAL_ONLY_TABLE, LOCAL_ONLY_TABLE])


class CableUpTests(CableFixture):
    def setUp(self):
        super().setUp()
        self.links = self.directory / "sys_net"
        self.conf = self.directory / "ipv6_conf"
        self.network_file = self.directory / "network/00-being-cable.network"
        self.enterContext(patch.object(network, "SYS_NET", self.links))
        self.enterContext(patch.object(network, "IPV6_CONF", self.conf))
        self.enterContext(patch.object(network, "CABLE_NETWORK", self.network_file))
        self.applied = []
        self.enterContext(patch.object(network, "apply", side_effect=lambda mind: self.applied.append(
            network.load_cable())))
        self.calls = []
        self.arping = subprocess.CompletedProcess([], 0, "Sent 2 probe(s) (2 broadcast(s))\n"
                                                  "Received 0 response(s) (0 request(s), 0 broadcast(s))\n", "")
        self.addresses = ""

    def port(self, name, kind="1", *, device=True, wireless=False):
        path = self.links / name
        path.mkdir(parents=True)
        (path / "type").write_text(kind + "\n")
        if device:
            (path / "device").mkdir()
        if wireless:
            (path / "wireless").mkdir()
        (self.conf / name).mkdir(parents=True)
        for setting in ("disable_ipv6", "addr_gen_mode", "accept_ra", "autoconf"):
            (self.conf / name / setting).write_text("9\n")

    def fake_run(self, command, **kwargs):
        self.calls.append(list(command))
        if command[0] == network.ARPING:
            return self.arping
        if command[:3] == [network.IP, "-o", "addr"]:
            return subprocess.CompletedProcess(command, 0, self.addresses, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def cable_up(self):
        with patch.object(network.subprocess, "run", side_effect=self.fake_run):
            return network.cable_up(self.directory, wait=0)

    def test_brings_up_the_first_wired_port_by_type_and_opens_the_firewall(self):
        self.port("aa-virtual", device=False)
        self.port("eth9", kind="776")
        self.port("wlp2s0", wireless=True)
        self.port("enp1s0f0")
        self.port("enp9s0")
        self.assertEqual(self.cable_up(), "enp1s0f0")
        commands = [" ".join(call) for call in self.calls]
        self.assertEqual(commands, [
            "/usr/bin/udevadm settle --timeout=15",
            "/usr/bin/ip link set dev enp1s0f0 up",
            "/usr/bin/ip -o addr show dev enp1s0f0",
            "/usr/sbin/arping -D -I enp1s0f0 -c 2 169.254.77.1",
            "/usr/bin/ip addr add 169.254.77.1/16 dev enp1s0f0",
            "/usr/bin/ip -6 addr add fe80::77:1/64 dev enp1s0f0",
        ])
        for setting, value in (("disable_ipv6", "0"), ("addr_gen_mode", "1"), ("accept_ra", "0"), ("autoconf", "0")):
            self.assertEqual((self.conf / "enp1s0f0" / setting).read_text(), value + "\n")
        self.assertEqual((self.conf / "enp9s0" / "addr_gen_mode").read_text(), "9\n")
        self.assertEqual(self.network_file.read_text().split("\n", 1)[1],
                         "[Match]\nName=enp1s0f0\n\n[Link]\nUnmanaged=yes\n")
        self.assertEqual(json.loads((self.run / network.CABLE_FILE).read_text()),
                         {"interface": "enp1s0f0", "ipv4": "169.254.77.1/16", "ipv6": "fe80::77:1/64"})
        self.assertEqual((self.run / network.CABLE_FILE).stat().st_mode & 0o777, 0o644)
        # The firewall is applied after the state exists, so it carries the cable rules.
        self.assertEqual(self.applied, [CABLE])

    def test_running_again_keeps_the_addresses_it_already_has(self):
        self.port("eth0")
        self.addresses = ("2: eth0    inet 169.254.77.1/16 scope global eth0\\       valid_lft forever\n"
                          "2: eth0    inet6 fe80::77:1/64 scope link \\       valid_lft forever\n")
        self.assertEqual(self.cable_up(), "eth0")
        self.assertFalse([call for call in self.calls if "add" in call or call[0] == network.ARPING])
        self.assertEqual(self.applied, [{"interface": "eth0"}])

    def test_refuses_an_address_another_device_answers_for(self):
        self.port("eth0")
        self.arping = subprocess.CompletedProcess([], 1, "Sent 1 probe(s) (1 broadcast(s))\n"
                                                  "Received 1 response(s) (0 request(s), 0 broadcast(s))\n", "")
        with self.assertRaisesRegex(ValueError, "already uses 169.254.77.1"):
            self.cable_up()
        self.assertFalse([call for call in self.calls if "add" in call])
        self.assertFalse((self.run / network.CABLE_FILE).exists())
        self.assertEqual(self.applied, [])

    def test_a_probe_that_cannot_run_does_not_stop_the_link(self):
        self.port("eth0")
        self.arping = subprocess.CompletedProcess([], 1, "", "arping: socket: Operation not permitted\n")
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            self.assertEqual(self.cable_up(), "eth0")
        self.assertIn("could not check 169.254.77.1", warning.getvalue())

    def test_refuses_when_the_cable_is_off_or_there_is_no_port(self):
        self.cmdline.write_text("being.try=1 being.cable=0\n")
        with self.assertRaisesRegex(ValueError, "off on this boot"):
            self.cable_up()
        self.assertEqual(self.calls, [])
        self.cmdline.write_text("being.try=1\n")
        self.port("wlan0", wireless=True)
        with self.assertRaisesRegex(ValueError, "No wired network port"):
            self.cable_up()
        self.assertFalse(self.network_file.exists())

    def test_waits_for_a_port_whose_driver_is_still_loading(self):
        moments = iter([0.0, 0.0, 0.6, 1.2])
        naps = []

        def nap(seconds):
            naps.append(seconds)
            if len(naps) == 2:
                self.port("eth0")

        self.assertEqual(network.wait_for_wired(5, sleep=nap, clock=lambda: next(moments)), "eth0")
        self.assertEqual(naps, [0.5, 0.5])


if __name__ == "__main__":
    unittest.main()
