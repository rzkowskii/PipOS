"""The order a final transcript is routed in, and code skills' yes-or-no questions
(docs/PIP-FUNCTIONS.md 5.2, task L3.3)."""

import asyncio
import json
import types
from unittest.mock import patch

import mind.turn
from mind.app_commands import AppCommand
from mind.reply import Confirm, Reply
from tests.test_mind import MindCase, said


def fake_everyday(answers):
    """An everyday registry that answers the phrases in `answers` (text -> Reply)."""
    seen = []

    async def handle(text, ctx):
        seen.append((text, ctx))
        return answers.get(text)

    module = types.SimpleNamespace(handle=handle, may_match=lambda text: text in answers, preload=lambda: ())
    return module, seen


class SkillCase(MindCase):
    answers = {}

    async def asyncSetUp(self):
        self.everyday, self.seen = fake_everyday(dict(self.answers))
        self.enterContext(patch.dict(mind.turn._OPTIONAL, {"everyday": self.everyday}))
        await super().asyncSetUp()

    async def ask(self, text, speaker="Richard"):
        """Say something and return what was said back."""
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker=speaker)
        await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        await asyncio.sleep(0)
        return " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])


class RoutingOrder(SkillCase):
    answers = {
        "start guest mode": Reply("[neutral] everyday took guest mode"),
        "what time is it": Reply("[neutral] everyday took the time"),
        "what's 17 times 23": Reply("[neutral] Three hundred and ninety-one."),
        "hello": Reply("[neutral] everyday took the hello"),
    }

    async def test_privacy_comes_before_every_skill(self):
        reply = await self.ask("start guest mode")
        self.assertIn("Guest mode is on", reply)
        self.assertEqual(self.seen, [], "privacy commands never reach the skills")

    async def test_the_workshop_comes_before_the_memory_commands(self):
        with patch.object(self.mind.apps, "parse", return_value=AppCommand("say", say="The workshop answered.")):
            reply = await self.ask("what time is it")
        self.assertEqual(reply, "The workshop answered.")
        self.assertEqual(self.seen, [])

    async def test_commands_come_before_everyday_and_everyday_before_greetings_and_the_model(self):
        self.assertEqual(await self.ask("what time is it"), "It's 7:05 in the evening.")
        self.assertEqual(self.seen, [], "a command is answered before any everyday skill is asked")
        self.assertEqual(await self.ask("what's 17 times 23"), "Three hundred and ninety-one.")
        self.assertEqual(await self.ask("hello"), "everyday took the hello")
        self.assertEqual(self.cortex_server.chats, [])
        await self.ask("I planted beans today")
        self.assertEqual(len(self.cortex_server.chats), 1, "what no skill takes goes to the model")

    async def test_a_skill_gets_the_moment_it_is_answering(self):
        self.mind.last_said = "It's 7:05 in the evening."
        await self.ask("what's 17 times 23")
        _, ctx = self.seen[-1]
        self.assertEqual((ctx.speaker, ctx.guest, ctx.owner, ctx.name), ("Richard", False, "Richard", "Pip"))
        self.assertEqual(ctx.last_said, "It's 7:05 in the evening.")
        self.assertEqual(ctx.body.get("battery"), 82)
        self.assertEqual(ctx.state_dir, self.root)
        self.assertIsInstance(ctx.clock_trusted, bool)

    async def test_skill_lines_are_not_kept_unless_asked(self):
        await self.ask("what's 17 times 23")
        self.assertFalse(self.memory.contains("17 times 23"))
        self.assertFalse(self.memory.contains("ninety-one"))
        self.assertEqual(self.mind.messages, [], "nor are they in the model's history")

    async def test_the_speculative_read_waits_for_any_code_answer(self):
        self.assertTrue(self.mind._may_be_code("what's 17 times 23"))
        self.assertTrue(self.mind._may_be_code("open the terminal"))
        self.assertTrue(self.mind._may_be_code("set a timer for"))
        self.assertFalse(self.mind._may_be_code("I went down to the river and"))

    async def test_permission_report_is_local_and_private_to_the_owner(self):
        self.assertTrue(self.mind._may_be_code("what are you allowed to do"))
        owner = await self.ask("What are you allowed to do?")
        self.assertIn("My camera is off.", owner)
        self.assertIn("Permissions in Care", owner)
        self.assertFalse(self.memory.contains("Permissions in Care"))
        self.assertEqual(await self.ask("What permissions do you have?", speaker="Visitor"),
                         "My owner can ask about my permissions.")
        self.assertEqual(self.cortex_server.chats, [])


