"""The spoken app commands: matched in code, with context rules and fixed,
honest replies (mind/app_commands.py)."""

import asyncio
import unittest
from unittest import mock

from mind import app_commands
from mind.app_commands import (
    REPLIES, AppBridge, AppContext, load_shelf, match_recipe, may_be_app_command, normalise, parse, read_line,
    report, spoken_numbers, status_line,
)
from mind.commands import parse as memory_parse
from mind.hands import RECIPES

SHELF = load_shelf()


def ctx(**fields):
    return AppContext(shelf=SHELF, **fields)


CLOSED = ctx()
SHOWN = ctx(terminal="shown")
MINIMISED = ctx(terminal="minimised")
WRITING = ctx(terminal="shown", job="writing", job_source="model")
RUNNING = ctx(terminal="shown", job="running")
WAITING = ctx(terminal="shown", job="waiting")
CANCELLED = ctx(terminal="shown", job="cancelled")


class NormaliseTest(unittest.TestCase):
    def test_address_fillers_and_politeness(self):
        self.assertEqual(normalise("Hey Pip, open the terminal."), "open the terminal")
        self.assertEqual(normalise("um, okay Pip can you please close the terminal for me"), "close the terminal")
        self.assertEqual(normalise("Pip, go ahead and run it please"), "run it")
        self.assertEqual(normalise("Hey Nova, hide it", name="Nova"), "hide it")

    def test_spoken_numbers(self):
        self.assertEqual(spoken_numbers("count down from twenty five"), "count down from 25")
        self.assertEqual(spoken_numbers("the seven times table"), "the 7 times table")
        self.assertEqual(spoken_numbers("forty-two and sixty"), "42 and 60")
        self.assertEqual(spoken_numbers("ten, then stop"), "10, then stop")


