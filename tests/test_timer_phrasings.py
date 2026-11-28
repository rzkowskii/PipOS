"""Everyday timer wordings that went to the model (route sweep, 28 Nov 2026).

Evidence label: host test, text in. "Stop the timer" with nothing ringing and
"timer for two minutes" routed to the model in the sweep of the voice
checklist and its rewordings (research_notes/2026-11-28-everyday-speech-route-sweep.md).
"""

import unittest

from mind import commands
from tests.test_turn_route import RouteLines


def kind(text):
    command = commands.parse(text, "pip")
    if command is None:
        return None
    if command.kind == "reminder":
        return f"reminder.{command.reminder.kind}"
    return command.kind


class Parsing(unittest.TestCase):
    def test_timer_for_a_duration_without_a_verb_sets_a_timer(self):
        for text in ("timer for two minutes", "a timer for 5 minutes", "Timer for ten seconds please"):
            self.assertEqual(kind(text), "reminder.timer", text)

    def test_stop_the_timer_cancels_it(self):
        for text in ("stop the timer", "stop my timer", "Can you stop the timer?"):
            self.assertEqual(kind(text), "reminder.cancel", text)

    def test_while_it_rings_stop_the_timer_still_acknowledges_it(self):
        for text in ("stop the timer", "stop my timer", "stop it"):
            self.assertEqual(commands.ring_reply(text, "pip").kind, "ack", text)

    def test_other_sentences_stay_as_they_were(self):
        for text in ("stop the alarm", "the timer for the oven is broken", "timer", "stop"):
            self.assertNotIn(kind(text), ("reminder.timer", "reminder.cancel"), text)


class Routes(RouteLines):
    async def test_the_sweep_misses_now_route_to_code(self):
        for text in ("timer for two minutes", "stop the timer"):
            count = len(self.routes())
            await self.say(text)
            self.assertEqual(self.routes()[count]["route"], "command.reminder", text)


for _name in [name for name in vars(RouteLines) if name.startswith("test_")]:
    setattr(Routes, _name, None)


if __name__ == "__main__":
    unittest.main()
