# BeingOS improvement plan

Written 23 Nov 2026, from a full review of the source, the 23 Nov code review (`out/logs/review/full-review.json`), three verification rounds, the polish and readiness reports, both research reports, the project log and the owner's requests. Eleven review areas were checked, and their findings were verified against the code and against each other. Where two areas disagreed, the call and its reason are in section 2.

How to read it: section 1 is the state of things, section 2 the rules for the work, and section 3 the phases with numbered tasks. Section 4 answers the battery question. Section 5 maps every owner request and every open issue to a task. Section 6 lists what is deliberately left out.

---

## 1. Where BeingOS stands

**Works and verified (in QEMU and host tests).**
- A Buildroot 6.18 appliance that boots straight to the eyes, with a read-only A/B root, RAUC updates with rollback, and a signed model pack.
- The organs on the Unix-socket bus: face, brainstem, mind, cortex (Qwen3.5-0.8B), voice (PocketSphinx and Flite) and opt-in eyes.
- The face matches the EVE mockups.
- The installer and Try menu look polished: 19/19 on the local-screen suite.
- Memory is SQLite on `/mind`. It survives reboot and power cut. "Remember / what do you remember / forget / that's wrong" work, with spoken confirmation, and forgetting reaches the database, the search index, the WAL, the live prompt and the model's slot. A believed clock guards expiry.
- Offline is real: nftables lockdown, the model on loopback only, no DNS or NTP unless chosen.
- Try mode keeps nothing on the internal disk.
- Mac tests: Rust 121/121; Python 523 run, all passing (13/13 in `tests.test_candidate_tests` now that `laptop_smoke` is classified; 22 skipped for `age` and the real model).

**Unverified.**
- Nothing has run on the MacBook yet: HD 4000 graphics, the Cirrus codec, mic and speakers, applesmc, the lid, the backlight, the FaceTime camera, BCM4331 Wi-Fi, the battery, or real model speed.
- The laptop image (`beingos_laptop_defconfig`) was built for the first time on 23 November 2026 and has not yet booted on real hardware. It has no seal and no manifest, and only the new smoke test is bound to it.
- On this source, guest suites L2-L6 and the 22-suite controller were not rerun. Only L0, L1 and local-screen were.
- The first-word time (target 3 s or less) is unmeasured. The estimates for this CPU are 15-28 tok/s prefill and 8-15 tok/s decode, which gives 3-6 s on a warm turn and 30-120 s on a cold one.

**Biggest risks.**
1. **The memory cannot be protected once installed.** On the shell-less laptop image there is no way to back up or update. Any fix after an SSD install means reinstalling, which erases the being. The only signing key is an unbacked lab key, and the laptop shares RAUC compatible strings with the lab image.
2. **Silent first contact.** The installer says "Say hello", but only "Hey Pip" (then a pause) works. The mixer has never been seen. One audio error kills the voice organ and leaves the face stuck. F2, F9 and F10 are media keys unless fn is held. The clock runs on UTC, so on AEDT the being naps through the owner's day.
3. **Battery care is absent.** There is no charge cap, no low-battery plea, no clean shutdown and no lid-closed policy on battery. Section 4 has the fix.
4. **Memory bugs that destroy data.** "Forget that" after a recall also deletes an earlier, unrelated fact, and it can do so across guest mode. "No, that's wrong, it's X" rewrites the owner's own question. Recall answers "What is my cat's name?" with "My name is Pip."
5. **Headline features cannot be switched on.** The camera, dreaming, memory mode and Wi-Fi need root files or CLIs that the laptop image cannot reach. Dreaming does not yet do what the owner describes: there is no second model, and it runs midnight to 5 am only.
6. **Process.** Nothing is committed. out/ is 66 GB with 23 look-alike images. Status is spread over five documents.

None of this stops the first Try boot. Risks 1 and 4 must be fixed before the being is installed on the SSD with real memories.

---

## 2. Principles for the work ahead

1. **The USB image built on 23 November is frozen.** This plan changes nothing in it. Fixes land in a separate branch or worktree after its sources are committed and tagged (task 0.1). The first hardware boot uses that image as built.
2. **Two hardware rounds before any install.** Round 1 uses the frozen image and only collects data. Round 2 is a second Try image with the first-contact fixes (tasks marked **[Try-2]**). The internal SSD is touched only after Phase 1 is complete.
3. **Try only, until updates are safe.** Do not install to the SSD, not even with the maintenance variant, until the laptop has its own RAUC compatible strings, a release key and an on-screen backup and update path (tasks 1.2-1.4). Reason: on the maintenance variant the documented `installed-bundle` command signs the lab rootfs under the same compatible string and key, so RAUC would accept it and turn the laptop back into the lab image.
4. **Every phase ends with host tests, a VM run on an image built from that exact source (with a recorded source-id), and a dated line in STATUS.md.**
5. **Measure on the real laptop before choosing models, threads, quants, thermal targets or battery thresholds.** Numbers in this plan marked "estimate" are not measurements.
6. **Never write firmware state without consent.** No EFI, NVRAM or persistent SMC writes happen without an owner choice shown on screen. Power controls restore defaults on exit, and new controllers ship in observe-only mode first.
7. **Move, don't delete.** Old images and candidates go to the NVMe archive.
8. **Parallel lanes.** Tasks are tagged with a lane so several pieces of work can proceed at once without touching the same files. **A** mind (Python), **B** Rust organs, **C** OS, kernel and build, **D** lab, tests and docs, **E** voice. Tasks in different lanes with no shared "depends on" can run in parallel.

### Calls made where the evidence conflicted

