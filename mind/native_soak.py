"""Run an explicitly armed native PipOS endurance observation on the installed Mac."""

import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import uuid4

from . import hardware_metrics, native_soak_sample
from .care_routes import guest_active
from .install_binding import installed_mapping
from .storage import atomic_write, private_directory, sync_directory
from .try_mode import CMDLINE, installed_trial_pending, is_try_boot


MIND = Path("/mind")
HOURS = (24, 48, 168)
INTERVAL = 30
MAX_GAP = INTERVAL + 15
POWER_CUT_WINDOW = 15 * 60
WALL_EXTENSION = 2 * 3600
EVIDENCE_LIMIT = 96 * 1024 * 1024
ORGANS = ("brainstem", "face", "voice", "mind", "cortex")
SERVICE = "being-native-soak.service"
RUN_PATTERN = r"[0-9a-f]{32}"
BOOT_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
PANEL_UNIT = "being-local-screen.service"


def base(mind):
    return Path(mind) / "log" / "soak"


def _private_parent(path):
    info = path.lstat()
    if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("The endurance log directory is unavailable.")


@contextmanager
def _file(path, flags, mode):
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, mode) as stream:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("The endurance record is unavailable.")
        yield stream


@contextmanager
def _locked(mind, *, create=False):
    root = base(mind)
    if create:
        root.parent.mkdir(mode=0o755, exist_ok=True)
    _private_parent(root.parent)
    if create:
        private_directory(root)
    _private_parent(root)
    with _file(root / ".lock", os.O_CREAT | os.O_RDWR, "r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield root


def _read(path):
    try:
        _private_parent(path.parent.parent)
        _private_parent(path.parent)
        with _file(path, os.O_RDONLY, "rb") as stream:
            block = stream.read(8193)
    except FileNotFoundError:
        return None
    if len(block) > 8192:
        raise ValueError("The endurance state is too large.")
    state = json.loads(block)
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("run"), str) or not re.fullmatch(RUN_PATTERN, state["run"])
            or state.get("hours") not in HOURS or type(state.get("offset")) is not int
            or not 0 < state["offset"] <= EVIDENCE_LIMIT or not isinstance(state.get("profile"), dict)
            or not isinstance(state.get("completed_organs"), list)
            or any(name not in ORGANS for name in state["completed_organs"])
            or type(state.get("observed_seconds")) not in (int, float)
            or not math.isfinite(state["observed_seconds"]) or state["observed_seconds"] < 0):
        raise ValueError("The endurance state is damaged.")
    return state


def _save(root, state, *, name="current.json"):
    atomic_write(root / name, json.dumps(state, allow_nan=False) + "\n")


def current(mind=MIND):
    state = _read(base(mind) / "current.json")
    if state is not None and "reason" not in state:
        return {**state, "armed": state.get("armed_utc") is not None,
                "pending_organ": (state.get("fault") or {}).get("organ")}
    return None


def last(mind=MIND):
    return _read(base(mind) / "last.json")


def _control(mind):
    if Path(mind).resolve() != MIND or not MIND.is_mount() or is_try_boot() or installed_trial_pending(mind):
        raise ValueError("Endurance runs require the confirmed installed system.")
    installed_mapping(CMDLINE.read_text())
    if guest_active():
        raise ValueError("Endurance controls are private while a guest is here.")


def _system(*args):
    result = subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=6, check=False)
    if result.returncode:
        raise OSError("The endurance service could not perform that action.")
    return result.stdout.decode("ascii", "replace")


def boot_id():
    value = hardware_metrics.read_field(Path("/proc/sys/kernel/random/boot_id"), 64)
    if not re.fullmatch(BOOT_PATTERN, value):
        raise ValueError("The boot identity is unavailable.")
    return value


def supported_hardware():
    return hardware_metrics.read_field(Path("/sys/class/dmi/id/product_name"), 128) == "MacBookPro9,2"


def _profile():
    info = hardware_metrics.system_info(Path("/"))
    profile = {"hardware": info["model"], "build_id": info["build_id"], "model_sha256": info["model_sha256"],
               "kernel": info["kernel"], "slot": info["slot"]}
    if (profile["hardware"] != "MacBookPro9,2" or not profile["build_id"]
            or not profile["model_sha256"] or not profile["kernel"] or profile["slot"] not in ("A", "B")):
        raise ValueError("The native soak needs an identified MacBookPro9,2 build and model pack.")
    return profile


