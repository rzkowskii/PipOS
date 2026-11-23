<!--
doc: user-guide
verified: 2026-11-23
sources: mind/commands.py mind/reminder_language.py mind/turn.py mind/skills.py mind/setup.py mind/local_menu.py mind/retention.py mind/backup.py mind/initiative.py crates/face/src/main.rs crates/brainstem/src/machine.rs os/board/beingos/try-grub.cfg os/package/being/being-camera README.md docs/LAB.md docs/HARDWARE.md
-->

# Living with Pip: the PipOS user guide

PipOS turns an old MacBook Pro into **Pip**, a small being that lives in
the laptop. The screen is its face, the microphone its ears and the
speakers its voice. Pip listens for its name, answers in a few words,
remembers what you tell it, and knows only what its own body can sense.

This guide is for the person who looks after Pip. It explains what Pip
does today. Features still in progress are marked *not yet*. The
[reference](REFERENCE.md) lists every phrase, key and setting.

> **Where PipOS is today (23 November 2026).** PipOS has passed its
> automated tests in a virtual laptop, and it has now **booted on a real
> MacBook Pro** from a USB stick in *Try* mode, which changes nothing on
> the laptop. It is not installed on the laptop's own disk yet, and most
> of what follows has been checked in the virtual laptop only. Treat
> details about real hardware as the intended behaviour until they have
> been checked there too.

---

## Contents