| Question | Call | Reason |
|---|---|---|
| Are Wi-Fi/Internet, default skills, the installer, voice choice and the Jev exploration "drift"? | **No.** The owner asked for them in a parallel workstream. | The requests are recorded in the project log. The 23 Nov review's drift labels #2, #5 and #7 are corrected. Jev ended negative and is archived (task 8.8), not reverted. |
| Charge-cap route | **An in-tree applesmc patch**, not applesmc-next. | applesmc is built in (`linux.config:111`), so an out-of-tree `applesmc.ko` can never load. |
| Is cpufreq missing from the kernel? | **No.** Only `CONFIG_X86_MSR` is missing. | The built kernel has intel_pstate (passive intel_cpufreq) and schedutil/performance (`out/ivybridge-power-policy/README.md`). |
| Is the mixer never initialised? | **It is.** alsa-restore falls back to `alsactl init`. The gap is control, tuning and persistence. | `alsactl` is built for both defconfigs (1958 build.log). The fix is volume control plus `alsactl store` to `/mind`. |
| Fans | **Raise `fan1_min` only; never manual mode; no mbpfan.** Raise the floor only with the lid closed or when not listening. | Safe if the controller dies, and keeps fan noise away from the mic. |
| llama.cpp batch size | **`LLAMA_ARG_BATCH=512`, ubatch 512.** | 128/128 failed to match long replies. 512 logical cut cancel-to-release from 7.3 s to 1.75 s (the staged `90-batch-trial.conf`). |
| Hybrid slot restore (#28194) | **Measure it:** `cache_n` after a restore on the laptop. | The pinned v1.0.0 appends checkpoints to the slot file. The research worry may not apply here. |
| Persist the brainstem UTC offset? | **No.** Set a default zone and add a Setup step instead. | The mind re-sends `mind.clock` on every 0.5 s beat. |
| Ship llama-bench? | **No.** Bench mode drives llama-server directly. llama-bench goes in the maintenance build only. | One harness, real production flags, no extra binary on the appliance. |
| `--mlock` | **Optional; measure it.** | No swap, and 16 GB is ample. |
| Senses line in the prompt | **Notable changes only, worded in first person** ("My battery is low…"). | The LAB.md A/B showed routine readings hurt this model. First person stops "You are awake". |
| Wi-Fi | **Wired Ethernet first** (no pairing needed). Then b43 with owner-staged firmware as an experiment. Fall back to an MT7612U USB adapter. | b43 HT-PHY support for the BCM4331 is weak. |
| Models partition | **Reserve 24 GiB at install** (an owner choice from 16 to 32). | The second dream model will not fit, and the partition cannot grow later. |
| Low-battery triggers | **Time to empty first, percentage second.** Plea at 15% or under 30 min; a second plea at under 10 min; save and power off at 5% or under 5 min (or a voltage floor). | Worn cells cut out at a reported 10-30%. Calibrate in the first hardware test. |
| Studio-assisted (off-machine) cortex | **An owner decision.** Default is loopback only on the appliance. | It is part of an alternative plan he adopted, but it is not wanted by default. |
| Dream model size | **Qwen3-1.7B Q4_0.** A 4B is an optional "long cool night" arm only. | Research report of 23 Nov; the owner's own "4B is too heavy". |
| Dream store | **Keep SQLite.** | PostgreSQL adds a server and RAM for nothing on one device. |
| Severity of "no on-device update path" | **High.** | It decides whether the being survives its first fix. |

---

## 3. Phases

Effort: **S** under an hour, **M** a few hours, **L** a day or more.

### Phase 0: First real boot with the frozen USB

**Goal.** Boot the MacBook from the frozen laptop-try image, learn as much as possible, and change nothing on the internal disk.

**Exit criteria.**
- The image's SHA-256 is recorded and its source tagged.
- The owner has run the boot script.
- BEINGLOGS has been copied to the Mac and parsed.
- A one-page "what we learned" note is written into STATUS.md.

**Blocks the first boot:** 0.2, 0.3 and 0.4 only.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 0.1 | **Commit and tag the source** of the first Try build as `first-hardware-try` (only with the owner's yes). Move older images to `out/images/lab/` (move, do not delete). | Nothing is committed, so no stick can be tied to its code. Prevents writing a lab image by mistake. | git; `out/images/` | S | owner yes | D |
| 0.2 | **Finish the laptop-try image.** Record its SHA-256 next to `/etc/being/build-id`. Run `test_laptop_smoke` and a static audit of exactly that file: no getty, root locked, no sshd, no lab units, BEINGLOGS present. | The laptop Try image was built on 23 November 2026 and has not yet booted on real hardware. | `lab/build.sh laptop-try`, `lab/tests/test_laptop_smoke.py`, `lab/hardware_image.py` | S | in-flux build | D |
| 0.3 | **Verify the in-flux boot-visibility work on that image.** Check the boot-messages entry shows kernel text, the face or Try menu shows with no brainstem, Try never reboots in a loop, and BEINGLOGS keeps a journal after power-off. | Without these, a failure on the Mac gives a black screen with nothing to read. | `try-grub.cfg`, `link.rs`, `being-console.c`, `being-brainstem.service`, `mind/usb_logs.py` | S | 0.2 | D |
| 0.4 | **Write `docs/FIRST-BOOT.md`, one page.** It covers which file and SHA-256 to write; `diskutil list`, `unmountDisk`, `sudo dd … of=/dev/rdiskN bs=4m`, then read back and hash; Option, then EFI Boot, then the three GRUB entries. Then the script below. Then a short press of the power button to shut down, and opening BEINGLOGS in Finder. It also lists known surprises: hold **fn** for F2, F9 and F10 (M mutes, Esc interrupts); say **"Hey Pip", pause, then talk**; silence is probably mixer state, not a dead organ; the lid logo may stay lit; summary.txt wrongly says "applesmc NOT found"; the clock is UTC. | No usable procedure exists. HARDWARE.md assumes a shell the laptop image removed. | `docs/FIRST-BOOT.md`; mark `docs/USB-READINESS.md` superseded | S | 0.2 | D |
| 0.5 | **Owner's boot script.** On AC with the lid open: 10 min idle (the warm-up measures prefill). Check hardware, then save. "Hey Pip" + "what time is it" (no model). Three short chats. "Remember that my cat is called Tom", then "What do you remember about my cat?". One long question. Unplug for 2 min, then replug. Close and open the lid. Press F10 and fn+F10, F2 and fn+F2, Esc, click the trackpad. Photograph the screen at each step and note stopwatch times from power to eyes and from end of speech to first word. | Turns the first boot into data. | `docs/FIRST-BOOT.md` | S (owner) | 0.4 | owner |
| 0.6 | **A Mac-side BEINGLOGS reader.** `lab/try_timings.py` (stdlib) prints the CPU model, renderer, failed units, state-change timestamps, and per request: prompt tokens, cached tokens, prompt and decode tok/s. | The journal already holds llama-server timing lines and brainstem state changes with microsecond stamps. | `lab/try_timings.py` | S | none | D [par] |
| 0.7 | **macOS chores before any wipe (owner).** If macOS is still on the internal SSD: install the last firmware update; record the battery cycle count and condition; set a firmware password (optional); run `bclm read`, then `sudo bclm write 77`, charge, and watch for "Not Charging" at about 80%. Decide between overwriting the Lexar's installer and using a second stick (recommended: a second stick, or make a Catalina installer first). | These steps are impossible after the wipe, and the charge cap is the first battery protection. | owner; record in `docs/HARDWARE.md` | S (owner) | none | owner |
| 0.8 | **Export the lab signing key** to an encrypted owner-held copy (age, on the NVMe archive). Make `lab/build.sh up` refuse to regenerate the key silently, and make `destroy` refuse until the key is exported. | It is the laptop's only trust root, and it lives only in a Docker volume. | `lab/build.sh` | S | none | C [par] |
| 0.9 | **Owner decision sheet** (`docs/DECISIONS-OWNER.md`). Each item has a recommended default and a one-line consequence: time-zone city (AEDT is implied); battery mode A or B; dream window and temperature target; camera default; voice natural or synthetic; recall order (relevance or recency); whether announcements are on by default; the Lexar or a second stick; release-key custody; Studio-assisted mode; scope of the terminal "workshop"; whether to accept the LFM licence for auditions; whether the mic stays open while asleep. | Several choices only he can make are scattered and silently defaulted. | new doc | S | none | D [par] |

### Phase 1: Fixes that must land before installing to the internal SSD

**Goal.** Make the being safe to install. Its memory must survive fixes, its data must be correct, and first contact must work. Tasks marked **[Try-2]** go into the second Try image, built after reading the Phase 0 logs.

**Exit criteria.**
- All Phase 1 tasks are merged.
- A sealed laptop candidate passes the boot gate (task 8.1) and the full controller on the lab image.
- An update from a USB stick and a backup/restore round trip both pass in QEMU, then on the Mac from a Try boot.
- The owner has answered the decision sheet.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 1.1 | **A laptop candidate lane.** Add `lab/build.sh candidate NAME --laptop`: build the laptop and lab images from one frozen snapshot, seal both in one `release.json`, and write a source-id into `/etc/being/source-id`. Add a lab-vs-laptop rootfs parity audit (allow-listed differences only: lab body, sshd, lab-access, gettys). Repeat the Lexar geometry readback for the 4-partition layout. | The image that boots the Mac is outside the sealed lane. | `lab/candidate-build.sh`, `lab/candidate_manifest.py`, `lab/candidate_tests.py`, `lab/hardware_image.py` | M | 0.1 | D |
| 1.2 | **Release key and laptop identity.** Create a passphrase-protected owner release key with two offline copies. Post-build sets `beingos-laptop-v1` and `beingos-laptop-installed-v1` when `BEINGOS_VARIANT=laptop`, writes `/usr/share/being/variant`, and ships only the release cert. `being-update-ready` refuses a bundle of the wrong variant. Add `lab/build.sh laptop-bundle`. Point HARDWARE.md at the laptop commands. Check that RAUC certificate time checks survive a dead RTC. | Prevents an update from turning the laptop into the lab image, and prevents key loss from stranding the being. Must land before the first install, because the keyring is baked into the installed root. | `os/board/beingos/post-build.sh`, `rauc.conf`, `being-update-ready`, `lab/updates.sh`, `lab/build.sh`, `mind/model_pack.py`, `docs/HARDWARE.md` | M | 0.8 | C |
| 1.3 | **"Update from a USB stick" in F2.** Mount a stick read-only (nodev, nosuid, noexec), list `*.raucb` files, run `rauc install`, show the version and the result, reboot into the trial slot, and rely on boot-health rollback. | No on-screen update path exists. | `mind/setup.py`, `mind/local_menu.py`, `mind/console_ui.py` | M | 1.2 | A |
| 1.4 | **"Back up to a USB stick" in F2 and Try.** Run `being-backup create` plus `export-deletions` plus `checkpoint`. Write the checkpoint as a file and show its first and last 8 characters. Restore reads the checkpoint from the file and asks the owner to confirm the short fingerprint, instead of typing 64 hidden hex characters. Accept a passphrase-protected age identity, and warn when the key sits on the same stick as the backup. Either include `reminders.db`, `initiative.json`, the weather city and `core.md`, or change "Everything" to an honest wording. Have `create` use `read_policy` and fail loudly on damage. | The being can be restored but never backed up. A create-only backup restores nothing. | `mind/backup.py`, `mind/backup_crypto.py`, `mind/local_menu.py`, `mind/setup.py` | L | none | A [par] |
| 1.5 | **Installer safety.** Read the target GPT read-only. If it holds APFS or HFS+, show "This drive holds macOS" with the pre-wipe checklist from 0.7 and a second confirmation. If it holds a BeingOS mind partition, offer **"Update the system, keep its memory"** and put erasing behind a distinct confirmation. After `invalidate_boot`, any error says the disk is erased and not startable. The finish screen adds the Option/Control-Enter startup-disk hint. Discard the mind range (BLKDISCARD) on erase. Default "Keep this Try profile?" to Yes when the profile differs from factory. | A disk holding a living being looks the same as a blank one, and the macOS-only battery and firmware steps are lost. | `mind/installer.py`, `mind/install_apply.py`, `mind/install_disks.py` | M | none | A [par] |
| 1.6 | **Reserve space on the models partition** (default 24 GiB; the owner can choose 16-32) and show it on the layout screen. Let the pack format carry `conversation` and `dream` roles. | The dream model and model upgrades would otherwise force a reinstall. | `mind/installer.py`, `mind/model_pack.py`, `default-model/pack.json` | S | none | A [par] |
| 1.7 | **Installed boot safety.** Drop `oops=panic` in the laptop `boot_console`. Add a GRUB unconfirmed-boot counter, and after 3 unconfirmed boots use a "show messages" entry with `panic=0` and the watchdog off. Use `timeout_style=hidden timeout=1` with Previous slot, Show messages and Safe graphics entries. Keep a persistent journal in `/mind/log/journal` (64 MB, 30 days) plus a `boots.txt`. Put bootstate and models in `local-fs.target.wants` with `nofail`. In `deny()`, always mount tmpfs `/var` and reboot on trial. Recreate an unreadable grubenv from the booted slot, keeping `grubenv.bak`. Allow updates when the booted slot is GOOD even if degraded. | A driver oops on a confirmed slot gives an endless loop. There are no logs after install. One damaged ESP leads to emergency mode with no login. | `os/board/beingos/grub.cfg`, `slots.sh`, `installed-grub.cfg`, `being-installed-generator`, `mind/boot_health.py`, `being-update-ready`, `post-build.sh` | M | none | C [par] |
| 1.8 | **[Try-2] Small kernel and command-line changes for the laptop.** Add `CONFIG_X86_MSR=m`, `hid_apple.fnmode=2` (or a oneshot writing it to sysfs), and `quiet vt.global_cursor_default=0` plus `ShowStatus=error` on installed laptop boots. Check the resolved `.config`. | MSR access for battery-out mode. The top row then sends F-keys. Boots show only the eyes. | `os/board/beingos/linux-laptop.config`, `slots.sh`, `post-build.sh` | S | 0.2 done | C [par] |
| 1.9 | **[Try-2] A "Senses and memory" (Care) page in F2.** Camera on/off (calls `being-camera`, with the LED explained). Dreams on/off with when it dreams. Memory mode with the scope warning. Join Wi-Fi (scan, pick, hidden passphrase prompt). Wired Ethernet on/off with no pairing. Date and time (clock plus `hwclock -w`, recorded in Memory's clock). Time zone. "What I remember": numbered facts and recent journal, forget by number with voice-style confirmation. Dream-journal viewer. In Try, apply to the RAM `/mind`. Add spoken toggles: "you can use your camera", "stop watching", "you can dream tonight". | The headline features sit behind root files on an image with no shell. | `mind/setup.py`, `mind/console_ui.py`, `mind/commands.py`, `mind/turn.py`, `mind/wifi.py`, `mind/network_enforce.py`, `mind/lab_access.py` | L | none | A |
| 1.10 | **[Try-2] A time zone from the first boot.** Add a factory `time_zone` to `config/being.json` (the owner's city from 0.9). Add a Setup and Try-to-Install step. `find_zone` accepts a unique last part ("Sydney"). Seed `skills.json` (zone and weather city) into the install. Until a zone is known, drop the clock from the senses line and turn off night-based drowsiness. | On AEDT the clock, quiet hours, drowsiness, morning wake and dream window are about 11 h off. | `config/being.json`, `mind/skills.py`, `mind/setup.py`, `mind/install_apply.py`, `crates/brainstem/src/body.rs` | S | 0.9 | A |
| 1.11 | **[Try-2] Memory data-loss and correction bugs.** (a) Remove `_last_telling`; carry the telling inside `_last_memory` and set it only on remember; clear it on guest change. (b) Accept "that's wrong, …" only when the last turn's subject came from memory. Splice "it's X" fragments into the fact, or ask for the whole sentence, and read the result back. (c) Drop "it" from `forget_last`, or confirm it. Bind `_last_memory` to the speaker. (d) Widen `_YES` (okay, sure, yes forget them, yes I'm sure). Any other reply while an offer is open answers "I've kept everything" in code. (e) Add tests: remember A, remember B, recall A, forget that, then B survives; the same across guest mode; and the Canberra case. | Ordinary use silently deletes memories, and corrections put words in the owner's mouth. | `mind/turn.py`, `mind/commands.py`, `mind/memory.py`, `tests/test_mind.py`, `tests/test_commands.py` | M | none | A |
| 1.12 | **[Try-2] Damaged files must not mean rebirth.** Tell a missing file (use defaults) from an unreadable one (error). When unreadable, keep `context.json` and the slot, show and say "unwell: my settings are damaged", and write `.bak` copies on save. `life.json` and `mind.db` are created only on a confirmed empty `/mind`. Persist guest mode in `/run/being/guest` and show it on the face. | One bad block makes a stranger, an amnesiac, or a guest silently recorded as the owner. | `mind/retention.py`, `mind/profile.py`, `mind/initiative.py`, `mind/__main__.py`, `mind/memory.py`, `crates/brainstem/src/life.rs` | M | none | A [par] |
| 1.13 | **[Try-2] A voice organ that survives bad audio.** Catch synthesis and playback errors per clause, skip the clause and still send `say.done final=true`. Retry capture with backoff (1-30 s). Bound the aplay wait. Order the unit after the sound-card device. In the brainstem: `organ.down` of mind or voice mid-turn sends `say.stop` and `mind.cancel`, then attends. Both timeouts send both stops. `hear.final` with no mind gives a concern look and attends. `attend()` goes to Idle when muted. Add unit tests. | The first codec hiccup leaves a deaf, mute being stuck "speaking" for up to 60 s or "thinking" for 120 s, in a 1 s restart loop. | `mind/voice.py`, `os/package/being/being-voice.service`, `crates/brainstem/src/machine.rs` | M | none | E + B |
| 1.14 | **[Try-2] Brainstem state across restarts.** Write `/run/being/stem-state` (muted, coarse state, natural sleep, lid_seq mark) and load it before `Machine::new`. Set `lid_wake` only after a confirmed closed reading or one completed wake. In `life.rs`, reuse this_wake when the boot id matches, and compute "awake N" on CLOCK_BOOTTIME. Add a Machine test for the physical boot sequence and a hub test with kill -9 while muted. | Every hardware boot goes idle before the mind is up. A restart reopens the mic and fakes a nap. | `crates/brainstem/src/machine.rs`, `main.rs`, `life.rs`, `tests/hub.rs` | M | in-flux brainstem work landed | B |
| 1.15 | **[Try-2] A think timeout that cannot kill legitimate replies.** Stopgap: `BEING_THINK_TIMEOUT` in `/etc/being/env`, passed as `--think-timeout` (300 s). Real fix: a `mind.progress` topic every ~5 s while the cortex is working, a no-progress deadline of ~45 s, and a configurable hard cap. | Cold turns on this CPU are estimated at 70-270 s, which hits the 120 s cancel. | `crates/brainstem/src/machine.rs`, `main.rs`, `mind/turn.py`, `mind/cortex.py`, `docs/PROTOCOL.md`, `being-brainstem.service` | M | none | B + A |
| 1.16 | **[Try-2] First words that work.** Installer finish and Try welcome say 'Say "Hey Pip", wait for the eyes to brighten, then talk.' (from `profile.wake_word`). An attention key: Space, Return, or a trackpad click while idle or drowsy sends `key.attend`, which is handled like a wake. A 60 s attend window on a birth wake. Accept `Mute` and `AudioMute` keycodes as mute. Make the Setup voice sample mute the mic itself. | The on-screen instruction is wrong, and if wake spotting fails there is no other way in. | `mind/installer.py`, `mind/local_menu.py`, `crates/face/src/main.rs`, `os/package/being/being-console.c`, `crates/brainstem/src/machine.rs`, `mind/setup.py` | S | in-flux face work landed | B + A |
| 1.17 | **[Try-2] Volume and levels.** Add "speak louder / quieter / set your volume to N" and a Setup "Volume and microphone" page with a test tone and a mic level meter. Handle the volume keys. Run `alsactl -f /mind/audio/asound.state` store and restore in an ExecStartPre. Choose the card by name (`plughw:CARD=PCH,DEV=0`, after confirming in Phase 0 logs). | The owner cannot make it louder or quieter, and levels reset every boot. | `mind/voice.py`, `mind/commands.py`, `mind/setup.py`, `being-voice.service`, `crates/face/src/main.rs` | M | Phase 0 logs | E |
| 1.18 | **[Try-2] Hardware report that answers the open questions** (all read-only). Read applesmc on the **6.18 layout** (`device/` of the nameless hwmon) and fix the test fixture (LVDS, not eDP). List SMC keys (BCLM, BFCL, CH0B, CH0C, fan keys) through `key_at_index`, and read data only for BCLM and BFCL. Add cpufreq, intel_pstate, no_turbo and max_perf_pct; RAPL limits and lock state; thermal_throttle counts; a 3 s single-thread load sampling MHz (flags a 1.2 GHz clamp). Add battery cycle count, full versus design capacity, and TB*T temperatures. Add `amixer scontents`, DRM connector types and dpms, bl_power before and after a 1 s blank with an owner prompt to look at the lid logo. Add a 10 s keycode capture (F1-F12 with and without fn), USB power/control, SATA LPM, rfkill, lspci -nn and the b43 core revision, and SSD SMART/wear (add smartmontools). Add a camera check with explicit consent: 10 s of presence, look for the green LED, then close the lid. | Each later decision in Phase 2 hinges on these facts. | `mind/usb_logs.py`, `mind/hardware_metrics.py`, `mind/installer.py` (Check hardware), `crates/brainstem` probe, `tests/test_usb_logs.py`, laptop defconfig | M | in-flux usb_logs work landed | A + C |
| 1.19 | **[Try-2] Minimum battery behaviour.** Speak the pleas and do the clean shutdown on the thresholds in section 4. Shut down after N minutes (default 10) closed on battery. Add a thanks on plug-in after a warning. Choose the check-in reply from real state. | The old battery cuts out with no warning and no save. | `crates/brainstem/src/body.rs`, `machine.rs`, `mind/turn.py`, `mind/profile.py`, `config/being.json` | M | 1.18 data for calibration | B + A |
| 1.20 | **[Try-2] Laptop-image unit fixes.** Setup restarts only units that exist (drop `being-lab-body` when absent). Network reconcile builds its unit list from installed units, or treats exit 5 as stopped, and starts OBSERVE only if present. Add a host test that every `*.service` named in `mind/*.py` and `os/package/being/*` exists in the laptop unit set. | On the laptop, Setup shows a red error and changing the network mode always fails. | `mind/setup.py`, `mind/network_enforce.py`, new `tests/test_laptop_units.py` | S | none | A [par] |
| 1.21 | **Decide on every staged patch in `out/`**, merging or dropping each and recording the decision in STATUS.md. Merge the snapshot transport and honest fallback from `deliberate-look` (the describer comes later). Merge `click-interrupt` and `brightness-keys` after the Phase 0 keycodes are known. Merge `sphinx-ds` (42% less wake-spotting CPU), `playback-completion` (about 22 ms less gap per clause) and `startup-order` (faster first reply), each after its own qualification. Keep `crocus-softpipe` for task 2.1. Fold in the `hardware-inventory` docs patch and the `installer-power-key` test. Drop `try-storage-menu`, which is superseded by the hardware report. | The owner's 21 Nov "merge all changes" is still undone, and the patches drift further each day. | `out/*/*.patch`, then the target files | M | 0.1 | D (review) + owners per lane |
| 1.22 | **Test hygiene for the gate.** Lab suites refuse an image whose source-id differs from the tree (`BEING_ALLOW_STALE=1` overrides). In `BEING_GATE=1` mode, skips and missing images fail, and `test_wifi` skips outside gate mode. Move the `ExecStopPost` reboot logic into `being-brainstem-failed` with host table tests. Strengthen laptop smoke: probe VTs 2-6 for a login prompt, test a hard quit with no ACPI event (a start line with no end line), and check that all organs are active with NRestarts=0. Make the hub reply `hello.ok`, and have clients wait for it (fixes a 5-7% flaky test and a real join race). | Green results today can come from 21 Nov images or skipped suites. | `lab/vm.py`, `lab/tests/*`, `os/package/being/being-brainstem.service`, `crates/bus/src/lib.rs`, `crates/brainstem/src/main.rs`, `mind/bus.py`, `crates/face/src/link.rs` | M | 1.1 | D + B |
| 1.23 | **Bring the documents up to date.** README gains a one-page honest status and a "What Pip is, honestly" paragraph he can share: a local 0.8B model, SQLite memory, a rule-based nightly journal today, a dream model planned, not sentient. Mark USB-READINESS and DESIGN-COVERAGE superseded by STATUS.md. Change DESIGN §7/§8 to the 1.7B dream default. Record the CPU as i5-3210M, to be confirmed from `/proc/cpuinfo`. Add `life.woke` and `mind.clock` to PROTOCOL.md. Note in full-review.json that drift items #2, #5 and #7 were owner requests. Fix LAB.md's "the same image for the laptop" and its cleanup step that deletes evidence. | Readers see stale and wrong status first. | `README.md`, `docs/*.md`, `DESIGN-AND-PLAN.md` | S | none | D [par] |

### Phase 2: Hardware bring-up from first-boot data

**Goal.** Make the body work properly on the real Mac: screen, sound, keys, lid, camera, battery, heat and power.

**Exit criteria.**
- From the second Try round: the face runs at 60 fps on crocus (not llvmpipe).
- The backlight is off with the lid closed.
- Speech is audible and "Hey Pip" is heard at 2 m over fan noise.
- The charge cap is confirmed holding, or the fallback chosen.
- The fan floor and CPU caps restore defaults on exit.
- Idle power at the wall is 9 W or less awake and 6 W or less asleep with the lid closed (targets; measure).

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 2.1 | **Graphics.** Confirm crocus on the HD 4000. Log frame time and RAPL PP1 once a minute. Render at 60 Hz when speaking, attending or blinking, 30 Hz when idle or thinking, and 5 Hz when blank. Give the face `CPUWeight=500` and Nice -5, and set `LP_NUM_THREADS=1`. Show a software-renderer note on screen and in the report. Once crocus is confirmed, apply `crocus-softpipe` and drop target LLVM (which also speeds up builds). | The face must never steal the model's cores. | `crates/face/src/main.rs`, `render.rs`, `being-face.service`, laptop defconfig | M | Phase 0 | B + C |
| 2.2 | **Lid and panel.** When blank, the face turns the connector off through KMS DPMS on SDL's own DRM fd, and turns it on before the first wake frame. Fallback: save brightness to `/run`, then set it to minimum. Blank only on a confirmed closed lid. Restore the panel on brainstem exit. F9 or any key while the panel is dark and the link is quiet acts as a local unblank. | bl_power does nothing on this LVDS panel, so the backlight stays on all night. A closed lid plus a failed brainstem gives a dark screen for good. | `crates/face/src/main.rs`, `link.rs`, `anim.rs`, `crates/brainstem/src/linux_body.rs`, `body.rs` | M | 2.1 | B |
| 2.3 | **Frozen-face watchdog.** Move `service_watchdog` into the bus crate. Ping it once per frame. Set `WatchdogSec=5s` and `NotifyAccess=main`. Change F2 from `.status()` to `.spawn()` with `try_wait()`. | Nothing detects a frozen face. | `crates/bus`, `crates/face/src/main.rs`, `being-face.service` | S | none | B [par] |
| 2.4 | **Audio tuning.** Record good default levels for the MacBookPro9,2 CS4206 as the factory `asound.state`. Fix the leading-pause trim (`BEING_VOICE_SHORTEN_FIRST_PAUSE`) after a listening check. | Generic defaults are about 20 dB under full, and the mic gain is unknown. | `os/package/being/`, `mind/voice.py` | S | 1.17, Try-2 data | E |
| 2.5 | **Keys and body inputs.** Confirm the keycodes. Finish mapping mute, volume and brightness (from the rebased patch). Trackpad click interrupts a reply. | The design says the body keys act on its body. | `crates/face`, `crates/brainstem/src/linux_body.rs` | S | 1.21 | B |
| 2.6 | **Camera.** Verify the LED and that capture stops on lid close. Confirm presence detection works with the real FaceTime HD camera (the 9,2 has a UVC-class camera, unlike later models). | "The webcam is its eyes." | `mind/eyes.py`, `being-eyes.service` | S | 1.9 | A |
| 2.7 | **Charge limit: the applesmc patch.** Add a `battery_charge_limit` (BCLM) and `battery_charge_led` (BFCL) attribute, created only if the key exists, accepting 20-100 and writing nothing at probe time. If the probe finds CH0B/CH0C, add an optional `battery_charge_inhibit`. Verify on the Mac: write 80, then watch the status go to "Not charging" at 80-84%. | See section 4. | `os/patches/linux/0001-hwmon-applesmc-battery-charge-limit.patch` | M | 1.18 data | C |
| 2.8 | **`being-bodycare`, one fail-safe power and thermal controller** (Rust crate `crates/bodycare`, Type=notify, watchdog, ExecStopPost `--restore`). (1) Re-assert the charge limit at boot and every 10 min, and detect a cap that is not honoured. (2) Raise `fan1_min` only, from the smoothed package temperature (coretemp "Package id 0"). Never manual mode. Steps of 250 rpm per 10 s. Held steady while listening. Raised only with the lid closed or not listening. Restore the factory minimum on exit. (3) CPU caps by state through intel_pstate `max_perf_pct` and `no_turbo`: talking uncapped (unless the package is over 92 °C); asleep or on battery no_turbo; dreaming uses a PI loop to the owner's target. (4) Battery-temperature gate: pause dreams over 40 °C, cap the CPU over 45 °C, resume under 37 °C. (5) Publish `body.care`. Ship in observe mode first, and enable writes after one supervised run. | One owner of every power write, which restores safely. Needed by dreaming. | `crates/bodycare` (new), `os/package/being/being-bodycare.service`, `config/being.json` | L | 2.7, 1.18 | B |
| 2.9 | **Power settings.** Add udev USB autosuspend for the camera (05ac:8509), Bluetooth and IR receiver, but never the keyboard or trackpad (check IDs from the report). `rfkill block wlan` when offline and `bluetooth` always. Turn the keyboard backlight off while asleep. Verify SATA `med_power_with_dipm`. Run `fstrim.timer` weekly on `/mind` and `/models`. Measure and only then adopt: HDA power_save (only while asleep), `i915.enable_fbc=1`, `pcie_aspm=powersave`. | Battery runtime and heat. | `os/package/being/60-being-power.rules`, `mind/network_enforce.py`, `crates/brainstem` | S | 1.18 | C |
| 2.10 | **Wi-Fi.** Ethernet first (from 1.9). For b43: the owner supplies the firmware, staged with `lab.b43_firmware` and its manifest, plus `wireless-regdb`. Check the core revision. If unreliable, document a supported MT7612U USB adapter. Add `IPv6PrivacyExtensions=yes`, `SendHostname=no`, `scan_ssid=1`, and back-off in `being-wifi` when there is no interface. | Wi-Fi is requested but cannot work today. | `os/configs/beingos_laptop_defconfig`, `mind/wifi.py`, `being-wifi.service` | M | 1.9 | C + A |
| 2.11 | **Kernel diet.** From the report's loaded modules and `lspci -k`, generate a laptop fragment that removes unused drivers and filesystems. Keep i915, HDA Cirrus, applesmc, tg3, b43/bcma, UVC, bcm5974, ACPI SBS, MSR and exFAT. Re-measure boot to face. | "Strip down Linux Kernel to the bare minimum." | `os/board/beingos/linux-laptop.config` | M | Phase 0 | C |
| 2.12 | **Startup-disk delay.** Document Option, then highlight EFI Boot, then Control-Enter (or `bless` from Internet Recovery). Measure. | About 30 s lost on every boot. | `docs/HARDWARE.md`, installer finish screen | S | 1.5 | D |
| 2.13 | **Linux-only Rust tests.** Run `cargo test --workspace` for x86_64-unknown-linux-gnu in the Colima builder. Rerun L5 against the hardened `linux_body.rs`. | `linux_body.rs` unit tests have never run anywhere. | `lab/build.sh` | S | none | D [par] |

### Phase 3: Correctness and the remaining medium findings

**Goal.** Memory, time and speech behave honestly in everyday use.

**Exit criteria.**
- Every row of section 5's issue table is closed or deferred with a reason.
- Every bug reproduced in a verification round has a regression test.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 3.1 | **Recall precision.** Strip possessive 's and ’s, and add contractions and fillers to `_STOP`. Facts first. Inject history only for memory questions or a confident match (2+ distinct terms, a bm25 floor). Use only owner "heard" lines. Use one excerpt of about 25 tokens with relative dates. Leave canned replies out of history. Apply date ranges only to memory lookups. | "What is my cat's name?" fails. Most turns carry 40-80 extra tokens of loosely related history (an estimated 2-5 s each). | `mind/memory.py`, `mind/turn.py`, `mind/commands.py` | M | 1.11 | A |
| 3.2 | **Memory questions answered properly.** Remember tails starting with when/how/what/why or ending in "?" go to the model. "Do you remember me?" is answered from the profile and life record. "What did we talk about?" summarises the owner's recent words. "What do you know about me?" lists the latest facts with numbers. Pronoun reflection in read-backs ("Got it. Your bike is called…"). Read back what was stored. Flag command replies so history skips them. | Memory sounds broken or robotic on first use. | `mind/commands.py`, `mind/turn.py` | M | 3.1 | A |
| 3.3 | **Facts that update.** On remember, detect the same subject and ask "Should that replace …?". Rank within a bm25 band by `coalesce(confirmed, ts)`. Expire on the same value, and link the retelling as a source. Answer the recall-order question from 0.9. | Contradictory facts are recalled together. | `mind/memory.py`, `mind/turn.py` | M | 0.9 | A |
| 3.4 | **Honest time.** Time, date and age answers consult `Memory.clock()` and say when unsure ("I lost track of time when my power went out. What time is it?"). Add "set the time to …" by voice. Age comes from the believed clock. Absolute reminders are held while the clock is not believed. Format dates in the owner's zone everywhere. | With an old battery, the RTC can reset, and the being then states wrong times confidently. | `mind/turn.py`, `mind/skills.py`, `mind/reminders.py` | M | 1.9, 1.10 | A |
| 3.5 | **Reminders that ring.** A due item with the lid open sends `being.wake` first. A closed-lid item is announced as overdue on the next opening. Late timers are worded honestly. Use human wording without IDs ("Richard, your thirty-minute timer is done"). Repeat up to 3 times until acknowledged. Say at creation when delivery is blocked. Forget also covers matching reminders. | Timers can silently fail. | `mind/turn.py`, `mind/skills.py`, `mind/reminders.py`, `crates/brainstem/src/machine.rs` | M | 1.10 | A |
| 3.6 | **Expiry (opt-in) done gently.** Scrub only the affected live messages. Count days in the owner's zone. Run only when asleep or idle, in a worker thread, in batches of 200 rows. Hold back the cutoff during an unconfirmed clock step. Say once when expiry is paused. | Today it wipes the conversation every day at about 11:00 Sydney time. | `mind/turn.py`, `mind/memory.py` | M | none | A |
| 3.7 | **Forgetting residuals.** In explicit-only mode, `forget_last` drops the last live pair. Normalise case, spacing and apostrophes in Python for copies. Day forgets match on the confirmed time. Count what `edit()` actually affects. Link replies only to `reply_to` plus the notes they read. "Except/since" date requests are answered as unsupported. Strip the speaker and coarsen timestamps on tombstones. After a forget, say once that older backups hold it until refreshed. | Gaps in the "forgetting really deletes" promise. | `mind/turn.py`, `mind/memory.py` | M | 1.11 | A |
| 3.8 | **Turn robustness.** Send `reply_done` before recording, and wrap the record in try/except. Pop unpaired user messages. Retry once on a context-size error. Compact greeting, check-in and simple-command pairs go into the model context. The parrot guard fires only on a question, or on 80% or more coverage with no new content word. | A rare 60 s mute, and disjointed follow-ups. | `mind/turn.py` | S | none | A [par] |
| 3.9 | **Honest self-talk.** Add deterministic answers to "are you alive / conscious / do you feel" ("I'm a small program living in this laptop… I don't know that I feel things the way you do"). Add a line to identity.md: never claim to be conscious. Word the look reply from the actual camera state. Check-in replies come from body state. Add "What did you dream about?" (also 6.1). | Honest-body principle, and the owner's 23 Nov "Is this accurate?". | `mind/commands.py`, `mind/turn.py`, `config/identity.md`, `mind/profile.py` | S | none | A [par] |
| 3.10 | **Voice privacy.** Shorten the follow-up window to about 6-8 s. Do not renew it on `hear.cancel` or after unprompted turns. Archive unanswered finals as "overheard" (not durable). Send speaker "unknown" until speaker ID exists. Keep wake spotting while asleep, add an "ears open" cue, and correct DESIGN line 345. | A TV can keep the being in a reply loop, and room speech is stored as the owner's words. | `mind/voice.py`, `mind/turn.py`, `crates/brainstem/src/machine.rs`, `crates/face` | M | none | E + B |
| 3.11 | **One-breath wake.** When the keyword is at onset and speech continues, replay the buffered audio after it into dictation. Cap keyword decoding about 2 s after onset. | "Hey Pip, what time is it?" in one breath is dropped. | `mind/recognition.py`, `tests/test_voice.py` (fake PCM) | M | none (5.2 supersedes) | E |
| 3.12 | **Bus hardening.** Reserve `voice.*`, `say.stop` and `mind.cancel`. Require a hello before routing. Allow `key.*` only from the face or console. Map SO_PEERCRED to the unit and allow only that organ name. Cap line length with `take()`. Use a bounded channel. Shut down dropped peers, with a 1 s write timeout. Sleep after an accept error. Guard `organ.up` with the peer check. | Small trust and resource gaps in the one process everything depends on. | `crates/brainstem/src/main.rs`, `crates/bus/src/lib.rs` | S | 1.22 hello.ok | B [par] |
| 3.13 | **Face polish leftovers.** A loading look while no brainstem has connected in the first 60 s. A dazed variant of sleep. Animation phases kept in f64, with breathing on real elapsed time. Lid corner radius, curved closing lid, neutral wake pose. A `--seed` flag for reproducible frame dumps. | Polished eyes were requested, and these are the last rough edges. | `crates/face/src/anim.rs`, `eye.frag`, `main.rs` | M | 2.2 | B [par] |
| 3.14 | **Organ sandboxing.** Add NoNewPrivileges, ProtectSystem=strict with ReadWritePaths, RestrictAddressFamilies, DeviceAllow and IPAddressDeny for cortex (check that the kernel has CGROUP_BPF and BPF_SYSCALL). Move the weather fetch to a sandboxed DynamicUser helper, which is the only process with egress in Internet mode. | Every organ runs as unsandboxed root. | `os/package/*/being-*.service`, `mind/weather.py`, `mind/network_enforce.py`, `linux.config` | M | none | C [par] |
| 3.15 | **Forgetting below the file system.** Rewrite `context.json` in place. Require `wal_checkpoint` busy=0 and retry. Run fstrim after a forget. Extend L2 to tokenize the canary and scan the raw disk for its int32 sequence, and to assert the slot file is gone or re-hashed. The complete answer is 6.9. | Old copies survive in freed SSD blocks, and the test cannot see token IDs. | `mind/turn.py`, `mind/memory.py`, `lab/tests/test_l2.py` | M | none | A + D |
| 3.16 | **Try diagnostics privacy.** Pass the journal through `mask_mac` and a serial-number scrubber. Change the menu copy to "Conversations are not kept. Boot diagnostics are saved on this stick." Add a Setup toggle for Try logging. Add a canary test that speech never reaches BEINGLOGS. Allow the `being.lid=open` override on Try boots only, never grant the camera under it, and show a marker. | The Try stick carries identifiers, and the menu text is no longer true. | `mind/usb_logs.py`, `mind/local_menu.py`, `crates/brainstem/src/linux_body.rs` | S | in-flux work landed | A [par] |
| 3.17 | **Maintenance builds made visible.** sshd LogLevel INFO to a persistent `/mind` log. A face glyph while any SSH session is open. A local key press arms the dev window. Enrolment expiry and revoke from F2. Hide "Local network" when lab-access is absent. | Remote root with no presence signal (maintenance builds only). | `mind/lab_access.py`, `being-lab-*.service`, `crates/face`, `mind/setup.py` | M | none | A + B |
| 3.18 | **Reproducible builds.** Add `cortex.hash` and a linux 6.18.64 hash, and set `BR2_DOWNLOAD_FORCE_CHECK_HASHES=y`. The being package removes its installed tree before installing. `lab/build.sh laptop` runs `being-rebuild`. Vendor the Rust crates. Build age with GOAMD64=v2, or record the exception. Vendor the JetBrains Mono fonts (OFL) with a hash instead of copying them from an unrelated project's `node_modules`. Mount factory images by PARTUUID with `fstab=no` (fixes the label collision with an installed mind). | What is tested and what ships can drift apart. | `os/package/cortex`, `os/package/being/being.mk`, `os/board/beingos/post-build.sh`, `lab/build.sh`, being-console font assets | M | none | C [par] |
| 3.19 | **Installer and Setup polish.** Redraw the quiet-hours submenu with console_ui. Show byte progress while files are prepared. Keep hints in two-column menus. Leave the menu after 3-5 idle minutes when no install is running. Use a "Restore complete" heading. "Surprise me" re-rolls until the wake phrase is valid. Write per-preset sample intros. Label the voice "Original voice". Per-personality greetings, first words and good-night lines. When a Try profile is kept, use "So this is my home now" (without claiming a memory). Persist that the first words were said. | The owner asked for a beautiful, polished installer. | `mind/initiative_setup.py`, `mind/installer.py`, `mind/install_apply.py`, `mind/console_ui.py`, `mind/setup.py`, `mind/profile.py` | M | none | A [par] |
| 3.20 | **Archive maintenance.** Delete FTS rows through triggers and rebuild only while asleep. Fold old ledger entries into a rolling hash. Store direct sources separately from weak context links. A correction re-evaluates only the edited row. The dream budget pages only owner rows. | The archive slowly gets slower, and forgetting cascades too widely. | `mind/memory.py`, `mind/dream_store.py`, `mind/turn.py` | M | none | A |

### Phase 4: Speed

**Goal.** First word at 3 s or less after the owner stops speaking (measured), on a warm turn, on the real laptop. No turn cancelled by a timeout.

**Exit criteria.**
- The per-turn timing log shows a median of about 20 or fewer new tokens per warm turn.
- No more than about 100 uncached tokens on the turn after a forget or compaction.
- A model and quant are chosen from laptop bench data.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 4.1 | **Per-turn timing log.** Keep the final SSE `timings` (prompt_n, cache_n, rates), plus time to first clause and parrot retries. Numbers only, no text. One line per turn. | It records latency on every boot of every image. | `mind/cortex.py`, `mind/turn.py` | S | none | A [par] |
| 4.2 | **Senses diet.** Send a first-person senses note only when something is notable or has changed. Drop the clock (time is a local command). Put always-true self facts in the cached identity block. | About 20-29 tokens on every turn (an estimated 0.7-2 s). | `mind/turn.py`, `crates/brainstem/src/body.rs`, `life.rs`, `config/identity.md` | S | none | A |
| 4.3 | **Reuse the previous reply from the cache.** Use a BeingOS chat template (`LLAMA_ARG_CHAT_TEMPLATE_FILE`) whose history assistant turns match the generation prompt. Store the raw think-filtered stream as the prompt-side message and keep cleaned text for the archive. Accept only if prompt_n per warm turn equals the new user block. | Each turn re-reads 22-27 tokens of its own last reply. | `os/package/cortex/`, `mind/turn.py`, `mind/cortex.py` | M | 4.1 | A + C |
| 4.4 | **Warm the whole prompt.** After a scrub, compaction or recovery, warm the full prompt in interruptible 32-token batches while idle. The first request after an erase ends at the identity boundary, so the durable checkpoint sits at about 187 tokens. Never prime shorter than the live cache unless the server restarted. Allow housekeeping during attending gaps and in every memory mode. Keep `_make_room` as a last resort with a small window. When a large cold read is expected, say "Give me a moment." first. | Cold re-reads after forget or compaction are estimated at 30-240 s. | `mind/turn.py`, `mind/cortex.py` | M | 1.15 | A |
| 4.5 | **Cortex flags.** Set `LLAMA_ARG_BATCH=512` and keep ubatch 512. Keep ctx 4096, f16 KV, flash-attn auto, CTX_CHECKPOINTS=6 and reasoning budget 0. Key the saved slot to the pack SHA-256 plus the server build. Measure `cache_n` after a restore (hybrid restore question). | Cancels take up to 2,048 tokens to land. A same-architecture model change would silently reuse a stale cache. | `os/package/cortex/being-cortex.service`, `mind/turn.py` | S | none | C + A |
| 4.6 | **Tighter reply shape.** GBNF: tag, then 1-2 sentences of bounded length ending in . ! or ?, with no newlines, parentheses or markup. max_tokens 48-64. Send top_k 20, top_p 0.8 and presence 1.0-1.5 from `being.json`. Add a boilerplate filter ("as an AI", "language model", "how can I help") that reuses the parrot retry. Measure with `lab/model_audition.py` before shipping. | 6/24 complete replies today. | `mind/speech.py`, `mind/cortex.py`, `config/being.json` | S | none | A [par] |
| 4.7 | **Bench mode on the Try stick (`being.bench=1`).** System facts; per model and setting a private llama-server with production flags (load time, cold prefill at 16-512 tokens, decode 64, the 6-turn script, slot save and restore, cancel-to-release); ASR and TTS real-time factor with the model busy; an optional 30 min soak at turbo, at 2.5 GHz and at 1.8 GHz with RAPL tokens per joule, always restored. Candidate GGUFs go on their own Try partition with a manifest. JSON to BEINGLOGS. | The laptop image has no shell, and model choice needs laptop numbers. | `os/board/beingos/try-grub.cfg`, new `being-bench.service`, `mind/bench.py`, `try-image.sh` | L | 1.18 | A + C |
| 4.8 | **Quant and thread trials.** Compare Q4_K_M with Q4_0, IQ4_NL and Q8_0 of the same source (Q4_K_M has no fast x86 prompt path). Threads 1/2/3/4 for generation and 2/4 for prompts. Poll 0 versus 50. Core pinning on distinct physical cores. mlock on and off. | The cheapest speed wins are probably here. | bench data, `being-cortex.service` | S | 4.7 | C |
| 4.9 | **Model packs carry runtime settings.** A signed "runtime" block: sampler, template kwargs, template file, architecture, checkpoint count, grammar compatibility. The cortex unit reads a generated env file. | Swapping models today needs code edits. | `mind/model_pack.py`, `os/package/cortex`, `mind/cortex.py` | M | 1.6 | A + C |
| 4.10 | **Waking-model auditions on the laptop**, in this order: MiniCPM5-1B, LFM2.5-1.2B (if the owner accepts its licence), Qwen3.5-2B, then LFM3-1.7B. Promote only if a model wins the blind review and keeps the median first word at 3 s or less. A 4B waking model is ruled out (an estimated 15-30 s to first word). Add an F2 "Brain" choice showing measured first-word time, which invalidates the slot. | The default model is the weakest candidate. | `lab/model_audition.py`, model packs, `mind/setup.py` | M | 4.7, 4.9 | A + D |
| 4.11 | **Boot speed.** Precompile the mind's Python (`compileall --invalidation-mode unchecked-hash`). Merge the staged `startup-order` patch after qualification. | Every organ start recompiles about 47 modules on a dual-core CPU. | `os/package/being/being.mk` | S | 1.21 | C [par] |
| 4.12 | **Freeze the ARM-hosted micro-optimisation lane.** No new `out/ivybridge-*` patches without laptop data showing the step is at least 5% of turn time. Boot-order, playback-completion and sphinx-ds are the exceptions and go through task 1.21. | M4 timings do not predict an AVX-only Ivy Bridge. | process | S | none | D |
| 4.13 | **Read-ahead rebuilt** on stable partial transcripts. Keep an exact record of the token sequence. Subscribe to `hear.cancel`. Target 12 or fewer new tokens after the end of speech. | An estimated 1.7-3 s first word, with headroom for a 1-1.2B model. | `mind/turn.py`, `mind/cortex.py` | M | 4.3, 5.2 | A |

### Phase 5: Voice

**Goal.** It sounds and listens like a creature, not a 2000s synthesiser.

**Exit criteria.**
- A chirp follows end of speech within 300 ms.
- The new ASR beats PocketSphinx on a recorded owner test set.
- The TTS real-time factor is 0.5 or less on one thread with the model busy.
- Flite still works as the fallback.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 5.1 | **Chirps and earcons.** Procedural stdlib tones in the voice organ, cached in `/run`: heard, thinking purr after 1.5 s, yes, no, sleepy sigh, charging thanks, timer bell. Each is a family of jittered variants. They obey mute, lid and quiet hours. The brainstem emits `stem.chirp` on state changes. A Setup switch turns them off. | "Sound is half the face." They also cover model latency. | `mind/voice.py`, `crates/brainstem/src/machine.rs` | M | 1.13 | E + B |
| 5.2 | **ASR upgrade.** Package the sherpa-onnx v2.0.2 Linux x64 release with ONNX Runtime 1.28.2 (it already loaded on the AVX-only QEMU guest), called through ctypes. Silero VAD plus streaming Zipformer-20M first, then Moonshine v2 if its real-time factor is 0.5 or less. Do "Hey Pip" spotting in the same runtime and accept a one-breath request. | PocketSphinx mishears, and each mishearing becomes an episode. | new `os/package/sherpa-onnx`, `mind/recognition.py`, `mind/voice.py` | L | 4.7 bench | E |
| 5.3 | **Natural TTS.** One persistent Piper (sherpa-onnx VITS) process on one thread, streaming into a single ALSA stream. Bench tiny-voca and KittenTTS alongside it. Add a light robot timbre. The voice picker plays every option. Keep Flite as the fallback. | The voice undercuts the polished face. | `mind/tts.py`, `mind/voice.py`, `mind/setup.py` | L | 5.2 package | E |
| 5.4 | **Speaker identity.** CAM++ embeddings, with opt-in owner enrolment in Setup. Each `hear.final` is tagged owner, guest or unknown. Only the owner can forget, correct or end guest mode. Unknown voices are not archived. | Today any voice is stored as the owner and can confirm a deletion. | `mind/voice.py`, `mind/turn.py`, `mind/setup.py` | L | 5.2 | E + A |
| 5.5 | **Barge-in.** First, wake-word spotting stays on in non-speaking work states. Then keyword-gated barge-in while speaking. Later, WebRTC AEC3 echo cancellation. | "Interruption during speech" in the adopted plan, and the owner's "stop writing that script" (7.9). | `mind/voice.py`, `crates/brainstem/src/machine.rs` | M | 5.2 | E + B |
| 5.6 | **Ears that react.** A rate-limited `hear.sound` on onset in idle or drowsy gives a small attentive flicker on the face. No transcript is made. | Shows the mic works, and the being feels alive. | `mind/voice.py`, `crates/face/src/anim.rs` | S | none | E + B [par] |

### Phase 6: Memory and dreaming

**Goal.** It dreams when the lid is closed, with a second small model, and the next day it knows something new, with sources. Recall understands paraphrase.

**Exit criteria.**
- The overnight QEMU run, then the laptop soak, produces cited candidate facts with zero invented quotes against the verifier.
- Thermal stays at or under the owner's target.
- Forgetting cascades to derived facts and vectors.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 6.1 | **Dream stage 1 (no model).** Consent from 1.9. An owner window option: "whenever the lid is closed on AC" or "night only". "What did you dream about?" answered from the journal with sources, or honestly ("I didn't dream last night…"). One tentative morning remark. | Dreaming is invisible today, and the model will invent a dream when asked. | `mind/dream_policy.py`, `mind/commands.py`, `mind/turn.py` | S | 1.9, 1.10 | A |
| 6.2 | **Dream stage 2 (the subconscious).** `being-dream-cortex.service` runs a second llama-server on 127.0.0.1:8081 with Qwen3-1.7B Q4_0 (about 1.1 GB, thinking off, temperature 0, ctx 2048, Nice 10), started only while eligible and stopped at a chunk boundary on wake. Pipeline: a deterministic pre-pass, then extraction with a per-call extractive grammar (quotes as sentence ids), then Python checks (verbatim quote, episode id, speaker), then a `candidate_facts` table with source links and a derivation table for cascading forgets. Promote a candidate on recurrence or owner confirmation. A token-budget queue carries work over to the next night. The 65 °C stop rule is replaced by bodycare's continuous cap (stop only on hard limits: package over 92 °C, battery over 40 °C, lid open, AC lost). Add MiniCheck-RoBERTa-L as phase 2b once its GGUF conversion is proven. Qwen3-4B stays an optional arm only. | The owner's description: a second LLM making sense of its data while closed. | new `mind/dream_model.py`, `mind/consolidation.py`, `mind/dream_store.py`, `mind/memory.py`, `os/package/cortex/being-dream-cortex.service` | L | 1.6, 2.8, 4.9 | A + C |
| 6.3 | **Semantic recall.** Phase 0: static blink-8M embeddings in pure-stdlib Python, in an `episode_vec` table with cascade delete, fused with BM25 by RRF (k=60), with a recency term and a cosine floor ("I don't recall" instead of a guess). Phase 1: granite-embedding-small-r2 in its own `llama-server --embedding`. No sqlite-vec (it cannot load, and it builds with AVX2). | Paraphrase recall, and fewer, better notes. | `mind/memory.py`, new `mind/embed.py` | M | 3.1 | A |
| 6.4 | **A small core profile.** During dreaming, write a short byte-stable `core.md` (owner's name, top confirmed facts, with fact ids), removed transitively on forget. Re-warm the prompt on wake. | `core.md` is read but nothing writes it. | `mind/consolidation.py`, `mind/__main__.py` | M | 6.2 | A |
| 6.5 | **Continuity.** A source-linked "unfinished thread" record that survives any gap. "You're back. It's been three days." Follow-ups on dated statements ("How did the dentist go?"). Milestones from the life record. | "The same Pip on every wake." | `mind/turn.py`, `mind/memory.py`, `mind/profile.py` | M | 6.1 | A |
| 6.6 | **Bring Pip home.** On the Try settings screen, an opt-in "Keep what you and Pip talked about" carries the compacted Try archive, life record and settings into the install through the restore path. | The being met in Try is otherwise reborn at install. | `mind/install_apply.py`, `mind/local_menu.py`, `mind/backup.py` | M | 1.4 | A |
| 6.7 | **Encrypted memory.** Phase 1: fscrypt with a per-boot key on `/mind/cortex` and the context files, so forgetting the key wipes them. Phase 2: optional LUKS2 or fscrypt for all of `/mind`, unlocked by an owner PIN on the face, with an honest "unattended boot" choice. | The only real erase on an SSD, and theft protection. | `linux.config`, `being-installed-generator`, `mind/install_apply.py`, `crates/face` | L | 1.7 | C + A |

### Phase 7: Character and polish

**Goal.** It feels alive and like itself: sound, reflexes, mood, recognition, rhythm.

**Exit criteria.**
- The owner's own judgement on a recorded week.
- The persona evaluation (8.3) does not regress.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 7.1 | **Birth ceremony.** `life.first` reaches the face: a slower flickering first power-on, two slow looks and a curious tilt, one soft chirp. First words are spoken on the first touch, key, wake or presence. | The moment he will film. | `crates/brainstem`, `crates/face/src/anim.rs`, `mind/turn.py` | S | 1.16, 5.1 | B + A |
| 7.2 | **Mood engine.** The `body.drives` already published drive the face (lid heaviness, gaze tempo, reunion eagerness) and the mind (greeting variants, reply temperature). A slow "contentment" value persists in `/mind`. | A persistent mood makes the same individual every day. | `crates/face`, `mind/turn.py`, `crates/brainstem/src/body.rs` | M | none | B + A [par] |
| 7.3 | **A body that feels.** Accelerometer bump or tilt gives a surprise look and a "whoa" chirp. Light changes give a squint or slow blink, and lights out a yawn. Auto-brightness from the light sensor (calibrated) with hysteresis. Keyboard glow breathes while asleep. Fan and heat give a "warm" cue and shorter replies. Plug and unplug give a content gesture or a sigh. Repeated trackpad strokes give a content squint. | "It is the laptop." | `crates/brainstem/src/linux_body.rs`, `body.rs`, `crates/face` | M | 2.8 | B |
| 7.4 | **Captions and health overlay** behind an F-key: last heard text, reply text, organ health. Off by default and never logged. | Hardware bring-up with no shell. | `crates/face`, `os/package/being/being-console.c` font assets | M | none | B [par] |
| 7.5 | **Privacy state on the face.** Ears covered when muted, a pupil glint while the camera is on, a soft ring in guest mode, a dot while the network is up, a marker during an SSH session. | The owner should see when the ears, eyes and network are active. | `crates/face` | M | 1.9 | B |
| 7.6 | **Persona LoRA** on the winning waking model, trained on synthetic dialogue only (Qwen3.8-27B on the Mac Studio). Never trained on owner data. Merge, requantise, A/B test. | The largest quality lever at 1B, and free at run time. | `lab/` training scripts, model pack | L | 4.10, 8.3 | D |
| 7.7 | **Owner recognition** (opt-in). YuNet plus SFace in OpenCV built without AVX2. Enrol 5-10 captures. Majority vote with an "unsure" band. Embeddings encrypted and deletable, used for greetings only and never to authenticate. | Being greeted by name when he sits down. | `mind/eyes.py`, `mind/setup.py` | L | 2.6 | A |
| 7.8 | **The workshop** (the owner's latest request): voice-driven simple apps such as "Hey Pip, open the terminal / minimise it / close it", "write a script that …", "stop writing that for a second". Design: reuse being-console's pty terminal as a pane or hand-over with the face. A new brainstem "working" state with wake spotting on and no think timeout. Generation streams as visible typing, with cancel and continue via an assistant prefix. Commands run as an unprivileged sandboxed user (systemd-run, DynamicUser, ProtectSystem=strict, writable `/mind/workshop`, PrivateNetwork unless Internet mode), shown first and run only after a spoken or keyboard yes, with a kill switch. Owner-only once 5.4 exists. "Install an application" is scoped to fetching single-file tools into the workshop in Internet mode, or bundled toy apps, because the root is read-only. A small coder model is optional. | Owner request, 23 Nov. | `os/package/being/being-console.c`, `crates/face`, `crates/brainstem/src/machine.rs`, `mind/commands.py`, new `mind/workshop.py` | L | 5.5, 5.4, 0.9 scope decision | A + B + E |
| 7.9 | **A capability policy.** One explicit file listing what the being may do (camera, network, workshop, announcements, dreams), shown in Care and read by every organ that acts. | "What it is allowed to do" in the identity layer he adopted; needed by 7.8. | `mind/policy.py`, `config/` | S | 1.9 | A |
| 7.10 | **Studio-assisted mode**, only if the owner chooses it in 0.9. An explicit opt-in remote cortex host, shown on the face. Otherwise reject non-loopback hosts on the appliance. | An option from an alternative plan; the owner's call. | `mind/cortex.py`, `mind/setup.py` | S | 0.9 | A |
| 7.11 | **Media gallery.** `lab/media.sh` renders the expression sheet, a 30 s tour and the installer screens into `docs/media/`, rebuilt when face or console files change. | VM preview and polish requests. Today's clips sit in a temporary folder. | `lab/media.sh`, `README.md` | S | none | D [par] |

### Phase 8: Soak tests and release

**Goal.** A being that runs for a week on the Mac without help, can be updated and backed up by the owner, and is built quickly and reproducibly.

**Exit criteria.**
- Soaks of 24 h, 48 h and one week pass on the Mac with no restarts of required organs, flat memory, bounded temperature, and the battery held at the cap.
- An update and a rollback are drilled on the hardware.
- A backup transplant is drilled.

| # | Task | Why | Files | Effort | Depends | Lane |
|---|---|---|---|---|---|---|
| 8.1 | **Three test tiers behind `scripts/check`.** `fast` (host, under 2 min, lists skips). `boot-gate` (laptop smoke plus l0, l1, l5, try, local_screen, install_apply, setup and one real-model l2 turn, about 40 min, on images sharing one source-id). `full` (controller plus installed recovery and migration, before any SSD install). Independent suites run in 3-4 parallel QEMU lanes. | The controller runs in series (about 80 min), and nothing separates must-pass checks from the long tail. | `scripts/check`, `lab/candidate_tests.py` | M | 1.1, 1.22 | D |
| 8.2 | **Privacy regression suite.** One canary checked after a forget across mind.db, FTS, WAL, context.json, slot token ids, reminders, BEINGLOGS and a restored backup. Mute and guest mode survive organ restarts. The canary never appears in the journal. | Privacy guarantees regress silently. | `lab/tests/test_privacy.py` | M | 3.15 | D |
| 8.3 | **Persona evaluation in the gate.** Multi-turn probes, invention rate against empty memory, tag validity, boilerplate rate, per-personality consistency, scored by a fixed local judge on the Mac Studio. Required when identity.md, profile.py or reply code change. | Persona changes were never checked against the real model. | `lab/model_audition.py`, `lab/persona_eval.py` | M | 4.6 | D |
| 8.4 | **Soaks on the Mac.** 24 h, 48 h and one week, each with a lid-closed dream night, unplug cycles and a power cut. Log RAPL, temperatures, fan, battery level and temperature, organ restarts, memory and first-word times. | The owner asked for a persistent being, not a demo. | soak script, BEINGLOGS or `/mind/log` | L | Phases 1-2 | D + owner |
| 8.5 | **Update channel.** Laptop bundles signed with the release key, applied from a USB stick (1.3). Then optionally over Ethernet in Internet mode. Drill the rollback on hardware. A key-rotation bundle procedure. | "Address all identified issues" without erasing the being. | `lab/updates.sh`, `docs/HARDWARE.md` | M | 1.2, 1.3 | C |
| 8.6 | **Automatic backups.** After a dream night on AC, write an age snapshot plus deletion proof to a stick labelled `being-backup` left plugged in. Keep the last N. "Last backup" is shown in Care and answered by voice. Transplant drill in QEMU, then on the Mac. | SSD death is otherwise the end of the being. | `mind/backup.py`, `mind/turn.py` | M | 1.4 | A |
| 8.7 | **Faster builds.** After the current build: Colima at about 14 CPUs and 48-64 GB. ccache with `--max-size=40G`. Set `BR2_JLEVEL` to 3-4 with `-j6`, or cap the link jobs. Check `df` before full builds. A Buildroot SDK external toolchain for both defconfigs. Generate the laptop defconfig from the lab one plus a fragment, with an allow-list diff test. Per-package directories in every lane. Reserve the 22-suite controller for internal-install candidates. | "Why is build time so long? Can it be sped up?" | `lab/build.sh`, `os/configs/*`, `lab/candidate-build.sh` | M | current build done | C |
| 8.8 | **Documents restructured.** `docs/DESIGN.md` (about 25 KB of intent, each commitment tagged built, changed or not built). `docs/DECISIONS.md` (each changed commitment with owner accept or reject). A generated `docs/STATUS.md` (evidence level per milestone, image hash, blockers, next gate) that other documents link to instead of repeating. The lab notebook, the Jev/OpenJev/Laya/Kev tooling and dated results move verbatim to `docs/archive/` and a tracked `evidence/` folder. The hosted decision client and the decision-model rows are removed. The laptop image excludes lab and telemetry modules. | The design is no longer a usable statement of intent, and status has to be re-derived each time. | `DESIGN-AND-PLAN.md`, `docs/`, `lab/decision_*`, `os/package/being/being.mk` | M | 1.23 | D [par] |

**Task count: 110** (Phase 0: 9, Phase 1: 23, Phase 2: 13, Phase 3: 20, Phase 4: 13, Phase 5: 6, Phase 6: 7, Phase 7: 11, Phase 8: 8). Two of the Phase 0 tasks, 0.5 and 0.7, are the owner's own steps.

### Rough timeline

All of this is an estimate.

- **First Try boot:** when 0.2-0.4 are done, so hours.
- **Second Try image (the [Try-2] tasks):** about 3-4 work periods, with 4-5 lanes running in parallel.
- **Ready to install on the SSD (all of Phase 1, plus Phase 2 tasks 2.1-2.3 and 2.7):** about 6-9 work periods, then at least a 24 h soak. This is slightly later than the earlier readiness estimate (5-9 work periods plus 1-2 weeks of soak), because the update, backup and release-key work is now required first.
- **Phases 4-7:** weeks, guided by laptop measurements.

---

## 4. The battery: the Linux solution and its fallbacks

The owner is right: the battery problem has Linux solutions. The kernel already has most of the pieces (ACPI battery and SBS, applesmc, coretemp, intel_pstate, RAPL). What is missing is `CONFIG_X86_MSR`, a small applesmc patch, and a service that uses them. The current image only reads sysfs and writes no power state, so the SMC's own fan curve and the CPU's own 105 °C protection stay in charge. The first boot is safe.

There are two modes. The owner picks one (decision sheet 0.9).

### Mode A (recommended): battery in, charge capped

1. **Today, under macOS, with no image change (task 0.7).** If macOS is still on the SSD: `bclm read`, then `sudo bclm write 77` (shows as about 80%, because Intel Macs overshoot by about 3%). Charge it and confirm "Not Charging" at about 80%. On pre-T2 Intel Macs the SMC stores BCLM and keeps it across operating systems until an SMC reset. Evidence: a 2012 MacBook Air stopped at 93% with 89 set, and applesmc-next PR #15 wrote BCLM on a MacBookPro9,2 (but did not show that charging stopped). Do not run `bclm persist`, which is macOS-only. Record the cycle count and condition.
2. **First Try boot (task 1.18).** Read-only inventory of the SMC keys (BCLM, BFCL, CH0B, CH0C) and read BCLM back. Record cycle_count, charge_full versus charge_full_design, and the TB0T-TB2T battery temperatures. Check whether charging stops at the cap under Linux.
3. **Linux charge cap (task 2.7).** An in-tree patch, `os/patches/linux/0001-hwmon-applesmc-battery-charge-limit.patch`, adds `/sys/devices/platform/applesmc.768/battery_charge_limit` (BCLM, 20-100, 100 means no cap) and `battery_charge_led` (BFCL), created only if the key exists. `BR2_GLOBAL_PATCH_DIR` already picks it up. **Not applesmc-next:** applesmc is built in, so its replacement module cannot load, and it has open crash and hook bugs on newer kernels.
4. **Keep it applied (task 2.8, being-bodycare).** Re-assert the owner's limit (default 80, choice of 60-80) at boot and every 10 minutes, because an SMC reset clears it. Verify it holds: on AC, at limit+5 and still "Charging" for 2 hours, log it and have Pip say "my charge limit isn't being honoured".
5. **Fallback A2.** If BCLM is absent or ignored and CH0B/CH0C exist: software hysteresis that inhibits charging at 80% and resumes at 75%, re-applied each boot (the keys are volatile).
6. **Fallback A3.** A new battery (A1322-type, user-replaceable on the 9,2 with screws), kept cool, with health monitored.

### Mode B: battery removed (last resort, opt-in)

Without a battery the firmware asserts BD PROCHOT and holds the CPU at about 1.2 GHz (measured on a 9,2: 1197 MHz on every core). The fix:
1. `CONFIG_X86_MSR=m` (task 1.8), with `msr.allow_writes=on` only in this mode.
2. At boot and on resume, only when no battery is present and AC is online: read MSR 0x1FC, clear bit 0, write it back (stdlib Python `os.pwrite` on `/dev/cpu/N/msr`, or bodycare). This restored 2.9-3.1 GHz in measurements.
3. A mandatory power cap so the 60 W MagSafe adapter is never exceeded (55 W was measured at full load): `no_turbo=1` and `max_perf_pct` of 85 or less, plus RAPL PL1 at about 25 W if it is not locked by firmware.
4. Power on automatically after power returns: `setpci -s 0:1f.0 0xa4.b=0`, re-applied each boot (check on the hardware).
5. Pip says why it is slower or faster, and warns that a power cut now means instant power-off. The settings are undone when a battery appears.

### Both modes: the being looks after its battery

These are tasks 1.19 and 2.8.
- **Pleas and shutdown.** Use time to empty (energy_now/power_now) first and percentage second:
  - at 15% or under 30 min: "I'm running low. Could you plug me in?", allowed in quiet hours;
  - under 10 min: a second plea;
  - at 5%, under 5 min, or below a voltage floor: one goodbye line, save the context and slot, then `systemctl poweroff`.
  Calibrate the thresholds in the first hardware test, because worn cells cut out at a reported 10-30%.
- **Plug-in.** A content gesture and "Thanks, that's better" after a warning.
- **Lid closed on battery.** Shut down after 10 minutes (configurable). S3 suspend comes later, once proven on the Mac (it needs the LID0 wakeup toggle).
- **Heat.** Bodycare raises the fan floor (`fan1_min` only, never manual) from the smoothed package temperature, while not listening. CPU caps depend on the being's state. Battery temperature over 40 °C pauses dreams; over 45 °C caps the CPU.
- **Honesty.** Pip can answer "How's your battery?" truthfully: "My battery holds 62% of its design charge; I'm keeping it at 80%." Once a month it reminds him if health is under 80% or the battery ever ran over 45 °C ("If my trackpad gets hard to click, it may be swelling").
- **Power savings.** Task 2.9: USB autosuspend for the camera and Bluetooth, rfkill, keyboard glow off while asleep, no turbo while asleep. Targets: 9 W or less awake and idle, 6 W or less asleep with the lid closed (estimates; measure through battery power_now and RAPL).
- **Physical care (owner).** Clean the fan and heatsink, repaste, inspect the battery for swelling, and run on a hard surface.

---

## 5. Traceability

### 5.1 Owner requests

| # | Request (owner's words, abridged) | Status now | Plan tasks |
|---|---|---|---|
| 1 | A stripped-down dedicated OS; the hardware is its brain and body | Done in QEMU; the kernel is not yet trimmed | 0.2, 2.11 |
| 2 | Target: 13-inch MacBookPro9,2, 16 GB, 256 GB SATA, 2.5 GHz i5 | Targeted; CPU still listed as unknown | 1.23, 1.18 |
| 3 | Boots purely into the AI; it "is" the laptop; digital life | Done in QEMU; unverified on the Mac | 0.2-0.5, 7.1 |
| 4 | A simple EVE-like face, glowing cyan eyes, the screen is its face | Done in host and QEMU; not yet on the HD 4000 | 2.1, 2.2, 3.13 |
| 5 | Talks and listens through the mic and speakers | Partial (robotic voice, no mixer control, no chirps) | 1.13, 1.16, 1.17, 2.4, 5.1-5.3 |
| 6 | Real time, or as close as possible | Unmeasured | 0.6, 1.15, 4.1-4.13 |
| 7 | The best tiny LLM it can feasibly run | Partial (the weakest candidate ships) | 4.7-4.10, 7.6 |
| 8 | All the RAM used for the AI | Drifted (about 14 GB idle) | 6.2, 6.3, 4.8 (mlock), 4.10 |
| 9 | Storage as a persistent memory database | Done; no backup or update on the laptop | 1.3, 1.4, 1.6, 8.6 |
| 10 | It learns | Partial ("remember that" only) | 3.3, 6.2-6.4 |
| 11 | Remember / what do you remember / forget / that's wrong | Done, with defects | 1.11, 3.1-3.3, 3.7 |
| 12 | Immediate mute; inspect and delete memories | Mute done (lost on restart); no on-screen inspector | 1.14, 1.16, 1.9, 3.2 |
| 13 | Visible listening, thinking and speaking states | Done | 2.1 (verify), 5.1 |
| 14 | Hardware awareness: battery, power, temperature, lid, mic | Partial (no plea, no light, accelerometer, fan or glow) | 1.19, 7.3 |
| 15 | The camera as eyes; "take a look at this" | Partial (unreachable consent; look not merged) | 1.9, 1.21, 2.6, 7.7 |
| 16 | Identity and personality | Done; not yet with the real model on the Mac | 3.19, 8.3, 7.6 |
| 17 | Proactive behaviour | Partial (greetings need the camera; reminders miss sleep) | 1.9, 3.5, 6.5 |
| 18 | The same Pip on every wake | Partial (24 h resume only) | 6.5, 6.6, 1.12 |
| 19 | It dreams when closed, using a second LLM | Missing (rule-based picker, hidden consent, night only) | 6.1, 6.2, 2.8 |
| 20 | Research tiny models and dreaming; is a 4B too heavy? | Researched (answer: yes, 1.7B); not yet in the design | 1.23, 6.2, 4.10 |
| 21 | Is the sentience description accurate? | Not addressed in the product | 3.9, 1.23 |
| 22 | Local, offline baseline | Done | 3.14 (keep it so) |
| 23 | Wi-Fi and Internet | Unusable on the laptop | 1.9, 2.10 |
| 24 | Default skills | Done offline; weather unreachable | 2.10, 3.14 |
| 25 | Installer with options, its own name, a choice of voice | Done (four Flite voices) | 5.3 |
| 26 | A beautiful, polished installer | Done in QEMU; leftovers remain | 3.19, 1.5 |
| 27 | Polished eye animation and personality | Done in host; leftovers remain | 3.13, 7.1, 7.2 |
| 28 | Build and test in a VM; a VM preview | Done | 7.11, 8.1 |
| 29 | Review the parallel workstreams under one authority; merge all changes | Partial (mediums open, patches unmerged, nothing committed) | 0.1, 1.21, this plan |
| 30 | The battery issue is resolvable on Linux | Missing in the OS | Section 4; 0.7, 1.8, 1.18, 1.19, 2.7-2.9 |
| 31 | Faster builds and progress (asked three times) | Partial | 4.12, 8.1, 8.7, 8.8 |
| 32 | Run as much of the work in parallel as possible | Done (process) | Lanes A-E, [par] marks |
| 33 | Test on the real hardware soon without disturbing the current build | In progress | Phase 0; principle 1 |
| 34 | Optional Mac Studio assisted mode (alternative plan) | Code path exists, not exposed | 0.9, 7.10 |
| 35 | "Analyse and review EVERYTHING … a comprehensive improvement plan" (23 Nov) | This document | all |
| 36 | Voice-commanded apps: open, minimise or close the terminal; write a script; "stop writing that" (latest request) | Not started | 7.8, 7.9, 5.5 |
| 37 | Natural interruption during speech (adopted plan) | Missing (only Esc; click patch unmerged) | 5.5, 1.21 |
| 38 | The two screenshots of 23 Nov (08:19 and 09:11) | Never reviewed (not available to the review) | 0.9: get them from the owner again |
| 39 | "How long until it's ready?" / "Is it ready for the USB?" | Answered here | Section 3, rough timeline |
| 40 | Identity layer: what it is allowed to do; its interests | Not built | 7.9, 7.2 |
| 41 | Faster first reply, "shortest comfortable delay" (adopted plan) | Unmeasured | Phase 4, 5.1 |

### 5.2 Every still-open known issue

Sources: the four review areas (vision, mind-core, mind-periphery, rust-organs), os-image, tests-and-docs, battery-thermal-power, performance, security, experience and the issue ledger. Duplicates found by several areas are grouped into one row. Already fixed or refuted items are not planned: the time-zone plumbing, the believed clock, forget confirmation, the round-1/2/3 forget and clock fixes, the restore mount, the anim test failures, the console compile, the laptop_smoke classification and the round-3 regression tests.

| Issue (severity as reconciled) | Source | Task |
|---|---|---|
| Laptop USB image not sealed or qualified (high, blocks first boot) | tests-docs, os-image, ledger | 0.2, 1.1 |
| Boot visibility and no-brainstem fallbacks in flux; F9 bus-only; dark after closed-lid plus failed brainstem (high, blocks first boot) | ledger, rust | 0.3, 2.2 |
| No first-boot procedure; docs point at superseded images (medium, blocks a useful boot) | tests-docs | 0.4 |
| Guest evidence older than the source; stale default images; skip counts as OK; no source-id | tests-docs | 1.22, 1.1 |
| Reboot-loop and no-brainstem logic untested; laptop smoke checks weak | tests-docs | 1.22 |
| Hub join race (flaky test, real race) | tests-docs | 1.22 |
| No owner update path; maintenance update would install the lab rootfs (high) | vision, os-image, security | 1.2, 1.3 |
| Unbacked lab key is the only trust root (high) | os-image, security, ledger | 0.8, 1.2 |
| No on-device backup; blind 64-hex checkpoint; restore overclaims; create-only backup restores nothing; key beside the backup; "authenticate" is just decryption (high) | periphery, security, ledger | 1.4 |
| Camera, dreams, memory mode, Wi-Fi, journal and clock unreachable on the laptop (high) | vision, periphery, security, experience | 1.9 |
| No time zone; UTC clock, quiet hours, night and dream window (high) | periphery, experience, rust, ledger | 1.10 |
| Brainstem UTC until the first mind.clock; offset not persisted (low) | ledger, rust | 1.10 (persisting it is not needed; see section 2) |
| `_last_telling` makes "forget that" delete an unrelated fact, even across guest mode (high) | mind-core | 1.11 |
| "That's wrong, …" rewrites the owner's question; "it's Jim" becomes the fact (high) | mind-core, experience, ledger | 1.11 |
| "Forget it" idiom deletes; forget not tied to the speaker | tests-docs, ledger | 1.11 |
| Narrow `_YES`; other replies go to an uninformed model (medium) | mind-core | 1.11 |
| Damaged policy, profile or initiative gives a silent rebirth; missing life.json or mind.db gives a new being; backup create uses load_policy (medium) | mind-core, periphery, ledger | 1.12, 1.4 |
| Guest mode in RAM only; resumes owner recording after a restart (medium) | ledger, security | 1.12 |
| Any audio error kills the voice organ; brainstem stuck thinking or speaking; timeouts stop only half the turn (high) | periphery, rust, ledger | 1.13 |
| Mute lost on brainstem restart; mute mid-reply leaves the being attending while deaf (medium) | rust, security, ledger | 1.14 |
| Every hardware boot goes idle after 1.5 s (lid_wake) (medium) | rust, ledger | 1.14 |
| Brainstem restart looks like a nap; false life facts; awake N on wall clock (low) | rust, ledger | 1.14 |
| Life record: missing life.json gives a rebirth (low) | rust | 1.12 |
| 120 s think timeout with no progress signal (high) | rust, mind-core | 1.15 |
| "Say hello" ignored; no attention key or touch; birth wake (high) | experience, periphery | 1.16 |
| F2, F9 and F10 are media keys; mute key unmapped; volume and brightness keys unmapped (medium) | periphery, rust, os, security | 1.8, 1.16, 2.5 |
| No volume control; levels reset each boot; card chosen by index (medium; premise corrected: alsactl does run) | vision, periphery, ledger | 1.17, 2.4 |
| Hardware report misses applesmc on 6.18 and has a wrong fixture; no SMC keys, cpufreq, RAPL, throttle, mixer, DPMS or keycodes (medium) | battery, performance, rust | 1.18 |
| No low-battery plea, critical shutdown or lid-closed-on-battery policy (high) | periphery, battery, ledger, experience | 1.19, section 4 |
| No charge cap; the kernel layout blocks applesmc-next (high) | battery, os, ledger | 0.7, 2.7, 2.8 |
| No `/dev/cpu/*/msr`; PROCHOT pins the CPU at 1.2 GHz without a battery (medium) | os, performance | 1.8, section 4 Mode B |
| No thermal or fan management; 65 °C stop rule (medium) | battery, vision | 2.8, 6.2 |
| Setup restart fails on a missing lab unit; network reconcile fails on missing units and OBSERVE (medium) | periphery, security, ledger | 1.20 |
| Ethernet only through SSH pairing (medium) | vision, periphery, ledger | 1.9 |
| No b43 firmware or regdb; IPv6 privacy, hostname, hidden SSIDs, Wi-Fi restart loop, weather IPv6 timeout (low) | os, vision, ledger | 2.10 |
| Staged patches never merged: look, click, brightness, storage, sphinx-ds, playback, boot-order, crocus-softpipe, docs and test patches (medium) | vision, critique | 1.21 |
| Nothing committed; out/ holds 23 look-alike images (medium) | vision, tests-docs, ledger | 0.1 |
| Installer cannot detect macOS or a being; no pre-wipe checklist; bare error after erase; stale data left; no startup-disk hint (medium) | periphery, ledger | 1.5, 2.12 |
| Models partition cannot hold the dream model (medium) | os | 1.6 |
| oops=panic reboot loop on installed boots (medium) | os | 1.7 |
| No persistent logs after install (medium) | os | 1.7 |
| Damaged ESP or models gives emergency mode (low/medium) | os, ledger | 1.7 |
| deny() masks rollback and leaves /var read-only (low/medium) | os, ledger | 1.7 |
| Corrupt grubenv; updates refused on a degraded slot (medium) | os, ledger | 1.7 |
| Installed boots print status text on the panel (low) | os | 1.8 |
| Factory images mount by shared labels and PARTUUIDs (medium) | os, ledger | 3.18 |
| Recall injects unrelated history; possessives never match (high) | mind-core, performance | 3.1 |
| Date words suppress fact recall (low) | ledger | 3.1 |
| Command grammar swallows conversation ("Do you remember me?", "Remember when", "What is this?", "What did we talk about?") (medium) | mind-core, experience, ledger | 3.2 |
| Robotic memory speech; no read-back; no pronoun reflection (medium) | experience | 3.2 |
| Facts never superseded; BM25 before recency; reaffirmed facts expire (medium) | mind-core, ledger | 3.3 |
| Time, date and age trust a known-wrong clock; no way to set it; reminders ignore the believed clock (medium) | mind-core, periphery | 3.4, 1.9 |
| Dates shown in UTC (low) | mind-core | 3.4 |
| Reminders silent while asleep; late ones worded as on time; ticket-style wording; ring once (medium) | periphery, experience, ledger | 3.5 |
| Expiry wipes the conversation daily; slow and synchronous; sub-day clock step; owner not told (low/medium) | mind-core, ledger | 3.6 |
| Forgetting residuals (explicit-only, prefilter, day corrections, reminders, over-linking, counts, tombstones, older backups) (low) | mind-core, security, ledger | 3.7, 3.20 |
| Turn endings: DB error hangs the voice; unpaired messages; context overflow (low) | mind-core, ledger | 3.8 |
| Command exchanges invisible to the model (low) | mind-core | 3.8 |
| Parrot guard suppresses normal answers (low) | mind-core | 3.8 |
| No honest answer to "are you alive?"; false "can't see"; fixed check-in claims; public description inaccurate (medium) | vision, mind-core, experience, ledger | 3.9, 1.23 |
| Model confuses its senses with the owner ("You are awake") (medium) | experience | 4.2, 3.9 |
| Follow-up window stores room speech as the owner's; mic open while asleep, unsignalled (medium) | periphery, security, ledger | 3.10 |
| One-breath wake dropped; keyword decoding uncapped (medium) | ledger, experience | 3.11, 5.2 |
| Bus hardening: forgeable topics, hello races, unbounded buffers, accept spin, self-declared identities (low) | rust, security, ledger | 3.12 |
| Face: f32 time, dt clamp, dazed first face, dazed sleep, polish leftovers, unmeasured fps (low) | rust, ledger | 3.13, 2.1 |
| F2 systemctl in the render loop; no frozen-face watchdog (low) | rust, ledger | 2.3 |
| Panel blanking a no-op on LVDS; blanking policy (medium) | rust, battery | 2.2 |
| Backlight writes still on the hub thread (low) | critique | 2.2 |
| Linux-only Rust code never unit-tested (low) | rust | 2.13 |
| Organs run as unsandboxed root; open egress in Internet mode (low) | periphery, security | 3.14 |
| Memory unencrypted; GRUB editable; no firmware password advice (low) | security | 0.7, 6.7 |
| Forgotten text survives in freed SSD blocks; raw-disk test blind to the KV cache (medium) | security, tests-docs, ledger | 3.15, 8.2 |
| Try journal unmasked; "Nothing is kept" wording; lid override opens the ears (low) | security, periphery | 3.16 |
| LAN maintenance root invisible and renewable (low) | security, ledger | 3.17 |
| Unpinned downloads; stale target files; prebuilt age; font copied from an unrelated project (low) | os, security, ledger, critique | 3.18 |
| Installer and Setup polish leftovers; menu with no idle timeout; Surprise name mismatch; Pip-only lines; Try being reborn at install; restore and staging limits (low) | periphery, experience, ledger | 3.19, 6.6, 1.4 |
| Migration test runner mounts the target read-write (low) | ledger | 1.22 |
| Archive maintenance cost; provenance over-linking; dream rewinds (low) | ledger | 3.20 |
| Warm-up of 256 tokens; cold reads after forget, compaction or recovery; checkpoint at token 28 (high/medium) | mind-core, performance, experience, ledger | 4.4 |
| Privacy modes re-read the whole context every cycle (medium) | performance | 4.4 |
| Senses tokens on every turn (medium) | performance | 4.2 |
| Template drift re-reads the last reply (medium) | performance | 4.3 |
| 2048-token batch delays cancels (medium) | performance | 4.5 |
| Slot not keyed to weights or build (medium) | performance | 4.5 |
| Few complete replies; open grammar; no boilerplate filter; bare sampler (high as a vision priority) | experience | 4.6, 4.10, 7.6 |
| No way to measure speed or throttling on the laptop; brain choice unmeasurable; 16 GB idle (medium) | vision, performance | 0.6, 4.1, 4.7, 4.8 |
| Read-ahead off and unworkable (low) | ledger, performance | 4.13 |
| Laptop keeps llvmpipe (low) | ledger | 2.1 |
| Robotic Flite voice; no chirps (medium) | vision, experience | 5.1, 5.3 |
| No speaker ID (medium, privacy) | security, periphery | 5.4 |
| Dreaming does not match the owner's description; dreams invisible; the model invents dreams (medium) | vision, experience | 6.1, 6.2 |
| Battery life mute; body drives unused (medium) | experience | 1.19, 7.2 |
| Most of the promised body unwired (accelerometer, glow, light) (medium) | vision, rust, ledger | 7.3 |
| Silent or jargon failures ("language model isn't answering", "conversation cache") (medium) | experience | 1.15, 3.8, 7.4 |
| Core commitments unbuilt (speech stack, realtime scheduling) (medium) | ledger | 5.2, 5.3, 2.1 |
| Docs lag (USB-READINESS, DESIGN §7/§8, CPU, README, LAB, PROTOCOL); design is a lab notebook; status scattered (medium/low) | vision, tests-docs, ledger | 1.23, 8.8 |
| Concluded experiments and lab tooling ride along in the image (low) | vision | 8.8 |
| Host suite skips the backup path and the real model; skips reported as passing (low) | tests-docs | 1.22, 8.1 |
| Build oversubscription; ccache size; full disk; slow builds (low) | os | 8.7 |
| Laptop defconfig maintained by hand (low) | os | 8.7 |
| Voice privacy tests stub out capture (low) | ledger | 3.11 |
| Scope items awaiting owner decisions (Studio cortex, telemetry in the image, Jev) (low) | ledger | 0.9, 7.10, 8.8 |
| Overwriting the Lexar's macOS installer (low, owner decision) | ledger | 0.7, 0.9 |
| No SSD health monitoring (critique) | critique | 1.18 |
| No power-on after AC restore in battery-out mode (critique) | critique | section 4 Mode B |

---

## 6. Deliberately not in the plan

- **Changing the frozen first Try image.** The owner asked for the first hardware test not to be disturbed. Every fix waits for task 0.1.
- **applesmc-next as a DKMS or out-of-tree module.** It cannot load against a built-in applesmc, and it has open crash bugs. The in-tree patch replaces it.
- **mbpfan, thermald or TLP.** mbpfan's manual fan mode is unsafe if it crashes. thermald adds nothing over a small controller that knows the being's state. Most TLP settings are already defaults here. Bodycare plus a short list of measured settings covers it.
- **A 4B waking model.** Estimated at 15-30 s to first word on this CPU. A 4B dream model stays an optional arm only.
- **PostgreSQL.** SQLite is enough on one device and keeps forgetting simple.
- **sqlite-vec.** Buildroot's Python cannot load extensions, and the build adds AVX2.
- **Training on the owner's conversations.** Weights cannot honour forgetting. Only synthetic persona data is used.
- **New ARM-hosted micro-optimisations** of llama.cpp without laptop data (task 4.12).
- **Reverting Wi-Fi, skills or the installer as "drift".** He asked for them. They stay opt-in, and offline remains the default.
- **The Jev/OpenJev decision model.** It ended negative (about 7 GiB, and it lost a correct recall). It is archived, not continued.
- **A root shell or SSH on the shipping laptop image.** Owner controls go into the on-screen Care page instead. Maintenance stays a separate, visible opt-in build.
- **Installing to the internal SSD before Phase 1 is complete,** even on the maintenance variant (principle 3).
- **A general package manager or desktop for the workshop feature.** It is scoped to a sandboxed workshop on a read-only root (task 7.8).
- **Claiming sentience, or describing features that do not exist.** The README and the being itself describe what is true today (task 3.9).
