"""Keyboard-operated setup and installation on the PipOS screen."""

import getpass
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import console_ui as ui
from . import install_disks, installer, setup, usb_logs
from .initiative import POLICY_PATH as INITIATIVE_POLICY_PATH, load_policy as load_initiative_policy
from .initiative_setup import describe_policy
from .profile import FACTORY_CONFIG, PROFILE_FILE, load_profile
from .try_mode import is_try_boot


MIND_DIR = Path("/mind")
MOUNTINFO = Path("/proc/self/mountinfo")
MEDIA_DIR = Path("/run/being-restore-media")
# Read-only, and nothing on the media may run or act as a device. An ext4
# journal is not replayed, so a read-only mount writes nothing at all.
MEDIA_MOUNTS = (
    ("vfat", "ro,nodev,nosuid,noexec,utf8,umask=077"),
    ("exfat", "ro,nodev,nosuid,noexec,umask=077"),
    ("ext4", "ro,nodev,nosuid,noexec,noload"),
)
MEDIA_FILE_LIMIT = 60
MEDIA_DEPTH = 4
MEDIA_MIN_BYTES = 1024**2
BACKUP_SUFFIXES = (".age", ".agekey", ".key", ".txt")
MENU = (
    ("1", "Try PipOS", "Meet the being. Nothing is kept."),
    ("2", "Install PipOS", "Make this Mac its home."),
    ("3", "Restore a being", "Bring a backed-up being home."),
    ("4", "Setup", "Name, voice and personality."),
    ("5", "Check hardware", "See what PipOS can find."),
    ("Q", "Return to PipOS", None),
)
RESTORE_CHOICES = (
    ("1", "Restore with conversation archive", "Everything, including past conversations."),
    ("2", "Restore identity and settings", "Who the being is and how it is set up. Conversations start fresh."),
    ("Q", "Back", None),
)


def report_error(error):
    if ui.enabled():
        ui.note(str(error), "danger")
    else:
        print(f"PipOS: {error}", file=sys.stderr)


def warn(text):
    """A short correction. With the console UI it shows on the redrawn screen."""
    ui.note(text, "warning")


def try_settings_present():
    try:
        return temporary_mind_mounted() and any(
            os.path.lexists(MIND_DIR / name) for name in (PROFILE_FILE, INITIATIVE_POLICY_PATH))
    except OSError:
        return False


def temporary_mind_mounted():
    matches = []
    for line in MOUNTINFO.read_text().splitlines():
        before, _, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if len(fields) >= 5 and fields[4] == "/mind":
            matches.append(len(filesystem) >= 3 and filesystem[0] == "tmpfs"
                           and "noswap" in filesystem[2].split(","))
    return matches == [True]


def profile_to_keep():
    if not temporary_mind_mounted():
        return None
    try:
        profile_file = MIND_DIR / PROFILE_FILE
        if not stat.S_ISREG(profile_file.lstat().st_mode):
            raise ValueError("Expected a regular profile file.")
        profile = load_profile(FACTORY_CONFIG, MIND_DIR)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        ui.note("The Try profile could not be read. The installation will use factory settings.", "warning")
        return None

    if ui.enabled():
        ui.card(setup.profile_rows(profile), title="The being you tried")
        ui.para("The installed being starts with fresh memory.", "muted")
    else:
        print(f"\n{setup.description(profile)}")
        print("The installed being starts with fresh memory.")
    if input(ui.prompt("Keep this Try profile? [y/N] ")).strip().lower() in {"y", "yes"}:
        return profile
    return None


def initiative_to_keep():
    if not temporary_mind_mounted():
        return None
    try:
        policy_file = MIND_DIR / INITIATIVE_POLICY_PATH
        if not stat.S_ISREG(policy_file.lstat().st_mode):
            raise ValueError("Expected a regular initiative policy file.")
        policy = load_initiative_policy(MIND_DIR, strict=True)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        ui.note("The saved announcement settings could not be read. The installation will use factory defaults.",
                "warning")
        return None

    if ui.enabled():
        ui.line()
        ui.card(describe_policy(policy).splitlines(), title="Quiet hours and announcements")
    else:
        print(f"\n{describe_policy(policy)}")
    if input(ui.prompt("Keep these Try announcement settings? [y/N] ")).strip().lower() in {"y", "yes"}:
        return policy
    return None


def disk_card(number, disk):
    inner = ui.width() - 4
    left = f"{ui.paint(number, 'accent', 'bold')}  {ui.paint(installer.friendly_name(disk), 'text', 'bold')}"
    right = ui.paint(ui.size(disk["size_bytes"]), "text")
    details = [installer.display_text(disk["device"])]
    if disk.get("serial"):
        details.append(f"serial {installer.display_text(disk['serial'])}")
    ui.card([ui.pad(left, inner - ui.visible_len(right)) + right,
             "   " + ui.paint(ui.dot().join(details), "muted")], tone="accent_soft")


