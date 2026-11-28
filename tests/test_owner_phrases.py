"""Every phrase docs/PIP-FUNCTIONS.md section 2 marks "code" is answered in
code, with zero model calls; near misses still reach the model (task L3.11).

The mind runs whole, with the real command, workshop and everyday modules,
a stand-in hands organ and a stand-in model server that counts its calls.
"""

import asyncio
import importlib.util
from contextlib import contextmanager, nullcontext
from datetime import datetime
from unittest import mock

from mind import action_log
from tests.test_mind import MindCase
from tests.test_turn_apps import Hands

# (module that answers it, phrase). The module is only checked for being
# present in this build; the phrase must then be answered in code.
CODE_PHRASES = [
    # 2.0 Reaching Pip
    ("readback", "What did you hear?"),
    ("readback", "What did I just say?"),
    ("readback", "Say that again"),
    ("readback", "Repeat that"),
    ("readback", "Pardon?"),
    ("readback", "What did you say?"),
    ("body_talk", "How well can you hear me?"),
    ("body_talk", "Check your hearing"),
    ("commands", "Speak up"),
    ("commands", "Be quieter"),
    ("commands", "Set your volume to 5"),
    ("voice_engines", "Use your natural voice"),
    ("voice_engines", "Use your original voice"),
    ("voice_engines", "Which voice are you using?"),
    # 2.1 Time and routines
    ("commands", "What time is it?"),
    ("commands", "What's the date?"),
    ("commands", "What day is it?"),
    ("commands", "What's tomorrow's date?"),
    ("commands", "What time is it in London?"),
    ("commands", "What's the time difference to London?"),
    ("commands", "Set the time to 7:30 pm"),
    ("commands", "The time is 7:30"),
    ("commands", "Set a timer for 10 minutes"),
    ("commands", "What timers do I have?"),
    ("commands", "How long left on my timer?"),
    ("commands", "Cancel my timer"),
    ("commands", "Remind me to call Mum tomorrow at 9 am"),
    ("commands", "What are my reminders?"),
    ("commands", "Cancel the reminder about Mum"),
    ("commands", "Wake me up at 7"),
    ("commands", "Set an alarm for 6:30 tomorrow"),
    ("commands", "Every weekday at 7, remind me to take my tablets"),
    ("commands", "Every Tuesday at 8 pm, remind me to put the bins out"),
    ("commands", "Snooze"),
    ("commands", "Five more minutes"),
    ("commands", "Snooze for ten minutes"),
    ("commands", "Good morning"),
    ("commands", "Hey Pip"),
    ("commands", "How are you?"),
    ("commands", "What's the weather?"),
    # 2.2 Memory, lists and notes
    ("commands", "Remember that my orchid is called Silverleaf"),
    ("commands", "What do you remember about my orchid?"),
    ("commands", "That's wrong, it's called Moonleaf"),
    ("commands", "What did I say about the orchid?"),
    ("commands", "Forget about my sister's birthday"),
    ("commands", "Forget that"),
    ("commands", "What do you know about me?"),
    ("commands", "Do you remember me?"),
    ("commands", "What did we talk about?"),
    ("diary", "What did we do yesterday?"),
    ("diary", "What did we talk about on Monday?"),
    ("lists", "Add milk to my shopping list"),
    ("lists", "Add eggs and bread to the shopping list"),
    ("lists", "Put 'call the bank' on my to-do list"),
    ("lists", "What's on my shopping list?"),
    ("lists", "Take milk off my shopping list"),
    ("lists", "Cross off milk"),
    ("lists", "Clear my shopping list"),
    ("lists", "What lists do you have?"),
    ("lists", "Make a note: the plumber comes Thursday"),
    ("lists", "Note that the spare key is in the blue pot"),
    ("lists", "Read my notes"),
    ("lists", "Delete my last note"),
    ("commands", "Start guest mode"),
    ("commands", "End guest mode"),
    ("commands", "Don't remember this"),
    ("commands", "End guest mode"),
    ("device", "When did you last back up?"),
    ("device", "Back yourself up"),
    # 2.3 Body and system
    ("commands", "How's your battery?"),
    ("commands", "Are you charging?"),
    ("commands", "How long will your battery last?"),
    ("body_talk", "How's your battery health?"),
    ("body_talk", "How warm are you?"),
    ("body_talk", "How hot are you?"),
    ("body_talk", "Why is your fan loud?"),
    ("body_talk", "How much space do you have?"),
    ("body_talk", "How much memory are you using?"),
    ("body_talk", "How long have you been awake?"),
    ("body_talk", "How bright is it in here?"),
    ("body_talk", "Is it dark in here?"),
    ("body_talk", "How much light is in the room?"),
    ("doctor", "Are you okay?"),
    ("doctor", "Run a check-up"),
    ("doctor", "What's wrong?"),
    ("action_log", "What did you do last night?"),
    ("action_log", "What did you do today?"),
    ("action_log", "What have you been up to?"),
    ("device", "Stop listening"),
    ("device", "Brighter"),
    ("device", "Dimmer"),
    ("device", "Set your brightness to 50 percent"),
    ("device", "Shut down"),
    ("device", "Switch off"),
    ("device", "Restart yourself"),
    ("device", "Go back to how you were"),
    ("device", "What version are you?"),
    ("device", "Did your update work?"),
    ("commands", "Go to sleep"),
    ("commands", "Good night"),
    # 2.5 Conversation and character
    ("commands", "What can you do?"),
    ("commands", "What else can you do?"),
    ("commands", "What can you do with lists?"),
    ("commands", "Are you alive?"),
    ("commands", "Are you sentient?"),
    ("commands", "Do you have feelings?"),
    ("commands", "What are you?"),
    ("commands", "What's your name?"),
    ("commands", "How old are you?"),
    ("commands", "What did you dream about?"),
    ("commands", "Did you dream last night?"),
    ("calc", "What's 17 times 23?"),
    ("calc", "What's 15 percent of 80?"),
    ("calc", "What's 120 divided by 7?"),
    ("calc", "Square root of 144"),
    ("calc", "What's 2 to the power of 10?"),
    ("units", "How many grams in a pound?"),
    ("units", "180 Fahrenheit in Celsius"),
    ("units", "Convert 5 miles to kilometres"),
    ("units", "How many cups in a litre?"),
    ("play", "How do you spell necessary?"),
    ("play", "Tell me a joke"),
    ("play", "Tell me a fun fact"),
    ("play", "Let's play a game"),
    ("play", "Guess the number"),
    ("play", "I give up"),
    ("play", "Rock paper scissors"),
    ("play", "Stop the game"),
    ("play", "Twenty questions"),
    ("play", "Stop the game"),
    ("commands", "Take a look at this"),
    ("commands", "What do you see?"),
]

