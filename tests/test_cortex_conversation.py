"""The 6-turn prompt_n harness (lab/turn_latency.py --conversation) against a simulated server (L7.2, L7.3).

The real measurement needs the real reflex.gguf on a llama-server; this checks
that the harness counts what it should, and that it tells the stable chat
template from the model's own one, which drops the empty thinking block from
earlier replies.
"""

import asyncio
import contextlib
import io
import unittest

from lab import turn_latency
from mind.cortex import Cortex, _shared_prefix

NO_THINK = "<think>\n\n</think>\n\n"


def render(messages, *, stable=True):
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    out = []
    for index, message in enumerate(messages):
        if message["role"] == "assistant":
            think = NO_THINK if stable or index > last_user else ""
            out.append(f"<|im_start|>assistant\n{think}{message['content']}<|im_end|>\n")
        else:
            out.append(f"<|im_start|>{message['role']}\n{message['content'].strip()}<|im_end|>\n")
    return "".join(out) + "<|im_start|>assistant\n" + NO_THINK


class SimulatedStream:
    def __init__(self, timings, raw):
        self.timings, self.raw, self._done = timings, raw, False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return self.raw


class SimulatedServer(Cortex):
    """Characters are tokens; slot 0 keeps the longest prefix it was given."""

    def __init__(self, stable=True):
        super().__init__()
        self.stable, self.cache, self.replies = stable, [], 0

    async def _render_chat(self, messages):
        return render(messages, stable=self.stable)

    async def _chat_tokens(self, prompt):
        return tuple(map(ord, prompt))

    async def _prime_cache(self, prompt):
        self.cache = list(prompt)
        return True

    async def slot(self, action, filename="being.slot"):
        if action == "erase":
            self.cache = []
        return True

    def stream(self, messages, grammar=None, max_tokens=96, temperature=0.7, **sampler):
        tokens = list(map(ord, render(messages, stable=self.stable)))
        cached = len(_shared_prefix(tuple(self.cache), tuple(tokens)))
        self.replies += 1
        reply = f"[joy] Reply number {self.replies}."
        self.cache = tokens + list(map(ord, reply))
        return SimulatedStream({"prompt_n": len(tokens) - cached, "cache_n": cached}, reply)


class Conversation(unittest.TestCase):
    def run_chat(self, server, prime=False):
        with contextlib.redirect_stderr(io.StringIO()):
            return asyncio.run(turn_latency.conversation(server, "You are Pip.", prime=prime))

    def test_stable_template_reads_only_the_new_user_block(self):
        plain = self.run_chat(SimulatedServer())
        self.assertTrue(plain["turns_2_6_within_tolerance"], plain)
        for row in plain["turns"][1:]:
            self.assertEqual(row["prompt_n"], row["user_block"])
            self.assertGreater(row["cache_n"], 0)
        self.assertEqual(plain["turns"][0]["cache_n"], 0)

    def test_the_models_own_template_is_caught(self):
        upstream = self.run_chat(SimulatedServer(stable=False))
        self.assertFalse(upstream["turns_2_6_within_tolerance"])
        row = upstream["turns"][1]
        self.assertGreater(row["prompt_n"], row["user_block"] + turn_latency.TOLERANCE,
                           "the previous reply is read again")

    def test_priming_the_boundary_lowers_what_each_turn_reads(self):
        plain = self.run_chat(SimulatedServer())
        primed = self.run_chat(SimulatedServer(), prime=True)
        self.assertTrue(primed["turns_2_6_within_tolerance"], primed)
        for row in primed["turns"][1:]:
            self.assertEqual(row["primed_before"], len("<|im_end|>\n<|im_start|>user\n"))
            self.assertEqual(row["prompt_n"], row["user_block"] - row["primed_before"])
        verdict = turn_latency.compare_conversations(plain, primed)
        self.assertTrue(verdict["keep_prime_boundary"])
        self.assertLess(verdict["primed_prompt_n"], verdict["plain_prompt_n"])


if __name__ == "__main__":
    unittest.main()
