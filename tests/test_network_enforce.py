import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mind import network_enforce as network
from mind.network_policy import INTERNET, LOCAL_ONLY, PAIRED_LAN, NetworkPolicy, save_policy
from mind.skills import Skills


PAIR = {"address": "192.168.0.20", "controller": "192.168.0.10", "ethernet": "eth0", "dev_enabled": True}


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
        nft.assert_called_once_with(LOCAL_ONLY, None, None)
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
