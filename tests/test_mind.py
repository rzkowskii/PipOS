"""The mind, driven through a stand-in bus and a stand-in model server."""

import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import mind.turn
from mind import weather
from mind.cortex import SLOT_FILE, Cortex
from mind.memory import Memory
from mind.network_policy import NetworkPolicy, save_policy
from mind.skills import Skills
from mind.turn import Config, Mind, speakable_time
from sim.fake_cortex import FakeCortex, default_reply, render

mind.turn.log = lambda *parts: None  # keep test output readable

SENSES = "19:05, battery 82% charging, cool, room bright"
GREET_WITH_TOPIC = "Hello there, what makes a kite fly?"
SYDNEY_GEOCODE = {
    "results": [{"name": "Sydney", "country": "Australia", "latitude": -33.87, "longitude": 151.21}]
}


class FakeBus:
    def __init__(self):
        self.sent = []
        self.connected = asyncio.Event()
        self.connected.set()
        self.generation = 1
        self.urgent = None
        self._queue = asyncio.Queue()

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, topic, **fields):
        self.sent.append({"t": topic, **fields})

    async def recv(self):
        return await self._queue.get()

    def push(self, topic, **fields):
        event = {"t": topic, **fields}
        if self.urgent is not None:
            self.urgent(event)
        self._queue.put_nowait(event)

    def of(self, topic):
        return [e for e in self.sent if e["t"] == topic]

    async def wait(self, topic, count=1, timeout=5.0):
        """Wait until `count` events of `topic` have been sent."""
        for _ in range(int(timeout / 0.01)):
            if len(self.of(topic)) >= count:
                return self.of(topic)[count - 1]
            await asyncio.sleep(0.01)
        raise AssertionError(f"no {topic} #{count}; sent: {[e['t'] for e in self.sent]}")


def said(bus):
    """Everything the mind asked the voice to say, joined."""
    return " ".join(e["text"] for e in bus.of("say.request") if e["text"])


class MindCase(unittest.IsolatedAsyncioTestCase):
    reply = staticmethod(default_reply)
    delay = 0.0
    cfg = {}

    async def asyncSetUp(self):
        self.cortex_server = FakeCortex(delay=self.delay, reply=self.reply).start()
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.mind = None
        self.task = None
        await self.start_mind()

    async def start_mind(self):
        self.bus = FakeBus()
        self.memory = Memory(self.root)
        cfg = Config(name="Pip", owner="Richard", identity="You are Pip.", settle=0.05, cortex_wait=0.3)
        for key, value in self.cfg.items():
            setattr(cfg, key, value)
        self.mind = Mind(self.bus, Cortex(self.cortex_server.url), self.memory, cfg, self.root)
        self.task = asyncio.create_task(self.mind.run())
        await self.bus.wait("mind.ready")
        self.bus.push("body.lid", closed=False)
        self.bus.push("body.senses", line=SENSES)
        self.bus.push("body.state", hour=19, minute=5, battery=82, charging=True, on_ac=True)

    async def stop_mind(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.memory.close()

    async def asyncTearDown(self):
        await self.stop_mind()
        self.cortex_server.stop()
        self.dir.cleanup()

    async def hear(self, text, turn):
        """Say something to it while it is attending, and wait for the reply to end."""
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard")
        await self.bus.wait("mind.reply_done", turn)
        # In real life the voice reports the end of speech.
        self.bus.push("say.done", final=True)

    async def warmed(self):
        async with asyncio.timeout(2):
            while self.mind._warm_task is None:
                await asyncio.sleep(0)
            await self.mind._warm_task


class Conversation(MindCase):
    async def test_reports_ready_with_a_working_cortex(self):
        self.assertEqual(self.bus.of("mind.ready")[0]["cortex"], True)

    async def test_a_turn_end_to_end(self):
        await self.hear(GREET_WITH_TOPIC, 1)
        topics = [e["t"] for e in self.bus.sent]
        # The face hears the emotion before the voice gets a word.
        self.assertLess(topics.index("mind.emotion"), topics.index("say.request"))
        self.assertEqual(self.bus.of("mind.emotion")[0]["name"], "joy")

        requests = self.bus.of("say.request")
        self.assertEqual([r["seq"] for r in requests], list(range(1, len(requests) + 1)))
        self.assertEqual([r["last"] for r in requests], [False] * (len(requests) - 1) + [True])
        self.assertEqual(requests[-1]["text"], "")
        self.assertEqual(said(self.bus), "Hello! It's good to hear you. What shall we talk about?")
        self.assertNotIn("[", said(self.bus))
        self.assertEqual(self.bus.of("mind.reply_done")[0]["spoke"], True)

        sent = self.cortex_server.chats[0]
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "You are Pip."})
        self.assertEqual(sent["messages"][1]["content"], f"({SENSES})\n{GREET_WITH_TOPIC}")
        self.assertIn("emotion ::=", sent["grammar"])
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertTrue(sent["cache_prompt"])

        log = [(kind, text) for _, kind, _, text in self.memory.recent()]
        self.assertEqual(log[0], ("heard", GREET_WITH_TOPIC))
        self.assertEqual(log[1][0], "said")

    async def test_overheard_speech_is_ignored_and_not_kept(self):
        self.bus.push("state.changed", state="idle")
        self.bus.push("hear.final", text="talking to someone else about my salary", speaker="Richard")
        await asyncio.sleep(0.2)
        self.assertEqual(self.cortex_server.chats, [])
        self.assertEqual(self.bus.of("say.request"), [])
        self.assertFalse(self.memory.contains("salary"))

    async def test_the_prompt_only_grows_by_appending(self):
        await self.hear(GREET_WITH_TOPIC, 1)
        await self.hear("how are you handling the update", 2)
        first, second = (c["messages"] for c in self.cortex_server.chats)
        self.assertEqual(second[: len(first)], first, "earlier turns must be sent unchanged")
        self.assertEqual(second[len(first)]["role"], "assistant")
        self.assertTrue(second[len(first)]["content"].startswith("[joy] Hello!"))
        self.assertEqual(second[-1]["content"], f"({SENSES})\nhow are you handling the update")

    async def test_reads_its_senses_truthfully(self):
        await self.hear("tell me about your battery", 1)
        self.assertIn("82 percent", said(self.bus))
        self.assertEqual(len(self.cortex_server.chats), 1)

    async def test_battery_command_uses_only_known_readings(self):
        self.bus.push("body.state", battery=41, charging=False, on_ac=True)
        await self.hear("how much charge do you have", 1)
        self.assertEqual(said(self.bus), "My battery is at 41 percent. I'm plugged in.")
        self.bus.sent.clear()
        self.bus.push("body.state", battery=None, charging=None, on_ac=None)
        await self.hear("how is your battery", 1)
        self.assertEqual(said(self.bus), "I can't read my battery level right now.")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_time_is_answered_from_the_body_clock_without_the_model(self):
        await self.hear("what time is it?", 1)
        self.assertEqual(said(self.bus), "It's 7:05 in the evening.")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_good_night_sends_it_to_sleep_after_it_has_spoken(self):
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="Good night", speaker="Richard")
        await self.bus.wait("mind.reply_done")
        await asyncio.sleep(0.1)
        self.assertEqual(self.bus.of("being.sleep"), [], "must wait for the voice to finish")
        self.bus.push("say.done", final=True)
        await self.bus.wait("being.sleep")
        self.assertEqual(self.bus.of("mind.emotion")[0]["name"], "sleepy")


