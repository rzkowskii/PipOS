"""An interrupted reply keeps only what Voice played (mind/played.py, say.played)."""

import asyncio
import json
import unittest
from unittest.mock import patch

from mind import played as played_module
from mind.played import PlayedReply
from tests.test_mind import MindCase
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)

WELL = "[joy] I'm well, thank you. A little curious about today. How are you?"
FIRST, SECOND, THIRD = "I'm well, thank you.", "A little curious about today.", "How are you?"


class Receipts(unittest.IsolatedAsyncioTestCase):
    async def test_only_this_replys_handed_clauses_count_until_it_closes(self):
        reply = PlayedReply()
        self.assertRegex(reply.token, r"^[0-9a-f]{16}$")
        reply.hand(1, "One.")
        reply.hand(2, "Two.")
        self.assertFalse(reply.observe({"reply": "0" * 16, "seq": 1}), "another reply's receipt")
        self.assertTrue(reply.observe({"reply": reply.token, "seq": 3}))
        self.assertTrue(reply.observe({"reply": reply.token, "seq": True}))
        self.assertEqual(reply.started, set(), "only clauses this mind handed over, by number")
        reply.observe({"reply": reply.token, "seq": 2})
        self.assertEqual(reply.kept(), ["Two."])
        reply.observe({"reply": reply.token, "final": True, "stopped": True})
        self.assertTrue(reply.settled.is_set())
        self.assertTrue(reply.stopped)
        reply.close()
        self.assertFalse(reply.observe({"reply": reply.token, "seq": 1}), "a late receipt is ignored")
        self.assertEqual(reply.kept(), ["Two."])

    async def test_waits_for_the_final_receipt_only_so_long(self):
        reply = PlayedReply()
        await asyncio.wait_for(reply.settle(timeout=5), 0.1)  # nothing handed: nothing to wait for
        reply.hand(1, "One.")
        await reply.settle(timeout=0.01)
        self.assertIsNone(reply.stopped, "a voice that crashed never ends it")


class PlayedCase(MindCase):
    delay = 0.03

    def token(self):
        return self.bus.of("say.request")[0]["reply"]

    def voice(self, *seqs, final=None, token=None):
        """What the voice reports: clauses whose first audio played, then its end."""
        token = token or self.token()
        for seq in seqs:
            self.bus.push("say.played", reply=token, seq=seq)
        if final is not None:
            self.bus.push("say.played", reply=token, final=True, stopped=final == "stopped")

    def said_rows(self):
        return [(text, json.loads(meta) if meta else {}) for text, meta in self.memory.db.execute(
            "SELECT text, meta FROM episodes WHERE kind = 'said' ORDER BY id")]

    async def until(self, check, timeout=2.0):
        async with asyncio.timeout(timeout):
            while not check():
                await asyncio.sleep(0.01)

    async def turn_over(self):
        await self.until(lambda: self.mind._turn is not None and self.mind._turn.done())


