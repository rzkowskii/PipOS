"""Bounded, read-only Linux telemetry for owner-controlled hardware trials."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time


SCHEMA_VERSION = 1
MAX_SECONDS = 300
MAX_INTERVAL = 60
SERVICES = (
    "being-brainstem.service", "being-face.service", "being-mind.service",
    "being-voice.service", "being-cortex.service", "being-wifi.service",
    "being-boot-health.service",
)
SERVICE_VALUES = {
    "LoadState": "load_state",
    "ActiveState": "active_state",
    "SubState": "sub_state",
    "NRestarts": "restarts",
    "ExecMainStatus": "main_exit_status",
    "ActiveEnterTimestampMonotonic": "active_since_boot_us",
    "MemoryCurrent": "memory_bytes",
    "CPUUsageNSec": "cpu_usage_ns",
}
STATE = r"[a-z][a-z-]{0,31}"
SAFE_LABEL = r"[a-zA-Z0-9][a-zA-Z0-9 +.,_()@/-]{0,95}"
SYS_NAME = r"[a-zA-Z0-9._-]{1,48}"
DISK = r"(?:(?:sd|hd|vd|xvd)[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)"
HWMON_DRIVERS = frozenset(("coretemp", "applesmc", "acpitz"))
SMC_TEMPERATURES = frozenset(("TC0D", "TC0P", "TG0D", "TG0P", "TN0D"))
MEMORY_FIELDS = {
    "MemTotal": "total_kib", "MemAvailable": "available_kib",
    "SwapTotal": "swap_total_kib", "SwapFree": "swap_free_kib",
}


def read_field(path, limit=64 * 1024):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            return stream.read(limit).strip()
    except OSError:
        return ""


def allowed(value, pattern):
    return value if re.fullmatch(pattern, value) else None


def number(value):
    try:
        return int(value)
    except ValueError:
        return None


def measure(path, *, lower=0, upper=None, divisor=1):
    value = number(read_field(path, 64))
    if value is None or value < lower or (upper is not None and value > upper):
        return None
    return value if divisor == 1 else round(value / divisor, 3)


def model_digest(path):
    try:
        digest = json.loads(read_field(path, 16 * 1024))["reflex"]["sha256"]
    except (KeyError, TypeError, ValueError):
        return None
    return allowed(digest, "[a-f0-9]{64}") if isinstance(digest, str) else None


def read_services(proc_root):
    names = (*SERVICE_VALUES, "Id", "MainPID")
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "--system", "--no-pager", "show",
             f"--property={','.join(names)}", *SERVICES],
            capture_output=True, encoding="utf-8", errors="replace", timeout=4, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    services = {}
    for block in result.stdout.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = props.get("Id")
        if unit not in SERVICES:
            continue
        state = {}
        for field, name in SERVICE_VALUES.items():
            value = props.get(field, "")
            if field.endswith("State"):
                state[name] = allowed(value, STATE)
            else:
                parsed = number(value)
                state[name] = parsed if parsed is not None and 0 <= parsed < (1 << 64) - 1 else None
        if unit == "being-cortex.service":
            state["arguments"] = cortex_arguments(proc_root, props.get("MainPID", ""))
        services[unit] = state
    return services


def cortex_arguments(proc_root, pid):
    """Read only numeric performance flags from llama-server's actual argv."""
    if not allowed(pid, r"[1-9][0-9]{0,9}"):
        return {}
    argv = read_field(proc_root / pid / "cmdline", 8192).rstrip("\0").split("\0")
    if Path(argv[0]).name != "llama-server":
        return {}
    flags = {"--threads": "threads", "--threads-batch": "prompt_threads",
             "--batch-size": "batch_size", "--ubatch-size": "ubatch_size"}
    result = {}
    for arg, next_arg in zip(argv[1:], argv[2:] + [""]):
        flag, separator, value = arg.partition("=")
        if flag in flags:
            parsed = number(value if separator else next_arg)
            if parsed is not None and 0 < parsed <= 1 << 20:
                result[flags[flag]] = parsed
    return result


