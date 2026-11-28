"""A mind restarted mid-turn (cable log, 25 Nov).

Restarting being-mind while Pip was thinking left the brainstem thinking for
the dead mind's turn for its whole 300 s budget: no name was taken and 34
tries went unheard. A new brainstem ends that turn on the new mind's hello.
For one that does not, the new mind gives the inherited turn up itself, as an
empty reply does (the mind may not send mind.cancel; only the brainstem may).
"""

import asyncio
import unittest
from unittest.mock import patch

from tests.test_mind import MindCase

GRACE = 0.05


class InheritedTurn(MindCase):
    async def settle(self):
        await asyncio.sleep(GRACE * 4)

    async def test_a_thinking_snapshot_at_start_is_given_up(self):
        with patch("mind.turn.INHERITED_TURN_GRACE", GRACE):
            self.bus.push("state.changed", state="thinking", muted=False, unwell=False)
            done = await self.bus.wait("mind.reply_done", timeout=2.0)
        self.assertIs(done["spoke"], False)
        self.assertEqual(self.cortex_server.chats, [], "no model turn for it")
        self.assertEqual(self.bus.of("say.request"), [])

    async def test_nothing_is_sent_when_the_brainstem_ends_it_first(self):
        with patch("mind.turn.INHERITED_TURN_GRACE", GRACE):
            self.bus.push("state.changed", state="thinking", muted=False, unwell=False)
            self.bus.push("mind.cancel", reason="mind_restarted")
            self.bus.push("state.changed", state="attending", prev="thinking", muted=False, unwell=False)
            await self.settle()
        self.assertEqual(self.bus.of("mind.reply_done"), [])

    async def test_only_the_first_state_this_process_hears(self):
        with patch("mind.turn.INHERITED_TURN_GRACE", GRACE):
            self.bus.push("state.changed", state="idle", muted=False, unwell=False)
            # A later snapshot (the brainstem restarted and resumed) is not inherited.
            self.bus.push("state.changed", state="thinking", muted=False, unwell=False)
            await self.settle()
        self.assertEqual(self.bus.of("mind.reply_done"), [])

    async def test_a_live_change_into_thinking_is_not_inherited(self):
        with patch("mind.turn.INHERITED_TURN_GRACE", GRACE):
            self.bus.push("state.changed", state="thinking", prev="listening", muted=False, unwell=False)
            await self.settle()
        self.assertEqual(self.bus.of("mind.reply_done"), [])

    async def test_speaking_at_start_is_left_to_the_voice(self):
        # The voice may hold the dead mind's whole reply and finish it.
        with patch("mind.turn.INHERITED_TURN_GRACE", GRACE):
            self.bus.push("state.changed", state="speaking", muted=False, unwell=False)
            await self.settle()
        self.assertEqual(self.bus.of("mind.reply_done"), [])

    async def test_a_turn_of_its_own_in_the_meantime_keeps_it(self):
        release = asyncio.Event()
        with patch("mind.turn.INHERITED_TURN_GRACE", GRACE):
            self.bus.push("state.changed", state="thinking", muted=False, unwell=False)
            await asyncio.sleep(0.01)
            self.mind._turn = turn = asyncio.create_task(release.wait())
            await self.settle()
        self.assertEqual(self.bus.of("mind.reply_done"), [])
        release.set()
        await turn

    async def test_not_behind_a_closed_lid(self):
        with patch("mind.turn.INHERITED_TURN_GRACE", GRACE):
            self.bus.push("body.lid", closed=True)
            self.bus.push("state.changed", state="thinking", muted=False, unwell=False)
            await self.settle()
        self.assertEqual(self.bus.of("mind.reply_done"), [])


class StoppedMidGrace(MindCase):
    async def test_stopping_the_mind_cancels_the_wait(self):
        with patch("mind.turn.INHERITED_TURN_GRACE", 30.0):
            self.bus.push("state.changed", state="thinking", muted=False, unwell=False)
            for _ in range(100):
                if self.mind._inherited_turn_task is not None:
                    break
                await asyncio.sleep(0.01)
            task = self.mind._inherited_turn_task
            self.assertIsNotNone(task)
            await self.stop_mind()
            self.assertTrue(task.cancelled())
            self.task = asyncio.create_task(asyncio.sleep(0))  # for tearDown

    async def asyncTearDown(self):
        await self.task
        self.cortex_server.stop()
        self.dir.cleanup()


if __name__ == "__main__":
    unittest.main()
