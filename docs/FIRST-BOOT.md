# First boot on the MacBook

One page for the first PipOS boot on the 13-inch MacBook Pro (mid-2012,
MacBookPro9,2). It writes the frozen Try stick, boots it, runs a short script
and brings the logs back. **Nothing is written to the Mac's internal disk.**
Do not pick "Install PipOS" on this round.

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
   (hold **fn**) brings the menu back later; **Q** leaves it.
4. **Talking.** Say **"Hey Pip"**, pause until the eyes brighten, *then* talk.
   "Say hello" alone does nothing on this image.
5. **Keys.** The top row sends media keys unless you hold **fn**: fn+F2 is the
   menu, fn+F10 or **M** mutes, **Esc** interrupts a reply, fn+F9 recovers a
   face that has no lid reading.
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
