import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from mind import wifi


class WifiProfile(unittest.TestCase):
    def test_wpa2_derives_the_standard_preshared_key(self):
        profile = wifi.Profile.from_password("wlan0", "IEEE", "wpa2", "password")
        self.assertIn("ssid=49454545", profile.supplicant_config())
        self.assertIn(
            "psk=f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e",
            profile.supplicant_config(),
        )
        self.assertNotIn("password", repr(profile))

    def test_wpa3_preserves_bytes_without_supplicant_config_injection(self):
        ssid = 'He said "bøø"\n#'
        password = 'WPA3 "#\\&å + whitespace'
        profile = wifi.Profile.from_password("labsta", ssid, "wpa3", password)
        lines = profile.supplicant_config().splitlines()
        self.assertIn(f"    ssid={ssid.encode().hex()}", lines)
        self.assertIn(f"    sae_password={password.encode().hex()}", lines)
        self.assertIn("    ieee80211w=2", lines)
        self.assertNotIn(password, profile.supplicant_config())

    def test_rejects_interface_patterns_and_out_of_range_credentials(self):
        bad_inputs = (
            ("wl*", "home", "wpa2", "12345678"),
            ("labsta\n[Network]", "home", "wpa2", "12345678"),
            ("wlan0", "ø" * 17, "wpa2", "12345678"),
            ("wlan0", "home", "wpa2", "small"),
            ("wlan0", "home", "wpa3", "\0"),
        )
        for arguments in bad_inputs:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                wifi.Profile.from_password(*arguments)

    def test_profile_file_stays_private_and_never_displays_its_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "network" / "wifi.json"
            profile = wifi.Profile.from_password("wlan0", "home", "wpa3", "my-secret-passphrase")
            result = CompletedProcess([], 0, f"wpa_state=COMPLETED\nprivate_key={profile.key}\n")
            with (patch.object(wifi, "PROFILE", path), patch.object(wifi.subprocess, "run", return_value=result),
                  patch.object(wifi, "effective_mode", return_value=wifi.PAIRED_LAN)):
                with wifi.owner_change():
                    wifi.atomic_write(path, json.dumps(asdict(profile)))
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(wifi.load_profile(), profile)
                with redirect_stdout(io.StringIO()) as output:
                    wifi.status()
                self.assertIn("wpa_state=COMPLETED", output.getvalue())
                self.assertNotIn(profile.key, output.getvalue())

                os.chmod(path, 0o644)
                with self.assertRaises(PermissionError):
                    wifi.load_profile()


class WifiNetworkPolicyTests(unittest.TestCase):
    def test_paired_network_uses_ipv4_dhcp_without_resolver_or_discovery(self):
        config = wifi.networkd_config("wlan0", wifi.PAIRED_LAN)
        self.assertIn("[Match]\nName=wlan0\n", config)
        self.assertIn("DHCP=ipv4\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n", config)
        self.assertIn("LLMNR=no\nMulticastDNS=no\nLLDP=no\nEmitLLDP=no\n", config)
        self.assertIn("[DHCPv4]\nUseDNS=no\nUseNTP=no\nUseDomains=no\n", config)
        self.assertNotIn("[DHCPv6]", config)

    def test_internet_network_accepts_dns_but_refuses_dhcp_time_and_local_discovery(self):
        config = wifi.networkd_config("wlan0", wifi.INTERNET)
        self.assertIn("DHCP=yes\nLinkLocalAddressing=ipv6\nIPv6AcceptRA=yes\n", config)
        for family in ("DHCPv4", "DHCPv6"):
            self.assertIn(f"[{family}]\nUseDNS=yes\nUseNTP=no\nUseDomains=no\n", config)
        self.assertIn("LLMNR=no\nMulticastDNS=no\nLLDP=no\nEmitLLDP=no\n", config)

    def test_wifi_refuses_an_unapplied_or_local_only_policy(self):
        profile = wifi.Profile.from_password("wlan0", "IEEE", "wpa2", "password")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "mode"
            with (patch.object(wifi, "NETWORK_MODE", marker), patch.object(wifi, "load_profile", return_value=profile),
                  patch.object(wifi.os, "execvp") as connect):
                self.assertEqual(wifi.effective_mode(), wifi.LOCAL_ONLY)
                for mode in ("", "local-only", "invalid", "paired"):
                    marker.write_text(mode + "\n")
                    with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "has not authorized"):
                        wifi.serve()
                connect.assert_not_called()
                marker.write_text("paired-lan\n")
                self.assertEqual(wifi.effective_mode(), wifi.PAIRED_LAN)