# A word each handler's answer must contain, so a phrase is known to have
# reached its own handler, not merely some code.
EXPECT = {
    "What time is it in London?": "London",
    "How long left on my timer?": "left",
    "Wake me up at 7": "alarm",
    "Every weekday at 7, remind me to take my tablets": "every weekday",
    "Remember that my orchid is called Silverleaf": "remember",
    "What do you remember about my orchid?": "Silverleaf",
    "Add milk to my shopping list": "milk",
    "What's on my shopping list?": "milk",
    "Make a note: the plumber comes Thursday": "plumber",
    "How's your battery?": "battery",
    "How's your battery health?": "battery",
    "How warm are you?": "temperature",
    "How hot are you?": "temperature",
    "Why is your fan loud?": "fan",
    "How much space do you have?": "free",
    "How much memory are you using?": "memory",
    "How bright is it in here?": "light is in the room",
    "Is it dark in here?": "light is in the room",
    "How much light is in the room?": "light",
    "Stop listening": "not listening",
    "Set your brightness to 50 percent": "50",
    "Shut down": "switch off",
    "Restart yourself": "restart",
    "What version are you?": "build",
    "Use your natural voice": "voice",
    "How well can you hear me?": "hearing",
    "What did you do last night?": "last night",
    "What's 17 times 23?": "three hundred and ninety-one",
    "How many grams in a pound?": "grams",
    "How many cups in a litre?": "metric cup",
    "How do you spell necessary?": "N, E, C, E, S, S, A, R, Y",
    "What can you do?": "timers",
    "Are you sentient?": "I don't know",
    "What did you dream about?": "dream",
    "Open the terminal": "Here's my terminal.",
    "Install snake": "snake",
    "What can you install?": "shelf",
}


