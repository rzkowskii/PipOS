import unittest
from datetime import date, datetime

from mind.commands import Command, greeting, history_query, may_be_command, parse


class CommandsTest(unittest.TestCase):
    def check(self, said, kind, arg=""):
        self.assertEqual(parse(said, "Pip"), Command(kind, arg), said)

    def test_remember(self):
        self.check("Remember that my cat is called Tom.", "remember", "my cat is called Tom")
        self.check("Pip, remember that the bins go out on Tuesday", "remember", "the bins go out on Tuesday")
        self.check("please remember I parked on level three", "remember", "I parked on level three")
        self.check("Hey Pip, don't forget that Anna is visiting", "remember", "Anna is visiting")

    def test_a_question_about_memory_is_recall_not_remember(self):
        self.check("Do you remember my cat's name?", "recall", "my cat's name")
        self.check("What do you remember about my sister?", "recall", "my sister")
        self.check("what do you know about the bins", "recall", "the bins")
        self.check("What have I told you about Anna?", "recall_heard", "Anna")
        self.check("Could you remember what we talked about Friday?", "recall", "Friday")

    def test_recall_ordinary_conversations(self):
        self.check("What did you say about orchids?", "recall_said", "orchids")
        self.check("What did I tell you about Hobart?", "recall_heard", "Hobart")
        self.check("What did I say about the coffee shop?", "recall_heard", "the coffee shop")
        self.check("When did I mention the coffee shop?", "recall_heard", "the coffee shop")
        self.check("What did we talk about yesterday?", "recall", "yesterday")
        self.check("Pip, what were we talking about?", "recall")

    def test_immediate_remind_me_questions_leave_recall_for_the_conversation_path(self):
        for said in (
            "Remind me where we stopped for coffee before the boat.",
            "Could you remind me where we said we'd put that thing?",
            "Can you remind me which pattern number I landed on for the linen apron?",
            "Pip, please remind me which drawer has the fuses.",
            "Remind me what I said about the coffee.",
            "Remind me when we met our host.",
            "Remind me who brought the coffee.",
            "Remind me how I fixed the latch.",
            "Remind me why we postponed the walk.",
            "Remind me of the choir detail we discussed.",
        ):
            with self.subTest(said=said):
                self.assertIsNone(parse(said, "Pip"))

    def test_timed_reminders_can_include_an_interrogative_in_the_task(self):
        now = datetime(2026, 11, 21, 9)
        for said, task in (
            ("Remind me to check where we stopped for coffee in ten minutes", "check where we stopped for coffee"),
            ("Remind me in ten minutes to check which pattern I chose", "check which pattern I chose"),
        ):
            with self.subTest(said=said):
                command = parse(said, "Pip", now=now)
                self.assertEqual(command.kind, "reminder")
                self.assertEqual(command.reminder.kind, "create")
                self.assertEqual(command.reminder.task, task)
                self.assertEqual(command.reminder.due_at, now.timestamp() + 600)

    def test_history_query(self):
        today = date(2026, 11, 21)
        for text, topic, start_day, end_day in [
            ("orchids", "orchids", None, None),
            ("Friday the film", "Friday the film", None, None),
            ("2026-02-30", "2026-02-30", None, None),
            ("on 9999-12-31", "on 9999-12-31", None, None),
            ("on 2026-11-19", "", 19, 20),
            ("orchids today", "orchids", 21, 22),
            ("yesterday", "", 20, 21),
            ("orchids last week", "orchids", 9, 16),
            ("orchids on Tuesday", "orchids", 17, 18),
            ("last Saturday", "", 14, 15),
        ]:
            with self.subTest(text=text):
                start = datetime(2026, 11, start_day).timestamp() if start_day else None
                end = datetime(2026, 11, end_day).timestamp() if end_day else None
                self.assertEqual(history_query(text, today=today), (topic, start, end))

    def test_forget(self):
        self.check("Forget that.", "forget_last")
        self.check("forget it", "forget_last")
        self.check("Forget about my sister's birthday", "forget", "my sister's birthday")
        self.check("forget what I said about the bins", "forget", "the bins")

    def test_correct(self):
        self.check("That's wrong, my cat is called Tim", "correct", "my cat is called Tim")
        self.check("No, that is not right. The bins go out on Monday.", "correct", "The bins go out on Monday")

    def test_small_things(self):
        self.check("What time is it?", "time")
        self.check("what's the time", "time")
        self.check("What is your name?", "name")
        self.check("Hey Pip, what should I call you?", "name")
        self.check("Good night.", "sleep")
        self.check("Pip, go to sleep", "sleep")
        self.check("Look at this!", "look")

    def test_date_and_weather_shortcuts(self):
        for said, kind, arg in [
            ("Pip, what’s today's date?", "date", ""),
            ("What day is today?", "day", ""),
            ("What day of the week is it?", "day", ""),
            ("What date is it tomorrow?", "date_tomorrow", ""),
            ("What's the weather like?", "weather_current", ""),
            ("What is it like outside right now?", "weather_current", ""),
            ("Tomorrow's forecast in Sydney, Australia", "weather_tomorrow", "Sydney, Australia"),
            ("What's the weather in Hobart tomorrow?", "weather_tomorrow", "Hobart"),
            ("What's the weather going to be like tomorrow?", "weather_tomorrow", ""),
            ("Will it rain today in Bathurst?", "weather_today", "Bathurst"),
            ("Set my weather city to Sydney, Australia", "weather_location", "Sydney, Australia"),
        ]:
            with self.subTest(said=said):
                self.check(said, kind, arg)
        for partial in ("what's the date", "what's the weather", "will it rain", "set my weather",
                        "look up tomorrow's weather", "what's tomorrow's forecast"):
            self.assertTrue(may_be_command(partial), partial)
        self.assertIsNone(parse("Weather is what you get."))

    def test_whole_social_turns(self):
        for text, kind in [
            ("Good morning, Pip!", "hello"),
            ("hey pip good morning", "hello"),
            ("Hello there again.", "hello"),
            ("hello there how are you today", "check_in"),
            ("Hi Pip, how’s it going?", "check_in"),
            ("Are you there, Pip?", "presence"),
        ]:
            self.assertEqual(greeting(text, "Pip"), kind, text)
        self.assertEqual(greeting("Hi River Song, how are you doing?", "River Song"), "check_in")

    def test_social_openings_keep_the_rest_of_the_turn(self):
        for text in [
            "Hi Pip, how does a rainbow form?",
            "Hey there, I moved the fern to the hall",
            "How are you handling the update",
            "Hello Kitty is my niece's favourite",
            "Morning fog made the lake disappear",
            "Please repeat the words hello there",
            "Hey Pip, remember that the keys are downstairs",
            "Good night",
        ]:
            self.assertIsNone(greeting(text, "Pip"), text)

    def test_ordinary_conversation_is_not_a_command(self):
        for said in [
            "Hello there",
            "I remember when we went to the beach",
            "How do you remember things?",
            "I keep forgetting my keys",
            "It was a good night out",
            "The time flies",
            "remember",
            "Is that wrong?",
            "What was that café we talked about before I went to Hobart?",
        ]:
            self.assertIsNone(parse(said, "Pip"), said)


if __name__ == "__main__":
    unittest.main()
