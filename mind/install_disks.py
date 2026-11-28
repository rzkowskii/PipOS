"""Inspect Linux block devices before offering an installation target."""

import binascii
import os
import re
import stat
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SYS_BLOCK = Path("/sys/class/block")
PROC = Path("/proc")
DEV = Path("/dev")
MIN_SIZE_BYTES = 4 * 1024**3


@dataclass(frozen=True)
class Block:
    name: str
    path: Path
    number: str
    parent: Path | None
    slaves: tuple[str, ...]
    held: bool
    read_only: bool

    @property
    def device(self):
        return f"/dev/{self.name}"


def read_number(path):
    value = path.read_text().strip()
    if not re.fullmatch(r"[0-9]+:[0-9]+", value):
        raise ValueError(f"Invalid block device number in {path}")
    return value


def block_devices():
    blocks = {}
    failed = False
    for entry in sorted(SYS_BLOCK.iterdir()):
        try:
            path = entry.resolve(strict=True)
            number = read_number(path / "dev")
            read_only = (path / "ro").read_text().strip()
            if read_only not in {"0", "1"} or number in blocks:
                raise ValueError(f"Inconsistent block device state at {path}")
            parent = path.parent if (path / "partition").exists() else None
            slaves = () if parent else tuple(read_number(slave / "dev") for slave in (path / "slaves").iterdir())
            held = next((path / "holders").iterdir(), None) is not None
            blocks[number] = Block(entry.name, path, number, parent, slaves, held, read_only == "1")
        except (OSError, ValueError):
            # A disappearing or unreadable device may have a live mount or holder.
            failed = True
    return blocks, failed


def disk_origins(blocks):
    paths = {block.path: number for number, block in blocks.items()}
    origins = {}

    def trace(number, visited):
        if number in visited:
            raise ValueError("Block device dependency cycle")
        if number not in origins:
            block = blocks[number]
            dependencies = (paths[block.parent],) if block.parent else block.slaves
            backing_file = block.path / "loop/backing_file"
            if backing_file.exists():
                backing = node_number(backing_file.read_text().rstrip("\n"), allow_file=True)
                if backing is None:
                    raise ValueError("Unable to resolve a loop device backing file")
                dependencies += (backing,)
            origins[number] = (set().union(*(trace(dep, visited | {number}) for dep in dependencies))
                               if dependencies else {number})
        return origins[number]

    failed = False
    for number in blocks:
        try:
            trace(number, set())
        except (OSError, KeyError, ValueError):
            failed = True
    return origins, failed


def unescape_proc(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def node_number(source, *, allow_file=False):
    path = PurePosixPath(source)
    if not path.is_absolute() or ".." in path.parts:
        return None
    if path.is_relative_to("/dev"):
        path = DEV / path.relative_to("/dev")
    try:
        metadata = Path(path).stat()
    except OSError:
        return None
    if stat.S_ISBLK(metadata.st_mode):
        number = metadata.st_rdev
    elif allow_file and stat.S_ISREG(metadata.st_mode):
        number = metadata.st_dev
    else:
        return None
    return f"{os.major(number)}:{os.minor(number)}"


def mounted_disks(origins):
    origins_by_mount = {}
    failed = False
    for line in (PROC / "self/mountinfo").read_text().splitlines():
        before, _, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if len(fields) < 6 or len(filesystem) < 3 or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
            raise ValueError("Invalid mountinfo entry")
        number, mount = fields[2], unescape_proc(fields[4])
        source = unescape_proc(filesystem[1])
        block_backed = not number.startswith("0:") or source.startswith("/dev/")
        if number not in origins and source.startswith("/dev/"):
            number = node_number(source)
        disks = origins.get(number, set())
        # A mount backed by a device we cannot trace invalidates the preflight.
        if (block_backed and not disks) or filesystem[0] in {"btrfs", "bcachefs", "zfs"}:
            failed = True
        origins_by_mount.setdefault(mount, []).append(disks)
    return origins_by_mount, failed


def swap_disks(origins):
    lines = (PROC / "swaps").read_text().splitlines()
    if not lines or not lines[0].split() or lines[0].split()[0] != "Filename":
        raise ValueError("Invalid swaps inventory")
    disks = set()
    failed = False
    for line in lines[1:]:
        fields = line.rsplit(None, 4)
        if len(fields) != 5 or fields[1] not in {"partition", "file"}:
            raise ValueError("Invalid swap entry")
        number = node_number(unescape_proc(fields[0]), allow_file=fields[1] == "file")
        if number in origins:
            disks.update(origins[number])
        else:
            failed = True
    return disks, failed


def optional_text(path):
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeError):
        return None
    return value if value and value.isprintable() else None


