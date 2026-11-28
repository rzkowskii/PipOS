import json
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from mind.__main__ import load_config
from mind.profile import (
    DEFAULT_TEMPERAMENT,
    PRESETS,
    PROFILE_FILE,
    Personality,
    daily_greeting,
    first_words,
    good_night,
    load_profile,
    personality_greetings,
    random_personality,
    save_profile,
)

FACTORY = Path(__file__).resolve().parent.parent / "config"


class BeingProfile(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.mind_dir = Path(work.name) / "mind"

    def test_factory_identity_and_replies_match_the_existing_being(self):
        cfg, _ = load_config(FACTORY, self.mind_dir)
        identity = (FACTORY / "identity.md").read_text().replace("{name}", "Pip").replace("{owner}", "Richard")
        self.assertEqual(cfg.identity, identity)
        self.assertEqual(personality_greetings(cfg.personality)["hello"][0], "[joy] Hi. Good to hear you.")
        self.assertFalse(self.mind_dir.exists())

    def test_every_factory_setting_reaches_the_mind(self):
        cfg, settings = load_config(FACTORY, self.mind_dir)
        self.assertEqual(cfg.time_zone, "Australia/Sydney")
        self.assertEqual((cfg.top_k, cfg.top_p, cfg.presence_penalty), (20, 0.8, 0))
        self.assertEqual(cfg.reply_shape, settings["reply_shape"])
        for key in ("battery_plea_percent", "battery_plea_minutes", "battery_second_plea_percent",
                    "battery_second_plea_minutes", "battery_critical_percent", "battery_critical_minutes"):
            self.assertEqual(getattr(cfg, key), settings[key], key)

    def test_changed_factory_settings_are_used_and_wrong_types_are_ignored(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        config = Path(work.name) / "config"
        config.mkdir()
        (config / "identity.md").write_text((FACTORY / "identity.md").read_text())
        settings = json.loads((FACTORY / "being.json").read_text())
        settings.update(reply_shape="strict", battery_plea_percent=22, top_k=True, top_p="0.9",
                        presence_penalty=None, time_zone=7)
        (config / "being.json").write_text(json.dumps(settings))
        cfg, _ = load_config(config, self.mind_dir)
        self.assertEqual(cfg.reply_shape, "strict")
        self.assertEqual(cfg.battery_plea_percent, 22)
        self.assertEqual((cfg.top_k, cfg.top_p, cfg.presence_penalty, cfg.time_zone), (None, None, None, ""))
        settings["reply_shape"] = "rambling"
        (config / "being.json").write_text(json.dumps(settings))
        self.assertEqual(load_config(config, self.mind_dir)[0].reply_shape, "open")

    def test_round_trip_keeps_the_wake_phrase_and_applies_personality_to_both_reply_paths(self):
        original = load_profile(FACTORY, self.mind_dir)
        profile = replace(original, name="Écho", voice="awb", rate=0.85, personality=PRESETS["calm"])
        save_profile(self.mind_dir, profile)
        self.assertEqual(load_profile(FACTORY, self.mind_dir), profile)
        self.assertEqual(stat.S_IMODE((self.mind_dir / PROFILE_FILE).stat().st_mode), 0o600)
        cfg, settings = load_config(FACTORY, self.mind_dir)
        self.assertIn("You are Écho", cfg.identity)
        self.assertIn("Keep reactions understated.", cfg.identity)
        self.assertIn("They are true and private", cfg.identity)
        self.assertEqual(cfg.ctx_tokens, settings["ctx_tokens"])
        self.assertEqual(personality_greetings(cfg.personality)["hello"][0], "[neutral] Hello. I'm listening.")
        save_profile(self.mind_dir, replace(profile, voice="future-voice"))
        new_cfg, _ = load_config(FACTORY, self.mind_dir)
        self.assertEqual(new_cfg.identity, cfg.identity)
        self.assertEqual(load_profile(FACTORY, self.mind_dir).voice, "future-voice")

    def test_newer_profile_is_preserved_and_services_can_still_boot(self):
        save_profile(self.mind_dir, load_profile(FACTORY, self.mind_dir))
        path = self.mind_dir / PROFILE_FILE
        unknown = json.dumps({"version": 2})
        path.write_text(unknown)
        with self.assertRaisesRegex(ValueError, "Unsupported profile format"):
            load_profile(FACTORY, self.mind_dir)
        with self.assertWarnsRegex(UserWarning, "Using factory settings"):
            cfg, _ = load_config(FACTORY, self.mind_dir)
        self.assertEqual(cfg.name, "Pip")
        self.assertEqual(path.read_text(), unknown)

    def test_daily_greetings_follow_the_owners_hour_and_vary_by_date(self):
        def at(day, hour):
            return daily_greeting(datetime(2025, 2, day, hour, 0))

        self.assertEqual(daily_greeting(None), "[joy] Hello. Good to see you.")
        self.assertIn("orning", at(20, 7))
        self.assertIn("afternoon", at(20, 14))
        self.assertIn("vening", at(20, 19))
        self.assertRegex(at(20, 23), "up late|Still up")
        self.assertEqual(at(20, 2), at(20, 23), "the small hours are late too")
        self.assertNotEqual(at(20, 7), at(21, 7), "neighbouring days differ")
        self.assertEqual(at(20, 7), at(20, 11), "one greeting for the whole morning")
        for hour in range(24):
            line = at(20, hour)
            self.assertRegex(line, r"^\[(joy|curiosity)\] ")
            self.assertLessEqual(len(line.split()), 10)

    def test_good_night_and_first_words(self):
        self.assertEqual(good_night(None), "[sleepy] Okay. Good night.")
        self.assertEqual(good_night(datetime(2025, 2, 20, 22, 30)), "[sleepy] Okay. Good night.")
        self.assertEqual(good_night(datetime(2025, 2, 20, 13, 0)), "[sleepy] Okay. I'll rest my eyes for a while.")
        self.assertEqual(first_words("Fred"), "[joy] Oh. Hello. I'm Fred. You're the very first person I've met.")

    def test_greetings_show_on_the_face_and_keep_their_spoken_first_lines(self):
        # The lab's spoken tests depend on these first lines.
        self.assertEqual(personality_greetings(PRESETS["warm"])["check_in"][0],
                         "[joy] I'm doing well, thanks. How about you?")
        self.assertEqual(personality_greetings(PRESETS["calm"])["hello"][0], "[neutral] Hello. I'm listening.")
        for personality in (*PRESETS.values(), Personality(0, 0, 0, 0, 0), Personality(2, 2, 2, 2, 2)):
            for replies in personality_greetings(personality).values():
                for reply in replies:
                    self.assertRegex(reply, r"^\[(neutral|joy|curiosity)\] \S")
        self.assertTrue(personality_greetings(PRESETS["playful"])["hello"][1].startswith("[curiosity]"))

    def test_the_persona_prompt_stays_short(self):
        identity = (FACTORY / "identity.md").read_text()
        # It is read on a dual-core 2012 CPU before the first reply (once,
        # then cached). The honesty line about what it is earns its place.
        self.assertLess(len(identity), 1000)
        self.assertIn("aiming to be more", identity)
        self.assertIn("Never claim to be conscious", identity)
        self.assertIn(DEFAULT_TEMPERAMENT, identity)
        self.assertIn("never servile", identity)
        # Every personality keeps the stance; only the default asks questions.
        self.assertNotIn("you ask questions", identity.replace(DEFAULT_TEMPERAMENT, "").lower())

    def test_random_roll_chooses_every_trait(self):
        with patch("mind.profile.secrets.randbelow", side_effect=[0, 2, 2, 0, 1]) as draw:
            result = random_personality()
        self.assertEqual(result, Personality(0, 2, 2, 0, 1))
        self.assertEqual([call.args for call in draw.call_args_list], [(3,)] * 5)
        self.assertNotIn(result, PRESETS.values())
