"""hands end to end on the Mac: a real local shell on a real pty (LocalCage,
not a cage), a recording renderer, a fake llama-server for slot 1, and the
mind's AppBridge wired to hands through an in-memory bus."""

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mind import hands as hands_module
from mind.app_commands import AppBridge
from mind.hands import CodeCortex, Hands, LocalCage, RecordingRenderer

ROOT = Path(__file__).resolve().parent.parent
SHELF = ROOT / "mind/workshop_shelf"
SHELLRC = ROOT / "os/package/being/being-hands-shellrc"

# BusyBox sed takes `sed -i '1i text' file`; the Mac's BSD sed does not. The
# free fixes Pip types use only these two forms, so a tiny stand-in will do.
SED_SHIM = r'''#!/usr/bin/env python3
import re, sys
args = sys.argv[1:]
assert args[0] == "-i", args
script, path = args[1], args[2]
lines = open(path).read().split("\n")
if lines and lines[-1] == "":
    lines.pop()
insert = re.fullmatch(r"(\d+)i (.*)", script)
delete = re.fullmatch(r"(\d+),\$d", script)
if insert:
    lines.insert(int(insert[1]) - 1, insert[2])
elif delete:
    lines = lines[: int(delete[1]) - 1]
else:
    sys.exit("unsupported")
open(path, "w").write("\n".join(lines) + "\n")
'''


