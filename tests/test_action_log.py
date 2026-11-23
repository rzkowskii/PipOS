"""The action log: counts and kinds only, rotated, cleared, and told."""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from mind import action_log

SYDNEY = ZoneInfo("Australia/Sydney")
CANARY = "canary-7f3a-the-plumber-comes-thursday"


def context(state_dir, now, guest=False):
    return SimpleNamespace(state_dir=Path(state_dir), now=now, guest=guest, clock_trusted=True, speaker="owner",
                           owner="Richard", last_heard=None, last_said=None, body={}, voice={}, send=None)


class ActionLogTests(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def lines(self):
        path = action_log.log_path(self.mind)
        return path.read_text(encoding="ascii").splitlines() if path.exists() else []

    def test_lines_are_exactly_ts_kind_and_count(self):
        self.assertTrue(action_log.record(self.mind, "list.add", 3, now=1_800_000_000.7))
        self.assertTrue(action_log.record(self.mind, "backup", now=1_800_000_100))
        self.assertEqual([json.loads(line) for line in self.lines()],
                         [{"ts": 1_800_000_000, "kind": "list.add", "count": 3},
                          {"ts": 1_800_000_100, "kind": "backup", "count": 1}])
        path = action_log.log_path(self.mind)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_canary_text_never_lands_in_the_log(self):
        for kind, count in ((CANARY, 1), ("list.add", CANARY), (f"list.add {CANARY}", 1), ("list.add", True),
                            ("list.add", 2.5), ("list.add", -1), ("list.add", 10**9), (None, 1), (["list.add"], 1)):
            self.assertFalse(action_log.record(self.mind, kind, count), (kind, count))
        action_log.record(self.mind, "note.add")
        raw = b"".join(path.read_bytes() for path in (self.mind / "log").iterdir())
        self.assertNotIn(CANARY.encode(), raw)
        # Only known kinds exist at all, and every one is a short dotted name.
        for kind in action_log.KINDS:
            self.assertRegex(kind, r"^[a-z]+(?:\.[a-z]+)?$")
        # A hand-planted line with text is never read back, nor told.
        with action_log.log_path(self.mind).open("a", encoding="ascii") as stream:
            stream.write(json.dumps({"ts": 1, "kind": "note.add", "count": 1, "text": CANARY}) + "\n")
            stream.write(json.dumps({"ts": 1, "kind": CANARY, "count": 1}) + "\n")
            stream.write("not json\n")
        found = action_log.entries(self.mind)
        self.assertEqual([entry["kind"] for entry in found], ["note.add"])
        self.assertNotIn(CANARY, json.dumps(found))

    def test_every_told_kind_has_a_place_in_the_telling_order(self):
        told = {kind for kind, template in action_log.KINDS.items() if template is not None}
        self.assertEqual(told, set(action_log.PRIORITY))
        for kind in ("list.add", "list.remove", "list.delete", "list.clear", "note.add", "note.delete", "note.clear",
                     "mute", "brightness", "shutdown", "reboot", "backup", "backup.failed", "forget", "guest.start",
                     "guest.end", "workshop.open", "workshop.run"):
            self.assertIn(kind, action_log.KINDS, "a kind other parts log")

    def test_rotation_at_one_megabyte_keeps_one_older_generation(self):
        path = action_log.log_path(self.mind)
        path.parent.mkdir(mode=0o700)
        line = json.dumps({"ts": 1, "kind": "timer.set", "count": 1}, separators=(",", ":")) + "\n"
        path.write_text(line * (action_log.ROTATE_BYTES // len(line)), encoding="ascii")
        self.assertTrue(action_log.record(self.mind, "backup", now=2))
        self.assertLessEqual((path.parent / action_log.OLD_NAME).stat().st_size, action_log.ROTATE_BYTES)
        self.assertEqual(self.lines(), ['{"ts":2,"kind":"backup","count":1}'])
        self.assertEqual(action_log.last(self.mind, ["backup"])["ts"], 2)
        self.assertGreater(len(action_log.entries(self.mind)), 1000, "both generations are read")

    def test_forget_everything_clears_both_generations(self):
        action_log.record(self.mind, "forget", 2)
        (self.mind / "log" / action_log.OLD_NAME).write_text('{"ts":1,"kind":"dream","count":1}\n')
        self.assertTrue(action_log.clear(self.mind))
        self.assertEqual(action_log.entries(self.mind), [])
        self.assertEqual(list((self.mind / "log").iterdir()), [])
        self.assertTrue(action_log.erase_all(self.mind), "nothing left is fine too")

    def test_a_full_disk_or_a_symlink_is_a_skipped_line_not_an_error(self):
        (self.mind / "log").mkdir()
        os.symlink("/nonexistent/elsewhere", action_log.log_path(self.mind))
        self.assertFalse(action_log.record(self.mind, "backup"))
        self.assertEqual(action_log.entries(self.mind), [])

    def test_summary_tells_at_most_three_things_from_templates(self):
        found = [{"ts": 1, "kind": kind, "count": count} for kind, count in
                 (("night.tidy", 1), ("backup.auto", 1), ("dream", 1), ("timer.set", 2), ("guest.end", 1))]
        self.assertEqual(action_log.summary(found, "last night"),
                         "[neutral] Last night I tidied my memory, backed myself up and dreamed. And a few small things.")
        self.assertEqual(action_log.summary([{"ts": 1, "kind": "timer.set", "count": 2},
                                             {"ts": 2, "kind": "list.add", "count": 1},
                                             {"ts": 3, "kind": "list.add", "count": 2}], "today"),
                         "[neutral] Today I set two timers and added three things to your lists.")
        self.assertEqual(action_log.summary([{"ts": 1, "kind": "guest.end", "count": 1}], "today"),
                         "[neutral] Nothing worth telling today: I mostly waited for you.")
        for line in (action_log.summary(found, "since yesterday"),):
            self.assertLessEqual(len(line.split()) - 1, 25)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = datetime(2026, 11, 24, 7, 30, tzinfo=SYDNEY)

    def ask(self, text, guest=False, now=None):
        return asyncio.run(action_log.handle(text, context(self.mind, now or self.now, guest)))

    def at(self, **delta):
        return (self.now + timedelta(**delta)).timestamp()

    def test_phrases_reach_the_handler_and_others_pass(self):
        for text in ("What did you do last night?", "what did you do today", "What have you been up to?",
                     "Hey Pip, what did you do while I was away?", "what did you get up to last night"):
            self.assertTrue(action_log.may_match(text), text)
            self.assertIsNotNone(self.ask(text), text)
        for text in ("what did you do with my keys", "what did you say", "what did I do last night",
                     "what do you do", "what did you dream about"):
            self.assertIsNone(self.ask(text), text)

    def test_last_night_counts_only_the_night(self):
        action_log.record(self.mind, "timer.set", now=self.at(hours=-20))   # yesterday 11:30, not the night
        action_log.record(self.mind, "night.tidy", now=self.at(hours=-6))   # 01:30
        action_log.record(self.mind, "backup.auto", now=self.at(hours=-5))
        action_log.record(self.mind, "repair.audio", now=self.at(hours=-4))
        reply = self.ask("What did you do last night?")
        self.assertEqual(reply.line, "[neutral] Last night I tidied my memory, backed myself up and reset my microphone.")
        self.assertFalse(reply.keep)
        today = self.ask("what did you do today")
        self.assertIn("reset my microphone", today.line)
        self.assertNotIn("timer", today.line)
        # The last day holds four kinds; the lightest is only alluded to.
        self.assertTrue(self.ask("what have you been up to").line.endswith("And a few small things."))

    def test_guests_are_not_told(self):
        action_log.record(self.mind, "forget", 3, now=self.at(hours=-1))
        self.assertEqual(self.ask("what did you do today", guest=True).line, "[neutral] I keep that for my owner.")


if __name__ == "__main__":
    unittest.main()
