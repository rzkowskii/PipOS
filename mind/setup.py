"""Choose a name, voice, personality and network mode from a local terminal."""

import argparse
import asyncio
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from . import care
from . import console_ui as ui
from . import voice_engines
from .bus import Bus
from .initiative_setup import configure_initiative
from .network_enforce import installed
from .network_policy import INTERNET, LOCAL_ONLY, PAIRED_LAN, load_policy, validate_time_server
from .profile import (
    FACTORY_CONFIG, PRESET_LABELS, PRESETS, SPEECH_RATES, TRAITS, VOICES, default_mind_dir,
    load_profile, personality_greetings, random_personality, save_profile,
)
from .recognition import Recognizer
from .try_mode import is_try_boot
from .tts import available_voices, playback_command, synthesis_command
from .voice import FW, TALKING, audio_devices, child


NAMES = ("Robin", "Clover", "Finn", "Hazel", "Jasper", "Milo", "Olive", "Wren")
LEVELS = {0: "Low", 1: "Medium", 2: "High"}
APPLIANCE_MIND = Path("/mind")
NETWORK_COMMAND = "/usr/sbin/being-network"
NETWORK_MODES = {LOCAL_ONLY: "Offline", PAIRED_LAN: "Local network", INTERNET: "Internet"}


def description(profile):
    preset = next((PRESET_LABELS[key] for key in PRESETS if PRESETS[key] == profile.personality), "Custom")
    traits = ", ".join(f"{LEVELS[getattr(profile.personality, trait)].lower()} {trait}" for trait in TRAITS)
    return (
        f"{profile.name} calls you {profile.owner}. Wake phrase: {profile.wake_word}\n"
        f"Voice: {VOICES.get(profile.voice, profile.voice)} ({SPEECH_RATES[profile.rate].lower()} speed)\n"
        f"Personality: {preset} ({traits})"
    )


def preset_label(profile):
    return next((PRESET_LABELS[key] for key in PRESETS if PRESETS[key] == profile.personality), "Custom")


def profile_rows(profile):
    """The profile as card rows for the console UI, with small trait gauges."""
    def row(label, value):
        return f"{ui.paint(label.ljust(13), 'muted')}{ui.paint(value, 'text')}"

    voice = VOICES.get(profile.voice, profile.voice)
    rows = [f"{ui.paint(profile.name, 'accent', 'bold')}{ui.paint(' calls you ', 'muted')}"
            f"{ui.paint(profile.owner, 'text', 'bold')}",
            "",
            row("Wake phrase", profile.wake_word),
            row("Voice", f"{voice}{ui.dot()}{SPEECH_RATES[profile.rate].lower()}"),
            row("Personality", preset_label(profile))]
    gauges = [f"{trait.capitalize()} {ui.meter(getattr(profile.personality, trait))}" for trait in TRAITS]
    rows.append(" " * 13 + "   ".join(gauges[:3]))
    rows.append(" " * 13 + "   ".join(gauges[3:]))
    return rows


def sample(profile):
    greetings = personality_greetings(profile.personality)
    lines = (greetings[topic][0].split("] ", 1)[-1] for topic in ("hello", "check_in"))
    return f"I'm {profile.name}. " + " ".join(lines)


