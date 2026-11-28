# MacBookPro9,2 SD card reader

The built-in PCI function is Broadcom `14e4:16bc`, class `0805` (SD host
controller). Linux identifies this BCM57765 function in the upstream
`sdhci-pci` driver. The laptop kernel therefore builds in `MMC`, `MMC_BLOCK`,
`MMC_SDHCI`, and `MMC_SDHCI_PCI`.

This is driver preparation, not permission to open cards. PipOS has no udisks
desktop automounter or SD entry in `fstab`. Insertion alone remains inert.
The explicit inspector and Keep intake have a separate SD path, but it is
disabled unless a physical qualification installs a root-owned, non-writable
`/etc/being/sd-card-qualified` containing exactly
`broadcom-14e4:16bc-sdhci-pci-v1`. The product image does not create that file.
USB remains the default path. SD is not admitted for install, recovery, backup,
export, ejection, or unattended Keep transfer.

## Identification evidence

Never assume the controller address stays `01:00.1` or that the card is
`mmcblk0`. On the physical laptop record:

- the PCI function's `vendor`, `device`, and `class`, and that its `driver`
  symlink resolves to `sdhci-pci`;
- the card's resolved `/sys/class/block/mmcblkN` ancestry, including its
  `mmc_host/mmcN/mmcN:*` node and `type` (`SD` rather than an inferred name);
- the kernel block device's major/minor number, size, read-only state, and
  partition table before and after insertion;
- the card node's kernel-provided `cid`, `manfid`, `oemid`, `name`, and serial
  as private qualification evidence, not as a spoken label or authorization;
- each selected partition's filesystem UUID and the same open-device `dev_t`
  checks already used by the confined removable-media helpers.

A filesystem label is descriptive only. It must not establish card identity or
authorize a mount.

At each explicit SD request the helper re-derives the controller/card ancestry,
hashes the private card identity fields, and binds the confirmation to that
digest, the whole-disk `dev_t`, and `diskseq`. It then uses only the existing
read-only FAT/exFAT mount and capability-free scanner. The same identity and the
selected partition's mounted `dev_t` are checked around the bounded read. A
named Keep file or photo batch runs immediately from that owner confirmation;
it is never armed as an idle/background import.

## Physical qualification gate

Use a disposable card and keep installation targets out of scope. Verify cold
boot with no card, repeated insertion/removal, read-only inspection, malformed
partition tables, the mechanical write-protect indication if the controller
exposes it, lid suspend/resume, and removal during an active bounded read.
Record kernel messages and confirm there is no automatic mount or filesystem
probe by a Pip service.

`sdhci-pci` is a bus-mastering controller and normally uses SDMA/ADMA through
the kernel DMA API. Before allowing untrusted cards into ordinary workflows,
record whether the Mac firmware exposes a working IOMMU and whether Linux has
actually placed this PCI function in an isolated DMA domain. Driver support
alone does not prove DMA isolation. If there is no usable IOMMU, document that
as residual physical-media trust rather than claiming confinement.

Runtime power management is also unqualified. Measure the empty reader and an
inserted card with its PCI `power/control` left at the kernel default, then with
`auto`; include resume and hot-remove behavior. The commented candidate in
`60-being-power.rules` remains disabled until that evidence shows it is both
safe and useful.
