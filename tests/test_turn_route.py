"""The mind's route line: one text-free line per turn naming the step that
answered it (mind/turn.py `_route`), read by the diagnostics and
lab/try_timings.py. The words appear only in voice-debug mode, and in guest
or private mode the line says nothing about the turn."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mind.turn
from tests.test_mind import MindCase
from tests.test_turn_apps import Hands


class RouteLines(MindCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.lines = []
        self.enterContext(mock.patch.object(mind.turn, "log", lambda *parts: self.lines.append(" ".join(map(str, parts)))))
        self.mind.__dict__["_voice_debug"] = False
        self.hands = Hands(self.bus)
        self.bus.push("organ.up", name="hands")
        await asyncio.sleep(0)

    async def say(self, text):
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard")
        await self.bus.wait("mind.reply_done", replies + 1, timeout=10.0)
        self.bus.push("say.done", final=True)
        await asyncio.sleep(0)

    def routes(self):
        return [json.loads(line.removeprefix("route ")) for line in self.lines if line.startswith("route ")]

    async def test_each_step_names_its_route_and_never_the_words(self):
        for text in ("Hey Pip, open a terminal", "what time is it", "what's 17 times 23", "hello",
                     "I planted beans today", "close the terminal"):
            await self.say(text)
        routes = self.routes()
        self.assertEqual([r["route"] for r in routes],
                         ["workshop.open", "command.time", "everyday.calc", "greeting", "model", "workshop.close"])
        self.assertEqual(routes[0]["chars"], len("Hey Pip, open a terminal"))
        for route in routes:
            self.assertEqual(set(route), {"route", "chars"}, route)
        logged = "\n".join(self.lines)
        for words in ("planted", "beans", "17 times"):
            self.assertNotIn(words, logged)

    async def test_voice_debug_adds_the_words(self):
        self.mind.__dict__["_voice_debug"] = True
        await self.say("open the monitor")
        route = self.routes()[-1]
        self.assertEqual(route, {"route": "workshop.open", "chars": 16, "text": "open the monitor"})

    async def test_guest_mode_says_nothing_about_the_turn_even_in_voice_debug(self):
        self.mind.__dict__["_voice_debug"] = True
        await self.say("start guest mode")
        self.assertEqual(self.routes()[-1]["route"], "command.guest_start")
        await self.say("I planted beans today")
        await self.say("what time is it")
        self.assertEqual(self.routes()[-2:], [{"route": "private"}, {"route": "private"}])
        self.assertNotIn("beans", "\n".join(self.lines))

    async def test_model_waiting_only_when_the_cache_clear_failed(self):
        """Out of private mode the slot must be cleared first: a clear that
        works leaves an ordinary model turn, and only a failed one waits."""
        answers = []

        async def erase():
            return answers.pop(0)
        self.mind._erase_cache = erase
        for cleared in (True, False):
            answers.append(cleared)
            self.mind._erase_required = True
            await self.say("I planted beans today")
            self.assertEqual(self.routes()[-1]["route"], "model" if cleared else "model.waiting", cleared)
            self.assertEqual(self.mind._erase_required, not cleared)
        self.assertEqual(answers, [], "the clear is tried once a turn")


class VoiceDebugSwitch(unittest.TestCase):
    def cmdline(self, text):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "cmdline"
        path.write_text(text)
        return path

    def test_try_boots_only_or_the_environment(self):
        on = self.cmdline("BOOT_IMAGE=/bzImage being.try=1 being.voice_debug=1 quiet\n")
        self.assertTrue(mind.turn.voice_debug_on(on, environ={}))
        installed = self.cmdline("BOOT_IMAGE=/bzImage being.voice_debug=1\n")
        self.assertFalse(mind.turn.voice_debug_on(installed, environ={}), "never on an installed system")
        self.assertFalse(mind.turn.voice_debug_on(self.cmdline("being.try=1\n"), environ={}))
        self.assertTrue(mind.turn.voice_debug_on(installed, environ={"BEING_VOICE_DEBUG": "1"}))
        self.assertFalse(mind.turn.voice_debug_on(Path("/nonexistent/cmdline"), environ={"BEING_VOICE_DEBUG": "0"}))

    def test_the_last_voice_debug_argument_counts_as_for_the_voice(self):
        from mind import voice

        undone = self.cmdline("being.try=1 being.cable=1 being.voice_debug=1 quiet being.voice_debug=0\n")
        self.assertFalse(mind.turn.voice_debug_on(undone, environ={}))
        self.assertEqual(voice.voice_debug({}, undone, try_boot=True), (False, None))
        redone = self.cmdline("being.try=1 being.voice_debug=0 being.voice_debug=1\n")
        self.assertTrue(mind.turn.voice_debug_on(redone, environ={}))
        self.assertTrue(voice.voice_debug({}, redone, try_boot=True)[0])

    def test_route_names_carry_no_words(self):
        self.assertEqual(mind.turn.route_name("workshop", "open", "", ""), "workshop.open")
        self.assertEqual(mind.turn.route_name("command", "Set Timer!"), "command.set_timer_")


if __name__ == "__main__":
    unittest.main()
