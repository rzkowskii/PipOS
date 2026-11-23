"""Write a verified fresh installation from the running Try USB."""

import errno
import binascii
import fcntl
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import install_disks
from .initiative import InitiativePolicy, save_policy
from .profile import Profile, save_profile
from .try_mode import is_try_boot


MIB = 1024 ** 2
MOUNTINFO = Path("/proc/self/mountinfo")
MEMINFO = Path("/proc/meminfo")
MODELS = Path("/models")
BOOTLOADER = Path("/usr/share/being/install/bootx64.efi")
GRUB_TEMPLATE = Path("/usr/share/being/installed-grub.cfg")
BLKSSZGET = 0x1268
BLKFLSBUF = 0x1261
BLKGETSIZE64 = 0x80081272
BLKGETDISKSEQ = 0x80081280
GPT_ENTRIES = 128
GPT_ENTRY_SIZE = 128
CHUNK = MIB
PARTITIONS = ("esp", "system-a", "system-b", "models", "mind")
TYPES = (
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    "0fc63daf-8483-4772-8e79-3d69d8477de4",
)


def geometry(plan):
    """Validate the sector layout independently of the presentation layer."""
    disk = plan["target"]
    sector = disk["logical_sector_bytes"]
    capacity = disk["size_bytes"]
    if sector not in (512, 4096) or capacity % sector or capacity < 4 * 1024 * MIB:
        raise ValueError("Unsupported target sector geometry.")
    parts = plan["partitions"]
    if tuple(part["name"] for part in parts) != PARTITIONS:
        raise ValueError("Expected the five PipOS partitions.")
    next_byte = MIB
    for part, filesystem in zip(parts, ("vfat", "squashfs", "squashfs", "ext4", "ext4")):
        kind = TYPES[0 if filesystem == "vfat" else 1]
        length = part["sector_count"] * sector
        if (part["start_lba"] * sector != next_byte or length != part["size_bytes"]
                or length <= 0 or length % MIB
                or part["gpt_type"] != kind or part["filesystem"] != filesystem):
            raise ValueError("Invalid PipOS partition layout.")
        next_byte += length
    if next_byte > capacity - MIB or parts[0]["size_bytes"] < (288 if sector == 4096 else 64) * MIB:
        raise ValueError("Target layout leaves insufficient room for the boot records.")
    return sector, capacity


def gpt_records(sector, capacity, parts, disk_id, ids):
    """Return protective MBR, two GPT headers, and the identical entry table."""
    sectors = capacity // sector
    table = bytearray(GPT_ENTRIES * GPT_ENTRY_SIZE)
    table_sectors = (len(table) + sector - 1) // sector
    last_lba = sectors - 1
    backup_table = last_lba - table_sectors
    first_usable = 2 + table_sectors
    last_usable = backup_table - 1
    for index, part in enumerate(parts):
        first = part["start_lba"]
        last = first + part["sector_count"] - 1
        if first < first_usable or last > last_usable:
            raise ValueError("Partition overlaps a GPT header or table.")
        struct.pack_into(
            "<16s16sQQQ72s", table, index * GPT_ENTRY_SIZE,
            uuid.UUID(part["gpt_type"]).bytes_le, ids[part["name"]].bytes_le,
            first, last, 0, part["name"].encode("utf-16le"),
        )
    table_crc = binascii.crc32(table)

    def table_header(current, alternate, entries):
        header = bytearray(sector)
        struct.pack_into(
            "<8sIIIIQQQQ16sQIII", header, 0,
            b"EFI PART", 0x10000, 92, 0, 0, current, alternate, first_usable,
            last_usable, disk_id.bytes_le, entries, GPT_ENTRIES, GPT_ENTRY_SIZE, table_crc,
        )
        struct.pack_into("<I", header, 16, binascii.crc32(header[:92]))
        return header

    protective_mbr = bytearray(sector)
    struct.pack_into("<B3sB3sII", protective_mbr, 446, 0, b"\x00\x02\x00", 0xEE,
                     b"\xff\xff\xff", 1, min(last_lba, 0xffffffff))
    protective_mbr[510:512] = b"\x55\xaa"
    return (protective_mbr, table_header(1, last_lba, 2), table,
            table_header(last_lba, 1, backup_table), backup_table * sector)


