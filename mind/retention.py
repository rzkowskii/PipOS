"""Local owner policy for conversation retention."""

import argparse
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .profile import default_mind_dir
from .storage import atomic_write


POLICY_FILE = "memory-policy.json"
MODES = ("persistent", "explicit-only", "session-only")
SECONDS_PER_DAY = 86_400
MAX_EXPIRY_DAYS = 36_500


@dataclass(frozen=True)
class MemoryPolicy:
    mode: str = "persistent"
    expiry_days: int | None = None

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError("Choose persistent, explicit-only, or session-only.")
        if self.expiry_days is not None and (self.mode != "persistent"
                or type(self.expiry_days) is not int or not 1 <= self.expiry_days <= MAX_EXPIRY_DAYS):
            raise ValueError("Expiry requires persistent memory and a positive number of days up to 36500.")

    def cutoff(self, now):
        return None if self.expiry_days is None else now - self.expiry_days * SECONDS_PER_DAY

    def to_dict(self):
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {"version", "mode", "expiry_days"}
                or type(value["version"]) is not int or value["version"] != 1):
            raise ValueError("Unsupported memory policy format.")
        return cls(value["mode"], value["expiry_days"])


def read_policy(root):
    path = Path(root) / POLICY_FILE
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return MemoryPolicy()
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("The memory policy must be a regular file.")
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("The memory policy is too large.")
    return MemoryPolicy.from_dict(json.loads(raw))


def load_policy(root):
    try:
        return read_policy(root)
    except (OSError, RecursionError, ValueError):
        # An unreadable policy cannot broaden use of the owner's archive.
        return MemoryPolicy("session-only")


def save_policy(root, policy):
    if not isinstance(policy, MemoryPolicy):
        raise TypeError("Expected a MemoryPolicy.")
    root = Path(root)
    if not root.is_dir():
        raise ValueError("Select an existing mind directory.")
    if (root / ".being-restore-in-progress").exists():
        raise ValueError("Finish the restore before editing memory settings.")
    atomic_write(root / POLICY_FILE, json.dumps(policy.to_dict(), sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-memory", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    status = commands.add_parser("status", help="show the current memory policy")
    status.add_argument("--mind-dir", type=Path, default=None)
    configure = commands.add_parser("set", help="save the policy for the next mind start")
    configure.add_argument("mode", choices=MODES)
    configure.add_argument("--expiry-days", type=int, help="delete memory older than this many days")
    configure.add_argument("--mind-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.mind_dir or default_mind_dir()
    try:
        if args.action == "set":
            policy = MemoryPolicy(args.mode, args.expiry_days)
            save_policy(root, policy)
        else:
            policy = read_policy(root)
        print(json.dumps(policy.to_dict(), sort_keys=True))
    except (OSError, RecursionError, ValueError) as exc:
        print(f"being-memory: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
