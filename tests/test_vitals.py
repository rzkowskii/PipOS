import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mind import doctor, vitals


class OvernightSampleTests(unittest.IsolatedAsyncioTestCase):
    def test_rtc_offset_requires_a_stable_wall_clock_read(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        rtc = root / vitals.RTC_SECONDS
        rtc.parent.mkdir(parents=True)
        rtc.write_text("1795687203\n")
        clock = iter((1795687200.0, 1795687200.2))
        self.assertEqual(vitals._rtc_offset(root, wall=lambda: next(clock)), 3)
        clock = iter((1795687200.0, 1795687199.0))
        self.assertIsNone(vitals._rtc_offset(root, wall=lambda: next(clock)))

    async def test_final_read_cannot_publish_after_the_night_gate_closes(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        eligible = True
        job = vitals.VitalJob(root, "2026-11-26", eligible=lambda: eligible, clock=lambda: 30)
        job.start = (0, {"cpu": {"energy_uj": 0, "range_uj": None}})
        ending = (30, {"cpu": {"energy_uj": 240_000_000, "range_uj": None}})

        def close_night_gate(*args):
            nonlocal eligible
            eligible = False
            return {}

        with patch.object(vitals, "_package", return_value=ending), patch.object(
            vitals, "_details", side_effect=close_night_gate
        ):
            self.assertFalse(await job.run_chunk(1))
        self.assertIsNone(job.start)
        self.assertFalse((root / vitals.FILE).exists())


class PackagePowerTrendTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        self.night = date(2026, 11, 26)

    def record(self, powers, *, different_build_at=None, missing_at=None):
        for offset, watts in enumerate(powers):
            if offset == missing_at:
                continue
            night = (self.night - timedelta(days=offset)).isoformat()
            build = "old-build" if offset == different_build_at else "current-build"
            vitals._commit(self.root, night, "MacBookPro9,2", build,
                           {"package_watts": watts, "seconds": 30})

    def trend(self):
        return vitals.package_power_trend(self.root, self.night.isoformat())

    def test_reports_a_sustained_change(self):
        self.record([10] * 9 + [7] * 28)
        self.assertEqual(self.trend(), 3)

    def test_waits_for_three_matching_windows(self):
        self.record([10] * 5 + [7] * 32)
        self.assertIsNone(self.trend())

    def test_does_not_compare_across_builds(self):
        self.record([10] * 9 + [7] * 28, different_build_at=20)
        self.assertIsNone(self.trend())

    def test_does_not_treat_a_missing_night_as_consecutive(self):
        self.record([10] * 9 + [7] * 29, missing_at=20)
        self.assertIsNone(self.trend())

    def test_doctor_reports_only_recent_power_for_the_running_build(self):
        self.record([10] * 9 + [7] * 28)
        now = datetime(2026, 11, 27, tzinfo=UTC).timestamp()
        facts = doctor.Facts(state_dir=self.root, now=now)
        with patch.object(vitals, "hardware_epoch", return_value=("MacBookPro9,2", "current-build")):
            findings = doctor.check_overnight_power(None, facts)
            self.assertIn("3.0 watts higher", doctor.spoken(findings))
            facts.now += 7 * 86400
            self.assertEqual(doctor.check_overnight_power(None, facts), [])
        facts.now = now
        with patch.object(vitals, "hardware_epoch", return_value=("MacBookPro9,2", "new-build")):
            self.assertEqual(doctor.check_overnight_power(None, facts), [])

    def test_first_seven_night_baseline_survives_reading_retention(self):
        for offset in range(vitals.MAX_NIGHTS + 7):
            night = (self.night + timedelta(days=offset)).isoformat()
            watts = 8 if offset < 7 else 12
            vitals._commit(self.root, night, "MacBookPro9,2", "current-build",
                           {"package_watts": watts, "seconds": 30})
            if offset == 5:
                self.assertIsNone(vitals.package_power_baseline(self.root, night))
        self.assertEqual(vitals.package_power_baseline(self.root, night), 8)

        next_night = (date.fromisoformat(night) + timedelta(days=1)).isoformat()
        vitals._commit(self.root, next_night, "MacBookPro9,2", "next-build",
                       {"package_watts": 12, "seconds": 30})
        self.assertIsNone(vitals.package_power_baseline(self.root, next_night))
