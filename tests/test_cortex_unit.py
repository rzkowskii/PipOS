"""The cortex unit, its chat template and the client's slot pinning (tasks L7.1, L7.2)."""

import json
import re
import shlex
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mind.cortex import CONVERSATION_SLOT, IDENTITY_SLOT, Cortex

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "os/package/cortex/being-cortex.service"
DREAM_SERVICE = SERVICE.with_name("being-dream-cortex.service")
RECIPE = ROOT / "os/package/cortex/cortex.mk"
TEMPLATE = ROOT / "config/chat-template.jinja"
GGUF = ROOT / "out/models/reflex.gguf"


def service_lines(path=SERVICE):
    text = path.read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def values(key, path=SERVICE):
    return [line.partition("=")[2] for line in service_lines(path) if line.partition("=")[0] == key]


def environment(path=SERVICE):
    env = {}
    for line in values("Environment", path):
        for word in shlex.split(line):
            name, _, value = word.partition("=")
            env[name] = value
    return env


def exec_start():
    (line,) = values("ExecStart")
    return shlex.split(line)


def flag(argv, name):
    return argv[argv.index(name) + 1]


class CortexUnit(unittest.TestCase):
    def test_two_slots_of_the_minds_context_each(self):
        argv = exec_start()
        self.assertEqual(argv[0], "/usr/bin/llama-server")
        self.assertEqual(flag(argv, "--parallel"), "2")
        self.assertEqual(flag(argv, "--ctx-size"), "8192")
        # The mind's own budget (config ctx_tokens) is one slot's share.
        config = json.loads((ROOT / "config/being.json").read_text())
        self.assertEqual(int(flag(argv, "--ctx-size")) // int(flag(argv, "--parallel")), config["ctx_tokens"])
        self.assertEqual(flag(argv, "--host"), "127.0.0.1")
        for kept in ("--cache-ram", "--no-cache-idle-slots", "--reasoning-budget", "--slot-save-path", "--no-webui"):
            self.assertIn(kept, argv)
        self.assertNotIn("--mlock", argv, "this llama-server has no --mlock; the load mode is in the environment")
        # Two generation and two prompt threads: more were only 1-3% faster at
        # idle and collapsed beside Piper (DEEP-DIVE item 20).
        self.assertEqual((flag(argv, "--threads"), flag(argv, "--threads-batch")),
                         ("${BEING_CORTEX_THREADS}", "${BEING_CORTEX_PROMPT_THREADS}"))
        self.assertEqual((environment()["BEING_CORTEX_THREADS"], environment()["BEING_CORTEX_PROMPT_THREADS"]),
                         ("2", "2"))

    def test_batches_attention_weight_and_load_mode(self):
        env = environment()
        # Measured on the MacBookPro9,2: 64/7 removes the 0.65 s fixed prompt
        # cost and releases a cancelled slot in about 1.9 s; flash attention
        # off decodes faster at depth.
        self.assertEqual((env["LLAMA_ARG_BATCH"], env["LLAMA_ARG_UBATCH"]), ("64", "7"))
        self.assertEqual(env["LLAMA_ARG_FLASH_ATTN"], "off")
        self.assertEqual(env["LLAMA_ARG_LOAD_MODE"], "mmap")
        self.assertNotIn("LLAMA_ARG_MLOCK", env, "not an option of the packaged llama-server")
        self.assertEqual(env["LLAMA_ARG_CTX_CHECKPOINTS"], "6")
        self.assertEqual(values("CPUWeight"), ["100"])
        self.assertEqual(values("LimitMEMLOCK"), ["infinity"])
        self.assertIn("CAP_IPC_LOCK", values("CapabilityBoundingSet")[0])
        self.assertEqual(values("OOMScoreAdjust"), ["300"])

    def test_oom_prefers_the_optional_dream_worker(self):
        (conversation_score,) = values("OOMScoreAdjust")
        (dream_score,) = values("OOMScoreAdjust", DREAM_SERVICE)
        self.assertGreater(int(dream_score), int(conversation_score))

    def test_both_model_servers_allow_same_origin_requests_only(self):
        for path in (SERVICE, DREAM_SERVICE):
            self.assertEqual(environment(path)["LLAMA_ARG_CORS_ORIGINS"], "localhost", path.name)

    def test_the_cancel_comment_is_the_laptop_measurement(self):
        text = SERVICE.read_text()
        self.assertNotIn("7.3 s to 1.75 s", text)
        self.assertIn("1.8-1.9 s", text)

    def test_chat_template_file_is_installed_where_the_unit_reads_it(self):
        path = environment()["LLAMA_ARG_CHAT_TEMPLATE_FILE"]
        self.assertEqual(path, "/usr/share/being/chat-template.jinja")
        recipe = RECIPE.read_text()
        self.assertIn("$(BR2_EXTERNAL_BEINGOS_PATH)/../config/chat-template.jinja", recipe)
        self.assertIn("$(TARGET_DIR)" + path, recipe)
        self.assertTrue(TEMPLATE.is_file())

    def test_recipe_installs_the_dream_server_unit(self):
        recipe = RECIPE.read_text()
        self.assertIn("$(CORTEX_PKGDIR)/being-dream-cortex.service", recipe)
        self.assertIn("$(TARGET_DIR)/usr/lib/systemd/system/being-dream-cortex.service", recipe)


def gguf_template(path):
    """tokenizer.chat_template from a GGUF file's metadata (standard library only)."""
    import struct
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    with open(path, "rb") as f:
        def number(fmt):
            return struct.unpack("<" + fmt, f.read(struct.calcsize(fmt)))[0]

        def string():
            return f.read(number("Q")).decode("utf-8")

        def skip(kind):
            if kind == 8:
                f.seek(number("Q"), 1)
            elif kind == 9:
                inner, count = number("I"), number("Q")
                if inner in sizes:
                    f.seek(sizes[inner] * count, 1)
                else:
                    for _ in range(count):
                        skip(inner)
            else:
                f.seek(sizes[kind], 1)

        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        number("I"), number("Q")
        for _ in range(number("Q")):
            key, kind = string(), number("I")
            if key == "tokenizer.chat_template" and kind == 8:
                return string()
            skip(kind)
    return None


class ChatTemplate(unittest.TestCase):
    def setUp(self):
        self.text = TEMPLATE.read_text()

    def test_every_past_reply_keeps_the_empty_thinking_block_of_the_generation_prompt(self):
        always = ("{{- '<|im_start|>' + message.role + '\\n<think>\\n' + reasoning_content + "
                  "'\\n</think>\\n\\n' + content }}")
        self.assertEqual(self.text.count(always), 1)
        self.assertNotIn("loop.index0 > ns.last_query_index", self.text)
        # With no reasoning, that renders the same bytes the generation prompt opened.
        self.assertIn("{{- '<think>\\n\\n</think>\\n\\n' }}", self.text)
        self.assertEqual("<think>\n" + "" + "\n</think>\n\n", "<think>\n\n</think>\n\n")

    def test_assistant_text_is_not_trimmed(self):
        branch = self.text.split('{%- elif message.role == "assistant" %}', 1)[1].split("{%- elif message.role", 1)[0]
        self.assertIn("{%- set content = render_content(message.content, false) %}", branch)
        self.assertNotIn("|trim %}\n        {%- set reasoning_content = ''", branch)
        self.assertFalse(self.text.endswith("\n"), "a trailing newline would be rendered after the prompt")

    def test_the_rest_is_the_models_own_template(self):
        if not GGUF.is_file():
            self.skipTest("no out/models/reflex.gguf")
        original = gguf_template(GGUF)
        self.assertIsNotNone(original)
        ours = re.sub(r"\A\{#-.*?#\}\n", "", self.text, flags=re.S)
        ours = re.sub(r"\n *\{#-.*?#\}", "", ours, flags=re.S)
        ours = ours.replace("\n        {%- set content = render_content(message.content, false) %}", "")
        restored = ours.replace(
            "        {{- '<|im_start|>' + message.role + '\\n<think>\\n' + reasoning_content + '\\n</think>\\n\\n' + content }}",
            "        {%- if loop.index0 > ns.last_query_index %}\n"
            "            {{- '<|im_start|>' + message.role + '\\n<think>\\n' + reasoning_content + '\\n</think>\\n\\n' + content }}\n"
            "        {%- else %}\n"
            "            {{- '<|im_start|>' + message.role + '\\n' + content }}\n"
            "        {%- endif %}")
        self.assertEqual(restored, original.rstrip("\n"))


class ReplyLength(unittest.TestCase):
    def test_replies_are_capped_at_64_tokens_in_the_strict_shape(self):
        config = json.loads((ROOT / "config/being.json").read_text())
        self.assertEqual(config["max_tokens"], 64)
        # Adopted after the audition of 24 November 2026 with the real
        # reflex.gguf: complete replies 20/21, 17/18 and 18/18 against open's
        # 20/21, 17/18 and 16/18 (lab/results/real-model-2026-11-24.json).
        self.assertEqual(config["reply_shape"], "strict")


class AuditionClient(unittest.TestCase):
    def test_the_audition_cortex_builds_streaming_payloads_with_samplers(self):
        from lab import model_audition
        profile = next(iter(model_audition.PROFILES))
        cortex = model_audition.AuditionCortex("http://127.0.0.1:1", profile)
        payload = cortex._payload([{"role": "user", "content": "hi"}], 8, 0.5, None, True, {"top_k": 20})
        self.assertEqual((payload["top_k"], payload["id_slot"], payload["seed"]), (20, 0, cortex.seed))
        cfg = model_audition.config(profile, ROOT / "config/identity.md", "strict")
        self.assertEqual((cfg.reply_shape, cfg.max_tokens), ("strict", 64))

    def test_a_reply_cut_off_at_its_token_limit_is_not_complete(self):
        from lab import model_audition
        rows = [{"completed": True, "cut_off": False, "reply": "Water evaporates."},
                {"completed": True, "cut_off": True, "reply": "Water evaporates from the"},
                {"completed": False, "cut_off": False, "reply": ""}]
        rate = model_audition.complete_rate(rows)
        self.assertEqual((rate["complete"], rate["spoken"], rate["sentence_end"], rate["trials"]), (1, 2, 1, 3))
        self.assertEqual(rate["rate"], 0.333)


class SSE(BaseHTTPRequestHandler):
    pieces = ("<think>", "\n\n</think>", "\n\n[joy] Hel", "lo there. ")
    bodies = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SSE.bodies.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in self.pieces:
            chunk = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        final = {"choices": [{"delta": {}}], "timings": {"prompt_n": 5, "cache_n": 40}}
        self.wfile.write(b"data: " + json.dumps(final).encode() + b"\n\ndata: [DONE]\n\n")


class StreamRaw(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        SSE.bodies = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SSE)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cortex = Cortex(f"http://127.0.0.1:{self.server.server_port}", timeout=5)

    async def test_raw_is_the_text_exactly_as_generated_without_thinking(self):
        stream = self.cortex.stream([{"role": "user", "content": "hi"}], max_tokens=64)
        pieces = [piece async for piece in stream]
        self.assertEqual("".join(pieces), "".join(SSE.pieces))
        self.assertEqual(stream.raw, "\n\n[joy] Hello there. ")
        self.assertEqual(stream.timings, {"prompt_n": 5, "cache_n": 40})
        path, body = SSE.bodies[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual((body["id_slot"], body["max_tokens"], body["cache_prompt"]), (CONVERSATION_SLOT, 64, True))

    async def test_an_unfinished_thinking_block_is_dropped(self):
        stream = self.cortex.stream([{"role": "user", "content": "hi"}])
        stream._pieces = ["[joy] Yes.", "<think>still going"]
        self.assertEqual(stream.raw, "[joy] Yes.")
        async for _ in stream:
            pass

    async def test_plain_openai_servers_get_no_slot_field(self):
        self.cortex.llama = False
        self.assertNotIn("id_slot", self.cortex._payload([], 8, 0.5))


class SlotCalls(unittest.IsolatedAsyncioTestCase):
    async def test_slot_actions_go_to_the_conversation_slot_with_a_bare_file_name(self):
        calls = []

        async def request(method, path, body):
            calls.append((method, path, body))
            if path == "/slots":
                return [{"id": 1, "n_ctx": 4096}, {"id": 0, "n_ctx": 4096, "is_processing": False}]
            return {}

        cortex = Cortex()
        cortex._cancellable_request = request
        self.assertTrue(await cortex.slot("save", IDENTITY_SLOT))
        self.assertTrue(await cortex.slot("restore"))
        self.assertFalse(await cortex.slot("save", "../etc/passwd"))
        self.assertFalse(await cortex.slot("delete"))
        self.assertEqual([(m, p, b) for m, p, b in calls], [
            ("POST", "/slots/0?action=save", {"filename": "identity.slot"}),
            ("POST", "/slots/0?action=restore", {"filename": "being.slot"})])
        self.assertEqual((await cortex.slot_info())["id"], 0)
        workshop = Cortex(slot_id=1)
        workshop._cancellable_request = request
        self.assertEqual((await workshop.slot_info())["id"], 1)
        with self.assertRaises(ValueError):
            Cortex(slot_id=-1)


if __name__ == "__main__":
    unittest.main()
