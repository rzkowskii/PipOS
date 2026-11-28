"""Keyboard-operated setup and installation on the PipOS screen."""

import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import console_ui as ui
from . import install_disks, installer, look, setup, type_chat, usb_logs
from .initiative import POLICY_PATH as INITIATIVE_POLICY_PATH, load_policy as load_initiative_policy
from .initiative_setup import describe_policy
from .profile import FACTORY_CONFIG, PROFILE_FILE, factory_profile, load_profile
from .try_mode import is_try_boot


MIND_DIR = Path("/mind")
DIAGNOSTIC_MARKER = Path("/usr/share/being/diagnostic")
MOUNTINFO = Path("/proc/self/mountinfo")
MEDIA_DIR = Path("/run/being-restore-media")
UPDATE_MEDIA_ROOT = Path("/run")
UPDATE_MEDIA_PREFIX = "being-update-"
# The foreign-media reader accepts only FAT and exFAT.
MEDIA_MOUNTS = (
    ("vfat", "ro,nodev,nosuid,noexec,noatime,utf8,umask=077"),
    ("exfat", "ro,nodev,nosuid,noexec,noatime,umask=077"),
)
# The encrypted backup Restore flow also accepts an ext4 backup. Reading it
# with noload prevents journal replay and keeps the source unchanged.
RECOVERY_MOUNTS = (
    *MEDIA_MOUNTS,
    ("ext4", "ro,nodev,nosuid,noexec,noatime,noload"),
)
# Only for writing a backup to a stick the owner picked. Still nothing on it
# may run or act as a device, and FAT writes go out at once (flush).
WRITABLE_MOUNTS = (
    ("vfat", "rw,nodev,nosuid,noexec,utf8,fmask=0177,dmask=0077,flush"),
    ("exfat", "rw,nodev,nosuid,noexec,fmask=0177,dmask=0077"),
    ("ext4", "rw,nodev,nosuid,noexec"),
)
MEDIA_FILE_LIMIT = 60
MEDIA_DEPTH = 4
MEDIA_MIN_BYTES = 1024**2
BACKUP_SUFFIXES = (".age", ".agekey", ".key", ".txt")
TRY_HINT = "Meet the being. Nothing is kept."


class RestoreMedia(list):
    """Displayed paths plus the exact read-only source they came from."""

    def __init__(self, files, *, root, device, uuid, private=False):
        super().__init__(files)
        self.root = Path(root)
        self.device = device
        self.uuid = uuid
        self.private = private