class Greetings(MindCase):
    async def test_social_check_ins_vary_and_leave_the_model_context_alone(self):
        await self.warmed()
        text = "hello there how are you today"
        replies = set()
        for _ in range(3):
            self.bus.sent.clear()
            await self.hear(text, 1)
            reply = said(self.bus)
            self.assertTrue(reply)
            self.assertLessEqual(len(reply.split()), 15)
            self.assertNotIn(text, reply.casefold())
            replies.add(reply)
        self.assertEqual(len(replies), 3)
        self.assertEqual(self.cortex_server.chats, [])
        self.assertEqual(self.mind.messages, [])

    async def test_greeting_preserves_startup_warmup_and_links_its_archive_reply(self):
        await self.warmed()
        finish = asyncio.Event()
        warmup = self.mind._warm_task = asyncio.create_task(finish.wait())
        try:
            await self.hear("Good morning, Pip", 1)
            self.assertEqual(said(self.bus), "Hi. Good to hear you.")
            self.assertFalse(warmup.done())
            self.assertEqual(self.cortex_server.chats, [])
            source = self.memory.recall_history("good morning", kind="heard")[0]
            self.assertEqual(self.memory.history_neighbor(source).text, said(self.bus))
        finally:
            finish.set()
            await warmup
        self.bus.sent.clear()
        await self.hear("Forget that", 1)
        self.assertFalse(self.memory.contains("Good morning"))
        self.assertFalse(self.memory.contains("Good to hear you"))


class Remembering(MindCase):
    async def test_remember_recall_and_use_in_conversation(self):
        await self.hear("Remember that my cat is called Tom", 1)
        self.assertEqual(said(self.bus), "Okay. I'll remember that.")
        self.assertEqual(self.cortex_server.chats, [], "commands never reach the model")

        self.bus.sent.clear()
        await self.hear("What do you remember about my cat?", 1)
        self.assertEqual(said(self.bus), "You told me: my cat is called Tom.")

        self.bus.sent.clear()
        await self.hear("I am thinking about getting my cat a toy", 1)
        content = self.cortex_server.chats[0]["messages"][-1]["content"]
        self.assertTrue(content.endswith('(you remember: they told you "my cat is called Tom")'), content)
        self.assertIn("my cat is called Tom", said(self.bus))

    async def test_forget_that_is_real(self):
        self.cortex_server.reply = lambda messages: "[neutral] Try zebra-7741."
        await self.hear("Remember that my locker code is zebra-7741", 1)
        await self.hear("tell me my locker code", 2)  # pulls it into the live conversation
        self.assertIn("Try zebra-7741.", said(self.bus))
        self.assertIn("zebra-7741", json.dumps(self.mind.messages))

        self.bus.sent.clear()
        await self.hear("Forget that", 1)
        self.assertEqual(said(self.bus), "Okay. That's forgotten.")
        self.assertFalse(self.memory.contains("zebra-7741"))
        self.assertNotIn("zebra-7741", json.dumps(self.mind.messages))
        self.assertNotIn("zebra-7741", (self.root / "context.json").read_text())
        self.assertIn(("action=erase", "being.slot"), self.cortex_server.slot_calls, "the model's cache keeps it")

        self.bus.sent.clear()
        await self.hear("What do you remember about my locker code?", 1)
        self.assertEqual(said(self.bus), "I don't remember anything about that.")

    async def test_forget_by_topic_takes_only_what_matches(self):
        await self.hear("Remember that my birthday is in May", 1)
        await self.hear("Remember that my sister's birthday is in June", 2)
        self.bus.sent.clear()
        await self.hear("Forget about my sister's birthday", 1)
        # Nothing goes until the count has been said back and agreed to.
        self.assertEqual(said(self.bus), "I found one thing to forget. Say yes, and I'll forget for good.")
        self.assertEqual(len(self.memory.facts()), 2)
        self.bus.sent.clear()
        await self.hear("Yes", 1)
        self.assertEqual(said(self.bus), "Okay. That's forgotten.")
        self.assertEqual([f.text for f in self.memory.facts()], ["my birthday is in May"])
        self.assertFalse(self.memory.contains("sister"), "the request itself must not be logged")

    async def test_anything_but_yes_keeps_what_was_offered_for_forgetting(self):
        await self.hear("Remember that my sister's birthday is in June", 1)
        await self.hear("Forget about my sister's birthday", 2)
        await self.hear("What time is it?", 3)
        self.assertEqual(len(self.memory.facts()), 1)
        # The offer is spent: a late yes forgets nothing.
        await self.hear("Yes", 4)
        self.assertEqual(len(self.memory.facts()), 1)

    async def test_forget_it_means_never_mind_and_keeps_everything(self):
        await self.hear("Remember that my sister lives in Perth", 1)
        await self.hear("Forget about my sister", 2)
        self.assertIn("Say yes", said(self.bus))
        self.bus.sent.clear()
        await self.hear("Forget it", 1)
        self.assertIn("kept everything", said(self.bus))
        self.assertEqual(len(self.memory.facts()), 1)
        self.bus.sent.clear()
        await self.hear("Yes", 1)
        self.assertEqual(len(self.memory.facts()), 1, "the offer was spent")

    async def test_an_offer_to_forget_does_not_outlive_a_closed_lid_or_another_voice(self):
        await self.hear("Remember that my sister lives in Perth", 1)
        await self.hear("Forget about my sister", 2)
        self.assertIsNotNone(self.mind._pending_forget)
        self.mind._close_lid()
        self.assertIsNone(self.mind._pending_forget)

        self.mind.lid_open = True
        self.mind._discard_reply = False
        await self.hear("Forget about my sister", 3)
        self.assertIsNotNone(self.mind._pending_forget)
        self.bus.push("hear.final", text="Yes", speaker="Mallory", seq=4)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.memory.facts()), 1, "only the person who asked can confirm")

    async def test_forget_everything_from_a_day_takes_that_day(self):
        await self.hear("The gate code is 4411", 1)
        for phrase in ("Forget everything from today", "Forget what I said today", "Forget everything I said today"):
            self.bus.sent.clear()
            await self.hear(phrase, 1)
            self.assertIn("Say yes", said(self.bus), phrase)
            self.mind._pending_forget = None

    async def test_forgetting_a_short_word_leaves_other_words_whole(self):
        await self.hear("I read a good book about cooking", 1)
        await self.hear("ok", 2)
        await self.hear("Forget that", 3)
        self.assertEqual(self.mind.messages[0]["content"].splitlines()[-1], "I read a good book about cooking")

    async def test_forget_that_after_a_clock_answer_does_not_reach_back(self):
        await self.hear("I love my sister dearly", 1)
        await self.hear("What time is it?", 2)
        self.bus.sent.clear()
        await self.hear("Forget that", 1)
        self.assertIn("not sure what to forget", said(self.bus))
        self.assertTrue(self.memory.contains("I love my sister dearly"))

    async def test_forgetting_a_short_fact_leaves_words_that_contain_it(self):
        await self.hear("I had a strawberry milkshake with Dana at the pier", 1)
        await self.hear("Remember milk", 2)
        await self.hear("Forget that", 3)
        self.assertEqual(self.memory.facts(), [])
        self.assertTrue(self.memory.contains("strawberry milkshake"))

        await self.hear("I like herbs and gardening on Sundays", 4)
        await self.hear("I like her", 5)
        await self.hear("Forget that", 6)
        self.assertTrue(self.memory.contains("herbs and gardening"))

    async def test_forgetting_a_day_takes_the_questions_that_looked_things_up(self):
        old = self.memory.log("heard", "My diagnosis is Lupusmarker and I am scared", "Richard")
        with self.memory.db:
            self.memory.db.execute("UPDATE episodes SET ts = ts - 3 * 86400 WHERE id = ?", (old,))
        await self.hear("Tell me about my diagnosis", 1)
        self.bus.sent.clear()
        await self.hear("Forget everything I said today", 1)
        self.assertIn("Say yes", said(self.bus))
        await self.hear("Yes", 2)
        self.assertNotIn("Lupusmarker", json.dumps(self.mind.messages))
        today = self.memory.db.execute(
            "SELECT count(*) FROM episodes WHERE kind = 'heard' AND id > ? AND text <> '[forgotten]'", (old,)).fetchone()[0]
        self.assertEqual(today, 0)
        self.assertTrue(self.memory.contains("Lupusmarker"), "the older day was not asked about")

    async def test_forget_that_takes_a_longer_fact_that_repeats_it(self):
        await self.hear("Remember that my pin is 4417 and my bank is Westfield Credit", 1)
        await self.hear("Remember that my pin is 4417", 2)
        await self.hear("Forget that", 3)
        self.assertFalse(self.memory.contains("4417"))

    async def test_forget_that_takes_the_second_telling_of_a_short_fact(self):
        await self.hear("Remember hunter2", 1)
        await self.hear("Please remember that hunter2", 2)
        await self.hear("Forget that", 3)
        self.assertFalse(self.memory.contains("hunter2"))

    async def test_forgetting_a_pleasantry_leaves_sentences_that_begin_with_it(self):
        await self.hear("Good morning, I have a knee appointment with Doctor Quillfeather at nine", 1)
        await self.hear("Good morning", 2)
        await self.hear("Forget that", 3)
        self.assertTrue(self.memory.contains("Quillfeather"))

    async def test_forgetting_a_topic_takes_the_question_that_named_it(self):
        self.memory.log("heard", "My diagnosis came back and I am scared", "Richard")
        await self.hear("Tell me about my Lupusmarker diagnosis", 1)
        self.bus.sent.clear()
        await self.hear("Forget about Lupusmarker", 1)
        self.assertIn("Say yes", said(self.bus))
        await self.hear("Yes", 2)
        self.assertFalse(self.memory.contains("Lupusmarker"))
        self.assertNotIn("Lupusmarker", json.dumps(self.mind.messages))

    async def test_an_offer_to_forget_runs_out(self):
        await self.hear("Remember that my sister's birthday is in June", 1)
        await self.hear("Forget about my sister's birthday", 2)
        self.mind._pending_forget["until"] = 0.0
        await self.hear("Yes", 3)
        self.assertEqual(len(self.memory.facts()), 1)

    async def test_forget_that_never_takes_a_fact_that_only_shared_a_word(self):
        await self.hear("Remember that my cat is called Tom", 1)
        await self.hear("My cat is sick today", 2)
        self.bus.sent.clear()
        await self.hear("Forget that", 1)
        self.assertEqual(said(self.bus), "Okay. That's forgotten.")
        self.assertEqual([f.text for f in self.memory.facts()], ["my cat is called Tom"])
        self.assertFalse(self.memory.contains("sick"), "the sentence just said is what goes")

    async def test_nothing_to_forget(self):
        await self.hear("Forget that", 1)
        self.assertIn("not sure what to forget", said(self.bus))

    async def test_forgetting_a_day_takes_that_day_not_the_word(self):
        await self.hear("We said today would be quiet", 1)
        # Push that sentence three days into the past.
        with self.memory.db:
            self.memory.db.execute("UPDATE episodes SET ts = ts - 3 * 86400")
        await self.hear("The fern needs water", 2)
        await self.hear("I am going on a picnic", 3)
        await self.hear("Forget about today", 4)
        self.assertTrue(self.memory.contains("picnic"), "nothing goes before the yes")
        await self.hear("Yes", 5)
        self.assertFalse(self.memory.contains("picnic"))
        self.assertFalse(self.memory.contains("fern"))
        self.assertTrue(self.memory.contains("would be quiet"), "an older sentence with the word in it stays")

    async def test_correction_replaces_the_last_fact(self):
        await self.hear("Remember that the bins go out on Tuesday", 1)
        await self.hear("That's wrong, the bins go out on Monday", 2)
        self.assertEqual([f.text for f in self.memory.facts()], ["the bins go out on Monday"])


