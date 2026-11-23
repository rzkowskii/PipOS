"""The day record: counts and topic words, never sentences, and forgetful."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mind import diary
from mind.memory import Memory

ZONE = timezone(timedelta(hours=11))
MONDAY = date(2026, 11, 23)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE).timestamp()


class Record(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = Memory(self.root, embedder=None)
        self.memory.clock = lambda reading=None: (None, self._now)
        self.ids = {}
        talk = [
            (9, 0, "The orchids need watering before the plumber comes"),
            (9, 2, "I think the plumber is late again"),
            (9, 4, "Japan is going to be amazing next month"),
            (14, 0, "My orchids are flowering, the plumber fixed the tap"),
            (14, 3, "I booked the hotel in Japan today"),
            (20, 0, "Mum rang about the orchids"),
        ]
        for hour, minute, text in talk:
            self._now = at(MONDAY, hour, minute)
            self.ids[text] = self.memory.log("heard", text, "Richard")
            self.memory.log("said", "That sounds good.", "Pip")
        self._now = at(MONDAY, 21)
        self.memory.log("heard", "remember that my orchid is called Silverleaf", "Richard", command=True)
        self.memory.log("heard", "My guest says hello", "Sam")
        log = self.root / "log"
        log.mkdir()
        entries = [("reminder.rang", 2), ("timer.set", 1), ("repair.audio", 1), ("dream", 1)]
        (log / "actions.jsonl").write_text("".join(
            json.dumps({"ts": int(at(MONDAY, 10 + i)), "kind": kind, "count": count}) + "\n"
            for i, (kind, count) in enumerate(entries)))

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def test_counts_and_the_top_three_owner_topics(self):
        record = diary.collect(self.memory.db, "Richard", MONDAY, zone=ZONE, state_dir=self.root)
        self.assertEqual(record["conversations"], 3)
        self.assertEqual(record["owner_lines"], 6)
        self.assertEqual([topic for topic, _ in record["topics"]], ["orchids", "plumber", "Japan"])
        self.assertEqual(record["reminders_done"], 2)
        self.assertEqual(record["timers"], 1)
        self.assertEqual(record["body_events"], {"repair.audio": 1})

    def test_the_stored_record_has_no_sentences(self):
        diary.build_day(self.memory, "Richard", MONDAY, zone=ZONE, state_dir=self.root)
        stored = [row for row in self.memory.db.execute("SELECT * FROM diary_days")]
        topics = [row[0] for row in self.memory.db.execute("SELECT topic FROM diary_topics")]
        self.assertEqual(len(stored), 1)
        self.assertEqual(topics, ["orchids", "plumber", "Japan"])
        text = json.dumps(stored) + json.dumps(topics)
        self.assertNotIn("watering", text)
        self.assertNotIn("hotel", text)

    def test_it_answers_what_we_talked_about(self):
        diary.build_day(self.memory, "Richard", MONDAY, zone=ZONE, state_dir=self.root)
        tuesday = datetime(2026, 11, 24, 8, tzinfo=ZONE)
        line = diary.answer(self.memory.db, "Richard", "What did we talk about yesterday?", now=tuesday, zone=ZONE,
                            state_dir=self.root)
        self.assertEqual(line, "[neutral] Yesterday we talked three times. You mentioned orchids, plumber and "
                               "Japan. I rang two reminders and set a timer.")
        self.assertLessEqual(len(line.split()) - 1, 25)
        self.assertEqual(diary.answer(self.memory.db, "Richard", "what did we do on Monday", now=tuesday, zone=ZONE),
                         line.replace("Yesterday", "On Monday"))
        self.assertEqual(diary.answer(self.memory.db, "Richard", "What did we talk about today?", now=tuesday,
                                      zone=ZONE), "[neutral] We didn't talk today, as far as I remember.")
        self.assertIsNone(diary.answer(self.memory.db, "Richard", "what did you say about the orchids", now=tuesday))

    def test_a_topic_goes_when_a_sentence_it_came_from_is_forgotten(self):
        diary.build_day(self.memory, "Richard", MONDAY, zone=ZONE, state_dir=self.root)
        self.memory.edit(episode_ids=[self.ids["I booked the hotel in Japan today"]])
        record = diary.stored(self.memory.db, "Richard", MONDAY)
        self.assertEqual([topic for topic, _ in record["topics"]], ["orchids", "plumber"])
        self.assertEqual(record["conversations"], 3, "counts carry no words and stay")

    def test_the_night_job_writes_each_finished_day_once(self):
        job = diary.DiaryJob(self.memory, "Richard", today=MONDAY + timedelta(days=2), zone=ZONE,
                             state_dir=self.root)
        job.resume((MONDAY - timedelta(days=1)).isoformat())
        done = asyncio.run(job.run_chunk(1.0))
        self.assertFalse(done)
        self.assertEqual(job.cursor, MONDAY.isoformat())
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        self.assertTrue(asyncio.run(job.run_chunk(1.0)))
        days = [row[0] for row in self.memory.db.execute("SELECT day FROM diary_days ORDER BY day")]
        self.assertEqual(days, ["2026-11-23", "2026-11-24"])

    def test_phrases(self):
        for phrase in ("What did we talk about?", "what did we do yesterday", "Hey Pip, what did we talk about on "
                       "Friday?", "what have we talked about today"):
            self.assertTrue(diary.may_match(phrase), phrase)
        for phrase in ("what did I say about the orchid", "what did you dream about", "what do we have today"):
            self.assertFalse(diary.may_match(phrase), phrase)
        self.assertEqual(diary.which_day("what did we talk about on Monday", MONDAY)[0], MONDAY - timedelta(days=7))


@dataclass
class Ctx:
    now: Any
    guest: bool
    state_dir: Path
    owner: str = "Richard"


class Skill(unittest.TestCase):
    def test_the_everyday_handler_reads_the_archive_read_only(self):
        try:
            import mind.reply  # noqa: F401
        except ImportError:
            self.skipTest("mind.reply is not here yet")
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp), embedder=None)
            memory.log("heard", "The orchids need water", "Richard")
            memory.log("heard", "The orchids look happier now", "Richard")
            memory.close()
            now = datetime.now(ZONE)
            reply = asyncio.run(diary.handle("what did we talk about today", Ctx(now, False, Path(tmp))))
            self.assertIn("orchids", reply.line)
            guest = asyncio.run(diary.handle("what did we talk about today", Ctx(now, True, Path(tmp))))
            self.assertNotIn("orchids", guest.line)
            self.assertIsNone(asyncio.run(diary.handle("tell me a joke", Ctx(now, False, Path(tmp)))))


if __name__ == "__main__":
    unittest.main()
