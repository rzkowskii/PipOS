"""Owner-controlled Wi-Fi on the appliance's writable data partition."""

import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import warnings
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .install_disks import node_number, unescape_proc
from .network_policy import INTERNET, LOCAL_ONLY, PAIRED_LAN
from .storage import atomic_write, private_directory, sync_directory


PROFILE = Path("/mind/network/wifi.json")
ETHERNET = Path("network/ethernet.json")  # relative to the mind directory
RUNTIME = Path("/run/being-wifi")
NETWORKD_FILE = Path("/run/systemd/network/00-being-wifi.network")
NETWORK_MODE = Path("/run/being-network/mode")
SYS_NET = Path("/sys/class/net")
MOUNTINFO = Path("/proc/self/mountinfo")
SERVICE = "being-wifi.service"
INTERFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
HEX = re.compile(r"[0-9a-fA-F]+")
# WPA supplicant 2.12 reads network options in a 2,000-byte line buffer.
SAE_PASSWORD_MAX = 990


@dataclass(frozen=True)
class Profile:
    interface: str
    ssid: str
    security: str
    key: str = field(repr=False)

    def __post_init__(self):
        if any(not isinstance(value, str) for value in (self.interface, self.ssid, self.security, self.key)):
            raise ValueError("Invalid Wi-Fi profile fields.")
        if not INTERFACE.fullmatch(self.interface):
            raise ValueError("Invalid Wi-Fi interface name.")
        if not 1 <= len(self.ssid.encode("utf-8")) <= 32:
            raise ValueError("A Wi-Fi network name must use 1 to 32 bytes.")
        if not HEX.fullmatch(self.key) or len(self.key) % 2:
            raise ValueError("Invalid Wi-Fi network key.")
        if self.security == "wpa2" and len(self.key) == 64:
            return
        if self.security == "wpa3" and len(self.key) <= 2 * SAE_PASSWORD_MAX and b"\0" not in bytes.fromhex(self.key):
            return
        raise ValueError("Invalid key or security mode for the Wi-Fi profile.")

    @classmethod
    def from_password(cls, interface, ssid, security, password):
        raw = password.encode("utf-8")
        if security == "wpa2":
            if len(raw) == 64 and HEX.fullmatch(password):
                key = password.lower()
            elif 8 <= len(raw) <= 63 and b"\0" not in raw:
                key = hashlib.pbkdf2_hmac("sha1", raw, ssid.encode("utf-8"), 4096, 32).hex()
            else:
                raise ValueError("Use a WPA2 password of 8 to 63 bytes or a 64-digit hexadecimal key.")
        else:
            key = raw.hex()
        return cls(interface, ssid, security, key)

    def supplicant_config(self):
        lines = [
            f"ctrl_interface={RUNTIME}",
            "update_config=0",
            "network={",
            f"    ssid={self.ssid.encode('utf-8').hex()}",
            "    proto=RSN",
            "    pairwise=CCMP",
        ]
        if self.security == "wpa2":
            lines.extend(("    key_mgmt=WPA-PSK", f"    psk={self.key}"))
        else:
            lines.extend(("    key_mgmt=SAE", "    ieee80211w=2", "    sae_pwe=2", f"    sae_password={self.key}"))
        return "\n".join((*lines, "}", ""))


