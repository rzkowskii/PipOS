"""Calls made in the mind's turn loop: brightness waits for the brainstem's
answer (L6.4), the prompt keeps the cortex's exact text (L7.2, L3.8), and the
embedding pack is verified before recall uses it."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mind.turn
from tests.test_mind import MindCase, said


class Brightness(MindCase):
    def answer_with(self, **fields):
        """The brainstem: answer every being.brightness with `fields`."""
        real = self.bus.send

        async def send(topic, **sent):
            await real(topic, **sent)
            if topic == "being.brightness":
                self.bus.push("body.brightness", **fields)
        self.bus.send = send

    async def test_at_the_top_it_says_so(self):
        self.answer_with(ok=True, level=100, at_max=True, at_min=False, limited=True)
        await self.hear("brighter", 1)
        self.assertEqual(said(self.bus), "That's as bright as I go.")
        self.assertEqual(self.bus.of("being.brightness"), [{"t": "being.brightness", "step": 1}])
        self.assertEqual(self.cortex_server.chats, [], "a code answer, not the model")

    async def test_at_the_bottom_it_says_so(self):
        self.answer_with(ok=True, level=5, at_max=False, at_min=True, limited=True)
        await self.hear("dimmer", 1)
        self.assertEqual(said(self.bus), "That's as dim as I go.")

    async def test_a_failed_write_is_not_reported_as_done(self):
        self.answer_with(ok=False, reason="write_failed", level=40)
        await self.hear("brighter", 1)
        self.assertEqual(said(self.bus), "My screen didn't change just now.")

    async def test_without_an_answer_it_waits_only_briefly(self):
        started = asyncio.get_running_loop().time()
        await self.hear("brighter", 1)
        waited = asyncio.get_running_loop().time() - started
        self.assertEqual(said(self.bus), "Brighter.")
        self.assertLess(waited, mind.turn.BRIGHTNESS_ANSWER_S + 1.0)
        self.assertIsNone(self.mind._brightness_answer)

    async def test_nothing_is_sent_or_awaited_with_the_lid_shut(self):
        self.mind.lid_open = False
        self.assertIsNone(await self.mind._send_for_skill("being.brightness", step=1))
        self.assertEqual(self.bus.of("being.brightness"), [])


class ExactHistory(MindCase):
    """The assistant turn kept in the prompt is the cortex's stream.raw."""

    replies = []

    @staticmethod
    def reply(messages):
        return ExactHistory.replies.pop(0) if ExactHistory.replies else "[joy] Okay. "

    async def test_after_a_retry_the_said_reply_is_kept_byte_for_byte(self):
        ExactHistory.replies[:] = ["I flew a kite at the beach today", "[joy]  Lovely, a kite at the beach! "]
        await self.hear("I flew a kite at the beach today", 1)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.cortex_server.chats), 2, "the parrot was asked again")
        self.assertEqual(self.mind.messages[-1]["content"], "[joy]  Lovely, a kite at the beach! ")
        self.assertEqual(said(self.bus), "Lovely, a kite at the beach!")

    async def test_a_reply_without_a_tag_is_still_kept_as_generated(self):
        ExactHistory.replies[:] = ["Lovely, a kite. "]
        await self.hear("I flew a kite", 1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.mind.messages[-1]["content"], "Lovely, a kite. ")


class EmbeddingPack(MindCase):
    def fakes(self, ok):
        checks = []

        def trusted(role, files):
            checks.append((role, [Path(path).name for path in files]))
            return (True, None) if ok else (False, "model SHA-256 differs")
        embed = SimpleNamespace(model_dir=lambda: Path("/models/embeddings/blink-8M"))
        packs = SimpleNamespace(trusted=trusted, on_models=lambda files: True)
        return checks, patch.dict(mind.turn._OPTIONAL, {"embed": embed, "model_pack": packs})

    async def test_recall_waits_for_the_pack_and_uses_it_once_verified(self):
        embedder = object()
        memory = self.mind._primary_memory
        checks, fakes = self.fakes(True)
        with fakes:
            memory.embedder = embedder
            self.mind._hold_embedder()
            self.assertIsNone(memory.embedder, "nothing unverified is used, even while it checks")
            await self.mind._embed_check
        self.assertIs(memory.embedder, embedder)
        self.assertEqual(checks, [("embeddings", ["model.safetensors", "tokenizer.json"])])
        memory.embedder = None

    async def test_an_unverified_pack_means_words_only_recall_with_a_log_line(self):
        memory = self.mind._primary_memory
        checks, fakes = self.fakes(False)
        lines = []
        with fakes, patch.object(mind.turn, "log", lambda *parts: lines.append(" ".join(map(str, parts)))), \
                patch.object(mind.turn, "EMBED_RECHECK_S", 0):
            memory.embedder = object()
            self.mind._hold_embedder()
            await self.mind._embed_check
            guest = self.mind._guest_embedder(SimpleNamespace(embedder=object()))
        self.assertIsNone(memory.embedder)
        self.assertIsNone(guest.embedder, "a guest's archive does not use it either")
        self.assertTrue(any("recall uses words only" in line and "SHA-256" in line for line in lines), lines)
        self.assertEqual(len(checks), mind.turn.EMBED_CHECKS, "tried again later, a bounded number of times")
        self.assertEqual(len(lines), 2, "said when it first fails and when it gives up, not every time")

    async def test_a_check_that_fails_once_is_tried_again_later(self):
        memory = self.mind._primary_memory
        embedder = object()
        answers = [(False, "openssl timed out"), (True, None)]
        embed = SimpleNamespace(model_dir=lambda: Path("/models/embeddings/blink-8M"))
        packs = SimpleNamespace(trusted=lambda role, files: answers.pop(0), on_models=lambda files: True)
        with patch.dict(mind.turn._OPTIONAL, {"embed": embed, "model_pack": packs}), \
                patch.object(mind.turn, "EMBED_RECHECK_S", 0), patch.object(mind.turn, "log", lambda *parts: None):
            memory.embedder = embedder
            self.mind._hold_embedder()
            await self.mind._embed_check
        self.assertIs(memory.embedder, embedder)
        self.assertEqual(answers, [])
        memory.embedder = None

    async def test_a_development_copy_is_used_without_a_pack(self):
        memory = self.mind._primary_memory
        embedder = object()
        embed = SimpleNamespace(model_dir=lambda: Path("/tmp/checkout/blink-8M"))
        packs = SimpleNamespace(trusted=lambda *a: (False, "never asked"), on_models=lambda files: False)
        with patch.dict(mind.turn._OPTIONAL, {"embed": embed, "model_pack": packs}):
            memory.embedder = embedder
            self.mind._hold_embedder()
        self.assertIs(memory.embedder, embedder)
        self.assertIsNone(self.mind._embed_check)
        memory.embedder = None
