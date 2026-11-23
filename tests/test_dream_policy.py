"""Eligibility fails closed on wake, invalid readings, and stale clocks."""

import unittest
from datetime import datetime
from unittest.mock import patch

from mind.dream_policy import DreamPolicy, telemetry_time


class DreamPolicyTests(unittest.TestCase):
    def snapshot(self, policy, *, now=10.0, **overrides):
        event = {"t": "body.state", "lid_closed": True, "on_ac": True, "temp": 65.0, "hour": 4, "minute": 59}
        policy.observe(event | overrides, now=now)

    def ready(self, enabled=True):
        policy = DreamPolicy(enabled)
        policy.observe({"t": "body.lid", "closed": True})
        policy.observe({"t": "state.changed", "state": "asleep"})
        self.snapshot(policy)
        return policy

    def test_requires_consent_and_fresh_asleep_closed_night_telemetry(self):
        self.assertFalse(self.ready(enabled=False).eligible(now=10.0))
        self.assertFalse(self.ready(enabled=1).eligible(now=10.0))
        policy = self.ready()
        self.assertTrue(policy.eligible(now=10.0))
        self.assertTrue(policy.eligible(now=17.0))
        self.assertFalse(policy.eligible(now=17.01))
        self.assertFalse(policy.eligible(now=9.99))
        self.snapshot(policy, now=20.0, hour=5, minute=0)
        self.assertFalse(policy.eligible(now=20.0))

    def test_new_invalid_reading_replaces_good_telemetry(self):
        for invalid in (
            {"on_ac": False}, {"on_ac": None}, {"on_ac": 1}, {"temp": None}, {"temp": "50"},
            {"temp": True}, {"temp": float("nan")}, {"temp": 65.01}, {"temp": -1},
            {"hour": -1}, {"hour": True}, {"minute": 60}, {"minute": None}, {"lid_closed": None},
        ):
            with self.subTest(invalid=invalid):
                policy = self.ready()
                self.snapshot(policy, now=11.0, **invalid)
                self.assertFalse(policy.eligible(now=11.0))

    def test_wake_uncertain_lid_and_disconnect_stop_work(self):
        policy = self.ready()
        policy.observe({"t": "state.changed", "state": "waking"})
        self.assertFalse(policy.eligible(now=11.0))
        policy.observe({"t": "state.changed", "state": "asleep"})
        for closed in (False, None):
            policy.observe({"t": "body.lid", "closed": closed})
            self.snapshot(policy)
            self.assertFalse(policy.eligible(now=11.0))
            policy.observe({"t": "body.lid", "closed": True})
            self.assertFalse(policy.eligible(now=11.0))
            self.snapshot(policy)
            self.assertTrue(policy.eligible(now=11.0))
        policy.reset()
        self.assertFalse(policy.eligible(now=11.0))

    def test_current_owner_clock_can_define_a_different_night(self):
        policy = self.ready()
        self.snapshot(policy, hour=19)
        self.assertFalse(policy.eligible(now=11.0))
        self.assertTrue(policy.eligible(now=11.0, local_time=datetime(2026, 11, 21, 0)))
        self.assertTrue(policy.eligible(now=11.0, local_time=datetime(2026, 11, 21, 4, 59, 59)))
        self.assertFalse(policy.eligible(now=11.0, local_time=datetime(2026, 11, 21, 5)))
        self.snapshot(policy, hour=99)
        self.assertFalse(policy.eligible(now=11.0, local_time=datetime(2026, 11, 21, 4, 59, 59)))

    def test_linux_freshness_clock_counts_suspend(self):
        with (patch("mind.dream_policy.time.CLOCK_BOOTTIME", 42, create=True),
              patch("mind.dream_policy.time.clock_gettime", return_value=123) as clock):
            self.assertEqual(telemetry_time(), 123)
            clock.assert_called_once_with(42)
