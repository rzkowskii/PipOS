import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mind import local_menu
from mind.initiative import InitiativePolicy, QuietHours, POLICY_PATH, save_policy
from mind.profile import PROFILE_FILE, Profile, save_profile


class LocalMenuTests(unittest.TestCase):
    def setUp(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mind_path = work / "mind"
        self.mountinfo = work / "mountinfo"
        self.mountinfo.write_text("23 22 0:56 / /mind rw,nosuid - tmpfs tmpfs rw,noswap\n")
        self.report = {
            "boot_disk": "/dev/sda", "model_disk": "/dev/sda", "layout_ready": True,
            "disks": [
                {"device": "/dev/loop0", "eligible": False, "model": "Loop", "size_bytes": 64 * 1024**2,
                 "reasons": ["unsupported_device"]},
                {"device": "/dev/sda", "eligible": False, "model": "Try USB", "size_bytes": 16 * 1024**3,
                 "reasons": ["boot_disk", "mounted"], "transport": "usb"},
                {"device": "/dev/sdb", "eligible": True, "model": "Intended SSD", "size_bytes": 32 * 1024**3,
                 "reasons": [], "transport": "sata", "device_number": "8:16", "device_sequence": 5,
                 "logical_sector_bytes": 512, "serial": "DISK-B"},
            ],
        }
        self.used = 256 * 1024**2
        self.try_boot = self.enterContext(patch.object(local_menu, "is_try_boot", return_value=True))
        self.tty = self.enterContext(patch("sys.stdin.isatty", return_value=True))
        self.enterContext(patch.object(local_menu, "MIND_DIR", self.mind_path))
        self.enterContext(patch.object(local_menu, "MOUNTINFO", self.mountinfo))
        self.setup = self.enterContext(patch.object(local_menu.setup, "main", return_value=0))
        self.scan = self.enterContext(patch.object(local_menu.installer, "scan", return_value=(self.report, self.used)))
        self.confirm = self.enterContext(patch.object(local_menu.installer, "confirm_installation"))
        self.which = self.enterContext(patch.object(local_menu.shutil, "which", return_value=None))

    def run_keys(self, keys):
        with (patch("builtins.input", side_effect=keys) as keyboard, redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors):
            result = local_menu.main()
        return result, output.getvalue(), errors.getvalue(), keyboard

    def test_try_restore_and_setup_actions_leave_installer_untouched(self):
        code, text, _, _ = self.run_keys(["3", "4", "1"])
        self.assertEqual(code, 0)
        self.assertIn("1  Try PipOS", text)
        self.assertIn("2  Install PipOS", text)
        self.assertIn("3  Restore a being", text)
        self.assertIn("Backup restore is still pending", text)
        self.setup.assert_called_once_with([])
        self.scan.assert_not_called()
        self.confirm.assert_not_called()

    def test_a_cable_stick_refuses_install_and_restore_before_any_disk_or_media(self):
        self.enterContext(patch.object(local_menu.installer, "cable_media", return_value=True))
        self.which.return_value = "/usr/bin/tool"     # restore would otherwise ask for media
        code, text, _, keyboard = self.run_keys(["2", "3", "q"])
        self.assertEqual(code, 0)
        self.assertEqual(text.count("This is a development stick. It can't install PipOS; use a release Try stick."),
                         2)
        self.assertEqual(keyboard.call_count, 3, "each refusal returns straight to the menu")
        self.scan.assert_not_called()
        self.confirm.assert_not_called()

    def test_try_menu_offers_talking_by_typing_and_how_to_talk(self):
        factory = Path(__file__).resolve().parent.parent / "config"
        with patch.object(local_menu.type_chat, "main", return_value=0) as typing, \
                patch.object(local_menu, "FACTORY_CONFIG", factory):
            code, text, _, _ = self.run_keys(["6", "q"])
        self.assertEqual(code, 0)
        typing.assert_called_once_with()
        text = local_menu.ui.plain(text)
        self.assertIn("6  Talk to Pip by typing", text)
        text = " ".join(text.split())
        self.assertIn('Say "Hey Pip", wait for a chirp, then talk. Or choose "Talk to Pip by typing".', text)
        self.assertEqual(text.count("Talk to Pip by typing"), 4, "on the menu and in the welcome, both times")
        self.setup.assert_not_called()
        self.confirm.assert_not_called()

    def test_typing_entry_and_welcome_use_the_beings_own_name_and_wake_phrase(self):
        save_profile(self.mind_path, replace(Profile(), name="Clover", wake_word="Hello Clover"))
        with patch.object(local_menu.type_chat, "main", return_value=0):
            _, text, _, _ = self.run_keys(["q"])
        text = " ".join(local_menu.ui.plain(text).split())
        self.assertIn("6 Talk to Clover by typing", text)
        self.assertIn('Say "Hello Clover", wait for a chirp, then talk. Or choose "Talk to Clover by typing".', text)

    def test_installed_opens_only_setup_and_requires_tty(self):
        self.try_boot.return_value = False
        code, text, _, keyboard = self.run_keys([])
        self.assertEqual(code, 0)
        self.assertNotIn("Install PipOS", text)
        keyboard.assert_not_called()
        self.setup.assert_called_once_with([])
        self.setup.reset_mock()
        self.tty.return_value = False
        code, _, errors, _ = self.run_keys([])
        self.assertEqual(code, 1)
        self.assertIn("interactive terminal", errors)
        self.setup.assert_not_called()
        self.confirm.assert_not_called()

    def test_only_eligible_disk_number_is_accepted_and_enter_cancels(self):
        code, text, _, _ = self.run_keys(["2", "/dev/sdb", "2", "", "q"])
        self.assertEqual(code, 0)
        self.assertIn("/dev/sda", text)
        self.assertIn("1  /dev/sdb", text)
        self.assertNotIn("/dev/loop0", text)
        self.assertEqual(text.count("Choose a numbered disk"), 2)
        self.confirm.assert_not_called()

    def test_chosen_profile_and_exact_plan_go_to_confirmation(self):
        profile = replace(Profile(), name="Clover", voice="awb")
        save_profile(self.mind_path, profile)
        (self.mind_path / "life.json").write_text("existing life record")
        (self.mind_path / "mind.db").write_text("trial conversation")
        code, text, _, _ = self.run_keys(["2", "1", "y", ""])
        self.assertEqual(code, 0)
        self.assertIn("The installed being starts with fresh memory", text)
        plan = local_menu.installer.installation_plan(self.report, self.used, "/dev/sdb")
        self.confirm.assert_called_once_with("/dev/sdb", expected_plan=plan, profile=profile, restore=None,
                                             flow=("Install", 3, 3))

    def test_persistent_or_linked_profile_is_not_sent_to_installer(self):
        outside = self.mind_path.parent / "other"
        save_profile(outside, Profile())
        self.mind_path.mkdir()
        (self.mind_path / PROFILE_FILE).symlink_to(outside / PROFILE_FILE)
        code, _, _, keyboard = self.run_keys(["2", "1", ""])
        self.assertEqual(code, 0)
        self.assertEqual(self.confirm.call_args.kwargs["profile"], None)
        self.assertNotIn("Keep this Try profile? [y/N] ", [call.args[0] for call in keyboard.call_args_list])

        self.mountinfo.write_text("23 22 8:5 / /mind rw,nosuid - ext4 /dev/sda5 rw\n")
        code, text, _, _ = self.run_keys(["4", "q"])
        self.assertEqual(code, 0)
        self.assertIn("temporary profile storage is unavailable", text)
        self.setup.assert_not_called()

    def test_try_initiative_carry_is_independent_of_try_profile(self):
        save_profile(self.mind_path, Profile())
        policy = InitiativePolicy(False, True, QuietHours("22:00", "07:00"))
        save_policy(self.mind_path, policy)
        code, text, _, _ = self.run_keys(["2", "1", "n", "y", ""])
        self.assertEqual(code, 0)
        self.assertIn("22:00 to 07:00", text)
        self.assertIsNone(self.confirm.call_args.kwargs["profile"])
        self.assertEqual(self.confirm.call_args.kwargs["initiative"], policy)
        code, _, _, _ = self.run_keys(["2", "1", "n", "n", ""])
        self.assertEqual(code, 0)
        self.assertNotIn("initiative", self.confirm.call_args.kwargs)

    def test_invalid_try_initiative_is_not_offered_or_seeded(self):
        self.mind_path.mkdir()
        invalid = self.mind_path / POLICY_PATH
        invalid.write_text('{"version":1,"reminders":"enabled"}')
        invalid.chmod(0o600)
        code, text, _, keyboard = self.run_keys(["2", "1", ""])
        self.assertEqual(code, 0)
        self.assertIn("The saved announcement settings could not be read", text)
        self.assertNotIn("Keep these Try announcement settings? [y/N] ", [call.args[0] for call in keyboard.call_args_list])
        self.assertNotIn("initiative", self.confirm.call_args.kwargs)

    def restore_fixture(self):
        self.which.return_value = "/usr/sbin/being-backup"
        files = tuple(self.mind_path.parent / name for name in ("owner snapshot.age", "identity.agekey", "latest.age"))
        for path in files:
            path.write_text("owner data")
        staged = self.mind_path.parent / "staged-mind"

        def confirm(_device, *, expected_plan, profile, restore, flow):
            self.assertEqual(flow, ("Restore", 4, 4))
            self.assertEqual(expected_plan["target"]["device"], "/dev/sdb")
            self.assertIsNone(profile)
            restore(staged)

        self.confirm.side_effect = confirm
        return files, staged

    def test_archive_restore_uses_pinned_checkpoint_and_never_falls_back(self):
        (snapshot, identity, proof), staged = self.restore_fixture()
        checkpoint = "a" * 64
        with (patch.object(local_menu.getpass, "getpass", side_effect=[str(snapshot), str(identity), str(proof), checkpoint]),
              patch.object(local_menu.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as backend):
            code, text, errors, _ = self.run_keys(["3", "1", "1", "q", "q"])
        self.assertEqual(code, 0)
        self.assertEqual(self.confirm.call_count, 1)
        backend.assert_called_once_with(
            ["/usr/sbin/being-backup", "restore", "--mind-dir", str(staged), "--input", str(snapshot),
             "--identity", str(identity), "--deletions", str(proof), "--current-checkpoint", checkpoint],
            stdin=local_menu.subprocess.DEVNULL, stdout=local_menu.subprocess.DEVNULL,
            stderr=local_menu.subprocess.DEVNULL, timeout=900, check=False,
        )
        self.assertIn("Backup restore failed", errors)
        self.assertIn("separately saved record", text)
        for private in (str(snapshot), str(identity), str(proof), checkpoint):
            self.assertNotIn(private, text + errors)

    def test_identity_only_restore_is_explicit_and_leaves_archive_empty(self):
        self.mind_path.mkdir()
        save_policy(self.mind_path, InitiativePolicy(False, True))
        (snapshot, identity, _), _ = self.restore_fixture()
        with (patch.object(local_menu.getpass, "getpass", side_effect=[str(snapshot), str(identity)]),
              patch.object(local_menu.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as backend):
            code, text, _, _ = self.run_keys(["3", "2", "1", ""])
        self.assertEqual(code, 0)
        self.assertIn("The conversation archive starts empty", text)
        self.assertNotIn("--deletions", backend.call_args.args[0])
        self.assertNotIn("--current-checkpoint", backend.call_args.args[0])
        self.assertNotIn("initiative", self.confirm.call_args.kwargs)

    def media_fixture(self):
        """A USB backup stick beside the SSD, with mounting replaced by a folder."""
        (snapshot, identity, proof), staged = self.restore_fixture()
        media = self.mind_path.parent / "media"
        (media / "backups").mkdir(parents=True)
        (media / ".Spotlight-V100").mkdir()
        (media / ".Spotlight-V100" / "store.db").write_text("index")
        picked = [media / "backups" / "being.age", media / "identity.agekey", media / "latest.age"]
        for path in picked:
            path.write_text("owner data")
        (media / "link.age").symlink_to("/etc/passwd")
        self.report["disks"].append({
            "device": "/dev/sdc", "eligible": True, "model": "Backup Stick", "size_bytes": 32 * 1024**3,
            "reasons": [], "transport": "usb", "device_number": "8:32", "device_sequence": 6,
            "logical_sector_bytes": 512, "serial": "DISK-C"})
        self.enterContext(patch.object(local_menu, "MEDIA_DIR", media))
        self.enterContext(patch.object(local_menu.install_disks, "volumes",
                                       return_value=[{"device": "/dev/sdc1", "size_bytes": 31 * 1024**3}]))
        self.unmount = self.enterContext(patch.object(local_menu, "unmount_media"))
        mounted = []

        def run(command, **_):
            if command[0] == "mount":
                mounted.append(command)
                # Once mounted, the real inventory stops offering the stick.
                stick = self.report["disks"][-1]
                stick["eligible"], stick["reasons"] = False, ["mounted"]
                return SimpleNamespace(returncode=0 if command[2] == "exfat" else 32)
            return SimpleNamespace(returncode=0)

        return picked, staged, mounted, run

    def test_restore_reads_a_backup_stick_and_never_offers_it_for_erasing(self):
        picked, staged, mounted, run = self.media_fixture()
        with patch.object(local_menu.subprocess, "run", side_effect=run) as backend:
            # Restore, identity only, media 1, backup file 1, identity file 2, disk 1, Enter.
            code, text, errors, _ = self.run_keys(["3", "2", "1", "1", "2", "1", ""])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual([command[2] for command in mounted], ["vfat", "exfat"])
        for command in mounted:
            options = command[4].split(",")
            self.assertTrue({"ro", "nodev", "nosuid", "noexec"} <= set(options), command)
            self.assertEqual(command[5], "/dev/sdc1")
        self.assertIn("1  backups/being.age", text)
        self.assertIn("2  identity.agekey", text)
        self.assertNotIn("Spotlight", text)
        self.assertNotIn("link.age", text)
        restore = backend.call_args_list[-1].args[0]
        self.assertEqual(restore[:8], ["/usr/sbin/being-backup", "restore", "--mind-dir", str(staged),
                                       "--input", str(picked[0]), "--identity", str(picked[1])])
        self.assertEqual(self.confirm.call_args.args[0], "/dev/sdb")
        self.assertIn("     /dev/sdc | Backup Stick", text, "listed as in use, without a number")
        self.assertIn("(contains a mounted filesystem)", text)
        self.assertTrue(self.unmount.called, "the media is released afterwards")

    def test_ext4_backup_media_is_mounted_without_replaying_its_journal(self):
        options = dict(local_menu.MEDIA_MOUNTS)["ext4"].split(",")
        self.assertIn("noload", options)
        self.assertIn("ro", options)

    def test_unreadable_media_is_reported_and_nothing_is_installed(self):
        _, _, _, _ = self.media_fixture()
        with patch.object(local_menu.subprocess, "run", return_value=SimpleNamespace(returncode=32)):
            code, _, errors, _ = self.run_keys(["3", "2", "1", "q", "q"])
        self.assertEqual(code, 0)
        self.assertIn("could not be read", errors)
        self.confirm.assert_not_called()

    def test_unreadable_restore_file_cannot_reach_confirmation(self):
        self.which.return_value = "/usr/sbin/being-backup"
        with patch.object(local_menu.getpass, "getpass", return_value="relative-private-path"):
            code, _, errors, _ = self.run_keys(["3", "1", "q", "q"])
        self.assertEqual(code, 0)
        self.assertIn("readable file on the mounted backup media", errors)
        self.assertNotIn("relative-private-path", errors)
        self.confirm.assert_not_called()


class ConsoleScreens(LocalMenuTests):
    """The same flows drawn for being-console, with BEING_CONSOLE_UI=1."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(local_menu.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68", "LINES": "32"}))
        local_menu.ui.discard_notes()
        self.addCleanup(local_menu.ui.discard_notes)

    @staticmethod
    def last_screen(text):
        return text.rsplit("\x1b[2J", 1)[-1]

    def prompts(self, keyboard):
        return [local_menu.ui.plain(call.args[0]) for call in keyboard.call_args_list]

    # The plain-text expectations of the inherited tests do not apply here.
    test_try_restore_and_setup_actions_leave_installer_untouched = None
    test_only_eligible_disk_number_is_accepted_and_enter_cancels = None
    test_chosen_profile_and_exact_plan_go_to_confirmation = None
    test_persistent_or_linked_profile_is_not_sent_to_installer = None
    test_try_initiative_carry_is_independent_of_try_profile = None
    test_invalid_try_initiative_is_not_offered_or_seeded = None
    test_archive_restore_uses_pinned_checkpoint_and_never_falls_back = None
    test_identity_only_restore_is_explicit_and_leaves_archive_empty = None
    test_restore_reads_a_backup_stick_and_never_offers_it_for_erasing = None
    test_unreadable_media_is_reported_and_nothing_is_installed = None
    test_unreadable_restore_file_cannot_reach_confirmation = None
    test_a_cable_stick_refuses_install_and_restore_before_any_disk_or_media = None

    def test_welcome_menu_keeps_its_keys_and_prompt(self):
        code, text, _, keyboard = self.run_keys(["q"])
        self.assertEqual(code, 0)
        screen = local_menu.ui.plain(self.last_screen(text))
        for entry in ("1  Try PipOS", "2  Install PipOS", "3  Restore a being", "4  Setup",
                      "5  Check hardware", "6  Talk to Pip by typing", "7  Back up to a USB stick",
                      "Q  Return to PipOS"):
            self.assertIn(entry, screen)
        self.assertTrue(self.prompts(keyboard)[0].endswith("Choice: "))
        self.assertTrue(self.prompts(keyboard)[0].rstrip().splitlines()[-1].strip().startswith("›"))

    def test_disk_cards_and_confirmed_install_keep_the_exact_plan(self):
        save_profile(self.mind_path, replace(Profile(), name="Clover"))
        code, text, _, keyboard = self.run_keys(["2", "9", "1", "y", ""])
        self.assertEqual(code, 0)
        prompts = self.prompts(keyboard)
        self.assertTrue(prompts[1].endswith("Disk number [Enter to go back]: "))
        self.assertTrue(prompts[-1].endswith("Press Enter to return to PipOS. "))
        self.assertIn("Choose a numbered disk", text)
        self.assertIn("Install · Step 1 of 3", text)
        self.assertIn("╭", text)
        self.assertIn("This is the USB stick PipOS is running from.", text)
        self.assertIn("Install · Step 2 of 3", text)
        plan = local_menu.installer.installation_plan(self.report, self.used, "/dev/sdb")
        self.confirm.assert_called_once_with("/dev/sdb", expected_plan=plan, profile=replace(Profile(), name="Clover"),
                                             restore=None, flow=("Install", 3, 3))

    def test_a_cable_stick_says_so_on_the_redrawn_menu(self):
        self.enterContext(patch.object(local_menu.installer, "cable_media", return_value=True))
        code, text, errors, _ = self.run_keys(["2", "q"])
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("This is a development stick. It can't install PipOS; use a release Try stick.",
                      " ".join(local_menu.ui.plain(self.last_screen(text)).split()))
        self.scan.assert_not_called()
        self.confirm.assert_not_called()

    def test_errors_stay_on_the_redrawn_menu(self):
        self.confirm.side_effect = ValueError("Confirmation did not match. The disk was left untouched.")
        code, text, errors, _ = self.run_keys(["2", "1", "q"])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertIn("Confirmation did not match", local_menu.ui.plain(self.last_screen(text)))

    def test_restore_from_media_reads_files_and_reports_failure_on_the_menu(self):
        picked, staged, _, run = self.media_fixture()

        def failing(command, **kwargs):
            if command[0] == "mount":
                return run(command, **kwargs)
            return SimpleNamespace(returncode=1)

        with patch.object(local_menu.subprocess, "run", side_effect=failing):
            code, text, _, keyboard = self.run_keys(["3", "2", "1", "1", "2", "1", "q", "q"])
        self.assertEqual(code, 0)
        prompts = self.prompts(keyboard)
        self.assertTrue(any(prompt.endswith("Encrypted backup file: ") for prompt in prompts))
        self.assertTrue(any(prompt.endswith("Private identity file: ") for prompt in prompts))
        self.assertIn("Restore identity and settings", text)
        failed = local_menu.ui.plain(text.split("\x1b[2J")[-2])
        self.assertIn("Restore · Step 1 of 4", failed)
        self.assertIn("Backup restore failed.", failed, "the error stays on the redrawn restore screen")
        self.assertTrue(self.unmount.called)

    def test_hardware_waits_for_enter_before_the_menu_returns(self):
        report = {"manufacturer": "Apple Inc.", "model": "MacBookPro9,2", "firmware": {"mode": "UEFI"},
                  "ram_mib": 7860, "battery": None, "lid": "open",
                  "audio": {"capture_pcm_detected": True, "playback_pcm_detected": False},
                  "input": {"keyboard_detected": True, "pointer_detected": None}}
        with patch.object(local_menu.installer, "hardware_report", return_value=report):
            code, text, _, keyboard = self.run_keys(["5", "", "q"])
        self.assertEqual(code, 0)
        self.assertTrue(self.prompts(keyboard)[1].endswith("Press Enter to go back. "))
        self.assertIn("MacBookPro9,2", text)


class PlainConsole(unittest.TestCase):
    """Without BEING_CONSOLE_UI=1 every helper is plain ASCII."""

    def setUp(self):
        environment = {key: value for key, value in local_menu.os.environ.items() if key != "BEING_CONSOLE_UI"}
        self.enterContext(patch.dict(local_menu.os.environ, environment, clear=True))
        self.ui = local_menu.ui

    def test_helpers_emit_no_escapes_or_glyphs(self):
        with redirect_stdout(io.StringIO()) as output:
            self.ui.screen("Choose a disk", context=self.ui.step(1, 3, "Install"), subtitle="Pick a disk.")
            self.ui.card(["one", self.ui.mark("success", "two")], title="Card")
            self.ui.options([("1", "Try PipOS", "hint")])
            self.ui.note("Saved.", "success")
            print(self.ui.bar(0.5, 10), self.ui.meter(1), self.ui.paint("x", "danger"), self.ui.prompt("Choice: "))
        text = output.getvalue()
        self.assertTrue(text.isascii(), text)
        self.assertNotIn("\x1b", text)
        self.assertIn("Install - Step 1 of 3 - Choose a disk", text)
        self.assertIn("  1  Try PipOS\n", text)
        self.assertIn("[#####-----]", text)
        self.assertIn("Choice: ", text)

    def test_styled_helpers_measure_and_pad_by_visible_width(self):
        with patch.dict(local_menu.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68"}):
            self.assertEqual(self.ui.width(), 64)
            styled = self.ui.paint("Erase", "danger", "bold")
            self.assertEqual(self.ui.visible_len(styled), 5)
            self.assertEqual(self.ui.visible_len(self.ui.bar(0.37, 40)), 40)
            self.assertEqual(self.ui.visible_len(self.ui.bar(1.0, 40)), 40)
            with redirect_stdout(io.StringIO()) as output:
                self.ui.card(["a long row " * 12, self.ui.mark("accent", "bullet " * 14)], title=styled)
            widths = {self.ui.visible_len(line) for line in output.getvalue().splitlines()}
            self.assertEqual(widths, {2 + 64})

    def test_stage_progress_prints_messages_unchanged_without_the_console(self):
        stages = (("Writing installation data.", "Writing", 1), ("Verifying installation data.", "Checking", 1))
        with redirect_stdout(io.StringIO()) as output:
            with self.ui.StageProgress("Installing", stages) as view:
                view.stage("Writing installation data.")
                for done in range(0, 101, 10):
                    view.advance(done, 100)
                self.assertAlmostEqual(view.fraction(), 0.5)
                view.stage("Verifying installation data.")
                view.stage("Something else.")
        self.assertEqual(output.getvalue(), "Writing installation data.\n  25%\n  50%\n  75%\n"
                                            "Verifying installation data.\nSomething else.\n")

    def test_stage_progress_draws_ticks_and_a_bar_in_place(self):
        stages = (("A.", "First", 1), ("B.", "Second", 3))
        with patch.dict(local_menu.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68"}):
            with redirect_stdout(io.StringIO()) as output:
                with self.ui.StageProgress("Installing", stages) as view:
                    view.stage("A.")
                    view.stage("B.")
                    view.advance(1, 3)
                    self.assertAlmostEqual(view.fraction(), 0.5)
            text = output.getvalue()
        frame = self.ui.plain(text.rsplit("\x1b[H", 1)[-1])
        self.assertIn("✓  First", frame)
        self.assertIn("✓  Second", frame, "a finished view ticks every stage")
        self.assertIn("100%", frame)
        self.assertEqual(text.count("\x1b[2J"), 1, "later frames redraw in place without clearing")
        self.assertTrue(text.endswith("\x1b[?25h"))

    def test_styled_screen_leaves_the_brand_to_being_console(self):
        with patch.dict(local_menu.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68", "LINES": "26"}):
            with redirect_stdout(io.StringIO()) as output:
                self.ui.screen("Choose a disk", context=self.ui.step(1, 3, "Install"), subtitle="Pick a disk.")
            lines = self.ui.plain(output.getvalue()).split("\x1b[H", 1)[-1].splitlines()
        self.assertNotIn("PipOS", lines[0], "being-console draws the eyes and wordmark above the text")
        self.assertTrue(lines[0].strip().startswith("Choose a disk"))
        self.assertTrue(lines[0].endswith("Install · Step 1 of 3"))
        self.assertEqual(self.ui.visible_len(lines[0]), 2 + 64, "the step sits at the right edge")

    def test_older_plain_screens_are_drawn_at_the_margin_with_their_words_unchanged(self):
        def submenu():
            print("\nQuiet hours and announcements")
            print("Quiet hours pause automatic speech.")
            print("\n  1  Toggle automatic reminders\n  K  Save and go back")
            return input("Choice: ")

        for styled in (False, True):
            environment = {"BEING_CONSOLE_UI": "1", "COLUMNS": "68"} if styled else {}
            with patch.dict(local_menu.os.environ, environment), patch("sys.stdin", io.StringIO("k\n")):
                with redirect_stdout(io.StringIO()) as output:
                    with self.ui.indented(skip="Quiet hours and announcements"):
                        answer = submenu()
            self.assertEqual(answer, "k")
            text = self.ui.plain(output.getvalue())
            self.assertIn("Choice: ", text)
            if not styled:
                self.assertEqual(text.count("Quiet hours and announcements"), 1)
                self.assertIn("  1  Toggle automatic reminders\n", text)
                continue
            self.assertNotIn("Quiet hours and announcements", text, "the styled header already shows it")
            self.assertTrue(text.startswith("  Quiet hours pause automatic speech.\n"), text)
            self.assertIn("   1  Toggle automatic reminders\n", text)
            self.assertTrue(text.endswith("  › Choice: "), text)

    def test_long_menus_fold_into_two_columns_only_when_the_screen_is_short(self):
        entries = [(str(number), f"Entry {number}", "hint") for number in range(1, 12)]
        for rows, expected in (("40", 11), ("26", 6)):
            with patch.dict(local_menu.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68", "LINES": rows}):
                with redirect_stdout(io.StringIO()) as output:
                    self.ui.screen("Setup")
                    for _ in range(12):
                        self.ui.line("filler")
                    self.ui.options(entries)
            menu = self.ui.plain(output.getvalue()).splitlines()[-expected:]
            self.assertEqual(len([line for line in menu if "Entry" in line]), expected, rows)
            text = self.ui.plain(output.getvalue())
            for number in range(1, 12):
                self.assertIn(f"{number}  Entry {number}", text)


class TryHomecoming(LocalMenuTests):
    """Bring Pip home, the time zone, the Try profile default and the checkpoint record."""

    # Inherited flows are covered above.
    for _name in [name for name in dir(LocalMenuTests) if name.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_bring_pip_home_carries_the_try_being_through_the_restore_path(self):
        save_profile(self.mind_path, replace(Profile(), name="Clover"))
        carried = []
        with (patch("mind.backup.has_conversation", return_value=True),
              patch("mind.backup.carry", side_effect=lambda source, target: carried.append((source, target)))):
            code, text, _, keyboard = self.run_keys(["2", "1", "y", ""])
            self.assertEqual(code, 0)
            kwargs = self.confirm.call_args.kwargs
            self.assertTrue(kwargs["carried"])
            self.assertIsNone(kwargs["profile"])
            kwargs["restore"](Path("/staged"))
        self.assertEqual(carried, [(self.mind_path, Path("/staged"))])
        self.assertIn("Keep what you and Pip talked about? [y/N] ", [call.args[0] for call in keyboard.call_args_list])

    def test_declining_bring_pip_home_offers_the_profile_with_yes_as_default(self):
        save_profile(self.mind_path, replace(Profile(), name="Clover"))
        with patch("mind.backup.has_conversation", return_value=True):
            code, _, _, keyboard = self.run_keys(["2", "1", "n", "", ""])
        self.assertEqual(code, 0)
        prompts = [call.args[0] for call in keyboard.call_args_list]
        self.assertIn("Keep this Try profile? [Y/n] ", prompts)
        self.assertEqual(self.confirm.call_args.kwargs["profile"].name, "Clover")
        self.assertNotIn("carried", self.confirm.call_args.kwargs)

    def test_try_time_zone_is_offered_and_seeded(self):
        self.mind_path.mkdir()
        (self.mind_path / "skills.json").write_text('{"time_zone": "Australia/Sydney"}')
        code, _, _, _ = self.run_keys(["2", "1", "", ""])
        self.assertEqual(code, 0)
        self.assertEqual(self.confirm.call_args.kwargs["skills"], {"time_zone": "Australia/Sydney"})

    def test_checkpoint_record_is_confirmed_by_its_fingerprint(self):
        from mind import backup

        picked, staged, _, run = self.media_fixture()
        record = picked[0].parent.parent / "pip-checkpoint.txt"
        record.write_text(backup.checkpoint_text("2b" * 16, "c" * 64, 1))
        with (patch.object(local_menu.subprocess, "run", side_effect=run) as backend,
              patch.object(local_menu.getpass, "getpass") as hidden):
            # Archive restore, media 1, backup 1, key 2, proof 4, record 5, confirm, disk 1, Enter.
            files = local_menu.media_files(picked[0].parent.parent)
            number = str(files.index(record) + 1)
            code, text, errors, _ = self.run_keys(["3", "1", "1", "1", "2", "3", number, "y", "1", ""])
        self.assertEqual((code, errors), (0, ""))
        hidden.assert_not_called()
        self.assertIn("cccccccc…cccccccc", text)
        self.assertIn("same stick", text)
        restore = backend.call_args_list[-1].args[0]
        self.assertEqual(restore[-2:], ["--current-checkpoint", "c" * 64])

    def test_a_wrong_record_file_is_asked_again(self):
        from mind import backup

        picked, staged, _, run = self.media_fixture()
        record = picked[0].parent.parent / "pip-checkpoint.txt"
        record.write_text(backup.checkpoint_text("2b" * 16, "d" * 64, 1))
        with (patch.object(local_menu.subprocess, "run", side_effect=run) as backend,
              patch.object(local_menu.getpass, "getpass") as hidden):
            files = local_menu.media_files(picked[0].parent.parent)
            wrong, number = str(files.index(picked[0]) + 1), str(files.index(record) + 1)
            code, text, errors, _ = self.run_keys(["3", "1", "1", "1", "2", "3", wrong, number, "y", "1", ""])
        self.assertEqual((code, errors), (0, ""))
        hidden.assert_not_called()
        self.assertIn("not a checkpoint record", text)
        self.assertEqual(backend.call_args_list[-1].args[0][-2:], ["--current-checkpoint", "d" * 64])

    def test_a_passphrase_key_is_unlocked_into_ram_before_any_progress_screen(self):
        from mind.backup_crypto import BackupCryptoError

        folder = self.mind_path.parent
        key = folder / "key.age"
        key.write_text("-----BEGIN AGE ENCRYPTED FILE-----\nYWdl\n-----END AGE ENCRYPTED FILE-----\n")

        def unlock(path, destination):
            Path(destination).write_text("AGE-SECRET-KEY-1" + "Q" * 58 + "\n")
            return Path(destination)

        with patch("mind.backup_crypto.unlock_identity", side_effect=unlock) as opened, \
                redirect_stdout(io.StringIO()) as output:
            path, holder = local_menu.unlock_if_protected(key)
        opened.assert_called_once()
        self.assertIn("passphrase", output.getvalue())
        self.assertNotEqual(path, key)
        self.assertTrue(path.is_file())
        holder.cleanup()
        self.assertFalse(path.exists(), "the unlocked key does not outlive the restore")
        plain = folder / "plain.key"
        plain.write_text("AGE-SECRET-KEY-1" + "Q" * 58 + "\n")
        self.assertEqual(local_menu.unlock_if_protected(plain), (plain, None))
        with (patch("mind.backup_crypto.unlock_identity", side_effect=BackupCryptoError("Wrong passphrase.")),
              redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Nothing was restored")):
            local_menu.unlock_if_protected(key)

    def test_try_hint_follows_whether_diagnostics_are_really_kept(self):
        for logged, expected in ((True, local_menu.TRY_HINT_LOGGED), (False, local_menu.TRY_HINT),
                                 (None, local_menu.TRY_HINT)):
            with self.subTest(logged=logged), patch.object(local_menu.usb_logs, "logging_enabled",
                                                           return_value=logged):
                self.assertEqual(local_menu.menu_entries()[0][2], expected)

    def test_try_menu_offers_backup_and_says_what_diagnostics_keep(self):
        folder = self.mind_path.parent / "boot-0001"
        folder.mkdir()
        with (patch.object(local_menu.usb_logs, "current_folder", return_value=folder),
              patch("mind.care.backup_menu") as backup_menu):
            code, text, _, _ = self.run_keys(["7", "q"])
        self.assertEqual(code, 0)
        backup_menu.assert_called_once_with(self.mind_path, temporary=True)
        self.assertIn("7  Back up to a USB stick", text)


class TryMenuFits(unittest.TestCase):
    def test_every_try_hint_shares_a_line_with_its_label(self):
        from mind import console_ui
        entries = list(local_menu.MENU) + [("1", "Try PipOS", local_menu.TRY_HINT_LOGGED)]
        label_width = max(len(entry[1]) for entry in entries) + 3
        longest = max(len(entry[2]) for entry in entries if len(entry) > 2 and entry[2])
        self.assertLessEqual(4 + label_width + longest, console_ui.MAX_WIDTH,
                             "a hint this long pushes every hint below its label and the menu overflows the panel")


class CameraCheckTests(unittest.TestCase):
    """The 10 s camera check always turns the camera off again."""

    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(local_menu, "MIND_DIR", self.mind))
        self.calls = []

    def run_camera(self, argv, **kwargs):
        self.calls.append(argv)
        return SimpleNamespace(returncode=0)

    def test_ctrl_c_during_the_wait_still_disables(self):
        with patch.object(local_menu.subprocess, "run", side_effect=self.run_camera), \
                patch.object(local_menu.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                local_menu.camera_check(10)
        self.assertEqual(self.calls, [["being-camera", "enable"], ["being-camera", "disable"]])

    def test_a_hung_enable_still_disables(self):
        import subprocess

        def run(argv, **kwargs):
            self.calls.append(argv)
            if argv[1] == "enable":
                raise subprocess.TimeoutExpired(argv, 40)
            raise subprocess.TimeoutExpired(argv, 40)  # the disable failing is swallowed
        with patch.object(local_menu.subprocess, "run", side_effect=run), patch.object(local_menu.time, "sleep"):
            with self.assertRaises(subprocess.TimeoutExpired):
                local_menu.camera_check(10)
        self.assertEqual(self.calls[-1], ["being-camera", "disable"])

    def test_a_camera_that_was_on_stays_on(self):
        (self.mind / "camera.enabled").write_text("")
        with patch.object(local_menu.subprocess, "run", side_effect=self.run_camera), \
                patch.object(local_menu.time, "sleep"):
            self.assertTrue(local_menu.camera_check(10))
        self.assertEqual(self.calls, [["being-camera", "enable"]])
