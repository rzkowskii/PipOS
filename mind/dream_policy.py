"""Eligibility for local overnight memory work."""

import time
from datetime import datetime


# The brainstem publishes body.state on changes and at least every five seconds.
TELEMETRY_MAX_AGE = 7.0
MAX_DREAM_TEMP_C = 65.0
NIGHT_END_HOUR = 5


def telemetry_time():
    """Use a clock that continues through Linux suspend, when available."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class DreamPolicy:
    def __init__(self, enabled=False):
        self.enabled = enabled is True
        self.reset()

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
        return hour < NIGHT_END_HOUR and 0 <= age <= TELEMETRY_MAX_AGE
