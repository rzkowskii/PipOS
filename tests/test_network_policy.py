import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from mind.network_policy import MODES, POLICY_PATH, NetworkPolicy, load_policy, save_policy, validate_time_server


class NetworkPolicyTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / POLICY_PATH

    def test_missing_policy_defaults_to_local_only_without_creating_files(self):
        self.assertEqual(load_policy(self.root), NetworkPolicy("local-only", False))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_owner_choices_round_trip_and_only_internet_with_consent_allows_web(self):
        for mode in MODES:
            for allowed in (False, True):
                with self.subTest(mode=mode, allowed=allowed):
                    policy = NetworkPolicy(mode, allowed)
                    save_policy(self.root, policy)
                    self.assertEqual(load_policy(self.root), policy)
                    self.assertEqual(policy.permits_web_lookup, mode == "internet" and allowed)
                    self.assertEqual(policy.effective_mode, "local-only")
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {
            "version": 1, "mode": "internet", "web_lookups": True,
        })

    def test_explicit_system_network_and_numeric_clock_round_trip(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                time_server = "2001:0db8:0000::7" if mode == "internet" else None
                policy = NetworkPolicy(mode, False, system_network=True, time_server=time_server)
                save_policy(self.root, policy)
                saved = load_policy(self.root)
                self.assertEqual(saved, policy)
                self.assertEqual(saved.effective_mode, mode)
                self.assertFalse(saved.permits_web_lookup)
                self.assertEqual(json.loads(self.path.read_text()), {
                    "version": 2, "mode": mode, "web_lookups": False, "system_network": True,
                    "time_server": "2001:db8::7" if time_server else None,
                })
        self.assertEqual(validate_time_server("10.1.2.3"), "10.1.2.3")
        self.assertEqual(validate_time_server("fd00::7"), "fd00::7")
        self.assertEqual(validate_time_server("1.1.1.1"), "1.1.1.1")

    def test_clock_requires_explicit_internet_and_a_supported_address(self):
        for mode, enabled in (("internet", False), ("paired-lan", True), ("local-only", True)):
            with self.subTest(mode=mode, enabled=enabled), self.assertRaises(ValueError):
                NetworkPolicy(mode, True, system_network=enabled, time_server="1.2.3.4")
        for address in ("timeserver.test", "1.2.3.4/32", " 1.2.3.4", "[2001:db8::5]", "fe80::1%wlan0",
                        "127.0.0.1", "169.254.10.11", "0.1.2.3", "255.255.255.255", "224.0.0.1",
                        "::1", "::", "ff02::1", "::ffff:127.0.0.1", 16909060):
            with self.subTest(address=address), self.assertRaises(ValueError):
                NetworkPolicy("internet", False, system_network=True, time_server=address)
        with self.assertRaises(ValueError):
            NetworkPolicy("internet", False, system_network=1)

    def test_invalid_documents_cannot_enable_web_lookups(self):
        save_policy(self.root, NetworkPolicy("internet", True))
        for content in (
            b"{", b"[]", b"\xff",
            b'{"version":2,"mode":"internet","web_lookups":true}',
            b'{"version":true,"mode":"internet","web_lookups":true}',
            b'{"version":1,"mode":"unknown","web_lookups":true}',
            b'{"version":1,"mode":"internet","web_lookups":1}',
            b'{"version":1,"mode":"internet"}',
            b'{"version":1,"mode":"internet","web_lookups":true}' + b" " * 1024,
            b'{"version":2,"mode":"internet","web_lookups":true,"system_network":true}',
            b'{"version":2,"mode":"internet","web_lookups":true,"system_network":1,"time_server":null}',
            b'{"version":2,"mode":"internet","web_lookups":true,"system_network":false,"time_server":"1.2.3.4"}',
            b'{"version":2,"mode":"internet","web_lookups":true,"system_network":true,"time_server":"timeserver.test"}',
            b'{"version":2,"mode":"internet","web_lookups":true,"system_network":false,"system_network":true,"time_server":null}',
        ):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assertEqual(load_policy(self.root), NetworkPolicy())

    def test_public_files_symlinks_and_pipes_cannot_supply_consent(self):
        save_policy(self.root, NetworkPolicy("internet", True))
        self.path.chmod(0o644)
        self.assertEqual(load_policy(self.root), NetworkPolicy())
        self.path.chmod(0o600)
        self.path.parent.chmod(0o755)
        self.assertEqual(load_policy(self.root), NetworkPolicy())
        with self.assertRaises(PermissionError):
            save_policy(self.root, NetworkPolicy())
        self.path.parent.chmod(0o700)
        elsewhere = self.root / "another-policy.json"
        self.path.rename(elsewhere)
        self.path.symlink_to(elsewhere)
        self.assertEqual(load_policy(self.root), NetworkPolicy())
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.assertEqual(load_policy(self.root), NetworkPolicy())

    def test_invalid_write_is_rejected_before_it_can_replace_the_policy(self):
        enabled = NetworkPolicy("internet", True)
        save_policy(self.root, enabled)
        with self.assertRaises(ValueError):
            NetworkPolicy("internet", "true")
        with self.assertRaises(TypeError):
            save_policy(self.root, {"mode": "local-only"})
        self.assertEqual(load_policy(self.root), enabled)

    def test_v2_explicitly_disabled_transport_does_not_override_legacy_skill_behavior(self):
        save_policy(self.root, NetworkPolicy())
        self.path.write_text(json.dumps({
            "version": 2, "mode": "internet", "web_lookups": True, "system_network": False, "time_server": None,
        }))
        policy = load_policy(self.root)
        self.assertEqual(policy.effective_mode, "local-only")
        self.assertTrue(policy.permits_web_lookup)