class Interrupting(MindCase):
    delay = 0.03

    async def test_cancel_stops_the_reply_and_marks_it_cut_off(self):
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="tell me a long story", speaker="Richard")
        await self.bus.wait("say.request", 2)
        self.bus.push("mind.cancel")
        await asyncio.sleep(0.4)

        requests = self.bus.of("say.request")
        self.assertFalse(any(r["last"] for r in requests), "a cut-off reply has no ending")
        self.assertLess(len(requests), 10, "it kept talking after being cancelled")
        self.assertEqual(self.bus.of("mind.reply_done"), [])
        self.assertTrue(self.mind.messages[-1]["content"].endswith("—"))
        for _ in range(100):
            if self.cortex_server.aborted:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.cortex_server.aborted, 1, "the model server kept generating")

        # It is fine afterwards.
        self.bus.sent.clear()
        await self.hear(GREET_WITH_TOPIC, 1)
        self.assertIn("Hello!", said(self.bus))

    async def test_a_new_utterance_replaces_the_reply_in_progress(self):
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="tell me a long story", speaker="Richard")
        await self.bus.wait("say.request", 2)
        self.bus.push("hear.final", text=GREET_WITH_TOPIC, speaker="Richard")
        await self.bus.wait("mind.reply_done")
        self.assertIn("Hello!", said(self.bus))
        self.assertEqual(len(self.cortex_server.chats), 2)

    async def test_lid_close_discards_the_turn_reply_and_closed_lid_input(self):
        await self.hear("Remember that my fern is called Moonleaf", 1)
        self.bus.sent.clear()
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="tell me a long story", speaker="Richard")
        await self.bus.wait("say.request", 2)
        count = len(self.bus.of("say.request"))
        self.bus.push("body.lid", closed=True)
        self.bus.push("hear.final", text="remember the hidden phrase budwood", speaker="Richard")
        await asyncio.wait_for(self.mind._turn, 2.0)
        self.assertEqual(len(self.bus.of("say.request")), count)
        self.assertFalse(self.bus.of("mind.reply_done"))
        self.assertTrue(self.memory.contains("Moonleaf"))
        self.assertTrue(self.memory.contains("tell me a long story"))
        self.assertFalse(self.memory.contains("sentence number"))
        self.assertFalse(self.memory.contains("budwood"))

        self.bus.sent.clear()
        self.bus.push("body.lid", closed=False)
        await self.hear("what time is it?", 1)
        self.assertEqual(said(self.bus), "It's 7:05 in the evening.")


class ThinkingWhileListening(MindCase):
    cfg = {"prefill": True}

    async def test_memory_commands_do_not_queue_model_prefills(self):
        await self.warmed()
        self.bus.push("state.changed", state="listening")
        for partial in ["what did I", "what did I say about", "what did I say about orchids"]:
            self.bus.push("hear.partial", text=partial)
            await asyncio.sleep(0.02)
        await self.hear("What did I say about orchids?", 1)
        self.assertFalse(any("what did" in p["prompt"] for p in self.cortex_server.prefills))
        self.assertEqual(self.cortex_server.chats, [])

    async def test_only_one_stable_partial_can_extend_the_warm_prompt(self):
        await self.warmed()
        warm_requests = len(self.cortex_server.prefills)
        self.bus.push("body.senses", line="")
        self.bus.push("state.changed", state="listening")
        for partial in ["what", "what is", "what is the", "what is the ocean", "what is the ocean like"]:
            self.bus.push("hear.partial", text=partial)
            await asyncio.sleep(0.03)
        self.bus.push("hear.final", text="what is the ocean like today", speaker="Richard")
        await self.bus.wait("mind.reply_done")

        # The first read is the warm-up on waking; the rest follow the words.
        prefills = [p["prompt"] for p in self.cortex_server.prefills]
        self.assertEqual(len(prefills), warm_requests + 1, prefills)
        self.assertTrue(prefills[-1].endswith("what is"), prefills)
        final = render(self.cortex_server.chats[0]["messages"])
        for earlier, later in zip(prefills, prefills[1:] + [final]):
            # Every prompt strictly extends the one before, so nothing is re-read.
            self.assertTrue(later.startswith(earlier), f"{later!r} does not extend {earlier!r}")
            self.assertFalse(earlier.endswith("<|assistant|>\n"), "read-ahead ran past the last word")

    async def test_no_reading_ahead_when_not_addressed(self):
        await self.warmed()
        self.bus.push("state.changed", state="idle")
        self.bus.push("hear.partial", text="some words from across the room")
        await asyncio.sleep(0.2)
        self.assertEqual(self.cortex_server.requests, [])
        self.assertFalse(any("across the room" in p["prompt"] for p in self.cortex_server.prefills))

    async def test_losing_lid_state_cancels_read_ahead_and_pending_words(self):
        await self.warmed()
        started = asyncio.Event()
        ended = asyncio.Event()

        async def prefill(_messages, **_budget):
            started.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()

        self.mind.cortex.prefill = prefill
        self.bus.push("state.changed", state="listening")
        self.bus.push("hear.partial", text="a few settling words")
        self.bus.push("hear.partial", text="a few settling words here")
        await asyncio.wait_for(started.wait(), 2)
        self.bus.push("hear.partial", text="a few settling words here still")
        self.bus.push("body.lid", closed=None)
        await asyncio.wait_for(ended.wait(), 2)
        self.assertIsNone(self.mind._last_partial)
        self.assertIsNone(self.mind._utterance_senses)
        self.assertEqual(self.mind._speculative_text, "")


