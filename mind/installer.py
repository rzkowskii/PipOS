"""Inspect the laptop and install PipOS from its Try media."""

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from . import console_ui as ui
from . import install_status
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
SPOKEN_STAGES = {
    INSTALL_STAGES[0][0]: "prepare",
    INSTALL_STAGES[1][0]: "write",
    INSTALL_STAGES[2][0]: "verify",
    INSTALL_STAGES[3][0]: "commit",
}
POWER_CUT_HELP = "If power cuts out, boot this USB again and start from disk selection."
PARTITION_MARKS = {
    "esp": ("Startup", "█", "warning"),
    "system-a": ("System A", "█", "accent"),
    "system-b": ("System B", "▓", "accent"),
    "models": ("Models", "█", "94"),
    "mind": ("Memory", "░", "muted"),
}
INSTALL_STEPS = ("Welcome", "Choose disk", "Make it yours", "Confirm", "Installing", "Wake")


def wizard_steps(current):
    """The active screen is named alongside its position in the install flow."""
    done = ui.paint("● " * current, "accent_soft", "dim")
    active = ui.paint("●", "accent", "bold") + " " + ui.paint(INSTALL_STEPS[current], "text", "bold")
    remaining = ui.paint(" ○" * (len(INSTALL_STEPS) - current - 1), "muted", "dim")
    return done + active + remaining


def card_width():
    return (min(ui.columns() - 2 * len(ui.MARGIN), 106) - 4) // 2


def card_rows(rows, width, tone):
    """A filled card that can sit beside another card in the native console."""
    inner = width - 4
    def edge(text):
        return ui.paint(text, tone, "dim", "40")

    top = edge("╭" + "─" * (width - 2) + "╮")
    body = []
    for row, *tones in rows:
        parts = textwrap.wrap(row, inner, break_on_hyphens=False) or [""]
        body.extend(edge("│ ") + ui.paint(ui.pad(part, inner), *tones, "40") + edge(" │")
                    for part in parts)
    return [top, *body, edge("╰" + "─" * (width - 2) + "╯")]


def paired_cards(left, right):
    """Keep both borders aligned when a local label wraps onto another row."""
    height = max(len(left), len(right))

    def padded(card):
        blank = ui.paint("│" + " " * (ui.visible_len(card[0]) - 2) + "│", "muted", "dim", "40")
        return [*card[:-1], *([blank] * (height - len(card))), card[-1]]

    for a, b in zip(padded(left), padded(right)):
        ui.line(a + "    " + b)


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


