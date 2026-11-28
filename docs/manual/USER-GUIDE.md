<!--
doc: user-guide
verified: 2026-11-28T13:17
sources: mind/commands.py mind/reminder_language.py mind/reminders.py mind/habits.py mind/care_habits.py mind/turn.py mind/type_chat.py mind/skills.py mind/local_screen.py mind/setup.py mind/care.py mind/local_menu.py mind/retention.py mind/backup.py mind/device.py mind/initiative.py mind/initiative_setup.py mind/voice.py mind/voice_engines.py mind/installer.py mind/recognition.py mind/app_commands.py mind/hands.py mind/usb_logs.py mind/diag.py mind/diag_report.py crates/face/src/main.rs crates/face/src/keys.rs mind/keys.py crates/brainstem/src/machine.rs crates/brainstem/src/body.rs crates/brainstem/src/battery.rs os/board/beingos/try-grub.cfg os/package/being/being-camera README.md docs/LAB.md docs/HARDWARE.md os/board/beingos/try-image.sh mind/diag_sound.py mind/diag_speech.py mind/keep.py mind/keep_export.py mind/keep_transfer.py mind/keep_documents.py mind/usb_arrival.py
-->

# Living with Pip: the PipOS user guide

PipOS turns an old MacBook Pro into **Pip**, a small being that lives in
the laptop. The screen is its face, the microphone its ears and the
speakers its voice. Pip listens for its name, answers in a few words,
remembers what you tell it, and knows only what its own body can sense.

PipOS is *aiming* to be a being with a life of its own. Nothing here proves
that it is sentient, and Pip itself says so when asked.

This guide is for the person who looks after Pip. It explains what Pip
does today. Features still in progress are marked *not yet*. The
[reference](REFERENCE.md) lists every phrase, key and setting.

> **Where PipOS is today (28 November 2026).** PipOS has passed its
> automated tests in a virtual laptop, and it has **booted on a real
> MacBook Pro** from a USB stick in *Try* mode, which changes nothing on
> the laptop. On the first boot, on 23 November, Pip heard "Hey Pip" but
> could not make out the words after it, so it never answered. Cable
> diagnostics since then have measured speech recognition and the natural
> voice on the same laptop. Speaking to Pip across the room, false wakes in
> a quiet room, battery and heat, and installing Pip on the laptop's own
> disk are still open. Treat details about real hardware as the intended
> behaviour until they have been checked there.
> [STATUS.md](../../STATUS.md) has the current state and says which results
> came from the laptop and which from the virtual one.

---

## Contents

