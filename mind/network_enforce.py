"""Apply the owner's device-wide IP and network service policy."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from .network_policy import (
    INTERNET, LOCAL_ONLY, MODES, PAIRED_LAN, POLICY_PATH,
    NetworkPolicy, save_policy, unique_object, validate_time_server,
)
from .policy import network_policy as load_policy
from .storage import atomic_write


MIND = Path("/mind")
RUN = Path("/run/being-network")
SYS_NET = Path("/sys/class/net")
TIME_CONF = Path("/run/systemd/timesyncd.conf.d/99-being-network.conf")
NETWORKD = "systemd-networkd.service"
NETWORKD_SOCKETS = ("systemd-networkd.socket", "systemd-networkd-varlink.socket")
RESOLVED = "systemd-resolved.service"
TIMESYNC = "systemd-timesyncd.service"
WIFI = "being-wifi.service"
OBSERVE = "being-lab-observe.service"
DEV = "being-lab-dev.service"
RECONCILE = "being-network-reconcile.service"
NETWORK_UNITS = (DEV, OBSERVE, WIFI, TIMESYNC, RESOLVED, NETWORKD, *NETWORKD_SOCKETS)
# Where systemd looks for unit files. The laptop image ships without the lab
# maintenance units, so every unit list is filtered through these first.
UNIT_DIRS = (Path("/etc/systemd/system"), Path("/run/systemd/system"), Path("/usr/lib/systemd/system"),
             Path("/lib/systemd/system"))
ETHERNET_FILE = Path("/run/systemd/network/02-being-ethernet.network")
OFFLINE = {"mode": LOCAL_ONLY, "effective_time_server": None, "paired": False}
IFNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
PRIVATE4 = "{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }"
DHCP4_TARGETS = "{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 255.255.255.255 }"

# The cable development link (development Try sticks only, docs/LAB.md): a
# fixed link-local address on the first wired port, reachable only from the
# link-local networks and only on the SSH port. It exists only on an image
# that carries the cable key, on a Try boot, without the being.cable=0 kill
# switch, and once `being-network cable-up` has written its state file.
CABLE_FILE = "cable.json"
CABLE_KEYS = Path("/usr/share/being/cable/authorized_keys")
CMDLINE = Path("/proc/cmdline")
CABLE_NETWORK = Path("/run/systemd/network/00-being-cable.network")
IPV6_CONF = Path("/proc/sys/net/ipv6/conf")
CABLE_ADDRESS4, CABLE_PREFIX4 = "169.254.77.1", 16
CABLE_ADDRESS6, CABLE_PREFIX6 = "fe80::77:1", 64
CABLE_PEERS4 = "169.254.0.0/16"
CABLE_PEERS6 = "fe80::/10"
CABLE_PORT = 22
ARPHRD_ETHER = "1"
IP = "/usr/bin/ip"
ARPING = "/usr/sbin/arping"
UDEVADM = "/usr/bin/udevadm"


def cable_rules(interface):
    """(incoming, outgoing) nftables rules for the cable link on `interface`."""
    if not isinstance(interface, str) or not IFNAME.fullmatch(interface):
        raise ValueError("Invalid cable interface name.")
    nd = "ip6 hoplimit 255 icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } accept"
    four = f"ip saddr {CABLE_PEERS4} ip daddr {CABLE_ADDRESS4}"
    six = f"ip6 saddr {CABLE_PEERS6} ip6 daddr {CABLE_ADDRESS6}"
    back4 = f"ip saddr {CABLE_ADDRESS4} ip daddr {CABLE_PEERS4}"
    back6 = f"ip6 saddr {CABLE_ADDRESS6} ip6 daddr {CABLE_PEERS6}"
    incoming = [
        f'iifname "{interface}" {four} tcp dport {CABLE_PORT} accept',
        f'iifname "{interface}" {six} tcp dport {CABLE_PORT} accept',
        f'iifname "{interface}" {nd}',
        f'iifname "{interface}" {four} icmp type echo-request accept',
        f'iifname "{interface}" {six} icmpv6 type echo-request accept',
    ]
    outgoing = [
        f'oifname "{interface}" {back4} tcp sport {CABLE_PORT} ct state established accept',
        f'oifname "{interface}" {back6} tcp sport {CABLE_PORT} ct state established accept',
        f'oifname "{interface}" {nd}',
        f'oifname "{interface}" {back4} icmp type echo-reply accept',
        f'oifname "{interface}" {back6} icmpv6 type echo-reply accept',
    ]
    return incoming, outgoing


def render_rules(mode, pair=None, time_server=None, cable=None):
    """Replace only the PipOS table in one nftables transaction.

    `cable` ({"interface": name} from load_cable) adds the cable development
    link in every mode; without it the rules are exactly the release rules.
    """
    if mode not in MODES:
        raise ValueError("Unknown effective network mode.")
    incoming = ['iifname "lo" accept']
    outgoing = ['oifname "lo" accept']
    if cable is not None:
        cable_in, cable_out = cable_rules(cable["interface"])
        incoming += cable_in
        outgoing += cable_out
    if mode != LOCAL_ONLY:
        if mode == PAIRED_LAN:
            incoming += [f"ip saddr {PRIVATE4} udp sport 67 udp dport 68 accept"]
            outgoing += [f"ip daddr {DHCP4_TARGETS} udp sport 68 udp dport 67 accept"]
        else:
            incoming += [
                "ct state established,related accept",
                "meta nfproto ipv4 udp sport 67 udp dport 68 accept",
                "meta nfproto ipv6 udp sport 547 udp dport 546 accept",
                "ip6 hoplimit 255 icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept",
                "icmpv6 type mld-listener-query accept",
            ]
            if time_server:
                address = ipaddress.ip_address(validate_time_server(time_server))
                family = "ip" if address.version == 4 else "ip6"
                outgoing += [f"{family} daddr {address} udp dport 123 accept"]
            outgoing += ["udp dport 123 counter name dropped drop", "tcp dport 123 counter name dropped drop", "accept"]
        if pair:
            appliance = ipaddress.IPv4Address(pair["address"])
            controller = ipaddress.IPv4Address(pair["controller"])
            ports = "{ 2222, 2223 }" if pair["dev_enabled"] else "2222"
            incoming += [
                f"ip saddr {controller} ip daddr {appliance} tcp dport {ports} ct state new,established accept",
            ]
            if mode == PAIRED_LAN:
                outgoing += [
                    f"ip saddr {appliance} ip daddr {controller} tcp sport {ports} ct state established accept",
                ]

    counted_drop = "counter name dropped drop"
    incoming.append(counted_drop)
    if outgoing[-1] != "accept":
        outgoing.append(counted_drop)

    return (
        "destroy table inet being_network\n"
        "table inet being_network {\n"
        "  counter dropped { }\n"
        "  chain input {\n"
        "    type filter hook input priority filter; policy drop;\n"
        + "".join(f"    {rule}\n" for rule in incoming)
        + "  }\n"
        "  chain output {\n"
        "    type filter hook output priority filter; policy drop;\n"
        + "".join(f"    {rule}\n" for rule in outgoing)
        + "  }\n"
        "  chain forward {\n"
        "    type filter hook forward priority filter; policy drop;\n"
        f"    {counted_drop}\n"
        "  }\n"
        "}\n"
    )


def dropped_packets(run=subprocess.run):
    """Only the aggregate since this policy table was loaded leaves nft."""
    try:
        result = run(["/usr/sbin/nft", "-j", "list", "counter", "inet", "being_network", "dropped"],
                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4, check=True)
        entries = json.loads(result.stdout)["nftables"]
        for entry in entries:
            counter = entry.get("counter", {})
            packets = counter.get("packets")
            if (counter.get("name") == "dropped" and type(packets) is int and 0 <= packets < 2**64):
                return packets
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def install_rules(mode, pair=None, time_server=None, cable=None):
    subprocess.run(
        ["/usr/sbin/nft", "--file", "-"], input=render_rules(mode, pair, time_server, cable),
        text=True, timeout=8, check=True,
    )


def write_state(state):
    RUN.mkdir(mode=0o755, exist_ok=True)
    mode = state["mode"]
    activation = RUN / "mode"
    if mode == LOCAL_ONLY:
        activation.unlink(missing_ok=True)
    atomic_write(RUN / "state.json", json.dumps(state) + "\n", mode=0o644)
    if mode != LOCAL_ONLY:
        atomic_write(activation, mode + "\n", mode=0o644)


def runtime_state():
    try:
        state = json.loads((RUN / "state.json").read_text(encoding="utf-8"))
        mode, server, paired = state["mode"], state["effective_time_server"], state["paired"]
        if (type(paired) is bool and mode in (PAIRED_LAN, INTERNET)
                and (RUN / "mode").read_text(encoding="ascii").strip() == mode):
            if server is None or mode == INTERNET and validate_time_server(server) == server:
                return {"mode": mode, "effective_time_server": server, "paired": paired}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        pass
    return dict(OFFLINE)


def write_time_config(server):
    TIME_CONF.parent.mkdir(parents=True, exist_ok=True)
    chosen = f"NTP={server}\n" if server else ""
    atomic_write(TIME_CONF, f"[Time]\nNTP=\n{chosen}FallbackNTP=\n", mode=0o644)


def lower_external_links(keep=None):
    """Take every link but loopback (and `keep`, the cable link) down."""
    failure = None
    for path in sorted(SYS_NET.glob("*")):
        if path.name not in ("lo", keep) and IFNAME.fullmatch(path.name):
            # Links can disappear during shutdown or USB removal.
            if (flags := path / "flags").exists():
                try:
                    up = int(flags.read_text(encoding="ascii"), 16) & 1
                except (OSError, ValueError):
                    continue
                if up:
                    try:
                        subprocess.run(["/usr/bin/ip", "link", "set", "dev", path.name, "down"],
                                       timeout=4, check=True)
                    except (OSError, subprocess.SubprocessError) as error:
                        if path.exists():
                            failure = error
    if failure:
        raise failure


def cable_enabled():
    """Whether this boot may run the cable link: the key is in the image, it
    is a Try boot, and the being.cable=0 kill switch is absent."""
    if not CABLE_KEYS.is_file():
        return False  # every release image: no cable key, nothing else is read
    try:
        words = CMDLINE.read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return False
    return "being.try=1" in words and "being.cable=0" not in words


def load_cable():
    """The cable link this boot set up ({"interface": name}), or None.

    The state file must be a regular file owned by this user that no one else
    can change, holding exactly the schema cable-up writes; anything else (and
    any boot the cable may not run on) reads as no cable at all.
    """
    if not cable_enabled():
        return None
    try:
        fd = os.open(RUN / CABLE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return None
            raw = source.read(4097)
        if len(raw) > 4096:
            return None
        state = json.loads(raw, object_pairs_hook=unique_object)
        if (type(state) is not dict or set(state) != {"interface", "ipv4", "ipv6"}
                or state["ipv4"] != f"{CABLE_ADDRESS4}/{CABLE_PREFIX4}"
                or state["ipv6"] != f"{CABLE_ADDRESS6}/{CABLE_PREFIX6}"
                or not isinstance(state["interface"], str) or not IFNAME.fullmatch(state["interface"])):
            return None
        return {"interface": state["interface"]}
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def lockdown():
    # Disable service activation even if nft itself reports a failure. The
    # cable link, when this boot has one, stays up and reachable.
    cable = load_cable()
    try:
        (RUN / "mode").unlink(missing_ok=True)
    finally:
        try:
            install_rules(LOCAL_ONLY, None, None, cable)
        finally:
            lower_external_links(keep=cable["interface"] if cable else None)
    write_state(OFFLINE)


def installed(*units, unit_dirs=None):
    """The given units that have a unit file on this image, in order."""
    dirs = UNIT_DIRS if unit_dirs is None else unit_dirs
    return tuple(unit for unit in units if any((directory / unit).exists() for directory in dirs))


def configure_owner_ethernet(mind, mode):
    """Wired Ethernet the owner switched on in Care: DHCP in Internet mode, no pairing."""
    from .wifi import ethernet_config, load_ethernet

    try:
        wanted = load_ethernet(mind)
    except (OSError, ValueError):
        wanted = False  # an unreadable switch never opens a link
    if mode != INTERNET or not wanted:
        ETHERNET_FILE.unlink(missing_ok=True)
        return False
    ETHERNET_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(ETHERNET_FILE, ethernet_config(), mode=0o644)
    return True


def apply(mind=MIND):
    from .lab_access import configure_ethernet, load_pair

    policy = load_policy(mind)
    mode = policy.effective_mode
    pair = load_pair(mind) if mode != LOCAL_ONLY else None
    server = policy.time_server if mode == INTERNET else None
    configure_ethernet(pair, mode)
    configure_owner_ethernet(mind, mode)
    write_time_config(server)
    install_rules(mode, pair, server, load_cable())
    write_state({"mode": mode, "effective_time_server": server, "paired": pair is not None})


def systemctl(action, *units, timeout=35, wait=True):
    if not units:
        return
    options = [] if wait else ["--no-block"]
    subprocess.run(["/usr/bin/systemctl", *options, action, *units], timeout=timeout, check=True)


def reconcile(mind=MIND):
    # Only units this image ships: the laptop image has no lab maintenance
    # units, and systemctl stop or start of a missing unit fails the change.
    present = installed(*NETWORK_UNITS)
    try:
        lockdown()
        systemctl("stop", *present)
        apply(mind)
        state = runtime_state()
        if state["mode"] == LOCAL_ONLY:
            return
        units = [NETWORKD, WIFI]
        if state["paired"]:
            units.append(OBSERVE)
        if state["mode"] == INTERNET:
            units.append(RESOLVED)
        if state["effective_time_server"]:
            units.append(TIMESYNC)
        systemctl("start", *(unit for unit in units if unit in present))
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            lockdown()
        finally:
            systemctl("stop", *present, wait=False)
        raise


def allows(action, state):
    if action == "require-link":
        return state["mode"] != LOCAL_ONLY
    if action == "require-pair":
        return state["mode"] != LOCAL_ONLY and state["paired"]
    if action == "require-internet":
        return state["mode"] == INTERNET
    return state["mode"] == INTERNET and bool(state["effective_time_server"])


def wired_interfaces():
    """Physical Ethernet ports by name: a device behind them, link type
    Ethernet, and no wireless radio. Never chosen by name pattern."""
    found = []
    for path in sorted(SYS_NET.glob("*")):
        if not IFNAME.fullmatch(path.name) or not (path / "device").exists():
            continue
        if (path / "wireless").exists() or (path / "phy80211").exists():
            continue
        try:
            if (path / "type").read_text(encoding="ascii").strip() != ARPHRD_ETHER:
                continue
        except OSError:
            continue
        found.append(path.name)
    return found


def wait_for_wired(seconds, sleep=time.sleep, clock=time.monotonic):
    """The first wired port, waiting up to `seconds` for its driver; or None."""
    deadline = clock() + seconds
    while True:
        ports = wired_interfaces()
        if ports:
            return ports[0]
        if clock() >= deadline:
            return None
        sleep(0.5)


def link_addresses(interface):
    """The addresses on `interface`, as ip -o addr shows them (address/prefix)."""
    result = subprocess.run([IP, "-o", "addr", "show", "dev", interface], capture_output=True, text=True,
                            timeout=4, check=True)
    found = set()
    for line in result.stdout.splitlines():
        words = line.split()
        for family in ("inet", "inet6"):
            if family in words and words.index(family) + 1 < len(words):
                found.add(words[words.index(family) + 1])
    return found


def address_in_use(interface):
    """Whether another device on the cable already answers for Pip's address.

    BusyBox arping -D exits 0 when no one answers and 1 when someone does; a
    failure to probe at all is reported and does not stop the link."""
    try:
        result = subprocess.run([ARPING, "-D", "-I", interface, "-c", "2", CABLE_ADDRESS4], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"being-network: could not check {CABLE_ADDRESS4} for a duplicate ({error}).", file=sys.stderr)
        return False
    if result.returncode == 0:
        return False
    answers = re.search(r"Received (\d+) (?:response|repl)", result.stdout + result.stderr)
    if answers and int(answers[1]) > 0:
        return True
    print(f"being-network: could not check {CABLE_ADDRESS4} for a duplicate "
          f"({(result.stdout + result.stderr).strip()[-200:]}).", file=sys.stderr)
    return False


def cable_network_file(interface):
    """systemd-networkd leaves the cable port alone, whatever the mode."""
    return ("# Written by being-network cable-up: the cable development link is set by hand.\n"
            f"[Match]\nName={interface}\n\n[Link]\nUnmanaged=yes\n")


def cable_up(mind=MIND, *, wait=10.0):
    """Bring the cable development link up and open it in the firewall."""
    if not cable_enabled():
        raise ValueError("Cable development is off on this boot: it needs the cable key, a Try boot "
                         "and no being.cable=0.")
    try:
        subprocess.run([UDEVADM, "settle", "--timeout=15"], timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # a port that is already there is still found below
    interface = wait_for_wired(wait)
    if interface is None:
        raise ValueError("No wired network port was found, so the cable link stays off.")
    CABLE_NETWORK.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(CABLE_NETWORK, cable_network_file(interface), mode=0o644)
    # No address of its own choosing and no router advertisements: only the
    # fixed link-local address below.
    conf = IPV6_CONF / interface
    for name, value in (("disable_ipv6", "0"), ("addr_gen_mode", "1"), ("accept_ra", "0"), ("autoconf", "0")):
        (conf / name).write_text(value + "\n", encoding="ascii")
    subprocess.run([IP, "link", "set", "dev", interface, "up"], timeout=4, check=True)
    present = link_addresses(interface)
    if f"{CABLE_ADDRESS4}/{CABLE_PREFIX4}" not in present:
        if address_in_use(interface):
            raise ValueError(f"Another device on the cable already uses {CABLE_ADDRESS4}, so Pip will not take it.")
        subprocess.run([IP, "addr", "add", f"{CABLE_ADDRESS4}/{CABLE_PREFIX4}", "dev", interface],
                       timeout=4, check=True)
    if f"{CABLE_ADDRESS6}/{CABLE_PREFIX6}" not in present:
        subprocess.run([IP, "-6", "addr", "add", f"{CABLE_ADDRESS6}/{CABLE_PREFIX6}", "dev", interface],
                       timeout=4, check=True)
    RUN.mkdir(mode=0o755, exist_ok=True)
    atomic_write(RUN / CABLE_FILE, json.dumps({
        "interface": interface, "ipv4": f"{CABLE_ADDRESS4}/{CABLE_PREFIX4}",
        "ipv6": f"{CABLE_ADDRESS6}/{CABLE_PREFIX6}"}) + "\n", mode=0o644)
    apply(mind)
    return interface


def cable_line(cable):
    return f"Cable development on ({cable['interface']}, {CABLE_ADDRESS4}, {CABLE_ADDRESS6})"


def status(mind, structured):
    state = runtime_state()
    policy = load_policy(mind)
    cable = load_cable()
    if structured:
        report = {"requested_mode": policy.effective_mode, **state}
        if cable:
            report["cable"] = {"interface": cable["interface"], "ipv4": CABLE_ADDRESS4, "ipv6": CABLE_ADDRESS6}
        print(json.dumps(report))
    else:
        print(f"Requested network mode {policy.effective_mode}")
        print(f"Effective network mode {state['mode']}")
        print(f"Network time {state['effective_time_server'] or 'off'}")
        print("Paired maintenance " + ("authorized" if state["paired"] else "off"))
        if cable:
            print(cable_line(cable))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-network")
    commands = parser.add_subparsers(dest="command", required=True)
    set_mode = commands.add_parser("set", help="set the owner-authorized device network mode")
    set_mode.add_argument("mode", choices=MODES)
    set_mode.add_argument("--web-lookups", action="store_true")
    set_mode.add_argument("--time-server", metavar="NUMERIC_ADDRESS")
    set_mode.add_argument("--offline-mind", type=Path, help="save from live Linux to a mounted PipOS mind")
    view = commands.add_parser("status", help="report the requested and effective modes")
    view.add_argument("--json", action="store_true")
    for action in ("lockdown", "apply", "reconcile", "refresh-pair", "require-link", "require-pair",
                   "require-internet", "require-time"):
        commands.add_parser(action)
    commands.add_parser("cable-up", help="bring up the cable development link (development Try sticks only)")
    args = parser.parse_args(argv)
    if args.command.startswith("require-"):
        return 0 if allows(args.command, runtime_state()) else 1
    if os.geteuid() != 0:
        parser.error("Run the network owner control as root.")

    try:
        if args.command == "set":
            from .wifi import offline_mind_mount, owner_change

            mind = offline_mind_mount(args.offline_mind) if args.offline_mind else MIND
            if not mind.is_mount():
                raise ValueError("Mount the PipOS mind partition first.")
            policy = NetworkPolicy(args.mode, args.web_lookups, args.mode != LOCAL_ONLY, args.time_server)
            with owner_change(mind / POLICY_PATH):
                save_policy(mind, policy)
            if args.offline_mind:
                print(f"Saved {policy.effective_mode} for the next PipOS boot.")
            else:
                systemctl("restart", RECONCILE, timeout=100)
                status(mind, False)
        elif args.command == "status":
            status(MIND, args.json)
        elif args.command == "refresh-pair":
            systemctl("restart", RECONCILE, wait=False)
        elif args.command == "lockdown":
            lockdown()
        elif args.command == "apply":
            apply()
        elif args.command == "reconcile":
            reconcile()
        elif args.command == "cable-up":
            print(cable_line({"interface": cable_up()}))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"being-network: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
