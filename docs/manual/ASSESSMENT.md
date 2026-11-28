<!--
doc: assessment
verified: 2026-11-28T13:16
sources: README.md STATUS.md DESIGN-AND-PLAN.md docs/PIP-FUNCTIONS.md docs/USB-READINESS.md docs/DESIGN-COVERAGE.md docs/LAB.md docs/DECISIONS-OWNER.md
-->

# PipOS project assessment, 28 November 2026

This is a review of PipOS, called BeingOS in the code. It covers what
exists, how well it is built, what is missing, and what to do next.
Update it at each milestone. Keep earlier verdicts in
[Assessment history](#assessment-history) rather than overwriting them.

## Verdict

**The software is architecturally sound, unusually well tested, and has
booted on the real laptop, where cable diagnostics have since measured
speech recognition and natural speech output. It has not yet been tried
with live speech across a room, and nothing is installed on the internal
disk. Since 24 November the working tree has grown by hundreds of
uncommitted changes. The three serious project risks are thin hardware
evidence, a large body of new code with mostly host and QEMU evidence, and
a young history kept by a single maintainer whose recent work is not yet
committed.**

| Area | Rating | Basis |
|---|---|---|
| Concept and design | Strong | A coherent product thesis: reflexes over intelligence, the mind partition as the self, an honest body. Every hardware limit is explained (AVX without AVX2, two cores, reading costing as much as writing) |
| Architecture | Strong | Eight organs in separate failure domains on a tiny JSON-lines bus, plus a dream model started at night. The face never blocks on the model. Lid sequencing, per-sender topic checks and organ names tied to their systemd units show careful thought about races and privacy |
| Implementation quality | Good | About 28.8k lines of Rust and 72.5k of Python in `mind/` (standard library only), counted on 28 November. There are atomic writes, fsyncs, revision-aware deletion and a believed clock that now persists a local clock floor. An adversarial review of the wave 2 code on 24 November found 49 issues, all fixed in `9fed87c`. The earlier review's 49 medium findings have not been triaged one by one. Later work was reviewed in sessions, and its code review closed further findings (see [STATUS.md](../../STATUS.md)) |
| Testing | Very strong for a solo project | The last recorded host run, `scripts/check fast` on 26 November, passed 301 Rust tests, 2,814 Python tests and 50 lab host tests, with 3 skipped and a real local model server. The manual checker reported stale pages on that run. There are QEMU guest suites for boot, power-cut, rollback, reinstall and raw-disk forgetting, a boot gate that refuses stale images, and sealed candidate manifests. The current ordinary laptop Try image passed the default boot and a keyboard-driven install. The private cable image passed 17 of 18 QEMU checks, and the keyboard Quick Menu case that failed has a source fix that is not yet rerun. A rerun of the full guest suites on matching media is still open, and an earlier run with a real model server up had 5 real-model tests failing, unexplained at the time |
| Product completeness | Broad, mostly unproven | Everyday skills in code (sums, units, lists, alarms, the doctor, the workshop), chirps, volume, battery pleas and power-off, a night shift with a dream model, and owner controls in the F2 menu all exist. Since 24 November the source adds native panels (Options, Quick Menu, Calculator, Lists, Memory, Journal, Mind, Monitor), an installer that draws the partition plan, a file Keep, habits and a catalogue of intents, an offline guide, and optional dictionary and Simple English packs. Most of it has host or QEMU evidence only. Speaker recognition and a deliberate vision skill are missing |
| Hardware readiness | Diagnosed on the laptop, not qualified | The laptop Try USB image first booted on the real MacBookPro9,2 on 23 November 2026, with the HD 4000 drawing the face, the model server on the CPU, and the sound codec, lid, battery, charger, SMC sensors, camera, keyboard, trackpad and Ethernet found. "Hey Pip" was heard once but the words after it were not made out. Later cable sessions found one useful microphone channel and measured speech recognition at 0.146 of real time and natural speech output on the laptop, using a diagnostic microphone input. Live speech at one and two metres, false wakes, end-to-end delay, heat, battery and the installed disk are not yet measured. The Broadcom Wi-Fi has no firmware in the image. Nothing is installed internally |
| Project hygiene | Mixed | The source is under git and published on GitHub under GPL-3.0 with a documentation site (23 November 2026). The history is young, and its last commit in this checkout is from 24 November: the later work sits in the `cable-diag` working tree, with hundreds of changed or untracked paths. The `out/` evidence directory is about 160 GB with over 1,300 log files, and stays local and unpublished. The design document is 127 KB. Try 2 and 3 were withdrawn from the public releases over the licence of their bundled voice |

## What exists

- **A bootable appliance OS.** It is a Buildroot image, cross-built on an
  Apple-silicon Mac in a Colima container. It has Linux 6.18.64, a
  read-only A/B SquashFS system, signed RAUC updates with automatic
  rollback, a separate models partition and a separate `/mind`
  partition. It boots straight to the face, with no login and no boot
  text.
- **A complete conversational loop in QEMU.** It has the "Hey Pip" wake
  phrase, streaming PocketSphinx transcription, a local Qwen3.5-0.8B
  model through llama.cpp built for Ivy Bridge, and Flite speech. Esc
  interrupts, and M or F10 mutes.
- **Memory with real guarantees.** The conversation archive and facts are
  in SQLite with FTS5. It has keyword, date and speaker recall,
  correction, and confirmed forgetting that reaches derived replies, the
  search index, the prompt and the model cache. Retention modes, guest
  mode and encrypted backups with deletion proofs are in place. The lab
  checked the virtual raw disk for forgotten bytes.
- **Everyday skills.** Clock, calendar, time zones, reminders, timers,
  weather behind consent, quiet hours and an opt-in daily greeting.
- **Body awareness.** Lid as a privacy switch, battery and AC, temperature,
  light, a life record that detects power loss and a new body, and
  drives.
- **A face with character.** Seventeen poses, spring physics, blinks,
  saccades, breathing, a speech envelope, a lid wake gesture and a
  low-battery cue. It renders with one GL shader on KMS. Brows and a
  mouth can be switched on separately, the default is eyes only, and the
  face's Look settings persist.
- **A USB story.** Try mode runs in RAM and leaves internal disks
  byte-identical. Install has a device-specific `ERASE` confirmation and
  verified writes. Restore reads from read-only media. A laptop Try image
  definition writes diagnostics to its own `PIPLOGS` partition.
- **Cable diagnostics.** `pip-diag` measures the microphone, speakers,
  voices, wake word and command routing on every image, and a private
  development stick adds root SSH over an Ethernet cable. The stick never
  installs and is not a release.
- **Native apps.** The face process draws Options, a Quick Menu,
  Calculator, Lists, Memory, Journal, Mind and Monitor panels, and the
  installer shows the real partition plan and progress. These have host and
  QEMU evidence, plus a bounded Options preview on the laptop panel.
- **Release engineering.** Frozen candidate builds, sealed manifests,
  signed model packs, a soak runner and a guest suite controller. Laptop update bundles
  carry a provenance receipt that a media audit can match against the
  image.

## Strengths

1. **Liveness is decoupled from intelligence.** A crashed, slow or missing
   model leaves a living face. This is the right call for two 2012 cores,
   and it is enforced by the process boundaries.
2. **Privacy is a property of the system, not a promise.** The lid fails
   closed. Audio and video never cross the bus. The camera needs three
   independent permissions. The network is offline by default and
   enforced with nftables.
3. **Factual answers never come from the model.** Time, battery, age,
   memory and reminders are code paths. This hides most of a small
   model's weaknesses exactly where users would notice them.
4. **The evidence culture.** Claims are tied to named logs, image digests
   and test cases. Failures, such as the FAT ESP corruption after a power
   cut and the QEMU SMBIOS comma bug, were found and fixed by tests rather
   than by luck.
5. **Updates and memory are separated.** The OS is disposable, and the
   design lets the being survive reflashes, rollbacks and moves to a new
   disk.

## Weaknesses and risks

Ranked by consequence.

| # | Risk | Why it matters | Recommendation |
|---|---|---|---|
| 1 | **Young history, single maintainer, local-only evidence.** The source is under git and public, but the history starts in November 2026, the work since 24 November is uncommitted in the working tree, and the logs, images and sealed candidates under `out/` exist on one machine | The earlier development is not recoverable from the repository. A disk failure loses the evidence that the test claims rest on. Nobody else has built the tree from a clean clone | Commit the working tree at a checkpoint, in reviewed slices. Keep `out/`, `target/` and keys out of the repository. Back up the evidence that current claims depend on to a second disk, and confirm that a clean clone builds |
| 2 | **Thin hardware evidence** | One Try boot on the real MacBook has succeeded. Stage timings from cable runs exist (for example speech recognition at 0.146 of real time), but a clean idle baseline, thermals, battery life and the delay from live speech to first audio are still unmeasured, and the design's core bet, that two Ivy Bridge cores can hold a comfortable conversation, is not yet shown end to end | Run the controlled baselines and the Phase 1 benchmarks on the laptop before adding more features |
| 3 | **A failed physical boot can't be diagnosed** | GRUB and kernel output go to serial, the journal is volatile, and the face is dark without a brainstem | The laptop Try image adds on-panel GRUB entries, "show boot messages" and `PIPLOGS`, and the first real boot brought a `PIPLOGS` record back. `pip-diag` and the cable stick now give direct access to a running laptop. The safe-graphics entry is not recorded as tried on real hardware |
| 4 | **Process overhead** | The project log runs to about 6,000 lines, the work was split across nine work areas, and there are 383 log files and 66 GB of evidence. Coordination cost now competes with building, and intent is hard to recover | Keep this manual as the single current-state description. Keep the project log as a log, not a specification. Prune or archive superseded candidates and images |
| 5 | **Model quality** | Qwen3.5-0.8B invents things and ignores memories it was given. A parrot guard was needed. The real model choice waits for hardware | Benchmark the Phase 1 shortlist on the laptop. Keep the "facts from code" rule |
| 6 | **Hearing on the real laptop** | The first boot heard the wake phrase but not the request. Later diagnostics found one working microphone channel (the other measured near silence), and the source now chooses the channel automatically. Live speech at a distance has not been tried since. Every spoken feature depends on it | Run [FIRST-BOOT.md section 8](../FIRST-BOOT.md#8-round-2-hearing-typing-and-speed) at one and two metres, and measure false wakes in a quiet room; typing to Pip is the fallback meanwhile |
| 7 | **All services run as root** | A bug in the Python mind or voice, both of which parse untrusted speech text and network JSON, runs with full privilege. The cortex, bodycare and eyes units are sandboxed, and the USB inspector drops privileges after mounting, but the organs still run as root | Give the mind, voice and network-facing code dedicated users, and extend the eyes-style sandboxing to the others. Some narrower isolation (Ears, the PDF reader) is in source but not yet qualified |
| 8 | **Lab signing key** | Updates are signed with a self-signed key in a Docker volume. Losing it means installed devices can't be updated. Leaking it means anyone can sign updates. The tooling for an owner-held key exists (`lab/build.sh release-key`), and laptop signing writes a provenance receipt that media audits check | Create the owner-held release key with an offline backup before the first internal install |
| 9 | **Factory owner name** | `config/being.json` still ships `owner: "Richard"` in every image | Make the default neutral ("friend", as `Profile` already uses). First-run setup sets the owner |
| 10 | **Backups depend on a stick being present** | Backups can be made from the F2 menu, by voice, and by the night shift to a labelled stick. Reminders and the weather city are not carried over. The design's own risk table calls SSD death "total" | Keep a labelled stick in the laptop, and check a restore end to end on the laptop before relying on it |
| 11 | **Broadcom Wi-Fi firmware is not packaged** | Redistribution terms are unresolved, so Wi-Fi may not work on first boot. Tooling stages owner-sourced b43 firmware into a laptop image when it is present | Keep Ethernet as the documented path. Decide on offline firmware provisioning |
| 12 | **Open review findings** | The first review's 49 medium findings were never triaged one by one, plus known edge cases (believed-clock drift, an unpersisted brainstem time zone). Pip cannot tell voices apart, so "only the person who asked" means only the owner or a guest | Triage these before the first internal install |
| 13 | **Voice licence** | The packed Alba voice is a fine-tune of a Piper voice whose data is licensed for research only. Try 2 and 3 were withdrawn over it. It is acceptable for the owner's own stick | Before any public image, replace it with Piper's retrain of 9 November 2026. The candidate audit has a flag, `--require-alba-retrain`, that checks for it. Production packing and an Ivy Bridge timing run are still open |

## Documentation state

Before this manual, documentation was large, but it was organized around
the development process rather than its readers:

- **`README.md`** was accurate, but it was a status page.
- **`DESIGN-AND-PLAN.md`** (854 lines) mixed design, plan, research notes
  and work logs.
- **`docs/LAB.md`** (1,539 lines) mixed how-to with experiment results.
- **`docs/USB-READINESS.md`** and **`docs/DESIGN-COVERAGE.md`** were
  status records.
- **`docs/PROTOCOL.md`** was accurate and concise. It was missing the
  `dreaming` expression, the `hour` field of `sim.body`, and the console
  as a sender of `key.*`.

Nothing addressed the owner of the device. This manual adds:

- an [architecture reference](ARCHITECTURE.md);
- a [lookup reference](REFERENCE.md);
- a [developer guide](DEVELOPER-GUIDE.md);
- a [user guide](USER-GUIDE.md).

Each file is tied to the source files it describes, so it can be checked
when they change (see [README](README.md#keeping-these-documents-true)).

Inconsistencies found while writing:

| Where | Inconsistency |
|---|---|
| `docs/LAB.md` | Said the Mac needs only QEMU, Colima and Docker. The scripts also use `python3`, `ffmpeg`, `say`, `7zz`, `cargo` and `curl` (fixed 24 November) |
| `docs/LAB.md`, `vm.py` table | Omits `--memory`, `--mic-input` and `--update-media` |
| `docs/USB-READINESS.md` | Says "boot-state partition". It is the ESP, mounted at `/bootstate` |
| `os/board/beingos/linux.config` | A header comment says the laptop kernel diet "comes later". It exists |
| `grub.cfg` / `post-image.sh` | Default `B_OK` differs (0 against 1). The grubenv value wins |
| `lab/vm.py` | Reports MacBookPro9,2 SMBIOS only when a test asks for it. Interactive VMs don't |
| `fake_cortex.py` / `run-sim.sh` | Default port 8080 against 8377 |
| Naming | PipOS in GRUB, menus and the certificate. BeingOS in the docs and paths |

## Recommended next steps

In this order:

1. **Commit and back up.** Commit the working tree in reviewed slices,
   keep the public repository current, and back up the local `out/`
   evidence to a second disk.
2. **Rerun the L2–L6 guest suites** on matching laptop and lab media.
3. **Follow up the first physical Try boot** (done 23 November 2026).
   Record live spoken turns, the lid, the camera and thermals on the real
   machine, and package the Broadcom Wi-Fi firmware or document Ethernet
   as the supported path.
4. **Run the Phase 1 benchmarks on the laptop** (the Try stick's
   **measure speed** entry). Choose the conversation model and speech
   stack, and verify the 3-second first-word target.
5. **Close the owner gaps:**
   - an owner-held signing key;
   - a neutral factory owner;
   - bodycare out of observe mode, once its controls are checked on the
     laptop.
6. **Harden.** Drop root for the mind and voice, and triage the 49 medium
   findings.
7. **Install internally**, and only after Phase 0 chores and explicit
   authorization. Then run the design's soaks: 24 h face, 48 h recovery,
   one week appliance.

## Assessment history

| Date | Verdict |
|---|---|
| 2026-11-23 | First assessment (this document). Software is QEMU-verified through L6, and the L7 candidate is superseded. No hardware evidence and no commits. Hardware readiness was rated not started and project hygiene weak, with missing version control as the top risk |
| 2026-11-23 | Update, same day. The source is under git and published on GitHub under GPL-3.0 with a documentation site, and the laptop Try USB image is built. It has not yet booted on a real MacBook, and no hardware measurements exist. The version-control risk is replaced by a young, single-maintainer history with local-only evidence. The software verdict is unchanged |
| 2026-11-23 | First hardware boot. The laptop Try USB image booted on the real MacBookPro9,2 and Pip woke on it. Graphics, sound, lid, battery, sensors, camera and Ethernet were found, with no failed services; Wi-Fi lacks firmware. Hardware readiness moves from "image built, not booted" to "first boot done". Measurements and the internal install remain |
| 2026-11-24 | Waves 1 to 3. The first boot's logs showed Pip heard its name but not the request. Wave 2 (the Pip functions) added hearing fixes, typing, everyday skills in code, rings, battery care, the night shift and dream model, the workshop and owner controls in F2; wave 3 fixed 49 review findings. Host suites pass; nothing of it has run in QEMU or on the laptop yet. "Product completeness" moves from partial to broad but mostly unproven; the settings-without-F2 risk is replaced by hearing on the real laptop |
| 2026-11-24 (evening) | Cable and diagnostics, uncommitted on the `cable-diag` branch. On the MacBook the owner found two separate faults. The spoken voice is garbled on the laptop's playback path (the same clips are clear on another Mac; the leads are HDA stream-position and period timing, `bdl_pos_adj`, resampling and speaker drive), which `pip-diag speaker` is built to tell apart. Poor hearing is likely a quiet microphone with a dead left channel: the first boots measured the left channel at -84 dBFS and the right at about -63 dBFS; the fix's effect on hardware is not yet measured. In answer: `pip-diag`, which measures the microphone, speakers, voices, wake word and command routing and says in plain English what is wrong (on every image, and as Try menu item 8); a development-only cable stick with root SSH from link-local addresses and a writable copy of the code, which never installs; a command-grammar second pass, voice debugging and engine overrides; the laptop's microphone channel chosen automatically; Alba as the default voice (the owner's choice after a 27-voice audition); and Mac-style top-row keys. Host suites passed in the integration run at about 21:40 (Python 2,402 passed, 29 skipped; lab host checks 42 passed); `mind/voice.py` and `tests/test_voice_keys.py` changed after that run and may not be covered. None of it has been built, run in QEMU or tried on the laptop. Risk 3 (diagnosing the physical laptop) and risk 6 (hearing) now have direct tools, still unproven on hardware. Before a public release image ships Alba, the packed voice (a fine-tune of Piper's lessac voice, whose data is licensed for research only; fine for the owner's own stick) must be replaced by Piper's retrain of 9 November 2026 |
| 2026-11-28 | Continuation after the cable work. The cable image and an ordinary laptop Try image passed their QEMU checks at 17 of 18 and on the default boot and keyboard install respectively; the one failure, the Quick Menu opening Lists, has a source fix not yet rerun. On the laptop, cable runs measured speech recognition (0.146 of real time) and natural speech output, and a native Options preview drew on the real panel. The host suite reached 301 Rust and 2,814 Python tests. The source added native panels, the Keep, habits, an offline guide and many hardware-evidence tools, and most of it is uncommitted in the `cable-diag` working tree. Try 2 and 3 were withdrawn over the licence of the packed voice. Live speech, power and thermal baselines and the internal install remain open. Project hygiene moves from "improving" to "mixed" because of the uncommitted volume |
