"""The workshop wired into the mind (task L3.4): AppBridge's hooks, with a
stand-in hands organ on the bus."""

import asyncio
import json

from tests.test_mind import MindCase


class Hands:
    """Answers the mind's workshop requests the way the hands organ would."""

    def __init__(self, bus):
        self.bus = bus
        self.requests = []
        send = bus.send

        async def answering(topic, **fields):
            await send(topic, **fields)
            if topic.startswith(("app.", "term.")):
                self.requests.append({"t": topic, **fields})
                self.answer(topic, fields)

        bus.send = answering

    def answer(self, topic, fields):
        req = fields.get("req")
        if topic == "app.open":
            self.bus.push("app.state", req=req, app=fields.get("app", "terminal"), view="shown", job=None)
        elif topic == "app.restore":
            self.bus.push("app.state", req=req, app="terminal", view="shown", job=None)
        elif topic == "app.minimise":
            self.bus.push("app.state", req=req, app="terminal", view="minimised", job=None)
        elif topic == "app.close":
            self.bus.push("app.state", req=req, app="terminal", view="closed", job=None)
        elif topic == "term.task":
            self.bus.push("term.status", req=req, id=fields.get("id"), phase="writing", source="recipe",
                          describe="a countdown")
        elif topic == "term.forget":
            self.bus.push("term.status", req=req, id=fields.get("id"), phase="forgotten", changed=[])
        elif topic == "term.control":
            if fields.get("action") == "run":
                self.bus.push("term.status", req=req, id=fields.get("id"), phase="running", source="recipe")
            elif fields.get("action") == "pause":
                self.bus.push("term.status", req=req, id=fields.get("id"), phase="paused", source="model")


class Workshop(MindCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.hands = Hands(self.bus)
        self.bus.push("organ.up", name="hands")
        await asyncio.sleep(0)

    async def ask(self, text, speaker="Richard"):
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker=speaker)
        await self.bus.wait("mind.reply_done", replies + 1)
        self.bus.push("say.done", final=True)
        await asyncio.sleep(0)
        return " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])

    def actions(self):
        path = self.root / "log" / "actions.jsonl"
        return [json.loads(line)["kind"] for line in path.read_text().splitlines()] if path.exists() else []

    async def test_open_the_terminal_is_answered_in_code(self):
        self.assertEqual(await self.ask("Hey Pip, open the terminal"), "Here's my terminal.")
        self.assertEqual(self.cortex_server.chats, [], "no model call")
        self.assertEqual([r["t"] for r in self.hands.requests], ["app.open"])
        self.assertEqual(await self.ask("minimise the terminal"), "Okay.")
        self.assertEqual(await self.ask("close the terminal"), "Closed.")
        self.assertEqual(self.cortex_server.chats, [])
        self.assertEqual(self.actions(), ["workshop.open"])
        self.assertFalse(self.memory.contains("terminal"), "workshop commands are not archived")

    async def test_continue_the_story_with_no_job_reaches_the_model(self):
        await self.ask("continue the story")
        self.assertEqual(len(self.cortex_server.chats), 1)
        self.assertEqual(self.cortex_server.chats[0]["max_tokens"], 160, "a story gets the longer budget")
        self.assertEqual(self.hands.requests, [])
        await self.ask("stop")
        self.assertEqual(len(self.cortex_server.chats), 2, "a bare stop with nothing open is conversation")

    async def test_guests_are_refused_the_terminal(self):
        await self.ask("start guest mode")
        self.assertIn({"t": "app.privacy", "guest": True}, self.bus.of("app.privacy"))
        self.assertEqual(await self.ask("open the terminal"), "My terminal stays closed while a guest is here.")
        self.assertEqual([r["t"] for r in self.hands.requests], ["app.privacy"])
        self.assertEqual(self.cortex_server.chats, [])

    async def test_a_model_turn_parks_a_script_the_model_is_writing_and_is_told_about_it(self):
        self.bus.push("term.status", id="job1", phase="writing", source="model", describe="a spiral")
        await asyncio.sleep(0)
        await self.ask("I like spirals a lot")
        controls = [r for r in self.hands.requests if r["t"] == "term.control"]
        self.assertEqual([(c["action"], c["reason"]) for c in controls], [("pause", "turn"), ("resume", "turn")])
        content = self.cortex_server.chats[0]["messages"][-1]["content"]
        self.assertIn("You are writing a script in the terminal: a spiral.", content)

    async def test_a_finished_script_is_announced_and_remembered_once_and_can_be_forgotten(self):
        self.bus.push("term.status", id="job2", phase="waiting", source="recipe", file="countdown.py",
                      sha256="ab", speakable=True)
        self.bus.push("state.changed", state="idle")
        self.bus.push("stem.beat")
        token = (await self.bus.wait("mind.announce"))["announcement"]
        self.bus.push("state.changed", state="speaking")
        self.bus.push("stem.announce_ready", announcement=token)
        await self.bus.wait("mind.reply_done")
        self.assertIn("It's ready. Say run it.", " ".join(e["text"] for e in self.bus.of("say.request")))
        self.bus.push("say.done", final=True, announcement=token)
        await asyncio.sleep(0)
        self.assertEqual(self.mind._app_lines, [])
        self.assertTrue(self.memory.contains("I wrote countdown.py in the terminal"))
        self.bus.push("stem.beat")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.memory.recall_history("countdown", kind="said")), 1, "recorded once")

        # "Forget that script", confirmed: the one-line memory goes too.
        self.assertEqual(await self.ask("forget that script"), "Should I forget that script and every copy of it?")
        self.assertTrue(self.memory.contains("countdown.py"), "nothing goes before the yes")
        self.assertEqual(await self.ask("yes"), "Okay. That script is forgotten.")
        self.assertFalse(self.memory.contains("countdown.py"))
        self.assertIn("forget", self.actions())
        self.assertEqual(self.cortex_server.chats, [])

    async def test_running_a_program_is_logged(self):
        self.bus.push("term.status", id="job3", phase="waiting", source="recipe", file="box.py", sha256="cd")
        await asyncio.sleep(0)
        self.assertEqual(await self.ask("run it"), "Running it.")
        self.assertEqual(self.actions(), ["workshop.run"])