def _encode(kind, stamp, boot, **data):
    return (json.dumps({"kind": kind, "utc": stamp, "boot_id": boot, **data},
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def _append(root, state, kind, stamp, boot, **data):
    content = _encode(kind, stamp, boot, **data)
    offset = state["offset"]
    limit = EVIDENCE_LIMIT if kind == "end" else EVIDENCE_LIMIT - 4096
    if offset + len(content) > limit:
        raise OSError("The endurance evidence limit was reached.")
    with _file(root / f"{state['run']}.jsonl", os.O_RDWR, "r+b") as stream:
        length = stream.seek(0, os.SEEK_END)
        if length < offset:
            raise ValueError("The endurance log has lost committed records.")
        # A cut between appending and publishing the state leaves one
        # uncommitted tail. Discard only that tail before resuming.
        stream.truncate(offset)
        stream.seek(offset)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    state["offset"] = offset + len(content)


def _finalize(root, state, reason, stamp, boot):
    if "reason" not in state:
        _append(root, state, "end", stamp, boot, reason=reason, observed_seconds=state["observed_seconds"])
        state["reason"] = reason
        _save(root, state)
    _save(root, state, name="last.json")
    (root / "current.json").unlink()
    sync_directory(root)


def start(mind, hours):
    _control(mind)
    if type(hours) is not int or hours not in HOURS:
        raise ValueError("Choose 24 hours, 48 hours or one week.")
    profile = _profile()
    stamp, boot, run_id = time.time(), boot_id(), uuid4().hex
    with _locked(mind, create=True) as root:
        if _read(root / "current.json") is not None:
            raise ValueError("An endurance run is already active.")
        content = _encode("start", stamp, boot, version=1, run=run_id, hours=hours, interval_s=INTERVAL,
                          **profile)
        with _file(root / f"{run_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        state = {"version": 1, "run": run_id, "offset": len(content), "hours": hours, "profile": profile,
                 "boot_id": boot, "start_utc": stamp, "last_utc": stamp, "last_mono": None,
                 "observed_seconds": 0, "armed_utc": None, "cut_done": False, "fault": None,
                 "completed_organs": [], "panel_enter_us": None}
        _save(root, state)
    try:
        _system("--no-block", "start", SERVICE)
    except (OSError, subprocess.SubprocessError):
        with _locked(mind) as root:
            state = _read(root / "current.json")
            if state is not None and state["run"] == run_id:
                _finalize(root, state, "sample-error", time.time(), boot)
        raise
    return run_id


def stop(mind):
    _control(mind)
    with _locked(mind) as root:
        state = _read(root / "current.json")
        if state is None:
            return
        _finalize(root, state, "stopped", time.time(), boot_id())
    _system("--no-block", "stop", SERVICE)


def _running(root):
    state = _read(root / "current.json")
    if state is None or "reason" in state or state["boot_id"] != boot_id() or state["last_mono"] is None:
        raise ValueError("Wait for this boot's first endurance sample.")
    return state


def arm_power_cut(mind):
    _control(mind)
    with _locked(mind) as root:
        state = _running(root)
        if state["cut_done"] or state["armed_utc"] is not None or state["fault"] is not None:
            raise ValueError("A scheduled interruption is already active or was completed.")
        stamp = time.time()
        _append(root, state, "armed_power_cut", stamp, state["boot_id"])
        state["armed_utc"] = stamp
        _save(root, state)


def arm_organ(mind, name):
    _control(mind)
    if name not in ORGANS:
        raise ValueError("Choose a listed organ.")
    with _locked(mind) as root:
        state = _running(root)
        if (state["hours"] != 48 or name in state["completed_organs"] or state["fault"] is not None
                or state["armed_utc"] is not None):
            raise ValueError("This organ cannot be scheduled for recovery now.")
        state["fault"] = {"organ": name, "expires_mono": time.monotonic() + 5 * INTERVAL,
                          "activation_us": None, "restarts": None}
        _save(root, state)


def _panel():
    properties = "ActiveState,InactiveExitTimestampMonotonic"
    raw = _system("show", f"--property={properties}", PANEL_UNIT)
    values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if values.get("ActiveState") not in {"inactive", "active", "activating", "deactivating", "failed"}:
        raise ValueError("The local screen state is unavailable.")
    active = values["ActiveState"] in {"active", "activating", "deactivating"}
    return active, hardware_metrics.number(values.get("InactiveExitTimestampMonotonic", ""))


def _fault_step(root, state, sample, stamp, monotonic, boot):
    fault = state["fault"]
    if fault is None:
        return True
    unit = f"being-{fault['organ']}.service"
    service = sample["services"].get(unit, {})
    activation = service.get("active_since_boot_us")
    restarts = service.get("restarts")
    healthy = (service.get("active_state") == "active" and service.get("sub_state") == "running"
               and type(activation) is int and activation > 0 and type(restarts) is int and restarts >= 0)
    if fault["activation_us"] is None:
        if monotonic >= fault["expires_mono"]:
            state["fault"] = None
            return True
        if not healthy or sample["local_screen"] or sample["lid_closed"] is not False or guest_active():
            return True
        fault.update(activation_us=activation, restarts=restarts, expires_mono=monotonic + 120)
        _append(root, state, "organ_fault", stamp, boot, unit=unit, activation_us=activation)
        _save(root, state)
        _system("kill", "--signal=SIGKILL", "--kill-whom=main", unit)
    elif healthy and activation != fault["activation_us"] and restarts == fault["restarts"] + 1:
        _append(root, state, "organ_recovered", stamp, boot, unit=unit, activation_us=activation)
        state["completed_organs"].append(fault["organ"])
        state["fault"] = None
    elif monotonic >= fault["expires_mono"] or (type(restarts) is int and restarts > fault["restarts"] + 1):
        return False
    return True


def _recover(root, state, mind, stamp, boot):
    armed = state["armed_utc"]
    booted_at = stamp - time.clock_gettime(time.CLOCK_BOOTTIME)
    if armed is None or state["cut_done"] or not 0 <= booted_at - armed <= POWER_CUT_WINDOW:
        return False
    waited = time.monotonic() + 60
    lost = native_soak_sample.previous_power_lost(mind, boot)
    while lost is None and time.monotonic() < waited:
        time.sleep(1)
        lost = native_soak_sample.previous_power_lost(mind, boot)
    archive_ok = native_soak_sample.archive_intact(mind) if lost is True else None
    _append(root, state, "recovery", time.time(), boot, previous_boot_id=state["boot_id"],
            lost_power=lost, archive_ok=archive_ok)
    if lost is not True or archive_ok is not True:
        return False
    state.update(boot_id=boot, last_mono=None, panel_enter_us=None, cut_done=True, armed_utc=None)
    _save(root, state)
    return True


def collect(mind=MIND):
    boot = boot_id()
    wait_until = time.monotonic() + 120
    while installed_trial_pending(mind) and time.monotonic() < wait_until:
        time.sleep(1)
    if installed_trial_pending(mind):
        return
    while True:
        sample_start = time.monotonic()
        with _locked(mind) as root:
            state = _read(root / "current.json")
            if state is None:
                return
            stamp = time.time()
            if "reason" in state:
                _finalize(root, state, state["reason"], stamp, boot)
                return
            if not 0 <= stamp - state["start_utc"] <= state["hours"] * 3600 + WALL_EXTENSION:
                _finalize(root, state, "expired", stamp, boot)
                return
            try:
                if _profile() != state["profile"]:
                    _finalize(root, state, "hardware-change", stamp, boot)
                    return
                if state["boot_id"] != boot and not _recover(root, state, mind, stamp, boot):
                    _finalize(root, state, "unexpected-reboot", time.time(), boot)
                    return
                stamp = time.time()
                previous = state["last_mono"]
                current_mono = time.monotonic()
                elapsed = 0 if previous is None else current_mono - previous
                if not 0 <= elapsed <= MAX_GAP:
                    _finalize(root, state, "sample-error", stamp, boot)
                    return
                if state["armed_utc"] is not None and stamp - state["armed_utc"] > POWER_CUT_WINDOW:
                    _append(root, state, "power_cut_expired", stamp, boot)
                    state["armed_utc"] = None
                was_active, prior_panel_enter = _panel()
                sample = native_soak_sample.snapshot(mind, voice_after=previous or current_mono,
                                                     voice_through=current_mono)
                active, panel_enter = _panel()
                panel_enter = max((n for n in (prior_panel_enter, panel_enter) if n is not None), default=None)
                if (state["panel_enter_us"] is not None and panel_enter is not None
                        and panel_enter > state["panel_enter_us"]):
                    _append(root, state, "panel_handoff", stamp, boot, activation_us=panel_enter)
                state.update(panel_enter_us=panel_enter, last_mono=current_mono, last_utc=stamp,
                             observed_seconds=state["observed_seconds"] + elapsed)
                sample["local_screen"] = was_active or active
                _append(root, state, "sample", stamp, boot, monotonic=current_mono, **sample)
                if not _fault_step(root, state, sample, stamp, current_mono, boot):
                    _finalize(root, state, "sample-error", time.time(), boot)
                    return
                _save(root, state)
                if state["observed_seconds"] >= state["hours"] * 3600:
                    _finalize(root, state, "duration", time.time(), boot)
                    return
            except (OSError, ValueError, subprocess.SubprocessError, TimeoutError):
                _finalize(root, state, "sample-error", time.time(), boot)
                raise
        time.sleep(max(0, sample_start + INTERVAL - time.monotonic()))


def main():
    if list(sys.argv[1:]) != ["collect"] or not MIND.is_mount() or is_try_boot():
        return 2
    installed_mapping(CMDLINE.read_text())
    collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
