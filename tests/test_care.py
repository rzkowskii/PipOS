"""The Care page: owner controls that need no shell, driven with fakes."""

import io
import json
import os
import shutil
import tempfile
import time
import unittest
import wave
from contextlib import nullcontext, redirect_stdout
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
        self.assertTrue((self.mind / "dreams-enabled").is_file())
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
        self.assertEqual(care.dream_settings(self.mind), (True, "night"))

    def test_invalid_consent_does_not_enable_dreams_or_widen_upkeep(self):
        path = self.mind / care.DREAM_CONSENT
        path.write_text('{"version":1,"window":"always"}')
        self.assertEqual(care.dream_settings(self.mind), (False, "night"))
        self.assertEqual(load_window(self.mind), "night")
        care.set_dreams(self.mind, True, "closed")
        path.chmod(0o666)
        self.assertEqual(care.dream_settings(self.mind), (False, "night"))
        old = path.rename(self.mind / "old-consent")
        old.chmod(0o600)
        path.symlink_to(old)
        self.assertEqual(care.dream_settings(self.mind), (False, "night"))

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
    compatible = "beingos-laptop-installed-v1"
    manifest_hash = "34" * 32

    def setUp(self):
        super().setUp()
        self.stick = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(care, "pick_stick", return_value="/dev/sdc1"))
        self.enterContext(patch("mind.backup.media_identity", return_value="1234-ABCD"))
        self.enterContext(patch("mind.backup.mounted_from", return_value=True))
        from mind import backup
        backup.save_media_binding(self.mind, "1234-ABCD")
        self.mount = self.enterContext(patch("mind.local_menu.mount_media", return_value=self.stick))
        self.unmount = self.enterContext(patch("mind.local_menu.unmount_media"))
        self.update_media = self.enterContext(patch("mind.local_menu.update_media", return_value=nullcontext(self.stick)))
        self.enterContext(patch("mind.local_menu.release_previous_update_media"))
        self.enterContext(patch.object(care.shutil, "which", return_value="/usr/bin/tool"))
        config = self.mind / "installed-rauc.conf"
        config.write_text(f"[system]\ncompatible={self.compatible}\n", encoding="utf-8")
        self.enterContext(patch.object(care, "RAUC_INSTALLED_CONF", config))

    def verified_manifest(self, compatible=None):
        return json.dumps({"compatible": compatible or self.compatible, "version": "1.2",
                           "format": "verity", "hash": self.manifest_hash})

    def test_update_is_read_only_checked_and_confirmed(self):
        (self.stick / "updates").mkdir()
        bundle = self.stick / "updates" / "pipos-1.2.raucb"
        bundle.write_bytes(b"bundle")
        calls = []

        def fake(command, **_):
            calls.append(command)
            if "info" in command:
                return CompletedProcess(command, 0, self.verified_manifest(), "")
            return CompletedProcess(command, 0, "", "")

        with patch.object(care, "quiet", side_effect=fake), patch.object(care.subprocess, "run", side_effect=fake), \
                self.keys("1", "y", "n"):
            care.update_menu(self.mind, try_boot=False)
        self.update_media.assert_called_once_with("/dev/sdc1")
        self.mount.assert_not_called()
        self.unmount.assert_not_called()
        self.assertIn(["rauc", f"--conf={care.RAUC_INSTALLED_CONF}", "install",
                       f"--require-manifest-hash={self.manifest_hash}", str(bundle)], calls)
        # The menu logs the installation. RAUC records the target separately.
        from mind import action_log, device

        self.assertEqual([entry["kind"] for entry in action_log.entries(self.mind)], ["update.install"])
        good = self.mind / "slot-good"
        good.write_text("B\n")
        cmdline = self.mind / "cmdline"
        cmdline.write_text("rauc.slot=B quiet\n")
        self.assertIn("can't verify which slot", device.update_line(self.mind, cmdline=cmdline, slot_good=good))
        self.assertNotIn(["systemctl", "reboot"], calls)
        self.assertIn(self.compatible, self.out.getvalue())

    def test_unsigned_update_never_installs(self):
        (self.stick / "bad.raucb").write_bytes(b"x")

        def fake(command, **_):
            return CompletedProcess(command, 1, "", "signature verification failed")

        with (patch.object(care, "quiet", side_effect=fake), patch.object(care, "install_bundle") as install,
              self.keys("1"), self.assertRaisesRegex(ValueError, "not signed")):
            care.update_menu(self.mind, try_boot=False)
        install.assert_not_called()
        from mind import action_log

        self.assertEqual(action_log.entries(self.mind), [], "an update that never went in is not logged")

    def test_wrong_signed_channel_never_reaches_install_confirmation(self):
        (self.stick / "factory.raucb").write_bytes(b"bundle")
        result = CompletedProcess([], 0, self.verified_manifest("beingos-laptop-v1"), "")
        with (patch.object(care, "quiet", return_value=result), patch.object(care, "yes") as confirm,
              patch.object(care, "install_bundle") as install, self.keys("1"),
              self.assertRaisesRegex(ValueError, "different PipOS system")):
            care.update_menu(self.mind, try_boot=False)
        confirm.assert_not_called()
        install.assert_not_called()

    def test_a_failed_install_is_not_logged_as_an_update(self):
        (self.stick / "pipos-1.3.raucb").write_bytes(b"bundle")

        info = CompletedProcess([], 0, self.verified_manifest(), "")
        failed = CompletedProcess([], 1, "", "slot busy")
        with (patch.object(care, "quiet", return_value=info),
              patch.object(care.subprocess, "run", return_value=failed),
              self.keys("1", "y"), self.assertRaisesRegex(ValueError, "did not install")):
            care.update_menu(self.mind, try_boot=False)
        from mind import action_log

        self.assertEqual(action_log.entries(self.mind), [])

    def test_try_cannot_update(self):
        care.update_menu(try_boot=True)
        self.update_media.assert_not_called()
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

    def test_backup_media_enrollment_and_replacement_are_explicit(self):
        from mind import backup

        (self.mind / backup.MEDIA_BINDING_FILE).unlink()
        with self.keys("n"):
            care.write_backup(self.mind, temporary=False)
        self.assertIsNone(backup.media_binding(self.mind))
        with self.keys("y"), patch.object(care, "write_locked_backup", side_effect=ValueError("write failed")), \
                self.assertRaisesRegex(ValueError, "write failed"):
            care.write_backup(self.mind, temporary=False)
        self.assertIsNone(backup.media_binding(self.mind))
        with self.keys("y"), patch.object(care, "write_locked_backup", return_value=(
                {"snapshot": self.stick / "pip.age", "checkpoint": "ab" * 32}, False)), \
                patch.object(care, "show_backup"):
            care.write_backup(self.mind, temporary=False)
        self.assertEqual(backup.media_binding(self.mind), "1234-ABCD")

        with patch("mind.backup.media_identity", return_value="9876-DCBA"), self.keys("n"):
            care.write_backup(self.mind, temporary=False)
        self.assertEqual(backup.media_binding(self.mind), "1234-ABCD")

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
                      "Time zone", "Workshop", "Permissions"):
            self.assertIn(label, try_rows)
        self.assertNotIn("Network", try_rows)
        self.assertIn("Network", installed_rows)
        with self.keys("q"):
            self.assertEqual(care.care_menu(self.mind), 0)

    def test_permissions_reads_live_choices_and_try_stays_offline(self):
        from mind.initiative import InitiativePolicy, QuietHours, save_policy as save_initiative
        from mind.network_policy import INTERNET, NetworkPolicy, save_policy as save_network
        from mind.policy import load_capabilities

        care.private_marker(self.mind / care.CAMERA_MARKER)
        care.set_dreams(self.mind, True, "closed")
        care.set_workshop(self.mind, False)
        save_initiative(self.mind, InitiativePolicy(greetings=True, quiet_hours=QuietHours("22:00", "07:00")))
        save_network(self.mind, NetworkPolicy(INTERNET, web_lookups=True, system_network=True))
        rows = dict(load_capabilities(self.mind).rows())
        self.assertEqual(rows, {
            "Camera": "On", "Network": "Internet", "Web lookups": "Available", "Workshop": "Off",
            "Reminders": "On", "Greetings": "On", "Morning brief": "On", "Quiet hours": "22:00 to 07:00",
            "Dreams": "Whenever the lid is closed",
        })
        with self.keys("p", "q", "q"):
            care.care_menu(self.mind, temporary=True)
        self.assertIn("Quiet hours", self.out.getvalue())
        try_rows = dict(load_capabilities(self.mind, temporary=True).rows())
        self.assertEqual((try_rows["Network"], try_rows["Web lookups"]), ("Offline", "Off"))

    @staticmethod
    def health_machine():
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

        return Machine()

    def test_health_page_shows_the_check_up_rows(self):
        self.assertIn("Health", {label for _, label, _ in care.summary_rows(self.mind, False)})
        with self.keys(""), patch.dict(os.environ, {"BEING_BUS": str(self.mind / "no-bus.sock")}):
            started = time.monotonic()
            care.health_page(self.mind, False, system=self.health_machine())
            self.assertLess(time.monotonic() - started, 1.5, "no running being: no wait")
        shown = self.out.getvalue()
        for row in ("face: stopped (failed)", "ears and voice: running", "last backup: never", "memory disk:",
                    "are you okay?"):
            self.assertIn(row, shown)

    def test_health_page_reads_the_body_from_the_running_being(self):
        body = {"battery": 81, "health_pct": 55, "cycles": 900, "temp": 84}
        with self.keys(""):
            care.health_page(self.mind, False, system=self.health_machine(), facts=lambda: (body, {}))
        shown = self.out.getvalue()
        self.assertIn("battery health: 55%", shown)
        self.assertIn("84", shown)
        self.assertIn("how well can you hear me?", shown)

    def test_live_facts_takes_the_retained_body_state_within_the_wait(self):
        import asyncio
        import socket
        import threading

        folder = Path(tempfile.mkdtemp(prefix="bus", dir="/tmp"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = folder / "bus.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        hello = []

        def brainstem():
            connection, _ = server.accept()
            with connection:
                hello.append(json.loads(connection.makefile().readline()))
                connection.sendall(b'{"t":"state.changed","state":"idle"}\n'
                                   b'{"t":"body.lid","closed":false}\n'
                                   b'{"t":"body.state","battery":64,"temp":51,"health_pct":81}\n')
                connection.recv(1)
        thread = threading.Thread(target=brainstem, daemon=True)
        thread.start()
        try:
            body, voice = care.live_facts(wait=2.0, path=str(path))
        finally:
            server.close()
        self.assertEqual(body, {"battery": 64, "temp": 51, "health_pct": 81, "lid_closed": False})
        self.assertEqual(voice, {})
        self.assertEqual(hello[0]["sub"], list(care.HEALTH_TOPICS))
        self.assertNotIn("voice.level_request", json.dumps(hello), "only the mind may ask the voice")
        self.assertEqual(care.live_facts(path=str(folder / "missing.sock")), ({}, {}))

    def test_a_damaged_memory_setting_is_shown_as_damaged(self):
        (self.mind / "memory-policy.json").write_text("{not json")
        rows = {label: value for _, label, value in care.summary_rows(self.mind, False)}
        self.assertTrue(rows["Memory"].startswith("Damaged"), rows["Memory"])


if __name__ == "__main__":
    unittest.main()