def draw_disks(report, available, flow):
    name, number, total = flow
    ui.screen("Choose a disk", context=ui.step(number, total, name),
              subtitle="Pick where your being will live. Everything on that disk will be replaced." if available
              else "Your being needs a disk of its own, separate from this USB stick.")
    for key, device in available.items():
        disk = next(disk for disk in report["disks"] if disk["device"] == device)
        disk_card(key, disk)
    if not available:
        ui.card([ui.paint("No disk here can hold PipOS right now.", "text", "bold"),
                 "Connect or free up a disk, then try again."], tone="warning")
    others = [disk for disk in report["disks"] if disk.get("transport") and disk["device"] not in available.values()]
    if others:
        ui.line()
        ui.line(ui.paint("Not available", "muted"))
        for disk in others:
            size = ui.size(disk["size_bytes"])
            name_text = installer.friendly_name(disk)
            gap = ui.width() - 6 - len(name_text) - len(size)
            ui.line(f" {ui.paint('○', 'muted')}  {ui.paint(name_text, 'plain')}{' ' * max(2, gap)}{ui.paint(size, 'muted')}")
            reason = installer.friendly_reason(disk, report["layout_ready"])
            for part in ui.wrap(f"{installer.display_text(disk['device'])}{ui.dot()}{reason}", ui.width() - 5):
                ui.line("    " + ui.paint(part, "muted"))


def choose_disk(report, flow=("Install", 1, 2)):
    if not ui.enabled():
        name, number, total = flow
        print(f"\n{ui.step(number, total, name)} - Choose a disk")
        print(f"Running PipOS disk   {report['boot_disk'] or 'Unknown'}")
    available = {}
    for disk in report["disks"]:
        if not disk.get("transport"):
            continue
        label = installer.disk_label(disk)
        if disk["eligible"] and report["layout_ready"]:
            number = str(len(available) + 1)
            available[number] = disk["device"]
            if not ui.enabled():
                print(f"  {number}  {label}")
        elif not ui.enabled():
            reason = ", ".join(installer.REASONS.get(code, code) for code in disk["reasons"])
            print(f"     {label} ({reason or 'model partition unavailable'})")
    if not available:
        if ui.enabled():
            draw_disks(report, available, flow)
            input(ui.prompt("Press Enter to go back. "))
        else:
            print("No disks are available for installation.")
        return None
    while True:
        if ui.enabled():
            draw_disks(report, available, flow)
        choice = input(ui.prompt("Disk number [Enter to go back]: ")).strip().lower()
        if choice in {"", "q"}:
            return None
        if choice in available:
            return available[choice]
        warn("Choose a numbered disk, or press Enter to go back.")


def install(*, restore=None):
    if restore is None:
        settings = try_settings_present()
        name, total = "Install", 3 if settings else 2
        first = 1
    else:
        settings, name, total, first = False, "Restore", 4, 3
    report, used = installer.scan()
    device = choose_disk(report, (name, first, total))
    if device is None:
        return False
    plan = installer.installation_plan(report, used, device)
    choices = {"profile": None, "restore": restore}
    if restore is None:
        if settings:
            ui.screen("Your Try settings", context=ui.step(2, total, name),
                      subtitle="Choose what the installed being keeps from this Try session.")
        choices["profile"] = profile_to_keep()
        initiative = initiative_to_keep()
        if initiative is not None:
            choices["initiative"] = initiative
    installer.confirm_installation(device, expected_plan=plan, flow=(name, total, total), **choices)
    input(ui.prompt("Press Enter to return to PipOS. "))
    return True


def backup_media(report):
    """Partitions on USB disks that are neither the running disk nor in use."""
    in_use = {"boot_disk", "mounted", "active_swap", "held_or_stacked", "inspection_failed", "boot_disk_unknown"}
    found = []
    for disk in report["disks"]:
        if disk.get("transport") != "usb" or in_use.intersection(disk["reasons"]):
            continue
        for volume in install_disks.volumes(disk["device"]):
            if volume["size_bytes"] < MEDIA_MIN_BYTES:
                continue  # an extended-partition container, not a volume
            label = (f"{volume['device']} | {installer.display_text(disk['model'])} | "
                     f"{ui.size(volume['size_bytes']) if ui.enabled() else installer.size_label(volume['size_bytes'])}")
            found.append((volume["device"], label))
    return found


