"""The lost clock of the first real boot (L5.7).

Measured on the laptop on 23 November 2026: the RTC read 2022-01-03T10:33:49
UTC (1641206029) and systemd advanced the time to its built-in epoch,
2026-10-17 18:13:30 UTC.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mind import clock_epoch

RTC_AT_BOOT = 1_641_206_029                                              # dmesg line 670
EPOCH = datetime(2026, 10, 17, 18, 13, 30, tzinfo=timezone.utc).timestamp()  # dmesg line 799
REAL = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc).timestamp()


class LostClock(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rtc = self.root / "since_epoch"
        self.epoch_file = self.root / "clock-epoch"
        self.synced = self.root / "synchronized"

    def advanced(self, *, rtc, now, epoch_file=True):
        self.rtc.write_text(f"{int(rtc)}\n")
        files = ()
        if epoch_file:
            self.epoch_file.touch()
            os.utime(self.epoch_file, (EPOCH, EPOCH))
            files = (self.epoch_file,)
        return clock_epoch.epoch_advanced(rtc=self.rtc, epochs=files, synchronised=self.synced, now=now)

    def test_the_first_boot_fixture_is_a_lost_clock(self):
        # 95 s after power-on, as in summary.txt.
        self.assertTrue(self.advanced(rtc=RTC_AT_BOOT + 95, now=EPOCH + 92))
        # With the epoch compiled into systemd, so no file: the RTC is years behind.
        self.assertTrue(self.advanced(rtc=RTC_AT_BOOT + 95, now=EPOCH + 92, epoch_file=False))
        self.assertTrue(clock_epoch.doubted(now=EPOCH + 92, rtc=self.rtc, epochs=(), synchronised=self.synced))

    def test_a_working_clock_is_believed(self):
        self.assertFalse(self.advanced(rtc=REAL, now=REAL + 1))
        self.assertFalse(self.advanced(rtc=REAL, now=REAL + 1, epoch_file=False))
        # Setting the time writes the RTC too; from then on it is believed.
        self.assertFalse(self.advanced(rtc=EPOCH + 37 * 86400, now=EPOCH + 37 * 86400 + 5))

    def test_a_network_sync_or_no_rtc_is_no_reason_to_doubt(self):
        self.synced.touch()
        self.assertFalse(self.advanced(rtc=RTC_AT_BOOT, now=EPOCH))
        self.synced.unlink()
        self.assertFalse(clock_epoch.epoch_advanced(rtc=self.root / "missing", epochs=(), synchronised=self.synced))
        self.rtc.write_text("garbage")
        self.assertFalse(clock_epoch.epoch_advanced(rtc=self.rtc, epochs=(), synchronised=self.synced))

    def test_an_rtc_kept_in_local_time_is_not_mistaken_for_a_lost_one(self):
        self.assertFalse(self.advanced(rtc=REAL + 11 * 3600, now=REAL, epoch_file=False))
        self.assertFalse(self.advanced(rtc=REAL - 10 * 3600, now=REAL, epoch_file=False))

    def test_a_clock_before_the_software_existed_is_doubted(self):
        self.assertTrue(clock_epoch.doubted(now=RTC_AT_BOOT, rtc=self.root / "missing", epochs=(),
                                            synchronised=self.synced))
        self.assertFalse(clock_epoch.doubted(now=REAL, rtc=self.root / "missing", epochs=(),
                                             synchronised=self.synced))

    def test_a_clock_file_touched_a_moment_ago_is_not_a_lost_clock(self):
        # timesyncd saves its clock file while it runs; the RTC counts whole
        # seconds and drifts, so it can read a little before that file.
        self.rtc.write_text(f"{int(REAL) - 2}\n")
        self.epoch_file.touch()
        os.utime(self.epoch_file, (REAL + 0.6, REAL + 0.6))
        self.assertFalse(clock_epoch.epoch_advanced(rtc=self.rtc, epochs=(self.epoch_file,),
                                                    synchronised=self.synced, now=REAL + 0.7))
        # Hours behind the epoch file is still a lost clock, even within a day of now.
        self.rtc.write_text(f"{int(REAL) - 3 * 3600}\n")
        self.assertTrue(clock_epoch.epoch_advanced(rtc=self.rtc, epochs=(self.epoch_file,),
                                                   synchronised=self.synced, now=REAL + 0.7))

    def test_the_question_it_asks(self):
        self.assertEqual(clock_epoch.LOST_CLOCK_LINE, "[concern] I lost track of time. What time is it?")
        self.assertIsInstance(clock_epoch.epoch_advanced(), bool)


if __name__ == "__main__":
    unittest.main()