def partition_rows(plan, width):
    """The planned partition sizes, rounded to console cells in disk order."""
    parts = plan["partitions"]
    total = sum(part["size_bytes"] for part in parts)
    previous = cumulative = 0
    bar, legend = [], []
    for partition in parts:
        label, mark, tone = PARTITION_MARKS[partition["name"]]
        size = partition["size_bytes"]
        cumulative += size
        boundary = (cumulative * width + total // 2) // total
        bar.append(ui.paint(mark * (boundary - previous), tone))
        legend.append(f"{ui.paint(mark, tone)} {label} {size_label(size)}")
        previous = boundary
    # Tiny partitions can round to zero cells on large disks. Their sizes
    # remain visible in the legend, without exaggerating their bar lengths.
    return ["".join(bar), *ui.wrap(ui.dot().join(legend[:3]), width),
            *ui.wrap(ui.dot().join(legend[3:]), width)]


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


def hardware_flags(rows):
    """Observation bits for the console's laptop diagram."""
    detected = {name: found for name, _, found in rows}
    fields = ("Lid", "Memory", "Battery", "Microphone", "Speakers", "Keyboard", "Trackpad")
    return sum(1 << bit for bit, field in enumerate(fields) if detected.get(field))


def show_hardware(report):
    if ui.enabled():
        diagram = ui.columns() >= 80 and ui.rows() >= 22 and not ui.pending_notes()
        ui.screen("Check hardware", subtitle="What PipOS can see on this Mac.")
        rows = hardware_rows(report)
        if diagram:
            ui.write(f"\x1b]7772;hardware={hardware_flags(rows)}\a")
            facts = []
            for label, value, found in rows:
                tone = "success" if found else "warning"
                first, *rest = textwrap.wrap(value, 33, break_on_hyphens=False) or [""]
                facts.append(ui.mark(tone, f"{label:<12}{ui.paint(first, 'text' if found else 'muted')}"))
                facts.extend(" " * 14 + ui.paint(part, "muted") for part in rest)
            for fact in facts:
                ui.line(fact)
        else:
            ui.card([ui.mark("success" if found else "warning", f"{label:<12}"
                             f"{ui.paint(value, 'text' if found else 'muted')}") for label, value, found in rows])
        ui.line()
        hint = "Try a conversation, and open and close the lid, to check them in use."
        for line in ui.wrap(hint, 48 if diagram else ui.width()):
            ui.line(ui.paint(line, "muted"))
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


def install_summary(*, profile=None, initiative=None, restore=None, skills=None, carried=False, appearance=None):
    """What installing does, in plain words."""
    if carried:
        return ["Keeps the being you met in Try, its conversations and settings."]
    if restore is not None:
        return ["Checks the being's backup before any disk write."]
    keeps = []
    if profile is not None:
        keeps.append(f"{display_text(profile.name)} (name, voice, personality)")
    if initiative is not None:
        keeps.append("quiet hours and announcements")
    if skills and skills.get("time_zone"):
        keeps.append(f"time zone {display_text(skills['time_zone'])}")
    if appearance is not None:
        keeps.append("Try appearance")
    rows = ["A new being starts with fresh memory."]
    if keeps:
        rows.append("Keeps " + ui.dot().join(keeps))
    return rows


def discard_supported(device, sys_root="/sys/class/block"):
    """Read the kernel's advertised discard support."""
    name = os.path.basename(os.path.realpath(str(device)))
    try:
        with open(os.path.join(sys_root, name, "queue", "discard_max_bytes")) as limit:
            return int(limit.read().strip() or "0") > 0
    except (OSError, ValueError):
        return None


def contents_warning(contents, discard=True):
    """Card rows for a target that already holds something, or None."""
    if not contents.get("readable", True):
        return [ui.paint("I couldn't read what this drive holds.", "text", "bold"),
                "It may hold macOS or a being. Installing erases whatever is on it."], "unknown"
    if contents.get("macos"):
        rows = [ui.paint("This drive holds macOS. Check these in macOS first.", "text", "bold")]
        if contents.get("being"):
            rows.append("It also holds a PipOS being and its memories.")
        rows += [f"{ui.glyph('•', '-')} {item}" for item in MACOS_CHECKLIST]
        return rows, "macos"
    if contents.get("being"):
        if discard is False:
            erase = ("Installing replaces that being, but this drive can't erase old blocks, so parts of "
                     "its memories may stay readable on the disk until new data overwrites them.")
        else:
            erase = "Installing replaces that being. Old memory data may still be recoverable after discard."
        return [ui.paint("This drive holds a PipOS being and its memories.", "text", "bold"),
                erase + " To keep its memory, start it from this drive "
                f"and use the menu ({MENU_KEYS}), Update from a USB stick, instead."], "being"
    return None, None


def second_confirmation(kind):
    """A distinct second challenge for a drive that holds macOS or a being."""
    word = {"macos": "MACOS", "being": "BEING", "unknown": "DRIVE"}[kind]
    what = {"macos": "macOS", "being": "the being on it", "unknown": "whatever is on it"}[kind]
    if input(ui.prompt(f"Type ERASE {word} to erase {what}: ")) != f"ERASE {word}":
        raise ValueError("Confirmation did not match. The disk was left untouched.")


def show_confirmation(plan, device, flow, *, warning=None, **choices):
    restoring = choices.get("restore") is not None and not choices.get("carried")
    name, number, total = flow or ("Restore" if restoring else "Install", None, None)
    context = ui.step(number, total, name) if number and (restoring or ui.columns() < 80) else name
    disk = plan["target"]
    ui.screen("Ready to restore" if restoring else "Ready to install", context=context)
    title = f"Disk layout{ui.dot()}{ui.size(disk['size_bytes'])}"
    summary = install_summary(**choices)
    model = " ".join(display_text(disk.get("model") or "").split())
    target = f"{model} ({device})" if model else device
    erase = f"Installing replaces all data on {target}."
    cancel = "Press Enter to leave the disk untouched."
    if ui.columns() >= 80:
        combined = 2 * card_width()
        review_width = combined // 3
        warning_width = combined - review_width
        partition_facts = []
        for part in plan["partitions"]:
            label, mark, tone = PARTITION_MARKS[part["name"]]
            partition_facts.append((f"{mark} {label}  {size_label(part['size_bytes'])}", tone))
        left = card_rows([(title, "accent", "bold"), ("",), *partition_facts, ("",),
                          *((row, "text") for row in summary)], review_width, "accent_soft")
        right_rows = [("This cannot be undone", "danger", "bold"), (erase, "danger", "bold")]
        if warning:
            right_rows.extend([("",), *((ui.plain(row), "warning") for row in warning)])
        else:
            right_rows.append(("Back up what you need.", "text"))
        right_rows.extend([("",), (cancel, "muted")])
        right = card_rows(right_rows, warning_width, "danger")
        if max(len(left), len(right)) + 5 <= ui.room():
            bar, *_ = partition_rows(plan, combined + 4)
            ui.line(bar)
            paired_cards(left, right)
            ui.para(POWER_CUT_HELP, "muted")
            if not restoring:
                ui.line(wizard_steps(3))
            return
    compact = ui.rows() < 26 or warning is not None
    bar, *legend = partition_rows(plan, ui.width() - (0 if compact else 4))
    if compact:
        ui.line(ui.paint(title, "accent", "bold"))
        for part in (bar, *legend, *summary):
            ui.para(part, "text")
        ui.para(erase, "danger", "bold")
        for row in warning or ("Back up what you need.",):
            ui.para(row, "warning")
        ui.para(cancel, "muted")
    else:
        ui.card([bar, *legend, "", *summary], title=title, tone="accent_soft")
        ui.card([ui.paint(erase, "danger", "bold"), f"Back up what you need. {cancel}"],
                title=ui.paint("This cannot be undone", "danger", "bold"), tone="danger")
    ui.para(POWER_CUT_HELP, "muted")
    if not restoring and ui.room() >= 4:
        ui.line(wizard_steps(3))


def wake_phrase(profile=None):
    if profile is not None:
        return profile.wake_word
    try:
        from .profile import FACTORY_CONFIG, load_profile

        return load_profile(FACTORY_CONFIG, Path("/mind")).wake_word
    except Exception:  # the finish screen must never fail on a profile problem
        return "Hey Pip"


def install_content_width():
    # being-console reserves the space to the right for the native illustration.
    return 48 if ui.columns() >= 80 and ui.rows() >= 16 else ui.width()


def install_meter(fraction, width):
    filled = round(min(1.0, max(0.0, fraction)) * width)
    return ui.paint("━" * filled, "accent") + ui.paint("━" * (width - filled), "muted", "dim")


def show_finish(restored, *, carried=False, wake_word="Hey Pip"):
    ui.screen(ui.mark("success", ui.paint("Restore complete." if restored and not carried else "Installation complete.",
                                          "success", "bold")),
              context="Restored" if restored and not carried else "Installed")
    width = install_content_width()
    if ui.enabled():
        ui.write("\x1b]7772;install=100\a")
    home = ("Your being has been brought back." if restored and not carried else
            "The being you met in Try has a home now." if carried else "Your being has a home now.")
    for part in ui.wrap(home, width):
        ui.line(ui.paint(part, "text"))
    ui.line()
    for number, text in enumerate((
        "Press Enter, then press the power button to shut down.",
        "Remove the USB stick.",
        "Turn the Mac on again. Your being will wake and remember you." if restored
        else "Turn the Mac on again. Your being will wake for the first time.",
        "If the Mac shows a folder with a question mark, hold Option at power-on, highlight EFI Boot, "
        "then press Control-Enter to start from it every time.",
    ), 1):
        first, *rest = ui.wrap(text, width - 4)
        ui.line(f" {ui.paint(str(number), 'accent', 'bold')}  {first}")
        for part in rest:
            ui.line(f"    {part}")
    ui.line()
    # A restored being keeps its own wake phrase, which this Try session cannot read.
    wake = (f'Say "{display_text(wake_word)}", wait for the eyes to brighten, then talk.' if wake_word else
            "Say its wake phrase, wait for the eyes to brighten, then talk.")
    for part in ui.wrap(wake, width):
        ui.line(ui.paint(part, "muted"))
    if (not restored or carried) and ui.room() >= 2:
        ui.line()
        ui.line(wizard_steps(5))


# Said before any disk is listed or chosen on a cable development stick.
CABLE_MESSAGE = "This is a development stick. It can't install PipOS; use a release Try stick."


def cable_media():
    """True on a cable development stick (mind.install_apply.CABLE_MEDIA), which never installs.

    install_apply keeps its own guard; this one says so before any disk is chosen."""
    from . import install_apply

    return os.path.lexists(install_apply.CABLE_MEDIA)


class InstallProgress(ui.StageProgress):
    """Keep stage estimates apart from bytes actually copied or checked."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.measured = None

    def stage(self, message):
        if message in {stage for stage, _, _ in self.stages}:
            self.measured = None
        super().stage(message)

    def advance(self, done, total):
        if total > 0:
            self.measured = self.current, max(0, min(done, total)), total
        super().advance(done, total)

    def progress_lines(self):
        width = install_content_width()
        fraction = self.fraction()
        rows = [ui.paint(f"{int(fraction * 100)}%", "bright", "bold") + ui.paint("  Overall estimate", "muted"),
                install_meter(fraction, width)]
        measured = self.measured
        if measured is not None and self.current == measured[0] and self.current in {1, 2}:
            _, done, total = measured
            verb = "Copied" if self.current == 1 else "Checked"
            rows.append(ui.paint(f"{verb} {done:,} / {total:,} bytes", "text"))
        else:
            rows.append("")
        return rows

    def lines(self):
        width = install_content_width()
        rows = [ui.header(self.title, self.context), ui.rule()]
        if self.subtitle:
            rows.extend(ui.paint(part, "muted") for part in ui.wrap(self.subtitle, width))
        rows.extend(["", *self.progress_lines(), ""])
        label_width = max(len(label) for _, label, _ in self.stages)
        side_by_side = width >= label_width + 12
        bar_width = width - label_width - 4 if side_by_side else width - 2
        for index, (_, label, _) in enumerate(self.stages):
            complete = index < self.current
            active = index == self.current
            fraction = 1 if complete else self.within if active else 0
            tone = "success" if complete else "accent" if active else "muted"
            marker = "✓" if complete else "●" if active else "○"
            label_text = ui.paint(ui.pad(label, label_width), "text" if active else "muted")
            stage = f"{ui.paint(marker, tone)} {label_text}"
            bar = install_meter(fraction, bar_width)
            if side_by_side:
                rows.append(f"{stage}  {bar}")
            else:
                rows.extend([stage.rstrip(), f"  {bar}"])
        elapsed = int(time.monotonic() - self.started)
        rows.append(ui.paint(f"{elapsed // 60}:{elapsed % 60:02d} elapsed", "muted"))
        rows.append("")
        if self.extra:
            rows.extend(ui.paint(part, "text") for part in ui.wrap(self.extra[-1], width))
        if self.footer:
            rows.extend(ui.paint(part, "muted") for part in ui.wrap(self.footer, width))
        if self.context == "Install":
            rows.extend(["", wizard_steps(4)])
        return rows

    def header_control(self):
        return f"\x1b]7772;install={int(self.fraction() * 100)}\a"

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            if ui.enabled():
                ui.write("\x1b]7772;install=off\a")


def confirm_installation(device, *, expected_plan=None, profile=None, initiative=None, restore=None, flow=None,
                         skills=None, carried=False, appearance=None):
    if cable_media():
        raise ValueError(CABLE_MESSAGE)
    if not is_try_boot() or os.geteuid() != 0 or not sys.stdin.isatty():
        raise ValueError("Install from an interactive root console in Try PipOS.")
    if restore is not None and any(value is not None for value in (profile, initiative, skills, appearance)):
        raise ValueError("Choose trial settings or a backup for the new installation.")
    from .mind_encryption import CRYPTSETUP, new_pin, wipe

    report, used = scan()
    plan = installation_plan(report, used, device)
    if expected_plan is not None and plan != expected_plan:
        raise ValueError("The disk or layout changed. Review it again.")
    if not any(disk["device"] == plan["boot_disk"] and disk["transport"] == "usb" for disk in report["disks"]):
        raise ValueError("The running Try media must be an identifiable USB disk.")
    contents = disk_contents(device, plan["target"]["logical_sector_bytes"])
    warning, kind = contents_warning(contents, discard=discard_supported(device))
    support_protection = os.access(CRYPTSETUP, os.X_OK)
    challenge = f"ERASE {device}"

    def review(protected=False):
        if ui.enabled():
            show_confirmation(plan, device, flow, warning=warning, profile=profile, initiative=initiative,
                              restore=restore, skills=skills, carried=carried, appearance=appearance)
        else:
            show_plan(plan)
            if restore is None:
                print("Installing creates a new being with fresh memory. Back up anything you want to keep.", flush=True)
            else:
                print("The backup will be restored and checked before writing to the selected disk.", flush=True)
            print(POWER_CUT_HELP, flush=True)
            if warning:
                print("\n".join(ui.plain(row) for row in warning), flush=True)
        if protected:
            ui.para("PIN protection selected. The installed memory requires the PIN after each full boot.", "success")
        elif support_protection:
            ui.para("Unattended startup uses unencrypted memory. Type E to choose PIN protection.", "muted")
        return input(ui.prompt(f"Type {challenge} to install: "))

    pin = None
    try:
        answer = review()
        if support_protection and answer.strip().lower() == "e":
            ui.screen("Protect your memory", context="Restore" if restore is not None else "Install")
            ui.para("Choose a 12–32 digit PIN. Pip will ask for it on this screen after every full boot.")
            ui.para("Keep a separate encrypted backup. Pip cannot recover a lost PIN.", "warning")
            pin = new_pin()
            answer = review(protected=True)
        if answer != challenge:
            raise ValueError("Confirmation did not match. The disk was left untouched.")
        if kind:
            second_confirmation(kind)
        return _write_installation(plan, device, contents, profile=profile, initiative=initiative, restore=restore,
                                   skills=skills, appearance=appearance, carried=carried, mind_pin=pin)
    finally:
        wipe(pin)


def _write_installation(plan, device, contents, *, profile, initiative, restore, skills, appearance, carried, mind_pin):
    from .install_apply import apply_installation

    def recheck():
        latest, model_bytes = scan()
        current = installation_plan(latest, model_bytes, device)
        if current != plan:
            raise ValueError("The disk or layout changed after confirmation. Review it again.")
        return current

    seed = {"expected_contents": contents}
    if profile is not None:
        seed["profile"] = profile
    if initiative is not None:
        seed["initiative"] = initiative
    if restore is not None:
        seed["restore"] = restore
    if skills is not None:
        seed["skills"] = skills
    if appearance is not None:
        seed["appearance"] = appearance
    # Preserve the legacy plain-install call shape; only the explicit
    # encrypted choice hands this in-memory value to install_apply.
    if mind_pin is not None:
        seed["mind_pin"] = mind_pin
    stages = INSTALL_STAGES
    if restore is not None and not carried:
        stages = (("Preparing the installation files in RAM.", "Unlocking and checking your backup", 18), *stages[1:])
    model = " ".join(display_text(plan["target"].get("model") or "").split()) or device
    restoring = restore is not None and not carried
    view = InstallProgress(
        "Restoring your being" if restoring else "Installing PipOS", stages,
        context="Restore" if restoring else "Install",
        subtitle=f"Target {model} ({device})" if model != device else f"Target {device}",
        footer="Keep the Mac plugged in and the USB stick in place. This takes a few minutes.")
    writing = False

    def stage(message):
        nonlocal writing
        # This stage begins with invalidating the old boot records.
        if message == INSTALL_STAGES[1][0]:
            writing = True
        view.stage(message)
        if message in SPOKEN_STAGES:
            install_status.report(SPOKEN_STAGES[message])

    try:
        with view:
            apply_installation(plan, recheck, progress=stage, meter=view.advance, **seed)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        install_status.report("stopped")
        if writing:
            detail = str(error) or "interrupted"
            raise ValueError(f"The installation stopped during disk writing to {device} ({detail}). "
                             "The selected disk may no longer start. "
                             "Start again from this USB and check the target disk before confirming.") from error
        raise
    install_status.report("complete")
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
