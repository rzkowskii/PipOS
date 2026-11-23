"""Speed in the mind (task L3.8): warm-up timing, full re-warm after
compaction, the identity snapshot, the turn boundary and a lean history."""

import asyncio
import json
import time
from unittest.mock import patch

from mind.turn import IDENTITY_RECORD, IDENTITY_SLOT, WARM_BATCH_TOKENS
from sim.fake_cortex import FakeCortex, default_reply
from tests.test_mind import MindCase


class WarmsAtOnce(MindCase):
    cfg = {"settle": 15.0}

    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        self.cortex_server = FakeCortex(delay=0.0, reply=default_reply).start()
        self.cortex_server.healthy = False  # the model is still loading
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        await self.start_mind()

    async def test_the_warm_up_starts_within_a_tenth_of_a_second_of_the_first_healthy_reply(self):
        # Resting, with housekeeping waiting out its 15 s settle, as on the laptop.
        self.bus.push("state.changed", state="idle")
        await asyncio.sleep(0.05)
        self.assertIsNotNone(self.mind._housekeeping)
        healthy_at, warm_at = [], []
        healthy, warm = self.mind.cortex.healthy, self.mind._warm

        async def watched_healthy():
            ok = await healthy()
            if ok and not healthy_at:
                healthy_at.append(time.monotonic())
            return ok

        async def watched_warm():
            warm_at.append(time.monotonic())
            await warm()

        self.mind.cortex.healthy = watched_healthy
        self.mind._warm = watched_warm
        await asyncio.sleep(0.3)
        self.cortex_server.healthy = True
        async with asyncio.timeout(3):
            while not warm_at:
                await asyncio.sleep(0.005)
        self.assertLess(warm_at[0] - healthy_at[0], 0.1)
        self.assertFalse(self.mind._housekeeping.done(), "housekeeping still settling did not hold it back")


class Speed(MindCase):
    def primes(self):
        return [p["prompt"] for p in self.cortex_server.prefills]

    async def test_after_compaction_the_whole_prompt_is_rewarmed_in_small_batches(self):
        await self.warmed()
        self.mind.cfg.keep_turns = 1
        for number in range(6):
            words = " ".join(f"word{number}x{i}" for i in range(12))
            self.mind.messages += [{"role": "user", "content": words},
                                   {"role": "assistant", "content": f"[neutral] reply {number} {words}"}]
        self.mind.state = "idle"
        before = len(self.cortex_server.prefills)
        await self.mind._compact()
        primes = self.primes()[before:]
        self.assertGreater(len(primes), 3)
        sizes = [len(p) for p in primes]
        self.assertTrue(all(b - a <= WARM_BATCH_TOKENS for a, b in zip([0] + sizes, sizes)), sizes)
        self.assertGreater(sizes[-1], 256, "not capped at the first 256 tokens")
        self.assertIn(self.mind.messages[-1]["content"], primes[-1], "the whole kept conversation is read")

    async def test_the_identity_snapshot_is_kept_and_restored_after_a_forget(self):
        await self.warmed()
        self.assertIn(("action=save", IDENTITY_SLOT), self.cortex_server.slot_calls)
        record = json.loads((self.root / "cortex" / IDENTITY_RECORD).read_text())
        self.assertEqual(record["tokens"], len(self.mind._warmed_prefix))
        await self.hear("Remember that the gate code is 4471", 1)
        self.cortex_server.slot_calls.clear()
        with patch.object(self.mind, "_identity_probe", return_value=record["tokens"]):
            await self.hear("forget that", 2)
            self.bus.push("state.changed", state="idle")
            async with asyncio.timeout(2):
                while ("action=restore", IDENTITY_SLOT) not in self.cortex_server.slot_calls:
                    await asyncio.sleep(0.01)
                await self.mind._warm_task
        self.assertTrue((self.root / "cortex" / IDENTITY_RECORD).exists(), "verified: kept")
        self.assertLess(self.cortex_server.slot_calls.index(("action=erase", "being.slot")),
                        self.cortex_server.slot_calls.index(("action=restore", IDENTITY_SLOT)))

    async def test_a_snapshot_that_does_not_check_out_is_discarded(self):
        await self.warmed()
        record = json.loads((self.root / "cortex" / IDENTITY_RECORD).read_text())
        await self.hear("Remember that the gate code is 4471", 1)
        with patch.object(self.mind, "_identity_probe", return_value=record["tokens"] - 20):
            await self.hear("forget that", 2)
            self.mind.state = "idle"
            self.mind._start_warmup()
            async with asyncio.timeout(2):
                await self.mind._warm_task
        calls = self.cortex_server.slot_calls
        restore = calls.index(("action=restore", IDENTITY_SLOT))
        self.assertEqual(calls[restore + 1], ("action=erase", "being.slot"), "the bad restore is erased")
        # The identity is then read afresh and kept again, from a clean read.
        self.assertEqual(calls[restore + 2:], [("action=save", IDENTITY_SLOT)])

    async def test_no_snapshot_holds_remembered_words_or_guest_prompts(self):
        await self.warmed()
        self.mind.summary = "Richard likes kites."
        self.assertIsNone(self.mind._identity_key())
        self.mind.summary = ""
        await self.hear("start guest mode", 1)
        self.assertIsNone(self.mind._identity_key())

    async def test_the_turn_boundary_is_primed_after_a_reply(self):
        await self.warmed()
        await self.hear("I flew a kite at the beach", 1)
        async with asyncio.timeout(2):
            while not any(p.endswith("Tell me more about that?<|end|>\n<|user|>\n") for p in self.primes()):
                await asyncio.sleep(0.01)
        self.assertTrue(self.mind._warmed_prefix, "the primed boundary is the new verified prefix")

    async def test_the_prompt_keeps_the_cortexs_exact_text(self):
        stream = self.mind.cortex.stream
        exact = "[curiosity]  Hm, you said: I flew a kite. Tell me more about that?"

        class Exact:
            """The real stream, with the text the cortex says it generated."""
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def __aiter__(self):
                return self.inner.__aiter__()

            raw = property(lambda self: exact)

        self.mind.cortex.stream = lambda *args, **kwargs: Exact(stream(*args, **kwargs))
        await self.hear("I flew a kite", 1)
        await asyncio.sleep(0.05)  # written into the history just after reply_done
        self.assertEqual(self.mind.messages[-1]["content"], exact, "byte for byte, not cleaned up")

    async def test_command_exchanges_stay_out_of_the_models_history(self):
        await self.hear("what time is it", 1)
        await self.hear("I flew a kite at the beach", 2)
        sent = json.dumps(self.cortex_server.chats[-1]["messages"])
        self.assertNotIn("7:05", sent)
        self.assertNotIn("what time is it", sent)

    async def test_a_story_gets_a_longer_budget_for_that_turn_only(self):
        await self.hear("tell me a story about a fox", 1)
        await self.hear("I like foxes", 2)
        self.assertEqual([c["max_tokens"] for c in self.cortex_server.chats], [160, 96])