class KeptReplies(SkillCase):
    answers = {"make a note the plumber comes thursday": Reply("[joy] Noted.", keep=True, logged="note.add")}

    async def test_keep_records_the_exchange_and_logged_writes_a_count(self):
        await self.ask("make a note the plumber comes thursday")
        self.assertTrue(self.memory.contains("plumber comes thursday"))
        log = (self.root / "log" / "actions.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["kind"] for line in log], ["note.add"])
        self.assertNotIn("plumber", "".join(log), "the action log holds kinds and counts only")


class Confirmations(SkillCase):
    async def asyncSetUp(self):
        self.done = []

        async def on_yes():
            self.done.append("yes")
            return Reply("[neutral] Cleared.", logged="list.clear")

        self.answers = {
            "clear my shopping list": Reply("[neutral] Clear your shopping list? Say yes.", confirm=Confirm(on_yes)),
        }
        await super().asyncSetUp()

    async def test_yes_runs_it_once(self):
        await self.ask("clear my shopping list")
        self.assertEqual(await self.ask("yes please"), "Cleared.")
        self.assertEqual(self.done, ["yes"])
        await self.ask("yes")
        self.assertEqual(self.done, ["yes"], "a second yes has nothing to answer")

    async def test_no_says_its_own_line(self):
        await self.ask("clear my shopping list")
        self.assertEqual(await self.ask("no"), "Okay, I've left it.")
        self.assertEqual(self.done, [])

    async def test_anything_else_lets_the_question_lapse_and_is_answered_as_usual(self):
        await self.ask("clear my shopping list")
        self.assertEqual(await self.ask("what time is it"), "It's 7:05 in the evening.")
        await self.ask("yes")
        self.assertEqual(self.done, [])

    async def test_only_the_person_asked_can_answer(self):
        await self.ask("clear my shopping list")
        await self.ask("yes", speaker="Visitor")
        self.assertEqual(self.done, [])

    async def test_it_lapses_after_fifteen_seconds(self):
        await self.ask("clear my shopping list")
        later = mind.turn._uptime() + 16
        with patch("mind.turn._uptime", return_value=later):
            await self.ask("yes")
        self.assertEqual(self.done, [])

    async def test_a_guest_change_or_a_closed_lid_cancels_it(self):
        await self.ask("clear my shopping list")
        await self.ask("start guest mode")
        await self.ask("end guest mode")
        await self.ask("yes")
        self.assertEqual(self.done, [])
        await self.ask("clear my shopping list")
        self.bus.push("body.lid", closed=True)
        self.bus.push("body.lid", closed=False, seq=5)
        await asyncio.sleep(0.05)
        await self.ask("yes")
        self.assertEqual(self.done, [])


class SwitchingOff(SkillCase):
    async def asyncSetUp(self):
        async def off():
            return Reply("[neutral] Saving myself. Goodbye for now.", action="poweroff", logged="shutdown")

        self.answers = {"shut down": Reply("[neutral] Shall I switch off? Say yes.", confirm=Confirm(off))}
        await super().asyncSetUp()
        self.calls = []

        def power(action):
            self.calls.append((action, (self.root / "context.json").exists(),
                               [e["t"] for e in self.bus.sent].count("mind.shutdown_ready")))
            return True

        self.mind._power_command = power

    async def test_it_switches_off_only_after_the_goodbye_and_a_full_save(self):
        await self.hear("Remember that the spare key is in the blue pot", 1)
        self.assertEqual(await self.ask("shut down"), "Shall I switch off? Say yes.")
        self.assertEqual(self.calls, [])
        await self.ask("yes")
        for _ in range(200):
            if self.calls:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.calls, [("poweroff", True, 1)])
        log = [json.loads(line)["kind"] for line in (self.root / "log" / "actions.jsonl").read_text().splitlines()]
        self.assertEqual(log, ["shutdown"])
