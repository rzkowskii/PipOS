"""Written chat with written replies (chat.mode reply=text): nothing is spoken, the mind reports what it shows."""

import asyncio
import time

from tests.test_mind import MindCase
from tests.test_turn_rings import Rings
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


class TextModeCase(MindCase):
    def text_mode(self, on=True, **fields):
        self.bus.push("chat.mode", mode="typed_text" if on else "spoken", input="typed" if on else "voice",
                      reply="text" if on else "voice", **fields)

    async def settle(self):
        await asyncio.sleep(0.05)

    def shown(self, since=0):
        return [e for e in self.bus.of("say.played")[since:] if "text" in e]


class Replies(TextModeCase):
    async def test_a_command_answer_is_shown_not_spoken(self):
        self.text_mode()
        await self.settle()
        await self.hear("what time is it", 1)
        self.assertEqual(self.bus.of("say.request"), [])
        shown = self.shown()
        self.assertTrue(shown)
        self.assertRegex(" ".join(e["text"] for e in shown), r"\d")
        self.assertEqual([e["seq"] for e in shown], list(range(1, len(shown) + 1)))
        final = [e for e in self.bus.of("say.played") if e.get("final")]
        self.assertEqual(len(final), 1)
        self.assertEqual({e["reply"] for e in self.bus.of("say.played")}, {final[0]["reply"]})
        self.assertEqual(final[0]["stopped"], False)
        self.assertEqual(self.bus.of("mind.reply_done")[-1]["spoke"], False)

    async def test_a_model_reply_is_shown_with_receipts_and_stays_in_the_history(self):
        self.text_mode()
        await self.settle()
        await self.hear("tell me something about ferns", 1)
        self.assertEqual(self.bus.of("say.request"), [])
        shown = self.shown()
        self.assertGreaterEqual(len(shown), 1)
        self.assertFalse(any(e["text"].startswith("[") for e in shown), "no tag in the shown words")
        self.assertEqual(self.bus.of("mind.reply_done")[-1]["spoke"], False)
        # The reply was shown to the end: the history keeps it as the model wrote it.
        assistant = [m for m in self.mind.messages if m["role"] == "assistant"]
        self.assertTrue(assistant)
        # Nothing was left open waiting for Voice.
        await self.until(lambda: not self.mind._plays or all(p.settled.is_set() for p in self.mind._plays.values()))

    async def test_it_works_with_no_voice_organ_at_all(self):
        self.text_mode()
        await self.settle()
        await self.hear("what time is it", 1)
        self.assertFalse(self.mind._voice_reports is None)
        # No say.done, no receipt from Voice ever: the next turn still starts at once.
        await asyncio.wait_for(self.hear("what day is it", 2), 3)

    async def until(self, check, timeout=2.0):
        async with asyncio.timeout(timeout):
            while not check():
                await asyncio.sleep(0.01)

    async def test_spoken_again_sends_say_request(self):
        self.text_mode()
        await self.settle()
        self.text_mode(False)
        await self.settle()
        await self.hear("what time is it", 1)
        self.assertTrue(self.bus.of("say.request"))
        self.assertEqual(self.shown(), [])

    async def test_a_guest_is_always_spoken_to(self):
        self.text_mode()
        await self.settle()
        self.mind._guest_memory = object()
        self.assertFalse(self.mind._text_reply())
        self.mind._guest_memory = None
        self.assertTrue(self.mind._text_reply())

    async def test_with_the_lid_closed_nothing_is_sent(self):
        self.text_mode()
        await self.settle()
        self.bus.push("body.lid", closed=True, seq=9)
        await self.settle()
        before = len(self.bus.of("say.played"))
        await self.mind._send_reply("say.request", text="Hello.", seq=1, last=False)
        self.assertEqual(len(self.bus.of("say.played")), before)


