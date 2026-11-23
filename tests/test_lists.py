import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from mind import lists
from mind.reply import Context


async def _send(*args, **fields):
    return None


class ListsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        lists._LAST.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def ctx(self, **overrides):
        fields = dict(now=None, clock_trusted=True, speaker="Richard", guest=False, state_dir=self.dir,
                      owner="Richard", last_heard=None, last_said=None, body={}, voice={}, send=_send)
        fields.update(overrides)
        return Context(**fields)

    def say(self, text, **overrides):
        return asyncio.run(lists.handle(text, self.ctx(**overrides)))

    def line(self, text, **overrides):
        reply = self.say(text, **overrides)
        return reply.line if reply else None

    def test_spec_phrases(self):
        self.assertEqual(self.line("Add milk to my shopping list"), "[joy] I've added milk to your shopping list.")
        self.assertEqual(self.line("Add eggs and bread to the shopping list"),
                         "[joy] I've added eggs and bread to your shopping list.")
        self.assertEqual(self.line("Put 'call the bank' on my to-do list"),
                         "[joy] I've added call the bank to your to-do list.")
        self.assertEqual(self.line("What's on my shopping list?"),
                         "[neutral] Your shopping list has milk, eggs and bread.")
        self.assertEqual(self.line("Take milk off my shopping list"),
                         "[neutral] Okay, I've taken milk off your shopping list.")
        self.assertEqual(self.line("Cross off eggs"), "[neutral] Okay, I've taken eggs off your shopping list.")
        self.assertEqual(self.line("What lists do you have?"), "[neutral] You have two lists: shopping and to-do.")
        clear = self.say("Clear my shopping list")
        self.assertEqual(clear.line, "[neutral] Clear the one thing off your shopping list? Say yes or no.")
        self.assertIsNotNone(clear.confirm)
        self.assertEqual(self.line("Make a note: the plumber comes Thursday"), "[joy] Noted: the plumber comes Thursday.")
        self.assertEqual(self.line("Note that the spare key is in the blue pot"),
                         "[joy] Noted: the spare key is in the blue pot.")
        self.assertEqual(self.line("Read my notes"), "[neutral] You have two notes. Newest first: the spare key is in "
                                                     "the blue pot; the plumber comes Thursday.")
        self.assertEqual(self.line("Delete my last note"),
                         "[neutral] I've deleted your last note: the spare key is in the blue pot.")
        self.assertEqual(self.line("Read my notes"), "[neutral] You have one note: the plumber comes Thursday.")

    def test_nothing_is_kept_in_memory_and_actions_are_logged(self):
        reply = self.say("add milk to my shopping list")
        self.assertFalse(reply.keep)
        self.assertEqual(reply.logged, "list.add")
        self.assertEqual(self.say("cross off milk").logged, "list.remove")
        self.assertEqual(self.say("make a note: buy stamps").logged, "note.add")
        self.assertEqual(self.say("delete my last note").logged, "note.delete")
        self.assertIsNone(self.say("what's on my shopping list").logged)

    def test_clear_confirms(self):
        self.say("add milk, eggs and tea to my shopping list")
        reply = self.say("clear my shopping list")
        self.assertEqual(reply.line, "[neutral] Clear all three things off your shopping list? Say yes or no.")
        self.assertEqual(lists.Lists(self.dir).items("shopping"), ["milk", "eggs", "tea"])   # nothing yet
        self.assertEqual(reply.confirm.on_no.line, "[neutral] Okay, I've left your shopping list as it is.")
        self.assertIs(reply.confirm.strict, True, "only a plain yes clears a list")
        done = asyncio.run(reply.confirm.on_yes())
        self.assertEqual(done.line, "[neutral] Done. Your shopping list is empty.")
        self.assertEqual(done.logged, "list.clear")
        self.assertEqual(lists.Lists(self.dir).items("shopping"), [])
        self.assertEqual(self.line("what's on my shopping list"), "[neutral] Your shopping list is empty.")
        self.assertEqual(self.line("clear my shopping list"), "[neutral] Your shopping list is already empty.")

    def test_delete_whole_list_confirms(self):
        self.say("add rake to my garden list")
        reply = self.say("delete my garden list")
        self.assertIn("Say yes or no", reply.line)
        self.assertEqual(asyncio.run(reply.confirm.on_yes()).line, "[neutral] Done. I've deleted your garden list.")
        self.assertEqual(lists.Lists(self.dir).names(), [])

    def test_read_back_five_then_more(self):
        self.say("add a, b, c, d, e, f, g and h to my shopping list")
        self.assertEqual(self.line("what's on my shopping list"),
                         "[neutral] Your shopping list has eight things: a, b, c, d, e, and three more.")
        self.assertEqual(lists.read_back(["x", "y"]), "x and y")
        self.assertEqual(lists.read_back([f"i{n}" for n in range(6)]), "i0, i1, i2, i3, i4, and one more")

    def test_names_and_duplicates(self):
        self.say("add milk to my groceries list")
        self.assertEqual(self.line("add Milk to the shopping list"), "[neutral] Milk is already on your shopping list.")
        self.assertEqual(self.line("add the eggs and milk to my shopping list"),
                         "[joy] I've added eggs to your shopping list. Milk was already there.")
        self.assertEqual(self.line("add egg to my shopping list"), "[neutral] Egg is already on your shopping list.")
        self.say("add call the bank to my to do list")
        self.assertEqual(lists.Lists(self.dir).names(), ["shopping", "to-do"])
        self.assertEqual(lists.list_name("my todo"), "to-do")
        self.assertEqual(lists.list_name("the things to do"), "to-do")

    def test_which_list_when_unclear(self):
        self.assertEqual(self.line("what's on my list"), "[neutral] You don't have any lists yet.")
        self.say("add milk to my shopping list")
        self.assertEqual(self.line("what's on my list"), "[neutral] Your shopping list has milk.")
        self.say("add rake to my garden list")
        self.assertEqual(self.line("what's on my list"), "[neutral] Which list? You have the shopping and garden lists.")
        self.assertEqual(self.line("add tea to my list"), "[neutral] Which list? You have the shopping and garden lists.")

    def test_remove_cases(self):
        self.assertEqual(self.line("cross off milk"), "[neutral] I can't find milk on any of your lists.")
        self.say("add milk to my shopping list")
        self.say("add milk to my cafe list")
        self.assertEqual(self.line("cross off milk"),
                         "[neutral] Milk is on your shopping and cafe lists. Which one should I take it off?")
        self.assertEqual(self.line("cross milk off my cafe list"),
                         "[neutral] Okay, I've taken milk off your cafe list. That was the last thing on it.")
        self.assertEqual(self.line("remove bread from my shopping list"), "[neutral] Bread isn't on your shopping list.")
        self.assertEqual(self.line("take tea off my holiday list"), "[neutral] You don't have a holiday list.")

    def test_take_it_off(self):
        self.say("add milk and bread to my shopping list")
        self.assertEqual(self.line("actually take that off"),
                         "[neutral] Okay, I've taken milk and bread off your shopping list.")
        self.assertEqual(self.line("take it off"), "[neutral] I'm not sure what to take off. Say the item and the list.")

    def test_check(self):
        self.say("add milk to my shopping list")
        self.assertEqual(self.line("is milk on my shopping list"), "[neutral] Yes, milk is on your shopping list.")
        self.assertEqual(self.line("is tea on my shopping list"), "[neutral] No, tea isn't on your shopping list.")

    def test_notes(self):
        self.assertEqual(self.line("read my notes"), "[neutral] You don't have any notes.")
        self.assertEqual(self.line("delete my last note"), "[neutral] You don't have any notes to delete.")
        for n in range(5):
            self.say(f"make a note that thing {n} matters")
        self.assertEqual(self.line("what notes do I have"),
                         "[neutral] You have five notes. Newest first: thing 4 matters; thing 3 matters; "
                         "thing 2 matters; and two more.")
        reply = self.say("delete all my notes")
        self.assertIn("Delete all five of your notes?", reply.line)
        self.assertIs(reply.confirm.strict, True, "only a plain yes deletes every note")
        self.assertEqual(asyncio.run(reply.confirm.on_yes()).line, "[neutral] Done. I've deleted five notes.")
        self.assertEqual(lists.Lists(self.dir).notes(), [])
        self.assertIsNone(self.say("make a note"))      # nothing to note: not ours
        self.assertEqual(self.line("jot down the gas bill is due"), "[joy] Noted: the gas bill is due.")

    def test_guests_are_refused(self):
        self.say("add milk to my shopping list")
        for said in ("what's on my shopping list", "add beer to my shopping list", "clear my shopping list",
                     "read my notes", "make a note: hello there", "what lists do you have"):
            with self.subTest(said=said):
                self.assertEqual(self.line(said, guest=True, speaker="guest"), lists.GUEST_LINE)
                self.assertEqual(self.line(said, speaker="guest"), lists.GUEST_LINE)
        self.assertEqual(lists.Lists(self.dir).items("shopping"), ["milk"])
        self.assertEqual(lists.Lists(self.dir).notes(), [])

    def test_restart(self):
        self.say("add milk to my shopping list")
        self.say("make a note: the plumber comes Thursday")
        # A new process: nothing is held in RAM, everything comes from the file.
        lists._LAST.clear()
        store = lists.Lists(self.dir)
        self.assertEqual(store.items("shopping"), ["milk"])
        self.assertEqual(store.notes(), ["the plumber comes Thursday"])
        self.assertEqual(self.line("what's on my shopping list"), "[neutral] Your shopping list has milk.")

    def test_file_mode_and_pragmas(self):
        self.say("add milk to my shopping list")
        path = self.dir / "lists.db"
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with lists._connect(self.dir) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(db.execute("PRAGMA secure_delete").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        db.close()

    def test_erase_all(self):
        self.say("add milk to my shopping list")
        self.say("make a note: secret canary words")
        lists.erase_all(self.dir)
        for suffix in ("", "-wal", "-shm"):
            self.assertFalse((self.dir / f"lists.db{suffix}").exists())
        self.assertEqual(lists.Lists(self.dir).names(), [])
        self.assertEqual(lists.Lists(self.dir).notes(), [])
        self.assertEqual(self.line("what lists do you have"), "[neutral] You don't have any lists yet.")
        lists.erase_all(self.dir)       # twice is fine
        self.assertFalse(any(self.dir.iterdir()))

    def test_secure_delete_overwrites_removed_items(self):
        self.say("add zebracanary to my shopping list")
        self.say("cross off zebracanary")
        with lists._connect(self.dir) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        data = b"".join(p.read_bytes() for p in self.dir.iterdir() if p.name.startswith("lists.db"))
        self.assertNotIn(b"zebracanary", data)

    def test_not_ours(self):
        for said in ("what time is it", "remember that my orchid is called Silverleaf", "tell me a joke",
                     "what's 2 plus 2", "turn it off", "take a look at this", "show me your face"):
            with self.subTest(said=said):
                self.assertIsNone(self.say(said))
        self.assertFalse(lists.may_match("how are you"))
        self.assertTrue(lists.may_match("cross off milk"))

    def test_split(self):
        self.assertEqual(lists.split_items("eggs, bread and butter"), ["eggs", "bread", "butter"])
        self.assertEqual(lists.split_items("'salt and pepper'"), ["salt and pepper"])
        self.assertEqual(lists.split_items("some milk"), ["milk"])

    def test_broken_file_changes_nothing(self):
        (self.dir / "lists.db").write_bytes(b"not a database at all" * 100)
        self.assertEqual(self.line("add milk to my shopping list"),
                         "[concern] I couldn't open my lists just now. Nothing was changed.")


class Review(ListsTest):
    def test_gate_stays_narrow(self):
        # The gate also decides whether a follow-up is meant for Pip.
        for said in ("turn the lights off", "I'm off to work", "the show is off tonight", "how are you"):
            with self.subTest(said=said):
                self.assertFalse(lists.may_match(said))
        for said in ("cross off milk", "tick bread off", "take milk off my shopping list", "take that off",
                     "make a note: call Mum", "what lists do you have"):
            with self.subTest(said=said):
                self.assertTrue(lists.may_match(said))

    def test_take_the_day_off_is_not_a_list(self):
        self.say("add milk to my shopping list")
        self.assertIsNone(self.say("take the day off"))
        self.assertIsNone(self.say("knock it off"))
        self.assertEqual(self.line("take milk off"), "[neutral] Okay, I've taken milk off your shopping list. "
                                                     "That was the last thing on it.")
        # The list verbs stay honest when the item is not there.
        self.assertEqual(self.line("cross off bread"), "[neutral] I can't find bread on any of your lists.")

    def test_a_note_keeps_its_words(self):
        self.assertEqual(self.line("make a note: the plumber comes for me now, please"),
                         "[joy] Noted: the plumber comes for me now.")

    def test_take_that_off_only_soon_after(self):
        self.say("add milk to my shopping list")
        lists._LAST["at"] -= lists.UNDO_WITHIN + 1
        self.assertEqual(self.line("take that off"),
                         "[neutral] I'm not sure what to take off. Say the item and the list.")
        self.assertEqual(lists.Lists(self.dir).items("shopping"), ["milk"])

    def test_session_only_keeps_nothing(self):
        from mind.retention import MemoryPolicy, save_policy
        save_policy(self.dir, MemoryPolicy("session-only"))
        for said in ("add milk to my shopping list", "what's on my shopping list", "make a note: call the bank",
                     "read my notes", "what lists do you have"):
            with self.subTest(said=said):
                self.assertEqual(self.line(said), lists.SESSION_ONLY_LINE)
        self.assertFalse(lists.database(self.dir).exists())

if __name__ == "__main__":
    unittest.main()