class OwnerPhrasesTest(unittest.TestCase):
    """The owner's own words from his request, and the design's table."""

    def kind(self, text, context=CLOSED):
        command = parse(text, context)
        return None if command is None else (command.kind, command.action or command.task or command.app)

    def test_owner_request(self):
        self.assertEqual(self.kind("Hey Pip, open the Terminal"), ("open", "terminal"))
        self.assertEqual(self.kind("Hey Pip, close the terminal", SHOWN), ("close", "terminal"))
        self.assertEqual(self.kind("Hey Pip, minimise the terminal", SHOWN), ("minimise", "terminal"))
        self.assertEqual(self.kind("Hey Pip, stop writing that script for a second", WRITING), ("control", "pause"))
        command = parse("Hey Pip, do the following in the terminal: write a script that counts down from ten", SHOWN)
        self.assertEqual((command.kind, command.task, command.recipe, command.slots),
                         ("task", "recipe", "countdown", (("n", 10),)))

    def test_open_variants(self):
        for text in ("open the terminal", "bring up the console", "give me a shell", "terminal please", "launch your terminal"):
            self.assertEqual(self.kind(text), ("open", "terminal"), text)
        self.assertEqual(self.kind("show me the terminal", MINIMISED), ("restore", "terminal"))
        self.assertEqual(self.kind("show me the terminal"), ("open", "terminal"))
        self.assertEqual(parse("open the terminal", SHOWN).say, REPLIES["already_open"])

    def test_minimise_restore_close(self):
        for text in ("minimize the terminal", "hide the terminal", "put away the terminal", "show me your face"):
            self.assertEqual(self.kind(text, SHOWN), ("minimise", "terminal"), text)
        for text in ("bring back the terminal", "switch to the terminal", "show it again"):
            self.assertEqual(self.kind(text, MINIMISED), ("restore", "terminal"), text)
        for text in ("quit the terminal", "exit the console", "shut the terminal", "get rid of the terminal"):
            self.assertEqual(self.kind(text, SHOWN), ("close", "terminal"), text)
        self.assertEqual(parse("close the terminal", CLOSED).say, REPLIES["not_open"])
        self.assertEqual(parse("hide the terminal", CLOSED).say, REPLIES["not_open"])
        self.assertIsNone(parse("show me your face", CLOSED))

    def test_job_controls(self):
        for text, action in (("pause", "pause"), ("hold on", "pause"), ("hang on a second", "pause"), ("wait", "pause"),
                             ("stop", "pause"), ("carry on", "resume"), ("keep going", "resume"), ("go on", "resume"),
                             ("cancel that", "cancel"), ("scrap it", "cancel"), ("throw it away", "cancel"),
                             ("never mind the script", "cancel"), ("forget that script", "cancel"),
                             ("stop it completely", "cancel")):
            self.assertEqual(self.kind(text, WRITING), ("control", action), text)
        self.assertEqual(self.kind("run it", WAITING), ("control", "run"))
        self.assertEqual(self.kind("try it", WAITING), ("control", "run"))
        self.assertEqual(self.kind("fix it", ctx(terminal="shown", job="failed")), ("control", "fix"))
        self.assertEqual(self.kind("carry on", CANCELLED), ("control", "undo"))
        self.assertEqual(self.kind("stop", RUNNING), ("control", "pause"))  # a bare stop freezes, never destroys

    def test_controls_without_a_job(self):
        self.assertEqual(parse("pause", SHOWN).say, REPLIES["nothing"])
        self.assertEqual(parse("carry on", WAITING).say, REPLIES["nothing"])
        # With no terminal they fall through to conversation.
        for text in ("continue the story", "continue", "stop", "wait", "go on", "run it", "cancel that"):
            self.assertIsNone(parse(text, CLOSED), text)

    def test_tasks_route_to_recipes_or_the_model(self):
        cases = {
            "write a script that draws a spiral": ("recipe", "spiral", ()),
            "write a program that prints the seven times table": ("recipe", "times_table", (("n", 7),)),
            "make a script to roll three dice": ("recipe", "dice", (("n", 3),)),
            "in the terminal, show me the files": ("recipe", "files", ()),
            "in the terminal, make a note saying buy milk": ("recipe", "note", (("text", "buy milk"),)),
            "write a script that counts down from ten": ("recipe", "countdown", (("n", 10),)),
            "write a script that sorts a list of words": ("script", "", ()),
            "in the terminal, count the lines in notes.txt": ("command", "", ()),
        }
        for text, (task, recipe, slots) in cases.items():
            command = parse(text, SHOWN)
            self.assertEqual((command.kind, command.task, command.recipe, command.slots), ("task", task, recipe, slots), text)
            self.assertFalse(command.force_model)

    def test_write_it_yourself_forces_the_model(self):
        for text in ("write a script that counts down from ten, write it yourself",
                     "write it yourself: a script that counts down from ten",
                     "write a script that draws a spiral from scratch"):
            command = parse(text, SHOWN)
            self.assertEqual((command.task, command.force_model), ("script", True), text)
        self.assertEqual(parse("write a script that counts down from ten, write it yourself", SHOWN).request,
                         "counts down from 10")

    def test_weather_in_the_terminal_is_honest(self):
        command = parse("in the terminal, show me the weather", SHOWN)
        self.assertEqual((command.recipe, command.say), ("warmth", app_commands.WEATHER_NOTE))

    def test_one_job_at_a_time(self):
        self.assertEqual(parse("write a script that draws a heart", WRITING).say, REPLIES["busy"])
        self.assertEqual(parse("install snake", RUNNING).say, REPLIES["busy"])

    def test_install(self):
        command = parse("install snake", SHOWN)
        self.assertEqual((command.kind, command.program), ("install", "snake"))
        self.assertEqual(parse("install the game of life", SHOWN).program, "life")
        self.assertEqual(parse("play snake", SHOWN).kind, "run_app")
        self.assertEqual(parse("uninstall snake", SHOWN).kind, "uninstall")
        self.assertEqual(parse("install firefox", SHOWN).say, REPLIES["no_download"])
        self.assertEqual(parse("download minecraft", CLOSED).say, REPLIES["no_download"])
        self.assertEqual(parse("what can you install", CLOSED).kind, "shelf_list")
        self.assertEqual(parse("install an application", CLOSED).kind, "shelf_list")
        self.assertIsNone(parse("play some music", SHOWN))
        self.assertIsNone(parse("open the window", SHOWN))

    def test_reports(self):
        for text in ("what happened", "did it work", "what are you doing", "are you done"):
            self.assertEqual(parse(text, WRITING).kind, "report", text)
        self.assertEqual(parse("what did it say", WAITING).kind, "read_line")
        self.assertIsNone(parse("what happened", CLOSED))

    def test_privacy_comes_first(self):
        for text in ("start guest mode", "end guest mode", "forget that", "don't remember this"):
            self.assertIsNone(parse(text, WRITING), text)
        # "forget that script" is a cancel only while a job exists...
        self.assertIsNone(parse("forget that script", SHOWN))
        # ...and once written it is a real forget, with a confirmation.
        self.assertEqual(parse("forget that script", WAITING).kind, "forget_script")

    def test_memory_commands_are_untouched(self):
        for text in ("remember that my cat is called Tom", "what time is it", "what do you remember about my cat",
                     "forget about my cat", "how are you", "set my weather location to Sydney"):
            self.assertIsNone(parse(text, SHOWN), text)
            self.assertIsNone(parse(text, WRITING), text)

    def test_near_misses(self):
        for text in ("I left the terminal at the airport", "the terminal was crowded today", "open your heart",
                     "write me a poem", "can you stop the rain", "make a cup of tea", "run away with me",
                     "show me a picture of a cat"):
            command = parse(text, SHOWN)
            self.assertTrue(command is None or command.kind in ("say",) and command.say == REPLIES["nothing"]
                            or command.kind not in ("open", "close", "task"), (text, command))

    def test_everyday_speech_is_not_an_app_command(self):
        # Ordinary requests that share a verb with a command fall through to
        # the conversation instead of getting the "I can't download" line.
        for text in ("get me a glass of water", "add milk to my shopping list", "get some sleep"):
            self.assertIsNone(parse(text, SHOWN), text)
        # A script request needs its verb ("write", "make"...), so passing
        # talk about a script or an app never starts a job.
        for text in ("a script for my play is due tomorrow", "app that tracks steps would be cool",
                     "program to the moon was expensive"):
            self.assertIsNone(parse(text, SHOWN), text)
        # A one-word "shell" is an answer in conversation, not a terminal.
        for text in ("shell", "a shell", "the shell"):
            self.assertIsNone(parse(text, CLOSED), text)
        self.assertEqual(self.kind("console", CLOSED), ("open", "terminal"))
        self.assertEqual(parse("install firefox", SHOWN).say, REPLIES["no_download"])
        self.assertEqual(parse("get me snake", SHOWN).kind, "install")

    def test_close_confirmation(self):
        pending = ctx(terminal="shown", job="running", pending="close")
        self.assertEqual(parse("yes", pending).action, "yes")
        self.assertEqual(parse("no, leave it", pending), None)
        self.assertEqual(parse("leave it", pending).action, "no")
        self.assertIsNone(parse("yes", RUNNING))

    def test_apps_off_and_guests(self):
        self.assertEqual(parse("open the terminal", ctx(enabled=False)).say, REPLIES["disabled"])
        self.assertEqual(parse("write a script that draws a spiral", ctx(guest=True)).say, REPLIES["guest"])
        self.assertEqual(parse("open the terminal", ctx(hands_up=False)).say, REPLIES["no_hands"])

    def test_every_recipe_pattern_has_a_recipe(self):
        for name, _ in app_commands.RECIPE_PATTERNS:
            self.assertIn("warmth" if name == "weather" else name, RECIPES)
        self.assertEqual(match_recipe("that draws a spiral"), ("spiral", ()))
        self.assertEqual(match_recipe("draws a box 12 wide"), ("box", (("n", 12),)))
        self.assertEqual(match_recipe("sorts my socks"), (None, ()))

    def test_command_start_for_read_ahead(self):
        for text in ("Hey Pip, open the", "write a script", "in the terminal", "carry on", "install"):
            self.assertTrue(may_be_app_command(text), text)
        for text in ("how was your day", "tell me a story"):
            self.assertFalse(may_be_app_command(text), text)

    def test_app_phrases_do_not_become_memory_forgets_when_routed_first(self):
        # mind/commands.py alone reads "forget that script" as a memory forget;
        # app commands must be tried first while a job exists.
        self.assertEqual(memory_parse("forget that script").kind, "forget")
        self.assertEqual(parse("forget that script", WRITING).action, "cancel")


