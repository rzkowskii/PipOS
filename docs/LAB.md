# The lab

The lab builds the real PipOS disk images and raises them in a virtual machine.
A Linux container builds the images; QEMU emulates a 2012-class laptop for them
to wake in. The lab image (`beingos_x86_64_defconfig`) keeps a serial console,
a root login and the lab body for these tests. **The MacBook gets a separate
laptop image** (`beingos_laptop_defconfig`: no shell, no sshd, no lab body),
built with `lab/build.sh laptop` or `laptop-try`. Never write the lab image to
a stick for the Mac. The first hardware boot is described in
[FIRST-BOOT.md](FIRST-BOOT.md); current status is in [STATUS.md](../STATUS.md).

## What it needs

Already on this Mac: QEMU, Colima and Docker. Nothing else is installed on
macOS. The build lives in one Colima profile, `beingos` (12 CPUs, 24 GB RAM,
100 GB sparse disk), which mounts only this project folder.

To stop the lab:

```bash
colima stop --profile beingos
```

Do not delete `out/` or the Colima profile to tidy up. `out/` holds the frozen
first-boot stick, sealed candidates and the evidence that STATUS.md links to,
and the profile's Docker volume holds the only copy of the lab signing key
(plan task 0.8). Free space by moving old images and candidates to the NVMe
archive disk, never by deleting them.

## Build

Stage the qualified Qwen3.5-0.8B Q4_K_M GGUF from Unsloth in `out/models/`.

```bash
lab/build.sh model /path/to/model.gguf
```

During packaging the builder checks the exact weight digest against the
reviewed model descriptor and signs that descriptor with the RAUC release
key. The factory and Try images carry the signed envelope on their model
partition. Before starting llama-server, Cortex verifies the envelope with
the certificate on the read-only system, matches the qualified identity,
and hashes the GGUF. To inspect the verified identity in a guest, run
`python3 /usr/lib/being/mind/model_pack.py verify --json`. The pack carries
the conversation role, upstream revisions, quantization, size, SHA-256 and
Apache-2.0 licence provenance. The full licence is installed under
`/usr/share/being/default-model/LICENSE`. Host model auditions can still
use other weights. [Qualified descriptor](../os/package/being/default-model/pack.json)

An older PipOS model partition can already contain these exact weights
and only the historical `manifest.json`. Provision it **before** installing
a system update that requires signed packs. On trusted Linux maintenance
media, mount the intended legacy model partition read-write at
`/mnt/old-models` and the signed candidate Try model partition read-only at
`/mnt/candidate-models`. Use the certificate pinned by the target's
existing trusted system and the model descriptor and verifier from the
candidate's verified system. With those system roots at `/mnt/old-system`
and `/mnt/candidate-system`, run

```sh
python3 /mnt/candidate-system/usr/lib/being/mind/model_pack.py provision \
  --models /mnt/old-models \
  --signed-pack /mnt/candidate-models/beingos-conversation.cms \
  --certificate /mnt/old-system/etc/rauc/keyring.pem \
  --descriptor /mnt/candidate-system/usr/share/being/default-model/pack.json
```

The command checks the donor signature and the existing weight checksum
before atomically publishing one metadata file. Unmount the legacy partition
before reboot. Missing or untrusted metadata leaves Cortex stopped. Fresh Try
and factory images already contain the envelope, and the installer copies it
with the weights. Verification time on the physical MacBook remains open.

Build the image. The first build takes about an hour, most of it LLVM; later
builds take minutes:

```bash
lab/build.sh
```

The result is `out/images/beingos.img`.

Other commands:

| Command | What it does |
|---|---|
| `lab/build.sh build being-rebuild all` | Rebuild the organs after changing their code, then the image |
| `lab/build.sh try-image` | Build the standalone Try PipOS image |
| `lab/build.sh try-screen` | Build a separate Try USB with the local keyboard menu |
| `lab/build.sh build PACKAGE-dirclean all` | Rebuild one package from clean, then the image |
| `lab/build.sh bundle` | Sign the current system as `out/images/beingos.raucb` |
| `lab/build.sh test-media` | Make the signed update fixtures for L3 |
| `lab/build.sh shell` | A shell in the builder |
| `lab/build.sh down` | Stop the builder VM (it holds 24 GB of RAM while running) |

**After changing a package's options in `os/configs/`, rebuild that package from
clean.** Buildroot does not notice option changes to something it has already
built. The login manager was missing from one image for exactly this reason.

When reusing the pre-L3 builder cache, refresh GRUB, the kernel, and the organs
with `lab/build.sh build grub2-rebuild linux-reconfigure being-rebuild all`.
For an earlier L3 cache with standalone RAUC, add `rauc-reconfigure` to the
same invocation to enable the install service.

## Build the laptop image

The images above are lab images: they keep a passwordless root getty on
`ttyS0`, the lab body, OpenSSH and the QEMU plumbing. The image for the
real MacBookPro9,2 comes from its own defconfig,
`os/configs/beingos_laptop_defconfig`, in its own output tree
(`/work/output/laptop`), so `beingos_x86_64_defconfig` and every QEMU
suite stay as they are.

```bash
lab/build.sh laptop        # out/images/beingos-laptop.img
lab/build.sh laptop-try    # out/images/beingos-laptop-try.img, the USB to boot on the MacBook
```

The first laptop build is a full build (about an hour; downloads and
ccache are shared with the lab tree). `lab/build.sh laptop-config` only
re-applies the defconfig and `lab/build.sh laptop-make ARGS...` runs make in
the laptop tree, for example `laptop-make being-rebuild all`.

What the laptop image leaves out, compared with the lab image:

| Lab piece | Laptop image |
|---|---|
| Root login and the getty on `ttyS0` | Root is locked (`BR2_TARGET_ENABLE_ROOT_LOGIN` off), no Buildroot getty, and `getty@`, `serial-getty@`, `autovt@`, `console-getty` and `container-getty@` are masked |
| Serial console | No `console=ttyS0` in the factory, installed or Try GRUB configuration, no GRUB serial terminal or module, no 8250 driver |
| Lab body (`sim/body.py`, `being-lab-body.service`) | Not installed (`BR2_PACKAGE_BEING_LAB_BODY` off) |
| OpenSSH, `being-lab-access`, `being-lab-observe`/`being-lab-dev`, the `being-observe` account | Not installed (`BR2_PACKAGE_BEING_LAN_MAINTENANCE` off, OpenSSH off) |
| QEMU-only kernel pieces | `linux-laptop.config` turns off fw_cfg, virtio console/input/net, virtio RNG, the i6300esb watchdog, `test_power`, `snd-aloop` and `uinput`. Virtio disk and display stay for a smoke boot in a plain VM. Hwsim and vivid only ever live in the separate lab Wi-Fi/camera overlays. |

The hardware body is unchanged: i915 with crocus (llvmpipe stays as the
fallback), HDA Cirrus audio, pocketsphinx and flite, b43/bcma and tg3,
applesmc, ACPI lid/battery, UVC camera and the iTCO watchdog. Both laptop
commands fail if the resolved configuration enables the lab body, root
login or a getty, or if the built tree still holds lab-only files or a
serial console in GRUB.

Paired LAN maintenance is an owner opt-in at build time, as in the
design. `BEING_LAPTOP_MAINTENANCE=1 lab/build.sh laptop` (or `laptop-try`)
builds in a separate tree, `/work/output/laptop-maintenance`, with
OpenSSH and `being-lab-access` added, and names the media
`beingos-laptop-maintenance.img` and `beingos-laptop-maintenance-try.img`.
Nothing listens until the owner runs `being-lab-access enroll`
([HARDWARE.md](HARDWARE.md)). Use a fresh tree rather than toggling
options in one: Buildroot never removes files a package already installed.

The b43 firmware is owner-sourced. When
`out/firmware-overlay/usr/lib/firmware/b43/` exists (staged with
`python3 -m lab.b43_firmware`), the laptop commands add it as the root
filesystem overlay; otherwise they print a note and the internal Wi-Fi
will have no firmware.

Still the same as the lab image: the RAUC keyring is the builder
volume's lab certificate, so laptop updates and model packs are signed
with the lab key until an owner-held release key exists.

## Run it

```bash
python3 lab/vm.py
```

A window opens with its face. The terminal you ran it from is its ears and
voice: type a line to say it. It keeps its disk, and so its memory, between
runs. Ctrl-A then X switches it off without warning, which it will notice next
time.

| Option | Effect |
|---|---|
| `--fresh` | Discard its disk and start from the built image: a new being |
| `--shell` | Put a root shell on the terminal, not the conversation |
| `--no-window` | No screen |
| `--sound` | Use the Mac microphone and speakers with Pip's local voice service |
| `--image PATH` | Start from a staged factory image with its own persistent lab disk |

It is slow here. The CPU is emulated instruction by instruction, and the face is
drawn by a software renderer at a quarter of the screen's size. None of the
speeds in the lab predict the laptop.

New virtual disks use 4 KiB qcow2 clusters to match the ext4 block size.
Virtual disks created with the older 64 KiB default can retain bytes after
ext4 discards a single block.

New interactive disks are independent copies. Rebuilding the factory image
preserves their installed system and memory. Existing copy-on-write disks
made before L3 still need the original factory image they were based on.

## Talk to Pip

Start the L4 image on the Mac:

```bash
python3 lab/vm.py --sound
```

Focus the QEMU face window. Say **“Hey Pip”**, wait for its eyes to show
attention, and then speak. Pip checks the wake phrase at the end of the
utterance. Follow-up turns can start while its eyes are still attending.
Standalone greetings, check-ins, and “Are you there?” get short local
replies that vary on repeat. A greeting with a topic goes through the
normal conversation path.
**M** or **F10** toggles the mic. **Esc** stops the current reply, including
audio already playing. The terminal shows the maintenance shell in this
mode. The serial text loop remains available when launched without
`--sound`.

The Mac launcher uses FFmpeg with AVFoundation to capture 16 kHz mono PCM.
The process that launches FFmpeg needs macOS microphone access. Use
`--mic-input` with a device name or index to select another microphone.
The host capture process runs only while the guest voice opens its input.
CoreAudio carries the guest's speaker output.