def optional_number(path):
    value = optional_text(path)
    return int(value) if value and re.fullmatch(r"[0-9]+", value) else None


def disk_transport(block):
    if "virtual" in block.path.parts or optional_text(block.path / "device/type") not in (None, "0"):
        return None
    for pattern, transport in ((r"usb[0-9]+", "usb"), (r"virtio[0-9]+", "virtio"), (r"ata[0-9]+", "sata"),
                               (r"nvme[0-9]+", "nvme")):
        if any(re.fullmatch(pattern, part) for part in block.path.parts[:-1]):
            return transport
    return None


def disk_serial(block):
    serial = optional_text(block.path / "device/serial") or optional_text(block.path / "serial")
    if serial:
        return serial
    try:
        page = (block.path / "device/vpd_pg80").read_bytes()
        if len(page) >= 4 and page[1] == 0x80 and len(page) == 4 + int.from_bytes(page[2:4], "big"):
            serial = page[4:].decode("ascii").strip()
            return serial if serial and serial.isprintable() else None
    except (OSError, UnicodeError):
        pass
    return None


def mount_origin(mounts, mountpoint, transports):
    sources = mounts.get(mountpoint, [])
    if not sources or any(len(disks) != 1 for disks in sources):
        return None
    resolved = set().union(*sources)
    if len(resolved) != 1:
        return None
    number = next(iter(resolved))
    return number if transports.get(number) else None


def volumes(device):
    """The partitions of a whole disk, or the disk itself when it has none."""
    blocks, _ = block_devices()
    disk = next((block for block in blocks.values() if block.parent is None and block.device == device), None)
    if disk is None:
        return []
    parts = [block for block in blocks.values() if block.parent == disk.path]
    found = []
    for block in sorted(parts or [disk], key=lambda block: block.name):
        sectors = optional_number(block.path / "size")
        if sectors and not block.held:
            found.append({"device": block.device, "size_bytes": sectors * 512})
    return found