class ForegroundPriority(MindCase):
    async def test_shipped_policy_only_sends_a_final_transcript(self):
        await self.warmed()
        prefills = len(self.cortex_server.prefills)
        self.bus.push("state.changed", state="listening")
        self.bus.push("hear.partial", text="tell me an ordinary story")
        self.bus.push("hear.partial", text="tell me an ordinary story please")
        await self.hear("tell me an ordinary story please", 1)
        self.assertEqual(len(self.cortex_server.prefills), prefills)
        self.assertEqual(len(self.cortex_server.chats), 1)

    async def test_final_cancels_background_before_sending_the_chat(self):
        await self.warmed()
        self.bus.push("state.changed", state="listening")
        await asyncio.sleep(0)
        active = set()
        cancelled = set()

        async def background(name):
            active.add(name)
            try:
                await asyncio.Future()
            finally:
                cancelled.add(name)

        for name in ("_prefill_task", "_warm_task", "_housekeeping"):
            setattr(self.mind, name, asyncio.create_task(background(name)))
        await asyncio.sleep(0)
        stream = self.mind.cortex.stream

        def after_cancellation(*args, **kwargs):
            self.assertEqual(cancelled, active)
            return stream(*args, **kwargs)

        self.mind.cortex.stream = after_cancellation
        self.bus.push("hear.final", text=GREET_WITH_TOPIC, speaker="Richard")
        await self.bus.wait("mind.reply_done")
        self.assertIn("Hello!", said(self.bus))
        self.assertEqual(len(cancelled), 3)

    async def test_failed_cache_erase_blocks_idle_and_shutdown_saves(self):
        await self.warmed()
        actions = []

        async def slot(action):
            actions.append(action)
            if action == "erase":
                self.assertTrue(self.mind._discard_cache)
            return False

        self.mind.cortex.slot = slot
        await self.mind._erase_cache()
        self.mind.state = "asleep"
        await self.mind._housekeep()
        await self.mind.shutdown()
        self.assertEqual(actions, ["erase"])


