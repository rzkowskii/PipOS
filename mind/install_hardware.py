"""Read-only Linux hardware observations for an installer preview."""

import re
from pathlib import Path


# These Linux key codes fit in the lowest bitmap word on 32- and 64-bit systems.
KEYBOARD_KEYS = (1 << 16) | (1 << 28) | (1 << 30)  # Q, Enter, A


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


def read_int(path):
    try:
        return int(read_text(path) or "")
    except ValueError:
        return None


def lowest_bitmap_word(path):
    try:
        return int((read_text(path) or "0").split()[-1], 16)
    except ValueError:
        return 0


def battery_report(supplies):
    for device in supplies:
        if read_text(device / "type") != "Battery" or read_text(device / "present") == "0":
            continue
        capacity = None
        for unit in ("energy", "charge"):
            full = read_int(device / f"{unit}_full")
            design = read_int(device / f"{unit}_full_design")
            if full and design and full > 0 and design > 0:
                capacity = round(100 * full / design, 1)
                break
        charge = read_int(device / "capacity")
        return {
            "device": device.name,
            "status": read_text(device / "status"),
            "charge_percent": charge if charge is not None and 0 <= charge <= 100 else None,
            "health": read_text(device / "health"),
            "full_capacity_percent_of_design": capacity,
        }
    return None


def inputs_report(proc_root, sys_root):
    keyboard = pointer = lid = False
    for device in (sys_root / "class/input").glob("input[0-9]*"):
        capabilities = device / "capabilities"
        keys = lowest_bitmap_word(capabilities / "key")
        relative_axes = lowest_bitmap_word(capabilities / "rel")
        properties = lowest_bitmap_word(device / "properties")
        keyboard |= keys & KEYBOARD_KEYS == KEYBOARD_KEYS
        pointer |= relative_axes & 0b11 == 0b11 or bool(properties & 1)  # REL_X/Y or INPUT_PROP_POINTER
        lid |= bool(lowest_bitmap_word(capabilities / "sw") & 1)  # SW_LID

    lid_states = set()
    for path in (proc_root / "acpi/button/lid").glob("*/state"):
        if match := re.fullmatch(r"state:\s*(open|closed)", read_text(path) or "", re.IGNORECASE):
            lid_states.add(match[1].lower())
    lid_status = lid_states.pop() if len(lid_states) == 1 else ("detected" if lid or lid_states else None)
    return {"keyboard_detected": keyboard or None, "pointer_detected": pointer or None}, lid_status


def hardware_report(*, proc_root=Path("/proc"), sys_root=Path("/sys")):
    """Report kernel observations. Null means there is no usable reading.

    Its RAM total is usable memory from Linux. Input and PCM capabilities only
    describe enumerated interfaces. The physical controls and audio need a
    separate functional test.
    """
    meminfo = read_text(proc_root / "meminfo") or ""
    memory = re.search(r"^MemTotal:\s+([0-9]+)\s+kB$", meminfo, re.MULTILINE)
    dmi = sys_root / "class/dmi/id"
    efi = sys_root / "firmware/efi"
    inputs, lid = inputs_report(proc_root, sys_root)
    pcm = read_text(proc_root / "asound/pcm")
    audio = {
        f"{direction}_pcm_detected": bool(re.search(rf":\s*{direction}\s+[1-9][0-9]*\b", pcm))
        if pcm is not None else None
        for direction in ("capture", "playback")
    }
    supplies = [device for device in sorted((sys_root / "class/power_supply").glob("*"))
                if read_text(device / "scope") != "Device"]
    online = {read_text(device / "online") for device in supplies if read_text(device / "type") == "Mains"}
    return {
        "firmware": {
            "mode": "UEFI" if efi.is_dir() else None,
            "version": read_text(dmi / "bios_version"),
        },
        "manufacturer": read_text(dmi / "sys_vendor"),
        "model": read_text(dmi / "product_name"),
        "ram_mib": int(memory[1]) // 1024 if memory else None,
        "battery": battery_report(supplies),
        "mains_online": True if "1" in online else False if online == {"0"} else None,
        "lid": lid,
        "audio": audio,
        "input": inputs,
    }