def inventory(*, min_size_bytes=MIN_SIZE_BYTES):
    """Return a read-only snapshot. Unknown origins or incomplete checks fail closed."""
    if type(min_size_bytes) is not int or min_size_bytes <= 0:
        raise ValueError("Minimum disk size must be a positive number of bytes")
    try:
        blocks, topology_failed = block_devices()
    except OSError:
        return {"boot_disk": None, "model_disk": None, "disks": []}
    origins, origins_failed = disk_origins(blocks)
    try:
        mounts, mounts_failed = mounted_disks(origins)
    except (OSError, ValueError):
        mounts, mounts_failed = {}, True
    try:
        swaps, swaps_failed = swap_disks(origins)
    except (OSError, ValueError):
        swaps, swaps_failed = set(), True

    failed = topology_failed or origins_failed or mounts_failed or swaps_failed
    whole = {number: block for number, block in blocks.items() if block.parent is None}
    transports = {number: disk_transport(block) for number, block in whole.items()}
    boot = mount_origin(mounts, "/", transports)
    models = mount_origin(mounts, "/models", transports)
    mounted = set().union(*(disks for sources in mounts.values() for disks in sources))
    protected = set().union(*(origins.get(block.number, set()) for block in blocks.values()
                              if block.held or (block.parent is None and origins.get(block.number) != {block.number})))
    read_only = set().union(*(origins.get(block.number, set()) for block in blocks.values() if block.read_only))
    disks = []
    for number, block in whole.items():
        transport = transports[number]
        size_sectors = optional_number(block.path / "size")
        size = (size_sectors or 0) * 512  # This sysfs size always uses 512-byte sectors.
        logical_sector = optional_number(block.path / "queue/logical_block_size")
        inspected = (size_sectors is not None and logical_sector is not None and logical_sector >= 512
                     and logical_sector & (logical_sector - 1) == 0 and node_number(block.device) == number)
        reasons = [reason for applies, reason in (
            (boot is None, "boot_disk_unknown"), (boot == number, "boot_disk"), (number in mounted, "mounted"),
            (number in swaps, "active_swap"), (number in protected or bool(block.slaves), "held_or_stacked"),
            (number in read_only, "read_only"), (not transport, "unsupported_device"),
            (size < min_size_bytes, "too_small"), (failed or not inspected, "inspection_failed"),
        ) if applies]
        disk = {
            "device": block.device, "device_number": number,
            "model": optional_text(block.path / "device/model") or f"{transport or 'Unknown'} block device",
            "size_bytes": size, "logical_sector_bytes": logical_sector,
            "eligible": not reasons, "reasons": reasons,
        }
        if transport:
            disk["transport"] = transport
        serial = disk_serial(block)
        if serial:
            disk["serial"] = serial
        sequence = optional_number(block.path / "diskseq")
        if sequence:
            disk["device_sequence"] = sequence
        disks.append(disk)
    return {"boot_disk": whole[boot].device if boot else None,
            "model_disk": whole[models].device if models else None, "disks": disks}


# --- what a disk already holds (read-only) ------------------------------------

APFS_TYPE = "7c3457ef-0000-11aa-aa11-00306543ecac"
HFS_TYPE = "48465300-0000-11aa-aa11-00306543ecac"
APPLE_BOOT_TYPE = "426f6f74-0000-11aa-aa11-00306543ecac"
CORE_STORAGE_TYPE = "53746f72-6167-11aa-aa11-00306543ecac"
MACOS_TYPES = frozenset((APFS_TYPE, HFS_TYPE, APPLE_BOOT_TYPE, CORE_STORAGE_TYPE))
BEING_NAMES = ("esp", "system-a", "system-b", "models", "mind")
ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX_TYPE = "0fc63daf-8483-4772-8e79-3d69d8477de4"
BEING_TYPES = (ESP_TYPE,) + (LINUX_TYPE,) * (len(BEING_NAMES) - 1)
GPT_ENTRY_LIMIT = 256


def _read_at(fd, size, offset):
    data = b""
    while len(data) < size:
        chunk = os.pread(fd, size - len(data), offset + len(data))
        if not chunk:
            break
        data += chunk
    return data


