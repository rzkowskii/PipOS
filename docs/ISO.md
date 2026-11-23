# The PipOS ISO

PipOS for the 13-inch MacBook Pro (Mid 2012, MacBookPro9,2) comes as one
file, `pipos-macbook.iso`, with its checksum in `pipos-macbook.iso.sha256`.
Write it to a USB stick, boot the MacBook from the stick, try PipOS, and
install it from the PipOS menu if you want to keep it.

**Trying changes nothing on the Mac.** Installing erases the Mac's internal
drive, macOS included. Make a macOS installer stick first if you may want
macOS back.

## What you need

- A USB stick of 2 GB or more. Everything on it is erased.
- Another computer to write the stick (a Mac, Linux or Windows).
- For installing: the MacBook on its charger.

## 1. Download and check the file

Download both files from the project's GitHub release into one folder.
Then check the download:

```bash
shasum -a 256 -c pipos-macbook.iso.sha256     # macOS
sha256sum -c pipos-macbook.iso.sha256         # Linux
```

It must print `pipos-macbook.iso: OK`. If not, download it again.

## 2. Write the stick

The ISO is written whole, as a disk image. Copying the file onto a stick
does not work.

**With balenaEtcher** (Mac, Windows or Linux): choose *Flash from file*,
pick `pipos-macbook.iso`, pick the stick, then *Flash*. If Etcher warns
that the image has no partition table or is not bootable, go on: it does
have one.

**With dd on a Mac:**

```bash
diskutil list external               # find the stick: /dev/diskN; check its size
diskutil unmountDisk /dev/diskN
sudo dd if=pipos-macbook.iso of=/dev/rdiskN bs=4m
sync
diskutil eject /dev/diskN
```

**With dd on Linux:** find the stick with `lsblk` (for example `/dev/sdb`),
then `sudo dd if=pipos-macbook.iso of=/dev/sdX bs=4M conv=fsync`.

Use the whole stick (`diskN`, `sdX`), not a partition on it, and make very
sure it is the stick: dd erases whatever it is pointed at.

After writing, the Mac may say "The disk you attached was not readable by
this computer". Click **Ignore**. Small volumes called `PIPOS` (a README)
and `PIPLOGS` may appear: `PIPLOGS` is where PipOS keeps its diagnostics.
Eject the stick before unplugging it.

## 3. Boot it on the MacBook

1. Plug in the stick, open the lid and power on. **Hold Option (alt)**
   until the startup icons appear.
2. Choose **EFI Boot** (the yellow USB icon) and press Return.
3. The PipOS boot menu waits 5 seconds:

| Entry | Key | When |
|---|---|---|
| Try PipOS | Return, or wait | Always first |
| Try PipOS (show boot messages) | **v** | The screen stays black, or the eyes never come |
| Try PipOS (safe graphics) | **s** | The picture is wrong or the screen goes black |

4. The PipOS menu appears. Choose **1 Try PipOS** to meet Pip. Nothing is
   saved: when you power off, the Try session is gone.

The stick does not boot from a CD or DVD, and it has no legacy (BIOS)
boot: always start it from USB with the Option key.

## 4. Install

**This erases the internal drive.** Keep the Mac on its charger.

1. Open the PipOS menu: choose **2 Install PipOS** when it first appears,
   or press **F2** on the eyes (the top row needs no fn key) and choose
   **2 Install PipOS**.
2. Pick the internal disk. The installer shows its model, size and the
   layout it will write. It never offers the USB stick.
3. Say whether to keep the name, voice and settings chosen while trying.
4. Type the confirmation exactly as shown, in capitals, for example
   `ERASE /dev/sda`. If the disk still holds macOS or a PipOS being, a
   second confirmation follows. Anything else cancels and leaves the disk
   untouched.
5. Wait while PipOS writes and checks the disk (a few minutes). When it
   says it has finished, power off, **remove the stick** and power on.

The Mac now starts PipOS from its own disk.

## If something goes wrong

- **No EFI Boot icon:** rewrite the stick, or try another USB port or stick.
- **"Cannot locate the PipOS Try media":** the boot menu could not find the
  rest of the stick. Try another port, or rewrite the stick.
- **Black screen:** restart, hold Option, and choose *show boot messages*
  (**v**) or *safe graphics* (**s**).
- **Diagnostics:** every Try boot writes a folder to the stick's `PIPLOGS`
  partition. Plug the stick into another computer to read it, or share it
  when reporting a problem.

## Building the ISO

From a laptop build (`lab/build.sh laptop`), in the project folder:

```bash
lab/make-iso.sh                      # writes out/iso/pipos-macbook.iso and .sha256
python3 -m unittest lab.tests.test_iso -v
```

`make-iso.sh` runs entirely in the Colima builder VM and only reads the
laptop build's `bootx64.efi`, `rootfs.squashfs` and `models.ext4`.
