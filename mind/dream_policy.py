"""Eligibility for local overnight memory work."""

import fcntl
import json
import os
import stat
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .storage import atomic_write, sync_directory


# The brainstem publishes body.state on changes and at least every five seconds.
TELEMETRY_MAX_AGE = 7.0
MAX_DREAM_TEMP_C = 65.0
NIGHT_END_HOUR = 5
CONSENT_FILE = "dreams-enabled"
ONCE_FILE = "dreams-once.json"
WINDOWS = ("night", "closed")  # night only, or whenever the lid is closed on AC
MAX_CONSENT_BYTES = 256
MAX_ONCE_BYTES = 256


def _local_zone(local_now):
    """Return the confirmed IANA zone for an aware local datetime."""
    if (not isinstance(local_now, datetime) or not isinstance(local_now.tzinfo, ZoneInfo)
            or not local_now.tzinfo.key):
        raise ValueError("Dream permission needs a known local time zone.")
    return local_now.tzinfo.key


def next_dream_night(local_now):
    """Return the local calendar date whose night is currently next/active."""
    _local_zone(local_now)
    return local_now.date() if local_now.hour < NIGHT_END_HOUR else local_now.date() + timedelta(days=1)


def _once_payload(mind_dir):
    path = Path(mind_dir) / ONCE_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077):
                return None
            raw = source.read(MAX_ONCE_BYTES + 1)
        if len(raw) > MAX_ONCE_BYTES:
            return None
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (isinstance(value, dict) and value.keys() == {"version", "date", "zone"}
                and type(value["version"]) is int and value["version"] == 1
                and isinstance(value["date"], str) and isinstance(value["zone"], str)):
            day = date.fromisoformat(value["date"])
            if day.isoformat() == value["date"]:
                return day, value["zone"]
    except (OSError, ValueError, RecursionError):
        pass
    return None


def allow_once(mind_dir, local_now):
    zone = _local_zone(local_now)
    planned = next_dream_night(local_now)
    with _consent_lock(mind_dir) as directory:
        atomic_write(directory / ONCE_FILE,
                     json.dumps({"version": 1, "date": planned.isoformat(), "zone": zone}) + "\n")
    return planned


def pending_once(mind_dir, local_now):
    stored = _once_payload(mind_dir)
    if stored is None:
        return None
    try:
        planned = next_dream_night(local_now)
        return planned if stored == (planned, _local_zone(local_now)) else None
    except (ValueError, OverflowError):
        return None


def _remove(path):
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_directory(path.parent)


def _unique_object(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Repeated dream consent field.")
    return fields


@contextmanager
def _consent_lock(mind_dir):
    directory = Path(mind_dir)
    descriptor = os.open(directory / ".dream-consent.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Dream permissions are unavailable.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory
    finally:
        os.close(descriptor)


def set_standing(mind_dir, enabled, window="night"):
    if enabled and window not in WINDOWS:
        raise ValueError("Choose night or whenever the lid is closed.")
    with _consent_lock(mind_dir) as directory:
        if enabled:
            _remove(directory / ONCE_FILE)
            atomic_write(directory / CONSENT_FILE, json.dumps({"version": 1, "window": window}) + "\n")
        else:
            try:
                _remove(directory / CONSENT_FILE)
            finally:
                _remove(directory / ONCE_FILE)


def expire_once(mind_dir, local_now):
    """Retire an elapsed night or superseded time zone on a trusted clock."""
    planned, zone = next_dream_night(local_now), _local_zone(local_now)
    value = _once_payload(mind_dir)
    if value is None or (value[0] >= planned and value[1] == zone):
        return False
    with _consent_lock(mind_dir) as directory:
        if _once_payload(directory) == value:
            _remove(directory / ONCE_FILE)
            return True
    return False


def consent_window(mind_dir):
    """Read valid owner consent. A legacy empty marker consents to nights."""
    path = Path(mind_dir) / CONSENT_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return None
            raw = source.read(MAX_CONSENT_BYTES + 1)
        if len(raw) > MAX_CONSENT_BYTES:
            return None
        if not raw.strip():
            return "night"
        value = json.loads(raw)
        if (isinstance(value, dict) and value.keys() == {"version", "window"}
                and type(value["version"]) is int and value["version"] == 1 and value["window"] in WINDOWS):
            return value["window"]
    except (OSError, ValueError, RecursionError):
        pass
    return None


def load_window(mind_dir):
    """The window used for overnight eligibility, including unconsented upkeep."""
    return consent_window(mind_dir) or "night"


def telemetry_time():
    """Use a clock that continues through Linux suspend, when available."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class DreamPolicy:
    def __init__(self, enabled=False, *, window="night", settings_dir=None):
        self.enabled = enabled is True
        self._window = window if window in WINDOWS else "night"
        # With a settings directory the owner's choice in Care is read live.
        self._settings_dir = settings_dir
        self._settings_seen = None
        self.reset()

    @property
    def window(self):
        if self._settings_dir is not None:
            path = Path(self._settings_dir) / CONSENT_FILE
            try:
                info = path.lstat()
                seen = (info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
            except OSError:
                seen = None
            if seen != self._settings_seen:
                self._settings_seen = seen
                self._window = load_window(self._settings_dir) if seen else "night"
        return self._window

    def reset(self):
        """Discard observations when the brainstem disconnects."""
        self._asleep = False
        self._closed = False
        self._reading = None

    def observe(self, event, *, now=None):
        topic = event.get("t")
        if topic == "state.changed":
            self._asleep = event.get("state") == "asleep"
        elif topic == "body.lid":
            closed = event.get("closed") is True
            if not closed or not self._closed:
                self._reading = None
            self._closed = closed
        elif topic == "body.state":
            hour, minute, temp = event.get("hour"), event.get("minute"), event.get("temp")
            if (self._closed and event.get("lid_closed") is True and event.get("on_ac") is True
                    and type(hour) is int and 0 <= hour < 24
                    and type(minute) is int and 0 <= minute < 60
                    and type(temp) in (int, float) and 0 <= temp <= MAX_DREAM_TEMP_C):
                self._reading = (hour, telemetry_time() if now is None else now)
            else:
                self._reading = None

    def eligible(self, *, now=None, local_time: datetime | None = None):
        if not (self.enabled and self._asleep and self._closed and self._reading is not None):
            return False
        hour, sampled = self._reading
        if local_time is not None:
            hour = local_time.hour
        age = (telemetry_time() if now is None else now) - sampled
        in_window = self.window == "closed" or hour < NIGHT_END_HOUR
        return in_window and 0 <= age <= TELEMETRY_MAX_AGE