1. [What you need](#what-you-need)
2. [Meeting Pip for the first time](#meeting-pip-for-the-first-time)
3. [Reading Pip's face](#reading-pips-face)
4. [Talking to Pip](#talking-to-pip)
5. [Things you can say](#things-you-can-say)
6. [Everyday things to ask](#everyday-things-to-ask)
7. [Reminders and timers](#reminders-and-timers)
8. [What Pip remembers, and how to change it](#what-pip-remembers-and-how-to-change-it)
9. [Making Pip yours](#making-pip-yours)
10. [The lid, battery and sleep](#the-lid-battery-and-sleep)
11. [Network, weather and privacy](#network-weather-and-privacy)
12. [The camera](#the-camera)
13. [Dreaming and the night's chores](#dreaming-and-the-nights-chores)
14. [Installing Pip on the laptop](#installing-pip-on-the-laptop)
15. [Backing up Pip](#backing-up-pip)
16. [Updates](#updates)
17. [Maintenance commands](#maintenance-commands)
18. [Troubleshooting](#troubleshooting)
19. [Known limitations](#known-limitations)
20. [Quick reference card](#quick-reference-card)
21. [Glossary](#glossary)

---

## What you need

| Item | Notes |
|---|---|
| A 13-inch Mid 2012 MacBook Pro (MacBookPro9,2) | PipOS is tuned for this exact model. Other 2012 models are untested |
| 16 GB RAM, SATA SSD (256 GB or more) | The internal drive is used only if you choose **Install** |
| A working battery, left in | Without a battery this model runs its CPU at about 1.2 GHz |
| A USB stick | For the Try image. Writing it erases the stick. [ISO.md](../ISO.md) explains writing the PipOS ISO |
| Optional: Wi-Fi or Ethernet | Only for weather, time sync, updates and paired maintenance. Talking, memory and the face all work offline |

Before you ever erase macOS, the design asks for three chores:

1. Apply Apple's last firmware update.
2. Test whether the battery charge cap works.
3. Make a macOS installer stick so you can go back.

## Meeting Pip for the first time

1. Plug in the PipOS USB stick and power on while holding **Option (⌥)**.
   Choose the EFI boot entry.
2. The boot menu starts the first entry after 5 s:
   - **Try PipOS**
   - **show boot messages** (press **v**)
   - **safe graphics** (press **s**). Use this if the screen stays black.
   - **alternative microphone** (**m**) and **alternative microphone 2**
     (**n**): two other set-ups for a microphone that hears "Hey Pip" but
     not the words after it.
   - **measure speed** (**b**): times Pip's models and voices on this
     laptop and saves the numbers with the stick's logs.
   - **measure boot and idle** (**i**): saves boot timing, processor power
     and service resource use while Pip runs normally.

   The first stick, of 23 November, has only the first three entries.
3. The start menu appears. Press **1 – Try PipOS**. The menu also offers
   **2** Install, **3** Restore a being, **4** Setup, **5** Check hardware,
   **6** Talk to Pip by typing, **7** Back up to a USB stick and **8** Run
   diagnostics (see [Running diagnostics](#running-diagnostics)). Left
   alone for four minutes, the menu gives the screen back to the eyes.
4. The screen goes black, and then two cyan eyes open. They peek,
   blink and settle. Pip is awake.

In Try mode, everything Pip learns is kept in RAM and forgotten at
power-off. Nothing is written to the laptop's internal drive. This is the
safe way to meet Pip, test the microphone and speakers, and decide
whether to install.

The very first time you say hello to a newly born Pip, it says something
like: *"Oh. Hello. I'm Pip. You're the very first person I've met."*

**The time zone.** The laptop's clock runs on UTC. Pip shows and speaks
the time in the time zone the image was built with (the shipped image
uses Australia/Sydney) until you choose another: say "Set my time zone to
Europe London" (or your own zone), or use Setup **T**. The zone decides:

- the time Pip reads out;
- reminder and alarm times;
- quiet hours;
- when Pip gets sleepy at night;
- "yesterday" in memory questions.

"Use the device time zone" switches to UTC.

If the boot goes wrong:

- With **show boot messages**, the text stays up for 20 seconds before
  the start menu appears. Press Esc, Enter, Space, F2 or Mission Control to
  go on sooner.
- If the normal entry hits a fatal error, the message stays for 30
  seconds and the Mac then restarts. Hold Option again to come back to
  the stick. If the stick's system cannot be found within 60 seconds, the
  boot stops with a message on screen.
- "Cannot locate the PipOS Try media" means the boot menu could not find
  the stick's partitions. Try another USB port or rewrite the stick.

## Reading Pip's face

Pip shows its expression through its eyes by default. You can add brows
and a mouth independently in Setup's Look menu or by asking Pip.

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
| Eyes shrunk to short, dim, flat lines | **Muted**: the microphone is off |
| Dazed, unfocused eyes | **Unwell**: part of Pip has stopped and is restarting. Give it a few seconds |
| A tired look every so often | **Low battery**: unplugged, with 15 % or less or about 30 minutes left. It also shows when the laptop has been hot for a while, but only on the real laptop, where Bodycare runs; a test image never shows it |
| A short sad look | A job you asked for **failed** |
| A relieved look | A job finished **successfully**, or the charger went in after a low-battery warning |

Pip also shows feelings while it talks: joy, curiosity, surprise,
skepticism, concern or sleepiness. Click or tap the trackpad and it
glances down. If it heard you but could not make out the words, it
looks puzzled for a moment, the way someone says "pardon?".

If you switch on Pip's emotes (the Effects layer, off at the factory), a
feeling can also draw a small mark: a blush, hearts, tears, sweat drops, a
sleep bubble, flame, steam, snow, or X or spiral pupils. No other marks appear
in conversation. Pip never shows flame or steam while its body is actually
hot, and never shows X or spiral pupils while it is unwell, has lost its link
or is still loading. With the layer off, Pip shows eyes only.

Pip also makes small sounds, unless the lid is closed, it is muted or it
is quiet hours: a chirp when it starts listening, another when it has
heard you, a soft purr while a long thought goes on, a sigh as it falls
asleep, and a bell before a reminder or alarm (an alarm's bell sounds
even in quiet hours).

## Talking to Pip

1. **Say "Hey Pip"**, then wait for the chirp and for its eyes to lock on
   you. Or press **Space** or **Return**, or click the trackpad, while the
   eyes rest: that does the same.
2. **Speak** your request in one go.
3. Pip thinks, then answers in one or two short sentences.
4. For **20 seconds** afterwards Pip stays attentive. You can ask a
   follow-up without saying "Hey Pip" again.

In those 20 seconds Pip only takes what is clearly meant for it: a
question, a sentence with its name in it, a yes or no to something it just
asked, an answer to a ring, or an everyday request such as a sum or "add
milk to my list". Remembering, reminders and alarms, setting the clock,
forgetting, corrections, guest mode, sleep, volume, the time zone and the
weather city need its name there, as in "Pip, remind me to call Mum at 6
pm.", or a question ("Can you remind me to call Mum at 6 pm?"). Other
room talk in that window is ignored and not kept.

Controls you can use at any time. The MacBook's top row works as it does
on a Mac: each key does what is printed on it, and you hold **fn** for F1
to F12. So in this guide **F2** means **fn+F2** (or the Mission Control
key), **F9** means **fn+F9**, and so on. The footers on the start menu,
the setup screens and the terminal say so too: **fn F10 Mic**, **fn F9
Panel**, **fn F3 Hide**.

| Key | Does |
|---|---|
| **Esc** | Stop Pip mid-sentence. Pip remembers saying only what you heard |
| **M** or **fn+F10** | Mute or unmute the microphone. The face shows when it is muted |
| **fn+F2**, or **Mission Control** (F3) | Open settings. In Try mode it opens the start menu; choose **4 – Setup** |
| **fn+F1** | Show or hide captions for what Pip hears and says, with a badge for how it answered. Pip's answer appears as it is spoken, so a stopped answer shows only what you heard. Off by default and never saved |
| **Shift+fn+F1** | Show or hide the hardware and service health overlay |
| **fn+F9** | Bring the face back if the screen stays blank because Pip has no lid reading |
| **Brightness** (F1, F2) | *New.* The screen dimmer or brighter, never so dim that the eyes vanish. A bar shows the new level |
| **Launchpad** (F4) | *New.* Open Pip's terminal; press again to put it away |
| **Keyboard light** (F5, F6) | *New.* The keys' backlight dimmer or brighter. It goes out with the lid closed and comes back when you open it |
| **Play/pause** (F8) | *New.* While the eyes rest: listen, as for "Hey Pip". While Pip talks: stop it, as Esc does |
| **Mute** (F10) | *New.* Pip's speaker off, and back on at the same level. This is the speaker, not the microphone: Pip still hears you. "speaker muted" stays on the face while it is off. A timer, alarm or reminder, or a low-battery warning, turns the speaker back on before it rings, and so does restarting Pip |
| **Volume** (F11, F12) | *New.* Pip's speaker quieter or louder. A bar shows it, and a short click at the new loudness, as on a Mac (not while Pip is listening to you or talking, or with its sounds switched off). The lowest step is silence, the same as the mute key |
| **Space**, **Return** or a click | While the eyes rest: listen, as if you had said "Hey Pip". Not while the terminal has the keyboard, and not while muted |
| **Mission Control** or **fn+F2**, then **6** (Try stick) or **Y** (installed) | Type to Pip instead of talking. It answers out loud, and each part shows on screen as it is spoken. **Esc** leaves |

The previous, next and eject keys do nothing. None of the top row's printed
keys is ever typed into the terminal. On the start menu and setup screens
the brightness, keyboard-light, volume and speaker-mute keys work as they do
on the face, with **fn+F9** and **fn+F10**, but no bar or "speaker muted"
note is shown there.

If Pip hears you but cannot make out the words, it says "Sorry, I
didn't catch that." (once each time you wake it). It says the same when
nothing follows "Hey Pip" and the room goes quiet for a couple of seconds;
it is still listening then, so just say your request again. It says sorry at
most once a minute; after that its face shows it missed you. When it heard
words but could not make them out a second time within ten minutes, it adds
that you can type to it instead: the menu key (**fn+F2**
or Mission Control), then **6** (on an installed Pip, then **Y**). Once it has answered you, a cough or the TV in the
few seconds after its reply gets no apology.

Tips for good conversations:

- **Pip is small on purpose.** Its language model is tiny, so that it can
  answer quickly on a 2012 processor. It is good at short, warm chat and
  at remembering things. It is not a search engine or a homework helper,
  and it sometimes makes things up in open conversation.
- **Facts come from code, not guesses.** The time, date, battery, its
  age, reminders, sums, conversions and memory recall are answered
  exactly. Ask those directly.
- **Pip cannot hear itself.** The microphone switches off while Pip
  speaks. Wait for it to finish, or press **Esc**.
- **Speak in a quiet room** if you can. Fan noise and distance make
  hearing worse. How well Pip hears on the real laptop has not been
  measured yet.
- **Replies may be slow.** On the real laptop, the goal is a first word
  within about three seconds after you stop talking. That has not been
  measured yet.
- **Muted means deaf.** While muted, Pip does not react to "Hey Pip" at
  all, even after you close and reopen the lid. Press **M** or **fn+F10** to
  unmute. "Stop listening" mutes it by voice, but nothing spoken can
  unmute it, because it cannot hear you.
- **Time limits.**
  - If you stop speaking for 15 seconds, Pip stops listening.
  - If its thinking stalls (no progress for 45 seconds, or 10 minutes in
    all), it gives up, looks concerned and says nothing. Just ask again.
  - Speech stalled for a minute is cut off.
  - If the model returns nothing, Pip says "Sorry. I lost my thought."
- **Pip knows a fixed English dictionary** for its name and wake phrase.
  The wake phrase is always heard with PocketSphinx, whose dictionary is
  fixed, so Setup refuses a wake phrase with words outside it. What you
  say after the wake phrase is written down by Moonshine when that is
  installed, has passed a speech test on this kind of processor and is
  fast enough on this laptop, and otherwise by PocketSphinx, which can only
  write words it knows: unusual names may then be heard as something else.
  When PocketSphinx's words are not a workshop command, a short request is
  read once more against the workshop's own phrases, so "Hey Pip, open a
  terminal" said in one breath is not lost to a near miss. To check how something was heard, ask "What did you
  hear?" straight after saying it.

## Things you can say

Pip answers most everyday requests with a fixed program, not the language
model, so the answer is instant and exact. These are examples: close
variations work too, and "Hey Pip" in front is optional while Pip is
already listening. Anything not on these lists goes to the language model
as ordinary conversation.

Items marked *new* are the second wave of work (24 November 2026). So far
they have been tested only on the development Mac, and
[STATUS.md](../../STATUS.md) records which of them have reached a stick.

### Hearing and voice

| Say | Pip |
|---|---|
| "What did you hear?" / "What did I just say?" | *New.* Reads back exactly what it heard, for example "I heard: 'set a timer for five minutes'." It keeps that only until it next sleeps |
| "Say that again" / "Pardon?" / "What did you say?" | *New.* Repeats its last line |
| "How well can you hear me?" / "Check your hearing" | *New.* Says how quiet the room is and how clearly your voice reaches it, from measured levels |
| "Speak up" / "Be quieter" / "Set your volume to 60" | Changes its volume. By voice it never goes below 10 percent, so you can still ask it back up ("Set your volume to 5" gives 10 and says so). The volume keys can go down to silence |
| "Use your natural voice" / "Use your original voice" / "Which voice are you using?" | *New.* Switches between the natural voice (Alba) and the original robot voice. Left on automatic, Pip speaks with the natural voice from its first reply, whenever that voice is installed and checks out. It measures the voice's speed later, while resting, and goes back to the original voice only if Alba takes more than 0.7 s to make each second of speech. "Which voice" says whether that has been checked yet |

### Time and routines

| Say | Pip |
|---|---|
| "What time is it?" / "What's the date?" / "What day is it?" | Reads the clock and calendar |
| "What time is it in London?" / "What's the time difference to London?" | *New.* The time elsewhere |
| "Set the time to 7:30 pm" | Sets the time of day (when its date is right). Only the owner can; a guest is refused |
| "It's the 28th of November 2026, twenty to four in the afternoon" (or "28 November 2026, 3:40 pm", or "2026-11-28 15:40"; typed works the same) | *New.* For a clock that was reset: sets the date and time together. Pip reads it back and waits for your **yes**, then sets its clock, moves running timers with it and tries to write the battery clock. You can also give the date first and the time in the next sentence. Say the year too. Only while Pip doubts its clock (or to correct one of its own settings in the same session). Refused to a guest, and a date that cannot be right (31 February, before 2026, earlier than what Pip remembers, a time the clocks skip) is refused with the reason. After a restart the clock is wrong again until the battery clock is set in Setup |
| "Set a timer for 10 minutes" / "How long left on my timer?" / "Cancel my timer" | Timers. "How long left" is *new* |
| "Remind me to call Mum tomorrow at 9 am" / "What are my reminders?" | Reminders. See [Reminders and timers](#reminders-and-timers) |
| "Wake me up at 7" / "Wake me at half past six" / "Set an alarm for 6:30 tomorrow" / "Set an alarm for ten minutes" | *New.* Alarms. "Quarter past" and "quarter to" work too. Alarms ring in quiet hours; reminders wait for them to end |
| "Every weekday at 7, remind me to take my tablets" / "Every Tuesday at 8 pm, remind me to put the bins out" | *New.* Repeating reminders |
| "Snooze" / "Five more minutes" / "Okay" / "Thanks" / "Got it" | *New.* When something rings, Pip says it three times, two minutes apart, until you answer. "Snooze" puts it off |
| "Good morning" (your first words of the day) | *New.* A short morning brief: today's reminders, anything about its body that matters, and at most one tentative thing from its night. Setup **9**, then **5**, turns it off |

If its clock was reset while it was off, Pip asks once: "I lost track of
time. What time is it?" (*new*).

### Lists, notes and memory

| Say | Pip |
|---|---|
| "Remember that my orchid is called Silverleaf" / "What do you remember about my orchid?" | Remembers and recalls. See [What Pip remembers](#what-pip-remembers-and-how-to-change-it) |
| "Forget about my sister's birthday" / "Forget that" | Counts what matches and waits for your yes, then really deletes it, reminders that hold those words included |
| "Forget everything" | Asks first, and takes only a plain yes. Then every memory, list, note, reminder and alarm goes for good |
| "Add milk to my shopping list" / "Add eggs and bread to the shopping list" | *New.* Lists, as many as you like |
| "What's on my shopping list?" / "Take milk off my shopping list" / "Cross off milk" | *New.* It reads back five items, then says how many more |
| "Clear my shopping list" / "What lists do you have?" | *New.* Clearing asks first and takes only a plain yes |
| "Make a note: the plumber comes Thursday" / "Read my notes" / "Delete my last note" | *New.* Notes that survive restarts |
| "What did we talk about?" / "What did we do yesterday?" | *New.* From a short record Pip writes each night |
| "When did you last back up?" / "Back yourself up" | *New.* Backing up needs a stick labelled for backups, and asks yes or no |
| "Start guest mode" / "End guest mode" / "Don't remember this" | Private conversations |

Lists and notes are kept in their own file. They survive restarts, go into
backups and are erased by "forget everything". Forgetting a topic leaves
them alone: take items off a list yourself. When Pip is set to keep nothing
after a restart (Care, Memory), it keeps no lists or notes at all and says
so. "Take that off" undoes the last thing added only within ten minutes.

A backup stick is one labelled `being-backup` (exFAT or ext4) or
`PIP-BACKUP` (FAT labels hold 11 characters). Pip writes into its
`PipOS-backups` folder and keeps the newest five sets. The first backup must
be made from the F2 menu, because that creates the backup key.

### Body and system

| Say | Pip |
|---|---|
| "How's your battery?" / "Are you charging?" / "How long will your battery last?" | The real battery reading |
| "How's your battery health?" | *New.* For example "My battery holds 81 percent of what it did when new, after 71 charge cycles." |
| "How warm are you?" / "Why is your fan loud?" | *New.* From its temperature sensors and fan |
| "How bright is it in here?" / "Is it dark in here?" | Uses the ambient-light sensor to describe the room as dark, dim or bright. Ask "How many lux is it?" for an approximate reading. Pip says when the reading is unavailable. |
| "How much space do you have?" / "How much memory are you using?" / "How long have you been awake?" | *New.* Disk, memory and uptime |
| "Are you okay?" / "Run a check-up" / "What's wrong?" | *New.* Up to three findings, the worst first |
| "What did you do last night?" / "What have you been up to?" | *New.* From its action log, which keeps counts and kinds only, never words |
| "Stop listening" | *New.* Mutes the microphone. Press **M** to let it listen again |
| "Brighter" / "Dimmer" / "Set your brightness to 50 percent" | *New.* The screen's brightness, never so dim that the eyes vanish. At either end it says "That's as bright as I go." or "That's as dim as I go." A voice change is undone when Pip restarts |
| "Go to sleep" / "Good night" | Sleeps until you wake it |
| "Shut down" / "Switch off" / "Restart yourself" | *New.* Asks first and takes only a plain yes, then saves itself before it goes |
| "What version are you?" / "Did your update work?" | *New.* Updates themselves are in the F2 menu |

Care (F2, then **C**) has a **Health** page (key **H**) with the same
check-up as "Are you okay?". About every ten minutes while it rests, Pip
quietly checks its own parts; a stuck part may be restarted or the
microphone reset, and that is logged, never spoken.

### The workshop

| Say | Pip |
|---|---|
| "Open the terminal" / "Open a new terminal" / "Launch a terminal" / "Minimise the terminal" / "Show me your face" / "Close the terminal" | *New.* A small terminal beside its eyes. The name may come last too ("Open the terminal, Pip"). While the Try menu has the screen, Pip says "Close the Try menu to see my terminal." The **Launchpad** key opens and hides it as well |
| "Write a script that counts down from ten" / "In the terminal, draw a box" | *New.* It types the program itself |
| "Stop writing that script for a second" / "Carry on" / "Cancel that" | *New.* Pause, resume or stop |
| "Run it" / "Did it work?" / "Fix it" | *New.* Runs only when you say so, and reports what happened |
| "What can you install?" / "Play snake" | *New.* Small programs from its shelf |
| "Show me how you're feeling" / "Open the monitor" / "Show me the clock" / "Show my timers" / "Show me the time" | *New.* A live body monitor, or a big clock with your timers, the next reminders and the next alarm. Opening one minimises the terminal |
| "Show me your face" / "Close the clock" / **Esc** | *New.* Closes the monitor or the clock |

The terminal is off in guest mode, and its files are kept in memory only.
Guests are refused the shelf, the monitor and the clock ("My clock stays
closed while a guest is here. It shows your reminders."). Care **8**
switches the whole workshop off.

### Conversation and play

| Say | Pip |
|---|---|
| "What can you do?" / "What else can you do?" / "What can you do with lists?" | A short tour of what it can do |
| "Are you alive?" / "Are you sentient?" / "What are you?" | An honest answer, always the same, from code |
| "What did you dream about?" / "Did you dream last night?" | From its dream journal only |
| "Can you see me?" / "What do you see?" | Pip reports a recent face detection while camera access is active. It says when the camera is unavailable or its view is unclear. The reply stays out of the conversation archive. |
| "What's 17 times 23?" / "What's 15 percent of 80?" / "Square root of 144" | *New.* Sums, said in words. It says "about" when it rounds |
| "How many grams in a pound?" / "180 Fahrenheit in Celsius" / "How many cups in a litre?" | *New.* Conversions. A cup is the metric 250 ml and a tablespoon the Australian 20 ml, and it says so. Pints, gallons and fluid ounces are UK measures unless you say "US". Data sizes count in thousands |
| "How do you spell necessary?" | *New.* Letter by letter |
| "Tell me a joke" / "Tell me a fun fact" | *New.* From a bundled collection, without repeats |
| "Let's play a game" / "Guess the number" / "Rock paper scissors" / "Twenty questions" | *New.* "I give up" or "Stop the game" ends one |
| "Tell me a story" | *New.* A longer reply from the language model, spoken as it is written |

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
| "Read today's reminders." / "What reminders do I have tomorrow?" | The pending reminders for that day (up to five). If Pip doubts its clock it asks for the time first |
| "Cancel reminder one." / "Cancel the timer." | Removes an item |

- Times can be "at 7:30 pm", "noon" or "in an hour and a half".
- A day word ("today", "tomorrow", "tonight", "next Friday",
  "on 2026-12-01") also needs a full time, as in "next Friday at 9 am".
- "Tonight" means the evening: "tonight at 9" is 9 p.m., and "tonight at
  12" is midnight.
- If you give a time from 1 to 12 without a.m. or p.m. ("at nine",
  "at 7:30") and no "tonight", Pip asks "Did you mean a.m. or p.m.?" Say
  the **whole reminder again** with the full time ("Remind me to call Mum
  tomorrow at nine p.m."). A one-word answer is not understood.
- Always give the task and the time in one sentence.
- Pip refuses these:
  - times that have passed;
  - invalid dates;
  - times skipped or repeated when the clocks change.
- A plain reminder rings once. Repeating reminders ("every weekday at 7")
  and alarms are *new*: see [Things you can say](#time-and-routines).
- Timers can be seconds, minutes, hours or days.
- Pip confirms a one-off reminder with its number, for example "Reminder 3
  set for tomorrow at 9:00 in the morning." Use that number to cancel it.
  Timers, alarms and repeats are confirmed in plain words ("Okay, ten
  minutes, starting now.") and get a number only when two of a kind run.
- "What timers do I have?" counts down ("Timer 2, five minutes left.").
- In a list, past-due items are called "overdue". If a task matches more
  than one reminder, Pip asks for the number.
- Reminders and timers work offline and survive restarts.
- **When the clock is wrong.** Timers, and reminders or alarms said as a
  delay ("in two hours"), count real time: they ring on time even if the
  clock is corrected in between. A reminder for a clock time set while Pip
  doubts its clock waits for a confirmed date. Tell Pip the date and time
  ("It's the 28th of November 2026, twenty to four in the afternoon"),
  or set them in Setup, when the date is uncertain. On an installed PipOS, that screen
  asks before setting the battery clock as well as the system clock.
- Pip announces them only when the lid is open, it is awake and it is not
  already talking. Anything that comes due while it is asleep or closed
  waits until it can speak.
- If an announcement is interrupted, the reminder stays pending and Pip
  tries again.
- **Quiet hours** (see [settings](#making-pip-yours)) delay automatic
  reminders and timers. Alarms still ring. You can still ask Pip for your
  list during quiet hours.

## What Pip remembers, and how to change it

Pip's memory is its self. It lives on its own partition, separate from the
operating system, so updates never erase it.

### What it keeps

By default (**persistent** mode), Pip keeps two things:

- a searchable text record of every conversation: what you said and what
  it replied, with dates;
- **facts** you explicitly ask it to remember.

It never keeps recordings of your voice. Audio is turned into text and
then discarded. Speech it overhears but that was not meant for it is
dropped at once.

### Asking Pip to remember and recall

| Say | Result |
|---|---|
| "Remember that my orchid is called Silverleaf." | Saved as a fact |
| "What do you remember about my orchid?" | Quotes what you told it |
| "What did I say about the café?" | Searches what you said |
| "What did you say about the café?" | Searches what Pip said |
| "What did we talk about last Friday?" | Searches one day |

Recall works best with a name, topic word or date from the original
conversation. When the image carries the small meaning model (blink-8M),
recall also finds things said in somewhat different words. Ask in
completely different words and it may still miss.

Care (F2, **C**, then **4**, *What I remember*) lists the facts it keeps
and forgets any one you choose.

### Picking up a conversation later

Say **“Pick this up later”** after a topic you want to continue.
**“What were we working on?”** brings back that thread even after a restart.
**“Close the thread”** removes the bookmark and leaves the original
conversation in the archive. Pip keeps one open thread for the owner in
persistent memory. Forgetting, correcting or expiring the original
statement also clears its bookmark.

If you bookmarked a dated plan and turned on greetings in Care, Pip
can ask once how it went after that day has passed. It waits until you
address it, the clock is trusted and quiet hours have ended.

### Correcting and forgetting

| Say | Result |
|---|---|
| "That's wrong, I called my orchid Silverleaf." | Replaces the last thing it recalled or remembered. Say the **whole corrected sentence** |
| "Forget that." | Forgets what you just said, or the item it just recalled |
| "Forget about my sister's birthday." | Pip says how many things match and asks you to confirm |
| "Forget everything from yesterday." | The same, for one day |
| "Forget everything." | Everything, after a plain yes |

For topic and day forgetting, Pip says something like *"I found 3 things
to forget. Say yes, and I'll forget for good."* You then have **45
seconds** to confirm. Confirming works like this:

- Answer with just "yes", "yeah", "yep", "go ahead", "do it" or
  "confirm". Answered without Pip's name, "okay" and "sure" do not count,
  because they are as often said to someone else.
- Anything else keeps everything:
  - "no", "never mind" or "forget it" gets "Okay. I've kept everything."
  - anything else, such as a new question, is answered as usual.
- Pip cannot tell voices apart. In guest mode only the guest's own
  requests can be confirmed, and a guest cannot ask it to forget
  everything.
- Closing the lid, or a reminder being announced, cancels the offer.

"Forget everything" asks once more ("Forget everything? Every memory,
list and note, for good.") and waits 15 seconds for a plain yes.

If nothing matches, Pip says "I don't have anything like that to
forget." If "Forget that" has nothing to act on, Pip asks you what it was
about.

Forgetting is thorough:

- It removes the words from the archive and the search index.
- It also removes any replies Pip made that used those words.
- It removes those words from Pip's short-term working memory.
- It cancels any reminder whose task holds the forgotten words.
- It erases the language model's cached copy of the conversation.
- On disk, the deleted data is overwritten.
- A backup made earlier still holds it, and Pip says so once.

### Private conversations

- **"Start guest mode"** or **"Don't remember this"** begins a private
  conversation. It is kept only in RAM. Pip cannot see your history,
  reminders or saved facts while it runs.
- **"End guest mode"** clears it.

### How long Pip keeps things

Care (F2, **C**, then **3**, *Memory*) sets the retention policy. The
change restarts the mind.

| Mode | What Pip keeps |
|---|---|
| **persistent** (default) | Everything. Care **3**, then **4**, can limit conversations to the last *N* days |
| **explicit-only** | Only facts you asked it to remember. New conversation is not archived and is gone when the mind restarts. What was already archived stays until you forget it |
| **session-only** | Nothing survives a restart. Reminders, lists, weather and announcements are off |

## Keeping files

Pip can keep files for you in a private store of its own, called the keep.
Only the owner can use it, and only on an installed Pip with persistent
memory and the lid open. It is not available in guest mode. While Pip has
not yet been told how "Forget everything" should treat the keep, it asks first. Say
"Preserve my keep when I forget everything" or "Erase my keep when I forget
everything". Pip asks you to confirm the choice with a plain yes.

| Say | Pip |
|---|---|
| "What's in my keep?" | Counts the file records, says if any are missing on this laptop, and names the latest |
| "Keep the photos" / "Resume copying photos" / "How far did you get keeping the photos?" | Copies the photos from the USB stick you have plugged in, and reports progress |
| "Keep the file called report" | Copies that one file from the USB stick |
| "Call kept file report by the name taxes" | Gives a kept file a spoken name |
| "Read me taxes from my keep" | Reads a short text file aloud |
| "What does taxes say about rent?" | Quotes matching passages from a kept document |
| "Give me taxes from my keep" | Copies the file to the USB stick without replacing anything on it, after you confirm |
| "Forget kept file taxes" | Removes that file from the keep |
| "Empty my keep" / "Back up my keep" | Empties the keep, or backs up its file contents |

Pip refuses a name that matches more than one file. Files are limited to 64
MB each. Writing to a stick works only on FAT and exFAT. Whether
the keep survives "Forget everything" is your choice above. The normal
encrypted backup holds the keep's catalogue, not the file contents, which
"Back up my keep" saves separately. The built-in SD card reader is
off by default, and Pip's file commands use the USB stick.

When a USB drive is plugged in while Pip is resting and not in guest mode
or quiet hours, Pip may say so, at most once every five minutes. It says what
kind of drive it is (a backup drive, another PipOS drive, a Mac-formatted
drive or a USB drive), its rough size and its label when that is a plain one. It
reads only the drive's description, never its files.

## Calculator

Say “Open the calculator” to show it beneath Pip’s eyes. Type a sum such as
`(2 + 3) * 4` and press **Enter** or **=**. **Backspace** edits and
**Ctrl+L** clears the entry and result. You can also ask Pip a sum while the
calculator is open to see its answer. **Esc** or “Close the calculator”
closes it. The calculation display clears on close or entry into guest
mode. Guests can still ask Pip a sum aloud.

## Lists and Memory

Say “Open the lists” or “Open the memory”, or choose **Lists** or **Memory**
from Quick Menu. In Lists, **Left** and **Right** switch lists. **Page Up**
and **Page Down** move through either panel, and **Esc** closes it.

Lists shows your saved lists and items. You can still say “Add milk to my
shopping list” or “Read my shopping list” to change or hear them. Memory shows
the latest 40 saved facts, matching Setup’s **What I remember** view. Both
panels are read-only, close on entering guest mode, and stay unavailable in
session-only mode.

Press **Command+Space** or **Fn+F6**, or say “Open the Quick Menu.” Use
**Tab** to switch between grid and list views for this session. In list
view, the pane beside the list explains the highlighted item. Press **/**
to search for an app or setting. Use the arrow keys and **Enter** to open
the highlighted result. Choosing a setting focuses the matching control
in Options. **Esc** leaves search or returns from Options to the menu.
At the menu, press **Esc** to dismiss it.

Say “Move the Quick Menu left.” For 15 seconds after an explicit open or move, you can follow it with “Actually, put it on the right.”

Say “Open Options” to keep the eyes on screen while changing your
settings. Use **Tab** and **Shift+Tab** to move between sections.
Use **Up** and **Down** for settings, and **Left**, **Right** or **Enter**
to save a choice. Layout controls the Quick Menu and all three widget
corners. If you set the Quick Menu to Off, ask Pip to open it again to
restore the shortcut. Voice and text controls saved captions, the heard
line and route badges. **Fn+F1** toggles captions for the current face
session. Behaviour controls idle glances and keeps deliberate gaze cues
available.

Setup → Look shares these settings. Say “Show the clock widget” or “Move the system widget to the bottom left.” The clock shows local time. The system widget shows available battery, AC and temperature readings. Widgets stay out of focused apps.

With Internet mode and web lookups enabled, choose **Look it up** in
Quick Menu and type a topic, or say “Search Wikipedia for Ada Lovelace.”
The panel shows the article introduction and its source. “Show the weather
widget” enables a tile that appears after you ask for the current weather.
Its reading expires and clears if online permission ends.

## Journal

Say “Open the journal” or choose **Journal** from Quick Menu. Use **Left**
and **Right** to switch between the **Day record**, **Dream journal**,
**Activity** and **Last night**. **Page Up** and **Page Down** move through entries. **Esc**
closes the panel.

The Day record shows up to 30 recorded days. The Dream journal shows up
to 12 moments Pip selected from your words, as in Care. Activity shows up
to 32 recent actions with dates and counts (for example, when Pip set a
timer). The Journal is read-only. It closes when a guest arrives and stays
unavailable in session-only mode.

**Last night** lists the stages the overnight shift finished and the
package-power reading when the hardware supplied one. Opening the lid
after a long sleep can show a short notice linking back to the Journal.

## Processes and the offline guide

Say “Show me your processes”, or open **Monitor** and press **Tab** to
switch between Body and Processes. The table shows Pip's services and
their automatic restart counts. You can say “Restart your face” or ask
to restart its ears and voice, thinking model, body care or hands. Pip
asks for a plain “yes” before requesting the restart.

“Ask your guide about backup” or “What does F9 do?” reads a short answer
from the offline guide. Replies with instructions offer “Next guide
step.” The guide is also available in guest mode.

Say “Open Options” to go straight to Look. “Open camera settings”,
“Open network settings”, “Open backup settings” and “Open update
settings” open their existing screens.

### Phrase habits

Say “New habit”, then “Habit trigger” followed by the phrase you want.
Say “Habit step” for each supported local action. “Save habit” reads the
sequence back and asks for a plain “yes.” “Run the … habit” tries it on
request. You can list, stop or forget your habits by voice.

You can also give the whole recipe in one turn.
“Pip, new habit when I say good night do set an alarm for seven, then
read tomorrow’s reminders, then go to sleep.” Give an alarm an explicit
local **a.m. or p.m.** time when you always mean that half of the day. A bare
12-hour time such as “seven” keeps that bounded ambiguity: on each run Pip
chooses the next matching seven a.m. or seven p.m., then speaks the exact
date and a.m./p.m. it chose before asking. Durations, repeating alarms and
named dates are not recipe steps. Saving the recipe does not authorize either
state change. Each time you freshly address Pip with the habit phrase, Pip
stops at the alarm and asks for a plain “yes” to set that exact time.
The answer window starts after Pip finishes speaking. If the clock or time
zone changes, or the announced time passes, run the habit again to hear a
new choice. Pip then reads the remaining steps. “Go to sleep” is allowed
only as the last step and asks for a separate “yes.” Alarm and sleep steps
are available for phrase habits.

## Mind

Say “Open the mind” or choose **Mind** from Quick Menu. The surface shows
the network illustration and explains when Pip uses a built-in action
or the model. Below it are counts and up to ten route IDs from this Mind session.
History contains only those counts and IDs. It clears when a guest
arrives or the Mind restarts, and recording is paused in explicit-only
mode. **Esc** closes the panel.

## Making Pip yours

Press **F2** on the face, or choose **4 – Setup** from the USB menu.

| Key | Setting |
|---|---|
| **1** | Its name |
| **2** | What it calls you |
| **3** | Wake phrase. It must be words the recognizer knows. Renaming Pip keeps the old wake phrase until you change it |
| **4** | Original voice: four built-in Flite voices |
| **5** | Speaking speed |
| **6** | Personality: *Warm and curious*, *Calm and concise*, *Playful and dry*, *Gentle and reflective*, random, or tune five traits yourself |
| **7** | Surprise me: random name, voice and personality. The wake phrase also becomes "Hey" plus the new name, if the recognizer knows that name |
| **8** | Network mode (installed Pip only) |
| **9** | Quiet hours and announcements |
| **L** | Look with eye colour, eye shape, glow strength, empty-screen face size, brows and mouth |
| **V** | Voice commands and keys for Look, screen apps and the calculator |
| **N** | Natural or original voice: automatic, natural (Piper) or original (Flite), with samples and a speed measurement |
| **C** | Senses and memory (Care). See the table below |
| **A** | Volume and microphone: levels, a test sound and a microphone meter |
| **T** | Time zone |
| **B** | Back up to a USB stick (installed Pip only; on a Try stick it is item **7** of the start menu) |
| **U** | Install an update on the installed system. USB and any approved download source |
| **Y** | Talk to Pip by typing (installed Pip only; on a Try stick it is item **6** of the start menu) |
| **P** | Hear a sample. Press **F10** first to mute the microphone so Pip doesn't hear itself |
| **K** | Keep your changes |
| **Q** | Cancel |

Look changes save immediately. Return to the face to see them. The default
is full-size soft cyan eyes, with brows and mouth off. Face size applies
when no app is open. Try keeps the look until you reboot.

You can also say “Make your eyes blue,” “Make your eyes round,” “Set your
glow to seventy percent,” “Make your face smaller,” “Show your brows,”
“Hide your mouth,” or “What is your current look?”

Ask “What can I say?” for examples. Ask about appearance, screen apps,
the calculator or keys for a focused list. Setup’s **V** page shows the
same commands in writing, along with keyboard shortcuts. Return to Pip’s
face before opening a screen app.

**Care** (key **C**):

| Key | Page |
|---|---|
| **1** | Camera on or off |
| **2** | Dreams on or off. Choose nights or whenever the lid is closed on power |
| **3** | Memory mode, and forgetting conversations after a number of days |
| **4** | What I remember: read the facts and forget one |
| **A** | Habits: your phrase habits, with their triggers and steps |
| **C** | Curiosity on or off. When on, Pip may ask one gentle question after a conversation you start, at most once on a trusted local day. Your answer is saved only if you then say yes to "Shall I remember that?" |
| **5** | Dream journal |
| **6** | Date and time (for a clock that went wrong) |
| **7** | Time zone |
| **8** | Workshop on or off |
| **9** | Network: join Wi-Fi, wired Ethernet on or off, forget Wi-Fi (installed Pip only) |
| **R** | Previous system: try the last confirmed system copy once (installed Pip only). See [Updates](#updates) |
| **E** | Endurance: native soak and power-cut evidence (installed Pip only) |
| **P** | Permissions for camera, network, workshop, announcements and dreams |
| **D** | Boot diagnostics on the stick on or off (Try only) |
| **H** | Health: the same check-up as "Are you okay?" |
| **L** | Offline library. See what the library includes and allow or turn off an installed pack |

Say “Open permissions” to see the page or “What are you allowed to do?”
to hear a summary.

How saving works:

- **K** on the main menu asks "Save these choices?". **Q** cancels the
  changes made with options 1 to 7: name, what it calls you, wake
  phrase, voice, speed and personality.
- Network, quiet-hours, Care, volume, time zone and voice-engine changes
  are saved inside their own screens. Network applies when you answer
  **y** to "Apply these network settings?". Quiet hours save when you
  press **K** on that screen.
- For Internet mode, answer **y** to "Allow requested public web
  lookups?" to enable weather.
- The time server must be typed as an IP address. Leave it blank to keep
  the device clock.

On an installed Pip:

- **Reboot** to apply name, voice, wake phrase and personality changes.
- Network and quiet-hours changes take effect as soon as you confirm
  them. Memory mode and time zone changes restart the mind.

In Try, everything applies immediately and lasts until you restart.

**Quiet hours and announcements** (option 9):

| Key | Setting | Default |
|---|---|---|
| 1 | Automatic reminders and timers | On |
| 2 | Once-daily greeting (needs the camera) | Off |
| 3 | Quiet hours, as a 24-hour start and end time. The window can cross midnight | None |
| 4 | Clear quiet hours | — |
| 5 | Morning brief | On |

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
- At 15 % or below (or about 30 minutes left), unplugged, it looks tired
  now and then.
- Unplugged, Pip asks to be plugged in when the battery reaches 15 % or
  about 30 minutes left, and asks again more urgently when about 10
  minutes are left. The readings must hold for half a minute first, so one
  bad reading never sets it off. When you plug it back in, it says
  "Thanks, that's better."
- At 5 % or about 5 minutes left (or a pack voltage that stays very
  low for half a minute), Pip saves everything, says goodbye and switches
  the laptop off, so the battery never cuts out under it. Plugging in
  during the 20 seconds of warning cancels the switch-off.
- **With the lid closed on battery for 10 minutes**, Pip saves itself and
  switches off. Plugged in, it stays on with the lid closed.

These thresholds are guesses for an old battery and have not yet been
checked on the real laptop.

**Sleep.**

- Pip counts 10 p.m. to 7 a.m. (in your time zone) as night, and a room
  under about 5 lux as dark.
- After 15 quiet minutes at night or in the dark, it gets drowsy. It
  falls asleep 3 minutes later.
- If it fell asleep on its own, it wakes once the room is light and it
  is after 7 a.m.
- "Good night" makes it sleep until you say its name or open the lid.
- Saying its wake phrase ("Hey Pip" by default) wakes it at any time
  while the lid is open and the microphone is not muted.

**Power cuts.** Pip's memory is designed to survive sudden power loss.
After an unexpected shutdown, Pip knows it lost power and may mention it
for the first few minutes after waking.

## Network, weather and privacy

Pip starts **Offline**. Talking, memory, reminders, timers and the face
all work without any network.

Change the mode in **F2 → 8 Network** (installed Pip only; Try stays
offline):

| Mode | What it allows |
|---|---|
| **Offline** (default) | Nothing leaves the laptop |
| **Local network** | Joins your network, so a paired Mac can do maintenance. Nothing reaches the Internet |
| **Internet** | Full access. **Weather** also needs you to allow *web lookups*, and **network time** needs an approved time server address |

Wi-Fi and wired Ethernet are set in **F2 → C → 9 Network**. A network
link only comes up in Internet mode, and the page offers to switch. The
laptop's built-in Broadcom Wi-Fi needs firmware that is not packaged yet,
so for now use the laptop's Ethernet port.

Weather uses the free Open-Meteo service and reports in °C. A lookup
sends Open-Meteo the city name, or that city's coordinates, and nothing
else about you. With lookups off, Pip says it needs Internet mode and
permission.

**Privacy at a glance**

| What | Where it goes |
|---|---|
| Your voice | Turned into text on the laptop. The audio is never stored |
| Conversations and memories | Only on the laptop's `/mind` partition, and in backups you make |
| Camera images | Off by default. When on, images never leave the face detector. Only "a face is here, at this position" is used |
| Language model | Runs on the laptop. Nothing is sent to any AI service |
| Boot diagnostics (Try sticks) | Hardware details and system messages on the stick's `PIPLOGS` partition, never what you say. Care **D** turns them off. The one exception is a *cable development stick*, which is for development only: it writes what Pip heard into those messages, and the **Voice debug** line of its `summary.txt` says ON |
| Diagnostics (Try menu **8**) | Measurements, test sounds and Pip's own voice on the `PIPLOGS` partition, and also the microphone's recordings made while a test sound or Pip's voice plays, which pick up whatever else is in the room. Recordings of the quiet room and of you speaking, and what you said, stay in memory and are deleted, unless you answer yes when the Voice check asks |

## The camera

The camera is **off by default**. When enabled:

- Pip's eyes follow your face.
- The camera stops whenever the lid closes, and while Pip is drowsy,
  asleep or still waking.
- The camera's green light should show while it is capturing. This has
  not yet been checked on the real MacBook.

Seeing a face does not make Pip listen. You still need to say its name.

To enable it, use **F2 → C → 1 Camera** or say “Hey Pip, you can use your
camera” and press **E** on the permission screen. Say “Hey Pip, stop
watching” to remove camera permission. The optional daily greeting uses
the camera to know someone is there.

*Not yet:* deliberate vision. Asked "What do you see?", Pip says its
camera is off, or that it can tell someone is there but cannot make out
things.

## Dreaming and the night's chores

On an installed Pip with persistent memory, “Sleep on my notes” asks Pip
to pick out key points overnight. It reads back the source and waits for
a plain “yes.” You can also name a list or ask it to use yesterday's
conversation. Once the lid is closed and power is connected, Pip uses the
night model to select excerpts. It reads the original words from their
source. If you change or forget the source, the old draft is unavailable.

“What are you sleeping on?” lists recent jobs. Say “Read my night draft”
or “Read night job one” to hear one. “Cancel night job one” asks before
deleting the request and draft. Your explicit request allows this work
even when automatic dreaming is off.

While it sleeps, Pip does its night's chores, and, if you opt in, it
dreams. The night's work runs only when all of these hold:

- Pip is asleep with the lid closed and plugged in;
- it is within the permitted window in your time zone;
- memory is in **persistent** mode and guest mode is off;
- its temperature reading is 65 °C or lower.

Opening the lid, saying its name or pressing a key stops the work within
about two seconds, and the next quiet spell carries on where it left off.

The chores run in a fixed order: a database check, gentle expiry (if you
set a limit), search-index and memory-vector upkeep, the rule-based
journal, the day record ("what did we do yesterday?"), a backup to a
labelled stick, the dream model, a weekly re-check of the model files,
and warming the conversation model for the morning. The upkeep runs every
such night. The journal and dream model need your permission. **F2 → C → 2
Dreams** offers recurring permission at night or whenever the lid is closed
on power. For one night, say “Hey Pip, you can dream tonight,” check the
date and midnight-to-5 a.m. window on screen, and press **Y**. Say “Hey Pip,
stop dreaming” to revoke permission. You can still ask about an earlier
dream from the saved journal.

- **The journal** notes things you said that seem worth remembering, and
  uses those notes to find the right memory later. It only ever points
  back to your own words. Care **5** shows it.
- **The dream model**, when the image carries it, is a second, larger
  model that reads the day's conversation and suggests what was worth
  keeping. Code checks every suggestion against the stored words. A
  suggestion is kept only as a candidate, and is never told to you as a
  fact until it comes up again on a later night or you confirm it.

If it did some work overnight, its wake blink has a little flutter.

## Installing Pip on the laptop

**Installing erases the laptop's internal drive, including macOS.** Make
sure you have done the three chores in [What you need](#what-you-need)
first.

1. Boot the USB stick and choose **2 – Install PipOS**.
2. Pick the internal disk. The installer shows its model, serial number,
   size and the exact layout. It never offers the USB stick itself.
3. Choose which Try settings to keep. If you have talked with Pip in
   Try, you can also choose **Bring Pip home** to carry the conversation
   archive and identity into the installation.
4. Type the confirmation exactly as shown, in capitals, for example
   `ERASE /dev/sda`. If the disk still holds macOS or a PipOS being, Pip
   shows a warning and then asks for a second confirmation:
   `ERASE MACOS` or `ERASE BEING`. Any difference, or a blank line,
   cancels with "The disk was left untouched." If Pip cannot read the
   partition table, the second confirmation is `ERASE DRIVE`.
   Old memory data may still be recoverable after installation.
5. Keep the Mac plugged in and the stick in place while Pip writes and
   verifies the installation. This takes a few minutes.
6. When it says it has finished, power off, **remove the USB stick**, and
   power on.

Disks the installer will not use appear under "Not available", with the
reason: in use, too small, read-only, or could not be checked.

**5 – Check hardware** also saves its report to the stick's `PIPLOGS`
partition, so you can share it, with the sound chip's full settings at the
end. Press **G** there for three guided checks
(the top-row keys, the screen going dark, and, with your permission, the
camera light). Their results are saved with the boot's diagnostics.

A freshly installed Pip is a *new being*. It has a new birth date, a new
identity and an empty memory, unless you restore one.

## Backing up Pip

Pip's memory is the being. If the SSD dies without a backup, that Pip is
gone. Backups are encrypted with **age**. You need three things, kept
separately:

1. **A key file.** Made the first time you back up, and saved on the
   backup stick. Move it to a different stick, or somewhere safe:
   anyone who has the key and a backup can read the backup. You can
   protect it with a passphrase.
2. **Snapshots.** Encrypted copies of Pip's memory, identity and settings.
3. **A "deletions" proof and checkpoint.** These ensure that things
   you told Pip to forget stay forgotten after a restore.

**From the menu.** Press **F2**, then **B** (on a Try stick, item **7**
of the start menu). Choose the stick; Pip writes only a new
`PipOS-backups` folder on it. When it finishes it shows a checkpoint
such as `1a2b3c4d…9f8e7d6c`. Write it down or photograph the screen: a
restore asks you to compare it.

**By voice.** "Back yourself up" writes to a stick labelled for backups
(see [Lists, notes and memory](#lists-notes-and-memory)), using the key
made from the menu. "When did you last back up?" says when.

**At night.** With a labelled stick plugged in, the night's chores
include a backup, at most about once a day.

At a maintenance console the same tool is `being-backup`:

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
- the retention mode, quiet hours and announcement settings;
- the conversation archive and facts;
- your lists and notes (not in session-only mode);
- the action log, which holds kinds and counts only.

**What it leaves out:**

- reminders, alarms and timers, and the weather city;
- Wi-Fi and network settings, and camera and dream consent;
- The manual core, the overnight core profile index and the model's caches.

**To restore**, boot the USB stick and choose **3 – Restore a being**:

| Option | Gives you back |
|---|---|
| **with conversation archive** | The whole being. Needs the snapshot, key, deletions proof and checkpoint |
| **identity and settings** | The same Pip (name, birth, personality) with an empty memory |

**Restoring is an installation.** Pip asks you to pick the internal disk
and type `ERASE /dev/sdX` (plus `ERASE MACOS` or `ERASE BEING` if the
disk still holds one), and that disk is wiped before the backup is
written to it.

- Put the backup files and key on a USB drive formatted FAT, exFAT or
  ext4. Pip mounts it read-only.
- Pick files from the list, or type full paths.
- A key with a passphrase asks for it once.
- For a full restore, Pip reads the checkpoint record from the stick and
  shows its short form to compare with the one you wrote down. Without a
  record you type the 64-character checkpoint; it stays hidden as you
  type.
- If something does not match, Pip says "Backup restore failed. Verify
  the backup, identity, and any supplied checkpoint, then retry."
- If the image lacks the backup tools, Pip says that restore is still
  pending on this image.

## Updates

PipOS keeps two copies of its operating system. An update is written to
the copy that is not running. The next time the laptop restarts, it
starts from the new copy.

- If the new copy works for a healthy minute, it becomes the main one.
- If it fails to start properly three times, the laptop goes back to the
  old copy by itself.
- Updates must carry PipOS's signature. Unsigned or wrong updates are
  refused.
- System slots and the archive use separate partitions. With an enrolled
  backup recipient, an installed update creates an encrypted checkpoint
  before writing its target slot.

If a memory or model continuity check fails on a system that already passed
confirmation, Pip keeps the current system available. Ask “Did your update
work?” or open **Care → Health** for the startup warning.

“Did your update work?” uses the slot recorded by the installer. Pip can say
when the update is waiting for a restart, when it passed its health check, or
when it used up its startup attempts and Pip is back on the previous system.

In **Care → Previous system**, Pip can try a previous confirmed slot that
understands the current archive. Review the slot and type the restart
challenge from the screen. You can also say “Go back to how you were” and
answer “Yes” to try the previous system once. If that system fails to
confirm, PipOS returns to the current slot.

To install one, put the `.raucb` update file on a FAT or exFAT USB stick, press
**F2**, then **U**, choose the stick and the update, and confirm. Pip
checks the signature and installed update channel before showing the running
and new versions. Keep the update stick plugged in until Pip reports the
result. RAUC checks that the signed manifest still matches what you confirmed
before writing the spare system copy. Pip offers to restart after success.
Try sticks cannot be updated. At a maintenance console the same step is
`rauc install`.

If a maintainer has approved an HTTPS release and Internet mode is active,
**F2 → U** also offers **Download approved update**. The screen shows the
approved host and size before requesting permission to download. If Pip needs
the foreground or the transfer fails, reopening Update lets you start again.
Press **Q** then **Enter** to stop a download. Once the entire release is checked,
Pip asks separately whether to install it.

## Maintenance commands

Almost every owner control is now in the F2 menu. A few things still need
a maintenance console:

- a hand-written `core.md` ("things it always knows");
- the command-line forms of the menu controls (`being-memory`,
  `being-camera`, `being-wifi`, `being-network`, `being-backup`, `rauc`);
- the battery, bodycare and kernel settings in
  [the reference](REFERENCE.md#environment-variables).

The **laptop** build of PipOS deliberately has no login console, so these
commands need one of these:

- a *maintenance* build, which adds paired-Mac SSH access;
- the Try-only *maintenance image*, which has a root login on the second
  console and measurement tools. It cannot install or restore
  ([LAB.md](../LAB.md#try-only-maintenance-image));
- a lab or virtual-laptop image;
- for development only, a *cable development* Try stick, which a Mac
  reaches over a plain Ethernet cable ([LAB.md](../LAB.md#cable-development-build)).
  It never installs, and it holds a secret key, so it is never given to
  anyone. Adding `being.cable=0` to a boot entry turns off the cable
  link, SSH and the writable copy of the code. Voice debugging stays on,
  because the entry also carries `being.voice_debug=1`; for an ordinary
  Try boot, add `being.voice_debug=0` after it as well (the last value
  counts).

## Troubleshooting

| Problem | Try |
|---|---|
| Black screen after the boot menu | On a Try stick, reboot and choose **safe graphics** (**s**) or **show boot messages** (**v**) to see what happened. Diagnostics are saved to the stick's `PIPLOGS` partition. On an installed PipOS, press **Esc** within the first second after power-on to show its hidden boot menu, then choose **safe graphics** (**s**), **show messages** (**v**) or **previous slot** (**p**). After three starts in a row that never became healthy, it shows its messages by itself |
| Screen blank with the lid open | Press **F9** |
| Pip doesn't react to "Hey Pip" | Check the face isn't showing *muted* (press **M**). Wait for the chirp and for the eyes to lock on before speaking. Get closer and reduce background noise. The wake phrase must be one the recognizer knows. Or press **Space** |
| Pip hears "Hey Pip" but not the rest | Ask "How well can you hear me?". Check the levels in **F2 → A**. On a Try stick, run **8 – Run diagnostics** (below), or reboot and try **alternative microphone** (**m**) or **alternative microphone 2** (**n**). Meanwhile, type to it (**F2**, then **6** or **Y**) |
| Pip's voice sounds garbled or broken up | On a Try stick, run **8 – Run diagnostics**, **2 – Full**: it plays test sounds through the speakers and records them, which tells a speaker or timing fault from the voice itself |
| Pip wakes when you didn't call it | Phrases that sound like the wake phrase can sometimes trigger it, especially with background speech or TV. Choose a more distinctive wake phrase in setup |
| Pip says "I can't think right now. My language model isn't answering." | The model is still loading or has restarted. Wait 20–30 seconds. Direct commands (time, reminders, memory) still work |
| Pip's eyes look dazed | Part of Pip has stopped and is restarting by itself. If it lasts minutes, restart the laptop |
| Pip talks over you or hears itself | It shouldn't, because the microphone is off while it speaks. Press **Esc** to stop it |
| A reply is nonsense or invented | This is the small model. Rephrase, ask a direct question, or correct it ("That's wrong, …") |
| Recall doesn't find something | Use a keyword or date from the original conversation |
| Pip ignored "remember…" right after it answered | In the follow-up window, requests that store something need its name: "Pip, remember…" |
| Weather refused | Set **Internet** mode **and** allow web lookups in F2 → 8 |
| A reminder never spoke | Check whether the lid was closed, it was quiet hours, or reminder announcements are turned off (F2 → 9). With the lid open and announcements on, it speaks at the next chance |
| The laptop switched itself off | It was on battery and either nearly empty or closed for 10 minutes. It saved itself first. Plug it in and power on |
| The laptop rebooted after an update | It probably rolled back to the previous version. Your memory is untouched |

### Running diagnostics

On a Try stick, the start menu's **8 – Run diagnostics** measures Pip's
microphone, speakers, voice and spoken commands, and says in plain words
what looks wrong. Choose:

| Key | Check | Takes |
|---|---|---|
| **1** | **Quick**: the system, the sound chip's set-up, how spoken commands are routed, the model, heat, battery and storage, and a short sound check. Nothing for you to do | About a minute |
| **2** | **Full**: everything in Quick, plus test sounds through the speakers recorded by the microphone, every voice scored, and the speech engines' speed | 10 to 15 minutes |
| **3** | **Voice**: you take part. Pip asks whether to keep your recordings (the answer is no unless you say yes), then asks you to say a few phrases, to rate each voice from 1 to 5, to say "Hey Pip" a few times from about a metre away, and to talk for a minute without its name | About 10 minutes |
| **Q** | Back | |

While the sound card is tested, Pip cannot hear or speak; it comes back by
itself when the check ends, and also if you press Ctrl-C. At the end the
screen shows the top problems and a verdict for the microphone, the
speakers, Pip's voice, the wake word, spoken commands and opening the
terminal by voice, and says where the full report was saved: a
`diag-…` folder in this boot's folder on the stick's `PIPLOGS` partition,
when the stick's logs are on (otherwise it is kept in memory only, until
power-off). Bring that folder back for a closer look.
A developer can run the same checks, and more, as `pip-diag` (see the
[reference](REFERENCE.md#diagnostics-pip-diag)).

## Known limitations

- **Few physical hardware results yet.** Pip has booted once on the real
  MacBook from a USB stick. It heard its name there but not the words
  after it. Speeds, microphone quality, speakers, battery behaviour and
  heat there are not measured yet, and Wi-Fi does not work until the
  Broadcom firmware is packaged.
- **Small model.** Qwen3.5-0.8B is a placeholder until benchmarks on the
  laptop choose the real model. It can invent things and sometimes
  ignores a memory it was given.
- **Speech.** The wake phrase is heard by PocketSphinx. After it, Pip
  uses Moonshine only if it is installed, has a recorded pass of the
  speech test for this processor (no stick records one yet, so Pip uses
  PocketSphinx) and measures fast enough on this laptop. It speaks with
  the natural Piper voice, Alba, when that is installed, unless it
  measures too slow here; otherwise with the robotic Flite voice. All of
  it is English-only.
- **Recall** finds paraphrases only roughly, and only with the meaning
  model installed.
- **Pip cannot tell voices apart.** Whoever speaks is taken as the owner,
  except in guest mode.
- **Not yet:**
  - deliberate vision ("what do you see");
  - reactions to the tilt sensor. Room light already affects when Pip
    sleeps and wakes;
  - a full memory browser (Care shows facts, not conversations).
- **Weather** is in Celsius only.
- **Wi-Fi** on the built-in Broadcom radio needs firmware that is not yet
  packaged. Ethernet is the dependable route.

## Quick reference card

```
WAKE        "Hey Pip" … chirp … speak        follow-ups: 20 s, no wake phrase
STOP        Esc          MUTE   M or fn+F10    SETTINGS  fn+F2   FACE BACK  fn+F9
CAPTIONS    fn+F1        TYPE   fn+F2 then 6 (Try) or Y (installed)
TOP ROW     brightness, Mission Control (settings), Launchpad (terminal),
            keyboard light, play/pause (listen or stop), speaker mute, volume
DIAGNOSE    Try start menu 8: 1 Quick, 2 Full, 3 Voice (report on PIPLOGS)
CLOCK       "What time is it?"  "What's the date?"  "Set my time zone to …"
REMIND      "Remind me to … at …"   "Set a timer for … minutes"   "What are my reminders?"
            "Cancel reminder two"   "Wake me up at 7"   "Snooze"
MEMORY      "Remember that …"   "What do you remember about …?"
            "What did I say about …?"   "What did we talk about yesterday?"
FIX         "That's wrong, <whole correct sentence>"   "Forget that"
            "Forget about …" → "yes" within 45 s
PRIVATE     "Start guest mode" / "Don't remember this"   …   "End guest mode"
BODY        "How is your battery?"   "Are you okay?"   "Good night"
LID         closed = deaf, blind and dark      open = Pip wakes
```

## Glossary

| Term | Meaning |
|---|---|
| **Pip** | The being. Its default name, which you can change |
| **PipOS** | The operating system that makes the laptop into Pip. The code calls it BeingOS |
| **Organ** | One of Pip's programs: face, brainstem, mind, voice, eyes, hands, bodycare, cortex |
| **Brainstem** | The part that keeps track of Pip's state and body |
| **Cortex** | The language model |
| **Care** | The F2 page for senses and memory |
| **/mind** | The partition holding Pip's memory and identity. Updates never touch it |
| **Try** | Running from the USB stick in RAM, changing nothing on the laptop |
| **Slot A / B** | The two copies of the operating system used for safe updates |
| **Guest mode** | A private conversation that is never saved |
| **Life record** | Pip's identity: when it was born, how many times it has woken, whether it lost power |
