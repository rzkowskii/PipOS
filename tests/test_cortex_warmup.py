import unittest
from unittest.mock import AsyncMock

from mind.cortex import Cortex


IDENTITY = [{"role": "system", "content": "You are Pip."}]
USER_HEADER = "<|system|>\nYou are Pip.<|end|>\n<|user|>\n"


def token_ids(text):
    # A terminal token disappears from the prefix when the prompt continues.
    return [1, *map(ord, text), 2]


class VerifiedWarmup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cortex = Cortex()
        self.requests = AsyncMock(side_effect=self.respond)
        self.cortex._cancellable_request = self.requests

    async def respond(self, method, path, body):
        self.assertEqual(method, "POST")
        if path == "/apply-template":
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(body["messages"][:-1], IDENTITY)
            self.assertEqual(body["messages"][-1]["role"], "user")
            content = body["messages"][-1]["content"]
            return {"prompt": USER_HEADER + content + "<|end|>\n<|assistant|>\n"}
        if path == "/tokenize":
            self.assertIs(body["add_special"], True)
            self.assertIs(body["parse_special"], True)
            return {"tokens": token_ids(body["content"])}
        self.assertEqual(path, "/completion")
        return {}

    def primes(self):
        return [call.args[2] for call in self.requests.await_args_list if call.args[1] == "/completion"]

    async def test_qwen_system_only_is_primed_with_verified_token_ids(self):
        cached = await self.cortex.warm_prefix(IDENTITY)
        final = {"messages": [*IDENTITY, {"role": "user", "content": "(17:00) Hello there."}],
                 "chat_template_kwargs": {"enable_thinking": False}}
        rendered = (await self.respond("POST", "/apply-template", final))["prompt"]
        self.assertEqual(cached, tuple(token_ids(USER_HEADER)[:-1]))
        self.assertEqual(list(cached), token_ids(rendered)[:len(cached)])
        self.assertEqual(self.primes(), [{"prompt": list(cached), "n_predict": 0, "cache_prompt": True, "id_slot": 0}])

    async def test_ambiguous_template_boundary_skips_warmup(self):
        async def duplicate_marker(method, path, body):
            response = await self.respond(method, path, body)
            if path == "/apply-template":
                response["prompt"] += body["messages"][-1]["content"]
            return response

        self.requests.side_effect = duplicate_marker
        self.assertEqual(await self.cortex.warm_prefix(IDENTITY), ())
        self.assertEqual(self.primes(), [])

    async def test_bounded_read_requires_an_exact_cached_extension(self):
        cached = await self.cortex.warm_prefix(IDENTITY)
        partial = [*IDENTITY, {"role": "user", "content": "well"}]
        self.assertFalse(await self.cortex.prefill(partial, prefix=cached, max_new_tokens=3))
        self.assertFalse(await self.cortex.prefill(partial, prefix=(0,), max_new_tokens=8))
        self.assertEqual(len(self.primes()), 1)
        self.assertTrue(await self.cortex.prefill(partial, prefix=cached, max_new_tokens=4))
        self.assertEqual(self.primes()[-1]["prompt"], token_ids(USER_HEADER + "well")[:-1])

    async def test_plain_openai_skips_the_warmup(self):
        self.cortex.llama = False
        self.assertEqual(await self.cortex.warm_prefix(IDENTITY), ())
        self.requests.assert_not_awaited()

    async def test_cold_batches_extend_the_verified_prefix_up_to_the_limit(self):
        cached = await self.cortex.warm_prefix(IDENTITY, batch_tokens=5, max_tokens=12)
        self.assertEqual(cached, tuple(token_ids(USER_HEADER)[:12]))
        self.assertEqual([prime["prompt"] for prime in self.primes()], [list(cached[:end]) for end in (5, 10, 12)])

    async def test_nonpositive_warmup_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "batch_tokens must be a positive integer"):
            await self.cortex.warm_prefix(IDENTITY, batch_tokens=0)
        with self.assertRaisesRegex(ValueError, "max_tokens must be a positive integer"):
            await self.cortex.warm_prefix(IDENTITY, max_tokens=-1)
        self.requests.assert_not_awaited()

    async def test_prime_boundary_reads_only_up_to_the_next_user_header(self):
        reply = {"role": "assistant", "content": "[joy] Hello."}

        async def with_reply(method, path, body):
            if path == "/apply-template":
                self.assertEqual(body["messages"][:-1], [*IDENTITY, reply])
                content = body["messages"][-1]["content"]
                return {"prompt": USER_HEADER + "[joy] Hello.<|end|>\n<|user|>\n" + content + "<|end|>\n<|assistant|>\n"}
            return await self.respond(method, path, body)

        self.requests.side_effect = with_reply
        primed = await self.cortex.prime_boundary([*IDENTITY, reply])
        boundary = USER_HEADER + "[joy] Hello.<|end|>\n<|user|>\n"
        self.assertEqual(primed, tuple(token_ids(boundary)[:-1]))
        self.assertEqual(self.primes(), [{"prompt": list(primed), "n_predict": 0, "cache_prompt": True, "id_slot": 0}])

    async def test_prime_boundary_needs_a_reply_last_and_respects_its_limit(self):
        self.assertEqual(await self.cortex.prime_boundary(IDENTITY), ())
        self.requests.assert_not_awaited()
        reply = [*IDENTITY, {"role": "assistant", "content": "[joy] Hi."}]

        async def with_reply(method, path, body):
            if path == "/apply-template":
                content = body["messages"][-1]["content"]
                return {"prompt": USER_HEADER + "[joy] Hi.<|end|>\n<|user|>\n" + content + "<|end|>\n"}
            return await self.respond(method, path, body)

        self.requests.side_effect = with_reply
        self.assertEqual(await self.cortex.prime_boundary(reply, max_tokens=4), ())
        self.assertEqual(self.primes(), [])
        with self.assertRaises(ValueError):
            await self.cortex.prime_boundary(reply, max_tokens=0)
        self.cortex.llama = False
        self.assertEqual(await self.cortex.prime_boundary(reply), ())

