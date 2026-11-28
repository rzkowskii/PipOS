"""Consent boundaries at the transcript, local screen, and one-night policy."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from mind import care_routes, dream_policy
from mind.night_shift import JournalJob
from tests.test_mind import MindCase, said


ZONE = ZoneInfo("Australia/Sydney")
EVENING = datetime(2026, 11, 26, 20, tzinfo=ZONE)
MIDNIGHT = datetime(2026, 11, 27, 0, tzinfo=ZONE)


class OneNight(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_elapsed_night_cannot_be_reselected_after_it_is_retired(self):
        day = dream_policy.allow_once(self.root, EVENING)
        self.assertEqual(day, MIDNIGHT.date())
        self.assertEqual(dream_policy.pending_once(self.root, MIDNIGHT), day)
        self.assertIsNone(dream_policy.pending_once(self.root, MIDNIGHT.astimezone(ZoneInfo("UTC"))))
        morning = MIDNIGHT.replace(hour=dream_policy.NIGHT_END_HOUR)
        self.assertIsNone(dream_policy.pending_once(self.root, morning))
        dream_policy.expire_once(self.root, morning)
        self.assertIsNone(dream_policy.pending_once(self.root, MIDNIGHT))
        self.assertFalse((self.root / dream_policy.CONSENT_FILE).exists())

    def test_revoke_clears_the_standing_marker_and_the_one_night_marker(self):
        dream_policy.set_standing(self.root, True, "closed")
        dream_policy.allow_once(self.root, EVENING)
        dream_policy.set_standing(self.root, False)
        self.assertIsNone(dream_policy.consent_window(self.root))
        self.assertIsNone(dream_policy.pending_once(self.root, MIDNIGHT))


class ConsentAtCommit(unittest.IsolatedAsyncioTestCase):
    async def test_a_staged_journal_does_not_publish_after_consent_expires(self):
        allowed = Mock(side_effect=(True, False))
        with patch("mind.dream_store.DreamStore", return_value=Mock()) as store:
            job = JournalJob(Mock(), "Richard", MIDNIGHT.date().isoformat(), eligible=allowed)
            await job.run_chunk(1)
        store.return_value.stage.assert_called_once()
        store.return_value.publish.assert_not_called()


class SpokenConsent(MindCase):
    async def test_apparent_grant_only_opens_the_key_confirmation_page(self):
        with (patch.object(care_routes, "request_page", AsyncMock(return_value=True)) as request,
              patch("mind.turn.revoke_camera", AsyncMock()) as revoke):
            await self.hear("You can use your camera", 1)
            request.assert_awaited_once_with("camera-enable")
            revoke.assert_not_awaited()
        self.assertIn("Press E", said(self.bus))
        self.assertFalse(self.memory.contains("You can use your camera"))

    async def test_unnamed_followup_does_not_revoke_even_after_an_unrelated_question(self):
        self.mind.last_said = "What shall we talk about?"
        with patch("mind.turn.revoke_camera", AsyncMock(return_value=True)) as revoke:
            self.bus.push("state.changed", state="attending")
            self.bus.push("hear.final", text="Stop watching", speaker="Richard", followup=True)
            await self.bus.wait("mind.reply_done", 1)
            revoke.assert_not_awaited()
            await self.hear("Hey Pip, stop watching", 2)
            revoke.assert_awaited_once_with(self.root)
        self.assertIn("Camera permission is off", said(self.bus))

    async def test_only_the_scheduled_night_is_eligible(self):
        self.mind.skills.zone = ZONE
        self.mind.skills.now = lambda: EVENING
        dream_policy.allow_once(self.root, EVENING)
        with patch.object(self.mind, "_clock_doubt", return_value=None) as clock:
            self.assertFalse(self.mind._dreams_allowed())
            self.mind.skills.now = lambda: MIDNIGHT
            self.assertTrue(self.mind._dreams_allowed())
            clock.return_value = "reset"
            self.assertFalse(self.mind._dreams_allowed())
            clock.return_value = None
            self.mind.skills.now = lambda: MIDNIGHT.replace(hour=dream_policy.NIGHT_END_HOUR)
            self.assertFalse(self.mind._dreams_allowed())
