"""One passive overnight hardware reading. The database holds no conversation."""

import asyncio
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from datetime import date
from pathlib import Path
from statistics import median

from . import hardware_metrics as metrics
from .app_monitor import Sampler
from .cortex import CortexError

FILE = "body.db"
WINDOW = 30.0
MAX_NIGHTS = 90
RECENT_NIGHTS = 7
REFERENCE_NIGHTS = 28
TREND_CONFIRMATIONS = 3
POWER_CHANGE_WATTS = 2.0
ACTIVE_PROBES = 8
DISK_PROBES = 10
ACTIVE_TIMEOUT = 1.5
ACTIVE_CANCEL_SETTLE = 2.0
ACTIVE_TREND_SHARE = 0.20
ACTIVE_HEAT_CHANGE_C = 6.0
BUILD = "etc/being/build-id"
BODY = "sys/class/dmi/id/product_name"
RTC_SECONDS = "sys/class/rtc/rtc0/since_epoch"
SMART_WEEK_DAYS = 7
SMART_MAX_PACKAGE_C = 75
SMART_OUTPUT_LIMIT = 64 * 1024
SMART_IDS = frozenset((5, 187, 199, 241))
SMART_WEAR_IDS = frozenset((173, 177, 202, 231, 233))
SMART_WEAR_NAME = re.compile(r"wear|life[_ -]?left|percent(?:age)?[_ -]?used", re.I)
SMARTCTL = "/usr/sbin/smartctl"


def hardware_epoch(root=Path("/")):
    body = metrics.allowed(metrics.read_field(root / BODY, 96), metrics.SAFE_LABEL) or "unknown"
    build = metrics.allowed(metrics.read_field(root / BUILD, 96), r"[a-zA-Z0-9._-]{1,96}") or "unknown"
    return body, build


def _package(root, clock):
    sampled = clock()
    packages = metrics.power_info(root / "sys", batteries=False)["packages"]
    return sampled, packages


def _rtc_offset(root, wall=time.time):
    """RTC minus system time, rounded to the RTC's whole-second precision."""
    before = wall()
    rtc = metrics.measure(root / RTC_SECONDS, upper=10_000_000_000)
    after = wall()
    if rtc is None or not 0 <= after - before <= 1:
        return None
    return round(rtc - (before + after) / 2)


def _archive_drive_writes(root, state_dir):
    """Read the parent disk of the mounted archive, without querying its serial."""
    boot = metrics.allowed(metrics.read_field(root / "proc/sys/kernel/random/boot_id", 48),
                           r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
    if boot is None:
        return None
    try:
        device = state_dir.stat().st_dev
        block = (root / f"sys/dev/block/{os.major(device)}:{os.minor(device)}").resolve(strict=True)
        if (block / "partition").is_file():
            block = block.parent
        device_number = metrics.allowed(metrics.read_field(block / "dev", 24), r"[0-9]+:[0-9]+")
    except (OSError, RuntimeError):
        return None
    # Stacked devices can re-map writes. The prototype only measures the
    # directly mounted, kernel-accounted disk.
    if device_number is None or (block / "dm").exists() or (block / "md").exists():
        return None
    fields = metrics.read_field(block / "stat", 384).split()
    written = metrics.number(fields[6]) if len(fields) >= 11 else None
    if written is None or written < 0:
        return None
    return {"boot": boot, "device": device_number, "bytes": written * 512}


def _archive_sata_disk(root, state_dir):
    """The direct SATA disk holding the mounted archive, without a raw identifier."""
    try:
        if not state_dir.is_mount():
            return None
        mounted = state_dir.stat().st_dev
        block = (root / f"sys/dev/block/{os.major(mounted)}:{os.minor(mounted)}").resolve(strict=True)
        if (block / "partition").is_file():
            block = block.parent
        device_number = metrics.allowed(metrics.read_field(block / "dev", 24), r"[0-9]+:[0-9]+")
        device_path = root / "dev" / block.name
        device = device_path.stat()
        ancestry = str((block / "device").resolve(strict=True))
        holders = tuple((block / "holders").iterdir())
        slaves = tuple((block / "slaves").iterdir())
        removable = metrics.read_field(block / "removable", 8).strip()
        rotational = metrics.read_field(block / "queue/rotational", 8).strip()
    except (OSError, RuntimeError):
        return None
    if (not re.fullmatch(r"sd[a-z]+", block.name) or "/ata" not in ancestry or "/usb" in ancestry
            or removable != "0" or rotational != "0"
            or device_number != f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
            or not stat.S_ISBLK(device.st_mode) or holders or slaves):
        return None
    runtime = metrics.read_field(block / "device/power/runtime_status", 24).strip()
    if runtime in ("suspended", "suspending"):
        return None
    raw_identity = (metrics.read_field(block / "wwid", 256).strip()
                    or metrics.read_field(block / "device/wwid", 256).strip())
    if not raw_identity or len(raw_identity) > 240:
        return None
    identity = hashlib.sha256(raw_identity.encode("utf-8", "strict")).hexdigest()
    return device_path, device_number, identity


def _smart_due(state_dir, night):
    try:
        previous = latest_smart(state_dir)
    except (OSError, sqlite3.Error, ValueError):
        return False
    if previous is None:
        return True
    try:
        return (date.fromisoformat(night) - date.fromisoformat(previous[0])).days >= SMART_WEEK_DAYS
    except (TypeError, ValueError):
        return False


async def _stop_process(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), 0.5)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            pass


