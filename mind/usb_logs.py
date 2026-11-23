"""Diagnostics for a Try boot, written to the Try USB's own PIPLOGS partition.

The laptop Try image carries a small FAT32 partition (GPT name ``try-logs``,
volume label ``PIPLOGS``) after its model partition. On every Try boot
``being-usb-logs run`` mounts it, makes one folder per boot and keeps there:

* ``summary.txt``: the obvious answers first (CPU, GPU driver and renderer,
  audio, lid, battery, camera, Wi-Fi chip and firmware, failed units);
* ``hardware-report.txt``: what /proc and /sys show about the machine;
* ``dmesg.txt`` and ``services.txt``: kernel log and unit states;
* ``journal.txt``: the system journal, copied as it is written and synced
  every few seconds, so a crash or a power cut still leaves most of it.

The partition is only ever looked for on the disk that holds the running
root file system, and only when that disk is attached over USB. Nothing here
opens, mounts or writes any other disk.
"""

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PARTNAME = "try-logs"
LABEL = "PIPLOGS"
MOUNT_POINT = Path("/run/beinglogs")
STATE_DIR = Path("/run/being-usb-logs")
MOUNT_OPTIONS = "rw,nodev,nosuid,noexec,noatime,flush,umask=0022,shortname=mixed,errors=remount-ro"
FOLDER = re.compile(r"\Aboot-(\d{4,})-")
JOURNAL_CAP = 32 * 1024 * 1024        # per boot
LOW_SPACE = JOURNAL_CAP + 8 * 1024 * 1024   # free space each new boot starts with, if pruning can
STOP_SPACE = 4 * 1024 * 1024          # stop copying the journal below this
KEEP_NEWEST = 3                       # this boot and the two before it are never pruned:
                                      # 2 x JOURNAL_CAP on 128 MB still leaves LOW_SPACE
SNAPSHOTS = (0, 20, 90, 300)          # seconds after start
SLOW_SNAPSHOT = 90                     # the one that also lists the SMC keys
LOGGING_OFF = "LOGGING-OFF.txt"        # on PIPLOGS: the owner switched Try diagnostics off
FSYNC_EVERY = 3.0
READ_LIMIT = 256 * 1024
NOTABLE = (
    ("renderer", re.compile(r"face: drawn by")),
    ("firmware", re.compile(r"firmware", re.I)),
    ("wifi", re.compile(r"\b(b43|bcma|brcm|wlan\d|wl\b|cfg80211)", re.I)),
    ("graphics", re.compile(r"\b(i915|drm|crocus|mesa|EGL|kmsdrm)", re.I)),
    ("audio", re.compile(r"\b(snd_hda|hda[-_ ]|ALSA|cs420)", re.I)),
    ("smc", re.compile(r"applesmc", re.I)),
    ("failed", re.compile(r"Failed with result|Failed to start|failed to load|core-dump|segfault|"
                          r"Scheduled restart job|Out of memory|Kernel panic|BUG:|Oops|watchdog", re.I)),
)
NOTABLE_KEEP = 40


# --- small readers -----------------------------------------------------------

def read(path, limit=READ_LIMIT):
    try:
        with open(path, "rb") as stream:
            return stream.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def field(path):
    text = read(path, 4096)
    return text.strip() if text is not None else None


def run(command, timeout=10):
    if not shutil.which(command[0]):
        return None
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return f"({command[0]} failed: {error})\n"
    return done.stdout[:READ_LIMIT].decode("utf-8", "replace")


def mask_mac(text):
    """Keep the maker prefix of a hardware address, hide the rest."""
    return re.sub(r"\b([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})(:[0-9a-fA-F]{2}){3}\b",
                  r"\1:xx:xx:xx", text)


# Serial numbers as the kernel, udev, power supplies, DMI and smartctl print them.
SERIALS = re.compile(
    r"(?i)(\b(?:serial[ _-]?(?:number|no\.?)|serial(?=\"\s*:|\s*=)|SerialNumber|ID_SERIAL(?:_SHORT)?|product_serial|"
    r"board_serial|chassis_serial|POWER_SUPPLY_SERIAL_NUMBER|iSerial)\"?\s*[:=]\s*)"
    # A quoted value is hidden up to its closing quote, spaces and all.
    r"(?:\"(?:[^\"\\]|\\.)*\"|[^\s\",;]+)")
# Speech that reaches a log by mistake: bus payloads with a text field.
SPEECH = re.compile(r'(?i)("(?:text|heard|transcript|partial)"\s*:\s*)"(?:[^"\\]|\\.)*"')


def scrub(text):
    """Remove what identifies this machine or its owner from a diagnostics line."""
    text = mask_mac(text)
    text = SERIALS.sub(lambda match: match[1] + ('"[hidden]"' if match[0].endswith('"') and
                                                  match[0][len(match[1]):].startswith('"') else "[hidden]"), text)
    return SPEECH.sub(r'\1"[speech removed]"', text)


def kernel_log(root):
    """The kernel ring buffer, read from /dev/kmsg without waiting."""
    path = root / "dev/kmsg"
    lines = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return run(["dmesg"]) if root == Path("/") else None
    try:
        while True:
            try:
                record = os.read(fd, 8192)
            except BlockingIOError:
                break
            except BrokenPipeError:     # overwritten while reading: carry on
                continue
            if not record:
                break
            head, _, message = record.decode("utf-8", "replace").partition(";")
            parts = head.split(",")
            stamp = f"{int(parts[2]) / 1e6:12.6f}" if len(parts) > 2 and parts[2].isdigit() else "?"
            text = message.split("\n", 1)[0]
            lines.append(f"[{stamp}] {text}")
    finally:
        os.close(fd)
    return "\n".join(lines) + "\n"


# --- finding the partition, on the boot disk only ----------------------------

def root_device(mountinfo):
    """major:minor of the device mounted at /, from /proc/self/mountinfo."""
    found = None
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/":
            found = fields[2]
    return found


def uevent(path):
    values = {}
    for line in (read(path / "uevent", 8192) or "").splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values


