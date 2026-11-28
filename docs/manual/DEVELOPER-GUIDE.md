<!--
doc: developer-guide
verified: 2026-11-24T22:17
sources: lab/build.sh lab/vm.py lab/cortex-dev.sh lab/make-iso.sh lab/cable.sh scripts/run-sim.sh scripts/check lab/candidate-build.sh lab/candidate_tests.py lab/soak.py lab/builder/Dockerfile os/configs/beingos_x86_64_defconfig os/configs/beingos_laptop_defconfig os/board/beingos/try-image.sh os/board/beingos/post-build.sh os/board/beingos/linux-cable.config os/package/being/Config.in Cargo.toml sim/fake_cortex.py sim/body.py mind/mic_port.py lab/mic_bridge.py mind/lab_access.py lab/model_audition.py lab/decision_eval.py lab/dream_recall_eval.py lab/recall_second_opinion.py lab/try_timings.py lab/tests/test_cable_dev.py lab/tests/test_voice_terminal.py mind/bus.py mind/everyday.py mind/diag.py mind/diag_report.py mind/keys.py crates/face/src/keys.rs crates/brainstem/src/main.rs mind/voice.py os/package/being/being-cable-link.service os/package/being/being-cable-ssh.service os/package/being/being-cable-workbench.service
-->

# PipOS developer guide

This guide takes you from a fresh checkout to a running, tested image. It
gives the shortest correct path. For the deeper procedures (installer QA,
update fixtures, Wi-Fi simulation, hardware qualification), see
[docs/LAB.md](../LAB.md) and [docs/HARDWARE.md](../HARDWARE.md). Where the
project stands is in [STATUS.md](../../STATUS.md).

## 1. Prerequisites on the Mac

| Tool | Used for |
|---|---|
| Colima + Docker | The Buildroot builder container (profile `beingos`: an arm64 `vz` VM with 14 CPUs, 48 GB and a 100 GB disk by default; `BEING_BUILDER_CPUS` and `BEING_BUILDER_MEMORY` change the first two when the VM is created) |
| QEMU (with EDK2 firmware) | The virtual Ivy Bridge laptop |
| Python 3 | The lab harness, the tests, and the mind when simulated |
| Rust / cargo | Host builds of the face, brainstem and bodycare, and the Rust tests |
| SDL2 + pkg-config | The face crate links SDL2 and finds it through pkg-config (`brew install sdl2 pkg-config`) |
| ffmpeg | `--sound` microphone bridge and the L4 tests |
| macOS `say` | Generated speech for voice tests, and the simulator's voice |
| 7-Zip (`7zz` or `7z`) | Candidate builds |
| curl | `lab/cortex-dev.sh` health checks |
| `age` and `age-keygen` (optional) | The backup tests; without them those tests are skipped |

To remove everything the lab created:

```bash
colima delete --profile beingos
```

Then delete `out/`.

## 2. Three ways to run Pip

| Mode | Command | Speed | Use for |
|---|---|---|---|
| Simulator, scripted replies | `scripts/run-sim.sh --fake` | Instant | UI, state machine, commands, memory |
| Simulator, real model | `lab/cortex-dev.sh start`, then `scripts/run-sim.sh --cortex http://127.0.0.1:8377` | Fast | Conversation quality, prompts |
| Real OS in QEMU | `python3 lab/vm.py` (add `--sound` for the Mac mic and speakers) | Slow (emulated CPU) | Boot, services, appliance behaviour |

In the text modes you type to Pip. Slash commands simulate the body:
`/lid close`, `/lid open`, `/battery 37`, `/plug`, `/lux 2`, `/temp 70`,
`/hour 23`, `/mute`, `/interrupt`, `/quit`.

`cargo build`, then `target/debug/face --demo`, shows every expression.

Development memory lives in `~/.local/share/being/mind`. Set `BEING_MIND`
to use another directory.

The simulator is made of these pieces:

- **`--fake` replies** come from `sim/fake_cortex.py`. It serves the
  llama-server HTTP API with scripted text, and can run alone:
  `python3 sim/fake_cortex.py --port 8080`.
- **The slash commands** are implemented in `sim/body.py`.
- **With `lab/vm.py --sound`**, the Mac microphone reaches the guest
  through `lab/mic_bridge.py` and a virtio-serial port named
  `org.beingos.microphone` (`mind/mic_port.py`). Speaker output goes
  through CoreAudio.

