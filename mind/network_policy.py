"""Owner-selected system connectivity and requested skill permissions."""

import ipaddress
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .storage import atomic_write, private_directory, sync_directory

LOCAL_ONLY = "local-only"
PAIRED_LAN = "paired-lan"
INTERNET = "internet"
MODES = (LOCAL_ONLY, PAIRED_LAN, INTERNET)
POLICY_PATH = Path("network/policy.json")
LEGACY_VERSION = 1
VERSION = 2
MAX_POLICY_BYTES = 1024


def validate_time_server(value):
    message = "Use a numeric unicast IPv4 or IPv6 time server address."
    if type(value) is not str or "%" in value:
        raise ValueError(message)
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(message) from None
    if (address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local
            or (address.version == 4 and (address.is_reserved or int(address) < 1 << 24))
            or (address.version == 6 and address.ipv4_mapped is not None)):
        raise ValueError(message)
    return str(address)


@dataclass(frozen=True)
class NetworkPolicy:
    mode: str = LOCAL_ONLY
    web_lookups: bool = False
    system_network: bool = False
    time_server: str | None = None

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError("Choose local-only, paired-lan, or internet.")
        if type(self.web_lookups) is not bool:
            raise ValueError("The web lookups choice must be a Boolean.")
        if type(self.system_network) is not bool:
            raise ValueError("The system network choice must be a Boolean.")
        if self.time_server is not None:
            if self.effective_mode != INTERNET:
                raise ValueError("Network time requires the owner to enable Internet mode.")
            object.__setattr__(self, "time_server", validate_time_server(self.time_server))

    @property
    def effective_mode(self):
        """Legacy skill permission never enables system transport."""
        return self.mode if self.system_network else LOCAL_ONLY

    @property
    def permits_web_lookup(self):
        """Public lookups need both the network mode and feature permission."""
        return self.mode == INTERNET and self.web_lookups


def unique_object(fields):
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("Repeated field in network policy.")
    return result


def load_policy(mind_dir) -> NetworkPolicy:
    """Read the current private policy, defaulting to local-only on any error."""
    path = Path(mind_dir) / POLICY_PATH
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return NetworkPolicy()
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    return NetworkPolicy()
                raw = source.read(MAX_POLICY_BYTES + 1)
        finally:
            os.close(directory)
        if len(raw) <= MAX_POLICY_BYTES:
            data = json.loads(raw, object_pairs_hook=unique_object)
            legacy_fields = {"version", "mode", "web_lookups"}
            if isinstance(data, dict) and type(data.get("version")) is int:
                if data["version"] == LEGACY_VERSION and data.keys() == legacy_fields:
                    return NetworkPolicy(data["mode"], data["web_lookups"])
                if data["version"] == VERSION and data.keys() == legacy_fields | {"system_network", "time_server"}:
                    return NetworkPolicy(data["mode"], data["web_lookups"], data["system_network"], data["time_server"])
    except (OSError, ValueError, RecursionError):
        pass
    return NetworkPolicy()


def web_lookup_ready(mind_dir, *, policy=None):
    """The skill's permission and, on the appliance, the active transport."""
    directory = Path(mind_dir)
    if policy is None:
        policy = load_policy(directory)
    if not policy.permits_web_lookup:
        return False
    if directory == Path("/mind"):
        from .network_enforce import runtime_state

        return policy.effective_mode == INTERNET and runtime_state()["mode"] == INTERNET
    return True


def save_policy(mind_dir, policy: NetworkPolicy) -> None:
    """Atomically save the owner's choice into an existing mind directory."""
    if not isinstance(policy, NetworkPolicy):
        raise TypeError("Expected a NetworkPolicy.")
    path = Path(mind_dir) / POLICY_PATH
    existed = path.parent.is_dir()
    private_directory(path.parent)
    if not existed:
        sync_directory(path.parent.parent)
    data = {"version": LEGACY_VERSION, "mode": policy.mode, "web_lookups": policy.web_lookups}
    if policy.system_network or policy.time_server is not None:
        data.update(version=VERSION, system_network=policy.system_network, time_server=policy.time_server)
    atomic_write(path, json.dumps(data, indent=2) + "\n")
