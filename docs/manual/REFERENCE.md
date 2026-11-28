<!--
doc: reference
verified: 2026-11-24T22:17
sources: mind/commands.py mind/reminder_language.py mind/turn.py mind/everyday.py mind/skills.py mind/profile.py mind/setup.py mind/care.py mind/local_menu.py mind/initiative_setup.py mind/retention.py mind/backup.py mind/dream_journal.py mind/network_enforce.py mind/network_policy.py mind/initiative.py mind/voice.py mind/voice_engines.py mind/mic_channel.py mind/mic_port.py mind/try_mode.py mind/tts.py mind/eyes.py mind/__main__.py crates/face/src/main.rs crates/face/src/keys.rs crates/brainstem/src/main.rs crates/brainstem/src/battery.rs crates/brainstem/src/kbd_light.rs mind/keys.py mind/local_screen.py os/board/beingos/slots.sh crates/bodycare/src/main.rs sim/body.py lab/vm.py lab/build.sh lab/cable.sh scripts/run-sim.sh os/package/being/being.env os/package/being/being-camera os/package/being/50-being.preset os/package/cortex/being-cortex.service os/package/cortex/being-dream-cortex.service os/board/beingos/try-grub.cfg os/board/beingos/try-image.sh config/being.json mind/wifi.py mind/reminders.py os/package/being/being-usb-logs os/package/being/being-hardware-report mind/diag.py mind/diag_report.py mind/diag_sound.py mind/diag_speech.py mind/diag_codec.py mind/diag_system.py os/package/being/pip-diag os/package/being/being-cable-link.service os/package/being/being-cable-ssh.service os/package/being/being-cable-workbench.service os/package/being/cable/sshd_config
-->

# PipOS reference

These are lookup tables for everything Pip understands, every key, command,
file and setting. They describe the current source. Each section names the
source file it comes from. When that file changes, check the table.

