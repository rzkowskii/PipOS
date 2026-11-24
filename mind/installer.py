"""Inspect the laptop and install PipOS from its Try media."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import console_ui as ui
from .install_disks import disk_contents, inventory
from .install_hardware import hardware_report
from .keys import MENU_KEYS
from .try_mode import is_try_boot


MIB = 1024**2
GIB = 1024**3
ALIGNMENT = MIB
MODEL_PATH = Path("/models")
MIND_MINIMUM = GIB
EFI_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX_TYPE = "0fc63daf-8483-4772-8e79-3d69d8477de4"
REASONS = {
    "boot_disk_unknown": "running PipOS disk could not be identified",
    "boot_disk": "running PipOS disk",
    "mounted": "contains a mounted filesystem",
    "active_swap": "contains active swap",
    "held_or_stacked": "in use by another block device",
    "read_only": "read-only device",
    "unsupported_device": "unsupported disk type",
    "too_small": "too small for the proposed layout",
    "inspection_failed": "device inspection incomplete",
}
# Plain-language reasons for the disk cards, most telling first.
FRIENDLY_REASONS = {
    "boot_disk": "This is the USB stick PipOS is running from.",
    "boot_disk_unknown": "PipOS could not tell which disk it started from.",
    "mounted": "It is in use right now.",
    "active_swap": "It is in use as swap memory.",
    "held_or_stacked": "Another part of the system is using it.",
    "read_only": "It is read-only.",
    "inspection_failed": "PipOS could not check it fully, so it will not risk it.",
    "unsupported_device": "This kind of disk is not supported.",
    "too_small": "It is too small for PipOS.",
}
TRANSPORTS = {"sata": "Internal drive", "ata": "Internal drive", "nvme": "Internal drive", "usb": "USB drive",
              "sd": "SD card", "mmc": "SD card"}
# What to do in macOS before its disk is replaced. These steps are impossible
# afterwards (docs/IMPROVEMENT-PLAN.md task 0.7).
MACOS_CHECKLIST = (
    "Back up anything you want to keep from macOS.",
    "Install the last firmware update macOS offers.",
    "Note the battery cycle count and condition (System Information, Power).",
    "Set the battery charge limit while macOS can still do it.",
    "Keep a macOS installer stick if you may want macOS back.",
)
INSTALL_STAGES = (
    ("Preparing the installation files in RAM.", "Preparing the being's files", 18),
    ("Writing installation data.", "Writing PipOS to the disk", 44),
    ("Verifying installation data.", "Checking every byte", 35),
    ("Committing the installation boot records.", "Making the disk startable", 3),
)


def model_allocation(used_bytes):
    # The staged replacement can be as large as all currently occupied model files.
    needed = 2 * used_bytes + 96 * MIB
    return ((needed + ALIGNMENT - 1) // ALIGNMENT) * ALIGNMENT


def fixed_partitions(model_bytes_used):
    return (
        # FAT32 must have enough clusters also on 4 KiB logical sectors.
        ("esp", "vfat", 512 * MIB, EFI_TYPE),
        ("system-a", "squashfs", 2 * GIB, LINUX_TYPE),
        ("system-b", "squashfs", 2 * GIB, LINUX_TYPE),
        ("models", "ext4", model_allocation(model_bytes_used), LINUX_TYPE),
    )


def minimum_target(model_bytes_used):
    if model_bytes_used is None:
        return 4 * GIB
    fixed_bytes = sum(size for _, _, size, _ in fixed_partitions(model_bytes_used))
    return max(4 * GIB, 2 * ALIGNMENT + fixed_bytes + MIND_MINIMUM)


def model_usage():
    if not MODEL_PATH.is_mount():
        return None
    fs = os.statvfs(MODEL_PATH)
    return (fs.f_blocks - fs.f_bfree) * fs.f_frsize


def scan():
    used = model_usage()
    report = inventory(min_size_bytes=minimum_target(used))
    if not report["boot_disk"] or report.get("model_disk") != report["boot_disk"]:
        used = None
    report["layout_ready"] = used is not None
    return report, used


def installation_plan(report, used, device):
    disk = next((disk for disk in report["disks"] if disk["device"] == device), None)
    if disk is None:
        raise ValueError(f"Choose a whole disk from the list. {device} was not found.")
    if not disk["eligible"]:
        reasons = ", ".join(REASONS.get(reason, reason) for reason in disk["reasons"])
        raise ValueError(f"{device} is unavailable as an install target ({reasons}).")
    if used is None:
        raise ValueError("The model partition must be mounted from the running PipOS disk to plan an installation.")

    sector = disk.get("logical_sector_bytes")
    capacity = disk["size_bytes"]
    if sector not in (512, 4096) or capacity % sector:
        raise ValueError(f"Could not verify the sector geometry of {device}.")
    if capacity < minimum_target(used):
        raise ValueError(f"{device} is too small for the proposed layout.")

    fixed = fixed_partitions(used)
    start = ALIGNMENT
    # Leave space for the backup GPT. The end is aligned down to whole MiB.
    end = capacity // ALIGNMENT * ALIGNMENT - ALIGNMENT
    mind_size = end - start - sum(size for _, _, size, _ in fixed)
    parts = []
    for name, filesystem, size, type_id in (*fixed, ("mind", "ext4", mind_size, LINUX_TYPE)):
        parts.append({
            "name": name,
            "filesystem": filesystem,
            "gpt_type": type_id,
            "start_lba": start // sector,
            "sector_count": size // sector,
            "size_bytes": size,
        })
        start += size
    return {"boot_disk": report["boot_disk"], "target": disk, "partitions": parts, "model_bytes_used": used}


def size_label(size):
    if size >= GIB:
        return f"{size / GIB:.2f} GiB"
    return f"{size / MIB:.0f} MiB"


def display_text(value):
    return "".join(char if char.isprintable() else " " for char in value)


def disk_label(disk):
    identity = [disk["device"], disk["model"], size_label(disk["size_bytes"])]
    identity.extend(disk[key] for key in ("transport", "serial", "device_number") if disk.get(key))
    return " | ".join(display_text(value) for value in identity)


def friendly_name(disk):
    kind = TRANSPORTS.get(disk.get("transport") or "", "Disk")
    model = " ".join(display_text(disk.get("model") or "").split())
    return f"{kind}{ui.dot()}{model}" if model else kind


def friendly_reason(disk, layout_ready=True):
    for code in FRIENDLY_REASONS:
        if code in disk["reasons"]:
            return FRIENDLY_REASONS[code]
    if disk["reasons"]:
        return "It cannot be used: " + ", ".join(REASONS.get(code, code) for code in disk["reasons"]) + "."
    if not layout_ready:
        return "PipOS needs its model partition ready before it can plan an install."
    return ""


def layout_summary(plan):
    names = {"esp": "startup", "models": "model", "mind": "memory"}
    parts = {part["name"]: part["size_bytes"] for part in plan["partitions"]}
    pieces = [f"{names['esp']} {ui.size(parts['esp'])}",
              f"system 2 x {ui.size(parts['system-a'])}",
              f"{names['models']} {ui.size(parts['models'])}",
              f"{names['mind']} {ui.size(parts['mind'])}"]
    return ui.dot().join(pieces)


def show_disks(report):
    print(f"Running PipOS disk   {report['boot_disk'] or 'Unknown'}")
    for disk in report["disks"]:
        reason = ", ".join(REASONS.get(reason, reason) for reason in disk["reasons"])
        if disk["eligible"]:
            reason = "Available for preview" if report["layout_ready"] else "Awaiting the boot disk's model partition"
        print(f"  {disk_label(disk)}   {reason}")
    if not report["disks"]:
        print("  No whole disks found.")


def show_plan(plan):
    disk = plan["target"]
    print(f"Read-only layout preview for {disk_label(disk)}")
    print(f"Device capacity   {disk['size_bytes']:,} bytes")
    print("Installing this layout would erase the selected disk.")
    for part in plan["partitions"]:
        print(f"  {part['name']:<10} {part['filesystem']:<10} {size_label(part['size_bytes']):>10}")


def hardware_rows(report):
    """(label, value, found) rows for the hardware screen."""
    system = " ".join(display_text(report[key]) for key in ("manufacturer", "model") if report[key])
    rows = [("System", system or "Unknown", bool(system)),
            ("Firmware", report["firmware"]["mode"] or "Unknown", bool(report["firmware"]["mode"]))]
    if report["ram_mib"] is not None:
        rows.append(("Memory", f"{report['ram_mib'] / 1024:.1f} GB usable", True))
    battery = report["battery"]
    if battery:
        readings = [display_text(battery["status"] or "Detected")]
        if battery["charge_percent"] is not None:
            readings.append(f"{battery['charge_percent']}% charged")
        if battery["full_capacity_percent_of_design"] is not None:
            readings.append(f"{battery['full_capacity_percent_of_design']:g}% of original capacity")
        rows.append(("Battery", ", ".join(readings), True))
    else:
        rows.append(("Battery", "Unknown", False))
    rows.append(("Lid", str(report["lid"]).capitalize() if report["lid"] else "Unknown", bool(report["lid"])))
    for section, key, label in (
        ("audio", "capture_pcm_detected", "Microphone"),
        ("audio", "playback_pcm_detected", "Speakers"),
        ("input", "keyboard_detected", "Keyboard"),
        ("input", "pointer_detected", "Trackpad"),
    ):
        detected = report[section][key]
        rows.append((label, "Found" if detected else "Unknown" if detected is None else "Not found", bool(detected)))
    return rows


def show_hardware(report):
    if ui.enabled():
        ui.screen("Check hardware", subtitle="What PipOS can see on this Mac.")
        ui.card([ui.mark("success" if found else "warning", f"{label:<12}{ui.paint(value, 'text' if found else 'muted')}")
                 for label, value, found in hardware_rows(report)])
        ui.line()
        ui.para("Try a conversation, and open and close the lid, to check them in use.", "muted")
        return
    print("Hardware detected by PipOS")
    system = " ".join(display_text(report[key]) for key in ("manufacturer", "model") if report[key])
    print(f"  System     {system or 'Unknown'}")
    firmware = report["firmware"]
    print(f"  Firmware   {firmware['mode'] or 'Unknown'}")
    if report["ram_mib"] is not None:
        print(f"  RAM        {report['ram_mib']:,} MiB usable")
    battery = report["battery"]
    if battery:
        readings = [display_text(battery["status"] or "Detected")]
        if battery["charge_percent"] is not None:
            readings.append(f"{battery['charge_percent']}% charged")
        if battery["full_capacity_percent_of_design"] is not None:
            readings.append(f"{battery['full_capacity_percent_of_design']:g}% of original capacity")
        print("  Battery    " + ", ".join(readings))
    else:
        print("  Battery    Unknown")
    print(f"  Lid        {report['lid'] or 'Unknown'}")
    for section, key, label in (
        ("audio", "capture_pcm_detected", "Audio input"),
        ("audio", "playback_pcm_detected", "Audio output"),
        ("input", "keyboard_detected", "Keyboard"),
        ("input", "pointer_detected", "Pointer"),
    ):
        detected = report[section][key]
        print(f"  {label:<12} {'Detected' if detected else 'Unknown' if detected is None else 'Unlisted'}")
    print("Try a conversation and the lid and input controls to check them in use.")


def install_summary(plan, *, profile=None, initiative=None, restore=None, skills=None, carried=False):
    """What installing does, in plain words."""
    disk = plan["target"]
    model = " ".join(display_text(disk.get("model") or "").split()) or display_text(disk["device"])
    rows = [ui.mark("accent", f"Makes {model} ({ui.size(disk['size_bytes'])}) the being's home.")]
    rows.append(ui.mark("accent", "Two copies of the system, so an update can always roll back."))
    rows.append(ui.mark("accent", "The language model, so the being thinks on this Mac."))
    if carried:
        rows.append(ui.mark("accent", "The being you met in Try, with what you talked about and its settings."))
    elif restore is not None:
        rows.append(ui.mark("accent", "The being from your backup, checked before anything is written."))
    else:
        rows.append(ui.mark("accent", "A new being with fresh, empty memory."))
        if profile is not None:
            rows.append(ui.mark("accent", f"Keeps the name, voice and personality of {display_text(profile.name)}."))
        if initiative is not None:
            rows.append(ui.mark("accent", "Keeps your quiet hours and announcement settings."))
        if skills and skills.get("time_zone"):
            rows.append(ui.mark("accent", f"Uses the time zone {display_text(skills['time_zone'])}."))
    return rows


def discard_supported(device, sys_root="/sys/class/block"):
    """True when the kernel says the drive can discard (forget) old blocks,
    False when it says it cannot, None when that can't be read. The new mind
    image is sparse: without discard, an old being's data stays in the blocks
    it never writes (the original hard disk, many USB bridges)."""
    name = os.path.basename(os.path.realpath(str(device)))
    try:
        with open(os.path.join(sys_root, name, "queue", "discard_max_bytes")) as limit:
            return int(limit.read().strip() or "0") > 0
    except (OSError, ValueError):
        return None


def contents_warning(contents, discard=True):
    """Card rows for a target that already holds something, or None.

    `discard` is discard_supported() for the drive: only a drive that can
    discard old blocks really erases the old being's memories."""
    if contents.get("being"):
        if discard:
            erase = "Installing erases that being for good."
        else:
            erase = ("Installing replaces that being, but this drive can't erase old blocks, so parts of "
                     "its memories may stay readable on the disk until new data overwrites them.")
        return [ui.paint("This drive holds a PipOS being and its memories.", "text", "bold"),
                erase + " To keep its memory, start it from this drive "
                f"and use the menu ({MENU_KEYS}), Update from a USB stick, instead."], "being"
    if contents.get("macos"):
        rows = [ui.paint("This drive holds macOS.", "text", "bold"), "Before it is erased, in macOS:"]
        rows += [f"{ui.glyph('•', '-')} {item}" for item in MACOS_CHECKLIST]
        return rows, "macos"
    if not contents.get("readable", True):
        # Never treat a drive that could not be read as empty.
        return [ui.paint("I couldn't read what this drive holds.", "text", "bold"),
                "It may hold macOS or a being. Installing erases whatever is on it."], "unknown"
    return None, None