def find_partition(root=Path("/"), *, stat=os.stat):
    """Return (device, sysfs path, disk name) of the logs partition, or raise ValueError."""
    number = root_device(read(root / "proc/self/mountinfo") or "")
    if not number or not re.fullmatch(r"\d+:\d+", number):
        raise ValueError("cannot tell which device holds the root file system")
    try:
        system = (root / "sys/dev/block" / number).resolve(strict=True)
    except OSError:
        raise ValueError(f"no sysfs entry for the root device {number}") from None
    if not (system / "partition").exists():
        raise ValueError("the root file system is not on a disk partition")
    disk = system.parent
    if "/usb" not in str(disk):
        raise ValueError(f"the boot disk {disk.name} is not attached over USB")
    matches = [part for part in sorted(disk.iterdir())
               if (part / "partition").exists() and uevent(part).get("PARTNAME") == PARTNAME]
    if len(matches) != 1:
        raise ValueError(f"the boot disk {disk.name} has {len(matches)} {PARTNAME} partitions")
    part = matches[0]
    name = uevent(part).get("DEVNAME") or part.name
    expected = field(part / "dev")
    device = root / "dev" / name
    for _ in range(50):
        try:
            st = stat(device)
        except OSError:
            time.sleep(0.1)
            continue
        if f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}" != expected:
            raise ValueError(f"{device} is not the {PARTNAME} partition")
        return device, part, disk.name
    raise ValueError(f"{device} did not appear")


def fat_label(device):
    """The FAT volume label, or raise ValueError if this is not a FAT file system."""
    try:
        with open(device, "rb") as stream:
            sector = stream.read(512)
    except OSError as error:
        raise ValueError(f"cannot read {device}: {error}") from None
    if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
        raise ValueError(f"{device} has no FAT boot sector")
    if sector[82:87] == b"FAT32":
        return sector[71:82].decode("ascii", "replace").strip()
    if sector[54:57] == b"FAT":
        return sector[43:54].decode("ascii", "replace").strip()
    raise ValueError(f"{device} is not FAT")


def mounted_at(mountinfo, point):
    return any(len(f := line.split()) > 4 and f[4] == str(point) for line in mountinfo.splitlines())


