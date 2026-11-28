"""Remember healthy installed slots and schedule a single rollback trial."""

import fcntl
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .install_binding import PARTUUIDS, installed_mapping, verify_partitions
from .install_disks import node_number
from .memory import SCHEMA_VERSION


CMDLINE = Path("/proc/cmdline")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
BOOTSTATE = Path("/bootstate")
BOOT_ENV = BOOTSTATE / "EFI/BOOT/grubenv"
LOCK = Path("/run/being-slot-selection.lock")
LOCK_WAIT = 5.0
HEALTHY = "PIP_HEALTHY_SLOT"
PREVIOUS = "PIP_PREVIOUS_GOOD"
ROLLBACK = "PIP_ROLLBACK"
LAST_UPDATE = "PIP_LAST_UPDATE_SLOT"


@dataclass(frozen=True)
class Rollback:
    current: str
    previous: str


def _bound_slot():
    guids, active = installed_mapping(CMDLINE.read_text())
    root = os.stat("/").st_dev
    verify_partitions(guids, active, f"{os.major(root)}:{os.minor(root)}")
    esp = os.stat(BOOTSTATE).st_dev
    mounted = f"{os.major(esp)}:{os.minor(esp)}"
    if not os.path.ismount(BOOTSTATE) or mounted != node_number(str(PARTUUIDS / guids[0])):
        raise ValueError("The installed boot-state partition is unavailable.")
    return ("A", "B")[active - 1]


def _read_environment(timeout=10):
    text = subprocess.check_output(["grub-editenv", BOOT_ENV, "list"], text=True, timeout=timeout)
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not key or not separator or key in values:
            raise ValueError("The installed boot state is malformed.")
        values[key] = value
    return values


def _save(values):
    if values:
        subprocess.run(["grub-editenv", BOOT_ENV, "set", *(f"{key}={value}" for key, value in values.items())],
                       check=True, timeout=10)
        with BOOT_ENV.open("rb") as file:
            os.fsync(file.fileno())


@contextmanager
def _boot_state():
    if os.geteuid() != 0:
        raise ValueError("Use the installed PipOS owner console.")
    fd = os.open(LOCK, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("The installed slot lock is not private.")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another update or boot-state operation is still in progress.") from None
                time.sleep(0.05)
        yield _bound_slot(), _read_environment()
    finally:
        os.close(fd)


def _receipt(slot):
    return f"{slot}:{SCHEMA_VERSION}"


def _rollback_is_pending(environment):
    request = environment.get(ROLLBACK)
    return bool(request and request.partition(":")[0] == BOOT_ID.read_text().strip())


def mark_healthy(slot):
    """Publish a successful installed boot before exposing its runtime receipt."""
    with _boot_state() as (booted, environment):
        if slot != booted:
            raise ValueError("The healthy slot changed before confirmation.")
        previous = environment.get("GOOD")
        values = {}
        if previous != slot:
            values.update({"GOOD": slot, f"{slot}_OK": "1", f"{slot}_TRY": "0"})
            receipt = environment.get(HEALTHY, "")
            if (previous in ("A", "B") and receipt == _receipt(previous)
                    and environment.get(f"{previous}_OK") == "1" and environment.get(f"{previous}_TRY") == "0"):
                values[PREVIOUS] = receipt
            else:
                values[PREVIOUS] = ""
        if environment.get(HEALTHY) != _receipt(slot):
            values[HEALTHY] = _receipt(slot)
        if environment.get("PIP_UNSEEN", "0") != "0":
            values["PIP_UNSEEN"] = "0"
        if environment.get(ROLLBACK) and not _rollback_is_pending(environment):
            values[ROLLBACK] = ""
        _save(values)


def check_update_admission():
    """Check admission before spending time and space on an archive snapshot."""
    with _boot_state() as (slot, environment):
        _require_update_admission(slot, environment)


def _require_update_admission(slot, environment):
    if environment.get("GOOD") != slot:
        raise ValueError("PipOS must confirm the running slot before installing an update.")
    if _rollback_is_pending(environment):
        raise ValueError("A restart to the previous system is already scheduled.")


def admitted_update():
    """Recheck admission and discard old proofs before RAUC replaces a slot."""
    with _boot_state() as (slot, environment):
        _require_update_admission(slot, environment)
        _save({key: "" for key in (PREVIOUS, LAST_UPDATE) if environment.get(key)})


def record_installed_update():
    """Record the target after RAUC has written and activated the update."""
    with _boot_state() as (slot, environment):
        _require_update_admission(slot, environment)
        target = "B" if slot == "A" else "A"
        if (environment.get("ORDER", "").split() != [target, slot]
                or environment.get(f"{target}_OK") != "1" or environment.get(f"{target}_TRY") != "0"):
            raise ValueError("RAUC did not leave the spare slot ready for the next boot.")
        _save({LAST_UPDATE: target})


def update_environment():
    """Read the installed boot evidence for a spoken update-status request."""
    try:
        return _read_environment(timeout=1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _plan(slot, environment):
    if environment.get("GOOD") != slot:
        raise ValueError("The running slot is still a trial.")
    if _rollback_is_pending(environment):
        raise ValueError("A restart to the previous system is already scheduled.")
    other = "B" if slot == "A" else "A"
    if (environment.get(f"{slot}_OK") != "1" or environment.get(f"{slot}_TRY") != "0"
            or environment.get(HEALTHY) != _receipt(slot) or environment.get(PREVIOUS) != _receipt(other)
            or environment.get(f"{other}_OK") != "1" or environment.get(f"{other}_TRY") != "0"):
        raise ValueError("There is no previous confirmed system with a compatible archive format.")
    return Rollback(slot, other)


def rollback_plan():
    with _boot_state() as (slot, environment):
        return _plan(slot, environment)


def schedule_rollback(expected):
    """Recheck the displayed choice and arm one attempt, keeping GOOD as fallback."""
    with _boot_state() as (slot, environment):
        plan = _plan(slot, environment)
        if plan != expected:
            raise ValueError("The available systems changed. Review them again.")
        previous = plan.previous
        # GRUB spends LEFT=1 before booting a tried, non-GOOD slot. If it
        # cannot confirm, the remaining zero attempts lead back to GOOD.
        _save({"ORDER": f"{previous} {slot}", f"{previous}_TRY": "1", f"{previous}_LEFT": "1",
               ROLLBACK: f"{BOOT_ID.read_text().strip()}:{previous}"})


if __name__ == "__main__":
    try:
        record_installed_update()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # RAUC already activated the update. A missing status receipt must not
        # report that this completed write failed or change the slot selection.
        print(f"Could not record the installed update for spoken status: {error}", file=sys.stderr, flush=True)