def graphics_inventory(sys_root):
    entries = sorted((sys_root / "class/drm").glob("card[0-9]*"))[:64]
    cards = {}
    for path in entries:
        if not re.fullmatch(r"card[0-9]{1,3}", path.name):
            continue
        try:
            driver = (path / "device/driver").readlink().name
        except OSError:
            driver = ""
        prefix = f"{path.name}-"
        connectors = [entry.name.removeprefix(prefix) for entry in entries
                      if re.fullmatch(rf"{prefix}(LVDS|eDP|DSI)-[0-9]{{1,3}}", entry.name)]
        cards[path.name] = {
            "driver": allowed(driver, SYS_NAME),
            "vendor_id": allowed(read_field(path / "device/vendor", 16), r"0x[0-9a-f]{4}"),
            "device_id": allowed(read_field(path / "device/device", 16), r"0x[0-9a-f]{4}"),
            "internal_connectors": connectors,
        }
    return cards


def audio_inventory(proc):
    pcms = {}
    for line in read_field(proc / "asound/pcm").splitlines()[:64]:
        match = re.match(r"^([0-9]{1,3})-([0-9]{1,3}):", line)
        if match:
            key = f"{int(match[1])}:{int(match[2])}"
            pcms[key] = {direction: bool(re.search(rf":\s*{direction}\s+[1-9][0-9]*(?=\s*(?::|$))", line))
                         for direction in ("capture", "playback")}
    return pcms


def lid_switch_detected(sys_root):
    detected = None
    for path in sorted((sys_root / "class/input").glob("input[0-9]*/capabilities/sw"))[:64]:
        words = read_field(path, 128).split()
        if words and re.fullmatch(r"[0-9a-fA-F]{1,16}", words[-1]):
            if int(words[-1], 16) & 1:  # EV_SW capability bit zero is SW_LID.
                return True
            detected = False
    return detected


def lid_closed(proc):
    states = set()
    for path in sorted((proc / "acpi/button/lid").glob("*/state"))[:4]:
        if match := re.fullmatch(r"state:\s*(open|closed)", read_field(path, 64)):
            states.add(match[1] == "closed")
    return states.pop() if len(states) == 1 else None


def backlight_info(sys_root):
    lights = {}
    for path in sorted((sys_root / "class/backlight").glob("*"))[:8]:
        if not re.fullmatch(SYS_NAME, path.name):
            continue
        maximum = measure(path / "max_brightness", lower=1)
        brightness = measure(path / "brightness", upper=maximum)
        lights[path.name] = {
            # Requested brightness avoids a driver callback that can wake a sleeping GPU.
            "requested_percent": round(100 * brightness / maximum, 1) if maximum and brightness is not None else None,
            "power_state": measure(path / "bl_power", upper=4),
            "type": allowed(read_field(path / "type", 32), r"raw|platform|firmware"),
        }
    return lights


def system_info(root):
    proc = root / "proc"
    sys_root = root / "sys"
    cpu = {}
    for line in read_field(proc / "cpuinfo").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            cpu.setdefault(key.strip(), value.strip())
    flags = cpu.get("flags", "").split()
    dmi = sys_root / "class/dmi/id"
    slot = re.search(r"(?:^|\s)rauc\.slot=([AB])(?:\s|$)", read_field(proc / "cmdline"))
    return {
        "build_id": allowed(read_field(root / "etc/being/build-id", 128), r"[a-zA-Z0-9._-]{1,96}"),
        "model_sha256": model_digest(root / "models/manifest.json"),
        "kernel": allowed(read_field(proc / "sys/kernel/osrelease", 128), r"[a-zA-Z0-9._+-]{1,96}"),
        "slot": slot[1] if slot else None,
        "model": allowed(read_field(dmi / "product_name", 128), SAFE_LABEL),
        "firmware": allowed(read_field(dmi / "bios_version", 128), SAFE_LABEL),
        "cpu_model": allowed(cpu.get("model name", ""), SAFE_LABEL),
        "microcode": allowed(cpu.get("microcode", ""), r"0x[a-fA-F0-9]{1,16}"),
        "cpu_flags": [flag for flag in ("avx", "avx2", "f16c", "fma") if flag in flags],
        "cpu_idle_driver": allowed(read_field(sys_root / "devices/system/cpu/cpuidle/current_driver", 64),
                                   r"[a-zA-Z0-9_-]{1,48}"),
        "hardware": {
            "drm_cards": graphics_inventory(sys_root),
            "alsa_pcms": audio_inventory(proc),
            "lid_switch_detected": lid_switch_detected(sys_root),
        },
    }


