"""Backups by voice and at night: lists and the action log ride along, a
forget-everything keeps them from coming back, the labelled stick is found,
old sets are pruned, and the night job is honest about what it did.

age is replaced by a plain copy here, so these run without it; the real
encryption is covered by test_backup.py where age is installed.
"""

import asyncio
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mind import action_log, backup
from mind.memory import Memory
from mind.profile import Profile, save_profile
from mind.retention import MemoryPolicy, save_policy

LIFE = {"id": "3c" * 16, "born": 1_723_000_000, "wakes": 2}
CANARY = "canary-91be-spare-key-in-the-blue-pot"
RECIPIENT = "age1" + "q" * 58


def plain_encrypt(recipient, source, destination):
    shutil.copyfileobj(source, destination)


def plain_decrypt(identity, source, destination):
    shutil.copyfileobj(source, destination)


def make_lists(path, items):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("CREATE TABLE IF NOT EXISTS items (list TEXT, text TEXT)")
        db.executemany("INSERT INTO items VALUES ('shopping', ?)", [(item,) for item in items])


def read_lists(path):
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT text FROM items ORDER BY rowid")]


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("mind.backup.encrypt", plain_encrypt))
        self.enterContext(patch("mind.backup.decrypt", plain_decrypt))
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.work / "mind"
        self.source.mkdir(mode=0o700)
        (self.source / "life.json").write_text(json.dumps(LIFE), encoding="utf-8")
        save_profile(self.source, Profile(name="Pip", owner="Richard"))
        memory = Memory(self.source)
        heard = memory.log("heard", "My cat is called Tom", speaker="Richard")
        memory.remember("My cat is called Tom", speaker="Richard", source=heard)
        memory.close()
        make_lists(self.source / "lists.db", ["milk", "eggs"])
        action_log.record(self.source, "list.add", 2, now=1_800_000_000)
        action_log.record(self.source, "backup", now=1_800_000_100)
        self.stick = self.work / "stick"
        self.stick.mkdir()
        self.identity = self.work / "owner.key"
        self.identity.write_text("fixture identity")

    def members(self, snapshot):
        with tarfile.open(fileobj=io.BytesIO(snapshot.read_bytes()), mode="r:") as archive:
            return {member.name: archive.extractfile(member).read() for member in archive}

    def target(self, name="restored"):
        path = self.work / name
        path.mkdir()
        return path

    def test_lists_and_the_action_log_are_in_the_snapshot(self):
        result = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_200)
        members = self.members(result["snapshot"])
        self.assertIn("lists.db", members)
        self.assertEqual([json.loads(line) for line in members["actions.jsonl"].decode().splitlines()],
                         [{"ts": 1_800_000_000, "kind": "list.add", "count": 2},
                          {"ts": 1_800_000_100, "kind": "backup", "count": 1}])
        lists = self.work / "lists-copy.db"
        lists.write_bytes(members["lists.db"])
        self.assertEqual(read_lists(lists), ["milk", "eggs"])
        self.assertNotIn("erasures.json", members, "nothing erased yet")

    def test_canary_text_planted_in_the_log_never_reaches_a_backup(self):
        with action_log.log_path(self.source).open("a", encoding="ascii") as stream:
            stream.write(json.dumps({"ts": 5, "kind": "note.add", "count": 1, "text": CANARY}) + "\n")
            stream.write(CANARY + "\n")
        result = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_200)
        self.assertNotIn(CANARY.encode(), result["snapshot"].read_bytes())

    def test_a_history_restore_brings_lists_and_log_back(self):
        result = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_200)
        target = self.target()
        restored = backup.restore(target, result["snapshot"], self.identity, result["deletions"], result["checkpoint"])
        self.assertTrue(restored["history_restored"])
        self.assertEqual(read_lists(target / "lists.db"), ["milk", "eggs"])
        self.assertEqual([entry["kind"] for entry in action_log.entries(target)], ["list.add", "backup"])
        self.assertEqual((target / "log").stat().st_mode & 0o777, 0o700)
        self.assertFalse((target / backup.RESTORE_STAGE).exists())

    def test_a_restore_without_history_brings_neither(self):
        result = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_200)
        target = self.target()
        backup.restore(target, result["snapshot"], self.identity)
        self.assertFalse((target / "lists.db").exists())
        self.assertFalse((target / "log").exists())

    def test_forget_everything_after_a_snapshot_keeps_lists_and_log_gone(self):
        old = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_200)
        # "Forget everything": lists erased, log cleared, the erasure counted.
        (self.source / "lists.db").unlink()
        action_log.clear(self.source)
        backup.note_erase(self.source)
        self.assertEqual(backup.erasures(self.source), 1)
        newer = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_300)
        target = self.target()
        backup.restore(target, old["snapshot"], self.identity, newer["deletions"], newer["checkpoint"])
        self.assertFalse((target / "lists.db").exists(), "an older snapshot does not undo the erase")
        self.assertEqual(action_log.entries(target), [])
        self.assertEqual(backup.erasures(target), 1, "the count travels, so the next restore knows too")
        # The newer snapshot, made after the erase, restores what it holds.
        make_lists(self.source / "lists.db", ["bread"])
        latest = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_400)
        again = self.target("again")
        backup.restore(again, latest["snapshot"], self.identity, latest["deletions"], latest["checkpoint"])
        self.assertEqual(read_lists(again / "lists.db"), ["bread"])

    def test_a_memory_that_keeps_nothing_backs_up_no_lists(self):
        save_policy(self.source, MemoryPolicy("session-only"))
        result = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_200)
        self.assertNotIn("lists.db", self.members(result["snapshot"]))

    def test_carrying_a_try_being_home_brings_its_lists_and_log(self):
        target = self.target("installed")
        backup.carry(self.source, target)
        self.assertEqual(read_lists(target / "lists.db"), ["milk", "eggs"])
        self.assertEqual(len(action_log.entries(target)), 2)

    def test_forget_everything_during_a_backup_never_brings_the_old_lists_back(self):
        # The erase lands after the lists and the log were copied but before
        # the manifest is written: the snapshot must still count the erasures
        # from before its copies, so its proof (made after) outnumbers it.
        real_copy = backup.actions_copy

        def copy_then_erase(source, target):
            real_copy(source, target)
            (self.source / "lists.db").unlink()
            action_log.clear(self.source)
            backup.note_erase(self.source)
        with patch("mind.backup.actions_copy", copy_then_erase):
            raced = backup.backup_to_folder(self.source, self.stick, RECIPIENT, now=1_800_000_200)
        manifest = json.loads(self.members(raced["snapshot"])["manifest.json"])
        self.assertEqual(manifest["erasures"], 0, "counted before the copies")
        self.assertNotIn("erasures.json", self.members(raced["snapshot"]))
        target = self.target()
        backup.restore(target, raced["snapshot"], self.identity, raced["deletions"], raced["checkpoint"])
        self.assertFalse((target / "lists.db").exists(), "the forgotten shopping list stays forgotten")
        self.assertEqual(action_log.entries(target), [])
        self.assertEqual(backup.erasures(target), 1)

    def test_an_unreadable_erasure_count_errs_on_forgetting(self):
        (self.source / backup.ERASURES_FILE).write_text("{broken")
        self.assertGreater(backup.erasures(self.source), 1000)