def pread_exact(fd, size, offset):
    data = bytearray()
    while len(data) < size:
        chunk = os.pread(fd, size - len(data), offset + len(data))
        if not chunk:
            raise OSError("Unexpected end of device or image.")
        data.extend(chunk)
    return data


def pwrite_all(fd, data, offset):
    view = memoryview(data)
    while view:
        length = os.pwrite(fd, view, offset)
        if length <= 0:
            raise OSError("Short write to the target.")
        view = view[length:]
        offset += length


def mounted_partition(mountpoint, filesystem, disk, blocks):
    matches = []
    for line in MOUNTINFO.read_text().splitlines():
        raw, separator, typed = line.partition(" - ")
        fields, fs = raw.split(), typed.split()
        if not separator or len(fields) < 6 or len(fs) < 3:
            raise ValueError("Invalid mount table.")
        if install_disks.unescape_proc(fields[4]) != mountpoint:
            continue
        partition = blocks.get(fields[2])
        if partition is None:
            source = install_disks.unescape_proc(fs[1])
            partition = blocks.get(install_disks.node_number(source))
        if (partition is None or partition.parent != disk.path or fs[0] != filesystem
                or "ro" not in fields[5].split(",") or "ro" not in fs[2].split(",")):
            raise ValueError(f"{mountpoint} must be read-only and directly on the running Try USB.")
        matches.append(partition)
    if len(matches) != 1:
        raise ValueError(f"Could not resolve a single source partition for {mountpoint}.")
    return matches[0]


def verify_fd(fd, number, *, sequence=None, sector=None, capacity=None):
    opened = os.fstat(fd)
    if not stat.S_ISBLK(opened.st_mode) or f"{os.major(opened.st_rdev)}:{os.minor(opened.st_rdev)}" != number:
        raise ValueError("A block device no longer matches the approved disk.")

    def claim_value(command, fmt):
        buffer = bytearray(struct.calcsize(fmt))
        fcntl.ioctl(fd, command, buffer, True)
        return struct.unpack(fmt, buffer)[0]

    if sequence is not None and (
        claim_value(BLKGETDISKSEQ, "=Q") != sequence or claim_value(BLKSSZGET, "=I") != sector
        or claim_value(BLKGETSIZE64, "=Q") != capacity
    ):
        raise ValueError("The target device sequence or geometry changed.")


@contextmanager
def source_device(plan):
    blocks, failed = install_disks.block_devices()
    boot = next((block for block in blocks.values() if block.device == plan["boot_disk"]), None)
    if failed or boot is None or boot.parent is not None or install_disks.disk_transport(boot) != "usb":
        raise ValueError("Could not verify the running Try USB.")
    root = mounted_partition("/", "squashfs", boot, blocks)
    mounted_partition(str(MODELS), "ext4", boot, blocks)
    descriptor = os.open(root.device, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        verify_fd(descriptor, root.number)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def claim_target(plan, recheck, sector, capacity):
    disk = plan["target"]
    sequence = disk.get("device_sequence")
    if type(sequence) is not int or sequence <= 0 or not disk["eligible"]:
        raise ValueError("The approved target has no verified device sequence.")
    if recheck() != plan:
        raise ValueError("The approved layout or target changed. Review it again.")
    descriptor = os.open(disk["device"], os.O_RDWR | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        verify_fd(descriptor, disk["device_number"], sequence=sequence, sector=sector, capacity=capacity)
        if recheck() != plan:
            raise ValueError("The approved layout or target changed while acquiring it.")
        yield descriptor
    finally:
        os.close(descriptor)


def squashfs_size(fd, sector):
    superblock = pread_exact(fd, 96, 0)
    used = struct.unpack_from("<Q", superblock, 40)[0]
    version = struct.unpack_from("<HH", superblock, 28)
    if superblock[:4] != b"hsqs" or version != (4, 0) or used < len(superblock):
        raise ValueError("The live system does not have a supported SquashFS superblock.")
    return (used + sector - 1) // sector * sector


def model_size():
    reflex = MODELS / "reflex.gguf"
    if not reflex.is_file() or reflex.stat().st_size == 0:
        raise ValueError("The model pack does not contain a reflex model.")
    total = 0
    filesystem = MODELS.stat().st_dev
    for parent, directories, files in os.walk(MODELS, followlinks=False):
        for name in (*directories, *files):
            item = (Path(parent) / name).lstat()
            if item.st_dev != filesystem or not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode)):
                raise ValueError("The source model pack contains a link, nested mount or special file.")
            if stat.S_ISREG(item.st_mode):
                total += item.st_size
    return total