class OfflineWifi(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.mount = Path(work).resolve() / "Being mind"
        self.mount.mkdir()
        self.info = self.mount.parent / "mountinfo"
        self.enterContext(patch.object(wifi, "MOUNTINFO", self.info))
        self.enterContext(patch.object(wifi.sys, "platform", "linux"))
        self.enterContext(patch.object(wifi, "node_number", return_value="8:5"))
        self.probe = self.enterContext(
            patch.object(wifi.subprocess, "run", return_value=CompletedProcess([], 0, "mind\n"))
        )

    def mounted(self, *, mount_options="rw", filesystem="ext4", device="8:5", root="/"):
        encoded = str(self.mount).replace(" ", r"\040")
        self.info.write_text(
            "33 1 8:2 / / rw - ext4 /dev/sda2 rw\n"
            f"49 33 {device} {root} {encoded} {mount_options} - {filesystem} /dev/sda5 rw\n"
        )

    def test_offline_configure_saves_the_existing_private_format_without_restarting_wifi(self):
        self.mounted()
        args = ["being-wifi", "configure", "wlan0", "Home", "--offline-mind", str(self.mount)]
        with (patch.object(wifi.sys, "argv", args), patch.object(wifi.getpass, "getpass", return_value="password"),
              patch.object(wifi.os, "geteuid", side_effect=[0, os.geteuid()])):
            wifi.main()
        profile_path = self.mount / "network" / "wifi.json"
        self.assertEqual(stat.S_IMODE(profile_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(profile_path.stat().st_mode), 0o600)
        with patch.object(wifi, "PROFILE", profile_path):
            self.assertEqual(wifi.load_profile(), wifi.Profile.from_password("wlan0", "Home", "wpa2", "password"))
        self.probe.assert_called_once_with(
            ["blkid", "-p", "-s", "LABEL", "-o", "value", "--", "/dev/sda5"],
            capture_output=True, text=True, timeout=5,
        )

    def test_refuses_live_root_wrong_filesystem_bind_subdirectory_and_read_only_mount(self):
        for mounted in ({"device": "8:2"}, {"filesystem": "tmpfs"}, {"root": "/another"}, {"mount_options": "ro"}):
            with self.subTest(mounted=mounted), self.assertRaisesRegex(ValueError, "separate read-write ext4"):
                self.mounted(**mounted)
                wifi.offline_mind_mount(self.mount)
        with self.assertRaisesRegex(ValueError, "separate read-write ext4"):
            wifi.offline_mind_mount(self.mount.parent)
        self.probe.assert_not_called()

    def test_wrong_label_is_rejected_before_password_prompt_or_write(self):
        self.mounted()
        self.probe.return_value = CompletedProcess([], 0, "other-label\n")
        args = ["being-wifi", "configure", "wlan0", "Home", "--offline-mind", str(self.mount)]
        with (patch.object(wifi.sys, "argv", args), patch.object(wifi.os, "geteuid", return_value=0),
              patch.object(wifi.getpass, "getpass") as prompt):
            with self.assertRaisesRegex(SystemExit, "PipOS 'mind' label"):
                wifi.main()
            prompt.assert_not_called()
        self.assertFalse((self.mount / "network").exists())


if __name__ == "__main__":
    unittest.main()
