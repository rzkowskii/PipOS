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


EXPIRY_BATCH = 200
EXPIRY_STATES = ("asleep", "idle")  # expiry runs only while resting, never mid-conversation


def local_cutoff(sure, expiry_days, zone=None):
    """The start of the owner's local day `expiry_days` before the day `sure` falls on.

    Counting whole days in the owner's zone means nothing from "today" is ever
    expired and a day goes all at once, instead of the archive being trimmed
    every day at the hour UTC midnight lands on.
    """
    from datetime import datetime, time as clock, timedelta

    if expiry_days is None or sure is None:
        return None
    day = datetime.fromtimestamp(sure, zone).date() - timedelta(days=expiry_days)
    return datetime.combine(day, clock(0), tzinfo=zone).timestamp() if zone is not None else \
        datetime.combine(day, clock(0)).timestamp()


def expiry_allowed(state, *, lid_open=True, busy=False):
    """Expiry is a quiet chore: only while resting and with no turn in progress."""
    return state in EXPIRY_STATES and not busy


def expiry_steps(db, cutoff, batch=EXPIRY_BATCH):
    """Sub-cutoffs that each expire at most about `batch` episodes, oldest first."""
    steps = []
    bound = None
    while True:
        row = db.execute(
            "SELECT ts FROM episodes WHERE ts < ? AND kind <> 'life' AND (text <> '[forgotten]' OR meta IS NOT NULL) "
            + ("AND ts >= ? " if bound is not None else "") + "ORDER BY ts LIMIT 1 OFFSET ?",
            (cutoff, bound, batch) if bound is not None else (cutoff, batch)).fetchone()
        if row is None or row[0] <= (bound if bound is not None else float("-inf")):
            break
        steps.append(row[0])
        bound = row[0]
        if len(steps) > 10_000:
            break
    steps.append(cutoff)
    return steps


def expire_gently(mind_dir, cutoff, *, batch=EXPIRY_BATCH, keep_going=lambda: True, memory_factory=None):
    """Expire in batches with a fresh connection, for a worker thread.

    Between batches `keep_going()` is asked whether the being is still
    resting; a wake stops the work and the rest waits for the next rest.
    Returns the MemoryEdit of every batch that ran.
    """
    from contextlib import closing

    if cutoff is None:
        return []
    if memory_factory is None:
        from .memory import Memory as memory_factory
    edits = []
    with closing(memory_factory(Path(mind_dir))) as memory:
        for step in expiry_steps(memory.db, cutoff, batch):
            if not keep_going():
                break
            change = memory.expire_before(step)
            if change:
                edits.append(change)
    return edits


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
