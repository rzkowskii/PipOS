<!--
doc: reference
verified: 2026-11-23T22:20
sources: mind/commands.py mind/reminder_language.py mind/turn.py mind/skills.py mind/profile.py mind/setup.py mind/local_menu.py mind/retention.py mind/backup.py mind/dream_journal.py mind/network_enforce.py mind/network_policy.py mind/initiative.py mind/voice.py mind/tts.py mind/eyes.py mind/__main__.py crates/face/src/main.rs crates/brainstem/src/main.rs sim/body.py lab/vm.py lab/build.sh scripts/run-sim.sh os/package/being/being.env os/package/being/being-camera config/being.json mind/wifi.py mind/reminders.py os/package/being/being-usb-logs os/package/being/being-hardware-report
-->

# PipOS reference

These are lookup tables for everything Pip understands, every key, command,
file and setting. They describe the current source. Each section names the
source file it comes from. When that file changes, check the table.

- [Spoken commands](#spoken-commands)
- [Keys and touch](#keys-and-touch)
- [On-screen menus](#on-screen-menus)
- [Maintenance commands](#maintenance-commands)
- [Files in /mind](#files-in-mind)
- [Factory configuration](#factory-configuration)
- [Services](#services)
- [Environment variables](#environment-variables)
- [Kernel command line](#kernel-command-line)
- [Lab and simulator commands](#lab-and-simulator-commands)

## Spoken commands

Sources: `mind/commands.py`, `mind/reminder_language.py`, `mind/turn.py`,
`mind/skills.py`

Matching works like this:

- It is case-insensitive and needs the **whole utterance** to match.
- A leading or trailing "please" is ignored. So is an address such as
  "Pip," or "hey Pip".
- Anything that matches nothing goes to the language model.
- These commands are always answered from code, never by the model.

### Clock and calendar

| Say | Pip replies |
|---|---|
| "What time is it?" / "What's the time?" / "Tell me the time" | "It's 3:05 in the afternoon." |
| "What's the date?" / "What's today's date?" / "What date is it?" | "It's Monday, November 23, 2026." |
| "What day is it?" / "What day is today?" | "It's Monday." |
| "What's tomorrow's date?" / "What day is tomorrow?" | "Tomorrow is …" |
| "Set my time zone to Australia Sydney" | Saves an IANA zone. Spoken forms such as "Australia slash Sydney" work |
| "Use the device time zone" | Clears the saved zone |

### Weather (needs Internet mode and web-lookup permission)

| Say | Behaviour |
|---|---|
| "What's the weather?" / "What's the weather in Sydney, Australia tomorrow?" / "Will it rain today?" / "Is it raining?" / "What's it like outside?" | Uses Open-Meteo, in Celsius. Reports current conditions, today or tomorrow |
| "Set my weather city to Sydney, Australia" | Saves the default city (allowed in any network mode) |
| "Clear my weather city" | Removes it |

When lookups are not allowed, Pip says: "Weather needs Internet mode and
permission for web lookups in setup."

### Reminders and timers

| Say | Behaviour |
|---|---|
| "Remind me to call Mum tomorrow at nine A.M." | One-off reminder. The time can be given as follows: <ul><li>"in N minutes / hours / days"</li><li>"at 7:30 pm", "noon" or "midnight"</li><li>a day: "today", "tonight", "tomorrow", "(next) Friday" or "2026-12-01". A day word also needs a full time, as in "next Friday at 9 am"</li></ul> |
| "Remind me in ten minutes to check the oven" | Relative reminder |
| "Set a timer for five minutes" / "Start a 10 minute timer" | Countdown |
| "What are my reminders?" / "List my timers" | Reads up to five pending items with their numbers |
| "Cancel reminder one" / "Cancel the timer" / "Delete the reminder to call Mum" | Removes an item by number or by task |

Pip asks for clarification in some cases:

- A bare hour such as "at nine" gets "Did you mean a.m. or p.m.?"
- A time in the past gets "That time has already passed."

When a timer finishes, a bell plays and Pip says "Richard, your ten-minute
timer is done." When a reminder is due, it says "You asked me to remind you:
*task*." An alarm says the time ("Good morning. It's 6:30 in the morning. Time to get up.").
A ring is said in 20 words or fewer, is honest when it is late, and repeats up
to three times until you answer ("okay" or "snooze").

### Memory

| Say | Behaviour |
|---|---|
| "Remember that my orchid is called Silverleaf" / "Don't forget that …" | Stores a fact. "Okay. I'll remember that." |
| "What do you remember about my orchid?" / "Do you remember what I said about …?" | Searches facts first ("You told me: …"). Then it searches the conversation archive and quotes the single best match with its date ("On November 20, 2026, you said: …") |
| "What did I say about the café?" / "What have I told you about …?" | Searches only what you said |
| "What did you say about …?" | Searches only what Pip said |
| "What did we talk about last Friday?" / "… yesterday" / "… on 2026-11-20" | Searches a single day in your time zone |
| "That's wrong, I called my orchid Silverleaf" | Replaces the last recalled or remembered item. Give the **full corrected wording** |
| "Forget that" | Forgets the utterance just heard, or the item just recalled or remembered |
| "Forget about my sister's birthday" / "Forget everything from yesterday" | Pip says how many items match, then waits up to 45 s for **"yes"** from the same speaker. Saying "no", "never mind" or "forget it" keeps everything |
| "Start guest mode" / "Don't remember this" | Starts a private RAM conversation. Your archive, private core, reminders and network requests are unavailable |
| "End guest mode" | Clears the guest conversation and returns to normal |

### About Pip

| Say | Pip replies |
|---|---|
| "What's your name?" | "My name is Pip." |
| "How old are you?" / "When were you born?" | Its age and wake count from the life record |
| "How is your battery?" / "How much charge do you have?" | Its battery percentage, and whether it is charging, plugged in or on battery |
| "What can you do?" | A short list of skills |
| "Go to sleep" / "Good night" | "Okay. Good night." Between 5 a.m. and 6 p.m. it says "Okay. I'll rest my eyes for a while." instead. Pip then sleeps until you say the wake phrase (not while muted) or open the lid |
| "What do you see?" / "Look at this" | "I can't see yet. My eyes aren't connected." (Deliberate vision is not built yet) |

### Greetings (local replies, no model)

Pip answers short greetings, check-ins and presence questions locally:

- greetings: "Hi", "Hello there", "Good morning"
- check-ins: "How are you?", "What's up?"
- presence: "Are you there?", "Can you hear me?"

The replies rotate with the chosen personality. A greeting with a topic
goes to the model.

### Replies when something goes wrong

| Pip says | Meaning |
|---|---|
| "I can't think right now. My language model isn't answering." | The model server is down or loading. Direct commands still work |
| "Sorry. I lost my thought." | The model produced nothing usable, or failed before saying anything. A reply that only repeats the question is retried once first |
| "I can't read my battery level right now." | The battery reading is unknown |
| "I don't remember anything about that." | Recall found no fact or archive match |
| "I'm not sure which memory you mean." | "That's wrong, …" had no recent memory to correct |
| "I'm not sure what to forget. Tell me what it was about." | "Forget that" had nothing to act on |
| "I don't have anything like that to forget." | Topic or day forgetting found no match |
| "Okay. I'll remember that for this session." | Session-only or guest mode. The fact is not kept |
| "Private conversations need my local language model." | Guest, session-only or explicit-only mode, and the model server is not on this machine, so open conversation is refused |
| "Use a time zone name, for example Australia slash Sydney." | The zone was not recognized. Say region and city |
| "Use a city name, for example Sydney, Australia." | The weather city was not usable |
| "I couldn't access my local settings or reminders." | A file on `/mind` could not be read or written |
| "Did you mean a.m. or p.m.? …" / "What time on that day?" / "That time has already passed." | Say the whole reminder again with a full, future time |
| "More than one reminder matches. Say the reminder number to cancel it." | Cancel by number |

### Private modes

In guest mode or `session-only` memory, only these commands work: time,
date, day, tomorrow, the memory commands, name, age, battery, sleep,
"What do you see?" and the guest commands. Anything else gets "I can tell you the time, date, or
battery level in a private conversation."

## Keys and touch

Sources: `crates/face/src/main.rs`, `os/package/being/being-console.c`

| Key | On the face | On a setup screen |
|---|---|---|
| **Esc** | Stop the current reply, including audio already playing | — |
| **M** | Mute or unmute the microphone | Typed as text |
| **F10** | Mute or unmute the microphone | Mute (for the **P** speaker preview) |
| **F9** | Recover the face when the lid reading is unknown | Same |
| **F2** | Open setup on an installed Pip, or the start menu in Try | — |
| **Click or tap** | Pip glances down; while the eyes rest it also listens, as for "Hey Pip" | — |
| **Space** or **Return** | While the eyes rest (idle, drowsy or asleep with the lid open) and no app panel has the keyboard: listen, as for "Hey Pip" (`key.attend`) | Typed as text |
| **Page Up / Page Down** | — | Scroll the history |
| **Number keys, Enter** | — | Choose menu items |

On the laptop Try stick, the GRUB menu (5 s timeout) offers three
entries:

- **Try PipOS**
- **show boot messages** (hotkey **v**)
- **safe graphics** (hotkey **s**, uses `nomodeset`)

## On-screen menus

Sources: `mind/local_menu.py`, `mind/setup.py`, `mind/initiative_setup.py`

### USB start menu (Try image)

| Key | Item |
|---|---|
| 1 | **Try PipOS**: meet the being. Nothing is kept |
| 2 | **Install PipOS**: choose a disk, review the layout, type `ERASE /dev/sdX` |
| 3 | **Restore a being**: 1 *with conversation archive* or 2 *identity and settings* |
| 4 | **Setup** |
| 5 | **Check hardware** |
| Q | Return to PipOS |

### Setup ("Make it yours"), from F2 or `being-setup`

| Key | Setting |
|---|---|
| 1 | Name (1–64 printable characters) |
| 2 | What it calls you |
| 3 | Wake phrase (must be in the PocketSphinx dictionary) |
| 4 | Voice: Flite `slt` (default), `rms`, `awb` or `kal16` |
| 5 | Speaking speed: slower, normal or faster (0.85 / 1.0 / 1.15) |
| 6 | Personality. Presets: *Warm and curious*, *Calm and concise*, *Playful and dry*, *Gentle and reflective*. You can also roll a random personality or tune each trait (warmth, curiosity, humour, expressiveness, detail) to 0, 1 or 2 |
| 7 | Surprise me: a random name, voice and personality |
| 8 | Network: Offline, Local network or Internet. Also covers web lookups and the time server (installed system only) |
| 9 | Quiet hours and announcements: 1 automatic reminders and timers, 2 daily greetings, 3 quiet-hours window, 4 clear quiet hours |
| P | Play a sample |
| K | Keep (asks to confirm) |
| Q | Cancel |

On an installed system, profile changes (keys 1–7) apply after a reboot.
Network and announcement changes (keys 8 and 9) apply at once. In Try,
everything applies immediately.

## Maintenance commands

These commands run as root on the device. Lab and QEMU images have a
root console. The laptop image has no console login in any build. An
image built with `BEING_LAPTOP_MAINTENANCE=1` adds key-only SSH from a
paired Mac (`being-lab-access`), and its root development login lasts at
most 30 minutes (see the
[user guide](USER-GUIDE.md#settings-you-cannot-reach-yet)).

| Command | Purpose |
|---|---|
| `being-setup [--show] [--dry-run] [--network] [--initiative]` | Personalization, network mode, announcements |
| `being-memory status` / `being-memory set persistent\|explicit-only\|session-only [--expiry-days N]` | Retention policy. `--expiry-days` (1–36500) works only with `persistent`. Nothing is expired while the clock is not trusted. Refused while a restore is unfinished. Takes effect when the mind next starts: `systemctl restart being-mind.service` |
| `being-camera enable\|disable\|status` | Camera permission (the root-owned 0600 marker `/mind/camera.enabled`). Must run as root. Starts or stops `being-eyes.service`. `status` shows both the permission and the service state |
| `being-dream-journal [--limit N]` | Read the overnight journal |
| `being-network status [--json]` / `being-network set MODE [--web-lookups] [--time-server IP]` | Network policy |
| `being-wifi configure IFACE SSID [--security wpa2\|wpa3] [--password-stdin] [--offline-mind PATH]` / `status` / `forget` / `scan [IFACE]` / `ethernet on\|off\|status` | Manage networking:<ul><li>save, show or remove the single Wi-Fi profile;</li><li>list nearby networks as JSON;</li><li>switch wired Ethernet (used in Internet mode).</li></ul>Without `--password-stdin` it asks for the password. `--offline-mind` saves to an installed mind partition mounted from a Linux live system. WPA2 or WPA3 personal only; no open or enterprise networks. Wi-Fi is used only outside Offline mode. Example: `printf '%s' "$PASS" \| being-wifi configure wlan0 "Home WiFi" --security wpa2 --password-stdin` |
| `being-backup keygen\|create\|export-deletions\|checkpoint\|restore` | Encrypted backup and restore (see the [user guide](USER-GUIDE.md#backing-up-pip)) |
| `being-installer [--check\|--list\|--plan DEV\|--install DEV] [--json]` | Hardware check, disk listing, layout preview, install |
| `being-hardware-report [--save]` | Print the hardware report. With `--save` it instead saves the summary and report (MAC addresses masked) to this boot's folder on the `PIPLOGS` partition of a laptop Try stick, and prints only where it saved. It exits 1 when no stick is in use |
| `being-usb-logs run\|report\|summary\|check` | Manage Try boot diagnostics on the `PIPLOGS` partition |
| `touch /mind/dreams-enabled` / `rm /mind/dreams-enabled` | Opt in to or out of overnight journal work |
| `rauc install BUNDLE.raucb` | Install a signed OS update to the inactive slot |

## Files in /mind

Sources: `README.md`, `mind/*.py`, `crates/brainstem/src/life.rs`

| File | Written by | Contents | In backups |
|---|---|---|---|
| `life.json` | brainstem | ID, birth, wake count, body, how the last wake ended | Yes |
| `profile.json` | setup | Name, owner, wake phrase, voice, rate, traits | Yes |
| `mind.db` | mind | Episodes, facts, FTS indexes, dream journal, deletion ledger, believed-clock state | Yes. The copy is compacted, dream tables are cleared and expired memory is dropped. Episodes are kept only in persistent mode, and facts are dropped in session-only mode |
| `memory-policy.json` | `being-memory` | Retention mode and expiry | Yes |
| `skills.json` | mind | Time zone and weather city | Time zone only |
| `reminders.db` | mind | Pending reminders and timers, greeting days | No |
| `initiative.json` | setup | Automatic reminders, daily greetings, quiet hours | No |
| `context.json` | mind | The conversation to resume within 24 h | No |
| `cortex/being.slot` | llama-server | Saved model KV cache | No |
| `core.md` | owner (by hand) | Optional "things it always knows". Persistent mode only | No |
| `network/policy.json`, `network/wifi.json` | `being-network`, `being-wifi` | Network mode, permissions, Wi-Fi credentials | No |
| `camera.enabled` | `being-camera` | Camera consent marker (root, 0600) | No |
| `dreams-enabled` | owner | Overnight-journal consent marker | No |
| `lab-access/` | `being-lab-access` | Paired-Mac maintenance keys (maintenance builds only) | No |
| `.being-restore-in-progress` | `being-backup restore` | Blocks the mind, voice and cortex until a restore completes | — |

On a development Mac, the mind directory is
`~/.local/share/being/mind`, unless `BEING_MIND` says otherwise.

## Factory configuration

Source: `config/`, installed to `/etc/being/`

| Key in `being.json` | Default | Notes |
|---|---|---|
| `name` | `Pip` | Overridden by `/mind/profile.json` |
| `wake_word` | `Hey Pip` | Overridden by the profile |
| `owner` | `Richard` | **Shipped in every image.** Change it before giving the image to anyone else |
| `cortex_url` | `http://127.0.0.1:8080` | The appliance unit passes `--cortex` explicitly |
| `ctx_tokens` | 4096 | |
| `max_tokens` | 96 | |
| `temperature` | 0.7 | The parrot-guard retry adds 0.3 |
| `prefill` | false | Read-ahead while listening (experimental) |

`identity.md` is the system prompt, with `{name}` and `{owner}`
placeholders. A non-default personality replaces its temperament
sentence.

## Services

Source: `os/package/being/*.service`, `os/package/cortex/being-cortex.service`

| Unit | Runs | Notes |
|---|---|---|
| `being-brainstem` | `being-brainstem --appliance-body …` | Early boot, `WatchdogSec=10s`, reboot after repeated failure. `CPUWeight=1000` |
| `being-face` | `being-face --fullscreen` (KMS) | Skipped while the USB menu is showing. `WatchdogSec=5s`, pinged every frame; shader cache in `/run/being/mesa`. `CPUWeight=1000` |
| `being-mind` | `python3 -m mind` | Waits for the brainstem only, never the cortex, so code answers work while the model loads. Blocked during a restore. `WatchdogSec=30s`, pinged from its event loop. `CPUWeight=1000` |
| `being-voice` | `python3 -m mind.voice` | Blocked during a restore and in the serial lab |
| `being-cortex` | `llama-server` | Gated by signed model-pack verification |
| `being-eyes` | `python3 -m mind.eyes` | Only when `/mind/camera.enabled` exists. Sandboxed |
| `being-boot-health` | `python3 -m mind.boot_health` | Confirms the A/B slot |
| `being-local-screen` | `being-console python3 -m mind.local_screen` | The on-panel menus. Conflicts with the face |
| `being-network-{lockdown,policy,reconcile}`, `being-wifi` | `being-network`, `being-wifi` | Firewall and links |
| `being-usb-logs` | `python3 -m mind.usb_logs run` | Try boots only |

The organ units read `/etc/being/env`: brainstem, face, mind, voice,
eyes, boot health and local screen. The cortex, network, Wi-Fi and
USB-log units do not.

```
BEING_BUS=/run/being/bus.sock
BEING_MIND=/mind
BEING_REQUIRE=face,mind,voice
```

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `BEING_BUS` | `$XDG_RUNTIME_DIR/being/bus.sock`, then `/tmp/being-$USER/bus.sock`. `being-setup` and the local screen default to `/run/being/bus.sock` | All organs, `being-setup`, local screen |
| `BEING_MIND` | The default depends on the organ:<ul><li>mind: `/mind` if writable, else `~/.local/share/being/mind`</li><li>eyes: `/mind`</li><li>brainstem: none. Without this or `--mind-dir`, it keeps no life record</li></ul> | brainstem, mind, eyes, voice, and tools that default to the mind directory (`being-memory`, `being-setup`, `being-dream-journal`) |
| `BEING_REQUIRE` | `face,mind,voice` (the `--require` default) | The brainstem unit, which passes it as `--require` |
| `BEING_CORTEX` | `cortex_url`, then `http://127.0.0.1:8080` | mind |
| `BEING_LOG` | off | brainstem (event trace to stderr) |
| `BEING_BODY`, `BEING_BOOT_ID` | DMI string and kernel boot ID | brainstem (testing identity and power-loss detection) |
| `BEING_ALSA_CAPTURE`, `BEING_ALSA_PLAYBACK` | `plughw:0,0` | voice |
| `BEING_VOICE_SHORTEN_FIRST_PAUSE` | off | voice |
| `BEING_CAMERA_DEVICE` | `/dev/video0` | eyes |
| `BEING_CONSOLE_UI` | off (`1` gives styled screens) | setup and menus |
| `BEING_SLOT_DEADLINE` | 180 | boot health |
| `BEING_LOCAL_SETUP` | set by the face unit | face (enables F2) |

## Kernel command line

| Argument | Effect |
|---|---|
| `being.try=1` | Try session: RAM `/mind`, updates off, USB logs on |
| `being.screen=1` | With `being.try=1` (Try-screen images), show the USB menu before the face. The face waits until the menu is dismissed |
| `being.trial=1` | Set by GRUB on an unconfirmed slot. A failed health check reboots |
| `being.installed=1 being.esp=… being.slot_a=… being.slot_b=… being.models=… being.mind=…` | Installed-disk partition binding |
| `being.verbose=1`, `being.verbose_hold=SECS` | Keep boot messages visible (default 20 s; 0 waits for a key) |
| `being.lid=open` | Treat a missing lid switch as open, after a 15 s grace |

## Lab and simulator commands

Sources: `lab/vm.py`, `lab/build.sh`, `scripts/run-sim.sh`, `sim/body.py`

| Command | Purpose |
|---|---|
| `lab/build.sh` (`build`) | Build `out/images/beingos.img` |
| `lab/build.sh model FILE.gguf` | Stage a model |
| `lab/build.sh try-image` / `try-screen` | USB Try images |
| `lab/build.sh laptop` / `laptop-try` | Laptop images: no serial, no root login |
| `lab/build.sh bundle` / `installed-bundle` / `test-media` | Signed updates and fixtures |
| `lab/build.sh candidate NAME` | Frozen, sealed release candidate under `out/candidates/NAME/` |
| `lab/build.sh up` / `down` / `destroy` | Colima builder lifecycle |
| `python3 lab/vm.py [--fresh] [--shell] [--sound] [--no-window] [--image P] [--update-media P] [--memory 4G] [--mic-input DEV]` | Run the image in QEMU |
| `lab/cortex-dev.sh start\|stop\|logs` | A real model server on `127.0.0.1:8377` |
| `scripts/run-sim.sh [--fake] [--cortex URL] [--model F] [--no-face] [--silent]` | All organs as Mac programs |
| `target/debug/face --demo` | Show every expression |

In the serial lab and the simulator, you can type these slash commands:
`/lid open|close`, `/battery N`, `/plug`, `/lux N`, `/temp N`, `/hour N`,
`/mute`, `/interrupt` and `/quit`.
