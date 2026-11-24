<!--
doc: assessment
verified: 2026-11-24T22:17
sources: README.md STATUS.md DESIGN-AND-PLAN.md docs/PIP-FUNCTIONS.md docs/USB-READINESS.md docs/DESIGN-COVERAGE.md docs/LAB.md docs/DECISIONS-OWNER.md
-->

# PipOS project assessment, 24 November 2026

This is a review of PipOS, called BeingOS in the code. It covers what
exists, how well it is built, what is missing, and what to do next.
Update it at each milestone. Keep earlier verdicts in
[Assessment history](#assessment-history) rather than overwriting them.

## Verdict

**The software is architecturally sound, unusually well tested, and has
booted once on the real laptop, where it heard its name but not the words
after it. Wave 2 roughly tripled the code in a day. It has three serious
project risks: thin hardware evidence, a large body of new code that has
only host evidence, and a young history kept by a single maintainer.**

| Area | Rating | Basis |
|---|---|---|
| Concept and design | Strong | A coherent product thesis: reflexes over intelligence, the mind partition as the self, an honest body. Every hardware limit is explained (AVX without AVX2, two cores, reading costing as much as writing) |
| Architecture | Strong | Eight organs in separate failure domains on a tiny JSON-lines bus, plus a dream model started at night. The face never blocks on the model. Lid sequencing, per-sender topic checks and organ names tied to their systemd units show careful thought about races and privacy |
| Implementation quality | Good | About 15.7k lines of Rust and 38.8k of Python in `mind/` (standard library only). There are atomic writes, fsyncs, revision-aware deletion and a believed clock. An adversarial review of the wave 2 code on 24 November found 49 issues, all fixed in `9fed87c`. The earlier review's 49 medium findings have not been triaged one by one |
| Testing | Very strong for a solo project | Mac suites pass on 24 November: 1,977 Python tests (39 skipped) and 245 Rust tests. A later run with a real model server up had 5 real-model tests failing, not yet explained (see [STATUS.md](../../STATUS.md)). There are 23 QEMU guest suites, power-cut, rollback and raw-disk forgetting tests, a boot gate that refuses stale images, and sealed candidate manifests. No image built from the wave 2 source has yet passed the guest suites |
| Product completeness | Broad, mostly unproven | Everyday skills in code (sums, units, lists, alarms, the doctor, the workshop), chirps, volume, battery pleas and power-off, a night shift with a dream model, and owner controls in the F2 menu all exist, with host evidence only. Deliberate vision, speaker recognition and a conversation browser are missing |
| Hardware readiness | First boot done | The laptop Try USB image booted on the real MacBookPro9,2 on 23 November 2026. The boot log shows the HD 4000 drawing the face, the model server running on the CPU, and the sound codec, lid, battery, charger, SMC sensors, camera, keyboard, trackpad and Ethernet all found, with no failed services. "Hey Pip" was heard once, but the words after it were not made out, so no reply was given: the capture level was too low. The Broadcom Wi-Fi has no firmware. Nothing is installed internally, and no systematic measurements exist |
| Project hygiene | Improving | The source is under git and published on GitHub under GPL-3.0 with a documentation site (23 November 2026). The history is young. 66 GB of `out/` evidence stays local and unpublished. The design document is 127 KB. Documentation was scattered and process-oriented before this manual |

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
- **A face with character.** Sixteen poses, spring physics, blinks,
  saccades, breathing, a speech envelope, a lid wake gesture and a
  low-battery cue. It renders with one GL shader on KMS.
- **A USB story.** Try mode runs in RAM and leaves internal disks
  byte-identical. Install has a device-specific `ERASE` confirmation and
  verified writes. Restore reads from read-only media. A laptop Try image
  definition writes diagnostics to its own `PIPLOGS` partition.
- **Release engineering.** Frozen candidate builds, sealed manifests,
  signed model packs, a soak runner and a 21-suite guest controller.

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
| 1 | **Young history, single maintainer, local-only evidence.** The source is now under git and public, but the history starts in November 2026, and the logs, images and sealed candidates under `out/` exist on one machine | The earlier development is not recoverable from the repository. A disk failure loses the evidence that the test claims rest on. Nobody else has built the tree from a clean clone | Commit at every checkpoint. Keep `out/`, `target/` and keys out of the repository. Back up the evidence that current claims depend on to a second disk, and confirm that a clean clone builds |
| 2 | **Thin hardware evidence** | One Try boot on the real MacBook has succeeded. Latency, audio quality, thermals and battery life are still unmeasured there, and the design's core bet, that two Ivy Bridge cores can hold a comfortable conversation, has no numbers behind it yet | Run the Phase 1 benchmarks on the laptop before adding features |
| 3 | **A failed physical boot can't be diagnosed** | GRUB and kernel output go to serial, the journal is volatile, and the face is dark without a brainstem | The laptop Try image adds on-panel GRUB entries, "show boot messages" and `PIPLOGS`, and the first real boot brought a `PIPLOGS` record back. The boot-messages and safe-graphics entries are still unexercised on real hardware |
| 4 | **Process overhead** | The project log runs to about 6,000 lines, the work was split across nine work areas, and there are 383 log files and 66 GB of evidence. Coordination cost now competes with building, and intent is hard to recover | Keep this manual as the single current-state description. Keep the project log as a log, not a specification. Prune or archive superseded candidates and images |
| 5 | **Model quality** | Qwen3.5-0.8B invents things and ignores memories it was given. A parrot guard was needed. The real model choice waits for hardware | Benchmark the Phase 1 shortlist on the laptop. Keep the "facts from code" rule |
| 6 | **Hearing on the real laptop** | The first boot heard the wake phrase but not the request. Every spoken feature depends on it | Run [FIRST-BOOT.md section 8](../FIRST-BOOT.md#8-round-2-hearing-typing-and-speed) with each microphone entry before anything else; typing to Pip is the fallback meanwhile |
| 7 | **All services run as root** | A bug in the Python mind or voice, both of which parse untrusted speech text and network JSON, runs with full privilege | Give the mind, voice and network-facing code dedicated users, and extend the eyes-style sandboxing to the others |
| 8 | **Lab signing key** | Updates are signed with a self-signed key in a Docker volume. Losing it means installed devices can't be updated. Leaking it means anyone can sign updates. The tooling for an owner-held key now exists (`lab/build.sh release-key`) | Create the owner-held release key with an offline backup before the first internal install |
| 9 | **Factory owner name** | `config/being.json` ships `owner: "Richard"` in every image | Make the default neutral ("friend", as `Profile` already uses). First-run setup sets the owner |
| 10 | **Backups depend on a stick being present** | Backups can be made from the F2 menu, by voice, and by the night shift to a labelled stick. Reminders and the weather city are not carried over. The design's own risk table calls SSD death "total" | Keep a labelled stick in the laptop, and check a restore end to end on the laptop before relying on it |
| 11 | **Broadcom Wi-Fi firmware is not packaged** | Redistribution terms are unresolved, so Wi-Fi may not work on first boot | Keep Ethernet as the documented path. Decide on offline firmware provisioning |
| 12 | **Open review findings** | The first review's 49 medium findings were never triaged one by one, plus known edge cases (believed-clock drift, an unpersisted brainstem time zone). Pip cannot tell voices apart, so "only the person who asked" means only the owner or a guest | Triage these before the first internal install |

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

1. **Keep the public repository current.** Commit at every checkpoint,
   and back up the local `out/` evidence to a second disk.
2. **Rerun the L2–L6 guest suites** on the rebuilt lab image.
3. **Follow up the first physical Try boot** (done 23 November 2026).
   Record a spoken turn, the lid, the camera and thermals on the real
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
| 2026-11-24 (evening) | Cable and diagnostics, on the `Dev` branch. On the MacBook the owner found two separate faults. The spoken voice is garbled on the laptop's playback path (the same clips are clear on another Mac; the leads are HDA stream-position and period timing, `bdl_pos_adj`, resampling and speaker drive), which `pip-diag speaker` is built to tell apart. Poor hearing is likely a quiet microphone with a dead left channel: the first boots measured the left channel at -84 dBFS and the right at about -63 dBFS; the fix's effect on hardware is not yet measured. In answer: `pip-diag`, which measures the microphone, speakers, voices, wake word and command routing and says in plain English what is wrong (on every image, and as Try menu item 8); a development-only cable stick with root SSH from link-local addresses and a writable copy of the code, which never installs; a command-grammar second pass, voice debugging and engine overrides; the laptop's microphone channel chosen automatically; Alba as the default voice (the owner's choice after a 27-voice audition); and Mac-style top-row keys. Host suites passed in the integration run at about 21:40 (Python 2,402 passed, 29 skipped; lab host checks 42 passed); `mind/voice.py` and `tests/test_voice_keys.py` changed after that run and may not be covered. None of it has been built, run in QEMU or tried on the laptop. Risk 3 (diagnosing the physical laptop) and risk 6 (hearing) now have direct tools, still unproven on hardware. Before a public release image ships Alba, the packed voice (a fine-tune of Piper's lessac voice, whose data is licensed for research only; fine for the owner's own stick) must be replaced by Piper's retrain of 9 November 2026 |
