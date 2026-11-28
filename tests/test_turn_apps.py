"""The workshop wired into the mind (task L3.4): AppBridge's hooks, with a
stand-in hands organ on the bus."""

import asyncio
import json

from mind import actions
from tests.test_mind import MindCase
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


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
            self.bus.push("app.state", req=req, app=fields.get("app", "terminal"), view="closed", job=None)
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

    async def test_calculator_opens_in_code_and_only_projects_sums_while_available(self):
        self.assertIn("forty-two", await self.ask("What is six times seven?"))
        self.assertEqual(self.bus.of("app.calc_display"), [])
        self.assertIn("Here's my calculator", await self.ask("Open the calculator."))
        self.assertEqual(self.bus.of("app.open")[-1]["app"], "calc")
        self.assertIn("twenty-one", await self.ask("What is seven times three?"))
        self.assertEqual(self.bus.of("app.calc_display"), [
            {"t": "app.calc_display", "expression": "seven times three", "result": "21", "error": False}
        ])
        await self.ask("Start guest mode.")
        self.assertIn("stays closed in guest mode", await self.ask("Open the calculator."))
        self.assertIn("nine", await self.ask("What is one plus eight?"))
        self.assertEqual(len(self.bus.of("app.calc_display")), 1)
        self.assertEqual(self.cortex_server.chats, [])
        self.assertFalse(self.memory.contains("seven times three"))

    async def test_organizer_open_and_close_are_code_and_guest_cannot_open(self):
        help_text = await self.ask("What can I say about screen apps?")
        self.assertIn("Open the lists", help_text)
        self.assertIn("Open the memory", help_text)
        self.assertIn("Open the journal", help_text)
        self.assertIn("Open the mind", help_text)
        for app in ("lists", "memory", "journal", "mind"):
            await self.ask(f"Open the {app}.")
            self.assertEqual(self.bus.of("app.open")[-1]["app"], app)
            self.assertEqual(self.bus.of("mind.route")[-1]["via"], "code")
            self.assertEqual(await self.ask(f"Close the {app}."), "Closed.")
            self.assertEqual(self.bus.of("app.close")[-1]["app"], app)
        await self.ask("Start guest mode.")
        opens = self.bus.of("app.open")
        for app in ("lists", "memory", "journal", "mind"):
            self.assertIn("guest mode", await self.ask(f"Open the {app}."))
        self.assertEqual(self.bus.of("app.open"), opens)
        self.assertEqual(self.cortex_server.chats, [])

    async def test_voice_help_examples_route_through_the_mind_and_respect_guest_mode(self):
        summary = await self.ask("Hey Pip, what can I say?")
        self.assertIn("Make your eyes blue", summary)
        self.assertIn("Open the calculator", summary)
        self.assertEqual(self.bus.of("mind.route")[-1], {"t": "mind.route", "via": "code"})
        appearance = await self.ask("What can I say about appearance?")
        for field in actions.LOOK_FIELDS.values():
            self.assertIn(field.phrases[0], appearance)
            for phrase in field.phrases:
                self.assertIn("saved", await self.ask(phrase))
        self.assertIn("current look", await self.ask("What can I say about eyes?"))
        self.assertIn("calculator", await self.ask("What can I say about screen apps?"))
        await self.ask("Start guest mode")
        previous_routes = self.bus.of("mind.route")
        self.assertNotIn("Make your eyes", await self.ask("What can I say about appearance?"))
        guest_math = await self.ask("What can I say about the calculator?")
        self.assertIn("six times seven", guest_math)
        self.assertNotIn("Open", guest_math)
        self.assertEqual(self.bus.of("mind.route"), previous_routes)
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
        await asyncio.wait_for(asyncio.shield(self.mind._turn), 3)
        self.assertFalse(self.memory.contains("countdown.py"))
        self.assertIn("forget", self.actions())
        self.assertEqual(self.cortex_server.chats, [])

    async def test_running_a_program_is_logged(self):
        self.bus.push("term.status", id="job3", phase="waiting", source="recipe", file="box.py", sha256="cd")
        await asyncio.sleep(0)
        self.assertEqual(await self.ask("run it"), "Running it.")
        self.assertEqual(self.actions(), ["workshop.run"])
