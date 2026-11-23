"""Only addressed speech is kept: the follow-up window, plain yeses for what
cannot be undone, and a guest who cannot set the clock."""

import asyncio
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mind.reply import Confirm, Reply
from tests.test_mind import MindCase
from tests.test_turn_routing import SkillCase


class Listening(SkillCase):
    async def say_(self, text, *, followup=False, speaker="Richard"):
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker=speaker, followup=followup)
        done = await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        await asyncio.sleep(0)
        return done, " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])

    async def followup(self, text):
        return await self.say_(text, followup=True)


class CommandShapedRoomTalk(Listening):
    """Room talk in the follow-up window that parses as a command."""

    async def test_dont_forget_your_keys_is_not_stored(self):
        await self.say_("What time is it?")
        done, reply = await self.followup("don't forget your keys")
        self.assertFalse(done["spoke"])
        self.assertEqual(reply, "")
        self.assertFalse(self.memory.contains("keys"))
        self.assertEqual(self.memory.facts(), [])
        self.assertEqual(self.cortex_server.chats, [])

    async def test_remember_to_buy_bread_is_not_stored(self):
        await self.say_("What time is it?")
        done, _ = await self.followup("remember to buy bread")
        self.assertFalse(done["spoke"])
        self.assertFalse(self.memory.contains("bread"))

    async def test_the_time_said_in_the_room_does_not_move_the_clock(self):
        self.mind.skills.zone = ZoneInfo("Australia/Sydney")
        await self.say_("What time is it?")
        with patch.object(self.mind, "_set_system_clock", return_value=True) as setter, \
                patch.object(self.mind, "_clock_doubt", return_value=None):
            done, _ = await self.followup("the time is 6:15 pm")
        setter.assert_not_called()
        self.assertFalse(done["spoke"])

    async def test_go_to_sleep_said_to_someone_else_does_not_send_it_to_sleep(self):
        await self.say_("What time is it?")
        done, reply = await self.followup("go to sleep")
        self.assertFalse(done["spoke"])
        self.assertEqual(reply, "")
        await asyncio.sleep(0.05)
        self.assertEqual(self.bus.of("being.sleep"), [])

    async def test_with_its_name_the_same_words_are_kept(self):
        await self.say_("What time is it?")
        _, reply = await self.followup("Pip, remember that my keys are on the hook")
        self.assertIn("I'll remember that", reply)
        self.assertTrue(self.memory.contains("hook"))

    async def test_a_harmless_command_in_the_window_is_still_answered(self):
        await self.say_("Remember that my cat is called Tom")
        _, reply = await self.followup("what time is it")
        self.assertEqual(reply, "It's 7:05 in the evening.")


class AnOpenQuestionIsNotAnAddress(Listening):
    async def asyncSetUp(self):
        self.cleared = []

        async def clear():
            self.cleared.append(True)
            return Reply("[neutral] Cleared.")

        self.answers = {"clear my list": Reply("[neutral] Clear the whole list?", confirm=Confirm(clear))}
        await super().asyncSetUp()

    async def test_room_talk_while_a_skill_asks_is_overheard(self):
        await self.say_("clear my list")
        done, reply = await self.followup("I told the doctor about my knee pain today")
        self.assertFalse(done["spoke"])
        self.assertEqual(reply, "")
        self.assertEqual(self.cortex_server.chats, [], "never sent to the model")
        self.assertFalse(self.memory.contains("knee"))
        self.assertEqual(self.cleared, [])
        # The question is still open for the real answer.
        _, reply = await self.followup("yes")
        self.assertEqual(reply, "Cleared.")
        self.assertEqual(self.cleared, [True])

    async def test_an_unnamed_okay_does_not_confirm_but_a_named_one_does(self):
        await self.say_("clear my list")
        done, _ = await self.followup("Okay.")
        self.assertFalse(done["spoke"])
        self.assertEqual(self.cleared, [])
        await self.say_("Pip, okay")
        self.assertEqual(self.cleared, [True], "an ordinary yes, said to it, confirms a harmless question")

    async def test_a_plain_no_in_the_window_still_answers(self):
        await self.say_("clear my list")
        _, reply = await self.followup("no")
        self.assertEqual(reply, "Okay, I've left it.")
        self.assertEqual(self.cleared, [])