class Stick(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mind = self.work / "mind"
        self.mind.mkdir()
        self.labels = self.work / "by-label"
        self.labels.mkdir()
        self.device = self.work / "sdb1"
        self.device.write_text("")
        self.mounted = self.work / "mounted"
        self.calls = []

    def label(self, name):
        os.symlink(self.device, self.labels / name)

    def mount(self, device):
        self.calls.append(("mount", device))
        self.mounted.mkdir(exist_ok=True)
        return self.mounted

    def unmount(self):
        self.calls.append(("unmount",))

    def test_the_last_backup_record_holds_a_time_and_how_only(self):
        self.assertIsNone(backup.last_backup(self.mind))
        backup.record_backup(self.mind, "voice", now=1_800_000_000)
        self.assertEqual(json.loads((self.mind / backup.LAST_RECORD).read_text()),
                         {"version": 1, "ts": 1_800_000_000, "how": "voice"})
        self.assertEqual(backup.last_backup(self.mind), 1_800_000_000)
        with self.assertRaises(ValueError):
            backup.record_backup(self.mind, CANARY)
        (self.mind / backup.LAST_RECORD).write_text('{"ts": "yesterday"}')
        self.assertIsNone(backup.last_backup(self.mind))

    def test_only_a_labelled_stick_is_used(self):
        os.symlink(self.device, self.labels / "being-mind")
        os.symlink(self.device, self.labels / "PIPLOGS")
        self.assertIsNone(backup.find_stick(self.labels))
        self.label("PIP-BACKUP")
        self.assertEqual(backup.find_stick(self.labels), str(self.device.resolve()))
        self.assertIsNone(backup.find_stick(self.work / "no-such-dir"))

    def test_readiness_names_what_is_missing(self):
        which = {"age": "/usr/bin/age", "age-keygen": "/usr/bin/age-keygen"}.get
        for found, reason in ((lambda name: None, "no_tool"), (which, "no_key")):
            with self.assertRaises(backup.Unavailable) as caught:
                backup.readiness(self.mind, which=found, by_label=self.labels)
            self.assertEqual(caught.exception.reason, reason)
        (self.mind / backup.RECIPIENT_FILE).write_text(RECIPIENT + "\n")
        with self.assertRaises(backup.Unavailable) as caught:
            backup.readiness(self.mind, which=which, by_label=self.labels)
        self.assertEqual(caught.exception.reason, "no_stick")
        self.label("being-backup")
        self.assertEqual(backup.readiness(self.mind, which=which, by_label=self.labels),
                         (str(self.device.resolve()), RECIPIENT))

    def test_prune_keeps_the_newest_sets_and_nothing_else_is_touched(self):
        for day in range(1, 9):
            stamp = f"202611{day:02d}-030000"
            for suffix in (".age", "-deletions.age", "-checkpoint.txt"):
                (self.work / f"pip-{stamp}{suffix}").write_text("x")
        (self.work / "holiday-photo.jpg").write_text("keep me")
        (self.work / "pip-notes.txt").write_text("keep me too")
        self.assertEqual(backup.prune(self.work, keep=5), 9)
        left = sorted(path.name for path in self.work.glob("pip-2026*"))
        self.assertEqual(len(left), 15)
        self.assertTrue(left[0].startswith("pip-20261104"))
        self.assertTrue((self.work / "holiday-photo.jpg").exists() and (self.work / "pip-notes.txt").exists())

    def sets(self, folder, *stamps):
        for stamp in stamps:
            for suffix in (".age", "-deletions.age", "-checkpoint.txt"):
                (folder / f"pip-{stamp}{suffix}").write_text("x")

    def stamps(self, folder):
        return sorted({backup.set_stamp(path) for path in folder.iterdir()} - {None})

    def test_a_clock_that_went_back_never_prunes_the_backup_just_written(self):
        november = [f"202611{day:02d}-030000" for day in range(10, 15)]
        self.sets(self.work, *november, "20240101-000512")
        # Ranked by name alone the new set would be the oldest, and deleted.
        self.assertEqual(backup.prune(self.work, keep=5, newest="20240101-000512"), 3)
        self.assertIn("20240101-000512", self.stamps(self.work))
        self.assertEqual(len(self.stamps(self.work)), 5)
        # The next night, still behind: the newer real backup replaces the last one.
        self.sets(self.work, "20240102-000400")
        backup.prune(self.work, keep=5, newest="20240102-000400")
        self.assertIn("20240102-000400", self.stamps(self.work))
        self.assertNotIn("20240101-000512", self.stamps(self.work))
        self.assertEqual(len(self.stamps(self.work)), 5, "the stick never fills up")

    def test_a_set_named_by_a_clock_that_ran_ahead_does_not_stay_forever(self):
        stick = self.work / "ahead"
        stick.mkdir()
        self.sets(stick, "20991231-235959", *[f"202611{day:02d}-030000" for day in range(10, 15)])
        backup.prune(stick, keep=5, newest="20261114-030000", clock_trusted=True)
        self.assertNotIn("20991231-235959", self.stamps(stick))
        self.assertEqual(len(self.stamps(stick)), 5)
        # Without a trusted clock nothing says which is right: the new set stays, and so does the rest by name.
        self.sets(stick, "20991231-235959")
        backup.prune(stick, keep=5, newest="20261114-030000")
        self.assertIn("20261114-030000", self.stamps(stick))
        self.assertIn("20991231-235959", self.stamps(stick))

    def test_a_voice_backup_with_the_clock_behind_keeps_its_set_and_tells_the_truth(self):
        (self.mind / backup.RECIPIENT_FILE).write_text(RECIPIENT + "\n")
        self.label("being-backup")
        which = {"age": "/usr/bin/age", "age-keygen": "/usr/bin/age-keygen"}.get
        folder = self.mounted / backup.BACKUP_FOLDER
        folder.mkdir(parents=True)
        self.sets(folder, *[f"202611{day:02d}-030000" for day in range(10, 15)])

        def fake_folder(mind_dir, folder, recipient, now=None):
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
            self.sets(folder, stamp)
            return {"checkpoint": "0" * 64, "snapshot": folder / f"pip-{stamp}.age"}
        behind = 1_704_067_512  # 2024-01-01 00:05:12 UTC
        with patch("mind.backup.backup_to_folder", fake_folder):
            result = backup.stick_backup(self.mind, "voice", mount=self.mount, unmount=self.unmount, which=which,
                                         by_label=self.labels, now=behind)
        self.assertIn("20240101-000512", self.stamps(folder))
        self.assertTrue(result["snapshot"].exists(), "the backup Pip reports is on the stick")

    def test_stick_backup_mounts_writes_prunes_unmounts_and_records(self):
        (self.mind / backup.RECIPIENT_FILE).write_text(RECIPIENT + "\n")
        self.label("being-backup")
        which = {"age": "/usr/bin/age", "age-keygen": "/usr/bin/age-keygen"}.get
        written = []

        def fake_folder(mind_dir, folder, recipient, now=None):
            written.append((folder, recipient))
            (folder / "pip-20261124-030000.age").write_text("x")
            return {"checkpoint": "0" * 64}
        with patch("mind.backup.backup_to_folder", fake_folder):
            result = backup.stick_backup(self.mind, "voice", mount=self.mount, unmount=self.unmount, which=which,
                                         by_label=self.labels, now=1_800_000_000)
        self.assertEqual(written, [(self.mounted / backup.BACKUP_FOLDER, RECIPIENT)])
        self.assertEqual(self.calls, [("mount", str(self.device.resolve())), ("unmount",)])
        self.assertEqual(result["pruned"], 0)
        self.assertEqual(backup.last_backup(self.mind), 1_800_000_000)

        def broken(mind_dir, folder, recipient, now=None):
            raise ValueError("The being changed while it was backed up.")
        with patch("mind.backup.backup_to_folder", broken):
            with self.assertRaises(backup.Unavailable) as caught:
                backup.stick_backup(self.mind, "voice", mount=self.mount, unmount=self.unmount, which=which,
                                    by_label=self.labels, now=1_800_000_500)
        self.assertEqual(caught.exception.reason, "failed")
        self.assertEqual(self.calls[-1], ("unmount",), "always unmounted")
        self.assertEqual(backup.last_backup(self.mind), 1_800_000_000, "a failure is not recorded as a backup")


class NightJob(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1_800_000_000.0

    def job(self, backup_fn):
        return backup.auto_backup_job(self.mind, backup=backup_fn, clock=lambda: self.now)

    def kinds(self):
        return [entry["kind"] for entry in action_log.entries(self.mind)]

    def test_once_a_night_and_honest_about_why_not(self):
        ran = []
        job = self.job(lambda: ran.append(1))
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual((ran, job.last_reason, self.kinds()), ([1], "done", ["backup.auto"]))

        backup.record_backup(self.mind, "night", now=self.now - 3600)
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual((len(ran), job.last_reason), (1, "recent"))

    def test_a_slow_backup_spans_one_second_chunks_and_never_spins(self):
        release = threading.Event()
        started = []

        def slow():
            started.append(1)
            release.wait(5)
        job = self.job(slow)
        began = time.monotonic()
        self.assertFalse(asyncio.run(job.run_chunk(0.3)), "still writing: ask again")
        self.assertGreaterEqual(time.monotonic() - began, 0.25, "it waited its budget, it did not spin")
        self.assertEqual((job.last_reason, job.running), ("running", True))
        self.assertFalse(asyncio.run(job.run_chunk(0.2)))
        self.assertEqual(len(started), 1, "one backup, however many chunks")
        release.set()
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual((job.last_reason, self.kinds()), ("done", ["backup.auto"]))

    def test_the_night_shift_runs_it_and_an_interrupt_stops_the_wait_at_once(self):
        from mind.night_shift import NightShift

        release = threading.Event()
        shift = NightShift(self.mind)
        job = self.job(lambda: release.wait(5))
        shift.register("backup", job)

        async def night():
            step = asyncio.ensure_future(shift.step(True))
            await asyncio.sleep(0.3)
            self.assertTrue(job.running)
            began = time.monotonic()
            running = shift.interrupt()
            result = await running
            self.assertLess(time.monotonic() - began, 0.5, "within the 2 s interrupt bound")
            self.assertEqual(result, {"backup": "interrupted"})
            await step
            release.set()
            return await shift.step(True)
        self.assertEqual(asyncio.run(night()), {"backup": "done"})
        self.assertEqual(self.kinds(), ["backup.auto"])

    def test_a_clock_that_went_back_does_not_stop_night_backups(self):
        ran = []
        job = self.job(lambda: ran.append(1))
        backup.record_backup(self.mind, "night", now=self.now + 400 * 86400)  # made while the clock ran ahead
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual((ran, job.last_reason), ([1], "done"))

    def test_backup_threads_run_below_the_conversation(self):
        seen = []

        def note():
            try:
                seen.append(os.getpriority(os.PRIO_PROCESS, threading.get_native_id()))
            except (AttributeError, OSError):
                seen.append(None)
        job = self.job(note)
        with patch("mind.backup.lower_priority", wraps=backup.lower_priority) as lowered:
            self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        lowered.assert_called_once_with()
        if os.uname().sysname == "Linux":
            self.assertGreaterEqual(seen[0], backup.BACKGROUND_NICE)

    def test_session_only_night_backups_leave_no_durable_log_line(self):
        save_policy(self.mind, MemoryPolicy("session-only"))
        job = self.job(lambda: None)
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual((job.last_reason, self.kinds()), ("done", []))

    def test_no_stick_is_quietly_nothing_and_a_failure_is_logged(self):
        def no_stick():
            raise backup.Unavailable("no_stick")
        job = self.job(no_stick)
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual((job.last_reason, self.kinds()), ("no_stick", []))

        def failed():
            raise backup.Unavailable("failed", "disk full")
        job = self.job(failed)
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual(self.kinds(), ["backup.failed"])

        def crashed():
            raise KeyError("anything at all")
        job = self.job(crashed)
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertEqual((job.last_reason, self.kinds()), ("failed", ["backup.failed", "backup.failed"]))


class OneAtATime(unittest.TestCase):
    def test_the_lock_is_shared_across_openers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "backup.lock"
            with backup.backup_lock(path):
                with self.assertRaises(backup.Unavailable) as caught:
                    with backup.backup_lock(path):
                        pass
                self.assertEqual(caught.exception.reason, "busy")
            with backup.backup_lock(path):
                pass

    def test_a_voice_backup_never_mounts_at_the_menus_media_folder(self):
        from mind import local_menu

        self.assertNotEqual(backup.STICK_DIR, local_menu.MEDIA_DIR)


if __name__ == "__main__":
    unittest.main()
