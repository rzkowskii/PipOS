import unittest
from datetime import date, datetime

from mind.commands import (
    Command, clock_time, correction_value, greeting, history_query, may_be_command, parse, reflect, ring_reply,
    snooze_minutes, splice_correction, subject_of, volume_level,
)


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
        self.check("What did we talk about on 2026-11-19?", "recall", "on 2026-11-19")
        self.check("What did we talk about today?", "recall", "today")
        self.check("Pip, what were we talking about?", "talked_about")
        self.check("What did we talk about earlier?", "talked_about")

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
        self.check("forget it", "never_mind")
        self.check("Never mind", "never_mind")
        self.check("forget this", "forget_last")
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


class NewCommandsTest(unittest.TestCase):
    def check(self, said, kind, arg=""):
        self.assertEqual(parse(said, "Pip"), Command(kind, arg), said)

    def test_remembering_a_question_is_not_a_fact(self):
        for said in ("Remember when we went fishing?", "Can you remember names?",
                     "remember how we met", "Remember what I told you"):
            with self.subTest(said=said):
                self.assertIsNone(parse(said, "Pip"))

    def test_a_fact_introduced_with_that_is_kept_whatever_its_first_word(self):
        # Sent to the model, "remember to ..." would get "I will!" with nothing kept.
        self.check("Remember that when I'm tired I get grumpy", "remember", "when I'm tired I get grumpy")
        self.check("Don't forget that how I take my tea is with milk", "remember", "how I take my tea is with milk")
        self.check("Remember to take the bins out on Tuesday", "remember", "to take the bins out on Tuesday")

    def test_only_one_value_subjects_can_be_replaced(self):
        from mind.commands import replaceable_subject
        self.assertEqual(replaceable_subject("my cat is called Tom"), "my cat is called")
        self.assertEqual(replaceable_subject("my birthday is in May"), "my birthday is in")
        self.assertEqual(replaceable_subject("my sister lives in Perth"), "my sister lives in")
        self.assertIsNone(replaceable_subject("I like tea"), "liking coffee too replaces nothing")
        self.assertIsNone(replaceable_subject("I went to Rome"))

    def test_memory_questions_with_their_own_answers(self):
        self.check("Do you remember me?", "remember_me")
        self.check("Pip, do you know who I am?", "remember_me")
        self.check("What do you know about me?", "about_me")
        self.check("What do you remember?", "about_me")
        self.check("What were we talking about?", "talked_about")
        self.check("What did we talk about about the garden", "recall", "about the garden")

    def test_honest_self_questions(self):
        for said, kind in [("Are you alive?", "self_alive"), ("Are you sentient", "self_aware"),
                           ("Are you conscious?", "self_aware"), ("Do you have feelings?", "self_feel"),
                           ("Can you feel anything?", "self_feel"), ("What are you?", "self_what"),
                           ("Are you real?", "self_what")]:
            self.check(said, kind)
        self.assertIsNone(parse("What are you doing later?", "Pip"))

    def test_volume_and_clock(self):
        self.check("Speak louder", "volume_up")
        self.check("I can't hear you", "volume_up")
        self.check("Pip, be quieter please", "volume_down")
        self.check("You're too loud", "volume_down")
        self.check("Set your volume to 40 percent", "volume_set", "40 percent")
        self.check("Set the time to 3:45 pm", "set_time", "3:45 pm")
        self.check("The time is 9 in the morning", "set_time", "9 in the morning")
        self.check("Set the time to 3", "set_time", "3")
        for partial in ("speak louder", "set your volume", "are you alive", "set the time"):
            self.assertTrue(may_be_command(partial), partial)

    def test_battery_phrasings(self):
        for said in ("How's your battery?", "How is your battery doing?", "Tell me about your battery",
                     "Are you charging?", "How long will your battery last?"):
            self.check(said, "battery")

    def test_battery_health_belongs_to_the_body_handler(self):
        for said in ("How's your battery health?", "How is your battery health"):
            self.assertIsNone(parse(said, "Pip"), said)

    def test_look_no_longer_takes_what_is_this(self):
        self.assertIsNone(parse("What is this?", "Pip"))
        self.check("What do you see?", "look")

    def test_its_name_alone_is_a_hello(self):
        for text in ("Pip", "Hey Pip.", "hello Pip", "Hi, Pip!", "hey"):
            self.assertEqual(greeting(text, "Pip"), "hello", text)

    def test_spoken_numbers_and_times(self):
        self.assertEqual([volume_level(x) for x in ("forty", "forty five percent", "70", "max", "half", "101", "loud")],
                         [40, 45, 70, 100, 50, None, None])
        self.assertEqual(clock_time("3:45 pm"), (15, 45))
        self.assertEqual(clock_time("12 am"), (0, 0))
        self.assertEqual(clock_time("9 in the morning"), (9, 0))
        self.assertEqual(clock_time("7 in the evening"), (19, 0))
        self.assertEqual(clock_time("15:05"), (15, 5))
        self.assertIsNone(clock_time("3"))
        self.assertIsNone(clock_time("13 pm"))

    def test_reflect_turns_the_owners_words_round(self):
        self.assertEqual(reflect("my cat is called Tom"), "your cat is called Tom")
        self.assertEqual(reflect("I'm going to Amsterdam with my sister"), "you're going to Amsterdam with your sister")
        self.assertEqual(reflect("I am here", start=True), "You are here")

    def test_a_short_correction_is_spliced_only_where_it_clearly_fits(self):
        for fact, value, result in [
            ("my cat is called Tom", "Jim", "my cat is called Jim"),
            ("my birthday is in May", "June", "my birthday is in June"),
            ("the bins go out on Tuesday", "Monday", "the bins go out on Monday"),
            ("hunter2", "swordfish", None),
            ("my cat is called Tom", "a very long new name for him", None),
            ("my orchid is called Silverleaf", "called Moonleaf", "my orchid is called Moonleaf"),
            ("my dog is named Rex", "named Bo", "my dog is named Bo"),
        ]:
            with self.subTest(fact=fact):
                self.assertEqual(splice_correction(fact, value), result)
        self.assertEqual(correction_value("it's Jim"), "Jim")
        self.assertEqual(correction_value("No, actually it's Canberra."), "Canberra")
        self.assertIsNone(correction_value("my cat is called Jim"))
        self.assertEqual(subject_of("my cat is called Tom"), subject_of("My cat is called Jim").lower())


