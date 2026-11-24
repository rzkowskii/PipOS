"""How Pip says its body is: only measured numbers, honest when missing."""

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mind import body_talk

# The first real boot of the MacBookPro9,2 (23 November).
LAPTOP = {"health_pct": 81, "cycles": 71, "temp": 48.0, "battery": 79, "on_ac": True, "charging": True}


def ctx(body=None, voice=None, state_dir="/", sent=None):
    async def send(topic, **fields):
        if sent is not None:
            sent.append((topic, fields))
    return SimpleNamespace(body=body or {}, voice=voice or {}, state_dir=Path(state_dir), send=send, guest=False)


def ask(text, **options):
    return asyncio.run(body_talk.handle(text, ctx(**options)))


def words(line):
    return len(re.sub(r"^\[[a-z]+\]\s*", "", line).split())


class Phrases(unittest.TestCase):
    def test_every_spec_phrase_reaches_its_answer(self):
        for text, kind in (
            ("How's your battery health?", "health"), ("what is your battery health", "health"),
            ("How many charge cycles has your battery done?", "health"), ("how worn is your battery", "health"),
            ("How warm are you?", "warmth"), ("How hot are you?", "warmth"), ("Are you overheating?", "warmth"),
            ("What's your temperature?", "warmth"),
            ("Why is your fan loud?", "fan"), ("why is your fan so noisy", "fan"), ("how fast is your fan going", "fan"),
            ("How much space do you have?", "disk"), ("how full is your disk", "disk"),
            ("are you running out of space", "disk"),
            ("How much memory are you using?", "memory"), ("how much ram do you have", "memory"),
            ("How long have you been awake?", "uptime"), ("how long have you been running", "uptime"),
            ("How well can you hear me?", "hearing"), ("Check your hearing", "hearing"),
            ("Hey Pip, can you hear me clearly?", "hearing"),
        ):
            self.assertEqual(body_talk.classify(text), kind, text)
            self.assertTrue(body_talk.may_match(text), text)

    def test_near_misses_pass_on(self):
        for text in ("How's your battery?", "are you charging", "how much do you remember", "what do you remember",
                     "how hot is it in London", "is it warm outside", "open the monitor", "how are you",
                     "can you hear me", "tell me about fans of football", "how long is the film",
                     "how much memory does a phone have", "the fan is loud"):
            self.assertIsNone(body_talk.classify(text), text)
            self.assertIsNone(ask(text), text)


class Battery(unittest.TestCase):
    def test_the_real_laptops_health_in_the_specs_words(self):
        reply = ask("How's your battery health?", body=LAPTOP)
        self.assertEqual(reply.line, "[neutral] My battery holds 81 percent of what it did when new, after 71 charge cycles.")
        self.assertFalse(reply.keep)

    def test_the_cap_is_mentioned_only_when_it_really_holds(self):
        held = ask("battery health", body={**LAPTOP, "charge_limit": 80, "cap_honoured": True})
        self.assertTrue(held.line.endswith("I'm keeping it at 80 percent."), held.line)
        for unsure in (None, False):
            line = ask("battery health", body={**LAPTOP, "charge_limit": 80, "cap_honoured": unsure}).line
            self.assertNotIn("keeping it", line)
        self.assertNotIn("keeping it", ask("battery health", body={**LAPTOP, "cap_honoured": True}).line,
                         "no cap chosen, nothing to claim")

    def test_unknown_and_worn(self):
        self.assertEqual(ask("battery health").line, "[neutral] I can't read my battery's health right now.")
        self.assertEqual(ask("battery health", body={"health_pct": 81}).line,
                         "[neutral] My battery holds 81 percent of what it did when new.")
        worn = ask("battery health", body={"health_pct": 52, "cycles": 1100}).line
        self.assertTrue(worn.startswith("[concern]") and "1,100 charge cycles" in worn and "new one" in worn, worn)
        self.assertTrue(ask("battery health", body={"health_pct": 99, "cycles": 1}).line.endswith(
            "after 1 charge cycle."))
        for junk in ("81", True, float("nan"), 400, -3):
            self.assertIn("can't read", ask("battery health", body={"health_pct": junk}).line)


class Heat(unittest.TestCase):
    def test_warmth_follows_the_package_temperature(self):
        self.assertEqual(ask("How warm are you?", body=LAPTOP).line, "[neutral] I'm cool: 48 degrees inside.")
        self.assertIn("warm, 68 degrees", ask("how hot are you", body={"package_c": 68.2, "temp": 40}).line)
        hot = ask("how hot are you", body={"package_c": 82}).line
        self.assertTrue(hot.startswith("[concern]") and "fan is working" in hot, hot)
        slowed = ask("how hot are you", body={"package_c": 82, "mode": "act", "max_perf_pct": 70}).line
        self.assertIn("slowing down", slowed)
        observing = ask("how hot are you", body={"package_c": 82, "mode": "observe", "max_perf_pct": 70}).line
        self.assertNotIn("slowing", observing, "observe mode changes nothing, so it claims nothing")
        self.assertIn("very hot", ask("are you overheating", body={"temp": 94}).line)
        self.assertEqual(ask("How warm are you?").line, "[neutral] I can't read my temperature right now.")

    def test_fan_reasons_are_true_ones(self):
        self.assertEqual(ask("Why is your fan loud?").line, "[neutral] I can't read my fan right now.")
        quiet = ask("why is your fan loud", body={"fan_rpm": 1999, "fan_factory_min": 2000, "temp": 48}).line
        self.assertEqual(quiet, "[neutral] My fan is at its quiet minimum, about 2,000 rpm.")
        held = ask("why is your fan loud", body={"fan_rpm": 3998, "fan_factory_min": 2000, "fan_floor": 4000,
                                                 "mode": "act", "package_c": 66}).line
        self.assertIn("holding it faster", held)
        wanted = ask("why is your fan loud", body={"fan_rpm": 2001, "fan_factory_min": 2000, "fan_floor": 4000,
                                                   "mode": "observe", "package_c": 50}).line
        self.assertNotIn("holding", wanted, "a floor only wanted in observe mode is not being held")
        hot = ask("why is your fan loud", body={"fan_rpm": 5210, "fan_factory_min": 2000, "package_c": 78}).line
        self.assertEqual(hot, "[neutral] It's at 5,210 rpm because I'm 78 degrees inside. It will slow when I cool.")