class InterruptedWhileThinking(PlayedCase):
    """Esc while the model is still writing the reply."""

    async def story(self, handed):
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="tell me a long story", speaker="Richard")
        await self.bus.wait("say.request", handed)

    async def test_nothing_played_keeps_nothing_and_no_unanswered_question(self):
        await self.story(2)
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.turn_over()
        self.assertNotIn("assistant", [m["role"] for m in self.mind.messages])
        self.assertNotIn("user", [m["role"] for m in self.mind.messages][-1:])
        self.assertEqual(self.said_rows(), [])

    async def test_one_played_clause_is_kept_cut_off(self):
        await self.story(3)
        self.voice(1)
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.turn_over()
        first = self.bus.of("say.request")[0]["text"]
        self.assertEqual(self.mind.messages[-1]["content"], f"[neutral] {first} —")
        self.assertEqual(self.said_rows(), [(first, self.said_rows()[0][1])])
        self.assertTrue(self.said_rows()[0][1]["interrupted"])
        self.assertEqual(self.mind.last_said, first)

    async def test_several_played_clauses_are_kept_and_the_unplayed_rest_is_not(self):
        await self.story(4)
        self.voice(1, 2, 3)
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.turn_over()
        handed = [r["text"] for r in self.bus.of("say.request")]
        said = " ".join(handed[:3])
        self.assertGreater(len(handed), 3, "a clause was handed over that never played")
        self.assertEqual(self.mind.messages[-1]["content"], f"[neutral] {said} —")
        self.assertEqual([text for text, _ in self.said_rows()], [said])

    async def test_a_late_receipt_after_the_turn_closed_changes_nothing(self):
        await self.story(3)
        self.voice(1)
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.turn_over()
        before = (list(self.mind.messages), self.said_rows())
        self.voice(2, 3, final="done")
        await asyncio.sleep(0.1)
        self.assertEqual((self.mind.messages, self.said_rows()), before)

    async def test_a_crashed_voice_leaves_the_receipts_already_received(self):
        await self.story(3)
        self.voice(1)
        with patch.object(played_module, "SETTLE_SECONDS", 0.05):
            self.bus.push("mind.cancel", reason="voice_lost")
            await self.turn_over()
        first = self.bus.of("say.request")[0]["text"]
        self.assertEqual([text for text, _ in self.said_rows()], [first])

    async def test_a_lid_closed_while_waiting_discards_the_reply_at_once(self):
        await self.story(3)
        self.voice(1)
        self.bus.push("mind.cancel")
        await asyncio.sleep(0.05)
        self.bus.push("body.lid", closed=True)
        await self.until(lambda: self.mind._turn.done(), timeout=0.5)
        self.assertEqual(self.said_rows(), [], "a closed lid discards a reply, as it always has")

    async def test_the_reply_sources_are_kept_with_the_played_part(self):
        await self.hear("I planted a fern today", 1)
        heard = self.mind.messages[-2]["episode_id"]
        self.bus.sent.clear()
        await self.story(3)
        self.voice(1)
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.turn_over()
        meta = self.said_rows()[-1][1]
        self.assertTrue(meta["interrupted"])
        self.assertIn(heard, meta["episodes"], "the earlier exchange the model read")
        self.assertIn(heard, self.mind.messages[-1]["episodes"])

    async def test_a_parroted_first_try_hands_nothing_over(self):
        tries = []

        def reply(messages):
            tries.append(1)
            if len(tries) == 1:
                return "[neutral] Tell me a long story. Tell me a long story."
            return "[neutral] Once there was a kite. It flew over the sea. It came home at night."

        self.cortex_server.reply = reply
        await self.story(2)
        self.assertEqual(self.bus.of("say.request")[0]["text"], "Once there was a kite.")
        self.voice(1)
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.turn_over()
        self.assertEqual(len(tries), 2)
        self.assertEqual(self.mind.messages[-1]["content"], "[neutral] Once there was a kite. —")


