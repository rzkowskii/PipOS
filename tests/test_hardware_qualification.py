import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mind import hardware_qualification


class QualificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_timing_and_hardware_share_one_window(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="being-hw-")))
        path = root / "bus.sock"
        fixture = {
            "proc/stat": "cpu 215 0 164 527 1 0 0 0",
            "sys/class/dmi/id/product_name": "MacBookPro9,2",
            "sys/class/hwmon/hwmon0/name": "coretemp",
            "sys/class/hwmon/hwmon0/temp1_input": "62000",
            "sys/class/hwmon/hwmon0/temp1_label": "Package id 0",
            "sys/class/powercap/intel-rapl:0/name": "package-0",
            "sys/class/powercap/intel-rapl:0/energy_uj": "1042000",
            "sys/class/powercap/intel-rapl:0/max_energy_range_uj": "2400000",
            "sys/class/power_supply/AC/type": "Mains",
            "sys/class/power_supply/AC/online": "1",
            "sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq": "2600000",
            "sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count": "4",
        }
        for relative, value in fixture.items():
            file_path = root / relative
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(value)
        disconnected = asyncio.get_running_loop().create_future()

        async def serve(reader, writer):
            try:
                await reader.readline()
                await asyncio.sleep(0.05)
                events = (
                    {"t": "body.lid", "seq": 1, "closed": False},
                    {"t": "panel.ready", "seq": 1},
                    {"t": "state.changed", "state": "listening", "muted": False},
                    {"t": "hear.final", "text": "Private question", "lid_seq": 1},
                    {"t": "state.changed", "state": "thinking", "muted": False},
                    {"t": "say.request", "text": "Private answer", "seq": 1, "last": False, "lid_seq": 1},
                    {"t": "say.started", "lid_seq": 1},
                    {"t": "state.changed", "state": "speaking", "muted": False},
                    {"t": "say.done", "final": True, "lid_seq": 1},
                )
                for event in events:
                    writer.write(json.dumps(event).encode() + b"\n")
                    await writer.drain()
                disconnected.set_result(await reader.read())
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_unix_server(serve, path):
            with patch.object(hardware_qualification.hardware_metrics, "read_services", return_value={}):
                result = await asyncio.wait_for(hardware_qualification.capture(1, 1, root=root, path=path), 3)
            self.assertEqual(await asyncio.wait_for(disconnected, 1), b"")

        self.assertEqual(result["clock"], "monotonic_ms_since_window_start")
        self.assertEqual(result["system"]["model"], "MacBookPro9,2")
        first, last = result["samples"]
        self.assertEqual([first["scheduled_at_ms"], last["scheduled_at_ms"]], [0, 1000])
        self.assertEqual(first["thermal"][0]["temperature_c"], 62)
        self.assertEqual(first["thermal_throttle"]["cpu0"]["core_events"], 4)
        self.assertEqual(first["cpu_frequency"]["policy0"]["current_khz"], 2600000)
        self.assertTrue(first["power"]["mains_online"])
        self.assertEqual(first["power"]["packages"]["package-0"]["energy_uj"], 1042000)
        self.assertEqual(first["power"]["batteries"], [])
        self.assertLessEqual(first["sample_start_at_ms"], first["sample_end_at_ms"])
        self.assertGreaterEqual(last["sample_start_at_ms"], 1000)
        [turn] = result["timing"]["voice_attempts"]
        [lid] = result["timing"]["lid_opens"]
        self.assertEqual(turn["outcome"], "completed")
        self.assertLessEqual(first["sample_start_at_ms"], lid["open_at_ms"])
        self.assertLessEqual(lid["open_at_ms"], turn["final_at_ms"])
        self.assertLessEqual(turn["final_at_ms"], turn["playback_launch_at_ms"])
        self.assertLessEqual(turn["playback_launch_at_ms"], turn["end_at_ms"])
        self.assertLessEqual(turn["end_at_ms"], last["sample_start_at_ms"])
        self.assertNotIn("Private", json.dumps(result))

    async def test_disconnected_bus_aborts_a_long_capture(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="being-hw-")))
        path = root / "bus.sock"

        async def disconnect(reader, writer):
            await reader.readline()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_unix_server(disconnect, path):
            with patch.object(hardware_qualification.hardware_metrics, "read_services", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "disconnected"):
                    await asyncio.wait_for(hardware_qualification.capture(60, 1, root=root, path=path), 2)