async def _smart_output(device_path, eligible):
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        SMARTCTL, "-j", "-n", "standby", "-H", "-A", str(device_path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        try:
            await _stop_process(await launch)
        except OSError:
            pass
        raise
    except OSError:
        return None
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        while True:
            if not eligible() or time.monotonic() >= deadline:
                await _stop_process(process)
                return None
            try:
                block = await asyncio.wait_for(process.stdout.read(8192), 0.25)
            except asyncio.TimeoutError:
                continue
            if not block:
                break
            output.extend(block)
            if len(output) > SMART_OUTPUT_LIMIT:
                await _stop_process(process)
                return None
        while process.returncode is None:
            if not eligible() or time.monotonic() >= deadline:
                await _stop_process(process)
                return None
            try:
                await asyncio.wait_for(process.wait(), 0.25)
            except asyncio.TimeoutError:
                continue
        returncode = process.returncode
    except BaseException:
        await _stop_process(process)
        raise
    # Bits 0-2 mean command/device/SMART-command failure. `-n standby`
    # deliberately returns bit 1 (status 2) when the disk is asleep. Bits
    # 3-7 are health/attribute/log findings: retain their JSON evidence.
    if returncode & 0x07 or not output:
        return None
    return bytes(output)


async def _smart_sample(root, state_dir, eligible):
    disk = await asyncio.to_thread(_archive_sata_disk, root, state_dir)
    if disk is None or not eligible():
        return None
    device_path, device_number, identity = disk
    output = await _smart_output(device_path, eligible)
    if output is None:
        return None
    # smartctl accepts a path rather than an already pinned block-device FD.
    # Re-admit it after the bounded child exits so a remove/re-enumerate race
    # cannot attach the result to a different archive disk.
    confirmed = await asyncio.to_thread(_archive_sata_disk, root, state_dir)
    if confirmed != (device_path, device_number, identity) or not eligible():
        return None
    try:
        answer = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(answer, dict):
        return None
    ata = answer.get("ata_smart_attributes")
    table = ata.get("table") if isinstance(ata, dict) else None
    if not isinstance(table, list):
        return None
    attributes = {}
    wear_count = 0
    for entry in table[:256]:
        if not isinstance(entry, dict) or type(entry.get("id")) is not int:
            continue
        attribute = entry["id"]
        name = entry.get("name")
        wear = attribute in SMART_WEAR_IDS or (isinstance(name, str) and SMART_WEAR_NAME.search(name) is not None)
        if attribute not in SMART_IDS and not wear:
            continue
        if wear and attribute not in SMART_IDS:
            if wear_count >= 8:
                continue
            wear_count += 1
        raw = entry.get("raw", {}).get("value") if isinstance(entry.get("raw"), dict) else None
        normalized = entry.get("value")
        if (type(raw) is int and 0 <= raw < 1 << 63
                and type(normalized) is int and 0 <= normalized <= 255):
            attributes[str(attribute)] = {"raw": raw, "normalized": normalized, "wear": wear}
    passed = answer.get("smart_status", {}).get("passed") if isinstance(answer.get("smart_status"), dict) else None
    if not attributes and type(passed) is not bool:
        return None
    return {"device": device_number, "identity": identity, "passed": passed if type(passed) is bool else None,
            "attributes": attributes}


def _watts(before, after):
    then, old = before
    now, current = after
    seconds = now - then
    if seconds < WINDOW - 1 or seconds > 2 * WINDOW:
        return None
    total = 0
    count = 0
    for key, ending in current.items():
        if key not in old:
            continue
        start, end = old[key]["energy_uj"], ending["energy_uj"]
        span = ending["range_uj"]
        delta = end - start
        if delta < 0 and span is not None and span == old[key]["range_uj"]:
            delta += span
        if 0 <= delta <= seconds * 200_000_000:
            total += delta
            count += 1
    return round(total / (seconds * 1_000_000), 2) if count else None


def _details(root, state_dir):
    reading = Sampler(root=root, disk=state_dir).sample()
    sys_root = root / "sys"
    supplies = metrics.power_info(sys_root)
    battery = supplies["batteries"][0] if supplies["batteries"] else {}
    devices = {}
    for name in ("face", "brainstem", "voice", "mind", "cortex", "hands"):
        path = sys_root / f"fs/cgroup/system.slice/being-{name}.service/memory.current"
        amount = metrics.measure(path, upper=128 * (1 << 30))
        if amount is not None:
            devices[name] = amount
    return {
        "package_c": reading["package_c"],
        "fan_rpm": [rpm for rpm, _ in reading["fans"]],
        "free_bytes": reading["disk"][1] if reading["disk"] else None,
        "battery_health": battery.get("health_percent"),
        "battery_cycles": battery.get("cycles"),
        "service_memory_bytes": devices,
    }


def _sample_watts(detail):
    if isinstance(detail, dict):
        power, seconds = detail.get("package_watts"), detail.get("seconds")
        if (type(power) in (int, float) and 0 <= power <= 200
                and type(seconds) in (int, float) and WINDOW - 1 <= seconds <= 2 * WINDOW):
            return power
    return None


def _record_baseline(db, body, build):
    if "unknown" in (body, build):
        return
    # Additive to v1 so a previous build can still read its overnight data.
    db.execute("""CREATE TABLE IF NOT EXISTS power_baselines (
        body TEXT NOT NULL, build TEXT NOT NULL, through_night TEXT NOT NULL,
        watts REAL NOT NULL, PRIMARY KEY(body, build))""")
    if db.execute("SELECT 1 FROM power_baselines WHERE body=? AND build=?", (body, build)).fetchone():
        return
    readings = db.execute(
        "SELECT night, detail FROM readings WHERE body=? AND build=? ORDER BY night", (body, build))
    watts = []
    for night, detail in readings:
        try:
            power = _sample_watts(json.loads(detail))
        except ValueError:
            continue
        if power is not None:
            watts.append(power)
        if len(watts) == RECENT_NIGHTS:
            db.execute("INSERT INTO power_baselines VALUES (?, ?, ?, ?)", (body, build, night, median(watts)))
            db.execute("""DELETE FROM power_baselines WHERE rowid NOT IN (
                SELECT rowid FROM power_baselines ORDER BY through_night DESC, rowid DESC LIMIT ?)""",
                       (MAX_NIGHTS,))
            return


def _commit(root, night, body, build, reading):
    path = root / FILE
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=0.15)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version, = db.execute("PRAGMA user_version").fetchone()
        if version == 0:
            db.execute("""CREATE TABLE IF NOT EXISTS readings (
                body TEXT NOT NULL, build TEXT NOT NULL, night TEXT NOT NULL,
                detail TEXT NOT NULL, PRIMARY KEY(body, build, night))""")
            db.execute("PRAGMA user_version=1")
        elif version != 1:
            return
        disk = reading.get("archive_drive")
        if disk is not None:
            previous = db.execute(
                "SELECT night, detail FROM readings WHERE body=? AND build=? AND night<? "
                "ORDER BY night DESC LIMIT 1", (body, build, night)).fetchone()
            if previous is not None:
                try:
                    prior = json.loads(previous[1]).get("archive_drive", {})
                    before = prior.get("bytes")
                    days = (date.fromisoformat(night) - date.fromisoformat(previous[0])).days
                    if (days == 1 and prior.get("boot") == disk["boot"] and prior.get("device") == disk["device"]
                            and type(before) is int and 0 <= before <= disk["bytes"]):
                        reading["archive_drive_daily_write_bytes"] = disk["bytes"] - before
                except (ValueError, TypeError, AttributeError):
                    pass  # an old or damaged sample cannot prevent tonight's observation
        smart = reading.get("smart")
        if isinstance(smart, dict):
            previous = db.execute(
                "SELECT night, detail FROM readings WHERE night<? ORDER BY night DESC, rowid DESC",
                (night,)).fetchall()
            for prior_night, encoded in previous:
                try:
                    prior = json.loads(encoded).get("smart")
                except (ValueError, TypeError, AttributeError):
                    continue
                if not isinstance(prior, dict):
                    continue
                try:
                    days = (date.fromisoformat(night) - date.fromisoformat(prior_night)).days
                except (TypeError, ValueError):
                    break
                # dev_t validates the live path at each sample but may change
                # across boots. The opaque WWID digest is the stable same-disk
                # receipt used for deltas.
                if not 1 <= days <= 35 or prior.get("identity") != smart.get("identity"):
                    break
                changes = {}
                old_attributes = prior.get("attributes", {})
                for key, current in smart.get("attributes", {}).items():
                    before = old_attributes.get(key) if isinstance(old_attributes, dict) else None
                    if not isinstance(current, dict) or not isinstance(before, dict):
                        continue
                    change = {}
                    old_raw, new_raw = before.get("raw"), current.get("raw")
                    if type(old_raw) is int and type(new_raw) is int and 0 <= old_raw <= new_raw:
                        change["raw"] = new_raw - old_raw
                    old_normal, new_normal = before.get("normalized"), current.get("normalized")
                    if (current.get("wear") is True
                            and type(old_normal) is int and type(new_normal) is int):
                        change["normalized"] = new_normal - old_normal
                    if change:
                        changes[key] = change
                if changes:
                    reading["smart_delta"] = {"days": days, "attributes": changes}
                break
        db.execute("INSERT OR REPLACE INTO readings VALUES (?, ?, ?, ?)",
                   (body, build, night, json.dumps(reading, sort_keys=True)))
        _record_baseline(db, body, build)
        db.execute("""DELETE FROM readings WHERE rowid NOT IN (
            SELECT rowid FROM readings ORDER BY night DESC, rowid DESC LIMIT ?)""", (MAX_NIGHTS,))


