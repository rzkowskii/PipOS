"""Verify an installed boot's partition aliases before mounting or writing."""

import os
import sys
import uuid
from pathlib import Path

from .install_disks import node_number, read_number


PARTITIONS = ("esp", "slot_a", "slot_b", "models", "mind")
PARTUUIDS = Path("/dev/disk/by-partuuid")
SYS_DEV_BLOCK = Path("/sys/dev/block")


def installed_mapping(cmdline):
    fields = ("being.installed", "being.try", "root", "rauc.slot", "fstab", "systemd.gpt_auto", "being.mind_crypt")
    fields += tuple(f"being.{field}" for field in PARTITIONS)
    values = {}
    for item in cmdline.split():
        key, _, value = item.partition("=")
        if key in fields:
            if key in values:
                raise ValueError(f"Repeated installed boot option {key}")
            values[key] = value

    if (values.get("being.installed"), values.get("fstab"), values.get("systemd.gpt_auto")) != ("1", "no", "no"):
        raise ValueError("Installed boot options are incomplete")
    if "being.try" in values:
        raise ValueError("Try and installed boot options coexist")
    if values.get("being.mind_crypt", "1") != "1":
        raise ValueError("Invalid encrypted memory option")

    guids = tuple(values.get(f"being.{field}", "") for field in PARTITIONS)
    for value in guids:
        parsed = uuid.UUID(value)
        if str(parsed) != value or not parsed.int:
            raise ValueError("Invalid installed partition UUID")
    if len(set(guids)) != len(guids):
        raise ValueError("Installed partition UUIDs must be distinct")

    slots = {"A": 1, "B": 2}
    active = slots.get(values.get("rauc.slot"))
    if active is None or values.get("root") != f"PARTUUID={guids[active]}":
        raise ValueError("Booted slot does not match the root partition option")
    return guids, active


def verify_partitions(guids, active, root_number):
    """The alias for each partition must be on the running root's actual disk."""
    parent = None
    for index, guid in enumerate(guids):
        device = node_number(str(PARTUUIDS / guid))
        if device is None:
            raise ValueError(f"Missing block device for {PARTITIONS[index]}")
        partition = (SYS_DEV_BLOCK / device).resolve(strict=True)
        if read_number(partition / "dev") != device:
            raise ValueError(f"Device identity changed for {PARTITIONS[index]}")
        if (partition / "partition").read_text().strip() != str(index + 1):
            raise ValueError(f"Unexpected partition number for {PARTITIONS[index]}")
        if parent is None:
            parent = partition.parent
        elif partition.parent != parent:
            raise ValueError("Installed partitions belong to different disks")
        if index == active and device != root_number:
            raise ValueError("Installed root alias does not identify the running root filesystem")


def main():
    try:
        guids, active = installed_mapping(Path("/proc/cmdline").read_text())
        root = os.stat("/").st_dev
        verify_partitions(guids, active, f"{os.major(root)}:{os.minor(root)}")
    except (OSError, ValueError) as exc:
        print(f"being-installed-bind: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