async def play_preview(audio):
    """Hold the open-lid and microphone checks for the duration of playback."""
    topics = ("body.lid", "state.changed")
    readings = {}
    ready = asyncio.Event()
    blocked = asyncio.Event()
    # The voice service never captures on the serial-only lab body.
    serial_only = (FW / "serial" / "raw").exists()

    def disarm():
        blocked.set()
        ready.set()

    def observe(event):
        readings[event["t"]] = event
        if all(topic in readings for topic in topics):
            state = readings["state.changed"]
            if (readings["body.lid"].get("closed") is not False or state.get("state") in TALKING
                    or not (serial_only or state.get("muted") is True)):
                disarm()
            else:
                ready.set()

    path = os.environ.get("BEING_BUS", "/run/being/bus.sock")
    bus = Bus(f"setup-preview-{os.getpid()}", topics, path=path, urgent=observe)
    bus.on_disconnect = disarm
    await bus.start()
    try:
        try:
            await asyncio.wait_for(ready.wait(), 3)
        except asyncio.TimeoutError:
            return False
        if blocked.is_set():
            return False
        async with child(*playback_command(audio_devices()[1], audio), stdout=asyncio.subprocess.DEVNULL,
                         stderr=asyncio.subprocess.DEVNULL) as player:
            playing = asyncio.create_task(player.wait())
            changed = asyncio.create_task(blocked.wait())
            try:
                done, _ = await asyncio.wait((playing, changed), timeout=20, return_when=asyncio.FIRST_COMPLETED)
                if changed in done:
                    return False
                if playing not in done or playing.result():
                    raise OSError("Could not play the audio sample.")
                return True
            finally:
                playing.cancel()
                changed.cancel()
                await asyncio.gather(playing, changed, return_exceptions=True)
    finally:
        await bus.stop()