def _merge_active(root, night, body, build, active):
    """Add an all-numeric active result to tonight's passive row."""
    path = root / FILE
    if not path.is_file() or path.is_symlink():
        return False
    with closing(sqlite3.connect(path, timeout=0.15)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT detail FROM readings WHERE body=? AND build=? AND night=?",
                         (body, build, night)).fetchone()
        if row is None:
            return False
        detail = json.loads(row[0])
        if not isinstance(detail, dict):
            return False
        detail["active"] = active
        db.execute("UPDATE readings SET detail=? WHERE body=? AND build=? AND night=?",
                   (json.dumps(detail, sort_keys=True), body, build, night))
    return True


def _disk_write(root):
    """One small durable write in the mind, with no owner data."""
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    fd, name = tempfile.mkstemp(prefix=".vital-", dir=root)
    try:
        os.write(fd, b"PipOS active vital sign\n".ljust(4096, b"\0"))
        os.fsync(fd)
    finally:
        os.close(fd)
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
    return time.monotonic() - started


def _energy_uj(before, after):
    """Package-energy delta across one short active probe."""
    _, old = before
    _, current = after
    total = 0
    found = False
    for key, ending in current.items():
        if key not in old:
            continue
        start, end, span = old[key]["energy_uj"], ending["energy_uj"], ending["range_uj"]
        delta = end - start
        if delta < 0 and span is not None and span == old[key]["range_uj"]:
            delta += span
        if 0 <= delta <= 300_000_000:
            total += delta
            found = True
    return total if found else None


