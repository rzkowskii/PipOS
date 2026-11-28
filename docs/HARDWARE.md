# Develop against the installed MacBook Pro

> **Read this first (23 Nov 2026).** This guide assumes a **maintenance** build
> with a shell and LAN access (`BEING_LAPTOP_MAINTENANCE=1`). The shipping
> laptop image and the first-boot Try stick have no shell, no SSH and no
> root login. For the first boot use [FIRST-BOOT.md](FIRST-BOOT.md), and read
> its logs with `lab/try_timings.py`. Do not install to the internal SSD yet,
> not even with the maintenance build (plan principle 3): the laptop does not
> yet have its own RAUC compatible strings and release key (plan 1.2), so a
> maintenance update could turn it back into the lab image.
>
> The CPU is recorded as an **i5-3210M** (2.5 GHz, 2 cores, 4 threads, AVX
> without AVX2), to be confirmed from `/proc/cpuinfo` in the first boot's
> `summary.txt`.
>
> **Before any wipe, under macOS (plan 0.7, owner):** install the last
> firmware update; record the battery cycle count and condition here; run
> `bclm read`, then `sudo bclm write 77`, charge, and look for "Not Charging"
> at about 80%. Record the results in this file.

Use the MacBookPro9,2 for the measurements that x86 emulation cannot
answer. The Mac Studio runs the controller, stores the evidence and builds
releases. The MacBook runs the candidate on its own CPU, screen, speakers,
microphone, battery and Wi-Fi. The controller connects directly on the
owner's LAN. Captures run on request and stay on the Mac Studio.

## Check the first USB candidate

Audit the exact raw image that will go onto the USB. This reads the image
file on the Mac Studio using 7-Zip and reports EFI console settings, build
IDs, drivers, firmware, and tools from the packaged files.

```sh
python3 -m lab.hardware_image out/images/beingos-try-install.img
```

The separate LAN-tested `beingos-hardware.img` contains Linux 6.18.64,
Broadcom `tg3`, `b43` and `bcma`, Cirrus CS420x/CS421x HDA, i915 and
Crocus, AppleSMC, coretemp, ACPI battery and lid support, backlight control,
RAPL, and the userspace tools for checking them. Its `b43_firmware` list
is empty. The existing `beingos-try.img` and LAN-tested image have a
`ttyS0` console and installer preview. For an interactive install on the
MacBook, confirm the chosen Try image offers a usable local keyboard and
display path to the installer. Try keeps the LAN listeners disabled.

These measurements require the laptop and the owner's go-ahead to boot it.
A Linux live USB can establish a read-only hardware inventory first.

| Area | First physical check |
|---|---|
| Power-on and display | Use Apple EFI to boot the selected stick. Record power-button-to-face time, i915 binding, the face service's `face: drawn by` line, and whether the panel actually blanks and reappears with the lid. |
| Wired recovery | Check the Ethernet PCI ID and `tg3` binding. After enrollment on the installed system, confirm DHCP, negotiated speed and duplex, and a pinned observation snapshot over Ethernet. Leave Ethernet available for the wireless reconnect test. |
| Wi-Fi | Record the PCI ID, actual driver and requested firmware names locally. Once firmware loads, verify association, DHCP, signal and reconnect after an access point restart and a lid cycle. Use wired management during the test so a Wi-Fi outage can be measured. |
| Speakers and microphone | Use `aplay -l`, `arecord -l` and `amixer -c CARD scontrols` to check the analog card and mixer. Test a spoken turn, mute and interrupt with the built-in mic and speakers. On the MacBookPro9,2, confirm Ears uses the mapped `PCH` capture card and PCM 0 even if ALSA card numbering changes. Test playback with `BEING_ALSA_PLAYBACK` in a volatile `being-voice.service` drop-in if needed. |
| Power and cooling | Confirm the thermals follow `coretemp` and AppleSMC reports, with fan RPM, AC state and battery discharge where available. Take an idle and warm-voice capture on AC and battery, then check sustained clocks and throttle counters during a longer repeated-turn run. |

