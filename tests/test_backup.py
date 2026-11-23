import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mind.memory import Memory
from mind.profile import PROFILE_FILE, Personality, Profile, load_profile, save_profile
from mind.retention import MemoryPolicy, load_policy, save_policy


ROOT = Path(__file__).resolve().parents[1]
LIFE = {"id": "1a" * 16, "born": 1_723_000_000, "wakes": 7, "body": "lab fixture"}
PROFILE = Profile(name="Franky", owner="Richard", wake_word="Hello Franky", voice="kal16", rate=1.15,
                  personality=Personality(1, 2, 0, 2, 1))
CORRECTED = "My violin is called Vermilion"
REPLACEMENT = "My violin is called Periwinkle"
FORGOTTEN = "The garden gate is called Mulberry"
RETAINED = "The telescope is called Cassiopeia"
DEPENDENT = "The hidden garden latch colour was mentioned here"
TRANSITIVE = "A later reply depends on the hidden latch colour"
TIME_ZONE = "Australia/Sydney"
WEATHER_LOCATION = "fixture-private-weather-location"
EXCLUDED = {
    "network/wifi.json": "fixture-wifi-password",
    "lab-access/access.json": "fixture-ssh-access-settings",
    "lab-access/ssh_host_ed25519_key": "fixture-ssh-host-private-key",
    "context.json": "fixture-saved-context",
    "cortex/slot.bin": "fixture-model-slot-cache",
    "reminders.db": "fixture-cancelled-reminder-queue",
}


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "requires age and age-keygen on PATH")
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.work / "source"
        self.source.mkdir()
        (self.source / "life.json").write_text(json.dumps(LIFE), encoding="utf-8")
        save_profile(self.source, PROFILE)
        (self.source / "skills.json").write_text(
            json.dumps({"time_zone": TIME_ZONE, "weather_location": WEATHER_LOCATION}), encoding="utf-8",
        )
        for name, value in EXCLUDED.items():
            target = self.source / name
            target.parent.mkdir(exist_ok=True)
            target.write_text(value, encoding="utf-8")

        self.memory = Memory(self.source)
        self.addCleanup(self.memory.close)
        corrected_source = self.memory.log("heard", CORRECTED, speaker=PROFILE.owner)
        self.corrected = self.memory.remember(CORRECTED, speaker=PROFILE.owner, source=corrected_source)
        forgotten_source = self.memory.log("heard", FORGOTTEN, speaker=PROFILE.owner)
        self.forgotten = self.memory.remember(FORGOTTEN, speaker=PROFILE.owner, source=forgotten_source)
        dependent = self.memory.log("said", DEPENDENT, speaker=PROFILE.name, recalled=[self.forgotten])
        self.memory.log("said", TRANSITIVE, speaker=PROFILE.name, episodes=[dependent])
        retained_source = self.memory.log("heard", RETAINED, speaker=PROFILE.owner)
        self.retained = self.memory.remember(RETAINED, speaker=PROFILE.owner, source=retained_source)
        with self.memory.db:
            self.memory.db.execute("INSERT INTO dream_journal VALUES (?, ?, 1, 0)", (PROFILE.owner, retained_source))
            self.memory.db.execute("INSERT INTO dream_checkpoints VALUES (?, ?, NULL, 1)", (PROFILE.owner, retained_source))

        self.identity = self.work / "owner.key"
        self.recipient = self.keygen(self.identity)
        self.backup = self.work / "archive.age"
        self.proof = self.work / "deletions.age"
        self.destination = self.work / "replacement"
        self.destination.mkdir()

    def command(self, *args, succeeds=True):
        result = subprocess.run([sys.executable, "-m", "mind.backup", *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def keygen(self, identity):
        recipient = self.command("keygen", "--identity", identity).stdout.strip()
        self.assertRegex(recipient, r"^age1[0-9a-z]+$")
        self.assertEqual(identity.stat().st_mode & 0o777, 0o600)
        return recipient

    def create(self):
        self.command("create", "--mind-dir", self.source, "--output", self.backup, "--recipient", self.recipient)
        with self.backup.open("rb") as stream:
            self.assertEqual(stream.readline(), b"age-encryption.org/v1\n")

    def checkpoint(self, proof=None):
        target = self.proof if proof is None else proof
        result = self.command("export-deletions", "--mind-dir", self.source, "--output", target,
                              "--recipient", self.recipient)
        checkpoint = json.loads(result.stdout)["checkpoint"]
        self.assertRegex(checkpoint, r"^[0-9a-f]{32,64}$")
        return checkpoint

    def decrypt(self, path):
        result = subprocess.run(["age", "--decrypt", "--identity", self.identity, path],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout

    def restore_args(self, *, input_path=None, identity=None, proof=None, checkpoint=None):
        args = ["restore", "--mind-dir", self.destination, "--input", input_path or self.backup,
                "--identity", identity or self.identity]
        if proof is not None:
            args += ["--deletions", proof]
        if checkpoint is not None:
            args += ["--current-checkpoint", checkpoint]
        return args

    def restored_memory(self):
        memory = Memory(self.destination)
        self.addCleanup(memory.close)
        return memory

    def assert_life_and_profile(self):
        life = json.loads((self.destination / "life.json").read_text(encoding="utf-8"))
        self.assertEqual({field: life[field] for field in ("id", "born", "wakes")},
                         {field: LIFE[field] for field in ("id", "born", "wakes")})
        self.assertEqual(load_profile(ROOT / "config", self.destination), PROFILE)
        skills = json.loads((self.destination / "skills.json").read_text(encoding="utf-8"))
        self.assertEqual(skills["time_zone"], TIME_ZONE)
        self.assertNotIn(WEATHER_LOCATION, skills.values())
        for name in EXCLUDED:
            self.assertFalse((self.destination / name).exists(), name)

    def assert_no_activation(self):
        for name in ("life.json", PROFILE_FILE, "mind.db"):
            self.assertFalse((self.destination / name).exists(), name)

    def test_transplant_old_snapshot_removes_stale_facts_and_derived_replies(self):
        self.create()
        self.assertTrue(self.memory.correct(self.corrected, REPLACEMENT))
        self.assertEqual(self.memory.forget([self.forgotten]), [FORGOTTEN])
        self.assertEqual(self.memory.fact(self.corrected).text, REPLACEMENT)
        checkpoint = self.checkpoint()

        (self.destination / "lost+found").mkdir()
        result = self.command(*self.restore_args(proof=self.proof, checkpoint=checkpoint))
        self.assertEqual(json.loads(result.stdout), {"history_restored": True, "life_id": LIFE["id"]})

        self.assert_life_and_profile()
        self.assertTrue((self.destination / "lost+found").is_dir())
        restored = self.restored_memory()
        self.assertEqual(restored.db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        self.assertEqual(restored.db.execute("SELECT count(*) FROM dream_journal").fetchone(), (0,))
        self.assertEqual(restored.db.execute("SELECT count(*) FROM dream_checkpoints").fetchone(), (0,))
        self.assertEqual(restored.fact(self.retained).text, RETAINED)
        self.assertEqual([item.text for item in restored.recall_history("Cassiopeia")], [RETAINED])
        stored = (self.destination / "mind.db").read_bytes()
        for phrase, search in ((CORRECTED, "Vermilion"), (FORGOTTEN, "Mulberry"), (DEPENDENT, "hidden garden latch"),
                               (TRANSITIVE, "depends hidden latch")):
            with self.subTest(phrase=phrase):
                self.assertFalse(restored.contains(phrase))
                self.assertNotIn(phrase.encode(), stored)
                self.assertEqual(restored.recall(search, all_terms=True), [])
                self.assertEqual(restored.recall_history(search, all_terms=True), [])

    def test_same_revision_checkpoint_restores_committed_rows_from_live_sqlite_wal(self):
        self.assertNotIn(RETAINED.encode(), (self.source / "mind.db").read_bytes())
        self.assertIn(RETAINED.encode(), (self.source / "mind.db-wal").read_bytes())
        self.create()
        checkpoint = self.checkpoint()

        self.command(*self.restore_args(proof=self.proof, checkpoint=checkpoint))

        self.assert_life_and_profile()
        restored = self.restored_memory()
        self.assertEqual(restored.fact(self.retained).text, RETAINED)
        self.assertEqual(restored.fact(self.corrected).text, CORRECTED)
        self.assertEqual(restored.fact(self.forgotten).text, FORGOTTEN)

    def test_fresh_disk_without_deletion_history_restores_identity_with_empty_archive(self):
        self.create()

        result = self.command(*self.restore_args())

        self.assertEqual(json.loads(result.stdout), {"history_restored": False, "life_id": LIFE["id"]})
        self.assert_life_and_profile()
        restored = self.restored_memory()
        self.assertEqual(restored.recall_history(""), [])
        self.assertEqual(restored.facts(), [])

    def test_latest_policy_masks_older_snapshot_and_carries_to_replacement(self):
        self.create()
        old_proof = self.work / "persistent-deletions.age"
        old_checkpoint = self.checkpoint(old_proof)
        policy = MemoryPolicy("session-only")
        save_policy(self.source, policy)
        new_checkpoint = self.checkpoint()
        self.assertNotEqual(old_checkpoint, new_checkpoint)
        self.command(*self.restore_args(proof=old_proof, checkpoint=new_checkpoint), succeeds=False)
        self.assert_no_activation()

        result = self.command(*self.restore_args(proof=self.proof, checkpoint=new_checkpoint))

        self.assertEqual(json.loads(result.stdout), {"history_restored": False, "life_id": LIFE["id"]})
        self.assertEqual(load_policy(self.destination), policy)
        self.assertEqual(self.restored_memory().facts(), [])
        self.assertNotIn(RETAINED.encode(), (self.destination / "mind.db").read_bytes())
        self.assertTrue(self.memory.contains(RETAINED))

    def test_session_only_identity_can_be_backed_up_without_a_durable_database(self):
        pristine = self.work / "pristine"
        pristine.mkdir()
        (pristine / "life.json").write_text(json.dumps(LIFE), encoding="utf-8")
        save_profile(pristine, PROFILE)
        policy = MemoryPolicy("session-only")
        save_policy(pristine, policy)

        self.command("create", "--mind-dir", pristine, "--output", self.backup, "--recipient", self.recipient)
        self.command(*self.restore_args())

        self.assertFalse((pristine / "mind.db").exists())
        self.assertEqual(load_policy(self.destination), policy)
        self.assertEqual(load_profile(ROOT / "config", self.destination), PROFILE)
        self.assertEqual(self.restored_memory().facts(), [])

    def test_explicit_only_backup_does_not_include_masked_conversation(self):
        masked = "A prior ordinary transcript is titled Ambermask"
        self.memory.log("heard", masked, speaker=PROFILE.owner)
        save_policy(self.source, MemoryPolicy("explicit-only"))
        self.create()
        checkpoint = self.checkpoint()
        self.assertNotIn(masked.encode(), self.decrypt(self.backup))

        self.command(*self.restore_args(proof=self.proof, checkpoint=checkpoint))

        self.assertEqual(load_policy(self.destination), MemoryPolicy("explicit-only"))
        restored = self.restored_memory()
        self.assertEqual(restored.fact(self.retained).text, RETAINED)
        self.assertEqual(restored.recall_history(""), [])

    def test_backup_expiry_filters_its_copy_without_changing_the_live_archive(self):
        aging = time.time() - 4 * 86_400
        with self.memory.db:
            self.memory.db.execute("UPDATE facts SET ts = ? WHERE id = ?", (aging, self.forgotten))
            self.memory.db.execute("UPDATE episodes SET ts = ? WHERE text = ?", (aging, FORGOTTEN))
        save_policy(self.source, MemoryPolicy(expiry_days=2))
        self.create()
        checkpoint = self.checkpoint()
        self.assertTrue(self.memory.contains(FORGOTTEN))
        self.assertNotIn(FORGOTTEN.encode(), self.decrypt(self.backup))

        self.command(*self.restore_args(proof=self.proof, checkpoint=checkpoint))

        self.assertEqual(load_policy(self.destination), MemoryPolicy(expiry_days=2))
        restored = self.restored_memory()
        self.assertFalse(restored.contains(FORGOTTEN))
        self.assertTrue(restored.contains(RETAINED))

    def test_snapshot_excludes_credentials_caches_and_private_location(self):
        self.create()

        plaintext = self.decrypt(self.backup)
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
            self.assertEqual(set(archive.getnames()), {"manifest.json", "life.json", PROFILE_FILE, "mind.db", "skills.json"})
            with archive.extractfile("skills.json") as settings:
                self.assertEqual(json.load(settings), {"time_zone": TIME_ZONE})
        for value in (*EXCLUDED.values(), WEATHER_LOCATION):
            self.assertNotIn(value.encode(), plaintext)

    def test_current_snapshot_preserves_corrected_wording_without_old_text(self):
        self.assertTrue(self.memory.correct(self.corrected, REPLACEMENT))
        self.assertEqual(self.memory.forget([self.forgotten]), [FORGOTTEN])
        checkpoint = self.checkpoint()
        self.create()

        plaintext = self.decrypt(self.proof)
        for text in (CORRECTED, REPLACEMENT, FORGOTTEN, RETAINED, DEPENDENT, TRANSITIVE):
            self.assertNotIn(text.encode(), plaintext)
        self.command(*self.restore_args(proof=self.proof, checkpoint=checkpoint))

        restored = self.restored_memory()
        self.assertEqual(restored.fact(self.corrected).text, REPLACEMENT)
        self.assertFalse(restored.contains(CORRECTED))
        self.assertFalse(restored.contains(FORGOTTEN))

    def test_wrong_private_key_cannot_activate_a_snapshot(self):
        self.create()
        wrong_key = self.work / "wrong.key"
        self.keygen(wrong_key)

        self.command(*self.restore_args(identity=wrong_key), succeeds=False)

        self.assert_no_activation()

    def test_damaged_ciphertext_cannot_activate_a_snapshot(self):
        self.create()
        ciphertext = self.backup.read_bytes()
        damaged = self.work / "damaged.age"

        for corrupt in (ciphertext[:-1], ciphertext[:-1] + bytes((ciphertext[-1] ^ 1,))):
            with self.subTest(ciphertext_length=len(corrupt)):
                damaged.write_bytes(corrupt)
                self.command(*self.restore_args(input_path=damaged), succeeds=False)
                self.assert_no_activation()

    def test_older_checkpoint_cannot_authorize_a_newer_snapshot(self):
        checkpoint = self.checkpoint()
        self.assertTrue(self.memory.correct(self.corrected, REPLACEMENT))
        self.create()

        self.command(*self.restore_args(proof=self.proof, checkpoint=checkpoint), succeeds=False)

        self.assert_no_activation()

    def test_authentic_but_stale_checkpoint_cannot_authorize_archive_restore(self):
        self.create()
        self.assertTrue(self.memory.correct(self.corrected, REPLACEMENT))
        stale_proof = self.work / "old-deletions.age"
        stale_checkpoint = self.checkpoint(stale_proof)
        self.assertEqual(self.memory.forget([self.forgotten]), [FORGOTTEN])
        current_checkpoint = self.checkpoint()
        self.assertNotEqual(stale_checkpoint, current_checkpoint)

        self.command(*self.restore_args(proof=stale_proof, checkpoint=current_checkpoint), succeeds=False)

        self.assert_no_activation()

    def test_proof_without_owner_checkpoint_cannot_authorize_archive_restore(self):
        self.create()
        self.checkpoint()

        self.command(*self.restore_args(proof=self.proof), succeeds=False)

        self.assert_no_activation()

    def test_existing_life_record_is_not_overwritten(self):
        self.create()
        existing = self.destination / "life.json"
        existing.write_text(json.dumps({**LIFE, "id": "2b" * 16}), encoding="utf-8")
        original = existing.read_bytes()

        self.command(*self.restore_args(), succeeds=False)

        self.assertEqual(existing.read_bytes(), original)
        self.assertFalse((self.destination / PROFILE_FILE).exists())

    def test_interrupted_restore_requires_the_same_inputs_to_finish(self):
        from mind import backup

        self.create()
        checkpoint = self.checkpoint()
        restore_args = self.restore_args(proof=self.proof, checkpoint=checkpoint)
        publish_file = backup._publish_file
        published = []

        class InterruptedRestore(BaseException):
            pass

        def interrupt_after_first_publication(source, destination):
            if published:
                raise InterruptedRestore
            publish_file(source, destination)
            published.append(destination)

        with patch.object(backup, "_publish_file", side_effect=interrupt_after_first_publication):
            with self.assertRaises(InterruptedRestore):
                backup.main(list(map(str, restore_args)))

        marker = self.destination / ".being-restore-in-progress"
        self.assertTrue(marker.is_file())
        self.assertTrue(published[0].is_file())
        self.command(*self.restore_args(), succeeds=False)
        self.assertTrue(marker.is_file())

        self.command(*restore_args)

        self.assertFalse(marker.exists())
        self.assert_life_and_profile()
        self.assertEqual(self.restored_memory().fact(self.retained).text, RETAINED)


if __name__ == "__main__":
    unittest.main()
