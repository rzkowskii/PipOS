"""Talking to the being by typing, from the local menu."""

import asyncio
import io
import json
import os
import pty
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mind import type_chat
from pathlib import Path
from mind.type_chat import Console, LineEditor, TypeChat, plain_lines


class FakeBus:
    def __init__(self, connected=True):
        self.sent = []
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self._queue = asyncio.Queue()

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, topic, **fields):
        self.sent.append({"t": topic, **fields})

    async def recv(self):
        return await self._queue.get()

    def topics(self):
        return [event["t"] for event in self.sent]


class Chat(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = FakeBus()
        self.shown = []
        self.chat = TypeChat(self.bus, name="Pip", owner="Richard",
                             show=lambda text, tone="text": self.shown.append((tone, text)))

    def ready(self, state="attending", **lid):
        self.chat.on_event({"t": "body.lid", "closed": False, "seq": 4, "snapshot": True, **lid})
        self.chat.on_event({"t": "state.changed", "state": state, "muted": False, "unwell": False})

    def texts(self):
        return [text for _, text in self.shown]

    async def test_an_attending_being_hears_the_line_as_addressed_speech_from_the_owner(self):
        self.ready("attending")
        self.assertTrue(await self.chat.say("  what time   is it  "))
        self.assertEqual(self.bus.topics(), ["hear.speech_start", "hear.final"])
        final = self.bus.sent[-1]
        self.assertEqual((final["text"], final["speaker"], final["lid_seq"]), ("what time is it", "Richard", 4))

    async def test_a_resting_being_is_woken_by_name_first_and_waits_to_attend(self):
        self.ready("idle")
        said = asyncio.create_task(self.chat.say("hello"))
        for _ in range(50):
            if self.bus.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.bus.topics(), ["hear.wake"], "no words before it attends")
        self.chat.on_event({"t": "state.changed", "state": "attending", "prev": "idle"})
        self.assertTrue(await said)
        self.assertEqual(self.bus.topics(), ["hear.wake", "hear.speech_start", "hear.final"])

    async def test_a_being_that_does_not_attend_gets_no_words(self):
        self.ready("drowsy")
        with patch.object(type_chat, "WAKE_WAIT", 0.1):
            self.assertFalse(await self.chat.say("hello"))
        self.assertEqual(self.bus.topics(), ["hear.wake"])
        self.assertIn("Pip did not answer to its name. It is drowsy.", self.texts())

    async def test_a_closed_or_unknown_lid_or_mute_is_respected_and_explained(self):
        self.ready("attending")
        self.chat.on_event({"t": "body.lid", "closed": True, "seq": 5})
        self.assertFalse(await self.chat.say("are you there"))
        self.assertIn("The lid is closed", self.texts()[-1])
        self.chat.on_event({"t": "body.lid", "closed": None, "seq": 6})
        self.assertFalse(await self.chat.say("are you there"))
        self.assertIn("can't tell whether the lid is open", self.texts()[-1])
        self.chat.on_event({"t": "body.lid", "closed": False, "seq": 7})
        self.chat.on_event({"t": "state.changed", "state": "attending", "muted": True})
        self.assertFalse(await self.chat.say("are you there"))
        self.assertIn("is muted", self.texts()[-1])
        self.assertEqual(self.bus.sent, [])

    async def test_an_unreachable_or_unknown_being_is_reported(self):
        chat = TypeChat(FakeBus(connected=False), name="Clover",
                        show=lambda text, tone="text": self.shown.append((tone, text)))
        self.assertFalse(await chat.say("hello"))
        self.assertIn("Clover can't be reached right now", self.texts()[-1])
        self.assertFalse(await self.chat.say("hello"))
        self.assertIn("Still connecting", self.texts()[-1])
        self.assertEqual(self.bus.sent, [])

    async def test_a_sleeping_being_is_only_woken_when_asked(self):
        self.ready("asleep")
        self.assertFalse(await self.chat.say("what time is it"))
        self.assertEqual(self.texts()[-1], 'Pip is asleep. Type "wake up" to wake Pip.')
        self.assertEqual(self.bus.sent, [])
        woke = asyncio.create_task(self.chat.say("Wake up!"))
        for _ in range(50):
            if self.bus.sent:
                break
            await asyncio.sleep(0.01)
        self.chat.on_event({"t": "state.changed", "state": "waking", "prev": "asleep"})
        self.chat.on_event({"t": "state.changed", "state": "attending", "prev": "waking"})
        self.assertTrue(await woke)
        self.assertEqual(self.bus.topics(), ["hear.wake"], "waking it says nothing to it")

    async def test_talking_over_a_reply_interrupts_it_first(self):
        self.ready("speaking")
        said = asyncio.create_task(self.chat.say("stop, a different question"))
        for _ in range(50):
            if self.bus.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.bus.topics(), ["key.interrupt"])
        self.chat.on_event({"t": "state.changed", "state": "attending", "prev": "speaking"})
        self.assertTrue(await said)
        self.assertEqual(self.bus.topics(), ["key.interrupt", "hear.speech_start", "hear.final"])

    async def test_empty_lines_are_not_sent(self):
        self.ready("attending")
        self.assertFalse(await self.chat.say("   "))
        self.assertEqual(self.bus.sent, [])

    async def test_states_and_the_reply_are_shown_as_they_happen(self):
        self.ready("attending")
        self.assertEqual(self.shown, [], "the state it was already in is not news")
        for state in ("listening", "thinking", "speaking"):
            self.chat.on_event({"t": "state.changed", "state": state})
        self.chat.on_event({"t": "say.request", "text": "It's 7:05", "seq": 1, "last": False})
        self.chat.on_event({"t": "say.request", "text": "in the evening.", "seq": 2, "last": False})
        self.chat.on_event({"t": "say.request", "text": "", "seq": 3, "last": True})
        self.chat.on_event({"t": "say.request", "text": "Next.", "seq": 1, "last": False})
        self.assertEqual(self.shown, [
            ("muted", "Pip is listening."), ("muted", "Pip is thinking."), ("muted", "Pip is speaking."),
            ("first_reply", "It's 7:05"), ("reply", "in the evening."), ("first_reply", "Next."),
        ])

    def model_reply(self, reply, *clauses):
        for seq, text in enumerate((*clauses, ""), 1):
            self.chat.on_event({"t": "say.request", "text": text, "seq": seq, "last": not text, "reply": reply})

    async def test_a_model_reply_is_shown_as_the_voice_plays_it(self):
        self.assertTrue({"say.played", "say.done"} <= set(type_chat.SUBS))
        self.ready("speaking")
        self.model_reply("a" * 16, "One.", "Two.", "Three.", "Four.")
        self.assertEqual(self.shown, [], "handed to the voice is not said")
        self.chat.on_event({"t": "say.played", "reply": "a" * 16, "seq": 1})
        self.chat.on_event({"t": "say.played", "reply": "a" * 16, "seq": 3})
        self.chat.on_event({"t": "say.played", "reply": "a" * 16, "seq": 2})
        # Typed over: the voice stops, and the fourth clause never played.
        self.chat.on_event({"t": "mind.cancel"})
        self.chat.on_event({"t": "say.played", "reply": "a" * 16, "final": True, "stopped": True})
        self.chat.on_event({"t": "say.played", "reply": "a" * 16, "seq": 4})
        self.chat.on_event({"t": "say.done", "final": True})
        self.assertEqual(self.shown, [("first_reply", "One."), ("reply", "Three.")],
                         "a skipped clause and one that never played are not shown")
        self.model_reply("b" * 16, "Next.")
        self.chat.on_event({"t": "say.played", "reply": "b" * 16, "seq": 1})
        self.assertEqual(self.shown[-1], ("first_reply", "Next."))

    async def test_a_voice_that_reports_no_playback_shows_the_reply_when_it_is_over(self):
        self.ready("speaking")
        self.model_reply("a" * 16, "One.", "Two.")
        self.chat.on_event({"t": "say.done", "final": True})
        self.assertEqual(self.shown, [("first_reply", "One."), ("reply", "Two.")])
        # No voice at all: shown a little after the reply was handed over.
        with patch.object(type_chat, "RECEIPT_WAIT", 0.01):
            self.model_reply("b" * 16, "Three.")
            self.chat.on_event({"t": "mind.reply_done", "spoke": True})
            await asyncio.sleep(0.05)
        self.assertEqual(self.shown[-1], ("first_reply", "Three."))

    async def test_a_voice_that_has_begun_speaking_is_not_second_guessed(self):
        self.ready("speaking")
        with patch.object(type_chat, "RECEIPT_WAIT", 0.01):
            self.model_reply("a" * 16, "Slow to play.")
            self.chat.on_event({"t": "say.started"})
            self.chat.on_event({"t": "mind.reply_done", "spoke": True})
            await asyncio.sleep(0.05)
        self.assertEqual(self.shown, [], "its receipts decide")

    async def test_once_the_voice_reports_only_what_it_played_is_shown(self):
        self.ready("speaking")
        self.model_reply("a" * 16, "One.")
        self.chat.on_event({"t": "say.played", "reply": "a" * 16, "seq": 1})
        self.chat.on_event({"t": "say.played", "reply": "a" * 16, "final": True, "stopped": False})
        with patch.object(type_chat, "RECEIPT_WAIT", 0.01):
            self.model_reply("b" * 16, "Never played.")
            self.chat.on_event({"t": "mind.reply_done", "spoke": True})
            self.chat.on_event({"t": "say.done", "final": True})
            await asyncio.sleep(0.05)
        self.assertEqual(self.texts(), ["One."])

    async def test_with_the_voice_gone_replies_show_as_they_are_sent(self):
        self.ready("speaking")
        self.model_reply("a" * 16, "Half said.")
        self.chat.on_event({"t": "organ.down", "name": "voice"})
        self.model_reply("b" * 16, "Only here.")
        self.assertEqual(self.texts(), [
            "Pip's voice has stopped, so it can't speak aloud. Pip's replies still show here.",
            "Only here.",
        ], "what the voice had not begun is dropped")

    async def test_a_closed_lid_drops_what_was_waiting(self):
        self.ready("speaking")
        self.model_reply("a" * 16, "Shut in.")
        self.chat.on_event({"t": "body.lid", "closed": True, "seq": 5})
        self.chat.on_event({"t": "say.done", "final": True})
        self.assertEqual(self.shown, [])

    async def test_a_reply_with_nothing_to_say_or_a_lost_one_is_explained(self):
        self.ready("thinking")
        self.chat.on_event({"t": "mind.reply_done", "spoke": False})
        self.chat.on_event({"t": "mind.cancel", "reason": "think_timeout"})
        self.assertEqual(self.texts(), ["Pip had nothing to say.", "Pip took too long to answer."])

    async def test_missed_spoken_words_are_shown_while_the_voice_says_sorry(self):
        self.assertIn("hear.missed", type_chat.SUBS)
        self.ready("attending")
        self.chat.on_event({"t": "hear.missed", "reason": "unclear"})
        self.assertEqual(self.shown, [("muted", "Pip didn't catch that. Say it again, or type it here.")])

    async def test_only_the_voice_going_away_is_reported_not_the_face_stopped_for_this_menu(self):
        self.ready("attending")
        # The face always stops while the menu is open, so the being reads unwell.
        self.chat.on_event({"t": "state.changed", "state": "attending", "muted": False, "unwell": True})
        self.chat.on_event({"t": "organ.down", "name": "face"})
        self.assertEqual(self.shown, [])
        self.chat.on_event({"t": "organ.down", "name": "voice"})
        self.chat.on_event({"t": "organ.down", "name": "voice"})
        self.chat.on_event({"t": "organ.up", "name": "voice"})
        self.chat.on_event({"t": "organ.up", "name": "voice"})
        self.assertEqual(self.texts(), [
            "Pip's voice has stopped, so it can't speak aloud. Pip's replies still show here.",
            "Pip's voice is back.",
        ])

    async def test_waking_that_cannot_finish_while_the_menu_is_open_is_explained(self):
        self.ready("asleep")
        with patch.object(type_chat, "ASLEEP_WAKE_WAIT", 0.1):
            woke = asyncio.create_task(self.chat.say("wake up"))
            for _ in range(50):
                if self.bus.sent:
                    break
                await asyncio.sleep(0.01)
            self.chat.on_event({"t": "state.changed", "state": "waking", "prev": "asleep"})
            self.assertFalse(await woke)
        self.assertEqual(self.bus.topics(), ["hear.wake"])
        self.assertIn("can't finish while this menu is open", self.texts()[-1])
        self.assertIn("choose Q to go back to Pip's face", self.texts()[-1])
        self.shown.clear()
        await self.chat.report_status(timeout=0.1)
        self.assertIn("still waking up", self.texts()[-1])

    async def test_status_is_reported_up_front(self):
        self.ready("asleep")
        await self.chat.report_status(timeout=0.1)
        self.assertEqual(self.texts(), ['Pip is asleep. Type "wake up" to wake Pip.'])
        self.shown.clear()
        self.chat.on_event({"t": "state.changed", "state": "idle"})
        await self.chat.report_status(timeout=0.1)
        self.assertEqual(self.shown, [])