class AStrictQuestion(Listening):
    async def asyncSetUp(self):
        self.done = []

        async def wipe():
            self.done.append(True)
            return Reply("[neutral] Wiped.")

        self.answers = {"wipe my notes": Reply("[concern] Wipe them all? Say yes.",
                                               confirm=Confirm(wipe, strict=True))}
        await super().asyncSetUp()

    async def test_a_strict_question_takes_only_a_plain_yes(self):
        await self.say_("wipe my notes")
        await self.say_("Pip, okay")
        self.assertEqual(self.done, [])
        await self.say_("wipe my notes")
        await self.say_("Pip, yes")
        self.assertEqual(self.done, [True])


class ForgetEverythingNeedsAPlainYes(Listening):
    async def test_okay_never_erases_everything(self):
        await self.say_("Remember that my cat is called Tom")
        for answer, followup in (("Okay.", True), ("Pip, okay", False), ("Sure", False), ("alright", True)):
            with self.subTest(answer=answer):
                _, offer = await self.say_("Forget everything")
                self.assertIn("Say yes", offer)
                await self.say_(answer, followup=followup)
                self.assertEqual(len(self.memory.facts()), 1)
                self.mind._pending_confirm = None

    async def test_a_plain_yes_in_the_window_erases(self):
        await self.say_("Remember that my cat is called Tom")
        await self.say_("Forget everything")
        _, reply = await self.followup("yes")
        self.assertEqual(reply, "Okay. I've forgotten everything.")
        self.assertEqual(self.memory.facts(), [])

    async def test_an_unnamed_sure_does_not_confirm_a_topic_forget(self):
        await self.say_("Remember that my sister lives in Perth")
        self.assertIn("Say yes", (await self.say_("Forget about my sister"))[1])
        done, _ = await self.followup("Sure")
        self.assertFalse(done["spoke"])
        self.assertEqual(len(self.memory.facts()), 1)
        _, reply = await self.followup("yes")
        self.assertIn("forgotten", reply)
        self.assertEqual(self.memory.facts(), [])


class RingWindow(MindCase):
    async def ring(self):
        self.bus.push("state.changed", state="idle")
        self.bus.push("stem.beat")
        token = (await self.bus.wait("mind.announce", 1))["announcement"]
        self.bus.push("state.changed", state="speaking")
        self.bus.push("stem.announce_ready", announcement=token)
        await self.bus.wait("mind.reply_done", len(self.bus.of("mind.reply_done")) + 1)
        self.bus.push("say.done", final=True, announcement=token)
        self.bus.push("state.changed", state="attending")
        await asyncio.sleep(0)

    async def test_room_talk_while_a_timer_rings_is_overheard(self):
        self.mind.skills.reminders.add("timer", time.time() - 1, kind="timer", duration=600)
        await self.ring()
        self.assertTrue(self.mind._ring_open())
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("hear.final", text="I bought new running shoes on sale", speaker="Richard", followup=True)
        done = await self.bus.wait("mind.reply_done", replies + 1)
        self.assertFalse(done["spoke"])
        self.assertEqual(self.cortex_server.chats, [])
        self.assertFalse(self.memory.contains("shoes"))


class AGuestCannotSetTheClock(MindCase):
    async def test_set_time_is_refused_in_guest_mode(self):
        self.mind.skills.zone = ZoneInfo("Australia/Sydney")
        await self.hear("Start guest mode", 1)
        with patch.object(self.mind, "_set_system_clock", return_value=True) as setter, \
                patch.object(self.mind._primary_memory, "owner_set_clock") as owner_clock, \
                patch.object(self.mind, "_clock_doubt", return_value=None):
            start = len(self.bus.of("say.request"))
            await self.hear("Pip, set the time to 3 pm", 2)
        setter.assert_not_called()
        owner_clock.assert_not_called()
        reply = " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])
        self.assertEqual(reply, "Only the person I live with can set my clock.")
