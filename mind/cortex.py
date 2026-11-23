"""The cortex: a language model behind an OpenAI-compatible HTTP API.

On the laptop this is llama-server on localhost. Pointing `base_url` at a bigger
machine is the whole of "assisted mode"; nothing else here changes.
"""

import asyncio
import http.client
import json
import re
import socket
import threading
from urllib.parse import urlparse


# The file the model server saves its cached state to, under its
# --slot-save-path directory.
SLOT_FILE = "being.slot"
# The identity snapshot: the cache right after a clean identity warm-up,
# restored after a forget, a guest change or an erase.
IDENTITY_SLOT = "identity.slot"
# being-cortex.service runs two slots: 0 is the conversation, 1 the
# workshop's code writing (mind/hands.py). Every request here is pinned to
# slot 0, so a code job never lands on, or evicts, the conversation's cache.
CONVERSATION_SLOT = 0
CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}
SAMPLER_KEYS = ("top_k", "top_p", "min_p", "presence_penalty", "repeat_penalty")


class CortexError(Exception):
    pass


_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)


def _shared_prefix(left, right):
    for index, (before, after) in enumerate(zip(left, right)):
        if before != after:
            return left[:index]
    return left[:len(right)]


class Disconnect:
    """Let the event loop interrupt a blocking HTTP worker's socket."""

    def __init__(self):
        self.sock = None
        self.cancelled = False

    def attach(self, sock):
        self.sock = sock
        if self.cancelled:
            self.cancel()

    def cancel(self):
        self.cancelled = True
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Stream:
    """A reply arriving piece by piece. Iterate it; call cancel() to cut it off."""

    def __init__(self, cortex, payload):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._disconnect = Disconnect()
        self._error = None
        # Written by the reading thread, read by the event loop: plain values.
        # (phase, tokens): ("prefill", prompt tokens read so far) while the
        # server reports prompt progress, then ("decode", pieces written).
        self.progress = (None, 0)
        # The server's own numbers for the finished reply (llama-server's
        # "timings": prompt_n, cache_n, predicted_n and their rates).
        self.timings = None
        # Every content piece, in order, exactly as the server sent it.
        self._pieces = []
        thread = threading.Thread(target=self._run, args=(cortex, payload), daemon=True)
        thread.start()

    def _run(self, cortex, payload):
        conn = None
        try:
            conn = cortex._connection()
            conn.request(
                "POST",
                cortex.path + "/v1/chat/completions",
                json.dumps(payload).encode(),
                {"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            # Keep the socket itself: once the response is read, the connection
            # object may let go of it, and cancel() still has to reach it.
            self._disconnect.attach(conn.sock)
            if self._disconnect.cancelled:
                return
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read(300).decode("utf-8", "replace")
                raise CortexError(f"HTTP {resp.status}: {detail}")
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choice = (chunk.get("choices") or [{}])[0]
                except (ValueError, AttributeError):
                    continue
                self._observe(chunk)
                text = (choice.get("delta") or {}).get("content") if isinstance(choice, dict) else None
                if text:
                    phase, tokens = self.progress
                    self.progress = ("decode", tokens + 1 if phase == "decode" else 1)
                    self._pieces.append(text)
                    self._put(text)
        except Exception as exc:  # the thread must always hand back an ending
            if not self._disconnect.cancelled:
                self._error = exc
        finally:
            if conn is not None:
                conn.close()
            self._put(None)

    def _observe(self, chunk):
        """Note prompt progress and final timings, when the server sends them."""
        if not isinstance(chunk, dict):
            return
        prompt = chunk.get("prompt_progress")
        if isinstance(prompt, dict) and self.progress[0] != "decode":
            processed, cache = prompt.get("processed"), prompt.get("cache")
            if type(processed) is int:
                read = processed - cache if type(cache) is int and cache <= processed else processed
                self.progress = ("prefill", max(0, read))
        timings = chunk.get("timings")
        if isinstance(timings, dict):
            self.timings = {key: value for key, value in timings.items() if type(value) in (int, float)}

    def _put(self, item):
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            pass  # the event loop has shut down

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            if self._error is not None:
                raise CortexError(str(self._error)) from self._error
            raise StopAsyncIteration
        return item

    def cancel(self):
        """Stop the reply. Closing the connection also stops the server generating."""
        self._disconnect.cancel()

    @property
    def raw(self):
        """What the model wrote so far, exactly, with any <think> block removed.

        Nothing is trimmed or cleaned. Kept in the prompt as the assistant
        turn, it renders (config/chat-template.jinja) byte for byte as it was
        generated, so the next turn reads only the new words. An unfinished
        thinking block is dropped.
        """
        return _THINK.sub("", "".join(self._pieces))


class Cortex:
    def __init__(self, base_url="http://127.0.0.1:8080", model="default", llama=True, timeout=180.0,
                 slot_id=CONVERSATION_SLOT):
        url = urlparse(base_url)
        if url.scheme != "http" or not url.hostname:
            raise ValueError(f"cortex URL must be http://host:port, got {base_url!r}")
        self.host = url.hostname
        self.port = url.port or 80
        self.path = url.path.rstrip("/")
        self.model = model
        # llama-server takes extra fields (grammar, prompt cache) that a plain
        # OpenAI-compatible server may reject.
        self.llama = llama
        self.timeout = timeout
        if type(slot_id) is not int or slot_id < 0:
            raise ValueError("slot_id must be a non-negative integer")
        self.slot_id = slot_id

    def _connection(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _payload(self, messages, max_tokens, temperature, grammar=None, stream=False, sampler=None):
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        for key, value in (sampler or {}).items():
            if key in SAMPLER_KEYS and type(value) in (int, float):
                payload[key] = value
        if self.llama:
            payload["cache_prompt"] = True
            payload["id_slot"] = self.slot_id
            if stream:
                # Prompt-reading progress in the stream, so a long cold read
                # can be told from a hang; the server's timings come at the end.
                payload["return_progress"] = True
            # No hidden "thinking" before the answer. It must be switched off in
            # the chat template itself: a model that opens a thinking block
            # collides with the grammar below and returns nothing to say.
            payload["chat_template_kwargs"] = CHAT_TEMPLATE_KWARGS
            if grammar:
                payload["grammar"] = grammar
        return payload

    def _request(self, method, path, body=None, timeout=None, disconnect=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            if disconnect is not None:
                if disconnect.cancelled:
                    raise CortexError("request cancelled")
                conn.connect()
                disconnect.attach(conn.sock)
                if disconnect.cancelled:
                    raise CortexError("request cancelled")
            conn.request(method, self.path + path, data, headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise CortexError(f"HTTP {resp.status} from {path}: {raw[:300].decode('utf-8', 'replace')}")
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                return {}
        except (OSError, http.client.HTTPException) as exc:
            raise CortexError(f"{path}: {exc}") from exc
        finally:
            conn.close()

    async def _cancellable_request(self, method, path, body):
        disconnect = Disconnect()
        try:
            return await asyncio.to_thread(self._request, method, path, body, disconnect=disconnect)
        except asyncio.CancelledError:
            disconnect.cancel()
            raise

    async def healthy(self):
        """True if a model is loaded and answering."""
        for path in ("/health", "/v1/models"):
            try:
                await asyncio.to_thread(self._request, "GET", path, None, 3.0)
                return True
            except CortexError:
                continue
        return False

    def stream(self, messages, grammar=None, max_tokens=96, temperature=0.7, **sampler):
        """Stream a reply. `sampler` may hold top_k, top_p and presence_penalty."""
        return Stream(self, self._payload(messages, max_tokens, temperature, grammar, stream=True, sampler=sampler))

    async def complete(self, messages, max_tokens=160, temperature=0.3):
        """A whole reply at once, for background work such as summaries."""
        payload = self._payload(messages, max_tokens, temperature)
        reply = await self._cancellable_request("POST", "/v1/chat/completions", payload)
        try:
            return reply["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise CortexError("reply had no content") from exc

    async def _render_chat(self, messages):
        response = await self._cancellable_request(
            "POST",
            "/apply-template",
            {"messages": messages, "chat_template_kwargs": CHAT_TEMPLATE_KWARGS},
        )
        prompt = response.get("prompt") if isinstance(response, dict) else None
        return prompt if isinstance(prompt, str) else ""

    async def _chat_tokens(self, prompt):
        # Match the text-chat path in the pinned llama-server. The tokenize
        # endpoint otherwise defaults to omitting added special tokens.
        response = await self._cancellable_request(
            "POST",
            "/tokenize",
            {"content": prompt, "add_special": True, "parse_special": True},
        )
        tokens = response.get("tokens") if isinstance(response, dict) else None
        if not isinstance(tokens, list) or any(type(token) is not int or token < 0 for token in tokens):
            return ()
        return tuple(tokens)

    async def _prime_cache(self, prompt):
        await self._cancellable_request(
            "POST", "/completion",
            {"prompt": prompt, "n_predict": 0, "cache_prompt": True, "id_slot": self.slot_id},
        )
        return True

    async def verified_prefix(self, messages):
        """Return stable token IDs before a prospective user message.

        Some templates, including Qwen3.5, reject a system-only message list.
        Render two disposable user continuations to locate the boundary and
        trim any trailing tokens that change when more text is present.
        """
        if not self.llama or not messages:
            return ()

        head = None
        full_prompts = []
        for marker in ("PipWarmupBoundaryA", "(PipWarmupBoundaryB)"):
            prompt = await self._render_chat([*messages, {"role": "user", "content": marker}])
            if prompt.count(marker) != 1:
                return ()
            candidate = prompt.partition(marker)[0]
            if not candidate or (head is not None and candidate != head):
                return ()
            head = candidate
            full_prompts.append(prompt)

        stable = await self._chat_tokens(head)
        for prompt in full_prompts:
            stable = _shared_prefix(stable, await self._chat_tokens(prompt))
            if not stable:
                break
        return stable

    async def warm_prefix(self, messages, *, batch_tokens=None, max_tokens=None):
        """Prime verified tokens, optionally using interruptible cold batches.

        Batches extend the prompt without trimming it. Use them on a cold slot
        so the early requests do not replace a longer restored cache.
        """
        for name, value in (("batch_tokens", batch_tokens), ("max_tokens", max_tokens)):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        prefix = (await self.verified_prefix(messages))[:max_tokens]
        if prefix:
            step = batch_tokens or len(prefix)
            for start in range(0, len(prefix), step):
                await self._prime_cache(list(prefix[:start + step]))
        return prefix

    async def prime_boundary(self, messages, *, max_tokens=None):
        """Read the turn boundary after a reply, while the reply is spoken.

        `messages` is the prompt with the reply just given as its last
        (assistant) message. The verified tokens up to where the next user
        message will start are primed: the end-of-turn marker and the next
        user header, a handful of tokens, so the next turn starts with less to
        read. Nothing is generated. Returns the primed token IDs (empty if
        nothing was primed); they are also a valid `prefix` for prefill().

        The prompt must end with an assistant message, or nothing is done.
        """
        if not self.llama or not messages or messages[-1].get("role") != "assistant":
            return ()
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer")
        prefix = await self.verified_prefix(messages)
        if not prefix or (max_tokens is not None and len(prefix) > max_tokens):
            return ()
        await self._prime_cache(list(prefix))
        return prefix

    async def prefill(self, messages, *, prefix=None, max_new_tokens=None):
        """Have the model read a prompt without answering, so the read is cached.

        This is "thinking while listening": on a slow CPU, reading costs about as
        much as writing, so it is done while the person is still talking.

        What is read must end exactly where the last message's text ends. A chat
        request would add the end-of-turn markers after it, and the next, longer
        prompt would then differ from the cache a few tokens back. Some models
        cannot step back at all, and the whole turn would be read again. So the
        prompt is rendered, cut after the last words, and sent raw.

        For a bounded read, supply the verified token prefix already primed in
        the slot and the allowed number of new tokens.

        llama-server only. Returns True if something was read.
        """
        if not self.llama or not messages:
            return False
        bounded = prefix is not None or max_new_tokens is not None
        if bounded and (not prefix or max_new_tokens is None or max_new_tokens <= 0):
            return False
        rendered = await self._render_chat(messages)
        tail = messages[-1]["content"].rstrip()
        if not tail or tail not in rendered:
            return False  # the template changed the text; nothing safe to cut at
        prompt = rendered[: rendered.rindex(tail) + len(tail)]
        if bounded:
            tokens = _shared_prefix(await self._chat_tokens(prompt), await self._chat_tokens(rendered))
            count = len(prefix)
            if tokens[:count] != prefix or not 0 < len(tokens) - count <= max_new_tokens:
                return False
            prompt = list(tokens)
        return await self._prime_cache(prompt)

    async def slot(self, action, filename=SLOT_FILE):
        """Save, restore or erase the conversation slot's cached state
        (llama-server --slot-save-path). `filename` is SLOT_FILE or
        IDENTITY_SLOT: a bare name inside that folder."""
        if not self.llama or action not in ("save", "restore", "erase"):
            return False
        if not isinstance(filename, str) or not filename or "/" in filename or filename.startswith("."):
            return False
        try:
            await self._cancellable_request(
                "POST", f"/slots/{self.slot_id}?action={action}", {"filename": filename}
            )
            return True
        except CortexError:
            return False

    async def slot_info(self):
        """The server's /slots entry for this slot (n_ctx, is_processing ...),
        or None when the server does not say."""
        try:
            slots = await self._cancellable_request("GET", "/slots", None)
        except CortexError:
            return None
        if not isinstance(slots, list):
            return None
        for entry in slots:
            if isinstance(entry, dict) and entry.get("id") == self.slot_id:
                return entry
        return None
