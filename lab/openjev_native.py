"""Serve the academic OpenJev pointer head over a quantized llama.cpp backbone.

This local lab adapter implements the Choice questions used by PipOS. Its
question rendering and reserved-token packing follow OpenJev's Apache-2.0
wev/api.py and wev/model.py so the same checkpoint head can score the rows.
"""

import argparse
import ctypes
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
import re
import time

import numpy as np
from safetensors.numpy import load_file
from tokenizers import Tokenizer


SPECIAL = ("<|fim_prefix|>", "<|fim_middle|>", "<|box_start|>", "<|box_end|>", "<|fim_suffix|>")
RESERVED = re.compile(r"<\|([A-Za-z0-9_]+)\|>")
MAX_QUESTIONS = 8
MAX_BODY_BYTES = 65536
MODEL_NAME = "openjev-1-7b-native"


def render(value, depth=0):
    """Flatten structured state using the column and line layout seen in training."""
    if value is None:
        return ""
    padding = "  " * depth
    if isinstance(value, list):
        return "\n".join(f"{padding}- {render(item, depth + 1).lstrip()}" for item in value)
    if isinstance(value, dict):
        return "\n".join(
            f"{padding}{key}:\n{render(item, depth + 1)}"
            if isinstance(item, (dict, list)) else f"{padding}{key}: {render(item)}"
            for key, item in value.items()
        )
    return str(value)


def pack(tokenizer, request, max_tokens):
    def encode(value):
        return tokenizer.encode(RESERVED.sub(r"<¦\1¦>", value), add_special_tokens=False).ids

    if not isinstance(request, dict) or "state" not in request:
        raise ValueError("a request needs a state")
    questions = request.get("questions")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"a request needs 1 through {MAX_QUESTIONS} questions")
    state_token, question_token, option_token, close_token, decide_token = (
        tokenizer.token_to_id(token) for token in SPECIAL
    )
    if state_token is None or question_token is None or option_token is None or close_token is None or decide_token is None:
        raise ValueError("tokenizer is missing the OpenJev delimiters")
    ids = [state_token, *encode(render(request["state"]))]
    prefix = len(ids)
    positions = list(range(prefix))
    segments = [0] * prefix
    readouts = []
    choices = []
    for index, (question_id, question) in enumerate(questions.items(), 1):
        if not isinstance(question, dict) or question.get("type") != "choice" or "instructions" not in question:
            raise ValueError("the adapter accepts Choice questions with instructions")
        criteria = question.get("criteria")
        if not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255:
            raise ValueError("a Choice question needs 1 through 255 options")
        start = len(ids)
        ids.extend([question_token, *encode(render(question["instructions"]))])
        option_ends = []
        for key, desc in criteria.items():
            label = key if desc is None or desc == "" else f"{key}: {render(desc)}"
            ids.extend([option_token, *encode(label), close_token])
            option_ends.append(len(ids) - 1)
        ids.append(decide_token)
        readouts.extend([len(ids) - 1, *option_ends])
        choices.append((question_id, list(criteria)))
        positions.extend(range(prefix, prefix + len(ids) - start))
        segments.extend([index] * (len(ids) - start))
        if len(ids) > max_tokens:
            raise ValueError(f"request exceeds the {max_tokens}-token limit")
    return ids, positions, segments, readouts, choices