Use `being-installer --check --json` for the kernel inventory when a
PipOS maintenance console is available. Use `lab.hardware collect`
after installation and enrollment for the private baseline. Driver
binding, PCM endpoint detection and software lid timing are reported
separately from audible speaker output and visible panel wake. [First-boot
and installer work](LAB.md#install-from-the-try-usb-in-the-lab)

### Qualify the MacBookPro9,2 body and controls

Record the image SHA-256, build ID, kernel, boot mode (Try or installed),
and power source for each run. For each organ record separately what Linux
enumerates, what changes when you exercise the hardware, and what Pip does
at the same time. The existing guest tests exercised simulated lid and
power inputs, the Linux lid reader with a virtual input device, and camera
consent with a detector fixture. The observations below are pending on the
physical MacBook. [Lid and body](LAB.md#try-the-lid-and-body) · [Camera fixture](LAB.md#try-camera-presence-and-gaze)

From a local PipOS maintenance shell, take a baseline before exercising
the controls. The sampler records named temperature channels and fan RPM,
independent mains and battery state, requested panel brightness and power,
and ALSA and lid capabilities. It samples the AppleSMC platform device
when attributes are absent from the hwmon class device. [Sampler](../mind/hardware_metrics.py)

```sh
being-installer --check --json
PYTHONPATH=/usr/lib/being python3 -m mind.hardware_metrics --seconds 30 --interval 1
aplay -l
arecord -l
awk '/^N: Name=|^H: Handlers=|^B: (KEY|ABS|SW)=/' /proc/bus/input/devices
for attr in /sys/devices/platform/applesmc*/light \
            /sys/devices/platform/applesmc*/position \
            /sys/class/leds/*kbd_backlight/brightness \
            /sys/class/video4linux/video*/name; do
    [ -r "$attr" ] || continue
    printf '%s ' "$attr"
    cat "$attr"
done
```

| Organ | Native exercise and evidence to record | Appliance behavior to verify |
|---|---|---|
| Battery and AC | Repeat the sample across unplug and reconnect. Check a normal charging state and, when available, a plugged-in full or charge-limited state. Compare `power_supply/*/online` on the mains device with the battery's `status` and `capacity`. | Ask “How much charge do you have?” after each transition and record the delay. AC online and battery charging are independent, so full on AC should report plugged in and leave the low-battery expression clear. Record the low-charge cue if the battery reaches that state during normal use. |
| Temperature and fans | Record idle, successive voice turns and quiet recovery on AC and battery. Keep the hwmon driver, `temp*_label`, value in °C, fan RPM, CPU frequency, and throttle counts. Verify which `coretemp` package/core labels and AppleSMC keys actually change. | Compare Pip's temperature report with the hottest readable `coretemp` channel. The metrics collector also sees named AppleSMC temperatures and fan speeds. Their influence on behavior remains a separate check. Record absent or unreadable channels as unknown and record the sampling interval. |
| Ambient light and panel | Read the available AppleSMC `light` channel under steady dim and bright conditions. Record the active `/sys/class/backlight` device, its `brightness`, `max_brightness` and `bl_power`, and observed panel glow. Check the built-in brightness keys through a lid cycle. | Verify lid close powers the backlight down and reopen restores the previous requested level after the first face frame. Ambient-driven brightness and drowsiness still need appliance acceptance once wired. |
| Lid and wake | Compare `SW_LID` on the named input device with panel light and actual audio during close, open and rapid reclose. Observe wake once with Cortex stopped in a controlled lab session. | Closing during speech stops playback and microphone capture. On reopen, the closed first frame precedes the wake gesture. Log lid-open-to-visible-frame time, preserved mute, and any duplicate wake or backlight flash. |
| Trackpad and keyboard | Identify the built-in trackpad and keyboard by name and event handler. On a live Linux image with `evtest`, observe `BTN_TOUCH` or `BTN_LEFT` from the intended gesture, then check whether the PipOS face receives it. Test Esc during thinking and during a longer spoken reply. | A click or exposed touch should produce a glance. Esc is the current interrupt. The separate tap-to-interrupt gate passes when a tap cancels the turn and makes actual speaker output stop as well. Record touch, physical click and Esc independently. |
| Microphone and speakers | Confirm the built-in ALSA capture/playback endpoints and mixer. Use a real spoken turn with “Hey Pip,” a follow-up, a nearby non-wake phrase and a long reply. Watch the chosen microphone's `/proc/asound/card*/pcm*c/sub*/status` while idle, speaking, muted and lid-closed. | Record audible output, wake and transcription success, and whether capture closes during speech, mute and lid close. Check M or F10 mute and Esc interrupt at the physical keyboard. Include a fan-noise run and measure stop time at the speakers. |
| Camera and indicator | Match the FaceTime HD name and device path in `/sys/class/video4linux`. With the owner's opt-in in Try, observe the physical LED and capture handle through allow, revoke, lid close and reopen. | Confirm local face tracking, LED and handle release on revocation or lid close, and that face presence without “Hey Pip” leaves speech attention closed. Eye-contact attention and deliberate descriptions have separate acceptance. |
| Balance and keyboard glow | Check whether AppleSMC exposes `position` and a keyboard-backlight LED class device. Observe the reported axes with a gentle stationary tilt and the LED level under built-in keyboard-light control. | Record any present kernel channels and whether the appliance exposes controls. Tilt response and asleep keyboard glow remain open. |

Keep observed device paths and endpoints with the results. A missing
attribute and a present attribute that stays unchanged are separate
outcomes. When a kernel channel is unavailable, record the fallback you
were able to use and any resulting appliance behavior that remains untested.

For camera recovery, restart Brainstem on the private diagnostic image
while the internal camera is active. Record the kernel USB authorization,
capture handle and LED through disconnect and recovery. Repeat with consent
revoked or the lid closed and verify capture stays off after the restart.
On a cold boot with camera consent off, record the same readings before
granting consent.

### Native trackpad motion

Use the built-in trackpad on the private maintenance image and disconnect
external pointing devices. Record the trackpad's event node and whether Linux
reports movement, physical click and tap there. Use the face's SDL pointer
probe to check the same input path through KMS. The probe reports the video
driver, event counts and the distance between the smallest and largest
observed coordinates on each axis in logical pixels.

From the [maintenance shell](#run-an-on-device-experiment), temporarily stop
the running `being-face` service so the probe can take the display. Restore
the service after the capture, including if the command fails.

```sh
SDL_VIDEODRIVER=kmsdrm /usr/bin/being-face --standalone --fullscreen --pointer-probe --exit-after 15
```

Run separate short windows for idle, a horizontal and vertical stroke, a
physical click, and a tap. Save each summary with its gesture label and image
build ID. A usable motion result shows `kmsdrm` and a measurable span on each
axis during the stroke. Compare the click and tap windows with the idle
window to see how SDL represents each gesture. This is the hardware evidence
needed for Shell Lab's pointer design decision.

### Present-on-change Try experiment

For WP-D7, add `being.face_present_on_change=1` to a single Try boot in
GRUB. On KMS the face can skip swaps when the idle eyes have barely moved
since the last presented frame. The next poll is then at 5 Hz. Active
animation, app surfaces, captions, widgets, decorated eyes and pending
panel wake continue to draw. An ordinary Try boot gives the control.

The once-a-minute face line reports skipped idle swaps. It also records the
p99 and the histogram of intervals between consecutive Active swaps. The
histogram uses upper bounds of 4, 8, 12, 16, 17, 20, 25, 33, 40, 50, 67,
80 and 100 ms, followed by the overflow count. Use these with the concurrent
model workload and battery power measurements. The interval series begins
after the first Active swap and excludes idle-to-active transition latency.

The independent Try argument `being.face_reply_ladder=1` auditions the
visual acknowledgement of an accepted utterance. It uses Voice's estimated
speech-end timestamp and computes the age when the face receives it. It can
blink in the first 300 ms, avert the
eyes from 300 ms to one second, and make small search movements from one to
two seconds. A late final skips stages whose time has already passed. An
app panel keeps its own gaze focus. A spoken filler and gaze ahead of
playback need separate playback timing and consent.

### Native voice diagnostic capture

For the private cable Try image, record the image hash, embedded source ID and
boot options. Start with automatic ASR and TTS selection and the built-in
microphone and speakers. With the lid open, speak the same labelled prompts
at 1 m and 2 m. Include a wake-and-command turn, a follow-up, a nearby
non-wake phrase and a long reply to interrupt. Observe a separate quiet
window for false wakes and record its duration.

For each attempt, record the selected ALSA endpoint and channel, input and
noise levels, wake and transcript verdicts, recognizer and voice, and time
to first audible word. Record mixer settings and compare EQ on and off at
matched volume for intelligibility. The [timing probe](#run-an-on-device-experiment)
can supply playback launch timing. Measure audible output separately.
Capture any forced-engine rerun separately from the automatic baseline.

Use the [cable workflow](LAB.md#cable-development-build) to collect
diagnostics and the boot journal over Ethernet. Run `lab/cable.sh pull-logs`
with `umask 077`. Keep the original capture private because voice debug
can include transcripts. In the acceptance record, use prompt labels,
counts and timings and remove transcripts and device identifiers.

## Enroll the laptop once

Use an installed image that contains `being-lab-access`. Keep the MacBook
and Mac Studio on a trusted network and reserve a private IPv4 address
for each device. The firewall and SSH keys enforce the enrolled source
address. Keep both SSH ports inside the LAN. The optional development
key grants full root access during each open window. Generate independent
keys and keep the private files on the Mac Studio. Pass the dedicated
key file directly to the controller.

```sh
access="$HOME/.ssh/beingos"
mkdir -p "$access"
chmod 700 "$access"
ssh-keygen -t ed25519 -N '' -f "$access/observe"
ssh-keygen -t ed25519 -N '' -f "$access/develop"
```

Copy only `observe.pub`, `develop.pub` (if granting development access),
and the repository's `mind/` source directory to a provisioning USB. Boot a Linux live USB
on the MacBook. Confirm the disk, the installed ext4 partition labeled
`mind`, and the radio device before mounting anything. If a PipOS Try
stick is attached, remove it to avoid choosing its disk. The examples
below assume the MacBook is reserved at `192.168.1.50`, the Mac Studio is
reserved at `192.168.1.20`, and the Linux interface name for the MacBook
radio is `wlan0`. Substitute the actual values and mounted key paths.

```sh
lsblk -o PATH,TRAN,SIZE,MODEL,FSTYPE,LABEL,PARTUUID,MOUNTPOINTS
iw dev
sudo mkdir -p /mnt/being-mind
sudo mount /dev/disk/by-partuuid/INSTALLED_MIND_PARTUUID /mnt/being-mind
cd /path/to/folder-containing-mind
sudo python3 -m mind.wifi configure wlan0 'Your Wi-Fi' --offline-mind /mnt/being-mind
sudo python3 -m mind.lab_access enroll 192.168.1.50 \
  --controller-ip 192.168.1.20 \
  --observe-key /path/to/observe.pub --dev-key /path/to/develop.pub \
  --offline-mind /mnt/being-mind
sudo python3 -m mind.network_enforce set paired-lan --offline-mind /mnt/being-mind
sudo umount /mnt/being-mind
```

The Wi-Fi command prompts for the password and accepts `--security wpa3`
for an SAE network. Verify the physical radio ID and firmware loading
with the live USB. The `b43` driver can require separately sourced
firmware. With the required, owner-approved files and a trusted SHA-256
manifest available locally, stage the matching HT PHY core from the Mac
Studio. Use 29 in the example only if the physical driver asks for the
revision 29 files. Revision 26 uses `--core 26`.

```sh
python3 -m lab.b43_firmware /path/to/b43 /path/to/SHA256SUMS --core 29
```

The default output is `out/firmware-overlay/usr/lib/firmware/b43/`, with
files readable by the Buildroot user. The overlay stays in the ignored
build output directory.
For a new version, choose a separate overlay path with `--overlay`.
In the active Buildroot configuration (`lab/build.sh make menuconfig`),
set the root filesystem overlay directory to `/src/out/firmware-overlay`.
Keep that overlay on subsequent physical releases and verify the actual
packaged firmware files before a Wi-Fi-dependent update. RAUC's core
health gate can confirm a build while LAN access is unavailable.
[Physical Wi-Fi checks](LAB.md#physical-checks)

If the radio needs work, connect the MacBook's Ethernet port to the same
router and use its reserved wired address. Skip the Wi-Fi command and add
`--ethernet INTERFACE` to enrollment with the actual name from `ip link`.
The controller can stay on Wi-Fi. Use the known router reservation for
the MacBook's wired interface. Once wireless is provisioned, keep that
wired observation address for reconnect experiments.

Enrollment prints the MacBook's SSH host-key lines, one per enabled port.
Transfer those lines from the live console to
`$HOME/.ssh/beingos/known_hosts` on the Mac Studio, then boot PipOS.
Use the same address in the commands below. If the address changes,
re-enroll and update the pins from trusted output. The host key persists
across re-enrollment and system updates.

The observation port is 2222. Its key can request a metrics capture only.
That listener starts when the system mode allows the link and the pair is
valid. The same checks apply after every reboot. The
development key can open or close the independent root listener on port
2223. It closes after 30 minutes, on explicit close or at reboot. To revoke
the enrolled keys, run `being-lab-access revoke` from an active development
session, or run `sudo python3 -m mind.lab_access revoke --offline-mind
/mnt/being-mind` from the live USB. Exclude `/mind/lab-access` and
`/mind/network` from memory backups and exports. Enroll again on a
replacement device.

## Choose the device network mode

On the installed MacBook, use **F2 → Network** or run
`being-setup --network` at a local maintenance console.
`being-network status --json` reports the saved transport choice and the
effective firewall mode. Wi-Fi credentials, paired keys, and the system
mode are separate owner choices. A v1 skills policy needs a fresh system
mode choice before the first upgrade with this firewall. Use F2 or the
mounted live-USB method to recover if a prior pairing loses access.

| Mode | Device traffic |
|---|---|
| Offline | IPv4 and IPv6 loopback. The managed links, SSH, resolver and time-sync service stay idle. |
| Local network | The configured links can associate and use IPv4 DHCP and ARP. The enrolled Mac can reach its permitted SSH ports over IPv4. Other IP traffic, including IPv6 application traffic, DNS, and network time, is blocked. |
| Internet | The configured links can use IPv4 and IPv6, including advertised DNS and IPv6 router discovery. Inbound IP is limited to responses, network bootstrap traffic, and the explicitly paired Mac. Weather needs its own consent. Network time needs a separately approved numeric server, with UDP/123 confined to that address and DHCP NTP and public fallbacks disabled. |

Switching modes resets the network services and ends any open development
window. Use the local controls when changing the mode that carries your
current connection. When an existing pair or Wi-Fi profile needs changing
without a reachable console, boot Linux live media and use the offline
provisioning commands above. The selected mode takes effect on the next
PipOS boot. Set `local-only` to return to Offline.

The next rebuilt image can run the isolated simulated dual-stack check.
That test uses synthetic addresses and QEMU. After physical authorization,
repeat association, reconnect, Ethernet recovery, DNS and packet capture
on the MacBook's actual adapters and router.

```sh
BEING_NETWORK_IMAGE=out/images/beingos-network.img \
python3 -m unittest -v lab.tests.test_network_modes
```

## Collect a baseline and compare a candidate

On the Mac Studio, from this repository:

```sh
access="$HOME/.ssh/beingos"
laptop=192.168.1.50
baseline=$(python3 -m lab.hardware collect "$laptop" \
  --known-hosts "$access/known_hosts" --key "$access/observe" \
  --scenario warm-voice-ac --variant baseline --seconds 60 --interval 2)
```

Speak the same short synthetic test sequence during each capture. Start
with idle, a direct reply, a model-backed reply, cancel, and lid wake.
Record cold and warm windows separately and repeat on AC and battery.
Keep the model, prompt, microphone conditions, and sampling settings the
same when changing one variable. Interleave the runs after the same
warmup. Sample longer soaks in consecutive windows of up to five minutes.

The command writes a private JSON file under `out/hardware/runs/`. It
includes build and model identifiers, slot, service health and restarts,
CPU load and clocks, memory, thermal and fan readings, battery, Wi-Fi
signal, and network and disk counters where Linux exposes them. The
hardware section also reports DRM driver bindings, ALSA endpoints,
backlight state and lid observations where available. Package
power may require a local root capture if the kernel restricts the Intel
energy counters. Missing values remain blank in the comparison. The
collector sends these measurements while conversation, audio, imagery,
journals, hardware serial numbers and Wi-Fi credentials stay on the laptop.

Run the same command with `--variant candidate` after the change, then
compare the two saved files.

```sh
python3 -m lab.hardware compare "$baseline" /path/to/candidate.json
```

## Run an on-device experiment

Open a time-limited window from the Mac Studio, then start a shell on the
MacBook. Run these commands with the same `access` and `laptop` values.

```sh
python3 -m lab.hardware open-dev "$laptop" \
  --known-hosts "$access/known_hosts" --key "$access/develop"
python3 -m lab.hardware dev "$laptop" \
  --known-hosts "$access/known_hosts" --key "$access/develop"
```

Use `/run/being-experiment` for temporary scripts and binaries. You can
edit a Python probe on the MacBook with `vi` and run it against the live
hardware. For service settings, use the volatile [systemd drop-in
example](LAB.md#qualify-performance-on-the-macbookpro92). Run a patched
service with synthetic fixtures and disposable state. The installed
system is read-only. Cross-build changes to Rust, the kernel and other
native binaries on the Mac Studio, then send the candidate back for
hardware testing.

From the Mac Studio, a non-interactive development command can move a
selected script to the laptop or pull the reviewed script back. Keep
transfer and source diffs explicit.

```sh
dev() {
  python3 -m lab.hardware dev "$laptop" \
    --known-hosts "$access/known_hosts" --key "$access/develop" -- "$@"
}
dev install -d -m 700 /run/being-experiment
dev sh -c 'cat > /run/being-experiment/probe.py' < probe.py
dev python3 /run/being-experiment/probe.py

(umask 077; dev cat /run/being-experiment/probe.py > /path/to/review/probe.py)
diff -u probe.py /path/to/review/probe.py
```

For a shared voice, lid and hardware timeline, run this command from the
controller and operate the laptop during the capture. Start the scripted
prompts a few seconds into the window and allow time for the final reply.

```sh
mkdir -m 700 -p out/hardware/timing
(umask 077; dev env PYTHONPATH=/usr/lib/being python3 -m mind.hardware_qualification \
  --seconds 60 --interval 1 > out/hardware/timing/baseline-voice-ac.json)
```

The probe reports final transcript to first speakable clause and to
playback launch, plus lid-open event to panel-ready. Each latency summary
gives the sample count, median and p95. A playback launch means the audio
process started. An attempt is `completed` after the voice process reports
the final playback done. `window_ended` means the capture finished before a
terminal event, whether playback was still pending or already in progress.
Report `timing.voice_outcomes` and `timing.voice_attempt_count` alongside the
latency summaries. Keep the number of scripted prompts separately to detect
utterances that never became accepted turns. Use an acoustic recording for
actual speaker onset and a physical measurement for visible display wake.

Use `timing.voice_attempts` to locate each accepted turn's `final_at_ms`,
`playback_launch_at_ms` when present, and `end_at_ms` on the same monotonic
clock as the hardware samples. `sample_start_at_ms` and `sample_end_at_ms`
bracket the sensor reads. The final sample is scheduled at the end of the
window so turns near the deadline have a trailing reading. Check nearby
sampled CPU frequencies, core temperatures, fan RPM and throttle counters
for slow and fast turns. Treat consecutive package energy readings as a
sample-interval measurement, with wrap handled using `range_uj` when
available. Root access can expose Intel RAPL counters where supported.
Use matched windows with the same settings, prompt order and pacing, and
compare AC and battery results separately using the recorded mains state.
The JSON contains numeric timings, outcome labels and hardware metrics.

Exit the root shell, clean up any experiment-specific drop-in and restart
the affected service, then close the development window. A reboot also
clears `/run`.

```sh
python3 -m lab.hardware close-dev "$laptop" \
  --known-hosts "$access/known_hosts" --key "$access/develop"
```

## Integrate and verify

Keep the reviewed code, fixed test inputs and an explanation of the
observed change in the Mac Studio checkout. Run the focused test there,
then build and sign from the reviewed source with the existing Buildroot
and the [RAUC update flow](LAB.md#install-and-verify-an-update-in-the-lab).
Keep the output paths or hashes with the benchmark results. Use the
installed channel for a MacBook installed from Try media and the factory
channel for the separate lab disk image.

```sh
lab/build.sh build being-rebuild all
lab/build.sh installed-bundle
```

The factory-image integration test exercises enrollment, pinned SSH,
development expiry, and the lab bundle on a disposable QEMU disk. Build
its matching bundle with `lab/build.sh bundle`. Run one guest at a time.

```sh
BEING_HARDWARE_IMAGE=out/images/beingos.img \
BEING_HARDWARE_BUNDLE=out/images/beingos.raucb \
python3 -m unittest -v lab.tests.test_hardware_ssh
```

With a development window open, stage the signed bundle on the installed
disk. The device verifies the signature and writes the inactive slot.
Use the installed image's matching builder trust key. Reboot explicitly
when ready for the trial, then collect the same scenarios and verify the
new build ID and slot. To examine RAUC state after reboot, open a new
development window and run `dev rauc status`.

```sh
dev install -d -m 700 /mind/lab-access/updates
dev sh -c 'cat > /mind/lab-access/updates/candidate.raucb' < out/images/beingos-installed.raucb
dev rauc install /mind/lab-access/updates/candidate.raucb
dev rm /mind/lab-access/updates/candidate.raucb
dev systemctl reboot
```

## Rotate the laptop release key

The installed A/B slots carry their own update certificates. A rotation
bundle is signed by the current key and installs a root filesystem that
trusts the replacement certificate. The separate model-pack certificate
must continue to verify the packs already on the SSD. Retain the current
public certificate and the matching Try image with the rotation records.

Make the replacement key with `lab/build.sh release-key` and the existing
offline custody procedure. With the currently installed public certificate
in `PIPOS_PREVIOUS_CERT`, set `PIPOS_NEXT_CERT` to the replacement public
certificate and `PIPOS_PREVIOUS_KEY` to the current encrypted private key.
Use a distinct `PIPOS_ROTATION_VERSION` for the bridge.

```sh
BEING_REPLACE_RELEASE_CERT=1 lab/build.sh release-cert "$PIPOS_NEXT_CERT"
BEINGOS_REQUIRE_RELEASE_KEY=1 lab/build.sh laptop-try
lab/build.sh laptop-rotation-bundle "$PIPOS_PREVIOUS_KEY" "$PIPOS_PREVIOUS_CERT" "$PIPOS_ROTATION_VERSION"
```

The build command writes `out/images/beingos-laptop-key-rotation-<version>.raucb`
and a receipt with both public certificate fingerprints. Audit the bridge
and the exact new Try image with RAUC and 7-Zip available. Set
`PIPOS_PREVIOUS_TRY` to the saved Try image matching the installed release.

```sh
python3 -m lab.hardware_image out/images/beingos-laptop-try.img \
  --match-laptop-bundle "out/images/beingos-laptop-key-rotation-$PIPOS_ROTATION_VERSION.raucb" \
  --previous-release-certificate "$PIPOS_PREVIOUS_CERT" \
  --release-certificate "$PIPOS_NEXT_CERT" \
  --previous-image "$PIPOS_PREVIOUS_TRY" --verify-laptop-bundle
```

Before using the bridge, sign a follow-up with the replacement private key
using `lab/build.sh laptop-bundle NEW_KEY VERSION`. Audit that ordinary
bundle against the replacement certificate and its matching Try image.

Rehearse on a disposable installation of the prior release before using the
MacBook's SSD. Apply the bridge through **F2 → Update → USB**, reboot and
wait for the trial to be confirmed. Verify a model reply and archive recall.
Exercise **Care → Previous system** to confirm the older slot still boots,
then return to the confirmed replacement slot. The bridge can also be
applied again while the prior slot is running. Installing the follow-up
from the confirmed replacement slot replaces the remaining old slot.
Verify both slots' trust and that an old-key update is refused.
Keep the signed bridge with its receipt and the rehearsal results.

The workflow helps pinpoint where a delay begins, find configurations
that stay fast once the laptop heats up, reproduce driver and Wi-Fi
faults on the hardware that exhibits them, and verify that candidate
changes survive a reboot without compromising the existing life record.