@contextmanager
def owner_change(profile_path=None):
    directory = (profile_path or PROFILE).parent
    existed = directory.is_dir()
    private_directory(directory)
    if not existed:
        sync_directory(directory.parent)
    descriptor = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def offline_mind_mount(mount):
    if sys.platform != "linux":
        raise ValueError("Offline provisioning requires Linux.")
    mount = mount.resolve(strict=True)
    target_entries = []
    live_devices = set()
    for line in MOUNTINFO.read_text().splitlines():
        before, _, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if len(fields) < 6 or len(filesystem) < 3:
            continue
        where = unescape_proc(fields[4])
        if where == "/":
            live_devices.add(fields[2])
        if where == str(mount):
            target_entries.append((fields, filesystem))
    error = "Mount the PipOS mind partition as a separate read-write ext4 filesystem."
    if len(target_entries) != 1 or not live_devices:
        raise ValueError(error)
    fields, filesystem = target_entries[0]
    number, root, options = fields[2], fields[3], fields[5].split(",")
    kind, source, super_options = filesystem[:3]
    source = unescape_proc(source)
    if (number in live_devices or root != "/" or kind != "ext4" or "rw" not in options
            or "rw" not in super_options.split(",") or not source.startswith("/dev/")
            or node_number(source) != number):
        raise ValueError(error)
    result = subprocess.run(["blkid", "-p", "-s", "LABEL", "-o", "value", "--", source],
                            capture_output=True, text=True, timeout=5)
    if result.returncode or result.stdout.removesuffix("\n") != "mind":
        raise ValueError("The selected filesystem does not have the PipOS 'mind' label.")
    return mount


