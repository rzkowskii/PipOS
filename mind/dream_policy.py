"""Eligibility for local overnight memory work."""

import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path


# The brainstem publishes body.state on changes and at least every five seconds.
TELEMETRY_MAX_AGE = 7.0
MAX_DREAM_TEMP_C = 65.0
NIGHT_END_HOUR = 5
CONSENT_FILE = "dreams-enabled"
WINDOWS = ("night", "closed")  # night only, or whenever the lid is closed on AC


def load_window(mind_dir):
    """The owner's dream window from the consent file. An older empty file means night."""
    path = Path(mind_dir) / CONSENT_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "night"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "night"
        raw = os.read(fd, 257)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return "night"
    window = value.get("window") if isinstance(value, dict) else None
    return window if window in WINDOWS else "night"


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
                info = path.stat()
                seen = (info.st_mtime_ns, info.st_size, info.st_ino)
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
