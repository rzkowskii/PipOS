<!--
doc: user-guide
verified: 2026-11-23T22:20
sources: mind/commands.py mind/reminder_language.py mind/turn.py mind/skills.py mind/setup.py mind/local_menu.py mind/retention.py mind/backup.py mind/initiative.py crates/face/src/main.rs crates/brainstem/src/machine.rs os/board/beingos/try-grub.cfg os/package/being/being-camera README.md docs/LAB.md docs/HARDWARE.md mind/installer.py mind/recognition.py mind/initiative_setup.py crates/brainstem/src/body.rs
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
5. [Things you can say](#things-you-can-say)
6. [Everyday things to ask](#everyday-things-to-ask)
7. [Reminders and timers](#reminders-and-timers)
8. [What Pip remembers, and how to change it](#what-pip-remembers-and-how-to-change-it)
9. [Making Pip yours](#making-pip-yours)
10. [The lid, battery and sleep](#the-lid-battery-and-sleep)
11. [Network, weather and privacy](#network-weather-and-privacy)
12. [The camera](#the-camera)
13. [Overnight journal](#overnight-journal)
14. [Installing Pip on the laptop](#installing-pip-on-the-laptop)
15. [Backing up Pip](#backing-up-pip)
16. [Updates](#updates)
17. [Settings you cannot reach yet](#settings-you-cannot-reach-yet)
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
| A USB stick | For the Try image. Writing it erases the stick |
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
   - Newer sticks add **alternative microphone** (**m**) and
     **alternative microphone 2** (**n**), two set-ups for a microphone
     that hears "Hey Pip" but not the words after it, and **measure
     speed** (**b**), which times Pip's models and voices on this laptop
     and saves the numbers with the stick's logs.
3. The start menu appears. Press **1 – Try PipOS**.
4. The screen goes black, and then two cyan eyes open. They peek,
   blink and settle. Pip is awake.

In Try mode, everything Pip learns is kept in RAM and forgotten at
power-off. Nothing is written to the laptop's internal drive. This is the
safe way to meet Pip, test the microphone and speakers, and decide
whether to install.

The very first time you say hello to a newly born Pip, it says something
like: *"Oh. Hello. I'm Pip. You're the very first person I've met."*

**Set the time zone first.** PipOS keeps its clock in UTC. Until you say
"Set my time zone to Australia Sydney" (or your own zone), these all run
on UTC:

- the time Pip reads out;
- reminder times;
- quiet hours;
- when Pip gets sleepy at night;
- "yesterday" in memory questions.

"Use the device time zone" goes back to UTC.

If the boot goes wrong:

- With **show boot messages**, the text stays up for 20 seconds before
  the start menu appears. Press Esc, Enter, Space or F2 to go on sooner.
- If the normal entry hits a fatal error, the message stays for 30
  seconds and the Mac then restarts. Hold Option again to come back to
  the stick.
- "Cannot locate the PipOS Try media" means the boot menu could not find
  the stick's partitions. Try another USB port or rewrite the stick.

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
| Eyes shrunk to short, dim, flat lines | **Muted**: the microphone is off |
| Dazed, unfocused eyes | **Unwell**: part of Pip has stopped and is restarting. Give it a few seconds |
| A tired look every so often | **Low battery**: 15 % or less and unplugged |

Pip also shows feelings while it talks: joy, curiosity, surprise,
skepticism, concern or sleepiness. Click or tap the trackpad and it
glances down. If it heard you but could not make out the words, it
squints for a moment, the way someone says "pardon?".

## Talking to Pip

1. **Say "Hey Pip"**, then pause until its eyes lock on you. Or press
   **Space** or **Return**, or click the trackpad, while the eyes rest:
   that does the same.
2. **Speak** your request in one go.
3. Pip thinks, then answers in one or two short sentences.
4. For **20 seconds** afterwards Pip stays attentive. You can ask a
   follow-up without saying "Hey Pip" again.

Controls you can use at any time:

| Key | Does |
|---|---|
| **Esc** | Stop Pip mid-sentence |
| **M** or **F10** | Mute or unmute the microphone. The face shows when it is muted |
| **F2** | Open settings. In Try mode it opens the start menu; choose **4 – Setup** |
| **F9** | Bring the face back if the screen stays blank because Pip has no lid reading |
| **Space**, **Return** or a click | While the eyes rest: listen, as if you had said "Hey Pip". Not while the terminal has the keyboard, and not while muted |
| **F2**, then **6** (Try stick) or **F2**, then **Y** (installed) | Type to Pip instead of talking. It answers on screen and out loud. **Esc** leaves |

If Pip hears you but cannot make out the words, it says "Sorry, I
didn't catch that." (once each time you wake it). The second time within
ten minutes it adds "You can also type to me: press F2, then 6." (on an
installed Pip, "then Y"). Once it has answered you, a cough or the TV in the
few seconds after its reply gets no apology.

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
- **Muted means deaf.** While muted, Pip does not react to "Hey Pip" at
  all, even after you close and reopen the lid. Press **M** or **F10** to
  unmute. "Stop listening" mutes it by voice (*new*), but nothing spoken
  can unmute it, because it cannot hear you.
- **Time limits.**
  - If you stop speaking for 15 seconds, Pip stops listening.
  - If its thinking stalls (no progress for 45 seconds, or 10 minutes in
    all), it gives up, looks concerned and says nothing. Just ask again.
  - Speech stalled for a minute is cut off.
  - If the model returns nothing, Pip says "Sorry. I lost my thought."
- **Pip knows a fixed English dictionary.** It can only write down words
  it knows, so unusual names may be heard as something else, and recall
  then cannot find them. To check how a name was heard, ask "What do you
  remember about …?" straight after telling it. Setup refuses a wake
  phrase that has words outside the dictionary, and says the dictionary
  does not contain that phrase.

## Things you can say

Pip answers most everyday requests with a fixed program, not the language
model, so the answer is instant and exact. These are examples: close
variations work too, and "Hey Pip" in front is optional while Pip is
already listening. Anything not on these lists goes to the language model
as ordinary conversation.

Items marked *new* are the second wave of work (24 November 2026). So far
they have been tested only on the development Mac, and
[STATUS.md](../../STATUS.md) records which of them have landed and which
have reached a stick.

### Hearing and voice

| Say | Pip |
|---|---|
| "What did you hear?" / "What did I just say?" | *New.* Reads back exactly what it heard, for example "I heard: 'set a timer for five minutes'." It keeps that only until it next sleeps |
| "Say that again" / "Pardon?" / "What did you say?" | *New.* Repeats its last line |
| "How well can you hear me?" / "Check your hearing" | *New.* Says how quiet the room is and how clearly your voice reaches it, from measured levels |
| "Speak up" / "Be quieter" / "Set your volume to 5" | Changes its volume |
| "Use your natural voice" / "Use your original voice" / "Which voice are you using?" | *New.* Switches between the natural voice (Alba) and the original robot voice. The natural voice is only chosen by itself if it measured fast enough on this laptop |

### Time and routines

| Say | Pip |
|---|---|
| "What time is it?" / "What's the date?" / "What day is it?" | Reads the clock and calendar |
| "What time is it in London?" / "What's the time difference to London?" | *New.* The time elsewhere |
| "Set the time to 7:30 pm" | Sets its clock (and the laptop's hardware clock) |
| "Set a timer for 10 minutes" / "How long left on my timer?" / "Cancel my timer" | Timers. "How long left" is *new* |
| "Remind me to call Mum tomorrow at 9 am" / "What are my reminders?" | Reminders. See [Reminders and timers](#reminders-and-timers) |
| "Wake me up at 7" / "Wake me at half past six" / "Set an alarm for 6:30 tomorrow" / "Set an alarm for ten minutes" | *New.* Alarms. "Quarter past" and "quarter to" work too. Alarms ring in quiet hours; reminders wait for them to end |
| "Every weekday at 7, remind me to take my tablets" / "Every Tuesday at 8 pm, remind me to put the bins out" | *New.* Repeating reminders |
| "Snooze" / "Five more minutes" / "Okay" / "Thanks" / "Got it" | *New.* When something rings, Pip says it three times, two minutes apart, until you answer. "Snooze" puts it off |
| "Good morning" (your first words of the day) | *New.* A short morning brief: today's reminders, anything about its body that matters, and at most one tentative thing from its night |

If its clock was reset while it was off, Pip asks once: "I lost track of
time. What time is it?" (*new*).

### Lists, notes and memory

| Say | Pip |
|---|---|
| "Remember that my orchid is called Silverleaf" / "What do you remember about my orchid?" | Remembers and recalls. See [What Pip remembers](#what-pip-remembers-and-how-to-change-it) |
| "Forget about my sister's birthday" / "Forget that" | Counts what matches and waits for your yes, then really deletes it |
| "Add milk to my shopping list" / "Add eggs and bread to the shopping list" | *New.* Lists, as many as you like |
| "What's on my shopping list?" / "Take milk off my shopping list" / "Cross off milk" | *New.* It reads back five items, then says how many more |
| "Clear my shopping list" / "What lists do you have?" | *New.* Clearing asks yes or no first |
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
| "How much space do you have?" / "How much memory are you using?" / "How long have you been awake?" | *New.* Disk, memory and uptime |
| "Are you okay?" / "Run a check-up" / "What's wrong?" | *New.* Up to three findings, the worst first |
| "What did you do last night?" / "What have you been up to?" | *New.* From its action log, which keeps counts and kinds only, never words |
| "Stop listening" | *New.* Mutes the microphone. Press **M** to let it listen again |
| "Brighter" / "Dimmer" / "Set your brightness to 50 percent" | *New.* The screen's brightness |
| "Go to sleep" / "Good night" | Sleeps until you wake it |
| "Shut down" / "Switch off" / "Restart yourself" | *New.* Asks first, then saves itself before it goes |
| "What version are you?" / "Did your update work?" | *New.* Updates themselves stay in the F2 menu |

Care (F2, then **C**) has a **Health** page (key **H**) with the same
check-up as "Are you okay?". About every ten minutes while it rests, Pip
quietly checks its own parts; a stuck part may be restarted or the
microphone reset, and that is logged, never spoken.

### The workshop

| Say | Pip |
|---|---|
| "Open the terminal" / "Minimise the terminal" / "Show me your face" / "Close the terminal" | *New.* A small terminal beside its eyes |
| "Write a script that counts down from ten" / "In the terminal, draw a box" | *New.* It types the program itself |
| "Stop writing that script for a second" / "Carry on" / "Cancel that" | *New.* Pause, resume or stop |
| "Run it" / "Did it work?" / "Fix it" | *New.* Runs only when you say so, and reports what happened |
| "What can you install?" / "Play snake" | *New.* Small programs from its shelf |
| "Show me how you're feeling" / "Open the monitor" / "Show me the clock" / "Show my timers" / "Show me the time" | *New.* A live body monitor, or a big clock with your timers, the next reminders and the next alarm. Opening one minimises the terminal |
| "Show me your face" / "Close the clock" / **Esc** | *New.* Closes the monitor or the clock |

The terminal is off in guest mode, and its files are kept in memory only.
Guests are refused the shelf, the monitor and the clock ("My clock stays
closed while a guest is here. It shows your reminders.").

### Conversation and play

| Say | Pip |
|---|---|
| "What can you do?" / "What else can you do?" / "What can you do with lists?" | A short tour of what it can do |
| "Are you alive?" / "Are you sentient?" / "What are you?" | An honest answer, always the same, from code |
| "What did you dream about?" / "Did you dream last night?" | From its dream journal only |
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
| "Cancel reminder one." / "Cancel the timer." | Removes an item |

- Times can be "at 7:30 pm", "noon" or "in an hour and a half".
- A day word ("today", "tomorrow", "tonight", "next Friday",
  "on 2026-12-01") also needs a full time, as in "next Friday at 9 am"
  or "tonight at 9 pm".
- For midnight tonight, say "tomorrow at midnight".
- If you give a time from 1 to 12 without a.m. or p.m. ("at nine",
  "at 7:30"), Pip asks "Did you mean a.m. or p.m.?" Say the **whole reminder again** with the full time ("Remind me
  to call Mum tomorrow at nine p.m."). A one-word answer is not
  understood.
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
seconds** to confirm. Confirming works like this:

- Answer with just "yes", "yeah", "go ahead", "do it" or "confirm".
- Anything else keeps everything:
  - "no", "never mind" or "forget it" gets "Okay. I've kept everything."
  - anything else, such as a new question, is answered as usual.
- Only the person who asked can confirm.
- Closing the lid, or a reminder being announced, cancels the offer.

If nothing matches, Pip says "I don't have anything like that to
forget." If "Forget that" has nothing to act on, Pip asks you what it was
about.

Forgetting is thorough:

- It removes the words from the archive and the search index.
- It also removes any replies Pip made that used those words.
- It removes those words from Pip's short-term working memory.
- It erases the language model's cached copy of the conversation.
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
| **7** | Surprise me: random name, voice and personality. The wake phrase also becomes "Hey" plus the new name, if the recognizer knows that name |
| **8** | Network mode (installed Pip only) |
| **9** | Quiet hours and announcements. Its **5** turns the morning brief on or off |
| **N** | Natural or original voice (also `being-setup --voice`) |
| **C** | Senses and memory (Care): camera, dreams, memory, clock, and the **Health** page (**H**) |
| **A** | Volume and microphone |
| **T** | Time zone |
| **B** | Back up to a USB stick (installed Pip only) |
| **U** | Update from a USB stick (installed Pip only) |
| **Y** | Talk to Pip by typing (installed Pip only; on a Try stick it is item **6** of the start menu) |
| **P** | Hear a sample. Press **F10** first to mute the microphone so Pip doesn't hear itself |
| **K** | Keep your changes |
| **Q** | Cancel |

How saving works:

- **K** on the main menu asks "Save these choices?". **Q** cancels the
  changes made with options 1 to 7: name, what it calls you, wake
  phrase, voice, speed and personality.
- Network and quiet-hours changes are saved inside their own screens.
  Network applies when you answer **y** to "Apply these network
  settings?". Quiet hours save when you press **K** on that screen.
- For Internet mode, answer **y** to "Allow requested public web
  lookups?" to enable weather.
- The time server must be typed as an IP address. Leave it blank to keep
  the device clock.

On an installed Pip:

- **Reboot** to apply name, voice, wake phrase and personality changes.
- Network and quiet-hours changes take effect as soon as you confirm
  them.

In Try, everything applies immediately.

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

Change the mode in **F2 → 8 Network**:

| Mode | What it allows |
|---|---|
| **Offline** (default) | Nothing leaves the laptop |
| **Local network** | Joins your network, so a paired Mac can do maintenance. Nothing reaches the Internet |
| **Internet** | Full access. **Weather** also needs you to allow *web lookups*, and **network time** needs an approved time server address |

Weather uses the free Open-Meteo service and reports in °C. A lookup
sends Open-Meteo the city name, or that city's coordinates, and nothing
else about you. With
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
- The camera stops whenever the lid closes, and while Pip is drowsy,
  asleep or still waking.
- The camera's green light should show while it is capturing. This has
  not yet been checked on the real MacBook.

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

- memory is in **persistent** mode and guest mode is off;
- Pip is asleep with the lid closed;
- it is plugged in;
- its temperature reading is 65 °C or lower;
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
4. Type the confirmation exactly as shown, in capitals, for example
   `ERASE /dev/sda`. If the disk still holds macOS or a PipOS being, Pip
   shows a warning and then asks for a second confirmation:
   `ERASE MACOS` or `ERASE BEING`. Any difference, or a blank line,
   cancels with "The disk was left untouched."
5. Keep the Mac plugged in and the stick in place while Pip writes and
   then checks every byte. This takes a few minutes.
6. When it says it has finished, power off, **remove the USB stick**, and
   power on.

Disks the installer will not use appear under "Not available", with the
reason: in use, too small, read-only, or could not be checked.

**5 – Check hardware** also saves its report to the stick's `PIPLOGS`
partition, so you can share it.

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

**Restoring is an installation.** Pip asks you to pick the internal disk
and type `ERASE /dev/sdX` (plus `ERASE MACOS` or `ERASE BEING` if the
disk still holds one), and that disk is wiped before the backup is
written to it.

- Put the backup files and key on a USB drive formatted FAT, exFAT or
  ext4. Pip mounts it read-only.
- Pick files from the list, or type full paths.
- For a full restore you also type the 64-character checkpoint. It stays
  hidden as you type.
- If something does not match, Pip says "Backup restore failed. Verify
  the backup, identity, and any supplied checkpoint, then retry."
- If the image lacks the backup tools, Pip says that restore is still
  pending on this image.

*Not yet:* automatic or scheduled backups.

## Updates

PipOS keeps two copies of its operating system. An update is written to
the copy that is not running. The next time the laptop restarts, it
starts from the new copy.

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
| Black screen after the boot menu | On a Try stick, reboot and choose **safe graphics** (**s**) or **show boot messages** (**v**) to see what happened. Diagnostics are saved to the stick's `PIPLOGS` partition. On an installed PipOS, press **Esc** within the first second after power-on to show its hidden boot menu, then choose **safe graphics** (**s**), **show messages** (**v**) or **previous slot** (**p**). After three starts in a row that never became healthy, it shows its messages by itself |
| Screen blank with the lid open | Press **F9** |
| Pip doesn't react to "Hey Pip" | Check the face isn't showing *muted* (press **M**). Wait for the eyes to lock on before speaking. Get closer and reduce background noise. The wake phrase must be one the recognizer knows |
| Pip wakes when you didn't call it | Phrases that sound like the wake phrase can sometimes trigger it, especially with background speech or TV. Choose a more distinctive wake phrase in setup |
| Pip says "I can't think right now. My language model isn't answering." | The model is still loading or has restarted. Wait 20–30 seconds. Direct commands (time, reminders, memory) still work |
| Pip's eyes look dazed | Part of Pip has stopped and is restarting by itself. If it lasts minutes, restart the laptop |
| Pip talks over you or hears itself | It shouldn't, because the microphone is off while it speaks. Press **Esc** to stop it |
| A reply is nonsense or invented | This is the small model. Rephrase, ask a direct question, or correct it ("That's wrong, …") |
| Recall doesn't find something | Use a keyword or date from the original conversation |
| Weather refused | Set **Internet** mode **and** allow web lookups in F2 → 8 |
| A reminder never spoke | Check whether the lid was closed, it was quiet hours, or reminder announcements are turned off (F2 → 9). With the lid open and announcements on, it speaks at the next chance |
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
- **Not yet:**
  - deliberate vision ("what do you see");
  - sound effects and chirps;
  - volume control and a spoken mute;
  - spoken low-battery warnings;
  - reactions to the tilt sensor. Room light already affects when Pip
    sleeps and wakes;
  - scheduled backups;
  - a memory browser.
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