def preview(profile):
    line = sample(profile)
    if ui.enabled():
        ui.line()
        ui.line(ui.mark("accent", ui.paint("Playing a sample", "text")))
        ui.para(f'"{line}"', "muted", indent="  ")
    else:
        print(f"\nPreview: {line}", flush=True)
    if profile.voice not in (available_voices() or ()):
        ui.note("The selected voice is unavailable.", "warning")
        return
    try:
        with tempfile.TemporaryDirectory(prefix="being-preview-") as directory:
            audio = Path(directory) / "sample.wav"
            subprocess.run(synthesis_command(profile.voice, profile.rate, line, audio), check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not asyncio.run(play_preview(audio)):
                ui.note("Audio preview needs PipOS awake with the lid open and microphone muted.", "warning")
    except (OSError, subprocess.SubprocessError):
        ui.note("Audio preview is unavailable. The sample is shown above." if not ui.enabled()
                else f'Audio preview is unavailable. The sample: "{line}"', "warning")


# --- natural or original voice ---------------------------------------------------

VOICE_SERVICE = "being-voice.service"
ENGINE_CHOICES = ("auto", "piper", "flite")


def engine_labels(status):
    return {"auto": "Automatic", "piper": f"Natural ({status['label']})", "flite": "Original"}


def voice_engine_rows(status):
    labels = engine_labels(status)
    limit = voice_engines.TTS_RTF_LIMIT
    if not status["installed"]:
        speed = "Not installed in this image"
    elif status["rtf"] is None:
        speed = "Not measured on this laptop yet"
    else:
        verdict = "fast enough" if status["rtf"] <= limit else "too slow for automatic use"
        speed = f"{status['rtf']:.2f} of real time ({verdict})"
    rows = [care.row("Speaking with", labels[status["active"]]),
            care.row("Choice", labels[status["preference"]] +
                     (", by measured speed" if status["preference"] == "auto" else ", chosen by you")),
            care.row("Natural voice speed", speed)]
    return rows + ["", f"Automatic uses the natural voice only if it measures {limit:g} of real time or less.",
                   "That means a 2-second sentence takes at most 0.7 seconds to make."]


def engine_sample(profile, engine, audio):
    """Write a sample of `engine` ("piper" or "flite") speaking to `audio`."""
    line = sample(profile)
    if engine == "piper":
        from .tts_sherpa import PiperEngine

        piper = PiperEngine()
        try:
            piper.synthesize_blocking(line, audio, profile.rate)
        finally:
            piper.close()
    else:
        subprocess.run(synthesis_command(profile.voice, profile.rate, line, audio), check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return line


def play_engine_sample(profile, engine):
    try:
        with tempfile.TemporaryDirectory(prefix="being-preview-") as directory:
            audio = Path(directory) / "sample.wav"
            line = engine_sample(profile, engine, audio)
            ui.para(f'"{line}"', "muted", indent="  ")
            if not asyncio.run(play_preview(audio)):
                ui.note("Audio preview needs PipOS awake with the lid open and microphone muted.", "warning")
    except Exception as error:  # a voice that will not load must not end setup
        ui.note(f"Could not play that voice: {error}", "warning")


def measure_natural_voice(mind_dir):
    from .tts_sherpa import PiperEngine

    piper = PiperEngine()
    try:
        ui.note("Measuring the natural voice: three sentences, a few seconds.")
        result = voice_engines.measure_tts(piper)
        entry = voice_engines.record(mind_dir, "tts", voice_engines.tts_key(voice_engines.build_id(), piper),
                                     result)
    except Exception as error:
        ui.note(f"Could not measure the natural voice: {error}", "warning")
        return None
    finally:
        piper.close()
    ui.note(f"The natural voice measured {entry['rtf']:.2f} of real time.", "success")
    return entry


def apply_engine(mind_dir, choice, *, dry_run=False, appliance=False):
    if dry_run:
        ui.note("Preview only: the voice choice was not saved.")
        return False
    voice_engines.save_preference(mind_dir, choice)
    if appliance and installed(VOICE_SERVICE):
        result = subprocess.run(["systemctl", "try-restart", VOICE_SERVICE], check=False, timeout=60,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            ui.note("Saved. The voice could not restart; the choice applies after the next restart.", "warning")
            return True
    ui.note("Voice choice saved.", "success")
    return True


def voice_page(mind_dir, profile, *, dry_run=False, appliance=False):
    """Choose the natural (Piper) or original (Flite) voice, or let speed decide."""
    while True:
        status = voice_engines.tts_status(mind_dir)
        labels = engine_labels(status)
        entries = [("1", labels["auto"], "Natural if it is fast enough here"),
                   ("2", labels["piper"], "Not installed" if not status["installed"] else None),
                   ("3", labels["flite"], "Pip's first voice")]
        entries = [entry if entry[2] else entry[:2] for entry in entries]
        if status["installed"]:
            entries += [("N", "Play the natural voice"), ("M", "Measure the natural voice now")]
        entries.append(("O", "Play the original voice"))
        choice = care.menu("Natural or original voice", entries, intro=voice_engine_rows(status),
                           context="Setup")
        if choice is None:
            return
        if choice in ("1", "2", "3"):
            selected = ENGINE_CHOICES[int(choice) - 1]
            if selected == "piper" and not status["installed"]:
                ui.note("The natural voice is not installed in this image.", "warning")
                continue
            apply_engine(mind_dir, selected, dry_run=dry_run, appliance=appliance)
        elif choice == "n":
            play_engine_sample(profile, "piper")
        elif choice == "o":
            play_engine_sample(profile, "flite")
        elif choice == "m":
            measure_natural_voice(mind_dir)


async def restart_try_organs():
    path = os.environ.get("BEING_BUS", "/run/being/bus.sock")
    bus = Bus("setup-reload", ("state.changed", "organ.up"), path=path)
    await bus.start()
    try:
        async with asyncio.timeout(45):
            # Receive the initial snapshot before requesting a restart, so fast
            # registrations from the new processes cannot be missed.
            while (await bus.recv())["t"] != "state.changed":
                pass
            # The laptop image has no lab body; naming a missing unit fails the restart.
            units = installed("being-mind.service", "being-voice.service", "being-lab-body.service")
            async with child("systemctl", "try-restart", *units) as process:
                if await process.wait():
                    raise RuntimeError("Could not restart the mind and audio services.")
            waiting = {"mind", "voice"}
            while waiting:
                event = await bus.recv()
                if event["t"] == "organ.up":
                    waiting.discard(event.get("name"))
    except TimeoutError as error:
        raise RuntimeError("The mind and audio services did not rejoin in time.") from error
    finally:
        await bus.stop()


def choose(title, options, *, intro=None, current=None):
    numbered = {str(number): key for number, key in enumerate(options, 1)}
    entries = [(number, options[key], "current" if key == current else None) for number, key in numbered.items()]
    entries.append(("0", "Back"))
    if not ui.enabled():
        print(f"\n{title}")
        ui.options(entries)
    while True:
        if ui.enabled():
            ui.screen(title, context="Setup")
            for row in intro or ():
                ui.line(row)
            if intro:
                ui.line()
            ui.options(entries)
        choice = input(ui.prompt("Choice: ")).strip()
        if choice in ("", "0", "q", "Q"):
            return None
        if choice in numbered:
            return numbered[choice]
        ui.note("Choose a number from the menu.", "warning")


def check_wake_phrase(phrase):
    Recognizer(phrase).close()


def edit_text(profile, field, label):
    current = getattr(profile, field)
    value = input(ui.prompt(f"{label} [{current}]: ")).strip()
    if not value or value == current:
        return profile
    try:
        draft = replace(profile, **{field: value})
        if field == "wake_word":
            check_wake_phrase(draft.wake_word)
    except (ValueError, RuntimeError, OSError) as error:
        ui.note(f"Could not use that {label.lower()}. {error}", "warning")
        return profile
    return draft


def tune_personality(profile):
    if ui.enabled():
        ui.screen("Tune personality", context="Setup",
                  subtitle="Enter 0 for low, 1 for medium, or 2 for high. Press Enter to keep a value.")
        for trait in TRAITS:
            ui.line(f"{ui.paint(trait.capitalize().ljust(16), 'plain')}{ui.meter(getattr(profile.personality, trait))}")
    else:
        print("\nEnter 0 for low, 1 for medium, or 2 for high. Press Enter to keep a value.")
    values = {}
    for trait in TRAITS:
        current = getattr(profile.personality, trait)
        while (value := input(ui.prompt(f"{trait.capitalize()} [{current}]: ")).strip()) not in {"", "0", "1", "2"}:
            if ui.enabled():
                ui.line(ui.mark("warning", "Enter 0, 1, 2, or press Enter."))
            else:
                print("Enter 0, 1, 2, or press Enter.")
        values[trait] = int(value) if value else current
    return replace(profile, personality=replace(profile.personality, **values))


def random_character(profile, voices):
    """A random name, voice and personality, with a wake phrase that matches the name.

    A name whose "Hey <name>" the recognizer cannot spot is re-rolled, so the
    being is never called one thing and woken by another.
    """
    voice = random.choice(tuple(voices)) if voices else profile.voice
    personality = random_personality()
    for name in random.sample(NAMES, len(NAMES)):
        phrase = f"Hey {name}"
        try:
            check_wake_phrase(phrase)
        except (ValueError, RuntimeError, OSError):
            continue
        return replace(profile, name=name, voice=voice, personality=personality, wake_word=phrase)
    # No name can be spotted (no recognizer here): keep the current name and phrase.
    return replace(profile, voice=voice, personality=personality)


def review_roll(profile, roll):
    """Return the selected draft and whether the user chose to finish setup."""
    result = roll()
    while True:
        if ui.enabled():
            ui.screen("Surprise me", context="Setup", subtitle="A new character, chosen at random.")
            ui.card(profile_rows(result), tone="accent_soft")
            ui.line()
            ui.para(f'"{sample(result)}"', "plain", indent="  ")
            ui.line()
            ui.line("   ".join(f"{ui.paint(key, 'accent', 'bold')} {ui.paint(label, 'plain')}" for key, label in (
                ("P", "Play"), ("K", "Keep"), ("R", "Roll again"), ("E", "Edit"), ("Q", "Discard"))))
        else:
            print(f"\n{description(result)}\nSample: {sample(result)}")
        choice = input(ui.prompt("Choice: ") if ui.enabled()
                       else "[P] Play sample  [K] Keep  [R] Roll again  [E] Edit  [Q] Discard roll: ").strip().lower()
        if choice == "p":
            preview(result)
        elif choice == "k":
            return result, True
        elif choice == "r":
            result = roll()
        elif choice == "e":
            return result, False
        elif choice == "q":
            return profile, False
        else:
            ui.note("Choose P, K, R, E, or Q.", "warning")


def edit_personality(profile):
    current = next((key for key in PRESETS if PRESETS[key] == profile.personality), None)
    choice = choose("Personality", {**PRESET_LABELS, "random": "Random personality", "tune": "Tune individual traits"},
                    current=current)
    if choice == "random":
        return review_roll(profile, lambda: replace(profile, personality=random_personality()))
    if choice == "tune":
        return tune_personality(profile), False
    if choice in PRESETS:
        return replace(profile, personality=PRESETS[choice]), False
    return profile, False


def describe_network(mode, web_lookups, time_server):
    return (f"System network mode  {NETWORK_MODES[mode]}\n"
            f"Public web lookups   {'Allowed when requested' if web_lookups else 'Off'}\n"
            f"Clock                {time_server if time_server else 'Device clock'}")


def network_rows(mode, web_lookups, time_server):
    return [f"{ui.paint(label, 'muted')}{ui.paint(value, 'text')}"
            for label, value in (line.split("  ", 1) for line in describe_network(mode, web_lookups, time_server)
                                 .splitlines())]


def configure_network(mind_dir):
    policy = load_policy(mind_dir)
    mode = policy.effective_mode
    if ui.enabled():
        intro = []
        ui.screen("Network", context="Setup")
        rows = network_rows(mode, mode == INTERNET and policy.permits_web_lookup, policy.time_server)
        intro = [ui.paint("Now", "muted")] + ["  " + row for row in rows]
    else:
        intro = None
        print(f"\n{describe_network(mode, mode == INTERNET and policy.permits_web_lookup, policy.time_server)}")
    selected = choose("System network mode", NETWORK_MODES, intro=intro, current=mode)
    if selected is None:
        return 0

    web_lookups = False
    time_server = None
    if selected == INTERNET:
        if ui.enabled():
            ui.screen("Internet", context="Setup",
                      subtitle="The being only looks things up on the web when you ask it to, and only if you allow it here.")
        web_lookups = input(ui.prompt("Allow requested public web lookups? [y/N] ")).strip().lower() in ("y", "yes")
        while value := input(ui.prompt("Network time server IPv4 or IPv6 address (blank to use device clock): ")).strip():
            try:
                time_server = validate_time_server(value)
            except ValueError as error:
                if ui.enabled():
                    ui.line(ui.mark("warning", str(error)))
                else:
                    print(error)
            else:
                break

    if ui.enabled():
        ui.screen("Review network settings", context="Setup")
        ui.card(network_rows(selected, web_lookups, time_server))
    else:
        print(f"\n{describe_network(selected, web_lookups, time_server)}")
    if input(ui.prompt("Apply these network settings? [y/N] ")).strip().lower() not in ("y", "yes"):
        return 0

    command = [NETWORK_COMMAND, "set", selected]
    if web_lookups:
        command.append("--web-lookups")
    if time_server:
        command.extend(("--time-server", time_server))
    return subprocess.run(command, check=False).returncode


# Typing to the being from Setup (an installed being's F2): the voice's
# "You can also type to me" hint names this key (mind/type_chat.py).
TYPE_CHAT_KEY = "Y"


def system_entries(appliance, installed_being, name=None):
    """Owner controls beyond the profile. They act at once, apart from Keep."""
    if not appliance:
        return []
    name = name or "Pip"
    entries = [("C", "Senses and memory", "Camera, dreams, memory, clock"),
               ("A", "Volume and microphone"), ("T", "Time zone")]
    if installed_being:
        entries += [("B", "Back up to a USB stick"), ("U", "Update from a USB stick"),
                    (TYPE_CHAT_KEY, f"Talk to {name} by typing", f"When {name} can't hear you.")]
    return entries


def run_system_entry(choice, mind_dir, *, temporary):
    if choice == "c":
        care.care_menu(mind_dir, temporary=temporary)
    elif choice == "a":
        care.audio_page(mind_dir, temporary)
    elif choice == "t":
        care.zone_page(mind_dir, temporary)
    elif choice == "b":
        care.backup_menu(mind_dir, temporary=temporary)
    elif choice == "u":
        care.update_menu(try_boot=temporary)
    elif choice == TYPE_CHAT_KEY.lower():
        from . import type_chat

        type_chat.main(mind_dir=mind_dir)


def personalize(profile, mind_dir, *, dry_run=False, network_dir=None, temporary=False, appliance=False):
    installed = available_voices() or set()
    voices = {key: label for key, label in VOICES.items() if key in installed}
    if not ui.enabled():
        print("PipOS personalization")
    while True:
        entries = [("1", "Name"), ("2", "What it calls you"), ("3", "Wake phrase"), ("4", "Voice"),
                   ("5", "Speaking speed"), ("6", "Personality"), ("7", "Surprise me", "A random new character")]
        if network_dir is not None:
            entries.append(("8", "Network"))
        entries += [("9", "Quiet hours and announcements"), ("N", "Natural or original voice")]
        extra = system_entries(appliance and not dry_run, network_dir is not None, name=profile.name)
        entries += extra
        entries += [("P", "Play sample"), ("K", "Keep these choices", "Finish setup"), ("Q", "Cancel")]
        if ui.enabled():
            subtitle = ("Try keeps these choices until you restart." if temporary else
                        "Try these choices out without saving." if dry_run else None)
            ui.screen("Make it yours", context="Setup", subtitle=subtitle)
            ui.card(profile_rows(profile), tone="accent_soft")
            ui.line()
        else:
            print(f"\n{description(profile)}")
            print()
        ui.options(entries)
        choice = input(ui.prompt("Choice: ")).strip().lower()
        keep = choice == "k"
        if choice == "1":
            profile = edit_text(profile, "name", "Name")
        elif choice == "2":
            profile = edit_text(profile, "owner", "What it calls you")
        elif choice == "3":
            profile = edit_text(profile, "wake_word", "Wake phrase")
        elif choice == "4":
            if voices:
                voice = choose("Voice", voices, current=profile.voice)
                if voice is not None:
                    profile = replace(profile, voice=voice)
            else:
                ui.note("No local voices were found.", "warning")
        elif choice == "5":
            rate = choose("Speaking speed", SPEECH_RATES, current=profile.rate)
            if rate is not None:
                profile = replace(profile, rate=rate)
        elif choice == "6":
            profile, keep = edit_personality(profile)
        elif choice == "7":
            profile, keep = review_roll(profile, lambda: random_character(profile, voices))
        elif choice == "8" and network_dir is not None:
            configure_network(network_dir)
        elif choice == "9":
            # Plain output keeps the submenu's own title as its heading.
            ui.screen("Quiet hours and announcements" if ui.enabled() else None, context="Setup")
            with ui.indented(skip="Quiet hours and announcements"):
                configure_initiative(mind_dir, dry_run=dry_run, temporary=temporary)
        elif choice == "n":
            voice_page(mind_dir, profile, dry_run=dry_run, appliance=appliance)
        elif choice in {key.lower() for key, *_ in extra}:
            try:
                run_system_entry(choice, mind_dir, temporary=temporary)
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
                ui.note(str(error), "danger")
        elif choice == "p":
            preview(profile)
        elif choice == "q":
            return None
        elif choice != "k":
            ui.note("Choose a number, P, K, or Q.", "warning")
        if keep:
            prompt = "Finish this preview?" if dry_run else "Save these choices?"
            if input(ui.prompt(f"{prompt} [y/N] ")).strip().lower() in ("y", "yes"):
                return profile


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-setup", description=__doc__)
    parser.add_argument("--config", type=Path, default=FACTORY_CONFIG, help="factory configuration directory")
    parser.add_argument("--mind-dir", type=Path, help="personalized profile directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="print the effective profile as JSON")
    mode.add_argument("--dry-run", action="store_true", help="try choices without saving")
    mode.add_argument("--network", action="store_true", help="change network settings on the installed appliance")
    mode.add_argument("--initiative", action="store_true", help="change quiet hours and automatic announcements")
    mode.add_argument("--care", action="store_true", help="camera, dreams, memory, clock and network")
    mode.add_argument("--audio", action="store_true", help="volume and microphone")
    mode.add_argument("--time-zone", action="store_true", help="choose the time zone")
    mode.add_argument("--backup", action="store_true", help="back up to a USB stick")
    mode.add_argument("--update", action="store_true", help="update the system from a USB stick")
    mode.add_argument("--voice", action="store_true", help="choose the natural or original voice")
    args = parser.parse_args(argv)
    if not args.show and not sys.stdin.isatty():
        parser.error("personalization requires an interactive terminal")

    mind_dir = args.mind_dir or default_mind_dir()
    try:
        on_appliance = mind_dir.resolve() == APPLIANCE_MIND
        live_preview = on_appliance and is_try_boot()
        network_dir = mind_dir if on_appliance and not live_preview and not args.dry_run else None
        if args.initiative:
            return configure_initiative(mind_dir, temporary=live_preview)
        if args.care or args.audio or args.time_zone or args.backup or args.update:
            if not on_appliance:
                raise ValueError("These settings are available on the appliance.")
            choice = "c" if args.care else "a" if args.audio else "t" if args.time_zone else \
                "b" if args.backup else "u"
            if choice == "u" and live_preview:
                raise ValueError("Updates are for an installed being.")
            run_system_entry(choice, mind_dir, temporary=live_preview)
            return 0
        if args.network:
            if network_dir is None:
                raise ValueError("Network setup is available on the installed appliance.")
            return configure_network(network_dir)
        profile = load_profile(args.config, mind_dir)
        if args.voice:
            voice_page(mind_dir, profile, appliance=on_appliance)
            return 0
        if args.show:
            print(json.dumps(asdict(profile), ensure_ascii=False))
            return 0
        if live_preview and not ui.enabled():
            print("Try PipOS keeps these choices until you reboot.")
        selected = personalize(profile, mind_dir, dry_run=args.dry_run, network_dir=network_dir, temporary=live_preview,
                               appliance=on_appliance)
        if selected is None:
            ui.note("Setup canceled.")
        elif args.dry_run:
            ui.note("Preview finished.")
        else:
            save_profile(mind_dir, selected)
            if live_preview:
                if ui.enabled():
                    ui.screen(None, context="Setup")
                    ui.line(ui.mark("accent", ui.paint(f"Waking {selected.name} with the new choices", "text")))
                try:
                    asyncio.run(restart_try_organs())
                except (OSError, RuntimeError) as error:
                    ui.note(f"Profile saved for this session. Could not restart the being: {error}", "danger",
                            stream=sys.stderr)
                    return 1
                ui.note("Profile applied for this Try session.", "success")
            else:
                ui.note("Profile saved. Reboot PipOS to use these choices.", "success")
        return 0
    except KeyboardInterrupt:
        print("\nSetup canceled.")
        return 130
    except EOFError:
        print("\nSetup canceled.")
        return 1
    except (OSError, ValueError) as error:
        ui.note(f"being-setup: {error}" if not ui.enabled() else str(error), "danger", stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