class Typing(unittest.TestCase):
    def test_line_editor_sends_on_enter_and_leaves_on_escape(self):
        editor = LineEditor(limit=8)
        for char in "helo":
            self.assertIsNone(editor.feed(char))
        editor.feed("\x7f")
        for char in "lo there":
            editor.feed(char)
        self.assertEqual(editor.buffer, "hello th", "the line stops at its limit")
        self.assertEqual(editor.feed("\r"), "hello th")
        self.assertEqual(editor.buffer, "")
        editor.feed("x")
        editor.feed("\x15")
        self.assertEqual(editor.feed("\n"), "")
        editor.feed("\t")
        self.assertEqual(editor.buffer, "", "control characters are not typed")
        editor.feed("é")
        self.assertEqual(editor.buffer, "é")
        self.assertIs(editor.feed("\x1b"), LineEditor.LEAVE)
        self.assertIs(editor.feed("\x03"), LineEditor.LEAVE)

    def test_names_come_from_the_profile_and_are_safe_to_draw(self):
        from dataclasses import replace
        from mind.profile import Profile, save_profile

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        save_profile(directory, replace(Profile(), name="Clover", owner="Sam"))
        self.assertEqual(type_chat.profile_names(directory), ("Clover", "Sam"))
        self.assertEqual(type_chat.profile_names(directory / "missing"), ("Pip", "friend"))
        shown = []
        chat = TypeChat(FakeBus(), show=lambda text, tone="text": shown.append(text))
        chat.on_event({"t": "say.request", "text": "Hi\x1b]0;x\x07 there\n", "seq": 1, "last": True})
        self.assertEqual(shown, ["Hi ]0;x there"])

    def test_plain_lines_leave_on_escape_or_end_of_input(self):
        typed = iter(["hello", "go \x1b"])
        self.assertEqual(list(plain_lines(lambda prompt: next(typed))), ["hello", None])

        def ended(prompt):
            raise EOFError

        self.assertEqual(list(plain_lines(ended)), [None])

    def test_plain_console_prints_replies_under_the_beings_name(self):
        environment = {key: value for key, value in os.environ.items() if key != "BEING_CONSOLE_UI"}
        with patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()) as out:
            console = Console("Pip", styled=False)
            console.show("Hello.", "first_reply")
            console.show("How are you?", "reply")
            console.show("Pip is thinking.", "muted")
        self.assertEqual(out.getvalue(), "Pip: Hello.\n     How are you?\nPip is thinking.\n")

    def test_styled_console_keeps_the_line_being_typed(self):
        with patch.dict(os.environ, {"BEING_CONSOLE_UI": "1"}), redirect_stdout(io.StringIO()) as out:
            console = Console("Pip", styled=True)
            console.editing, console.buffer = True, "half a sen"
            console.show("It's 7:05.", "first_reply")
        text = out.getvalue()
        self.assertTrue(text.startswith("\r\x1b[K"))
        self.assertIn("It's 7:05.", type_chat.ui.plain(text))
        self.assertTrue(type_chat.ui.plain(text).endswith("› half a sen"))


