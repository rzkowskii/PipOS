"""Requests the recogniser misheard on the real laptop that still mean one command.

Evidence label: host test, text in. Moonshine heard the owner's "What time is
it?" as "Well time is it." (real laptop, 28 Nov 2026, scripted run).
"""

import unittest

from mind import commands


class MisheardTime(unittest.TestCase):
    def test_well_time_is_it_is_the_time(self):
        for text in ("Well time is it.", "well time is it?", "Hey Pip, well time is it"):
            command = commands.parse(text, "pip")
            self.assertEqual(command.kind if command else None, "time", text)

    def test_other_well_sentences_are_not(self):
        for text in ("well, it is time", "well timed", "it is well past time"):
            command = commands.parse(text, "pip")
            self.assertNotEqual(command.kind if command else None, "time", text)


if __name__ == "__main__":
    unittest.main()