def cpu_counters(proc):
    row = read_field(proc / "stat").split("\n", 1)[0].split()
    values = [number(value) for value in row[1:9]]  # Guest CPU time is already counted in user/nice.
    if row[:1] != ["cpu"] or len(values) != 8 or any(value is None or value < 0 for value in values):
        return {}
    return {"ticks": sum(values), "idle_ticks": values[3], "iowait_ticks": values[4]}


def memory_info(proc):
    result = {field: None for field in MEMORY_FIELDS.values()}
    for line in read_field(proc / "meminfo").splitlines():
        key, _, value = line.partition(":")
        if key in MEMORY_FIELDS and (match := re.fullmatch(r"\s*(\d+)\s+kB", value)):
            result[MEMORY_FIELDS[key]] = int(match[1])
    return result


def thermal_info(sys_root):
    sensors = []
    for chip in sorted((sys_root / "class/hwmon").glob("hwmon[0-9]*"))[:16]:
        driver = read_field(chip / "name", 64)
        # applesmc registers a bare hwmon class device. The driver's name
        # and sensors are attributes of its platform device on Linux 6.18.
        if not driver and read_field(chip / "device/name", 64) == "applesmc":
            chip = chip / "device"
            driver = "applesmc"
        if driver not in HWMON_DRIVERS:
            continue
        for path in sorted(chip.glob("*_input")):
            match = re.fullmatch(r"(temp|fan)([0-9]{1,3})_input", path.name)
            if not match:
                continue
            channel = f"{match[1]}{match[2]}"
            label = (allowed(read_field(chip / f"{channel}_label", 64), SAFE_LABEL)
                     if driver != "applesmc" or match[1] == "temp" else None)
            # SMC temperature labels are cached. Sample only relevant keys
            # instead of polling the controller for every exposed reading.
            if driver == "applesmc" and match[1] == "temp" and label not in SMC_TEMPERATURES:
                continue
            quantity = "temperature_c" if match[1] == "temp" else "fan_rpm"
            value = (measure(path, lower=-40_000, upper=150_000, divisor=1000)
                     if match[1] == "temp" else measure(path, upper=50_000))
            if value is not None:
                sensors.append({"driver": driver, "channel": channel, "label": label, quantity: value})
    return sensors


def thermal_throttle_info(sys_root):
    result = {}
    for path in sorted((sys_root / "devices/system/cpu").glob("cpu[0-9]*/thermal_throttle"))[:32]:
        events = measure(path / "core_throttle_count")
        if events is not None and re.fullmatch(r"cpu[0-9]{1,4}", path.parent.name):
            result[path.parent.name] = {"core_events": events}
    return result


def cpu_frequency(sys_root):
    result = {}
    for policy in sorted((sys_root / "devices/system/cpu/cpufreq").glob("policy[0-9]*"))[:32]:
        if not re.fullmatch(r"policy[0-9]+", policy.name):
            continue
        result[policy.name] = {
            "current_khz": measure(policy / "scaling_cur_freq", lower=1),
            "governor": allowed(read_field(policy / "scaling_governor", 64), r"[a-zA-Z0-9_-]{1,48}"),
            "driver": allowed(read_field(policy / "scaling_driver", 64), r"[a-zA-Z0-9_-]{1,48}"),
        }
    return result


