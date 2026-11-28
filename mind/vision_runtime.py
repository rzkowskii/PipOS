"""Small, bounded Unix-socket runtime for single-frame vision inference."""

import base64
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from .cortex import CHAT_TEMPLATE_KWARGS


PROMPT = (
    "Describe the main visible subject in one short sentence, including its colour if clear. "
    "For a geometric shape, name both its colour and shape. Avoid guesses about a person's "
    "identity, age, feelings or intent. If the frame is blank or too dark "
    "to make out a subject, say exactly: I can't tell."
)
MAX_TOKENS = 32
MAX_FRAME_BYTES = 512 * 1024
MAX_REPLY_BYTES = 64 * 1024
LOAD_TIMEOUT = 45
TURN_TIMEOUT = 25
PROFILES = {"small": (64, 128, 512), "reference": (1024, 1024, 1536)}


def _request(socket_path, endpoint, payload, timeout):
    """Make one local HTTP request, with a hard response-size bound."""
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(timeout)
            peer.connect(os.fspath(socket_path))
            conn.sock = peer
            conn.request("GET" if body is None else "POST", endpoint, body,
                         {"Content-Type": "application/json"})
            with conn.getresponse() as response:
                if response.status != 200:
                    raise ValueError(f"Vision server returned HTTP {response.status}.")
                data = response.read(MAX_REPLY_BYTES + 1)
                if len(data) > MAX_REPLY_BYTES:
                    raise ValueError("Vision server reply was oversized.")
                return json.loads(data)
    finally:
        conn.close()


@contextmanager
def server(binary, model, projector, scratch, profile, *, timeout=LOAD_TIMEOUT):
    """Start llama-server and yield ``(socket_path, startup_ms)`` when healthy."""
    minimum, maximum, context = PROFILES[profile]
    socket_path = Path(scratch) / "vision.sock"
    command = [os.fspath(binary), "--model", os.fspath(model), "--mmproj", os.fspath(projector),
               "--host", os.fspath(socket_path), "--no-ui", "--device", "none", "-ngl", "0",
               "--no-mmproj-offload", "--no-warmup", "-t", "2", "-tb", "2", "--parallel", "1",
               "--ctx-size", str(context), "--image-min-tokens", str(minimum),
               "--image-max-tokens", str(maximum), "--no-cache-prompt", "--cache-ram", "0",
               "--no-slots", "--no-context-shift", "--log-disable", "--jinja"]
    env = {key: value for key, value in os.environ.items() if not key.startswith("LLAMA_ARG_")}
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=os.fspath(scratch), env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        deadline = started + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError("Vision server exited during startup.")
            try:
                health = _request(socket_path, "/health", None, timeout=0.5)
                if isinstance(health, dict) and health.get("status") == "ok":
                    break
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        else:
            raise ValueError(f"Vision server did not become ready within {timeout} seconds.")
        yield socket_path, round((time.monotonic() - started) * 1000)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def describe(socket_path, image, content_type, *, timeout=TURN_TIMEOUT):
    """Describe one image and return ``(answer, finish_reason, timing)``."""
    if not isinstance(image, (bytes, bytearray, memoryview)):
        raise TypeError("image must be bytes-like")
    if content_type not in ("image/jpeg", "image/x-portable-pixmap"):
        raise ValueError("unsupported vision frame format")
    image = bytes(image)
    if not 0 < len(image) <= MAX_FRAME_BYTES:
        raise ValueError("image exceeds MAX_FRAME_BYTES")
    encoded = base64.b64encode(image).decode("ascii")
    payload = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{encoded}"}},
        {"type": "text", "text": PROMPT},
    ]}], "max_tokens": MAX_TOKENS, "temperature": 0, "seed": 103, "stream": False,
                "cache_prompt": False, "chat_template_kwargs": CHAT_TEMPLATE_KWARGS}
    started = time.monotonic()
    response = _request(socket_path, "/v1/chat/completions", payload, timeout)
    elapsed = round((time.monotonic() - started) * 1000)
    try:
        if not isinstance(response, dict):
            raise ValueError("completion response must be an object")
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("expected one completion choice")
        choice = choices[0]
        answer = choice["message"]["content"].strip()
        finish = choice["finish_reason"]
        if not isinstance(answer, str) or not isinstance(finish, str):
            raise ValueError("invalid completion fields")
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Malformed model reply.") from error
    timing = {"request_ms": elapsed}
    if isinstance(response.get("timings"), dict):
        timing.update({key: response["timings"][key] for key in
                       ("cache_n", "prompt_n", "prompt_ms", "predicted_n", "predicted_ms")
                       if type(response["timings"].get(key)) in (int, float)})
    return answer, finish, timing