def run_tool(*args):
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=900)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"{args[0]} failed ({result.returncode}): {detail[-450:]}")


@contextmanager
def staging(size):
    available = re.search(r"^MemAvailable:\s+(\d+) kB$", MEMINFO.read_text(), re.MULTILINE)
    if available is None or int(available[1]) * 1024 < size + 512 * MIB:
        raise ValueError("Insufficient free RAM to stage this installation. The target is untouched.")
    with tempfile.TemporaryDirectory(prefix="being-install-", dir="/run") as directory:
        run_tool("mount", "-t", "tmpfs", "-o", f"noswap,nodev,nosuid,noexec,mode=0700,size={size}",
                 "tmpfs", directory)
        try:
            yield Path(directory)
        finally:
            run_tool("umount", directory)


def empty_image(path, length):
    with path.open("xb") as output:
        output.truncate(length)


def boot_files(directory, ids):
    root = directory / "EFI" / "BOOT"
    root.mkdir(parents=True)
    if not BOOTLOADER.is_file() or BOOTLOADER.stat().st_size <= 512:
        raise ValueError("The Try USB has no packaged UEFI loader.")
    shutil.copyfile(BOOTLOADER, root / "bootx64.efi")
    config = GRUB_TEMPLATE.read_text()
    tokens = ("INSTALL_ESP_UUID", "SLOT_A_UUID", "SLOT_B_UUID", "INSTALL_MODELS_UUID", "INSTALL_MIND_UUID")
    for token, name in zip(tokens, PARTITIONS):
        if token not in config:
            raise ValueError(f"The installed boot template is missing {token}.")
        config = config.replace(token, str(ids[name]))
    (root / "grub.cfg").write_text(config)
    run_tool("grub-editenv", root / "grubenv", "create")
    run_tool("grub-editenv", root / "grubenv", "set", "ORDER=A B", "GOOD=A", "A_OK=1",
             "A_TRY=0", "A_LEFT=0", "B_OK=1", "B_TRY=0", "B_LEFT=0")


def restore_mind_image(path, directory, restore):
    mind = directory / "restore-mind"
    mind.mkdir(mode=0o700)
    run_tool("mount", "-t", "ext4", "-o", "loop,nodev,nosuid,noexec", path, mind)
    try:
        restore(mind)
    finally:
        run_tool("umount", mind)