if __name__ == "__main__":
    unittest.main()


class RoutinesCommandsTest(unittest.TestCase):
    """L5.2, L5.4, L5.8 and L5.9: new command kinds and the public gate."""

    def check(self, said, kind, arg=""):
        self.assertEqual(parse(said, "Pip"), Command(kind, arg), said)

    def test_a_past_day_is_the_diary_and_recall_still_works(self):
        for said, arg in (("What did we do yesterday?", "yesterday"),
                          ("Pip, what did we talk about on Monday?", "on Monday"),
                          ("What were we talking about last Friday", "last Friday"),
                          ("What did we do last week?", "last week")):
            with self.subTest(said=said):
                self.check(said, "diary", arg)
                topic, start, end = history_query(arg)
                self.assertEqual(topic, "")
                self.assertIsNotNone(start)
        for said, kind, arg in (("What did we talk about the orchid?", "recall", "the orchid"),
                                ("What do you remember about my orchid?", "recall", "my orchid"),
                                ("What did I say about the orchid?", "recall_heard", "the orchid"),
                                ("What did we talk about?", "talked_about", ""),
                                ("What did we do?", "talked_about", ""),
                                ("What did we talk about Monday's film?", "recall", "Monday's film")):
            with self.subTest(said=said):
                self.check(said, kind, arg)

    def test_snooze_and_acknowledgement(self):
        for said, minutes in (("Snooze", "10"), ("Snooze for ten minutes", "10"), ("snooze it for 5 minutes", "5"),
                              ("Five more minutes", "5"), ("Give me 15 more minutes please", "15"),
                              ("A few more minutes", "5"), ("Snooze the alarm for half an hour", "30")):
            with self.subTest(said=said):
                self.check(said, "snooze", minutes)
        for said in ("Stop the alarm", "Turn off the timer", "Dismiss my reminder", "I'm up", "I am awake"):
            with self.subTest(said=said):
                self.check(said, "ack")
        for said in ("Okay", "thanks", "I need five more minutes", "Give me lots more minutes", "stop"):
            with self.subTest(said=said):
                self.assertIsNone(parse(said, "Pip"))
        self.assertIsNone(snooze_minutes("a thousand"))
        self.assertIsNone(snooze_minutes("three hours"))

    def test_while_ringing_short_answers_count(self):
        for said in ("Okay", "OK thanks", "Thanks, Pip", "Thank you", "got it", "Stop", "I'm up", "alright then",
                     "Pip, stop the alarm"):
            with self.subTest(said=said):
                self.assertEqual(ring_reply(said, "Pip"), Command("ack"))
        self.assertEqual(ring_reply("Snooze", "Pip"), Command("snooze", "10"))
        self.assertEqual(ring_reply("ten more minutes", "Pip"), Command("snooze", "10"))
        for said in ("Tell me a story", "What time is it?", "okay so tell me about dogs"):
            with self.subTest(said=said):
                self.assertIsNone(ring_reply(said, "Pip"))

    def test_time_elsewhere(self):
        for said, kind, arg in (
            ("What time is it in London?", "world_time", "London"),
            ("What's the time in New York right now?", "world_time", "New York"),
            ("Pip, what time is it now in Tokyo", "world_time", "Tokyo"),
            ("What's the time difference to London?", "time_difference", "London"),
            ("What's the time difference between London and Tokyo?", "time_difference", "London and Tokyo"),
            ("How many hours ahead is Perth?", "time_difference", "Perth"),
            ("How far behind is London from here?", "time_difference", "London"),
            ("Is London ahead or behind us?", "time_difference", "London"),
        ):
            with self.subTest(said=said):
                self.check(said, kind, arg)
        self.check("What time is it?", "time")

    def test_help(self):
        for said, kind, arg in (
            ("What can you do?", "skills", ""), ("Help", "skills", ""), ("What can I say?", "skills", ""),
            ("What else can you do?", "skills_more", ""),
            ("What can you do with lists?", "skills_group", "lists"),
            ("What can you do with the terminal?", "skills_group", "the terminal"),
            ("What can you do with your body?", "skills_group", "your body"),
            ("Help me with alarms", "skills_group", "alarms"),
            ("What can you do for me?", "skills", ""),
        ):
            with self.subTest(said=said):
                self.check(said, kind, arg)

    def test_help_with_something_it_has_no_help_for_is_conversation(self):
        for said in ("Help me with my homework", "help me with it", "What can you do with a paperclip?",
                     "help with dinner", "what can you do about me"):
            with self.subTest(said=said):
                self.assertIsNone(parse(said, "Pip"), said)
        self.check("Help me with the shopping list", "skills_group", "the shopping list")
        self.check("What can you do with timers?", "skills_group", "timers")

    def test_the_public_gate(self):
        for partial in ("wake me up at", "set an alarm", "every weekday at", "snooze", "five more minutes",
                        "how long left", "what time is it in", "what's the time difference",
                        "what else can you do", "what did we do", "I'm up", "help", "stop the alarm"):
            with self.subTest(partial=partial):
                self.assertTrue(may_be_command(partial, "Pip"), partial)
                self.assertTrue(may_be_command(partial), partial)
        for said in ("tell me a story", "I like the sea", "how does a rainbow form", "every so often I sing",
                     "stop being silly"):
            with self.subTest(said=said):
                self.assertFalse(may_be_command(said, "Pip"), said)