def second_confirmation(kind):
    """A distinct second challenge for a drive that holds macOS or a being."""
    word = {"macos": "MACOS", "being": "BEING", "unknown": "DRIVE"}[kind]
    what = {"macos": "macOS", "being": "the being on it", "unknown": "whatever is on it"}[kind]
    if input(ui.prompt(f"Type ERASE {word} to erase {what}: ")) != f"ERASE {word}":
        raise ValueError("Confirmation did not match. The disk was left untouched.")


def show_confirmation(plan, device, flow, **choices):
    restoring = choices.get("restore") is not None and not choices.get("carried")
    name, number, total = flow or ("Restore" if restoring else "Install", None, None)
    context = ui.step(number, total, name) if number else name
    ui.screen("Ready to restore" if restoring else "Ready to install", context=context,
              subtitle="Here is what happens next. Nothing is written until you confirm.")
    ui.card(install_summary(plan, **choices), title="Restoring" if restoring else "Installing")
    ui.para(layout_summary(plan), "muted", indent="  ")
    ui.line()
    disk = plan["target"]
    model = " ".join(display_text(disk.get("model") or "").split())
    ui.card([ui.paint(f"Everything on {model} ({device}) will be erased." if model else
                      f"Everything on {device} will be erased.", "text", "bold"),
             "This cannot be undone. Back up anything you want to keep first."],
            title=ui.paint("Erase", "danger", "bold"), tone="danger")
    ui.line()
    ui.para("Type the words below exactly, or press Enter to keep the disk safe.", "muted")