def power_info(sys_root):
    packages = {}
    powercap = sys_root / "class/powercap"
    for path in list(powercap.glob("intel-rapl:[0-9]*")) + list(powercap.glob("intel-rapl/intel-rapl:[0-9]*")):
        name = allowed(read_field(path / "name", 64), r"package-[0-9]{1,3}")
        energy = measure(path / "energy_uj")
        if name and energy is not None:
            packages[name] = {"energy_uj": energy, "range_uj": measure(path / "max_energy_range_uj", lower=1)}
    batteries = []
    mains = set()
    for path in sorted((sys_root / "class/power_supply").glob("*"))[:16]:
        kind = read_field(path / "type", 64)
        if kind == "Mains":
            mains.add(measure(path / "online", upper=1))
        if (kind != "Battery" or read_field(path / "scope", 64) == "Device"
                or measure(path / "present", upper=1) == 0):
            continue
        watts = measure(path / "power_now", lower=-500_000_000, upper=500_000_000, divisor=1_000_000)
        if watts is not None:
            watts = abs(watts)
        else:
            current = measure(path / "current_now", lower=-100_000_000, upper=100_000_000)
            voltage = measure(path / "voltage_now")
            if current is not None and voltage is not None:
                watts = round(abs(current) * voltage / 1_000_000_000_000, 3)
        status = read_field(path / "status", 64)
        batteries.append({
            "charge_percent": measure(path / "capacity", upper=100),
            "status": status if status in {"Charging", "Discharging", "Full", "Not charging"} else None,
            "watts": watts,
        })
    return {"mains_online": True if 1 in mains else False if mains == {0} else None,
            "batteries": batteries, "packages": packages}


