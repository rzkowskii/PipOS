# What Pip can do: the function spec and the wave 2 plan

Written 24 November 2026. This merges three independent designs (a companion view, a performance view and an operating-system view) into one spec. The inputs were: the owner's own messages since the first day; the README; the improvement plan and its wave 1; the workshop design ([design/voice-apps.md](design/voice-apps.md)); the two 23 November research reports; the real first-boot logs from the MacBook (`out/hardware-logs/first-real-boot-2026-11-23/`); and the current code.

How to read it:

- Sections 1 and 2 are for the owner: what Pip is for, and what you can say to it.
- Section 3 covers speed, section 4 the bar for "done".
- Section 5 is the engineering plan for tonight, split into ten lanes. No two lanes edit the same file.

Every number is labelled. **Measured** means it comes from the real laptop's journal. **Estimate** means it does not, and it stays an estimate until bench mode runs on the laptop.

---

## 1. The vision, in the owner's words

### What he asked for

On the first day he wrote:

> "I want to build a stripped down OS dedicated to running AI to make an old laptop into a persistent being, with the hardware fully dedicated to being its brain and body."

Earlier, in the conversation he pasted, he described the machine as one whose "whole purpose is that it "is" the laptop, its own entity so to speak", and added:

> "It can talk, listen, learn... Its storage is used as a persistent 'memory' database"

For the face he wanted "A very simple animated face like on a robot (Think EVA from Wall-E)", "Where the screen IS its face." When the build began he said:

> "You are essentially building an OS with an integrated AI. The OS sort of "is" the AI in a way. A being... An entity. We are building digital life in a way."

Of its nights: "It dreams when you close it, using a second LLM (Like a subconscious mind!) to make sense of its data". Of the model, he said "I think that a 4B model is too heavy considering the target hardware".

He asked for a showcase, "As a proof of concept (As I know the target hardware is constrained)", with voice commands like "Hey Pip, open the Terminal", "Hey Pip, minimise the terminal", "Hey Pip, stop writing that script for a second". And he added: "Obviously I don't want to bog the hardware down".

He named it: "PipOS is a new type of Operating System. A semi-autonomous 'aiming to be' sentient OS."

Then came the first real boot:

> "It installed. But it just blinks and looks at me? I speak and it does nothing. I can't even bring up a console or interact. It doesn't (Pip) talk back."

And tonight: "Focus on optimisation and quality and useful OS functions that 'Pip' can handle."

### What that means, as one picture

You open the lid of a 2012 MacBook Pro and two eyes wake up. There is no desktop, no login and no cloud. You say "Hey Pip" and it answers out loud.

- **It is useful every day.** It keeps your timers, alarms, reminders, lists and notes. It does sums. It knows the time anywhere.
- **It remembers.** It remembers what you tell it, and when you ask it to forget, it really forgets.
- **It looks after its own body.** That means its battery, its heat, its disk and its software. It tells you plainly how it is.
- **It dreams.** With the lid closed and on power, it goes over the day with a second, slower model. Next morning it may say one tentative thing it worked out.
- **It can show off.** On request it opens a small terminal and writes and runs little programs, without slowing its face or its voice.

### What the first boot taught us

The first boot showed that everything else depends on two things: Pip has to **hear** you, and you have to be able to **reach** it some other way when it can't.

On the real laptop, "Hey Pip" was heard. The brainstem went from idle to attending at 155.5 s. Then nothing could be made of what came next, and Pip went back to idle at 175.6 s. The microphone was too quiet. There was no typing path, and Pip never said that it had missed anything. So wave 2 starts with the ears, the typing fallback and an honest "Sorry, I didn't catch that."

### "Semi-autonomous, aiming to be sentient", as behaviour

Pip does some things by itself. Each one is small, predictable and rate-limited, and each is decided by code, not by the language model. It never claims an experience it did not have. Concretely:

| Pip does this on its own | When | Limits |
|---|---|---|
| Rings timers, reminders and alarms, and repeats them until you say "okay" | When they are due and the lid is open | Up to 3 rings, 2 minutes apart. With the lid closed it says it is late on the next opening |
| Asks to be plugged in, saves itself and says goodbye before the battery dies | By time-to-empty, then by percentage | Built (plan section 4) |
| Tells you its day when you first speak to it in the morning | Your first words of the day | Only after you speak, at most once a day, never in guest mode |
| Looks after itself: restarts a stuck part, resets a silent microphone mixer, re-warms its model | When a check fails | Only the listed safe repairs. Each one goes in its log and is told to you if you ask |
| Works the night shift: tidies memory, backs up to a labelled stick and dreams | Lid closed, on power, cool enough | Stops within 2 s when the lid opens, it hears its name or a key is pressed |
| Keeps an action log, so "What did you do last night?" has an exact answer | Always | Counts and kinds only, never words |
| Mentions one thing from its dream | On your first words of the morning | One sentence, always marked "I might have that wrong", built only from quotes it has checked |

**Pip never:**

- speaks in quiet hours, except for an alarm you set;
- volunteers anything in guest mode;
- stores speech that was not addressed to it;
- claims to have heard words it did not hear;
- claims to feel, to be conscious or to be alive.

Asked "are you sentient?", it says, in code, that it is a small program aiming to be more, and that it does not know that it feels things the way you do.

**Initiative budget.** Apart from timers, reminders, alarms and battery pleas, Pip makes at most 3 unprompted remarks a day. Each family (morning brief, nudges, questions) has its own switch in Care (Mission Control, or fn+F2). Saying "stop asking me things" turns questions off.

---

## 2. The function catalogue

How to read the tables:

- **Runs in**: **code** means it is answered by a fixed program, instantly and correctly, with no model. **model** means the 0.8B waking model. **dream** means the 1.7B night model.
- **Cost** is on this laptop. Prompt reading was measured at 22.5 tokens per second, about 44 ms per token. Every code answer costs 0 model tokens.
- **State**: **today** means built and on the main tree. **W2-Ln** means built tonight by lane n (section 5). **later** means it needs the laptop or the owner first.
- **Phrases** are examples of what Pip understands. "Hey Pip" in front is optional when Pip is already listening. Close variants work too, and the routing test (task L3.11) holds every phrase listed here.

### 2.0 Reaching Pip: ears, voice, keyboard

