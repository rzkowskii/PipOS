"""A misheard one-word brightness step is read back by its sound (mind/short_commands.py).

Evidence label: host test over transcripts from the real laptop. The heard
texts below are what Moonshine tiny wrote on the real laptop (28 Nov 2026,
round L: synthetic speech replayed through the diagnostic mic, the word given
to Moonshine on its own, and the live corpus run). Only text is kept here, no
audio.
"""

import os
import unittest
from pathlib import Path

from mind import device, recognition, short_commands
from mind.short_commands import device_word, sound_key

# Moonshine on the real laptop, "Brighter." said alone (6 runs) and live in a window.
HEARD_BRIGHTER = ("Righter,", "Righter.", "Righter", "Breiter.", "Righter.", "Breitre.")
# "Dimmer." said alone (6 runs) and live in a one-breath window ("Demo.").
HEARD_DIMMER = ("The ma.", "Dima.", "The more.", "The more.", "Demo.")
# Too far from "dimmer" to be taken for it without also taking ordinary words.
MISSED_DIMMER = ("The map.", "The map.")

# Every other transcript of round L, the corpus requests (code and model turns).
LAPTOP_OTHERS = (
    "Open a terminal.", "Close the terminal.", "How is your battery?", "How is your battery", "What time is it?",
    "But what time is it?", "Set a timer for one minute.", "He set a timer for one minute.",
    "Set timer for one minute.", "Set time after one minute.", "So", "You", "It's",
)
# One or two words the owner may well say to Pip on their own.
SHORT_SPEECH = (
    "Thank you.", "Thanks.", "Hi.", "Hello.", "Okay.", "Yes.", "No.", "Never mind.", "Stop.", "Cancel.",
    "Good night.", "Good morning.", "Timer.", "Time.", "Dinner?", "Summer.", "Demon.", "The moon.", "The end.",
    "The menu.", "The matter?", "Theme.", "Dime.", "Retry.", "Retire.", "Righto.", "Righty-o.", "Writer.",
    "Rider.", "Better.", "Brother.", "Bright.", "Bread.", "Pretty.", "Pride.", "Them.", "Dream.", "Damn.",
    "Tommy.", "Timmy.", "Dummy.", "Louder.", "Quieter.", "Darker.", "Slower.", "Speak up.", "Be quiet.",
    "What?", "Pardon?", "Sorry?", "Hey Pip.", "Pip.", "Please.", "Right.", "Alright.", "Great.", "Nice.",
    "I'm here.", "Go on.", "Tell me more.", "The Matrix.", "Demo please now and then",
)
# Dictionary words that share a key with a brightness word: names and rare
# words only (checked against the pronouncing dictionary below). "demo" is
# taken on purpose: Moonshine heard "dimmer" as "Demo." on the laptop.
RARE_COLLISIONS = frozenset((
    "bruyette", "righetti", "righter", "deamer", "demaio", "demar", "demaray", "demaree", "demaria", "demario",
    "demary", "demauro", "demay", "demayo", "demeo", "demeree", "demery", "demeyer", "demma", "demme", "demmer",
    "demo", "demore", "demory", "demuro", "diemer", "dimaio", "dimare", "dimaria", "dimario", "dimauro", "dimeo",
    "dimry", "dimuro", "thieme", "thima",
))


class HeardOnTheLaptop(unittest.TestCase):
    def test_misheard_brighter_becomes_brighter(self):
        for text in HEARD_BRIGHTER:
            self.assertEqual(device_word(text), "brighter", text)

    def test_misheard_dimmer_becomes_dimmer(self):
        for text in HEARD_DIMMER:
            self.assertEqual(device_word(text), "dimmer", text)

    def test_the_far_misreadings_are_left_alone(self):
        for text in MISSED_DIMMER:
            self.assertIsNone(device_word(text), text)

    def test_four_of_six_dimmer_and_six_of_six_brighter_said_alone(self):
        alone_dimmer = ("The map.", "The map.", "The ma.", "Dima.", "The more.", "The more.")
        alone_brighter = ("Righter,", "Righter.", "Righter", "Breiter.", "Righter.", "Brighter.")
        self.assertEqual(sum(device_word(text) == "dimmer" for text in alone_dimmer), 4)
        # "Brighter." itself is a command already; the voice never asks.
        self.assertEqual(sum(device_word(text) == "brighter" for text in alone_brighter[:5]), 5)
        self.assertEqual(device.classify(alone_brighter[5]), ("step", 1))

    def test_what_it_becomes_is_the_brightness_step(self):
        self.assertEqual(device.classify("brighter"), ("step", 1))
        self.assertEqual(device.classify("dimmer"), ("step", -1))

    def test_courtesy_words_around_it_do_not_matter(self):
        self.assertEqual(device_word("The more, please."), "dimmer")
        self.assertEqual(device_word("Hey Pip, righter now"), "brighter")
        self.assertEqual(device_word("Okay Pip, Breiter please"), "brighter")


class LeftAlone(unittest.TestCase):
    def test_other_laptop_transcripts_are_left_alone(self):
        for text in LAPTOP_OTHERS:
            self.assertIsNone(device_word(text), text)

    def test_short_speech_is_left_alone(self):
        for text in SHORT_SPEECH:
            self.assertIsNone(device_word(text), text)

    def test_more_than_two_words_is_never_read(self):
        self.assertIsNone(device_word("the more the merrier"))
        self.assertIsNone(device_word("Righter than you think"))
        self.assertIsNone(device_word("Dima is my friend"))

    def test_nothing_to_read(self):
        for text in ("", "   ", "...", None, 3, "please now"):
            self.assertIsNone(device_word(text), text)

    def test_the_dictionary_holds_no_common_word_that_sounds_like_one(self):
        found = [path for path in (os.environ.get("BEING_CMUDICT"), recognition.MODEL / "cmudict-en-us.dict")
                 if path and Path(path).is_file()]
        if not found:
            self.skipTest("no pronouncing dictionary here (set BEING_CMUDICT to its path)")
        taken = set()
        with open(found[0], encoding="utf-8", errors="replace") as words:
            for line in words:
                word = line.split(" ", 1)[0]
                if "(" in word:
                    continue
                if device_word(word) is not None:
                    taken.add(word)
        self.assertEqual(taken - RARE_COLLISIONS - {"brighter", "dimmer"}, set())


class SoundKey(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(sound_key("brighter"), sound_key("Breiter"))
        self.assertEqual(sound_key("brighter"), sound_key("Breitre"))
        self.assertEqual(sound_key("dimmer"), sound_key("themore"))
        self.assertEqual(sound_key("dimmer"), sound_key("Dima"))
        # Voicing is kept: "timer" is not "dimmer", "pretty" is not "brighter".
        self.assertNotEqual(sound_key("dimmer"), sound_key("timer"))
        self.assertNotEqual(sound_key("brighter"), sound_key("pretty"))
        self.assertEqual(sound_key(""), "")
        self.assertEqual(sound_key("hmm"), "hm")

    def test_the_targets_are_commands(self):
        for command in short_commands.TARGETS:
            self.assertIsNotNone(device.classify(command), command)


if __name__ == "__main__":
    unittest.main()