class ReviewFixes(TextModeCase):
    async def test_private_and_camera_fields_travel_with_the_receipt_and_a_final_comes_too(self):
        self.text_mode()
        await self.settle()
        await self.mind._send_reply("say.request", text="A secret.", seq=1, last=True, private=True, camera_seq=4)
        receipts = self.bus.of("say.played")
        self.assertEqual([("text" in r, r.get("final", False)) for r in receipts], [(True, False), (False, True)])
        self.assertTrue(all(r.get("private") is True and r.get("camera_seq") == 4 for r in receipts))
        self.assertEqual(self.bus.of("say.request"), [])

    async def test_a_mode_change_in_the_middle_of_a_reply_does_not_split_it(self):
        self.text_mode()
        await self.settle()
        self.mind._begin_reply()
        self.text_mode(False)
        await self.settle()
        await self.mind._send_reply("say.request", text="One.", seq=1, last=False, reply="a" * 16)
        await self.mind._send_reply("say.request", text="", seq=2, last=True, reply="a" * 16)
        self.assertEqual(self.bus.of("say.request"), [], "the whole reply stays on the screen")
        self.assertEqual(len(self.bus.of("say.played")), 2)
        await self.mind._send_reply("mind.reply_done", spoke=True)
        self.assertEqual(self.bus.of("mind.reply_done")[-1]["spoke"], False)
        self.assertIsNone(self.mind._reply_text, "the next reply decides afresh")

    async def test_a_cancelled_text_reply_ends_stopped_at_once(self):
        self.text_mode()
        await self.settle()
        played = __import__("mind.played", fromlist=["PlayedReply"]).PlayedReply()
        played.hand(1, "One.")
        self.mind._register_played(played)
        self.mind._begin_reply()
        self.mind._cancel()
        await self.settle()
        self.assertTrue(played.settled.is_set())
        self.assertTrue(played.stopped)
        finals = [r for r in self.bus.of("say.played") if r.get("final")]
        self.assertEqual([(r["reply"], r["stopped"]) for r in finals], [(played.token, True)])

    async def test_a_new_connection_is_spoken_until_a_mode_is_heard_on_it(self):
        self.text_mode()
        await self.settle()
        self.assertTrue(self.mind._text_reply())
        self.bus.generation += 1
        self.assertFalse(self.mind._text_reply())
        self.text_mode()
        await self.settle()
        self.assertTrue(self.mind._text_reply())

    async def test_an_unknown_mode_is_not_called_a_guest_refusal(self):
        await VoicePhrases.phrase(self, "Pip, let's go quiet now", "loud")
        said = " ".join(e["text"] for e in self.bus.of("say.request"))
        self.assertNotIn("owner", said)


class Confirmations(TextModeCase):
    def question(self, answered):
        from mind.reply import Confirm, Reply

        async def yes():
            answered.append("yes")
            return Reply("[neutral] Done.")

        return Reply("[neutral] Shall I? Say yes.", confirm=Confirm(yes, plain_yes=True, after_speech=True),
                     private=True)

    async def test_a_question_is_armed_at_once_and_a_typed_yes_answers_it(self):
        self.text_mode()
        await self.settle()
        answered = []
        # No say.done and no confirm_ack from any voice: the question is open already.
        await asyncio.wait_for(self.mind._speak_reply(self.question(answered), "ask", "Richard"), 1)
        self.assertIsNotNone(self.mind._pending_confirm)
        self.assertAlmostEqual(self.mind._pending_confirm["until"] - _uptime(), 15, delta=2)
        self.assertEqual(self.bus.of("say.request"), [])
        self.assertTrue(self.shown())
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="yes", speaker="Richard")
        await self.bus.wait("mind.reply_done", 2)
        self.assertEqual(answered, ["yes"])

    async def test_spoken_the_question_still_waits_for_the_voice(self):
        from unittest.mock import patch

        await self.settle()
        answered = []
        with patch("mind.turn.HABIT_QUESTION_SPEECH_SECONDS", 0.05):
            await self.mind._speak_reply(self.question(answered), "ask", "Richard")
        self.assertIsNone(self.mind._pending_confirm, "no end of speech was reported")
        self.assertTrue(self.bus.of("say.request"))


def _uptime():
    from mind.turn import _uptime
    return _uptime()