def create_images(directory, parts, ids, sector, *, profile=None, initiative=None, restore=None):
    images = {}
    for part in parts:
        name = part["name"]
        length = part["size_bytes"]
        if name.startswith("system-"):
            continue
        path = directory / f"{name}.img"
        empty_image(path, length)
        if name == "esp":
            boot_files(directory, ids)
            volume_id = f"{uuid.uuid4().int & 0xffffffff:08x}"
            # One sector per cluster keeps this FAT32 volume above the
            # minimum cluster count even on a 4096-byte logical sector disk.
            run_tool("mkfs.fat", "-F", "32", "-S", str(sector), "-s", "1",
                     "-i", volume_id, "-n", "BEINGOS", path)
            run_tool("mcopy", "-i", path, "-s", directory / "EFI", "::/")
            check = directory / "checked"
            check.mkdir()
            for name_on_esp in ("bootx64.efi", "grub.cfg", "grubenv"):
                checked = check / name_on_esp
                run_tool("mcopy", "-i", path, f"::/EFI/BOOT/{name_on_esp}", checked)
                if checked.read_bytes() != (directory / "EFI" / "BOOT" / name_on_esp).read_bytes():
                    raise OSError(f"The staged EFI file {name_on_esp} could not be verified.")
        else:
            populate = ("-d", MODELS) if name == "models" else ()
            if name == "mind" and (profile is not None or initiative is not None):
                seed = directory / "mind-seed"
                seed.mkdir(mode=0o700)
                if profile is not None:
                    save_profile(seed, profile)
                if initiative is not None:
                    save_policy(seed, initiative)
                populate = ("-d", seed)
            run_tool("mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-m", "0",
                     "-L", name, "-U", str(uuid.uuid4()), "-E", "nodiscard,lazy_itable_init=1",
                     *populate, path)
            if name == "mind" and restore is not None:
                restore_mind_image(path, directory, restore)
            run_tool("e2fsck", "-f", "-n", path)
        images[name] = path
    return images


def extents(fd, length, sparse):
    if not sparse:
        yield 0, length
        return
    position = 0
    while position < length:
        try:
            start = os.lseek(fd, position, os.SEEK_DATA)
        except OSError as error:
            # Linux returns ENXIO when there is no data at or beyond position.
            if error.errno == errno.ENXIO:
                return
            raise
        end = min(length, os.lseek(fd, start, os.SEEK_HOLE))
        yield start, end - start
        position = end


def transfer(source, target, base, length, *, sparse=False, moved=None):
    """Copy required bytes and return a range list and digest for readback.

    `moved`, when given, is told the number of bytes after each chunk. It only
    reports progress and never changes what is written.
    """
    digest = hashlib.sha256()
    ranges = list(extents(source, length, sparse))
    if not ranges:
        raise ValueError("Staged partition has no data.")
    for start, count in ranges:
        for offset in range(start, start + count, CHUNK):
            data = pread_exact(source, min(CHUNK, start + count - offset), offset)
            pwrite_all(target, data, base + offset)
            digest.update(data)
            if moved is not None:
                moved(len(data))
    return base, ranges, digest.digest()