Two caveats:

- `lab/cortex-dev.sh` builds the same llama.cpp v1.0.0 inside the builder
  container: an arm64 Linux VM in Colima, not emulated. It serves
  `out/models/reflex.gguf` with 4 threads, one slot and a 4096-token
  context (the appliance runs two slots in 8192), on `127.0.0.1:8377`
  unless `BEING_PORT` names another port. Its speed tells you nothing
  about the laptop. It needs three things in place first: the builder, a
  model staged with `lab/build.sh model`, and an image build that has
  downloaded the llama.cpp tarball. It shares the builder with image
  builds, so do not start it while one is running.
- QEMU timings also predict nothing. The CPU is emulated and the face is
  software-rendered at quarter scale.

## 3. Build

```bash
lab/build.sh model path/to/model.gguf   # once: stage the conversation model
lab/build.sh                            # build out/images/beingos.img (first build ≈ 1 h)
```

| Target | Output |
|---|---|
| `lab/build.sh` | `out/images/beingos.img`: lab factory image with serial console and lab body |
| `lab/build.sh try-image` / `try-screen` | `beingos-try.img` / `beingos-try-screen.img`: USB Try, serial or on-panel menu |
| `lab/build.sh laptop` | `beingos-laptop.img`: no getty, no root login, no SSH, no lab body |
| `BEING_LAPTOP_MAINTENANCE=1 lab/build.sh laptop` | `beingos-laptop-maintenance.img`: adds paired-Mac SSH |
| `lab/build.sh laptop-try` | `beingos-laptop-try.img`: the stick for the real MacBook, with an on-panel GRUB and a `PIPLOGS` partition. `BEING_TRY_BENCH=1` gives the `-bench` stick with the model benchmark as the default and a separate read-only boot and idle entry. |
| `BEING_LAPTOP_CABLE=1 lab/build.sh laptop-try` | `beingos-laptop-cable-try.img`, in its own tree `/work/output/laptop-cable`: the development-only cable stick (below). Run `lab/build.sh cable-key PUBKEY HOSTKEY` once first |
| `lab/make-iso.sh [DIR]` | `out/iso/pipos-macbook.iso`: the same stick as one hybrid ISO ([ISO.md](../ISO.md)). Refuses the cable build |
| `lab/build.sh bundle` / `installed-bundle` | Signed RAUC updates for the factory or installed channel |
| `lab/build.sh release-key DIR` / `release-cert FILE` / `laptop-bundle KEY` | The owner's passphrase-protected release key (made on the Mac, outside the repository), its certificate in the builder, and laptop updates signed with it |
| `lab/build.sh laptop-rotation-bundle OLD_KEY OLD_CERT VERSION` | Signed bridge to the replacement release certificate. [Rotation procedure](../HARDWARE.md#rotate-the-laptop-release-key) |
| `lab/build.sh candidate NAME [--laptop]` | Frozen, sealed release candidate in `out/candidates/NAME/`; `--laptop` adds the laptop disk and Try stick from the same snapshot, with a rootfs parity report |

The build details:

- Buildroot 2026.08.2 is pinned by SHA-256 and runs in a Debian trixie
  container as a non-root user.
- The target CPU is `BR2_x86_ivybridge`. Nothing in the image may use
  AVX2. llama.cpp is built with `GGML_NATIVE=OFF` and AVX, but no AVX2,
  FMA or BMI2.
- The lab signing key is created by `lab/build.sh up` in the
  `beingos-work` volume, under `/work/beingos-keys`. Only its certificate
  enters the image. Once `release-cert` has imported the owner's
  certificate, laptop images trust only that one for updates.
- Every image written to `out/images` gets `IMAGE.source-id`, the
  `python3 -m lab.source_id` of the tree when the build started, but only
  when the run rebuilt the being package and then the image. The gate
  (`BEING_GATE=1`) refuses an image without a matching id.
- The commands that rebuild the being package first clean its build
  directory, so every source file is copied in again
  (`BEING_FAST_REBUILD=1` skips that). `BEING_PACK_ROLES` chooses which
  model packs are staged.

## 4. Test

```bash
scripts/check fast                                      # everything below that runs on the Mac, a few minutes
cargo test --workspace                                  # Rust: bus, brainstem, face, bodycare
python3 -m unittest discover -s tests -t .              # host Python (about 2,000 tests, about 4 min)
python3 -m unittest discover -s lab/tests/host -t .     # host checks of the lab tools
python3 -m unittest discover -s lab/tests -t . -v       # QEMU guest suites (slow)
```

- `tests/test_real_cortex.py` runs only when a llama-server answers on
  `127.0.0.1:8377` (for example after `lab/cortex-dev.sh start`), or on
  the port named by `BEING_TEST_CORTEX_PORT`.
- The backup tests need both `age` and `age-keygen` on `PATH`. A few
  tests need the Rust binaries, so run `cargo build` first. Other skips
  are Linux-only sockets and the real model.
- `scripts/check fast` also runs `scripts/check-docs.py` and the media
  check. Both are advisory there: reported, never blocking.

| Guest suite | Proves |
|---|---|
| `test_l0` | Ivy Bridge CPU flags, kernel 6.18, UEFI SATA boot, all services up |
| `test_l1` | Eyes and nothing else on screen. Face crash recovery and brainstem-loss look |
| `test_l2` | Real model reply, commands without the model, continuity across reboot, power cut and new body, raw-disk forgetting |
| `test_l3` | Signed update, rollback after three failed trials, watchdogs, 20 seeded power cuts |
| `test_l4` | Wake phrase, transcription, speech, mute and interrupt over ALSA loopback |
| `test_l5` | Lid close stops audio. Lid open wakes without the model |
| `test_l6` | Opt-in camera tracking and release |
| `test_local_screen`, `test_try`, `test_installer`, `test_install_apply`, `test_setup` | USB menu, Try isolation, installer, first-run setup |
| `test_laptop_smoke` | Laptop Try stick through its screen and keyboard: GRUB entries, `PIPLOGS`, install |
| `test_iso` | The ISO as the MacBook would use it |
| `test_apps` | The workshop apps over the real bus |
| `test_voice_terminal` | "Hey Pip, open a terminal." said in one breath, at full level, 30 dB and 45 dB down, and by a second voice, through the lab's ALSA loopback: the wake, the transcript and which pass gave it, the mind's `workshop.open` route with no model turn, and the terminal on screen |
| `test_cable_dev` | The cable stick over QEMU's user network standing in for the cable (`BEING_LAPTOP_CABLE_TRY_IMAGE=out/images/beingos-laptop-cable-try.img`, `BEING_CABLE_IDENTITY` for the login key). Not part of a sealed candidate |
| Others | Model pack tamper, face on USB, hardware audit and SSH maintenance, network modes, Wi-Fi (hwsim), Broadcom firmware |

`scripts/check boot-gate` runs the must-pass suites (laptop smoke, L0, L1,
L5, Try, local screen, install/apply, setup and one real-model L2 turn) in
parallel QEMU lanes with `BEING_GATE=1`, so a skip or a stale image fails.
`scripts/check full --manifest out/candidates/NAME/release.json` runs the
whole candidate controller.

For a release candidate by hand:

```bash
lab/build.sh candidate NAME --laptop
cd out/candidates/NAME/source
python3 -m lab.candidate_tests --manifest ../release.json --output ../guest-evidence
python3 -m lab.soak --image ../media/beingos.img --seconds 300 --interval 30
```

### Evaluating models, speed and memory

The model is chosen by benchmark, not by taste:

| Tool | Measures |
|---|---|
| `lab/model_audition.py`, `lab/turn_latency.py`, `lab/voice_latency_qemu.py` | Replies and latency |
| `lab/quant_candidates.py` | Quantisation candidates for a model |
| `lab/bench_guest.py`, `lab/bench_report.py`, `lab/try_timings.py` | Measure models and speech with `being.bench=1`. Collect boot, service and hardware evidence with `being.bench=2`. Reports go to `PIPLOGS` when available. |
| `lab/sherpa_avx_smoke.py` | That the speech engine runs without AVX2 |
| `lab/decision_eval.py` (fixtures in `decision_fixtures.py`) | Recall routing decisions |
| `lab/dream_recall_eval.py` (scored on `dream_recall_holdout.py`) | Recall across an overnight pass |
| `lab/recall_second_opinion.py` (scored on `decision_holdout.py`) | The optional OpenJev second opinion (see [LAB.md](../LAB.md#compare-local-recall-decisions)) |

Keep holdout sets out of prompt tuning.

### Reaching a running Pip

Lab images have a root console (`python3 lab/vm.py --shell`). For a
laptop, the maintenance build (`BEING_LAPTOP_MAINTENANCE=1`) is needed:

1. In `paired-lan` mode, enroll the Mac's public key with
   `being-lab-access` (`mind/lab_access.py`).
2. Port 2222 is a read-only observe listener (`being-lab-observe`).
3. Port 2223 is a development login (`being-lab-dev`). You open it
   explicitly, and it closes after at most 30 minutes.

[HARDWARE.md](../HARDWARE.md#enroll-the-laptop-once) has the enrollment
steps. The maintenance listeners never start on a Try boot, so for a Try
stick on the bench use the cable development build instead.

#### The cable development build

A development-only Try stick that the Mac reaches over a plain Ethernet
cable, with no router, DHCP or pairing. It is the release laptop image
plus OpenSSH's server, the cable link (`BR2_PACKAGE_BEING_CABLE_DEV`) and
an audio-debugging kernel fragment (`os/board/beingos/linux-cable.config`).
[LAB.md](../LAB.md#cable-development-build) has the full procedure; in
short:

1. Make two Ed25519 keys on the Mac, outside the repository: a login key
   (a passphrase is fine) and a host key without a passphrase, because
   every stick presents it. Import them once with
   `lab/build.sh cable-key LOGIN.pub HOSTKEY`.
2. Build: `BEING_LAPTOP_CABLE=1 lab/build.sh laptop-try` (with
   `BEING_LAPTOP_PACKAGE_JOBS=3` for a cold build, which takes about an
   hour). It is refused together with `BEING_LAPTOP_MAINTENANCE=1` or
   `BEINGOS_REQUIRE_RELEASE_KEY=1`.
3. Boot the stick and connect the cable. `lab/cable.sh ssh-config`
   writes the host key pin and prints the `Host pip` (IPv6) and `pip4`
   (IPv4) blocks to add to your SSH configuration. Then
   `lab/cable.sh ping`, and `ssh pip` (or `lab/cable.sh ssh`, which needs
   no configuration).
4. Edit `mind/*.py` on the Mac and run `lab/cable.sh push`: it copies
   them onto Pip's writable copy of the code and try-restarts the organs
   that run it. It fails if the workbench is not mounted or if none of
   those organs is active afterwards; it does not catch a down mind or
   voice while another organ runs, so check
   `ssh pip systemctl status being-mind being-voice`. `lab/cable.sh pull-logs` brings `/run/being-diag` and the
   boot's `PIPLOGS` folder back to `out/hardware-logs/cable-DATE/`.

Every boot entry adds `being.cable=1 being.voice_debug=1`. The local journal
holds voice transcripts and the mind's route words in owner mode. The
`PIPLOGS` export redacts those words. Pip takes
`169.254.77.1` and `fe80::77:1` on its first wired port and accepts root
SSH with that one key from link-local addresses only. `being.cable=0` on
the GRUB command line turns off the link, SSH and the writable copy of the
code for a boot. Voice debugging stays on unless `being.voice_debug=0` is
also added after it. The stick never
installs, never becomes an ISO and is never signed as an update, and its
image holds the SSH host key: never commit, attach or share it.

#### Diagnostics

`pip-diag` runs on every laptop and lab image, as root over the cable or
from the Try menu's item 8. For example:

```bash
ssh pip 'pip-diag quick --json --now '"$(date -u +%s)"
ssh -t pip pip-diag voice        # interactive: you speak and listen
ssh pip 'pip-diag hear --phrase "Hey Pip, open a terminal." --expect-route workshop.open'
ssh pip pip-diag restore         # after a run was cut off
```

The [reference](REFERENCE.md#diagnostics-pip-diag) lists every
subcommand, flag, exit code and output file. Without the cable, the Try
stick's `PIPLOGS` partition (where each run's `diag-<UTC>/` folder goes)
and the F1 captions overlay are the ways to see what the laptop did.

## 5. Where to change things

| You want to… | Edit |
|---|---|
| Add a spoken command | `mind/commands.py` (pattern) and `mind/turn.py` (handler). If it stores or changes something, add it to `ADDRESS_ONLY_COMMANDS`. Add a test in `tests/`. Update [REFERENCE.md](REFERENCE.md#spoken-commands) and [USER-GUIDE.md](USER-GUIDE.md) |
| Add an everyday skill | A module with `may_match(text)` and `handle(text, ctx)`, listed in `ORDER` in `mind/everyday.py`. It never calls the model |
| Change the personality or prompt | `config/identity.md`, `mind/profile.py` |
| Add a bus topic | Edit these places:<ul><li>the sending organ;</li><li>`docs/PROTOCOL.md`;</li><li>the receiver's subscription list (`SUBS` in `mind/turn.py`, the voice and eyes equivalents, `crates/face/src/link.rs` or `crates/bodycare/src/main.rs`);</li><li>`allowed_from` in `crates/brainstem/src/main.rs`, if only one sender is allowed, and `crates/brainstem/src/peer.rs` for a new organ name.</li></ul>`mind/bus.py` duplicates the Rust socket-path rule in `crates/bus`, so change both together. A topic that must preempt a running turn, as `mind.cancel` does, belongs in the bus's `urgent` callback |
| Change a state or timer | `crates/brainstem/src/machine.rs`. Update [ARCHITECTURE.md §4](ARCHITECTURE.md#4-states) |
| Change battery care | `crates/brainstem/src/battery.rs` (the brainstem's judgement and power-off) and the plea lines in `mind/turn.py` |
| Change the face | `crates/face/src/{anim,eye,render}.rs`, `eye.frag` |
| Change what a key does | `crates/face/src/keys.rs` (the top row, by SDL scancode, and the evdev-only Mission Control and Launchpad), `crates/face/src/main.rs` (Esc, M, F-keys), and `mind/keys.py` for the keys the mind answers (a new one goes in its `TOPICS`, which the mind subscribes to). `allowed_from` in `crates/brainstem/src/main.rs` already admits every `key.*` topic from the face alone on the laptop, so a new key topic needs no entry there; add it to the brainstem test `the_top_row_keys_come_from_the_face_and_reach_the_mind`. Only a new topic that is not a key, such as `being.kbd_light`, needs its own arm in `allowed_from`. Update [REFERENCE.md](REFERENCE.md#keys-and-touch) |
| Add a diagnostics check | A function in `mind/diag_*.py` that returns `check(id, section, status, value, evidence, hint)` items, run through `guarded()` in a section of `mind/diag.py`. Anything that opens the sound card goes inside `diag_sound.AudioSession`. For it to count in a verdict area, start its id with one of the prefixes that `diag_report.AREAS` lists for that area (such as `mic.`, `speaker.`, `loopback.`, `tts.`, `voice.wake`, `commands.`, `hear.terminal` or `workshop.`); the area names themselves are not matched. Add a test with fakes in `tests/test_diag*.py` |
| Add a service or package | `os/package/being/` (`being.mk`, the unit file, `50-being.preset`) |
| Change the kernel | `os/board/beingos/linux.config`; laptop-only changes go in `linux-laptop.config` |
| Change the disk layout | `os/board/beingos/genimage.cfg` (factory) and `mind/installer.py` (installed) |

## 6. Working rules for this repository

- **Check the status first.** [STATUS.md](../../STATUS.md) says what is
  verified, at which level (host, QEMU or the real Mac), and what is still
  open. Update it when your change moves a claim.
  [DECISIONS-OWNER.md](../DECISIONS-OWNER.md) lists what only the owner
  can decide.
- **One owner per builder or guest.** Whoever starts the Colima builder,
  a model server in it or a QEMU guest also stops it.
- **Rebuild before guest tests.** `out/images` can predate the source.
  Compare each image's `.source-id` with `python3 -m lab.source_id`.
- **No physical writes without explicit owner approval.** This covers USB
  sticks and internal disks.
- **Keep the docs true.** If you change a file listed in a manual page's
  `sources:` header, re-check that page and update its `verified:` stamp.
  `scripts/check-docs.py` lists the pages that need a look.
