"""The body monitor (mind/app_monitor.py): read-only readings from a fixture
/proc and /sys laid out like the MacBookPro9,2's, and the picture drawn from
them, within its 5 ms a frame."""

import re
import tempfile
import time
import unittest
from pathlib import Path

from mind import app_monitor
from mind.app_monitor import Sampler, bar, cpu_percent, cpu_times, render

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAT = """cpu  400 0 200 4000 100 0 0 0 0 0
cpu0 100 0 50 1000 25 0 0 0 0 0
cpu1 100 0 50 1000 25 0 0 0 0 0
cpu2 100 0 50 1000 25 0 0 0 0 0
cpu3 100 0 50 1000 25 0 0 0 0 0
intr 1 2 3
"""
MEMINFO = "MemTotal:       16337000 kB\nMemFree:  9000000 kB\nMemAvailable:   12000000 kB\n"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def body_tree(root):
    """A /proc and /sys with what the laptop has: 4 logical CPUs, coretemp
    (package and two cores) and applesmc's one fan, behind a bare hwmon
    class device as on Linux 6.18."""
    write(root / "proc/stat", STAT)
    write(root / "proc/meminfo", MEMINFO)
    write(root / "proc/uptime", "11520.42 20000.00\n")
    core = root / "sys/class/hwmon/hwmon1"
    write(core / "name", "coretemp\n")
    for index, (label, value) in enumerate((("Package id 0", 48000), ("Core 0", 47000), ("Core 1", 46000)), 1):
        write(core / f"temp{index}_label", label + "\n")
        write(core / f"temp{index}_input", f"{value}\n")
    smc = root / "sys/class/hwmon/hwmon2"
    write(smc / "device/name", "applesmc\n")
    write(smc / "device/fan1_input", "2003\n")
    write(smc / "device/fan1_max", "6200\n")
    write(smc / "device/fan1_label", "Master\n")
    # Something else entirely: never read.
    write(root / "sys/class/hwmon/hwmon3/name", "nvme\n")
    write(root / "sys/class/hwmon/hwmon3/temp1_input", "99000\n")


def lines(frame):
    rows = re.split(r"\x1b\[\d+;1H", frame)[1:]
    return [ANSI.sub("", row) for row in rows]


BODY = {"battery": 64, "charging": False, "on_ac": False, "minutes_left": 190, "health_pct": 81, "cycles": 71,
        "temp": 50.0}


class SamplerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        body_tree(self.root)
        self.now = [100.0]
        self.sampler = Sampler(root=self.root, disk=self.dir.name, clock=lambda: self.now[0])

    def test_a_reading(self):
        reading = self.sampler.sample()
        self.assertEqual(len(reading["cpus"]), 4)
        self.assertEqual(reading["cpus"][0], (150, 1175))
        self.assertEqual(reading["memory"]["available_kib"], 12000000)
        self.assertAlmostEqual(reading["uptime_s"], 11520.42)
        self.assertEqual(reading["fans"], [(2003, 6200)])
        self.assertEqual(reading["cores_c"], [("Core 0", 47.0), ("Core 1", 46.0)])
        self.assertEqual(reading["package_c"], 48.0)
        size, free = reading["disk"]
        self.assertGreater(size, 0)
        self.assertGreaterEqual(size, free)

    def test_sensors_are_found_once_then_rechecked(self):
        self.sampler.sample()
        write(self.root / "sys/class/hwmon/hwmon2/device/fan2_input", "1500\n")
        self.assertEqual(len(self.sampler.sample()["fans"]), 1, "no rescan within 30 s")
        self.now[0] += app_monitor.DISCOVER_EVERY
        self.assertEqual(len(self.sampler.sample()["fans"]), 2)

    def test_missing_and_broken_files(self):
        (self.root / "proc/stat").write_text("cpu0 a b c\n")
        (self.root / "sys/class/hwmon/hwmon2/device/fan1_input").write_text("lots\n")
        reading = Sampler(root=self.root, disk="/no/such/disk").sample()
        self.assertEqual((reading["cpus"], reading["fans"], reading["disk"]), ([], [], None))
        frame = render(reading, None, {}, {})
        self.assertIn("Thinking    no reading", "\n".join(lines(frame)))

    def test_cpu_percent(self):
        before = [(100, 1000), (0, 1000)]
        after = [(600, 2000), (0, 1000)]
        self.assertEqual(cpu_percent(before, after), [50.0, None])
        self.assertIsNone(cpu_percent(None, after))
        self.assertIsNone(cpu_percent(before, after[:1]))
        self.assertEqual(cpu_times(self.root / "proc")[3], (150, 1175))


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        body_tree(self.root)
        self.sampler = Sampler(root=self.root, disk=self.dir.name)

    def frame(self, body=BODY, model=None):
        first = self.sampler.sample()
        later = STAT.replace("100 0 50 1000", "100 0 50 2000").replace("cpu0 100 0 50 2000", "cpu0 600 0 50 1500")
        write(self.root / "proc/stat", later)
        second = self.sampler.sample()
        return render(second, first, body, model or {"state": "thinking", "tok_s": 8.43})

    def test_the_picture(self):
        text = "\n".join(lines(self.frame()))
        for words in ("awake 3 h 12 min", "50 %  CPU 1", "0 %  CPU 4", "48 °C",
                      "core 0 47°", "2,003 rpm", "64 %  on battery, about 3 h 10 min left",
                      "holds 81 % of what it did when new, 71 charge cycles", "4.4 GB of 16.7 GB in use",
                      "free of", "thinking", "writes 8.4 tokens a second (my last script)"):
            self.assertIn(words, text)

    def test_every_row_is_placed_and_fits(self):
        frame = self.frame()
        self.assertTrue(frame.startswith("\x1b[?25l"), "the cursor is hidden")
        rows = lines(frame)
        self.assertEqual(len(rows), app_monitor.ROWS)
        self.assertTrue(all(len(row) <= app_monitor.COLUMNS for row in rows), max(map(len, rows)))
        self.assertNotIn("\n", frame, "positioned rows: the panel never scrolls")
        self.assertEqual(frame.count("\x1b[K"), app_monitor.ROWS, "each row clears what was there")

    def test_honest_when_nothing_is_known(self):
        text = "\n".join(lines(render({"cpus": [(1, 2)]}, None, {}, {})))
        for words in ("…  CPU 1", "Battery     no reading yet", "Memory      no reading", "not heard from yet",
                      "writing speed not measured yet"):
            self.assertIn(words, text)
        self.assertNotIn("°C", text)

    def test_plugged_in_and_charging(self):
        text = "\n".join(lines(self.frame({"battery": 80, "charging": True, "on_ac": True, "minutes_left": 30})))
        self.assertIn("80 %  charging", text)
        self.assertNotIn("left", text.split("Battery")[1].split("\n")[0])

    def test_bars(self):
        self.assertEqual(ANSI.sub("", bar(0.5, width=4)), "██░░")
        self.assertEqual(ANSI.sub("", bar(1 / 32, width=4)), "▏░░░")
        self.assertEqual(ANSI.sub("", bar(2.0, width=4)), "████")
        self.assertEqual(ANSI.sub("", bar(None, width=4)), "░░░░")

    def test_only_glyphs_the_console_draws(self):
        # being-console's font: ASCII, its extra punctuation and its drawn
        # blocks (lab/console_preview.py EXTRA and DRAWN).
        from lab.console_preview import DRAWN, EXTRA
        allowed = set(EXTRA) | set(DRAWN)
        for char in "".join(lines(self.frame())):
            self.assertTrue(" " <= char <= "~" or char in allowed, repr(char))

    def test_a_frame_takes_at_most_5_ms(self):
        previous = self.sampler.sample()
        runs = []
        for _ in range(50):
            started = time.perf_counter()
            reading = self.sampler.sample()
            render(reading, previous, BODY, {"state": "idle"})
            runs.append((time.perf_counter() - started) * 1000)
            previous = reading
        runs.sort()
        self.assertLessEqual(runs[len(runs) // 2], 5.0, runs)


if __name__ == "__main__":
    unittest.main()