- [Spoken commands](#spoken-commands)
- [Keys and touch](#keys-and-touch)
- [On-screen menus](#on-screen-menus)
- [Maintenance commands](#maintenance-commands)
- [Diagnostics (pip-diag)](#diagnostics-pip-diag)
- [Voice debug and route lines](#voice-debug-and-route-lines)
- [Files in /mind](#files-in-mind)
- [Factory configuration](#factory-configuration)
- [Services](#services)
- [Environment variables](#environment-variables)
- [Kernel command line](#kernel-command-line)
- [Lab and simulator commands](#lab-and-simulator-commands)
- [Cable development build](#cable-development-build)

## Spoken commands

Sources: `mind/commands.py`, `mind/reminder_language.py`, `mind/turn.py`,
`mind/skills.py`, `mind/everyday.py`

Matching works like this:

- It is case-insensitive and needs the **whole utterance** to match.
- A leading or trailing "please" is ignored. So is an address such as
  "Pip," or "hey Pip".
- An utterance is routed in this order: guest mode on or off (which
  goes ahead even while a question is open); an answer to something Pip
  asked (a forget offer, a yes-or-no question, a ring); the workshop; the commands below; the everyday skills; greetings and the
  morning brief; then the language model.
- Commands are always answered from code, never by the model.
- In the 20 s follow-up window without Pip's name, commands that store or
  change something (remember, reminders and alarms, snooze, set the time,
  forget, correct, guest mode, sleep, volume, time zone, weather city) are
  ignored as room talk, unless the utterance is a question (it ends in a
  question mark or starts with a word such as "can", "could" or "what").
  Everyday skills and questions are still answered.

The everyday skills (`mind/everyday.py`, in this order: read-back, sums,
units, lists and notes, play, body talk, device, doctor, action log,
diary, voice engines) have their phrases in the user guide's
[Things you can say](USER-GUIDE.md#things-you-can-say).

### Clock and calendar

| Say | Pip replies |
|---|---|
| "What time is it?" / "What's the time?" / "Tell me the time" | "It's 3:05 in the afternoon." |
| "What's the date?" / "What's today's date?" / "What date is it?" | "It's Monday, November 23, 2026." |
| "What day is it?" / "What day is today?" | "It's Monday." |
| "What's tomorrow's date?" / "What day is tomorrow?" | "Tomorrow is …" |
| "What time is it in London?" | The time there |
| "What's the time difference to London?" / "Is London ahead or behind?" | The difference in hours |
| "Set the time to 7:30 pm" / "The time is 7:30" | Sets the system and hardware clocks; moves running timers with the clock. Refused to a guest |
| "Set my time zone to Australia Sydney" | Saves an IANA zone. Spoken forms such as "Australia slash Sydney" work |
| "Use the device time zone" | Saves an empty zone: UTC |

Until a zone is saved, the factory zone in `being.json` applies.

### Weather (needs Internet mode and web-lookup permission)

| Say | Behaviour |
|---|---|
| "What's the weather?" / "What's the weather in Sydney, Australia tomorrow?" / "Will it rain today?" / "Is it raining?" / "What's it like outside?" | Uses Open-Meteo, in Celsius. Reports current conditions, today or tomorrow |
| "Set my weather city to Sydney, Australia" | Saves the default city (allowed in any network mode) |
| "Clear my weather city" | Removes it |

When lookups are not allowed, Pip says: "Weather needs Internet mode and
permission for web lookups in setup."

### Reminders, alarms and timers

| Say | Behaviour |
|---|---|
| "Remind me to call Mum tomorrow at nine A.M." | One-off reminder. The time can be given as follows: <ul><li>"in N minutes / hours / days"</li><li>"at 7:30 pm", "noon" or "midnight"</li><li>a day: "today", "tonight", "tomorrow", "(next) Friday" or "2026-12-01". A day word also needs a full time, as in "next Friday at 9 am". With "tonight" an hour from 1 to 11 is the evening and 12 is midnight</li></ul> |
| "Remind me in ten minutes to check the oven" | Relative reminder: counts real seconds, like a timer |
| "Every weekday at 7, remind me to …" / "Every Tuesday at 8 pm, remind me to …" / "Every day at …" | Repeating reminder (`daily`, `weekdays` or `weekly:<day>`) |
| "Wake me up at 7" / "Set an alarm for 6:30 tomorrow" / "Set an alarm for ten minutes" | Alarm. Rings in quiet hours |
| "Set a timer for five minutes" / "Start a 10 minute timer" / "How long left on my timer?" | Countdown |
| "What are my reminders?" / "List my timers" | Reads up to five pending items with their numbers |
| "Cancel reminder one" / "Cancel the timer" / "Delete the reminder to call Mum" | Removes an item by number or by task |
| "Snooze" / "Snooze for 5 minutes" / "Five more minutes" | Puts off what is ringing (10 minutes by default, at most 120) |
| "Okay" / "Thanks" / "Got it" / "Stop the alarm" / "I'm up" | Stops what is ringing. Bare "okay" and "thanks" count only while something rings |

Pip asks for clarification in some cases:

- A bare hour such as "at nine" (without "tonight") gets "Did you mean
  a.m. or p.m.?"
- A time in the past gets "That time has already passed."

When a timer finishes, a bell plays and Pip says, for example, "Sam, your
ten-minute timer is done." (with the owner's name). When a reminder is
due, it says "You asked me to remind you: *task*." An alarm says the time
("Good morning. It's 6:30 in the morning. Time to get up."). A ring is
said in 20 words or fewer, is honest when it is late, and repeats up to
three times, two minutes apart, until you answer.

While the clock is doubted (reset, or gone backwards), only timers and
delays ring. A clock-time reminder set then is held and worked out again
when the clock is set.

### Memory

| Say | Behaviour |
|---|---|
| "Remember that my orchid is called Silverleaf" / "Don't forget that …" | Stores a fact. "Okay. I'll remember that." |
| "What do you remember about my orchid?" / "Do you remember what I said about …?" | Searches facts first ("You told me: …"). Then it searches the conversation archive and quotes the single best match with its date ("On November 20, 2026, you said: …") |
| "What did I say about the café?" / "What have I told you about …?" | Searches only what you said |
| "What did you say about …?" | Searches only what Pip said |
| "What did we talk about last Friday?" / "… on 2026-11-20" | Searches a single day in your time zone |
| "What did we talk about yesterday?" / "What did we do on Monday?" | Answered from the day record the night shift writes |
| "What did we talk about?" / "What have we done today?" | Today's conversation |
| "What do you remember about me?" / "Do you remember me?" | What it keeps about the owner |
| "That's wrong, I called my orchid Silverleaf" | Replaces the last recalled or remembered item. Give the **full corrected wording** |
| "Forget that" | Forgets the utterance just heard, or the item just recalled or remembered, and any reminder holding those words |
| "Forget about my sister's birthday" / "Forget everything from yesterday" | Pip says how many items match, then waits up to 45 s for **"yes"**. Saying "no", "never mind" or "forget it" keeps everything. Reminders holding the forgotten words are cancelled too |
| "Forget everything" | Owner only, not in guest mode. Waits 15 s for a plain yes. Erases every fact and archived sentence, the day records, lists and notes, the jokes record, the action log, reminders, alarms, timers and greeted days, and the model's cache |
| "Never mind" / "Forget it" | Nothing is deleted |
| "Start guest mode" / "Don't remember this" | Starts a private RAM conversation. Your archive, private core, reminders and network requests are unavailable |
| "End guest mode" | Clears the guest conversation and returns to normal |

**What counts as yes.** For a forget asked for by name: "yes", "yeah",
"yep", "yup", "sure", "okay", "alright", "absolutely", "correct", "do it",
"go ahead", "confirm", "I'm sure", with an optional "please". A plain yes
only ("yes", "yeah", "yep", "yup", "do it", "go ahead", "confirm", "I'm
sure") for "forget everything", switching off or restarting, clearing a
list or the notes, and for any answer heard in the follow-up window
without Pip's name.

### About Pip

| Say | Pip replies |
|---|---|
| "What's your name?" | "My name is Pip." |
| "How old are you?" / "When were you born?" | Its age and wake count from the life record |
| "How is your battery?" / "How much charge do you have?" / "How long will your battery last?" | Its battery percentage, charging or plugged-in state, and time left when known |
| "What can you do?" / "What else can you do?" / "What can you do with lists?" / "Help" | A short tour, or one group of skills |
| "Go to sleep" / "Good night" | "Okay. Good night." Between 5 a.m. and 6 p.m. it says "Okay. I'll rest my eyes for a while." instead. Pip then sleeps until you say the wake phrase (not while muted) or open the lid |
| "Are you alive?" / "Are you sentient?" / "Do you have feelings?" / "What are you?" | A fixed, honest answer from code |
| "Can you see me?" / "What do you see?" / "Look at this" | Pip can report a recent face detection while the camera is allowed. Face identification and object descriptions are pending. The reply is private. |
| "What did you dream about?" | From its dream journal only |
| "Speak up" / "Be quieter" / "Set your volume to 5" | Volume, through the voice organ |

### Greetings (local replies, no model)

Pip answers short greetings, check-ins and presence questions locally:

- greetings: "Hi", "Hello there", "Good morning"
- check-ins: "How are you?", "What's up?"
- presence: "Are you there?", "Can you hear me?"

The replies rotate with the chosen personality. A greeting with a topic
goes to the model. The first addressed words of the owner's day also get
the morning brief, unless Setup **9**, **5** turned it off.

### Replies when something goes wrong

| Pip says | Meaning |
|---|---|
| "I can't think right now. My language model isn't answering." | The model server is down or loading. Direct commands still work |
| "Sorry. I lost my thought." | The model produced nothing usable, or failed before saying anything. A reply that only repeats the question, or opens with assistant boilerplate, is retried first |
| "Sorry, I didn't catch that." | The voice heard speech after the wake but made out no words |
| "I can't read my battery level right now." | The battery reading is unknown |
| "I don't remember anything about that." | Recall found no fact or archive match |
| "I'm not sure which memory you mean." | "That's wrong, …" had no recent memory to correct |
| "I'm not sure what to forget. Tell me what it was about." | "Forget that" had nothing to act on |
| "I don't have anything like that to forget." | Topic or day forgetting found no match |
| "I can only forget a topic or a single day, like yesterday." | The forget request had an "except" or an open date range |
| "Okay. I'll remember that for this session." | Session-only or guest mode. The fact is not kept |
| "Private conversations need my local language model." | Guest, session-only or explicit-only mode, and the model server is not on this machine, so open conversation is refused |
| "Use a time zone name, for example Australia slash Sydney." | The zone was not recognized. Say region and city |
| "Use a city name, for example Sydney, Australia." | The weather city was not usable |
| "I couldn't access my local settings or reminders." | A file on `/mind` could not be read or written |
| "Did you mean a.m. or p.m.? …" / "What time on that day?" / "That time has already passed." | Say the whole reminder again with a full, future time |
| "More than one reminder matches. Say the reminder number to cancel it." | Cancel by number |
| "Only the person I live with can set my clock." | A guest asked to set the time |

### Private modes

In guest mode or `session-only` memory, these commands still work: the
memory commands (on the private conversation), name, age, battery, the
self questions, volume, sleep, "What do you see?", dreams, the clock and
calendar, the time elsewhere and "what can you do". Reminders, weather,
the time zone and the weather city get "I can tell you the time, date, or
battery level in a private conversation." A guest cannot set the clock.

## Keys and touch

Sources: `crates/face/src/main.rs`, `crates/face/src/keys.rs`, `mind/keys.py`,
`crates/brainstem/src/kbd_light.rs`, `os/package/being/being-console.c`,
`mind/local_screen.py`, `mind/voice.py`

The laptop boots with `hid_apple.fnmode=1`, as a Mac does: the top row does
what is printed on it, and **fn** with a top-row key sends F1 to F12. Pip's
own function keys (F1, F2, F9, F10) therefore need **fn** on the MacBook; an
external keyboard without media keys sends them as before.

| Key | On the face | On a setup screen |
|---|---|---|
| **Esc** | Stop the current reply, including audio already playing (`key.interrupt`). Pip remembers only the part that played | Leaves the typing chat |
| **M** | Mute or unmute the microphone (`key.mute`). A letter while the terminal has the keyboard | Typed as text |
| **fn+F10** | Mute or unmute the microphone | Mute (for the **P** speaker preview) |
| **fn+F1** | Show or hide captions and the route badge. Off by default and never saved | — |
| **Shift+fn+F1** | Show or hide the hardware and service health overlay | — |
| **fn+F9** | Recover the face when the lid reading is unknown (`key.panel_recover`) | Same |
| **fn+F2**, or **Mission Control** (F3) | Open setup on an installed Pip, or the start menu in Try | — |
| **fn+F3** | While the workshop terminal has the keyboard: put it away (hands' control word `\0f3`) | — |
| **Brightness** (F1, F2) | The panel, about a sixteenth a press, never below 5 % (`key.brightness`, then `being.brightness` with `by: 6` and `req: "key"`, which `body.brightness` carries back so a spoken "brighter" never takes a key's answer). Held, it keeps stepping | Same, without the bar (see below) |
| **Launchpad** (F4) | Open the workshop terminal; pressed again, put it away, then show it again (`key.apps`). Nothing is said; a refusal (a guest, no hands) is logged | — |
| **Keyboard light** (F5, F6) | The keyboard's backlight (applesmc `smc::kbd_backlight`) in sixteenths (`key.kbd_light` with `dir` `down` or `up`, then `being.kbd_light`). A keyboard's own light toggle key sends `dir: "toggle"`: off, and back on at the level it had. It goes out while the panel is dark and comes back with it | Same, without the bar |
| **Play/pause** (F8) | While the eyes rest and no app panel has the keyboard: listen (`key.attend`). Otherwise: stop the reply (`key.interrupt`) | — |
| **Previous**, **Next** (F7, F9), **Eject** | Nothing; the face logs the press | Nothing |
| **Mute** (F10) | The speaker off, and on again at the level it had (`key.mute_speaker`). The microphone's mute is **M** or **fn+F10**. A key mute is for now only: `voice.volume` goes with `level: 0, persist: false`, which the voice applies without saving it or storing the sound card's state; the mind keeps the level from before it, the face keeps a faint "speaker muted" up, and a ring, a battery plea or a restart of the mind brings the sound back (`mind/keys.py`). A restart of the voice ends a key mute: it plays at the saved level and reports it (`voice.volume_state`), which clears the note and the mind's mute. A stored level under 10 never starts the voice quieter than 10; that level is saved and reported, and reported again when the mind connects | Same, without the note |
| **Volume** (F11, F12) | The speaker in sixteenths (0, 6, 12 … 94, 100) (`key.volume`, then `voice.volume` with `feedback: true`). Each press clicks once at the new level, as a Mac does: a 30 ms tick that replaces one still playing, under the chirp rules (chirps on, lid open, microphone not muted, not while Pip speaks or in quiet hours) and never while the microphone is open for your words. The lowest step is silence, a key mute like the mute key's, and makes no click. Held, it keeps stepping | Same, without the bar |
| **Click or tap** | Pip glances down (`key.touch`); while the eyes rest it also listens, as for "Hey Pip" | — |
| **Space** or **Return** | While the eyes rest (idle, drowsy or asleep with the lid open, not muted) and no app panel has the keyboard: listen, as for "Hey Pip" (`key.attend`) | Typed as text |
| Other keys | Typed into the workshop terminal while it has the keyboard (never the top row's printed keys) | — |
| **Page Up / Page Down** | While the workshop terminal has the keyboard: scroll its history (`\0pgup`, `\0pgdn`) | Scroll the history |
| **Number keys, Enter** | — | Choose menu items |

After a brightness, volume or keyboard-light change the face shows a bar
with the new level for 1.5 s, drawn from what the brainstem and the voice
report they set (`body.brightness`, `voice.volume_state`, `body.kbd_light`),
so a voice command ("louder", "dimmer") shows it too. On a setup screen a
key leaves a request in the screen gate (`brightness-up`, `brightness-down`,
`kbd-light-up`, `kbd-light-down`, `kbd-light-toggle`, `volume-up`,
`volume-down`, `speaker-mute`, and `mute` and `recover` for fn+F10 and
fn+F9), and `mind/local_screen.py` sends each on as the face's key event
(`key.brightness {dir}`, `key.kbd_light {dir}`, `key.volume {dir}`,
`key.mute_speaker`, `key.mute`, `key.panel_recover`), so they act as they do
on the face. A held level key steps at most ten times a second there. The
setup screens draw no level bar and no "speaker muted" note; the volume
click still plays. Where the top row is printed-first (a keyboard bound to hid-apple
with `fnmode` 1, or 3 for an Apple keyboard), the start menu's, setup's and
terminal's footers show Pip's keys as **fn F10**, **fn F9** and **fn F3**;
other keyboards see plain F-keys. A verbose boot's wait also ends on
Mission Control. SDL has no code for Mission
Control and Launchpad, so the face reads those two from the keyboard's
event device itself.

On the laptop Try stick, the GRUB menu (5 s timeout) offers these entries
(the first stick, of 23 November, has only the first three):

| Entry | Hotkey | Adds to the kernel command line |
|---|---|---|
| **Try PipOS** | — | — |
| **show boot messages** | **v** | `being.verbose=1`, kernel and systemd text, no panic reboot |
| **safe graphics** | **s** | `nomodeset` |
| **alternative microphone** | **m** | `snd_hda_intel.model=mbp81 being.mic_channel=auto` |
| **alternative microphone 2** | **n** | `snd_hda_intel.model=mbp101` |
| **measure speed** | **b** | `being.bench=1` |
| **measure boot and idle** | **i** | `being.bench=2` |

The ISO and the [cable development stick](#cable-development-build) both
include these seven entries. The cable entries also add `being.cable=1 being.voice_debug=1`
(`os/board/beingos/try-image.sh`).

## On-screen menus

Sources: `mind/local_menu.py`, `mind/setup.py`, `mind/care.py`,
`mind/initiative_setup.py`

### USB start menu (Try image)

| Key | Item |
|---|---|
| 1 | **Try PipOS**: meet the being. Nothing is kept |
| 2 | **Install PipOS**: choose a disk, review the layout, type `ERASE /dev/sdX` |
| 3 | **Restore a being**: 1 *with conversation archive* or 2 *identity and settings* |
| 4 | **Setup** |
| 5 | **Check hardware**. **G** there runs the guided key, screen and camera checks |
| 6 | **Talk to Pip by typing** |
| 7 | **Back up to a USB stick**: this Try being |
| 8 | **Run diagnostics**: 1 *Quick* (about a minute, nothing to do), 2 *Full* (10 to 15 minutes, with test sounds) or 3 *Voice* (you speak and listen, about 10 minutes); **Q** goes back. It runs `pip-diag quick`, `full` or `voice` on the screen, then shows the top of `diag.txt` (at most 28 lines) and the folder it was saved in. Ctrl-C ends the run, and the run itself puts the voice back. See [Diagnostics (pip-diag)](#diagnostics-pip-diag) |
| Q | Return to PipOS |

Left for 240 s, the menu returns to the eyes.

### Setup ("Make it yours"), from F2 or `being-setup`

| Key | Setting |
|---|---|
| 1 | Name (1–64 printable characters) |
| 2 | What it calls you |
| 3 | Wake phrase (must be in the PocketSphinx dictionary) |
| 4 | Original voice: Flite `slt` (default), `rms`, `awb` or `kal16` |
| 5 | Speaking speed: slower, normal or faster (0.85 / 1.0 / 1.15) |
| 6 | Personality. Presets: *Warm and curious*, *Calm and concise*, *Playful and dry*, *Gentle and reflective*. You can also roll a random personality or tune each trait (warmth, curiosity, humour, expressiveness, detail) to 0, 1 or 2 |
| 7 | Surprise me: a random name, voice and personality |
| 8 | Network: Offline, Local network or Internet. Also covers web lookups and the time server (installed system only) |
| 9 | Quiet hours and announcements: 1 automatic reminders and timers, 2 daily greetings, 3 quiet-hours window, 4 clear quiet hours, 5 morning brief, K save, Q discard |
| N | Natural or original voice: 1 automatic, 2 natural (Piper), 3 original (Flite); N and O play samples, M measures the natural voice |
| C | Care: senses and memory (below). On the appliance only |
| A | Volume and microphone: louder, quieter, set volume, test sound, microphone gain and a 4 s level meter. On the appliance only |
| T | Time zone. On the appliance only |
| B | Back up to a USB stick. Installed system only |
| U | Update from a USB stick. Installed system only |
| Y | Talk to Pip by typing. Installed system only |
| P | Play a sample |
| K | Keep (asks to confirm) |
| Q | Cancel |

On an installed system, profile changes (keys 1–7) apply after a reboot.
Network and announcement changes apply at once. Care's memory mode and
time zone restart the mind. In Try, everything applies immediately.

### Care ("Senses and memory")

| Key | Page |
|---|---|
| 1 | Camera on or off (runs `being-camera`) |
| 2 | Dreams: 1 at night only (lid closed, on power, midnight to 5 a.m.), 2 whenever the lid is closed on power, 3 stop |
| 3 | Memory: persistent, explicit-only, session-only, or 4 forget conversations after N days |
| 4 | What I remember: the newest 40 facts; forget one by number |
| 5 | Dream journal: the newest 12 entries |
| 6 | Date and time: set the clock (also the hardware clock), or choose the zone |
| 7 | Time zone |
| 8 | Workshop on or off |
| 9 | Network: join Wi-Fi (scan or hidden), wired Ethernet on or off, forget Wi-Fi. Installed system only |
| P | Permissions: view the owner settings for camera, network, workshop, announcements and dreams |
| D | Boot diagnostics on the Try stick on or off. Try only |
| H | Health: the doctor's check-up |
| L | Offline library. View sources, licences and credits, or change consent for an installed pack |

## Maintenance commands

These commands run as root on the device. Lab and QEMU images have a
root console. The laptop image has no console login in any build. An
image built with `BEING_LAPTOP_MAINTENANCE=1` adds key-only SSH from a
paired Mac (`being-lab-access`), and its root development login lasts at
most 30 minutes (see the
[user guide](USER-GUIDE.md#maintenance-commands)). Most of these
controls are also in Setup and Care.

| Command | Purpose |
|---|---|
| `being-setup [--show\|--dry-run\|--network\|--initiative\|--care\|--audio\|--time-zone\|--backup\|--update\|--voice]` | Personalization, or one of its pages directly. `--care`, `--audio`, `--time-zone`, `--backup` and `--update` work on the appliance only |
| `being-memory status` / `being-memory set persistent\|explicit-only\|session-only [--expiry-days N]` | Retention policy. `--expiry-days` (1–36500) works only with `persistent`. Nothing is expired while the clock is not trusted. Refused while a restore is unfinished. Takes effect when the mind next starts: `systemctl restart being-mind.service` |
| `being-camera enable\|disable\|status` | Camera permission (the root-owned 0600 marker `/mind/camera.enabled`). Must run as root. Starts or stops `being-eyes.service`. `status` shows both the permission and the service state |
| `being-dream-journal [--limit N]` | Read the overnight journal |
| `being-network status [--json]` / `being-network set MODE [--web-lookups] [--time-server IP]` | Network policy |
| `being-wifi configure IFACE SSID [--security wpa2\|wpa3] [--password-stdin] [--offline-mind PATH]` / `status` / `forget` / `scan [IFACE]` / `ethernet on\|off\|status` | Manage networking:<ul><li>save, show or remove the single Wi-Fi profile;</li><li>list nearby networks as JSON;</li><li>switch wired Ethernet (used in Internet mode).</li></ul>Without `--password-stdin` it asks for the password. `--offline-mind` saves to an installed mind partition mounted from a Linux live system. WPA2 or WPA3 personal only; no open or enterprise networks. Wi-Fi is used only outside Offline mode. Example: `printf '%s' "$PASS" \| being-wifi configure wlan0 "Home WiFi" --security wpa2 --password-stdin` |
| `being-backup keygen --identity F` / `create\|export-deletions --mind-dir D --output F --recipient R` / `checkpoint --mind-dir D` / `restore --mind-dir D --input F --identity F [--deletions F --current-checkpoint HEX]` | Encrypted backup and restore (see the [user guide](USER-GUIDE.md#backing-up-pip)) |
| `being-installer [--check\|--list\|--plan DEV\|--install DEV] [--json]` | Hardware check, disk listing, layout preview, install |
| `being-hardware-report [--save]` | Print the hardware report. With `--save` it instead saves the summary and report (MAC addresses masked) to this boot's folder on the `PIPLOGS` partition of a laptop Try stick, and prints only where it saved. It exits 1 when no stick is in use |
| `being-usb-logs run\|report\|summary\|check\|mic-test` | Manage Try boot diagnostics on the `PIPLOGS` partition; `mic-test` measures the microphone level once (numbers only). Each boot folder also gets the full codec dumps (`card0-codec0.txt` and so on, read with the processing coefficients shown), and `summary.txt` gains the lines **Codec IDs**, **HDA model**, **Cable link** and **Voice debug** |
| `pip-diag COMMAND [options]` | Measure hearing, voice, commands and body, and say what is wrong (see [Diagnostics (pip-diag)](#diagnostics-pip-diag)). On every laptop and lab image |
| `being-network cable-up` | Bring up the cable development link. Run by `being-cable-link.service`; refused unless the image carries the cable key, the boot is a Try boot and `being.cable=0` is absent. `being-network status` then names the cable port and both addresses |
| `being-bodycare [--once] [--mode act] [--charge-limit N] [--restore]` | The power and thermal keeper (see `being-bodycare --help`). The unit runs it in observe mode: it decides and reports, and writes nothing |
| `python3 /usr/lib/being/mind/model_pack.py verify [--role ROLE]` | Check a signed model pack |
| `rauc install BUNDLE.raucb` | Install a signed OS update to the inactive slot (Setup **U** does the same) |

## Diagnostics (pip-diag)

Sources: `mind/diag.py`, `mind/diag_report.py`, `mind/diag_sound.py`,
`mind/diag_speech.py`, `mind/diag_codec.py`, `mind/diag_system.py`,
`os/package/being/pip-diag`, `mind/local_menu.py`

`pip-diag` measures Pip's hearing, voice, spoken commands and body, and
says in plain English what is wrong. It is `/usr/sbin/pip-diag` on every
laptop and lab image, a shell wrapper that runs `python3 -m mind.diag`.
Run it as root over the [cable link](#cable-development-build), or from
the Try menu's item **8** (Quick, Full or Voice).

| Command | Needs root | What it does |
|---|---|---|
| `quick` | No | About a minute, automatic. The build, clock, units, a module import check, the bus and boot timing; the codec dump; every command phrase through the mind's own routing, and the workshop state; two model prompts; heat (5 s), battery, firewall and storage; and a read-only wired check of the bound driver (`tg3` on the MacBookPro9,2), carrier, speed/duplex, addresses and networkd setup/address state; then the mixer settings. Offline or owner-disabled Ethernet is reported as intentional. When Internet mode and wired Ethernet are both selected, it distinguishes missing driver, missing carrier and linked-without-address, and points only to the ordinary F2 → C → 9 setting, cable/router port or router DHCP. It never raises a link, reloads networkd or changes network policy. As root with a sound card, it also records the room floor and plays one test tone. Last, each voice's speed and the words decoded from it |
| `full` | Yes | 10 to 15 minutes, automatic. All of `quick` with every model prompt and 60 s of heat under load, plus the microphone sweep, the speaker battery, its timing, the acoustic loopback and speaker isolation, every voice scored before and after the speaker, the recognizer's self-test, the engines' speed check and, when memory allows a second model server, `bench --quick`. On a voice-debug boot it also says three phrases to the running voice (as `commands --e2e`) |
| `voice` | Yes | Interactive, with you there, about 10 minutes. It asks whether to keep recordings, then: your speech at a few microphone gain settings; command phrases for you to say, decoded by each engine (`--phrases N`, default 10, is held between 5 and 40, but no more than the 16 phrases in the command table are said, so a run says 5 to 16: the five core phrases first, then the others in table order); each voice played for a 1 to 5 rating, and a channel test; wake trials at 1 m (`--trials`, default 10; `--two-metres` adds 2 m); and 60 s of talk without the name, to count false wakes. Refused with `--json` |
| `commands` | No (`--e2e` needs root) | The command phrases through the mind's routing, in this process, and the workshop state. `--e2e` also renders three phrases with Flite ("Hey Pip, open a terminal.", "Hey Pip, what time is it?", "Hey Pip, close the terminal.") and says each to the running voice through the diagnostic microphone. On a voice-debug boot, as root, `--e2e` is the default |
| `workload` | Journal access | Up to 512 route entries from this boot and Linux CPU stalls for brainstem, mind, voice and face. Private and overheard turns are excluded. If the existing voice-debug journal includes words, the report estimates how many model fallbacks looked like commands. `diag.json` contains counts and pressure readings. |
| `continuity` | Archive read access | Validates the current life record and reads the archive's SQLite integrity and format. It checks the conversation and workshop slots against the configured context size through the local cortex API. The report contains health results and format numbers. |
| `mic` | Yes | The mixer, the room floor per channel (a dead channel is named) and the Mic Boost × Capture sweep |
| `speaker` | Yes | The test-signal battery through `plughw:0,0` and `hw:0,0` (rate, underruns, the `aplay -v` chain), small, default and large periods, the acoustic loopback (level, latency, distortion, octave bands, dropouts) and speaker isolation |
| `tts` | Yes | Every voice: its speed, and the words PocketSphinx decodes from it before and after the speaker |
| `codec` | Yes | The codec dump, its subsystem id, the fixup the driver picked and the codec settings. `--model NAME` (`apple`, `mbp81`, `mbp101`, `mbp53`, `mbp55`, `imac27`, `imac27_122`, `mba42`, `mba6`) switches the fixup live, then sets the mixer again with `being-audio-init --defaults`; `--coef HEX` (16 bits, such as `0x102a` or `0x000a`) writes coefficient 0x02 of node 0x11. Either one then re-reads the ADC setting and records the room floor. Live changes need the cable build's kernel (hwdep and reconfiguration); elsewhere they report skip |
| `hear FILE` or `hear --phrase TEXT` | Yes | Plays a WAV (any rate; the louder channel is used), or renders TEXT on Pip (`--voice slt\|kal16\|piper`, default `slt`), to the running voice through the diagnostic microphone, then reads from the journal the wake, the `heard:` line and its delay, the mind's route and whether the terminal opened. `--asr pocketsphinx\|moonshine\|auto` forces the recognizer for the run; `--expect-route ROUTE` (such as `workshop.open`) names the route to expect, which is otherwise predicted from the phrase; `--patience N` (1 to 20) multiplies every wait, for an emulated CPU. The terminal counts as opened only if it is still up a few seconds later and hands did not log that it failed to start |
| `engines` | No | Whether Moonshine and the natural voice are installed and verified, the CPU's AVX and AVX2 flags, and `mind.voice_bench` |
| `restore` | Yes | Put the voice and the mixer back after an interrupted run (below) |
| `report [FOLDER]` | No | Print `diag.txt` of the latest run, or of FOLDER; with `--json`, its checks one per line |

Options every command takes:

| Option | Effect |
|---|---|
| `--json` | One JSON line per check on stdout, and nothing else |
| `--now UNIX` | The Mac's time (`date -u +%s`), to measure clock drift |
| `--out DIR` | Write the report to DIR |
| `--yes-keep-recordings` | Keep recordings of the room and of you, and their transcripts (default: RAM only, removed) |
| `--stimulus-level DBFS` | Test signal level, −40 to −3 dBFS (default −20) |
| `--no-unit` | Run audio work in this process, not under `systemd-run` |

`restore` also takes `--quiet` (say nothing on stdout; what it did goes to
stderr, and nothing at all when there was nothing to put back) and
`--force` (restore even while another run holds the sound card).

**Exit codes.** 0 when no check failed, 1 when any check failed, 2 when
the run could not happen: a bad option, a command that needs root, another
run already using the sound card, an interrupted run, or a fault in
`pip-diag` itself. `report` exits 0 or 1 from the saved checks, and 2 when
there is no report. `restore` exits 0 when it puts things back and
also when another live run holds the lock (it then does nothing). It exits
2 when not run as root, or when the lock file cannot be opened and
`--force` is not given.

**Output.** Each run writes to `<PIPLOGS boot folder>/diag-<UTC>/` when
this boot's logs are on a stick, else to `/run/being-diag/<UTC>/` (RAM), or
to `--out`. The UTC stamp looks like `20261124-104500Z`. The folder holds:

- `diag.json`: `kind: "pipos-diag-v1"`, the command, start and end times,
  boot and build ids, the boot entry's options, the clock, and every check
  as `{id, section, status, value, evidence, hint}` with a status of
  `pass`, `warn`, `fail` or `skip`;
- `diag.txt`: the top problems first (at most ten), a verdict in plain
  English for six areas (the microphone, the speakers, Pip's spoken voice,
  the wake word, spoken commands, opening the terminal by voice), then
  every check;
- the evidence: WAVs, dumps and measurements.

A run keeps at most 400 evidence files and 200 MB, and leaves 48 MB free on
the stick; what does not fit is made in RAM, removed at the end, and noted
in `diag.json` as `files_not_kept`. `/run/being-diag/last-run.json` names the
latest finished run, and `/run/being-diag/active.json` exists while one is
going.

**Privacy.** Kept without asking: the generated test signals, Pip's
rendered voice, and the microphone's recordings made while a test sound or
Pip's voice plays (`loop-*.wav`, the gain sweep's `*-tone.wav` and
`tts-*-room.wav`). Those recordings also pick up whatever else is in the
room. The silent room-floor recordings, the recordings of your speech in
`voice`, and transcripts of what you said are made in RAM
(`/run/being-diag/tmp`) and removed, unless you pass
`--yes-keep-recordings` or answer yes when `voice` asks.

**The voice and the mixer.** The card's `hw:0,0` is exclusive, so a
section that opens it sets the voice aside first: it writes the marker
`/run/being-diag/voice-stopped`, saves the mixer (`alsactl store` to
`/run/being-diag/mixer.state`) and stops `being-voice`. `hear` and
`--e2e` instead restart the voice with the runtime drop-in
`/run/systemd/system/being-voice.service.d/50-diag-mic.conf`, which sets
`BEING_DIAG_MIC=/run/being-diag/mic.sock` and `BEING_VOICE_DEBUG=1` (and
`BEING_ASR` for `--asr`). On the way out, normally, on an error, or on
SIGINT, SIGTERM or SIGHUP (a dropped SSH session), it removes the drop-in
and reloads systemd, puts the mixer back, restarts the voice (or stops it,
if the run started a voice that was not running) and removes the marker,
the mixer state and the socket. Each section removes its own RAM
recordings (the `pip-diag-owner-*` and `pip-diag-*` folders under
`/run/being-diag/tmp`) as it finishes. `restore` removes only room-floor
files named after a process that is no longer alive, so after a run killed
outright (SIGKILL) those folders stay in RAM until power-off.

As root, an audio run (and `quick` or `commands`) goes under a transient
unit, `pip-diag-audio`, run by `systemd-run` with
`ExecStopPost=/usr/sbin/pip-diag restore --quiet`, so the voice comes back
even if the run is killed outright. Only one run at a time uses the sound
card (`/run/being-diag/lock`). `pip-diag restore` takes that lock first:
while another run holds it, it says so and does nothing, because that run
puts everything back itself; `--force` restores anyway.

## Voice debug and route lines

Sources: `mind/voice.py`, `mind/turn.py`, `mind/try_mode.py`,
`mind/mic_port.py`, `mind/usb_logs.py`

Voice debugging writes what Pip heard into the journal (and so into a Try
stick's `journal.txt`). It is on when the kernel command line of a **Try**
boot has `being.voice_debug=1` (as with any repeated argument, the last
`being.voice_debug=` counts, so `being.voice_debug=0` typed at the GRUB
prompt turns it off), or when the voice's environment has
`BEING_VOICE_DEBUG=1`. An installed system's command line alone never turns
it on. Every entry of a cable development stick turns it on.

| Line | Organ | When |
|---|---|---|
| `wake phrase …, transcript text in the log on (from the kernel command line)` | voice | At start, with voice debugging on (`off` otherwise) |
| `heard: "<text>" (engine E, confidence C, log-posterior L)` | voice | Each final transcript, with voice debugging on. E is `pocketsphinx`, `pocketsphinx grammar` (the command grammar's second pass gave it) or `moonshine` |
| `dictation ended after S s: final transcript of N characters, confidence C, log-posterior L, …; voiced V s, …` | voice | Each final transcript, always; never the words |
| `command grammar second pass: matched a command of N words (margin M per frame), …` or `… no match (why); keeping the open-vocabulary transcript` | voice | When a short dictation was not a workshop command and was read again with the command grammar |
| `playback {"wav_s": …, "rate": …, "wall_s": …}` | voice | Each spoken clause, always; a warning when it played far shorter or longer than its length |
| `route {"route": "workshop.open", "chars": 15}` | mind | Each turn, always. With voice debugging on, a `"text"` field holds the words |
| `route {"route": "private"}` | mind | Each turn while a guest is here, or while memory is `session-only` or `explicit-only` (any mode but `persistent`), whatever the debug setting |

In guest mode (`app.privacy` with `guest: true`, from the mind) the voice
keeps transcript text out of the log even with voice debugging on.

The route names: `workshop.KIND`, `command.KIND`, `everyday.SKILL` (the
module that answered), `greeting`, `model`, `model.unavailable`,
`model.waiting` (the conversation cache could not be cleared), `pending`,
`overheard`, `discarded`, and `private`.

`summary.txt` on the stick says whether each is on in its **Cable link**
and **Voice debug** lines, and where voice debugging came from (the kernel
command line, `/etc/being/env`, a unit drop-in, or transcripts seen in the
journal). On a cable boot its first line also ends "(CABLE DEVELOPMENT
BOOT)".

**The diagnostic microphone.** When the voice starts with
`BEING_DIAG_MIC=PATH`, it does not open ALSA capture. It connects to the
Unix stream socket PATH, trying every 0.2 s for up to 10 s (then it logs
that and uses the normal microphone), and reads raw S16_LE 16 kHz mono
audio, never faster than real time, through the same gain, wake, endpoint
and recognition path as the microphone, mixed to one channel. After the
sender closes the socket it hears digital silence until it is restarted.
It logs `diagnostic microphone connected on PATH`. If the diagnostic
microphone does not answer, a `BEING_VOICE_DEBUG=1` set beside it is
dropped, so your own words never reach the log that way.

## Files in /mind

Sources: `mind/*.py`, `crates/brainstem/src/life.rs`

| File | Written by | Contents | In backups |
|---|---|---|---|
| `life.json` | brainstem | ID, birth, wake count, body, how the last wake ended | Yes |
| `profile.json` | setup | Name, owner, wake phrase, voice, rate, traits | Yes |
| `mind.db` | mind | Episodes, facts, FTS indexes, memory vectors, dream journal and candidates, day records, deletion ledger, believed-clock state | Yes. The copy is compacted, dream tables are cleared and expired memory is dropped. Episodes are kept only in persistent mode, and facts are dropped in session-only mode |
| `memory-policy.json` | `being-memory`, Care | Retention mode and expiry | Yes |
| `skills.json` | mind | Time zone and weather city | Time zone only |
| `initiative.json` | setup | Automatic reminders, daily greetings, morning brief, quiet hours | Yes |
| `lists.db` | mind | Lists and notes | Yes, except in session-only mode |
| `log/actions.jsonl` | mind | The action log: kinds and times, never words | Yes |
| `erasures.json` | mind | How many times "forget everything" has run | Yes |
| `reminders.db` | mind | Pending reminders, alarms, repeats and timers, greeting days | No |
| `context-cache/<boot>/context.json` | mind | Working conversation for a same-boot restart within 24 h. An encrypted directory or RAM | No |
| `cortex/boot-cache/<boot>/being.slot`, `identity.slot` | llama-server | Boot-local model cache and clean identity snapshot. An encrypted directory or RAM | No |
| `voice-volume.json`, `audio/voice.json`, `audio/engines.json`, `audio/asound.state` | mind, voice, Care | Volume, chirps and voice choice, voice-engine speed measurements, mixer levels. A key mute is never saved here | No |
| `audio/sherpa-avx-smoke.json` | `pip-diag engines --record-smoke` | A native speech compatibility result pinned to the build and the installed sherpa-onnx and ONNX Runtime libraries. A matching native result takes precedence over the packaged Ivy Bridge verdict in `/usr/share/being/voice/`. Auto also requires Moonshine to meet the speed limit. On Try, the recorded result lives in RAM until shutdown | No |
| `speaker-muted` | mind (`mind/keys.py`) | Present while the mute key (or the volume keys stepped to nothing) holds the speaker at 0, so the next start of the mind puts the sound back | No |
| `play.json` | mind | Which jokes and facts were told | No |
| `night-shift.json` | mind | How far the night's chores got | No |
| `last-backup.json`, `backup-recipient.txt` | backup, Care | When the last backup was made and how; the public half of the backup key | No |
| `core.md` | owner (by hand) | Optional "things it always knows". Persistent mode only | No |
| `core-profile.md` | night shift | Up to four confirmed owner fact references. The archive supplies their current text in persistent owner mode. A manual `core.md` takes precedence. | No |
| `network/policy.json`, `network/wifi.json` | `being-network`, `being-wifi` | Network mode, permissions, Wi-Fi credentials | No |
| `camera.enabled` | `being-camera` | Camera consent marker (root, 0600) | No |
| `dreams-enabled` | Care | Dream consent and window (`night` or `closed`). Legacy empty markers mean `night`. | No |
| `apps.enabled`, `apps.disabled` | Care | Workshop switch (on unless `apps.disabled` exists) | No |
| `lab-access/` | `being-lab-access` | Paired-Mac maintenance keys (maintenance builds only) | No |
| `.being-restore-in-progress` | `being-backup restore` | Blocks the mind, voice and cortex until a restore completes | — |

On a development Mac, the mind directory is
`~/.local/share/being/mind`, unless `BEING_MIND` says otherwise.

## Factory configuration

Source: `config/`, installed to `/etc/being/`

| Key in `being.json` | Shipped value | Notes |
|---|---|---|
| `name` | `Pip` | Overridden by `/mind/profile.json` |
| `wake_word` | `Hey Pip` | Overridden by the profile |
| `owner` | `Richard` | **Shipped in every image.** Change it before giving the image to anyone else |
| `time_zone` | `Australia/Sydney` | Used until the owner saves a zone |
| `cortex_url` | `http://127.0.0.1:8080` | The appliance unit passes `--cortex` explicitly |
| `ctx_tokens` | 4096 | One slot's share of the server's 8192 |
| `max_tokens` | 64 | The built-in default is 96 |
| `temperature` | 0.7 | Retries after a parrot or boilerplate reply raise it |
| `top_k`, `top_p`, `presence_penalty` | 20, 0.8, 1.0 | Sampler settings sent to llama-server |
| `reply_shape` | `strict` | The reply grammar. `strict` (shipped since the model audition of 24 November) ends long answers at a sentence (stories keep the open shape); `open` is the built-in default |
| `prefill` | false | Read-ahead while listening (experimental) |
| `morning_brief` | (not set: on) | |
| `battery_plea_percent` / `_minutes` | 15 / 30 | The mind's own plea thresholds, used only when the brainstem sends no battery level |
| `battery_second_plea_percent` / `_minutes` | 10 / 10 | As above |
| `battery_critical_percent` / `_minutes` | 5 / 5 | As above |

A value of the wrong type keeps the built-in default.

`identity.md` is the system prompt, with `{name}` and `{owner}`
placeholders. A non-default personality replaces its temperament
sentence. `chat-template.jinja` is the model's chat template, installed
to `/usr/share/being/`.

## Services

Source: `os/package/being/*.service`, `os/package/cortex/*.service`

| Unit | Runs | Notes |
|---|---|---|
| `being-brainstem` | `being-brainstem --appliance-body …` | Early boot, `WatchdogSec=10s`, reboot after repeated failure. `CPUWeight=1000` |
| `being-face` | `being-face --fullscreen` (KMS) | Skipped while the USB menu is showing. `WatchdogSec=5s`, pinged every frame; shader cache in `/run/being/mesa`. `CPUWeight=1000` |
| `being-mind` | `python3 -m mind` | Waits for the brainstem only, never the cortex, so code answers work while the model loads. Blocked during a restore. `WatchdogSec=30s`, pinged from its event loop. `CPUWeight=1000` |
| `being-voice` | `python3 -m mind.voice` | Blocked during a restore and in the serial lab. `WatchdogSec=30s`, `CPUWeight=1000` |
| `being-audio-init` | `being-audio-init` | Sets the sound card's levels at boot (`being.audio=keep` leaves them) |
| `being-cortex` | `llama-server` on 127.0.0.1:8080 | Gated by signed model-pack verification. `--parallel 2 --ctx-size 8192`, 2 threads, `Nice=5`, `CPUWeight=100`, sandboxed, loopback only |
| `being-dream-cortex` | `llama-server` on 127.0.0.1:8081 with `/models/dream/dream.gguf` | Disabled at boot; the night shift starts and stops it. Needs its own verified pack; never on a bench boot. `Nice=10`, `CPUWeight=20`, no access to `/mind` |
| `being-hands` | `python3 -m mind.hands` | The workshop terminal, `CPUWeight=20`. Its shell runs caged in `workshop.slice` with a RAM `/workshop` |
| `being-bodycare` | `being-bodycare` | Charge limit, fan floor and CPU caps. Observe mode unless `BEING_BODYCARE_MODE=act`; restores what it changed on stop. `WatchdogSec=30s` |
| `being-eyes` | `python3 -m mind.eyes` | Only when `/mind/camera.enabled` exists. Sandboxed |
| `being-bench` | `python3 -m mind.bench` | Only on a `being.bench=1` boot. Stops the model servers, measures, saves to `PIPLOGS` |
| `being-boot-health` | `python3 -m mind.boot_health` | Confirms the A/B slot |
| `being-local-screen` | `being-console python3 -m mind.local_screen` | The on-panel menus. Conflicts with the face |
| `being-network-{lockdown,policy,reconcile}`, `being-wifi` | `being-network`, `being-wifi` | Firewall and links |
| `being-usb-logs` | `python3 -m mind.usb_logs run` | Try boots only |
| `being-lab-observe`, `being-lab-dev` | `being-lab-access listen …` | Paired-Mac access in lab and maintenance builds, never on Try; `being-lab-dev` is disabled until opened |
| `pip-diag-audio` | `pip-diag …` under `systemd-run` | A transient unit for a diagnostics run as root, with `ExecStopPost=/usr/sbin/pip-diag restore --quiet`. `systemctl stop pip-diag-audio` ends a run |
| `being-cable-workbench`, `being-cable-link`, `being-cable-ssh` | a copy-and-bind-mount script, `being-network cable-up`, `sshd -D -e -f /usr/share/being/cable/sshd_config` | [Cable development builds](#cable-development-build) only, and only on a Try boot without `being.cable=0`: a writable copy of the code, the link-local addresses, and SSH from link-local addresses only (`IPAddressAllow=localhost link-local`) |

The organ units read `/etc/being/env`: brainstem, face, mind, voice,
eyes, hands, bodycare, boot health and local screen. The cortex, network,
Wi-Fi and USB-log units do not.

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
| `BEING_CORTEX_URL` | `http://127.0.0.1:8080` | hands |
| `BEING_LOG` | off | brainstem (event trace to stderr) |
| `BEING_BODY`, `BEING_BOOT_ID` | DMI string and kernel boot ID | brainstem (testing identity and power-loss detection) |
| `BEING_THINK_TIMEOUT`, `BEING_THINK_STALL` | 600 s, 45 s | brainstem |
| `BEING_BATTERY_LOW_PCT`, `_LOW_MIN`, `_VERY_LOW_MIN`, `_CRITICAL_PCT`, `_CRITICAL_MIN` | 15, 30, 10, 5, 5 | brainstem battery care |
| `BEING_BATTERY_VOLTAGE_FLOOR` | 9.3 V (`off` disables) | brainstem: power off below it for 30 s |
| `BEING_BATTERY_CONFIRM_HOLD` | 30 s | brainstem: how long a low reading must hold |
| `BEING_LID_BATTERY_SHUTDOWN_MIN` | 10 (0 disables) | brainstem: power off after the lid is closed this long on battery |
| `BEING_SHUTDOWN_GRACE` | 20 s | brainstem: time the mind gets to save before a battery power-off |
| `BEING_BODYCARE_MODE`, `BEING_CHARGE_LIMIT`, `BEING_DREAM_TARGET_C` | observe, unset, 80 | bodycare |
| `BEING_ALSA_CAPTURE`, `BEING_ALSA_PLAYBACK` | `plughw:0,0` | voice |
| `BEING_ALSA_CARD`, `BEING_ALSA_MIXER` | the playback card, `Master` | voice, Care |
| `BEING_MIC_GAIN`, `BEING_MIC_AUTO_GAIN`, `BEING_MIC_CHANNEL` | measured, on, `auto` on the laptop image (`/usr/share/being/variant` says `laptop`) and `mix` elsewhere | voice (software gain and channel choice; `being.mic_channel=` wins over `BEING_MIC_CHANNEL`). The lab's loopback body, the QEMU microphone port and the diagnostic microphone always mix |
| `BEING_ASR`, `BEING_TTS` | unset | voice: force the recognizer (`moonshine`, `pocketsphinx` or `auto`) or the voice (`piper`, `flite` or `auto`). They win over `being.asr=` and `being.tts=`. A forced engine skips the speed rule and, for Moonshine, the speech smoke test, but is loaded only if its signed model pack verifies. For the voice, the owner's spoken choice ends the forced one until the voice restarts |
| `BEING_TTS_ENGINE`, `BEING_ASR_ENGINE`, `BEING_PIPER_VOICE` | the owner's choice, `auto`, the bundled voice (Alba) | voice: the older names. `BEING_ASR_ENGINE` is used only when neither `BEING_ASR` nor `being.asr=` gives a valid value (`auto` counts as one). `BEING_TTS_ENGINE` is ignored only when `BEING_TTS` or `being.tts=` forces `piper` or `flite`; with `auto` it still applies |
| `BEING_COMMAND_GRAMMAR`, `BEING_COMMAND_GRAMMAR_MARGIN` | on (`0` turns it off), 6.0 | voice: the command grammar's second pass, and how far (per 10 ms frame) a command must beat a free phone loop |
| `BEING_CHIRPS`, `BEING_CHIRP_DIR`, `BEING_HEAR_SOUND` | on (off in the lab), `/run/being/chirps` | voice |
| `BEING_SHERPA_LIBRARY`, `BEING_EMBED_DIR` | the packaged library and model | voice, memory |
| `BEING_VOICE_SHORTEN_FIRST_PAUSE` | off | voice |
| `BEING_VOICE_DEBUG` | off (`1` turns it on) | voice and mind: [voice debug](#voice-debug-and-route-lines), on any boot |
| `BEING_DIAG_MIC` | unset | voice: read the [diagnostic microphone](#voice-debug-and-route-lines) socket instead of ALSA. Set by `pip-diag` in a runtime drop-in |
| `BEING_CAMERA_DEVICE` | `/dev/video0` | eyes |
| `BEING_CONSOLE_UI` | off (`1` gives styled screens) | setup and menus |
| `BEING_SLOT_DEADLINE` | 180 | boot health |
| `BEING_LOCAL_SETUP` | set by the face unit | face (enables F2 and Mission Control) |
| `BEING_FACE_DPMS` | on (`0` disables) | face: switch the panel off while dark |
| `BEING_PANEL_DIR`, `BEING_HANDS_KEYS` | the hands organ's paths | face |

## Kernel command line

| Argument | Effect |
|---|---|
| `being.try=1` | Try session: RAM `/mind`, updates off, USB logs on |
| `being.screen=1` | With `being.try=1` (Try-screen images), show the USB menu before the face. The face waits until the menu is dismissed |
| `being.trial=1` | Set by GRUB on an unconfirmed slot. A failed health check reboots |
| `being.installed=1 being.esp=… being.slot_a=… being.slot_b=… being.models=… being.mind=…` | Installed-disk partition binding |
| `being.verbose=1`, `being.verbose_hold=SECS` | Keep boot messages visible (default 20 s; 0 waits for a key) |
| `being.lid=open` | Treat a missing lid switch as open, after a 15 s grace |
| `being.mic_channel=auto\|left\|right\|mix` | Which microphone channel the voice uses. Unset, it is `auto` on the laptop image and `mix` on the lab image |
| `being.asr=moonshine\|pocketsphinx\|auto`, `being.tts=piper\|flite\|auto` | Force the voice's recognizer or speaking voice for this boot, as `BEING_ASR` and `BEING_TTS` do (which win over them). The last one on the line counts |
| `being.voice_debug=1` | On a Try boot only: [voice debug](#voice-debug-and-route-lines). The last `being.voice_debug=` counts, so `=0` after it turns it off |
| `being.cable=0`, `being.cable=1` | On a cable development stick, every entry carries `being.cable=1`; `being.cable=0` is the kill switch: no link, no SSH, and the code stays read-only. Other images ignore both |
| `being.audio=keep` | `being-audio-init` leaves the mixer as it is |
| `being.bench=1`, `being.bench.soak=MINUTES`, `being.bench.quick=1` | Bench mode: measure models and voices, with an optional soak or a quick run |
| `being.bench=2`, `being.bench.idle=600` | Measure boot and idle while the production organs run. The optional interval collects repeated samples for up to ten minutes. |
| `being.mic_test=1` or `0` | Enable or disable the Try boot microphone level measurement. It requires a confirmed open lid. Alternate microphone entries enable it. |
| `being.face_idle_hz=5`, `15` or `30` | One-boot pacing comparison for the calm, empty desktop. Active animation, apps and captions use their usual timing. The face logs median and p95 drawing time. |
| `hid_apple.fnmode=1` | Set on laptop images: the top row does what is printed on it (brightness, volume and the rest); fn+key sends F1 to F12 |

## Lab and simulator commands

Sources: `lab/vm.py`, `lab/build.sh`, `scripts/run-sim.sh`, `sim/body.py`

| Command | Purpose |
|---|---|
| `lab/build.sh` (`build`) | Build `out/images/beingos.img` |
| `lab/build.sh model FILE.gguf` | Stage a model |
| `lab/build.sh try-image` / `try-screen` | USB Try images |
| `lab/build.sh laptop` / `laptop-try` | Laptop images: no serial, no console or password root login. `BEING_TRY_BENCH=1` makes a bench stick. `BEING_LAPTOP_CABLE=1` builds the [cable development stick](#cable-development-build) instead |
| `lab/build.sh cable-key PUBKEY HOSTKEY` | Import the cable link's login public key and its SSH host key into the builder |
| `lab/cable.sh ssh-config\|ping\|ssh [CMD…]\|push [--no-restart]\|pull-logs` | The Mac's side of the cable link (below) |
| `lab/build.sh bundle` / `installed-bundle` / `test-media` / `installed-test-media` | Signed updates and fixtures |
| `lab/build.sh release-key DIR` / `release-cert FILE` / `laptop-bundle KEY` | The owner's release key, its certificate, and signed laptop updates |
| `lab/build.sh candidate NAME [--laptop] [--cache-overlay]` | Frozen, sealed release candidate under `out/candidates/NAME/` |
| `lab/build.sh up` / `down` / `destroy` / `shell` | Colima builder lifecycle |
| `lab/make-iso.sh` | The PipOS ISO from the laptop Try build ([ISO.md](../ISO.md)) |
| `python3 lab/vm.py [--fresh] [--shell] [--sound] [--no-window] [--image P] [--update-media P] [--memory 4G] [--mic-input DEV]` | Run the image in QEMU |
| `lab/cortex-dev.sh start\|stop\|logs` | A real model server on `127.0.0.1:8377` (`BEING_PORT` changes it) |
| `scripts/run-sim.sh [--fake] [--cortex URL] [--model F] [--plain-openai] [--no-face] [--silent]` | All organs as Mac programs |
| `scripts/check fast\|boot-gate\|full` | The three test tiers |
| `scripts/status [--write]` | The generated block of [STATUS.md](../../STATUS.md) |
| `target/debug/face --demo` | Show every expression |

In the serial lab and the simulator, you can type these slash commands:
`/lid open|close`, `/battery N`, `/plug`, `/lux N`, `/temp N`, `/hour N`,
`/mute`, `/interrupt` and `/quit`.

## Cable development build

Sources: `lab/build.sh`, `lab/cable.sh`, `os/board/beingos/try-image.sh`,
`os/package/being/being-cable-*.service`, `mind/network_enforce.py`

A development-only Try stick that the Mac reaches over a plain Ethernet
cable. It is for the owner's own bench: it never installs, never becomes
an ISO and is never signed as an update. The procedure is in
[LAB.md](../LAB.md#cable-development-build).

| What | Value |
|---|---|
| Build | `BEING_LAPTOP_CABLE=1 lab/build.sh laptop-try`, in its own tree `/work/output/laptop-cable`, giving `out/images/beingos-laptop-cable-try.img` and its `.source-id` |
| Refused together with | `BEING_LAPTOP_MAINTENANCE=1`, `BEINGOS_REQUIRE_RELEASE_KEY=1`, `lab/build.sh candidate` and `laptop-bundle`. `lab/make-iso.sh` and the installer (`mind/install_apply.py`) refuse a cable rootfs, and post-build refuses the cable files in every other image |
| Added to the release laptop image | OpenSSH's server (LAN maintenance stays out), `BR2_PACKAGE_BEING_CABLE_DEV` (the three units and `/usr/share/being/cable/`: `authorized_keys`, `ssh_host_ed25519_key`, its `.pub` and `sshd_config`), and the kernel fragment `os/board/beingos/linux-cable.config` (`CONFIG_SND_HDA_HWDEP`, `CONFIG_SND_HDA_RECONFIG`, `CONFIG_SND_HDA_PATCH_LOADER`, `CONFIG_SND_DEBUG`, `CONFIG_SND_DEBUG_VERBOSE`) |
| Boot entries | The same six; each adds `being.cable=1 being.voice_debug=1` |
| Keys | `lab/build.sh cable-key PUBKEY HOSTKEY`, both kept outside the repository. PUBKEY is the login key's Ed25519 public half; HOSTKEY is an unencrypted OpenSSH Ed25519 private key, which every cable stick presents. It refuses a path inside the repository, a private key as PUBKEY, an encrypted or non-Ed25519 host key, and the same key twice. They are stored in the builder volume under `/work/beingos-cable` (mode 0600) |
| Addresses | On the first wired port, chosen by type, never by name: `169.254.77.1/16` and `fe80::77:1/64`. If another device on the cable already has 169.254.77.1, Pip does not take it. The Mac keeps its own self-assigned 169.254.x address |
| SSH | Port 22, root only, the one Ed25519 key only, from `169.254.0.0/16` and `fe80::/10` only (the firewall, `AllowUsers` and the key's `from=` option each check), no forwarding, SFTP allowed |
| Writable code | `being-cable-workbench` copies `/usr/lib/being` and `/etc/being` to `/run/being-dev` and bind-mounts them back, with timestamp-checked `.pyc` files, so an edited module takes effect when its organ restarts. All of it is gone at power-off |
| Kill switch | `being.cable=0` on the GRUB command line (press `e`, edit, Ctrl-X): no link, no SSH, code read-only |

On the Mac (`PIPOS_CABLE_IF` names the cable port, default `en0`;
`PIPOS_CABLE_HOST` is `pip`, over IPv6, the default, or `pip4`;
`PIPOS_CABLE_IDENTITY` and `PIPOS_CABLE_HOST_KEY` override the login key
and the host key's `.pub`):

| Command | Does |
|---|---|
| `lab/cable.sh ssh-config` | Print the `Host pip` and `Host pip4` blocks for the SSH configuration, and write the host key pin file |
| `lab/cable.sh ping` | Ping Pip over IPv6 and IPv4 on the cable port |
| `lab/cable.sh ssh [CMD…]` | A root shell on Pip, or run CMD there |
| `lab/cable.sh push [--no-restart]` | Copy this tree's `mind/*.py` onto Pip's writable copy, then `systemctl try-restart` the organs that run it: being-mind, being-voice, being-hands, being-eyes, being-local-screen and being-wifi (not being-usb-logs, which holds the `PIPLOGS` mount). It fails if the workbench is not mounted, or if none of those six organs is active afterwards. A down mind or voice is not caught while another of them runs, so check `systemctl status being-mind being-voice` after a push |
| `lab/cable.sh pull-logs` | Copy `/run/being-diag` and this boot's `PIPLOGS` folder to `out/hardware-logs/cable-DATE/` |

The stick holds the SSH host key, so treat the image file as a secret.