def mount(device):
    info = read("/proc/self/mountinfo") or ""
    if mounted_at(info, MOUNT_POINT):
        return MOUNT_POINT
    # A power cut leaves the FAT dirty; repair only this partition first.
    for tool in ("fsck.fat", "fsck.vfat", "dosfsck"):
        if shutil.which(tool):
            log(run([tool, "-a", "-w", str(device)], timeout=60) or "")
            break
    MOUNT_POINT.mkdir(mode=0o755, parents=True, exist_ok=True)
    done = subprocess.run(["mount", "-t", "vfat", "-o", MOUNT_OPTIONS, str(device), str(MOUNT_POINT)],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
    if done.returncode:
        raise ValueError(f"mount failed: {done.stdout.decode('utf-8', 'replace').strip()}")
    return MOUNT_POINT


# --- folders on the stick ----------------------------------------------------

def boot_folders(base):
    found = []
    try:
        entries = list(os.scandir(base))
    except OSError:
        return found
    for entry in entries:
        match = FOLDER.match(entry.name)
        if match and entry.is_dir(follow_symlinks=False):
            found.append((int(match[1]), entry.name))
    return sorted(found)


def new_folder(base, now=None):
    now = time.gmtime(now)
    number = (boot_folders(base)[-1][0] + 1) if boot_folders(base) else 1
    name = f"boot-{number:04d}-{time.strftime('%Y%m%d-%H%M%S', now)}Z"
    path = base / name
    path.mkdir()
    return number, path


def free_bytes(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return 0
    return st.f_bavail * st.f_frsize


def prune(base, current, *, low=LOW_SPACE, keep=KEEP_NEWEST, free=free_bytes):
    """Remove the oldest boot folders while space is short. Returns what went."""
    removed = []
    folders = [name for _, name in boot_folders(base)]
    candidates = [name for name in folders[:-keep] if name != current.name] if len(folders) > keep else []
    while candidates and free(base) < low:
        name = candidates.pop(0)
        shutil.rmtree(base / name, ignore_errors=True)
        removed.append(name)
    return removed


def write_file(path, text):
    """Replace a file on the stick, making sure the new copy reached it."""
    temporary = path.with_name(path.name + ".new")
    with open(temporary, "w", encoding="utf-8", errors="replace") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_dir(path.parent)


def append_line(path, line):
    with open(path, "a", encoding="utf-8", errors="replace") as stream:
        stream.write(line.rstrip("\n") + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --- the hardware report -----------------------------------------------------

def cpu_info(root):
    text = read(root / "proc/cpuinfo", 1024 * 1024) or ""
    first = text.split("\n\n", 1)[0]
    values = {}
    for line in first.splitlines():
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()
    return {"model": values.get("model name"), "flags": values.get("flags"),
            "count": len(re.findall(r"^processor\s*:", text, re.M))}


def dmi(root):
    base = root / "sys/class/dmi/id"
    return {key: field(base / key) for key in (
        "sys_vendor", "product_name", "product_version", "board_vendor", "board_name",
        "bios_vendor", "bios_version", "bios_date")}


def driver_of(path):
    try:
        return os.path.basename(os.readlink(path / "driver"))
    except OSError:
        return None


def pci_devices(root):
    devices = []
    base = root / "sys/bus/pci/devices"
    for path in sorted(base.glob("*")):
        vendor, device = field(path / "vendor"), field(path / "device")
        devices.append({
            "slot": path.name, "class": (field(path / "class") or "")[2:6],
            "id": f"{(vendor or '')[2:]}:{(device or '')[2:]}",
            "subsystem": f"{(field(path / 'subsystem_vendor') or '')[2:]}:{(field(path / 'subsystem_device') or '')[2:]}",
            "driver": driver_of(path), "revision": field(path / "revision"),
        })
    return devices


def usb_devices(root):
    devices = []
    base = root / "sys/bus/usb/devices"
    for path in sorted(base.glob("*")):
        if not (path / "idVendor").exists():
            continue
        interfaces = []
        for interface in sorted(path.glob(f"{path.name}:*")):
            interfaces.append({"class": field(interface / "bInterfaceClass"), "driver": driver_of(interface)})
        devices.append({
            "path": path.name, "id": f"{field(path / 'idVendor')}:{field(path / 'idProduct')}",
            "manufacturer": field(path / "manufacturer"), "product": field(path / "product"),
            "speed": field(path / "speed"), "interfaces": interfaces,
        })
    return devices


def drm_connectors(root):
    connectors = []
    for path in sorted((root / "sys/class/drm").glob("card*-*")):
        modes = (read(path / "modes", 8192) or "").split()
        connectors.append({"name": path.name, "status": field(path / "status"),
                           "enabled": field(path / "enabled"), "modes": modes[:8]})
    cards = {}
    for path in sorted((root / "sys/class/drm").glob("card[0-9]")):
        cards[path.name] = driver_of(path / "device")
    return cards, connectors


def input_devices(root):
    text = read(root / "proc/bus/input/devices") or ""
    devices = []
    for block in text.strip().split("\n\n"):
        name = re.search(r'^N: Name="(.*)"', block, re.M)
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        if name:
            devices.append({"name": name[1], "handlers": handlers[1].strip() if handlers else ""})
    return devices


def lid_state(root):
    for path in sorted((root / "proc/acpi/button/lid").glob("*/state")):
        match = re.search(r"state:\s*(\w+)", read(path) or "")
        if match:
            return match[1]
    return None


def hwmon(root):
    sensors = []
    for path in sorted((root / "sys/class/hwmon").glob("hwmon*")):
        name, base = field(path / "name"), path
        if name is None and not list(path.glob("*_input")):
            # Linux 6.18 applesmc: a nameless hwmon whose readings live on the
            # platform device (hwmonN/device/), named by that device's driver.
            driver = driver_of(path / "device")
            if driver:
                name, base = driver, path / "device"
        readings = {}
        for item in sorted(base.glob("*_input"))[:64]:
            label = field(item.with_name(item.name.replace("_input", "_label")))
            value = field(item)
            readings[item.name + (f" ({label})" if label else "")] = value
        sensors.append({"name": name, "readings": readings})
    return sensors


def applesmc_dir(root):
    """Where applesmc keeps its attributes, on old and 6.18 layouts alike, or None."""
    platform = root / "sys/devices/platform/applesmc.768"
    candidates = [platform] if platform.is_dir() else []
    for path in sorted((root / "sys/class/hwmon").glob("hwmon*")):
        if field(path / "name") == "applesmc":
            candidates.append(path)
        elif driver_of(path / "device") == "applesmc":
            candidates.append(path / "device")
    for candidate in candidates:
        if (candidate / "key_count").exists() or list(candidate.glob("fan*_input")):
            return candidate
    return candidates[0] if candidates else None


SMC_INTERESTING = re.compile(r"\A(?:BCLM|BFCL|CH0B|CH0C|CH0I|CH0K|CH0R|F\d[A-Za-z]{2}|FNum|TB\dT|B0AC|B0AV|BNum)\Z")
SMC_READ = ("BCLM", "BFCL")  # data is read only for these two


def smc_keys(root, *, budget=10.0, limit=2048):
    """List SMC keys through key_at_index; read data only for BCLM and BFCL.

    Writing an index only selects which key the next reads describe. No SMC
    value is ever written.
    """
    base = applesmc_dir(root)
    if base is None or not (base / "key_count").exists():
        return {"available": False}
    try:
        count = int(field(base / "key_count") or "0")
    except ValueError:
        return {"available": False}
    found, values = [], {}
    started = time.monotonic()
    for index in range(min(count, limit)):
        if time.monotonic() - started > budget:
            found.append(f"(stopped after {index} of {count} keys: time budget)")
            break
        try:
            with open(base / "key_at_index", "w", encoding="ascii") as selector:
                selector.write(str(index))
            name = (field(base / "key_at_index_name") or "").strip()
        except OSError:
            continue
        if SMC_INTERESTING.match(name):
            kind = field(base / "key_at_index_type")
            found.append(f"{name} type={kind or '?'} bytes={field(base / 'key_at_index_data_length') or '?'}")
            if name in SMC_READ:
                try:
                    with open(base / "key_at_index_data", "rb") as data:
                        values[name] = data.read(32).hex()
                except OSError as error:
                    values[name] = f"(unreadable: {error})"
    return {"available": True, "key_count": count, "keys": found, "values": values}


def smc_readings(root):
    base = applesmc_dir(root)
    if base is None:
        return None
    readings = {}
    for item in sorted(base.glob("*"))[:400]:
        if re.fullmatch(r"(?:fan\d+_(?:input|min|max|label|manual|output)|temp\d+_(?:input|label)|light)", item.name):
            readings[item.name] = field(item)
    return {"path": str(base.relative_to(root)) if base.is_relative_to(root) else str(base), "readings": readings}


def cpu_power(root):
    """cpufreq, intel_pstate, RAPL limits and throttle counts, read-only."""
    base = root / "sys/devices/system/cpu"
    policy = base / "cpufreq/policy0"
    info = {key: field(policy / key) for key in (
        "scaling_driver", "scaling_governor", "scaling_min_freq", "scaling_max_freq", "scaling_cur_freq",
        "cpuinfo_min_freq", "cpuinfo_max_freq", "energy_performance_preference")}
    info["intel_pstate"] = {key: field(base / "intel_pstate" / key) for key in (
        "status", "no_turbo", "max_perf_pct", "min_perf_pct", "turbo_pct", "num_pstates")}
    rapl = {}
    for zone in sorted((root / "sys/class/powercap").glob("intel-rapl:*")):
        entry = {"name": field(zone / "name"), "enabled": field(zone / "enabled")}
        for constraint in sorted(zone.glob("constraint_*_name")):
            number = constraint.name.split("_")[1]
            entry[field(constraint) or f"constraint {number}"] = {
                "limit_uw": field(zone / f"constraint_{number}_power_limit_uw"),
                "window_us": field(zone / f"constraint_{number}_time_window_us")}
        rapl[zone.name] = entry
    info["rapl"] = rapl
    info["rapl_lock"] = "unknown (the lock bit is in MSR 0x610; this image reads no MSRs)"
    throttle = {}
    for cpu in sorted(base.glob("cpu[0-9]*")):
        counts = {key: field(cpu / "thermal_throttle" / key) for key in
                  ("core_throttle_count", "package_throttle_count")}
        if any(counts.values()):
            throttle[cpu.name] = counts
    info["thermal_throttle"] = throttle
    return info


def battery_health(root):
    """Cycle count, full versus design capacity, from every battery."""
    found = []
    for path in sorted((root / "sys/class/power_supply").glob("*")):
        values = uevent(path)
        if values.get("POWER_SUPPLY_TYPE") != "Battery":
            continue
        full = values.get("POWER_SUPPLY_CHARGE_FULL") or values.get("POWER_SUPPLY_ENERGY_FULL")
        design = values.get("POWER_SUPPLY_CHARGE_FULL_DESIGN") or values.get("POWER_SUPPLY_ENERGY_FULL_DESIGN")
        health = None
        try:
            health = round(100 * int(full) / int(design), 1) if full and design and int(design) else None
        except ValueError:
            pass
        found.append({"name": path.name, "cycle_count": values.get("POWER_SUPPLY_CYCLE_COUNT"),
                      "full": full, "design": design, "health_percent": health,
                      "voltage_now": values.get("POWER_SUPPLY_VOLTAGE_NOW"),
                      "power_now": values.get("POWER_SUPPLY_POWER_NOW") or values.get("POWER_SUPPLY_CURRENT_NOW"),
                      "status": values.get("POWER_SUPPLY_STATUS"), "capacity": values.get("POWER_SUPPLY_CAPACITY")})
    return found


def connector_details(root):
    """Each DRM connector's type (LVDS on this MacBook), status and DPMS state."""
    found = []
    for path in sorted((root / "sys/class/drm").glob("card*-*")):
        kind = re.sub(r"-\d+\Z", "", path.name.split("-", 1)[1]) if "-" in path.name else path.name
        found.append({"name": path.name, "type": kind, "status": field(path / "status"),
                      "dpms": field(path / "dpms"), "enabled": field(path / "enabled")})
    return found


def power_controls(root):
    """USB runtime power control, SATA link power management, rfkill."""
    usb = {path.name: field(path / "power/control") for path in sorted((root / "sys/bus/usb/devices").glob("*"))
           if (path / "power/control").exists()}
    sata = {path.name: field(path / "link_power_management_policy")
            for path in sorted((root / "sys/class/scsi_host").glob("host*"))
            if (path / "link_power_management_policy").exists()}
    rfkill = [{"name": field(path / "name"), "type": field(path / "type"), "soft": field(path / "soft"),
               "hard": field(path / "hard")} for path in sorted((root / "sys/class/rfkill").glob("rfkill*"))]
    return {"usb_power_control": usb, "sata_link_power": sata, "rfkill": rfkill}


def bcma_cores(root):
    """The Broadcom Wi-Fi chip's cores; b43 support depends on the 802.11 core revision."""
    return [{"device": path.name, **{key: field(path / key) for key in ("manuf", "id", "rev", "class")}}
            for path in sorted((root / "sys/bus/bcma/devices").glob("*"))]


def clamp_probe(root=Path("/"), seconds=3.0, *, spawn=None, sleep=time.sleep):
    """Run one busy thread for a few seconds and sample the clock speed.

    Without a battery the firmware asserts PROCHOT and holds the CPU near
    1.2 GHz; this shows whether that is happening.
    """
    cpus = sorted((root / "sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))
    if not cpus:
        return {"available": False}
    command = [sys.executable, "-c", f"import time\nend=time.monotonic()+{seconds}\nwhile time.monotonic()<end: pass"]
    process = (spawn or (lambda: subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))()
    samples = []
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            sleep(0.25)
            values = []
            for path in cpus:
                try:
                    values.append(int(field(path) or "0") // 1000)
                except ValueError:
                    pass
            if values:
                samples.append(max(values))
    finally:
        try:
            process.wait(seconds + 2)
        except subprocess.TimeoutExpired:
            process.kill()
    if not samples:
        return {"available": False}
    peak = max(samples)
    return {"available": True, "seconds": seconds, "peak_mhz": peak, "lowest_mhz": min(samples),
            "clamped": peak <= 1300}



def power_supplies(root):
    supplies = []
    for path in sorted((root / "sys/class/power_supply").glob("*")):
        values = uevent(path)
        values.pop("POWER_SUPPLY_SERIAL_NUMBER", None)
        supplies.append({"name": path.name, "values": values})
    return supplies


def backlights(root):
    return [{"name": path.name, "type": field(path / "type"), "brightness": field(path / "brightness"),
             "actual": field(path / "actual_brightness"), "max": field(path / "max_brightness")}
            for path in sorted((root / "sys/class/backlight").glob("*"))]


def interfaces(root):
    found = []
    for path in sorted((root / "sys/class/net").glob("*")):
        device = path / "device"
        found.append({
            "name": path.name, "state": field(path / "operstate"),
            "address": mask_mac(field(path / "address") or ""),
            "driver": driver_of(device),
            "wireless": (path / "wireless").exists() or (path / "phy80211").exists(),
        })
    return found


def block_devices(root):
    found = []
    for path in sorted((root / "sys/class/block").glob("*")):
        if (path / "partition").exists():
            continue
        sectors = field(path / "size")
        try:
            real = str(path.resolve())
        except OSError:
            real = ""
        parts = [f"{part.name} {uevent(part).get('PARTNAME', '')} {int(field(part / 'size') or 0) * 512 // 2**20} MiB"
                 for part in sorted(path.resolve().glob(f"{path.name}*")) if (part / "partition").exists()]
        found.append({
            "name": path.name, "size_mib": int(sectors or 0) * 512 // 2**20,
            "removable": field(path / "removable"), "read_only": field(path / "ro"),
            "model": field(path / "device/model"), "usb": "/usb" in real, "partitions": parts,
        })
    return found


def firmware_files(root, folder="b43"):
    base = root / "lib/firmware" / folder
    try:
        return sorted(item.name for item in base.iterdir())
    except OSError:
        return []


def cameras(root):
    found = [field(path / "name") for path in sorted((root / "sys/class/video4linux").glob("video*"))]
    return [name for name in found if name]


def sound(root):
    cards = read(root / "proc/asound/cards") or ""
    codecs = []
    for path in sorted((root / "proc/asound").glob("card*/codec#*")):
        match = re.search(r"^Codec: (.*)$", read(path, 4096) or "", re.M)
        if match:
            codecs.append(f"{path.parent.name}/{path.name}: {match[1]}")
    return cards, codecs, read(root / "proc/asound/pcm") or ""


def section(title, body):
    body = body if isinstance(body, str) else json.dumps(body, indent=1, ensure_ascii=False)
    return f"===== {title} =====\n{body.rstrip()}\n\n"


def hardware_report(root=Path("/"), *, slow=False, load=False):
    """A plain-text report of everything the kernel shows about this machine.

    `slow` adds the SMC key list, once per boot and for Check hardware.
    `load` adds a 3 s single-thread load, only when the owner asks for a
    check, so it never competes with the being's own start.
    """
    parts = []

    def add(title, make):
        try:
            value = make()
        except Exception as error:      # one unreadable source never loses the rest
            value = f"(could not read: {error!r})"
        parts.append(section(title, value if value not in (None, "", [], {}) else "(nothing)"))

    add("kernel", lambda: f"{read(root / 'proc/version') or ''}cmdline: {read(root / 'proc/cmdline') or ''}")
    add("build", lambda: read(root / "etc/being/build-id"))
    add("firmware (DMI/SMBIOS)", lambda: dmi(root))
    add("EFI", lambda: {"booted_with_efi": (root / "sys/firmware/efi").exists(),
                        "platform_bits": field(root / "sys/firmware/efi/fw_platform_size")})
    add("cpu", lambda: cpu_info(root))
    add("memory", lambda: read(root / "proc/meminfo"))
    add("pci (sysfs)", lambda: pci_devices(root))
    add("lspci -k", lambda: run(["lspci", "-k"]) if root == Path("/") else None)
    add("usb (sysfs)", lambda: usb_devices(root))
    add("drm", lambda: dict(zip(("cards", "connectors"), drm_connectors(root))))
    add("alsa cards", lambda: sound(root)[0])
    add("alsa codecs", lambda: sound(root)[1])
    add("alsa pcm", lambda: sound(root)[2])
    add("input devices", lambda: read(root / "proc/bus/input/devices"))
    add("lid", lambda: lid_state(root))
    add("hwmon (applesmc, coretemp, ...)", lambda: hwmon(root))
    add("applesmc readings", lambda: smc_readings(root))
    if slow:
        add("applesmc keys (key_at_index, data for BCLM and BFCL only)", lambda: smc_keys(root))
    add("power supplies", lambda: power_supplies(root))
    add("battery health", lambda: battery_health(root))
    add("cpu frequency, intel_pstate, RAPL, throttling", lambda: cpu_power(root))
    if load:
        add("single-thread load (3 s): clock speed", lambda: clamp_probe(root) if root == Path("/") else None)
    add("backlight", lambda: backlights(root))
    add("drm connectors (type, dpms)", lambda: connector_details(root))
    add("mixer (amixer scontents)", lambda: (run(["amixer", "scontents"]) or "(amixer is not on this image)")
        if root == Path("/") else None)
    add("network interfaces", lambda: interfaces(root))
    add("lspci -nn", lambda: run(["lspci", "-nn"]) if root == Path("/") else None)
    add("bcma cores (b43 core revision)", lambda: bcma_cores(root))
    add("b43 firmware files", lambda: firmware_files(root))
    add("power controls (USB, SATA link, rfkill)", lambda: power_controls(root))
    add("cameras (video4linux)", lambda: cameras(root))
    add("block devices (read from sysfs only)", lambda: block_devices(root))
    add("SSD health (smartctl)", lambda: (run(["smartctl", "-H", "-A", "/dev/sda"])
                                          or "(smartctl is not on this image)") if root == Path("/") else None)
    add("mounts", lambda: read(root / "proc/self/mounts"))
    add("loaded modules", lambda: read(root / "proc/modules"))
    return scrub("".join(parts))


# --- the summary ------------------------------------------------------------

KNOWN = {
    "8086:0166": "Intel HD Graphics 4000 (Ivy Bridge)",
    "14e4:4331": "Broadcom BCM4331 Wi-Fi",
    "14e4:16b4": "Broadcom BCM57765 Ethernet",
    "8086:1e20": "Intel 7 Series HD Audio",
}


def summary(root=Path("/"), *, boot=None, notable=None, journal_bytes=None, failed_units=None):
    notable = notable or {}
    lines = []

    def say(label, value):
        lines.append(f"{label + ':':<16}{value}")

    def guard(make, default=None):
        try:
            return make()
        except Exception as error:      # a missing source never loses the summary
            lines.append(f"{'(error)':<16}{error!r}")
            return default

    machine = guard(lambda: dmi(root))
    if isinstance(machine, dict):
        say("Machine", " ".join(v for v in (machine["sys_vendor"], machine["product_name"]) if v) or "unknown")
        say("Firmware", " ".join(v for v in (machine["bios_version"], machine["bios_date"]) if v) or "unknown")
    say("EFI boot", "yes" if (root / "sys/firmware/efi").exists() else "NO")
    cpu = guard(lambda: cpu_info(root))
    if isinstance(cpu, dict):
        avx2 = " avx2" in f" {cpu['flags'] or ''} "
        say("CPU", f"{cpu['model'] or 'unknown'} ({cpu['count']} threads{', has AVX2' if avx2 else ''})")
    pci = guard(lambda: pci_devices(root), [])
    pci = pci if isinstance(pci, list) else []
    gpus = [d for d in pci if d["class"].startswith("03")]
    say("GPU", "; ".join(f"{KNOWN.get(d['id'], d['id'])} driver={d['driver'] or 'NONE'}" for d in gpus) or "none found")
    drawn = notable.get("renderer") or []
    say("GL renderer", drawn[-1].split("face: drawn by", 1)[1].strip() if drawn else "not logged yet (face not started?)")
    drm = guard(lambda: drm_connectors(root))
    if isinstance(drm, tuple):
        shown = [f"{c['name']}={c['status']}{' ' + c['modes'][0] if c['modes'] else ''}" for c in drm[1]]
        say("Displays", ", ".join(shown) or "no DRM connectors")
    cards, codecs, _ = guard(lambda: sound(root), ("", [], ""))
    say("Audio", "; ".join(line.strip() for line in cards.splitlines() if re.match(r"\s*\d+ \[", line)) or "NO sound card")
    if codecs:
        say("Audio codecs", "; ".join(codecs))
    inputs = guard(lambda: input_devices(root), [])
    inputs = inputs if isinstance(inputs, list) else []
    lid = [d for d in inputs if "lid" in d["name"].lower()]
    say("Lid switch", (f"found ({lid[0]['name']}), state {lid_state(root) or 'unknown'}") if lid else "NOT found")
    keyboards = [d["name"] for d in inputs if "kbd" in d["handlers"].split()]
    say("Keyboards", ", ".join(keyboards) or "none")
    supplies = guard(lambda: power_supplies(root), [])
    supplies = supplies if isinstance(supplies, list) else []
    batteries = [s for s in supplies if s["values"].get("POWER_SUPPLY_TYPE") == "Battery"]
    if batteries:
        v = batteries[0]["values"]
        say("Battery", f"found {batteries[0]['name']}, {v.get('POWER_SUPPLY_CAPACITY', '?')}% "
                       f"{v.get('POWER_SUPPLY_STATUS', '')}".rstrip())
    else:
        say("Battery", "NOT found")
    mains = [s for s in supplies if s["values"].get("POWER_SUPPLY_TYPE") == "Mains"]
    say("Charger", ", ".join(f"{s['name']} online={s['values'].get('POWER_SUPPLY_ONLINE', '?')}" for s in mains) or "not found")
    if batteries:
        health = guard(lambda: battery_health(root), [])
        if health:
            first = health[0]
            say("Battery health", f"{first['health_percent']}% of design" if first["health_percent"] is not None
                else "design capacity unknown")
            say("Battery cycles", first["cycle_count"] or "unknown")
    smc = guard(lambda: smc_readings(root))
    if smc:
        fans = {k: v for k, v in smc["readings"].items() if k.startswith("fan") and k.endswith("_input")}
        say("applesmc", f"found at {smc['path']}, {len(smc['readings'])} readings, "
                        f"fans {json.dumps(fans) if fans else 'none'}")
    else:
        say("applesmc", "NOT found")
    power = guard(lambda: cpu_power(root))
    if isinstance(power, dict) and power.get("scaling_driver"):
        pstate = power["intel_pstate"]
        say("CPU scaling", f"{power['scaling_driver']} {power['scaling_governor'] or ''}, max "
                           f"{int(power['scaling_max_freq'] or 0) // 1000} MHz, no_turbo={pstate['no_turbo']}, "
                           f"max_perf_pct={pstate['max_perf_pct']}")
    camera = guard(lambda: cameras(root), [])
    usb = guard(lambda: usb_devices(root), [])
    usb = usb if isinstance(usb, list) else []
    uvc = [d for d in usb if any(i["class"] == "0e" for i in d["interfaces"])]
    say("Camera", ", ".join(camera) if camera else (f"USB video device {uvc[0]['id']} but no video4linux node" if uvc else "NOT found"))
    wifi = [d for d in pci if d["id"] == "14e4:4331" or d["class"].startswith("0280")]
    firmware = guard(lambda: firmware_files(root), [])
    radios = [i["name"] for i in guard(lambda: interfaces(root), []) if isinstance(i, dict) and i["wireless"]]
    say("Wi-Fi chip", "; ".join(f"{KNOWN.get(d['id'], d['id'])} driver={d['driver'] or 'NONE'}" for d in wifi) or "none found")
    say("Wi-Fi firmware", (f"{len(firmware)} b43 files" if firmware else "NO b43 firmware in /lib/firmware/b43")
        + (f", interface {', '.join(radios)}" if radios else ", no wireless interface"))
    wired = [d for d in pci if d["class"].startswith("0200")]
    say("Ethernet", "; ".join(f"{KNOWN.get(d['id'], d['id'])} driver={d['driver'] or 'NONE'}" for d in wired) or "none found")
    memory = re.search(r"^MemTotal:\s*(\d+)", read(root / "proc/meminfo") or "", re.M)
    say("Memory", f"{int(memory[1]) // 1024} MiB" if memory else "unknown")
    if failed_units is not None:
        say("Failed units", failed_units.strip().replace("\n", "; ") or "none")
    if journal_bytes is not None:
        say("Journal copied", f"{journal_bytes // 1024} KiB")
    head = [f"PipOS Try boot {boot or ''}".rstrip(),
            f"Written {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}, "
            f"{uptime(root)} after power-on", ""]
    tail = []
    for key, title in (("failed", "Failures and restarts"), ("firmware", "Firmware messages"),
                       ("wifi", "Wi-Fi messages"), ("graphics", "Graphics messages"),
                       ("audio", "Audio messages"), ("smc", "applesmc messages")):
        if notable.get(key):
            tail += ["", f"--- {title} (first {len(notable[key])}) ---", *notable[key]]
    return scrub("\n".join(head + lines + tail) + "\n")


def uptime(root):
    text = field(root / "proc/uptime")
    try:
        return f"{float(text.split()[0]):.0f} s"
    except (AttributeError, ValueError, IndexError):
        return "unknown time"


# --- the running service -----------------------------------------------------

def log(message):
    if message:
        print(f"being-usb-logs: {message.strip()}", file=sys.stderr, flush=True)


class Journal:
    """journalctl -f copied to a file on the stick, synced every few seconds."""

    def __init__(self, path, cursor, *, cap=JOURNAL_CAP, notable=None):
        self.path, self.cursor, self.cap = path, cursor, cap
        self.stream = open(path, "ab")
        self.written = self.stream.tell()
        self.pending = False
        self.last_sync = time.monotonic()
        self.process = None
        self.restarts = 0
        self.partial = b""
        self.unwritten = b""
        self.notable = notable if notable is not None else {}
        self.stopped = False

    def start(self):
        if self.process is not None or self.stopped:
            return
        command = ["journalctl", "--boot", "--follow", "--no-tail", "--no-pager", "--quiet",
                   "--output=short-monotonic", f"--cursor-file={self.cursor}"]
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            stdin=subprocess.DEVNULL)
        except OSError as error:
            log(f"cannot start journalctl: {error}")
            self.stopped = True
            return
        os.set_blocking(self.process.stdout.fileno(), False)

    def scan(self, data):
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()[-4096:]
        for raw in lines:
            text = raw.decode("utf-8", "replace")
            for key, pattern in NOTABLE:
                kept = self.notable.setdefault(key, [])
                if len(kept) < NOTABLE_KEEP and pattern.search(text):
                    kept.append(scrub(text)[:300])

    def feed(self, data, free):
        self.scan(data)
        if self.stopped:
            return
        # Identifiers are masked on whole lines, so none can hide across reads.
        pending = self.unwritten + data
        cut = pending.rfind(b"\n") + 1
        if cut == 0 and len(pending) < 4096:
            self.unwritten = pending
            return
        cut = cut or len(pending)
        self.unwritten = pending[cut:]
        data = scrub(pending[:cut].decode("utf-8", "replace")).encode("utf-8")
        if self.written + len(data) > self.cap or free < STOP_SPACE:
            self.stream.write(b"\n[being-usb-logs: journal copy stopped here, "
                              + (b"size cap reached" if free >= STOP_SPACE else b"stick nearly full") + b"]\n")
            self.stop()
            return
        self.stream.write(data)
        self.written += len(data)
        self.pending = True

    def read(self, free):
        if self.process is None:
            return False
        try:
            data = os.read(self.process.stdout.fileno(), 65536)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if data:
            self.feed(data, free)
            return True
        self.process.wait()
        self.process = None
        if self.restarts < 20 and not self.stopped:
            self.restarts += 1
            time.sleep(1)
            self.start()
        return False

    def sync(self, force=False):
        if self.stream.closed or not self.pending:
            return
        if force or time.monotonic() - self.last_sync >= FSYNC_EVERY:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.pending = False
            self.last_sync = time.monotonic()

    def stop(self):
        self.stopped = True
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        if not self.stream.closed:
            if self.unwritten:
                self.stream.write(scrub(self.unwritten.decode("utf-8", "replace")).encode("utf-8"))
                self.unwritten = b""
            self.pending = True
            self.sync(force=True)
            self.stream.close()


def failed_units():
    return run(["systemctl", "list-units", "--failed", "--plain", "--no-legend", "--no-pager"])


def snapshot(folder, number, journal, label, *, slow=False):
    """Rewrite the report files in this boot's folder."""
    failed = failed_units()
    for name, make in (
        ("summary.txt", lambda: summary(boot=f"#{number}", notable=journal.notable if journal else None,
                                        journal_bytes=journal.written if journal else None,
                                        failed_units=failed)),
        ("hardware-report.txt", lambda: f"Snapshot: {label}\n\n" + hardware_report(slow=slow)),
        ("dmesg.txt", lambda: scrub(kernel_log(Path("/")) or "")),
        ("services.txt", lambda: run(["systemctl", "list-units", "--all", "--plain", "--no-pager"]) or ""),
    ):
        try:
            write_file(folder / name, make())
        except Exception as error:
            log(f"could not write {name}: {error!r}")


def going_down():
    jobs = run(["systemctl", "list-jobs", "--no-legend", "--no-pager"], timeout=3) or ""
    for target in ("reboot.target", "poweroff.target", "halt.target", "kexec.target"):
        if target in jobs:
            return target.split(".")[0]
    return "stop"


def current_folder():
    try:
        return Path((STATE_DIR / "folder").read_text(encoding="utf-8").strip())
    except OSError:
        return None


def serve():
    cmdline = (read("/proc/cmdline") or "").split()
    if "being.try=1" not in cmdline:
        log("not a Try boot; nothing to do")
        return 0
    try:
        device, _, disk = find_partition()
        label = fat_label(device)
        base = mount(device)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        log(f"no logs partition used: {error}")
        return 0
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (base / LOGGING_OFF).exists():
        if (STATE_DIR / "enable").exists():
            (base / LOGGING_OFF).unlink(missing_ok=True)
            (STATE_DIR / "enable").unlink(missing_ok=True)
            (STATE_DIR / "off").unlink(missing_ok=True)
            append_line(base / "boots.txt", "diagnostics switched back on by the owner")
        else:
            (STATE_DIR / "off").write_text("owner\n", encoding="utf-8")
            log("the owner switched Try diagnostics off; nothing is written")
            os.chdir("/")
            subprocess.run(["umount", str(base)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 0
    log(f"writing diagnostics to {device} (label {label or 'none'}) on boot disk {disk}")
    folder = current_folder()
    if folder is None or not folder.is_dir():
        number, folder = new_folder(base)
        (STATE_DIR / "folder").write_text(f"{folder}\n", encoding="utf-8")
        removed = prune(base, folder)
        build = (read("/etc/being/build-id") or "").strip()
        append_line(base / "boots.txt", f"{folder.name} started build={build or 'unknown'}"
                    + (f" (pruned {', '.join(removed)})" if removed else ""))
        write_file(base / "LATEST.txt", folder.name + "\n")
    else:
        number = int(FOLDER.match(folder.name)[1])
        append_line(base / "boots.txt", f"{folder.name} service restarted")

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    journal = Journal(folder / "journal.txt", STATE_DIR / "cursor")
    journal.start()
    selector = selectors.DefaultSelector()
    started = time.monotonic()
    due = list(SNAPSHOTS)
    registered = None
    while not stopping:
        if registered is not journal.process:
            if registered is not None:
                try:
                    selector.unregister(registered.stdout)
                except (KeyError, ValueError):
                    pass
                registered.stdout.close()
                registered = None
            if journal.process is not None:
                selector.register(journal.process.stdout, selectors.EVENT_READ)
                registered = journal.process
        elapsed = time.monotonic() - started
        if due and elapsed >= due[0]:
            # The slow checks once per boot, after the being has settled.
            when = due.pop(0)
            snapshot(folder, number, journal, f"{when} s after the logger started", slow=when == SLOW_SNAPSHOT)
            continue
        wait = min(1.0, (due[0] - elapsed) if due else 1.0)
        if journal.pending:
            wait = min(wait, max(0.05, FSYNC_EVERY - (time.monotonic() - journal.last_sync)))
        events = selector.select(max(0.05, wait)) if registered else (time.sleep(wait) or [])
        if events:
            free = free_bytes(base)
            for _ in range(64):     # drain a burst, but keep snapshots on time
                if not journal.read(free) or registered is not journal.process or not selector.select(0):
                    break
        try:
            journal.sync()
        except OSError as error:
            log(f"journal sync failed: {error}")
    how = going_down()
    # Read what is already queued, then close cleanly.
    deadline = time.monotonic() + 1.5
    while journal.process is not None and time.monotonic() < deadline and journal.read(free_bytes(base)):
        pass
    journal.stop()
    try:
        write_file(folder / "summary.txt", summary(boot=f"#{number}", notable=journal.notable,
                                                   journal_bytes=journal.written, failed_units=failed_units()))
        append_line(base / "boots.txt", f"{folder.name} ended cleanly ({how}) after {uptime(Path('/'))}")
    except OSError as error:
        log(f"could not record the end of the boot: {error}")
    os.chdir("/")
    os.sync()
    subprocess.run(["umount", str(base)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def save_check(menu_report=None):
    """Save a 'Check hardware' result to the stick. Returns the file, or None.

    For mind/local_menu.py: ``usb_logs.save_check(report)`` after showing
    ``installer.hardware_report()``. It never raises.
    """
    try:
        folder = current_folder()
        if folder is None or not folder.is_dir():
            return None
        path = folder / f"check-hardware-{time.strftime('%H%M%S', time.gmtime())}.txt"
        body = ""
        if menu_report is not None:
            body += section("Check hardware (menu)", json.dumps(menu_report, indent=1, default=str))
        body += section("summary", summary())
        body += hardware_report(slow=True, load=True)
        write_file(path, scrub(body))
        return path
    except Exception as error:
        log(f"could not save the hardware check: {error!r}")
        return None


# --- guided checks, with the owner at the keyboard (Check hardware, G) ------

KEY_NAMES = {
    1: "Esc", 59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6", 65: "F7", 66: "F8", 67: "F9",
    68: "F10", 87: "F11", 88: "F12", 113: "Mute", 114: "VolumeDown", 115: "VolumeUp", 120: "Scale",
    161: "Eject", 163: "NextSong", 164: "PlayPause", 165: "PreviousSong", 204: "Dashboard",
    224: "BrightnessDown", 225: "BrightnessUp", 229: "KbdIllumDown", 230: "KbdIllumUp", 464: "Fn",
}
EVENT = "llHHi"  # struct input_event on x86_64: timeval, type, code, value


def keyboard_events(root=Path("/")):
    text = read(root / "proc/bus/input/devices") or ""
    found = []
    for block in text.strip().split("\n\n"):
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        if handlers and "kbd" in handlers[1].split():
            found += [root / "dev/input" / name for name in handlers[1].split() if re.fullmatch(r"event\d+", name)]
    return found


def capture_keys(seconds=10.0, *, paths=None, clock=time.monotonic):
    """Key codes pressed in the next few seconds, by name where known. Read-only."""
    import struct

    size = struct.calcsize(EVENT)
    handles = []
    for path in paths if paths is not None else keyboard_events():
        try:
            handles.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            continue
    pressed = []
    selector = selectors.DefaultSelector()
    try:
        for fd in handles:
            selector.register(fd, selectors.EVENT_READ)
        deadline = clock() + seconds
        while handles and clock() < deadline and len(pressed) < 200:
            for key, _ in selector.select(max(0.0, min(0.25, deadline - clock()))):
                try:
                    data = os.read(key.fd, size * 64)
                except BlockingIOError:
                    continue
                for offset in range(0, len(data) - size + 1, size):
                    _, _, kind, code, value = struct.unpack_from(EVENT, data, offset)
                    if kind == 1 and value == 1:
                        pressed.append(KEY_NAMES.get(code, f"code {code}"))
    finally:
        selector.close()
        for fd in handles:
            os.close(fd)
    return {"devices": len(handles), "pressed": pressed}


def blank_check(root=Path("/"), *, seconds=1.0, sleep=time.sleep):
    """Turn the panel backlight off through bl_power for a moment, then restore it."""
    results = []
    for path in sorted((root / "sys/class/backlight").glob("*")):
        control = path / "bl_power"
        before = field(control)
        if before is None:
            results.append({"name": path.name, "bl_power": None})
            continue
        entry = {"name": path.name, "bl_power_before": before, "brightness": field(path / "actual_brightness")}
        try:
            control.write_text("4\n")
            sleep(seconds)
            entry["brightness_while_off"] = field(path / "actual_brightness")
        except OSError as error:
            entry["error"] = str(error)
        finally:
            try:
                control.write_text(f"{before}\n")
            except OSError as error:
                entry["restore_error"] = str(error)
        entry["bl_power_after"] = field(control)
        results.append(entry)
    return results


def save_guided(results):
    """Write the guided check results to this boot's folder. Returns the file or None."""
    try:
        folder = current_folder()
        if folder is None or not folder.is_dir():
            return None
        path = folder / f"guided-check-{time.strftime('%H%M%S', time.gmtime())}.txt"
        write_file(path, scrub(section("Guided checks", results)))
        return path
    except Exception as error:
        log(f"could not save the guided check: {error!r}")
        return None


def logging_enabled():
    """None when this boot has no PIPLOGS stick, else whether diagnostics are kept."""
    if (STATE_DIR / "off").exists():
        return False
    folder = current_folder()
    return True if folder is not None and folder.is_dir() else None


def set_logging(on, *, systemctl=None):
    """The Setup switch for Try diagnostics. Off lasts across boots of this stick."""
    control = systemctl or (lambda *args: subprocess.run(["systemctl", *args], check=False, timeout=30,
                                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if on:
        (STATE_DIR / "enable").write_text("owner\n", encoding="utf-8")
        (STATE_DIR / "folder").unlink(missing_ok=True)
        control("restart", "being-usb-logs.service")
        return
    folder = current_folder()
    if folder is not None and folder.is_dir():
        write_file(folder.parent / LOGGING_OFF, "The owner switched Try diagnostics off in Setup.\n"
                   "Delete this file, or switch them on again in Setup, to keep diagnostics.\n")
    (STATE_DIR / "off").write_text("owner\n", encoding="utf-8")
    control("stop", "being-usb-logs.service")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-usb-logs", description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="the Try boot service: mount PIPLOGS and keep this boot's logs there")
    commands.add_parser("report", help="print the hardware report")
    commands.add_parser("summary", help="print the summary")
    commands.add_parser("check", help="save a hardware check to this boot's folder on the stick")
    args = parser.parse_args(argv)
    if args.command == "run":
        return serve()
    if args.command == "report":
        sys.stdout.write(hardware_report())
        return 0
    if args.command == "summary":
        sys.stdout.write(summary())
        return 0
    path = save_check()
    print(f"Saved to the USB stick: {path}" if path else "No PIPLOGS partition is in use on this boot.")
    return 0 if path else 1


if __name__ == "__main__":
    sys.exit(main())
