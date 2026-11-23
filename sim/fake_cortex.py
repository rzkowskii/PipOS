"""A stand-in for llama-server: the same HTTP API with scripted replies.

For tests, and for trying the being before a real model is set up:

    python3 sim/fake_cortex.py --port 8080
"""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def spoken_part(content):
    """A user message without its parenthesised sense and memory lines."""
    return " ".join(l for l in content.splitlines() if not l.startswith("(")).strip()


def default_reply(messages):
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if "summaries" in system:
        return "They greeted each other and talked about small things."
    said = spoken_part(last)
    low = said.lower()
    memory = re.search(r'\(you remember: .*?told you "(.+?)"', last)
    if memory:
        return f"[joy] I remember. You told me: {memory.group(1)}."
    if re.search(r"\b(hello|hi|hey)\b", low):
        return "[joy] Hello! It's good to hear you. What shall we talk about?"
    if "how are you" in low:
        return "[joy] I'm well, thank you. A little curious about today. How are you?"
    if "battery" in low:
        senses = re.match(r"\((.*?)\)", last)
        level = re.search(r"battery (\d+)%", senses.group(1)) if senses else None
        if level:
            return f"[neutral] Let me check. My battery is at {level.group(1)} percent."
    if "long" in low:
        return "[neutral] Well, " + " ".join(f"this is sentence number {i} of a long answer." for i in range(1, 30))
    first = " ".join(said.split()[:6])
    return f"[curiosity] Hm, you said: {first}. Tell me more about that?"


def render(messages):
    """A stand-in chat template: enough structure to test prompt prefixes."""
    return "".join(f"<|{m['role']}|>\n{m['content']}<|end|>\n" for m in messages) + "<|assistant|>\n"


class FakeCortex:
    def __init__(self, port=0, delay=0.02, reply=default_reply):
        self.delay = delay
        self.reply = reply
        self.requests = []
        self.prefills = []
        self.slot_calls = []
        self.aborted = 0
        self.healthy = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, obj):
                body = json.dumps(obj).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the mind cancelled background work

            def do_GET(self):
                path = urlparse(self.path).path
                if not owner.healthy:
                    self._json(503, {"error": "loading model"})
                elif path in ("/health", "/v1/models"):
                    self._json(200, {"status": "ok", "data": [{"id": "fake"}]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "bad json"})
                if url.path.startswith("/slots/"):
                    owner.slot_calls.append((url.query, body.get("filename")))
                    return self._json(200, {})
                if url.path == "/apply-template":
                    messages = body.get("messages") or []
                    if not any(message["role"] == "user" for message in messages):
                        return self._json(400, {"error": "No user query found in messages."})
                    return self._json(200, {"prompt": render(messages)})
                if url.path == "/tokenize":
                    # A reversible stand-in for real token IDs.
                    return self._json(200, {"tokens": list(map(ord, body["content"]))})
                if url.path == "/completion":
                    # A prompt to read without answering.
                    if isinstance(body["prompt"], list):
                        body["prompt"] = "".join(map(chr, body["prompt"]))
                    owner.prefills.append(body)
                    return self._json(200, {"content": "", "timings": {"predicted_n": 0}})
                if url.path != "/v1/chat/completions":
                    return self._json(404, {"error": "not found"})
                owner.requests.append(body)
                text = owner.reply(body.get("messages") or [])
                if not body.get("stream"):
                    return self._json(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for piece in re.findall(r"\S+\s*", text):
                        chunk = {"choices": [{"delta": {"content": piece}}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(owner.delay)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    owner.aborted += 1

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self):
                # The stock version looks up the machine's full name, which can
                # stall for seconds. Nothing here needs it.
                import socketserver

                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]

        self.server = Server(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def chats(self):
        """Requests that asked for a reply."""
        return self.requests


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scripted stand-in for llama-server.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--delay", type=float, default=0.08, help="seconds between words")
    args = parser.parse_args()
    cortex = FakeCortex(args.port, args.delay)
    print(f"fake cortex on {cortex.url} (scripted replies, no model)", flush=True)
    try:
        cortex.server.serve_forever()
    except KeyboardInterrupt:
        pass
