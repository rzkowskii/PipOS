"""What it last heard and said: RAM only, for "what did you hear?" (task L3.7),
and the action log's writes from the mind (task L3.12)."""

import asyncio
import json

from tests.test_turn_routing import SkillCase


class ReadBack(SkillCase):
    answers = {}

    def last_ctx(self):
        return self.seen[-1][1]

    async def test_the_previous_words_and_reply_reach_the_skills(self):
        await self.ask("what time is it")
        await self.ask("what did you hear")
        ctx = self.last_ctx()
        self.assertEqual(ctx.last_heard, "what time is it", "the words before this question")
        self.assertEqual(ctx.last_said, "It's 7:05 in the evening.")
        self.assertNotIn("what time is it", (self.root / "context.json").read_text())

    async def test_nothing_heard_yet_means_none(self):
        await self.ask("what did you hear")
        self.assertIsNone(self.last_ctx().last_heard)

    async def test_sleep_lid_and_guest_mode_clear_it(self):
        for change in ("sleep", "lid", "guest"):
            with self.subTest(change=change):
                await self.ask("what time is it")
                if change == "sleep":
                    self.bus.push("state.changed", state="asleep")
                elif change == "lid":
                    self.bus.push("body.lid", closed=True)
                    self.bus.push("body.lid", closed=False, seq=9)
                else:
                    await self.ask("start guest mode")
                    self.seen.clear()
                await asyncio.sleep(0.05)
                self.assertIsNone(self.mind.last_heard)
                if change == "guest":
                    # Only its own word that guest mode is on survives the change.
                    self.assertTrue(self.mind.last_said.startswith("Guest mode is on."))
                else:
                    self.assertIsNone(self.mind.last_said)
                await self.ask("what did you hear")
                self.assertIsNone(self.last_ctx().last_heard)
                if change == "guest":
                    await self.ask("end guest mode")


class ActionLog(SkillCase):
    def kinds(self):
        path = self.root / "log" / "actions.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        for line in lines:
            self.assertEqual(set(json.loads(line)), {"ts", "kind", "count"})
        return [(json.loads(line)["kind"], json.loads(line)["count"]) for line in lines]

    async def test_forgets_and_guest_mode_are_counted_without_words(self):
        await self.hear("Remember that my sister's birthday is in June", 1)
        await self.hear("Remember that my sister lives in Perth", 2)
        await self.ask("Forget about my sister")
        await self.ask("yes")
        await self.ask("start guest mode")
        await self.ask("end guest mode")
        self.assertEqual(self.kinds(), [("forget", 2), ("guest.start", 1), ("guest.end", 1)])
        self.assertNotIn("sister", (self.root / "log" / "actions.jsonl").read_text())

    async def test_forget_that_counts_one(self):
        await self.hear("Remember that the gate code is 4471", 1)
        await self.ask("forget that")
        self.assertEqual(self.kinds(), [("forget", 1)])
