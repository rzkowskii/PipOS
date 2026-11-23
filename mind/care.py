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
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import wave
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import console_ui as ui
from .network_enforce import installed
from .storage import atomic_write, sync_directory


MIND = Path("/mind")
CAMERA_COMMAND = "/usr/sbin/being-camera"
WIFI_COMMAND = "/usr/sbin/being-wifi"
NETWORK_COMMAND = "/usr/sbin/being-network"
MIND_SERVICE = "being-mind.service"
VOICE_SERVICE = "being-voice.service"
CAMERA_MARKER = "camera.enabled"
DREAM_CONSENT = "dreams-enabled"
APPS_ENABLED = "apps.enabled"
APPS_DISABLED = "apps.disabled"
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


def menu(title, entries, *, intro=(), subtitle=None, context="Care"):
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
    return (Path(mind) / CAMERA_MARKER).is_file()


def set_camera(on):
    result = quiet([CAMERA_COMMAND, "enable" if on else "disable"], timeout=40)
    if result.returncode:
        raise ValueError((result.stderr or "").strip() or "The camera setting could not be changed.")
    return result.stdout.strip()


def camera_page(mind, temporary):
    on = camera_enabled(mind)
    intro = [row("Camera", "On" if on else "Off"), "",
             "When the camera is on I look for a face, to greet you and to see when you are near.",
             "Only where a face is leaves my eyes: pictures are not saved or sent anywhere.",
             "The green light beside the camera is on whenever it is in use."]
    if temporary:
        intro.append("In Try this lasts until you restart.")
    choice = menu("Camera", [("1", "Turn the camera off" if on else "Turn the camera on")], intro=intro)
    if choice == "1":
        set_camera(not on)
        done("The camera is on. Watch for the green light." if not on else "The camera is off.")


# --- dreaming -----------------------------------------------------------------

def dream_settings(mind=MIND):
    """(enabled, window). The consent file may hold the window as JSON."""
    path = Path(mind) / DREAM_CONSENT
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False, "night"
    if not stat.S_ISREG(info.st_mode):
        return False, "night"
    from .dream_policy import load_window

    return True, load_window(mind)


def set_dreams(mind, enabled, window="night"):
    path = Path(mind) / DREAM_CONSENT
    if not enabled:
        remove_marker(path)
        return
    if window not in DREAM_WINDOWS:
        raise ValueError("Choose night or whenever the lid is closed.")
    private_marker(path, json.dumps({"version": 1, "window": window}) + "\n")


def dreams_page(mind, temporary):
    enabled, window = dream_settings(mind)
    state = DREAM_WINDOWS[window] if enabled else "Off"
    intro = [row("Dreams", state), "",
             "While I dream I read back over what you told me that day and keep a journal",
             "of the moments that mattered, each linked to your own words. I use no model",
             "for this yet, and I never make things up for it. Ask me what I dreamt about."]
    if temporary:
        intro.append("In Try the journal is gone when you restart.")
    entries = [("1", "Dream at night only", "lid closed, on power, midnight to 5 am"),
               ("2", "Dream whenever the lid is closed", "on power, any time")]
    if enabled:
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
    restart_mind("the memory setting")
    done("Memory setting saved for this Try session." if temporary else "Memory setting saved.")


# --- what it remembers ----------------------------------------------------------

def remembered(mind=MIND, limit=40):
    """The newest facts, as (id, text). Read-only: nothing is created."""
    database = Path(mind) / "mind.db"
    if not database.is_file():
        return []
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as db:
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


