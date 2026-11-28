"""The Care page: the being's senses, memory, network, time and sound, from F2.

The laptop image has no shell, so every owner control that used to need a root
file or a command line lives here: the camera, dreaming, the memory mode,
Wi-Fi and wired Ethernet, the date, time and time zone, what it remembers,
the dream journal, the workshop switch, volume and the microphone, backups to
a USB stick and system updates from one.

Each control says what it really does. Changes that the mind only reads at
start restart the mind organ, and say so. In Try every change lands in the RAM
/mind and is gone at the next restart.
"""

import array
import asyncio
import configparser
import json
import math
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path

from . import clock_epoch, console_ui as ui
from .dream_policy import CONSENT_FILE as DREAM_CONSENT
from .network_enforce import installed
from .policy import CAMERA_MARKER, WORKSHOP_DISABLED as APPS_DISABLED
from .policy import camera_allowed, dream_window, load_capabilities, workshop_allowed
from .storage import atomic_write, sync_directory


MIND = Path("/mind")
CAMERA_COMMAND = "/usr/sbin/being-camera"
WIFI_COMMAND = "/usr/sbin/being-wifi"
NETWORK_COMMAND = "/usr/sbin/being-network"
MIND_SERVICE = "being-mind.service"
VOICE_SERVICE = "being-voice.service"
APPS_ENABLED = "apps.enabled"
AUDIO_STATE = Path("audio/asound.state")  # relative to the mind directory
BACKUP_FOLDER = "PipOS-backups"
KEY_NAME = "pip-backup-key.age"
RECIPIENT_FILE = "backup-recipient.txt"  # the public half, kept in /mind
UPDATE_SUFFIX = ".raucb"
UPDATE_LIMIT = 20
RAUC_INSTALLED_CONF = Path("/run/systemd/generator.early/being-installed/rauc.conf")
BUILD_ID = Path("/etc/being/build-id")
COMMON_ZONES = (
    "Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane", "Australia/Adelaide", "Australia/Perth",
    "Pacific/Auckland", "Europe/London", "America/New_York", "America/Los_Angeles", "UTC",
)
MEMORY_MODES = {
    "persistent": ("Keeps our conversations", "I keep our conversations and what you ask me to remember."),
    "explicit-only": ("Only what you ask", "I keep only what you ask me to remember. New conversations are not "
                                           "archived. What is already archived stays until you forget it."),
    "session-only": ("Nothing after a restart", "I keep nothing once I restart. What is already on the disk "
                                                "stays there, unused, until you switch back or forget it."),
}
DREAM_WINDOWS = {
    "night": "At night, with the lid closed on power",
    "closed": "Whenever the lid is closed on power",
}
_NETWORK_MODE_LABELS = {"local-only": "Offline", "paired-lan": "Local network", "internet": "Internet"}
METER_SECONDS = 4.0
TONE_HZ = 660.0
TONE_SECONDS = 0.8
SAMPLE_RATE = 16000


# --- small helpers ------------------------------------------------------------

def run(command, *, timeout=60, **options):
    """One external tool. Missing tools and timeouts come back as a failed result."""
    try:
        return subprocess.run(command, timeout=timeout, check=False, **options)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", f"{command[0]} is not on this image.")
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))


def quiet(command, *, timeout=60, **options):
    return run(command, timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True, text=True, **options)


def yes(question, default=False):
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(ui.prompt(f"{question} {hint} ")).strip().lower()
    return answer in {"y", "yes"} or (default and answer == "")


def pause():
    if ui.enabled():
        ui.flush_notes()  # a note made on this screen is read before going back
        input(ui.prompt("Press Enter to go back. "))


def done(text, tone="success"):
    ui.note(text, tone)


def restart_mind(reason="the change"):
    """The mind reads these settings at start, so it is restarted to use them."""
    units = installed(MIND_SERVICE)
    if not units:
        return False
    result = quiet(["systemctl", "try-restart", *units], timeout=60)
    if result.returncode:
        ui.note(f"Saved, but the mind could not restart to use {reason}. It applies after the next restart.",
                "warning")
        return False
    return True


def private_marker(path, contents=""):
    atomic_write(path, contents, mode=0o600)


def remove_marker(path):
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_directory(path.parent)


def menu(title, entries, *, intro=(), subtitle=None, context="Care", draw=None):
    """Draw a menu and return the chosen key, lower-cased. Enter or Q means back."""
    keys = {entry[0].lower() for entry in entries}
    while True:
        if ui.enabled():
            ui.screen(title, context=context, subtitle=subtitle)
            for row in intro:
                ui.line(row)
            if intro:
                ui.line()
        else:
            print(f"\n{title}")
            for row in intro:
                print(ui.plain(row))
        if ui.enabled() and draw is not None:
            draw(entries)
        else:
            ui.options([*entries, ("Q", "Back")])
        choice = input(ui.prompt("Choice: ")).strip().lower()
        if choice in ("", "q", "0"):
            return None
        if choice in keys:
            return choice
        ui.note("Choose an entry from the menu.", "warning")


def row(label, value, width=18):
    return f"{ui.paint(label.ljust(width), 'muted')}{ui.paint(value, 'text')}"


# --- camera -------------------------------------------------------------------

def camera_enabled(mind=MIND):
    return camera_allowed(Path(mind) / CAMERA_MARKER)


def set_camera(on):
    result = quiet([CAMERA_COMMAND, "enable" if on else "disable"], timeout=40)
    if result.returncode:
        raise ValueError((result.stderr or "").strip() or "The camera setting could not be changed.")
    return result.stdout.strip()


def camera_page(mind, temporary, *, enable_only=False):
    on = camera_enabled(mind)
    intro = [row("Camera", "On" if on else "Off"), "",
             "When the camera is on I look for a face, to greet you and to see when you are near.",
             "Only where a face is leaves my eyes: pictures are not saved or sent anywhere.",
             "The green light beside the camera is on whenever it is in use."]
    if temporary:
        intro.append("In Try this lasts until you restart.")
    entries = [("E", "Enable camera")] if enable_only and not on else []
    if not enable_only:
        entries = [("1", "Turn the camera off" if on else "Turn the camera on")]
    if not entries:
        done("The camera is already on.")
        return
    choice = menu("Camera", entries, intro=intro)
    if choice == "1":
        set_camera(not on)
        done("The camera is on. Watch for the green light." if not on else "The camera is off.")
    elif choice == "e":
        from .care_routes import guest_active

        # Voice-requested consent is deliberately rechecked at the commit point.
        if guest_active():
            done("I did not enable the camera while a guest is here.", "warning")
            return
        try:
            set_camera(True)
        except (OSError, ValueError, subprocess.SubprocessError):
            done("I couldn't confirm camera permission. Check Camera in Care.", "warning")
            return
        done("The camera is on. Watch for the green light.")


# --- dreaming -----------------------------------------------------------------

def dream_settings(mind=MIND):
    """(enabled, window). The consent file may hold the window as JSON."""
    window = dream_window(mind)
    return window is not None, window or "night"


def set_dreams(mind, enabled, window="night"):
    from .dream_policy import set_standing

    set_standing(mind, enabled, window)


def dream_local_now(mind):
    zone = zone_of(mind)
    stamp = time.time()
    if zone is None or clock_epoch.doubted(now=stamp, state_dir=mind):
        return None
    return datetime.fromtimestamp(stamp, zone)


def next_dream_permission(mind):
    from .dream_policy import ONCE_FILE, pending_once

    if not (Path(mind) / ONCE_FILE).is_file():
        return None
    now = dream_local_now(mind)
    return pending_once(mind, now) if now is not None else None