class CancellablePrefill(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_a_prefill_disconnects_the_waiting_request(self):
        completion_started = asyncio.Event()
        completion_disconnected = asyncio.Event()

        async def handler(reader, writer):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                content_length = next(
                    int(line.split(b":", 1)[1])
                    for line in header.splitlines()
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(content_length)
                if header.startswith(b"POST /apply-template "):
                    body = b'{"prompt":"some spoken words"}'
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                        + str(len(body)).encode() + b"\r\n\r\n" + body
                    )
                    await writer.drain()
                else:
                    completion_started.set()
                    await reader.read()
                    completion_disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cortex = Cortex(f"http://127.0.0.1:{port}", timeout=3.0)
            task = asyncio.create_task(cortex.prefill([{"role": "user", "content": "some spoken words"}]))
            try:
                await asyncio.wait_for(completion_started.wait(), 2.0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(completion_disconnected.wait(), 2.0)


class WithoutACortex(MindCase):
    async def asyncSetUp(self):
        self.cortex_server = FakeCortex().start()
        self.cortex_server.healthy = False
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        await self.start_mind()

    async def test_wakes_anyway_and_says_it_cannot_think(self):
        self.assertEqual(self.bus.of("mind.ready")[0]["cortex"], False)
        await self.hear(GREET_WITH_TOPIC, 1)
        self.assertIn("can't think right now", said(self.bus))
        self.assertEqual(self.bus.of("mind.emotion")[0]["name"], "concern")
        # Commands still work, because they never needed the model.
        self.bus.sent.clear()
        await self.hear("Remember that my cat is called Tom", 1)
        self.assertEqual(said(self.bus), "Okay. I'll remember that.")

    async def test_can_greet_without_the_model(self):
        await self.hear("Pip, are you there?", 1)
        self.assertEqual(said(self.bus), "I'm here. I can hear you.")
        self.assertEqual(self.cortex_server.chats, [])

    async def test_everyday_skills_use_local_clock_and_skip_the_model(self):
        await self.hear("Set my time zone to Australia slash Sydney", 1)
        self.assertEqual(self.mind.skills.zone.key, "Australia/Sydney")
        now = datetime(2026, 11, 21, 9, 5, tzinfo=ZoneInfo("Australia/Sydney"))
        with patch.object(self.mind.skills, "now", return_value=now):
            self.bus.sent.clear()
            await self.hear("Pip, what's today's date?", 1)
            self.assertEqual(said(self.bus), "It's Saturday, November 21, 2026.")
            self.bus.sent.clear()
            await self.hear("What time is it?", 1)
            self.assertEqual(said(self.bus), "It's 9:05 in the morning.")

        weather = self.mind.skills.weather.reply = AsyncMock(return_value="Open-Meteo forecasts 21 degrees Celsius.")
        self.bus.sent.clear()
        await self.hear("Set my weather city to Sydney, Australia", 1)
        weather.assert_not_awaited()
        self.bus.sent.clear()
        save_policy(self.root, NetworkPolicy(mode="internet", web_lookups=True))
        await self.hear("What's the weather tomorrow?", 1)
        weather.assert_awaited_once_with("tomorrow", "Sydney, Australia")
        self.assertEqual(said(self.bus), "Open-Meteo forecasts 21 degrees Celsius.")
        restored = Skills(self.root)
        self.assertEqual((restored.zone.key, restored.location), ("Australia/Sydney", "Sydney, Australia"))
        self.assertFalse(self.memory.contains("Open-Meteo"))
        self.assertEqual(self.cortex_server.chats, [])

    async def test_cancelling_weather_allows_the_next_local_reply(self):
        save_policy(self.root, NetworkPolicy(mode="internet", web_lookups=True))
        entered = asyncio.Event()

        async def pending_weather(period, city):
            entered.set()
            await asyncio.Future()

        self.mind.skills.weather.reply = pending_weather
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="What's the weather in Sydney?")
        await asyncio.wait_for(entered.wait(), 1.0)
        self.bus.push("mind.cancel")
        async with asyncio.timeout(1.0):
            await self.hear("What time is it?", 1)
        self.assertEqual(said(self.bus), "It's 7:05 in the evening.")

    async def test_can_create_list_and_cancel_reminders_and_timers(self):
        now = datetime(2026, 11, 21, 9, 5, tzinfo=ZoneInfo("Australia/Sydney"))
        with patch.object(self.mind.skills, "now", return_value=now):
            await self.hear("Remind me to call Mum tomorrow at 9 a.m.", 1)
            await self.hear("Start a ten minute timer", 2)
            self.assertEqual(said(self.bus), "Reminder 1 set for tomorrow at 9:00 in the morning. "
                                             "Timer 2 started for 10 minutes.")
            self.bus.sent.clear()
            await self.hear("What are my reminders?", 1)
            self.assertIn("Reminder 1, call Mum", said(self.bus))
            self.assertNotIn("Timer", said(self.bus))
            self.bus.sent.clear()
            await self.hear("Cancel timer 1", 1)
            self.assertIn("couldn't find", said(self.bus))
            self.bus.sent.clear()
            await self.hear("Cancel reminder one", 1)
            self.assertEqual(said(self.bus), "Cancelled reminder 1.")
            self.bus.sent.clear()
            await self.hear("Cancel the timer", 1)
            self.assertEqual(said(self.bus), "Cancelled timer 2.")
        self.assertEqual(self.mind.skills.reminders.upcoming(), [])
        self.assertFalse(self.memory.contains("call Mum"))
        self.assertEqual(self.cortex_server.chats, [])

    async def test_interrupted_reminder_retries_and_needs_its_own_voice_receipt(self):
        reminders = self.mind.skills.reminders
        entry = reminders.add("Check the oven.", time.time() - 1)
        self.bus.push("state.changed", state="listening")
        self.bus.push("stem.beat")
        self.bus.push("state.changed", state="idle")
        self.bus.push("stem.beat")
        first = (await self.bus.wait("mind.announce"))["announcement"]
        self.assertEqual(len(self.bus.of("mind.announce")), 1)
        self.bus.push("state.changed", state="thinking")
        self.bus.push("stem.announce_ready", announcement=first)
        await self.bus.wait("mind.reply_done")
        self.assertEqual(self.bus.of("say.request")[-1]["announcement"], first)
        self.bus.push("say.done", final=True)
        with patch("mind.turn.ANNOUNCE_RETRY", 0.0):
            self.bus.push("mind.cancel")
        self.assertEqual(reminders.upcoming(), [entry])

        self.bus.push("state.changed", state="attending")
        self.bus.push("stem.beat")
        second = (await self.bus.wait("mind.announce", 2))["announcement"]
        self.assertNotEqual(first, second)
        self.bus.push("state.changed", state="thinking")
        self.bus.push("stem.announce_ready", announcement=second)
        await self.bus.wait("mind.reply_done", 2)
        self.bus.push("say.done", final=True, announcement=first)
        await asyncio.sleep(0)
        self.assertEqual(reminders.upcoming(), [entry])
        self.bus.push("say.done", final=True, announcement=second)
        self.bus.push("body.lid", closed=True)
        await asyncio.sleep(0)
        self.assertEqual(reminders.upcoming(), [])
        self.assertEqual(self.cortex_server.chats, [])


class WeatherRequests(MindCase):
    def assert_web_lookup_blocked(self):
        reply = said(self.bus).lower()
        self.assertIn("internet mode", reply)
        self.assertIn("web lookups", reply)
        self.assertEqual(self.cortex_server.chats, [])

    async def test_missing_and_restrictive_policies_never_resolve_or_fetch_weather(self):
        resolve = socket.getaddrinfo
        local_hosts = {"127.0.0.1", "::1", "localhost"}

        def only_loopback(host, *args, **kwargs):
            if host not in local_hosts:
                self.fail(f"Unexpected lookup for {host}")
            return resolve(host, *args, **kwargs)

        policies = (
            None,
            NetworkPolicy(mode="local-only", web_lookups=True),
            NetworkPolicy(mode="paired-lan", web_lookups=True),
            NetworkPolicy(mode="internet", web_lookups=False),
        )
        with (
            patch.object(socket, "getaddrinfo", side_effect=only_loopback) as dns,
            patch.object(self.mind.skills.weather, "_fetch_json", side_effect=AssertionError("Unexpected weather fetch")) as fetch,
        ):
            for policy in policies:
                with self.subTest(policy=policy):
                    if policy is not None:
                        save_policy(self.root, policy)
                    self.bus.sent.clear()
                    await self.hear("What's the weather in Sydney, Australia tomorrow?", 1)
                    self.assert_web_lookup_blocked()
            fetch.assert_not_called()
            self.assertEqual({call.args[0] for call in dns.call_args_list} - local_hosts, set())

    async def test_saving_and_restoring_a_city_does_not_grant_web_lookups(self):
        with patch.object(self.mind.skills.weather, "_fetch_json") as fetch:
            await self.hear("Set my weather city to Sydney, Australia", 1)
            fetch.assert_not_called()
        await self.stop_mind()
        await self.start_mind()
        self.assertEqual(self.mind.skills.location, "Sydney, Australia")
        with patch.object(self.mind.skills.weather, "_fetch_json") as fetch:
            await self.hear("What's the weather tomorrow?", 1)
            fetch.assert_not_called()
        self.assert_web_lookup_blocked()

    async def test_enabling_web_lookups_allows_weather_without_a_restart(self):
        conditions = {"current": {"time": "2026-11-21T15:45", "temperature_2m": 21.1, "weather_code": 2}}
        question = "What's the weather in Sydney, Australia?"
        with patch.object(self.mind.skills.weather, "_fetch_json", side_effect=[SYDNEY_GEOCODE, conditions]) as fetch:
            await self.hear(question, 1)
            self.assert_web_lookup_blocked()
            fetch.assert_not_called()

            save_policy(self.root, NetworkPolicy(mode="internet", web_lookups=True))
            self.bus.sent.clear()
            await self.hear(question, 1)

        self.assertIn("Open-Meteo reports 21 degrees Celsius", said(self.bus))
        self.assertIn("in Sydney, Australia", said(self.bus))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [weather.GEOCODER, weather.FORECAST])
        self.assertEqual(self.cortex_server.chats, [])

    async def test_revocation_after_geocoding_blocks_forecast_and_next_turn(self):
        save_policy(self.root, NetworkPolicy(mode="internet", web_lookups=True))

        def geocode_and_revoke(host, path, params, timeout):
            self.assertEqual(host, weather.GEOCODER)
            self.assertEqual(params["name"], "Sydney")
            save_policy(self.root, NetworkPolicy(mode="internet", web_lookups=False))
            return SYDNEY_GEOCODE

        with patch.object(self.mind.skills.weather, "_fetch_json", side_effect=geocode_and_revoke) as fetch:
            await self.hear("What's the weather in Sydney, Australia?", 1)
            self.assert_web_lookup_blocked()
            self.bus.sent.clear()
            await self.hear("What's the weather in Sydney, Australia?", 1)
            self.assert_web_lookup_blocked()
        fetch.assert_called_once()

    async def test_weather_timeout_leaves_local_and_model_turns_responsive(self):
        save_policy(self.root, NetworkPolicy(mode="internet", web_lookups=True))
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_fetch(*args):
            entered.set()
            release.wait(3.0)
            finished.set()
            return {}

        with patch.object(weather, "LOOKUP_TIMEOUT", 0.2), \
                patch.object(self.mind.skills.weather, "_fetch_json", side_effect=slow_fetch):
            try:
                async with asyncio.timeout(2.0):
                    await self.hear("What's the weather in Sydney?", 1)
                    self.assertTrue(entered.is_set())
                    self.assertEqual(said(self.bus), weather.UNAVAILABLE)
                    self.assertEqual(self.cortex_server.chats, [])
                    self.bus.sent.clear()
                    await self.hear("What time is it?", 1)
                    self.assertEqual(said(self.bus), "It's 7:05 in the evening.")
                    await self.hear(GREET_WITH_TOPIC, 2)
                    self.assertIn("It's good to hear you", said(self.bus))
                    self.assertEqual(len(self.cortex_server.chats), 1)
                self.assertFalse(finished.is_set())
            finally:
                release.set()


class BrokenReplies(MindCase):
    reply = staticmethod(lambda messages: "")

    async def test_an_empty_reply_is_owned_up_to(self):
        await self.hear(GREET_WITH_TOPIC, 1)
        self.assertEqual(said(self.bus), "Sorry. I lost my thought.")
        self.assertEqual(self.mind.messages, [], "a failed turn leaves nothing in the context")


class Thinker(MindCase):
    reply = staticmethod(lambda messages: "<think>They greeted me. I should greet back.</think>[joy] Hi! **Nice** to see you \U0001F600.")

    async def test_thinking_markup_and_emoji_are_never_spoken(self):
        await self.hear(GREET_WITH_TOPIC, 1)
        self.assertEqual(said(self.bus), "Hi! Nice to see you .")
        self.assertEqual(self.bus.of("mind.emotion")[0]["name"], "joy")


class Reciter(MindCase):
    reply = staticmethod(lambda messages: "[joy] Tom sleeps well.\n(19:05, battery 82% charging, cool) and so on for a long time " * 3)

    async def test_private_notes_are_never_read_aloud(self):
        await self.hear("tell me about Tom", 1)
        self.assertEqual(said(self.bus), "Tom sleeps well.")
        self.assertEqual(self.mind.messages[-1]["content"], "[joy] Tom sleeps well.")
        self.assertTrue(self.bus.of("say.request")[-1]["last"], "the reply still ends properly")

    async def test_inline_senses_are_cut_but_an_incomplete_label_is_spoken(self):
        self.cortex_server.reply = lambda _: f"[joy] Tom sleeps well. ({SENSES}) More notes."
        await self.hear("tell me about Tom", 1)
        self.assertEqual(said(self.bus), "Tom sleeps well.")
        self.assertEqual(self.mind.messages[-1]["content"], "[joy] Tom sleeps well.")

        self.cortex_server.reply = lambda _: "[neutral] Fine (private"
        await self.hear("tell me more about Tom", 2)
        self.assertEqual(said(self.bus), "Tom sleeps well. Fine (private")