class ActiveVitalJob:
    """Bounded active inference and durable-write probes on the spare model slot.

    One operation runs per night-shift chunk. Cancellation closes its HTTP
    request, and no partial result reaches body.db.
    """

    READ = ("Measure this fixed local reading workload without using its meaning: "
            "amber birch copper delta ember forest granite harbor ivory juniper kettle linen "
            "meadow nickel ochre paper quartz river silver timber umber velvet willow xylem yellow zinc.")
    WRITE = "List ordinary objects as plain lowercase words without punctuation."

    def __init__(self, root, night, cortex, *, eligible, available=lambda: True,
                 hardware=Path("/"), clock=time.monotonic):
        from .cortex import Cortex

        self.state_dir, self.root, self.night = Path(root), Path(hardware), night
        self.eligible, self.available, self.clock = eligible, available, clock
        self.local = cortex.llama and cortex.host in ("127.0.0.1", "::1", "localhost")
        # Slot 1 is the workshop slot. Active vitals never evict conversation.
        base = f"http://{cortex.host}:{cortex.port}{cortex.path}"
        self.cortex = Cortex(base, model=cortex.model, llama=cortex.llama,
                             timeout=ACTIVE_TIMEOUT, slot_id=1)
        self.reads, self.writes, self.disk, self.energy = [], [], [], []
        self.read_attempts = self.write_attempts = 0
        self.temperatures = []
        self.cooldown_until = 0.0

    @property
    def cursor(self):
        return {"reads": self.reads, "writes": self.writes, "disk": self.disk,
                "energy": self.energy, "temperatures": self.temperatures,
                "read_attempts": self.read_attempts, "write_attempts": self.write_attempts}

    def resume(self, cursor):
        if not isinstance(cursor, dict):
            return
        for name, limit in (("reads", ACTIVE_PROBES), ("writes", ACTIVE_PROBES),
                            ("disk", DISK_PROBES), ("energy", 2 * ACTIVE_PROBES)):
            values = cursor.get(name)
            if isinstance(values, list):
                setattr(self, name, [value for value in values[:limit]
                                     if type(value) in (int, float) and value >= 0])
        values = cursor.get("temperatures")
        if isinstance(values, list):
            self.temperatures = [value for value in values[:2 * ACTIVE_PROBES + DISK_PROBES]
                                 if type(value) in (int, float) and -50 <= value <= 150]
        for name in ("read_attempts", "write_attempts"):
            value = cursor.get(name)
            if type(value) is int and 0 <= value <= ACTIVE_PROBES:
                setattr(self, name, value)

    async def _temperature(self):
        reading = await asyncio.to_thread(lambda: Sampler(root=self.root, disk=self.state_dir).sample()["package_c"])
        if type(reading) in (int, float):
            self.temperatures.append(round(reading, 1))

    async def _model_probe(self, writing):
        prompt = self.WRITE if writing else self.READ
        before = await asyncio.to_thread(_package, self.root, self.clock)
        payload = {"prompt": prompt, "n_predict": 16 if writing else 1, "temperature": 0,
                   "cache_prompt": False, "id_slot": 1, "stream": False}
        async with asyncio.timeout(ACTIVE_TIMEOUT):
            response = await self.cortex._cancellable_request("POST", "/completion", payload)
        after = await asyncio.to_thread(_package, self.root, self.clock)
        timings = response.get("timings") if isinstance(response, dict) else {}
        timings = timings if isinstance(timings, dict) else {}
        key = "predicted_per_second" if writing else "prompt_per_second"
        speed = timings.get(key)
        tokens = timings.get("predicted_n") if writing else timings.get("prompt_n")
        if type(speed) in (int, float) and speed > 0:
            (self.writes if writing else self.reads).append(round(speed, 2))
        used = _energy_uj(before, after)
        if used is not None and type(tokens) in (int, float) and tokens > 0:
            self.energy.append(round(used / 1_000_000 / tokens, 6))
        await self._temperature()

    async def run_chunk(self, _budget):
        if not self.local:
            return True
        if not self.eligible():
            return False
        if not self.available():
            await asyncio.sleep(_budget)
            return False
        remaining = self.cooldown_until - self.clock()
        if remaining > 0:
            await asyncio.sleep(min(_budget, remaining))
            return False
        if self.read_attempts < ACTIVE_PROBES:
            if not self.temperatures:
                await self._temperature()
            try:
                await self._model_probe(False)
            except (TimeoutError, CortexError):
                self.read_attempts += 1
                self.cooldown_until = self.clock() + ACTIVE_CANCEL_SETTLE
                return False
            self.read_attempts += 1
            return False
        if self.write_attempts < ACTIVE_PROBES:
            try:
                await self._model_probe(True)
            except (TimeoutError, CortexError):
                self.write_attempts += 1
                self.cooldown_until = self.clock() + ACTIVE_CANCEL_SETTLE
                return False
            self.write_attempts += 1
            return False
        if len(self.disk) < DISK_PROBES:
            elapsed = await asyncio.to_thread(_disk_write, self.state_dir)
            self.disk.append(round(elapsed, 6))
            await self._temperature()
            return False
        if not self.eligible():
            return False
        body, build = hardware_epoch(self.root)
        active = {
            "read_tps": median(self.reads) if self.reads else None,
            "write_tps": median(self.writes) if self.writes else None,
            "disk_sync_ms": round(1000 * median(self.disk), 2),
            "joules_per_token": median(self.energy) if self.energy else None,
            "start_c": self.temperatures[0] if self.temperatures else None,
            "peak_c": max(self.temperatures) if self.temperatures else None,
            "end_c": self.temperatures[-1] if self.temperatures else None,
            "read_samples": len(self.reads), "write_samples": len(self.writes),
            "disk_samples": len(self.disk),
        }
        await asyncio.to_thread(_merge_active, self.state_dir, self.night, body, build, active)
        # If passive package counters were unavailable there is deliberately
        # no row to extend; do not repeat an active workload all night.
        return True

    def interrupted(self):
        # Request cancellation closes its HTTP socket. Cursor state keeps
        # only whole measurements and the next quiet spell resumes here.
        pass


