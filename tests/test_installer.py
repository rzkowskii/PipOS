import re
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mind import install_status, installer
from mind.installer import ALIGNMENT, GIB, MIB, installation_plan, minimum_target


def report(capacity=8 * GIB, sector=512, reasons=()):
    return {
        "boot_disk": "/dev/sda",
        "disks": [{
            "device": "/dev/sdb",
            "size_bytes": capacity,
            "logical_sector_bytes": sector,
            "eligible": not reasons,
            "reasons": reasons,
        }],
    }


class LayoutPreview(unittest.TestCase):
    def test_layout_is_aligned_on_512_and_4096_byte_disks(self):
        used = 531 * MIB
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                capacity = 8 * GIB + sector
                plan = installation_plan(report(capacity, sector), used, "/dev/sdb")
                parts = plan["partitions"]
                self.assertEqual([part["name"] for part in parts], ["esp", "system-a", "system-b", "models", "mind"])
                self.assertEqual([part["size_bytes"] for part in parts[:3]], [512 * MIB, 2 * GIB, 2 * GIB])
                self.assertEqual(parts[3]["size_bytes"], 2 * used + 96 * MIB)
                cursor = ALIGNMENT
                for part in parts:
                    self.assertEqual(part["start_lba"] * sector, cursor)
                    self.assertEqual(part["sector_count"] * sector, part["size_bytes"])
                    cursor += part["size_bytes"]
                    self.assertEqual(cursor % ALIGNMENT, 0)
                self.assertEqual(cursor, 8 * GIB - ALIGNMENT)

    def test_minimum_leaves_a_full_gib_for_memory(self):
        used = 640 * MIB
        capacity = minimum_target(used)
        with self.assertRaisesRegex(ValueError, "too small"):
            installation_plan(report(capacity - MIB), used, "/dev/sdb")
        self.assertEqual(installation_plan(report(capacity), used, "/dev/sdb")["partitions"][-1]["size_bytes"], GIB)

    def test_partition_bar_uses_plan_sizes_and_names_even_if_a_partition_is_too_small_to_draw(self):
        for capacity in [8 * GIB, 512 * GIB]:
            with self.subTest(capacity=capacity), patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1"}):
                plan = installation_plan(report(capacity), 531 * MIB, "/dev/sdb")
                sizes = [part["size_bytes"] for part in plan["partitions"]]
                width = 64
                bar, *legend = installer.partition_rows(plan, width)
                boundaries = [0] + [round(sum(sizes[:i + 1]) / sum(sizes) * width) for i in range(len(sizes))]
                expected = [end - start for start, end in zip(boundaries, boundaries[1:]) if end > start]
                runs = re.findall(r"\x1b\[[0-9;]+m([█▓░]+)\x1b\[0m", bar)
                self.assertEqual([len(run) for run in runs], expected)
                self.assertEqual(installer.ui.visible_len(bar), width)
                labels = " ".join(installer.ui.plain(line) for line in legend)
                for label, size in zip(["Startup", "System A", "System B", "Models", "Memory"], sizes):
                    self.assertIn(f"{label} {installer.size_label(size)}", labels)
                self.assertTrue(all(installer.ui.visible_len(line) <= width for line in legend))

    def test_refuses_in_use_targets_and_unknown_model_or_sector_geometry(self):
        with self.assertRaisesRegex(ValueError, "mounted filesystem"):
            installation_plan(report(reasons=("mounted",)), 0, "/dev/sdb")
        with self.assertRaisesRegex(ValueError, "whole disk"):
            installation_plan(report(), 0, "/dev/sdb1")
        with self.assertRaisesRegex(ValueError, "model partition"):
            installation_plan(report(), None, "/dev/sdb")
        with self.assertRaisesRegex(ValueError, "sector geometry"):
            installation_plan(report(sector=None), 0, "/dev/sdb")


