PIPLOGS - what PipOS wrote while it ran from this USB stick
================================================================

Every Try boot of this stick makes one folder here:

  boot-NNNN-YYYYMMDD-HHMMSSZ   NNNN counts boots; the time is the Mac's
                               clock in UTC, which may be wrong.

Inside each folder:

  summary.txt            The short answers first: CPU, graphics driver and
                         renderer, sound card, lid switch, battery, camera,
                         Wi-Fi chip and firmware, failed services.
  hardware-report.txt    Everything the kernel shows about the machine.
  journal.txt            The system log, copied while it was written and
                         saved every few seconds. It may contain what was
                         said to the being and Wi-Fi network names.
  dmesg.txt              The kernel log.
  services.txt           The state of every service.
  check-hardware-*.txt   Saved by "Check hardware" in the Try menu.

boots.txt has one line when each boot starts and one when it ends cleanly.
A boot with no "ended cleanly" line crashed, lost power or was switched off
by holding the power button. Many boots close together mean a reboot loop.

PipOS writes nothing to the Mac's internal disk during Try. When this
partition runs short of space the oldest boot folders are removed (the three
newest are always kept; boots.txt keeps the history). It is safe to delete
folders here from the Mac.
