<!--
doc: reference
verified: 2026-11-24T04:50
sources: mind/commands.py mind/reminder_language.py mind/turn.py mind/everyday.py mind/skills.py mind/profile.py mind/setup.py mind/care.py mind/local_menu.py mind/initiative_setup.py mind/retention.py mind/backup.py mind/dream_journal.py mind/network_enforce.py mind/network_policy.py mind/initiative.py mind/voice.py mind/voice_engines.py mind/mic_channel.py mind/tts.py mind/eyes.py mind/__main__.py crates/face/src/main.rs crates/brainstem/src/main.rs crates/brainstem/src/battery.rs crates/bodycare/src/main.rs sim/body.py lab/vm.py lab/build.sh scripts/run-sim.sh os/package/being/being.env os/package/being/being-camera os/package/being/50-being.preset os/package/cortex/being-cortex.service os/package/cortex/being-dream-cortex.service os/board/beingos/try-grub.cfg config/being.json mind/wifi.py mind/reminders.py os/package/being/being-usb-logs os/package/being/being-hardware-report
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
| "What do you see?" / "Look at this" | Camera off: "I can't see right now. My camera is switched off." Camera on: it can tell someone is there but cannot make out things |
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

Sources: `crates/face/src/main.rs`, `os/package/being/being-console.c`

| Key | On the face | On a setup screen |
|---|---|---|
| **Esc** | Stop the current reply, including audio already playing (`key.interrupt`) | Leaves the typing chat |
| **M** | Mute or unmute the microphone (`key.mute`). A letter while the terminal has the keyboard | Typed as text |
| **F10** | Mute or unmute the microphone | Mute (for the **P** speaker preview) |
| **F1** | Show or hide the captions and health overlay (off by default; nothing in it is saved) | — |
| **F9** | Recover the face when the lid reading is unknown (`key.panel_recover`) | Same |
| **F2** | Open setup on an installed Pip, or the start menu in Try | — |
| **Click or tap** | Pip glances down (`key.touch`); while the eyes rest it also listens, as for "Hey Pip" | — |
| **Space** or **Return** | While the eyes rest (idle, drowsy or asleep with the lid open, not muted) and no app panel has the keyboard: listen, as for "Hey Pip" (`key.attend`) | Typed as text |
| Other keys | Typed into the workshop terminal while it has the keyboard | — |
| **Page Up / Page Down** | — | Scroll the history |
| **Number keys, Enter** | — | Choose menu items |

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

