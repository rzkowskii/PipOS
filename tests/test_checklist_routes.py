"""Every request on the v12 voice checklist routes where the checklist expects, heard perfectly.

Evidence label: host test, text in and route out, no audio. The phrases are
Parts A and B of the owner's voice checklist (out/diag-work/voice-script.md,
28 Nov 2026), plus everyday rewordings of the code-answered ones.
"""

import unittest

from mind import commands, device
from tests.test_turn_route import RouteLines

# Part A, said in order (the timer is cancelled after it is set; the terminal
# is closed after it is opened).
PART_A = (
    ("what time is it?", "command.time"),
    ("set a timer for two minutes", "command.reminder"),
    ("cancel the timer", "command.reminder"),
    ("how is your battery?", "command.battery"),
    ("make the screen brighter", "everyday.device"),
    ("open a terminal", "workshop.open"),
    ("close the terminal", "workshop.close"),
    ("what's the capital of France?", "model"),
    ("tell me one fact about the Moon", "model"),
    ("how are you feeling today?", "model"),
)
# Part B suggestions that are answered in code.
PART_B = (
    ("set a timer for one minute", "command.reminder"),
    ("cancel the timer", "command.reminder"),
    ("make the screen dimmer", "everyday.device"),
    ("what can you do?", "command.skills"),
    ("be quieter", "command.volume_down"),
    ("speak up", "command.volume_up"),
)
# How people also say the code-answered requests.
REWORDED = (
    ("what's the time?", "command.time"),
    ("tell me the time", "command.time"),
    ("do you know what time it is?", "command.time"),
    ("how much battery have you got?", "command.battery"),
    ("how much battery do you have?", "command.battery"),
    ("can you make the screen brighter?", "everyday.device"),
    ("turn the screen up", "everyday.device"),
    ("turn the screen down", "everyday.device"),
    ("dim the screen", "everyday.device"),
    ("make the screen darker", "everyday.device"),
    ("make your screen brighter", "everyday.device"),
    ("turn up the brightness", "everyday.device"),
    ("start a two minute timer", "command.reminder"),
    ("cancel my timer", "command.reminder"),
)


class ChecklistRoutes(RouteLines):
    async def route_of(self, text):
        count = len(self.routes())
        await self.say(text)
        routes = self.routes()
        return routes[count]["route"] if len(routes) > count else None

    async def check(self, rows):
        got = [(text, await self.route_of(text)) for text, _ in rows]
        self.assertEqual(got, [(text, route) for text, route in rows])

    async def test_part_a_routes_as_expected(self):
        await self.check(PART_A)

    async def test_part_b_code_requests_route_as_expected(self):
        await self.check(PART_B)

    async def test_rewordings_route_as_expected(self):
        # A timer to cancel, set first and not checked here.
        await self.route_of("set a timer for five minutes")
        await self.check(REWORDED)


# Only the checklist here: the route-line tests themselves run in tests/test_turn_route.py.
for _name in [name for name in vars(RouteLines) if name.startswith("test_")]:
    setattr(ChecklistRoutes, _name, None)


class BrightnessWords(unittest.TestCase):
    def test_the_screen_as_well_as_your_screen(self):
        for text in ("make the screen brighter", "Make the screen brighter.", "make your screen brighter",
                     "make the face brighter", "turn the screen up", "Hey Pip, can you make the screen brighter?"):
            self.assertEqual(device.classify(text), ("step", 1), text)
        for text in ("make the screen dimmer", "make the screen darker", "make your screen darker",
                     "turn the screen down", "dim the screen", "dim your screen", "dim the face"):
            self.assertEqual(device.classify(text), ("step", -1), text)

    def test_other_sentences_about_the_screen_are_not_steps(self):
        for text in ("the screen is brighter today", "make the screen bigger", "turn the screen off",
                     "why is the screen dimmer", "make the screen brighter than the sun"):
            self.assertIsNone(device.classify(text), text)

    def test_battery_have_you_got(self):
        for text in ("how much battery have you got?", "how much charge have you got left",
                     "how much battery do you have"):
            command = commands.parse(text, "pip")
            self.assertEqual(command.kind if command else None, "battery", text)

    def test_do_you_know_what_time_it_is(self):
        command = commands.parse("do you know what time it is?", "pip")
        self.assertEqual(command.kind if command else None, "time")


if __name__ == "__main__":
    unittest.main()
