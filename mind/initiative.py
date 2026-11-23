"""Owner settings for announcements, the morning brief and quiet hours.

The file keeps its version 1 shape. The morning brief is on by default and
its key is written only when the owner turns it off, so an untouched
policy reads the same as before the brief existed.
"""

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .storage import atomic_write

POLICY_PATH = Path("initiative.json")
VERSION = 1
MAX_POLICY_BYTES = 1024
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


@dataclass(frozen=True)
class QuietHours:
    start: str
    end: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not _TIME.fullmatch(value) for value in (self.start, self.end)):
            raise ValueError("Use 24-hour HH:MM times for quiet hours.")
        if self.start == self.end:
            raise ValueError("Quiet hours need different start and end times.")


@dataclass(frozen=True)
class InitiativePolicy:
    reminders: bool = True
    greetings: bool = False
    quiet_hours: QuietHours | None = None
    brief: bool = True

    def __post_init__(self):
        if type(self.reminders) is not bool or type(self.greetings) is not bool or type(self.brief) is not bool:
            raise ValueError("Reminder, greeting and brief choices must be Boolean.")
        if self.quiet_hours is not None and not isinstance(self.quiet_hours, QuietHours):
            raise ValueError("Use a QuietHours window or None.")

    def is_quiet(self, local_now: datetime) -> bool:
        """Return whether the supplied local wall time is within quiet hours."""
        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours.start, self.quiet_hours.end
        current = f"{local_now.hour:02d}:{local_now.minute:02d}"
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def may_announce(self, kind, local_now: datetime) -> bool:
        """Whether a due item of `kind` may be spoken now.

        An alarm is the one thing said in quiet hours, and it does not
        depend on the automatic reminders switch: the owner asked for that
        sound at that time. The lid still has to be open.
        """
        if kind == "alarm":
            return True
        return self.reminders and not self.is_quiet(local_now)


_DISABLED = InitiativePolicy(reminders=False, greetings=False, brief=False)
_FIELDS = {"version", "reminders", "greetings", "quiet_hours"}


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Repeated initiative policy field.")
    return fields


def load_policy(mind_dir, *, strict=False) -> InitiativePolicy:
    """Load the policy. Strict readers require an existing, valid file."""
    path = Path(mind_dir) / POLICY_PATH
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Expected a private regular initiative policy file.")
            raw = source.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ValueError("The initiative policy exceeds its allowed size.")
        data = json.loads(raw, object_pairs_hook=_unique_fields)
        if (not isinstance(data, dict) or data.keys() not in (_FIELDS, _FIELDS | {"brief"})
                or type(data["version"]) is not int or data["version"] != VERSION):
            raise ValueError("Invalid initiative policy format.")
        window = data["quiet_hours"]
        return InitiativePolicy(
            reminders=data["reminders"],
            greetings=data["greetings"],
            quiet_hours=QuietHours(**window) if window is not None else None,
            brief=data.get("brief", True),
        )
    except FileNotFoundError:
        if strict:
            raise
        return InitiativePolicy()
    except OSError:
        if strict:
            raise
        return _DISABLED
    except (TypeError, ValueError, RecursionError) as error:
        if strict:
            raise ValueError("Invalid initiative policy.") from error
        return _DISABLED


def save_policy(mind_dir, policy: InitiativePolicy) -> None:
    """Atomically write the private policy in an existing mind directory."""
    if not isinstance(policy, InitiativePolicy):
        raise TypeError("Expected an InitiativePolicy.")
    data = {"version": VERSION, **asdict(policy)}
    if policy.brief:
        del data["brief"]  # the default: the file keeps its original shape
    atomic_write(Path(mind_dir) / POLICY_PATH, json.dumps(data, indent=2) + "\n")