def wake_phrase(profile=None):
    if profile is not None:
        return profile.wake_word
    try:
        from .profile import FACTORY_CONFIG, load_profile

        return load_profile(FACTORY_CONFIG, Path("/mind")).wake_word
    except Exception:  # the finish screen must never fail on a profile problem
        return "Hey Pip"


def show_finish(restored, *, carried=False, wake_word="Hey Pip"):
    ui.screen(ui.mark("success", ui.paint("Restore complete." if restored and not carried else "Installation complete.",
                                          "success", "bold")),
              context="Restored" if restored and not carried else "Installed")
    ui.para("Your being has been brought back." if restored and not carried else
            "The being you met in Try has a home now." if carried else "Your being has a home now.", "text")
    ui.line()
    for number, text in enumerate((
        "Press Enter, then press the power button to shut down.",
        "Remove the USB stick.",
        "Turn the Mac on again. Your being will wake and remember you." if restored
        else "Turn the Mac on again. Your being will wake for the first time.",
        "If the Mac shows a folder with a question mark, hold Option at power-on, highlight EFI Boot, "
        "then press Control-Enter to start from it every time.",
    ), 1):
        first, *rest = ui.wrap(text, ui.width() - 4)
        ui.line(f" {ui.paint(str(number), 'accent', 'bold')}  {first}")
        for part in rest:
            ui.line(f"    {part}")
    ui.line()
    # A restored being keeps its own wake phrase, which this Try session cannot read.
    ui.para(f'Say "{display_text(wake_word)}", wait for the eyes to brighten, then talk.' if wake_word else
            "Say its wake phrase, wait for the eyes to brighten, then talk.", "muted")