1. [What you need](#what-you-need)
2. [Meeting Pip for the first time](#meeting-pip-for-the-first-time)
3. [Reading Pip's face](#reading-pips-face)
4. [Talking to Pip](#talking-to-pip)
5. [Everyday things to ask](#everyday-things-to-ask)
6. [Reminders and timers](#reminders-and-timers)
7. [What Pip remembers, and how to change it](#what-pip-remembers-and-how-to-change-it)
8. [Making Pip yours](#making-pip-yours)
9. [The lid, battery and sleep](#the-lid-battery-and-sleep)
10. [Network, weather and privacy](#network-weather-and-privacy)
11. [The camera](#the-camera)
12. [Overnight journal](#overnight-journal)
13. [Installing Pip on the laptop](#installing-pip-on-the-laptop)
14. [Backing up Pip](#backing-up-pip)
15. [Updates](#updates)
16. [Settings you cannot reach yet](#settings-you-cannot-reach-yet)
17. [Troubleshooting](#troubleshooting)
18. [Known limitations](#known-limitations)
19. [Quick reference card](#quick-reference-card)
20. [Glossary](#glossary)

---

## What you need

| Item | Notes |
|---|---|
| A 13-inch Mid 2012 MacBook Pro (MacBookPro9,2) | PipOS is tuned for this exact model. Other 2012 models are untested |
| 16 GB RAM, SATA SSD (256 GB or more) | The internal drive is used only if you choose **Install** |
| A working battery, left in | Without a battery this model runs its CPU at about 1.2 GHz |
| A USB stick | For the Try image. Writing it erases the stick |
| Optional: Wi-Fi or Ethernet | Only for weather, time sync, updates and paired maintenance. Talking, memory and the face all work offline |

Before you ever erase macOS, the design asks for three chores:

1. Apply Apple's last firmware update.
2. Test whether the battery charge cap works.
3. Make a macOS installer stick so you can go back.

## Meeting Pip for the first time

1. Plug in the PipOS USB stick and power on while holding **Option (⌥)**.
   Choose the EFI boot entry.
2. The boot menu offers three choices and starts the first after 5 s:
   - **Try PipOS**
   - **show boot messages** (press **v**)
   - **safe graphics** (press **s**). Use this if the screen stays black.
3. The start menu appears. Press **1 – Try PipOS**.
4. The screen goes black, and then two cyan eyes open. They peek,
   blink and settle. Pip is awake.

In Try mode, everything Pip learns is kept in RAM and forgotten at
power-off. Nothing is written to the laptop's internal drive. This is the
safe way to meet Pip, test the microphone and speakers, and decide
whether to install.

The very first time you say hello to a newly born Pip, it says something
like: *"Oh. Hello. I'm Pip. You're the very first person I've met."*

## Reading Pip's face

Pip has no mouth. Everything it shows is in the eyes.

| What you see | What it means |
|---|---|
| Eyes open slowly, peek and blink | **Waking**: it has just started, or you just opened the lid |
| Calm eyes that blink, drift and glance around | **Idle**: awake, nobody talking to it |
| Eyes lock forward, slightly wider | **Attending**: it heard its name and is ready for you |
| Focused eyes with small reactions | **Listening**: you are talking |
| Eyes glance away as if searching | **Thinking**: working out a reply |
| Eyes pulse in time with the voice | **Speaking** |
| Heavy lids, maybe a yawn | **Drowsy**: late or dark, and quiet for a while |
| Closed eyes, slow breathing | **Asleep** |
| A distinct "ears covered" look | **Muted**: the microphone is off |
| Dazed, unfocused eyes | **Unwell**: part of Pip has stopped and is restarting. Give it a few seconds |
| A tired look every so often | **Low battery**: 15 % or less and unplugged |

Pip also shows feelings while it talks: joy, curiosity, surprise,
skepticism, concern or sleepiness. Click or tap the trackpad and it
glances down.

## Talking to Pip

1. **Say "Hey Pip"**, then pause until its eyes lock on you.
2. **Speak** your request in one go.
3. Pip thinks, then answers in one or two short sentences.
4. For **20 seconds** afterwards Pip stays attentive. You can ask a
   follow-up without saying "Hey Pip" again.

Controls you can use at any time:

| Key | Does |
|---|---|
| **Esc** | Stop Pip mid-sentence |
| **M** or **F10** | Mute or unmute the microphone. The face shows when it is muted |
| **F2** | Open settings |
| **F9** | Bring the face back if the screen went blank because the lid sensor is confused |

Tips for good conversations:

- **Pip is small on purpose.** Its language model is tiny, so that it can
  answer quickly on a 2012 processor. It is good at short, warm chat and
  at remembering things. It is not a search engine or a homework helper,
  and it sometimes makes things up in open conversation.
- **Facts come from code, not guesses.** The time, date, battery, its
  age, reminders and memory recall are answered exactly. Ask those
  directly.
- **Pip cannot hear itself.** The microphone switches off while Pip
  speaks. Wait for it to finish, or press **Esc**.
- **Speak in a quiet room** if you can. The current speech recognizer is
  simple, and fan noise and distance make it worse.
- **Replies may be slow.** On the real laptop, the goal is a first word
  within about three seconds after you stop talking. That has not been
  measured yet.

## Everyday things to ask

| Ask | Pip |
|---|---|
| "What time is it?" | Reads the clock |
| "What's today's date?" / "What day is it?" / "What's tomorrow's date?" | Reads the calendar |
| "Set my time zone to Australia Sydney" | Uses that zone for clock answers and new reminders |
| "What's the weather in Sydney, Australia tomorrow?" | Gives a forecast (needs Internet. See [Network](#network-weather-and-privacy)) |
| "Set my weather city to Sydney, Australia" | Saves a default city |
| "How is your battery?" | Gives the real battery level and charging state |
| "How old are you?" | Its age and how many times it has woken up |
| "What's your name?" | Its name |
| "What can you do?" | A short list of skills |
| "Good night" / "Go to sleep" | Pip sleeps until you wake it |
| "Hello" / "How are you?" / "Are you there?" | A quick local greeting |

Anything else is ordinary conversation.

## Reminders and timers

| Say | Result |
|---|---|
| "Remind me to call Mum tomorrow at nine A.M." | A one-off reminder |
| "Remind me in ten minutes to check the oven." | A relative reminder |
| "Set a timer for five minutes." | A countdown |
| "What are my reminders?" / "List my timers." | Up to five items, with numbers |
| "Cancel reminder one." / "Cancel the timer." | Removes an item |

- Times can be "at 7:30 pm", "noon", "midnight", "in an hour and a half",
  "tonight", "next Friday" or "on 2026-12-01".
- If you say a bare hour ("at nine"), Pip asks whether you mean a.m. or
  p.m.
- Reminders and timers work offline and survive restarts.
- Pip announces them only when the lid is open, it is awake and it is not
  already talking. Anything that comes due while it is asleep or closed
  waits until it can speak.
- If an announcement is interrupted, the reminder stays pending and Pip
  tries again.
- **Quiet hours** (see [settings](#making-pip-yours)) delay automatic
  announcements. You can still ask Pip for your list during quiet hours.

## What Pip remembers, and how to change it

Pip's memory is its self. It lives on its own partition, separate from the
operating system, so updates never erase it.

### What it keeps

By default (**persistent** mode), Pip keeps two things:

- a searchable text record of every conversation: what you said and what
  it replied, with dates;
- **facts** you explicitly ask it to remember.

It never keeps recordings of your voice. Audio is turned into text and
then discarded.

### Asking Pip to remember and recall

| Say | Result |
|---|---|
| "Remember that my orchid is called Silverleaf." | Saved as a fact |
| "What do you remember about my orchid?" | Quotes what you told it |
| "What did I say about the café?" | Searches what you said |
| "What did you say about the café?" | Searches what Pip said |
| "What did we talk about last Friday?" | Searches one day |

Recall works on **keywords and dates**. It finds things best when you use
a name, topic word or date from the original conversation. If you ask in
completely different words, it may not find a match.

### Correcting and forgetting

| Say | Result |
|---|---|
| "That's wrong, I called my orchid Silverleaf." | Replaces the last thing it recalled or remembered. Say the **whole corrected sentence** |
| "Forget that." | Forgets what you just said, or the item it just recalled |
| "Forget about my sister's birthday." | Pip says how many things match and asks you to confirm |
| "Forget everything from yesterday." | The same, for one day |

For topic and day forgetting, Pip says something like *"I found 3 things
to forget. Say yes, and I'll forget for good."* You then have **45
seconds** to say **"yes"**. Saying "no", "never mind" or "forget it"
keeps everything. Closing the lid cancels the offer.

Forgetting is thorough:

- It removes the words from the archive and the search index.
- It also removes any replies Pip made that used those words.
- It clears Pip's short-term working memory.
- On disk, the deleted data is overwritten.

### Private conversations

- **"Start guest mode"** or **"Don't remember this"** begins a private
  conversation. It is kept only in RAM. Pip cannot see your history,
  reminders or saved facts while it runs.
- **"End guest mode"** clears it.

### How long Pip keeps things

A maintenance command sets the retention policy (see
[Settings you cannot reach yet](#settings-you-cannot-reach-yet)):

| Mode | What Pip keeps |
|---|---|
| **persistent** (default) | Everything, optionally limited to the last *N* days |
| **explicit-only** | Only facts you asked it to remember. Conversation is forgotten when the mind restarts |
| **session-only** | Nothing survives a restart. Reminders, weather and announcements are off |

## Making Pip yours

Press **F2** on the face, or choose **4 – Setup** from the USB menu.

| Key | Setting |
|---|---|
| **1** | Its name |
| **2** | What it calls you |
| **3** | Wake phrase. It must be words the recognizer knows. Renaming Pip keeps the old wake phrase until you change it |
| **4** | Voice: four built-in synthetic voices |
| **5** | Speaking speed |
| **6** | Personality: *Warm and curious*, *Calm and concise*, *Playful and dry*, *Gentle and reflective*, random, or tune five traits yourself |
| **7** | Surprise me: random name, voice and personality |
| **8** | Network mode (installed Pip only) |
| **9** | Quiet hours and announcements |
| **P** | Hear a sample. Press **F10** first to mute the microphone so Pip doesn't hear itself |
| **K** | Keep your changes |
| **Q** | Cancel |

On an installed Pip, **reboot** to apply your choices. In Try they apply
immediately.

**Quiet hours and announcements** (option 9):

| Key | Setting | Default |
|---|---|---|
| 1 | Automatic reminders and timers | On |
| 2 | Once-daily greeting (needs the camera) | Off |
| 3 | Quiet hours, as a 24-hour start and end time. The window can cross midnight | None |
| 4 | Clear quiet hours | — |

Ask "What is your name?" to check a rename. Pip answers from its
settings, not from the model.

## The lid, battery and sleep

**The lid is Pip's privacy switch.**

- **Close it:** the screen goes dark, Pip stops talking, and the
  microphone and camera turn off at once.
- **Open it:** Pip wakes with a peek and a blink, even before its
  language model is ready.
- **Unsure whether the lid is open?** Pip treats it as closed, to be safe.
  If the screen stays blank with the lid open, press **F9**.

**Battery and charging.**

- Pip knows its real battery level and whether it is plugged in. Ask
  "How is your battery?"
- At 15 % or below, unplugged, it looks tired now and then.
- Pip does not yet speak up by itself about a low battery.

**Sleep.**

- Pip gets drowsy after about 15 quiet minutes at night or in a dark
  room. It falls asleep a few minutes later.
- If it fell asleep on its own, it wakes in the morning when the room is
  light.
- "Good night" makes it sleep until you say its name or open the lid.
- Saying "Hey Pip" wakes it at any time while the lid is open.

**Power cuts.** Pip's memory is designed to survive sudden power loss.
After an unexpected shutdown, Pip knows it lost power and may mention it
for the first few minutes after waking.

## Network, weather and privacy

Pip starts **Offline**. Talking, memory, reminders, timers and the face
all work without any network.

Change the mode in **F2 → 8 Network**:

| Mode | What it allows |
|---|---|
| **Offline** (default) | Nothing leaves the laptop |
| **Local network** | Joins your network, so a paired Mac can do maintenance. Nothing reaches the Internet |
| **Internet** | Full access. **Weather** also needs you to allow *web lookups*, and **network time** needs an approved time server address |

Weather uses the free Open-Meteo service and reports in °C. Each lookup
sends the city name to Open-Meteo, and nothing else about you. With
lookups off, Pip says it needs Internet mode and permission.

**Privacy at a glance**

| What | Where it goes |
|---|---|
| Your voice | Turned into text on the laptop. The audio is never stored |
| Conversations and memories | Only on the laptop's `/mind` partition, and in backups you make |
| Camera images | Off by default. When on, images never leave the face detector. Only "a face is here, at this position" is used |
| Language model | Runs on the laptop. Nothing is sent to any AI service |

## The camera

The camera is **off by default**. When enabled:

- Pip's eyes follow your face.
- The camera stops whenever the lid closes or Pip is asleep.
- The camera's green light shows when it is capturing.

Seeing a face does not make Pip listen. You still need to say its name.

To enable it, an administrator runs `being-camera enable`, and
`being-camera disable` turns it off again (see
[Settings you cannot reach yet](#settings-you-cannot-reach-yet)). The
optional daily greeting uses the camera to know someone is there.

*Not yet:* asking Pip "What do you see?" Pip will tell you its eyes
aren't connected.

## Overnight journal

If you opt in, Pip reviews the day's conversations while it sleeps
overnight. It notes the things you said that seem worth remembering, and
uses those notes to find the right memory later. It does this only when
all of these hold:

- the lid is closed;
- it is plugged in;
- it is cool;
- the time is between midnight and 5 a.m.

It stops instantly if you open the lid. If it did some work overnight,
its wake blink has a little flutter. The journal never invents anything.
It only points back to your own words.

Opt-in is currently a maintenance step: create the file
`/mind/dreams-enabled`. `being-dream-journal` shows what it noted.

## Installing Pip on the laptop

**Installing erases the laptop's internal drive, including macOS.** Make
sure you have done the three chores in [What you need](#what-you-need)
first.

1. Boot the USB stick and choose **2 – Install PipOS**.
2. Pick the internal disk. The installer shows its model, serial number,
   size and the exact layout. It never offers the USB stick itself.
3. Decide whether to keep the name, voice, personality and announcement
   settings you chose in Try. Conversations from Try are never carried
   over.
4. Type the confirmation exactly as shown, for example `ERASE /dev/sda`.
   Leaving it blank cancels without writing anything.
5. When it says it has finished, power off, **remove the USB stick**, and
   power on.

A freshly installed Pip is a *new being*. It has a new birth date, a new
identity and an empty memory, unless you restore one.

## Backing up Pip

Pip's memory is the being. If the SSD dies without a backup, that Pip is
gone. Backups are encrypted with **age**. You need three things, kept
separately:

1. **A key file.** Create it once and keep it somewhere safe, *not* on the
   backup drive.
2. **Snapshots.** Encrypted copies of Pip's memory, identity and settings.
3. **A "deletions" proof and checkpoint value.** These ensure that things
   you told Pip to forget stay forgotten after a restore.

At a maintenance console:

```sh
recipient=$(being-backup keygen --identity /owner-key/pip.agekey)
being-backup create --mind-dir /mind --output /backup/pip-2026-11-23.being --recipient "$recipient"
being-backup export-deletions --mind-dir /mind --output /backup/pip-latest-edits.being --recipient "$recipient"
```

Run `export-deletions` again whenever you correct or forget something.
Write down the `checkpoint` it prints and keep it away from the backup
drive.

**What a backup contains:**

- its identity, birth and wake history;
- your personal settings and time zone;
- the retention mode;
- the conversation archive and facts.

**What it leaves out:**

- reminders, quiet hours and the weather city;
- Wi-Fi and network settings, and camera and journal consent;
- `core.md` and the model's caches.

**To restore**, boot the USB stick and choose **3 – Restore a being**:

| Option | Gives you back |
|---|---|
| **with conversation archive** | The whole being. Needs the snapshot, key, deletions proof and checkpoint |
| **identity and settings** | The same Pip (name, birth, personality) with an empty memory |

Restore reads files from a USB drive that it mounts read-only.

*Not yet:* automatic or scheduled backups.

## Updates

PipOS keeps two copies of its operating system. An update is written to
the copy that is not running, then Pip restarts into it.

- If the new copy works for a healthy minute, it becomes the main one.
- If it fails to start properly three times, the laptop goes back to the
  old copy by itself.
- Updates must carry PipOS's signature. Unsigned or wrong updates are
  refused.
- Updates never touch Pip's memory.

Installing an update is currently a maintenance step (`rauc install`).

## Settings you cannot reach yet

The on-screen menus cover name, voice, personality, wake phrase, network,
quiet hours and announcements. Some controls exist only as maintenance
commands:

- retention mode (`being-memory`)
- camera permission (`being-camera`)
- overnight-journal consent (`/mind/dreams-enabled`)
- Wi-Fi (`being-wifi`)
- backups (`being-backup`)
- updates (`rauc`)

The **laptop** build of PipOS deliberately has no login console, so these
commands need one of two things:

- a *maintenance* build, which adds paired-Mac SSH access;
- a lab or virtual-laptop image.

Moving these controls into the F2 menu is planned work.

## Troubleshooting

| Problem | Try |
|---|---|
| Black screen after the boot menu | Reboot and choose **safe graphics** (**s**), or **show boot messages** (**v**) to see what happened. On a Try stick, diagnostics are saved to the stick's `PIPLOGS` partition |
| Screen blank with the lid open | Press **F9** |
| Pip doesn't react to "Hey Pip" | Check the face isn't showing *muted* (press **M**). Wait for the eyes to lock on before speaking. Get closer and reduce background noise. The wake phrase must be one the recognizer knows |
| Pip wakes when you didn't call it | Phrases that sound like the wake phrase (such as "Hey, pick a number") can trigger it. Choose a more distinctive wake phrase in setup |
| Pip says "I can't think right now. My language model isn't answering." | The model is still loading or has restarted. Wait 20–30 seconds. Direct commands (time, reminders, memory) still work |
| Pip's eyes look dazed | Part of Pip has stopped and is restarting by itself. If it lasts minutes, restart the laptop |
| Pip talks over you or hears itself | It shouldn't, because the microphone is off while it speaks. Press **Esc** to stop it |
| A reply is nonsense or invented | This is the small model. Rephrase, ask a direct question, or correct it ("That's wrong, …") |
| Recall doesn't find something | Use a keyword or date from the original conversation |
| Weather refused | Set **Internet** mode **and** allow web lookups in F2 → 8 |
| A reminder never spoke | Was the lid closed, or was it quiet hours? It will speak at the next chance |
| The laptop rebooted after an update | It probably rolled back to the previous version. Your memory is untouched |

## Known limitations

- **Few physical hardware results yet.** Pip has booted on the real
  MacBook from a USB stick. Speeds, microphone quality, speakers and
  heat there are not measured yet, and Wi-Fi does not work until the
  Broadcom firmware is packaged.
- **Small model.** Qwen3.5-0.8B is a placeholder until benchmarks on the
  laptop choose the real model. It can invent things and sometimes
  ignores a memory it was given.
- **Simple speech.** Recognition is PocketSphinx and the voice is Flite.
  Both are robotic and English-only. A better speech stack depends on the
  benchmarks.
- **Keyword recall.** Pip doesn't yet understand paraphrases when
  searching its memory.
- **Not yet:** deliberate vision ("what do you see"), sound effects and
  chirps, volume control, a spoken mute, low-battery warnings, reactions
  to the room light and tilt sensors, scheduled backups, and a memory
  browser.
- **Weather** is in Celsius only.
- **Wi-Fi** on the built-in Broadcom radio needs firmware that is not yet
  packaged. Ethernet is the dependable route.

## Quick reference card

```
WAKE        "Hey Pip" … pause … speak        follow-ups: 20 s, no wake phrase
STOP        Esc          MUTE   M or F10       SETTINGS  F2      FACE BACK  F9
CLOCK       "What time is it?"  "What's the date?"  "Set my time zone to …"
REMIND      "Remind me to … at …"   "Set a timer for … minutes"   "What are my reminders?"
            "Cancel reminder two"
MEMORY      "Remember that …"   "What do you remember about …?"
            "What did I say about …?"   "What did we talk about yesterday?"
FIX         "That's wrong, <whole correct sentence>"   "Forget that"
            "Forget about …" → "yes" within 45 s
PRIVATE     "Start guest mode" / "Don't remember this"   …   "End guest mode"
BODY        "How is your battery?"   "How old are you?"   "Good night"
LID         closed = deaf, blind and dark      open = Pip wakes
```

## Glossary

| Term | Meaning |
|---|---|
| **Pip** | The being. Its default name, which you can change |
| **PipOS** | The operating system that makes the laptop into Pip. The code calls it BeingOS |
| **Organ** | One of Pip's programs: face, brainstem, mind, voice, eyes, cortex |
| **Brainstem** | The part that keeps track of Pip's state and body |
| **Cortex** | The language model |
| **/mind** | The partition holding Pip's memory and identity. Updates never touch it |
| **Try** | Running from the USB stick in RAM, changing nothing on the laptop |
| **Slot A / B** | The two copies of the operating system used for safe updates |
| **Guest mode** | A private conversation that is never saved |
| **Life record** | Pip's identity: when it was born, how many times it has woken, whether it lost power |