# 2.4 The workshop: said with the terminal open and a script on the go.
WORKSHOP_PHRASES = [
    "Open the terminal",
    "Hey Pip, open a new terminal, Pip",
    "And open a terminal",
    "Open a terminal window",
    "Minimise the terminal",
    "Bring back the terminal",
    "Show me your face",
    "Bring back the terminal",
    "Write a script that counts down from ten",
    "Stop writing that script for a second",
    "Carry on",
    "Hold on",
    "Carry on",
    "Cancel that",
    "In the terminal, draw a box",
    "What happened?",
    "Did it work?",
    "Run it",
    "Run that again",
    "Fix it",
    "What can you install?",
    "Install snake",
    "Play snake",
    "Run life",
    "Show me how you're feeling",
    "Open the monitor",
    "Show me the clock",
    "Show my timers",
    "Close the terminal",
]

# Ordinary conversation, some of it close to a command, that must still reach
# the model. With nothing open in the workshop, "stop", "hold on" and "carry
# on" are conversation too.
NEAR_MISSES = [
    "continue the story",
    "tell me a story",
    "stop",
    "hold on",
    "carry on",
    "what happened?",
    "did it work?",
    "how's it going with the garden",
    "I had a lovely walk by the river today",
    "my sister called me this morning",
    "the terminal at the airport was packed",
    "they opened a new terminal at the airport",
    "a terminal",
    "I need to remember to buy a present for Sam",
    "what do you think about rainy days",
    "why is the sky blue",
    "can you help me plan a picnic",
    "do you like music",
    "what's your favourite colour",
    "I'm feeling a bit tired today",
    "we went to the beach on Sunday",
    "the weather was lovely yesterday",
    "my cat knocked over a plant",
    "I'm thinking about learning the piano",
    "how do volcanoes work",
    "what should I cook for dinner",
    "I watched a film about space",
    "what makes a rainbow",
    "my friend is visiting next week",
    "I planted tomatoes in the garden",
    "the kids are at school",
    "I'm going to the shops later",
    "what's the best way to learn Spanish",
    "I love the sound of rain",
    "the dog needs a walk",
    "we should paint the fence",
    "how do bees make honey",
    "what would you do on a holiday",
    "I finished my book last night",
    "the bus was late again",
    "my back is a bit sore",
    "I think I'll have a nap",
    "the garden looks nice today",
    "what's a good name for a goldfish",
    "I'm learning to knit",
    "my battery of tests went well at the doctor's",
    "how's your day",
]


# Phrases about a time of day are asked at a fixed local time, so the test
# passes whenever it runs (MindCase's body.state says 19:05).
EVENING = datetime(2026, 11, 24, 19, 5).astimezone()
MORNING = datetime(2026, 11, 24, 9, 0).astimezone()
CLOCKED = ("action_log",)


def present(module):
    return module == "commands" or importlib.util.find_spec(f"mind.{module}") is not None