class Announcements(TextModeCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reminders = self.mind.skills.reminders

    async def ring_text(self, count=1):
        self.bus.push("state.changed", state="idle")
        self.bus.push("stem.beat")
        token = (await self.bus.wait("mind.announce", count))["announcement"]
        self.bus.push("state.changed", state="speaking")
        self.bus.push("stem.announce_ready", announcement=token)
        await self.bus.wait("mind.reply_done", len(self.bus.of("mind.reply_done")) + 1)
        return token

    async def test_a_timer_is_text_plus_one_chime_and_is_complete_without_the_voice(self):
        self.text_mode()
        await self.settle()
        entry = self.reminders.add("timer", time.time() - 1, kind="timer", duration=600)
        await self.ring_text()
        words = " ".join(e["text"] for e in self.shown())
        self.assertIn("ten-minute timer is done", words)
        requests = self.bus.of("say.request")
        self.assertEqual(len(requests), 1, "one chime, nothing spoken")
        self.assertEqual((requests[0]["text"], requests[0].get("chirp")), ("", "bell"))
        self.assertEqual(self.reminders.delivery(entry.id).rings, 1, "counted as rung without any say.done")
        self.assertEqual(self.bus.of("mind.reply_done")[-1]["spoke"], False)

    async def test_an_alarm_still_rings_aloud(self):
        self.text_mode()
        await self.settle()
        self.reminders.add("wake up", time.time() - 1, kind="alarm")
        await self.ring_text()
        requests = self.bus.of("say.request")
        self.assertEqual(requests[0].get("chirp"), "bell")
        self.assertTrue(any(e["text"] for e in requests), "its words are spoken")
        self.assertEqual(self.shown(), [])
        self.assertTrue(self.bus.of("mind.reply_done")[-1]["spoke"])
        # And the next ordinary reply is text again.
        self.assertTrue(self.mind._text_reply())

    async def test_spoken_mode_is_unchanged(self):
        self.reminders.add("timer", time.time() - 1, kind="timer", duration=600)
        await Rings.ring(self, 1)
        self.assertEqual(self.shown(), [])


class VoicePhrases(TextModeCase):
    """"Let's type" and the like: the mind only asks the bridge (chat.mode.request); the mode is the bridge's."""

    async def phrase(self, text, arg, speaker="Richard", guest=False):
        from unittest.mock import patch
        from mind import commands

        command = commands.Command("chat_mode", arg)
        real = commands.parse
        with patch.object(commands, "parse", side_effect=lambda t, *a, **k: command if t == text else real(t, *a, **k)):
            if guest:
                self.mind._guest_memory = object()
            try:
                self.bus.push("state.changed", state="attending")
                self.bus.push("hear.final", text=text, speaker=speaker)
                await self.bus.wait("mind.reply_done", 1)
            finally:
                self.mind._guest_memory = None

    async def test_the_owner_asks_for_each_mode(self):
        for arg, line in (("typed_voice", "type"), ("typed_text", "type"), ("spoken", "Okay.")):
            self.bus.sent.clear()
            await self.phrase("Pip, let's go quiet now", arg)
            self.assertEqual([e["mode"] for e in self.bus.of("chat.mode.request")], [arg])
            self.assertIn(line, " ".join(e["text"] for e in self.bus.of("say.request")))

    async def test_a_guest_or_another_speaker_is_refused(self):
        await self.phrase("Pip, let's go quiet now", "typed_text", guest=True)
        self.assertEqual(self.bus.of("chat.mode.request"), [])
        self.bus.sent.clear()
        await self.phrase("Pip, let's go quiet now", "typed_text", speaker="Robin")
        self.assertEqual(self.bus.of("chat.mode.request"), [])

    async def test_an_unknown_mode_is_refused(self):
        await self.phrase("Pip, let's go quiet now", "loud")
        self.assertEqual(self.bus.of("chat.mode.request"), [])

    def test_it_is_a_command_that_needs_the_name(self):
        from mind.turn import ADDRESS_ONLY_COMMANDS
        self.assertIn("chat_mode", ADDRESS_ONLY_COMMANDS)