The ISO's boot menu (`os/board/beingos/iso-grub.cfg`) has the same six
entries.

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
| 2 | Dreams: 1 at night only (lid closed, on power, midnight to 5 a.m.), 2 whenever the lid is closed on power (saved in `dreams-enabled`, but the mind does not read the window yet and keeps to the night), 3 stop |
| 3 | Memory: persistent, explicit-only, session-only, or 4 forget conversations after N days |
| 4 | What I remember: the newest 40 facts; forget one by number |
| 5 | Dream journal: the newest 12 entries |
| 6 | Date and time: set the clock (also the hardware clock), or choose the zone |
| 7 | Time zone |
| 8 | Workshop on or off |
| 9 | Network: join Wi-Fi (scan or hidden), wired Ethernet on or off, forget Wi-Fi. Installed system only |
| D | Boot diagnostics on the Try stick on or off. Try only |
| H | Health: the doctor's check-up |

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
| `being-usb-logs run\|report\|summary\|check\|mic-test` | Manage Try boot diagnostics on the `PIPLOGS` partition; `mic-test` measures the microphone level once (numbers only) |
| `being-bodycare [--once] [--mode act] [--charge-limit N] [--restore]` | The power and thermal keeper (see `being-bodycare --help`). The unit runs it in observe mode: it decides and reports, and writes nothing |
| `python3 /usr/lib/being/mind/model_pack.py verify [--role ROLE]` | Check a signed model pack |
| `rauc install BUNDLE.raucb` | Install a signed OS update to the inactive slot (Setup **U** does the same) |

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
| `context.json` | mind | The conversation to resume within 24 h | No |
| `cortex/being.slot`, `cortex/identity.slot` | llama-server | Saved model cache, and the snapshot after a clean identity warm-up | No |
| `voice-volume.json`, `audio/voice.json`, `audio/engines.json`, `audio/asound.state` | mind, voice, Care | Volume, chirps and voice choice, voice-engine speed measurements, mixer levels | No |
| `play.json` | mind | Which jokes and facts were told | No |
| `night-shift.json` | mind | How far the night's chores got | No |
| `last-backup.json`, `backup-recipient.txt` | backup, Care | When the last backup was made and how; the public half of the backup key | No |
| `core.md` | owner (by hand) | Optional "things it always knows". Persistent mode only | No |
| `network/policy.json`, `network/wifi.json` | `being-network`, `being-wifi` | Network mode, permissions, Wi-Fi credentials | No |
| `camera.enabled` | `being-camera` | Camera consent marker (root, 0600) | No |
| `dreams-enabled` | Care | Dream consent marker; may hold the window (`night` or `closed`, not yet read by the mind) | No |
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
| `BEING_BODYCARE_MODE`, `BEING_CHARGE_LIMIT`, `BEING_DREAM_TARGET_C` | observe, unset, 70 | bodycare |
| `BEING_ALSA_CAPTURE`, `BEING_ALSA_PLAYBACK` | `plughw:0,0` | voice |
| `BEING_ALSA_CARD`, `BEING_ALSA_MIXER` | the playback card, `Master` | voice, Care |
| `BEING_MIC_GAIN`, `BEING_MIC_AUTO_GAIN`, `BEING_MIC_CHANNEL` | measured, on, `mix` | voice (software gain and channel choice; `being.mic_channel=` also sets the channel) |
| `BEING_TTS_ENGINE`, `BEING_ASR_ENGINE`, `BEING_PIPER_VOICE` | the owner's choice, `auto`, the bundled voice | voice |
| `BEING_CHIRPS`, `BEING_CHIRP_DIR`, `BEING_HEAR_SOUND` | on (off in the lab), `/run/being/chirps` | voice |
| `BEING_SHERPA_LIBRARY`, `BEING_EMBED_DIR` | the packaged library and model | voice, memory |
| `BEING_VOICE_SHORTEN_FIRST_PAUSE`, `BEING_VOICE_DEBUG` | off | voice |
| `BEING_CAMERA_DEVICE` | `/dev/video0` | eyes |
| `BEING_CONSOLE_UI` | off (`1` gives styled screens) | setup and menus |
| `BEING_SLOT_DEADLINE` | 180 | boot health |
| `BEING_LOCAL_SETUP` | set by the face unit | face (enables F2) |
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
| `being.mic_channel=auto\|left\|right\|mix` | Which microphone channel the voice uses |
| `being.audio=keep` | `being-audio-init` leaves the mixer as it is |
| `being.bench=1`, `being.bench.soak=MINUTES`, `being.bench.quick=1` | Bench mode: measure models and voices, with an optional soak or a quick run |
| `hid_apple.fnmode=2` | Set on laptop images: the top row sends F1 to F12 without fn |

## Lab and simulator commands

Sources: `lab/vm.py`, `lab/build.sh`, `scripts/run-sim.sh`, `sim/body.py`

| Command | Purpose |
|---|---|
| `lab/build.sh` (`build`) | Build `out/images/beingos.img` |
| `lab/build.sh model FILE.gguf` | Stage a model |
| `lab/build.sh try-image` / `try-screen` | USB Try images |
| `lab/build.sh laptop` / `laptop-try` | Laptop images: no serial, no root login. `BEING_TRY_BENCH=1` makes a bench stick |
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
