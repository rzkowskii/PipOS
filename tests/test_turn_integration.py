"""The integration pass's wiring in the turn: the diary day record, guests'
clock answers, the body as handlers see it, the doctor's hooks, the words-only
history search, and a clock set by voice that keeps timers and the date honest."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import mind.turn
from mind.reply import Reply
from tests.test_mind import MindCase, said
from tests.test_turn_routing import SkillCase


def fake_diary(line):
    seen = []

    async def handle(text, ctx):
        seen.append((text, ctx.guest))
        return Reply(f"[neutral] {line}") if line else None

    return SimpleNamespace(handle=handle, may_match=lambda text: True), seen


class DiaryDays(SkillCase):
    async def test_a_day_question_is_answered_from_the_day_record(self):
        diary, seen = fake_diary("Yesterday we talked three times, mostly about beans.")
        with patch.dict(mind.turn._OPTIONAL, {"diary": diary}):
            reply = await self.ask("What did we do yesterday?")
        self.assertEqual(reply, "Yesterday we talked three times, mostly about beans.")
        self.assertEqual(seen, [("What did we do yesterday?", False)])
        self.assertEqual(self.cortex_server.chats, [], "a code answer, not the model")

    async def test_without_a_day_record_it_falls_back_to_the_owners_own_words(self):
        diary, _ = fake_diary(None)
        with patch.dict(mind.turn._OPTIONAL, {"diary": diary}):
            reply = await self.ask("What did we do yesterday?")
        self.assertNotIn("I don't have a record of that day yet", reply)
        self.assertEqual(self.cortex_server.chats, [])


class GuestsMayAskTheClock(SkillCase):
    async def test_a_guest_hears_the_time_elsewhere_and_the_help(self):
        await self.ask("start guest mode")
        for question in ("What time is it in Tokyo?", "What can you do?"):
            reply = await self.ask(question, speaker="guest")
            self.assertNotIn("in a private conversation", reply, question)


class TheBodyAsHandlersSeeIt(MindCase):
    async def test_brightness_mute_lid_and_cortex_use_reach_the_context(self):
        self.bus.push("body.brightness", ok=False, level=100, at_max=True, reason="no_control")
        self.bus.push("state.changed", state="idle", muted=True)
        await asyncio.sleep(0.05)
        body = self.mind._context("Richard", None).body
        self.assertIs(body["muted"], True)
        self.assertEqual(body["brightness"], 100)
        self.assertIs(body["brightness_at_max"], True)
        self.assertEqual(body["brightness_reason"], "no_control")
        self.assertIn("lid_closed", body)
        self.assertIn("cortex_busy", body)
        self.bus.push("state.changed", state="idle", muted=False)
        await asyncio.sleep(0.05)
        self.assertIs(self.mind._context("Richard", None).body["muted"], False)

    async def test_a_model_reply_tells_the_doctor_its_speed(self):
        seen = []
        doctor = SimpleNamespace(note_turn=lambda **kw: seen.append(kw))
        with patch.dict(mind.turn._OPTIONAL, {"doctor": doctor}):
            stream = SimpleNamespace(timings={"prompt_n": 40, "prompt_per_second": 22.5,
                                              "predicted_per_second": 6.1})
            self.mind._log_timing(stream, 1.2, 0, None)
            self.mind._log_timing(stream, None, 1, RuntimeError("failed"))
        self.assertEqual(seen, [{"prompt_tps": 22.5, "decode_tps": 6.1, "prompt_n": 40}],
                         "only a turn that worked is judged")

    async def test_the_quiet_check_runs_only_when_due_and_resting(self):
        started = []

        async def self_care(ctx):
            started.append(ctx.body.get("lid_closed"))
            return []

        doctor = SimpleNamespace(self_care=self_care)
        with patch.dict(mind.turn._OPTIONAL, {"doctor": doctor}), \
                patch.object(mind.turn.Path, "is_dir", return_value=True):
            self.mind.state = "idle"
            self.mind._maybe_self_care()
            self.assertIsNone(self.mind._self_care_task, "not due yet")
            self.mind._self_care_at = time.monotonic() - mind.turn.SELF_CARE_INTERVAL - 1
            self.mind.state = "listening"
            self.mind._maybe_self_care()
            self.assertIsNone(self.mind._self_care_task, "never in the middle of a conversation")
            self.mind.state = "idle"
            self.mind._maybe_self_care()
            await self.mind._self_care_task
        self.assertEqual(started, [False])


class HistorySearch(MindCase):
    async def test_an_ordinary_sentence_searches_by_words_only(self):
        calls = []
        real = self.mind.memory.recall_history

        def recall_history(*args, **kwargs):
            calls.append(kwargs.get("meaning"))
            return real(*args, **kwargs)

        with patch.object(self.mind.memory, "recall_history", side_effect=recall_history):
            self.mind._history_for("beans in the garden", "beans in the garden", None, None, False, set())
            self.mind._history_for("beans in the garden", "beans in the garden", None, None, True, set())
        self.assertEqual(calls, [False, True])


class ClockSetByVoice(MindCase):
    async def test_running_timers_move_with_the_clock(self):
        self.mind.skills.zone = ZoneInfo("Australia/Sydney")
        moved = []
        with patch.object(self.mind, "_set_system_clock", return_value=True), \
                patch.object(self.mind, "_clock_doubt", return_value=None), \
                patch.object(self.mind.skills.reminders, "clock_moved", side_effect=moved.append):
            await self.hear("Set the time to 3:45 pm", 1)
        self.assertEqual(len(moved), 1)
        self.assertLess(abs(moved[0]), 12 * 3600 + 60)

    async def test_a_date_that_was_doubted_is_still_owned_up_to(self):
        # The laptop's first boot: the clock read a plausible date that was
        # 37 days out. Setting the time of day writes the RTC and clears the
        # memory's doubt, but the date is no surer than before.
        self.mind.skills.zone = ZoneInfo("Australia/Sydney")
        clock_set = []

        def setter(timestamp):
            clock_set.append(timestamp)
            return True

        with patch.object(self.mind, "_set_system_clock", side_effect=setter), \
                patch.object(self.mind, "_clock_doubt", side_effect=lambda: None if clock_set else "behind"):
            await self.hear("Set the time to 3:45 pm", 1)
        self.assertEqual(len(clock_set), 1)
        self.assertIn("My date may still be wrong", said(self.bus))
