"""QEMU integration coverage for Linux Wi-Fi using simulated radios.

Set BEING_WIFI_IMAGE to the policy-enabled hwsim companion explicitly. The
AP and its address and HTTP fixtures all live inside a disposable guest,
separate from the station and the host network.
"""

import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import time
import unittest
from unittest.mock import patch

from lab import vm


IMAGE = os.environ.get("BEING_WIFI_IMAGE")
RUN = "/run/being-wifi-test"
AP_NAMESPACE = "being-wifi-ap"
STATION = "labsta"
AP = "labap"
AP_ADDRESS = "192.0.2.1"
DNS_NAME = "being.test"
DHCP_TIME = "192.0.2.88"
PORT = 18080
PASSPHRASE = "synthetic-fixture-only"
SAE_PASSPHRASE = 'synthetic-"\\sae-only'
MARKER = "response from the simulated Wi-Fi access point"
MANAGED_PROFILE = "/mind/network/wifi.json"
MANAGED_CONTROL = "/run/being-wifi"
MANAGED_NETWORK = "/run/systemd/network/00-being-wifi.network"
FIXTURE_NETWORK = "/run/systemd/network/02-being-wifi-test.network"
WIFI_COMMAND = "/usr/sbin/being-wifi"


class Wifi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not IMAGE or not Path(IMAGE).is_file():
            # Outside gate mode a missing companion is a skip; BEING_GATE=1 turns it into a failure.
            raise unittest.SkipTest("Set BEING_WIFI_IMAGE to the policy-enabled hwsim companion.")

    def sh(self, command, timeout=30):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def wait(self, command, check=bool, timeout=45):
        deadline = time.monotonic() + timeout
        output = ""
        while time.monotonic() < deadline:
            status, output = self.vm.run(command, 12)
            if status == 0 and check(output):
                return output
            time.sleep(0.25)
        _, logs = self.vm.run(
            f"tail -30 {RUN}/*.log 2>/dev/null; "
            "journalctl -b -u systemd-networkd -u being-wifi.service -n 20 --no-pager -o cat",
            12,
        )
        self.fail(f"timed out waiting for {command}:\n{output}\nGuest fixture logs:\n{logs}")

    def start_lab(self, *, managed=False):
        self.vm = vm.VM(image=Path(IMAGE).resolve())
        self.addCleanup(self.vm.destroy)
        self.boot_lab(managed=managed)

    def assert_mode(self, mode):
        status = json.loads(self.sh("being-network status --json"))
        self.assertEqual(status["requested_mode"], mode, status)
        self.assertEqual(status["mode"], mode, status)
        self.assertIsNone(status["effective_time_server"], status)

    def boot_lab(self, *, managed, reconnect=False):
        original = vm.machine_args

        def isolate(*args, **kwargs):
            command = original(*args, **kwargs)
            command[command.index("user,id=net0")] = "user,id=net0,restrict=on"
            return command

        with patch.object(vm, "machine_args", isolate):
            self.vm.start()
        self.vm.login(120)
        self.sh(
            "command -v iw && command -v ip && command -v hostapd && "
            "command -v wpa_supplicant && command -v wpa_cli && command -v dnsmasq && "
            "command -v networkctl && command -v being-network && "
            f"mkdir -p {RUN}/control {RUN}/www /run/systemd/network"
        )
        self.assert_mode("internet" if reconnect else "local-only")
        # Remove the emulated wired carrier as a possible route to the fixture.
        self.sh("for device in /sys/class/net/*; do "
                'iface=${device##*/}; [ "$iface" = lo ] || ip link set "$iface" down || exit 1; '
                "done")
        self.sh("test -d /sys/module/mac80211_hwsim || modprobe mac80211_hwsim radios=2")
        radios = re.findall(r"(?ms)^phy#(\d+)\s.*?^\s+Interface (\S+)", self.sh("iw dev"))
        self.assertEqual(len(radios), 2, radios)
        (ap_phy, ap_interface), (_, station_interface) = radios

        # Create both PHYs in the initial namespace so the simulated medium is
        # shared, then isolate AP addressing to force traffic through the PHYs.
        self.sh(f"ip link set {ap_interface} down && ip link set {station_interface} down && "
                f"ip netns add {AP_NAMESPACE} && "
                f"iw phy phy{ap_phy} set netns name {AP_NAMESPACE} && "
                f"ip -n {AP_NAMESPACE} link set {ap_interface} name {AP} && "
                f"ip link set {station_interface} name {STATION} && "
                f"ip -n {AP_NAMESPACE} addr add {AP_ADDRESS}/24 dev {AP} && "
                f"ip -n {AP_NAMESPACE} link set {AP} up && "
                f"ip -n {AP_NAMESPACE} link set lo up")
        self.vm.put(f"{RUN}/www/check", MARKER)
        self.vm.put(f"{RUN}/check.py", f"""import sys
from urllib.request import ProxyHandler, build_opener
host = sys.argv[1] if len(sys.argv) > 1 else '{AP_ADDRESS}'
opener = build_opener(ProxyHandler({{}}))
with opener.open(f'http://{{host}}:{PORT}/check', timeout=3) as response:
    print(response.read().decode())
""")
        if not managed:
            self.vm.put(FIXTURE_NETWORK, f"""[Match]
Name={STATION}

[Network]
DHCP=ipv4
LinkLocalAddressing=no
IPv6AcceptRA=no
IgnoreCarrierLoss=no

[DHCPv4]
UseRoutes=no
UseGateway=no
UseDNS=no
UseNTP=no
""")
        if not reconnect:
            # Reconciliation starts networkd after the station and its optional
            # fixture file are ready. A reboot must use the saved policy itself.
            self.sh("being-network set internet", 110)
        self.assert_mode("internet")
        self.wait("systemctl is-active systemd-networkd.service", lambda text: text == "active")
        self.sh("! systemctl is-active --quiet systemd-timesyncd.service")
        dns_options = (
            f"--no-resolv --host-record={DNS_NAME},{AP_ADDRESS} --dhcp-option=6,{AP_ADDRESS} "
            f"--dhcp-option=42,{DHCP_TIME}"
            if managed else "--port=0 --dhcp-option=6"
        )
        self.sh(f"(ip netns exec {AP_NAMESPACE} dnsmasq --keep-in-foreground --conf-file=/dev/null "
                f"--interface={AP} --bind-interfaces --user=root --log-facility=- --log-dhcp {dns_options} "
                "--dhcp-range=192.0.2.20,192.0.2.30,255.255.255.0,5m --dhcp-option=3 "
                f"--pid-file={RUN}/dnsmasq.pid --dhcp-leasefile={RUN}/leases >{RUN}/dhcp.log 2>&1 &)")
        self.sh(f"(ip netns exec {AP_NAMESPACE} python3 -m http.server --bind {AP_ADDRESS} "
                f"--directory {RUN}/www {PORT} >{RUN}/http.log 2>&1 &)")
        self.wait(f"grep 'started, version' {RUN}/dhcp.log")
        self.vm.wait_state("idle", 120)

    def start_ap(self, ssid, key_management, pmf, *, password=PASSPHRASE):
        self.vm.put(f"{RUN}/ap.conf", f"""interface={AP}
driver=nl80211
ssid={ssid}
hw_mode=g
channel=1
wpa=2
wpa_key_mgmt={key_management}
wpa_passphrase={password}
rsn_pairwise=CCMP
ieee80211w={pmf}
""")
        self.sh(f"(ip netns exec {AP_NAMESPACE} hostapd {RUN}/ap.conf >{RUN}/ap.log 2>&1 & "
                f"echo $! >{RUN}/ap.pid)")
        self.wait(f"grep AP-ENABLED {RUN}/ap.log")

    def stop_ap(self):
        self.sh(f'kill "$(cat {RUN}/ap.pid)"')
        self.wait(f'! kill -0 "$(cat {RUN}/ap.pid)" 2>/dev/null && echo stopped')

    @property
    def station_cli(self):
        return f"wpa_cli -p {RUN}/control -i {STATION}"

    def start_station(self, ssid, key_management, pmf, *, password=PASSPHRASE):
        self.vm.put(f"{RUN}/station.conf", f"""ctrl_interface={RUN}/control
network={{
    ssid="{ssid}"
    key_mgmt={key_management}
    psk="{password}"
    pairwise=CCMP
    ieee80211w={pmf}
}}
""")
        self.sh(f"(wpa_supplicant -Dnl80211 -i {STATION} -c {RUN}/station.conf >{RUN}/station.log 2>&1 &)")
        self.wait(f"{self.station_cli} ping", lambda output: output == "PONG")

    def stop_station(self):
        self.sh(f"{self.station_cli} terminate")
        self.wait(f"test ! -S {RUN}/control/{STATION} && echo stopped")

    def configure_managed(self, ssid, security, password):
        arguments = shlex.join(("configure", STATION, ssid, "--security", security, "--password-stdin"))
        output = self.sh(f"printf '%s\\n' {shlex.quote(password)} | {WIFI_COMMAND} {arguments}")
        self.assertNotIn(password, output)

    def mode_and_owner(self, path):
        code = "import os, stat, sys; info = os.stat(sys.argv[1]); print(f'{stat.S_IMODE(info.st_mode):o}:{info.st_uid}')"
        return self.sh(shlex.join(("python3", "-c", code, path)))

    def check_connection(self, ssid, key_management, *, cli=None):
        cli = cli or self.station_cli
        output = self.wait(f"{cli} status", lambda text: "wpa_state=COMPLETED" in text)
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        self.assertEqual(values["ssid"], ssid)
        self.assertEqual(values["key_mgmt"], key_management)
        self.assertEqual(values["pairwise_cipher"], "CCMP")
        self.assertIn(f"SSID: {ssid}", self.sh(f"iw dev {STATION} link"))
        route = self.wait(f"ip -4 route get {AP_ADDRESS}", lambda text: f"dev {STATION}" in text).split()
        self.assertEqual(route[route.index("dev") + 1], STATION)
        lease = ipaddress.IPv4Address(route[route.index("src") + 1])
        self.assertLessEqual(ipaddress.IPv4Address("192.0.2.20"), lease)
        self.assertLessEqual(lease, ipaddress.IPv4Address("192.0.2.30"))
        self.assertEqual(self.sh("ip -4 route show default && ip -6 route show default"), "")
        self.assertEqual(self.wait(f"python3 {RUN}/check.py"), MARKER)
        print(f"  {ssid} negotiated {key_management}/CCMP, leased {lease} and reached the AP", flush=True)

    def test_authentication_dhcp_and_reconnect_keep_local_commands_available(self):
        self.start_lab()
        self.assertEqual(self.vm.converse("Remember that my cat is called Marzipan", 30), "Okay. I'll remember that.")

        self.start_ap("being-wpa2", "WPA-PSK", 0)
        self.start_station("being-wpa2", "WPA-PSK", 0, password="deliberately-wrong")
        self.wait(f"grep -Ei 'WRONG_KEY|pre-shared key may be incorrect' {RUN}/station.log")
        self.assertNotIn("wpa_state=COMPLETED", self.sh(f"{self.station_cli} status"))
        self.stop_station()
        self.start_station("being-wpa2", "WPA-PSK", 0)
        self.check_connection("being-wpa2", "WPA2-PSK")

        self.stop_ap()
        self.wait(f"grep CTRL-EVENT-DISCONNECTED {RUN}/station.log")
        self.wait(f"iw dev {STATION} link", lambda text: text == "Not connected.")
        self.wait(f"ip -4 -o addr show dev {STATION}", lambda text: not text)
        status, _ = self.vm.run(f"python3 {RUN}/check.py", 12)
        self.assertNotEqual(status, 0, "the AP fixture remained reachable during the outage")
        self.assertRegex(self.vm.converse("What time is it?", 30), r"\b\d{1,2}:\d{2}\b")
        self.assertEqual(self.vm.converse("What do you remember about my cat?", 30),
                         "You told me: my cat is called Marzipan.")

        self.start_ap("being-wpa2", "WPA-PSK", 0)
        self.check_connection("being-wpa2", "WPA2-PSK")
        self.stop_station()
        self.stop_ap()

        for ssid, ap_key_management, pmf in (
            ("being-transition", "WPA-PSK SAE", 1),
            ("being-wpa3", "SAE", 2),
        ):
            self.start_ap(ssid, ap_key_management, pmf)
            self.start_station(ssid, "SAE", 2)
            self.check_connection(ssid, "SAE")
            self.stop_station()
            self.stop_ap()

    def test_owner_profile_reconnects_after_reboot_and_can_be_forgotten(self):
        ssid = "being-owner"
        cli = f"wpa_cli -p {MANAGED_CONTROL} -i {STATION}"
        service_state = "systemctl show -p ActiveState --value being-wifi.service"
        self.start_lab(managed=True)
        self.assertTrue(self.sh("find /lib/modules/$(uname -r) -name 'b43.ko*'"))
        self.sh(f"test ! -e {MANAGED_PROFILE}")
        self.assertEqual(self.sh(service_state), "inactive")
        self.assertEqual(self.vm.converse("Remember that my cat is called Marzipan", 30),
                         "Okay. I'll remember that.")

        self.start_ap(ssid, "WPA-PSK", 0)
        self.configure_managed(ssid, "wpa2", PASSPHRASE)
        self.assertEqual(self.mode_and_owner("/mind/network"), "700:0")
        self.assertEqual(self.mode_and_owner(MANAGED_PROFILE), "600:0")
        self.check_connection(ssid, "WPA2-PSK", cli=cli)
        self.assertEqual(self.wait(f"python3 {RUN}/check.py {DNS_NAME}"), MARKER)
        self.sh(f"grep -Fx UseNTP=no {MANAGED_NETWORK} && ! systemctl is-active --quiet systemd-timesyncd.service")
        self.assertNotIn(PASSPHRASE, self.sh(f"{WIFI_COMMAND} status"))

        self.stop_ap()
        self.wait(f"iw dev {STATION} link", lambda text: text == "Not connected.")
        self.wait(f"ip -4 -o addr show dev {STATION}", lambda text: not text)
        self.assertEqual(self.vm.converse("What do you remember about my cat?", 30),
                         "You told me: my cat is called Marzipan.")
        self.start_ap(ssid, "WPA-PSK", 0)
        self.check_connection(ssid, "WPA2-PSK", cli=cli)

        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(180), "the guest did not power off")
        self.vm.stop()
        self.boot_lab(managed=True, reconnect=True)
        self.start_ap(ssid, "WPA-PSK", 0)
        self.check_connection(ssid, "WPA2-PSK", cli=cli)
        self.assertEqual(self.mode_and_owner(MANAGED_PROFILE), "600:0")
        self.assertEqual(self.wait(f"python3 {RUN}/check.py {DNS_NAME}"), MARKER)

        self.stop_ap()
        sae_ssid = "being-owner-sae"
        self.start_ap(sae_ssid, "SAE", 2, password=SAE_PASSPHRASE)
        self.configure_managed(sae_ssid, "wpa3", SAE_PASSPHRASE)
        self.check_connection(sae_ssid, "SAE", cli=cli)
        self.assertNotIn(SAE_PASSPHRASE, self.sh(f"{WIFI_COMMAND} status"))

        self.sh(f"{WIFI_COMMAND} forget")
        self.sh(f"test ! -e {MANAGED_PROFILE} && test ! -e {MANAGED_NETWORK} && test ! -d {MANAGED_CONTROL}")
        self.assertIn("Wi-Fi is unconfigured.", self.sh(f"{WIFI_COMMAND} status").splitlines())
        self.assertEqual(self.sh(service_state), "inactive")
        self.wait(f"iw dev {STATION} link", lambda text: text == "Not connected.")
        self.wait(f"ip -4 -o addr show dev {STATION}", lambda text: not text)
        status, _ = self.vm.run(f"python3 {RUN}/check.py", 12)
        self.assertNotEqual(status, 0, "the old AP remained reachable after forgetting its profile")
        self.assertEqual(self.vm.converse("What do you remember about my cat?", 30),
                         "You told me: my cat is called Marzipan.")