# Said before any disk is listed or chosen on a cable development stick.
CABLE_MESSAGE = "This is a development stick. It can't install PipOS; use a release Try stick."


def cable_media():
    """True on a cable development stick (mind.install_apply.CABLE_MEDIA), which never installs.

    install_apply keeps its own guard; this one says so before any disk is chosen."""
    from . import install_apply

    return os.path.lexists(install_apply.CABLE_MEDIA)


def confirm_installation(device, *, expected_plan=None, profile=None, initiative=None, restore=None, flow=None,
                         skills=None, carried=False):
    if cable_media():
        raise ValueError(CABLE_MESSAGE)
    if not is_try_boot() or os.geteuid() != 0 or not sys.stdin.isatty():
        raise ValueError("Install from an interactive root console in Try PipOS.")
    if restore is not None and (profile is not None or initiative is not None or skills is not None):
        raise ValueError("Choose trial settings or a backup for the new installation.")
    from .install_apply import apply_installation

    report, used = scan()
    plan = installation_plan(report, used, device)
    if expected_plan is not None and plan != expected_plan:
        raise ValueError("The disk or layout changed. Review it again.")
    if not any(disk["device"] == plan["boot_disk"] and disk["transport"] == "usb" for disk in report["disks"]):
        raise ValueError("The running Try media must be an identifiable USB disk.")
    if ui.enabled():
        show_confirmation(plan, device, flow, profile=profile, initiative=initiative, restore=restore,
                          skills=skills, carried=carried)
    else:
        show_plan(plan)
        if restore is None:
            print("Installing creates a new being with fresh memory. Back up anything you want to keep.", flush=True)
        else:
            print("The backup will be restored and checked before writing to the selected disk.", flush=True)
    warning, kind = contents_warning(disk_contents(device, plan["target"].get("logical_sector_bytes") or 512),
                                     discard=bool(discard_supported(device)))
    if warning:
        if ui.enabled():
            ui.card(warning, title=ui.paint("Look first", "warning", "bold"), tone="warning")
        else:
            print("\n".join(ui.plain(row) for row in warning), flush=True)
    challenge = f"ERASE {device}"
    if input(ui.prompt(f"Type {challenge} to install: ")) != challenge:
        raise ValueError("Confirmation did not match. The disk was left untouched.")
    if kind:
        second_confirmation(kind)

    def recheck():
        latest, model_bytes = scan()
        current = installation_plan(latest, model_bytes, device)
        if current != plan:
            raise ValueError("The disk or layout changed after confirmation. Review it again.")
        return current

    seed = {}
    if profile is not None:
        seed["profile"] = profile
    if initiative is not None:
        seed["initiative"] = initiative
    if restore is not None:
        seed["restore"] = restore
    if skills is not None:
        seed["skills"] = skills
    stages = INSTALL_STAGES
    if restore is not None and not carried:
        stages = (("Preparing the installation files in RAM.", "Unlocking and checking your backup", 18), *stages[1:])
    model = " ".join(display_text(plan["target"].get("model") or "").split()) or device
    restoring = restore is not None and not carried
    view = ui.StageProgress(
        f"{'Restoring your being' if restoring else 'Installing PipOS'} on {model}", stages,
        context="Restore" if restoring else "Install",
        footer="Keep the Mac plugged in and the USB stick in place. This takes a few minutes.")
    erased = []

    def stage(message):
        # From this stage on the old boot records are gone.
        if message == INSTALL_STAGES[1][0]:
            erased.append(True)
        view.stage(message)

    try:
        with view:
            apply_installation(plan, recheck, progress=stage, meter=view.advance, **seed)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if erased:
            raise ValueError(f"The installation stopped after {device} was erased ({error}). "
                             "It no longer starts and holds no being. Run the installation again.") from error
        raise
    if ui.enabled():
        show_finish(restore is not None, carried=carried,
                    wake_word=None if restore is not None and not carried else wake_phrase(profile))
        return
    print("Installation complete.", flush=True)
    print("Power off, remove the Try USB, then start the Mac from the installed drive.", flush=True)


