"""The Care page: owner controls that need no shell, driven with fakes."""

import io
import json
import tempfile
import time
import unittest
import wave
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mind import care
from mind.dream_policy import DreamPolicy, load_window
from mind.memory import Memory
from mind.retention import read_policy


class CareTestCase(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.out = self.enterContext(redirect_stdout(io.StringIO()))
        # No unit files on the host: nothing is restarted.
        self.enterContext(patch.object(care, "installed", return_value=()))

    def keys(self, *answers):
        return patch("builtins.input", side_effect=list(answers))


class Senses(CareTestCase):
    def test_dream_consent_records_the_window_the_mind_reads(self):
        self.assertEqual(care.dream_settings(self.mind), (False, "night"))
        with self.keys("2"):
            care.dreams_page(self.mind, False)
        self.assertEqual(care.dream_settings(self.mind), (True, "closed"))
        self.assertTrue((self.mind / "dreams-enabled").is_file(), "turn.py checks is_file()")
        self.assertEqual((self.mind / "dreams-enabled").stat().st_mode & 0o777, 0o600)
        policy = DreamPolicy(True, settings_dir=self.mind)
        self.assertEqual(policy.window, "closed")
        with self.keys("3"):
            care.dreams_page(self.mind, False)
        self.assertFalse((self.mind / "dreams-enabled").exists())
        self.assertEqual(policy.window, "night")

    def test_an_older_empty_consent_file_means_night(self):
        (self.mind / "dreams-enabled").write_text("")
        self.assertEqual(load_window(self.mind), "night")

    def test_camera_goes_through_being_camera(self):
        with (patch.object(care, "quiet", return_value=CompletedProcess([], 0, "Camera permission enabled.", ""))
              as run, self.keys("1")):
            care.camera_page(self.mind, False)
        run.assert_called_once_with([care.CAMERA_COMMAND, "enable"], timeout=40)
        with (patch.object(care, "quiet", return_value=CompletedProcess([], 1, "", "being-camera: no /mind")),
              self.assertRaisesRegex(ValueError, "no /mind")):
            care.set_camera(True)

    def test_workshop_is_on_by_default_and_switches_off(self):
        self.assertTrue(care.workshop_enabled(self.mind))
        with self.keys("1"):
            care.workshop_page(self.mind, False)
        self.assertFalse(care.workshop_enabled(self.mind))
        self.assertFalse((self.mind / care.APPS_ENABLED).exists())
        with self.keys("1"):
            care.workshop_page(self.mind, False)
        self.assertTrue((self.mind / care.APPS_ENABLED).exists())

    def test_memory_mode_shows_its_scope_and_asks_first(self):
        with self.keys("2", "n"):
            care.memory_page(self.mind, False)
        self.assertEqual(read_policy(self.mind).mode, "persistent")
        with self.keys("2", "y"):
            care.memory_page(self.mind, False)
        self.assertEqual(read_policy(self.mind).mode, "explicit-only")
        self.assertIn("I keep only what you ask me to remember.", self.out.getvalue())
        with self.keys("4", "30", "y"):
            care.memory_page(self.mind, False)
        self.assertEqual(read_policy(self.mind).expiry_days, 30)


class Remembering(CareTestCase):
    def test_forget_by_number_asks_like_the_voice_and_really_forgets(self):
        memory = Memory(self.mind)
        heard = memory.log("heard", "My cat is called Tom", speaker="Richard")
        memory.remember("My cat is called Tom", speaker="Richard", source=heard)
        memory.remember("My bike is red", speaker="Richard")
        memory.close()
        with self.keys("1", "n", ""):
            care.remember_page(self.mind, False)
        self.assertIn("I've kept everything.", self.out.getvalue())
        self.assertEqual(len(care.remembered(self.mind)), 2)
        with self.keys("1", "y", ""):
            care.remember_page(self.mind, False)
        self.assertEqual([text for _, text in care.remembered(self.mind)], ["My bike is red"])
        check = Memory(self.mind)
        self.addCleanup(check.close)
        self.assertFalse(check.contains("called Tom"), "the heard line it came from is blanked too")
        self.assertIn("Older backups still hold it", self.out.getvalue())

    def test_forgetting_a_fact_also_drops_reminders_that_hold_its_words(self):
        from mind.reminders import Reminders

        memory = Memory(self.mind)
        memory.remember("Call the dentist about my tooth", speaker="Richard")
        memory.close()
        reminders = Reminders(self.mind)
        reminders.add("call the dentist about my tooth", 2_000_000_000)
        reminders.add("water the plants", 2_000_000_000)
        reminders.close()
        with self.keys("1", "y"):
            care.remember_page(self.mind, False)
        reminders = Reminders(self.mind)
        self.addCleanup(reminders.close)
        self.assertEqual([entry.text for entry in reminders.upcoming()], ["water the plants"])

    def test_nothing_to_show_creates_no_archive(self):
        with self.keys(""):
            care.remember_page(self.mind, False)
        self.assertFalse((self.mind / "mind.db").exists())


class Clock(CareTestCase):
    def test_typed_times_are_read_in_the_owners_zone(self):
        sydney = ZoneInfo("Australia/Sydney")
        stamp = care.parse_local_time("2026-11-23 18:30", sydney)
        self.assertEqual(datetime.fromtimestamp(stamp, sydney).strftime("%Y-%m-%d %H:%M"), "2026-11-23 18:30")
        today = care.parse_local_time("07:05", sydney, today=datetime(2026, 1, 2).date())
        self.assertEqual(datetime.fromtimestamp(today, sydney).strftime("%Y-%m-%d %H:%M"), "2026-01-02 07:05")
        for bad in ("tomorrow", "1999-01-01 00:00"):
            with self.assertRaises(ValueError):
                care.parse_local_time(bad, sydney)

    def test_set_clock_reaches_the_system_clock_and_the_memory(self):
        Memory(self.mind).close()
        seen = []
        with patch.object(care.shutil, "which", return_value=None):
            care.set_clock(self.mind, 1_800_000_000.0, settime=seen.append)
        self.assertEqual(seen, [1_800_000_000.0])

    def test_a_clock_set_in_care_moves_running_timers_with_it(self):
        from mind.reminders import Reminders

        reminders = Reminders(self.mind)
        start = time.time()
        reminders.add("timer", start + 600, kind="timer", duration=600, now=start)
        reminders.add("call Mum", 1_900_000_000.0, now=start)
        reminders.db.close()
        with patch.object(care.shutil, "which", return_value=None), \
                patch.object(care.time, "time", return_value=start):
            care.set_clock(self.mind, start + 86_400.0, settime=lambda value: None)
        reminders = Reminders(self.mind)
        try:
            dues = dict(reminders.db.execute("SELECT kind, due_at FROM reminders").fetchall())
        finally:
            reminders.db.close()
        self.assertAlmostEqual(dues["timer"], start + 600 + 86_400.0, places=3)
        self.assertEqual(dues["reminder"], 1_900_000_000.0, "a reminder keeps its wall-clock time")

    def test_time_zone_page_saves_a_city(self):
        with self.keys("Sydney", "y"):
            care.zone_page(self.mind, False)
        self.assertEqual(json.loads((self.mind / "skills.json").read_text())["time_zone"], "Australia/Sydney")
        self.assertEqual(care.zone_of(self.mind).key, "Australia/Sydney")


class Sound(CareTestCase):
    SGET = ("Simple mixer control 'Master',0\n  Capabilities: pvolume pswitch\n"
            "  Mono: Playback 54 [74%] [-15.00dB] [on]\n")

    def test_levels_are_read_and_set_through_amixer(self):
        calls = []

        def fake(command, **_):
            calls.append(command)
            if command[-1] == "scontrols":
                return CompletedProcess(command, 0, "Simple mixer control 'Master',0\n"
                                                    "Simple mixer control 'Capture',0\n", "")
            return CompletedProcess(command, 0, self.SGET, "")

        with patch.object(care, "quiet", side_effect=fake), patch.dict(care.os.environ, {"BEING_ALSA_CARD": "PCH"}):
            names = care.controls()
            self.assertEqual((care.playback_control(names), care.capture_control(names)), ("Master", "Capture"))
            self.assertEqual(care.level("Master"), (74, True))
            self.assertEqual(care.set_level("Master", 140), 100)
        self.assertEqual(calls[-1], ["amixer", "-c", "PCH", "sset", "Master", "100%", "unmute"])

    def test_missing_amixer_is_explained(self):
        with (patch.object(care, "quiet", return_value=CompletedProcess([], 127, "", "")),
              patch.dict(care.os.environ, {"BEING_CONSOLE_UI": "1"}), self.keys("")):
            care.audio_page(self.mind, False)
        self.assertIn("no mixer tool", care.ui.plain(self.out.getvalue()))

    def test_tone_is_a_short_mono_wave(self):
        path = care.tone_file(self.mind / "tone.wav")
        with wave.open(str(path)) as sound:
            self.assertEqual((sound.getnchannels(), sound.getframerate()), (1, care.SAMPLE_RATE))
            self.assertAlmostEqual(sound.getnframes() / care.SAMPLE_RATE, care.TONE_SECONDS, places=2)

    def test_meter_reports_the_loudest_peak_and_a_busy_microphone(self):
        import array

        loud = array.array("h", [0, 16384, -8192] * 100).tobytes()
        stream = io.BytesIO(loud + bytes(3200))
        process = SimpleNamespace(stdout=stream, wait=lambda: 0)
        drawn = []
        with patch.object(care, "devices", return_value=("hw:0", "hw:0")):
            peak = care.mic_meter(1, popen=lambda *a, **k: process, draw=drawn.append)
        self.assertAlmostEqual(peak, 0.5, places=3)
        self.assertTrue(drawn)
        busy = SimpleNamespace(stdout=io.BytesIO(b""), wait=lambda: 1)
        with patch.object(care, "devices", return_value=("hw:0", "hw:0")), self.assertRaisesRegex(ValueError, "in use"):
            care.mic_meter(1, popen=lambda *a, **k: busy)


class Sticks(CareTestCase):
    def setUp(self):
        super().setUp()
        self.stick = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(care, "pick_stick", return_value="/dev/sdc1"))
        self.mount = self.enterContext(patch("mind.local_menu.mount_media", return_value=self.stick))
        self.unmount = self.enterContext(patch("mind.local_menu.unmount_media"))
        self.enterContext(patch.object(care.shutil, "which", return_value="/usr/bin/tool"))

    def test_update_is_read_only_checked_and_confirmed(self):
        (self.stick / "updates").mkdir()
        bundle = self.stick / "updates" / "pipos-1.2.raucb"
        bundle.write_bytes(b"bundle")
        calls = []

        def fake(command, **_):
            calls.append(command)
            if command[:2] == ["rauc", "info"]:
                return CompletedProcess(command, 0, json.dumps({"compatible": "beingos-laptop-installed-v1",
                                                                "version": "1.2"}), "")
            return CompletedProcess(command, 0, "", "")

        with patch.object(care, "quiet", side_effect=fake), patch.object(care, "run", side_effect=fake), \
                self.keys("1", "y", "n"):
            care.update_menu(try_boot=False)
        self.mount.assert_called_once_with("/dev/sdc1")  # read-only: no writable flag
        self.assertIn(["rauc", "install", str(bundle)], calls)
        self.assertTrue(self.unmount.called)
        self.assertNotIn(["systemctl", "reboot"], calls)
        self.assertIn("beingos-laptop-installed-v1", self.out.getvalue())

    def test_unsigned_update_never_installs(self):
        (self.stick / "bad.raucb").write_bytes(b"x")
        calls = []

        def fake(command, **_):
            calls.append(command)
            return CompletedProcess(command, 1, "", "signature verification failed")

        with patch.object(care, "quiet", side_effect=fake), patch.object(care, "run", side_effect=fake), \
                self.keys("1"), self.assertRaisesRegex(ValueError, "not signed"):
            care.update_menu(try_boot=False)
        self.assertFalse(any(command[:2] == ["rauc", "install"] for command in calls))

    def test_try_cannot_update(self):
        care.update_menu(try_boot=True)
        self.mount.assert_not_called()
        self.assertIn("installed being", self.out.getvalue())

    def test_backup_writes_to_a_new_folder_and_shows_the_fingerprint(self):
        (self.mind / care.RECIPIENT_FILE).write_text("age1" + "q" * 58 + "\n")
        result = {"snapshot": self.stick / care.BACKUP_FOLDER / "pip-1.age", "checkpoint": "ab" * 32}
        with (patch("mind.backup.backup_to_folder", return_value=result) as write, self.keys()):
            care.backup_menu(self.mind)
        self.mount.assert_called_once_with("/dev/sdc1", writable=True)
        self.assertEqual(write.call_args.args[1], self.stick / care.BACKUP_FOLDER)
        self.assertIn("abababab…abababab", self.out.getvalue())
        self.assertTrue(self.unmount.called)

    def test_a_backup_tool_failure_is_a_sentence_not_a_crash(self):
        from mind.backup_crypto import BackupCryptoError

        (self.mind / care.RECIPIENT_FILE).write_text("age1" + "q" * 58 + "\n")
        with (patch("mind.backup.backup_to_folder", side_effect=BackupCryptoError("Could not encrypt the backup.")),
              self.keys(), self.assertRaisesRegex(ValueError, "not made: Could not encrypt")):
            care.backup_menu(self.mind)
        self.assertTrue(self.unmount.called)

    def test_a_protected_key_waits_so_a_made_up_passphrase_can_be_written_down(self):
        def fake_key(path):
            Path(path).write_text("-----BEGIN AGE ENCRYPTED FILE-----\n")
            return "age1" + "z" * 58

        result = {"snapshot": self.stick / care.BACKUP_FOLDER / "pip-1.age", "checkpoint": "cd" * 32}
        with (patch("mind.backup_crypto.generate_protected_key", side_effect=fake_key),
              patch("mind.backup.backup_to_folder", return_value=result), self.keys("y", "") as answers):
            care.backup_menu(self.mind)
        self.assertEqual(answers.call_count, 2)
        self.assertIn("made-up passphrase", self.out.getvalue())
        self.assertTrue((self.stick / care.BACKUP_FOLDER / care.KEY_NAME).exists())

    def test_first_backup_makes_a_key_on_the_stick_and_keeps_only_the_public_half(self):
        def fake_key(path):
            Path(path).write_text("AGE-SECRET-KEY-1" + "Q" * 58 + "\n")
            return "age1" + "z" * 58

        result = {"snapshot": self.stick / care.BACKUP_FOLDER / "pip-1.age", "checkpoint": "cd" * 32}
        with (patch("mind.backup_crypto.generate_key", side_effect=fake_key),
              patch("mind.backup.backup_to_folder", return_value=result), self.keys("n")):
            care.backup_menu(self.mind)
        key = self.stick / care.BACKUP_FOLDER / "pip-backup-key.key"
        self.assertTrue(key.exists())
        self.assertEqual((self.mind / care.RECIPIENT_FILE).read_text().strip(), "age1" + "z" * 58)
        self.assertNotIn("SECRET", (self.mind / care.RECIPIENT_FILE).read_text())
        self.assertIn("same stick", self.out.getvalue())