class VitalJob:
    """Wait in short cancellable chunks between the two package energy readings."""

    def __init__(self, root, night, *, eligible, clock_trusted=lambda: False,
                 hardware=Path("/"), clock=time.monotonic):
        self.state_dir = Path(root)
        self.root = Path(hardware)
        self.night = night
        self.clock = clock
        self.eligible = eligible
        self.clock_trusted = clock_trusted
        self.start = None

    async def run_chunk(self, budget):
        if self.start is None:
            self.start = await asyncio.to_thread(_package, self.root, self.clock)
            if not self.start[1]:
                return True
        remaining = WINDOW - (self.clock() - self.start[0])
        if remaining > 0:
            await asyncio.sleep(min(budget, remaining))
            return False
        ending = await asyncio.to_thread(_package, self.root, self.clock)
        watts = _watts(self.start, ending)
        if watts is None:
            return True
        detail = await asyncio.to_thread(_details, self.root, self.state_dir)
        writes = await asyncio.to_thread(_archive_drive_writes, self.root, self.state_dir)
        if writes is not None:
            detail["archive_drive"] = writes
        package_c = detail.get("package_c")
        if (self.clock_trusted() and self.eligible() and _smart_due(self.state_dir, self.night)
                and type(package_c) in (int, float) and package_c < SMART_MAX_PACKAGE_C):
            smart = await _smart_sample(self.root, self.state_dir, self.eligible)
            if smart is not None and self.eligible():
                detail["smart"] = smart
        if self.clock_trusted():
            rtc_offset = await asyncio.to_thread(_rtc_offset, self.root)
            if rtc_offset is not None and self.clock_trusted():
                detail["rtc_offset_seconds"] = rtc_offset
        # A body event can change the night gate while the hardware reads
        # are in flight. Recheck before persisting or counting this window.
        if not self.eligible():
            self.interrupted()
            return False
        body, build = hardware_epoch(self.root)
        seconds = round(ending[0] - self.start[0], 1)
        _commit(self.state_dir, self.night, body, build, {**detail, "package_watts": watts, "seconds": seconds})
        return True

    def interrupted(self):
        self.start = None