def load_profile():
    try:
        fd = os.open(PROFILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as source:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError(f"Expected a private profile file at {PROFILE}.")
        try:
            return Profile(**json.load(source))
        except (TypeError, ValueError):
            raise ValueError("Unreadable Wi-Fi profile.") from None


def wireless(interface):
    return (SYS_NET / interface / "phy80211").is_dir()


def effective_mode():
    try:
        mode = NETWORK_MODE.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return LOCAL_ONLY
    return mode if mode in (PAIRED_LAN, INTERNET) else LOCAL_ONLY


def networkd_config(interface, mode, *, ethernet=False):
    if not INTERFACE.fullmatch(interface):
        raise ValueError("Invalid network interface name.")
    if mode not in (PAIRED_LAN, INTERNET):
        raise ValueError("Network policy has not authorized a link.")
    internet = mode == INTERNET
    lines = ["[Match]", f"Name={interface}"]
    if ethernet:
        lines.append("Type=ether")
    lines.extend((
        "", "[Network]", f"DHCP={'yes' if internet else 'ipv4'}",
        f"LinkLocalAddressing={'ipv6' if internet else 'no'}",
        f"IPv6AcceptRA={'yes' if internet else 'no'}", "LLMNR=no", "MulticastDNS=no",
        "LLDP=no", "EmitLLDP=no",
        "", "[DHCPv4]", f"UseDNS={'yes' if internet else 'no'}", "UseNTP=no", "UseDomains=no",
    ))
    if internet:
        lines.extend(("", "[DHCPv6]", "UseDNS=yes", "UseNTP=no", "UseDomains=no"))
    return "\n".join((*lines, ""))


def ethernet_config():
    """Every wired link, DHCP, with the same privacy choices as Internet Wi-Fi."""
    lines = ["[Match]", "Type=ether", "",
             "[Network]", "DHCP=yes", "LinkLocalAddressing=ipv6", "IPv6AcceptRA=yes",
             "IPv6PrivacyExtensions=yes", "LLMNR=no", "MulticastDNS=no", "LLDP=no", "EmitLLDP=no",
             "", "[DHCPv4]", "UseDNS=yes", "UseNTP=no", "UseDomains=no", "SendHostname=no",
             "", "[DHCPv6]", "UseDNS=yes", "UseNTP=no", "UseDomains=no", ""]
    return "\n".join(lines)


def load_ethernet(mind=Path("/mind")):
    """Whether the owner switched wired Ethernet on. A missing file means off."""
    path = Path(mind) / ETHERNET
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise PermissionError(f"Expected a private Ethernet setting at {path}.")
        raw = source.read(257)
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("Unreadable Ethernet setting.") from None
    if not isinstance(value, dict) or set(value) != {"version", "enabled"} or value["version"] != 1 \
            or type(value["enabled"]) is not bool:
        raise ValueError("Unreadable Ethernet setting.")
    return value["enabled"]


def save_ethernet(mind, enabled):
    path = Path(mind) / ETHERNET
    with owner_change(path):
        atomic_write(path, json.dumps({"version": 1, "enabled": bool(enabled)}) + "\n")


def wireless_interfaces(sys_net=None):
    base = SYS_NET if sys_net is None else sys_net
    try:
        names = sorted(path.name for path in base.iterdir())
    except OSError:
        return []
    return [name for name in names if INTERFACE.fullmatch(name) and (base / name / "phy80211").is_dir()]


def parse_scan(text):
    """Networks from `iw dev X scan`, strongest first, one entry per name.

    Hidden networks (no name) are left out; the owner types those by hand.
    """
    networks = {}
    current = None

    def keep(entry):
        if entry and entry.get("ssid") and entry["security"] in ("wpa2", "wpa3"):
            old = networks.get(entry["ssid"])
            if old is None or entry["signal"] > old["signal"]:
                networks[entry["ssid"]] = entry

    for raw in text.splitlines():
        line = raw.strip()
        if raw.startswith("BSS "):
            keep(current)
            current = {"ssid": "", "signal": -100.0, "security": "open", "suites": set()}
        elif current is None:
            continue
        elif line.startswith("signal:"):
            try:
                current["signal"] = float(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("SSID:"):
            name = line[5:].strip()
            # iw escapes unprintable bytes as \xNN; such names are not offered.
            current["ssid"] = name if name.isprintable() and "\\x" not in name and \
                len(name.encode("utf-8")) <= 32 else ""
        elif line.startswith("RSN:"):
            current["security"] = "wpa2"
        elif "Authentication suites:" in line and current["security"] == "wpa2":
            suites = set(line.split(":", 1)[1].split())
            current["security"] = "wpa2" if "PSK" in suites else "wpa3" if "SAE" in suites else "other"
        elif line.startswith("WPA:") and current["security"] == "open":
            current["security"] = "other"  # WPA1 only: not supported
    keep(current)
    return sorted(({key: value for key, value in entry.items() if key != "suites"} for entry in networks.values()),
                  key=lambda entry: -entry["signal"])


def link_up(interface, sys_net=None):
    """Whether the kernel has the link administratively up."""
    base = SYS_NET if sys_net is None else sys_net
    try:
        return bool(int((base / interface / "flags").read_text(encoding="ascii"), 16) & 1)
    except (OSError, ValueError):
        return False


def scan(interface, *, run=subprocess.run):
    """Ask the radio which networks are near. Only on the owner's request.

    The link is raised for the scan and lowered again unless it was already
    up in a mode that allows it. A first join in Internet mode finds the link
    down, because nothing manages it yet. No address is requested and nothing
    is joined.
    """
    if not INTERFACE.fullmatch(interface):
        raise ValueError("Invalid Wi-Fi interface name.")
    raised = False
    try:
        if effective_mode() == LOCAL_ONLY or not link_up(interface):
            run(["ip", "link", "set", "dev", interface, "up"], check=True, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raised = True
        result = run(["iw", "dev", interface, "scan"], capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            raise ValueError("The Wi-Fi radio could not scan. Its firmware may be missing.")
        return parse_scan(result.stdout)
    finally:
        if raised:
            run(["ip", "link", "set", "dev", interface, "down"], check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def serve():
    profile = load_profile()
    if profile is None:
        return
    network = networkd_config(profile.interface, effective_mode())
    if not wireless(profile.interface):
        raise ValueError(f"Waiting for Wi-Fi interface {profile.interface}.")
    private_directory(RUNTIME)
    atomic_write(RUNTIME / "interface", profile.interface)
    supplicant_file = RUNTIME / "wpa_supplicant.conf"
    atomic_write(supplicant_file, profile.supplicant_config())
    NETWORKD_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(NETWORKD_FILE, network, mode=0o644)
    subprocess.run(["networkctl", "reload"], check=True)
    command = ["wpa_supplicant", "-D", "nl80211", "-i", profile.interface, "-c", str(supplicant_file)]
    os.execvp(command[0], command)


def cleanup():
    marker = RUNTIME / "interface"
    if not marker.exists():
        return
    interface = marker.read_text(encoding="utf-8")
    if INTERFACE.fullmatch(interface) and wireless(interface):
        subprocess.run(["networkctl", "down", interface], check=False)
    NETWORKD_FILE.unlink(missing_ok=True)
    if subprocess.run(["systemctl", "is-active", "--quiet", "systemd-networkd.service"], check=False).returncode == 0:
        subprocess.run(["networkctl", "reload"], check=True)
    (RUNTIME / "wpa_supplicant.conf").unlink(missing_ok=True)
    marker.unlink(missing_ok=True)


def status():
    profile = load_profile()
    mode = effective_mode()
    print(f"Network mode={mode}")
    if profile is None:
        print("Wi-Fi is unconfigured.")
        return
    print(f"Wi-Fi profile for {json.dumps(profile.ssid)} on {profile.interface} ({profile.security.upper()}).")
    if mode == LOCAL_ONLY:
        print("Wi-Fi is offline.")
        return
    result = subprocess.run(
        ["wpa_cli", "-p", str(RUNTIME), "-i", profile.interface, "status"],
        capture_output=True, text=True, check=False,
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    for item in ("wpa_state", "key_mgmt", "ip_address"):
        if item in fields:
            print(f"{item}={fields[item]}")
    if "wpa_state" not in fields:
        print("Wi-Fi is disconnected.")


def password_from_prompt(from_stdin):
    if from_stdin:
        return sys.stdin.readline(SAE_PASSWORD_MAX + 2).removesuffix("\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Wi-Fi password: ")
        except getpass.GetPassWarning:
            raise ValueError("Use an interactive terminal or --password-stdin.") from None


def main():
    parser = argparse.ArgumentParser(prog="being-wifi")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="save a profile for an authorized network connection")
    configure.add_argument("interface")
    configure.add_argument("ssid")
    configure.add_argument("--security", choices=("wpa2", "wpa3"), default="wpa2")
    configure.add_argument("--password-stdin", action="store_true")
    configure.add_argument("--offline-mind", type=Path,
                           help="save to an installed mind partition mounted by a Linux live system")
    commands.add_parser("status", help="show the saved network and connection state")
    commands.add_parser("forget", help="disconnect and remove the saved network")
    wired = commands.add_parser("ethernet", help="switch owner wired Ethernet (used in Internet mode)")
    wired.add_argument("action", choices=("on", "off", "status"))
    survey = commands.add_parser("scan", help="list nearby Wi-Fi networks as JSON")
    survey.add_argument("interface", nargs="?")
    commands.add_parser("serve")
    commands.add_parser("cleanup")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root.")
    try:
        if args.command == "configure":
            profile_path = (offline_mind_mount(args.offline_mind) / PROFILE.relative_to("/mind")
                            if args.offline_mind else PROFILE)
            profile = Profile.from_password(args.interface, args.ssid, args.security,
                                            password_from_prompt(args.password_stdin))
            with owner_change(profile_path):
                atomic_write(profile_path, json.dumps(asdict(profile)) + "\n")
                if args.offline_mind is None:
                    subprocess.run(["systemctl", "restart", SERVICE], check=True)
        elif args.command == "forget":
            with owner_change():
                PROFILE.unlink(missing_ok=True)
                sync_directory(PROFILE.parent)
                subprocess.run(["systemctl", "stop", SERVICE], check=True)
        elif args.command == "ethernet":
            if args.action != "status":
                save_ethernet(Path("/mind"), args.action == "on")
                subprocess.run(["systemctl", "restart", "being-network-reconcile.service"], check=True, timeout=100)
            print("Wired Ethernet " + ("on" if load_ethernet() else "off")
                  + (" (used in Internet mode)." if effective_mode() != INTERNET else "."))
        elif args.command == "scan":
            names = [args.interface] if args.interface else wireless_interfaces()
            if not names:
                raise ValueError("No Wi-Fi interface. The radio or its firmware is missing.")
            print(json.dumps({"interface": names[0], "networks": scan(names[0])}))
        elif args.command == "serve":
            serve()
        elif args.command == "cleanup":
            cleanup()
        else:
            status()
    except (EOFError, OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"being-wifi: {error}")


if __name__ == "__main__":
    main()