def wifi_station_signal(interface):
    if not allowed(interface, r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}"):
        return None
    try:
        result = subprocess.run(
            ["/usr/sbin/iw", "dev", interface, "station", "dump"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", timeout=0.5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None

    # A managed interface normally has one AP. Do not choose between peers
    # or record addresses, per-chain readings or any other command output.
    stations = 0
    signals = []
    for line in result.stdout.splitlines():
        if line.startswith("Station "):
            stations += 1
        if match := re.fullmatch(r"\s*signal:\s*(-[0-9]{1,3})(?:\s+\[[0-9, -]+\])?\s+dBm\s*", line):
            signals.append(int(match[1]))
    return signals[0] if stations == 1 and len(signals) == 1 and -128 <= signals[0] < 0 else None


def network_info(proc, sys_root):
    signals = {}
    for line in read_field(proc / "net/wireless").splitlines():
        link, _, values = line.partition(":")
        cols = values.split()
        if len(cols) >= 3 and re.fullmatch(r"-\d{1,3}\.?", cols[2]):
            signal = int(cols[2].rstrip("."))
            if -128 <= signal < 0:
                signals[link.strip()] = signal

    # Never return kernel interface names: a name such as wlx… can contain a MAC address.
    index = {"wifi": 0, "ethernet": 0}
    links = {}
    station_queries = 0
    for path in sorted((sys_root / "class/net").glob("*"))[:32]:
        if not (path / "device").exists():
            continue
        wifi = (path / "wireless").is_dir() or (path / "phy80211").exists() or path.name in signals
        kind = "wifi" if wifi else "ethernet"
        alias = f"{kind}{index[kind]}"
        index[kind] += 1
        carrier = measure(path / "carrier", upper=1)
        if carrier is None:
            # Linux returns EINVAL for the carrier attribute when the interface is down.
            state = read_field(path / "operstate", 32)
            if state == "up":
                carrier = 1
            elif state in {"down", "lowerlayerdown", "notpresent"}:
                carrier = 0
        signal = signals.get(path.name) if wifi and carrier != 0 else None
        if wifi and carrier == 1 and signal is None and station_queries < 2:
            station_queries += 1
            signal = wifi_station_signal(path.name)
        stats = path / "statistics"
        links[alias] = {
            "connected": bool(carrier) if carrier is not None else None,
            "signal_dbm": signal,
            **{key: measure(stats / key) for key in ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors")},
        }
        if not wifi:
            links[alias]["speed_mbps"] = measure(path / "speed", lower=1) if carrier == 1 else None
            links[alias]["duplex"] = allowed(read_field(path / "duplex", 16), r"half|full") if carrier == 1 else None
    return links


def disk_counters(proc):
    disks = {}
    for line in read_field(proc / "diskstats").splitlines():
        row = line.split()
        if len(row) < 14 or not re.fullmatch(DISK, row[2]):
            continue
        sectors_in, sectors_out, busy_ms = (number(row[index]) for index in (5, 9, 12))
        if all(value is not None and value >= 0 for value in (sectors_in, sectors_out, busy_ms)):
            # Linux counts sectors here in units of 512 bytes, including on a 4Kn drive.
            disks[row[2]] = {"read_bytes": sectors_in * 512, "write_bytes": sectors_out * 512, "busy_ms": busy_ms}
    return disks


def snapshot(root, elapsed):
    proc = root / "proc"
    sys_root = root / "sys"
    return {
        "elapsed_seconds": elapsed,
        "cpu": cpu_counters(proc),
        "memory": memory_info(proc),
        "thermal": thermal_info(sys_root),
        "thermal_throttle": thermal_throttle_info(sys_root),
        "cpu_frequency": cpu_frequency(sys_root),
        "lid_closed": lid_closed(proc),
        "backlights": backlight_info(sys_root),
        "power": power_info(sys_root),
        "network": network_info(proc, sys_root),
        "disk": disk_counters(proc),
    }


def changes(counters, key, rollover=None):
    if len(counters) < 2 or any(counter.get(key) is None for counter in counters):
        return None
    total = 0
    for before, after in zip(counters, counters[1:]):
        change = after[key] - before[key]
        if change < 0:
            limit = before.get(rollover)
            if not limit or limit != after.get(rollover) or max(before[key], after[key]) >= limit:
                return None
            change += limit
        total += change
    return total


def per_device_deltas(samples, section, keys):
    devices = set.intersection(*(set(sample[section]) for sample in samples))
    return {device: {key: changes([sample[section][device] for sample in samples], key) for key in keys}
            for device in sorted(devices)}


def summarize(samples):
    cpu = [sample["cpu"] for sample in samples]
    ticks = changes(cpu, "ticks")
    idle = changes(cpu, "idle_ticks")
    iowait = changes(cpu, "iowait_ticks")
    cpu_valid = ticks and idle is not None and iowait is not None and ticks >= idle + iowait
    available_values = [sample["memory"]["available_kib"] for sample in samples
                        if sample["memory"]["available_kib"] is not None]
    frequencies = [policy["current_khz"] for sample in samples for policy in sample["cpu_frequency"].values()
                   if policy["current_khz"] is not None]
    temperatures = [sensor["temperature_c"] for sample in samples for sensor in sample["thermal"]
                    if "temperature_c" in sensor]
    cpu_temperatures = [sensor["temperature_c"] for sample in samples for sensor in sample["thermal"]
                        if sensor["driver"] == "coretemp" and "temperature_c" in sensor]
    throttle_deltas = per_device_deltas(samples, "thermal_throttle", ("core_events",))
    throttle_events = [delta["core_events"] for delta in throttle_deltas.values() if delta["core_events"] is not None]
    fans = [sensor["fan_rpm"] for sample in samples for sensor in sample["thermal"] if "fan_rpm" in sensor]
    packages = [sample["power"]["packages"] for sample in samples]
    elapsed = samples[-1]["elapsed_seconds"] - samples[0]["elapsed_seconds"]
    package_energy = [changes([sample[name] for sample in packages], "energy_uj", rollover="range_uj")
                      for name in set.intersection(*(set(sample) for sample in packages))]
    battery_draws = []
    for sample in samples:
        draws = [battery["watts"] for battery in sample["power"]["batteries"] if battery["status"] == "Discharging"]
        if draws and None not in draws:
            battery_draws.append(sum(draws))
    wifi_links = [link for sample in samples for alias, link in sample["network"].items() if alias.startswith("wifi")]
    wifi_carrier = [link["connected"] for link in wifi_links if link["connected"] is not None]
    wifi_signals = [link["signal_dbm"] for link in wifi_links
                    if link["signal_dbm"] is not None and link["connected"] is not False]
    return {
        "elapsed_seconds": elapsed,
        "cpu_busy_percent": round(100 * (ticks - idle - iowait) / ticks, 2) if cpu_valid else None,
        "cpu_iowait_percent": round(100 * iowait / ticks, 2) if cpu_valid else None,
        "memory_available_min_kib": min(available_values, default=None),
        "cpu_frequency_mean_khz": round(sum(frequencies) / len(frequencies)) if frequencies else None,
        "temperature_max_c": max(temperatures, default=None),
        "cpu_temperature_max_c": max(cpu_temperatures, default=None),
        # Logical CPUs can report the same hardware event. Do not sum them.
        "cpu_core_throttle_max_events": max(throttle_events, default=None),
        "fan_max_rpm": max(fans, default=None),
        "package_power_mean_watts": (round(sum(package_energy) / (1_000_000 * elapsed), 3)
                                     if elapsed and package_energy and None not in package_energy else None),
        "battery_discharge_mean_watts": round(sum(battery_draws) / len(battery_draws), 3) if battery_draws else None,
        "wifi_signal_mean_dbm": round(sum(wifi_signals) / len(wifi_signals), 2) if wifi_signals else None,
        "wifi_connected_percent": round(100 * sum(wifi_carrier) / len(wifi_carrier), 2) if wifi_carrier else None,
        "network": per_device_deltas(samples, "network", ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors")),
        "disk": per_device_deltas(samples, "disk", ("read_bytes", "write_bytes", "busy_ms")),
    }


def collect(seconds, interval, *, root=Path("/"), clock=time.monotonic, sleep=time.sleep,
            services=read_services):
    if not 0 <= seconds <= MAX_SECONDS or not 1 <= interval <= MAX_INTERVAL:
        raise ValueError("Sampling duration or interval out of range.")
    system = system_info(root)
    service_start = services(root / "proc")
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    start = clock()
    samples = [snapshot(root, 0.0)]
    for offset in range(interval, seconds + interval, interval):
        remaining = start + min(offset, seconds) - clock()
        if remaining > 0:
            sleep(remaining)
        elapsed = clock() - start
        samples.append(snapshot(root, round(elapsed, 3)))
        if elapsed >= seconds:
            break
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at,
        "sampling": {"seconds": seconds, "interval": interval},
        "system": system,
        "services": {"start": service_start, "end": services(root / "proc") if seconds else service_start},
        "samples": samples,
        "window": summarize(samples),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-metrics", description=__doc__, allow_abbrev=False)
    parser.add_argument("--seconds", type=int, default=0, help="time to sample, up to 300 seconds")
    parser.add_argument("--interval", type=int, default=5, help="seconds between samples, from 1 to 60")
    args = parser.parse_args(argv)
    if not 0 <= args.seconds <= MAX_SECONDS or not 1 <= args.interval <= MAX_INTERVAL:
        parser.error(f"--seconds must be 0–{MAX_SECONDS} and --interval must be 1–{MAX_INTERVAL}.")
    if sys.platform != "linux":
        parser.error("This collector reads Linux hardware interfaces and must run on the PipOS device.")
    print(json.dumps(collect(args.seconds, args.interval), allow_nan=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