def progress_counter(meter, total):
    """Turn byte counts into meter(done, total) calls, at most every 1/200th."""
    if meter is None or total <= 0:
        return None
    done = 0
    step = max(CHUNK, total // 200)
    last = -step

    def moved(count):
        nonlocal done, last
        done += count
        if done - last >= step or done >= total:
            last = done
            meter(min(done, total), total)

    return moved


def bytes_to_write(images, parts, root_size):
    """Bytes the write stage copies: the system twice and each image's data."""
    total = 0
    for part in parts:
        if part["name"].startswith("system-"):
            total += root_size
        else:
            with images[part["name"]].open("rb", buffering=0) as image:
                total += sum(count for _, count in extents(image.fileno(), part["size_bytes"], True))
    return total


def write_validated(target, capacity, sector, parts, records, images, source, root_size, progress, meter=None):
    mbr, primary, table, backup, backup_table = records
    total = bytes_to_write(images, parts, root_size) if meter is not None else 0

    def invalidate_boot():
        pwrite_all(target, bytes(MIB), 0)
        pwrite_all(target, bytes(MIB), capacity - MIB)
        os.fsync(target)

    invalidate_boot()
    progress("Writing installation data.")
    written = progress_counter(meter, total)
    verification = []
    for part in parts:
        name = part["name"]
        offset, length = part["start_lba"] * sector, part["size_bytes"]
        # Clear stale filesystem signatures at both ends before adding new data.
        boundary = bytes(64 * 1024)
        pwrite_all(target, boundary, offset)
        pwrite_all(target, boundary, offset + length - len(boundary))
        if name.startswith("system-"):
            verification.append(transfer(source, target, offset, root_size, moved=written))
        else:
            with images[name].open("rb", buffering=0) as image:
                verification.append(transfer(image.fileno(), target, offset, length, sparse=True, moved=written))
    os.fsync(target)
    fcntl.ioctl(target, BLKFLSBUF)
    progress("Verifying installation data.")
    checked = progress_counter(meter, total)
    for base, ranges, expected in verification:
        digest = hashlib.sha256()
        for start, count in ranges:
            for offset in range(start, start + count, CHUNK):
                data = pread_exact(target, min(CHUNK, start + count - offset), base + offset)
                digest.update(data)
                if checked is not None:
                    checked(len(data))
        if digest.digest() != expected:
            raise OSError("Written partition data failed verification.")

    progress("Committing the installation boot records.")
    try:
        pwrite_all(target, table, backup_table)
        pwrite_all(target, table, 2 * sector)
        os.fsync(target)
        pwrite_all(target, backup, capacity - sector)
        pwrite_all(target, primary, sector)
        pwrite_all(target, mbr, 0)
        os.fsync(target)
        fcntl.ioctl(target, BLKFLSBUF)
        if any(pread_exact(target, len(data), offset) != data for data, offset in (
            (mbr, 0), (primary, sector), (table, 2 * sector),
            (table, backup_table), (backup, capacity - sector),
        )):
            raise OSError("The installed GPT failed readback verification.")
    except (OSError, ValueError, KeyboardInterrupt):
        try:
            invalidate_boot()
        except OSError:
            pass
        raise


def apply_installation(plan, recheck, *, progress=print, meter=None, profile=None, initiative=None, restore=None):
    """Install from the Try USB. Validate any mind content before claiming the disk.

    `progress` receives each stage message and the optional `meter` receives
    (done, total) bytes within the write and verify stages.
    """
    if sys.platform != "linux" or os.geteuid() != 0 or not is_try_boot():
        raise ValueError("Installation requires a root session booted from Try PipOS USB.")
    if ((profile is not None and not isinstance(profile, Profile))
            or (initiative is not None and not isinstance(initiative, InitiativePolicy))
            or (restore is not None and not callable(restore))):
        raise ValueError("Invalid installed mind initialization.")
    if restore is not None and (profile is not None or initiative is not None):
        raise ValueError("Choose trial settings or a backup for the new installation.")
    sector, capacity = geometry(plan)
    parts = plan["partitions"]
    disk_id = uuid.uuid4()
    ids = {part["name"]: uuid.uuid4() for part in parts}
    records = gpt_records(sector, capacity, parts, disk_id, ids)
    with source_device(plan) as source:
        root_size = squashfs_size(source, sector)
        if any(root_size > part["size_bytes"] for part in parts if part["name"].startswith("system-")):
            raise ValueError("The live system image will not fit the planned slots.")
        bytes_to_stage = model_size() + parts[-1]["size_bytes"] // 256 + 256 * MIB
        progress("Preparing the installation files in RAM.")
        with staging(bytes_to_stage) as directory:
            images = create_images(directory, parts, ids, sector, profile=profile, initiative=initiative, restore=restore)
            with claim_target(plan, recheck, sector, capacity) as target:
                write_validated(target, capacity, sector, parts, records, images, source, root_size, progress,
                                meter)
    return {"disk_guid": str(disk_id), "partuuids": {name: str(value) for name, value in ids.items()}}