class NativeModel:
    def __init__(self, bridge, model, source, *, threads, max_tokens):
        self.lib = ctypes.CDLL(str(bridge.resolve(strict=True)))
        ints = np.ctypeslib.ndpointer(dtype=np.int32, ndim=1, flags="C_CONTIGUOUS")
        floats = np.ctypeslib.ndpointer(dtype=np.float32, ndim=2, flags="C_CONTIGUOUS")
        self.lib.openjev_load.argtypes = [ctypes.c_char_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
        self.lib.openjev_load.restype = ctypes.c_void_p
        self.lib.openjev_free.argtypes = [ctypes.c_void_p]
        self.lib.openjev_free.restype = None
        self.lib.openjev_width.argtypes = [ctypes.c_void_p]
        self.lib.openjev_width.restype = ctypes.c_int32
        self.lib.openjev_hidden.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                                           ints, ints, ints, ctypes.c_int32, ints, floats]
        self.lib.openjev_hidden.restype = ctypes.c_int

        checkpoint = json.loads((source / "wev.json").read_text())
        if checkpoint["head_type"] != "pointer" or checkpoint["base"] != "Qwen/Qwen3-1.7B-Base":
            raise ValueError("expected the academic OpenJev-1.7B pointer checkpoint")
        self.temperature = float(checkpoint.get("temperature") or 1.0)
        if not math.isfinite(self.temperature) or self.temperature <= 0:
            raise ValueError("expected a positive finite checkpoint temperature")
        self.tokenizer = Tokenizer.from_file(str(source / "tokenizer.json"))
        self.head = load_file(str(source / "head.safetensors"))
        self.max_tokens = max_tokens
        self.handle = self.lib.openjev_load(bytes(model.resolve(strict=True)), threads, max_tokens, MAX_QUESTIONS)
        if not self.handle:
            raise RuntimeError("could not load the llama.cpp bridge and backbone")
        self.width = self.lib.openjev_width(self.handle)
        head_size = checkpoint["head_dim"]
        if any(self.head[f"{name}.weight"].shape != (head_size, self.width) or
               self.head[f"{name}.bias"].shape != (head_size,) for name in ("q", "k")):
            self.close()
            raise ValueError("native backbone width and pointer head do not match")
        self.scale = 1.0 / (math.sqrt(head_size) * self.temperature)

    def close(self):
        if self.handle:
            self.lib.openjev_free(self.handle)
            self.handle = None

    def predict(self, request):
        start = time.perf_counter()
        ids, positions, segments, readouts, choices = pack(self.tokenizer, request, self.max_tokens)
        token_rows, token_positions, token_segments, output_indices = (
            np.asarray(values, dtype=np.int32) for values in (ids, positions, segments, readouts)
        )
        hidden = np.empty((len(readouts), self.width), dtype=np.float32)
        code = self.lib.openjev_hidden(self.handle, len(ids), len(choices), token_rows, token_positions,
                                       token_segments, len(readouts), output_indices, hidden)
        if code != 0:
            raise RuntimeError(f"native inference failed ({code})")
        offset = 0
        answers = {}
        for question_id, option_names in choices:
            count = len(option_names)
            query = self.head["q.weight"] @ hidden[offset] + self.head["q.bias"]
            keys = hidden[offset + 1: offset + count + 1] @ self.head["k.weight"].T + self.head["k.bias"]
            logits = (keys @ query) * self.scale
            if not np.isfinite(logits).all():
                raise RuntimeError("native inference returned invalid scores")
            probs = np.exp(logits - logits.max())
            probs /= probs.sum()
            best = int(np.argmax(probs))
            confidence = (float(probs[best]) - 1 / count) / (1 - 1 / count) if count > 1 else 1.0
            answers[question_id] = {
                "type": "choice", "choice": option_names[best], "confidence": round(confidence, 4),
                "probabilities": {name: round(float(p), 4) for name, p in zip(option_names, probs)},
            }
            offset += count + 1
        output_tokens = len(self.tokenizer.encode(json.dumps(answers), add_special_tokens=False).ids)
        return {"model": request.get("model") or MODEL_NAME, "answers": answers,
                "usage": {"input_tokens": len(ids), "output_tokens": output_tokens},
                "latency_ms": round((time.perf_counter() - start) * 1000)}


def handler(model):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def respond(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/v1/models":
                self.respond(200, {"data": [{"id": MODEL_NAME}]})
            else:
                self.respond(404, {"error": "unknown endpoint"})

        def do_POST(self):
            if self.path != "/v1/systemone":
                self.respond(404, {"error": "unknown endpoint"})
                return
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 1 <= size <= MAX_BODY_BYTES:
                    self.respond(413, {"error": "invalid request size"})
                    return
                result = model.predict(json.loads(self.rfile.read(size)))
            except (ValueError, RecursionError) as error:
                self.respond(422, {"error": str(error)})
            except RuntimeError as error:
                self.respond(500, {"error": str(error)})
            else:
                self.respond(200, result)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True, help="compiled llama.cpp bridge")
    parser.add_argument("--model", type=Path, required=True, help="native OpenJev backbone GGUF")
    parser.add_argument("--source", type=Path, required=True, help="original pointer head and tokenizer directory")
    parser.add_argument("--port", type=int, default=8382)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=1024)
    args = parser.parse_args()
    if not 1 <= args.threads <= 4 or not 128 <= args.max_tokens <= 4096:
        parser.error("threads must be 1 through 4 and max-tokens 128 through 4096")
    model = NativeModel(args.bridge, args.model, args.source, threads=args.threads, max_tokens=args.max_tokens)
    try:
        with HTTPServer(("127.0.0.1", args.port), handler(model)) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        model.close()


if __name__ == "__main__":
    main()
