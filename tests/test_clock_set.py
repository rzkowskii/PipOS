"""Telling Pip the date and time after its clock was reset (mind/clock_set.py)."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from mind import clock_epoch, clock_set, commands, everyday
from mind.turn import Mind

SYDNEY = ZoneInfo("Australia/Sydney")


class Reading(unittest.TestCase):
    def check(self, text, day, month, year, clock):
        said = clock_set.read(text)
        self.assertEqual((said.day, said.month, said.year, said.clock), (day, month, year, clock), text)

    def test_the_forms_an_owner_would_say_or_type(self):
        for text, expected in (
            ("It's the 28th of November 2026, twenty to four in the afternoon", (28, 11, 2026, (15, 40))),
            ("28 November 2026, 3:40 pm", (28, 11, 2026, (15, 40))),
            ("2026-11-28 15:40", (28, 11, 2026, (15, 40))),
            ("it is the twenty eighth of November twenty twenty six at quarter to four in the afternoon",
             (28, 11, 2026, (15, 45))),
            ("It's November 28th, half past two in the afternoon", (28, 11, None, (14, 30))),
            ("it's the 1st of December 2026, ten past nine at night", (1, 12, 2026, (21, 10))),
            ("today is Saturday the 28th of November, 9 in the morning", (28, 11, None, (9, 0))),
            ("it's the third of January two thousand and twenty seven, midnight", (3, 1, 2027, (0, 0))),
            ("it's 28 Nov 2026 noon", (28, 11, 2026, (12, 0))),
            ("it's the 28th of November 2026, three forty five p.m.", (28, 11, 2026, (15, 45))),
            ("the date is the 5th of May 2027 and it is 7 o'clock in the evening", (5, 5, 2027, (19, 0))),
            ("it's 2026-12-03, quarter past eight in the morning", (3, 12, 2026, (8, 15))),
        ):
            self.check(text, *expected)

    def test_a_date_alone_and_a_time_alone(self):
        self.check("it's the 28th of November 2026", 28, 11, 2026, None)
        said = clock_set.read("twenty to four in the afternoon")
        self.assertEqual((said.day, said.clock), (None, (15, 40)))

    def test_a_time_without_am_or_pm_is_asked_about_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "morning or afternoon"):
            clock_set.read("it's the 28th of November 2026, 3:40")
        self.check("it's the 28th of November 2026, 15:40", 28, 11, 2026, (15, 40))

    def test_nothing_in_ordinary_talk_reads_as_a_date(self):
        for text in ("what time is it", "it's cold today", "remind me on the 3rd to call mum",
                     "the weather in November"):
            self.assertFalse(clock_set.read(text).has_date, text)


class Moment(unittest.TestCase):
    def moment(self, text, today=datetime(2026, 10, 17, 9, 0, tzinfo=SYDNEY), state_dir=None):
        return clock_set.moment(clock_set.read(text), SYDNEY, today.date(), state_dir)

    def test_a_good_date_becomes_the_owners_local_moment(self):
        when = self.moment("it's the 28th of November 2026, twenty to four in the afternoon")
        self.assertEqual((when.year, when.month, when.day, when.hour, when.minute), (2026, 11, 28, 15, 40))
        self.assertEqual(when.tzinfo, SYDNEY)

    def test_the_year_comes_from_the_clock_when_it_is_not_said(self):
        when = self.moment("it's November 28th, 3:40 pm")
        self.assertEqual(when.year, 2026)
        with self.assertRaisesRegex(ValueError, "year"):
            self.moment("it's November 28th, 3:40 pm", today=datetime(2022, 1, 3, 9, 0, tzinfo=SYDNEY))

    def test_a_date_that_cannot_be_right_is_refused_with_a_reason(self):
        with self.assertRaisesRegex(ValueError, "no 31 February 2027"):
            self.moment("it's the 31st of February 2027, 3 pm")
        with self.assertRaisesRegex(ValueError, "before 2026"):
            self.moment("it's the 2nd of January 2020, 3 pm")
        with self.assertRaisesRegex(ValueError, "2100"):
            self.moment("it's the 2nd of January 2101, 3 pm")

    def test_a_date_before_what_pip_already_remembers_is_refused(self):
        floor = datetime(2026, 11, 23, 12, 0, tzinfo=SYDNEY).timestamp()
        with patch.object(clock_epoch, "floor", return_value=int(floor)):
            with self.assertRaisesRegex(ValueError, "earlier than things I already remember"):
                self.moment("it's the 20th of November 2026, 3 pm", state_dir="/mind")
            self.moment("it's the 24th of November 2026, 3 pm", state_dir="/mind")


class Skill(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock_set._pending.clear()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = SimpleNamespace(now=datetime(2026, 10, 17, 9, 0, tzinfo=SYDNEY), guest=False, speaker="Richard",
                                   state_dir=Path(self.dir.name), name="Pip")

    PHRASE = "It's the 28th of November 2026, twenty to four in the afternoon"

    async def test_it_reads_back_and_asks_for_a_yes_without_changing_anything(self):
        with patch.object(clock_set.care, "set_clock") as setter:
            reply = await clock_set.handle(self.PHRASE, self.ctx)
        setter.assert_not_called()
        self.assertIn("Saturday 28 November 2026, 3:40 in the afternoon", reply.line)
        self.assertIn("yes", reply.line.casefold())
        self.assertIsNotNone(reply.confirm)
        self.assertTrue(reply.confirm.strict, "a plain yes, not an okay or a sure")

    async def test_on_a_yes_it_goes_through_the_confirmed_clock_path_and_the_battery_clock(self):
        reply = await clock_set.handle(self.PHRASE, self.ctx)
        with patch.object(clock_set, "appliance", return_value=True), \
                patch.object(clock_set.care, "set_clock", return_value=True) as setter:
            done = await reply.confirm.on_yes()
        expected = datetime(2026, 11, 28, 15, 40, tzinfo=SYDNEY).timestamp()
        setter.assert_called_once_with(self.ctx.state_dir, expected, date_confirmed=True, write_rtc=True)
        self.assertIn("3:40 in the afternoon", done.line)
        self.assertNotIn("battery clock", done.line)

    async def test_it_says_so_when_the_battery_clock_would_not_take_it(self):
        reply = await clock_set.handle(self.PHRASE, self.ctx)
        with patch.object(clock_set, "appliance", return_value=True), \
                patch.object(clock_set.care, "set_clock", return_value=False):
            done = await reply.confirm.on_yes()
        self.assertIn("battery clock", done.line)

    async def test_a_development_machines_clock_is_never_set(self):
        reply = await clock_set.handle(self.PHRASE, self.ctx)
        with patch.object(clock_set, "appliance", return_value=False), \
                patch.object(clock_set.care, "set_clock") as setter:
            done = await reply.confirm.on_yes()
        setter.assert_not_called()
        self.assertIn("only set my own clock on my laptop", done.line)

    async def test_failures_are_told_plainly(self):
        reply = await clock_set.handle(self.PHRASE, self.ctx)
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        for error, words in ((PermissionError("no"), "couldn't change my clock myself"),
                             (busy, "timers are busy"), (ValueError("range"), "couldn't change my clock myself")):
            with patch.object(clock_set, "appliance", return_value=True), \
                    patch.object(clock_set.care, "set_clock", side_effect=error):
                done = await reply.confirm.on_yes()
            self.assertIn(words, done.line)

    async def test_a_guest_cannot_set_the_clock(self):
        self.ctx.guest = True
        with patch.object(clock_set.care, "set_clock") as setter:
            reply = await clock_set.handle(self.PHRASE, self.ctx)
        setter.assert_not_called()
        self.assertIsNone(reply.confirm)
        self.assertIn("Only the person I live with", reply.line)
        # And nothing waits to be combined for a guest.
        self.assertEqual(clock_set._pending, {})

    async def test_without_a_time_zone_it_says_so_instead_of_guessing(self):
        self.ctx.now = None
        reply = await clock_set.handle(self.PHRASE, self.ctx)
        self.assertIsNone(reply.confirm)
        self.assertIn("time zone", reply.line)

    async def test_a_wrong_date_is_refused_with_the_reason_and_asks_nothing(self):
        reply = await clock_set.handle("it's the 31st of February 2027, 3 pm", self.ctx)
        self.assertIsNone(reply.confirm)
        self.assertIn("no 31 February 2027", reply.line)
        reply = await clock_set.handle("it's the 2nd of January 2020, 3 pm", self.ctx)
        self.assertIsNone(reply.confirm)
        self.assertIn("before 2026", reply.line)

    async def test_a_date_then_a_time_in_the_next_sentence(self):
        first = await clock_set.handle("it's the 28th of November 2026", self.ctx)
        self.assertIsNone(first.confirm)
        self.assertIn("what time", first.line.casefold())
        self.assertTrue(clock_set.may_match("twenty to four in the afternoon"), "the time that follows is ours")
        second = await clock_set.handle("twenty to four in the afternoon", self.ctx)
        self.assertIn("Saturday 28 November 2026, 3:40 in the afternoon", second.line)
        self.assertIsNotNone(second.confirm)
        self.assertEqual(clock_set._pending, {})

    async def test_a_date_that_waited_too_long_is_not_combined_with_a_later_time(self):
        await clock_set.handle("it's the 28th of November 2026", self.ctx)
        key = str(self.ctx.state_dir)
        clock_set._pending[key] = (clock_set._pending[key][0], time.monotonic() - 1)
        self.assertFalse(clock_set.may_match("twenty to four in the afternoon"))
        self.assertIsNone(await clock_set.handle("twenty to four in the afternoon", self.ctx))

    async def test_a_date_mentioned_in_passing_is_not_a_request(self):
        for text in ("my birthday is on the 3rd of March 2027", "what is the weather on 28 November",
                     "I was born in 2001", "what time is it"):
            self.assertIsNone(await clock_set.handle(text, self.ctx), text)

    async def test_the_name_and_politeness_are_ignored(self):
        reply = await clock_set.handle("Hey Pip, it's the 28th of November 2026, 3:40 pm, please", self.ctx)
        self.assertIsNotNone(reply.confirm)

    async def test_it_works_for_typed_text_too(self):
        # Typed lines come through the same handler, in the same words.
        reply = await clock_set.handle("it's 2026-11-28 15:40", self.ctx)
        self.assertIsNotNone(reply.confirm)


class Wiring(unittest.TestCase):
    def test_the_skill_is_registered_and_gates_the_speculative_read(self):
        self.assertIn("clock_set", everyday.ORDER)
        everyday._DEFAULT._modules = None
        self.assertIn("clock_set", [module.__name__.rsplit(".", 1)[-1] for module in everyday.preload()])
        self.assertTrue(everyday.may_match("it's the 28th of November 2026, twenty to four in the afternoon"))
        self.assertFalse(everyday.may_match("it's a lovely day"))

    def test_the_time_of_day_command_does_not_swallow_a_whole_date(self):
        self.assertEqual(commands.parse("set the time to 3:45 pm", "Pip").kind, "set_time")
        for text in ("set the time to 3:45 pm on the 28th of November 2026",
                     "the time is 3:45 pm on 28 November 2026"):
            command = commands.parse(text, "Pip")
            self.assertTrue(command is None or command.kind != "set_time", text)


class Asking(unittest.TestCase):
    """The reset is explained once per boot, then only a short pointer."""

    def mind(self):
        return SimpleNamespace()

    def test_the_first_question_gets_the_whole_request_and_later_ones_a_short_one(self):
        stub = self.mind()
        first = Mind._doubtful_clock_line(stub, "reset")
        again = Mind._doubtful_clock_line(stub, "reset", what="date")
        self.assertIn("clock was reset", first)
        self.assertIn("28th of November", first, "it shows how to say it")
        self.assertNotIn("reset", again)
        self.assertLess(len(again), len(first))
        self.assertIn("date and time", again)

    def test_a_clock_that_went_backwards_is_asked_for_once_as_well(self):
        stub = self.mind()
        first = Mind._doubtful_clock_line(stub, "behind")
        again = Mind._doubtful_clock_line(stub, "behind")
        self.assertIn("gone backwards", first)
        self.assertNotIn("gone backwards", again)

    def test_a_new_boot_asks_again(self):
        self.assertIn("reset", Mind._doubtful_clock_line(self.mind(), "reset"))
        self.assertIn("reset", Mind._doubtful_clock_line(self.mind(), "reset"))


if __name__ == "__main__":
    unittest.main()