def _gpt_copy(fd, sector, header_lba, alternate_lba):
    header = bytearray(_read_at(fd, sector, header_lba * sector))
    if header[:8] != b"EFI PART":
        return None
    if len(header) != sector:
        raise ValueError("Incomplete GPT header.")
    revision, header_size, crc = struct.unpack_from("<III", header, 8)
    if revision != 0x10000 or not 92 <= header_size <= sector:
        raise ValueError("Unsupported GPT header.")
    struct.pack_into("<I", header, 16, 0)
    if binascii.crc32(header[:header_size]) != crc:
        raise ValueError("Invalid GPT header checksum.")
    own, alternate, first_usable, last_usable = struct.unpack_from("<QQQQ", header, 24)
    last_lba = max(header_lba, alternate_lba)
    if ((own, alternate) != (header_lba, alternate_lba)
            or not 2 <= first_usable <= last_usable < last_lba):
        raise ValueError("Invalid GPT header bounds.")
    entries_lba, count, size, table_crc = struct.unpack_from("<QIII", header, 72)
    if not 128 <= size <= 4096 or not 1 <= count <= GPT_ENTRY_LIMIT or size % 8:
        raise ValueError("Unsupported GPT entry layout.")
    end_lba = entries_lba + (count * size + sector - 1) // sector
    if header_lba == 1:
        table_fits = 2 <= entries_lba < end_lba <= first_usable
    else:
        table_fits = last_usable < entries_lba < end_lba <= header_lba
    if not table_fits:
        raise ValueError("Invalid GPT entry table bounds.")
    table = _read_at(fd, count * size, entries_lba * sector)
    if len(table) != count * size or binascii.crc32(table) != table_crc:
        raise ValueError("Invalid GPT entry table checksum.")
    found = []
    for index in range(count):
        entry = table[index * size:(index + 1) * size]
        if not any(entry[:16]):
            continue
        kind = str(uuid.UUID(bytes_le=bytes(entry[:16])))
        first, last = struct.unpack_from("<QQ", entry, 32)
        if not first_usable <= first <= last <= last_usable:
            raise ValueError("Invalid GPT partition bounds.")
        name = entry[56:128].decode("utf-16le", "replace").split("\0", 1)[0]
        found.append((kind, name, first, last))
    ranges = sorted((first, last) for _, _, first, last in found)
    if any(last >= following for (_, last), (following, _) in zip(ranges, ranges[1:])):
        raise ValueError("Overlapping GPT partitions.")
    return (header[56:72], first_usable, last_usable, table, found)


def gpt_partitions(fd, sector, capacity=None):
    """Read a usable GPT copy and reject conflicting valid copies."""
    if capacity is None:
        capacity = os.lseek(fd, 0, os.SEEK_END)
    last_lba = capacity // sector - 1
    if capacity % sector or last_lba <= 1:
        return None
    copies = []
    damaged = False
    for own, alternate in ((1, last_lba), (last_lba, 1)):
        try:
            copy = _gpt_copy(fd, sector, own, alternate)
        except ValueError:
            damaged = True
            continue
        if copy is not None:
            copies.append(copy)
    if copies:
        if len(copies) == 2 and copies[0][:-1] != copies[1][:-1]:
            raise ValueError("The primary and backup GPT disagree.")
        return copies[0][-1]
    mbr = _read_at(fd, 512, 0)
    protective = mbr[510:512] == b"\x55\xaa" and any(mbr[index] == 0xee for index in (450, 466, 482, 498))
    if damaged or protective:
        raise ValueError("The disk has no readable GPT.")
    return None


def filesystem_signature(fd, offset, length=1536):
    """apfs, hfs or None, from the first bytes of a partition."""
    head = _read_at(fd, min(length, 1536), offset)
    if head[32:36] == b"NXSB":
        return "apfs"
    if head[1024:1026] in (b"H+", b"HX"):
        return "hfs"
    return None


def inspect_target(fd, sector, capacity):
    """Return the disk summary and the validated archive extent, if any."""
    result = {"macos": False, "being": False, "partitions": 0, "readable": False}
    mind_range = None
    try:
        parts = gpt_partitions(fd, sector, capacity) or ()
        result["partitions"] = len(parts)
        pipos = parts[:len(BEING_NAMES)]
        result["being"] = tuple(name for _, name, _, _ in pipos) == BEING_NAMES
        for kind, _, first, last in parts:
            if kind in MACOS_TYPES or filesystem_signature(fd, first * sector, (last - first + 1) * sector):
                result["macos"] = True
                break
        if (result["being"] and tuple(kind for kind, *_ in pipos) == BEING_TYPES
                and sum(name == "mind" for _, name, _, _ in parts) == 1):
            _, _, first, last = pipos[-1]
            mind_range = (first * sector, (last - first + 1) * sector)
        result["readable"] = True
    except (OSError, ValueError):
        pass
    return result, mind_range


def disk_contents(device, sector=512):
    """Inspect the selected disk without changing its contents."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            capacity = os.lseek(fd, 0, os.SEEK_END)
            result, _ = inspect_target(fd, sector, capacity)
            return result
        finally:
            os.close(fd)
    except OSError:
        return {"macos": False, "being": False, "partitions": 0, "readable": False}
