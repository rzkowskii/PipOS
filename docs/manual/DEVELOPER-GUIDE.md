<!--
doc: developer-guide
verified: 2026-11-23
sources: lab/build.sh lab/vm.py lab/cortex-dev.sh scripts/run-sim.sh lab/candidate-build.sh lab/candidate_tests.py lab/soak.py lab/builder/Dockerfile os/configs/beingos_x86_64_defconfig os/configs/beingos_laptop_defconfig Cargo.toml
-->

# PipOS developer guide

This guide takes you from a fresh checkout to a running, tested image. It
gives the shortest correct path. For the deeper procedures (installer QA,
update fixtures, Wi-Fi simulation, hardware qualification), see
[docs/LAB.md](../LAB.md) and [docs/HARDWARE.md](../HARDWARE.md).

## 1. Prerequisites on the Mac

| Tool | Used for |
|---|---|
| Colima + Docker | The Buildroot builder container (profile `beingos`: 12 CPU, 24 GB, 100 GB disk) |
| QEMU (with EDK2 firmware) | The virtual Ivy Bridge laptop |
| Python 3 | The lab harness, the tests, and the mind when simulated |
| Rust / cargo | Host builds of the face and brainstem, and the Rust tests |
| SDL2 + pkg-config | The face crate links SDL2 and finds it through pkg-config (`brew install sdl2 pkg-config`) |
| ffmpeg | `--sound` microphone bridge and the L4 tests |
| macOS `say` | Generated speech for voice tests, and the simulator's voice |
| 7-Zip (`7zz` or `7z`) | Candidate builds |
| curl | `lab/cortex-dev.sh` health checks |

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

Two caveats:

- `lab/cortex-dev.sh` builds the same llama.cpp v1.0.0 natively for the
  Mac, with 4 threads. Its speed tells you nothing about the laptop. It
  also needs an image build to have downloaded the llama.cpp tarball
  first.
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
| `lab/build.sh laptop-try` | `beingos-laptop-try.img`: the stick for the real MacBook, with an on-panel GRUB and a `PIPLOGS` partition |
| `lab/build.sh bundle` / `installed-bundle` | Signed RAUC updates for the factory or installed channel |
| `lab/build.sh candidate NAME` | Frozen, sealed release candidate in `out/candidates/NAME/` |

The build details:

- Buildroot 2026.08.2 is pinned by SHA-256 and runs in a Debian trixie
  container as a non-root user.
- The target CPU is `BR2_x86_ivybridge`. Nothing in the image may use
  AVX2. llama.cpp is built with `GGML_NATIVE=OFF` and AVX, but no AVX2,
  FMA or BMI2.
- The signing key is created by `lab/build.sh up` in the `beingos-work`
  volume, under `/work/beingos-keys`. Only its certificate enters the
  image.

## 4. Test

```bash
cargo test --workspace                                  # Rust: bus, brainstem, face
python3 -m unittest discover -s tests -t .              # host Python (≈ 500 tests, < 1 min)
python3 -m unittest discover -s lab/tests -t . -v       # QEMU guest suites (slow)
```

- `tests/test_real_cortex.py` runs only when `lab/cortex-dev.sh` is up on
  port 8377, or when `BEING_TEST_CORTEX_PORT` is set.
- 16 backup tests need `age` on `PATH`.

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
| `test_laptop_smoke` | Laptop Try stick: GRUB entries, `PIPLOGS`, install |
| Others | Model pack tamper, face on USB, hardware audit, network modes, Wi-Fi (hwsim) |

For a release candidate:

```bash
lab/build.sh candidate NAME
cd out/candidates/NAME/source
python3 -m lab.candidate_tests --manifest ../release.json --output ../guest-evidence
python3 -m lab.soak --image ../media/beingos.img --seconds 300 --interval 30
```

## 5. Where to change things

| You want to… | Edit |
|---|---|
| Add a spoken command | `mind/commands.py` (pattern) and `mind/turn.py` (handler). Add a test in `tests/`. Update [REFERENCE.md](REFERENCE.md#spoken-commands) and [USER-GUIDE.md](USER-GUIDE.md) |
| Change the personality or prompt | `config/identity.md`, `mind/profile.py` |
| Add a bus topic | The sending organ and `docs/PROTOCOL.md`. If only one sender is allowed, also edit the per-sender checks in `crates/brainstem/src/main.rs` |
| Change a state or timer | `crates/brainstem/src/machine.rs`. Update [ARCHITECTURE.md §4](ARCHITECTURE.md#4-states) |
| Change the face | `crates/face/src/{anim,eye,render}.rs`, `eye.frag` |
| Add a service or package | `os/package/being/` (`being.mk`, the unit file, `50-being.preset`) |
| Change the kernel | `os/board/beingos/linux.config`; laptop-only changes go in `linux-laptop.config` |
| Change the disk layout | `os/board/beingos/genimage.cfg` (factory) and `mind/installer.py` (installed) |

## 6. Working rules for this repository

- **Check the release record first.** [USB-READINESS.md](../USB-READINESS.md)
  and [DESIGN-COVERAGE.md](../DESIGN-COVERAGE.md) say what is verified
  and what is still open. Update them when your change moves a claim.
- **One owner per builder or guest.** Whoever starts the Colima builder
  or a QEMU guest also stops it.
- **Rebuild before guest tests.** `out/images` can predate the source.
  Check the timestamps.
- **No physical writes without explicit owner approval.** This covers USB
  sticks and internal disks.
- **Keep the docs true.** If you change a file listed in a manual page's
  `sources:` header, re-check that page and update its `verified:` date.
  `scripts/check-docs.py` lists the pages that need a look.
