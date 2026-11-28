import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from mind import update_checkpoint, update_slots


class UpdateAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.environment = {"GOOD": "A", update_slots.PREVIOUS: "B:old"}
        self.enterContext(patch.object(update_slots, "_boot_state")).return_value.__enter__.return_value = (
            "A", self.environment,
        )
        self.save = self.enterContext(patch.object(update_slots, "_save"))
        self.recipient = self.enterContext(patch.object(update_checkpoint, "enrolled_recipient", return_value="owner"))
        self.export = self.enterContext(patch.object(update_checkpoint.backup, "create_update_checkpoint"))
        self.boot_id = self.enterContext(patch.object(update_slots, "BOOT_ID"))
        self.boot_id.read_text.return_value = "this-boot\n"
        self.error = self.enterContext(redirect_stderr(io.StringIO()))
        self.enterContext(redirect_stdout(io.StringIO()))

    def test_unconfirmed_slot_is_rejected_before_reading_the_recipient(self):
        self.environment["GOOD"] = "B"
        self.assertEqual(update_checkpoint.main(), 1)
        self.assertIn("confirm the running slot", self.error.getvalue())
        self.recipient.assert_not_called()
        self.export.assert_not_called()
        self.save.assert_not_called()

    def test_rollback_scheduled_during_export_prevents_slot_replacement(self):
        def schedule_during_export(*args):
            self.environment[update_slots.ROLLBACK] = "this-boot:B"

        self.export.side_effect = schedule_during_export
        self.assertEqual(update_checkpoint.main(), 1)
        self.assertIn("restart to the previous system is already scheduled", self.error.getvalue())
        self.save.assert_not_called()

    def test_previous_slot_proof_is_invalidated_only_after_successful_export(self):
        self.environment[update_slots.LAST_UPDATE] = "A"
        def verify_proof_still_available(*args):
            self.save.assert_not_called()

        self.export.side_effect = verify_proof_still_available
        self.assertEqual(update_checkpoint.main(), 0)
        self.export.assert_called_once()
        self.save.assert_called_once_with({update_slots.PREVIOUS: "", update_slots.LAST_UPDATE: ""})

    def test_completed_install_records_only_an_activated_spare_slot(self):
        self.environment.update(ORDER="A B", B_OK="1", B_TRY="0")
        with self.assertRaisesRegex(ValueError, "spare slot ready"):
            update_slots.record_installed_update()
        self.save.assert_not_called()

        self.environment["ORDER"] = "B A"
        update_slots.record_installed_update()
        self.save.assert_called_once_with({update_slots.LAST_UPDATE: "B"})
