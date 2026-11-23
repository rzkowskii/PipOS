import asyncio
import dataclasses
import unittest
from pathlib import Path

from mind.reply import Confirm, Context, Reply, clean, is_guest


async def _send(*args, **fields):
    return None


def context(**overrides):
    fields = dict(now=None, clock_trusted=True, speaker="Richard", guest=False, state_dir=Path("/nonexistent"),
                  owner="Richard", last_heard=None, last_said=None, body={}, voice={}, send=_send)
    fields.update(overrides)
    return Context(**fields)


class ReplyTypes(unittest.TestCase):
    def test_reply_defaults_store_nothing(self):
        reply = Reply("[neutral] Hello.")
        self.assertFalse(reply.keep)
        self.assertIsNone(reply.confirm)
        self.assertIsNone(reply.action)
        self.assertIsNone(reply.logged)

    def test_types_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Reply("[neutral] x").line = "y"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context().guest = True

    def test_confirm_defaults(self):
        async def yes():
            return Reply("[neutral] Done.")
        confirm = Confirm(yes)
        self.assertEqual(confirm.on_no.line, "[neutral] Okay, I've left it.")
        self.assertEqual(confirm.seconds, 15.0)
        self.assertEqual(asyncio.run(confirm.on_yes()).line, "[neutral] Done.")

    def test_context_fields_match_the_spec(self):
        names = [f.name for f in dataclasses.fields(Context)]
        self.assertEqual(names[:11], ["now", "clock_trusted", "speaker", "guest", "state_dir", "owner",
                                      "last_heard", "last_said", "body", "voice", "send"])
        self.assertEqual(context().name, "pip")

    def test_guest(self):
        self.assertTrue(is_guest(context(guest=True)))
        self.assertTrue(is_guest(context(speaker="guest")))
        self.assertFalse(is_guest(context()))


class Clean(unittest.TestCase):
    def test_address_and_politeness(self):
        self.assertEqual(clean("Hey Pip, could you add milk to my list, please?"), "add milk to my list")
        self.assertEqual(clean("Pip what's 2 plus 2"), "what's 2 plus 2")
        self.assertEqual(clean("ok pip, um, tell me a joke thanks"), "tell me a joke")
        self.assertEqual(clean("Hey Nova, tell me a joke", name="Nova"), "tell me a joke")

    def test_keeps_case_numbers_and_signs(self):
        self.assertEqual(clean("Make a note: Call Mum."), "Make a note: Call Mum")
        self.assertEqual(clean("what's 3.5 times 1,000"), "what's 3.5 times 1000")
        self.assertEqual(clean("15% of 80"), "15% of 80")
        self.assertEqual(clean("17*23"), "17*23")
        self.assertEqual(clean("’tis"), "'tis")

    def test_empty(self):
        self.assertEqual(clean(""), "")
        self.assertEqual(clean(None), "")


class KeepTail(unittest.TestCase):
    def test_keep_tail_takes_off_only_politeness(self):
        self.assertEqual(clean("make a note: the plumber comes for me now please"),
                         "make a note: the plumber comes")
        self.assertEqual(clean("make a note: the plumber comes for me now please", keep_tail=True),
                         "make a note: the plumber comes for me now")


class KeepsNothing(unittest.TestCase):
    def test_policy(self):
        import tempfile
        from mind.reply import keeps_nothing
        from mind.retention import MemoryPolicy, save_policy
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(keeps_nothing(folder))           # no policy file: persistent
            save_policy(Path(folder), MemoryPolicy("session-only"))
            self.assertTrue(keeps_nothing(folder))
            save_policy(Path(folder), MemoryPolicy("explicit-only"))
            self.assertFalse(keeps_nothing(folder))

if __name__ == "__main__":
    unittest.main()
