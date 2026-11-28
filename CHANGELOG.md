# Changelog

What changed in PipOS, newest first. Times are Australian Eastern
(AEDT). Test images are on the
[Releases page](https://github.com/rzkowskii/PipOS/releases), and each
release's notes say what was and was not checked. Try 2 and Try 3 were
withdrawn on 25 November because they carry a natural voice whose licence does not allow public distribution (see below).

## Unreleased: the `Dev` branch, checkpoint of 28 November 2026

A checkpoint of four days of work since the cable and diagnostics
snapshot below. It passes the host tests on the development Mac
(28 November, on this branch: Rust 371 passed; Python 3,048 run, all
passing, 64 skipped for tools, models and downloads a fresh checkout
does not have; lab host checks 64 passed). It has not been reviewed as a whole yet, and none of it has
been built into a published image or tried on the MacBook as a set.
[STATUS.md](STATUS.md) has the hardware and QEMU evidence for the parts
that were tried on their own. A review of the face code after this
checkpoint found 20 faults, among them a brief link loss that silences
the mouth for the rest of a reply; their fixes follow in a later update.

- **Hearing and voice.** A separate Ears service owns the microphone and
  hands the stream to Voice. If capture outlives a refusal, a privacy
  latch turns hearing and voice off until restart. Speaker EQ for the
  laptop's small speakers, prepared stock phrases, and a record of which
  parts of a reply were actually played, so an interrupted reply keeps
  only what you heard.
- **Face.** The Face Lab expressions (89 poses, effects and scenes),
  eleven Look palettes, optional brows and mouth (eyes only by default),
  a quick menu, options and mouth shapes for speech.
- **Everyday use.** The Keep for documents, an offline library and
  spoken guide, habits and suggested routines, alarms with an optional
  sleep (each needs its own yes), a calculator, a journal, lookups,
  vitals, speaker and camera descriptions behind consent.
- **Updates and storage.** An encrypted checkpoint before each update,
  typed rollback to the previous system, optional full-disk encryption
  of memory at install, archive integrity checks and a real partition
  map in the installer.
- **Body.** USB camera and device plug-in handling, the keyboard light,
  body maps on the bus, and a night dream temperature limit of 80 °C.
- **Lab.** Cable stick tooling, more `pip-diag` rounds, candidate image
  manifests and parity checks, soak and recall benchmarks, auditions.

## Unreleased: the `Dev` branch (24 November 2026, evening)

The first boots on the MacBook found two separate faults. The microphone
comes up too quiet: the first boots measured its left channel at about
-84 dBFS and its right at about -63 dBFS, so one channel is probably
dead. And Pip's spoken voice is garbled on the laptop's own speakers,
though the same sound clips are clear on another Mac. This work is about
finding and fixing both, and about making the keyboard behave like a
Mac's.

Everything here has passed the tests on the development Mac (23:42, on
this branch: Rust 261 passed; Python 2,468 run, all passing, 62 skipped
for models, downloads and builds a fresh checkout does not have; lab host
checks 42 passed). None of it has been built into an image, run in the
virtual laptop or tried on the MacBook yet. The test images do not
include it.

### Diagnostics

- **`pip-diag`**, on every image and as item 8 of the Try menu. It
  checks the microphone, the speakers, both voices, the wake phrase and
  the routing of commands, and says in plain English what is wrong. Its
  checks: `quick`, `full`, `voice`, `commands`, `mic`, `speaker`, `tts`,
  `codec`, `hear`, `engines`, `restore` and `report`.
- Results go to `diag.json` and `diag.txt` in a `diag-…` folder on the
  stick's PIPLOGS partition, readable on any Mac.
- The speaker checks play test sounds and record them, to tell a speaker
  or timing fault apart from the voice itself.
- `pip-diag codec` shows the sound chip's set-up and can switch it while
  Pip runs. The USB logs now include the sound chip's full dump.

### A cable stick, for development only

- A separate Try stick, built with `BEING_LAPTOP_CABLE=1 lab/build.sh
  laptop-try` after `lab/build.sh cable-key`, lets a Mac reach a running
  Pip over an Ethernet cable: SSH with one key, from link-local addresses
  only, and a writable copy of the code for that boot. `being.cable=0`
  switches it off. `lab/cable.sh` drives it from the Mac.
- It never installs, is never made into an ISO and is never signed as an
  update. Ordinary images still have no SSH at all.
- How to build it is in [LAB.md](docs/LAB.md#cable-development-build),
  and how to boot it is in
  [FIRST-BOOT.md section 9](docs/FIRST-BOOT.md#9-cable-stick-v6).

### Hearing and voice

- When the first transcript is not a command, Pip checks what it heard a
  second time against the list of commands.
- Pip picks the laptop microphone's working channel by itself.
- New switches for testing: a diagnostic microphone (`BEING_DIAG_MIC`),
  voice debugging on a Try boot (`being.voice_debug=1`), and a choice of
  speech engines (`being.asr=`, `being.tts=`).
- The natural voice is now Piper's **Alba**, chosen by ear from 27
  voices. The original voice (Flite) is used only if Alba fails its
  check, will not load, or runs slower than 0.7 of real time on the
  laptop (the limit was 0.35).
- Piper's own phonemizer is packaged (`piper-phonemize`), because the
  speech library no longer turns text into phonemes by itself.

### Keys

- The top-row keys work as they do on a Mac (`hid_apple.fnmode=1`):
  brightness, keyboard light, and volume with a click. Speaker mute
  works but is not yet remembered across restarts. Mission Control opens
  the menu and Launchpad opens the terminal. Pip's own F1, F2, F3, F9 and
  F10 need fn.

### Fixes

- "What did you do last night?" asked after 6 p.m. now means the night
  before, not the evening in progress. The test for it no longer depends
  on the time of day it runs.
- No part of the mind needs zlib, which the laptop's Python does not
  have.
- Each turn writes one `route` line to the log, saying how the request
  was handled.
- The health check watches the workshop terminal. The hardware report
  lists the terminal, camera, battery-care and sound set-up services.
- Guest mode carries over when the voice restarts.
- Pip knows when the Try menu has the screen and the face is down.

### Documentation

- README, [STATUS.md](STATUS.md) and the user guide now name the
  published test images, the faults found on 24 November and what has
  been measured.
- The owner's choices behind this work are recorded in
  [DECISIONS.md](docs/DECISIONS.md) rows 18 to 20 and
  [DECISIONS-OWNER.md](docs/DECISIONS-OWNER.md) item 22.
- The manual pages describe `pip-diag`, the cable stick and the new
  keys.

### Still open

- Run `pip-diag` on the laptop, and try the two other microphone set-ups
  in the boot menu (**M** and **N**).
- The virtual-laptop suites for the cable stick (`test_cable_dev`) and
  for a spoken "Hey Pip, open a terminal" (`test_voice_terminal`) have
  not run yet.
- The docs checker flags six manual pages whose sources changed after
  they were last verified (22:17). They need checking again.
- The packaged Alba voice is a fine-tune of a voice whose recordings are
  licensed for research only. Before a public image ships Alba, it must
  be replaced by Piper's retrained Alba, which is built on a base trained
  from scratch.

## Withdrawn: Try 2 and Try 3 (25 November 2026)

Both images carried the natural voice Alba in the version packaged by
sherpa-onnx, which was fine-tuned from Piper's lessac voice. The lessac
recordings are licensed for research only and may not be distributed,
so these images should not have been public. Both releases are drafts
again and their downloads are gone. They return, rebuilt, once the voice
is replaced by Piper's retrained Alba or another voice whose licence
allows it. Try 1 has no Piper voice and stays up.

## Try 3: `v0.2.1-try.3` (24 November 2026, 05:29, withdrawn)

Tuned on the real models.

- The conversation model uses its own chat format, so the prompt stays
  cached between turns and replies start sooner. Replies keep a short
  spoken shape by default.
- Every dream the model proposes is checked against the day's record
  before it is kept. Night upkeep runs even with dreaming switched off.
- Recall thresholds tuned on measured results, so newer facts win.
- Every model pack is checked against its signature before it loads,
  not only when it is installed.
- The Try menu fits the screen again, and the ISO has all six boot
  entries.

## Try 2: `v0.2.0-try.2` (24 November 2026, 04:37, withdrawn)

The first boot's findings, answered.

- Hearing: microphone levels set at boot, a chirp when Pip hears its
  name, "Sorry, I didn't catch that", automatic microphone gain and
  channel, and talking by typing.
- Everyday skills in code: world time, timers, alarms and repeating
  reminders with snooze, sums, unit conversions, lists and notes, jokes
  and games.
- Body: battery health, temperature, brightness, safe shutdown, a
  self-check that can make safe repairs, backups on request, and a log of
  what Pip did.
- Memory: recall by meaning, a daily diary and night upkeep. Rule-based
  dreaming; model-based dreaming needs a separate pack.
- The workshop: "open the terminal", with Pip's eyes above it.
- A natural voice and recogniser (Piper and Moonshine), used only when
  the laptop is fast enough.

## Try 1: `v0.1.0-try.1` (24 November 2026, 00:00)

The first hardware test image, with the USB controller fix that let the
MacBook boot it. Graphics, lid, keyboard, trackpad, battery, sensors and
camera are found; the model reads prompts at about 22 tokens a second.
Known issue: the microphone level was too low for speech to be
understood.

## First boot on the real MacBook Pro (23 November 2026)

PipOS booted on the 2012 MacBook Pro from a USB stick. Pip woke, drew its
face on the GPU and heard its name, but could not make out the words
after it.

## First public release (23 November 2026)

The source, the manual and the documentation site, published under the
GNU General Public License v3.0.