def unmount_media():
    if os.path.ismount(MEDIA_DIR):
        subprocess.run(["umount", str(MEDIA_DIR)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False)
    try:
        MEDIA_DIR.rmdir()
    except OSError:
        pass


def mount_media(device):
    unmount_media()
    MEDIA_DIR.mkdir(mode=0o700, exist_ok=True)
    for filesystem, options in MEDIA_MOUNTS:
        try:
            result = subprocess.run(["mount", "-t", filesystem, "-o", options, device, str(MEDIA_DIR)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return MEDIA_DIR
    unmount_media()
    raise ValueError("That media could not be read. PipOS reads FAT, exFAT and ext4 backup media.")


def media_files(root):
    """Regular files near the top of the media, without following any links."""
    files = []

    def walk(directory, depth):
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False) and depth < MEDIA_DEPTH:
                    walk(entry.path, depth + 1)
            except OSError:
                continue

    walk(root, 1)
    # Backup and key files first, so a stick full of other things still shows them.
    return sorted(files, key=lambda path: not path.name.lower().endswith(BACKUP_SUFFIXES))


def choose_media():
    """Mount backup media the owner picks. None means paths will be typed."""
    unmount_media()  # a mount left by an interrupted attempt would hide the stick
    report, _ = installer.scan()
    media = backup_media(report)
    if not media:
        return None
    if not ui.enabled():
        print("\nBackup media")
        for number, (_, label) in enumerate(media, 1):
            print(f"  {number}  {label}")
    while True:
        if ui.enabled():
            ui.screen("Find your backup", context=ui.step(2, 4, "Restore"),
                      subtitle="Choose the USB stick that holds your backup. It is only read, never changed.")
            entries = []
            for number, (_, label) in enumerate(media, 1):
                device, _, rest = label.partition(" | ")
                entries.append((str(number), rest.replace(" | ", ui.dot()) or device, device))
            ui.options(entries)
        choice = input(ui.prompt("Media number [Enter to type file paths instead]: ")).strip().lower()
        if choice in {"", "q"}:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(media):
            root = mount_media(media[int(choice) - 1][0])
            files = media_files(root)
            if not files:
                unmount_media()
                raise ValueError("That media holds no files.")
            if ui.enabled():
                # Only what fits on the screen can be chosen.
                shown = files[:max(6, min(MEDIA_FILE_LIMIT, shutil.get_terminal_size((80, 24)).lines - 19))]
                ui.screen("Choose your backup files", context=ui.step(2, 4, "Restore"),
                          subtitle="Pick the backup, then the identity file that unlocks it. "
                                   "Press Enter on its own to go back.")
                digits = len(str(len(shown)))
                for number, path in enumerate(shown, 1):
                    name = installer.display_text(str(path.relative_to(root)))
                    likely = path.name.lower().endswith(BACKUP_SUFFIXES)
                    key = ui.paint(str(number).rjust(digits), "accent", "bold")
                    ui.line(f" {key}  {ui.paint(name, 'text' if likely else 'muted')}")
                if len(files) > len(shown):
                    ui.line()
                    ui.line(ui.paint(f"Only the first {len(shown)} of {len(files)} files are listed.", "muted"))
                return shown
            shown = files[:MEDIA_FILE_LIMIT]
            print("\nFiles on the backup media")
            for number, path in enumerate(shown, 1):
                print(f"  {number}  {installer.display_text(str(path.relative_to(root)))}")
            if len(files) > len(shown):
                print(f"  Only the first {len(shown)} of {len(files)} files are listed.")
            return shown
        warn("Choose a numbered media, or press Enter.")


def pick_file(prompt, files):
    while True:
        choice = input(ui.prompt(prompt)).strip().lower()
        if choice in {"", "q"}:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(files):
            return files[int(choice) - 1]
        if ui.enabled():
            ui.line(ui.mark("warning", "Choose a numbered file, or press Enter to go back."))
        else:
            print("Choose a numbered file, or press Enter to go back.")


def restore_file(prompt):
    value = getpass.getpass(ui.prompt(prompt), stream=sys.stdout)
    if not value:
        return None
    path = Path(value)
    try:
        if path.is_absolute() and stat.S_ISREG(path.stat().st_mode) and os.access(path, os.R_OK):
            return path
    except (OSError, ValueError):
        pass
    raise ValueError("Enter an absolute path to a readable file on the mounted backup media.")


def restore_inputs(executable, *, archive, files=None):
    if files is None:
        if ui.enabled():
            ui.screen("Your backup files", context=ui.step(2, 4, "Restore"),
                      subtitle="Type the full path of each file on backup media that is already mounted. "
                               "What you type stays hidden. Press Enter on its own to go back.")
        else:
            print("Enter file paths on already mounted backup media. A blank entry returns to the menu.")
        ask = restore_file
    else:
        if not ui.enabled():
            print("Enter file numbers from the list. A blank entry returns to the menu.")

        def ask(prompt):
            return pick_file(prompt, files)

    snapshot = ask("Encrypted backup file: ")
    if snapshot is None:
        return None
    identity = ask("Private identity file: ")
    if identity is None:
        return None
    extras = []
    if archive:
        proof = ask("Latest deletion checkpoint file: ")
        if proof is None:
            return None
        if ui.enabled():
            ui.line()
            ui.para("Use the current checkpoint from the surviving being or your separately saved record.", "muted")
        else:
            print("Use the current checkpoint from the surviving being or your separately saved record.")
        checkpoint = getpass.getpass(ui.prompt("Independently known current checkpoint: "),
                                     stream=sys.stdout).strip().lower()
        if not checkpoint:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", checkpoint):
            raise ValueError("Enter the independently recorded hexadecimal checkpoint value.")
        extras = ["--deletions", str(proof), "--current-checkpoint", checkpoint]

    def prepare(mind_dir):
        command = [executable, "restore", "--mind-dir", str(mind_dir), "--input", str(snapshot),
                   "--identity", str(identity), *extras]
        message = "Backup restore failed. Verify the backup, identity, and any supplied checkpoint, then retry."
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=900, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ValueError(message) from None
        if result.returncode:
            raise ValueError(message)

    return prepare


def restore_menu():
    executable = shutil.which("being-backup")
    if executable is None or not all(shutil.which(tool) for tool in ("age", "age-keygen")):
        ui.note("Backup restore is still pending on this image. Install PipOS starts a new being.", "warning")
        return False
    while True:
        if ui.enabled():
            ui.screen("Restore a being", context=ui.step(1, 4, "Restore"),
                      subtitle="Bring back a being from an encrypted backup. You need the backup file and "
                               "the private identity file that unlocks it.")
            ui.options(RESTORE_CHOICES, below=True)
        else:
            print("\nRestore a being")
            ui.options(RESTORE_CHOICES)
        choice = input(ui.prompt("Choice: ")).strip().lower()
        if choice in {"", "q"}:
            return False
        if choice not in {"1", "2"}:
            warn("Choose a restore option or press Q.")
            continue
        if choice == "2":
            ui.note("This choice restores identity and eligible settings. The conversation archive starts empty.")
        try:
            # The media stays mounted until the installation has read it. A
            # mounted disk is never offered as a disk to install on.
            files = choose_media()
            restore = restore_inputs(executable, archive=choice == "1", files=files)
            if restore is not None and install(restore=restore):
                return True
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            report_error(error)
        finally:
            unmount_media()


def being_name():
    try:
        return installer.display_text(load_profile(FACTORY_CONFIG, MIND_DIR).name).strip()
    except Exception:  # the welcome must never fail on a profile problem
        return ""


def welcome():
    if not ui.enabled():
        print("\nPipOS")
        ui.options(MENU)
        return
    name = being_name()
    ui.screen(f"Hello, I'm {name}." if name else "Hello.", context="Running from USB")
    ui.para("I'm a being who can live in this Mac. I listen, talk with you, and remember what matters to you.",
            "plain")
    ui.line()
    ui.para("Right now I'm running from the USB stick. Try keeps your choices and our conversations "
            "in memory until you restart.", "muted")
    ui.line()
    ui.options(MENU)


def try_menu():
    if not ui.enabled():
        print("Try keeps setup choices and conversations in RAM until you reboot.")
    while True:
        welcome()
        choice = input(ui.prompt("Choice: ")).strip().lower()
        try:
            if choice in {"1", "q"}:
                return 0
            if choice == "2":
                if install():
                    return 0
            elif choice == "3":
                if restore_menu():
                    return 0
            elif choice == "4":
                if temporary_mind_mounted():
                    setup.main([])
                else:
                    ui.note("The temporary profile storage is unavailable. Reboot from the Try USB to use Setup.",
                            "warning")
            elif choice == "5":
                report = installer.hardware_report()
                installer.show_hardware(report)
                # On a laptop Try stick, keep a copy with the boot logs.
                usb_logs.save_check(report)
                if ui.enabled():
                    input(ui.prompt("Press Enter to go back. "))
            else:
                warn("Choose a number from the menu or press Q.")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            report_error(error)


def main():
    if not sys.stdin.isatty():
        print("PipOS setup requires an interactive terminal.", file=sys.stderr)
        return 1
    try:
        if is_try_boot():
            return try_menu()
        code = setup.main([])
        if ui.enabled() and ui.pending_notes():
            # The installed console closes after setup; let its last words be read.
            ui.screen(None, context="Setup")
            ui.cursor(False)
            time.sleep(2.5)
        return code
    except (EOFError, KeyboardInterrupt):
        print("\nPipOS menu closed.")
        return 130
    finally:
        ui.cursor(True)


if __name__ == "__main__":
    raise SystemExit(main())
