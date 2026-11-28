"""Safety and privacy edges of the turn: follow-ups that only look like a
skill, switching off without a yes, the identity snapshot's own checks, the
night's re-warm, and the workshop's news in every memory mode."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from mind.reply import Reply
from mind.turn import IDENTITY_RECORD, IDENTITY_SLOT, WARM_BATCH_TOKENS, AppLine, Announcement, RewarmJob
from tests.test_mind import MindCase
from tests.test_turn_routing import SkillCase


class FollowUpsThatOnlyLookLikeASkill(SkillCase):
    answers = {"add milk to my shopping list": Reply("[joy] Added milk.")}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # A skill's gate is broad on purpose ("list" anywhere): it is only a
        # hint that a skill might answer.
        self.everyday.may_match = lambda text: "list" in text

    async def followup(self, text):
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard", followup=True)
        return await self.bus.wait("mind.reply_done", replies + 1)

    async def test_room_talk_that_trips_a_gate_is_overheard_not_answered_or_kept(self):
        done = await self.followup("I left the shopping list on the fridge for your mum")
        self.assertFalse(done["spoke"])
        self.assertEqual(self.cortex_server.chats, [], "never sent to the model")
        self.assertFalse(self.memory.contains("fridge"))
        self.assertIsNone(self.mind.last_heard, "not even kept in RAM as heard")

    async def test_a_skill_that_really_answers_a_follow_up_still_does(self):
        await self.followup("add milk to my shopping list")
        said = [e["text"] for e in self.bus.of("say.request") if e["text"]]
        self.assertEqual(said, ["Added milk."])
        self.assertEqual(self.cortex_server.chats, [])


class SwitchingOffNeedsAYes(SkillCase):
    async def asyncSetUp(self):
        self.answers = {
            "off now": Reply("[neutral] Goodbye.", action="poweroff", logged="shutdown"),
        }
        await super().asyncSetUp()
        self.calls = []
        self.mind._power_command = lambda action: self.calls.append(action) or True

    async def test_a_skill_cannot_switch_it_off_without_a_spoken_yes(self):
        self.assertEqual(await self.ask("off now"), "Goodbye.")
        await asyncio.sleep(0.05)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "log" / "actions.jsonl").exists(), "nothing done, nothing logged")


class IdentityProbe(MindCase):
    async def test_the_probe_reads_only_the_identity_in_the_conversations_own_slot(self):
        await self.warmed()
        record = json.loads((self.root / "cortex" / IDENTITY_RECORD).read_text())
        self.mind.messages = [{"role": "user", "content": "x " * 400}, {"role": "assistant", "content": "[joy] ok"}]
        before = len(self.cortex_server.prefills)
        await self.mind._identity_probe(record["tokens"])
        probe = self.cortex_server.prefills[before:]
        self.assertEqual(len(probe), 1)
        self.assertEqual(probe[0]["id_slot"], self.mind.cortex.slot_id, "pinned to slot 0, never the workshop's")
        self.assertLessEqual(len(probe[0]["prompt"]), record["tokens"], "the identity only, not the conversation")
        self.assertNotIn("x x x", probe[0]["prompt"])

    async def test_a_new_model_file_or_build_never_gets_an_old_snapshot(self):
        await self.warmed()
        key = self.mind._identity_key()
        with patch.object(self.mind, "_model_fingerprint", return_value="reflex|99|123|456|build-2"):
            self.assertNotEqual(self.mind._identity_key(), key)

    async def test_a_server_that_cannot_say_what_it_cached_stops_the_churn(self):
        await self.warmed()
        await self.hear("Remember that the gate code is 4471", 1)
        with patch.object(self.mind, "_identity_probe", return_value=None):
            await self.hear("forget that", 2)
            self.mind.state = "idle"
            self.mind._start_warmup()
            async with asyncio.timeout(2):
                await self.mind._warm_task
        self.assertFalse((self.root / "cortex" / IDENTITY_RECORD).exists())
        calls = self.cortex_server.slot_calls
        restore = calls.index(("action=restore", IDENTITY_SLOT))
        self.assertNotIn(("action=save", IDENTITY_SLOT), calls[restore:], "not saved again to be thrown away")
        self.assertIsNone(self.mind._identity_key())


class NightRewarm(MindCase):
    async def test_the_rewarm_moves_forward_every_chunk_and_never_restarts_from_the_top(self):
        await self.warmed()
        self.mind.messages = [{"role": "user", "content": "y " * 200}, {"role": "assistant", "content": "[joy] fine"}]
        sent = []
        real = self.mind.cortex._prime_cache

        async def slow_prime(prompt):
            sent.append(len(prompt))
            await asyncio.sleep(0.03)  # each batch outlasts the chunk's budget
            return await real(prompt)

        self.mind.cortex._prime_cache = slow_prime
        job = RewarmJob(self.mind)
        chunks = 0
        while not await job.run_chunk(0.01):
            chunks += 1
            self.assertLess(chunks, 200, "the re-warm never finishes")
        self.assertEqual(sent, sorted(sent), "each chunk carries on from the last batch")
        self.assertEqual(len(sent), len(set(sent)), "no batch is read twice")
        self.assertTrue(all(b - a <= WARM_BATCH_TOKENS for a, b in zip([0] + sent, sent)))
        self.assertFalse(self.mind._needs_warmup)
        self.assertEqual(len(self.mind._warmed_prefix), sent[-1])

    async def test_a_turn_between_chunks_starts_the_rewarm_afresh(self):
        await self.warmed()
        self.mind._rewarm = ["old", (1, 2, 3), 2]
        await self.hear("what time is it", 1)
        self.assertIsNone(self.mind._rewarm)


class WorkshopNews(MindCase):
    async def test_a_line_said_is_not_said_again_in_session_only_mode(self):
        self.mind.policy = SimpleNamespace(mode="session-only", expiry_days=None)
        self.mind._app_lines = ["Your countdown script finished."]
        self.mind._announcement = Announcement(AppLine("Your countdown script finished."), "tok", 0.0, speaking=True)
        self.mind._complete_announcement("tok")
        self.assertIsNone(self.mind._announcement)
        self.assertEqual(self.mind._app_lines, [])

    async def test_a_guest_never_hears_the_owners_workshop_news(self):
        self.mind._app_lines = ["Your countdown script finished."]
        self.mind._announcement = Announcement(AppLine("Your countdown script finished."), "tok", 0.0)
        self.mind._guest_memory = self.mind.memory
        try:
            start = len(self.bus.of("say.request"))
            await self.mind._speak_announcement("tok")
            self.assertEqual(self.bus.of("say.request")[start:], [])
            self.assertEqual(self.mind._app_lines, [])
        finally:
            self.mind._guest_memory = None


class WhatCannotBeUndoneNeedsAPlainYes(MindCase):
    """Switching off, restarting and clearing a list or the notes: "okay" and
    "sure" are not a yes for these, only a plain yes is."""

    ask = SkillCase.ask

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.calls = []
        self.mind._power_command = lambda action: self.calls.append(action) or True

    async def test_okay_does_not_switch_it_off_or_restart_it(self):
        for request, answer in (("Shut down", "okay"), ("Restart yourself", "sure")):
            self.assertIn("Say yes", await self.ask(request))
            await self.ask(answer)
            await asyncio.sleep(0.05)
            self.assertEqual(self.calls, [], answer)
            self.mind._pending_confirm = None

    async def test_a_plain_yes_still_switches_it_off(self):
        self.assertIn("Shall I switch off", await self.ask("Shut down"))
        self.assertIn("Goodbye", await self.ask("yes"))
        async with asyncio.timeout(5):
            await self.mind._turn
        self.assertEqual(self.calls, ["poweroff"])

    async def test_previous_system_requires_the_owner_and_an_exact_yes(self):
        from mind import device, update_slots

        cmdline = self.root / "cmdline"
        cmdline.write_text("being.installed=1 rauc.slot=B\n")
        plan = update_slots.Rollback("B", "A")
        request = "Go back to how you were"
        with (patch.object(device, "CMDLINE", cmdline),
              patch.object(update_slots, "rollback_plan", return_value=plan) as read,
              patch.object(update_slots, "schedule_rollback") as schedule):
            self.assertIn("Only my owner", await self.ask(request, speaker="guest"))
            read.assert_not_called()
            for answer in ("yes please", "do it", "no"):
                self.assertIn("previous system once", await self.ask(request))
                await self.ask(answer)
                schedule.assert_not_called()
                self.assertEqual(self.calls, [])

            self.assertIn("previous system once", await self.ask(request))
            await self.ask("yes", speaker="guest")
            schedule.assert_not_called()

            self.assertIn("previous system once", await self.ask(request))
            schedule.side_effect = ValueError("The available systems changed. Review them again.")
            self.assertIn("systems changed", await self.ask("yes"))
            self.assertEqual(self.calls, [])
            schedule.reset_mock(side_effect=True)

            self.assertIn("previous system once", await self.ask(request))
            self.assertIn("scheduled one start", await self.ask("yes"))
            async with asyncio.timeout(5):
                await self.mind._turn
            schedule.assert_called_once_with(plan)
            self.assertEqual(self.calls, ["reboot"])

    async def test_okay_does_not_clear_a_list_or_the_notes(self):
        await self.ask("Add milk to my shopping list")
        await self.ask("Make a note: the plumber comes Thursday")
        self.assertIn("Say yes or no", await self.ask("Clear my shopping list"))
        await self.ask("okay")
        self.mind._pending_confirm = None
        self.assertIn("milk", await self.ask("What's on my shopping list?"))
        self.assertIn("Say yes or no", await self.ask("Delete all my notes"))
        await self.ask("sure")
        self.mind._pending_confirm = None
        self.assertIn("plumber", await self.ask("Read my notes"))
        await self.ask("Clear my shopping list")
        self.assertIn("empty", await self.ask("yes"))


class AFailedSwitchOff(SkillCase):
    async def asyncSetUp(self):
        from mind.reply import Confirm

        async def off():
            return Reply("[neutral] Saving myself. Goodbye for now.", action="poweroff", logged="shutdown")

        self.answers = {"shut down": Reply("[neutral] Shall I switch off? Say yes.", confirm=Confirm(off))}
        await super().asyncSetUp()
        self.mind._power_command = lambda action: False  # not on the appliance

    async def test_it_keeps_answering_when_systemd_will_not_switch_it_off(self):
        await self.ask("shut down")
        await self.ask("yes")
        async with asyncio.timeout(5):
            await self.mind._turn  # the goodbye, the save and the refused systemctl
        self.assertTrue(self.mind.lid_open, "it did not act as if its lid were shut")
        self.assertEqual(await self.ask("what time is it"), "It's 7:05 in the evening.")


class CorrectingAName(MindCase):
    async def test_its_called_after_a_recall_stores_the_name_once(self):
        # PIP-FUNCTIONS 2.2: remember, recall, then "That's wrong, it's called Moonleaf".
        await self.hear("Remember that my orchid is called Silverleaf", 1)
        await self.hear("What do you remember about my orchid?", 2)
        await self.hear("That's wrong, it's called Moonleaf", 3)
        texts = [fact.text for fact in self.memory.facts()]
        self.assertEqual(texts, ["my orchid is called Moonleaf"])
        self.assertEqual(self.cortex_server.chats, [])