def latest(root):
    path = Path(root) / FILE
    if not path.is_file() or path.is_symlink():
        return None
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.15)) as db:
        row = db.execute("SELECT night, detail FROM readings ORDER BY night DESC, rowid DESC LIMIT 1").fetchone()
        if row:
            detail = json.loads(row[1])
            if isinstance(detail, dict):
                return row[0], detail
    return None


def latest_smart(root):
    """Most recent bounded SMART sample, which may predate the latest nightly row."""
    path = Path(root) / FILE
    if not path.is_file() or path.is_symlink():
        return None
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.15)) as db:
        rows = db.execute("SELECT night, detail FROM readings ORDER BY night DESC, rowid DESC LIMIT 35").fetchall()
    for night, encoded in rows:
        try:
            detail = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        if isinstance(detail, dict) and isinstance(detail.get("smart"), dict):
            return night, detail["smart"], detail.get("smart_delta")
    return None


def package_power_baseline(root, night):
    path = Path(root) / FILE
    if not path.is_file() or path.is_symlink():
        return None
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.15)) as db:
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='power_baselines' AND type='table'").fetchone():
            return None
        row = db.execute(
            """SELECT baseline.watts FROM readings AS reading LEFT JOIN power_baselines AS baseline
               ON baseline.body=reading.body AND baseline.build=reading.build AND baseline.through_night<=reading.night
               WHERE reading.night=? ORDER BY reading.rowid DESC LIMIT 1""", (night,)).fetchone()
    return row[0] if row and type(row[0]) in (int, float) and 0 <= row[0] <= 200 else None