class Page(CareTestCase):
    def test_care_page_lists_its_controls_and_hides_network_in_try(self):
        try_rows = {label for _, label, _ in care.summary_rows(self.mind, True)}
        installed_rows = {label for _, label, _ in care.summary_rows(self.mind, False)}
        for label in ("Camera", "Dreams", "Memory", "What I remember", "Dream journal", "Date and time",
                      "Time zone", "Workshop"):
            self.assertIn(label, try_rows)
        self.assertNotIn("Network", try_rows)
        self.assertIn("Network", installed_rows)
        with self.keys("q"):
            self.assertEqual(care.care_menu(self.mind), 0)

    def test_health_page_shows_the_check_up_rows(self):
        from mind import doctor

        class Machine(doctor.System):
            def __init__(self):
                self.systemctl = None

            def units(self, names):
                return {name: {"Id": name, "ActiveState": "failed" if name == "being-face.service" else "active",
                               "NRestarts": "0"} for name in names}

            def failed_units(self):
                return []

            def cortex_ok(self):
                return True

            def last_backup(self, state_dir):
                return None

            def restart(self, unit):
                raise AssertionError("the Health page only looks; it never repairs")

        self.assertIn("Health", {label for _, label, _ in care.summary_rows(self.mind, False)})
        with self.keys(""):
            care.health_page(self.mind, False, system=Machine())
        shown = self.out.getvalue()
        for row in ("face: stopped (failed)", "ears and voice: running", "last backup: never", "memory disk:",
                    "are you okay?"):
            self.assertIn(row, shown)

    def test_a_damaged_memory_setting_is_shown_as_damaged(self):
        (self.mind / "memory-policy.json").write_text("{not json")
        rows = {label: value for _, label, value in care.summary_rows(self.mind, False)}
        self.assertTrue(rows["Memory"].startswith("Damaged"), rows["Memory"])


if __name__ == "__main__":
    unittest.main()