PocketSphinx handles keyword spotting, endpointing, and streaming English
transcription in the guest. Flite synthesizes replies locally and defaults to SLT.
During synthesis and playback the voice closes its microphone input,
which keeps it from hearing its own replies. Audio stays out of the
PipOS event bus and captured audio is never written to the mind
partition. The lab stack uses the existing wake, transcription, and speech
events while the laptop's production speech stack remains a benchmark
decision.

The headless audio tests play generated speech through ALSA loopback and
exercise the local microphone transport with PCM fixtures. They use
macOS `say` with Samantha and FFmpeg:

```bash
python3 -m unittest lab.tests.test_l4 -v
```

## Personalize the being

Open the staged personalization image and log in as `root` in the guest:

```bash
python3 lab/vm.py --image out/images/beingos-setup.img --shell
```

Use `--sound` in place of `--shell` for speaker previews, then run:

```sh
being-setup
```

Choose the name, what it calls you, a wake phrase, a local voice and speaking
speed, and a personality. Pick a preset, tune the individual traits, or roll
them all with **Random personality**. **Surprise me** rolls the name, an
installed voice and the personality together. Play a sample at any point.
For an audible preview in the sound lab, keep the lid open and press **M** in
the face window to mute the microphone. The preview is also printed as text.

Run `being-setup --dry-run` for a trial and `being-setup --show` to print
the saved profile or factory defaults. After saving, reboot the guest to load the choices.
The profile lives in `/mind/profile.json` beside the life record and archive.
Renaming keeps its existing wake phrase until you choose a different one.
Ask “What is your name?” to check the new name without waiting for the model.
This terminal command is the setup path for the lab build.

## Preview an installation

Open the staged preflight image, log in as `root`, and run `being-installer`:

```bash
python3 lab/vm.py --image out/images/beingos-installer.img --shell
```

The menu checks which hardware Linux detects and lists each disk with its
identifier, size, and eligibility. The disk supplying the running system and
disks in use are excluded. Select a whole device to preview the EFI partition,
two system slots, space for the current models and a replacement, and the
remaining space for `/mind`. This is a read-only stage of the installer.

```sh
being-installer --check
being-installer --list
being-installer --plan /dev/sdb
```

Each command also accepts `--json`. The hardware check reports detected
interfaces. Use a spoken turn and the lid and keyboard controls to test
whether those interfaces work on the laptop.

The automated preflight check boots the lab image from virtual USB with a
separate disposable SATA disk:

```bash
BEING_INSTALLER_IMAGE=out/images/beingos-installer.img python3 -m unittest lab.tests.test_installer -v
```

## Try PipOS

`out/images/beingos-try.img` boots straight into a temporary session.
Use the usual QEMU launcher to talk from the serial console:

```bash
python3 lab/vm.py --image out/images/beingos-try.img
```

Use `--sound` or `--shell` to open the guest shell. Run `being-setup`
to try a name, wake phrase, voice and personality. Saving applies them
for this session. Run `being-installer` to check detected hardware and
preview a disk layout.

The session stores its profile and conversations in a private RAM
filesystem. The operating system and model come from the Try image,
with the model mounted read-only. A reboot starts with factory choices
and a fresh memory. The update services are disabled for Try.

Run the two-boot check with a disposable copy of the normal disk attached:

```bash
BEING_TRY_EXISTING_IMAGE=out/images/beingos-installer.img python3 -m unittest lab.tests.test_try -v
```

## Install from the Try USB in the lab

The local-screen candidate starts with **Try PipOS**, **Install
PipOS**, **Restore a being**, **Setup**, and **Check hardware**. Use
number keys and **Enter**. **F2** opens setup from the eyes. In Setup,
**F10** mutes the microphone for the **P** speaker preview. **F9**
recovers the screen when the lid reading is unavailable.

The screen uses the same disk review and device-specific `ERASE`
confirmation as the maintenance command. You can explicitly keep your
Try profile and announcement settings. Try keeps both in RAM. A fresh
installation starts with an empty archive.
Restore appears as pending when the backup program or its age tools are
absent. With those tools installed, the Restore screen takes owner-selected
files on mounted media and stages the restore before writing the selected
disk.

In Setup, **9** opens **Quiet hours and announcements**. Use **1** to
toggle automatic reminders and timers, **2** for daily greetings, **3**
to enter a 24-hour start and end in local time, and **4** to clear quiet
hours. **K** saves and **Q** discards. The defaults are automatic
reminders on, daily greetings off, and no quiet hours. Greetings use
presence from the separately enabled camera. To keep these settings in
a fresh installation, save them in Try and answer **Y** at the separate
announcement-settings prompt. Installed systems also offer
`being-setup --initiative`.

The install-capable Try image uses the same temporary session. Boot it from
USB and open a root maintenance console. Run `being-installer`, choose
**Install PipOS**, and review the exact whole-disk path, model, serial
when available, capacity and layout. The device-specific `ERASE` challenge
authorizes erasing that drive. A blank confirmation returns without writes.
The equivalent console command is `being-installer --install DEVICE`.

The installer stages files in RAM, rechecks and exclusively opens the
confirmed disk, then verifies what it writes before publishing the new GPT.
It installs two read-only system slots, the selected model files and a
fresh `/mind`. The installed machine gets a new life record. The local
screen can seed the selected profile into that volume after explicit
choice. Trial conversations remain in RAM.
Power off after the success message, remove the USB, and boot the installed
drive.

```bash
BEING_INSTALL_TRY_IMAGE=out/images/beingos-try-install.img python3 -m unittest lab.tests.test_install_apply -v
```

The screen lab drives the actual display and keyboard in QEMU. It uses
disposable SATA images and saves screenshots under `BEING_LAB_OUTPUT`
when set. The Restore case requires a screen image with `being-backup`,
`age` and `age-keygen` installed.

```bash
BEING_SCREEN_IMAGE=out/images/beingos-try-screen-restore.img python3 -m unittest lab.tests.test_local_screen -v
```

To verify the announcement settings, use a staged Try image with the
initiative controls and the matching Mind and brainstem:

```bash
BEING_SCREEN_IMAGE=out/images/beingos-try-screen-initiative.img python3 -m lab.initiative_install_qa
```

This uses the guest keyboard to save and carry the choices onto a
disposable SATA disk. It boots that disk without the USB, reboots it,
and checks direct requests during quiet hours through the serial lab body.

Installed systems use their own signed update channel. After a current
build, `lab/build.sh installed-bundle` signs `beingos-installed.raucb`.
The original `bundle` command still targets factory lab images. To build
the QA disk from two already signed artifacts, use the exact installed and
factory bundles you will supply to the guest test.

```bash
lab/build.sh installed-test-media out/images/beingos-installed.raucb out/images/beingos-install-qa-lab.raucb
BEING_INSTALL_TRY_IMAGE=out/images/beingos-try-install.img \
BEING_INSTALL_BUNDLE=out/images/beingos-installed.raucb \
BEING_HARDWARE_BUNDLE=out/images/beingos-install-qa-lab.raucb \
BEING_INSTALL_UPDATE_IMAGE=out/images/beingos-installed-update-qa.img \
python3 -m unittest lab.tests.test_install_apply -v
```

The test hashes both bundles mounted by the guest and compares them to the
specified host files. It verifies the primary and backup partition tables,
cuts power during the raw installation write, and boots installed slot A
without the USB. On slot A, it checks that both a signed factory-channel
bundle and an unsigned test file are rejected without changing slot B or
its boot state. It then installs the signed installed-channel bundle and
boots slot B without the USB to check health confirmation, the life ID
and a remembered fact. A further boot with the USB reattached checks disk
binding. The Try USB and the other attached PipOS disk are compared byte
for byte against their original images. The fixture uses the same system
payload for both slots. Failed-trial rollback, interruption during a RAUC
update, and upgrading a historical unsigned model partition are separate
checks.

## Verify the face on the first USB candidate

Select the exact Try or install-capable Try image intended for USB. The
face and brainstem should come from the same source build so the retained
low-battery reading reaches the face. Record the image digest and the
packaged `being-face` and `being-brainstem` digests. Pass the expected
face digest from the build with `BEING_FACE_SHA256` when available.

```bash
candidate=out/images/beingos-try-install.img
shasum -a 256 "$candidate"
BEING_FACE_USB_IMAGE="$candidate" python3 -m unittest -v lab.tests.test_face_usb
```

The check uses a disposable overlay and boots through QEMU USB storage.
On menu-enabled media it selects **Try PipOS** with the guest keyboard
before inspecting the rendered 1280×800 panel. Legacy Try media boots
directly to the face. The test looks for idle motion, low battery, AC
recovery and mute. The emulated software renderer targets 20 fps at
quarter size. For a visual review, boot the same image with
`python3 lab/vm.py --image "$candidate"` and use the serial lab controls.
The interactive launcher attaches the image as a virtual SATA disk.

