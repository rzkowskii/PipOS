"""Apply the owner's device-wide IP and network service policy."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .network_policy import (
    INTERNET, LOCAL_ONLY, MODES, PAIRED_LAN, POLICY_PATH,
    NetworkPolicy, load_policy, save_policy, validate_time_server,
)
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
OFFLINE = {"mode": LOCAL_ONLY, "effective_time_server": None, "paired": False}
IFNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
PRIVATE4 = "{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }"
DHCP4_TARGETS = "{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 255.255.255.255 }"


def render_rules(mode, pair=None, time_server=None):
    """Replace only the PipOS table in one nftables transaction."""
    if mode not in MODES:
        raise ValueError("Unknown effective network mode.")
    incoming = ['iifname "lo" accept']
    outgoing = ['oifname "lo" accept']
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
            outgoing += ["udp dport 123 drop", "tcp dport 123 drop", "accept"]
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

    return (
        "destroy table inet being_network\n"
        "table inet being_network {\n"
        "  chain input {\n"
        "    type filter hook input priority filter; policy drop;\n"
        + "".join(f"    {rule}\n" for rule in incoming)
        + "  }\n"
        "  chain output {\n"
        "    type filter hook output priority filter; policy drop;\n"
        + "".join(f"    {rule}\n" for rule in outgoing)
        + "  }\n"
        "  chain forward { type filter hook forward priority filter; policy drop; }\n"
        "}\n"
    )


def install_rules(mode, pair=None, time_server=None):
    subprocess.run(
        ["/usr/sbin/nft", "--file", "-"], input=render_rules(mode, pair, time_server),
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


def lower_external_links():
    failure = None
    for path in sorted(SYS_NET.glob("*")):
        if path.name != "lo" and IFNAME.fullmatch(path.name):
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


def lockdown():
    # Disable service activation even if nft itself reports a failure.
    try:
        (RUN / "mode").unlink(missing_ok=True)
    finally:
        try:
            install_rules(LOCAL_ONLY)
        finally:
            lower_external_links()
    write_state(OFFLINE)


def apply(mind=MIND):
    from .lab_access import configure_ethernet, load_pair

    policy = load_policy(mind)
    mode = policy.effective_mode
    pair = load_pair(mind) if mode != LOCAL_ONLY else None
    server = policy.time_server if mode == INTERNET else None
    configure_ethernet(pair, mode)
    write_time_config(server)
    install_rules(mode, pair, server)
    write_state({"mode": mode, "effective_time_server": server, "paired": pair is not None})


def systemctl(action, *units, timeout=35, wait=True):
    options = [] if wait else ["--no-block"]
    subprocess.run(["/usr/bin/systemctl", *options, action, *units], timeout=timeout, check=True)


def reconcile(mind=MIND):
    try:
        lockdown()
        systemctl("stop", *NETWORK_UNITS)
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
        systemctl("start", *units)
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            lockdown()
        finally:
            systemctl("stop", *NETWORK_UNITS, wait=False)
        raise


def allows(action, state):
    if action == "require-link":
        return state["mode"] != LOCAL_ONLY
    if action == "require-pair":
        return state["mode"] != LOCAL_ONLY and state["paired"]
    if action == "require-internet":
        return state["mode"] == INTERNET
    return state["mode"] == INTERNET and bool(state["effective_time_server"])


def status(mind, structured):
    state = runtime_state()
    policy = load_policy(mind)
    if structured:
        print(json.dumps({"requested_mode": policy.effective_mode, **state}))
    else:
        print(f"Requested network mode {policy.effective_mode}")
        print(f"Effective network mode {state['mode']}")
        print(f"Network time {state['effective_time_server'] or 'off'}")
        print("Paired maintenance " + ("authorized" if state["paired"] else "off"))


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
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"being-network: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
