"""Look preferences through Setup, the voice turn and the persisted state."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import commands, look, setup
from tests.test_mind import MindCase, said


class LookStoreTests(unittest.TestCase):
    def test_defaults_and_bad_fields_are_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            store = look.Store(temp)
            self.assertEqual(store.read(), look.Look())
            store.path.write_text('{"glow":"warm","shape":"round","strength":true,"size":"tiny","brows":1,"mouth":true}')
            self.assertEqual(store.read(), look.Look(shape="round", mouth=True))
            store.path.write_text('{"glow":')
            self.assertEqual(store.read(), look.Look())

    def test_two_settings_writers_preserve_each_other_and_mirror(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mirror = root / "run" / "look.json"
            options, voice = look.Store(root, mirror=mirror), look.Store(root, mirror=mirror)
            options.set("shape", "sharp")
            voice.set("glow", "blue")
            options.set("brows", True)
            active = voice.set("strength", 1.4)
            self.assertEqual(active, look.Look("blue", "sharp", 1.4, brows=True, revision=4))
            self.assertEqual(json.loads(mirror.read_text()), active.payload())
            self.assertEqual(look.Store(root).read(), active)
            with self.assertRaises(ValueError):
                voice.set("glow", "sepia")
            with self.assertRaises(ValueError):
                voice.set("mouth", "yes")
            self.assertEqual(options.set("strength", 1.4), active)
            self.assertEqual(options.refresh(), active)

    def test_setup_saves_each_independent_control(self):
        with tempfile.TemporaryDirectory() as temp:
            keys = ("glow", "rose", "shape", "round", "size", "medium", "brows", True, "mouth", False, None)
            with patch.object(setup, "choose", side_effect=keys) as choose:
                setup.look_page(temp)
                self.assertIn("cyan eyes", choose.call_args_list[0].kwargs["intro"][0])
                self.assertEqual(tuple(choose.call_args_list[1].args[1]), look.EYE_COLOURS)
            saved = look.Store(temp).read()
            self.assertEqual(saved, look.Look("rose", "round", 1.0, "medium", True, False, 4))
            with patch.object(setup, "choose", side_effect=("mouth", True, None)):
                setup.look_page(temp, dry_run=True)
            self.assertFalse(look.Store(temp).read().mouth)

    def test_a_mention_is_not_a_look_command(self):
        for text in ("add red light bulbs to my shopping list", "why are your eyes blue",
                     "I can't make your eyes blue", "make your face larger in the photo", "I like your mouth"):
            self.assertIsNone(commands.parse(text, "Pip"), text)


class LookVoiceTests(MindCase):
    async def test_clock_and_system_widgets_are_separate_owner_preferences(self):
        self.assertFalse(look.Store(self.root).read().clock_widget)
        await self.hear("Hey Pip, show the clock widget.", 1)
        await self.hear("Show the system widget.", 2)
        await self.hear("Hide the clock widget.", 3)
        saved = look.Store(self.root).read()
        self.assertFalse(saved.clock_widget)
        self.assertTrue(saved.system_widget)
        self.assertEqual(self.bus.of("screen.prefs")[-1], {"t": "screen.prefs", **saved.payload()})
        await self.hear("Start guest mode.", 4)
        await self.hear("Hide the system widget.", 5)
        self.assertEqual(look.Store(self.root).read(), saved)

    async def test_spoken_look_uses_code_and_reports_the_face_echo(self):
        await self.hear("Hey Pip, make your eyes blue.", 1)
        request = await self.bus.wait("screen.prefs")
        self.assertEqual(request["glow"], "blue")
        self.assertEqual(request["revision"], 1)
        await self.hear("Make your eyes square.", 2)
        await self.hear("Make your face smaller.", 3)
        await self.hear("Set your glow to one hundred and forty percent.", 4)
        await self.hear("Show your eyebrows.", 5)
        await self.hear("Turn your mouth on.", 6)
        await self.hear("Hide your brows.", 7)
        active = look.Look("blue", "sharp", 1.4, "medium", False, True, 7)
        self.assertEqual(look.Store(self.root).read(), active)
        self.assertTrue(all(not entry.get("messages") for entry in self.cortex_server.chats))

        self.bus.push("screen.prefs_state", **active.payload())
        await self.hear("What is your current look?", 8)
        self.assertIn("I have blue eyes", said(self.bus))
        self.assertIn("my mouth is on", said(self.bus))
        self.bus.push("organ.down", name="face")
        self.bus.push("organ.up", name="face")
        fresh = await self.bus.wait("screen.prefs", 8)
        self.assertEqual(fresh, {"t": "screen.prefs", **active.payload()})

    async def test_unknown_colour_and_guest_changes_are_refused_without_a_write(self):
        await self.hear("Change your eye colour to sepia.", 1)
        self.assertIn("cyan, blue, teal, mint, violet, green, amber, red, rose, yellow, or white", said(self.bus))
        await self.hear("Start guest mode.", 2)
        await self.hear("Make your eyes blue.", 3)
        self.assertIn("change my saved look outside guest mode", said(self.bus))
        self.assertFalse((self.root / "look.json").exists())

    async def test_warm_colour_aliases_and_random_choice_are_saved(self):
        await self.hear("Change your eye colour to red.", 1)
        self.assertEqual(look.Store(self.root).read().glow, "red")
        self.assertEqual(self.bus.of("screen.prefs")[-1]["glow"], "red")
        await self.hear("Make your eyes orange.", 2)
        self.assertEqual(look.Store(self.root).read().glow, "amber")
        await self.hear("Pick a random colour.", 3)
        selected = look.Store(self.root).read().glow
        self.assertIn(selected, look.EYE_COLOURS)
        self.assertNotEqual(selected, "amber")

    async def test_emotes_eye_details_and_mood_colour_are_independent_voice_preferences(self):
        await self.hear("Hey Pip, show your emotes.", 1)
        await self.hear("Turn your eye details on.", 2)
        await self.hear("Turn mood color on.", 3)
        await self.hear("Hide your emotes.", 4)
        saved = look.Store(self.root).read()
        self.assertFalse(saved.effects)
        self.assertTrue(saved.eye_details)
        self.assertTrue(saved.mood_colour)
        self.assertEqual(self.bus.of("screen.prefs")[-1], {"t": "screen.prefs", **saved.payload()})
        self.assertIn("hide my emotes", said(self.bus))