class System(unittest.TestCase):
    def test_disk_memory_and_uptime_from_the_kernel(self):
        line = body_talk.disk_line("/mind", usage=lambda path: (120e9, 79e9, 41e9))
        self.assertEqual(line, "[neutral] I have 41 gigabytes free on my memory disk, out of 120 gigabytes.")
        low = body_talk.disk_line("/mind", usage=lambda path: (120e9, 114e9, 6e9))
        self.assertTrue(low.startswith("[concern]") and "6.0 gigabytes" not in low and "6 gigabytes" in low, low)
        self.assertIn("500 megabytes", body_talk.disk_line("/", usage=lambda path: (8e9, 7.5e9, 5e8)))

        def broken(path):
            raise OSError("gone")
        self.assertEqual(body_talk.disk_line("/", usage=broken), "[neutral] I can't read my disk right now.")
        with tempfile.TemporaryDirectory() as proc:
            proc = Path(proc)
            (proc / "meminfo").write_text("MemTotal:       16301056 kB\nMemFree:  1 kB\nMemAvailable:   14100000 kB\n")
            (proc / "uptime").write_text("11520.31 22000.10\n")
            self.assertEqual(body_talk.memory_line(proc),
                             "[neutral] I'm using 2.3 gigabytes of my 16 gigabytes of working memory.")
            self.assertEqual(body_talk.uptime_line(proc),
                             "[neutral] I've been running for three hours and 12 minutes, since I last started.")
            (proc / "uptime").write_text("200000.0 1\n")
            self.assertIn("two days and seven hours", body_talk.uptime_line(proc))
            (proc / "uptime").write_text("42.0 1\n")
            self.assertIn("less than a minute", body_talk.uptime_line(proc))
            (proc / "meminfo").write_text("garbage\n")
            self.assertIn("can't read", body_talk.memory_line(proc))
        self.assertIn("can't tell", body_talk.uptime_line(Path("/nonexistent-proc")))
        self.assertIn("free on my memory disk", ask("how much space do you have", state_dir=tempfile.gettempdir()).line)


class Hearing(unittest.TestCase):
    def test_hearing_from_measured_levels(self):
        sent = []
        clear = asyncio.run(body_talk.handle("How well can you hear me?",
                                             ctx(voice={"floor_dbfs": -62, "speech_peak_dbfs": -18}, sent=sent)))
        self.assertEqual(clear.line, "[joy] The room is quiet and your voice reaches me clearly.")
        self.assertEqual(sent, [("voice.level_request", {})], "fresh levels are asked for")
        self.assertIn("faintly", ask("check your hearing", voice={"floor_dbfs": -60, "speech_peak_dbfs": -48}).line)
        self.assertIn("faintly", ask("check your hearing", voice={"floor_dbfs": -45, "speech_peak_dbfs": -40}).line)
        self.assertIn("noisy", ask("check your hearing", voice={"floor_dbfs": -38}).line)
        self.assertIn("haven't measured your voice", ask("check your hearing", voice={"floor_dbfs": -70}).line)
        silent = ask("how well can you hear me", voice={"floor_dbfs": -120, "speech_peak_dbfs": -120}).line
        self.assertTrue(silent.startswith("[concern]"), silent)
        self.assertIn("type to me: press Mission Control, or fn and F2, then 6.", silent)
        self.assertIn("haven't measured my hearing", ask("how well can you hear me").line)
        self.assertIn("not perfectly", ask("how well can you hear me",
                                           voice={"floor_dbfs": -60, "speech_peak_dbfs": -38}).line)

    def test_a_lost_bus_does_not_lose_the_answer(self):
        async def broken(topic, **fields):
            raise ConnectionError("bus gone")
        reply = asyncio.run(body_talk.handle("check your hearing", SimpleNamespace(voice={"floor_dbfs": -62},
                                                                                     send=broken, body={})))
        self.assertIn("quiet", reply.line)


class Lines(unittest.TestCase):
    def test_every_line_is_tagged_and_short(self):
        lines = [
            body_talk.health_line({**LAPTOP, "charge_limit": 80, "cap_honoured": True}), body_talk.health_line({}),
            body_talk.health_line({"health_pct": 40, "cycles": 2000}),
            *(body_talk.warmth_line({"package_c": t, "mode": "act", "max_perf_pct": 50}) for t in (40, 60, 80, 95)),
            *(body_talk.fan_line(b) for b in ({}, {"fan_rpm": 0, "temp": 40}, {"fan_rpm": 3000, "temp": 60},
                                              {"fan_rpm": 3000, "fan_floor": 3000, "fan_factory_min": 2000,
                                               "mode": "act"})),
            *(body_talk.hearing_line(v) for v in ({}, {"floor_dbfs": -100}, {"floor_dbfs": -30},
                                                  {"floor_dbfs": -60, "speech_peak_dbfs": -20})),
        ]
        for line in lines:
            self.assertRegex(line, r"^\[(neutral|joy|concern)\] \S")
            self.assertLessEqual(words(line), 25, line)


if __name__ == "__main__":
    unittest.main()