def _private_restore_device(device, uuid, *, selected=None, destination=None):
    """Ask one transient private-mount service to discover or stage."""
    from . import backup

    MEDIA_DIR.mkdir(mode=0o700, exist_ok=True)
    command = [
        "systemd-run", f"--unit={backup.RESTORE_DEVICE_UNIT}",
        "--quiet", "--wait", "--pipe", "--collect", "--service-type=exec",
        "--property=Type=exec", "--property=PrivateMounts=yes", "--property=PrivateNetwork=yes",
        "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=CAP_SYS_ADMIN",
        "--property=AmbientCapabilities=CAP_SYS_ADMIN", "--property=DevicePolicy=closed",
        f"--property=DeviceAllow={device} r", "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=TasksMax=8", "--property=MemorySwapMax=0", "--property=RuntimeMaxSec=15min",
        f"--property=ReadWritePaths={MEDIA_DIR}", "/usr/sbin/being-backup", "restore-device",
        "--device", device, "--uuid", uuid,
    ]
    if selected:
        command.insert(command.index("/usr/sbin/being-backup"), f"--property=ReadWritePaths={destination}")
        command.extend(("--destination", str(destination)))
        for role, relative in selected:
            command.extend(("--selected", f"{role}={relative}"))
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=910, check=False)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(("systemctl", "stop", f"{backup.RESTORE_DEVICE_UNIT}.service"),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        raise ValueError("I stopped a restore-media operation that took too long.") from None
    if result.returncode:
        raise ValueError("The private restore-media operation failed.")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("The private restore-media operation did not answer safely.") from None
# Short enough to share a line with its label: a longer hint moves every hint
# onto its own line and the Try menu no longer fits the 1280x800 panel.
TRY_HINT_LOGGED = "Nothing is kept. Logs go to this stick."
MENU = (
    ("1", "Try PipOS", TRY_HINT),
    ("2", "Install PipOS", "Make this Mac its home."),
    ("3", "Restore a being", "Bring a backed-up being home."),
    ("4", "Setup", "Name, voice and personality."),
    ("5", "Check hardware", "See what PipOS can find."),
    ("6", "Talk to Pip by typing", "When Pip can't hear you."),
    ("7", "Back up to a USB stick", "Save this Try being to a stick."),
    ("8", "Run diagnostics", "Test the mic, speakers and voice."),
    ("Q", "Return to PipOS", None),
)
TYPE_CHAT_KEY = "6"
BACKUP_KEY = "7"
DIAG_KEY = "8"
DIAG_CHOICES = (
    ("1", "Quick", "About a minute. Nothing for you to do."),
    ("2", "Full", "10 to 15 minutes. Pip plays test sounds."),
    ("3", "Voice", "You speak and listen. About 10 minutes."),
    ("Q", "Back", None),
)
DIAG_MODES = {"1": "quick", "2": "full", "3": "voice"}
DIAG_LAST_RUN = Path("/run/being-diag/last-run.json")
DIAG_SUMMARY_LINES = 28
RESTORE_CHOICES = (
    ("1", "Restore with conversation archive",
     "Who the being is, its settings and past conversations. Reminders, Wi-Fi and the weather city start fresh."),
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
            os.path.lexists(MIND_DIR / name) for name in (PROFILE_FILE, INITIATIVE_POLICY_PATH, look.FILE))
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
    try:
        changed = profile != factory_profile(FACTORY_CONFIG)
    except Exception:  # a factory problem must not block keeping the Try choices
        changed = True
    # Choices the owner made in Try are kept unless they say otherwise.
    answer = input(ui.prompt("Keep this Try profile? [Y/n] " if changed else "Keep this Try profile? [y/N] "))
    answer = answer.strip().lower()
    if answer in {"y", "yes"} or (changed and answer == ""):
        return profile
    return None


def zone_to_keep():
    """The time zone chosen in Try, to seed the installed being's clock."""
    if not temporary_mind_mounted():
        return None
    import json

    from .skills import find_zone

    try:
        # Only a zone the owner chose in Try; the factory zone needs no question.
        settings = json.loads((MIND_DIR / "skills.json").read_text(encoding="utf-8"))
        chosen = settings.get("time_zone") if isinstance(settings, dict) else None
        zone = find_zone(chosen) if isinstance(chosen, str) and chosen else None
    except (OSError, ValueError):
        return None
    if zone is None:
        return None
    key = installer.display_text(zone.key)
    if input(ui.prompt(f"Keep the time zone {key}? [Y/n] ")).strip().lower() in {"", "y", "yes"}:
        return {"time_zone": zone.key}
    return None


def try_appearance():
    if not temporary_mind_mounted():
        return None
    try:
        if not stat.S_ISREG((MIND_DIR / look.FILE).lstat().st_mode):
            return None
    except OSError:
        return None
    active = look.Store(MIND_DIR).read()
    default = look.Look()
    return active if any(getattr(active, field) != getattr(default, field) for field in look.FIELDS) else None


def appearance_to_keep():
    active = try_appearance()
    if active is not None and input(
        ui.prompt(f"Keep the Try appearance ({active.describe()})? [y/N] ")
    ).strip().lower() in {"y", "yes"}:
        return active
    return None


def carry_to_keep():
    """Bring Pip home: an opt-in offer, only when Try holds something the owner said."""
    from . import backup

    if not temporary_mind_mounted() or not backup.has_conversation(MIND_DIR):
        return None
    if ui.enabled():
        ui.line()
        ui.card(["You can bring this being home as it is: what you talked about, what it remembers,",
                 "its name, voice and settings. Otherwise the installed being starts fresh."],
                title="Bring Pip home")
    else:
        print("\nBring Pip home: carry what you talked about, what it remembers and its settings "
              "into the installation. Otherwise the installed being starts fresh.")
    if input(ui.prompt("Keep what you and Pip talked about? [y/N] ")).strip().lower() not in {"y", "yes"}:
        return None
    appearance = try_appearance()

    def restore(target):
        backup.carry(MIND_DIR, target)
        if appearance is not None:
            look.seed(target, appearance)

    return restore


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
    contents = install_disks.disk_contents(disk["device"], disk.get("logical_sector_bytes") or 512)
    if contents["being"]:
        details.append("holds a PipOS being")
    elif contents["macos"]:
        details.append("holds macOS")
    ui.card([ui.pad(left, inner - ui.visible_len(right)) + right,
             "   " + ui.paint(ui.dot().join(details), "muted")], tone="accent_soft")


def disk_tiles(target, number, source, width):
    details = [f"{installer.display_text(target['device'])}{ui.dot()}{ui.size(target['size_bytes'])}"]
    if target.get("serial"):
        details.append(f"Serial {installer.display_text(target['serial'])}")
    contents = install_disks.disk_contents(target["device"], target.get("logical_sector_bytes") or 512)
    if contents["being"]:
        details.append("Holds a PipOS being")
    elif contents["macos"]:
        details.append("Holds macOS")
    target_rows = [(f"{number}  {installer.friendly_name(target)}", "bright", "bold"),
                   *((detail, "text") for detail in details), ("! Erased only after confirmation", "warning")]
    source_rows = [(installer.friendly_name(source), "plain", "bold"),
                   (f"{installer.display_text(source['device'])}{ui.dot()}{ui.size(source['size_bytes'])}", "muted"),
                   (installer.friendly_reason(source), "muted")]
    installer.paired_cards(installer.card_rows(target_rows, width, "accent_soft"),
                           installer.card_rows(source_rows, width, "muted"))


def draw_disks(report, available, flow):
    name, number, total = flow
    context = name if name == "Install" and ui.columns() >= 80 else ui.step(number, total, name)
    ui.screen("Choose a disk", context=context,
              subtitle="Pick where your being will live. Everything on that disk will be replaced." if available
              else "Your being needs a disk of its own, separate from this USB stick.")
    disks = {disk["device"]: disk for disk in report["disks"]}
    source = disks.get(report.get("boot_disk"))
    paired = len(available) == 1 and source is not None and source.get("transport") and ui.columns() >= 80
    if paired:
        key, device = next(iter(available.items()))
        disk_tiles(disks[device], key, source, installer.card_width())
    else:
        for key, device in available.items():
            disk_card(key, disks[device])
    if not available:
        ui.card([ui.paint("No disk here can hold PipOS right now.", "text", "bold"),
                 "Connect or free up a disk, then try again."], tone="warning")
    others = [disk for disk in report["disks"] if disk.get("transport") and disk["device"] not in available.values()
              and (not paired or disk is not source)]
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
    if name == "Install":
        ui.line()
        ui.line(installer.wizard_steps(1))


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


def cable_refusal():
    """On a cable development stick, say up front that it never installs. True when refused."""
    if not installer.cable_media():
        return False
    if ui.enabled():
        ui.note(installer.CABLE_MESSAGE, "warning")
    else:
        print(installer.CABLE_MESSAGE)
    return True


def diagnostic_refusal():
    """Diagnostic media measures hardware but never installs or restores onto a target."""
    if not DIAGNOSTIC_MARKER.is_file():
        return False
    message = "This maintenance image cannot install or restore PipOS. Restart with the normal Try USB."
    if ui.enabled():
        ui.note(message, "warning")
    else:
        print(message)
    return True


def install(*, restore=None):
    if diagnostic_refusal() or cable_refusal():
        return False
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
            context = name if ui.enabled() and ui.columns() >= 80 else ui.step(2, total, name)
            ui.screen("Your Try settings", context=context,
                      subtitle="Choose what the installed being keeps from this Try session.")
            if ui.enabled():
                ui.line(installer.wizard_steps(2))
                ui.line()
        carried = carry_to_keep()
        if carried is not None:
            # The whole Try being goes home, its settings included.
            choices.update(restore=carried, carried=True)
        else:
            choices["profile"] = profile_to_keep()
            initiative = initiative_to_keep()
            if initiative is not None:
                choices["initiative"] = initiative
            zone = zone_to_keep()
            if zone is not None:
                choices["skills"] = zone
            appearance = appearance_to_keep()
            if appearance is not None:
                choices["appearance"] = appearance
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


def mount_media(device, *, writable=False):
    if writable:
        return _mount_media(device, WRITABLE_MOUNTS, "FAT, exFAT and ext4 backup media")
    return _mount_media(device, MEDIA_MOUNTS, "FAT and exFAT media")


def _mount_media(device, mounts, supported):
    unmount_media()
    MEDIA_DIR.mkdir(mode=0o700, exist_ok=True)
    if _mount_at(device, MEDIA_DIR, mounts):
        return MEDIA_DIR
    unmount_media()
    raise ValueError(f"That media could not be read. This screen supports {supported}.")


def _mount_at(device, root, mounts):
    for filesystem, options in mounts:
        try:
            result = subprocess.run(["mount", "-t", filesystem, "-o", options, device, str(root)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


@contextmanager
def update_media(device):
    """Give RAUC a dedicated host-visible, read-only mount for this update."""
    from . import care

    parent = Path(tempfile.mkdtemp(prefix=UPDATE_MEDIA_PREFIX, dir=UPDATE_MEDIA_ROOT))
    root = parent / "media"
    deferred = False
    try:
        root.mkdir(mode=0o700)
        if not _mount_at(device, root, MEDIA_MOUNTS):
            raise ValueError("That update stick could not be read. Use FAT or exFAT media.")
        yield root
    except care.UpdateWaitInterrupted:
        deferred = True
        (parent / "pending").touch(mode=0o600)
        raise
    finally:
        if not deferred and not _release_update_media(parent):
            ui.note("The update stick is still in use. Open Update again to release it.", "warning")


def _release_update_media(parent):
    from . import care

    root = parent / "media"
    if os.path.ismount(root):
        care.quiet(["umount", str(root)], timeout=15)
        if os.path.ismount(root):
            (parent / "pending").touch(mode=0o600)
            return False
    if root.exists():
        root.rmdir()
    (parent / "pending").unlink(missing_ok=True)
    parent.rmdir()
    return True


def release_previous_update_media():
    """Keep timed-out or interrupted updates reachable until RAUC is idle."""
    from . import care

    pending = list(UPDATE_MEDIA_ROOT.glob(f"{UPDATE_MEDIA_PREFIX}*/pending"))
    if pending and not care.update_service_idle():
        raise ValueError("PipOS could not confirm that the updater is idle. "
                         "Leave the USB stick plugged in and check again later.")
    for marker in pending:
        if not _release_update_media(marker.parent):
            raise ValueError("The earlier update stick is still in use.")


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
            from . import backup

            device = media[int(choice) - 1][0]
            identity = backup.media_identity(device)
            if identity is None:
                raise ValueError("That backup stick has no stable filesystem identity.")
            if (Path("/etc/being/env").is_file() and shutil.which("systemd-run")
                    and Path("/usr/sbin/being-backup").is_file()):
                try:
                    with backup.backup_lock():
                        answer = _private_restore_device(device, identity)
                except backup.Unavailable:
                    raise ValueError("Another backup-media operation is already in progress.") from None
                entries = answer.get("files") if isinstance(answer, dict) else None
                if not isinstance(entries, list) or not entries:
                    raise ValueError("That media holds no files.")
                files = []
                for entry in entries[:MEDIA_FILE_LIMIT]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                        raise ValueError("The private restore-media operation returned an unsafe list.")
                    relative = Path(entry["id"])
                    if (relative.is_absolute() or not relative.parts or len(relative.parts) > 5
                            or any(part in ("", ".", "..") for part in relative.parts)):
                        raise ValueError("The private restore-media operation returned an unsafe path.")
                    files.append(MEDIA_DIR / relative)
                if ui.enabled():
                    ui.screen("Choose your backup files", context=ui.step(2, 4, "Restore"),
                              subtitle="Pick the backup, then the identity file that unlocks it. "
                                       "Press Enter on its own to go back.")
                    digits = len(str(len(files)))
                    for number, entry in enumerate(entries[:len(files)], 1):
                        key = ui.paint(str(number).rjust(digits), "accent", "bold")
                        label = installer.display_text(entry.get("label", entry["id"]))
                        ui.line(f" {key}  {ui.paint(label, 'text')}")
                else:
                    print("\nFiles on the backup media")
                    for number, entry in enumerate(entries[:len(files)], 1):
                        print(f"  {number}  {installer.display_text(entry.get('label', entry['id']))}")
                return RestoreMedia(files, root=MEDIA_DIR, device=device, uuid=identity, private=True)
            root = _mount_media(device, RECOVERY_MOUNTS, "FAT, exFAT and ext4 backup media")
            if backup.media_identity(device) != identity or not backup.mounted_from(root, device):
                unmount_media()
                raise ValueError("The backup stick changed while it was being opened.")
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
                return RestoreMedia(shown, root=root, device=device, uuid=identity)
            shown = files[:MEDIA_FILE_LIMIT]
            print("\nFiles on the backup media")
            for number, path in enumerate(shown, 1):
                print(f"  {number}  {installer.display_text(str(path.relative_to(root)))}")
            if len(files) > len(shown):
                print(f"  Only the first {len(shown)} of {len(files)} files are listed.")
            return RestoreMedia(shown, root=root, device=device, uuid=identity)
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


def confirm_checkpoint_record(record):
    """Parse a staged checkpoint record and have the owner confirm it."""
    from .backup import fingerprint, read_checkpoint_file

    _, checkpoint = read_checkpoint_file(record)
    short = fingerprint(checkpoint)
    if ui.enabled():
        ui.line()
        ui.card([ui.paint(f"Checkpoint {short}", "text", "bold"),
                 "Compare it with the checkpoint the surviving being showed, or the one you wrote down."],
                title="Check the fingerprint")
    else:
        print(f"Checkpoint {short}")
        print("Compare it with the checkpoint the surviving being showed, or the one you wrote down.")
    if input(ui.prompt("Does it match? [y/N] ")).strip().lower() not in {"y", "yes"}:
        raise ValueError("The checkpoint was not confirmed. Nothing was restored.")
    return checkpoint


def checkpoint_from_record(files):
    """Read a local record and have the owner confirm its fingerprint."""
    from .backup import read_checkpoint_file

    while True:
        record = pick_file("Checkpoint record file [Enter to type the checkpoint instead]: ", files)
        if record is None:
            return None
        try:
            read_checkpoint_file(record)
        except (OSError, ValueError):
            message = "That file is not a checkpoint record. Choose the one ending in -checkpoint.txt."
            ui.line(ui.mark("warning", message)) if ui.enabled() else print(message)
            continue
        return confirm_checkpoint_record(record)


def unlock_if_protected(identity):
    """(key path, holder). A passphrase-protected key is unlocked into RAM once.

    The holder is a temporary directory that removes the unlocked key when it
    is cleaned up or dropped; it is None for a key without a passphrase.
    """
    from .backup import working_directory
    from .backup_crypto import BackupCryptoError, is_protected_file, unlock_identity

    if not is_protected_file(identity):
        return identity, None
    holder = working_directory()
    if ui.enabled():
        ui.line()
        ui.para("This key has a passphrase. Type it when asked; it is not shown.", "muted")
    else:
        print("This key has a passphrase. Type it when asked; it is not shown.")
    try:
        return unlock_identity(identity, Path(holder.name) / "identity.key"), holder
    except BackupCryptoError as error:
        holder.cleanup()
        raise ValueError(f"{error} Nothing was restored.") from None


def stage_restore_media(media, selected, destination):
    """Use a capability-empty transient broker on the appliance.

    Developer/host fixtures have neither the packaged executable nor systemd,
    so they use the same implementation in-process. The recovery caller is
    already root; this is process confinement, not a new authorization path.
    """
    from . import backup

    if media.private:
        requests = [(role, str(Path(path).relative_to(media.root))) for role, path in selected]
        try:
            with backup.backup_lock():
                answer = _private_restore_device(media.device, media.uuid, selected=requests,
                                                 destination=destination)
        except backup.Unavailable:
            raise ValueError("Another backup-media operation is already in progress.") from None
        staged = answer.get("staged") if isinstance(answer, dict) else None
        if not isinstance(staged, dict) or set(staged) != {role for role, _ in selected}:
            raise ValueError("The private restore-media operation returned unexpected files.")
        paths = {role: Path(path) for role, path in staged.items()}
        if any(path.parent != Path(destination) or path.name != f"{role}.source"
               for role, path in paths.items()):
            raise ValueError("The private restore-media operation returned an unsafe staged path.")
        return paths

    appliance = Path("/etc/being/env").is_file()
    runner = shutil.which("systemd-run")
    helper = Path("/usr/sbin/being-backup")
    if not appliance or runner is None or not helper.is_file():
        return backup.stage_restore_artifacts(media.root, media.device, media.uuid, selected, destination)
    command = [
        runner, "--unit=being-restore-stage", "--quiet", "--wait", "--pipe", "--collect",
        "--service-type=exec", "--property=Type=exec", "--property=PrivateNetwork=yes",
        "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=InaccessiblePaths=/mind",
        "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=",
        "--property=DevicePolicy=closed", "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=LockPersonality=yes", "--property=RestrictSUIDSGID=yes",
        "--property=RestrictNamespaces=yes", "--property=SystemCallArchitectures=native",
        "--property=ProtectKernelTunables=yes", "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes", "--property=ProtectControlGroups=yes",
        "--property=ProtectClock=yes", "--property=UMask=0077",
        "--property=TasksMax=8", "--property=MemorySwapMax=0",
        "--property=RuntimeMaxSec=15min", "--property=TimeoutStopSec=10s",
        f"--property=ReadOnlyPaths={media.root}", f"--property=ReadWritePaths={destination}",
        str(helper), "stage-restore", "--root", str(media.root), "--device", media.device,
        "--uuid", media.uuid, "--destination", str(destination),
    ]
    for role, path in selected:
        command.extend(("--artifact", f"{role}={path}"))
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=910, check=False)
    except subprocess.TimeoutExpired:
        try:
            stopped = subprocess.run(("systemctl", "stop", "being-restore-stage.service"),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=15, check=False)
            if stopped.returncode:
                subprocess.run(("systemctl", "kill", "--kill-whom=all", "--signal=KILL",
                                "being-restore-stage.service"), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            try:
                subprocess.run(("systemctl", "kill", "--kill-whom=all", "--signal=KILL",
                                "being-restore-stage.service"), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        raise ValueError("I stopped backup-file staging because it took too long.") from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError("I couldn't start the confined backup-file staging helper.") from None
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        message = detail[-1].removeprefix("being-backup: ") if detail else "The backup files could not be staged safely."
        raise ValueError(message[:300])
    try:
        answer = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("The confined backup-file staging helper did not answer safely.") from None
    expected = {role for role, _ in selected}
    if set(answer) != expected:
        raise ValueError("The confined backup-file staging helper returned unexpected files.")
    paths = {role: Path(path) for role, path in answer.items()}
    if any(path.parent != Path(destination) or path.name != f"{role}.source"
           for role, path in paths.items()):
        raise ValueError("The confined backup-file staging helper returned an unsafe path.")
    return paths


def restore_inputs(executable, *, archive, files=None):
    staged_holder = None
    keep_restore = False
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
    if files is not None:
        # Restoring works, but the stick alone opens the backup.
        ui.note("The private key is on the same stick as the backup. Anyone with this stick can read it. "
                "Keep the key on a different stick.", "warning")
    extras = []
    proof = None
    record = None
    if archive:
        proof = ask("Latest deletion checkpoint file: ")
        if proof is None:
            return None
        checkpoint = None
        if files is not None:
            record = pick_file("Checkpoint record file [Enter to type the checkpoint instead]: ", files)
            if (isinstance(files, RestoreMedia) and files.private
                    and Path("/usr/sbin/being-keep-file-backup").is_file()):
                keep_restore = input(ui.prompt(
                    "Also restore encrypted Keep files already named by this authenticated catalog? [y/N] "
                )).strip().lower() in {"y", "yes"}
    if files is not None:
        from . import backup

        if not isinstance(files, RestoreMedia):
            raise ValueError("The selected restore media has no stable source identity.")
        staged_holder = backup.working_directory()
        staged_root = Path(staged_holder.name)
        selected = [("snapshot", snapshot), ("identity", identity)]
        if proof is not None:
            selected.append(("deletions", proof))
        if record is not None:
            selected.append(("checkpoint", record))
        try:
            staged = stage_restore_media(files, selected, staged_root)
            unmount_media()
            if os.path.ismount(files.root):
                raise ValueError("The backup files were staged, but the stick could not be unmounted safely.")
        except BaseException:
            staged_holder.cleanup()
            raise
        snapshot = staged["snapshot"]
        identity = staged["identity"]
        if proof is not None:
            proof = staged["deletions"]
        if record is not None:
            try:
                checkpoint = confirm_checkpoint_record(staged["checkpoint"])
            except (OSError, ValueError):
                staged_holder.cleanup()
                raise
    else:
        # Rescue operators may name already-mounted files (including a key on
        # a separate stick). Copy each opaque input through a stable regular
        # file descriptor before age or any parser sees it. These mounts are
        # externally owned, so this path deliberately does not unmount them.
        from . import backup

        staged_holder = backup.working_directory()
        staged_root = Path(staged_holder.name)
        staged = {}
        selected = [("snapshot", snapshot), ("identity", identity)]
        if proof is not None:
            selected.append(("deletions", proof))
        try:
            for role, path in selected:
                staged.update(backup.stage_restore_artifacts(Path(path).parent, None, None,
                                                             [(role, path)], staged_root))
        except BaseException:
            staged_holder.cleanup()
            raise
        snapshot = staged["snapshot"]
        identity = staged["identity"]
        if proof is not None:
            proof = staged["deletions"]

    # A passphrase is asked only after removable media is unmounted, before a
    # progress screen can draw over age's prompt.
    try:
        identity, unlocked = unlock_if_protected(identity)
    except BaseException:
        if staged_holder is not None:
            staged_holder.cleanup()
        raise
    if archive:
        if checkpoint is None:
            if ui.enabled():
                ui.line()
                ui.para("Use the current checkpoint from the surviving being or your separately saved record.",
                        "muted")
            else:
                print("Use the current checkpoint from the surviving being or your separately saved record.")
            checkpoint = getpass.getpass(ui.prompt("Independently known current checkpoint: "),
                                         stream=sys.stdout).strip().lower()
            if not checkpoint:
                if unlocked is not None:
                    unlocked.cleanup()
                if staged_holder is not None:
                    staged_holder.cleanup()
                return None
            if not re.fullmatch(r"[0-9a-f]{64}", checkpoint):
                if unlocked is not None:
                    unlocked.cleanup()
                if staged_holder is not None:
                    staged_holder.cleanup()
                raise ValueError("Enter the independently recorded hexadecimal checkpoint value.")
        extras = ["--deletions", str(proof), "--current-checkpoint", checkpoint]

    keep_result = {}

    def prepare(mind_dir):
        from .backup_crypto import BackupCryptoError, _read_identity

        command = [executable, "restore", "--mind-dir", str(mind_dir), "--input", str(snapshot),
                   "--identity", str(identity), *extras]
        message = "Backup restore failed. Verify the backup, identity, and any supplied checkpoint, then retry."
        key = None
        try:
            if keep_restore:
                key = _read_identity(identity)
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=900, check=False)
        except (BackupCryptoError, OSError, subprocess.SubprocessError):
            raise ValueError(message) from None
        finally:
            if unlocked is not None:
                unlocked.cleanup()  # the unlocked key lived in RAM only for this restore
            if staged_holder is not None:
                staged_holder.cleanup()  # encrypted source copies lived in private /run
        if result.returncode:
            raise ValueError(message)
        if keep_restore:
            try:
                keep = subprocess.run(
                    ["/usr/sbin/being-keep-file-backup", "restore", "--mind-dir", str(mind_dir),
                     "--device", files.device, "--uuid", files.uuid],
                    input=key, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=1810, check=False,
                )
                if len(keep.stdout) > 4096:
                    raise ValueError("The Keep recovery returned too much information.")
                answer = json.loads(keep.stdout.decode("ascii"))
                if (keep.returncode or not isinstance(answer, dict) or answer.get("ok") is not True
                        or any(type(answer.get(name)) is not int or answer[name] < 0
                               for name in ("restored", "missing", "skipped"))):
                    raise ValueError("The encrypted Keep files could not be recovered.")
                keep_result.update(answer)
            except (BackupCryptoError, OSError, subprocess.SubprocessError, UnicodeError,
                    json.JSONDecodeError, ValueError) as error:
                keep_result["error"] = str(error)[:200]

    prepare.keep_result = keep_result
    return prepare


def restore_menu():
    # A restore installs too: a cable stick refuses before any media or disk is chosen.
    if diagnostic_refusal() or cable_refusal():
        return False
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
                keep_result = getattr(restore, "keep_result", {})
                if "error" in keep_result:
                    ui.note("The being was restored, but its encrypted Keep files were not: "
                            + keep_result["error"], "warning")
                elif keep_result.get("ok") is True:
                    ui.note(f"Recovered {keep_result['restored']} Keep file"
                            f"{'s' if keep_result['restored'] != 1 else ''}; "
                            f"{keep_result['missing']} remain missing.", "plain")
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


def flush_keys():
    """Drop key presses typed during a check, so they don't answer the next prompt."""
    try:
        import termios
    except ImportError:
        return
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (termios.error, OSError, ValueError):  # termios.error is not an OSError: stdin is not a terminal
        pass


def answer(question):
    return input(ui.prompt(f"{question} [y/N] ")).strip().lower() in {"y", "yes"}


def guided_checks():
    """Keys, screen and camera, with the owner watching. Results go to the stick."""
    results = {}
    ui.screen("Guided checks", context="Check hardware",
              subtitle="Three short checks. Their results are saved with this boot's diagnostics.")
    ui.para("1. Keys. For 10 seconds, press F1 to F12 along the top row, then the same keys while "
            "holding fn.", "plain")
    input(ui.prompt("Press Enter to start. "))
    results["keys"] = usb_logs.capture_keys(10)
    flush_keys()
    ui.note(f"Heard {len(results['keys']['pressed'])} key presses.")
    ui.line()
    ui.para("2. Screen. The screen goes dark for one second. Watch the Apple logo on the lid.", "plain")
    input(ui.prompt("Press Enter to start. "))
    results["blank"] = usb_logs.blank_check()
    results["blank_screen_went_dark"] = answer("Did the screen go dark?")
    results["blank_lid_logo_went_dark"] = answer("Did the Apple logo on the lid go dark?")
    ui.line()
    ui.para("3. Camera. With your permission the camera runs for 10 seconds. Look for the green light. "
            "No pictures are kept.", "plain")
    if answer("Turn the camera on for 10 seconds?"):
        results["camera_started"] = camera_check(10)
        results["camera_light_seen"] = answer("Did the green light come on?")
    else:
        results["camera"] = "skipped by the owner"
    saved = usb_logs.save_guided(results)
    ui.note("Saved with the boot diagnostics." if saved else "There is no diagnostics stick on this boot.",
            "success" if saved else "muted")
    input(ui.prompt("Press Enter to go back. "))


def camera_check(seconds):
    """The camera on for `seconds`, as the owner agreed, then off again if it
    was off before: always, even after Ctrl-C or a hung command."""
    was_on = (MIND_DIR / "camera.enabled").is_file()
    started = False
    try:
        started = subprocess.run(["being-camera", "enable"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=40, check=False).returncode == 0
        time.sleep(seconds)
    finally:
        if not was_on:
            try:
                subprocess.run(["being-camera", "disable"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=40, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
    return started


def wake_phrase():
    try:
        return installer.display_text(load_profile(FACTORY_CONFIG, MIND_DIR).wake_word).strip()
    except Exception:  # the welcome must never fail on a profile problem
        return ""


def menu_entries(name=None):
    """The Try menu, worded for what this boot really keeps, with the being's own name."""
    try:
        logged = usb_logs.logging_enabled() is True
    except OSError:
        logged = False
    name = name or "Pip"
    entries = []
    for key, label, hint in MENU:
        if DIAGNOSTIC_MARKER.is_file() and key in {"2", "3"}:
            continue
        if key == TYPE_CHAT_KEY:
            label, hint = f"Talk to {name} by typing", f"When {name} can't hear you."
        elif logged and hint == TRY_HINT:
            hint = TRY_HINT_LOGGED
        entries.append((key, label, hint))
    return tuple(entries)


def how_to_talk(name):
    name = name or "Pip"
    wake = wake_phrase() or f"Hey {name}"
    return f'Say "{wake}", wait for a chirp, then talk. Or choose "Talk to {name} by typing".'


def welcome():
    if not ui.enabled():
        name = being_name()
        print("\nPipOS")
        print(how_to_talk(name))
        ui.options(menu_entries(name))
        return
    name = being_name()
    ui.screen(f"Hello, I'm {name}." if name else "Hello.", context="Running from USB")
    ui.para(how_to_talk(name), "plain")
    ui.line()
    entries = menu_entries(name)
    trial = entries[0]
    install = next((entry for entry in entries if entry[0] == "2"), None)
    other = [entry for entry in entries[1:] if entry[0] != "2"]
    trial_status = "Choices and conversations clear when you restart."
    if trial[2] == TRY_HINT_LOGGED:
        trial_status += " Logs go to this USB."
    if install is not None and ui.columns() >= 80:
        width = installer.card_width()
        left = installer.card_rows([
            ("○", "accent"), (f"{trial[0]}  {trial[1]}", "bright", "bold"),
            (trial_status, "muted"), ("",), ("✓ No changes to the internal disk", "success"),
        ], width, "accent_soft")
        right = installer.card_rows([
            ("↓", "accent"), (f"{install[0]}  {install[1]}", "bright", "bold"),
            ("Give Pip a home and keep its memory between wakes.", "muted"), ("",),
            ("! Erases the disk you choose", "warning"),
        ], width, "accent_soft")
        installer.paired_cards(left, right)
    elif install is not None:
        ui.card([ui.option(*trial[:2]), ui.paint(trial_status, "muted")], tone="accent_soft")
        ui.card([ui.option(*install[:2]),
                 ui.paint("Review the disk layout before confirming an erase.", "muted")], tone="accent_soft")
    else:
        ui.card([ui.option(*trial[:2]), ui.paint(trial_status, "muted"),
                 ui.paint("Maintenance media cannot install or restore PipOS.", "warning")],
                tone="accent_soft")
    ui.line()
    ui.options(other)
    if ui.room() >= 2:
        ui.line()
        ui.line(installer.wizard_steps(0))


def run_diagnostic(mode):
    """Run pip-diag MODE on this screen and keyboard; its exit code.

    The child gets Ctrl-C itself and puts the voice back; the menu only waits,
    so it never kills a run halfway through its restore."""
    import signal

    command = [sys.executable, "-m", "mind.diag", mode]
    try:
        child = subprocess.Popen(command)
    except OSError:
        return 2
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        while True:
            try:
                return child.wait()
            except InterruptedError:
                continue
    finally:
        signal.signal(signal.SIGINT, previous)


def diagnostic_summary(last_run=None):
    """(folder, the summary lines of diag.txt), or (None, []) when no run finished."""
    import json

    try:
        folder = Path(json.loads((last_run or DIAG_LAST_RUN).read_text(encoding="utf-8"))["folder"])
        text = (folder / "diag.txt").read_text(encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        return None, []
    lines = [line for line in text.split("Every check", 1)[0].splitlines() if line.strip()]
    keep = [line for line in lines if not line.startswith(("command:", "started utc:", "finished utc:", "folder:",
                                                          "clock:", "entry:", "boot id:", "build id:"))]
    return folder, keep[:DIAG_SUMMARY_LINES]


def diagnostics_menu():
    """Quick, Full or Voice diagnostics; the summary is shown here and saved to the stick."""
    while True:
        if ui.enabled():
            ui.screen("Run diagnostics", context="Diagnostics",
                      subtitle="Measure the microphone, speakers, voice and spoken commands. "
                               "Results go to the logs stick when there is one.")
            ui.options(DIAG_CHOICES, below=True)
        else:
            print("\nRun diagnostics")
            ui.options(DIAG_CHOICES)
        choice = input(ui.prompt("Choice: ")).strip().lower()
        if choice in {"", "q"}:
            return
        if choice not in DIAG_MODES:
            warn("Choose 1, 2 or 3, or press Q.")
            continue
        mode = DIAG_MODES[choice]
        if ui.enabled():
            ui.screen(f"Diagnostics: {DIAG_CHOICES[int(choice) - 1][1]}", context="Diagnostics",
                      subtitle="Pip's ears and voice pause while the sound card is tested.")
        flush_keys()
        started = time.time()
        code = run_diagnostic(mode)
        flush_keys()
        folder, lines = diagnostic_summary()
        try:
            fresh = folder is not None and (folder / "diag.txt").stat().st_mtime >= started - 1
        except OSError:
            fresh = False
        if ui.enabled():
            ui.screen("Diagnostics finished" if code != 2 else "Diagnostics could not run", context="Diagnostics",
                      subtitle=f"Saved in {installer.display_text(str(folder))}" if fresh else None)
            for line in lines if fresh else []:
                for part in ui.wrap(installer.display_text(line), ui.width() - 2) or [""]:
                    ui.line(part)
        else:
            print("\n".join(lines) if fresh else "No diagnostics report was written.")
        input(ui.prompt("Press Enter to go back. "))
        return


IDLE_SECONDS = 240


class Idle(Exception):
    """Nobody touched the menu for a while."""


def idle_input(text, seconds=IDLE_SECONDS):
    """input(), but give the screen back to the eyes after a few idle minutes."""
    import signal

    if not hasattr(signal, "SIGALRM") or not ui.enabled():
        return input(text)

    def expire(*_):
        raise Idle()

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        return input(text)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def try_menu():
    if not ui.enabled():
        print("Try keeps setup choices and conversations in RAM until you reboot.")
    while True:
        welcome()
        try:
            # Only this top menu times out: nothing is half-done here.
            choice = idle_input(ui.prompt("Choice: ")).strip().lower()
        except Idle:
            return 0
        try:
            if choice in {"1", "q"}:
                return 0
            if choice == "2":
                if diagnostic_refusal():
                    continue
                if install():
                    return 0
            elif choice == "3":
                if diagnostic_refusal():
                    continue
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
                    ui.para("Press G for guided checks.", "muted")
                    if input(ui.prompt("Press Enter to go back. ")).strip().lower() == "g":
                        guided_checks()
            elif choice == TYPE_CHAT_KEY:
                type_chat.main()
            elif choice == BACKUP_KEY:
                from . import care

                care.backup_menu(MIND_DIR, temporary=True)
            elif choice == DIAG_KEY:
                diagnostics_menu()
            else:
                warn("Choose a number from the menu or press Q.")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            report_error(error)


def _requested_page(page):
    from . import care

    temporary = is_try_boot()
    if page == "look":
        return setup.main(["--look"])
    if page == "care":
        return setup.main(["--care"])
    if page == "camera":
        care.camera_page(MIND_DIR, temporary)
    elif page == "camera-enable":
        care.camera_page(MIND_DIR, temporary, enable_only=True)
    elif page == "dream-tonight":
        care.dream_tonight_page(MIND_DIR)
    elif page == "permissions":
        care.permissions_page(MIND_DIR, temporary)
    elif page == "network":
        care.network_page(MIND_DIR, temporary)
    elif page == "backup":
        care.backup_menu(MIND_DIR, temporary=temporary)
    elif page == "update":
        try:
            care.update_menu(MIND_DIR, try_boot=temporary)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            report_error(error)
    return 0


def main(*, page=None):
    if not sys.stdin.isatty():
        print("PipOS setup requires an interactive terminal.", file=sys.stderr)
        return 1
    try:
        if page is not None:
            code = _requested_page(page)
        elif is_try_boot():
            return try_menu()
        else:
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