| What | Say or do | Runs in | Cost | State |
|---|---|---|---|---|
| Wake it | "Hey Pip" (wait for the chirp, then talk) | code (PocketSphinx keyphrase) | always-on spotting, a few percent of one core (estimate) | today |
| One breath for commands | "Hey Pip, what time is it?" | code | as above | today |
| Attention key | Press **Space**, or click the trackpad, while the eyes are resting | code | 0 | W2-L10 (face), W2-L6 (brainstem) |
| Type to Pip | The menu key (**Mission Control**, or **fn+F2**; **F2** alone on round-2 sticks before 24 November, fn+F2 on the first stick), then **6** "Talk to Pip by typing" (Try stick; installed: the menu key, then **Y**) | code; replies shown and spoken | 0 | W2-L1 (port of the hotfix) |
| Pip owns up when it missed you | (automatic) "Sorry, I didn't catch that." Once per wake. The second time in 10 minutes it adds "You can also type to me: press Mission Control, or fn and F2, then 6." | code | 0 | W2-L1 |
| What did you hear? | "What did you hear?", "What did I just say?" | code: "I heard: 'set a timer for five minutes'." Kept in memory only for this wake, never stored | 0 | W2-L4 |
| Say that again | "Say that again", "Repeat that", "Pardon?", "What did you say?" | code: repeats its last line | 0 | W2-L4 |
| How well it hears | "How well can you hear me?", "Check your hearing" | code, from measured levels: "The room is quiet and your voice reaches me clearly." | 0 | W2-L1 (levels), W2-L6 (wording) |
| Louder and quieter | "Speak up", "Be quieter", "Set your volume to 5" | code | 0 | today |
| Natural voice | "Use your natural voice", "Use your original voice", "Which voice are you using?" | code | Piper on one thread: real-time factor 0.3-0.5 (estimate). Alba is the default from the first reply (the owner's choice, 24 November); "auto" sets it aside only if it fails its check or measures slower than 0.7 of real time on this laptop | W2-L2 |
| Better hearing | (automatic) After "Hey Pip", Moonshine transcribes what you say | code (sherpa-onnx) | an estimated 0.3-0.8 s after you stop, while the model is idle | W2-L2 |
| A microphone that wakes up right | (automatic) Boost and capture level are set at start; a quiet microphone gets a software lift | code | about 1% of one core (estimate; measured per frame tonight) | W2-L1 |

### 2.1 Time and routines

| What | Say | Runs in | Cost | State |
|---|---|---|---|---|
| Time, date, day | "What time is it?", "What's the date?", "What day is it?", "What's tomorrow's date?" | code | 0 | today |
| Time elsewhere | "What time is it in London?", "What's the time difference to London?" | code | 0 | W2-L5 |
| Set its clock | "Set the time to 7:30 pm", "The time is 7:30" | code; also writes the hardware clock | 0 | today |
| It notices a lost clock | (automatic) If the clock was reset at boot, it asks once: "I lost track of time. What time is it?" | code | 0 | W2-L5, W2-L8 |
| Timers | "Set a timer for 10 minutes", "What timers do I have?", "Cancel my timer", "How long left on my timer?" | code | 0 | today; "how long left" W2-L5 |
| Reminders | "Remind me to call Mum tomorrow at 9 am", "What are my reminders?", "Cancel the reminder about Mum" | code | 0 | today |
| Rings like a person | (automatic) A bell, then "[your name], your ten-minute timer is done." Repeats twice more, 2 minutes apart, until "okay", "thanks", "got it", "stop" or "I'm up". Late rings say so: "It finished 12 minutes ago, while my lid was closed." | code | 0 | W2-L3 (wiring), W2-L5 (wording) |
| Alarms | "Wake me up at 7", "Set an alarm for 6:30 tomorrow" | code | 0 | W2-L5 |
| Repeating reminders | "Every weekday at 7, remind me to take my tablets", "Every Tuesday at 8 pm, remind me to put the bins out" | code | 0 | W2-L5 |
| Snooze | "Snooze", "Five more minutes", "Snooze for ten minutes" | code | 0 | W2-L5 |
| Morning brief | Your first words of the day ("Good morning", "Hey Pip", "How are you?") get a greeting, today's reminders, a body note if something matters, and one tentative dream remark | code | 0 tokens; about 4 s of speech once a day | W2-L5 (composer), W2-L3 (trigger) |
| Quiet hours | Set in Care (Mission Control, or fn+F2) | code | 0 | today |
| Weather and desktop weather widget | "What's the weather?" and "Show the weather widget." The widget shows the latest requested current reading for up to 15 minutes while Internet mode and web lookups are enabled. | Open-Meteo on request | 0 model tokens | implemented |
| Text lookup | "Open the lookup" or "Search Wikipedia for platypus." In Internet mode with web lookups enabled, select **Look it up** from the Quick Menu, type a topic, and press Enter. | One plaintext Wikipedia article introduction and citation | 0 model tokens | implemented |

### 2.2 Memory, lists and notes

| What | Say | Runs in | Cost | State |
|---|---|---|---|---|
| Remember a fact | "Remember that my orchid is called Silverleaf" | code | 0 | today |
| Recall | "What do you remember about my orchid?", "What did I say about the orchid?" | code; paraphrase-aware recall comes tonight: "what's my flower called?" also finds it | about 40-100 ms per search at 5,000 memories (estimate) | today; semantic W2-L8 |
| Correct | "That's wrong, it's called Moonleaf" (just after a recall) | code | 0 | today |
| Forget | "Forget about my sister's birthday" (it counts the matches and waits for your yes), "Forget that" | code | 0 | today; also covers vectors and dream facts W2-L8 |
| What do you know about me | "What do you know about me?", "Do you remember me?" | code | 0 | today |
| Today and yesterday | "What did we talk about?", "What did we do yesterday?", "What did we talk about on Monday?" | code (a nightly day record); later one dream-checked sentence | 0 | W2-L8 (diary), W2-L5 (phrase) |
| Lists | "Add milk to my shopping list", "Add eggs and bread to the shopping list", "Put 'call the bank' on my to-do list", "What's on my shopping list?", "Take milk off my shopping list", "Cross off milk", "Clear my shopping list" (asks yes or no), "What lists do you have?" | code; it reads back 5 items, then "and three more" | 0 | W2-L4 |
| Notes | "Make a note: the plumber comes Thursday", "Note that the spare key is in the blue pot", "Read my notes", "Delete my last note" | code | 0 | W2-L4 |
| Guest mode | "Start guest mode", "End guest mode", "Don't remember this" | code | 0 | today |
| Backups | "When did you last back up?", "Back yourself up" (needs a stick labelled for backups; asks yes or no) | code | seconds to a minute of disk work, run when idle | W2-L6 |

Lists and notes live in their own file on Pip's disk. They survive restarts, go into backups and are erased by "forget everything". A topic forget ("forget about the plumber") leaves them alone: you manage lists with "take it off".

### 2.3 Body and system

| What | Say | Runs in | Cost | State |
|---|---|---|---|---|
| Battery now | "How's your battery?", "Are you charging?", "How long will your battery last?" | code | 0 | today |
| Battery health | "How's your battery health?" gives "My battery holds 81 percent of what it did when new, after 71 charge cycles." It adds "I'm keeping it at 80 percent" only if the charge cap is really holding | code (measured: 4,684 of 5,770 mAh, 71 cycles) | 0 | W2-L6 |
| Heat and fan | "How warm are you?", "How hot are you?", "Why is your fan loud?" | code, from the sensors (21 temperature channels and 1 fan found) | 0 | W2-L6 |
| Disk, memory, uptime | "How much space do you have?", "How much memory are you using?", "How long have you been awake?" | code | 0 | W2-L6 |
| Check-up | "Are you okay?", "Run a check-up", "What's wrong?" | code: at most 3 findings, worst first, for example "My ears restarted twice today; they're fine now." | 1-2 s of disk reads | W2-L6 |
| Self-repair | (automatic, and told on request) Restarts a stopped organ, re-applies the microphone mixer when the mic delivers digital silence | code | 0 | W2-L6 |
| What did you do | "What did you do last night?", "What did you do today?", "What have you been up to?" | code, from the action log | 0 | W2-L6 |
| Stop listening | "Stop listening" mutes, then "Okay, I'm not listening. Press M to let me listen again." | code | 0 | W2-L6 |
| Brightness | "Brighter", "Dimmer", "Set your brightness to 50 percent" | code | 0 | W2-L6 |
| Sleep | "Go to sleep", "Good night" | code | 0 | today |
| Switch off and restart | "Shut down", "Switch off", "Restart yourself" ("Shall I switch off? Say yes."), then it saves itself first | code | 0 | W2-L6 |
| Version and updates | "What version are you?", "Did your update work?" Updates themselves stay in the menu (Mission Control, or fn+F2) | code | 0 | W2-L6 |
| Care screen | Mission Control, or fn+F2: Care, with Health, Backup, Update, Camera, Dreams, Memory, Network, Workshop and Audio | code | 0 | today; Health page W2-L6 |
| Privacy | Close the lid (eyes, ears and camera stop), M mutes, Esc interrupts | code | 0 | today |

### 2.4 Workshop and apps

The Workshop accepts voice commands for the Terminal, Body Monitor and Clock. The
current lab and physical evidence is in [STATUS.md](../STATUS.md).

| What | Say | Runs in | Cost | State |
|---|---|---|---|---|
| Terminal | "Open the terminal", "Minimise the terminal", "Show me your face", "Bring back the terminal", "Close the terminal" | code | about 0 until opened. Panel shown in 1.5-3 s (estimate) | W2-L3 (wiring), W2-L9 |
| Write a script | "Write a script that counts down from ten", "In the terminal, draw a box" | code from the recipe book (it types for about 12 s); "write it yourself" uses the model | recipes 0 tokens; a model script is about 30 new tokens plus up to 400 written, tens of seconds (estimate) | W2-L3, W2-L9; model writing needs the second model slot (W2-L7) |
| Pause, carry on, cancel | "Stop writing that script for a second", "Hold on", "Carry on", "Cancel that" | code | 0 | W2-L3, W2-L9 |
| Run it | "Run it", "Run that again", "Fix it", "What happened?", "Did it work?" | code; reports are built from facts, never by the model | 0 | W2-L3, W2-L9 |
| Shelf programs | "What can you install?", "Install snake", "Play snake", "Run life" | code | 0 | W2-L9 |
| Body monitor | "Show me how you're feeling", "Open the monitor" | code: per-core CPU, temperature, fan, battery and health, memory, disk, model speed, once a second | about 1% of one core while open (estimate) | W2-L9 |
| Clock | "Show me the clock", "Show my timers" | code: big clock, next reminder, countdowns | about 1% of one core while open | W2-L9 |

The terminal is off in guest mode. Files there are RAM-only, and scripts run only when you say "run it". These are the existing workshop decisions.

### 2.5 Conversation and character

| What | Say | Runs in | Cost | State |
|---|---|---|---|---|
| Chat | Anything that is not a command | model (Qwen3.5-0.8B) | today about 50 new tokens (2.2 s) plus 11-14 written (estimate), so about 4-4.5 s to its first word (estimate). Target ≤ 3 s, section 3 | today; faster W2-L3, W2-L7 |
| What can you do | "What can you do?", "What else can you do?", "What can you do with lists?" | code; the answer is kept current | 0 | W2-L5 |
| Honest self-talk | "Are you alive?", "Are you sentient?", "Do you have feelings?", "What are you?" | code | 0 | today |
| Name and age | "What's your name?", "How old are you?" | code | 0 | today |
| Dreams | "What did you dream about?" (answered from its dream journal only), "Did you dream last night?" | code | 0 | today; stage 2 content W2-L8 |
| Sums | "What's 17 times 23?", "What's 15 percent of 80?", "What's 120 divided by 7?", "Square root of 144", "What's 2 to the power of 10?" | code; rounding is said ("about 17.1") | microseconds | W2-L4 |
| Conversions | "How many grams in a pound?", "180 Fahrenheit in Celsius", "Convert 5 miles to kilometres", "How many cups in a litre?" (metric cup, and it says so) | code | microseconds | W2-L4 |
| Spelling | "How do you spell necessary?" | code, letter by letter | 0 | W2-L4 |
| Jokes and facts | "Tell me a joke", "Tell me a fun fact" | code; a bundled bank with no repeats | 0 | W2-L4 |
| Games | "Let's play a game", "Guess the number", "Rock paper scissors", "Twenty questions", "I give up", "Stop the game" | code | 0 | W2-L4 |
| A story | "Tell me a story" | model, with a longer budget (160 tokens), spoken clause by clause | about 160 tokens written, 10-20 s in total, speech starting after the first clause (estimate) | W2-L3 |
| Look | "Can you see me?", "Take a look at this", "What do you see?" | code reports fresh face presence from the current camera grant | 0 | face presence in source, object descriptions pending |

### 2.6 Autonomy (what it does without being asked)

| Behaviour | Rule | Runs in | Cost | State |
|---|---|---|---|---|
| Night shift | Lid closed, on AC, held to bodycare's temperature target. In order: database checkpoint and check, gentle expiry, search index upkeep, memory vectors, rule-based journal, day record, backup to a labelled stick, dream model, weekly model re-check, re-warm for the morning | code, then dream | maintenance takes minutes. Dreaming: 1.7B at an estimated 5-8 tok/s, 50-100K tokens a night against about 10K needed on a typical day (research, estimate) | W2-L8 |
| Dream stage 2 | Extracts facts with exact quotes, which code then checks. New facts are only "candidates" until they recur on another night or you confirm one | dream | as above | W2-L8 |
| Self-care | Charge cap, fan floor and CPU caps (bodycare); battery pleas; organ watchdogs; disk-full guard | code | about 0 | partly today; watchdogs and disk guard W2-L10, W2-L8 |
| Morning continuity | "You're back. It's been three days." The brief, and one dream remark | code | 0 | today (welcome back); brief W2 |
| Accountability | The action log; "what did you do?" | code | 0 | W2-L6 |
| Curiosity | Factory-off in Care. When enabled: at most one fixed gentle question on a trusted local day, at the end of an owner-started conversation; its bounded answer stays in RAM until the separate "Shall I remember that?", "yes" | code | 0 | implemented after wave 2; model-generated questions remain out of scope |
| Follow-ups | Says "How did the dentist go?" the day after you mentioned it | code | 0 | later (after wave 2) |

---

## 3. The speed plan: measured now, targets tonight and later

### 3.1 What the real laptop measured (journal of 23 November)

| Quantity | Measured | Where |
|---|---|---|
| Prompt reading, Qwen3.5-0.8B Q4_K_M, 2 threads, 32-token batches | 22.33-22.59 tok/s (44.3-44.8 ms per token) | journal.txt lines 1062-1102 |
| Identity warm-up | 192 tokens, 6 batches of 1.42 s, 8.5 s of compute | lines 1062-1108 |
| Cortex answering, then warm-up starting | 25.55 s, then about 40.6 s: **15 s wasted** | lines 1054, 1062 |
| Model hash before the cortex starts (USB stick) | 18.1 s (5.78 s to 23.85 s) | lines 1011, 1036 |
| Model load after the hash | 1.3 s (the file is already in the page cache) | lines 1046-1052 |
| Boot | 3.25 s kernel plus 20.67 s userspace | line 1040 |
| Mind start | 23.9 s, **held back by the cortex hash** (`After=being-cortex.service`) | line 1038, being-mind.service:6 |
| Voice organ start | 1.2 s | lines 1006-1011 region |
| Decode speed | **not measured**: the only requests generated nothing | lines 1063-1103 |
| "Hey Pip" heard, then nothing understood | attending at 155.47 s, idle at 175.56 s | lines 1119-1120 |
| Renderer | GLES 3.0, Mesa HD 4000 (IVB GT2): the GPU, not software | summary.txt |
| Battery | 4,684 of 5,770 mAh (81% of design), 71 cycles | hardware-report.txt 778-784 |
| Package temperature at idle | 48 °C | hardware-report.txt 743 |
| Clock | The RTC read 2022-01-03 and systemd moved it to 2026-10-17; the real date was 2026-11-23 | journal; summary stamp |

### 3.2 A warm conversational turn: where the time goes

| Step | MacBookPro9,2, archived v5 (three turns) | Wave 2 target | How |
|---|---|---|---|
| End of speech and final words | 1.0–1.6 s combined | 0.4–0.6 s endpoint plus at most 0.6 s final recognition | Silero VAD and the selected recognizer (L2) |
| Read new tokens | About 1.5 s for roughly 30 tokens, with a stepped prefill cost | About 0.9 s for up to 20 tokens | Reuse the spoken reply prefix, prime the turn boundary and keep code exchanges out of history (L7, L3) |
| Write the emotion tag and first clause | 17.4% of one captured turn | Up to 8 tokens, about 0.5–1.0 s | Shorten the first clause (L7) |
| First audio | Flite took 2.1% of that turn | Piper within 0.4 s for a four-word clause | Synthesis and playback (L2) |
| **End of speech to first word** | One decomposed turn spent 41.9% on endpoint and final recognition, then 38.6% on prefill | **Median at most 3.0 s on the laptop, with 2.5 s as the stretch goal** | |

**A code answer** (time, timers, lists, sums, body): ≤ 1.2 s from end of speech to first audio, and 0 model tokens. The estimate today is 0.6-1.0 s.

**Cold stalls to remove:**

- **After a forget, a guest change or an erase.** Today it re-reads the 192-token identity, 8.5 s (measured rate). Target ≤ 1 s: restore an identity snapshot slot (L3, L7). The snapshot checks itself: if the next request's `cache_n` is not the identity length, it is discarded.
- **After compaction (every 35-40 turns).** Today only 256 tokens are re-warmed, so the next turn reads 300-500 tokens, 13-22 s (estimate). Target 0 s on the next turn: re-warm the whole prompt while idle, in interruptible 32-token batches (L3).

### 3.3 Boot and readiness

| Step | Today (measured) | Target | How |
|---|---|---|---|
| Code commands work (time, timers, typing) | The mind spoke at 8.4 s on the archived v5 MacBook boot | ≤ 8 s after the kernel | The mind starts after the brainstem and answers code commands while the model loads (L10) |
| Model warm and ready | 49.1 s | ≤ 35 s on the Try stick | Start the warm-up on the first healthy reply, not the next 5 s tick (L3) |
| Model hash on the installed SSD | about 1.5-3 s per boot (estimate) | about 0.1 s | Verdict cache keyed on partition, inode, size and times, with a full re-hash weekly in the night shift (L7). The Try stick still hashes, because it has nowhere to keep the verdict |
| Python start-up of each organ | about 47 modules recompiled at every start | 1-2 s saved over a boot (estimate) | Ship precompiled `.pyc` (unchecked-hash), drop `PYTHONDONTWRITEBYTECODE` (L10, L1, L9) |
| Apple startup-disk delay | about 30 s (research) | 0 | Owner step: Option, then Control-Enter on EFI Boot (already in the docs) |

### 3.4 Raw speed: decided by bench mode on the laptop, prepared tonight

| Lever | Expected gain (estimate) | Tonight | On the laptop |
|---|---|---|---|
| Quantization across the AVX1 tinyBLAS and tiled matrix kernels | The gain is pending a native bake-off. Batches of eight or more rows can use tiled AVX1 for K-quants. Single-token decode takes a different path, and Q8_K activation packing is scalar on x86. | Build candidates from the original weights (L7) | Compare native prefill and decode, then audition the candidates |
| Prompt threads 2 vs 4; generation threads 2 vs 3; `--poll 0` | 0-25% | Bench flags (L7) | Bench |
| Batches 64/7 and flash attention off (`LLAMA_ARG_BATCH=64 LLAMA_ARG_UBATCH=7 LLAMA_ARG_FLASH_ATTN=off`) | Measured on the MacBookPro9,2 (25 Nov 2026): a cancelled cold read frees the slot in 1.8-1.9 s instead of 13.9-17.7 s at 512 (the lab's 1.75 s did not hold); the 0.65 s fixed cost on prompts of 12 tokens and more is gone, so the median first token fell from 1.41 s to 0.96 s; decode at a depth of 2000 tokens is 14.0 against 13.0 tok/s. Cold reads are 11% slower | Set (L7) | Measured; re-check flash attention if the KV cache is quantized |
| Weights mapped, not locked (`LLAMA_ARG_LOAD_MODE=mmap`) | The packaged llama-server has no `--mlock` or `LLAMA_ARG_MLOCK`; the native MacBookPro9,2 generator selects `mmap+mlock`, protects 768 MiB for cortex and 256 MiB for voice beneath an explicit 1 GiB `system.slice` allowance, and leaves every other body on this portable default | Native profile is source-complete; portable default remains set (L7) | Confirm locked bytes, effective hierarchical `memory.min`, PSI and absence of page-ins under backup/night load on the physical laptop |
| Second slot (`--parallel 2 --ctx-size 8192`) | The workshop can write code without evicting the conversation cache, which would otherwise cost a cold re-read of 8.5 s or more | Set it (L7); hands already uses slot 1 | Measure memory use and slot 0 `cache_n` after a code job |
| `max_tokens` 96 to 64 | About a third off the longest replies; the CPU is free sooner | Set it (L7) | Audition |
| Strict reply shape (a tag and 1-2 sentences) | More complete replies than today's 6 of 24 | Only if the audition on the Mac does not regress (L7) | Persona check |
| CPU weights: face, voice, brainstem 1000; cortex 100; dream and hands 20 | Steady face and audio under full load | Set them (L10, L1, L7, L8, L9) | Face frame-time log |
| Writable Mesa shader cache | Faster face restarts. The journal shows "Failed to create /root/.cache for shader cache" | `MESA_SHADER_CACHE_DIR=/run/being/mesa` (L10) | |

### 3.5 Memory and CPU budget, awake and dreaming

| Part | RAM (estimate) | CPU |
|---|---|---|
| Waking model and two 4K slots | about 0.7-0.9 GB | both cores only while reading or writing |
| Dream model (1.7B Q4_K_M) and a 2K context | about 1.5 GB, only while dreaming | both cores at the held temperature, Nice 10 |
| ONNX Runtime with Silero, Moonshine tiny and Piper | about 250 MB | Moonshine after you stop speaking; Piper on 1 thread while speaking |
| Static memory vectors (blink-8M) | about 31 MB | under 5 ms per sentence |
| Everything else | under 400 MB | |
| **Total** | **under 3 GB of 16 GB** | |

---

## 4. The quality bar: what decides "done"

### 4.1 Gates for tonight (host and lab)

1. `python3 -m unittest discover -s tests -t .` finishes with **0 failures**. Today it ran 991 tests with 3 failures, all stale:
   - `tests/test_dream_lifecycle.py` still expects a tuple where `Subject` now exists;
   - `tests/test_installed_generator.py` (slots A and B) expects bootstate under `local-fs.target.requires`. Bootstate and models are now `nofail` wants mounts on purpose (plan 1.7).

   `cargo test --workspace` passes, and skips are listed and justified.
2. **Owner-phrase routing test** (`tests/test_owner_phrases.py`, L3). Every phrase in section 2 marked code reaches its handler, with a fake cortex that records **zero** model calls. At least 40 near-miss phrases still go to the model, for example "continue the story", "how's it going" with no job running, and "stop" with no terminal open.
3. **Honesty tests.**
   - Pip never says "I heard" unless a final transcript existed.
   - Dream remarks come only from checked items.
   - Self-talk answers stay in code.
   - A read-back is the exact transcript.
4. **Forgetting tests.** Remember, then recall (creating vectors), then a dream candidate, then a diary topic, then forget. Afterwards the item is gone from `mind.db`, FTS, `episode_vec`, `candidate_facts`, the journal and the diary, and the slot is invalidated. "Forget everything" also clears lists and the action log.
5. **No lane edits a file it does not own.** Each lane's diff is checked against its ownership list (section 5).
6. **Public repository hygiene.** `scripts/check-docs.py` passes, and no project file names the tools used to build it.
7. **QEMU, if an image is built tonight from the merged source:**
   - L0, L1, local screen 19/19, laptop smoke;
   - a real-model L2 turn;
   - the sherpa-onnx Ivy Bridge smoke (`lab/sherpa_avx_smoke.py`, no SIGILL);
   - the apps suite.

### 4.2 Measurements that decide "done" on the laptop (after tonight)

| Measure | Pass | Tool |
|---|---|---|
| End of speech to first word, warm model turn, median of 20 | ≤ 3.0 s | voice timing line plus `lab/try_timings.py` (L1, L7) |
| End of speech to first audio, code answers, median of 20 | ≤ 1.2 s | same |
| "Hey Pip" heard at 1 m, quiet room | ≥ 9 of 10 | owner script |
| "Hey Pip" heard at 2 m, fans running | ≥ 8 of 10 | owner script |
| Commands understood, 40 recorded owner phrases | ≥ 90% with Moonshine; must beat PocketSphinx | `mind/voice_bench.py` (L2) |
| Piper real-time factor, 1 thread, model busy | ≤ 0.5, and decode slows by no more than 15% | bench mode (L7, L2) |
| Moonshine tiny real-time factor | ≤ 0.3 | bench mode |
| Decode tok/s for each quant | recorded; the fastest passing quant ships | bench mode |
| Face frame time with the model busy | p99 ≤ 33 ms | face log (pace.rs) |
| A dream night | Zero invented quotes after verification, a typical day finished, the package at or under the owner's target (80 °C) | night shift log |
| A soak of 24 h, then a week | No required organ restarts, flat memory, the battery held at the cap | plan 8.4 |

### 4.3 Definition of done for each function

A function is done when:

1. its phrases pass the routing test;
2. its spoken lines are fixed in code, at most 25 words, with an emotion tag;
3. its guest-mode behaviour is defined and tested;
4. its stored data is covered by forget, erase and backup, or it stores nothing;
5. a code function makes no model call (fake-cortex count);
6. it appears in "what can you do" and in the user guide.

---

## 5. Engineering: interfaces, then the wave 2 lanes

### 5.1 Conflicts resolved between the three designs

| Question | Decision | One-line reason |
|---|---|---|
| Typing fallback: the hotfix's F2 typing chat, or a new text line in the face | **Port the hotfix's `type_chat.py` (F2, 6)**; add Space and click to attend; face text input later | The hotfix is written and tested; text input in the KMS face is a riskier change |
| Microphone gain: the Python `MicLevel`, or an ALSA softvol stage | **`being-audio-init` plus `MicLevel` now**; softvol only if per-frame cost is over 2% of a core on the laptop | Written and tested; the cost is about 1% (estimate) and is logged tonight |
| Codec quirk to try | **Two Try entries: `m` = `model=mbp81`, `n` = `model=mbp101`** | The hotfix chose mbp81. The 2012 mbp101 fixup sets the DMIC pins; one extra boot settles it |
| Recogniser after the wake | **PocketSphinx keeps "Hey Pip"; Silero VAD plus Moonshine v2 tiny transcribes**; Zipformer dropped | These files are downloaded and MIT-licensed, and the keyphrase spotter already heard "Hey Pip" on the real laptop |
| Confirm commands when recognition confidence is low | **No; destructive commands always confirm** (forget, clear list, close a busy terminal, shut down) | Moonshine gives no confidence score; "What did you hear?" covers the rest |
| Piper as a one-shot command per clause (wave 1), or resident | **Resident, through the sherpa-onnx C API from ctypes in the voice organ**, with Flite as the per-clause fallback | Reloading a 64 MB voice for every clause costs an estimated 0.3-1 s |
| When Piper becomes the default | **From the first reply; set aside only if its idle RTF measured on this laptop is above 0.7** (it was ≤ 0.35 before 24 November); bench confirms | The owner chose the natural voice (Alba) on 24 November, even if it starts a little later (owner decision 13) |
| Workshop code slot | **`--parallel 2 --ctx-size 8192`** | hands already targets slot 1; parking slot 0 would cost 8.5 s or more each time |
| Dream model quant | **Ship the downloaded Q4_K_M as the dream pack; bench Q4_0 later** | It is downloaded and hash-checked; overnight speed is not on the latency path |
| Conversation quant | **Build Q4_0, IQ4_NL and Q8_0 candidates; do not switch tonight** | Plan principle 5: measure on the laptop first |
| Morning brief trigger | **His first addressed words of the local day** (after quiet hours, or 05:00) | Needs no camera and never interrupts; greetings on lid open stay off (owner decision 10) |
| Diary, action log and morning report | **One action log (counts only) plus a nightly day record built in code**; a dream sentence later | Accountability with nothing private stored |
| Lists in `mind.db` or their own file | **Their own `/mind/lists.db`** | Disjoint ownership tonight, and lists are managed explicitly, not by topic forgets |
| New voice bus topics | **Allowed explicitly in the brainstem's `allowed_from`** | Wave 1's bus hardening refuses unknown `voice.*` topics, and every `body.*` topic from the mind |
| Brightness topic | **`being.brightness`**, not `body.brightness` | `body.*` is reserved for the brainstem |
| Battery health phrase | **Handled by `body_talk`**; the `battery` pattern drops "health" | One owner for each phrase |
| "Make a note" | **A durable note** unless it is inside a terminal task | The owner's notes should survive a restart |
| Curiosity questions and follow-up threads | **After wave 2** | The factory-off fixed-question path and explicit answer consent now ship; model-generated questions remain gated. Dated bookmarked follow-ups are separate. |
| Alarms with the lid closed | **Default: no sound.** A new open owner decision | The lid is the privacy switch |

### 5.2 Interfaces every lane codes against (frozen for tonight)

**`mind/reply.py` (L4).** The types every code handler uses:

```python
@dataclass(frozen=True)
class Reply:
    line: str                       # "[tag] words", spoken and captioned
    keep: bool = False              # record the exchange in memory (default: no)
    confirm: "Confirm | None" = None
    action: str | None = None       # "poweroff" | "reboot" | None: run by the mind after line
    logged: str | None = None       # action-log kind, e.g. "list.add"

@dataclass(frozen=True)
class Confirm:
    on_yes: Callable[[], Awaitable[Reply]]
    on_no: Reply = Reply("[neutral] Okay, I've left it.")
    seconds: float = 15.0

@dataclass(frozen=True)
class Context:
    now: datetime | None            # owner's local aware time; None if zone unknown
    clock_trusted: bool
    speaker: str                    # owner name, "guest" or "unknown"
    guest: bool
    state_dir: Path                 # /mind
    owner: str
    last_heard: str | None          # last hear.final this wake (RAM only)
    last_said: str | None           # last spoken line, tag stripped
    body: Mapping[str, Any]         # latest body.state plus body.care fields
    voice: Mapping[str, Any]        # latest voice.level
    send: Callable[..., Awaitable[None]]   # bus send(topic, **fields)
```

**Handler modules.** Each code-skill module exposes `may_match(text) -> bool`, a cheap regular expression that tells the mind not to start a speculative model read. It also exposes `async handle(text, ctx) -> Reply | None`, which returns None to pass the utterance on. `mind/everyday.py` (L4) imports them lazily in this order and skips any that are missing:

`readback, calc, units, lists, play, body_talk, device, removable_media, doctor, action_log, diary, ponder, voice_engines, care_routes, library`

`removable_media` is inert on insertion. Only the owner's explicit “what's on
this stick?” starts its short-lived, networkless reader, and only with the lid
open. It accepts one otherwise-unused USB disk, mounts FAT or exFAT read-only
with `nodev,nosuid,noexec`, and validates diskseq plus the mounted dev_t. The
root parent then forks a reader, chroots it into that read-only mount, closes
every descriptor except a capped result pipe, drops groups, UID/GID and all
Linux capabilities, and only that child counts bounded file metadata without
following links. The parent unmounts before answering. “Eject the stick” opens no filesystem: it
verifies that the one USB disk is not mounted or in use, flushes it, and says
when it can be removed. Installer, restore, encrypted backup and Try-log media
retain their existing explicit workflows.

The Care backup picker is the enrollment boundary for writable media. It asks
before first binding or replacement and stores only the selected partition's
filesystem UUID in `/mind/backup-media.json`; it writes no enrollment marker to
the stick. Once present, voice and night backups resolve and re-check that UUID
instead of trusting a label. Existing labelled destinations remain compatible
until the owner next makes an explicit menu backup. Restore remains read-only
selection of any supported backup source and neither reads nor changes the
destination binding.

Handlers never call the model, and their lines are not archived unless `keep=True`.

**Routing order in `Mind._run_turn` (L3):**

1. pending confirmations (forget offer, any `Reply.confirm`, a terminal close confirm, ring acknowledgement);
2. privacy commands (guest start and end);
3. `AppBridge.parse(text, guest)`;
4. `commands.parse` (memory, time, reminders, alarms, snooze, diary day, self-talk, volume);
5. `everyday.handle`;
6. greeting or check-in, or the morning brief;
7. the model.

The speculative-read gate is `commands.may_be_command(text) or app_commands.may_be_app_command(text) or everyday.may_match(text)`.

**New bus topics.** The brainstem's `allowed_from` lists every one (L6). PROTOCOL.md documents them (L10).

| Topic | From, to | Fields |
|---|---|---|
| `hear.missed` | voice, to all | `reason` ("no_speech" or "unclear") (hotfix) |
| `voice.level` | voice, to all | `floor_dbfs, speech_peak_dbfs, gain, heard_ago_s, asr, tts`; every 30 s while resting, and on request. Numbers only |
| `voice.level_request` | mind, to voice | none |
| `voice.engine` | mind, to voice | `tts`: "piper", "flite" or "auto" |
| `key.attend` | face, to brainstem | none; handled like a wake when resting and not lid-blocked |
| `being.brightness` | mind, to brainstem | `step` (+1 or -1) or `level` (0-100) |
| `body.state` (extended) | brainstem | adds `health_pct`, `cycles` |
| `body.care` (extended) | bodycare | adds `charge_limit`, `cap_honoured` |

**Model pack layout (L7).**

- Every image role has a signed descriptor under `os/package/being/model-packs/<role>/pack.json`, with a sha256 for each file. Optional library media carries its generated descriptor beside the staged pack.
- `model_pack.verify(role)` checks a role and `model_pack.root(role)` returns its directory.
- Sources are `out/models` and `out/voice`, with the hashes in their DOWNLOADS.json files.

| Role | Path on the models partition |
|---|---|
| conversation | `/models/reflex.gguf` (unchanged) |
| dream | `/models/dream/dream.gguf` (Qwen3-1.7B Q4_K_M) |
| vad and asr | `/models/asr/silero_vad.onnx`, `/models/asr/moonshine-tiny/{encoder_model.ort, decoder_model_merged.ort, tokens.txt}` |
| tts | `/models/voice/piper/default/{en_GB-alba-medium.onnx, en_GB-alba-medium.onnx.json, tokens.txt, espeak-ng-data/}` |
| embeddings | `/models/embeddings/blink-8M/{model.safetensors, tokenizer.json}` |
| speaker | `/models/speaker/campplus.onnx` (staged only; unused tonight) |
| bench (bench stick only) | `/models/bench/*.gguf` |
| library (owner-selected) | `/models/library/oewn/{dictionary.sqlite3, LICENSE}`; optional, signed separately, and inactive until `being-library enable` validates it and records consent |
| library_simple (owner-selected preview) | `/models/library/simple-en/{articles.sqlite3, LICENSE, ATTRIBUTION}`; optional, signed separately, and inactive until `being-library enable-simple` records separate consent |

The Tier 1 release input is the standard [Open English WordNet 2025 edition](https://github.com/globalwordnet/english-wordnet/releases/tag/2025-edition).
Stage `english-wordnet-2025.xml.gz` from that release with `LICENSE.md` and
`WNDB_License.txt` from the [release commit](https://github.com/globalwordnet/english-wordnet/tree/dc343f2683279ecbb13fab4e2fd778d7b162d287).
Prepare the database and descriptor on the development Mac without a key.

```sh
python3 -m lab.package_dictionary --oewn-2025 SOURCE_DIR --prepare-only --output STAGE
```

To sign the official source, omit `--prepare-only` and supply the release credentials.

```sh
python3 -m lab.package_dictionary --oewn-2025 SOURCE_DIR \
  --output STAGE --certificate RELEASE.pem --key RELEASE.key
```

The command checks all three source hashes and converts the exact WN-LMF
release. The first staged database is 21,426,176 bytes. The pack carries
written credit to the Open English WordNet team and Princeton University
WordNet. Audition definitions and check live capacity before promoting the pack.
**Care → Offline library** lists the signed source, licence, snapshot and credit
and lets the owner change consent for each installed pack independently.
The owner can also say “enable my dictionary” or “disable my dictionary”. Pip
asks for confirmation before changing the setting. Guests
can use an enabled dictionary but cannot change that setting.

`lab/package_simple_english.py` is the capacity/licensing preview and offline
import path for the official `simplewiki-20261101-pages-articles.xml.bz2`
snapshot. It performs no download and checks the published 360,149,050-byte
size, SHA-1 and MD5 before bounded decompression. It requires the exact CC
BY-SA 4.0 licence plus written snapshot, contributor, dump-URL and modification
attribution. The first prepared pack has 156,392 articles and 49,778 redirects
in a 57,266,176-byte SQLite database. The packager retains bounded markup-free
opening prose and redirects that resolve to retained articles. The signed
pack uses the separate `library_simple` role. Lookup accepts “look up X in my
Simple English library” and “what does [Simple English] Wikipedia say about
X”, resolves an exact title or exact corpus redirect, and speaks Wikipedia
attribution on every answer. When an exact base title has multiple
parenthetical corpus titles, it offers at most four pack-recorded choices.
Say “Simple English option two” within 75 seconds in the same wake to select
one. Pip checks the speaker and consent again. “More from Simple English” reads one bounded
verbatim continuation under the same constraints, with attribution repeated.
Generic “look up” and “tell me about” language
continues through the existing handlers/model. The owner must
enable it separately from the dictionary.
`--intake-check` verifies those small/local inputs without decompressing the
dump and prints the 1 GiB conversion ceiling. The exact installed capacity
decision remains `being-library import-simple --check` against the completed,
signed pack and that installation's real ext4 free blocks and inodes. Fuzzy
matching, corpus publication and a larger partition remain separate decisions.
`--prepare-only --output STAGE` writes the checked database and descriptor
without an envelope or signing key, matching the dictionary packager's review
boundary. A legacy uncompressed XML input requires its caller-reviewed SHA-256,
its own snapshot label and matching Wikimedia dump attribution. Conversion hashes and parses the same opened
regular file descriptor, so replacing a path between those steps cannot swap
in different bytes.

On an already-installed system, root can explicitly import either offline
pack from a packager-produced role directory with `being-library
import-dictionary --source /mounted/stage/library/oewn` or `being-library
import-simple --source /mounted/stage/library/simple-en`. This does not scan
or mount media. The command authenticates and fully hashes the donor while the
models partition is read-only, temporarily remounts that installed partition,
copies into an exact-path staging tree, authenticates and hashes the copied
bytes again, flushes them to disk, atomically publishes an absent role, and
restores the read-only mount. After a power cut before publication, retry on
the next boot. The importer reclaims its private staging tree and rechecks
available space before copying. It refuses Try boots and replacement of an
existing pack. Import never
creates a consent marker: the owner must still enable that role separately.

Add `--check` to either import command to authenticate the staged pack and
report fit without remounting the installed models partition. The JSON includes
free bytes and inodes, estimated space needed for the staged files, and whether
an estimated copy of the currently occupied space would still fit afterwards.
If `unfinished_stage` is set, these figures include its current disk use.
The command exits with a failure status if the pack itself cannot fit. Save
the report from the 256 GB installation alongside the actual pack size and
spoken lookup evidence.

**Night shift (L8).** `NightShift.register(name, job)`. A job has `async run_chunk(budget_s) -> bool`, which returns True when it is done for tonight. `step(eligible)` runs the jobs in the fixed order of section 2.6. `interrupt()` stops at the next chunk boundary within 2 s. Other lanes supply jobs:

- `backup.auto_backup_job` (L6);
- `model_pack.rehash_job` (L7);
- the mind's `rewarm` job (L3).

**Overnight speech.** The installed owner can say “Prepare my speech
overnight” and confirm. After eligible night jobs finish, Voice can use the
current Piper voice to prepare up to eight exact clauses from the morning
brief, upcoming rings and a current Extract draft. The low-priority worker
stops on wake or loss of eligible telemetry. Audio stays in a bounded RAM
cache and only plays on an exact match from the normal reply path. Forgetting
and source changes clear prepared audio. “Stop preparing my speech overnight”
turns the setting off and clears the cache after confirmation.

**The Keep.** On a confirmed persistent installation, the owner first says
“Preserve my keep when I forget everything” or “Erase my keep when I forget
everything” and confirms. “Keep the photos” and “Keep the file called boiler
manual” confirm a particular USB stick before copying. “What's in my keep?”
shows a count and recent names. “Read the notes from my keep” reads short
UTF-8 text. “What does the boiler manual say about pressure?” quotes matching
passages from an exact kept text, Markdown or text-based PDF file. PDF lookup
reads at most the first 40 pages in a separate service. “Sleep on the boiler
manual” asks for confirmation and queues an Extract from a short set of
passages. Pip rechecks the kept file before the job and again before readback.
“Give me the boiler manual
from my keep” confirms the destination
and copies without replacing an existing file. “Call kept file boiler
manual by the name heating manual” saves a spoken name. “Forget kept file
boiler manual” and “Empty my keep” require an exact selection and a plain yes.
Photo copying runs while Pip is idle and pauses when you address it.
“Resume copying the photos” asks you to confirm the same filesystem.
“How far did you get keeping the photos?” reports its last completed
position. File names and quoted text use local speech and captions.

“Back up my keep” makes a separate incremental encrypted file backup on the
enrolled backup stick. Normal archive backups carry the encrypted Keep
catalog and bind its erasures and renamed items to the current proof.
Selected-media Restore offers to recover any missing file contents for the
restored catalog. A file backup can fill an existing entry without adding a
new one.

**Dream server (L8).** `being-dream-cortex.service` runs llama-server on 127.0.0.1:8081 with `--ctx-size 2048 --parallel 1 --threads 2 --threads-batch 2 --reasoning-budget 0 --cache-ram 0`, Nice 10 and CPUWeight 20. It is sandboxed like the cortex and keeps no slot path. It is started and stopped by the night shift through systemctl, and is never enabled at boot.

**Cortex additions (L7).**

- `stream.raw` holds the think-filtered text exactly as generated.
- `Cortex.prime_boundary(messages)` primes the turn boundary.
- `Cortex.slot("save" | "restore", "identity.slot")` is used for the identity snapshot.

**Memory hooks (L8).** `Memory.clock()` consults `clock_epoch.epoch_advanced()` (L5). Lists erase through `lists.erase_all(state_dir)` (L4), which the mind's erase path calls (L3).

### 5.3 Rules for tonight

- **A file not listed under any lane is frozen.** Lanes may read anything.
- **New files are allowed only at the paths a lane lists.** Mind modules stay flat (`mind/*.py`), because `being.mk` installs only `mind/*.py`.
- **The hotfix port is a manual re-application, not a merge.** Its base, `e9241bc`, is not an ancestor of main (`6ccf7f9`). Read `git -C out/hotfix/src diff HEAD` and re-apply each change's intent onto main, keeping wave 1's behaviour wherever both touched the same code: ChirpBank chirps, capture retry, engine selection.
- **Code against section 5.2 and test with fakes.** L3 finishes with an integration pass against the modules that have landed.
- **Nothing needs the real laptop.** Where a choice needs laptop numbers, build the default and the measurement.
- **Never rebuild or modify the frozen first-boot stick.** Do not touch `out/hotfix`.
- **Keep the public repository clean.** No project file, comment or commit message may name the tools or workflows used to build PipOS.
- **The mind stays Python standard library only.** `ctypes` counts as standard library.

### 5.4 The ten lanes

#### L1: Hearing (port of the audio hotfix)

**Owns:**
- `mind/voice.py`, `mind/recognition.py`, `mind/tts.py`, `mind/local_menu.py`, `mind/usb_logs.py`
- new: `mind/mic_channel.py`, `mind/type_chat.py`
- new: `os/package/being/being-audio-init`, `os/package/being/being-audio-init.service`
- `os/package/being/being-voice.service`, `os/board/beingos/try-grub.cfg`, `os/package/being/being-console.c`, `lab/tests/test_laptop_smoke.py`
- tests: `tests/test_voice*.py`, `tests/test_recognition*.py`, `tests/test_tts*.py`, `tests/test_usb_logs.py`, `tests/test_local_menu.py`, `tests/test_mic_port.py`
- new tests: `tests/test_mic_channel.py`, `tests/test_type_chat.py`, `tests/test_voice_apology.py`, `tests/test_voice_feedback.py`, `tests/test_console_keys.py`, `tests/test_try_grub.py`, `tests/test_audio_init.py`

**Tasks:**

- **L1.1 Port the hotfix onto main.**
  - Do: re-apply the worktree diff's intent onto the owned files, keeping wave 1 behaviour where both touched the same code. The hotfix's single chirp is superseded by ChirpBank.
  - Accept: all the hotfix's tests pass on main, and `test_voice_robust` and `test_tts_chirps` still pass. The hotfix's `turn.py` and `test_mind.py` changes are handed to L3.
- **L1.2 `being-audio-init` and its unit.**
  - Do: a oneshot unit with `Before=being-voice.service` and `After=sound.target`. It restores `/mind/audio/asound.state` if present. Otherwise it sets Capture to 85%, Mic Boost to 2 of 3 steps, Master to 85% and PCM to 100%. `being.audio=keep` skips it.
  - The alternative Try microphone entries add `being.mic_test=1`. The level check runs before Voice and requires a confirmed open lid throughout capture. Ordinary Try boots skip it. `being.mic_test=0` overrides it on any Try boot. PIPLOGS records the result or reason for a skip.
  - Accept: `tests/test_audio_init.py` runs the script against a fake `amixer` that lists the CS4206 controls. It checks the boost step, that no analog input playback path is unmuted, and that a missing `amixer` exits 0.
- **L1.3 `MicLevel` auto gain and channel choice.**
  - Do: the floor is lifted towards −60 dBFS, at most 8×, with −3 dBFS headroom. Add a ChannelPicker for `being.mic_channel=auto`. The gain cost is logged once per boot (µs per frame).
  - Accept: unit tests pass, and the gain costs ≤ 0.6 ms per 30 ms frame on the host.
- **L1.4 Missed words.**
  - Do: "Sorry, I didn't catch that." once per wake, never while asleep, muted or speaking. The second miss within 10 minutes adds the typing hint, once a day. Publish `hear.missed`.
  - Accept: tests.
- **L1.5 `voice.level`.**
  - Do: publish `voice.level` (fields in 5.2) and answer `voice.level_request`.
  - Accept: tests; no transcript text in any field.
- **L1.6 Latency line.**
  - Do: one log line per reply, `voice timing {"eos_to_final_s", "final_to_audio_s", "eos_to_audio_s"}`, numbers only.
  - Accept: a fake-clock test, and `lab/try_timings.py` (L7) parses it.
- **L1.7 Engine hooks.**
  - Do: use `voice_engines.choose_tts/choose_asr` (L2) when they import. An engine with `async synthesize(text, wav, rate)` runs in a worker thread. Any engine error falls back to Flite for that clause. Handle `voice.engine` and persist it in `voice.json`, with a version migration.
  - Accept: fake-engine tests.
- **L1.8 Unit hygiene.**
  - Do: in `being-voice.service`, add CPUWeight=1000, drop PYTHONDONTWRITEBYTECODE, and add WatchdogSec=30s with NotifyAccess=main. The watchdog lines go in only after the voice pings through `mind/sd_notify.py` (L3).
  - Accept: tests read the unit.
- **L1.9 Try menu entries.**
  - Do: add "alternative microphone" (`m`: `snd_hda_intel.model=mbp81 being.mic_channel=auto`), "alternative microphone 2" (`n`: `model=mbp101`) and "measure speed" (`b`: `being.bench=1`).
  - Accept: `test_try_grub` passes and the laptop smoke menu count is updated.
- **L1.10 Typing from the menu.**
  - Do: F2 item 6 "Talk to Pip by typing", the "how to talk" line, and Esc passed through in being-console.
  - Accept: the ported tests pass.

#### L2: Voice engines (sherpa-onnx, Silero, Moonshine, Piper)

**Owns:**
- new: `os/package/sherpa-onnx/**`
- new: `mind/sherpa.py`, `mind/asr_sherpa.py`, `mind/tts_sherpa.py`, `mind/voice_engines.py`, `mind/voice_bench.py`
- `mind/setup.py` (voice page)
- `lab/sherpa_avx_smoke.py`, `lab/sherpa_avx_assets.json`; new: `lab/sherpa_layout_check.c`
- tests: `tests/test_sherpa*.py`, `tests/test_asr_sherpa.py`, `tests/test_tts_sherpa.py`, `tests/test_voice_engines.py`, `tests/test_voice_bench.py`, `tests/test_setup.py`

**Tasks:**

- **L2.1 Buildroot package `sherpa-onnx`.**
  - Do: package the prebuilt v2.0.3 linux-x64-shared, pinned by its sha256. Install `libsherpa-onnx-c-api.so` and `libonnxruntime.so` (1.28.2) to `/usr/lib`. Licences go to `/usr/share/licenses/sherpa-onnx`, covering Apache-2.0, MIT ORT and GPL-3 espeak-ng. `LICENSES.md` credits the Alba dataset (CC BY 4.0, CSTR, University of Edinburgh). v2.0.2 is the fallback with the same ORT.
  - Accept: the package files are complete. L10 adds the `Config.in` source line and the defconfig line.
- **L2.2 `mind/sherpa.py` ctypes bindings.**
  - Do: bind only Silero VAD, the offline recogniser (Moonshine v2: `encoder` plus `merged_decoder`) and offline TTS (VITS). Load lazily; a missing library raises `Unavailable(reason)`.
  - Accept: `lab/sherpa_layout_check.c`, compiled on the host against the v2.0.3 header, prints sizeof and offsetof for each struct used, and a test matches them against ctypes.
- **L2.3 `SherpaRecognizer(RecognizerEngine)`.**
  - Do:
    - Compose PocketSphinx `Recognizer` for the wake.
    - While attending, feed Silero VAD in 512-sample windows (threshold 0.5, 0.3 s minimum silence, 15 s maximum speech). Emit `hear.speech_start` on speech.
    - Decode each segment with Moonshine in one background thread; `feed()` returns `hear.final` once the result is ready.
    - No partials; an empty result becomes an empty final.
    - Buffers are dropped on `stop_capture`.
  - Accept:
    - fake-sherpa tests of the event order;
    - `feed()` returns in ≤ 2 ms during a fake 800 ms decode;
    - exactly 1 worker thread;
    - `engine_choice` falls back honestly when files are missing.
- **L2.4 Resident Piper.**
  - Do: `tts_sherpa.PiperEngine` loads once (1 thread, cpu) and `synthesize()` writes a 16-bit WAV with `length_scale = 1/rate`.
  - Accept: fake-library tests; an error raises so the voice falls back to Flite.
- **L2.5 `voice_engines`.**
  - Do:
    - `choose_tts`: "auto" picks Piper from the first reply, and keeps it unless its idle RTF over 3 runs of a fixed 12-word sentence is above 0.7 (0.35 until 24 November). The result is recorded in `/mind/audio/engines.json`, keyed by build-id and voice, and re-measured weekly.
    - `choose_asr`: Moonshine if its files are present and its idle RTF on a test WAV is ≤ 0.3.
    - A handler for "use your natural voice", "use your original voice" and "which voice are you using?".
  - Accept: tests with injected measurements.
- **L2.6 Setup voice page.**
  - Do: list "Natural (Alba)" and "Original"; play a sample; show the measured RTF and whether the choice is automatic.
  - Accept: `tests/test_setup.py`.
- **L2.7 `mind/voice_bench.py`.**
  - Do: measure VAD, Moonshine tiny and base, and Piper RTF, idle and beside a busy cortex. JSON output, used by bench mode (L7).
  - Accept: it runs on the host with fakes. The real-library run is in lab QEMU.
- **L2.8 Ivy Bridge smoke.**
  - Do: `lab/sherpa_avx_smoke.py` loads v2.0.3 under QEMU `-cpu IvyBridge`, then runs VAD, a Moonshine decode of `test_wavs/0.wav` (non-empty text) and one Piper sentence.
  - Accept: no SIGILL. If the lab image is not built tonight, record it as pending; it is not done.

#### L3: Mind core (the turn and its hooks)

**Owns:**
- `mind/turn.py`, `mind/__main__.py`
- new: `mind/sd_notify.py`
- tests: `tests/test_mind.py`, `tests/test_dream_lifecycle.py`
- new tests: `tests/test_turn_*.py`, `tests/test_owner_phrases.py`

**Tasks:**

- **L3.1 Stale dream test.**
  - Do: update `test_dream_lifecycle` for `Subject`.
  - Accept: it passes.
- **L3.2 `hear.missed` port.**
  - Do: port the hotfix's `turn.py` `hear.missed` handling and its `test_mind` cases.
  - Accept: tests pass.
- **L3.3 Routing and confirmation.**
  - Do: route in the order of 5.2. One generic confirmation reuses `_YES` and `_NO`, is bound to the speaker, lasts 15 s and is cleared on a guest change or lid close. Speak `Reply` lines as canned, and run `Reply.action` (poweroff or reboot) after `_prepare_shutdown`.
  - Accept: `tests/test_turn_routing.py`.
- **L3.4 Workshop wiring.**
  - Do: build `AppBridge` in `__main__` and wire its hooks:
    - `observe()` for its subscriptions;
    - `parse()` at step 3;
    - `handle(cmd, say)`;
    - `lease()` around `_converse`;
    - `announcement()` on each beat;
    - `privacy()` on a guest change;
    - `forgotten_episodes()` on forget;
    - `model_note()` in the senses note;
    - `episode()` and `note_episode()` around recording.
  - Accept: `tests/test_turn_apps.py`:
    - "open the terminal" makes 0 cortex calls;
    - "continue the story" with no job reaches the model;
    - guests are refused.
- **L3.5 Rings.**
  - Do: use `Skills.announcement(entry, owner, rings, slept)`, `Reminders.rang` and `acknowledge`. A bell chirp comes first. Repeat every `RING_GAP`, up to 3 times. Acknowledgements and snooze come from L5's command kinds.
  - Accept: tests. No "Timer N is up" remains.
- **L3.6 Morning brief.**
  - Do: on the first addressed turn of the local day, a greeting, check-in or bare wake gets `brief.compose(...)` (L5). A command first gets its answer plus at most one clause about today's reminders. Use `record_greeting`; off in guest mode or when the policy says so.
  - Accept: tests with a fake clock.
- **L3.7 Read-back state.**
  - Do: keep `last_heard` and `last_said` in RAM, cleared on sleep, lid close and guest change, and pass them in `Context`.
  - Accept: tests.
- **L3.8 Speed in the mind.**
  - Do:
    - Start the warm-up within 0.1 s of the first healthy reply, including while the Try menu is up.
    - Re-warm the full prompt after compaction, in interruptible 32-token batches.
    - Identity snapshot: save after the first clean warm-up; restore after a forget, guest change or erase; keep it only if the next `cache_n` is at least the identity length minus 8.
    - Call `prime_boundary` after `reply_done`.
    - Use `stream.raw` for the history kept in the prompt.
    - Keep canned and command exchanges out of the model's history.
    - The story intent sets `max_tokens` 160 for that turn.
  - Accept: fake-cortex tests count the tokens sent and the timing.
- **L3.9 Night hooks.**
  - Do: replace the insides of `_dream_batch` and `_interrupt_dream` with `NightShift` (L8), register the `rewarm` job, and replace `_dream_line` with `dream_journal.dream_report`.
  - Accept: tests.
- **L3.10 `mind/sd_notify.py`.**
  - Do: READY=1 and WATCHDOG=1 through an AF_UNIX datagram; the mind pings every 5 s from its event loop.
  - Accept: a test against a socket. L10 turns on WatchdogSec once this lands.
- **L3.11 Owner-phrase routing test.**
  - Do: `tests/test_owner_phrases.py` covers every section 2 phrase and at least 40 near-misses.
  - Accept: it passes with 0 model calls for code phrases.
- **L3.12 Action-log writes.**
  - Do: record forgets (counts), guest start and end, workshop open and run, shutdown, and backups through `action_log.record` (L6).
  - Accept: tests.

#### L4: Everyday skills (code answers)

**Owns:**
- new: `mind/reply.py`, `mind/everyday.py`, `mind/calc.py`, `mind/units.py`, `mind/lists.py`, `mind/play.py`, `mind/play_data.py`, `mind/readback.py`
- new tests: `tests/test_reply.py`, `tests/test_everyday.py`, `tests/test_calc.py`, `tests/test_units.py`, `tests/test_lists.py`, `tests/test_play.py`, `tests/test_readback.py`

**Tasks:**

- **L4.1 `reply.py` and `everyday.py`.**
  - Do: implement them exactly as 5.2: lazy imports, the fixed order, and `may_match`.
  - Accept: tests with missing modules.
- **L4.2 Calculator.**
  - Do:
    - Spoken numbers, through read-only imports of `app_commands.spoken_numbers` and `skills.number_words`.
    - Operators: "plus", "minus", "take away", "times", "multiplied by", "divided by", "over", "percent of", "squared", "cubed", "square root of", "to the power of", "add X and Y".
    - A whitelist AST with no `eval`; exponents up to 100; results up to 1e15.
    - Answers in words, saying "about" when rounding to 3 significant figures, and an honest line for division by zero.
  - Accept: at least 60 cases, including every phrase in section 2.
- **L4.3 Units.**
  - Do: length, mass, volume, temperature, speed, data, time and cooking. A cup is metric 250 mL, and Pip says so.
  - Accept: at least 50 cases.
- **L4.4 Lists and notes.**
  - Do:
    - `/mind/lists.db` in WAL mode with `secure_delete`.
    - The phrases in section 2.2. "And" and commas split items.
    - Read back 5 items, then "and N more".
    - "Clear" asks yes or no through `Confirm`.
    - Guests are refused.
    - `erase_all()` for the erase path.
  - Accept: tests, including a restart and an erase.
- **L4.5 Play.**
  - Do:
    - At least 150 original, family-friendly jokes and facts in `play_data.py`, with no repeats among the last 100 (`/mind/play.json`).
    - Guess the number, rock paper scissors, twenty questions over about 30 animals, and spelling.
    - Game state is RAM-only. "I give up" and "stop the game" end a game.
  - Accept: tests.
- **L4.6 Read-back and repeat.**
  - Do: "What did you hear?" and "what did I just say?" read the transcript; "say that again", "repeat that", "pardon?", "what did you say?" and "sorry?" repeat the last line.
  - Accept: tests. "What did you say about X" stays recall, which `commands.parse` reaches first.

#### L5: Time, reminders and routines

**Owns:**
- `mind/commands.py`, `mind/skills.py`, `mind/reminders.py`, `mind/reminder_language.py`, `mind/profile.py`, `mind/initiative.py`, `mind/initiative_setup.py`
- new: `mind/brief.py`, `mind/clock_epoch.py`
- tests: `tests/test_commands.py`, `tests/test_skills*.py`, `tests/test_reminders.py`, `tests/test_reminder_language*.py`, `tests/test_profile.py`, `tests/test_initiative*.py`, `tests/test_time_zone_lookup.py`
- new tests: `tests/test_brief.py`, `tests/test_clock_epoch.py`

**Tasks:**

- **L5.1 Alarms and repeats.**
  - Do: migrate `reminders.db` (a nullable rule: daily, weekdays, or weekly on a day). Parse "wake me (up) at…", "set an alarm for…" and "every … remind me to…". Compute the next time in the owner's zone, correct across Sydney's daylight-saving changes. Hold alarms while the clock is doubted.
  - Accept: tests across both DST changes.
- **L5.2 Snooze and acknowledgement.**
  - Do: `Reminders.snooze(id, minutes)` and the `snooze` and `ack` command kinds.
  - Accept: tests.
- **L5.3 Timer replies.**
  - Do: "How long left on my timer?" and "how long until my reminder?". Creation replies carry no ID ("Okay, ten minutes, starting now."); IDs appear only when two items of a kind are running.
  - Accept: tests.
- **L5.4 Time elsewhere.**
  - Do: "What time is it in London?" and the time difference.
  - Accept: tests.
- **L5.5 Announcement wording.**
  - Do: `Skills.announcement` lines of at most 20 words, honest about lateness.
  - Accept: tests.
- **L5.6 Morning brief composer.**
  - Do: `brief.compose(now, reminders_today, body, dream_remark, days_away)` returns at most 3 clauses and 35 words. The `brief` policy switch defaults to on.
  - Accept: tests.
- **L5.7 Lost clock.**
  - Do: `clock_epoch.epoch_advanced()` is true when the RTC's `since_epoch` is before systemd's clock-epoch time.
  - Accept: a test with a fixture for the 2022 RTC and the 2026-10-17 epoch.
- **L5.8 Help text.**
  - Do: new "What can you do?" text, plus "what else can you do?" and per-group lines ("with lists", "with the terminal", "with your body") that cover everything in section 2.
  - Accept: tests.
- **L5.9 Public command gate.**
  - Do: add `commands.may_be_command(text)`. Remove "health" from the `battery` pattern. Add a `diary` kind ("what did we do yesterday", "what did we talk about on Monday") before `recall`.
  - Accept: tests, including that `recall` still works.

#### L6: Body and system (Python and Rust)

**Owns:**
- new: `mind/body_talk.py`, `mind/device.py`, `mind/doctor.py`, `mind/action_log.py`
- `mind/care.py`, `mind/backup.py`, `mind/backup_crypto.py`, `mind/hardware_metrics.py`
- `crates/brainstem/**`, `crates/bodycare/**`, `crates/bus/**`
- tests: `tests/test_care.py`, `tests/test_backup*.py`, `tests/test_hardware_metrics*.py`
- new tests: `tests/test_body_talk.py`, `tests/test_device.py`, `tests/test_doctor.py`, `tests/test_action_log.py`

**Tasks:**

- **L6.1 Battery health fields.**
  - Do: `body.state` gains `health_pct` (charge_full over charge_full_design) and `cycles`; `body.care` gains `charge_limit` and `cap_honoured`.
  - Accept: Rust tests with the 4684000 and 5770000 fixture give 81.
- **L6.2 Bus permissions.**
  - Do: `allowed_from` permits the topics in 5.2: `voice.level` from voice; `voice.level_request` and `voice.engine` from the mind; `key.attend` from the face; `being.brightness` from the mind. The brainstem handles `key.attend` like a wake, and `being.brightness` writes `intel_backlight` within bounds, restoring it on exit.
  - Accept: Rust tests.
- **L6.3 `body_talk`.**
  - Do: the phrases in section 2.3 for battery health, warmth, fan, disk, memory, uptime and "how well can you hear me". Numbers come from `ctx.body` and `ctx.voice`.
  - Accept: tests, including the "cap honoured" wording.
- **L6.4 `device`.**
  - Do:
    - "Stop listening" sends the same `key.mute` path and says how to undo it.
    - Brightness commands.
    - Shut down and restart through `Confirm` and `Reply.action`.
    - "What version are you?" reads `/etc/being/build-id`.
    - "Did your update work?" answers from boot-health and the action log.
  - Accept: tests.
- **L6.5 Doctor.**
  - Do: check organs and their restarts (`systemctl show`), the cortex and last turn speed against the 22.5 tok/s baseline, the mic floor and the time since speech was last understood, free space, `PRAGMA quick_check` on each database, backup age, battery, temperatures, the clock and failed units. Safe repairs: restart one failed organ (never the cortex mid-turn), and re-run `being-audio-init` when the floor is at or below −90 dBFS. Each repair is logged. A Care "Health" page shows the same results.
  - Accept: tests with injected readings. Say at most 3 findings.
- **L6.6 Action log.**
  - Do: `/mind/log/actions.jsonl` holds `{ts, kind, count}` only, rotated at 1 MB, with an enumerated set of kinds. The "what did you do…" handler answers from templates. "Forget everything" clears it.
  - Accept: tests; a canary shows no text ever lands in the log.
- **L6.7 Backup by voice.**
  - Do: "when did you last back up?" and "back yourself up" (stick present, then a confirm). The backup includes `lists.db` and `actions.jsonl`. Provide `auto_backup_job` for the night shift (plan 8.6).
  - Accept: tests.
- **L6.8 Cap detection.**
  - Do: bodycare detects a cap that is not honoured and publishes it, in observe mode as today.
  - Accept: Rust tests.

#### L7: Speed, cortex, model packs and bench

**Owns:**
- `mind/cortex.py`, `mind/model_pack.py`, `mind/speech.py`
- new: `mind/bench.py`
- `lab/package_model.py`, `lab/bench_guest.py`, `lab/bench_report.py`, `lab/try_timings.py`, `lab/model_audition.py`, `lab/turn_latency.py`; new: `lab/quant_candidates.py`
- `config/**`; new: `config/chat-template.jinja`
- `os/package/cortex/**`, except the dream service file (L8). L7 adds its install line to `cortex.mk`
- `os/package/being/default-model/**`, `os/package/being/model-packs/**` (new), `os/package/being/30-model-pack.conf`, `os/package/being/being-bench.service` (new)
- `os/board/beingos/post-image.sh`, `try-image.sh`, `try-genimage.cfg`, `genimage.cfg`
- tests: `tests/test_cortex*.py`, `tests/test_model_pack*.py`, `tests/test_speech.py`, `lab/tests/test_model_pack.py`
- new tests: `tests/test_bench.py`, `tests/test_try_timings.py`

**Tasks:**

- **L7.1 Cortex unit.**
  - Do: `LLAMA_ARG_BATCH=64` with ubatch 7 and `LLAMA_ARG_FLASH_ATTN=off` (measured on the laptop, 25 Nov 2026), `--parallel 2 --ctx-size 8192`, `--threads 2 --threads-batch 2`, CPUWeight=100, `LLAMA_ARG_LOAD_MODE=mmap` (a drop-in may set `mmap+mlock`; this llama-server has no `--mlock`), same-origin CORS only (`LLAMA_ARG_CORS_ORIGINS=localhost`) and OOMScoreAdjust=300.
  - Accept: a unit test parses `ExecStart`. A lab probe shows slot 0's `cache_n` is unchanged after a slot 1 job (pending if no image is built).
  - Optional write trial: `defer_idle_slot_writes` is factory-off. When enabled,
    settled idle housekeeping still saves `context.json` but leaves the warm
    llama slot in RAM; the existing slot file is refreshed only on software
    sleep, lid close or shutdown. Pending installed trials do not gain this
    write path. Forget/privacy cancellation, generation stamps and the
    existing cache-invalidated fingerprint remain authoritative.
- **L7.2 Stable chat template.**
  - Do: past assistant turns render byte-identically to what was generated, including the empty think block. Set `LLAMA_ARG_CHAT_TEMPLATE_FILE` and expose `stream.raw`.
  - Accept: on the Mac, with the real `reflex.gguf`, `lab/turn_latency.py` shows `prompt_n` for turns 2-6 equal to the new user block ±2 tokens.
- **L7.3 `prime_boundary()`.**
  - Do: add `Cortex.prime_boundary()`.
  - Accept: kept only if the same harness shows `prompt_n` falling.
- **L7.4 Pack format v2.**
  - Do: role packs as in 5.2, each file with its sha256, a signed descriptor for each role, and tar.bz2 archives extracted during packaging. `verify(role)` and `root(role)`.
  - Accept: tests. `post-image.sh` stages every role, and `models.ext4` grows with its content.
- **L7.5 Verdict cache.**
  - Do: `/mind/cortex/pack-verdict.json` keyed on partition UUID, inode, size, mtime and ctime in ns, sha256 and build-id. A stat match plus a signature check skips the full hash. `rehash_job` runs weekly in the night shift.
  - Accept: tests for tamper cases (size, mtime, inode, a new build).
- **L7.6 Bench mode.**
  - Do:
    - `mind/bench.py` comes from `lab/bench_guest.py`.
    - `being-bench.service` runs with `ConditionKernelCommandLine=being.bench=1`, as a oneshot, writing JSON to PIPLOGS.
    - It measures:
      - prefill at 16, 32, 128 and 512 tokens;
      - decode of 64 tokens;
      - threads 2, 3 and 4, and prompt threads 2 and 4;
      - every `/models/bench` quant;
      - slot save and restore `cache_n`;
      - cancel-to-release;
      - the dream model;
      - voice RTF through `voice_bench` (L2).
    - `BEING_TRY_BENCH=1` builds the bench stick.
  - Accept: host tests with a fake server.
- **L7.7 Quant candidates.**
  - Do: `lab/quant_candidates.py` builds Q4_0, IQ4_NL and Q8_0 of Qwen3.5-0.8B, and Q4_0 of Qwen3-1.7B, from the original weights, recording hashes and licences.
  - Accept: the script works. Downloads are optional tonight; the default model is not changed.
- **L7.8 Timing reader.**
  - Do: `lab/try_timings.py` parses the voice and turn timing lines and prints medians.
  - Accept: a test on fixture lines.
- **L7.9 Reply length.**
  - Do: `max_tokens` 64. Turn on the strict reply shape only if `model_audition` on the Mac shows at least today's complete-reply rate. Record the numbers in lab notes, not docs.
  - Accept: tests read the config.

#### L8: Memory, recall and dreams

**Owns:**
- `mind/memory.py`, `mind/retention.py`, `mind/consolidation.py`, `mind/dream_store.py`, `mind/dream_journal.py`, `mind/dream_policy.py`
- new: `mind/embed.py`, `mind/dream_model.py`, `mind/dream_verify.py`, `mind/night_shift.py`, `mind/diary.py`
- new: `os/package/cortex/being-dream-cortex.service`
- `os/package/being/being-dream-journal`
- tests: `tests/test_memory*.py`, `tests/test_retention*.py`, `tests/test_consolidation*.py`, `tests/test_dream_*.py` (except `test_dream_lifecycle.py`)
- new tests: `tests/test_embed.py`, `tests/test_night_shift.py`, `tests/test_diary.py`, `tests/test_dream_verify.py`, `tests/test_dream_model.py`

**Tasks:**

- **L8.1 `embed.py` (blink-8M in the standard library).**
  - Do: the BERT normaliser (lowercase, clean text), BERT pre-tokeniser, WordPiece (`##`, `[UNK]`), a safetensors header parse (F32, 30,000×256), mean pooling and L2 normalisation.
  - Accept: cosine ≥ 0.9999 against reference vectors for 20 sentences. Generate the references once on the Mac with the upstream library in a throwaway environment and store them in the test. RAM ≤ 40 MB; ≤ 2 ms per sentence on the host.
- **L8.2 Vector tables.**
  - Do: `episode_vec` and `fact_vec` (int8 plus scale), with `ON DELETE CASCADE` and `foreign_keys=ON`. Embed at record time off the event loop, and backfill in the night shift.
  - Accept: a forget leaves 0 rows.
- **L8.3 Hybrid recall.**
  - Do: FTS top 50 and vector top 50, fused by RRF (k=60), plus recency. A cosine floor gives "I don't recall". Existing recall APIs keep their signatures.
  - Accept: on a 30-query paraphrase set, hit@1 ≥ 80% and above the FTS-only baseline; at most 1 false recall on 20 unrelated queries.
- **L8.4 Incremental FTS.**
  - Do: delete changed rows from FTS in forget and edit; run `optimize` in the night shift.
  - Accept: a forget at 10,000 rows takes ≤ 50 ms on the host.
- **L8.5 Disk guard.**
  - Do: below 10% free, stop archiving overheard text and compact. Below 5%, keep explicit facts only. `memory.disk_state()` reports it for the doctor.
  - Accept: tests.
- **L8.6 Lost clock in `Memory.clock()`.**
  - Do: use `clock_epoch.epoch_advanced()`.
  - Accept: a test.
- **L8.7 `NightShift`.**
  - Do: as in 5.2. All jobs except the checkpoint need AC and bodycare's permission.
  - Accept: a job is interrupted within 2 s, and cursors resume.
- **L8.8 Dream service and client.**
  - Do: `being-dream-cortex.service` as in 5.2. `dream_model.py` uses JSON-schema extraction (the flat schema of the research report, section 2.3), temperature 0 and thinking off. Lines are chunked as `[E1234|owner|2026-11-23 19:02]`, 800-1,200 tokens each, behind a byte-stable instruction prefix of under 300 tokens.
  - Accept: tests with a fake server.
- **L8.9 `dream_verify.py`.**
  - Do: check that the episode id is in the chunk, the normalised quote is a substring of that episode, and the speaker matches. The being's own lines are never owner facts. A claim is at most 160 characters, and its names, numbers and dates appear in the quote. Failures are rejected and never repaired.
  - Accept: at least 30 adversarial cases are all rejected; at least 10 valid cases are all accepted.
- **L8.10 Candidates and forgetting.**
  - Do: `candidate_facts` and derivation tables. A candidate is promoted when it recurs from a different episode on a later night, or when the owner confirms it. Forgetting cascades transitively.
  - Accept: direct, indirect and inferential forget tests pass.
- **L8.11 Morning remark and dream report.**
  - Do: the morning remark and the dream report come from checked items only.
  - Accept: tests.
- **L8.12 Day record.**
  - Do: `diary.py` keeps a nightly record: conversation count, top 3 topics from owner speech, reminders done, timers and body events. It answers the `diary` command kind (via L3), and topics citing forgotten episodes cascade.
  - Accept: tests.
- **L8.13 Optional real-model run.**
  - Do: with the downloaded GGUF and a llama-server on the Mac, run 3 fixture days.
  - Accept: the yield is recorded, and invented quotes after verification are 0.

#### L9: Workshop and apps

**Owns:**
- `mind/hands.py`, `mind/app_commands.py`, `mind/workshop_shelf/**`
- new: `mind/app_monitor.py`, `mind/app_clock.py`
- `os/package/being/being-hands.service`, `os/package/being/being-hands-shellrc`, `os/package/being/workshop.mount`, `os/package/being/workshop.slice`
- `crates/face/src/panel.rs`
- tests: `tests/test_hands*.py`, `tests/test_app_commands.py`, `tests/test_console_panel.py`
- new tests: `tests/test_app_monitor.py`, `tests/test_app_clock.py`
- new: `lab/tests/test_apps.py`
- `lab/console_preview.py`

**Tasks:**

- **L9.1 Bridge contract.**
  - Do: check `AppBridge` against section 5.2 and L3.4, and add `may_be_app_command(text)`. Bare "make a note…", "stop", "wait", "continue" and "how's it going" must fall through when there is no terminal or job.
  - Accept: tests.
- **L9.2 Monitor and clock apps.**
  - Do: remove the "milestone 2" refusal. Write Python renderers that send ANSI at 1 Hz into `being-console --panel` through the existing renderer lifecycle, reading `hardware_metrics` and `reminders` read-only.
  - Accept: tests; ≤ 5 ms per rendered frame on the host.
- **L9.3 Eye strip.**
  - Do: verify the strip layout and transition, including with the monitor and clock shown.
  - Accept: `cargo test -p face panel` passes.
- **L9.4 Hands unit.**
  - Do: `being-hands.service` gets CPUWeight=20 and loses PYTHONDONTWRITEBYTECODE.
  - Accept: a unit test.
- **L9.5 Shelf replies.**
  - Do: check shelf listing and "play snake", and the guest refusal.
  - Accept: tests.
- **L9.6 Lab apps suite.**
  - Do: `lab/tests/test_apps.py` opens, minimises, restores and closes over the bus; checks that close kills the cgroup; and checks local screen stays 19/19.
  - Accept: it runs if a lab image from this source exists; otherwise it is pending.

#### L10: OS image, face and docs

**Owns:**
- `os/Config.in`, `os/configs/**`, `os/package/being/Config.in`, `os/package/being/being.mk`, `os/package/being/50-being.preset`
- every other `os/package/being/*.service` and file not owned above
- `os/board/beingos/linux*.config`, `post-build.sh`, `slots.sh`, `grub.cfg`, `iso-grub.cfg`
- `crates/face/**`, except `panel.rs`
- `docs/**` (except this file), `README.md`, `STATUS.md`, `scripts/**`
- tests: `tests/test_os_image.py`, `tests/test_laptop_units.py`, `tests/test_installed_generator.py`

**Tasks:**

- **L10.1 Stale generator test.**
  - Do: update `test_installed_generator`: bootstate and models are wants mounts with nofail; mind stays a requires mount.
  - Accept: it passes.
- **L10.2 `being.mk`.**
  - Do: install `being-audio-init` (to `/usr/libexec/being/`) and its unit, and `being-bench.service`. Precompile `.pyc` with `compileall --invalidation-mode unchecked-hash`. Add the sherpa-onnx `Config.in` source line and `BR2_PACKAGE_SHERPA_ONNX=y` in both defconfigs.
  - Accept: `test_os_image` and `test_laptop_units` cover the new units.
- **L10.3 Preset.**
  - Do: add `disable being-dream-cortex.service` and `disable being-bench.service` before the `being-*` glob.
  - Accept: a test.
- **L10.4 Units.**
  - Do: `being-mind.service` drops `After=being-cortex.service`. The face, mind and brainstem get CPUWeight=1000. The face gets `MESA_SHADER_CACHE_DIR=/run/being/mesa` with a RuntimeDirectory. Mind and face lose PYTHONDONTWRITEBYTECODE. The mind gets WatchdogSec=30s and NotifyAccess=main, only after L3.10 lands.
  - Accept: unit tests, and a mind started without the cortex answers "what time is it" in a host simulation.
- **L10.5 Face.**
  - Do:
    - A watchdog ping each frame, with `WatchdogSec=5s` and NotifyAccess=main in `being-face.service`, using `crates/bus` notify (read-only use).
    - `key.attend` on Space, Return or a trackpad click while no panel has focus and the eyes are resting.
    - A squint on `hear.missed`.
  - Accept: Rust tests.
- **L10.6 Docs.**
  - Do:
    - User guide "Things you can say", from section 2.
    - FIRST-BOOT round-2 steps: Try entries `m`, `n` and `b`; typing with F2 then 6; Space to attend.
    - PROTOCOL.md: the topics in 5.2.
    - STATUS.md: Phase 0 answers from the first boot (CPU i5-3210M, the crocus renderer, CS4206, 22.5 tok/s prompt reading, battery 71 cycles and 81%, 48 °C at idle, no failed units, the clock reset).
  - Accept: `scripts/check-docs.py` passes.
- **L10.7 Kernel options.**
  - Do: check that the resolved laptop `.config` has `PCI_MSI`, `PCIEPORTBUS`, the memory and pids cgroups, and PSI. Record what is missing; do not change the frozen stick.
  - Accept: a test or a note in STATUS.md.

### 5.5 Merge order and the integration pass

1. **Lanes 1, 2 and 4-10 land in any order.** Their files do not overlap.
2. **L3 rebases on all of them.** It turns its fakes into the real modules, runs the owner-phrase routing test, and applies the units' watchdog lines with L10.
3. **Run the full host suite and `cargo test`.** Then build the lab and laptop images from one source-id, if time allows, and run the QEMU gates of 4.1.
4. **Write one dated line in STATUS.md (L10).** It records what landed, what is pending a laptop measurement, and the open owner decisions: alarms with the lid closed (new), the source tag, the LFM licence, and the screenshots.

### 5.6 Left for after tonight (needs the laptop or the owner)

- Choosing the quant, the thread counts and the face renderer settings from bench data.
- Enabling bodycare writes after one supervised run.
- Tuning the mixer defaults from the round-2 logs.
- Wi-Fi.
- Speaker identity (CAM++ is staged, not used).
- Echo cancellation and barge-in.
- Describing what the camera sees.
- The persona LoRA.
- Model-generated Curiosity questions and broader inferred follow-up threads (the fixed opt-in question and
  explicit dated bookmark paths do not depend on these gates).
- The face's own typing line.
- Studio-assisted mode (owner's choice).