class FakeStem:
    """A brainstem stand-in on a real Unix socket: it answers the first line it hears."""

    def __init__(self, path, state="attending"):
        self.path, self.state = path, state
        self.received = []
        self.heard = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.ready.wait(5)
        return self

    def __exit__(self, *_):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(asyncio.start_unix_server(self._serve, path=self.path))
        self.ready.set()
        self.loop.run_forever()
        self.server.close()
        for task in asyncio.all_tasks(self.loop):
            task.cancel()
        self.loop.run_until_complete(asyncio.sleep(0))
        self.loop.close()

    async def _serve(self, reader, writer):
        def send(**event):
            writer.write((json.dumps(event) + "\n").encode())

        while line := await reader.readline():
            event = json.loads(line)
            self.received.append(event)
            if event["t"] == "hello":
                send(t="body.lid", closed=False, seq=2, snapshot=True)
                send(t="state.changed", state=self.state, muted=False, unwell=False)
            elif event["t"] == "hear.final":
                send(t="state.changed", state="thinking", prev="attending")
                send(t="say.request", text=f"Hello {event['speaker']}.", seq=1, last=False)
                send(t="say.request", text="", seq=2, last=True)
                self.heard.set()
            await writer.drain()
        writer.close()


class OverTheBus(unittest.TestCase):
    def test_typed_line_reaches_the_bus_and_the_reply_is_shown(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="btc-", dir="/tmp"))
        path = os.path.join(directory, "bus.sock")
        environment = {key: value for key, value in os.environ.items() if key != "BEING_CONSOLE_UI"}
        with FakeStem(path) as stem, patch.dict(os.environ, environment, clear=True), \
                patch.object(type_chat, "profile_names", return_value=("Pip", "Richard")), \
                redirect_stdout(io.StringIO()) as out:
            typed = iter(["are you there", ""])
            prompts = []

            def read(prompt):
                prompts.append(prompt)
                if len(prompts) == 2:
                    # Leave only once the reply has had time to arrive.
                    stem.heard.wait(5)
                    for _ in range(100):
                        if "Hello Richard." in out.getvalue():
                            break
                        threading.Event().wait(0.02)
                return next(typed)

            self.assertEqual(type_chat.main(path=path, read=read), 0)
        hello = stem.received[0]
        self.assertEqual((hello["t"], hello["name"]), ("hello", "keyboard"))
        self.assertIn("say.request", hello["sub"])
        topics = [event["t"] for event in stem.received]
        self.assertEqual(topics[1:], ["hear.speech_start", "hear.final"])
        self.assertEqual(stem.received[-1]["speaker"], "Richard")
        self.assertEqual(stem.received[-1]["lid_seq"], 2)
        text = out.getvalue()
        self.assertIn("Talk to Pip by typing", text)
        self.assertIn("Pip is thinking.", text)
        self.assertIn("Pip: Hello Richard.", text)
        self.assertEqual(prompts, ["You: ", "You: "])

    def run_on_console(self, path, keys):
        """Run the chat in being-console's styled mode on a real pseudo-terminal."""
        controller, terminal = pty.openpty()
        self.addCleanup(os.close, controller)
        environment = {**os.environ, "BEING_CONSOLE_UI": "1", "COLUMNS": "70", "LINES": "30",
                       "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        script = ("from mind import type_chat; type_chat.profile_names = lambda *_: ('Pip', 'Richard'); "
                  f"raise SystemExit(type_chat.main(path={path!r}))")
        child = subprocess.Popen([sys.executable, "-c", script], stdin=terminal, stdout=terminal, stderr=terminal,
                                 env=environment, close_fds=True)
        os.close(terminal)
        output = b""

        def read_until(wanted, timeout=10):
            nonlocal output
            deadline = time.monotonic() + timeout
            while not wanted or wanted not in type_chat.ui.plain(output.decode(errors="replace")):
                late = time.monotonic() > deadline  # one reading: two could straddle the deadline
                if not wanted and late:
                    return
                if late:
                    child.kill()
                    self.fail(f"never saw {wanted!r} in {output!r}")
                import select
                if select.select([controller], [], [], 0.1)[0]:
                    try:
                        output += os.read(controller, 4096)
                    except OSError:
                        break

        for wanted, typed in keys:
            read_until(wanted)
            os.write(controller, typed)
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            read_until("", 0.1)  # keep the terminal drained while it closes
            time.sleep(0.05)
        if child.poll() is None:
            child.kill()
            self.fail(f"the chat did not go back: {output!r}")
        self.assertEqual(child.returncode, 0)
        return type_chat.ui.plain(output.decode(errors="replace"))

    def test_on_the_console_typing_is_echoed_and_enter_or_escape_goes_back(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="btc-", dir="/tmp"))
        path = os.path.join(directory, "bus.sock")
        with FakeStem(path) as stem:
            text = self.run_on_console(path, [("Talk to Pip", b"are you thi\x7fere\r"),
                                              ("Hello Richard.", b"\r")])
            self.assertEqual(stem.received[-1]["text"], "are you there")
        self.assertIn("You: are you there", text)
        self.assertIn("Pip: Hello Richard.", text)
        with FakeStem(path):
            text = self.run_on_console(path, [("Talk to Pip", b"half a line\x1b")])
        self.assertIn("› half a line", text)

    def test_without_a_brainstem_it_says_so_and_returns(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="btc-", dir="/tmp"))
        environment = {key: value for key, value in os.environ.items() if key != "BEING_CONSOLE_UI"}
        with patch.dict(os.environ, environment, clear=True), patch.object(type_chat, "CONNECT_WAIT", 0.2), \
                patch.object(type_chat, "profile_names", return_value=("Pip", "Richard")), \
                redirect_stdout(io.StringIO()) as out:
            typed = iter(["hello", ""])
            code = type_chat.main(path=os.path.join(directory, "none.sock"), read=lambda prompt: next(typed))
        self.assertEqual(code, 0)
        self.assertIn("Pip can't be reached right now", out.getvalue())


if __name__ == "__main__":
    unittest.main()