class FakeLlama:
    """Enough of llama-server for slot 1: apply-template, tokenize, a
    streaming /completion with token IDs, and slot erase."""

    def __init__(self):
        self.requests = []
        self.replies = []        # one list of text chunks per /completion
        self.delay = 0.0
        self.erased = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                owner.requests.append((self.path, body))
                if self.path == "/apply-template":
                    prompt = "".join(f"<{m['role']}>{m['content']}" for m in body["messages"]) + "<assistant>"
                    return self.reply({"prompt": prompt})
                if self.path == "/tokenize":
                    return self.reply({"tokens": list(range(1, min(len(body["content"]), 300) + 1))})
                if self.path.startswith("/slots/1"):
                    owner.erased += 1
                    return self.reply({})
                if self.path == "/completion":
                    chunks = owner.replies.pop(0) if owner.replies else ["```"]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        for index, chunk in enumerate(chunks):
                            time.sleep(owner.delay)
                            event = {"content": chunk, "tokens": [1000 + index], "stop": False}
                            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                            self.wfile.flush()
                        self.wfile.write(b'data: {"content": "", "stop": true}\n\n')
                        self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                    return None
                self.send_error(404)
                return None

            def reply(self, data):
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def completions(self):
        return [body for path, body in self.requests if path == "/completion"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MindBus:
    def __init__(self):
        self.sent = []
        self.hands = None

    async def send(self, topic, **fields):
        event = {"t": topic, **fields}
        self.sent.append(event)
        if self.hands is not None:
            asyncio.get_running_loop().create_task(self.hands.on_event(event))


class HandsBus:
    def __init__(self):
        self.sent = []
        self.bridge = None
        self.urgent = None

    async def send(self, topic, **fields):
        event = {"t": topic, **fields}
        self.sent.append(event)
        if self.bridge is not None:
            self.bridge.observe(event)


@unittest.skipUnless(hasattr(os, "login_tty") and os.path.exists("/bin/sh"), "needs a local pty shell")
class HandsSessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.workshop = base / "workshop"
        self.workshop.mkdir()
        tools = base / "bin"
        tools.mkdir()
        (tools / "sed").write_text(SED_SHIM.replace("/usr/bin/env python3", sys.executable))
        (tools / "sed").chmod(0o755)
        (tools / "python3").symlink_to(sys.executable)
        path = f"{tools}:{SHELF / 'bin'}:/usr/bin:/bin"
        self.cage = LocalCage(self.workshop, SHELLRC, path=path, extra_env={"BEING_SHELF": str(SHELF)})
        self.llama = FakeLlama()
        self.hands_bus, self.mind_bus = HandsBus(), MindBus()
        self.renderers = []

        def renderer(app):
            made = RecordingRenderer(app)
            self.renderers.append(made)
            return made
        self.hands = Hands(self.hands_bus, cage=self.cage, cortex=CodeCortex(self.llama.url), workshop=self.workshop,
                           renderer=renderer, disabled=base / "apps.disabled")
        self.disabled = base / "apps.disabled"
        self.bridge = AppBridge(self.mind_bus, shelf=SHELF, enabled=lambda: not self.disabled.exists())
        self.bridge.hands_up = True
        self.hands_bus.bridge = self.bridge
        self.mind_bus.hands = self.hands
        self.said = []
        self.ticker = asyncio.get_running_loop().create_task(self.hands._ticker())
        await self.hands.on_event({"t": "state.changed", "state": "idle"})
        await self.hands.on_event({"t": "body.lid", "closed": False, "seq": 1})

    async def asyncTearDown(self):
        self.ticker.cancel()
        await self.hands.forget({"all": True, "quiet": True})
        self.llama.close()
        self.dir.cleanup()

    async def say(self, line):
        self.said.append(line)

    async def hear(self, text):
        command = self.bridge.parse(text)
        self.assertIsNotNone(command, text)
        await self.bridge.handle(command, self.say)
        return command

    async def until(self, check, timeout=15.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            await asyncio.sleep(0.05)
        self.fail(f"timed out waiting for {what}; job={self.hands.job and self.hands.job.phase} "
                  f"reason={self.hands.job and self.hands.job.reason} said={self.said} "
                  f"screen={bytes(self.renderers[-1].data[-600:]) if self.renderers else b''}")

    def phase(self):
        return self.hands.job.phase if self.hands.job else None

    def screen(self):
        return self.renderers[-1].data.decode("utf-8", "replace")

    async def announced(self):
        lines = []
        while (line := self.bridge.announcement()) is not None:
            lines.append(line)
        return lines

    async def open_minimised(self):
        """Open, then minimise so Pip types at once (no animation) and the tests stay fast."""
        await self.hear("Hey Pip, open the terminal.")
        self.assertEqual(self.said[-1], "Here's my terminal.")
        await self.until(lambda: "pip:~$" in self.screen(), what="prompt")
        await self.hear("minimise the terminal")
        self.assertEqual(self.said[-1], "Okay.")

    # -- the demo -----------------------------------------------------------------

    async def test_open_minimise_restore_close(self):
        await self.hear("open the terminal")
        self.assertEqual(self.said, ["Here's my terminal."])
        state = [e for e in self.hands_bus.sent if e["t"] == "app.state"][-1]
        self.assertEqual((state["view"], state["session"]), ("shown", "being-shell-1"))
        flags = self.renderers[-1].flag_log[-1]
        self.assertEqual((flags["visible"], flags["status"], flags["title"]), (True, "Ready", "Pip's terminal"))
        await self.hear("open the terminal")
        self.assertEqual(self.said[-1], "It's already open.")
        await self.hear("hide the terminal")
        self.assertEqual(self.said[-1], "Okay.")
        self.assertFalse(self.renderers[-1].flag_log[-1]["visible"])
        await self.hear("show me the terminal")
        self.assertEqual(self.said[-1], "Here it is.")
        await self.hear("close the terminal")
        self.assertEqual(self.said[-1], "Closed.")
        self.assertEqual(self.bridge.view["terminal"], "closed")
        self.assertIn(("stop", "being-shell-1"), self.cage.calls)
        await self.hear("close the terminal")
        self.assertEqual(self.said[-1], "The terminal isn't open.")

    async def test_recipe_written_checked_and_run(self):
        await self.open_minimised()
        await self.hear("Hey Pip, write a script that draws a spiral.")
        self.assertEqual(self.said[-1], "I know this one.")
        await self.until(lambda: self.phase() == "waiting", what="written")
        self.assertIn("It's ready. Say run it.", await self.announced())
        text = self.screen()
        self.assertIn("# you asked: write a script that draws a spiral", text)
        self.assertIn("# recipe: spiral (from Pip's recipe book)", text)
        written = (self.workshop / "spiral.py").read_text()
        self.assertEqual(written, hands_module.RECIPES["spiral"].build({}))
        self.assertEqual(self.bridge.episode()[0], "I wrote spiral.py in the terminal")
        await self.hear("what happened")
        self.assertEqual(self.said[-1], "It's written from my recipe book. Say run it.")
        await self.hear("run it")
        self.assertEqual(self.said[-1], "Running it.")
        await self.until(lambda: self.phase() == "done", what="ran")
        self.assertEqual(self.hands.job.exit, 0)
        self.assertIn("It ran without errors.", await self.announced())
        self.assertIn("# # #", self.screen())
        # Written again: never overwrites.
        await self.hear("write a script that draws a spiral")
        await self.until(lambda: self.phase() == "waiting", what="second spiral")
        self.assertTrue((self.workshop / "spiral-2.py").exists())

    async def test_one_liner_runs_after_a_countdown(self):
        await self.open_minimised()
        with mock.patch.object(hands_module, "COUNTDOWN", 1):
            await self.hear("in the terminal, show me the files")
            await self.until(lambda: self.phase() == "countdown", what="countdown")
            self.assertTrue(self.screen().rstrip().endswith("ls -l"))
            await self.until(lambda: self.phase() == "done", what="listed")
        self.assertIn("total", self.screen())

    async def test_stop_during_countdown_clears_the_line(self):
        await self.open_minimised()
        await self.hear("in the terminal, show me the disk space")
        await self.until(lambda: self.phase() == "countdown", what="countdown")
        await self.hear("stop")
        await self.until(lambda: self.phase() == "cancelled", what="cancelled")
        self.assertEqual(self.said[-1], "Okay. I didn't run it.")  # never "Paused." for a run that is gone
        await asyncio.sleep(0.5)
        self.assertNotIn("Filesystem", self.screen())

    async def test_model_script_with_free_import_fix(self):
        self.llama.replies.append(["for i in range(3, 0, -1):\n", "    print(i)\n", "    time.sleep(0)\n", "```"])
        await self.open_minimised()
        await self.hear("write a script that counts backwards from three, write it yourself")
        self.assertEqual(self.said[-1], "I'll try.")
        await self.until(lambda: self.phase() == "waiting", what="written by the model")
        text = self.screen()
        self.assertIn("# written by my language model (Qwen3.5 0.8B)", text)
        self.assertIn("sed -i '1i import time' counts_backwards.py", text)
        self.assertTrue((self.workshop / "counts_backwards.py").read_text().startswith("import time\n"))
        first = self.llama.completions()[0]
        self.assertEqual((first["id_slot"], first["return_tokens"], first["cache_prompt"]), (1, True, True))
        self.assertEqual((first["temperature"], first["top_k"], first["repeat_penalty"]), (0.2, 20, 1.0))
        self.assertIn("root ::= r0", first["grammar"])
        self.assertIsInstance(first["seed"], int)
        await self.hear("run it")
        await self.until(lambda: self.phase() == "done", what="ran")
        self.assertIn("1", self.screen().split("python3 counts_backwards.py")[-1])

    async def test_policy_refusal_is_said_honestly(self):
        self.llama.replies.append(["import subprocess\n", "subprocess.run(['ls'])\n", "```"])
        await self.open_minimised()
        await self.hear("write a script that lists things, write it yourself")
        await self.until(lambda: self.phase() == "failed", what="refused")
        self.assertIn("I wrote it, but I won't run it: it imports subprocess.", await self.announced())
        await self.hear("run it")
        self.assertEqual(self.said[-1], "There's nothing ready to run.")

    async def test_failed_run_and_one_fix(self):
        self.llama.replies.append(["print(1 / 0)\n", "```"])
        self.llama.replies.append(["print(1)\n", "```"])
        await self.open_minimised()
        await self.hear("write a script that divides, write it yourself")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        await self.until(lambda: self.phase() == "failed", what="failed")
        self.assertEqual(self.hands.job.exit, 1)
        lines = await self.announced()
        self.assertIn("It failed with exit code 1. Say fix it if you'd like me to try once more.", lines)
        await self.hear("fix it")
        await self.until(lambda: self.phase() == "waiting", what="fixed")
        fix = self.llama.completions()[1]
        self.assertEqual((fix["seed"], fix["temperature"]), (self.llama.completions()[0]["seed"] + 1, 0.4))
        prompt = [body for path, body in self.llama.requests if path == "/apply-template"][-1]["messages"]
        self.assertIn("ZeroDivisionError", prompt[-1]["content"])
        self.assertTrue((self.workshop / "divides-2.py").exists())

    async def test_pause_parks_the_model_and_resume_extends_the_prompt(self):
        self.llama.delay = 0.15
        self.llama.replies.append([f"print({n})\n" for n in range(12)] + ["```"])
        await self.open_minimised()
        await self.hear("write a script that prints numbers, write it yourself")
        await self.until(lambda: self.hands.job and len(self.hands.job.tokens) >= 3, what="some tokens")
        self.llama.replies.append([f"print({n})\n" for n in range(3)] + ["```"])
        await self.hear("stop writing that script for a second")
        self.assertEqual(self.said[-1], "Paused. Say carry on when you're ready.")
        self.assertEqual(self.phase(), "paused")
        self.assertTrue(self.hands.job.parked)
        kept = list(self.hands.job.tokens)
        body = self.hands.job.body
        await self.hear("carry on")
        self.assertEqual(self.said[-1], "Carrying on.")
        await self.until(lambda: self.phase() == "waiting", what="finished after resume")
        first, second = self.llama.completions()[:2]
        self.assertEqual(second["prompt"], first["prompt"] + kept)
        self.assertTrue(second["grammar"].startswith(f"root ::= r{body.count(chr(10))}"))
        self.assertEqual((self.workshop / "prints_numbers.py").read_text(), self.hands.job.body)

    async def test_wake_phrase_holds_typing_and_quiet_releases_it(self):
        self.llama.delay = 0.1
        self.llama.replies.append([f"print({n})\n" for n in range(30)] + ["```"])
        await self.hear("open the terminal")
        await self.until(lambda: "pip:~$" in self.screen(), what="prompt")
        with mock.patch.object(hands_module, "HOLD_SECONDS", 0.6):
            await self.hear("write a script that prints a lot, write it yourself")
            await self.until(lambda: self.phase() == "writing", what="writing")
            self.hands.urgent({"t": "hear.wake"})
            self.assertEqual(self.phase(), "held")
            typed = len(self.hands._typist.typed)
            await asyncio.sleep(0.4)
            self.assertEqual(len(self.hands._typist.typed), typed)
            self.assertGreater(len(self.hands.job.tokens), 0)  # generation went on into the buffer
            await self.until(lambda: self.phase() == "writing", timeout=3, what="released")

    async def test_conversation_turn_parks_the_job(self):
        self.llama.delay = 0.2
        self.llama.replies.append([f"print({n})\n" for n in range(20)] + ["```"])
        await self.open_minimised()
        await self.hear("write a script that prints, write it yourself")
        await self.until(lambda: self.phase() == "writing", what="writing")
        self.assertTrue(self.bridge.job_holds_model)
        self.assertEqual(self.bridge.model_note(), "(You are writing a script in the terminal: prints.)")
        self.assertTrue(await self.bridge.lease())
        self.assertEqual(self.phase(), "paused")
        self.assertEqual(self.hands.job.reason, "turn")
        await self.bridge.unlease()
        await self.until(lambda: self.phase() in ("reading", "writing", "waiting"), what="resumed")

    async def test_cancel_inside_the_heredoc_writes_nothing_and_undo_retypes(self):
        await self.hear("open the terminal")  # shown: typing is paced, so it can be caught mid-way
        await self.until(lambda: "pip:~$" in self.screen(), what="prompt")
        await self.hear("write a script that draws a heart")
        await self.until(lambda: "cat > heart.py" in self.screen(), what="heredoc started")
        await self.hear("cancel that")
        self.assertEqual(self.said[-1], "Cancelled.")
        await asyncio.sleep(0.6)
        self.assertFalse((self.workshop / "heart.py").exists())
        await self.hear("minimise the terminal")
        await self.hear("carry on")
        self.assertEqual(self.said[-1], "Okay, I'll carry on with it.")
        await self.until(lambda: self.phase() == "waiting", what="retyped")
        self.assertTrue((self.workshop / "heart.py").exists())

    async def test_close_while_running_asks_first(self):
        await self.open_minimised()
        await self.hear("write a script that shows a clock for 20 seconds")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        await self.until(lambda: self.phase() == "running", what="running")
        await self.hear("close the terminal")
        self.assertEqual(self.said[-1], "Something is still running. Close it anyway?")
        await self.hear("yes")
        self.assertEqual(self.said[-1], "Closed.")
        self.assertEqual(self.bridge.view["terminal"], "closed")

    async def test_escape_stops_a_run_and_stop_freezes_it(self):
        await self.open_minimised()
        await self.hear("write a script that shows a clock for 30 seconds")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        await self.until(lambda: self.phase() == "running", what="running")
        await self.hear("stop")
        self.assertEqual(self.phase(), "frozen")
        self.assertIn(("freeze", "being-shell-1"), self.cage.calls)
        await self.hear("carry on")
        self.assertEqual(self.phase(), "running")
        await self.hands.on_event({"t": "key.interrupt"})
        await self.until(lambda: self.phase() == "failed", what="stopped")
        self.assertEqual(self.hands.job.reason, "stopped")
        self.assertIn("Stopped.", await self.announced())

    async def test_escape_leaves_the_job_alone_while_pip_speaks(self):
        await self.open_minimised()
        await self.hear("write a script that shows a clock for 30 seconds")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        await self.until(lambda: self.phase() == "running", what="running")
        await self.hands.on_event({"t": "state.changed", "state": "speaking"})
        await self.hands.on_event({"t": "key.interrupt"})
        await asyncio.sleep(0.3)
        self.assertEqual(self.phase(), "running")

    async def test_time_limit(self):
        await self.open_minimised()
        with mock.patch.object(hands_module, "RUN_LIMIT", 1.0):
            await self.hear("write a script that shows a clock for 30 seconds")
            await self.until(lambda: self.phase() == "waiting", what="written")
            await self.hear("run it")
            await self.until(lambda: self.phase() == "failed", what="limited")
        self.assertEqual(self.hands.job.reason, "time_limit")
        self.assertIn("It ran for a minute, so I stopped it.", await self.announced())

    async def test_cancel_kills_a_program_that_ignores_ctrl_c(self):
        self.llama.replies.append(["import time\n", "while True:\n", "    try:\n", "        time.sleep(60)\n",
                                   "    except KeyboardInterrupt:\n", "        print('ignored')\n", "```"])
        await self.open_minimised()
        with mock.patch.object(hands_module, "KILL_GRACE", 0.5):
            await self.hear("write a script that never stops, write it yourself")
            await self.until(lambda: self.phase() == "waiting", what="written")
            await self.hear("run it")
            await self.until(lambda: self.phase() == "running" and not self.hands.session.foreground_is_shell(),
                             what="running in front")
            await self.hear("cancel that")
            self.assertEqual(self.said[-1], "Cancelled.")
            self.assertEqual(self.phase(), "cancelled")
            # The phase has moved on, but the kill after the grace still comes.
            await self.until(lambda: ("kill_all_but", "being-shell-1") in self.cage.calls, what="the kill")
            await self.until(self.hands.session.foreground_is_shell, what="the shell back in front")

    async def test_a_shelf_game_is_not_cut_off_by_the_output_limit(self):
        await self.open_minimised()
        await self.hear("install snake")
        await self.until(lambda: self.phase() == "done", what="installed")
        with mock.patch.object(hands_module, "OUTPUT_BYTES", 200), mock.patch.object(hands_module, "OUTPUT_LINES", 5):
            await self.hear("play snake")
            self.assertEqual(self.said[-1], "Starting snake.")
            await self.until(lambda: self.phase() == "running" and self.hands.job.output_bytes > 2000, what="frames")
            await asyncio.sleep(0.3)
            self.assertEqual((self.phase(), self.hands.job.reason), ("running", ""))
            self.assertGreater(self.hands.job.deadline - self.hands.job.run_started, hands_module.RUN_LIMIT)
            await self.hear("show me the terminal")
            self.hands.on_key(b"q")  # the owner quits the game
            await self.until(lambda: self.phase() == "done", what="game over")

    async def test_time_spent_frozen_does_not_count(self):
        await self.open_minimised()
        with mock.patch.object(hands_module, "RUN_LIMIT", 1.5):
            await self.hear("write a script that shows a clock for 30 seconds")
            await self.until(lambda: self.phase() == "waiting", what="written")
            await self.hear("run it")
            await self.until(lambda: self.phase() == "running", what="running")
            await self.hear("stop")
            self.assertEqual(self.phase(), "frozen")
            await asyncio.sleep(1.8)
            await self.hear("carry on")
            self.assertEqual(self.said[-1], "Carrying on.")
            await asyncio.sleep(0.3)
            self.assertEqual(self.phase(), "running")  # not stopped the moment it thawed
            await self.until(lambda: self.phase() == "failed", what="the limit, later")
        self.assertEqual(self.hands.job.reason, "time_limit")

    async def test_carry_on_with_the_lid_closed_is_answered(self):
        self.llama.delay = 0.2
        self.llama.replies.append([f"print({n})\n" for n in range(20)] + ["```"])
        await self.open_minimised()
        await self.hear("write a script that prints, write it yourself")
        await self.until(lambda: self.phase() == "writing", what="writing")
        await self.hands.on_event({"t": "body.lid", "closed": True, "seq": 2})
        await self.hear("carry on")
        self.assertEqual(self.said[-1], "My lid is closed, so I can't use my terminal right now.")
        self.assertEqual(self.phase(), "paused")

    async def test_an_owner_freeze_survives_the_lid(self):
        await self.open_minimised()
        await self.hear("write a script that shows a clock for 30 seconds")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        await self.until(lambda: self.phase() == "running", what="running")
        await self.hear("stop")
        await self.hands.on_event({"t": "body.lid", "closed": True, "seq": 2})
        await self.hands.on_event({"t": "body.lid", "closed": False, "seq": 3})
        self.assertEqual(self.phase(), "frozen")
        self.assertNotIn(("thaw", "being-shell-1"), self.cage.calls)  # stays frozen until "carry on"
        await self.hear("carry on")
        self.assertEqual(self.phase(), "running")

    async def test_undo_of_a_shelf_job_retypes_the_helper_not_the_model(self):
        await self.hear("open the terminal")  # shown: paced typing, so the cancel lands mid-line
        await self.until(lambda: "pip:~$" in self.screen(), what="prompt")
        await self.hear("install snake")
        await self.hear("cancel that")
        self.assertEqual(self.said[-1], "Cancelled.")
        await self.hear("minimise the terminal")
        await self.hear("carry on")
        self.assertEqual(self.said[-1], "Okay, I'll carry on with it.")
        await self.until(lambda: self.phase() == "done", what="installed after undo")
        self.assertTrue((self.workshop / "apps/snake/main.py").is_file())
        self.assertEqual(self.llama.completions(), [])

    async def test_a_number_out_of_range_is_refused_honestly(self):
        await self.open_minimised()
        await self.hear("write a script that rolls 9 dice")
        self.assertEqual(self.said[-1], "My recipe for that takes a number from one to six.")

    async def test_too_warm_freezes_the_cage_and_cooling_thaws_it(self):
        await self.open_minimised()
        await self.hear("write a script that shows a clock for 30 seconds")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        await self.until(lambda: self.phase() == "running", what="running")
        await self.hands.on_event({"t": "body.state", "temp": 93.0, "battery": 80, "on_ac": True})
        self.assertEqual((self.phase(), self.hands.job.reason), ("frozen", "heat"))
        self.assertIn(("freeze", "being-shell-1"), self.cage.calls)
        await self.hear("carry on")
        self.assertEqual(self.said[-1], "I'm too warm, so my terminal is resting until I cool down.")
        self.assertEqual(self.phase(), "frozen")
        await self.hands.on_event({"t": "body.state", "temp": 85.0})  # between the thresholds: still resting
        self.assertEqual(self.phase(), "frozen")
        await self.hands.on_event({"t": "body.state", "temp": 70.0})
        self.assertEqual(self.phase(), "running")
        self.assertIn(("thaw", "being-shell-1"), self.cage.calls)

    async def test_unplugged_and_low_no_new_runs(self):
        await self.open_minimised()
        await self.hands.on_event({"t": "body.state", "temp": 50.0, "battery": 12, "on_ac": False})
        await self.hear("in the terminal, show me the files")
        self.assertEqual(self.said[-1], "My battery is low, so I won't start programs until I'm plugged in.")
        await self.hear("write a script that draws a spiral")  # writing is still fine
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        self.assertEqual(self.said[-1], "My battery is low, so I won't start programs until I'm plugged in.")
        await self.hands.on_event({"t": "body.state", "battery": 12, "on_ac": True})
        await self.hear("run it")
        self.assertEqual(self.said[-1], "Running it.")

    async def test_a_renderer_that_did_not_start_is_never_called_shown(self):
        class DeadRenderer(RecordingRenderer):
            def alive(self):
                return False
        self.hands.make_renderer = DeadRenderer
        await self.hear("open the terminal")
        self.assertEqual(self.said[-1], "My terminal didn't open.")
        self.assertEqual(self.hands.view["terminal"], "closed")
        self.assertIsNone(self.hands.session)
        self.assertIn(("stop", "being-shell-1"), self.cage.calls)

    async def test_lid_closed_freezes_and_opening_says_so(self):
        self.llama.delay = 0.2
        self.llama.replies.append([f"print({n})\n" for n in range(20)] + ["```"])
        await self.open_minimised()
        await self.hear("write a script that prints, write it yourself")
        await self.until(lambda: self.phase() == "writing", what="writing")
        await self.hands.on_event({"t": "body.lid", "closed": True, "seq": 2})
        self.assertEqual(self.phase(), "paused")
        self.assertIn(("freeze", "being-shell-1"), self.cage.calls)
        await self.hands.on_event({"t": "body.lid", "closed": False, "seq": 3})
        self.assertIn(("thaw", "being-shell-1"), self.cage.calls)
        self.assertIn("I paused while the lid was closed. Say carry on.", await self.announced())

    async def test_install_from_the_shelf_and_the_honest_refusal(self):
        await self.open_minimised()
        await self.hear("install snake")
        self.assertEqual(self.said[-1], "Installing snake. It's from my built-in shelf.")
        await self.until(lambda: self.phase() == "done", what="installed")
        self.assertIn("No internet was used", self.screen())
        self.assertTrue((self.workshop / "apps/snake/main.py").is_file())
        await self.hear("install firefox")
        self.assertTrue(self.said[-1].startswith("I can't download programs."))
        await self.hear("what can you install")
        self.assertIn("snake", self.said[-1])

    async def test_forget_that_script_removes_every_copy(self):
        await self.open_minimised()
        await self.hear("write a script that draws a tree")
        await self.until(lambda: self.phase() == "waiting", what="written")
        shutil.copy(self.workshop / "tree.py", self.workshop / "elsewhere.py")
        (self.workshop / "tree-9.py").write_text("print('changed')\n")
        erased = self.llama.erased
        await self.hear("forget that script")
        self.assertEqual(self.said[-1], "Should I forget that script and every copy of it?")
        await self.hear("yes")
        self.assertEqual(self.said[-1], "That script is forgotten. I also found changed copies: tree-9.py. "
                                        "Should I remove those too?")
        self.assertFalse((self.workshop / "tree.py").exists())
        self.assertFalse((self.workshop / "elsewhere.py").exists())
        self.assertTrue((self.workshop / "tree-9.py").exists())  # changed: asked about first
        self.assertEqual(self.renderers[-1].wiped, 1)
        self.assertNotIn(b"tree", bytes(self.hands.transcript.data))
        self.assertEqual(self.llama.erased, erased + 1)
        self.assertIsNone(self.bridge.status)
        # The old shell's in-memory history held the script: a fresh shell.
        self.assertIn(("stop", "being-shell-1"), self.cage.calls)
        self.assertEqual(self.hands.session.unit, "being-shell-2")
        await self.hear("yes")
        self.assertEqual(self.said[-1], "Okay. I removed those copies too.")
        self.assertFalse((self.workshop / "tree-9.py").exists())
        await self.until(lambda: "pip:~$" in self.screen(), what="the fresh shell's prompt")

    async def test_guest_mode_clears_and_closes(self):
        await self.open_minimised()
        await self.hear("write a script that draws a box")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.bridge.privacy(True)
        await self.until(lambda: self.hands.view["terminal"] == "closed", what="closed")
        await self.until(lambda: not any(self.workshop.iterdir()), what="cleared")
        await self.hear("open the terminal")
        self.assertEqual(self.said[-1], "My terminal stays closed while a guest is here.")

    async def test_apps_switched_off(self):
        self.disabled.write_text("")
        await self.hear("open the terminal")
        self.assertEqual(self.said[-1], "My apps are switched off. You can turn them on in Setup.")

    async def test_owners_keys_pause_pip_and_reach_the_shell(self):
        await self.hear("open the terminal")
        await self.until(lambda: "pip:~$" in self.screen(), what="prompt")
        await self.hear("write a script that draws a heart")
        await self.until(lambda: "cat > heart.py" in self.screen(), what="typing")
        self.hands.on_key(b"#")
        self.assertIsNotNone(self.hands._typist.human_until)
        typed = len(self.hands._typist.typed)
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.hands._typist.typed), typed)
        self.hands.on_key(b"\0f3")
        await self.until(lambda: self.hands.view["terminal"] == "minimised", what="F3 hides")
        await self.until(lambda: self.phase() == "waiting", what="finished after the owner's key")
        self.assertEqual((self.workshop / "heart.py").read_text(), hands_module.RECIPES["heart"].build({}))

    async def test_key_socket(self):
        path = Path(self.dir.name) / "keys"
        self.hands.key_socket_path = str(path)
        self.hands._open_key_socket()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        await self.hear("open the terminal")
        await self.until(lambda: "pip:~$" in self.screen(), what="prompt")
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            for key in (b"e", b"c", b"h", b"o", b" ", b"h", b"i", b"\r"):
                sender.sendto(key, str(path))
        finally:
            sender.close()
        await self.until(lambda: "\nhi" in self.screen().replace("\r", ""), what="echo hi")
        asyncio.get_running_loop().remove_reader(self.hands._key_sock.fileno())
        self.hands._key_sock.close()

    async def test_what_did_it_say(self):
        await self.open_minimised()
        await self.hear("write a script that tosses a coin")
        await self.until(lambda: self.phase() == "waiting", what="written")
        await self.hear("run it")
        await self.until(lambda: self.phase() == "done", what="ran")
        await self.hear("what did it say")
        self.assertIn(self.said[-1], ('It said: "Heads!"', 'It said: "Tails!"'))

    async def test_restart_reannounces_to_the_mind(self):
        await self.hear("open the terminal")
        self.hands_bus.sent.clear()
        await self.hands.on_event({"t": "organ.up", "name": "mind"})
        self.assertEqual([e["view"] for e in self.hands_bus.sent if e["app"] == "terminal"], ["shown"])

    async def test_other_apps_are_honestly_not_there_yet(self):
        await self.hear("show me how you're feeling")
        self.assertEqual(self.said[-1], "I don't have that app yet. I can open my terminal.")


if __name__ == "__main__":
    unittest.main()
