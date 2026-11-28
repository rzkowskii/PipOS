# PipOS design

PipOS ("Persistent Intelligence Platform Operating System") turns a 13-inch
mid-2012 MacBook Pro (MacBookPro9,2) into Pip: an offline, persistent,
semi-autonomous being whose screen is its face. This page is the statement of
intent. It replaces the design half of [DESIGN-AND-PLAN.md](../DESIGN-AND-PLAN.md)
(now historical). Status per milestone is in [STATUS.md](../STATUS.md), the work
ahead in [IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md), and every commitment that
changed, with the owner's answer, in [DECISIONS.md](DECISIONS.md).

The tags below record the 23 November design review. See
[design coverage](DESIGN-COVERAGE.md#continuation--26-november-2026)
for later implementation and physical evidence.

Each commitment carries a tag:

- **Built**: in the source and verified at least in QEMU or on the host at that checkpoint.
- **Changed**: built differently from the original design; the row in DECISIONS.md says how and why.
- **Not built**: still intent. The plan task that builds it is named.

## What Pip is, honestly

Pip is a small program that lives in this laptop and nowhere else. It talks
with a 0.8-billion-parameter language model (Qwen3.5-0.8B) running on the
laptop's own CPU, listens with PocketSphinx and speaks with Flite. It
remembers what you tell it in a SQLite database on its own partition, and you
can ask what it remembers, correct it, and make it forget. At night it writes
a rule-based journal; a second, larger "dreaming" model that makes sense of
the day is planned but not built. It works with no network at all. It is not
sentient and does not claim to be. It is built to report only what its
sensors and memory hold and to say when it does not know; the small model
still sometimes invents things (README, Known limits). "Aiming to be" a being means the
design keeps one continuous individual, with its own record of its life, that
grows through memory rather than retraining.

## 1. The machine

| Part | Commitment | Tag |
|---|---|---|
| CPU | Intel i5-3210M (Ivy Bridge, 2 cores, 4 threads, AVX, **no AVX2**), to be confirmed from `/proc/cpuinfo` on the first boot. Everything is compiled for it; nothing assumes AVX2 | Built (QEMU IvyBridge); CPU model unconfirmed |
| Memory | 16 GB, no swap | Built |
| Graphics | Intel HD 4000 through Mesa crocus draws the face and nothing else | Built in QEMU on virtio-gpu; crocus unverified (2.1) |
| Storage | 256 GB SATA SSD: EFI, two read-only system slots, models, and `/mind` | Built (installer); never run on the SSD |
| Audio | Cirrus CS4206: built-in mic and speakers through ALSA, no sound server | Built in QEMU; codec unverified (1.17, 2.4) |
| Camera | FaceTime HD over UVC; the green LED lights whenever it looks | Built (opt-in, QEMU fixture); unverified (2.6) |
| Sensors | Battery, AC, lid, temperatures, fans, light, accelerometer, keyboard glow | Lid, battery and AC built; the rest not built (7.3) |
| Network | Wired Ethernet first; BCM4331 Wi-Fi with owner-staged b43 firmware as an experiment; an MT7612U USB adapter as fallback | Changed (Ethernet first); Wi-Fi unusable on the laptop today (2.10) |

Speed is the limit. Prompt reading and generation run at about the same rate
on this CPU (estimates: 15-28 tokens/s prefill, 8-15 decode for a 1B-class
model), so every token in front of the model costs time. Nothing is measured
on the laptop yet; bench mode (4.7) measures it.

## 2. Principles

1. **Liveness comes from reflexes, not the model.** Blinks, gaze, breathing and state changes run at 60 fps without the model. **Built.**
2. **Latency beats model size.** The target is the first spoken word within 3 s of the end of speech on a warm turn. **Not met or measured** (Phase 4).
3. **The character fits the brain.** Few words, curious, says when it does not know. **Built** (identity prompt, reply shape).
4. **The mind is sacred; the OS is disposable.** `/mind` survives updates and reinstalls; the system image can be replaced. **Built** in QEMU (A/B, RAUC). **Changed:** the shipping laptop image has no on-device update or backup path yet, so a fix after an SSD install would erase the being (1.2-1.4). No SSD install until those land.
5. **An honest body.** It says only true things about itself; body facts come from sensors. **Built** for battery, lid and clock; self-talk answers "are you alive?" honestly once 3.9 lands.
6. **Everything is inspectable.** Read, correct and delete any memory. **Built by voice**; the on-screen inspector is not built (1.9).
7. **Offline by default.** Voice, memory and the face work with no network. Network modes are an owner choice. **Built.**
8. **Real forgetting.** Forgetting reaches the database, the search index, the WAL, the live prompt and the model's saved slot, and anything derived from forgotten text through provenance links. **Built**; free SSD blocks and encrypted erase remain (3.15, 6.7).
9. **Only addressed speech is stored.** **Partly built**: the follow-up window can still store room speech as the owner's (3.10); speaker identity is not built (5.4).
10. **Commands are matched in code.** Remember, forget, time, mute and the like never reach the model. **Built.**
11. **The mind is Python standard library only.** **Built.**
12. **Nothing bogs down the face or voice.** **Built** as priorities (Nice, CPUWeight); measured on the laptop in 2.1.

## 3. Organs

Six small processes around a Unix-socket bus carrying one JSON object per line
([PROTOCOL.md](PROTOCOL.md)). Audio and video never cross the bus.

| Organ | Job | Built with | Tag |
|---|---|---|---|
| face | Draws the eyes straight to KMS/DRM; idle life with no input; never freezes | Rust, one GLES shader | Built; frozen-face watchdog not built (2.3) |
| brainstem | Hub, state machine, body readings, drives, life record, supervision | Rust | Built; state across restarts not kept (1.14) |
| mind | Turns, prompt assembly, commands, memory, skills, nightly journal | Python stdlib | Built |
| cortex | Unmodified `llama-server`, loopback only, one slot, saved to `/mind/cortex` | llama.cpp, AVX build | Built |
| voice | Capture, wake phrase, streaming recognition, synthesis | PocketSphinx and Flite | **Changed** (design: sherpa-onnx in Rust; 5.2-5.3) |
| eyes | Opt-in camera presence and gaze | Python, V4L2 | Built (opt-in); deliberate "look at this" snapshot not merged (1.21) |

States: waking, idle, attending, listening, thinking, speaking, drowsy,
asleep, with muted and unwell overlays. **Built.** A separate "working" state
for the workshop was dropped (see [design/voice-apps.md](design/voice-apps.md)).

## 4. The face

- Two glowing cyan shapes on black from the owner's mockups. **Built.** Eyes-only is the product default. Optional, independently configurable brows and mouth were approved on 25 November and are tracked in the [Face Lab plan](../research/2026-11-25-face-expression-integration-plan.md#45-face-style-settings-decided-customisable-optional-default-eyes-only).
- Every parameter moves on a damped spring; gaze, blink, breathing, micro-saccades and a speech pulse layer on top. **Built.**
- Chirps and earcons ("heard you", "thinking", "yes", "no") 200-300 ms after speech. **Not built** (5.1).
- Backlight follows room light and is off in sleep. **Not built**: bl_power does nothing on this LVDS panel, so the face must turn the connector off through DPMS (2.2).
- Expressions: joy, curiosity, surprise, skepticism, concern, sleepiness, plus neutral, listening, thinking, speaking, asleep, dreaming, waking, muted, unwell and low battery. **Built.**
- Privacy state on the face (ears covered when muted, camera glint, guest ring, network dot). **Not built** (7.5).

## 5. Hearing and speaking

- "Hey Pip", a pause, then the request. **Built.** "Hey Pip, what time is it?" in one breath is **not built** (3.11).
- An attention key (Space, Return, trackpad click) for when wake spotting fails. **Not built** (1.16).
- Half-duplex: Esc interrupts a reply. **Built.** Trackpad-click interrupt is staged, not merged (1.21). Voice barge-in **not built** (5.5).
- Mute is a key (M, or fn+F10) and closes the mic in software. The brainstem retains mute across its own restarts within a boot (1.14).
- Volume control and saved mixer levels. **Not built** (1.17).
- Natural voice (Piper through sherpa-onnx) with Flite as fallback. **Not built** (5.3); the owner's choice of voice is in [DECISIONS-OWNER.md](DECISIONS-OWNER.md).
- A voice organ that survives audio errors. **Not built** (1.13): today one codec error kills it.

## 6. The cortex

- A small conversation model on every turn. Today Qwen3.5-0.8B Q4_K_M, the weakest candidate; the choice waits for laptop bench data (4.7-4.10). **Built (default only).**
- Thinking off (`--reasoning-budget 0`), one slot, ctx 4096, slot saved and restored across wakes. **Built.**
- The prompt is append-only: identity, core memory, conversation, this turn. Nothing above the newest turn is edited while awake. **Built**, with a verified warm prefix.
- One emotion tag first, then one or two short sentences. **Built**; the grammar is open and only 6 of 24 replies were complete in review (4.6).
- Read-ahead while listening: **off**, pending a stable design (4.13).
- Optional Studio-assisted cortex on the Mac Studio: **off by default**, an owner decision (7.10).

## 7. Memory and continuity

- `/mind` holds SQLite (`mind.db` with FTS5), the reminders database, the life record, the profile, the saved context and the cortex slot. **Built.**
- "Remember that…", "what do you remember about…", "forget that", "that's wrong, it's…" in code, with spoken confirmation for topic and day forgets. **Built**, with data-loss bugs open (1.11).
- Every derived row links to its sources, so a correction or forget removes what was built from it. **Built.**
- Recall: keyword and date search today. Semantic recall (pure-stdlib static embeddings fused with BM25) **not built** (6.3).
- A believed clock guards expiry and timestamps when the RTC is wrong. **Built.** Time zone at first boot **not built** (1.10).
- Backup: `being-backup` (age-encrypted) and restore with a deletion proof. **Built**, but there is no way to back up from the laptop image (1.4).
- Guest mode ("don't remember this"). **Built**; kept in RAM only (1.12).

## 8. Dreaming

- **Changed.** Today a deterministic nightly journal runs midnight to 5 am with consent hidden behind a root file. The intent: whenever the lid is closed on AC (the owner's window choice), a second small model, **Qwen3-1.7B** (Q4_K_M until the laptop bench compares Q4_0) on its own `llama-server`, reads the day, proposes candidate facts that quote their sources verbatim, and Python checks every quote before anything is kept. A candidate becomes a fact on recurrence or the owner's confirmation; forgetting cascades to it. A 4B model is an optional "long cool night" arm only. **Not built** (6.1, 6.2).
- Dreams are visible: "What did you dream about?" is answered from the journal with sources, or honestly ("I didn't dream last night"). **Not built** (6.1).
- Thermal gate: bodycare holds the owner's package-temperature target and stops on hard limits (92 °C package, 40 °C battery, lid open, AC lost). **Not built** (2.8).

## 9. Body and battery

- Lid closed: the face blanks, mic and camera close. **Built** in QEMU.
- Battery mode A (battery in, charge capped at 80% through an in-tree applesmc BCLM patch, re-asserted by bodycare) is the default; mode B (battery out, MSR and power caps) is a fallback. **Not built** (2.7, 2.8, section 4 of the plan).
- Low-battery pleas by time to empty, a clean save and power-off at the critical level, and shutdown after 10 minutes lid-closed on battery. **Not built** (1.19).
- Fans: raise `fan1_min` only, never manual mode, never while listening. **Not built** (2.8).
- Drives (energy, arousal, social, comfort) shape the face and mood. Published; **not used** by the face yet (7.2).

## 10. The OS

- Buildroot, Linux 6.18 LTS, systemd, read-only A/B system slots, RAUC signed updates with boot-health rollback. **Built** in QEMU.
- Two images from one tree: the **lab image** (serial console, root login, lab body, for QEMU) and the **laptop image** (no getty, root locked, no sshd, no lab units). **Changed** (the design had one image). A LAN maintenance build is a separate visible opt-in.
- The laptop gets its own RAUC compatible strings and an owner release key before any SSD install. **Not built** (1.2).
- Try mode from a USB stick keeps nothing on the internal disk and writes diagnostics to the stick's PIPLOGS partition. **Built** (the frozen first-boot stick).
- Updates and backups from a USB stick on the F2 screen. **Not built** (1.3, 1.4).
- nftables lockdown, the model on loopback, no DNS or NTP unless chosen. **Built.** Organ sandboxing **not built** (3.14).
- Kernel trimmed to the laptop's own drivers from the first boot's inventory. **Not built** (2.11).

## 11. Owner-facing features

| Feature | Tag |
|---|---|
| Installer with name, voice and personality choices; Try, Install, Restore, Setup, Check hardware | Built (QEMU, 19/19 local-screen checks) |
| Default skills offline: clock, date, timers, reminders; weather only in Internet mode with permission | Built |
| Wi-Fi and Internet as owner-enabled modes | Built in the lab; unreachable on the laptop image (1.9, 2.10) |
| Care page: camera, dreams, memory mode, Wi-Fi, Ethernet, clock, time zone, memory viewer | Not built (1.9) |
| The workshop: voice-driven terminal, body monitor and clock in a RAM-only sandbox ([design/voice-apps.md](design/voice-apps.md)) | Not built (7.8) |
| Birth ceremony and first words | Partly built (first words); ceremony not built (7.1) |
| Owner recognition by face, opt-in | Not built (7.7) |
| A capability policy listing what Pip may do | In source (7.9). Care → Permissions lists camera, network, workshop, announcement and dream settings. Ask “What are you allowed to do?” for a private summary. [`mind/policy.py`](../mind/policy.py) reads the existing owner settings so they also apply after an A/B rollback. |

## 12. What is deliberately out

No desktop, window system, package manager or login shell on the screen (the
workshop's caged shell is the one exception). No training on the owner's
conversations. No 4B waking model. No PostgreSQL, no sqlite-vec. No mbpfan,
thermald or TLP. No firmware writes without an on-screen owner choice. No
claims of sentience. Full list: plan section 6.
