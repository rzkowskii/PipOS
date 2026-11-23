import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mind.initiative import InitiativePolicy, POLICY_PATH, QuietHours, load_policy, save_policy


class InitiativeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / POLICY_PATH
        self.disabled = InitiativePolicy(reminders=False)

    def test_missing_policy_keeps_reminders_enabled_without_creating_a_file(self):
        self.assertEqual(load_policy(self.root), InitiativePolicy(True, False, None))
        self.assertFalse(self.path.exists())
        self.assertFalse(load_policy(self.root).is_quiet(datetime(2026, 11, 21)))

    def test_policy_roundtrip_is_private_and_uses_the_versioned_shape(self):
        policy = InitiativePolicy(False, True, QuietHours("22:00", "07:00"))
        save_policy(self.root, policy)
        self.assertEqual(load_policy(self.root), policy)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {
            "version": 1,
            "reminders": False,
            "greetings": True,
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        })
        save_policy(self.root, InitiativePolicy())
        self.assertEqual(load_policy(self.root), InitiativePolicy())
        self.assertIsNone(json.loads(self.path.read_text())["quiet_hours"])

    def test_quiet_hours_use_local_time_with_an_exclusive_end(self):
        overnight = InitiativePolicy(quiet_hours=QuietHours("22:00", "07:00"))
        same_day = InitiativePolicy(quiet_hours=QuietHours("08:00", "09:30"))
        local = timezone(timedelta(hours=11))
        cases = [
            (overnight, 21, 59, 59, False),
            (overnight, 22, 0, 0, True),
            (overnight, 0, 0, 0, True),
            (overnight, 6, 59, 59, True),
            (overnight, 7, 0, 0, False),
            (same_day, 7, 59, 59, False),
            (same_day, 8, 0, 0, True),
            (same_day, 9, 29, 59, True),
            (same_day, 9, 30, 0, False),
        ]
        for policy, hour, minute, second, quiet in cases:
            with self.subTest(policy=policy, hour=hour, minute=minute, second=second):
                when = datetime(2026, 11, 21, hour, minute, second, tzinfo=local)
                self.assertEqual(policy.is_quiet(when), quiet)

    def test_invalid_policy_constructors_reject_coercion_and_invalid_times(self):
        for args in (("24:00", "07:00"), ("22:00", "7:00"), ("22:00", "07:60"),
                     ("07:00", "07:00"), ("２２:００", "07:00"), (None, "07:00")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                QuietHours(*args)
        for kwargs in ({"reminders": 1}, {"greetings": "true"}, {"quiet_hours": ("22:00", "07:00")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                InitiativePolicy(**kwargs)

    def test_invalid_existing_policy_fails_closed_and_can_be_replaced(self):
        valid = json.dumps({"version": 1, "reminders": True, "greetings": True, "quiet_hours": None})
        cases = [
            b"{truncated",
            b"\xff",
            b"null",
            valid.replace('"version": 1', '"version": 2').encode(),
            valid.replace('"version": 1', '"version": true').encode(),
            valid.replace('"reminders": true', '"reminders": 1').encode(),
            valid.replace('"greetings": true', '"greetings": false, "greetings": true').encode(),
            valid.replace('"quiet_hours": null', '"quiet_hours": {"start": "22:00"}').encode(),
            (valid + " " * 1024).encode(),
        ]
        for contents in cases:
            with self.subTest(contents=contents[:100]):
                self.path.write_bytes(contents)
                self.path.chmod(0o600)
                self.assertEqual(load_policy(self.root), self.disabled)
        save_policy(self.root, InitiativePolicy(greetings=True))
        self.assertEqual(load_policy(self.root), InitiativePolicy(greetings=True))

    def test_insecure_or_unreadable_file_suppresses_initiative(self):
        save_policy(self.root, InitiativePolicy(greetings=True))
        with patch("mind.initiative.os.open", side_effect=PermissionError):
            self.assertEqual(load_policy(self.root), self.disabled)
        self.path.chmod(0o644)
        self.assertEqual(load_policy(self.root), self.disabled)
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(load_policy(self.root), self.disabled)
        self.path.rmdir()
        self.path.symlink_to(self.root / "missing-policy.json")
        self.assertEqual(load_policy(self.root), self.disabled)

    def test_invalid_write_preserves_the_existing_policy(self):
        policy = InitiativePolicy(greetings=True)
        save_policy(self.root, policy)
        with self.assertRaises(TypeError):
            save_policy(self.root, {"greetings": False})
        self.assertEqual(load_policy(self.root), policy)
