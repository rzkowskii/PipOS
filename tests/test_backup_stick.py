"""Backups to a USB stick, checkpoint records, and bringing a Try being home."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import backup, backup_crypto
from mind.initiative import InitiativePolicy, QuietHours, load_policy as load_initiative, save_policy as save_initiative
from mind.memory import Memory
from mind.profile import Profile, load_profile, save_profile
from mind.retention import MemoryPolicy, save_policy
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


ROOT = Path(__file__).resolve().parents[1]
LIFE = {"id": "2b" * 16, "born": 1_723_000_000, "wakes": 3}
FACT = "My cat is called Tom"
CHATTER = "We talked about the weather in Hobart"


class TryBeing(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.work / "try-mind"
        self.source.mkdir(mode=0o700)
        (self.source / "life.json").write_text(json.dumps(LIFE), encoding="utf-8")
        save_profile(self.source, Profile(name="Clover", owner="Richard"))
        save_initiative(self.source, InitiativePolicy(False, True, QuietHours("22:00", "07:00")))
        (self.source / "skills.json").write_text(json.dumps({"time_zone": "Australia/Sydney",
                                                             "weather_location": "private place"}))
        (self.source / "reminders.db").write_text("not carried")
        memory = Memory(self.source)
        self.addCleanup(memory.close)
        heard = memory.log("heard", FACT, speaker="Richard")
        memory.remember(FACT, speaker="Richard", source=heard)
        memory.log("heard", CHATTER, speaker="Richard")
        self.target = self.work / "new-mind"
        self.target.mkdir()

    def test_try_archive_with_owner_words_is_offered(self):
        self.assertTrue(backup.has_conversation(self.source))
        empty = self.work / "empty"
        empty.mkdir()
        self.assertFalse(backup.has_conversation(empty))
        (empty / "mind.db").write_text("not a database")
        self.assertFalse(backup.has_conversation(empty))
        Memory(self.work / "fresh").close()
        self.assertFalse(backup.has_conversation(self.work / "fresh"))

    def test_carry_brings_life_settings_and_archive_but_not_reminders_or_location(self):
        backup.carry(self.source, self.target)
        self.assertEqual(json.loads((self.target / "life.json").read_text())["id"], LIFE["id"])
        self.assertEqual(load_profile(ROOT / "config", self.target).name, "Clover")
        self.assertEqual(load_initiative(self.target, strict=True).quiet_hours, QuietHours("22:00", "07:00"))
        self.assertEqual(json.loads((self.target / "skills.json").read_text()), {"time_zone": "Australia/Sydney"})
        self.assertFalse((self.target / "reminders.db").exists())
        carried = Memory(self.target)
        self.addCleanup(carried.close)
        self.assertEqual([fact.text for fact in carried.facts()], [FACT])
        self.assertTrue(carried.contains(CHATTER))

    def test_carry_follows_the_memory_policy(self):
        save_policy(self.source, MemoryPolicy("explicit-only"))
        backup.carry(self.source, self.target)
        carried = Memory(self.target)
        self.addCleanup(carried.close)
        self.assertEqual([fact.text for fact in carried.facts()], [FACT])
        self.assertFalse(carried.contains(CHATTER))

    def test_carry_refuses_a_mind_that_is_not_empty(self):
        (self.target / "something").write_text("x")
        with self.assertRaisesRegex(ValueError, "empty, inactive"):
            backup.carry(self.source, self.target)

    def test_damaged_memory_policy_fails_a_backup_loudly(self):
        (self.source / "memory-policy.json").write_text("{damaged")
        with self.assertRaises(ValueError):
            backup.create(self.source, self.work / "out.age", "age1" + "q" * 58)
        with self.assertRaises(ValueError):
            backup.deletion_checkpoint(self.source)


class CheckpointRecords(unittest.TestCase):
    def test_fingerprint_shows_both_ends(self):
        checkpoint = "0123456789abcdef" * 4
        self.assertEqual(backup.fingerprint(checkpoint), "01234567…89abcdef")
        with self.assertRaises(ValueError):
            backup.fingerprint("not hex")

    def test_record_round_trip_and_rejects_other_files(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        record = work / "pip-checkpoint.txt"
        record.write_text(backup.checkpoint_text("2b" * 16, "f" * 64, 5))
        self.assertEqual(backup.read_checkpoint_file(record), ("2b" * 16, "f" * 64))
        record.write_text(json.dumps({"kind": "deletions", "checkpoint": "f" * 64}))
        with self.assertRaises(ValueError):
            backup.read_checkpoint_file(record)


class ProtectedIdentity(unittest.TestCase):
    def test_passphrase_protected_key_is_unlocked_by_age_before_use(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        locked = work / "key.age"
        locked.write_bytes(b"-----BEGIN AGE ENCRYPTED FILE-----\nabc\n-----END AGE ENCRYPTED FILE-----\n")
        key = b"AGE-SECRET-KEY-1" + b"Q" * 58
        with patch.object(backup_crypto, "_run", return_value=key + b"\n") as run:
            self.assertEqual(backup_crypto._read_identity(locked), key + b"\n")
        self.assertEqual(run.call_args.args[0], ["age", "--decrypt"])
        with patch.object(backup_crypto, "_run", return_value=b"garbage"):
            with self.assertRaises(backup_crypto.BackupCryptoError):
                backup_crypto._read_identity(locked)


    def test_unlocking_once_writes_a_private_plain_key_and_refuses_to_overwrite(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        locked = work / "key.age"
        locked.write_bytes(b"age-encryption.org/v1\n-> scrypt abc 18\n")
        self.assertTrue(backup_crypto.is_protected_file(locked))
        plain = work / "plain.key"
        plain.write_bytes(b"# created\nAGE-SECRET-KEY-1" + b"Q" * 58 + b"\n")
        self.assertFalse(backup_crypto.is_protected_file(plain))
        self.assertFalse(backup_crypto.is_protected_file(work / "missing"))
        key = b"AGE-SECRET-KEY-1" + b"Q" * 58
        with patch.object(backup_crypto, "_run", return_value=key + b"\n"):
            unlocked = backup_crypto.unlock_identity(locked, work / "unlocked.key")
            self.assertEqual(unlocked.read_bytes(), key + b"\n")
            self.assertEqual(unlocked.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(backup_crypto.BackupCryptoError):
                backup_crypto.unlock_identity(locked, unlocked)


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "requires age and age-keygen on PATH")
class StickBackup(TryBeing):
    def test_backup_folder_holds_snapshot_proof_and_matching_checkpoint(self):
        identity = self.work / "owner.key"
        recipient = backup_crypto.generate_key(identity)
        stick = self.work / "stick"
        stick.mkdir()
        result = backup.backup_to_folder(self.source, stick, recipient, now=1_800_000_000)
        self.assertTrue(result["snapshot"].name.startswith("pip-"))
        life_id, checkpoint = backup.read_checkpoint_file(result["checkpoint_file"])
        self.assertEqual((life_id, checkpoint), (LIFE["id"], result["checkpoint"]))
        restored = backup.restore(self.target, result["snapshot"], identity, result["deletions"], checkpoint)
        self.assertTrue(restored["history_restored"])
        self.assertEqual(load_initiative(self.target, strict=True).greetings, True)
        with self.assertRaisesRegex(ValueError, "already exists"):
            backup.backup_to_folder(self.source, stick, recipient, now=1_800_000_000)


if __name__ == "__main__":
    unittest.main()