class BodyClock(MindCase):
    async def beat(self):
        self.bus.push("stem.beat")
        await asyncio.sleep(0.05)

    def clocks(self):
        return [event["utc_offset"] for event in self.bus.sent if event["t"] == "mind.clock"]

    async def test_the_brainstem_is_told_the_owners_zone_once_and_again_after_it_restarts(self):
        await self.beat()
        await self.beat()
        self.assertEqual(self.clocks(), [None], "a new connection is told once that the device zone is in use")
        await self.hear("Set my time zone to Asia/Tokyo", 1)
        await self.beat()
        await self.beat()
        self.assertEqual(self.clocks(), [None, 9 * 3600])
        self.bus.generation += 1
        await self.beat()
        self.assertEqual(self.clocks(), [None, 9 * 3600, 9 * 3600])
        await self.hear("Use the device time zone", 2)
        await self.beat()
        self.assertEqual(self.clocks()[-1], None)


class Parroting(MindCase):
    def test_parrot_check(self):
        from mind.turn import _parrots
        self.assertTrue(_parrots("How are you feeling right now?", "How are you feeling right now?"))
        self.assertTrue(_parrots("how are you feeling", "How are you feeling right now?"))
        self.assertFalse(_parrots("I feel bright, thank you.", "How are you feeling right now?"))
        self.assertFalse(_parrots("I'm a very small being, only a few minutes old.", "How old are you?"))
        self.assertFalse(_parrots("How old are you, then?", "I'm forty years old, and I live by the sea with my dog."))
        self.assertFalse(_parrots("Yes.", "Yes."))

    async def test_a_reply_that_only_repeats_the_question_is_never_said(self):
        replies = iter(["[curiosity] How are you feeling right now?", "[joy] Bright and warm, thank you."])
        self.cortex_server.reply = lambda _: next(replies)
        await self.hear("How are you feeling right now?", 1)
        self.assertEqual(said(self.bus), "Bright and warm, thank you.")
        self.assertNotIn("How are you feeling right now?",
                         [m["content"] for m in self.mind.messages if m["role"] == "assistant"][-1])

    async def test_a_second_parrot_is_not_said_either(self):
        self.cortex_server.reply = lambda _: "[curiosity] How are you feeling right now?"
        await self.hear("How are you feeling right now?", 1)
        self.assertEqual(said(self.bus), "Sorry. I lost my thought.")


class Age(MindCase):
    def test_age_reply_comes_from_the_life_record(self):
        from mind.turn import _age_reply
        self.assertEqual(_age_reply((1000.0, 1), 1000.0 + 150), "[joy] I'm 2 minutes old. This is my first time awake.")
        self.assertEqual(_age_reply((0.0, 2), 3 * 3600), "[joy] I'm 3 hours old. I've woken up 2 times.")
        self.assertEqual(_age_reply((0.0, 9), 12 * 86_400 + 60), "[joy] I'm 12 days old. I've woken up 9 times.")
        self.assertIn("not sure", _age_reply(None, 0))

    async def test_how_old_are_you_is_answered_without_the_model(self):
        self.bus.push("life.woke", id="a" * 32, born=time.time() - 300, wakes=3, first=False)
        await asyncio.sleep(0.05)
        chats = len(self.cortex_server.chats)
        await self.hear("How old are you?", 1)
        self.assertEqual(said(self.bus), "I'm 5 minutes old. I've woken up 3 times.")
        self.assertEqual(len(self.cortex_server.chats), chats)


class Housekeeping(MindCase):
    cfg = {"ctx_tokens": 260, "keep_turns": 1}

    async def test_compacts_when_idle_and_rereads_the_new_prompt(self):
        await self.hear("My mandolin is stored in the blue cupboard.", 1)
        for i in range(1, 4):
            await self.hear(f"hello number {i}", i + 1)
        before = len(self.mind.messages)
        self.assertGreater(self.mind._tokens(), 0.6 * 260)

        self.bus.push("state.changed", state="idle")
        for _ in range(200):
            if self.mind.highlights:
                break
            await asyncio.sleep(0.02)
        self.assertEqual([source.text for source in self.mind._highlight_sources()], [
            "My mandolin is stored in the blue cupboard.",
        ])
        self.assertEqual(len(self.mind.messages), 2)
        self.assertLess(len(self.mind.messages), before)
        self.assertIn("blue cupboard", self.mind.system)
        self.assertTrue(all(request["stream"] for request in self.cortex_server.chats))
        await asyncio.wait_for(self.mind._housekeeping, timeout=5)
        self.assertIn("blue cupboard", self.cortex_server.prefills[-1]["prompt"], "the new prompt is read while idle")

    async def test_a_full_context_is_cut_to_half_in_one_step(self):
        filler = "word " * 30
        for _ in range(12):
            self.mind.messages.extend([
                {"role": "user", "content": filler},
                {"role": "assistant", "content": "[neutral] " + filler},
            ])
        self.assertGreater(self.mind._tokens(), 0.9 * 260)
        self.mind._make_room()
        after = len(self.mind.messages)
        self.assertLessEqual(self.mind._tokens(), 0.5 * 260)
        # The next turns fit without cutting again, so the model is not made
        # to read the whole prompt afresh on each of them.
        self.mind.messages.extend([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "[neutral] hi"}])
        self.mind._make_room()
        self.assertEqual(len(self.mind.messages), after + 2)

    async def test_no_compaction_mid_conversation(self):
        for i in range(4):
            await self.hear(f"hello number {i}", i + 1)
        await asyncio.sleep(0.3)
        self.assertEqual(self.mind.highlights, ())
        self.assertGreater(len(self.mind.messages), 2)

    async def test_compaction_still_allows_ordinary_search_for_an_omitted_turn(self):
        archived = "I keep the travel alarm clock in the green suitcase."
        statements = [
            archived,
            "The folding bicycle in the garage needs a new chain before winter.",
            "I use wholemeal flour for the sourdough starter every Sunday.",
            "My utility account is paid on the first Tuesday of each month.",
            "The keyboard is in the red crate next to the guitar amplifier.",
            "I planted daffodil bulbs along the front gate over the weekend.",
        ]
        ids = []
        for statement in statements:
            source = self.memory.log("heard", statement, "Richard")
            ids.append(source)
            self.mind.messages.extend([
                {"role": "user", "content": statement, "episode_id": source},
                {"role": "assistant", "content": "[neutral] Thanks for telling me."},
            ])
        self.bus.push("state.changed", state="idle")
        async with asyncio.timeout(4):
            while not self.mind.highlights:
                await asyncio.sleep(0.02)
        self.assertEqual(self.mind.highlights, tuple(reversed(ids[2:5])))
        self.assertNotIn(archived, self.mind.system)

        await self.hear("I'm trying to locate the travel alarm clock we discussed.", 1)
        latest = self.cortex_server.chats[-1]["messages"][-1]["content"]
        self.assertIn(archived, latest)

    async def test_highlight_reloads_canonical_wording_after_correction_and_disappears_on_forget(self):
        original = "My mandolin is stored in the blue cupboard."
        corrected = "My mandolin is stored in the stairwell chest."
        await self.hear(original, 1)
        for i in range(1, 4):
            await self.hear(f"hello number {i}", i + 1)
        self.bus.push("state.changed", state="idle")
        async with asyncio.timeout(4):
            while not self.mind.highlights:
                await asyncio.sleep(0.02)
        highlight_ids = self.mind.highlights
        await self.stop_mind()
        self.assertNotIn(original, (self.root / "context.json").read_text())
        await self.start_mind()
        self.assertEqual(self.mind.highlights, highlight_ids)
        self.assertIn(original, self.mind.system)

        await self.hear("What did I say about the mandolin?", 1)
        await self.hear(f"That's wrong, {corrected}", 2)
        self.assertNotIn("blue cupboard", self.mind.system)
        self.assertIn("stairwell chest", self.mind.system)
        await self.stop_mind()
        await self.start_mind()
        self.assertIn("stairwell chest", self.mind.system)

        await self.hear("What did I say about the mandolin?", 1)
        await self.hear("Forget that", 2)
        self.assertEqual(self.mind.highlights, ())
        self.assertNotIn("stairwell chest", self.mind.system)
        self.assertFalse(self.memory.contains("stairwell chest"))
        await self.stop_mind()
        await self.start_mind()
        self.assertEqual(self.mind.highlights, ())
        self.assertNotIn("stairwell chest", self.mind.system)


