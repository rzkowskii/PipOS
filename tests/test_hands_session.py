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
from unittest.mock import patch
from contextlib import closing
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mind import action_log, app_organizer, diary, hands as hands_module, lists
from mind.app_mind import RECENT, RouteHistory
from mind.dream_store import DreamStore
from mind.memory import Memory
from mind.retention import MemoryPolicy, save_policy
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
        self.mind_dir = base / "mind"
        self.mind_dir.mkdir()
        self.hands = Hands(self.hands_bus, cage=self.cage, cortex=CodeCortex(self.llama.url), workshop=self.workshop,
                           renderer=renderer, disabled=base / "apps.disabled", body_root=base / "root",
                           state_dir=self.mind_dir)
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

    async def test_disabling_workshop_stops_the_open_cage_and_preserves_files(self):
        await self.open_minimised()
        script = self.workshop / "keep.py"
        script.write_text("print('kept')\n")
        self.disabled.touch()
        await self.until(lambda: self.bridge.view["terminal"] == "closed", what="workshop closed")
        self.assertIsNone(self.hands.session)
        self.assertIn(("stop", "being-shell-1"), self.cage.calls)
        self.assertEqual(script.read_text(), "print('kept')\n")
        await self.hear("open the terminal")
        self.assertIn("switched off", self.said[-1])

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

    async def test_builtin_one_liner_runs_after_a_countdown(self):
        await self.open_minimised()
        with mock.patch.object(hands_module, "COUNTDOWN", 1):
            await self.hear("in the terminal, show me the files")
            await self.until(lambda: self.phase() == "countdown", what="countdown")
            self.assertTrue(self.screen().rstrip().endswith("ls -l"))
            await self.until(lambda: self.phase() == "done", what="listed")
        self.assertIn("total", self.screen())

    async def test_generated_one_liner_runs_only_after_run_it(self):
        await self.open_minimised()
        self.llama.replies.append(["touch approved.txt\n"])
        target = self.workshop / "approved.txt"
        with mock.patch.object(hands_module, "COUNTDOWN", 0):
            await self.hear("in the terminal, touch approved.txt")
            await self.until(lambda: self.phase() == "waiting" and "# touch approved.txt" in self.screen(),
                             what="command preview")
            await self.hands.tick()
        self.assertFalse(target.exists())
        self.assertIn("It's ready. Say run it.", await self.announced())
        await self.hear("run it")
        self.assertEqual(self.said[-1], "Running it.")
        await self.until(lambda: self.phase() == "done", what="command ran")
        self.assertTrue(target.is_file())

    async def test_enter_does_not_submit_a_generated_command_preview(self):
        await self.open_minimised()
        self.llama.replies.append(["touch approved.txt\n"])
        await self.hear("in the terminal, touch approved.txt")
        await self.until(lambda: self.phase() == "waiting", what="command preview")
        await self.hear("show me the terminal")
        self.hands.on_key(b"\r")
        await self.until(lambda: self.phase() == "cancelled", what="preview cancelled")
        self.hands.on_key(b"\r")
        await self.hear("run it")
        self.assertEqual(self.said[-1], "There's nothing ready to run.")
        self.assertFalse((self.workshop / "approved.txt").exists())

    async def test_cursor_move_cannot_change_the_checked_one_liner(self):
        await self.hear("open the terminal")
        await self.until(lambda: "pip:~$" in self.screen(), what="prompt")
        self.llama.replies.append(["echo checked > proof.txt\n"])
        with mock.patch.object(hands_module, "HUMAN_QUIET", 0.1):
            await self.hear("in the terminal, echo checked to proof.txt")
            await self.until(lambda: self.hands._typist.current_line().startswith("# ech"),
                             what="comment being typed")
            self.hands.on_key(b"\x01")  # Ctrl-A moves the shell cursor inside the preview.
            await self.until(lambda: self.phase() == "waiting", what="command preview")
        self.assertEqual(list(self.workshop.iterdir()), [])
        await self.hear("run it")
        await self.until(lambda: self.phase() == "done", what="command ran")
        self.assertEqual([file.name for file in self.workshop.iterdir()], ["proof.txt"])
        self.assertEqual((self.workshop / "proof.txt").read_text(), "checked\n")

    async def test_new_job_replaces_the_pending_one_liner(self):
        await self.open_minimised()
        self.llama.replies.append(["touch approved.txt\n"])
        await self.hear("in the terminal, touch approved.txt")
        await self.until(lambda: self.phase() == "waiting", what="command preview")
        with mock.patch.object(hands_module, "COUNTDOWN", 0):
            await self.hear("in the terminal, show me the files")
            await self.until(lambda: self.phase() == "done", what="built-in command ran")
        self.assertIn("total", self.screen())
        self.assertFalse((self.workshop / "approved.txt").exists())

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

    async def test_a_wake_word_while_reading_the_prompt_never_leaves_typing_held(self):
        # "Pip ..." heard during the long prompt read: the hold must outlast
        # the move to writing and be let go when the hold time is up.
        self.llama.delay = 0.05
        self.llama.replies.append([f"print({n})\n" for n in range(6)] + ["```"])
        await self.open_minimised()
        with mock.patch.object(hands_module, "HOLD_SECONDS", 1.0):
            started = asyncio.Event()
            real = self.hands.cortex.prompt_tokens

            async def slow_prompt(*args, **kwargs):
                started.set()
                await asyncio.sleep(0.4)  # the prompt read
                return await real(*args, **kwargs)
            with mock.patch.object(self.hands.cortex, "prompt_tokens", slow_prompt):
                await self.hear("write a script that prints a few, write it yourself")
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual(self.phase(), "reading")
                self.hands.urgent({"t": "hear.wake"})
                self.assertTrue(self.hands._typist.held)
                await self.until(lambda: self.hands.job.tokens, what="the first token")
                self.assertIn(self.phase(), ("held", "waiting", "checking"))
                await self.until(lambda: self.phase() == "waiting", what="typed after the hold")
        self.assertTrue(any(self.workshop.glob("*.py")))

    async def test_the_release_lets_go_even_after_the_phase_moved_on(self):
        await self.open_minimised()
        self.llama.delay = 0.05
        self.llama.replies.append([f"print({n})\n" for n in range(40)] + ["```"])
        await self.hear("write a script that prints many, write it yourself")
        await self.until(lambda: self.phase() == "writing", what="writing")
        self.hands._typist.hold()  # held, but the phase says writing
        await self.hands.control("release", "quiet")
        self.assertFalse(self.hands._typist.held)

    async def test_forgetting_a_job_mid_write_answers_forgotten_and_redacts(self):
        self.llama.delay = 0.1
        self.llama.replies.append([f"print({n})\n" for n in range(40)] + ["```"])
        await self.open_minimised()
        await self.hear("write a script that prints lots, write it yourself")
        await self.until(lambda: self.phase() == "writing", what="writing")
        await self.hear("clear the workshop")
        await self.hear("yes")
        # The cancelled job's status is not the answer: "forgotten" is.
        self.assertEqual(self.said[-1], "Okay. My workshop is empty.")
        self.assertIsNone(self.hands.job)

    async def test_a_late_forgotten_still_redacts_the_episode(self):
        await self.open_minimised()
        await self.hear("write a script that draws a tree")
        await self.until(lambda: self.phase() == "waiting", what="written")
        line, meta = self.bridge.episode()
        self.bridge.note_episode("ep-1", meta)
        real = self.hands.forget

        async def slow_forget(event):
            await asyncio.sleep(0.5)
            await real(event)
        with mock.patch.object(self.hands, "forget", slow_forget), \
                mock.patch("mind.app_commands.FORGET_WAIT", 0.1):
            await self.hear("forget that script")
            await self.hear("yes")
            self.assertEqual(self.said[-1], "I couldn't reach my hands to forget it.")
            self.assertEqual(self.bridge.forgotten_episodes(), [])
            await self.until(lambda: not (self.workshop / "tree.py").exists(), what="forgotten late")
            await self.until(lambda: self.bridge.status is None, what="the late answer")
        self.assertEqual(self.bridge.forgotten_episodes(), ["ep-1"])

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
        self.llama.replies.append(["import time\n", "while True:\n", "    try:\n",
                                   "        print('interrupt handler ready', flush=True)\n", "        time.sleep(60)\n",
                                   "    except KeyboardInterrupt:\n", "        print('ignored')\n", "```"])
        await self.open_minimised()
        with mock.patch.object(hands_module, "KILL_GRACE", 0.5):
            await self.hear("write a script that never stops, write it yourself")
            await self.until(lambda: self.phase() == "waiting", what="written")
            await self.hear("run it")
            await self.until(lambda: "\ninterrupt handler ready\n" in self.screen().replace("\r", ""),
                             what="the running program can catch Ctrl-C")
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
        self.assertIsNone(self.bridge.status)
        # The answer comes as soon as the files are gone; the fresh shell,
        # the wipe and the slot erase follow.
        await self.until(lambda: self.llama.erased == erased + 1, what="the slot erase")
        self.assertEqual(self.renderers[-1].wiped, 1)
        self.assertNotIn(b"tree", bytes(self.hands.transcript.data))
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

    # -- the shelf ----------------------------------------------------------------------

    async def test_play_snake_installs_it_first_and_shows_the_terminal(self):
        await self.open_minimised()
        await self.hear("play snake")
        self.assertEqual(self.said[-1], "Starting snake.")
        self.assertEqual(self.hands.view["terminal"], "shown", "a game needs its player")
        self.assertEqual(self.hands.job.line, "install snake && run snake")
        await self.until(lambda: self.phase() == "running", what="the game")
        await self.until(lambda: "No internet was used" in self.screen(), what="installed from the shelf")
        self.assertTrue((self.workshop / "apps/snake/main.py").is_file())
        await self.until(lambda: "Q to quit" in self.screen(), what="the board")
        self.hands.on_key(b"q")
        await self.until(lambda: self.phase() == "done", what="game over")
        self.renderers[-1].wipe()
        await self.hear("play snake")
        self.assertEqual(self.hands.job.line, "run snake", "installed now: it just runs")
        await self.until(lambda: "Q to quit" in self.screen(), what="the board again")
        self.hands.on_key(b"q")
        await self.until(lambda: self.phase() == "done", what="game over again")

    async def test_the_shelf_listing_and_guests(self):
        await self.hear("what can you install")
        listed = self.said[-1]
        self.assertTrue(listed.startswith("I can't download anything, but my shelf has "), listed)
        for name in ("dice", "guess", "hangman", "life", "matrix", "snake", "stopwatch"):
            self.assertIn(name, listed)
        self.assertTrue(listed.endswith("Say install and a name."))
        await self.bridge.privacy(True)
        for text in ("what can you install", "install snake", "play snake", "run life"):
            await self.hear(text)
            self.assertEqual(self.said[-1], "My terminal stays closed while a guest is here.", text)
        await self.hear("show me the clock")
        self.assertEqual(self.said[-1], "My clock stays closed while a guest is here. It shows your reminders.")
        await self.hear("open the monitor")
        self.assertEqual(self.said[-1], "My monitor stays closed while a guest is here.")
        self.assertEqual((self.hands.view["clock"], self.hands.view["monitor"]), ("closed", "closed"))
        # hands refuses on its own too (a guest change the mind has not seen yet).
        self.assertEqual(self.bridge._refused_line({"reason": "guest", "app": "clock"}),
                         "My clock stays closed while a guest is here. It shows your reminders.")
        self.assertEqual(self.hands.view["terminal"], "closed")
        self.assertFalse((self.workshop / "apps").exists())

    # -- the body monitor and the clock ---------------------------------------------

    def app_renderer(self, app):
        return [made for made in self.renderers if made.app == app][-1]

    def text_of(self, app):
        return self.app_renderer(app).data.decode("utf-8", "replace")

    async def test_organizer_voice_focus_keys_live_updates_and_guest_wipe(self):
        self.hands.organizer_path = Path(self.dir.name) / "o.sock"
        server = await app_organizer.start_server(
            self.hands.organizer_path, self.mind_dir, lambda: not self.bridge.guest)
        self.assertEqual(self.hands.organizer_path.stat().st_mode & 0o777, 0o600)

        async def close_server():
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(close_server)
        store = lists.Lists(self.mind_dir)
        store.add("shopping", [f"Private groceries {i:02}" for i in range(1, 20)])
        store.add("to-do", ["Private errands", "One item\x1b[2J\r\nWith a pasted line"])
        memory = Memory(self.mind_dir)
        memory.remember("Private owner fact", speaker="Richard")
        memory.close()
        await self.hear("open the terminal")
        terminal = self.app_renderer("terminal")
        with mock.patch.object(hands_module, "APP_FRAME", 0.05):
            await self.hear("open my lists")
            lists_panel = self.app_renderer("lists")
            self.assertEqual(self.hands.view["terminal"], "minimised")
            self.assertFalse(terminal.visible)
            self.assertEqual(lists_panel.flag_log[0]["focus"], 1)
            first = self.text_of("lists")
            self.assertIn("Private groceries 01", first)
            self.assertNotIn("Private groceries 17", first)
            await asyncio.sleep(0.1)
            self.assertEqual(self.text_of("lists"), first, "idle refresh does not redraw")
            self.hands.on_key(b"\0pgdn")
            await self.until(lambda: "Private groceries 17" in self.text_of("lists"), what="second page")
            self.hands.on_key(b"\x1b[C")
            await self.until(lambda: "Private errands" in self.text_of("lists"), what="next list")
            self.assertIn("One item [2J With a pasted line", self.text_of("lists"))
            self.assertNotIn("One item\x1b", self.text_of("lists"))
            store.add("to-do", ["A new errand"])
            await self.until(lambda: "A new errand" in self.text_of("lists"), what="read current data")
            self.assertEqual(len(store.items("to-do")), 3)
        await self.hear("open the memory")
        memory_panel = self.app_renderer("memory")
        self.assertIn("Private owner fact", self.text_of("memory"))
        self.assertEqual((lists_panel.wiped, lists_panel.data, lists_panel.visible), (1, b"", False))
        self.assertEqual(memory_panel.flag_log[0]["focus"], 1)
        owner_words = ("Private groceries", "Private errands", "Private owner fact")
        for event in self.hands_bus.sent + self.mind_bus.sent:
            self.assertFalse(any(words in str(event) for words in owner_words), event)
        # The urgent path blanks the panel before any queued terminal work finishes.
        self.hands.urgent({"t": "app.privacy", "guest": True})
        self.assertEqual((memory_panel.wiped, memory_panel.data), (1, b""))
        self.assertEqual(self.hands._refusal("lists"), "guest")
        await self.bridge.privacy(True)
        await self.until(lambda: self.bridge.view["memory"] == "closed", what="owner facts hidden")
        self.assertEqual((memory_panel.data, memory_panel.visible), (b"", False))
        for app in ("lists", "memory"):
            self.assertEqual((await self.hear(f"open the {app}")).kind, "say")
            await self.hands.on_event({"t": "app.open", "app": app, "req": "direct"})
            self.assertEqual(self.hands_bus.sent[-1]["reason"], "guest")
            self.assertEqual((await app_organizer.fetch(self.hands.organizer_path, app, 0, 0))["status"],
                             "private")

    async def test_session_only_wipes_open_lists_and_refuses_both_panels(self):
        self.hands.organizer_path = Path(self.dir.name) / "o.sock"
        server = await app_organizer.start_server(self.hands.organizer_path, self.mind_dir, lambda: True)

        async def close_server():
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(close_server)
        lists.Lists(self.mind_dir).add("private", ["Existing owner item"])
        with mock.patch.object(hands_module, "APP_FRAME", 0.05):
            await self.hear("open the lists")
            renderer = self.app_renderer("lists")
            self.assertIn("Existing owner item", self.text_of("lists"))
            save_policy(self.mind_dir, MemoryPolicy("session-only"))
            await self.until(lambda: self.bridge.view["lists"] == "closed", what="retention gate")
            self.assertEqual((renderer.wiped, renderer.data), (1, b""))
        for app in ("lists", "memory", "journal", "mind"):
            await self.hear(f"open the {app}")
            self.assertIn("session only", self.said[-1])
            self.assertEqual(self.bridge.view[app], "closed")
            self.assertEqual((await app_organizer.fetch(self.hands.organizer_path, app, 0, 0))["status"],
                             "private")

    async def test_journal_reads_three_real_sources_and_wipes_the_owner_quote(self):
        self.hands.organizer_path = Path(self.dir.name) / "o.sock"
        server = await app_organizer.start_server(
            self.hands.organizer_path, self.mind_dir, lambda: not self.bridge.guest, owner="Richard")

        async def close_server():
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(close_server)
        with (closing(Memory(self.mind_dir)) as memory,
              patch.object(memory, "disk_state", return_value={"state": "ok"})):
            # Seed the owner's and guest's archived speech even if the host
            # filesystem is below the appliance's conversation threshold.
            self.assertIsNotNone(memory.log("heard", "I moved the orchids to the study.", "Richard"))
            self.assertIsNotNone(memory.log("heard", "This guest likes purple onions.", "Guest"))
            dreams = DreamStore(memory)
            self.assertTrue(dreams.publish(dreams.stage("Richard")))
            self.assertTrue(dreams.publish(dreams.stage("Guest")))
            today = date.today()
            diary.build_day(memory, "Richard", today, state_dir=self.mind_dir)
            diary.build_day(memory, "Guest", today, state_dir=self.mind_dir)
        action_log.record(self.mind_dir, "timer.set", 2)
        await self.hear("open the journal")
        renderer = self.app_renderer("journal")
        self.assertEqual(renderer.flag_log[0]["focus"], 1)
        self.assertIn("Day record", self.text_of("journal"))
        self.assertIn("we talked once", self.text_of("journal"))
        self.hands.on_key(b"\x1b[C")
        await self.until(lambda: "I moved the orchids to the study." in self.text_of("journal"), what="owner dream")
        self.assertNotIn("purple onions", self.text_of("journal"))
        self.hands.on_key(b"\x1b[C")
        await self.until(lambda: "set two timers" in self.text_of("journal"), what="activity page")
        self.hands.on_key(b"\x1b[D")
        await self.until(lambda: "Dream journal" in self.text_of("journal").rsplit("\x1b[2J", 1)[-1],
                         what="return to dream page")
        self.hands.urgent({"t": "app.privacy", "guest": True})
        self.assertEqual(renderer.data, b"")
        await self.bridge.privacy(True)
        await self.until(lambda: self.bridge.view["journal"] == "closed", what="journal closed in guest mode")
        self.assertEqual((renderer.alive(), renderer.data), (False, b""))
        await self.hear("open the dream journal")
        self.assertIn("stays private", self.said[-1])

    async def test_mind_reads_live_counts_and_ids_and_has_no_saved_history(self):
        self.hands.organizer_path = Path(self.dir.name) / "o.sock"
        history = RouteHistory()
        server = await app_organizer.start_server(
            self.hands.organizer_path, self.mind_dir, lambda: not self.bridge.guest,
            routes=history.snapshot)

        async def close_server():
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(close_server)
        for i in range(RECENT + 1):
            history.add("code", f"command.{i}")
        history.add("code", "Private utterance!")
        with mock.patch.object(hands_module, "APP_FRAME", 0.05):
            await self.hear("open the mind")
            renderer = self.app_renderer("mind")
            self.assertIn("Routing illustration", self.text_of("mind"))
            self.assertIn(f"Code  {RECENT + 1}", self.text_of("mind"))
            self.assertIn(f"command.{RECENT}", self.text_of("mind"))
            self.assertNotIn("command.0", self.text_of("mind"))
            self.assertNotIn("Private utterance!", self.text_of("mind"))
            history.add("model", "model")
            await self.until(lambda: "Model  1" in self.text_of("mind"), what="live route counts")
            self.assertNotIn("command.", str(self.mind_bus.sent))
            self.assertNotIn("command.", str(self.hands_bus.sent))
            self.assertEqual(list(self.mind_dir.iterdir()), [])
        await self.hands.on_escape()
        self.assertEqual((renderer.visible, renderer.data), (False, b""))
        self.assertEqual(RouteHistory().snapshot()["rows"], [])

    async def test_calculator_owns_focused_keys_and_only_redraws_on_changes(self):
        await self.hear("open the terminal")
        terminal = self.app_renderer("terminal")
        with mock.patch.object(hands_module, "APP_FRAME", 0.02):
            await self.hear("open the calculator")
            calculator = self.app_renderer("calc")
            self.assertEqual(self.said[-1], "Here's my calculator. Type a sum or ask me one.")
            self.assertEqual(self.hands.view["terminal"], "minimised")
            self.assertFalse(terminal.visible)
            self.assertEqual(calculator.flag_log[0]["focus"], 1)
            for key in b"(2+3)*59\x7f\r":
                self.hands.on_key(bytes((key,)))
            screen = self.text_of("calc")
            self.assertIn("(2+3)*5", screen)
            self.assertIn("25", screen)
            self.assertNotIn("(2+3)", self.text_of("terminal"))
            await asyncio.sleep(0.08)
            self.assertEqual(self.text_of("calc"), screen)
            await self.hands.on_event({"t": "state.changed", "state": "idle"})
            await self.hands.on_escape()
            self.assertFalse(calculator.visible)
            self.assertEqual((calculator.wiped, self.bridge.view["calc"]), (1, "closed"))

    async def test_calculator_drops_displays_when_lid_is_closed_or_guest_active(self):
        await self.hear("show me the calculator")
        renderer = self.app_renderer("calc")
        await self.hands.on_event({"t": "app.calc_display", "expression": "Two plus two", "result": "4", "error": False})
        self.assertIn("Two plus two", self.text_of("calc"))
        await self.hands.on_event({"t": "app.calc_display", "expression": "Bad\x1b[H", "result": "8", "error": False})
        self.assertNotIn("Bad", self.text_of("calc"))
        await self.hands.on_lid(True)
        before = self.text_of("calc")
        await self.hands.on_event({"t": "app.calc_display", "expression": "Four minus two", "result": "2", "error": False})
        self.assertEqual(self.text_of("calc"), before)
        await self.hands.on_lid(False)
        await self.bridge.privacy(True)
        await self.until(lambda: self.bridge.view["calc"] == "closed", what="calculator closed for a guest")
        self.assertEqual((renderer.wiped, renderer.alive(), renderer.data), (1, False, b""))
        await self.hear("open the calculator")
        self.assertIn("stays closed in guest mode", self.said[-1])

    async def test_failed_calculator_open_preserves_the_shown_terminal(self):
        await self.hear("open the terminal")
        terminal = self.app_renderer("terminal")
        with mock.patch.object(RecordingRenderer, "ready", return_value=False):
            await self.hear("open the calculator")
        self.assertEqual(self.said[-1], "My calculator didn't open.")
        self.assertFalse(self.app_renderer("calc").alive())
        self.assertEqual(self.hands.view["calc"], "closed")
        self.assertTrue(terminal.visible)
        with mock.patch.object(terminal, "hide", return_value=False):
            await self.hear("open the calculator")
        self.assertEqual(self.said[-1], "My calculator didn't open.")
        self.assertFalse(self.app_renderer("calc").alive())
        self.assertEqual(self.hands.view["terminal"], "shown")

    async def test_the_monitor_opens_over_the_terminal_and_the_clock_replaces_it(self):
        await self.hear("open the terminal")
        terminal = self.app_renderer("terminal")
        with mock.patch.object(hands_module, "APP_FRAME", 0.05):
            await self.hear("Hey Pip, show me how you're feeling")
            self.assertEqual(self.said[-1], "Here's how I'm doing.")
            self.assertEqual((self.hands.view["monitor"], self.hands.view["terminal"]), ("shown", "minimised"))
            self.assertEqual((self.bridge.view["monitor"], self.bridge.view["terminal"]), ("shown", "minimised"))
            # The terminal's panel is really hidden (SIGUSR2), not only flagged.
            self.assertEqual(terminal.shown_log[-1], False)
            monitor = self.app_renderer("monitor")
            self.assertEqual(monitor.flag_log[0]["title"], "How I'm feeling")
            self.assertIn("Thinking", self.text_of("monitor"))
            self.assertEqual(monitor.flag_log[0]["focus"], 1, "Tab switches the monitor's tabs")
            size = len(monitor.data)
            await self.until(lambda: len(monitor.data) > 2 * size, what="a frame a second")
            source = self.hands._app_sources["monitor"][0]
            with mock.patch.object(source.system, "units", return_value={}):
                self.hands.on_key(b"\t")
                await self.until(lambda: "Automatic restarts" in self.text_of("monitor"), what="the processes tab")
                body_frames = self.text_of("monitor").count("Thinking")
                self.hands.on_key(b"\t")
                await self.until(lambda: self.text_of("monitor").count("Thinking") > body_frames, what="the body tab")
            await self.hear("open the monitor")
            self.assertEqual(self.said[-1], "It's already open.")
            await self.hear("show me the clock")
            self.assertEqual(self.said[-1], "Here's my clock.")
            self.assertEqual((self.hands.view["monitor"], self.hands.view["clock"]), ("closed", "shown"))
            self.assertEqual((monitor.started, monitor.visible, monitor.wiped), (False, False, 1))
            self.assertEqual(self.app_renderer("clock").flag_log[0]["title"], "Pip's clock")
            self.assertIn("█", self.text_of("clock"))
            await self.hear("show me your face")
            self.assertEqual(self.said[-1], "Okay.")
            self.assertEqual((self.hands.view["clock"], self.bridge.view["clock"]), ("closed", "closed"))
            self.assertEqual(self.hands.view["terminal"], "minimised", "the terminal keeps running")
            await self.hear("bring back the terminal")
            self.assertEqual(self.said[-1], "Here it is.")
            self.assertEqual(terminal.shown_log[-1], True)
            # The terminal over a small app: the small app goes.
            await self.hear("show my timers")
            self.assertEqual(self.hands.view["terminal"], "minimised")
            await self.hear("open the terminal")
            self.assertEqual(self.said[-1], "Here it is.")
            self.assertEqual((self.hands.view["terminal"], self.hands.view["clock"]), ("shown", "closed"))
        self.assertLessEqual(self.hands.frame_ms["monitor"], 5.0)

    async def test_the_clock_shows_timers_and_reminders_and_never_writes(self):
        from mind.reminders import Reminders
        now = time.time()
        store = Reminders(self.mind_dir)
        store.add("Timer", now + 250, kind="timer", duration=600, now=now - 350)
        store.add("call Mum", now + 3 * 3600, now=now)
        store.close()
        database = self.mind_dir / "reminders.db"
        before = (database.stat().st_mtime_ns, database.read_bytes())
        await self.hear("show my timers")
        self.assertEqual(self.said[-1], "Here's my clock.")
        screen = self.text_of("clock")
        self.assertIn("10-minute timer", screen)
        self.assertIn("4:1", screen)
        self.assertIn("call Mum", screen)
        self.assertEqual((database.stat().st_mtime_ns, database.read_bytes()), before)
        await self.hear("close the clock")
        self.assertEqual(self.said[-1], "Closed.")
        await self.hear("close the clock")
        self.assertEqual(self.said[-1], "My clock isn't open.")

    async def test_nothing_is_drawn_while_the_lid_is_closed(self):
        with mock.patch.object(hands_module, "APP_FRAME", 0.05):
            await self.hear("open the monitor")
            monitor = self.app_renderer("monitor")
            await self.hands.on_event({"t": "body.lid", "closed": True, "seq": 2})
            await asyncio.sleep(0.1)
            size = len(monitor.data)
            await asyncio.sleep(0.3)
            self.assertEqual(len(monitor.data), size)
            await self.hands.on_event({"t": "body.lid", "closed": False, "seq": 3})
            await self.until(lambda: len(monitor.data) > size, what="drawing again")

    async def test_the_monitor_reads_the_body(self):
        await self.hands.on_event({"t": "body.state", "battery": 64, "charging": False, "on_ac": False,
                                   "minutes_left": 190, "health_pct": 81, "cycles": 71, "temp": 52.5})
        self.hands.code_tok_s = 8.4
        await self.hear("show me how you're feeling")
        screen = self.text_of("monitor")
        for words in ("64 %", "about 3 h 10 min left", "holds 81 % of what it did when new", "71 charge cycles",
                      "52 °C", "writes 8.4 tokens a second"):
            self.assertIn(words, screen)

    async def test_a_renderer_that_ends_closes_its_app(self):
        # The renderer's unit has a run-time limit, and may die: the app is
        # then closed for real, and the mind hears it, so asking again opens
        # a fresh one instead of "It's already open."
        with mock.patch.object(hands_module, "APP_FRAME", 0.05):
            await self.hear("show me the clock")
            first = self.app_renderer("clock")
            first.started = False
            await self.until(lambda: self.hands.view["clock"] == "closed", what="closed after its renderer ended")
            await self.until(lambda: self.bridge.view["clock"] == "closed", what="the mind told")
            self.assertNotIn("clock", self.hands._app_sources)
            await self.hear("show me the clock")
            self.assertEqual(self.said[-1], "Here's my clock.")
            self.assertIsNot(self.app_renderer("clock"), first)

    async def test_guests_and_escape_close_the_small_apps(self):
        await self.hear("open the monitor")
        await self.hands.on_event({"t": "key.interrupt"})
        self.assertEqual(self.hands.view["monitor"], "closed", "Esc closes it when nothing else is going on")
        await self.hear("show me the clock")
        await self.bridge.privacy(True)
        await self.until(lambda: self.hands.view["clock"] == "closed", what="closed for the guest")
        self.assertEqual(self.app_renderer("clock").wiped, 1)
        await self.hear("show me the clock")
        self.assertEqual(self.said[-1], "My clock stays closed while a guest is here. It shows your reminders.")


if __name__ == "__main__":
    unittest.main()