def menu():
    trying = is_try_boot()
    if trying:
        print("Try PipOS. Settings and conversations clear when you reboot.")
    while True:
        print("\nPipOS installer\n  1  Check hardware\n  2  Review disk layout")
        if trying:
            print("  3  Install PipOS")
        print("  Q  Exit")
        choice = input("Choice: ").strip().lower()
        if choice in ("q", ""):
            return
        if choice == "1":
            show_hardware(hardware_report())
        elif choice == "2":
            report, used = scan()
            show_disks(report)
            if not report["layout_ready"] or not any(disk["eligible"] for disk in report["disks"]):
                continue
            device = input("Disk path to preview [Enter to go back]: ").strip()
            if device:
                try:
                    latest, used = scan()
                    listed = next((disk for disk in report["disks"] if disk["device"] == device), None)
                    plan = installation_plan(latest, used, device)
                    if plan["target"] != listed or plan["boot_disk"] != report["boot_disk"]:
                        raise ValueError("The disk list changed. Review the new disk list before selecting.")
                    show_plan(plan)
                except ValueError as exc:
                    print(exc, file=sys.stderr)
        elif choice == "3" and trying:
            if cable_media():
                print(CABLE_MESSAGE)
                continue
            report, _ = scan()
            show_disks(report)
            if not report["layout_ready"] or not any(disk["eligible"] for disk in report["disks"]):
                continue
            device = input("Disk path to install [Enter to go back]: ").strip()
            if device:
                try:
                    confirm_installation(device)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    print(f"being-installer: {exc}", file=sys.stderr)
                else:
                    return
        else:
            print("Choose an action in the menu.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-installer", description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list whole disks and target eligibility")
    mode.add_argument("--check", action="store_true", help="summarize hardware detectable by Linux")
    mode.add_argument("--plan", metavar="DEVICE", help="preview a layout for a whole disk")
    mode.add_argument("--install", metavar="DEVICE", help="install from Try media with confirmation")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)
    if args.install and args.json:
        parser.error("--install requires interactive disk confirmation")

    try:
        if args.install:
            confirm_installation(args.install)
            return 0
        if args.check:
            result, render = hardware_report(), show_hardware
        elif args.plan:
            report, used = scan()
            result, render = installation_plan(report, used, args.plan), show_plan
        elif args.list or args.json or not sys.stdin.isatty():
            result, _ = scan()
            render = show_disks
        else:
            menu()
            return 0
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            render(result)
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nInstaller closed.")
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"being-installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