| Cue | What to check |
| --- | --- |
| Cold start and idle | The first face is dim and closed. It opens into two cyan eyes on black. At idle the eyes hold, blink, make occasional glances, and settle. |
| Lid wake | `/lid close` goes black. `/lid open` gives one closed first frame followed by the staggered squint, blink and opening. Repeat with the model stopped, then close again mid-wake to check that the gesture is canceled. The matching `panel.ready` sequence marks the first presented frame. |
| Conversation | Type a request in the serial lab, allow one reply, and interrupt a second. Check attention widens, listening holds and thinking glances aside. Use `--sound` with the wake phrase to see playback supply the speech accents. A canceled reply releases its expression. For camera gaze, use the opt-in [camera fixture](#try-camera-presence-and-gaze). |
| Muted | Press M or F10. The two narrow eyes persist across lid close and open, after the waking gesture. Unmute to recover the awake pose. |
| Low battery | At idle use `/battery 12` to simulate 12% while unplugged. At 15% or below with AC disconnected, the tired pose appears for six seconds and can recur every 45 seconds when idle. `/plug` clears the cue during that window. Check the normal reply pose on a low-battery turn. |
| Unwell | Stop `being-mind` in the guest and check the asymmetric dazed eyes. Restart the service and confirm recovery. Losing the brainstem heartbeat also shows dazed on a visible panel. |
| Overnight work | Keep the panel black while eligible journal work runs. After a committed batch with journal entries, the next confirmed opening starts with the same closed eyes and adds a brief flutter during the wake blink. Opening during an uncommitted batch gives the usual wake. `face --demo` also shows the sleeping flutter on a host. |

On the 13-inch MacBookPro9,2, use the USB Try session and the built-in
1280×800 panel. Confirm i915/Crocus in the kernel and face logs and record
if the face falls back to llvmpipe. Check the scanout mode and renderer in
the maintenance shell.

```sh
journalctl -b -u being-face -o cat | grep 'drawn by'
cat /sys/class/drm/card*-LVDS-*/modes
sha256sum /usr/bin/being-face /usr/bin/being-brainstem
```

The accelerated face targets 60 fps. Measure frame interval median, p95
and long gaps at idle and during a repeatable voice/model turn. Record
package power where available, fan speed, temperature and CPU load in
matched AC and battery runs. This measures CPU and GPU sharing on the
physical hardware. See the [hardware capture workflow](HARDWARE.md#run-an-on-device-experiment).

Record cold boot to first visible eyes separately from warm lid-open
latency. Across 30 warm cycles on AC and battery, include rapid reclose,
mute and a stopped cortex. Use a short high-frame-rate panel video and
`mind.hardware_timing` to collect lid-open to first visible frame and
lid-open to `panel.ready` separately. Aim for p95 visible wake within
500 ms and a completed gesture within 1.5 seconds of the first visible
frame. Confirm the panel stays black when the lid is closed and check
that the wake cue runs once per confirmed open.

```sh
PYTHONPATH=/usr/lib/being python3 -m mind.hardware_timing --seconds 120
```

## Try the lid and body

Start the serial lab with `python3 lab/vm.py`. For an existing VM disk from before
L5, apply the signed update below first. Enter `/lid close` to blank the
face and stop the current turn. Enter `/lid open` to see the eyes
open unevenly, blink, and settle. The gesture runs with the model stopped.
A click or tap makes the eyes glance down. Enter `/battery 37` and ask
“How much charge do you have?” to check the simulated battery. `/plug`
sets the simulated charger.

The appliance build reads the Linux lid switch and AC and battery devices.
Closing the lid, or losing its reading, blocks microphone capture and
speech. F9 can recover the face when the lid state is unknown. The mic
stays closed until the lid is confirmed open. The backlight is restored
after the renderer presents its first frame. Lid sleep is handled in
userspace; the physical MacBook's panel, power draw and suspend behavior
still need testing.

`python3 -m unittest lab.tests.test_l5 -v` exercises the visible wake and
audio shutdown, and uses a virtual Linux input device to test the same
lid reader the appliance uses.

## Try camera presence and gaze

The camera starts disabled. From the PipOS maintenance shell as root,
`being-camera enable` records the owner's preference on `/mind` and starts
the eyes service. `being-camera status` displays permission and service
status. `being-camera disable` removes permission and stops the service.
Service status can be active while the detector is waiting for permission
from the brainstem or for the camera device to become available.

The detector uses `/dev/video0` by default. On the physical MacBook, verify
that this is the built-in camera, then check that its hardware indicator
follows capture when the lid opens and closes, permission changes, or the
device disappears. With a face in view, Pip's eyes should track its center.
Speech still requires the wake phrase. Image data lives only in the local
detector. The gaze message carries a position and expires if the detector
stops reporting.

The lab case replaces the detector in a disposable guest with a fixture
that emits known coordinates. It exercises the production permission
command, systemd unit, brainstem and face-event path, including releasing
the capture child on lid close and opt-out. It also checks that the guest
has the UVC driver and classifier installed.

```bash
BEING_L6_IMAGE=out/images/beingos-l6.img python3 -m unittest lab.tests.test_l6 -v
```

The staged L6 image passed the guest case. The eyes moved 30 pixels toward
the fixture and returned within 2 pixels of center on opt-out. Lid close
reaped the capture child, and reopening restored tracking. The native
detector correctly handled the positive and blank JPEG fixtures and
discarded truncated and invalid inputs. Guest test (`out/logs/l6-qemu.log`) · Native fixtures (`out/logs/eyes-detector-native.log`) · [Pinned pico detector and licence](../os/package/being/PICO-PROVENANCE.md).

## Compare local recall decisions

`python3 -m lab.decision_eval` runs Pip's current parser and SQLite recall
against a synthetic conversation archive. It substitutes a fixed reply for
the generative model so it can measure routing and source selection directly.
Explicit commands and explicit recall are scored separately as parser controls.

To include a decision model, start a llama.cpp server with its local GGUF and
an alias. For example, start Laya with one CPU thread, then run the comparison
from the project directory after the server is ready.

```bash
llama-server -m /path/to/Laya-Q8_0.gguf --alias laya --host 127.0.0.1 --port 8381 -ngl 0 -t 1 -tb 1 -c 1024 -b 1024 -ub 1024 -np 1
```

```bash
python3 -m lab.decision_eval --local laya=http://127.0.0.1:8381 --json out/logs/decision-eval.json
```

Repeat `--local ALIAS=LOOPBACK_URL` to score additional models on the same cases.
Stop the model servers after the run. The report includes correct routes,
correct archive sources, ordinary turns routed to history, and decision time.
Decision time includes the candidate search and excludes speech, reply
generation and cold model loading. The models can select only from the same
small shortlist produced by SQLite. TypeSafe's Jev is a hosted model. The
default run and the loopback comparison use only local inputs and services.

### Retest the OpenJev second opinion

With the independent OpenJev-1.7B checkpoint available on disk, start its
`wev-ai` server on loopback using a Python 3.13 environment with `wev-ai`
0.2.2, PyTorch 2.15.0 and Transformers 5.23.0. Use the checkpoint revision
`7418ae8b6e3b6a6507a8a58729ae491cd1c732d9` for comparison with the saved run.
This starts the CPU server in the foreground. Stop it after the test.

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 \
  python -c 'import torch; torch.set_num_threads(1); torch.set_num_interop_threads(1); from wev.serve import main; main()' \
  --model /path/to/openjev-1.7b --host 127.0.0.1 --port 8382 --device cpu --dtype fp32 --max_branch 2048
```

```bash
python3 -m lab.recall_second_opinion --local openjev-1-7b=http://127.0.0.1:8382 \
  --json out/logs/recall-second-opinion-holdout.json
```

The benchmark preserves the fixed 0.8 cutoff and consults OpenJev only
when the baseline selected archive context on a non-command turn. It
records any lost baseline-correct source by case. The deadline rows
replay the measured response times with baseline fallback. SQLite has
already fetched context by this stage, so the measured reduction is in
archive context passed to the conversation model. This runs against a
fresh synthetic archive and leaves the live mind out of the experiment.

For physical timing, first run the same benchmark on native Linux to
verify the runtime and measure decision latency and memory. Measure
final-transcript to first audio with the decision off and on, with the
face and speech services loaded, to see the cost to a real turn. The
current appliance image needs a separate compatible Python runtime to
run this particular model server. Timing under x86 emulation on the Mac
Studio cannot predict timing on the MacBook.

### Try quantized OpenJev for consolidation

The standalone pack in `out/openjev-opt/pack` contains the Q4_K_M
backbone, the original pointer head and tokenizer, and upstream
provenance. `python3 -m lab.export_openjev_pack --help` describes how to
create the pack from the pinned upstream weights and llama.cpp `b12359`.

Build the dedicated inference bridge from an unmodified `b12359` source
tree. Use `native` for the development Mac and `ivybridge` on an x86-64
Linux build host for the laptop. The Ivy Bridge build uses AVX and F16C
with newer instruction sets disabled. It needs its own llama.cpp library
because it skips vocabulary logits and returns only the hidden vectors
used by the pointer head.

```bash
sh lab/build_openjev_bridge.sh /path/to/llama.cpp-b12359 out/openjev-opt/bridge native
```

With NumPy, Safetensors and Tokenizers available to Python, start the
loopback server in the foreground. Use `libopenjev.so` on Linux and stop
the server after the comparison.

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
  python -m lab.openjev_native --bridge out/openjev-opt/bridge/libopenjev.dylib \
  --model out/openjev-opt/pack/model.gguf --source out/openjev-opt/pack --port 8382 --threads 1 --max-tokens 512
```

```bash
python3 -m lab.openjev_consolidation_eval --local openjev-q4=http://127.0.0.1:8382 \
  --json out/logs/openjev-consolidation-eval.json
```

The evaluation uses fresh synthetic conversations to compare which
existing archive excerpts get carried forward. Pip's idle compactor
selects source IDs locally and its archive remains searchable. The
experimental server runs separately from Pip and has no write path to
the archive.

## Back up and restore a being

Generate an age key once on storage that supports private file permissions
and keep it separately from the snapshot. Save the public recipient printed
by `keygen` for future exports. Use a dated filename for each snapshot on
your selected backup drive or mounted share.

```sh
recipient=$(being-backup keygen --identity /owner-key/pip.agekey)
being-backup create --mind-dir /mind --output /backup/pip-2026-11-21.being --recipient "$recipient"
being-backup export-deletions --mind-dir /mind --output /backup/pip-latest-edits.being --recipient "$recipient"
```

Use the saved public `age1…` value for later exports. Export deletions again
after a correction or forget and keep the printed `checkpoint` value independently
of the backup drive. If the original archive survives, `being-backup
checkpoint --mind-dir /mind` reads its current value. Encrypted proof
authentication verifies file integrity. Freshness comes from the current
checkpoint you obtained independently. A matching old proof and old value
alone cannot establish whether an edit happened later.

Restore to the blank, inactive mind partition before starting the installed
system. The restore accepts an ext4 volume with `lost+found`. Supply the
latest proof and independently retained checkpoint to recover history.

```sh
being-backup restore --mind-dir /mnt/new-mind --input /backup/pip-2026-11-21.being \
  --identity /owner-key/pip.agekey --deletions /backup/pip-latest-edits.being \
  --current-checkpoint "$checkpoint"
```

When the current checkpoint or proof is unavailable, choose identity and
settings by leaving out both history options. Life ID, birth and wake
records, the chosen profile, and time zone carry over. The archive starts
empty. With verified history, an old snapshot retains unrelated
conversations and removes superseded wording. A new snapshot is needed
to recover a correction made after the selected snapshot.

The backup includes committed SQLite archive rows and their deletion
lineage. It excludes Wi-Fi and SSH credentials, network permissions,
camera opt-in, model and conversation caches, derived dream state,
reminders, the weather location and the free-form core prompt. A backup
already exported cannot be rewritten by a later forget. Retire older
encrypted copies when they no longer belong in the retention set.

An interrupted activation leaves `.being-restore-in-progress` on the
destination. Retry the same authenticated inputs on the same inactive
partition. The installed services defer using its memory until the marker
clears. Appliance plaintext scratch files use `/run` and require room for
several temporary copies of the archive. Development runs via `python3 -m
mind.backup` use the host's temporary directory unless run on PipOS.

The host fixtures cover export and restore. A disposable QEMU overlay
also ran the candidate Python files and pinned Linux age binary against
ext4. The packaged installer image still needs its Restore check.
Scheduling and paired-Mac transfer are separate integration work.
Host fixture results (`out/logs/backup-host-final.log`),
Host Rust results (`out/logs/backup-rust-final.log`),
QEMU overlay result (`out/logs/backup-qemu-overlay.log`).

## Choose conversation retention

`being-memory status` prints the saved policy. Set it before the first
Mind start or restart `being-mind.service` after changing it on an
installed appliance.

| Mode | What Pip can use and keep |
| --- | --- |
| `persistent` | The searchable local text archive, explicit facts and recent conversational continuity. This is the default for existing disks. |
| `persistent --expiry-days N` | The same archive with an owner-selected age limit. Applying expiry removes older facts, conversations and linked derived replies. |
| `explicit-only` | Explicit facts from Remember commands survive a restart. Ordinary conversation is for working context during the current run. Pip cannot search the earlier transcript archive in this mode. |
| `session-only` | Both ordinary conversation and Remember commands stay in RAM for the current Mind run. The durable facts and transcript archive are unavailable to Pip. |

```sh
being-memory set explicit-only --mind-dir /mind
systemctl restart being-mind.service
```

Switching to explicit-only or session-only preserves pre-existing rows
on the disk and masks the history the mode withholds. Returning to
persistent mode makes eligible old history searchable again. Expiry removes
records at Mind start and on UTC day rollover. Backup and restore also
apply their current age cutoff. Session-only and explicit-only skip saved conversation context,
model slot writes, and the Dream Journal. Their eligible contents carry
through backup according to the selected mode. Full archive restore
also checks the current mode recorded in the deletion proof.

Say **Start guest mode** or **Don't remember this** before talking
privately. Pip starts a separate RAM conversation with the owner archive,
working context, private core, reminders and network requests unavailable.
Say **End guest mode** to return to the owner's policy. Restarting Mind
also ends the guest session. Guest conversation stays out of the archive,
model slot files and backups. Guest mode is a local conversation control.

The focused host suite passed 135 tests. The QEMU probe ran the same
Mind source through its installed service with a simulated serial body
and local scripted model. It verified session-only clearing on a cold
reboot and recovery of an eligible persistent fact after a mode change.
Host results (`out/logs/retention-checkpoint-host.log`),
QEMU results (`out/logs/retention-qemu-overlay.log`).

## Inspect the offline Dream Journal

On a build with overnight maintenance, the local administrator can opt in
by creating `/mind/dreams-enabled`. Removing that file pauses new work.
The worker runs while Linux is awake and Pip is asleep with the lid
confirmed closed, AC connected, and a fresh CPU temperature at or below
65°C. Its window is midnight to 05:00 in the owner's configured time zone.
Each night allows up to four committed pages of at most 32 archive rows,
spaced at least 15 seconds apart in a running Mind. The count and last
committed row survive a process restart. [Sleep policy](../mind/dream_policy.py),
[Journal storage](../mind/dream_store.py).

Read the dated excerpts and episode IDs from the local console.

```bash
being-dream-journal --limit 20
```

For an existing development archive, use the same reader from the project.

```bash
python3 -m mind.dream_journal --config config --mind-dir "$BEING_ARCHIVE"
python3 -m unittest tests.test_dream_policy tests.test_dream_store tests.test_dream_journal tests.test_dream_lifecycle
```

Publication rechecks the archive revision and source rows in the same
SQLite transaction that advances the checkpoint. Corrections remove the
affected link and queue the canonical source for reconsideration. A
forget removes the link and suppresses the source from future batches.
Journal rows store the link and dates. The reader fetches excerpts from
the original owner utterances. Host evidence (`out/logs/dreams-host-final.log`).

### Check Friday recall after Saturday's pass

```bash
python3 -m lab.dream_recall_eval --repeats 11 --output out/logs/dream-recall-heldout-final.json
```

The replay builds matched synthetic archives. One copy runs the real
overnight worker through lid, sleep, power, temperature and heartbeat
events. The runner applies source edits after the first publication and
inserts Saturday morning updates after the sleep pass. It replays the
same final transcript through the regular spoken recall command and
checks the reply text and the source linked in the archive. An untimed
control verifies that opting in before the worker runs leaves the answer
and source unchanged. [Held-out fixtures](../lab/dream_recall_holdout.py).

| Set | Correct source or abstention before | Correct source or abstention after | Full expected excerpt before / after |
|---|---:|---:|---:|
| 32 compact controls | 31 | 31 | 31 / 31 |
| 16 larger archives | 7 | 14 | 7 / 13 |
| All 48 | 38 | 45 | 38 / 44 |

The seven new source matches all came from the larger archives. One
reply gave the requested splitter detail in a short excerpt. It missed
the full-wording rule. No previously correct source changed. The five
corrected sources used the new wording, and no forgotten source was
republished or cited. Three pre-existing partial replies still needed
abstention, including the unrelated hit after one forget. Per-turn results (`out/logs/dream-recall-heldout-final.json`).

Across 11 alternating repeats per condition, median time from
`hear.final` to the first `say.request` changed from 0.820 to 0.893 ms
on the development Mac. The ten cases that ran the extra journal
query changed from 0.912 to 1.176 ms median and 1.789 to 2.411 ms at
p95. The fixture uses text events and local SQLite. Speaker latency
and the installed MacBook remain separate measurements.

## Install and verify an update in the lab

Each system slot contains a read-only SquashFS with its own kernel. The EFI
partition holds GRUB's boot state and RAUC's installation metadata. Both
slots share the separate `/models` and `/mind` partitions.

The builder signs local RAUC verity bundles. The private key lives in the
builder volume and the factory image includes its certificate. This trust
root and the fixed slot partition IDs are for the single-system-disk lab.

To run the signed update checks headlessly:

```bash
lab/build.sh test-media
lab/build.sh down
python3 -m unittest lab.tests.test_l3 -v
```

To try a good update interactively, start the guest with the fixture media:

```bash
python3 lab/vm.py --shell --update-media out/images/beingos-update-fixtures.img
```

Log in as root. Once `/run/being-slot-good` contains the running slot, install
the bundle and reboot:

```bash
mount -t ext4 -o ro,noload /dev/vda /mnt/updates
rauc install /mnt/updates/good.raucb
reboot
```

RAUC writes and activates the inactive slot. GRUB records each trial before
booting it. The new slot is confirmed after 60 continuous seconds with a
healthy brainstem, required organs, and model server. If a trial cannot reach
that state, it reboots and GRUB returns to the confirmed slot after three
attempts. The lab's broken fixture uses a shorter deadline to exercise that
path. A trial can only install another update after it has been confirmed.

systemd restarts a stalled brainstem using its service watchdog. It also
feeds the virtual hardware watchdog independently. A kernel panic triggers
a reboot. Recovery from a hang before Linux can arm the watchdog still
depends on a reset or power cycle.

## Test it

```bash
python3 -m unittest discover -s lab/tests -t . -v
```

Each suite boots the image in QEMU, drives it through its serial lines, and
looks at its screen. Every test starts from a fresh copy-on-write disk, so the
built image is never modified.

| Suite | Proves |
|---|---|
| `test_l0` | Boots through UEFI and GRUB from a SATA disk; the CPU has AVX but not AVX2; clean power-off; the power button |
| `test_l1` | Power-on leads to eyes and nothing else; the face moves; a crashed face returns; a missing brainstem shows on the face |
| `test_l2` | A real model answers on the emulated CPU. Memory survives cold boots, a power cut, and a simulated body change. Compacted highlights, corrections, and forgetting are checked across four boots, including a scan of the raw virtual disk. |
| `test_l3` | Both systems are read-only; invalid update media is rejected; a bad signed update rolls back; a healthy update is confirmed; both watchdogs work; the confirmed slot survives an interrupted update; committed facts survive 20 abrupt power cuts |
| `test_l4` | A wake phrase opens attention, partial transcripts precede final text, local synthesis plays through a virtual speaker, mute closes input, and Esc stops playback |
| `test_l5` | Closing the lid blanks the face and stops audio, the face wakes without the cortex, and the Linux lid reader handles initial state, switch events and device loss |
| `test_l6` | Opt-in gaze reaches the rendered face, lid close reaps the detector, reopening restores it, and opt-out stops the service and releases gaze |

Screen captures from the tests land in `out/lab/`.

All eight L2 checks passed on 21 November 2026. Seven passed in the full
run (`out/logs/l2-final-qemu.log`). The CPU check passed in a focused rerun
after it was updated to request trace output directly
(`out/logs/l2-final-cpu-verified.log`). The guest reported AVX, and the real
model answered on the emulated Ivy Bridge CPU. The forgetting check found
the retained fact and no trace of the forgotten test word on the raw disk.

All five L3 checks and the 16 L0/L1 regressions passed on the L3 image on
21 November 2026. The lab rejected unsigned media, rolled back the broken
signed image, and confirmed the healthy update. Cutting power during a
slot write left the confirmed slot bootable. Across 20 additional cuts
during boot, after commits, and during open transactions, the same identity
and all 16 committed facts survived with clean SQLite integrity checks.
The six L2 conversation checks, including a real model reply, also passed.
See `out/logs/l3-update-final.log`, `out/logs/l3-power-regression.log`, and
`out/logs/l3-final-checks.log` for the combined evidence.

All three L4 audio checks passed on 21 November 2026. ALSA loopback verified
the wake phrase, streaming partials before the final transcript, synthesized
PCM at the virtual speaker, and the M and Esc controls. The microphone port
test checked that the guest stops requesting host audio on mute and during
playback. In a separate turn, Pip transcribed a greeting, generated a reply
with the real local model, and spoke it with Flite. See
`out/logs/l4-final-qemu.log`. Input fixtures supplied the speech in these
checks. Live microphone testing and timing on the physical laptop remain
hardware work.
The same run passed 17 boot, face, and serial-mode regression checks.

## How the virtual laptop matches the real one

| | Real laptop | Virtual laptop |
|---|---|---|
| CPU | Ivy Bridge, 2 cores / 4 threads | QEMU's `IvyBridge` model, 2 cores / 4 threads. Same instruction set: AVX, no AVX2 |
| Firmware | Apple EFI | UEFI (EDK2) |
| Disk | SATA SSD | SATA disk through the same AHCI driver |
| Sound | Intel HDA, Cirrus codec | Intel HDA, generic codec |
| Screen | 1280x800 | 1280x800 |
| Graphics | Intel HD 4000 (crocus driver) | No GPU: llvmpipe software renderer |
| Networking | Gigabit Ethernet and the MacBook's own Wi-Fi radio | Emulated `e1000e` Ethernet. A lab kernel can add simulated Wi-Fi radios. |
| Watchdog | Intel chipset | Emulated Intel 6300ESB watchdog |
| Sensors | Apple SMC | None; tested with fixtures later |

The physical MacBook is needed to measure Apple firmware boot behavior, graphics,
Cirrus audio, sensors, cooling, battery use, and native speed.

## Qualify performance on the MacBookPro9,2

Use the [installed MacBook development guide](HARDWARE.md) to collect
matched baselines and candidate metrics over the owner's LAN.

The staged tuning build can be opened in its own QEMU guest.

```sh
python3 lab/vm.py --image out/images/beingos-ivybridge.img
```

Five focused checks passed against this image. Both gzip SquashFS slots
booted with the same identity and stored fact. The face appeared cleanly,
the lid stopped audio and the panel woke with the model server stopped.
With the real model, the guest warmed 187 verified prompt tokens and
spoke a response through ALSA after transcribing recorded speech. These
checks exercised the emulated Ivy Bridge path.
Verification (`out/logs/ivybridge-verification.log`),
Image manifest (`out/logs/ivybridge-image-manifest.json`).

The hardware target is two Ivy Bridge cores with four hardware threads,
16 GiB of RAM, Intel HD 4000, and a 256–512 GB SATA SSD. Keep Apple EFI and
GRUB for bring-up. Upstream coreboot has no MacBookPro9,2 board port, and
the community MacBookPro9,1 port is for the 15-inch board. Apple firmware
or Intel microcode updates can address errata. AVX2 and FMA require
different CPU hardware. Record the installed microcode before evaluating
an early Linux load. [Apple model identification](https://support.apple.com/en-au/108052),
[Coreboot Apple boards](https://github.com/coreboot/coreboot/tree/main/src/mainboard/apple),
[15-inch port](https://github.com/jegly/coreboot-mbp91),
[Linux microcode loader](https://docs.kernel.org/arch/x86/microcode.html).

Use i915 and Crocus for the face, and use the CPU for inference. Mesa's
HasVK omits the 16-bit storage feature that the pinned llama.cpp Vulkan
backend requires on Ivy Bridge. The pinned OpenCL backend requires
OpenCL 2.0 and Ivy Bridge's archived Beignet driver provides OpenCL 1.2.
Rusticl takes its OpenCL FP16 capability from the Gallium driver.
Crocus does not advertise the shader FP16 capability that the pinned
backend also requires.
Any experimental compute backend needs a measured win with the face and
voice active, since the CPU and integrated GPU share memory bandwidth and
the package power budget. [Mesa HasVK device features](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-26.2.5/src/intel/vulkan_hasvk/anv_device.c#L398),
[Vulkan requirement](https://github.com/ggml-org/llama.cpp/blob/v1.0.0/ggml/src/ggml-vulkan/ggml-vulkan.cpp#L4919-L4923),
[OpenCL requirement](https://github.com/ggml-org/llama.cpp/blob/v1.0.0/ggml/src/ggml-opencl/ggml-opencl.cpp#L9091-L9118),
[Beignet platform support](https://github.com/intel/beignet),
[Crocus shader capabilities](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-26.2.5/src/gallium/drivers/crocus/crocus_screen.c#L139),
[Rusticl FP16 capability](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-26.2.5/src/gallium/frontends/rusticl/core/device.rs#L925).

Start with read-only inventory from a Linux development USB. Record the
board and EFI revision, exact CPU and microcode, DIMM sizes and speeds,
the CPU frequency and idle drivers, and the SSD model and negotiated
AHCI link speed. A general-purpose Linux live USB can supply `dmidecode`,
`lspci -nnk`, `lsblk -D`, and SMART tools. On PipOS the kernel's
readings are available locally through sysfs and the journal.

```sh
cat /sys/class/dmi/id/product_name /sys/class/dmi/id/bios_version
grep -E '^(model name|stepping|microcode|flags)[[:space:]]*:' /proc/cpuinfo | sort -u
cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list
cat /sys/devices/system/cpu/cpuidle/current_driver
cat /sys/devices/system/cpu/cpufreq/policy*/scaling_driver
cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor
grep -H . /sys/class/hwmon/hwmon*/name /sys/class/hwmon/hwmon*/temp*_input \
    /sys/class/hwmon/hwmon*/fan*_input
journalctl -b -k | grep -Ei 'SATA link up|i915|thermal|throttl'
journalctl -b -u being-face | grep 'face: drawn by'
```

Record which sensors exist before using their data. Match temperatures
to their `hwmon` names and labels. The Intel RAPL package energy counter
in `/sys/class/powercap` can estimate CPU package watts from its change
over a timed interval, accounting for wraparound. Use a wall meter or
battery telemetry for whole-machine power. Record useful idle residency,
fan speed, temperature, clock speed and throttling at idle and through
30 minutes of repeated turns. Run the same test on AC and battery.
[Intel idle](https://docs.kernel.org/admin-guide/pm/intel_idle.html),
[Intel P-state](https://docs.kernel.org/admin-guide/pm/intel_pstate.html),
[Core temperature](https://docs.kernel.org/hwmon/coretemp.html),
[Power capping interface](https://docs.kernel.org/power/powercap/powercap.html).

Keep the model, turn prompts, face and voice fixed for the first CPU A/B.
Compare 1, 2 and 4 generation threads while holding prompt threads at 2,
then tune prompt threads with the best generation result. Interleave
baseline and candidate runs after the same warmup. Include a direct
reply, a short model turn, a longer turn, a sourced recall, a cancel
during thinking, and a lid wake. Record median and p95 time from final
transcript to useful speaker audio, prompt cache hits, tokens per
second, dropped frames or audio underruns, and sustained temperature.
Measure power-button-to-face and face-to-first-model-reply separately.
Use a spare drive for discard readback tests and keep SQLite durability
and the existing `/mind` discard behavior in the performance baseline.
[Transcript timing harness](#reply-timing),
[Physical speech and storage checks](../DESIGN-AND-PLAN.md#phase-1-benchmarks).

The service starts with two threads each for prompt processing and
generation. For a trial, change one value in a volatile systemd drop-in,
restart the service, and let Pip reach its usual warm state before
recording. The drop-in expires at reboot.

```sh
mkdir -p /run/systemd/system/being-cortex.service.d
cat > /run/systemd/system/being-cortex.service.d/90-thread-trial.conf <<'EOF'
[Service]
Environment=BEING_CORTEX_THREADS=4
Environment=BEING_CORTEX_PROMPT_THREADS=2
EOF
systemctl daemon-reload
systemctl restart being-cortex
```

Trial a smaller prompt batch independently if canceled work delays the
next reply. The pinned server accepts `LLAMA_ARG_BATCH` and
`LLAMA_ARG_UBATCH` in the same drop-in. A matched long-prefill native
probe shortened slot release with both set to 128. Cold replies varied,
so collect warm and cold quality as well as wait time on the MacBook.
[Cortex service](../os/package/cortex/being-cortex.service).

### Prompt-only cache warmup

The pinned Cortex server samples one token per warmup request even when
PipOS requests zero. An experimental llama.cpp patch skips sampling for
the simple native completion used to warm the single CPU-only Qwen3.5 slot.
In four alternating pairs on the ARM development Mac, median warmup of
187 prompt tokens in six requests fell from 752 ms to 712 ms (5.27%).
The candidate sampled zero tokens. Greedy replies and cache accounting
matched before and after save and restore, including restores across
builds and after reusing or changing a prefix.

The same comparison measured a longer median slot release after canceling
a 2,691-token request, from 7.70 s to 8.17 s. Keep the patch out of release
builds pending a matched MacBookPro9,2 run that measures warmup, interruption,
and final-transcript-to-speaker latency with the face and voice active.
The experiment's two matched binaries and comparison script are under
`out/cortex-cache-only/`.
[Experimental patch](../lab/cortex-cache-only.patch),
Native comparison (`out/cortex-cache-only/matched-native.json`).

To test the patch in a separate Buildroot workspace, copy it into that
workspace's `os/patches/cortex/` directory. Run
`lab/build.sh make -j4 cortex-dirclean` before rebuilding the image so
Buildroot unpacks the pinned source and applies the patch. Repeat the clean
build after removing the patch to obtain a stock comparison image.

### Verify appliance timeouts

On a rebuilt system, check the startup entry with
`journalctl -b -u being-brainstem | grep 'brainstem: .*timeouts'`.
On the MacBook, expect `standard timeouts, think 120s, speak 60s`.
The lab harness sets a QEMU firmware marker for serial, loopback, or
microphone runs and selects `QEMU lab timeouts, think 1800s, speak 900s`.
The lab body package can be present in the physical image. The operating
mode comes from the firmware marker at runtime.

### Archived conversation searches

For up to six matches, the archive ranks eligible episodes before building
the final excerpts. Queries that exclude sources of saved facts use a
partial SQLite index on the source ID. Both changes preserve the archive's
existing ranking, date bounds, and excerpts. Larger and unlimited searches
use the original ranking query.

On the development Mac with the pinned SQLite 3.53.4 source, a synthetic
search across 24,000 equally ranked long entries fell from 123 ms to
19 ms median and from 130 ms to 21 ms at p95. Ordinary query medians
changed by less than 1 ms in the same run. The fact-source index also
helped filtered lookups in a synthetic dense archive. Physical qualification
still needs cold reads after boot and warm searches while voice is active.
Pinned SQLite comparison (`out/archive-recall/combined-sqlite3534.json`),
Fact-source comparison (`out/archive-recall/fact-source-index-dense-native.json`).

## Proposed QEMU tests for faster replies and Wi-Fi

This is a research plan. Use one disposable guest at a time, with a fresh
overlay for cold trials and an explicit deadline for each batch. Record the
image and model hashes, QEMU version, CPU and memory configuration, and the
settings under test. The current checkout defaults to final-only inference.
Check what the test image actually ships before treating it as the baseline.
[Factory settings](../config/being.json), [VM harness](../lab/vm.py).

### Reply timing

The image sends completed turns to the model by default. Startup can warm
up to 256 verified prompt tokens in 32-token steps. A standalone greeting
can speak while warmup continues. A turn that needs the model cancels
warmup before submitting its request. If the model has just started
decoding a chunk, releasing its slot can still take time. The
experimental `prefill: true` setting permits one read-ahead attempt with a
verified warm prefix, a 24-new-token limit, and a 500 ms client deadline.

For the text-side comparison, point the transcript harness at an isolated
single-slot llama-server with a disposable cache directory. It resets that
slot between trials.

```bash
python3 -m lab.turn_latency --cortex http://127.0.0.1:8377 --repeats 3 --warm
```

The harness reports time to first content and first speakable clause,
prompt cache reuse, and slot release after a canceled prefill. To measure
the separate speech path, the L4 real-model test timestamps the final
transcript and the first nonzero PCM captured from the virtual speaker.

#### Spoken turn timing and cadence

Replay a known WAV through ALSA and time the last audible input sample,
final transcript, first speakable clause, playback launch, and first
captured speaker PCM on the same guest clock. For a model-free reference,
use `--case time`. The `sea` fixture scripts two clauses and an inline
private note. It runs the real recognizer, Mind, Flite and ALSA, checks
the two expected clauses, and records internal output silences at 10 ms
resolution. The fixture excludes model generation time from the speech
comparison. [Voice latency runner](../lab/voice_latency_qemu.py).

```bash
python3 -m lab.voice_latency_qemu --image out/images/beingos-voice-latency.img \
  --case sea --baseline-voice "$BEING_BASELINE_VOICE" --repeats 3
```

Point `BEING_BASELINE_VOICE` at the previous image's `voice.py`. The runner
checks the baseline and packaged source hashes in one disposable guest.
It reverses the order on alternating pairs. Keep the input, reply and
voice settings fixed. The longest silence includes any padding Flite adds
within or between sentences. Native speech replay put the
shipped median end-to-transcript at 340 ms. A more aggressive cutoff
saved 60 ms but split a 330 ms hesitation. The staged build retains the
shipping cutoff. Endpoint replay (`out/l4-endpoint-replay.json`).

In the alternating Ivy Bridge guest replay, the median longest gap fell
from 860 to 490 ms. Median input end to first PCM was 1.39 s for the
baseline and 1.32 s for the candidate. An earlier grouped run also
shortened the gap, from 880 to 490 ms, but the candidate took longer to
start speaking. Most of that extra time was before the final transcript.
Each comparison used three turns per variant. The repeatable change was
the shorter pause between sentences. The trials used a scripted reply
under emulation. Complete traces (`out/logs/voice-latency-qemu-comparison.json`).

The pinned Flite 2.2 replay found that the same PCM detector misses
210 and 220 ms at the start of the two synthesized WAVs. Flite's segment
timing labels the first 184 and 224 ms as pause. The measured guest
playback-launch interval includes those quiet edges and the capture
pipeline. Use the WAV timing alongside ALSA playback position before
changing the buffers. Amplitude-based trimming can also remove quiet
consonants. A WAV-only trial kept 40 ms of initial pause and left samples
from the first modeled speech segment onward byte-identical. At the
normal rate it removed median pauses of 166 ms for SLT, 104 ms for RMS,
and 213 ms for AWB. KAL16's reported durations differed from the WAV
by 95–141 ms, so it was excluded. Test the new onset against real
playback before enabling it by default.
Native Flite measurements (`out/flite-edge-profile/measurements.json`),
WAV trial (`out/ivybridge-qualification/flite-leading-trial.json`).

The voice service has an opt-in first-clause trial for SLT, RMS and AWB.
It asks Flite for segment times with the WAV and keeps the original WAV
if those times cannot be matched to the file. Later clauses keep their
existing cadence. In 44 matched default-voice pairs on the development
Mac, the WAV detector signaled speech 160 ms earlier at the median and
the work before handing playback the WAV increased by 0.47 ms. This
comparison ran the voice service's synthesis path with a silent WAV
reader in place of ALSA. Matched native run (`out/voice-first-pause/matched-native.json`).

To compare both settings on one rebuilt image, use the same fixture,
voice and ALSA path for each pair.

```bash
python3 -m lab.voice_latency_qemu --image out/images/beingos.img \
  --case sea --first-pause-trial --repeats 3
```

For a physical trial, capture the baseline before enabling the following
volatile drop-in. Keep the same built-in speaker, voice, rate and
prompts across both settings. Pair the timing capture with external
audio so the first useful word can be measured. Include short words
starting with fricatives or stops and check Esc and lid closure.

```sh
mkdir -p /run/systemd/system/being-voice.service.d
cat > /run/systemd/system/being-voice.service.d/80-first-pause-trial.conf <<'EOF'
[Service]
Environment=BEING_VOICE_SHORTEN_FIRST_PAUSE=1
EOF
systemctl daemon-reload
systemctl restart being-voice
```

Remove `/run/systemd/system/being-voice.service.d/80-first-pause-trial.conf`
and repeat the reload and restart to return to the baseline. The trial
defaults to off. [MacBook timing capture](HARDWARE.md#run-an-on-device-experiment).

In a matched Mac Studio run through the mind, the local greeting path
reduced exact greeting echoes from 7 of 21 to zero. Median time from the
final text to the first speakable clause was 254 ms with the model and
0.44 ms on the local path. All 21 ordinary conversation, quote, time,
and sourced recall controls produced the same replies as before. Median
clause time for the 15 model-backed controls was 281 ms before and 274 ms
after. The model still repeated the rainbow question in one mixed turn
and sidestepped it twice. This was a written-reply check before synthesis.
Matched transcripts and timings (`out/logs/greeting-comparison-native.json`).

With prerecorded input in QEMU, the greeting reached the virtual speaker
1.80 s after the final transcript. The same boot warmed 187 prompt tokens
and then spoke a Qwen reply to a different, open-ended question in 53.65 s.
Both measurements used captured ALSA PCM under emulation.
QEMU voice run (`out/logs/greeting-qemu.log`).

1. **Isolate the model queue.** Replay the same short, long, hesitant, and
   revised transcripts against one slot on the same Ivy Bridge guest and
   GGUF model. Start with matched final-only and legacy read-ahead cases,
   then test legacy read-ahead with cancellation, verified warm-prefix
   final-only, and bounded read-ahead. Keep sampling and prompt settings
   fixed. A test server that holds prefill at a known point can exercise
   the mind's cancellation quickly. Repeat the case with real llama.cpp
   to verify server-side slot release. Use the existing transcript harness
   as a starting point. It needs emulation-appropriate timeouts and a
   dedicated server because it resets slot 0. Watch `/slots` or the server
   trace as a speculative request disconnects. Record when its server slot
   becomes available, when the final request begins, and how many prompt
   tokens the final request reuses. Check cache hits on both a warm turn
   and a restart from saved state. [Transcript harness](../lab/turn_latency.py),
   [Pinned llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/v1.0.0/tools/server/README.md).
2. **Verify the voice path.** Reuse L4's fixed 16 kHz audio recordings and
   ALSA loopback. Use one clock in the guest for end of the voiced input,
   `hear.final`, model queue and first token, first speakable clause, and
   the first sustained speech signal captured from the speaker loopback.
   Establish input and output clock alignment with a fixture marker.
   `say.started` marks playback launch, and `say.level` describes the source
   WAV. Use captured PCM for the playback measure and confirm the words
   belong to the expected reply. Include a time or explicit memory command
   as a model-free control, a sourced recall, a revised transcript, and an
   interrupted turn. Check that the cancelled turn produces no late audio
   and that correct recall and forgetting still pass. [L4 audio harness](../lab/tests/test_l4.py),
   [Voice implementation](../mind/voice.py), [ALSA timing](https://alsa.alsa-project.org/alsa-doc/alsa-lib/pcm.html).

Start with a short and a revised case in each of the first two configurations.
Expand only after confirming the expected queue state. Alternate the test
order and retain per-run timings, ranges, and timeouts. Use QEMU to verify
queue ordering, cache reuse, command bypass, and the complete speech path.
Use a larger repeated sample on the MacBook to judge median and p95
end-of-speech to first useful audio. The earlier 168.4-second trace began
at the final transcript and located a queueing delay under emulation.
Earlier voice trace (`out/logs/l4-cortex-latency.log`),
[QEMU accelerator reference](https://www.qemu.org/docs/master/system/introduction.html#virtualisation-accelerators).

#### First matched replay

On 21 November 2026 one short transcript ran in three modes in the same
Ivy Bridge guest with the Qwen3.5-0.8B Q4_K_M model and one llama.cpp slot.

| Mode | Final transcript to first clause | Cached prompt tokens |
|---|---:|---:|
| Legacy read-ahead | 193.1 s | 209 |
| Cold final-only | 157.5 s | 0 |
| Final-only with a verified warm prefix | 46.7 s | 187 |

The legacy request spent 156.7 s on its speculative read. Preparing the
verified warm prefix took a separate 166.7 s. The warm path shifts work
earlier so it can be reused across later turns. All three replay replies
echoed the input, so the timing measures the first speakable clause.
The separate L4 integration test reached actual ALSA output at 65.7 s
after the final transcript and produced a different reply. One emulated
sample per mode on a shared host is useful for diagnosing the queue.
The physical machine sets the latency and quality budgets.
Replay results (`out/logs/reply-qemu.json`), Full voice smoke test (`out/logs/foreground-qemu.log`).

### Conversation model audition

`lab.model_audition` starts a disposable single-slot llama-server on
loopback and runs the production Mind with a temporary SQLite archive.
Use the pinned llama.cpp build `b12359-b1b770d90`. The shipped profile
uses the factory Qwen sampling. The Liquid profile uses the publisher's
temperature 0.1, top-k 50 and repetition penalty 1.05. Use the official
350M checkpoint with the checksum in the provenance record.
Candidate provenance (`out/model-audition/lfm350m-provenance.json`).

```bash
python3 -m lab.model_audition --server "$BEING_AUDITION_SERVER" \
  --model out/models/reflex.gguf --profile shipped \
  --output out/model-audition/qwen-native-rerun.json
python3 -m lab.model_audition --server "$BEING_AUDITION_SERVER" \
  --model out/model-audition/LFM2.5-350M-QAD-Q4_0.gguf --profile liquid \
  --output out/model-audition/lfm350m-native-rerun.json
```

The original report used eight intents with three fixed seeds. The
current runner has seven model cases because Pip now answers a direct
name question locally. The runner records the text and time of every
clause, the emotion event, and the actual note
sent to the model for the grounded turn. The greeting, name, time, battery,
vision, remember, recall, correct, and forget controls stay local. The
runner checks their replies and verifies that the forgotten fixture is
gone from the archive. A separate greedy probe checks cache hits after
warmup and save, erase, and restore. To test a prompt change, pass
`--identity FILE --holdout` and run the same holdout cases against the
factory identity as well. To test the effect of body telemetry on the
model, compare the default run with `--senses ''` while retaining the
same identity, seeds, and recall fixtures.

| Model | Complete replies with no hard failure | Replies with a useful clause | First clause p50 / p95 |
|---|---:|---:|---:|
| Shipped Qwen3.5-0.8B Q4_K_M | 6/24 | 11/24 | 257 / 417 ms |
| Liquid LFM2.5-350M QAD Q4_0 | 0/24 | 3/24 | 61 / 88 ms |

Blind review marked echoes, fragments, and unsupported claims separately
from partial answers. Both models emitted emotion events and reused
their verified prefixes after restore, 187 and 194 tokens respectively.
The two-thread Mac Studio timings end before speech synthesis, and include
any short fragment sent to the voice. The appliance keeps Qwen.
Written replies, blind review, and timings (`out/model-audition/native-comparison.json`).

A shorter identity got the 350M model to answer the diagnostic rainbow
question. In six fresh cases across three seeds, neither identity gave a
complete, fully grounded answer. Both versions overlooked the supplied
rehearsal hall and invented a name for the neighbour's dog. The shorter
identity remains an experiment. Blind holdout results (`out/model-audition/holdout-comparison.json`).

#### Body telemetry comparison

With the same Qwen checkpoint, sampling, identity, and recall notes,
omitting routine body readings cut median time to the first clause from
288 to 210 ms on the original set and from 277 to 218 ms on the holdout.
Blind review found 7 versus 3 complete clean answers on the original
24 turns and 1 versus 4 on the 18 holdout turns. Hard failures totalled
25 across both sets in each configuration. The shipped prompt still
receives the readings. Replies, blind scores, and timings (`out/model-audition/qwen-telemetry-comparison.json`).

#### Larger Liquid candidate and a direct-answer prompt

The pinned Liquid LFM2.5-1.2B-Instruct QAD Q4_0 model is 696 MB, compared
with 533 MB for Qwen. Run it with the same `liquid` profile. The
direct-answer prompt uses the shipped Qwen sampler and unchanged
telemetry and recall notes. In the comparison below, the local name
command is outside both model sets. LFM 1.2B provenance (`out/model-audition/lfm12b-provenance.json`),
Trial prompt (`out/model-audition/answer-first-identity.md`).

| Variant | Clean complete replies (original / holdout) | Clean answers using the supplied location | First clause p50 (original / holdout) |
|---|---:|---:|---:|
| Qwen with the shipped prompt | 5/21 / 1/18 | 1/6 | 311 / 277 ms |
| Qwen with the direct-answer prompt | 4/21 / 2/18 | 2/6 | 337 / 303 ms |
| Liquid LFM2.5-1.2B-Instruct QAD Q4_0 | 9/21 / 3/18 | 6/6 | 428 / 252 ms |

The 1.2B model doubled the count of clean complete replies overall and
gave the right location in all six grounded trials. It still had hard
failures in 13 of 18 holdout replies. The direct-answer prompt increased
hard failures on both sets, including spoken copies of notes. Both
experiments stay in the lab. Blind review and per-turn results (`out/model-audition/reflex-variants-comparison.json`).

#### Voice loop in QEMU

The separate audio harness boots a fresh overlay for each model and
replays the same “Hey Pip”, sea question, and local time control. It
checks the candidate's SHA-256 before boot and after copying it into the
guest. It records the active model, sampling settings, cache warmup,
spoken wording, and time from final transcript to first captured PCM.
Use `--models lfm` for a candidate-only rerun. With `--output`, each
finished guest is saved before the next one starts.

```bash
python3 -m lab.model_voice_qemu out/model-audition/LFM2.5-350M-QAD-Q4_0.gguf \
  --output out/model-audition/voice-qemu-report.json
```

In the Qwen reference guest, Pip warmed 187 tokens and reached virtual
speaker PCM 47.38 s after the sea question and 1.16 s after the time
question. The first Liquid guest verified the 350M weights and started
its server. The warmup check timed out before it reached the voice
fixtures. These are emulated Ivy Bridge runs. Qwen capture (`out/model-audition/voice-qemu.json`),
Liquid warmup result (`out/model-audition/voice-qemu-candidate.json`).

The fresh Liquid retry confirmed its active model path and sampler.
Its first 32-token prefill batch took 146.57 seconds under emulation.
Pip transcribed the sea fixture, then the 600-second guest budget expired
while the runner waited for playback. That budget includes boot, model
installation, and prefill. The run gives no time-to-audio measurement for
Liquid. QEMU comparison and server trace (`out/model-audition/voice-qemu-comparison.json`).

### Audition speech recognition and synthesis on Ivy Bridge

First run the compatibility check of the voice's own engines on the released sherpa-onnx v2.0.3 Linux x64 C API with its ONNX Runtime 1.28.2 library. It boots a disposable overlay of the Ivy Bridge image under QEMU `-cpu IvyBridge` (AVX, no AVX2, FMA or BMI), checks the guest's CPU flags, and runs each engine in its own Python process through the same ctypes bindings the voice uses (`mind/sherpa.py`): Silero VAD over the Moonshine test recording, a Moonshine v2 tiny decode of its `test_wavs/0.wav` (24 kHz, given at its own rate), and one sentence in the Piper voice. A stage killed by SIGILL is named in the report. The assets are the local downloads in `out/voice`, checked against the pinned hashes; nothing is fetched. `--fetch-only` only verifies them, and `--prepare-only DIR` stages and checks the media without booting a VM. QEMU's speed says nothing about the laptop's, so timings are recorded as emulated. [Asset revisions, sizes, and hashes](../lab/sherpa_avx_assets.json), [Runtime release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/v2.0.3).

```bash
python3 -m lab.sherpa_avx_smoke --fetch-only
python3 -m lab.sherpa_avx_smoke --prepare-only /tmp/sherpa-media
python3 -m lab.sherpa_avx_smoke
```

The report goes to `out/sherpa-avx/ivybridge-smoke.json`. This v2.0.3 run is pending: it needs a lab image built from the current source. An earlier check of the v2.0.2 archive with a public English Zipformer 20M checkpoint initialized both its FP32 and INT8 exports and returned the same transcript for a 6.625-second clip in QEMU 11.1.1; that recogniser has since been dropped for Moonshine.

For the MacBook comparison, start with the packaged PocketSphinx 5.0.4 and Flite 2.2 SLT at the shipped rate, with first-pause trimming off. Change one stage per comparison. Pair runs in alternating order across a cold service start and warm turns. Keep the other stages, ALSA configuration, room and playback level fixed. Start with a local time or recall question to isolate speech processing, then use the same warm Qwen reply settings for conversational turns. Record the exact package, weights, CPU flags, thread count, cache state, and power source.

| Stage | Comparison and fixtures | Record |
| --- | --- | --- |
| Recognition | Replay matched 16 kHz mono clips through PocketSphinx and both Zipformer exports with common end boundaries first. Then feed paced frames through each streaming path. Try one and two inference threads while the face runs. Include quiet and fan noise, uncommon names, corrections, short commands, and a 330 ms hesitation. | Time to partial, last audible sample to final transcript, word error rate, correct names and commands, split and missed turns, peak RSS and idle CPU. |
| Wake and turn end | Measure the current wake and endpoint settings before trying a keyword model or VAD. Include “Hey Pip”, “Hey, pick a number”, wake followed by a same-breath command, the follow-up window and a renamed wake phrase. Include no-wake periods, a mute, a lid close and a speak-over attempt. | Missed wakes, false wakes per hour, early cuts, endpoint delay and whether a closed lid or mute keeps capture off. Keep missed turns in the denominator. |
| Synthesis | Use the same accepted first clause, reply punctuation, nominal pace and matched loudness. Compare stock Flite with its optional first-pause trim before auditioning another voice. Include quiet initial consonants, proper names and a two-clause reply. | Clause-ready to first intelligible sound, longest inter-clause gap, cutoff behavior, pronunciation and blind preference. |
| Full turn | Use a shared acoustic clock for the input and speaker on the physical laptop. Keep the face active, repeat on AC and battery, and sample after a sustained 30-minute mixed workload. | Report median and p95 from the last audible user sample to the first intelligible, useful reply, alongside all failures. Compare to the Phase 1 goal of 3 s or less and report CPU, heat, fan and power separately. |

The QEMU check exercises the upstream WAV CLI. A Pip integration trial needs one resident streaming recognizer behind the existing voice path so frame delivery, end-of-turn, cancel, lid and mute can be tested. Keep PocketSphinx and Flite as the bootable defaults while qualifying any replacement. Use public or synthetic audio for the pre-USB comparisons. Keep retention and hosted inference off. [Existing ALSA replay](#spoken-turn-timing-and-cadence), [Physical measurements](HARDWARE.md).

| Optional comparison | First gate |
| --- | --- |
| sherpa English Zipformer 3.3M keyword spotting | Verify the exact checkpoint's redistribution terms and test fixed and renamed phrases against PocketSphinx using the same false-wake set. [KWS checkpoint](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html#sherpa-onnx-kws-zipformer-gigaspeech-3-3m-2024-01-01-english) |
| Silero VAD and Smart Turn | Trial Silero's speech boundaries and Smart Turn's completed-utterance estimate separately against the shipped endpoint. Keep the ordinary timeout available for an uncertain prediction. [Silero](https://github.com/snakers4/silero-vad), [Smart Turn](https://github.com/pipecat-ai/smart-turn) |
| Moonshine Small Streaming | Qualify the exact x86 binary and model on the same frames if Zipformer gives too many cuts or errors. Whisper.cpp `tiny.en` or `base.en` can supply an offline transcript reference. [Moonshine license](https://github.com/moonshine-ai/moonshine/blob/main/docs/license.md), [whisper.cpp](https://github.com/ggml-org/whisper.cpp) |
| Piper or Phonon 31M | Pin one voice by name and model hash and check voice redistribution terms. Gate on Ivy Bridge CPU operation, first audible word and sustained-load behavior. Compare against Flite with the same playback path. [Piper voice licensing](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/LICENSING.md), [Phonon runtime](https://github.com/gradium-ai/xn-ptts/blob/main/ptts/README.md) |

Keep the remaining design options outside the first latency matrix. TEN VAD and openWakeWord need a review of the exact package and model terms. Parakeet is an offline transcript comparison on public fixtures. Kitten TTS Nano needs its own phonemizer, runtime and first-audio check. CAM++ speaker verification needs an owner enrollment decision and separate false-acceptance, false-rejection and replay tests. [TEN VAD license](https://github.com/TEN-framework/ten-vad/blob/main/LICENSE), [openWakeWord licenses](https://github.com/dscripka/openWakeWord#license), [Parakeet v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2), [Kitten Nano INT8](https://huggingface.co/KittenML/kitten-tts-nano-0.8-int8).

### Network and Wi-Fi

1. **Exercise network outages over virtual Ethernet.** Give the existing
   `e1000e` device a stable name, toggle it with QMP `set_link`, and confirm
   the carrier state inside the guest. Use `restrict=on` for QEMU user
   networking and `guestfwd` for the local test HTTPS service. Use a test CA so
   certificate validation runs normally. Boot disconnected, reconnect, and
   exercise DNS, TLS, and interrupted download failures separately.
   Check that the face, voice, and archive stay available. Once download
   support exists, reuse the L3 signed fixtures. A truncated or invalid
   bundle must leave the confirmed slot and `/mind` intact. A complete,
   trusted bundle should follow the existing inactive-slot path.
   [QEMU user networking](https://www.qemu.org/docs/master/system/invocation.html),
   [QMP link control](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html#command-QMP-set_link),
   [L3 update harness](../lab/tests/test_l3.py).
2. **Exercise Wi-Fi joining with simulated radios.** Add
   `mac80211_hwsim`, `hostapd`, and network fixtures to a lab-only image.
   Create both simulated radios in the guest's initial network namespace
   so they share a simulated medium. Move the access-point radio into an
   isolated namespace that runs its DHCP and test services. Have the
   PipOS station use `wpa_supplicant` and the
   planned address service, with Ethernet disconnected. Verify the SSID,
   negotiated security, address, route through the station, and a request
   to a service reachable through the access point. Use WPA2-Personal
   first, then exercise WPA3 transition and SAE configurations where
   supported. Test a wrong password, AP restart, reconnect, and a Wi-Fi
   connection with no Internet. Finish with a voice and recall turn while
   the AP is down. [Linux simulated radios](https://wireless.docs.kernel.org/en/latest/en/users/drivers/mac80211_hwsim.html),
   [Linux 6.18 simulator](https://github.com/torvalds/linux/blob/v6.18/drivers/net/wireless/virtual/mac80211_hwsim.c).

#### First simulated Wi-Fi result

On 21 November 2026 the guest rejected a wrong password, joined the WPA2
AP, and reached an HTTP fixture over the wireless interface. With the AP
stopped, the station lost its association and DHCP address. Pip answered a
time question and recalled a stored fact during the outage. After restart
the same supplicant reassociated and systemd-networkd restored the address.
The transition and WPA3-only profiles each negotiated SAE and carried a
test request. The case ran in about 71 seconds. WAN and update-download
fixtures are separate follow-up tests. Wi-Fi result (`out/logs/wifi-qemu.log`).

Reproduce with an existing PipOS Buildroot cache. The builder creates
the separately named Wi-Fi image with the simulator tools for this test.

```bash
bash lab/wifi-build.sh
python3 -m unittest -v lab.tests.test_wifi.Wifi.test_authentication_dhcp_and_reconnect_keep_local_commands_available
```

#### Owner-managed Wi-Fi

On a PipOS shell, `iw dev` shows the available Wi-Fi interfaces. Use
the actual interface name and the access point's network name.

```sh
being-wifi configure wlan0 'My network' --security wpa2
being-wifi status
```

The command prompts for the password without echoing it. Use
`--security wpa3` for an SAE network. The service stores one protected
profile on `/mind`. In a build with the device network policy, choose
Local network or Internet explicitly so the service can associate and
obtain an address. Advertised DNS is available in Internet mode. Use
`being-network status --json` to verify the applied mode. `being-wifi
forget` disconnects and removes the profile. Voice and memory start
independently of Wi-Fi. [Network setup and recovery](HARDWARE.md#choose-the-device-network-mode)

The target kernel includes a `b43` module for physical bring-up.
The MacBook's internal radio may require separate firmware. Stage and
verify owner-supplied files offline before building the selected image.
Keep paired Ethernet available for the radio qualification.
The lab build adds simulated radios and the AP fixture.

QEMU verified WPA2 and WPA3 with the installed service, including DHCP,
DNS, reconnection after an access point restart and cold boot, and profile
removal. Pip recalled a stored fact during the outage and after the profile
was removed. The owner-managed case took about 128 seconds against an isolated
AP. QEMU result (`out/logs/wifi-managed-qemu-final.log`).

```bash
bash lab/wifi-build.sh beingos-wifi-managed.img
BEING_WIFI_IMAGE=out/images/beingos-wifi-managed.img python3 -m unittest lab.tests.test_wifi -v
```

### Physical checks

On the physical MacBook, boot a development USB and confirm its PCI radio
ID, firmware load, supported bands, authentication against the actual
router, signal and reconnect through lid events. Repeat with the intended
USB adapter if needed. Measure real speech latency with the built-in mic
and speakers, including fan noise, plus frame pacing, heat, and power with
the radio active. These hardware measurements set the release budgets.
[Wi-Fi design](../DESIGN-AND-PLAN.md#network-and-maintenance),
[Optimisation design](../DESIGN-AND-PLAN.md#optimisation-order-and-acceptance).

## Where things are

```
os/                       the image definition (a Buildroot external tree)
  configs/                what goes into the image
  board/beingos/          kernel config, bootloader, disk layout, build hooks
  package/being/          the organs and their startup units
  package/cortex/         llama.cpp's server, built for Ivy Bridge
  patches/                our fixes to other people's packages
lab/
  build.sh                drives the builder
  vm.py                   the virtual laptop and the test harness
  tests/                  one suite per milestone
```

## Things the lab has already caught

- **SDL asked for a screen format the display refuses.** SDL's KMS/DRM backend
  scans out ARGB8888. The virtual display's main plane only takes XRGB8888, and
  so do Intel GPUs before Skylake, which includes the laptop's. The screen
  stayed black. Fixed by `os/patches/sdl2/`.
- **The eyes flew off the screen at low frame rates.** The gaze spring was only
  stable at 60 frames a second. Fixed in the face, with a test.
- **Buildroot's stock llama.cpp would be built with AVX2.** It would have
  crashed on the laptop. `os/package/cortex/` sets every instruction-set flag
  itself, and `test_l2` checks the result on an AVX-only CPU.
- **Deleting a memory left its text on the disk.** SQLite scrubs its own file,
  but the write-ahead log's freed blocks kept the words. The mind partition is
  mounted with `discard`. The test overlays use 4 KiB clusters so QEMU can
  discard individual ext4 blocks. `test_l2` scans the raw disk to check.
- **Model replies could preserve part of a forgotten fact.** The mind now
  tracks which facts were provided to the model and scrubs replies and
  summaries linked to those facts when asked to forget. Older episode logs
  without fact references still use a match against the full fact text.
- **QEMU rejected the simulated MacBook's model identifier.** The harness
  now escapes commas in SMBIOS values, so the same virtual disk can boot in
  a machine reporting `MacBookPro9,2`.
