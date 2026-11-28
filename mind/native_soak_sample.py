"""Typed observations from the native Mac, without speech or archive contents."""

import json
import math
import socket
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from . import hardware_metrics


SYS = Path("/sys")
PROC = Path("/proc")
ORGANS = frozenset(f"being-{name}.service" for name in ("brainstem", "face", "voice", "mind", "cortex"))
BUS_TOPICS = ("body.lid", "state.changed", "body.care")


def _number(value, low, high):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def _body():
    latest = {}
    deadline = time.monotonic() + 4
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bus:
            bus.settimeout(4)
            bus.connect("/run/being/bus.sock")
            hello = {"t": "hello", "name": "native-soak", "sub": BUS_TOPICS}
            bus.sendall((json.dumps(hello) + "\n").encode())
            with bus.makefile("rb") as stream:
                while len(latest) < len(BUS_TOPICS):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    bus.settimeout(remaining)
                    line = stream.readline(8193)
                    if not line or len(line) > 8192:
                        break
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("t") in BUS_TOPICS:
                        latest[event["t"]] = event
    except (OSError, ValueError):
        pass
    return latest


def _battery_temp(care):
    measured = _number(care.get("battery_c"), 0, 90)
    if measured is not None:
        return measured
    for path in sorted((SYS / "class/power_supply").glob("*"))[:16]:
        if (hardware_metrics.read_field(path / "type", 32) == "Battery"
                and hardware_metrics.read_field(path / "scope", 32) != "Device"):
            return hardware_metrics.measure(path / "temp", lower=1, upper=899, divisor=10)
    return None


def _archive(mind):
    path = Path(mind) / "mind.db"
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)


def _dream(mind):
    try:
        with closing(_archive(mind)) as db:
            row = db.execute("SELECT night, batches FROM dream_checkpoints WHERE night IS NOT NULL "
                             "ORDER BY night DESC, batches DESC LIMIT 1").fetchone()
        if row is None:
            return True, None
        if isinstance(row[0], str) and type(row[1]) is int and row[1] >= 0:
            return True, {"night": row[0], "batches": row[1]}
    except (OSError, sqlite3.Error):
        pass
    return False, None


def _audio_after(start, end):
    try:
        output = subprocess.run(
            ["/usr/bin/journalctl", "-b", "-u", "being-voice.service", "--grep", "^voice timing ",
             "--output=json", "--output-fields=MESSAGE,__MONOTONIC_TIMESTAMP", "-n", "256", "--no-pager"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if output.returncode:
        return []
    result = []
    for line in output.stdout.splitlines():
        try:
            event = json.loads(line)
            stamp = int(event["__MONOTONIC_TIMESTAMP"]) / 1_000_000
            message = event["MESSAGE"]
            if not start < stamp <= end or not message.startswith("voice timing "):
                continue
            value = _number(json.loads(message.removeprefix("voice timing "))["eos_to_audio_s"], 0, 600)
            if value is not None:
                result.append(value)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return result


def snapshot(mind, *, voice_after, voice_through):
    body = _body()
    lid = body.get("body.lid", {}).get("closed")
    state = body.get("state.changed", {})
    care = body.get("body.care", {})
    power = hardware_metrics.power_info(SYS)
    batteries = power["batteries"]
    battery = batteries[0] if len(batteries) == 1 else {}
    temperatures = hardware_metrics.thermal_info(SYS)
    package_temps = [sensor["temperature_c"] for sensor in temperatures
                     if sensor["driver"] == "coretemp" and "temperature_c" in sensor
                     and (sensor.get("label") or "").startswith("Package")]
    fans = [sensor["fan_rpm"] for sensor in temperatures if "fan_rpm" in sensor]
    package = _number(care.get("package_c"), 0, 150)
    fan = _number(care.get("fan_rpm"), 0, 50_000)
    cap = care.get("cap_honoured")
    limit = care.get("charge_limit")
    dream_read, dream = _dream(mind)
    services = hardware_metrics.read_services(PROC)
    return {"lid_closed": lid if type(lid) is bool else None,
            "unwell": state.get("unwell") if type(state.get("unwell")) is bool else None,
            "mains_online": power["mains_online"], "battery_percent": battery.get("charge_percent"),
            "battery_temp_c": _battery_temp(care),
            "charge_limit": limit if type(limit) is int and 50 <= limit <= 100 else None,
            "cap_honoured": cap if type(cap) is bool else None, "package_energy": power["packages"],
            "package_temp_c": package if package is not None else max(package_temps, default=None),
            "fan_rpm": fan if fan is not None else max(fans, default=None),
            "available_kib": hardware_metrics.memory_info(PROC)["available_kib"],
            "services": {name: value for name, value in services.items() if name in ORGANS},
            "dream_read": dream_read, "dream": dream, "first_audio_s": _audio_after(voice_after, voice_through)}


def previous_power_lost(mind, boot_id):
    try:
        record = json.loads(hardware_metrics.read_field(Path(mind) / "life.json", 64 * 1024))
        wake = record.get("this_wake")
    except (AttributeError, ValueError):
        return None
    if record.get("boot") != boot_id or not isinstance(wake, dict) or wake.get("new_body") is not False:
        return None
    lost = wake.get("lost_power")
    return lost if type(lost) is bool else None


def archive_intact(mind):
    try:
        with closing(_archive(mind)) as db:
            deadline = time.monotonic() + 8
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            return db.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
    except (OSError, sqlite3.Error):
        return None