class LinesTest(unittest.TestCase):
    def test_status_lines(self):
        self.assertEqual(status_line({"phase": "waiting", "speakable": True}), "It's ready. Say run it.")
        self.assertEqual(status_line({"phase": "done", "exit": 0, "speakable": True}), "It ran without errors.")
        self.assertEqual(status_line({"phase": "failed", "exit": 2, "speakable": True, "source": "recipe"}),
                         "It failed with exit code 2.")
        self.assertIn("Say fix it", status_line({"phase": "failed", "exit": 1, "speakable": True, "source": "model",
                                                 "kind": "script"}))
        self.assertEqual(status_line({"phase": "paused", "reason": "lid", "speakable": True}), REPLIES["lid"])
        self.assertEqual(status_line({"phase": "paused", "reason": "stall", "speakable": True}), REPLIES["stall"])
        self.assertEqual(status_line({"phase": "waiting"}), "")  # not marked speakable
        for status in ({"phase": "done", "exit": 0}, {"phase": "failed", "exit": 1}):
            self.assertNotIn("works", status_line({**status, "speakable": True}))

    def test_reports(self):
        self.assertEqual(report({"phase": "writing", "lines": 14, "source": "model"}),
                         "I'm writing it with my language model. Fourteen lines so far.")
        self.assertEqual(report({"phase": "paused", "lines": 14}), "It's paused at line fourteen.")
        self.assertEqual(report({"phase": "done", "exit": 0}, {"lines": 12}),
                         "It ran without errors. It printed twelve lines. They're on the screen.")
        self.assertEqual(report(None), REPLIES["nothing"])

    def test_read_line(self):
        self.assertEqual(read_line({"text": "Lift off!", "lines": 11}), 'It said: "Lift off!"')
        self.assertEqual(read_line({"text": "", "lines": 3}), "It printed three lines. They're on the screen.")
        self.assertEqual(read_line(None), "It didn't print anything I can read out.")


