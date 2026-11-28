"""Guest-side authenticated backup stages for the privacy acceptance suite."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from mind.backup import authenticate, extract_snapshot
from privacy_probe import MIND, WORK, archive_canary


def backup(*args):
    result = subprocess.run(["being-backup", *map(str, args)], capture_output=True,
                            text=True, timeout=90, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "being-backup failed")
    return result.stdout.strip()


def main():
    identity = WORK / "owner.key"
    snapshot = WORK / "before.age"
    recipient_file = WORK / "recipient.txt"
    stage = sys.argv[1]
    if stage == "before":
        recipient = backup("keygen", "--identity", identity)
        backup("create", "--mind-dir", MIND, "--output", snapshot, "--recipient", recipient)
        recipient_file.write_text(recipient)
        # Check the encrypted input to the later restore actually contains
        # the canary. Hold decrypted copies only in the guest's tmpfs.
        with tempfile.TemporaryDirectory(dir=WORK) as custody, tempfile.TemporaryFile(dir=WORK) as plaintext:
            extract = Path(custody)
            authenticate(snapshot, identity, plaintext)
            extract_snapshot(plaintext, extract)
            archive_canary(extract, True)
    elif stage == "after":
        deletions = WORK / "after-deletions.age"
        expected = json.loads(backup("checkpoint", "--mind-dir", MIND))["checkpoint"]
        export = json.loads(backup("export-deletions", "--mind-dir", MIND, "--output", deletions,
                                   "--recipient", recipient_file.read_text()))
        assert export["checkpoint"] == expected, "deletion proof does not match the current archive checkpoint"
        restored = WORK / "restored"
        restored.mkdir(mode=0o700)
        result = json.loads(backup("restore", "--mind-dir", restored, "--input", snapshot, "--identity", identity,
                                   "--deletions", deletions, "--current-checkpoint", expected))
        assert result["history_restored"], "the authenticated restore skipped the archive"
        archive_canary(restored, False)
        for name in ("context.json", "cortex", "reminders.db"):
            assert not (restored / name).exists(), f"backup unexpectedly restored {name}"
    else:
        raise ValueError("unknown backup probe stage")


if __name__ == "__main__":
    main()