class InterruptedWhileSpeaking(PlayedCase):
    """Esc after the whole reply was written down, while Voice still speaks it.
    The model is quicker than the voice, so this is the usual case."""

    async def written(self):
        self.cortex_server.reply = lambda _: WELL
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="Tell me about kites", speaker="Richard")
        await self.bus.wait("mind.reply_done")
        await self.turn_over()
        self.assertEqual(self.mind.messages[-1]["content"], WELL)
        self.assertEqual(self.said_rows()[-1][0], f"{FIRST} {SECOND} {THIRD}")
        self.assertEqual(self.bus.of("say.request")[-1]["reply"], self.token(), "the end carries it too")

    async def test_only_the_played_clauses_stay_in_the_history_and_the_archive(self):
        await self.written()
        self.voice(1)
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.until(lambda: self.mind._played is None)
        self.assertEqual(self.mind.messages[-1]["content"], f"[joy] {FIRST} —")
        [(text, meta)] = self.said_rows()
        self.assertEqual(text, FIRST)
        self.assertTrue(meta["interrupted"])
        self.assertEqual(self.mind.last_said, FIRST)
        self.assertFalse(self.memory.contains("curious"), "never heard, so not findable")

    async def test_voice_stopping_short_on_its_own_is_enough(self):
        # A failed reply in the voice ends it stopped, with no cancel at all.
        await self.written()
        self.voice(1, 2, final="stopped")
        await self.until(lambda: self.mind._played is None)
        self.assertEqual(self.mind.messages[-1]["content"], f"[joy] {FIRST} {SECOND} —")

    async def test_a_clause_the_voice_could_not_play_is_dropped_too(self):
        await self.written()
        self.voice(1, 3, final="done")
        await self.until(lambda: self.mind._played is None)
        self.assertEqual(self.mind.messages[-1]["content"], f"[joy] {FIRST} {THIRD} —")
        self.assertEqual([text for text, _ in self.said_rows()], [f"{FIRST} {THIRD}"])

    async def test_nothing_played_removes_the_reply_and_its_question(self):
        await self.hear("I planted a fern today", 1)
        before = self.mind.last_said
        self.bus.sent.clear()
        await self.written()
        self.bus.push("mind.cancel")
        self.voice(final="stopped")
        await self.until(lambda: self.mind._played is None)
        self.assertEqual([m["role"] for m in self.mind.messages], ["user", "assistant"], "the fern stays")
        self.assertEqual(len(self.said_rows()), 1)
        self.assertNotIn("curious", self.said_rows()[0][0])
        self.assertEqual(self.mind.last_said, before)

    async def test_a_reply_played_to_the_end_stays_exactly_as_written(self):
        await self.written()
        self.voice(1, 2, 3, final="done")
        await self.until(lambda: self.mind._played is None)
        self.bus.push("mind.cancel")
        await asyncio.sleep(0.1)
        self.assertEqual(self.mind.messages[-1]["content"], WELL, "the model's own text keeps the cache")
        self.assertEqual([text for text, _ in self.said_rows()], [f"{FIRST} {SECOND} {THIRD}"])
        self.assertNotIn("interrupted", self.said_rows()[0][1])

    async def test_a_crashed_voice_leaves_the_receipts_already_received(self):
        await self.written()
        self.voice(1, 2)
        with patch.object(played_module, "SETTLE_SECONDS", 0.05):
            self.bus.push("mind.cancel", reason="voice_lost")
            await self.until(lambda: self.mind._played is None)
        self.assertEqual(self.mind.messages[-1]["content"], f"[joy] {FIRST} {SECOND} —")

    async def test_closing_the_lid_settles_it_at_once(self):
        await self.written()
        self.voice(1)
        self.bus.push("body.lid", closed=True)
        await self.until(lambda: self.mind._played is None, timeout=0.5)
        self.assertEqual(self.mind.messages[-1]["content"], f"[joy] {FIRST} —")
        self.assertEqual([text for text, _ in self.said_rows()], [FIRST])
        self.assertIsNone(self.mind.last_said, "a closed lid forgets this wake's words all the same")

    async def test_the_next_turn_reads_only_what_was_played(self):
        await self.written()
        self.voice(1)
        self.bus.push("mind.cancel")
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="Tell me about the sea", speaker="Richard")
        await asyncio.sleep(0.05)
        self.voice(final="stopped")  # the voice's end arrives while the next turn starts
        await self.bus.wait("mind.reply_done", 2)
        self.assertEqual(len(self.cortex_server.requests), 2)
        prompt = self.cortex_server.requests[-1]["messages"]
        assistant = [m["content"] for m in prompt if m["role"] == "assistant"]
        self.assertIn(f"[joy] {FIRST} —", assistant)
        self.assertFalse(any("curious" in content for content in assistant), assistant)

    async def test_a_receipt_for_an_older_reply_changes_nothing(self):
        await self.written()
        self.voice(1, final="stopped", token="0123456789abcdef")
        await asyncio.sleep(0.1)
        self.assertIsNotNone(self.mind._played)
        self.assertEqual(self.mind.messages[-1]["content"], WELL)


class VoiceWithoutReceipts(PlayedCase):
    """An older voice, or a stand-in, never reports playback: replies stay as written."""

    async def test_an_interrupted_reply_keeps_what_was_handed_over(self):
        with patch.object(played_module, "SETTLE_SECONDS", 0.05):
            self.bus.push("state.changed", state="attending")
            self.bus.push("hear.final", text="tell me a long story", speaker="Richard")
            await self.bus.wait("say.request", 2)
            self.bus.push("mind.cancel")
            await self.turn_over()
        handed = " ".join(r["text"] for r in self.bus.of("say.request"))
        self.assertEqual(self.mind.messages[-1]["content"], f"[neutral] {handed} —")

    async def test_a_written_reply_stays_as_written(self):
        self.cortex_server.reply = lambda _: WELL
        await self.hear("I planted a fern today", 1)
        with patch.object(played_module, "SETTLE_SECONDS", 0.05):
            self.bus.push("mind.cancel")
            await asyncio.sleep(0.2)
        self.assertEqual(self.mind.messages[-1]["content"], WELL)
        self.assertEqual([text for text, _ in self.said_rows()], [f"{FIRST} {SECOND} {THIRD}"])


if __name__ == "__main__":
    unittest.main()
