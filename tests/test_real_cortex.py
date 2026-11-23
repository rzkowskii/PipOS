"""The mind against a real llama-server.

Skipped unless one is listening on 127.0.0.1:8377. Set BEING_TEST_CORTEX_PORT
to exercise a private server and its disposable slot. Start the usual server with:

    lab/cortex-dev.sh start

Everything else in tests/ uses a scripted stand-in, which cannot show whether
the real server accepts what the mind sends it.
"""

import asyncio
import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path

import mind.turn
from mind.cortex import Cortex
from mind.memory import Memory
from mind.speech import EMOTIONS, GRAMMAR
from mind.turn import Config, Mind
from tests.test_mind import FakeBus, said

PORT = int(os.environ.get("BEING_TEST_CORTEX_PORT", 8377))
URL = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent.parent


def get(path):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"null")
    finally:
        conn.close()


def post(payload):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=120)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(payload), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def reachable():
    try:
        return get("/health")[0] == 200
    except OSError:
        return False


@unittest.skipUnless(reachable(), f"no llama-server on 127.0.0.1:{PORT}")
class RealCortex(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mind.turn.log = lambda *parts: None
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.bus = FakeBus()
        self.memory = Memory(self.root)
        identity = (ROOT / "config" / "identity.md").read_text().replace("{name}", "Pip").replace("{owner}", "Richard")
        cfg = Config(name="Pip", owner="Richard", identity=identity, settle=0.05, cortex_wait=5)
        self.mind = Mind(self.bus, Cortex(URL), self.memory, cfg, self.root)
        self.task = asyncio.create_task(self.mind.run())
        await self.bus.wait("mind.ready", timeout=20)
        self.bus.push("body.lid", closed=False)
        self.bus.push("body.senses", line="19:05, battery 82% charging, cool, room bright")

    async def asyncTearDown(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.memory.close()
        self.dir.cleanup()

    async def hear(self, text, turn):
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard")
        await self.bus.wait("mind.reply_done", turn, timeout=120)

    async def wait_for_warmup(self):
        async with asyncio.timeout(20):
            while self.mind._warm_task is None:
                await asyncio.sleep(0.01)
            await self.mind._warm_task

    async def test_it_reports_a_working_cortex(self):
        self.assertTrue(self.bus.of("mind.ready")[0]["cortex"])

    async def test_a_real_reply_has_an_emotion_and_words(self):
        for i, text in enumerate(["Hello, what makes a kite fly?", "What is your name?", "Tell me about the sea."], 1):
            self.bus.sent.clear()
            await self.hear(text, 1)
            spoken = said(self.bus)
            self.assertGreater(len(spoken.split()), 1, f"nothing to say to {text!r}")
            self.assertNotIn("lost my thought", spoken)
            self.assertNotIn("[", spoken)
            self.assertNotIn("think>", spoken)
            self.assertIn(self.bus.of("mind.emotion")[0]["name"], EMOTIONS)

    async def test_reading_ahead_is_reused_by_the_reply(self):
        # These API calls bypass Mind's scheduling. Wait for its background
        # startup work so only this test writes the slot.
        await self.wait_for_warmup()
        system = {"role": "system", "content": self.mind.system}
        heard = "(19:05, battery 82% charging)\nI was wondering whether you could tell me"
        cortex = self.mind.cortex
        # Words arrive a few at a time; each read-ahead extends the last.
        for words in (9, 11, 13):
            part = " ".join(heard.split(" ")[:words])
            self.assertTrue(await cortex.prefill([system, {"role": "user", "content": part}]))
        _, reply = await asyncio.to_thread(
            post,
            {"messages": [system, {"role": "user", "content": heard + " something about the sea"}],
             "max_tokens": 24, "cache_prompt": True, "grammar": GRAMMAR,
             "chat_template_kwargs": {"enable_thinking": False}},
        )
        timings = reply["timings"]
        self.assertGreater(timings["cache_n"], 150, "the earlier reading was thrown away")
        # Only the last few words and the turn markers are left to read.
        self.assertLess(timings["prompt_n"], 16, "it re-read what it had already read")
        self.assertTrue(reply["choices"][0]["message"]["content"].startswith("["))

    async def test_verified_startup_prefix_is_reused_after_slot_restore(self):
        await self.wait_for_warmup()
        count = len(self.mind._warmed_prefix)
        self.assertGreater(count, 0, "startup should warm Pip's identity")
        cortex = self.mind.cortex
        messages = self.mind._prompt({
            "role": "user",
            "content": self.mind._user_content("tell me about the sea", self.mind.senses),
        })
        payload = cortex._payload(messages, 24, 0, GRAMMAR)
        self.assertTrue(await cortex.slot("save"))
        status, warm = await asyncio.to_thread(post, payload)
        self.assertEqual(status, 200, warm)
        self.assertEqual(warm["timings"]["cache_n"], count)
        self.assertTrue(await cortex.slot("erase"))
        self.assertTrue(await cortex.slot("restore"))
        status, restored = await asyncio.to_thread(post, payload)
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored["timings"]["cache_n"], count)
        self.assertEqual(restored["timings"]["prompt_n"], warm["timings"]["prompt_n"])

    async def test_cancelling_really_stops_the_server_generating(self):
        self.bus.push("state.changed", state="attending")
        self.mind.cfg.max_tokens = 2000
        self.bus.push("hear.final", text="Count slowly from one to five hundred, every number.", speaker="Richard")
        await self.bus.wait("say.request", timeout=60)
        self.bus.push("mind.cancel")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if not get("/slots")[1][0]["is_processing"]:
                break
        self.assertFalse(get("/slots")[1][0]["is_processing"], "the server kept generating after the cancel")

    async def test_saving_restoring_and_erasing_its_cache(self):
        await self.hear("Tell me about the sea.", 1)
        cortex = self.mind.cortex
        self.assertTrue(await cortex.slot("save"))
        self.assertTrue(await cortex.slot("restore"))
        self.assertTrue(await cortex.slot("erase"))


if __name__ == "__main__":
    unittest.main()
