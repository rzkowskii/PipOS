"""Metadata-only recognition of newly arrived USB storage.

Reads sysfs and udev's existing database only. It never opens a block device,
mounts a filesystem, scans contents, or puts identifiers/labels on the bus.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from . import install_disks

UDEV = Path("/run/udev/data")
MAX_UDEV = 64 * 1024
BACKUP_LABELS = frozenset(("being-backup", "pip-backup", "beingbackup", "pipbackup"))
PIP_PARTS = ("esp", "system-a", "system-b", "models", "mind")
APPLE_TYPES = frozenset((
    "48465300-0000-11aa-aa11-00306543ecac",  # HFS+
    "7c3457ef-0000-11aa-aa11-00306543ecac",  # APFS
    "426f6f74-0000-11aa-aa11-00306543ecac",  # Apple boot
))
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,31}")


@dataclass(frozen=True)
class Arrival:
    capacity_bytes: int
    label: str | None
    kind: str

    def line(self):
        gib = self.capacity_bytes / (1024 ** 3)
        amount = max(1, round(gib))
        capacity = f"about {amount} {'gigabyte' if amount == 1 else 'gigabytes'}"
        if self.kind == "backup":
            what = "your backup drive"
        elif self.kind == "pipos":
            what = "another PipOS drive"
        elif self.kind == "mac":
            what = "a Mac-formatted drive"
        else:
            what = "a USB drive"
        named = f" called {self.label}" if self.label else ""
        return f"[neutral] I noticed {what}{named}, {capacity}."


def _properties(path):
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_UDEV + 1)
    except OSError:
        return {}
    if len(raw) > MAX_UDEV:
        return {}
    values = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("E:"):
            continue
        key, separator, value = line[2:].partition("=")
        if separator and key not in values and len(key) <= 64 and len(value) <= 256:
            values[key] = value
    return values


def _label(properties):
    value = properties.get("ID_FS_LABEL")
    if value and SAFE_LABEL.fullmatch(value) and not any(ord(char) < 32 for char in value):
        return " ".join(value.split())
    return None


def snapshot(udev=UDEV):
    """{kernel identity: Arrival}; all identity stays inside this process."""
    try:
        blocks, failed = install_disks.block_devices()
    except OSError:
        return {}
    if failed:
        return {}
    whole = [block for block in blocks.values()
             if block.parent is None and install_disks.disk_transport(block) == "usb"]
    result = {}
    for disk in whole[:32]:
        sectors = install_disks.optional_number(disk.path / "size")
        sequence = install_disks.optional_number(disk.path / "diskseq")
        if not sectors or not sequence:
            continue
        parts = sorted((block for block in blocks.values() if block.parent == disk.path), key=lambda item: item.name)
        metadata = [_properties(Path(udev) / f"b{part.number}") for part in (parts or [disk])]
        labels = [label for props in metadata if (label := _label(props))]
        names = tuple(props.get("ID_PART_ENTRY_NAME", "").casefold() for props in metadata)
        types = {props.get("ID_PART_ENTRY_TYPE", "").casefold() for props in metadata}
        if any(label.casefold() in BACKUP_LABELS for label in labels):
            kind = "backup"
        elif names[:len(PIP_PARTS)] == PIP_PARTS:
            kind = "pipos"
        elif types.intersection(APPLE_TYPES):
            kind = "mac"
        else:
            kind = "storage"
        label = next((label for label in labels if label.casefold() not in BACKUP_LABELS), None)
        result[(disk.number, sequence)] = Arrival(sectors * 512, label, kind)
    return result


class Recognizer:
    """Compare arrival snapshots; no old drive is announced after a restart."""

    def __init__(self, *, scan=snapshot):
        self.scan = scan
        self.present = scan()

    def observe(self, attached):
        current = self.scan()
        new = [value for key, value in current.items() if key not in self.present]
        self.present = current
        return new[0] if attached is True and len(new) == 1 else None