def dream_tonight_page(mind):
    """Keyboard-confirmed, one-night permission for the trusted local clock."""
    from .care_routes import guest_active
    from .dream_policy import allow_once, next_dream_night, pending_once
    from .retention import load_policy

    if load_policy(mind).mode != "persistent":
        done("I need conversation memory enabled to dream.", "warning")
        return
    local_now = dream_local_now(mind)
    if local_now is None:
        done("Set my date and time zone before choosing tonight's dream.", "warning")
        return
    night = next_dream_night(local_now)
    if dream_settings(mind)[0]:
        done("My usual dream permission already covers tonight.")
        return
    if pending_once(mind, local_now) is not None:
        done(f"I already have permission for {night:%d %B %Y}.")
        return
    zone = local_now.tzinfo
    intro = [f"{night:%A, %d %B %Y}, midnight to 5:00 am ({zone.key}).",
             "I'll need my lid closed, power connected and a safe temperature."]
    if menu("Dream tonight", [("Y", "Allow this night")], intro=intro) != "y":
        return
    if guest_active() or load_policy(mind).mode != "persistent":
        done("I couldn't save dream permission in this session.", "warning")
        return
    now_again = dream_local_now(mind)
    if now_again is None or now_again.tzinfo.key != zone.key or next_dream_night(now_again) != night:
        done("The time changed. Open tonight's permission again to check the date.", "warning")
        return
    try:
        allow_once(mind, now_again)
    except (OSError, ValueError):
        done("I couldn't save this night's permission. Try again from Care.", "warning")
        return
    done(f"I may dream on {night:%d %B %Y}, midnight to 5:00 am.")


def dreams_page(mind, temporary):
    enabled, window = dream_settings(mind)
    once = None if enabled else next_dream_permission(mind)
    state = DREAM_WINDOWS[window] if enabled else f"One night · {once:%d %b}" if once else "Off"
    intro = [row("Dreams", state), "",
             "While I dream I keep a journal linked to your own words. When my dream",
             "model is available, it proposes memories that I check against the source.",
             "Ask me what I dreamt about."]
    if temporary:
        intro.append("In Try the journal is gone when you restart.")
    entries = [("1", "Dream at night only", "lid closed, on power, midnight to 5 am"),
               ("2", "Dream whenever the lid is closed", "on power, any time")]
    if enabled or once is not None:
        entries.append(("3", "Stop dreaming"))
    choice = menu("Dreams", entries, intro=intro)
    if choice in ("1", "2"):
        set_dreams(mind, True, "night" if choice == "1" else "closed")
        done("I'll dream at night with the lid closed." if choice == "1"
             else "I'll dream whenever my lid is closed and I'm plugged in.")
    elif choice == "3":
        set_dreams(mind, False)
        done("I won't dream. The journal I already kept stays until you forget it.")


# --- memory mode --------------------------------------------------------------

def memory_page(mind, temporary):
    from .retention import MemoryPolicy, read_policy, save_policy

    try:
        policy = read_policy(mind)
    except (OSError, ValueError) as error:
        ui.note(f"The memory setting is damaged ({error}). Choose a mode to replace it.", "danger")
        policy = None
    label = MEMORY_MODES[policy.mode][0] if policy else "Damaged"
    if policy and policy.expiry_days:
        label += f", forgets after {policy.expiry_days} days"
    entries = [(str(number), MEMORY_MODES[mode][0], mode) for number, mode in enumerate(MEMORY_MODES, 1)]
    entries.append(("4", "Forget conversations after a number of days", "persistent memory only"))
    choice = menu("Memory", entries, intro=[row("Memory", label), "",
                                           "This decides what I keep from our conversations."])
    if choice is None:
        return
    if choice == "4":
        value = input(ui.prompt("Days to keep conversations (blank to keep them until you forget them): ")).strip()
        try:
            new = MemoryPolicy("persistent", int(value) if value else None)
        except ValueError as error:
            ui.note(str(error) if value.isdigit() else "Use a whole number of days.", "warning")
            return
        warning = (f"Conversations older than {new.expiry_days} days will be deleted for good, a little at a "
                   "time while I rest." if new.expiry_days else "I will keep conversations until you forget them.")
    else:
        mode = list(MEMORY_MODES)[int(choice) - 1]
        new = MemoryPolicy(mode)
        warning = MEMORY_MODES[mode][1]
    ui.para(warning, "plain")
    if not yes("Use this memory setting?"):
        return
    save_policy(mind, new)
    if new.mode != "persistent":
        from . import habit_suggestions

        try:
            habit_suggestions.erase_all(mind)
        except OSError:
            ui.note("The memory setting is saved. I couldn't clear the habit observations.", "warning")
    restart_mind("the memory setting")
    done("Memory setting saved for this Try session." if temporary else "Memory setting saved.")


# --- what it remembers ----------------------------------------------------------

