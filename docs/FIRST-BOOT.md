# First boot on the MacBook

One page for the first PipOS boots on the 13-inch MacBook Pro (mid-2012,
MacBookPro9,2). It writes a Try stick, boots it, runs a short script and
brings the logs back. **Nothing is written to the Mac's internal disk.**
Do not pick "Install PipOS" on these rounds.

Round 1 (sections 1 to 7) ran on 23 November 2026 with the frozen stick: Pip
heard "Hey Pip" but could not make out the words after it. Round 2
([section 8](#8-round-2-hearing-typing-and-speed)) needs a new stick built
from the current source, and tests the fixes.

## 1. The file to write

| | |
|---|---|
| File | `out/hardware-usb/pipos-laptop-try-20261123/pipos-laptop-try.img` |
| Size | 991,973,376 bytes (about 1 GB; any stick of 2 GB or more) |
| SHA-256 | `8de540df7b99ac86cedb1ecb374c885041f6b1bcfe482ac950c2fdfa6588e612` |

This image is frozen. Never rebuild or edit it. Check it before writing:

```bash
cd ~/Developer/BeingOS/out/hardware-usb/pipos-laptop-try-20261123
shasum -a 256 -c SHA256SUMS        # must print: pipos-laptop-try.img: OK
```

Use a spare stick. Writing erases all of it. The Lexar still holds the macOS
Monterey installer: keep it for now (see [DECISIONS-OWNER.md](DECISIONS-OWNER.md), item 3).

## 2. Write the stick on the Mac Studio

```bash
diskutil list external              # find the stick: /dev/diskN, check its size and name
diskutil unmountDisk /dev/diskN
sudo dd if=pipos-laptop-try.img of=/dev/rdiskN bs=4m status=progress
sync
# macOS mounts the new PIPLOGS volume at once and writes its own files to it.
# Unmount again before reading back, or the hash can differ for that reason alone:
diskutil unmountDisk /dev/diskN
# Read it back and hash exactly the image's length (237 x 4 MiB covers it):
sudo dd if=/dev/rdiskN bs=4m count=237 2>/dev/null | head -c 991973376 | shasum -a 256
diskutil eject /dev/diskN
```

The read-back hash must equal the SHA-256 above. Use `rdiskN` (raw, fast),
never `disk1` or whatever holds macOS. If macOS says "The disk you attached was
not readable", click **Ignore**. A `PIPLOGS` volume may mount after writing;
that is normal, which is why the second `unmountDisk` comes before the read-back.
`write-usb.sh` beside the image does the same steps for the 61.9 GB "USB Flash
Drive" it was written for. It names `disk16` and refuses any other disk, and it
does not unmount before its read-back: if it reports MISMATCH, run the
`unmountDisk` and read-back lines above before writing again.

## 3. Boot it

1. Plug in the charger and the stick, lid open. Power on and **hold Option**
   (alt) until the startup icons appear. Pick **EFI Boot** (the yellow USB
   icon) and press Return.
2. GRUB shows three entries for 5 seconds. Doing nothing boots the first. The keys are lower-case letters, no fn needed.

| Entry | Key | When |
|---|---|---|
| Try PipOS | Return, or wait | Always first |
| Try PipOS (show boot messages) | **v** | If the screen stays black or the eyes never come |
| Try PipOS (safe graphics) | **s** | If the boot-messages entry shows graphics (i915) errors or a garbled screen |

3. The Try menu says Hello. Press **1** (Try PipOS) to meet the being. **F2**
   (on the first stick, hold **fn**) brings the menu back later; **Q** leaves it.
4. **Talking.** Say **"Hey Pip"**, pause until the eyes brighten, *then* talk.
   "Say hello" alone does nothing on this image.
5. **Keys.** On the first stick the top row sent media keys unless you held
   **fn**: fn+F2 was the menu, fn+F10 or **M** muted, fn+F9 recovered a face
   with no lid reading. Every Try entry on a round-2 stick boots with
   `hid_apple.fnmode=2`, so the top row sends F1-F12 itself: **F2** is the
   menu, **F10** or **M** mutes, **F9** recovers the face. **Esc** interrupts
   a reply on both.
6. **Shutting down.** A short press of the power button. Holding it (a hard
   power-off) loses the last few seconds of the log.

## 4. Known surprises on this image

- Silence is most likely the mixer level, not a dead voice organ. Note it and carry on.
- The Apple logo on the lid may stay lit with the lid closed.
- `summary.txt` says "applesmc NOT found". That is a reader bug on this kernel, not a missing chip.
- The clock is UTC, so times are about 11 hours behind Sydney. You can say
  "Hey Pip … set my time zone to Australia Sydney"; Try forgets it at power-off.
- Replies may take a minute or more on a cold model. The eyes show thinking.
- The fan may run loud. The SMC's own fan curve and the CPU's own heat limit stay in charge; PipOS writes no power settings.

## 5. The boot script (about 30 minutes)

On AC, lid open. Keep a stopwatch running and **photograph the screen at each step**.

| # | Do | Note down |
|---|---|---|
| 1 | Power on with Option held, through to the eyes | Time from power button to GRUB, and to the eyes |
| 2 | Leave it alone for **10 minutes** (the model warms up) | Anything odd: fan, flicker, eyes |
| 3 | fn+F2, **5 Check hardware**, let it save, then **Q** | Photo of each screen |
| 4 | "Hey Pip", pause, "What time is it?" (no model needed) | Did the eyes brighten? Was the answer heard? |
| 5 | Three short chats: "Hey Pip … how are you?", "… what's your name?", "… tell me something about yourself" | Time from the end of your sentence to the first word |
| 6 | "Hey Pip … remember that my cat is called Tom", then "Hey Pip … what do you remember about my cat?" | The exact replies |
| 7 | One long question, for example "Hey Pip … can you explain how you remember things and what happens when you forget?" | Time to first word; whether it finished |
| 8 | Unplug the charger for 2 minutes, then plug it back in | Any reaction on the face or in speech |
| 9 | Close the lid for 30 seconds, then open it | Did the face blank and wake? Did the lid logo stay lit? |
| 10 | Press F10, then fn+F10; F2, then fn+F2 (Q to leave); Esc during a reply; click the trackpad | What each one did |
| 11 | Short press of the power button | Time until it is off |

If anything hangs for more than 3 minutes, photograph it, hold the power
button, and boot again and press **v** at the GRUB menu (boot messages).

## 6. Read the logs on the Mac

Plug the stick into the Mac Studio. The `PIPLOGS` volume opens in Finder: one
`boot-NNNN-…` folder per boot plus `boots.txt` (README.txt explains every
file). Then:

```bash
cd ~/Developer/BeingOS
python3 lab/try_timings.py /Volumes/PIPLOGS
```

It prints the CPU, the renderer, failed services, the state changes with
times and, for every model request, prompt tokens, cached tokens and the
prompt and decode speeds.

## 7. What to send back

- The whole `PIPLOGS` folder, copied to `~/Developer/BeingOS/out/hardware-try/round1/` (drag it in Finder).
- The photos, and the notes from the table above, especially the stopwatch times.
- Anything that surprised you, in your own words.

`journal.txt` may hold what you said to Pip and nearby Wi-Fi names, so keep
the folder private.

## 8. Round 2: hearing, typing and speed

Round 2 uses a new Try stick built from the current source. Its file name,
size and SHA-256 come with it, in `SHA256SUMS` beside the image. Write and
check it exactly as in sections 1 and 2, on a spare stick. The frozen round-1
stick stays as it is.

### What is new on this stick

- **The top row sends F-keys.** Press **F2** on its own for the menu, and
  **F10** to mute. Hold **fn** only for brightness and volume. If F2 changes
  the brightness instead, hold fn and tell us.
- **Typing to Pip.** F2, then **6** "Talk to Pip by typing". What you type is
  answered on screen and out loud. **Esc** leaves it.
- **Space to attend.** While the eyes rest, press **Space** or **Return**, or
  click the trackpad, and Pip listens as if you had said "Hey Pip". This does
  nothing while the terminal panel has the keyboard or while Pip is muted.
- **Pip owns up.** If it hears you but cannot make out the words, it says
  "Sorry, I didn't catch that." and squints for a moment. The second time
  within 10 minutes it adds how to type to it.
- **The microphone is set up at start.** Capture level and boost are set
  before the ears start, and a quiet microphone gets a software lift.

### The GRUB menu

GRUB shows six entries for 5 seconds. The keys are lower-case letters.

| Entry | Key | When |
|---|---|---|
| Try PipOS | Return, or wait | First, always |
| Try PipOS (show boot messages) | **v** | The screen stays black or the eyes never come |
| Try PipOS (safe graphics) | **s** | The boot messages show i915 errors or a garbled screen |
| Try PipOS (alternative microphone) | **m** | The first microphone set-up (codec fixup `mbp81`, and Pip picks the better channel) |
| Try PipOS (alternative microphone 2) | **n** | The second microphone set-up (codec fixup `mbp101`, for the digital microphone pins) |
| Try PipOS (measure speed) | **b** | Bench mode: Pip measures its models and voices on this laptop and saves the numbers with its logs |

### The round-2 script (about 45 minutes)

On AC, lid open, in a quiet room. Sit about 1 metre from the laptop. Keep the
stopwatch and photograph anything odd.

| # | Boot | Do | Note down |
|---|---|---|---|
| 1 | Return (default) | Wait until the eyes settle (about a minute) | Time from power button to the eyes |
| 2 | default | "Hey Pip", wait for the chirp, then "What time is it?" | Was the answer right? Did it say "Sorry, I didn't catch that."? |
| 3 | default | Do step 2 five times, then five times from 2 metres | How many of the ten worked |
| 4 | default | Press **Space**, then say "What time is it?" without "Hey Pip" | Did the eyes brighten? Was it answered? |
| 5 | default | F2, **6**, type `what time is it`, Return; then `remember that my cat is called Tom`; then `what do you remember about my cat`; then Esc | The replies, on screen and spoken |
| 6 | default | "Hey Pip … how well can you hear me?" | The exact reply |
| 7 | default | Short press of the power button | Time until it is off |
| 8 | **m** | Steps 2, 3 and 6 again | Count and replies |
| 9 | **n** | Steps 2, 3 and 6 again | Count and replies |
| 10 | **b** | Leave it alone with the lid open and the charger in, until the PIPLOGS stick has a bench file in that boot's folder (or 30 minutes have passed), then press the power button briefly | Anything odd: fan, eyes, how long it took |

Boot each entry by powering on with **Option** held, picking **EFI Boot** and
pressing its key at the GRUB menu. Shut down with a short press of the power
button between boots.

### Bringing the logs back

As in sections 6 and 7, but copy the `PIPLOGS` folder to
`~/Developer/BeingOS/out/hardware-try/round2/`. Each boot has its own folder,
so the microphone entries can be compared side by side. Keep the folder
private: `journal.txt` may hold what you said and typed.