def package_power_trend(root, night, *, expected_epoch=None):
    """Compare seven nights with the previous 28, within one body and build."""
    path = Path(root) / FILE
    if not path.is_file() or path.is_symlink():
        return None
    window = RECENT_NIGHTS + REFERENCE_NIGHTS
    needed = window + TREND_CONFIRMATIONS - 1
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.15)) as db:
        rows = db.execute(
            "SELECT body, build, night, detail FROM readings ORDER BY night DESC, rowid DESC LIMIT ?",
            (needed,),
        ).fetchall()
    if len(rows) != needed or rows[0][2] != night:
        return None

    epoch = rows[0][:2]
    if "unknown" in epoch or (expected_epoch is not None and epoch != expected_epoch):
        return None
    last = date.fromisoformat(night).toordinal()
    watts = []
    for offset, (body, build, label, encoded) in enumerate(rows):
        if (body, build) != epoch or date.fromisoformat(label).toordinal() != last - offset:
            return None
        power = _sample_watts(json.loads(encoded))
        if power is None:
            return None
        watts.append(power)

    differences = [
        median(watts[start:start + RECENT_NIGHTS]) - median(watts[start + RECENT_NIGHTS:start + window])
        for start in range(TREND_CONFIRMATIONS)
    ]
    if all(delta >= POWER_CHANGE_WATTS for delta in differences) or all(
        delta <= -POWER_CHANGE_WATTS for delta in differences
    ):
        return differences[0]
    return None