class SilentBus:
    def __init__(self):
        self.sent = []

    async def send(self, topic, **fields):
        self.sent.append({"t": topic, **fields})


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = SilentBus()
        self.bridge = AppBridge(self.bus, shelf=SHELF, enabled=lambda: True)
        self.bridge.hands_up = True
        self.said = []

    async def say(self, line):
        self.said.append(line)

    async def test_never_claims_an_open_terminal_without_app_state(self):
        with mock.patch.object(app_commands, "OPEN_WAIT", 0.05):
            await self.bridge.handle(parse("open the terminal", CLOSED), self.say)
        self.assertEqual(self.said, ["My terminal didn't open."])
        self.assertEqual(self.bus.sent[0]["t"], "app.open")

    async def test_open_answered(self):
        async def answer():
            await asyncio.sleep(0.01)
            req = self.bus.sent[0]["req"]
            self.bridge.observe({"t": "app.state", "app": "terminal", "view": "shown", "req": req})
        asyncio.get_running_loop().create_task(answer())
        await self.bridge.handle(parse("open the terminal", CLOSED), self.say)
        self.assertEqual(self.said, ["Here's my terminal."])
        self.assertEqual(self.bridge.view["terminal"], "shown")

    async def test_refusal_is_reported(self):
        async def answer():
            await asyncio.sleep(0.01)
            self.bridge.observe({"t": "app.refused", "reason": "disabled", "req": self.bus.sent[0]["req"]})
        asyncio.get_running_loop().create_task(answer())
        await self.bridge.handle(parse("open the terminal", CLOSED), self.say)
        self.assertEqual(self.said, [REPLIES["disabled"]])

    async def test_hands_going_down_closes_the_views(self):
        self.bridge.observe({"t": "app.state", "app": "terminal", "view": "shown"})
        self.bridge.observe({"t": "organ.down", "name": "hands"})
        self.assertEqual(self.bridge.view["terminal"], "closed")
        self.assertEqual(self.bridge.parse("open the terminal").say, REPLIES["no_hands"])

    async def test_announcements_and_episode(self):
        self.bridge.observe({"t": "term.status", "id": "j1", "phase": "waiting", "file": "spiral.py",
                             "sha256": "ab", "speakable": True, "source": "recipe"})
        self.assertEqual(self.bridge.announcement(), "It's ready. Say run it.")
        self.assertIsNone(self.bridge.announcement())
        self.assertEqual(self.bridge.episode(), ("I wrote spiral.py in the terminal", {"file": "spiral.py",
                                                                                      "sha256": "ab", "job": "j1"}))
        self.assertIsNone(self.bridge.episode())  # recorded once, not on every beat
        self.bridge.guest = True
        self.bridge.observe({"t": "term.status", "id": "j2", "phase": "waiting", "file": "tree.py", "sha256": "cd"})
        self.assertIsNone(self.bridge.episode())

    async def test_forgetting_a_script_hands_back_its_episodes(self):
        self.bridge.observe({"t": "term.status", "id": "j1", "phase": "waiting", "file": "spiral.py", "sha256": "ab"})
        text, meta = self.bridge.episode()
        self.bridge.note_episode(41, meta)
        self.bridge.note_episode(42, {"file": "other.py", "job": "j0"})
        self.bridge.observe({"t": "term.status", "id": "j1f", "phase": "waiting", "file": "spiral-2.py",
                             "sha256": "ef", "fixed": True})
        self.bridge.note_episode(43, self.bridge.episode()[1])
        await self.bridge.handle(parse("forget that script", WAITING), self.say)

        async def answer():
            await asyncio.sleep(0.01)
            sent = self.bus.sent[-1]
            self.assertEqual((sent["t"], sent["id"]), ("term.forget", "j1f"))
            self.bridge.observe({"t": "term.status", "phase": "forgotten", "removed": 2, "changed": [],
                                 "req": sent["req"]})
        asyncio.get_running_loop().create_task(answer())
        await self.bridge.handle(self.bridge.parse("yes"), self.say)
        self.assertEqual(self.said[-1], REPLIES["forgotten"])
        self.assertEqual(sorted(self.bridge.forgotten_episodes()), [41, 43])  # the fix and the file it fixed
        self.assertEqual(self.bridge.forgotten_episodes(), [])

    async def test_forget_with_nothing_to_forget_says_so(self):
        self.bridge.observe({"t": "term.status", "id": "j1", "phase": "waiting", "file": "spiral.py"})
        await self.bridge.handle(parse("forget that script", WAITING), self.say)

        async def answer():
            await asyncio.sleep(0.01)
            sent = self.bus.sent[-1]
            self.bridge.observe({"t": "term.status", "phase": None, "reason": "no_job", "req": sent["req"]})
        asyncio.get_running_loop().create_task(answer())
        await self.bridge.handle(self.bridge.parse("yes"), self.say)
        self.assertEqual(self.said[-1], "I don't have a script to forget.")

    async def test_refusals_say_what_is_true(self):
        def refused(**fields):
            return self.bridge._refused_line({"t": "app.refused", **fields})
        self.assertEqual(refused(reason="bad_slot", low=1, high=60),
                         "My recipe for that takes a number from one to sixty.")
        self.assertEqual(refused(reason="lid_closed"), REPLIES["lid_closed"])
        self.assertTrue(all(refused(reason=r) for r in ("busy", "failed", "unknown", "lid_closed", "bad_slot")))

    async def test_weather_note_only_once_the_job_started(self):
        async def refuse():
            await asyncio.sleep(0.01)
            self.bridge.observe({"t": "app.refused", "reason": "lid_closed", "req": self.bus.sent[-1]["req"]})
        asyncio.get_running_loop().create_task(refuse())
        await self.bridge.handle(parse("in the terminal, show me the weather", SHOWN), self.say)
        self.assertEqual(self.said, [REPLIES["lid_closed"]])

    async def test_lease_is_free_without_a_model_job(self):
        self.assertFalse(await self.bridge.lease())
        self.bridge.observe({"t": "term.status", "id": "j1", "phase": "writing", "source": "recipe"})
        self.assertFalse(await self.bridge.lease())
        self.assertEqual(self.bus.sent, [])

    async def test_lease_parks_a_model_job_and_gives_up_after_half_a_second(self):
        self.bridge.observe({"t": "term.status", "id": "j1", "phase": "writing", "source": "model"})
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.assertTrue(await self.bridge.lease())
        self.assertLess(loop.time() - started, 0.8)
        self.assertEqual(self.bus.sent[-1]["action"], "pause")
        self.assertEqual(self.bus.sent[-1]["reason"], "turn")
        await self.bridge.unlease()
        self.assertEqual(self.bus.sent[-1]["action"], "resume")

    async def test_privacy(self):
        self.bridge.observe({"t": "term.status", "id": "j1", "phase": "waiting", "speakable": True})
        await self.bridge.privacy(True)
        self.assertEqual(self.bus.sent[-1], {"t": "app.privacy", "guest": True})
        self.assertIsNone(self.bridge.status)
        self.assertIsNone(self.bridge.announcement())

    async def test_shelf_list(self):
        await self.bridge.handle(parse("what can you install", CLOSED), self.say)
        self.assertTrue(self.said[0].startswith("I can't download anything, but my shelf has dice, guess"))


if __name__ == "__main__":
    unittest.main()