class OwnerPhrases(MindCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.hands = Hands(self.bus)
        self.bus.push("organ.up", name="hands")
        await asyncio.sleep(0)

    async def say(self, text):
        """Say it, wait for the whole reply, and return (reply, model calls)."""
        calls = len(self.cortex_server.chats)
        start = len(self.bus.of("say.request"))
        replies = len(self.bus.of("mind.reply_done"))
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker="Richard")
        await self.bus.wait("mind.reply_done", replies + 1, timeout=10.0)
        self.bus.push("say.done", final=True)
        await asyncio.sleep(0)
        spoken = " ".join(e["text"] for e in self.bus.of("say.request")[start:] if e["text"])
        return spoken, len(self.cortex_server.chats) - calls

    @contextmanager
    def clock(self, now):
        """The handlers' local time is `now`, with or without a known time zone."""
        with mock.patch.object(self.mind, "_local_now", return_value=now), \
                mock.patch.object(action_log, "local_now", return_value=now):
            yield

    def settle(self):
        """Each phrase on its own: no question left open from the one before."""
        self.mind._pending_forget = None
        self.mind._pending_confirm = None
        self.mind._ring = None
        self.mind.apps.pending = ""

    async def test_every_code_phrase_is_answered_without_the_model(self):
        self.memory.remember("my sister's birthday is in June", "Richard", None)
        for module, phrase in CODE_PHRASES:
            with self.subTest(phrase=phrase):
                if not present(module):
                    self.skipTest(f"mind/{module}.py is not in this build")
                self.settle()
                with self.clock(EVENING) if module in CLOCKED else nullcontext():
                    reply, calls = await self.say(phrase)
                self.assertEqual(calls, 0, f"{phrase!r} reached the model; it said {reply!r}")
                self.assertTrue(reply, f"{phrase!r} got no reply")
                if phrase in EXPECT:
                    self.assertIn(EXPECT[phrase].casefold(), reply.casefold(), f"{phrase!r} reached the wrong handler")

    async def test_last_night_is_last_night_in_the_morning_and_the_evening(self):
        for now in (MORNING, EVENING):
            with self.subTest(hour=now.hour):
                self.settle()
                with self.clock(now):
                    reply, calls = await self.say("What did you do last night?")
                self.assertEqual(calls, 0)
                self.assertIn("last night", reply.casefold())
                self.assertNotIn("tonight", reply.casefold())

    async def test_every_workshop_phrase_is_answered_without_the_model(self):
        for phrase in WORKSHOP_PHRASES:
            with self.subTest(phrase=phrase):
                self.settle()
                if phrase in ("Stop writing that script for a second", "Hold on", "Cancel that"):
                    # A script is being written while these are said.
                    self.bus.push("term.status", id="job1", phase="writing", source="recipe")
                elif phrase in ("What happened?", "Did it work?", "Run it", "Run that again"):
                    self.bus.push("term.status", id="job2", phase="waiting", source="recipe",
                                  file="box.py", sha256="ab")
                elif phrase == "Fix it":
                    self.bus.push("term.status", id="job3", phase="failed", source="model", exit=1,
                                  kind="script", file="box.py")
                elif phrase == "Carry on":
                    self.bus.push("term.status", id="job1", phase="paused", source="recipe")
                await asyncio.sleep(0)
                reply, calls = await self.say(phrase)
                self.assertEqual(calls, 0, f"{phrase!r} reached the model; it said {reply!r}")
                self.assertTrue(reply, f"{phrase!r} got no reply")
                if phrase in EXPECT:
                    self.assertIn(EXPECT[phrase].casefold(), reply.casefold(), f"{phrase!r} reached the wrong handler")

    async def test_near_misses_still_reach_the_model(self):
        self.assertGreaterEqual(len(NEAR_MISSES), 40)
        for phrase in NEAR_MISSES:
            with self.subTest(phrase=phrase):
                self.settle()
                reply, calls = await self.say(phrase)
                self.assertGreaterEqual(calls, 1, f"{phrase!r} was answered in code: {reply!r}")

    async def test_a_bare_hows_it_going_is_a_check_in_not_a_job_report(self):
        reply, calls = await self.say("how's it going")
        self.assertEqual(calls, 0)
        self.assertNotIn("writing", reply)
        self.assertNotIn("terminal", reply)
