"""Save the owner's encrypted pre-update snapshot from the installed RAUC hook."""

import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

from . import backup, update_slots
from .backup_crypto import BackupCryptoError, RECIPIENT


def enrolled_recipient(mind):
    try:
        with backup.regular_file(mind / backup.RECIPIENT_FILE) as source:
            info = os.fstat(source.fileno())
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("The backup recipient must be owned and writable only by root.")
            raw = source.read(129)
    except FileNotFoundError:
        return None
    if len(raw) > 128 or not RECIPIENT.fullmatch(value := raw.strip().decode("ascii")):
        raise ValueError("The backup recipient is invalid.")
    return value


def main():
    mind = Path("/mind")
    try:
        update_slots.check_update_admission()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"No update was started. {error}", file=sys.stderr, flush=True)
        return 1
    try:
        recipient = enrolled_recipient(mind)
        if recipient is not None:
            backup.create_update_checkpoint(mind, recipient)
    except backup.Unavailable:
        print("Another backup is running. Wait for it to finish before updating.", file=sys.stderr, flush=True)
        return 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError, BackupCryptoError):
        print("The encrypted pre-update checkpoint could not be saved. Check the backup recipient and free "
              "space in Care before updating.", file=sys.stderr, flush=True)
        return 1
    try:
        update_slots.admitted_update()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"No update was started. {error}", file=sys.stderr, flush=True)
        return 1
    if recipient is None:
        print("No pre-update checkpoint was made. Set up an encrypted backup in Care to enable it.", flush=True)
    else:
        print("Saved the encrypted pre-update checkpoint.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