def set_clock(mind, timestamp, *, settime=None):
    """Set the system clock and the battery clock, and tell the memory."""
    (settime or (lambda value: time.clock_settime(time.CLOCK_REALTIME, value)))(timestamp)
    if shutil.which("hwclock"):
        quiet(["hwclock", "-w", "-u"], timeout=20)
    database = Path(mind) / "mind.db"
    if database.is_file():
        from .memory import Memory

        try:
            with closing(Memory(Path(mind))) as memory:
                # An owner-set time is what the memory should believe.
                accept = getattr(memory, "owner_set_clock", None)
                (accept or memory.clock)(timestamp)
        except sqlite3.Error:
            pass


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
    timestamp = parse_local_time(value, zone)
    shown = datetime.fromtimestamp(timestamp, zone)
    if not yes(f"Set the clock to {shown:%A %d %B %Y, %H:%M}?"):
        return
    set_clock(mind, timestamp)
    done("Clock set.")


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
    intro = [row("Network mode", {"local-only": "Offline", "paired-lan": "Local network",
                                  "internet": "Internet"}.get(network_mode(mind), "Offline")),
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
    return not (Path(mind) / APPS_DISABLED).exists()


def set_workshop(mind, on):
    mind = Path(mind)
    if on:
        remove_marker(mind / APPS_DISABLED)
        private_marker(mind / APPS_ENABLED)
    else:
        remove_marker(mind / APPS_ENABLED)
        private_marker(mind / APPS_DISABLED)


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


def backup_key(mind, folder, temporary):
    """The public recipient to encrypt to, making a key on first use."""
    from .backup_crypto import generate_key, generate_protected_key

    recipient = recipient_on_file(mind)
    if recipient:
        return recipient
    ui.para("Your backups are locked with a key that only you keep. I'll make one now and save it on "
            "this stick. Move it to a different stick, or somewhere safe, after this: anyone who has "
            "the key and a backup can read the backup.", "plain")
    protect = yes("Protect the key with a passphrase?", default=True)
    path = folder / (KEY_NAME if protect else Path(KEY_NAME).with_suffix(".key").name)
    if path.exists() or path.is_symlink():
        raise ValueError(f"A key file is already on this stick ({path.name}). Keep it, or move it away to make "
                         "a new one.")
    from .backup import working_directory

    # FAT sticks have no hard links, so the key is made in RAM and copied once.
    with working_directory() as directory:
        staged = Path(directory) / path.name
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
    try:
        root = local_menu.mount_media(device, writable=True)
        folder = root / BACKUP_FOLDER
        folder.mkdir(mode=0o700, exist_ok=True)
        recipient = backup_key(mind, folder, temporary)
        view = ui.StageProgress("Backing up", (("Saving.", "Saving the being and its archive", 3),
                                               ("Proving.", "Recording what was forgotten", 1)),
                                context="Backup", footer="Keep the stick in until this finishes.")
        with view:
            view.stage("Saving.")
            result = backup.backup_to_folder(Path(mind), folder, recipient)
            view.stage("Proving.")
        key_on_stick = any((folder / name).exists() for name in (KEY_NAME, Path(KEY_NAME).with_suffix(".key").name))
        os.sync()
    finally:
        local_menu.unmount_media()
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
    """Compatible and version of an update bundle, checked against the keyring."""
    command = ["rauc", "info", "--output-format=json"]
    if RAUC_INSTALLED_CONF.exists():
        command.append(f"--conf={RAUC_INSTALLED_CONF}")
    result = quiet([*command, str(path)], timeout=120)
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        raise ValueError("That update is not signed for this being, or it is damaged."
                         + (f" ({detail[-1][:160]})" if detail else ""))
    info = json.loads(result.stdout)
    return {"compatible": info.get("compatible", ""), "version": info.get("version", "") or "unknown",
            "build": info.get("build", "") or ""}


def bundles(root):
    from . import local_menu

    return [path for path in local_menu.media_files(root) if path.name.endswith(UPDATE_SUFFIX)][:UPDATE_LIMIT]


def current_version():
    try:
        return BUILD_ID.read_text(encoding="utf-8").strip().splitlines()[0][:80]
    except (OSError, IndexError, UnicodeError):
        return "unknown"


def install_bundle(path):
    """rauc install, through the installed system's RAUC service and its checks."""
    result = run(["rauc", "install", str(path)], timeout=1800, stdin=subprocess.DEVNULL,
                 capture_output=True, text=True)
    if result.returncode:
        lines = [line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
        raise ValueError("The update did not install. The running system is unchanged."
                         + (f" ({lines[-1][:200]})" if lines else ""))


def update_menu(*, try_boot=None):
    """Update the system from a USB stick, keeping the being's memory."""
    from . import local_menu
    from .try_mode import is_try_boot

    if (is_try_boot() if try_boot is None else try_boot):
        ui.note("Updates are for an installed being. Install PipOS first.", "warning")
        return
    if not shutil.which("rauc"):
        ui.note("This image cannot install updates (no rauc).", "warning")
        return
    device = pick_stick("Update from a USB stick",
                        "Choose the stick that holds the update. It is only read, never changed.")
    if device is None:
        return
    try:
        root = local_menu.mount_media(device)
        found = bundles(root)
        if not found:
            ui.note("That stick holds no PipOS update (a .raucb file).", "warning")
            return
        entries = [(str(number), installer_text(str(path.relative_to(root)), 50)) for number, path in
                   enumerate(found, 1)]
        choice = menu("Choose the update", entries, intro=[row("Running now", current_version())])
        if choice is None:
            return
        bundle = found[int(choice) - 1]
        info = bundle_info(bundle)
        rows = [row("Running now", current_version()), row("Update", info["version"]),
                row("Made for", info["compatible"])]
        if info["build"]:
            rows.append(row("Build", info["build"][:40]))
        rows += ["", "My memory stays as it is. The update goes into my spare system copy; if it does not",
                 "start properly, I go back to this one on my own."]
        if ui.enabled():
            ui.screen("Ready to update", context="Update")
            ui.card(rows)
        else:
            print("\n".join(ui.plain(text) for text in rows))
        if not yes("Install this update?"):
            return
        view = ui.StageProgress("Updating", (("Installing.", "Installing into the spare system copy", 1),),
                                context="Update", footer="Keep the Mac plugged in. This takes a few minutes.")
        with view:
            view.stage("Installing.")
            install_bundle(bundle)
    finally:
        local_menu.unmount_media()
    done("The update is installed. I'll start the new system on the next restart.")
    if yes("Restart now to start the new system?", default=True):
        quiet(["systemctl", "reboot"], timeout=30)


# --- the page ---------------------------------------------------------------------

def summary_rows(mind, temporary):
    enabled, window = dream_settings(mind)
    zone = zone_of(mind)
    from .retention import read_policy

    try:
        memory = MEMORY_MODES[read_policy(mind).mode][0]
    except (OSError, ValueError, RecursionError):
        memory = "Damaged: choose a mode"  # the mind treats it as keeping nothing new
    rows = [("1", "Camera", "On" if camera_enabled(mind) else "Off"),
            ("2", "Dreams", ("Night" if window == "night" else "Lid closed") if enabled else "Off"),
            ("3", "Memory", memory),
            ("4", "What I remember", "Read or forget"),
            ("5", "Dream journal", "What I kept while dreaming"),
            ("6", "Date and time", f"{datetime.now(zone):%a %H:%M}"),
            ("7", "Time zone", zone.key if zone else "Not set"),
            ("8", "Workshop", "On" if workshop_enabled(mind) else "Off")]
    if not temporary:
        rows.append(("9", "Network", "Wi-Fi and wired Ethernet"))
    else:
        from . import usb_logs

        logged = usb_logs.logging_enabled()
        if logged is not None:
            rows.append(("D", "Boot diagnostics", "Saved on this stick" if logged else "Off"))
    return rows


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


def care_menu(mind=MIND, *, temporary=False):
    """The Care page. Returns when the owner goes back."""
    mind = Path(mind)
    pages = {"1": camera_page, "2": dreams_page, "3": memory_page, "4": remember_page,
             "5": lambda where, _: journal_page(where), "6": clock_page, "7": zone_page,
             "8": workshop_page, "9": network_page, "d": diagnostics_page}
    while True:
        entries = summary_rows(mind, temporary)
        choice = menu("Senses and memory", entries, context="Setup",
                      subtitle="Try keeps these until you restart." if temporary else
                      "What I may use, and what I keep.")
        if choice is None:
            return 0
        try:
            pages[choice](mind, temporary)
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
            ui.note(str(error), "danger")