class InstallConfirmation(unittest.TestCase):
    def setUp(self):
        self.report = report()
        self.report["disks"][0].update(model="Target SSD", transport="sata", serial="TARGET")
        self.report["disks"].append({"device": "/dev/sda", "transport": "usb"})
        self.enterContext(patch.object(installer, "is_try_boot", return_value=True))
        self.enterContext(patch.object(installer.os, "geteuid", return_value=0))
        self.input_is_tty = self.enterContext(patch.object(installer.sys.stdin, "isatty", return_value=True))
        self.scan = self.enterContext(patch.object(installer, "scan", return_value=(self.report, 0)))
        # A blank, readable target unless a test says otherwise.
        self.enterContext(patch.object(installer, "disk_contents", return_value={
            "macos": False, "being": False, "partitions": 0, "readable": True}))
        self.write = Mock(side_effect=lambda plan, recheck, **_: recheck())
        self.cable = Path(self.enterContext(tempfile.TemporaryDirectory())) / "cable"
        self.enterContext(patch.dict("sys.modules", {"mind.install_apply": SimpleNamespace(
            apply_installation=self.write, CABLE_MEDIA=self.cable)}))
        self.enterContext(patch.object(installer, "cable_media", lambda: self.cable.exists()))
        self.output = self.enterContext(redirect_stdout(StringIO()))

    def test_install_from_a_pipe_cannot_start_the_writer(self):
        self.input_is_tty.return_value = False
        with self.assertRaisesRegex(ValueError, "interactive root console"):
            installer.confirm_installation("/dev/sdb")
        self.scan.assert_not_called()
        self.write.assert_not_called()

    def test_a_cable_stick_says_so_before_any_disk_is_listed(self):
        self.cable.mkdir()
        with patch("builtins.input") as keyboard:
            with self.assertRaisesRegex(ValueError, "^This is a development stick. It can't install PipOS; "
                                                    "use a release Try stick.$"):
                installer.confirm_installation("/dev/sdb")
        keyboard.assert_not_called()
        self.scan.assert_not_called()
        self.write.assert_not_called()

    def test_the_installer_menu_refuses_on_a_cable_stick_and_returns_to_its_menu(self):
        self.cable.mkdir()
        with patch("builtins.input", side_effect=["3", "q"]) as keyboard:
            installer.menu()
        self.assertEqual(keyboard.call_count, 2)
        self.assertIn(installer.CABLE_MESSAGE, self.output.getvalue())
        self.scan.assert_not_called()
        self.write.assert_not_called()

    def test_install_on_the_command_line_refuses_on_a_cable_stick(self):
        self.cable.mkdir()
        with redirect_stderr(StringIO()) as errors:
            self.assertEqual(installer.main(["--install", "/dev/sdb"]), 1)
        self.assertEqual(errors.getvalue(), f"being-installer: {installer.CABLE_MESSAGE}\n")
        self.scan.assert_not_called()

    def test_writer_requires_the_exact_device_challenge(self):
        with patch("builtins.input", return_value="ERASE /dev/sda"):
            with self.assertRaisesRegex(ValueError, "Confirmation did not match"):
                installer.confirm_installation("/dev/sdb")
        self.write.assert_not_called()

        with patch("builtins.input", return_value="ERASE /dev/sdb") as confirm:
            installer.confirm_installation("/dev/sdb")
        confirm.assert_called_once_with("Type ERASE /dev/sdb to install: ")
        self.assertIn("Installation complete.", self.output.getvalue())

    def test_encrypted_choice_forwards_only_the_in_memory_pin_after_same_challenge(self):
        from mind import mind_encryption

        pin = bytearray(b"386047195275")
        second = bytearray(pin)
        seen = []
        self.write.side_effect = lambda *args, **kwargs: seen.append(bytes(kwargs["mind_pin"]))
        with (patch.object(installer.os, "access", return_value=True),
              patch.object(mind_encryption, "read_pin", side_effect=[pin, second]),
              patch("builtins.input", side_effect=["e", "ERASE /dev/sdb"]) as confirm):
            installer.confirm_installation("/dev/sdb")
        self.assertIs(self.write.call_args.kwargs.get("mind_pin"), pin)
        self.assertEqual(seen, [b"386047195275"])
        self.assertEqual([call.args[0] for call in confirm.call_args_list], ["Type ERASE /dev/sdb to install: "] * 2)
        self.assertNotIn("386047195275", self.output.getvalue())
        self.assertFalse(pin)
        self.assertFalse(second)

    def test_protected_install_mismatch_leaves_the_target_untouched(self):
        from mind import mind_encryption

        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                first = bytearray(b"386047195275")
                other = None if cancelled else bytearray(b"386047195276")
                with (patch.object(installer.os, "access", return_value=True),
                      patch.object(mind_encryption, "read_pin", side_effect=[first, other]),
                      patch("builtins.input", return_value="e")):
                    with self.assertRaisesRegex(ValueError, "did not match"):
                        installer.confirm_installation("/dev/sdb")
                self.write.assert_not_called()
                self.assertFalse(first)
                self.assertFalse(other)

    def test_revalidation_detects_replacement_after_confirmation(self):
        replaced = {**self.report, "disks": [{**self.report["disks"][0], "serial": "CHANGED"}, self.report["disks"][1]]}
        self.scan.side_effect = [(self.report, 0), (replaced, 0)]
        with patch("builtins.input", return_value="ERASE /dev/sdb"):
            with self.assertRaisesRegex(ValueError, "changed after confirmation"):
                installer.confirm_installation("/dev/sdb")
        self.assertNotIn("Installation complete.", self.output.getvalue())

    def test_console_confirmation_keeps_the_challenge_and_reports_progress(self):
        seen = []
        spoken = []

        def write(plan, recheck, *, progress, meter, expected_contents):
            recheck()
            for index, (stage, _, _) in enumerate(installer.INSTALL_STAGES):
                progress(stage)
                if index in {1, 2}:
                    meter(50, 100)
                seen.append(stage)
            progress("Private label and progress details")

        self.write.side_effect = write
        self.enterContext(patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68"}))
        token = install_status.sink.set(spoken.append)
        self.addCleanup(install_status.sink.reset, token)
        with patch("builtins.input", return_value="ERASE /dev/sdb") as confirm:
            installer.confirm_installation("/dev/sdb", flow=("Install", 2, 2))
        prompt = installer.ui.plain(confirm.call_args.args[0])
        self.assertTrue(prompt.endswith("Type ERASE /dev/sdb to install: "), prompt)
        self.assertEqual(seen, [stage for stage, _, _ in installer.INSTALL_STAGES])
        self.assertEqual(spoken, ["prepare", "write", "verify", "commit", "complete"])
        text = installer.ui.plain(self.output.getvalue())
        self.assertIn("Install · Step 2 of 2", text)
        self.assertIn("replaces all data", text)
        self.assertIn("Target SSD", text)
        self.assertIn("Copied 50 / 100 bytes", text)
        self.assertIn("Checked 50 / 100 bytes", text)
        self.assertIn("Overall estimate", text)
        self.assertIn("\x1b]7772;install=0\a", text)
        self.assertIn("\x1b]7772;install=100\a", text)
        self.assertIn("\x1b]7772;install=off\a", text)
        self.assertIn(installer.POWER_CUT_HELP, " ".join(text.split()))
        finish = text.rsplit("Installation complete.", 1)
        self.assertEqual(len(finish), 2)
        self.assertIn("Remove the USB stick.", finish[1])

    def test_narration_waits_for_both_confirmations_and_reports_only_a_fixed_failure(self):
        spoken = []
        token = install_status.sink.set(spoken.append)
        self.addCleanup(install_status.sink.reset, token)

        def write(_plan, _recheck, *, progress, meter, expected_contents):
            self.assertEqual(spoken, [])
            progress(installer.INSTALL_STAGES[1][0])
            raise OSError("a private path")

        self.write.side_effect = write
        with patch("mind.installer.contents_warning", return_value=([], "data")), \
                patch("builtins.input", return_value="ERASE /dev/sdb"), \
                patch("mind.installer.second_confirmation", side_effect=ValueError("second word did not match")):
            with self.assertRaisesRegex(ValueError, "second word"):
                installer.confirm_installation("/dev/sdb")
        self.assertEqual(spoken, [])
        self.write.assert_not_called()

        with patch("mind.installer.contents_warning", return_value=([], "data")), \
                patch("builtins.input", return_value="ERASE /dev/sdb"), \
                patch("mind.installer.second_confirmation") as confirmed:
            with self.assertRaisesRegex(ValueError, "stopped during disk writing"):
                installer.confirm_installation("/dev/sdb")
        confirmed.assert_called_once_with("data")
        self.assertEqual(spoken, ["write", "stopped"])

    def test_failed_install_progress_does_not_use_a_completion_frame(self):
        with patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1"}):
            view = installer.InstallProgress("Installing", installer.INSTALL_STAGES)
            with self.assertRaisesRegex(OSError, "disk stopped"):
                with view:
                    view.stage(installer.INSTALL_STAGES[1][0])
                    view.advance(4096, 8192)
                    raise OSError("disk stopped")
        text = self.output.getvalue()
        self.assertNotIn("\x1b]7772;install=100\a", text)
        self.assertIn("\x1b]7772;install=off\a", text)

    def test_console_mismatch_writes_nothing(self):
        self.enterContext(patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1"}))
        with patch("builtins.input", return_value=""):
            with self.assertRaisesRegex(ValueError, "left untouched"):
                installer.confirm_installation("/dev/sdb")
        self.write.assert_not_called()


class TargetSafety(InstallConfirmation):
    """A drive that holds macOS or a being needs a second, distinct confirmation."""

    def contents(self, **found):
        value = {"macos": False, "being": False, "partitions": 1, "readable": True, **found}
        self.enterContext(patch.object(installer, "disk_contents", return_value=value))

    def test_unreadable_target_is_never_treated_as_empty(self):
        self.contents(readable=False, partitions=0)
        with patch("builtins.input", side_effect=["ERASE /dev/sdb", ""]):
            with self.assertRaisesRegex(ValueError, "left untouched"):
                installer.confirm_installation("/dev/sdb")
        self.write.assert_not_called()
        self.assertIn("couldn't read what this drive holds", self.output.getvalue())
        with patch("builtins.input", side_effect=["ERASE /dev/sdb", "ERASE DRIVE"]):
            installer.confirm_installation("/dev/sdb")
        self.write.assert_called_once()

    def test_macos_target_shows_the_checklist_and_needs_erase_macos(self):
        self.contents(macos=True)
        with patch("builtins.input", side_effect=["ERASE /dev/sdb", "ERASE /dev/sdb"]):
            with self.assertRaisesRegex(ValueError, "left untouched"):
                installer.confirm_installation("/dev/sdb")
        self.write.assert_not_called()
        self.assertIn("This drive holds macOS.", self.output.getvalue())
        self.assertIn("battery cycle count", self.output.getvalue())
        with patch("builtins.input", side_effect=["ERASE /dev/sdb", "ERASE MACOS"]) as confirm:
            installer.confirm_installation("/dev/sdb")
        self.assertEqual(confirm.call_args.args[0], "Type ERASE MACOS to erase macOS: ")
        self.write.assert_called_once()

    def test_wide_macos_review_keeps_the_layout_and_erase_warning_in_one_view(self):
        self.contents(macos=True)
        self.report["disks"][0]["model"] = "Apple SSD SM256E"
        with patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "104", "LINES": "26"}), \
                patch("builtins.input", side_effect=["ERASE /dev/sdb", ""]) as keys:
            with self.assertRaisesRegex(ValueError, "left untouched"):
                installer.confirm_installation("/dev/sdb", flow=("Install", 2, 2))
        self.write.assert_not_called()
        view = installer.ui.plain(self.output.getvalue()).splitlines()
        self.assertTrue(any("Disk layout" in row and "This cannot be undone" in row for row in view))
        self.assertTrue(any("battery cycle count" in row for row in view))
        self.assertTrue(any("Memory" in row and "GiB" in row for row in view))
        self.assertTrue(all(len(row) <= 104 for row in view))
        self.assertLessEqual(len(view), 22, "leave screen rows for the typed challenges")
        self.assertEqual([installer.ui.plain(call.args[0]).strip(" \n›") for call in keys.call_args_list],
                         ["Type ERASE /dev/sdb to install:", "Type ERASE MACOS to erase macOS:"])

    def test_short_macos_review_keeps_the_title_and_warning_above_both_prompts(self):
        self.contents(macos=True)
        with patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "75", "LINES": "23"}), \
                patch("builtins.input", side_effect=["ERASE /dev/sdb", ""]):
            with self.assertRaisesRegex(ValueError, "left untouched"):
                installer.confirm_installation("/dev/sdb", flow=("Install", 2, 2))
        lines = installer.ui.plain(self.output.getvalue()).splitlines()
        self.assertIn("Ready to install", lines[0])
        self.assertTrue(any("replaces all data" in row for row in lines))
        self.assertTrue(any("Keep a macOS installer stick" in row for row in lines))
        self.assertLessEqual(len(lines), 19, "reserve four rows for the two typed challenges")
        self.write.assert_not_called()

    def test_being_target_points_to_an_update_and_needs_erase_being(self):
        self.contents(being=True, macos=False)
        with patch("builtins.input", side_effect=["ERASE /dev/sdb", "ERASE BEING"]) as confirm:
            installer.confirm_installation("/dev/sdb")
        self.assertIn("use the menu (Mission Control, or fn and F2), Update from a USB stick, instead.",
                      self.output.getvalue())
        self.assertEqual(confirm.call_args.args[0], "Type ERASE BEING to erase the being on it: ")
        self.assertTrue(self.write.call_args.kwargs["expected_contents"]["being"])

    def test_failure_during_disk_writing_says_how_to_start_again(self):
        self.contents()

        def write(plan, recheck, *, progress, meter, expected_contents):
            progress(installer.INSTALL_STAGES[0][0])
            progress(installer.INSTALL_STAGES[1][0])
            raise OSError("Short write to the target.")

        self.write.side_effect = write
        with patch("builtins.input", return_value="ERASE /dev/sdb"):
            with self.assertRaisesRegex(ValueError, r"stopped during disk writing to /dev/sdb.*Start again from this USB"):
                installer.confirm_installation("/dev/sdb")

    def test_failure_before_the_erase_is_reported_unchanged(self):
        self.contents()

        def write(plan, recheck, *, progress, meter, expected_contents):
            progress(installer.INSTALL_STAGES[0][0])
            raise ValueError("Insufficient free RAM to stage this installation. The target is untouched.")

        self.write.side_effect = write
        with patch("builtins.input", return_value="ERASE /dev/sdb"):
            with self.assertRaisesRegex(ValueError, "target is untouched"):
                installer.confirm_installation("/dev/sdb")

    def test_finish_screen_gives_the_wake_phrase_and_startup_disk_hint(self):
        self.contents()
        self.write.side_effect = lambda plan, recheck, **_: recheck()
        self.enterContext(patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68"}))
        with patch("builtins.input", return_value="ERASE /dev/sdb"):
            installer.confirm_installation("/dev/sdb", skills={"time_zone": "Australia/Sydney"})
        text = installer.ui.plain(self.output.getvalue())
        self.assertIn("Control-Enter", text)
        self.assertIn("wait for the eyes to brighten", text)
        self.assertIn("Australia/Sydney", text)
        self.assertEqual(self.write.call_args.kwargs["skills"], {"time_zone": "Australia/Sydney"})


    def test_a_restore_finishes_without_naming_the_try_wake_phrase(self):
        self.contents()
        self.write.side_effect = lambda plan, recheck, **_: recheck()
        self.enterContext(patch.dict(installer.os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68"}))
        with patch("builtins.input", return_value="ERASE /dev/sdb"):
            installer.confirm_installation("/dev/sdb", restore=lambda target: None)
        text = installer.ui.plain(self.output.getvalue())
        self.assertIn("Restore complete.", text)
        self.assertIn("Say its wake phrase", text)
        self.assertNotIn('Say "Hey', text)


class CableMedia(unittest.TestCase):
    def test_the_check_is_the_install_apply_guard_path(self):
        from mind import install_apply

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.object(install_apply, "CABLE_MEDIA", folder / "cable"):
            self.assertFalse(installer.cable_media())
            (folder / "cable").mkdir()
            self.assertTrue(installer.cable_media())


class DiskWording(unittest.TestCase):
    def test_reasons_and_names_read_plainly(self):
        disk = {"device": "/dev/sdb", "model": "SanDisk  Ultra\x07", "transport": "usb", "reasons": ["mounted", "boot_disk"]}
        self.assertEqual(installer.friendly_name(disk), "USB drive - SanDisk Ultra")
        self.assertEqual(installer.friendly_reason(disk), "This is the USB stick PipOS is running from.")
        self.assertIn("model partition", installer.friendly_reason({"reasons": []}, layout_ready=False))
        self.assertIn("odd", installer.friendly_reason({"reasons": ["odd"]}))


class DiscardHonesty(unittest.TestCase):
    def test_a_drive_without_discard_is_not_promised_erasure(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            for name, value in (("sda", "0\n"), ("sdb", "2147450880\n")):
                queue = Path(root) / name / "queue"
                queue.mkdir(parents=True)
                (queue / "discard_max_bytes").write_text(value)
            self.assertIs(installer.discard_supported("/dev/sda", sys_root=root), False)
            self.assertIs(installer.discard_supported("/dev/sdb", sys_root=root), True)
            self.assertIsNone(installer.discard_supported("/dev/sdc", sys_root=root))
        rows, kind = installer.contents_warning({"being": True}, discard=False)
        self.assertEqual(kind, "being")
        self.assertNotIn("for good", rows[1])
        self.assertIn("may stay readable", rows[1])
        rows, _ = installer.contents_warning({"being": True}, discard=True)
        self.assertIn("may still be recoverable", rows[1])