def _active(detail):
    value = detail.get("active") if isinstance(detail, dict) else None
    if not isinstance(value, dict):
        return None
    fields = {}
    for key in ("read_tps", "write_tps", "disk_sync_ms", "joules_per_token", "peak_c"):
        item = value.get(key)
        if type(item) in (int, float) and item >= 0:
            fields[key] = item
    return fields if {"read_tps", "write_tps", "disk_sync_ms"} <= fields.keys() else None


def active_baseline(root, night):
    """First seven complete active passes in this body/build epoch."""
    path = Path(root) / FILE
    if not path.is_file() or path.is_symlink():
        return None
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.15)) as db:
        epoch = db.execute("SELECT body, build FROM readings WHERE night=?", (night,)).fetchone()
        if epoch is None or "unknown" in epoch:
            return None
        rows = db.execute("SELECT detail FROM readings WHERE body=? AND build=? ORDER BY night", epoch).fetchall()
    samples = [active for (encoded,) in rows if (active := _active(json.loads(encoded))) is not None][:RECENT_NIGHTS]
    if len(samples) != RECENT_NIGHTS:
        return None
    keys = set.intersection(*(set(sample) for sample in samples))
    return {key: median(sample[key] for sample in samples) for key in keys}


def active_trend(root, night, *, expected_epoch=None):
    """Confirmed seven-vs-28 active-workload changes in one body/build."""
    path = Path(root) / FILE
    if not path.is_file() or path.is_symlink():
        return None
    window = RECENT_NIGHTS + REFERENCE_NIGHTS
    needed = window + TREND_CONFIRMATIONS - 1
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.15)) as db:
        rows = db.execute("SELECT body, build, night, detail FROM readings ORDER BY night DESC, rowid DESC LIMIT ?",
                          (needed,)).fetchall()
    if len(rows) != needed or rows[0][2] != night:
        return None
    epoch, last = rows[0][:2], date.fromisoformat(night).toordinal()
    if "unknown" in epoch or (expected_epoch is not None and epoch != expected_epoch):
        return None
    samples = []
    for offset, (body, build, label, encoded) in enumerate(rows):
        if (body, build) != epoch or date.fromisoformat(label).toordinal() != last - offset:
            return None
        sample = _active(json.loads(encoded))
        if sample is None:
            return None
        samples.append(sample)
    changes = {}
    for key in ("read_tps", "write_tps", "disk_sync_ms", "peak_c", "joules_per_token"):
        if not all(key in sample for sample in samples):
            continue
        deltas = []
        for start in range(TREND_CONFIRMATIONS):
            recent = median(sample[key] for sample in samples[start:start + RECENT_NIGHTS])
            older = median(sample[key] for sample in samples[start + RECENT_NIGHTS:start + window])
            deltas.append(recent - older)
        threshold = ACTIVE_HEAT_CHANGE_C if key == "peak_c" else None
        if threshold is not None and (all(d >= threshold for d in deltas) or all(d <= -threshold for d in deltas)):
            changes[key] = deltas[0]
        elif threshold is None:
            reference = median(sample[key] for sample in samples[RECENT_NIGHTS:window])
            if reference > 0 and (all(d >= reference * ACTIVE_TREND_SHARE for d in deltas)
                                  or all(d <= -reference * ACTIVE_TREND_SHARE for d in deltas)):
                changes[key] = deltas[0] / reference
    return changes or None


def erase_all(root):
    (Path(root) / FILE).unlink(missing_ok=True)