class SlotPersistence(MindCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.warmed()
        self.slot_file = self.root / "cortex" / SLOT_FILE
        self.slot_file.parent.mkdir()
        self.saves = 0
        self.save_success = True
        slot = self.mind.cortex.slot

        async def write_slot(action, filename=SLOT_FILE):
            if action != "save":
                return await slot(action, filename)
            self.saves += 1
            self.slot_file.write_text(f"{'complete' if self.save_success else 'partial'} {self.saves}")
            return self.save_success

        self.mind.cortex.slot = write_slot

    async def sleep_save(self):
        self.mind.state = "asleep"
        await self.mind._housekeep()

    async def test_shutdown_reuses_an_unchanged_sleep_save(self):
        await self.sleep_save()
        await self.stop_mind()
        self.assertEqual(self.saves, 1)
        self.assertEqual(self.slot_file.read_text(), "complete 1")

    async def test_shutdown_finishes_background_cancellation_before_saving(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def warmup():
            try:
                started.set()
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                stopped.set()

        self.mind._warm_task = asyncio.create_task(warmup())
        await started.wait()
        slot = self.mind.cortex.slot

        async def save(action, filename=SLOT_FILE):
            if action == "save":
                self.assertTrue(stopped.is_set())
            return await slot(action, filename)

        self.mind.cortex.slot = save
        await self.stop_mind()
        self.assertEqual(self.saves, 1)

    async def test_model_turn_requires_a_new_slot_save(self):
        await self.sleep_save()
        await self.hear(GREET_WITH_TOPIC, 1)
        await self.mind._turn
        await self.stop_mind()
        self.assertEqual(self.saves, 2)
        self.assertEqual(self.slot_file.read_text(), "complete 2")

    async def test_shutdown_retries_a_failed_sleep_save(self):
        self.save_success = False
        await self.sleep_save()
        self.assertEqual(self.slot_file.read_text(), "partial 1")
        self.save_success = True
        await self.stop_mind()
        self.assertEqual(self.saves, 2)
        self.assertEqual(self.slot_file.read_text(), "complete 2")

    async def test_shutdown_replaces_a_missing_saved_slot(self):
        await self.sleep_save()
        self.slot_file.unlink()
        await self.stop_mind()
        self.assertEqual(self.saves, 2)
        self.assertEqual(self.slot_file.read_text(), "complete 2")

    async def test_forgetting_cancels_warmup_and_unlinks_a_late_append(self):
        self.memory.remember("my orchid is called Velvetbox", "Richard")
        await self.sleep_save()
        warmup_started = asyncio.Event()
        warmup_stopped = asyncio.Event()

        async def warmup():
            try:
                warmup_started.set()
                await asyncio.Future()
            finally:
                warmup_stopped.set()

        self.mind._warm_task = asyncio.create_task(warmup())
        await warmup_started.wait()
        slot = self.mind.cortex.slot

        async def finish_erase(action):
            if action == "erase":
                self.assertTrue(warmup_stopped.is_set())
                self.assertFalse(self.slot_file.exists())
                # A save already running can finish its checkpoint append
                # after the first unlink but before ERASE acknowledges.
                self.slot_file.write_text("old model checkpoint")
            return await slot(action)

        self.mind.cortex.slot = finish_erase
        await self.hear("Forget that my orchid is called Velvetbox", 1)
        self.bus.sent.clear()
        await self.hear("Yes", 1)
        self.assertFalse(self.slot_file.exists())
        self.assertFalse(self.memory.contains("Velvetbox"))
        self.assertIn("That's forgotten", said(self.bus))

    async def test_restart_does_not_restore_a_slot_while_invalidation_is_pending(self):
        await self.hear(GREET_WITH_TOPIC, 1)
        await self.mind._turn
        self.memory.remember("my orchid is called Velvetbox", "Richard")
        await self.sleep_save()
        slot = self.mind.cortex.slot

        async def fail_erase(action):
            if action == "erase":
                self.slot_file.write_text("late checkpoint append")
                return False
            return await slot(action)

        self.mind.cortex.slot = fail_erase
        await self.hear("Forget that my orchid is called Velvetbox", 2)
        await self.hear("Yes", 3)
        await self.mind._turn
        revision = self.memory.revision
        messages = self.mind.messages.copy()
        await self.stop_mind()
        saved = json.loads((self.root / "context.json").read_text())
        self.assertEqual(saved["memory_revision"], revision)
        self.assertTrue(saved["cache_invalidated"])
        self.assertTrue(self.slot_file.exists(), "the late append should survive the interrupted erase")

        self.cortex_server.slot_calls.clear()
        await self.start_mind()
        await self.warmed()
        self.assertEqual(self.mind.messages, messages)
        self.assertIn(("action=erase", SLOT_FILE), self.cortex_server.slot_calls)
        self.assertNotIn(("action=restore", SLOT_FILE), self.cortex_server.slot_calls)
        self.assertFalse(self.slot_file.exists())


class Continuity(MindCase):
    async def test_picks_up_the_thread_after_a_restart(self):
        await self.hear(GREET_WITH_TOPIC, 1)
        messages = list(self.mind.messages)
        await self.stop_mind()
        self.assertIn(("action=save", "being.slot"), self.cortex_server.slot_calls)

        await self.start_mind()
        self.assertEqual(self.mind.messages, messages)
        for _ in range(100):
            if ("action=restore", "being.slot") in self.cortex_server.slot_calls:
                break
            await asyncio.sleep(0.02)
        self.assertIn(("action=restore", "being.slot"), self.cortex_server.slot_calls)

        await self.hear("How are you handling the update?", 1)
        sent = self.cortex_server.chats[-1]["messages"]
        self.assertEqual(sent[1 : 1 + len(messages)], [
            {"role": message["role"], "content": message["content"]} for message in messages
        ])

    async def test_casual_history_survives_expiry_and_can_be_corrected_and_forgotten(self):
        self.cortex_server.reply = lambda _: "[neutral] Velvetbox sounds like a fitting name."
        await self.hear("I called my orchid Velvetbox", 1)
        await self.hear("The dahlias are in pots", 2)
        await self.stop_mind()
        path = self.root / "context.json"
        saved = json.loads(path.read_text())
        saved["saved"] = time.time() - 7 * 24 * 3600
        path.write_text(json.dumps(saved))
        await self.start_mind()
        self.assertEqual(self.mind.messages, [])

        await self.hear("What did I say about orchids?", 1)
        self.assertIn("you said: I called my orchid Velvetbox", said(self.bus))
        await self.hear("That's wrong, I called my orchid Silverleaf", 2)
        self.assertFalse(self.memory.contains("Velvetbox"))
        self.assertTrue(self.memory.contains("The dahlias are in pots"))
        await self.stop_mind()
        await self.start_mind()

        await self.hear("What did I say about orchids?", 1)
        self.assertIn("you said: I called my orchid Silverleaf", said(self.bus))
        self.assertNotIn("Velvetbox", said(self.bus))
        await self.hear("Forget that", 2)
        self.assertFalse(self.memory.contains("Silverleaf"))
        self.assertNotIn("Silverleaf", path.read_text())
        self.bus.sent.clear()
        await self.hear("What did I say about orchids?", 1)
        self.assertEqual(said(self.bus), "I don't remember anything about that.")

    async def test_past_exchange_is_supplied_during_ordinary_conversation(self):
        await self.hear("The café in Hobart was named Moonfern", 1)
        await self.hear("Remember that I like café au lait", 2)
        await self.stop_mind()
        path = self.root / "context.json"
        path.unlink()
        await self.start_mind()
        await self.hear("Which café did we talk about in Hobart?", 1)
        turn = self.cortex_server.chats[-1]["messages"][-1]["content"]
        self.assertIn("past conversation:", turn)
        self.assertIn("The café in Hobart was named Moonfern", turn)
        await self.hear("That's wrong, the café in Hobart was named Fernlea", 2)
        self.assertFalse(self.memory.contains("Moonfern"))
        self.assertTrue(self.memory.contains("Fernlea"))

    async def test_recall_respects_who_said_it(self):
        self.cortex_server.reply = lambda _: "[neutral] The café was fake."
        await self.hear("The café in Hobart was named Moonfern", 1)

        self.bus.sent.clear()
        await self.hear("What did I say about the café?", 1)
        self.assertIn("you said: The café in Hobart was named Moonfern", said(self.bus))

        self.bus.sent.clear()
        await self.hear("What did you say about the café?", 1)
        self.assertIn("I said: The café was fake", said(self.bus))

    async def test_interrupted_memory_edit_does_not_restore_old_context(self):
        await self.hear("My orchid is called Blackwhisker", 1)
        await self.stop_mind()
        with_storage = Memory(self.root)
        try:
            source = with_storage.recall_history("orchid")[0]
            with_storage.edit(episode_ids=[source.id])
        finally:
            with_storage.close()

        await self.start_mind()
        self.assertEqual(self.mind.messages, [])
        self.assertFalse((self.root / "context.json").exists())
        async with asyncio.timeout(2):
            while self.mind._warm_task is None:
                await asyncio.sleep(0)
            await self.mind._warm_task
        self.assertIn(("action=erase", "being.slot"), self.cortex_server.slot_calls)

    async def test_a_changed_identity_starts_fresh(self):
        await self.hear(GREET_WITH_TOPIC, 1)
        await self.stop_mind()
        saved = json.loads((self.root / "context.json").read_text())
        saved["identity"] = "someone else"
        (self.root / "context.json").write_text(json.dumps(saved))
        await self.start_mind()
        self.assertEqual(self.mind.messages, [])


class Waking(MindCase):
    async def test_each_wake_is_written_down_once(self):
        woke = dict(id="abc", born=1, wakes=3, first=False, lost_power=True, new_body=False, body="lab machine")
        self.bus.push("life.woke", **woke)
        self.bus.push("life.woke", **woke)  # the brainstem repeats it to newcomers
        await asyncio.sleep(0.1)
        life = [(kind, text) for _, kind, _, text in self.memory.recent() if kind == "life"]
        self.assertEqual(life, [("life", "woke after losing power")])
        # A restarted mind does not log the same wake again.
        await self.stop_mind()
        await self.start_mind()
        self.bus.push("life.woke", **woke)
        await asyncio.sleep(0.1)
        self.assertEqual(len([1 for _, kind, _, _ in self.memory.recent() if kind == "life"]), 1)

    async def test_a_birth_and_a_new_body_are_named(self):
        self.bus.push("life.woke", id="abc", wakes=1, first=True, lost_power=False, new_body=False)
        self.bus.push("life.woke", id="abc", wakes=2, first=False, lost_power=False, new_body=True)
        await asyncio.sleep(0.1)
        life = [text for _, kind, _, text in self.memory.recent() if kind == "life"]
        self.assertEqual(life, ["born", "woke in a new body"])

    async def test_its_first_hello_after_birth_is_its_first_words(self):
        born = dict(id="abc", born=1, wakes=1, first=True, lost_power=False, new_body=False)
        self.bus.push("life.woke", **born)
        await self.hear("Hello", 1)
        self.assertEqual(said(self.bus), "Oh. Hello. I'm Pip. You're the very first person I've met.")
        self.assertEqual(self.bus.of("mind.emotion")[-1]["name"], "joy")
        # Said once only, however often the brainstem repeats the birth.
        self.bus.push("life.woke", **born)
        self.bus.sent.clear()
        await self.hear("Hello", 1)
        self.assertEqual(said(self.bus), "Hi. Good to hear you.")
        # A mind restarted on its birth day finds the birth already logged.
        await self.stop_mind()
        await self.start_mind()
        self.bus.push("life.woke", **born)
        await self.hear("Hello", 1)
        self.assertEqual(said(self.bus), "Hi. Good to hear you.")

    async def test_an_ordinary_wake_has_no_first_words(self):
        self.bus.push("life.woke", id="abc", born=1, wakes=4, first=False, lost_power=False, new_body=False)
        await self.hear("Hello", 1)
        self.assertEqual(said(self.bus), "Hi. Good to hear you.")


class Character(MindCase):
    """Canned replies carry a feeling, so the face acts as it speaks."""

    def emotion(self):
        return self.bus.of("mind.emotion")[-1]["name"]

    async def test_remembering_and_recalling_for_you_is_a_joy(self):
        await self.hear("Remember that my cat is called Tom", 1)
        self.assertEqual(self.emotion(), "joy")
        await self.hear("What do you remember about my cat?", 2)
        self.assertEqual(self.emotion(), "joy")
        self.bus.sent.clear()
        await self.hear("What do you remember about my kite?", 1)
        self.assertEqual(said(self.bus), "I don't remember anything about that.")
        self.assertEqual(self.emotion(), "concern")

    async def test_an_unclear_forget_looks_concerned(self):
        await self.hear("Forget that", 1)
        self.assertIn("not sure what to forget", said(self.bus))
        self.assertEqual(self.emotion(), "concern")

    async def test_its_name_is_said_with_joy(self):
        await self.hear("What is your name?", 1)
        self.assertEqual(said(self.bus), "My name is Pip.")
        self.assertEqual(self.emotion(), "joy")

    async def test_a_low_unplugged_battery_is_a_worry(self):
        self.bus.push("body.state", hour=19, minute=5, battery=9, charging=False, on_ac=False)
        await self.hear("What's your battery level?", 1)
        self.assertEqual(said(self.bus), "My battery is at 9 percent. I'm running on battery.")
        self.assertEqual(self.emotion(), "concern")

    async def test_it_says_good_night_only_at_night_in_the_owners_zone(self):
        await self.hear("Set my time zone to Australia slash Sydney", 1)
        noon = datetime(2025, 2, 20, 1, 0, tzinfo=ZoneInfo("UTC"))  # Sydney noon
        with patch("mind.skills.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone: noon.astimezone(zone)
            self.bus.sent.clear()
            self.bus.push("state.changed", state="attending")
            self.bus.push("hear.final", text="Good night", speaker="Richard")
            await self.bus.wait("mind.reply_done")
        self.assertEqual(said(self.bus), "Okay. I'll rest my eyes for a while.")
        self.assertEqual(self.emotion(), "sleepy")


class WakingUp(MindCase):
    async def test_it_reads_who_it_is_before_anyone_speaks(self):
        await self.warmed()
        self.assertNotIn(("action=restore", "being.slot"), self.cortex_server.slot_calls)
        first = self.cortex_server.prefills[-1]
        self.assertEqual(first["n_predict"], 0)
        self.assertEqual(first["prompt"], "<|system|>\nYou are Pip.<|end|>\n<|user|>\n")
        # The first reply then extends that reading.
        await self.hear(GREET_WITH_TOPIC, 1)
        self.assertTrue(render(self.cortex_server.chats[0]["messages"]).startswith(first["prompt"]))

    async def test_successful_restore_is_not_overwritten_by_a_shorter_warmup(self):
        await self.warmed()
        await self.hear(GREET_WITH_TOPIC, 1)
        await self.stop_mind()
        count = len(self.cortex_server.prefills)
        await self.start_mind()
        await self.warmed()
        self.assertIn(("action=restore", "being.slot"), self.cortex_server.slot_calls)
        self.assertEqual(len(self.cortex_server.prefills), count)


class Small(unittest.TestCase):
    def test_speakable_time(self):
        self.assertEqual(speakable_time(0, 5), "12:05 in the morning")
        self.assertEqual(speakable_time(9, 30), "9:30 in the morning")
        self.assertEqual(speakable_time(12, 0), "12:00 in the afternoon")
        self.assertEqual(speakable_time(19, 5), "7:05 in the evening")
        self.assertEqual(speakable_time(23, 59), "11:59 at night")


if __name__ == "__main__":
    unittest.main()