def remembered(mind=MIND, limit=40, *, timeout=5):
    """The newest facts, as (id, text). Read-only: nothing is created."""
    database = Path(mind) / "mind.db"
    if not database.is_file():
        return []
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout)) as db:
        rows = db.execute("SELECT id, text FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return rows[::-1]


def forget_fact(mind, fact_id):
    """Forget one fact for good, with everything derived from it.

    The mind is stopped first: its live prompt may hold the fact. On restart it
    sees the archive's new revision and discards its saved context and cache.
    """
    from .memory import Memory

    units = installed(MIND_SERVICE)
    if units and quiet(["systemctl", "stop", *units], timeout=60).returncode:
        raise ValueError("The mind could not be paused, so nothing was forgotten.")
    try:
        with closing(Memory(Path(mind))) as memory:
            texts = memory.forget([fact_id])
        forget_reminders(mind, texts)
    finally:
        if units:
            quiet(["systemctl", "start", *units], timeout=60)
    return texts


def forget_reminders(mind, texts):
    """Forgetting reaches reminders too: none may keep the forgotten words."""
    from .reminders import Reminders

    if not texts or not (Path(mind) / "reminders.db").is_file():
        return 0
    with closing(Reminders(Path(mind))) as reminders:
        return reminders.forget_mentioning(texts)


def remember_page(mind, temporary):
    try:
        facts = remembered(mind)
    except sqlite3.Error as error:
        ui.note(f"I couldn't read my memory ({error}).", "danger")
        return
    if not facts:
        ui.note("I don't remember anything you asked me to yet. Say \"remember that ...\".")
        pause()
        return
    while True:
        if ui.enabled():
            ui.screen("What I remember", context="Care",
                      subtitle="Things you asked me to remember, newest last. Forget one by its number.")
        else:
            print("\nWhat I remember")
        for number, (_, text) in enumerate(facts, 1):
            line = f" {ui.paint(str(number).rjust(2), 'accent', 'bold')}  {installer_text(text)}"
            ui.line(line) if ui.enabled() else print(ui.plain(line))
        choice = input(ui.prompt("Number to forget [Enter to go back]: ")).strip()
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(facts):
            ui.note("Choose a number from the list.", "warning")
            continue
        fact_id, text = facts[int(choice) - 1]
        # The same confirmation the voice command uses, never a silent delete.
        if not yes(f'Should I forget "{installer_text(text)}"?'):
            ui.note("I've kept everything.")
            continue
        forget_fact(mind, fact_id)
        done("Forgotten." + ("" if temporary else " Older backups still hold it until you make a new one."))
        facts = remembered(mind)
        if not facts:
            return


def installer_text(value, limit=160):
    line = " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def journal_page(mind):
    from .dream_store import journal
    from .profile import FACTORY_CONFIG, load_profile

    database = Path(mind) / "mind.db"
    entries = []
    if database.is_file():
        owner = load_profile(FACTORY_CONFIG, Path(mind)).owner
        with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as db:
            entries = journal(db, owner, limit=12)
    if ui.enabled():
        ui.screen("Dream journal", context="Care", subtitle="Moments I kept while dreaming, with your own words.")
    else:
        print("\nDream journal")
    if not entries:
        ui.para("No dream journal entries yet.", "muted") if ui.enabled() else print("No dream journal entries yet.")
    zone = zone_of(mind)
    for entry in entries:
        heard = datetime.fromtimestamp(entry.source.ts, zone)
        head = f"{heard:%a %d %b %H:%M}"
        if ui.enabled():
            ui.line(f" {ui.paint(head, 'accent')}  {installer_text(entry.source.text, ui.width() - 20)}")
        else:
            print(f"  {head}  {installer_text(entry.source.text)}")
    pause()


# --- date, time and time zone ---------------------------------------------------

def zone_of(mind):
    from .skills import Skills

    try:
        skills = Skills(Path(mind))
        return skills.zone
    except Exception:  # an unreadable setting falls back to the device clock
        return None


def save_zone(mind, zone):
    from .skills import Skills

    skills = Skills(Path(mind))
    try:
        skills.set_zone(zone)
    finally:
        skills.close()


def choose_zone():
    """A ZoneInfo the owner picks, or None to go back."""
    from .skills import find_zone

    entries = [(str(number), key) for number, key in enumerate(COMMON_ZONES, 1)]
    if ui.enabled():
        ui.screen("Time zone", context="Care", subtitle="Pick one, or type a city or zone name such as Sydney.")
    else:
        print("\nTime zone")
    ui.options(entries)
    while True:
        value = input(ui.prompt("Number, city or zone [Enter to go back]: ")).strip()
        if not value:
            return None
        if value.isdigit() and 1 <= int(value) <= len(COMMON_ZONES):
            value = COMMON_ZONES[int(value) - 1]
        zone = find_zone(value)
        if zone is not None:
            return zone
        ui.note("I don't know that place. Try the nearest big city, or a name like Australia/Sydney.", "warning")


def zone_page(mind, temporary):
    zone = choose_zone()
    if zone is None:
        return
    now = datetime.now(zone)
    if not yes(f"Use {zone.key}? It is {now:%H:%M} there now.", default=True):
        return
    save_zone(mind, zone)
    restart_mind("the time zone")
    done(f"Time zone set to {zone.key}." + (" It lasts until you restart." if temporary else ""))


def parse_local_time(text, zone, *, today=None):
    """A wall time typed by the owner: 'YYYY-MM-DD HH:MM' or just 'HH:MM' for today."""
    text = " ".join(text.split())
    for pattern in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%H:%M"):
        try:
            value = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern == "%H:%M":
            day = today or datetime.now(zone).date()
            value = value.replace(year=day.year, month=day.month, day=day.day)
        moment = value.replace(tzinfo=zone)
        if not 2020 <= moment.year <= 2100:
            raise ValueError("Use a year from 2020 to 2100.")
        return moment.timestamp()
    raise ValueError("Type the date and time as 2026-11-23 18:30, or just 18:30 for today.")


def set_clock(mind, timestamp, *, settime=None, date_confirmed=False, write_rtc=False):
    """Set the wall clock and write the RTC only for a confirmed full date.

    Running timers move with the clock, so a ten-minute timer started before
    the change still rings ten minutes after it started (Reminders.clock_moved).
    """
    if not clock_epoch.CLOCK_FLOOR <= timestamp <= clock_epoch.MAX_CLOCK:
        raise ValueError("Enter a supported date on or after 1 January 2026.")
    with ExitStack() as cleanup:
        reminders = None
        if (Path(mind) / "reminders.db").is_file():
            from .reminders import Reminders

            reminders = Reminders(Path(mind))
            cleanup.callback(reminders.db.close)
            # Reserve the writer before changing CLOCK_REALTIME. If it is
            # busy, the local screen can retry without moving running timers.
            reminders.db.execute("BEGIN IMMEDIATE")
        before = time.time()
        trusted = date_confirmed or not clock_epoch.doubted(now=before, state_dir=mind)
        (settime or (lambda value: time.clock_settime(time.CLOCK_REALTIME, value)))(timestamp)
        if reminders is not None:
            reminders.clock_moved(timestamp - before, resolve_wall=trusted)
    if trusted:
        if settime is None:
            try:
                clock_epoch.confirm_this_boot(timestamp)
            except OSError:
                ui.note("I couldn't record the clock confirmation for this boot.", "warning")
        try:
            clock_epoch.remember(mind, timestamp, correct_date=True, wait=0.5)
        except OSError:
            ui.note("I couldn't save the trusted clock floor.", "warning")
    database = Path(mind) / "mind.db"
    if trusted and database.is_file():
        from .memory import Memory

        try:
            with closing(Memory(Path(mind))) as memory:
                # An owner-set time is what the memory should believe.
                accept = getattr(memory, "owner_set_clock", None)
                (accept or memory.clock)(timestamp)
        except sqlite3.Error:
            pass
    if write_rtc and date_confirmed:
        tool = shutil.which("hwclock")
        return tool is not None and quiet([tool, "-w", "-u"], timeout=20).returncode == 0
    return None


def clock_page(mind, temporary):
    zone = zone_of(mind)
    now = datetime.now(zone)
    where = zone.key if zone else "the device clock (no time zone set)"
    intro = [row("Now", f"{now:%A %d %B %Y, %H:%M}"), row("Time zone", where), "",
             "Set this if my clock is wrong, for example after my battery went flat."]
    choice = menu("Date and time", [("1", "Set the date and time"), ("2", "Choose the time zone")], intro=intro)
    if choice == "2":
        zone_page(mind, temporary)
        return
    if choice != "1":
        return
    if zone is None:
        ui.note("Choose the time zone first, so the time you type means what you think.", "warning")
        zone_page(mind, temporary)
        zone = zone_of(mind)
        if zone is None:
            return
    value = input(ui.prompt("New date and time (2026-11-23 18:30, or 18:30): ")).strip()
    if not value:
        return
    full_date = " " in " ".join(value.split())
    if not full_date and clock_epoch.doubted(state_dir=mind):
        ui.note("My date is uncertain. Enter the full date and time.", "warning")
        return
    timestamp = parse_local_time(value, zone)
    shown = datetime.fromtimestamp(timestamp, zone)
    write_rtc = full_date and not temporary
    clocks = "the system and battery clocks" if write_rtc else "the system clock"
    if not yes(f"Set {clocks} to {shown:%A %d %B %Y, %H:%M}?"):
        return
    try:
        rtc_saved = set_clock(mind, timestamp, date_confirmed=full_date, write_rtc=write_rtc)
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            done("My timers are busy. Try setting the clock again in a moment.", "warning")
            return
        raise
    done("System clock set. I couldn't update the battery clock." if rtc_saved is False else "Clock set.")


# --- network ------------------------------------------------------------------

def wifi_state():
    result = quiet([WIFI_COMMAND, "status"], timeout=20)
    match = re.search(r'Wi-Fi profile for "(.*)" on (\S+)', result.stdout or "")
    return (json.loads(f'"{match[1]}"'), match[2]) if match else (None, None)


def ethernet_on(mind):
    from .wifi import load_ethernet

    try:
        return load_ethernet(Path(mind))
    except (OSError, ValueError):
        return False


def network_mode(mind):
    from .network_policy import load_policy

    return load_policy(Path(mind)).effective_mode


def need_internet(mind):
    """Wi-Fi and Ethernet only carry traffic in Internet mode. Offer to switch."""
    from .network_policy import INTERNET, load_policy

    policy = load_policy(Path(mind))
    if policy.effective_mode == INTERNET:
        return True
    ui.para("A network link only comes up in Internet mode. I still look things up on the web "
            "only when you ask, and only if you allow it.", "plain")
    if not yes("Switch the network mode to Internet?"):
        return False
    command = [NETWORK_COMMAND, "set", INTERNET]
    if policy.permits_web_lookup:
        command.append("--web-lookups")
    if policy.time_server:
        command += ["--time-server", policy.time_server]
    if run(command, timeout=120, stdin=subprocess.DEVNULL).returncode:
        raise ValueError("The network mode could not be changed.")
    return True


def join_wifi(mind):
    result = quiet([WIFI_COMMAND, "scan"], timeout=40)
    if result.returncode:
        raise ValueError((result.stderr or "").strip().removeprefix("being-wifi: ")
                         or "The Wi-Fi radio could not scan.")
    found = json.loads(result.stdout)
    interface, networks = found["interface"], found["networks"][:12]
    entries = [(str(number), installer_text(network["ssid"], 40),
                f"{network['security'].upper()}, {network['signal']:.0f} dBm")
               for number, network in enumerate(networks, 1)]
    entries.append(("H", "A hidden network", "type its name"))
    choice = menu("Join Wi-Fi", entries, subtitle="Networks near me, strongest first.")
    if choice is None:
        return
    if choice == "h":
        ssid = input(ui.prompt("Network name: ")).strip()
        security = "wpa3" if yes("Does it use WPA3 only?") else "wpa2"
    else:
        network = networks[int(choice) - 1]
        ssid, security = network["ssid"], network["security"]
    if not ssid:
        return
    import getpass

    password = getpass.getpass(ui.prompt(f"Password for {installer_text(ssid, 40)} (hidden): "), stream=sys.stdout)
    if not password:
        return
    if not need_internet(mind):
        return
    saved = run([WIFI_COMMAND, "configure", interface, ssid, "--security", security, "--password-stdin"],
                timeout=60, input=password + "\n", capture_output=True, text=True)
    if saved.returncode:
        raise ValueError((saved.stderr or "").strip().removeprefix("being-wifi: ") or "Wi-Fi was not saved.")
    done(f"Joining {installer_text(ssid, 40)}.")


def network_page(mind, temporary):
    if temporary:
        ui.note("Network settings are for an installed being. Try stays offline.", "warning")
        return
    ssid, interface = wifi_state()
    wired = ethernet_on(mind)
    intro = [row("Network mode", _NETWORK_MODE_LABELS.get(network_mode(mind), "Offline")),
             row("Wi-Fi", f"{installer_text(ssid, 40)} on {interface}" if ssid else "Not set"),
             row("Wired Ethernet", "On" if wired else "Off")]
    entries = [("1", "Join a Wi-Fi network"), ("2", "Turn wired Ethernet off" if wired else "Turn wired Ethernet on",
                                               "plug in a cable, no pairing")]
    if ssid:
        entries.append(("3", "Forget this Wi-Fi network"))
    choice = menu("Network", entries, intro=intro)
    if choice == "1":
        join_wifi(mind)
    elif choice == "2":
        if not wired and not need_internet(mind):
            return
        result = quiet([WIFI_COMMAND, "ethernet", "off" if wired else "on"], timeout=120)
        if result.returncode:
            raise ValueError("Wired Ethernet could not be changed.")
        done("Wired Ethernet is off." if wired else "Wired Ethernet is on. Plug in a cable.")
    elif choice == "3" and ssid:
        if yes(f"Forget {installer_text(ssid, 40)} and its password?"):
            if quiet([WIFI_COMMAND, "forget"], timeout=40).returncode:
                raise ValueError("The Wi-Fi network could not be forgotten.")
            done("Wi-Fi network forgotten.")


# --- the workshop switch (docs/design/voice-apps.md, Decisions 1) ---------------

def workshop_enabled(mind=MIND):
    """On by default, on Try and installed beings alike, until switched off here."""
    return workshop_allowed(Path(mind) / APPS_DISABLED)


def set_workshop(mind, on):
    mind = Path(mind)
    if on:
        private_marker(mind / APPS_ENABLED)
        remove_marker(mind / APPS_DISABLED)
    else:
        private_marker(mind / APPS_DISABLED)
        remove_marker(mind / APPS_ENABLED)


def workshop_page(mind, temporary):
    on = workshop_enabled(mind)
    intro = [row("Workshop", "On" if on else "Off"), "",
             "The workshop lets you ask me to open a terminal, write small scripts and run them.",
             "It is a caged space in memory that is cleared when I restart. Nothing in it can",
             "reach my memories or the system, and a script only runs when you say \"run it\"."]
    choice = menu("Workshop", [("1", "Turn the workshop off" if on else "Turn the workshop on")], intro=intro)
    if choice == "1":
        set_workshop(mind, not on)
        done("The workshop is off." if on else "The workshop is on. Say \"open the terminal\".")


# --- volume and microphone (task 1.17) --------------------------------------------

def sound_card():
    """The internal card by name when it is there (plughw:CARD=PCH), else the default."""
    card = os.environ.get("BEING_ALSA_CARD")
    if card:
        return card
    return "PCH" if Path("/proc/asound/PCH").exists() else None


def amixer(*args):
    card = sound_card()
    return quiet(["amixer", *(("-c", card) if card else ()), *args], timeout=10)


def controls():
    result = amixer("scontrols")
    if result.returncode == 127:
        return None
    return re.findall(r"Simple mixer control '([^']+)',\d+", result.stdout or "")


def level(name):
    """(percent, on) of a mixer control, or None."""
    result = amixer("sget", name)
    if result.returncode:
        return None
    match = re.search(r"\[(\d{1,3})%\](?:.*?\[(on|off)\])?", result.stdout or "")
    if not match:
        return None
    return int(match[1]), match[2] != "off"


def playback_control(names):
    return next((name for name in ("Master", "PCM", "Speaker", "Headphone") if name in names), None)


def capture_control(names):
    return next((name for name in ("Capture", "Internal Mic Boost", "Mic Boost", "Mic") if name in names), None)


def set_level(name, percent):
    percent = max(0, min(100, int(percent)))
    result = amixer("sset", name, f"{percent}%", "unmute")
    if result.returncode:
        amixer("sset", name, f"{percent}%")  # some controls have no switch
    return percent


def store_levels(mind):
    """Keep the levels across restarts, on the mind partition."""
    if not shutil.which("alsactl"):
        return False
    target = Path(mind) / AUDIO_STATE
    target.parent.mkdir(mode=0o700, exist_ok=True)
    return quiet(["alsactl", "-f", str(target), "store"], timeout=10).returncode == 0


def tone_file(path, *, seconds=TONE_SECONDS, hz=TONE_HZ, volume=0.4):
    """A soft two-note chime, written with the standard library."""
    frames = array.array("h")
    total = int(SAMPLE_RATE * seconds)
    for index in range(total):
        t = index / SAMPLE_RATE
        note = hz if t < seconds / 2 else hz * 1.25
        envelope = min(1.0, index / 400, (total - index) / 1600)
        frames.append(int(32767 * volume * envelope * math.sin(2 * math.pi * note * t)))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(frames.tobytes())
    return path


def devices():
    from .voice import audio_devices

    return audio_devices()


def play_tone():
    with tempfile.TemporaryDirectory(prefix="being-tone-") as directory:
        path = tone_file(Path(directory) / "tone.wav")
        result = quiet(["aplay", "-q", "-D", devices()[1], str(path)], timeout=10)
    if result.returncode:
        raise ValueError("The test sound could not play. The speakers may be busy or muted.")


def loudness(chunk):
    """Peak level of 16-bit little-endian samples, 0 to 1."""
    samples = array.array("h")
    samples.frombytes(chunk[:len(chunk) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    return max((abs(value) for value in samples), default=0) / 32768


def decibels(fraction):
    return -90.0 if fraction <= 0 else max(-90.0, 20 * math.log10(fraction))


def mic_meter(seconds=METER_SECONDS, *, popen=subprocess.Popen, draw=None):
    """Show the microphone level live for a few seconds. Returns the loudest peak."""
    command = ["arecord", "-q", "-D", devices()[0], "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
               "-t", "raw", "-d", str(int(math.ceil(seconds)))]
    try:
        process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError:
        raise ValueError("The microphone could not be opened. arecord is not on this image.") from None
    loudest = 0.0
    chunk_bytes = SAMPLE_RATE // 10 * 2
    try:
        while True:
            chunk = process.stdout.read(chunk_bytes)
            if not chunk:
                break
            peak = loudness(chunk)
            loudest = max(loudest, peak)
            (draw or draw_level)(peak)
    finally:
        process.stdout.close()
        code = process.wait()
    if code and loudest == 0:
        raise ValueError("The microphone is in use while I listen. Press M to mute me, then try again.")
    return loudest


def draw_level(peak):
    fraction = (decibels(peak) + 60) / 60
    text = f"{ui.bar(fraction, 32)} {decibels(peak):6.1f} dB"
    sys.stdout.write("\r  " + text)
    sys.stdout.flush()


def audio_page(mind, temporary):
    while True:
        names = controls()
        if names is None:
            ui.note("This image has no mixer tool (amixer), so I can't read or set my levels here.", "warning")
            pause()
            return
        playback, capture = playback_control(names), capture_control(names)
        speaker = level(playback) if playback else None
        microphone = level(capture) if capture else None
        intro = [row("Speaker volume", f"{speaker[0]}%{'' if speaker[1] else ' (muted)'}" if speaker else
                     "No volume control found"),
                 row("Microphone gain", f"{microphone[0]}%" if microphone else "No microphone control found")]
        if not names:
            intro += ["", "The sound card shows no controls. Its driver may not have loaded."]
        entries = []
        if speaker:
            entries += [("+", "Louder"), ("-", "Quieter"), ("V", "Set the volume", "0 to 100")]
        entries.append(("T", "Play a test sound"))
        if microphone:
            entries += [("G", "Set the microphone gain", "0 to 100")]
        entries.append(("M", "Show the microphone level", f"{METER_SECONDS:g} seconds"))
        choice = menu("Volume and microphone", entries, intro=intro)
        if choice is None:
            return
        try:
            if choice in ("+", "-") and speaker:
                set_level(playback, speaker[0] + (10 if choice == "+" else -10))
                store_levels(mind)
                play_tone()
            elif choice == "v" and speaker:
                value = input(ui.prompt("Volume (0 to 100): ")).strip()
                if value.isdigit():
                    set_level(playback, int(value))
                    store_levels(mind)
                    play_tone()
            elif choice == "g" and microphone:
                value = input(ui.prompt("Microphone gain (0 to 100): ")).strip()
                if value.isdigit():
                    set_level(capture, int(value))
                    store_levels(mind)
            elif choice == "t":
                play_tone()
            elif choice == "m":
                ui.para("Speak normally from where you usually sit.", "plain")
                loudest = mic_meter()
                print()
                verdict = ("That is too quiet to hear you well. Raise the gain." if decibels(loudest) < -35 else
                           "That is close to clipping. Lower the gain a little." if decibels(loudest) > -3 else
                           "That level is good.")
                ui.note(f"Loudest {decibels(loudest):.0f} dB. {verdict}")
                pause()
        except ValueError as error:
            ui.note(str(error), "warning")


# --- USB sticks: backups and updates ---------------------------------------------

def pick_stick(title, subtitle):
    """Mount nothing: just choose a USB partition. Returns its device or None."""
    from . import installer, local_menu

    report, _ = installer.scan()
    media = local_menu.backup_media(report)
    if not media:
        ui.note("Plug in a USB stick, then try again.", "warning")
        return None
    entries = []
    for number, (device, label) in enumerate(media, 1):
        _, _, rest = label.partition(" | ")
        entries.append((str(number), rest.replace(" | ", ui.dot()) or device, device))
    choice = menu(title, entries, subtitle=subtitle)
    return media[int(choice) - 1][0] if choice else None


def recipient_on_file(mind):
    from .backup_crypto import RECIPIENT

    path = Path(mind) / RECIPIENT_FILE
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if RECIPIENT.fullmatch(value) else None


def backup_key(mind, folder, temporary, *, device=None, expected_uuid=None):
    """The public recipient to encrypt to, making a key on first use."""
    from .backup_crypto import generate_key, generate_protected_key

    recipient = recipient_on_file(mind)
    if recipient:
        return recipient
    ui.para("Your backups are locked with a key that only you keep. I'll make one now and save it on "
            "this stick. Move it to a different stick, or somewhere safe, after this: anyone who has "
            "the key and a backup can read the backup.", "plain")
    protect = yes("Protect the key with a passphrase?", default=True)
    name = KEY_NAME if protect else Path(KEY_NAME).with_suffix(".key").name
    path = folder / name if folder is not None else None
    if path is not None and (path.exists() or path.is_symlink()):
        raise ValueError(f"A key file is already on this stick ({path.name}). Keep it, or move it away to make "
                         "a new one.")
    from .backup import working_directory

    # FAT sticks have no hard links, so the key is made in RAM and copied once.
    with working_directory() as directory:
        staged = Path(directory) / name
        if protect:
            ui.para("Type the passphrase twice when asked. It is not shown.", "muted")
            recipient = generate_protected_key(staged)
            # Left empty, age makes up a passphrase and prints it once. The
            # next screen would hide it, so wait until it is written down.
            ui.para("If you left it empty, age showed a made-up passphrase above. Write it down now: "
                    "without it the key cannot open your backups.", "plain")
            input(ui.prompt("Press Enter when you are ready. "))
        else:
            recipient = generate_key(staged)
        if device is not None:
            from . import backup

            backup._run_device_backup(backup._device_backup_command(
                device, expected_uuid, source=staged, name=name
            ))
        else:
            copy_new(staged, path)
    # Only the public half stays with the being, for later backups.
    atomic_write(Path(mind) / RECIPIENT_FILE, recipient + "\n", mode=0o600)
    return recipient


def copy_new(source, destination):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output, open(source, "rb") as data:
        shutil.copyfileobj(data, output)
        output.flush()
        os.fsync(output.fileno())
    sync_directory(destination.parent)


def backup_menu(mind=MIND, *, temporary=False):
    """Back up the being to a USB stick: snapshot, deletion proof and checkpoint record."""
    from .backup_crypto import BackupCryptoError

    try:
        return write_backup(mind, temporary=temporary)
    except (BackupCryptoError, RuntimeError, sqlite3.Error) as error:
        # Every failure reaches the owner as a sentence, never as a crash of the menu.
        raise ValueError(f"The backup was not made: {error}") from None


def write_backup(mind, *, temporary):
    from . import backup, local_menu

    if not (shutil.which("age") and shutil.which("age-keygen")):
        ui.note("Backups need the age tool, which this image does not have.", "warning")
        return
    device = pick_stick("Back up to a USB stick",
                        "Choose the stick to write the backup to. Only a new folder on it is written.")
    if device is None:
        return
    identity = backup.media_identity(device)
    if identity is None:
        ui.note("I can't give that stick a stable identity, so I won't enroll it for backups.", "warning")
        return
    enrolled = backup.media_binding(mind)
    enroll = enrolled != identity
    if enroll:
        question = ("Use this as your enrolled backup stick?"
                    if enrolled is None else
                    "This is a different stick. Replace the enrolled backup stick with this one?")
        if not yes(question, default=False):
            ui.note("I left the enrolled backup stick unchanged.", "plain")
            return
    # One backup at a time: the mind's voice and night backups take this lock too.
    try:
        with backup.backup_lock():
            result, key_on_stick = write_locked_backup(mind, device, temporary, expected=identity)
            # Enrollment changes only with a completed backup. Confirmation
            # alone must not redirect a concurrent voice/night backup, and a
            # failed manual backup must leave the prior destination intact.
            if enroll:
                backup.save_media_binding(mind, identity)
            # Record only after both the writable mount is closed and any
            # confirmed enrollment change is durable.
            backup.record_backup(Path(mind), "menu")
    except backup.Unavailable:
        ui.note("I'm already backing myself up. Try again in a minute.", "warning")
        return
    show_backup(result, key_on_stick)


def write_locked_backup(mind, device, temporary, *, expected=None):
    from . import backup, local_menu

    mounted = False
    try:
        expected = expected or backup.media_binding(mind)
        if expected is None or backup.media_identity(device) != expected:
            raise ValueError("The selected stick is no longer the enrolled backup destination.")
        if (Path("/etc/being/env").is_file() and shutil.which("systemd-run")
                and Path("/usr/sbin/being-backup").is_file()):
            had_recipient = recipient_on_file(mind) is not None
            recipient = backup_key(mind, None, temporary, device=device, expected_uuid=expected)
            view = ui.StageProgress("Backing up", (("Saving.", "Saving the being and its archive", 3),
                                                   ("Proving.", "Recording what was forgotten", 1)),
                                    context="Backup", footer="Keep the stick in until this finishes.")
            with view:
                view.stage("Saving.")
                result = backup.backup_to_device(Path(mind), device, expected, recipient, prune_after=False)
                view.stage("Proving.")
            return result, not had_recipient
        root = local_menu.mount_media(device, writable=True)
        mounted = True
        if backup.media_identity(device) != expected or not backup.mounted_from(root, device):
            raise ValueError("The backup stick changed while it was being opened.")
        folder = root / BACKUP_FOLDER
        folder.mkdir(mode=0o700, exist_ok=True)
        recipient = backup_key(mind, folder, temporary)
        view = ui.StageProgress("Backing up", (("Saving.", "Saving the being and its archive", 3),
                                               ("Proving.", "Recording what was forgotten", 1)),
                                context="Backup", footer="Keep the stick in until this finishes.")
        with view:
            view.stage("Saving.")
            result = backup.backup_to_folder(Path(mind), folder, recipient,
                                             device=device, expected_uuid=expected)
            view.stage("Proving.")
        key_on_stick = any((folder / name).exists() for name in (KEY_NAME, Path(KEY_NAME).with_suffix(".key").name))
        os.sync()
    finally:
        # Do not unmount a restore/installer view merely because the selected
        # device failed its pre-mount identity check. Once mount_media succeeds
        # this workflow owns the shared menu mount and must always release it.
        if mounted:
            local_menu.unmount_media()
            if os.path.ismount(root):
                raise ValueError("The backup was written, but the stick could not be unmounted safely.")
    return result, key_on_stick


def show_backup(result, key_on_stick):
    from . import backup

    short = backup.fingerprint(result["checkpoint"])
    rows = [ui.mark("success", f"Saved {result['snapshot'].name} in {BACKUP_FOLDER}."),
            ui.mark("accent", f"Checkpoint {short}"),
            "Write the checkpoint down, or photograph this screen. A restore asks you to confirm it."]
    if key_on_stick:
        rows.append(ui.mark("warning", "The key is on the same stick. Move it to another stick."))
    if ui.enabled():
        ui.screen("Backup complete", context="Backup")
        ui.card(rows)
    else:
        print("\n".join(ui.plain(text) for text in rows))
    pause()


def bundle_info(path):
    """Read the verified verity manifest and require the installed channel."""
    config = configparser.ConfigParser(interpolation=None)
    try:
        with RAUC_INSTALLED_CONF.open(encoding="utf-8") as file:
            config.read_file(file)
        compatible = config["system"]["compatible"]
        if not compatible:
            raise ValueError("empty compatible string")
    except (OSError, ValueError, KeyError, configparser.Error):
        raise ValueError("PipOS could not verify its installed update configuration.") from None
    command = ["rauc", f"--conf={RAUC_INSTALLED_CONF}", "info", "--output-format=json", str(path)]
    result = quiet(command, timeout=120)
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        raise ValueError("That update is not signed for this being, or it is damaged."
                         + (f" ({detail[-1][:160]})" if detail else ""))
    try:
        info = json.loads(result.stdout)
    except (TypeError, ValueError):
        info = None
    if not isinstance(info, dict) or info.get("format") != "verity":
        raise ValueError("PipOS could not verify this update's signed manifest.")
    digest = info.get("hash")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("PipOS could not verify this update's signed manifest.")
    if info.get("compatible") != compatible:
        raise ValueError("This signed update is for a different PipOS system.")
    version, build = info.get("version"), info.get("build")
    return {"compatible": compatible,
            "version": installer_text(version, 60) if isinstance(version, str) and version else "unknown",
            "build": installer_text(build, 40) if isinstance(build, str) else "",
            "hash": digest}


def bundles(root):
    from . import local_menu

    return [path for path in local_menu.media_files(root) if path.name.endswith(UPDATE_SUFFIX)][:UPDATE_LIMIT]


def current_version():
    try:
        return BUILD_ID.read_text(encoding="utf-8").strip().splitlines()[0][:80]
    except (OSError, IndexError, UnicodeError):
        return "unknown"


class UpdateWaitInterrupted(ValueError):
    """The RAUC client stopped waiting before the service's result was known."""


def update_service_idle():
    # RAUC's GetSlotStatus method refuses while the install worker is busy.
    return quiet(["rauc", f"--conf={RAUC_INSTALLED_CONF}", "status", "--output-format=json"],
                 timeout=10).returncode == 0


def install_bundle(path, manifest_hash):
    """Have RAUC reverify the exact manifest presented for confirmation."""
    command = ["rauc", f"--conf={RAUC_INSTALLED_CONF}", "install",
               f"--require-manifest-hash={manifest_hash}", str(path)]
    try:
        result = subprocess.run(command, timeout=1800, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, check=False)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        # The D-Bus client can stop while the service is still writing the slot.
        result = None
    if result is None or (result.returncode and not update_service_idle()):
        raise UpdateWaitInterrupted("PipOS could not confirm that the update finished. Leave the USB stick plugged in. "
                                    "Open Update again to check whether the installer is still busy.")
    if result.returncode:
        lines = [line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
        raise ValueError("The update did not install. The running system is unchanged."
                         + (f" ({lines[-1][:200]})" if lines else ""))


def _offer_update(bundle, mind):
    from . import action_log, backup

    info = bundle_info(bundle)
    rows = [row("Running now", current_version()), row("Update", info["version"]),
            row("Made for", info["compatible"])]
    if info["build"]:
        rows.append(row("Build", info["build"]))
    rows += ["", "My memory stays as it is. The update goes into my spare system copy. If it does not",
             "start properly, I go back to this one on my own."]
    if os.path.lexists(Path(mind) / backup.RECIPIENT_FILE):
        rows.append("I will save an encrypted recovery checkpoint on this disk before installing.")
    else:
        rows.append("No backup key is configured. This update will run without a recovery checkpoint.")
    if ui.enabled():
        ui.screen("Ready to update", context="Update")
        ui.card(rows)
    else:
        print("\n".join(ui.plain(text) for text in rows))
    if not yes("Install this update?"):
        return False
    view = ui.StageProgress("Updating", (("Installing.", "Installing into the spare system copy", 1),),
                            context="Update", footer="Keep the Mac plugged in. This takes a few minutes.")
    with view:
        view.stage("Installing.")
        install_bundle(bundle, info["hash"])
    action_log.record(Path(mind), "update.install")
    return True


def _usb_update(mind):
    from . import local_menu

    device = pick_stick("Update from a USB stick",
                        "Choose the stick that holds the update. It is only read, never changed.")
    if device is None:
        return False
    with local_menu.update_media(device) as root:
        found = bundles(root)
        if not found:
            ui.note("That stick holds no PipOS update (a .raucb file).", "warning")
            return False
        entries = [(str(number), installer_text(str(path.relative_to(root)), 50)) for number, path in
                   enumerate(found, 1)]
        choice = menu("Choose the update", entries, intro=[row("Running now", current_version())])
        return choice is not None and _offer_update(found[int(choice) - 1], mind)


def _download_update(mind, release):
    from . import update_download

    title = "Download approved update"
    ui.screen(title, context="Update")
    ui.line(f"{ui.size(release.bytes)} from {installer_text(release.host, 64)}")
    if not yes("Download this update?"):
        return False
    progress = ui.StageProgress(title, (("Downloading.", "Downloading and checking the release", 1),),
                                context="Update", footer="Press Q then Enter to stop the download.")

    def stop_requested():
        if not sys.stdin.isatty() or not select.select([sys.stdin], [], [], 0)[0]:
            return False
        return sys.stdin.readline().strip().casefold() in ("q", "quit")

    with ExitStack() as resources:
        with progress:
            progress.stage("Downloading.")
            bundle = resources.enter_context(update_download.staged_bundle(
                mind, release, progress=progress.advance, install_idle=update_service_idle, cancel=stop_requested))
        try:
            return _offer_update(bundle, mind)
        except UpdateWaitInterrupted:
            raise UpdateWaitInterrupted("PipOS could not confirm that the update finished. "
                                        "Open Update again to check whether the installer is still busy.") from None


def update_menu(mind=MIND, *, try_boot=None):
    """Review, verify and install a release through the installed RAUC service."""
    from . import local_menu, update_download
    from .try_mode import is_try_boot

    if (is_try_boot() if try_boot is None else try_boot):
        ui.note("Updates are for an installed being. Install PipOS first.", "warning")
        return
    if not shutil.which("rauc"):
        ui.note("This image cannot install updates (no rauc).", "warning")
        return
    local_menu.release_previous_update_media()
    update_download.release_previous(mind, install_idle=update_service_idle)
    release = None
    if update_download.internet_enabled(mind):
        try:
            release = update_download.approved_release(Path(mind) / update_download.LOCAL_SOURCE)
            if release is None:
                release = update_download.approved_release()
        except (OSError, ValueError, RecursionError):
            ui.note("The approved download source could not be read. Choose a signed USB update.", "warning")
    choice = "1"
    if release is not None:
        choice = menu("System update", [("1", "From a USB stick"), ("2", "Download approved update")],
                      intro=[row("Running now", current_version())])
        if choice is None:
            return
    applied = _download_update(mind, release) if choice == "2" else _usb_update(mind)
    if not applied:
        return
    done("The update is installed. I'll start the new system on the next restart.")
    if yes("Restart now to start the new system?", default=True):
        quiet(["systemctl", "reboot"], timeout=30)


def previous_system_page(_mind, temporary):
    """The local owner's typed choice to try the last confirmed system."""
    from .update_slots import rollback_plan, schedule_rollback

    if temporary:
        return
    plan = rollback_plan()
    rows = [row("Running now", f"{current_version()} · slot {plan.current}"),
            row("Previous confirmed system", f"slot {plan.previous}"), "",
            "The Mac will restart and try that system once. My archive stays on this disk.",
            "If the previous system cannot confirm its health, PipOS returns to this slot."]
    if ui.enabled():
        ui.screen("Restart into the previous system", context="Update")
        ui.card(rows)
    else:
        print("\n".join(ui.plain(text) for text in rows))
    challenge = f"RESTART {plan.previous}"
    if input(ui.prompt(f"Type {challenge} to restart, or Enter to go back: ")) != challenge:
        return
    schedule_rollback(plan)
    result = quiet(["systemctl", "--no-block", "reboot"], timeout=30)
    if result.returncode:
        done("The previous system is scheduled for the next restart. Automatic restart could not start.", "warning")


def endurance_page(mind, temporary):
    """Owner-controlled native endurance run; never cuts power itself."""
    if temporary:
        done("Endurance runs are available only on the installed system.", "warning")
        return
    from .care_routes import guest_active
    from .retention import read_policy
    from . import native_soak
    if guest_active() or read_policy(mind).mode != "persistent":
        done("I did not open an endurance run while guest or temporary memory is active.", "warning")
        return
    while True:
        current = native_soak.current(mind)
        last = native_soak.last(mind)
        entries = [("1", "Start 24-hour run"), ("2", "Start 48-hour run"), ("3", "Start 1-week run")]
        if current is not None:
            entries = [("s", "Stop the current run"), ("p", "Arm the planned power cut"),
                       ("c", "Authorize a deliberate organ recovery")]
        else:
            entries.append(("l", "Last run evidence"))
        intro = ["Runs use native hardware and write evidence only under /mind/log/soak.",
                 "Pip never cuts power from this screen; the physical action is separate."]
        if current is not None:
            intro += [f"Running: {current['hours']} h · {current['observed_seconds']} s · {current['run']}",
                      f"Armed for planned cut: {'yes' if current['armed'] else 'no'}",
                      f"Pending organ recovery: {current.get('pending_organ') or 'none'}"]
        elif last is not None:
            intro += [f"Last: {last['reason']} · {last['hours']} h · {last['observed_seconds']} s"]
        choice = menu("Endurance", entries, intro=intro)
        if choice is None:
            return
        if choice in {"1", "2", "3"}:
            native_soak.start(mind, {"1": 24, "2": 48, "3": 168}[choice])
            done("The endurance run has started. Evidence is saved under /mind/log/soak.")
        elif choice == "s":
            native_soak.stop(mind)
            done("The endurance run has stopped.")
        elif choice == "p":
            if yes("Mark one physical interruption in the next 15 minutes? Pip will not cut power.", default=False):
                native_soak.arm_power_cut(mind)
                done("One physical interruption is marked for the next 15 minutes. Perform it yourself; Pip will not cut power here.")
        elif choice == "c":
            name = input(ui.prompt("Type CRASH brainstem, face, voice, mind or cortex: ")).strip().lower()
            if not name.startswith("crash ") or name[6:] not in {"brainstem", "face", "voice", "mind", "cortex"}:
                done("No organ was authorized.", "warning")
                continue
            organ = name[6:]
            if not yes(f"Deliberately stop {organ}; it should restart after you leave Care?", default=False):
                continue
            native_soak.arm_organ(mind, organ)
            done(f"{organ} recovery is authorized for the next run checkpoint. Leave Care before the deliberate stop.")
        elif choice == "l":
            done("The last run's evidence is under /mind/log/soak.")


# --- the page ---------------------------------------------------------------------

_CARE_SECTIONS = (
    ("Privacy and memory", ("1", "9", "3", "4", "A", "C", "P")),
    ("Sleep and time", ("2", "5", "6", "7")),
    ("System", ("8", "L", "H", "R", "D", "E")),
)
_CARE_STATE_KEYS = {"1", "2", "3", "8", "9", "C", "D"}


def summary_rows(mind, temporary):
    enabled, window = dream_settings(mind)
    dream_state = ("Night" if window == "night" else "Lid closed") if enabled else (
        "Tonight" if next_dream_permission(mind) is not None else "Off")
    zone = zone_of(mind)
    from .retention import read_policy

    try:
        memory = MEMORY_MODES[read_policy(mind).mode][0]
    except (OSError, ValueError, RecursionError):
        memory = "Damaged: choose a mode"  # the mind treats it as keeping nothing new
    from .continuity import curiosity_enabled

    rows = [("1", "Camera", "On" if camera_enabled(mind) else "Off"),
            ("2", "Dreams", dream_state),
            ("3", "Memory", memory),
            ("4", "What I remember", "Read or forget"),
            ("A", "Habits", "Triggers and steps"),
            ("C", "Curiosity", "On" if curiosity_enabled(mind) else "Off"),
            ("5", "Dream journal", "What I kept while dreaming"),
            ("6", "Date and time", f"{datetime.now(zone):%a %H:%M}"),
            ("7", "Time zone", zone.key if zone else "Not set"),
            ("8", "Workshop", "On" if workshop_enabled(mind) else "Off"),
            ("P", "Permissions", "What Pip may use"),
            ("L", "Offline library", "Sources and licences"),
            ("H", "Health", "A check-up of my parts")]
    if not temporary:
        rows.append(("9", "Network", "Wi-Fi and wired Ethernet"))
        rows.append(("R", "Previous system", "Try the last confirmed slot"))
        rows.append(("E", "Endurance", "Native soak and power-cut evidence"))
    else:
        from . import usb_logs

        logged = usb_logs.logging_enabled()
        if logged is not None:
            rows.append(("D", "Boot diagnostics", "Saved on this stick" if logged else "Off"))
    return rows


def curiosity_page(mind=MIND, temporary=False):
    """Explicit owner control for the factory-off daily conversation question."""
    from .continuity import curiosity_enabled, set_curiosity

    enabled = curiosity_enabled(mind)
    intro = [row("Curiosity", "On" if enabled else "Off"), "",
             "When this is on, Pip may ask one gentle question after a conversation you start,",
             "at most once on a trusted local day. Your answer stays in memory only long enough",
             "to ask ‘Shall I remember that?’ It is saved only if you then say yes."]
    verb = "Turn off" if enabled else "Turn on"
    if menu("Curiosity", [("1", verb)], intro=intro) != "1":
        return
    if not yes(f"{verb} Curiosity?"):
        return
    set_curiosity(mind, not enabled)
    done(f"Curiosity is {'off' if enabled else 'on'}.")


def permissions_page(mind=MIND, temporary=False):
    """Show the consolidated owner capability policy without changing it."""
    intro = ["The lid, guest mode and quiet hours can limit use."]
    if temporary:
        intro.append("In Try, changes last until you restart.")
    rows = load_capabilities(mind, temporary=temporary, local_now=dream_local_now(mind)).rows()
    intro.extend(row(label, status, width=24) for label, status in rows)
    menu("Permissions", [], intro=intro, context="Care")


def _care_entry(key, title, value, width):
    cap = ui.paint(f" {key} ", "bright", "bold", "44")
    label = f"{cap}  {ui.paint(title, 'text')}"
    available = width - ui.visible_len(label) - 3
    if available < 3:
        return label
    if len(value) > available:
        value = value[:available - 1].rstrip() + "…"
    if key in _CARE_STATE_KEYS:
        if value.startswith("Damaged"):
            state = ("warning", "43")
        elif value in ("On", "Night", "Lid closed", "Tonight", "Saved on this stick"):
            state = ("success", "42")
        elif value in ("Off", "Offline"):
            state = ("muted", "40")
        else:
            state = ("accent", "40")
        detail = ui.paint(f" {value} ", *state)
    else:
        detail = f" {ui.paint(value, 'muted')} "
    return ui.pad(label, width - ui.visible_len(detail)) + detail


def _draw_care_summary(entries, mind):
    facts = {key: (title, value) for key, title, value in entries}
    if "9" in facts:
        title, _ = facts["9"]
        facts["9"] = (title, _NETWORK_MODE_LABELS.get(network_mode(mind), "Offline"))
    if ui.room() < len(entries) + len(_CARE_SECTIONS) + 1:
        compact = [(key, title) for key, title, _ in entries] + [("Q", "Back")]
        if len(compact) <= ui.room():
            ui.options(compact)
        else:
            column = (ui.width() - 2) // 2
            half = (len(compact) + 1) // 2

            def cell(key, title):
                if len(title) > column - 4:
                    title = title[:column - 5].rstrip() + "…"
                return ui.pad(ui.option(key, title), column)

            for index, entry in enumerate(compact[:half]):
                right = index + half
                ui.line(cell(*entry) + ("  " + cell(*compact[right]) if right < len(compact) else ""))
        return
    boxed = ui.room() >= len(entries) + 2 * len(_CARE_SECTIONS) + 1
    width = ui.width() - 4 if boxed else ui.width()
    for title, keys in _CARE_SECTIONS:
        rows = [_care_entry(key, *facts[key], width) for key in keys if key in facts]
        if boxed:
            ui.card(rows, title=ui.paint(title, "accent", "bold"), tone="94", inner=width)
        else:
            ui.line(ui.paint(title, "accent", "bold"))
            for row in rows:
                ui.line(row)
    ui.line(ui.paint(" Enter / Q ", "bright", "44") + ui.paint("  Back", "muted"))


HEALTH_WAIT = 2.0          # seconds the Health page waits for the running being's readings
HEALTH_TOPICS = ("body.state", "body.lid", "voice.level")


async def _listen_for_facts(path, wait):
    from .bus import Bus

    bus = Bus(f"care-health-{os.getpid()}", HEALTH_TOPICS, path=path)
    body, voice = {}, {}
    await bus.start()
    try:
        async with asyncio.timeout(wait):
            # body.state is retained, so the brainstem sends it the moment we
            # join; a voice.level comes only if the voice happens to send one.
            while "battery" not in body:
                event = await bus.recv()
                fields = {key: value for key, value in event.items() if key != "t"}
                if event["t"] == "body.state":
                    body.update(fields)
                elif event["t"] == "body.lid" and isinstance(event.get("closed"), bool):
                    body["lid_closed"] = event["closed"]
                elif event["t"] == "voice.level":
                    voice = fields
    except TimeoutError:
        pass
    finally:
        await bus.stop()
    return body, voice


def live_facts(wait=HEALTH_WAIT, path=None):
    """(body, voice) from the running being, for the Health page: the
    brainstem's retained body.state (battery, temperature) and the lid, and
    the voice's last voice.level if one arrives within `wait` seconds. Only
    the mind may ask the voice for a level (voice.level_request), so this
    never asks. ({}, {}) when no being is running or nothing came in time;
    never raises."""
    path = path or os.environ.get("BEING_BUS", "/run/being/bus.sock")
    if not Path(path).exists():
        return {}, {}
    try:
        return asyncio.run(_listen_for_facts(path, wait))
    except Exception:
        return {}, {}


def health_page(mind, temporary, *, system=None, facts=None):
    """The same check-up as "are you okay?", as rows. Battery and heat come
    from the running being's body.state (waited for at most HEALTH_WAIT);
    hearing is measured by the voice, so it is asked for by voice."""
    from . import archive_integrity, context_storage, doctor

    body, voice = (facts or live_facts)()
    rows = doctor.health_rows(system or doctor.System(), doctor.Facts(state_dir=Path(mind), body=body, voice=voice))
    lines = [ui.mark(mark, text) for mark, text in rows]
    context_mode = context_storage.read_mode()
    if context_mode is not None:
        label = "Encrypted with a key for this boot" if context_mode == "encrypted" else "RAM only this boot"
        lines.append(ui.mark("muted", f"Working context · {label}"))
    full = archive_integrity.latest(mind) if not temporary else None
    if full is not None:
        label, tone = {"ok": ("Passed", "success"), "damaged": ("Damage detected", "danger"),
                       "timeout": ("Timed out", "warning"), "unavailable": ("Could not check", "warning")}[full["outcome"]]
        lines.append(ui.mark(tone, f"Full archive check · {full['day'].isoformat()} · {label}"))
    if body:
        lines += ["", "Hearing: ask me \"how well can you hear me?\" or \"are you okay?\"."]
    else:
        lines += ["", "Battery, temperature and hearing: ask me \"are you okay?\"."]
    if ui.enabled():
        ui.screen("Health", context="Care")
        ui.card(lines)
    else:
        print("\n".join(ui.plain(text) for text in lines))
    pause()


def diagnostics_page(mind, temporary):
    from . import usb_logs

    logged = usb_logs.logging_enabled()
    if logged is None:
        return
    intro = [row("Boot diagnostics", "On" if logged else "Off"), "",
             "On a Try stick I keep boot diagnostics on its PIPLOGS partition, to help fix problems on this Mac.",
             "They hold hardware details and system messages, never what you say. Hardware addresses",
             "and serial numbers are hidden."]
    choice = menu("Boot diagnostics", [("1", "Turn them off" if logged else "Turn them on")], intro=intro)
    if choice == "1":
        usb_logs.set_logging(not logged)
        done("Diagnostics are off on this stick. What was already saved this boot stays." if logged
             else "Diagnostics are on again.")


def library_page(mind, temporary):
    from . import library_pack
    from .try_mode import installed_trial_pending

    roles = list(library_pack.PACKS)
    packs = [library_pack.pack_information(mind, role) for role in roles]
    entries = [(str(index), pack["name"], pack["state"]) for index, pack in enumerate(packs, 1)]
    choice = menu("Offline library", entries, intro=[row("Guide", "Included in the signed PipOS system")])
    if choice is None:
        return
    index = int(choice) - 1
    chosen = packs[index]
    details = "Signed pack details" if chosen["authenticated"] else "Expected source details"
    intro = [details, row("Status", chosen["state"]), row("Licence", chosen["licence"]),
             row("Snapshot", installer_text(chosen["snapshot"] or "Unavailable"))]
    credit = installer_text(chosen["credit"], 400)
    credit_lines = ui.wrap(credit, indent=" " * 18)
    intro.extend([row("Credit", credit_lines[0].lstrip()), *credit_lines[1:]])
    if chosen["written_licence"]:
        intro.append(row("Written licence", installer_text(chosen["written_licence"])))
    if chosen["written_attribution"]:
        intro.append(row("Written credit", installer_text(chosen["written_attribution"])))
    enabled = chosen["enabled"]
    if not (enabled or chosen["authenticated"]) or (not temporary and installed_trial_pending(mind)):
        menu(chosen["name"], [], intro=intro)
        return
    verb = "Disable" if enabled else "Enable"
    if menu(chosen["name"], [("1", verb, chosen["state"])], intro=intro) != "1":
        return
    if not yes(f"{verb} {chosen['name']}?"):
        return
    root = Path(mind) / "library"
    if roles[index] == "library":
        marker = root / "dictionary-enabled"
        operation = library_pack.disable if enabled else library_pack.enable
    else:
        marker = root / "simple-en-enabled"
        operation = library_pack.disable if enabled else library_pack.enable_simple
    operation(marker)
    done(f"{chosen['name']} is {'disabled' if enabled else 'enabled'}.")


def care_menu(mind=MIND, *, temporary=False):
    """The Care page. Returns when the owner goes back."""
    mind = Path(mind)
    from . import care_habits

    pages = {"1": camera_page, "2": dreams_page, "3": memory_page, "4": remember_page,
             "5": lambda where, _: journal_page(where), "6": clock_page, "7": zone_page,
             "8": workshop_page, "9": network_page, "p": permissions_page,
             "d": diagnostics_page, "h": health_page, "l": library_page,
             "r": previous_system_page, "a": care_habits.page, "c": curiosity_page,
             "e": endurance_page}
    while True:
        entries = summary_rows(mind, temporary)
        choice = menu("Senses and memory", entries, context="Care",
                      subtitle="Try keeps these until you restart." if temporary else
                      "What I may use, and what I keep.",
                      draw=lambda rows: _draw_care_summary(rows, mind))
        if choice is None:
            return 0
        try:
            pages[choice](mind, temporary)
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
            ui.note(str(error), "danger")
