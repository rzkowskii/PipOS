"""Audit an actual PipOS disk image without mounting or booting it."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile


DRIVERS = {
    "b43": "b43",
    "bcma": "bcma",
    "gigabit_ethernet": "tg3",
    "hda_controller": "snd-hda-intel",
    "cirrus_cs420x": "snd-hda-codec-cs420x",
    "cirrus_cs421x": "snd-hda-codec-cs421x",
    "intel_graphics": "i915",
    "apple_smc": "applesmc",
    "cpu_temperature": "coretemp",
    "acpi_lid": "button",
    "acpi_mains": "ac",
    "acpi_battery": "battery",
    "acpi_smart_battery": "sbs",
    "acpi_video": "video",
    "backlight": "backlight",
    "rapl": "intel_rapl_msr",
    "package_thermal": "x86_pkg_temp_thermal",
}
TOOLS = (
    "iw", "wpa_supplicant", "wpa_cli", "ip", "ethtool", "lspci", "networkctl",
    "arecord", "aplay", "amixer", "alsactl", "journalctl", "sshd", "ssh-keygen",
    "rauc", "being-installer", "being-lab-access", "being-network", "nft",
)


def seven(tool, *args):
    return subprocess.run([tool, *args], capture_output=True, timeout=30, check=True).stdout


def entries(raw):
    for block in raw.decode("utf-8").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" in fields:
            yield fields


def entries_for(tool, kind, path):
    return list(entries(seven(tool, "l", "-ba", "-slt", f"-t{kind}", str(path))))


def member(tool, kind, path, name):
    return seven(tool, "x", "-so", "-spd", f"-t{kind}", str(path), name)


def copy_partition(source, partition, destination, *, squashfs=False):
    """Copy a read-only image range into a private temporary file."""
    offset, length = (int(partition[field]) for field in ("Offset", "Size"))
    if offset < 0 or length <= 0 or offset + length > source.stat().st_size:
        raise ValueError(f"Invalid partition extent for {partition['Path']}.")
    with source.open("rb") as image:
        if squashfs:
            image.seek(offset)
            header = image.read(96)
            if len(header) != 96 or header[:4] != b"hsqs":
                raise ValueError(f"Expected SquashFS at {partition['Path']}.")
            used, = struct.unpack_from("<Q", header, 40)
            if not len(header) <= used <= length:
                raise ValueError(f"Invalid SquashFS byte count for {partition['Path']}.")
            length = used
        image.seek(offset)
        digest = hashlib.sha256()
        with destination.open("wb") as temporary:
            while length:
                block = image.read(min(length, 1 << 20))
                if not block:
                    raise ValueError(f"Short read in {partition['Path']}.")
                temporary.write(block)
                digest.update(block)
                length -= len(block)
        return digest.hexdigest()


def module_name(path):
    return Path(path).name.split(".ko", 1)[0].replace("-", "_")


def kernel_drivers(paths, builtin):
    built = {module_name(line) for line in builtin.splitlines() if line.endswith(".ko")}
    modules = {module_name(path) for path in paths if re.search(r"\.ko(?:\.(?:gz|xz|zst))?$", path)}
    return {
        key: "built-in" if module_name(name) in built else "module" if module_name(name) in modules else None
        for key, name in DRIVERS.items()
    }


def root_report(tool, path):
    kind = "SquashFS"
    paths = {item["Path"] for item in entries_for(tool, kind, path)}
    version_file = next((name for name in sorted(paths) if name.endswith("/modules.builtin")), None)
    builtin = member(tool, kind, path, version_file).decode() if version_file else ""
    build_file = "etc/being/build-id"
    gallium = next((name for name in sorted(paths) if re.fullmatch(r"usr/lib/libgallium-[\d.]+\.so", name)), None)
    crocus = "usr/lib/dri/crocus_dri.so" in paths
    if not crocus and gallium:
        binary = member(tool, kind, path, gallium)
        crocus = all(marker in binary for marker in (b"crocus_resolve_color\0", b"crocus_hiz_exec\0"))
    sdl = next((name for name in sorted(paths, reverse=True)
                if re.fullmatch(r"usr/lib/libSDL2-2\.0\.so(?:\.\d+)+", name)), None)
    env_file = "etc/being/env"
    env = member(tool, kind, path, env_file).decode() if env_file in paths else ""
    iw = next((name for name in ("usr/bin/iw", "usr/sbin/iw") if name in paths), None)
    metrics = "usr/lib/being/mind/hardware_metrics.py"
    return {
        "build_id": member(tool, kind, path, build_file).decode().strip() if build_file in paths else None,
        "kernel": Path(version_file).parent.name if version_file else None,
        "drivers": kernel_drivers(paths, builtin),
        "b43_firmware": sorted(name for name in paths if "/firmware/b43/" in name
                               and re.search(r"\.fw(?:\.(?:xz|zst))?$", name)),
        "tools": sorted(name for name in TOOLS if any(f"{prefix}/{name}" in paths for prefix in ("usr/bin", "usr/sbin"))),
        "mesa_crocus_marker": crocus,
        "sdl2_kmsdrm_marker": bool(sdl and b"KMSDRM" in member(tool, kind, path, sdl)),
        "iw_station_dump_marker": bool(iw and b"List all stations known" in member(tool, kind, path, iw)),
        "legacy_lab_timeout_overrides": dict(re.findall(r"^BEING_(THINK|SPEAK)_TIMEOUT=(\d+)$", env, re.MULTILINE)),
        "networkd": "usr/lib/systemd/systemd-networkd" in paths,
        "boot_network_firewall": "usr/lib/systemd/system/being-network-lockdown.service" in paths,
        "timesyncd_enabled_by_preset": "etc/systemd/system/sysinit.target.wants/systemd-timesyncd.service" in paths,
        "installed_update_binding": "usr/lib/being/mind/install_binding.py" in paths,
        "installer_writer": "usr/lib/being/mind/install_apply.py" in paths,
        "lan_observer": "usr/lib/systemd/system/being-lab-observe.service" in paths,
        "lan_development": "usr/lib/systemd/system/being-lab-dev.service" in paths,
        "hardware_metrics_sha256": hashlib.sha256(member(tool, kind, path, metrics)).hexdigest() if metrics in paths else None,
        "hardware_timing": "usr/lib/being/mind/hardware_timing.py" in paths,
    }


def inspect(path, tool):
    if not path.is_file():
        raise ValueError("Provide a regular PipOS image file.")
    # Use a generated GPT partition name, independent of UUIDs and sector offsets.
    parts = {item["Path"].split(".", 1)[1].removesuffix(".img"): item
             for item in entries_for(tool, "GPT", path)
             if re.fullmatch(r"\d+\.[\w-]+\.img", item["Path"])}
    roots = [name for name in ("try-system", "system-a", "system-b") if name in parts]
    esp = parts.get("try-esp") or parts.get("esp")
    if not roots or esp is None:
        raise ValueError("Expected a PipOS GPT with EFI and system partitions.")
    with tempfile.TemporaryDirectory(prefix="being-image-audit-") as temporary:
        directory = Path(temporary)
        boot = directory / "boot.fat"
        copy_partition(path, esp, boot)
        boot_paths = {item["Path"].lower() for item in entries_for(tool, "FAT", boot)}
        grub = member(tool, "FAT", boot, "EFI/BOOT/grub.cfg").decode()
        reports = {}
        inspections = {}
        for name in roots:
            system = directory / "system.squashfs"
            digest = copy_partition(path, parts[name], system, squashfs=True)
            if digest not in inspections:
                inspections[digest] = root_report(tool, system)
            reports[name] = {"squashfs_bytes_sha256": digest, **inspections[digest]}
    return {
        "image": path.name,
        "image_bytes": path.stat().st_size,
        "efi_x86_64": "efi/boot/bootx64.efi" in boot_paths,
        "grub_outputs": sorted(set(re.findall(r"^terminal_output(?:\s+)(.*)$", grub, re.MULTILINE))),
        "kernel_consoles": sorted(set(re.findall(r"\bconsole=([^\s;]+)", grub))),
        "system_partitions": reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    tool = shutil.which("7zz") or shutil.which("7z")
    if not tool:
        parser.error("Install 7-Zip to inspect GPT, FAT and SquashFS image files.")
    try:
        print(json.dumps(inspect(args.image.resolve(), tool), indent=2))
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"Image audit failed. {error}")


if __name__ == "__main__":
    main()
