import asyncio
import json
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import play, play_data
from mind.reply import Context


async def _send(*args, **fields):
    return None


class PlayCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        play._GAME.clear()
        play._LAST.clear()
        for kind in play._GUEST_RECENT:
            play._GUEST_RECENT[kind].clear()
        self.rng = random.Random(7)

    def tearDown(self):
        play._GAME.clear()
        self._tmp.cleanup()

    def ctx(self, **overrides):
        fields = dict(now=None, clock_trusted=True, speaker="Richard", guest=False, state_dir=self.dir,
                      owner="Richard", last_heard=None, last_said=None, body={}, voice={}, send=_send)
        fields.update(overrides)
        return Context(**fields)

    def say(self, text, **overrides):
        return asyncio.run(play.handle(text, self.ctx(**overrides), rng=self.rng))

    def line(self, text, **overrides):
        reply = self.say(text, **overrides)
        return reply.line if reply else None


class Bank(PlayCase):
    def test_size_and_uniqueness(self):
        self.assertGreaterEqual(len(play_data.JOKES) + len(play_data.FACTS), 150)
        self.assertGreater(len(play_data.JOKES), play.RECENT)
        self.assertGreater(len(play_data.FACTS), play.RECENT)
        everything = play_data.JOKES + play_data.FACTS
        self.assertEqual(len(set(everything)), len(everything))
        self.assertEqual(len({play.item_id(t) for t in everything}), len(everything))

    def test_lines_are_short_and_clean(self):
        unsuitable = re.compile(r"\b(?:kill|dead|death|blood(?!\b(?: is|$))|drunk|beer|wine|sexy|stupid|idiot|hate"
                                r"|damn|hell|butt|poo(?!\b comes)|gun)\b", re.I)
        for text in play_data.JOKES + play_data.FACTS:
            with self.subTest(text=text):
                self.assertLessEqual(len(text.split()), 25)
                self.assertNotRegex(text, r"\[")
                if unsuitable.search(text):
                    self.assertIn(text, {"Octopuses have three hearts and blue blood.",
                                         "Starfish have no brain and no blood.",
                                         "Wombat poo comes out in cubes."})

    def test_no_repeats_over_the_last_hundred_across_restarts(self):
        told = []
        for n in range(230):
            kind = "joke" if n % 3 else "fact"
            if n == 120:
                play._LAST.clear()     # a restart: only play.json carries over
            told.append((kind, play.pick(kind, self.dir, rng=self.rng)))
        for kind in ("joke", "fact"):
            of_kind = [text for k, text in told if k == kind]
            for index, text in enumerate(of_kind):
                window = of_kind[max(0, index - play.RECENT):index]
                self.assertNotIn(text, window)
        # and across both kinds together
        texts = [text for _, text in told]
        for index, text in enumerate(texts):
            self.assertNotIn(text, texts[max(0, index - play.RECENT):index])
        stored = json.loads((self.dir / "play.json").read_text())
        self.assertEqual(len(stored["recent"]["joke"]), play.RECENT)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{8}", i) for i in stored["recent"]["joke"]))

    def test_guest_writes_nothing(self):
        for _ in range(5):
            self.assertIsNotNone(self.line("tell me a joke", guest=True, speaker="guest"))
        self.assertFalse((self.dir / "play.json").exists())

    def test_erase_all(self):
        self.line("tell me a joke")
        self.assertTrue((self.dir / "play.json").exists())
        play.erase_all(self.dir)
        self.assertFalse((self.dir / "play.json").exists())

    def test_damaged_file_is_ignored(self):
        (self.dir / "play.json").write_text("{not json")
        self.assertIsNotNone(self.line("tell me a fun fact"))


class Phrases(PlayCase):
    def test_jokes_and_facts(self):
        for said in ("tell me a joke", "Tell me a joke", "do you know any jokes", "make me laugh",
                     "tell me another joke", "say something funny"):
            with self.subTest(said=said):
                self.assertTrue(self.line(said).startswith("[joy] "))
        for said in ("tell me a fun fact", "Tell me a fun fact", "tell me something interesting", "another fact",
                     "give me a random fact"):
            with self.subTest(said=said):
                self.assertTrue(self.line(said).startswith("[curiosity] "))

    def test_another_follows_the_last_kind(self):
        self.line("tell me a fun fact")
        self.assertTrue(self.line("another one").startswith("[curiosity] "))
        play._LAST.clear()
        self.assertIsNone(self.say("another one"))

    def test_spelling(self):
        self.assertEqual(self.line("How do you spell necessary?"),
                         "[neutral] Necessary is spelled N, E, C, E, S, S, A, R, Y.")
        self.assertEqual(self.line("spell it's"), "[neutral] It's is spelled I, T, apostrophe, S.")
        self.assertEqual(self.line("how do you spell ice cream"),
                         "[neutral] That's two words: I, C, E; then C, R, E, A, M.")
        self.assertEqual(self.line("how is rhythm spelled"), "[neutral] Rhythm is spelled R, H, Y, T, H, M.")

    def test_menu(self):
        self.assertEqual(self.line("let's play a game"),
                         "[joy] We could play guess the number, rock paper scissors or twenty questions. Which one?")

    def test_not_ours(self):
        for said in ("what time is it", "stop", "yes", "rock", "50", "I give up", "what's 2 plus 2",
                     "add milk to my list", "how are you"):
            with self.subTest(said=said):
                self.assertIsNone(self.say(said))
        self.assertFalse(play.may_match("how are you"))
        self.assertTrue(play.may_match("tell me a joke"))
        self.assertFalse(play.may_match("50"))


class Games(PlayCase):
    def test_guess_the_number(self):
        self.assertEqual(self.line("guess the number"),
                         "[joy] I'm thinking of a number between one and a hundred. Have a guess.")
        secret = play._GAME["secret"]
        self.assertTrue(play.may_match("50"))
        low, high, tries = 1, 100, 0
        while True:
            guess = (low + high) // 2
            tries += 1
            line = self.line(f"is it {guess}")
            if guess == secret:
                self.assertTrue(line.startswith("[joy] Yes! It was "))
                break
            if "igher" in line:
                low = guess + 1
            else:
                self.assertIn("ower", line)
                high = guess - 1
        self.assertLessEqual(tries, 7)
        self.assertEqual(play._GAME, {})

    def test_number_words_and_range(self):
        self.say("guess the number")
        play._GAME["secret"] = 42
        self.assertEqual(self.line("forty two"), "[joy] Yes! It was forty-two. You got it in first go.")
        self.say("guess the number")
        play._GAME["secret"] = 42
        self.assertEqual(self.line("500"), "[neutral] It's between one and a hundred.")
        self.assertEqual(self.line("41"), "[neutral] Very close, but higher.")
        self.assertEqual(self.line("10"), "[neutral] Higher.")
        self.assertIsNone(self.say("what's the weather like"))      # passes on, game stays
        self.assertEqual(self.line("I give up"), "[neutral] Okay. My number was forty-two.")
        self.assertIsNone(self.say("50"))

    def test_rock_paper_scissors(self):
        self.assertEqual(self.line("rock paper scissors"), "[joy] Rock, paper, scissors! Say rock, paper or scissors.")
        with mock.patch.object(self.rng, "choice", return_value="scissors"):
            self.assertEqual(self.line("rock"), "[joy] I chose scissors. Rock blunts scissors, so you win. "
                                                "You one, me zero.")
        with mock.patch.object(self.rng, "choice", return_value="scissors"):
            self.assertEqual(self.line("I choose paper"), "[joy] I chose scissors. Scissors cut paper, so I win. "
                                                          "You one, me one.")
        with mock.patch.object(self.rng, "choice", return_value="paper"):
            self.assertEqual(self.line("paper"), "[joy] We both chose paper. A draw. You one, me one.")
        self.assertEqual(self.line("stop the game"), "[neutral] Okay, game over. It was a draw, one to one.")

    def test_twenty_questions_guesses_every_animal(self):
        for animal, traits in play_data.ANIMALS.items():
            with self.subTest(animal=animal):
                play._GAME.clear()
                line = self.line("twenty questions")
                self.assertIn("Think of an animal", line)
                for _ in range(play.MAX_QUESTIONS + 1):
                    guessed = play._GAME.get("guessing")
                    if guessed:
                        self.assertTrue(line.endswith(f"Is it {play._article(guessed)} {guessed}?"), line)
                        line = self.line("yes" if guessed == animal else "no")
                    else:
                        key = play._GAME["asked"][-1]
                        self.assertTrue(line.endswith(play_data.QUESTIONS[key]), line)
                        line = self.line("yes" if key in traits else "no")
                    if not play._GAME:
                        break
                self.assertIn(f"I got it! It was {play._article(animal)} {animal}", line)

    def test_twenty_questions_survives_a_wrong_answer_and_unsure(self):
        self.say("twenty questions")
        answers = 0
        line = ""
        while play._GAME and answers < 25:
            question = play.play_data.QUESTIONS.get(play._GAME["asked"][-1]) if play._GAME.get("asked") else None
            guessing = play._GAME.get("guessing")
            if guessing:
                line = self.line("yes" if guessing == "wombat" else "no")
            elif answers == 1:
                line = self.line("I don't know")
            elif answers == 2:
                key = play._GAME["asked"][-1]
                line = self.line("no" if key in play_data.ANIMALS["wombat"] else "yes")    # a mistake
            else:
                key = play._GAME["asked"][-1]
                line = self.line("yes" if key in play_data.ANIMALS["wombat"] else "no")
            self.assertIsNotNone(line, question)
            answers += 1
        self.assertFalse(play._GAME)
        self.assertTrue("wombat" in line or "You win" in line, line)

    def test_twenty_question_limit(self):
        self.say("twenty questions")
        line = ""
        for _ in range(play.MAX_QUESTIONS + 5):
            if not play._GAME:
                break
            line = self.line("no")
        self.assertFalse(play._GAME)
        self.assertTrue("You win" in line or "beaten me" in line, line)

    def test_stop_and_idle_and_speaker(self):
        self.say("twenty questions")
        self.assertEqual(self.line("stop the game"), "[neutral] Okay, we'll stop. You can tell me what it was, if you like.")
        self.say("guess the number")
        play._GAME["at"] -= play.GAME_IDLE + 1
        self.assertIsNone(self.say("50"))
        self.assertEqual(play._GAME, {})
        self.say("guess the number")
        self.assertIsNone(self.say("50", guest=True, speaker="guest"))     # a guest does not inherit the game
        self.assertEqual(play._GAME, {})

    def test_state_is_ram_only(self):
        self.say("guess the number")
        self.say("50")
        self.assertEqual(list(self.dir.iterdir()), [])


class Review(PlayCase):
    def test_how_is_needs_spelled(self):
        for said in ("how is your day", "how's it going", "how is my orchid", "how is it spelt", "spell it",
                     "how is the weather"):
            with self.subTest(said=said):
                self.assertIsNone(self.say(said))
        self.assertEqual(self.line("how is necessary spelled"),
                         "[neutral] Necessary is spelled N, E, C, E, S, S, A, R, Y.")

    def test_gate_stays_narrow(self):
        # The gate also decides whether a follow-up is meant for Pip.
        for said in ("that was pretty funny", "I want to play outside", "it's an interesting idea",
                     "more coffee", "one more thing", "rock music"):
            with self.subTest(said=said):
                self.assertFalse(play.may_match(said))
        for said in ("tell me a joke", "let's play a game", "rock paper scissors", "twenty questions",
                     "how do you spell necessary", "make me laugh", "I'm bored", "guess the number"):
            with self.subTest(said=said):
                self.assertTrue(play.may_match(said))
        self.line("tell me a joke")
        self.assertTrue(play.may_match("another one"))

    def test_only_moves_pass_the_gate_during_a_game(self):
        self.say("guess the number")
        self.assertTrue(play.may_match("fifty"))
        self.assertTrue(play.may_match("is it twenty three"))
        self.assertTrue(play.may_match("stop the game"))
        self.assertFalse(play.may_match("the dog wants a walk"))
        self.say("stop the game")
        self.say("rock paper scissors")
        self.assertTrue(play.may_match("rock"))
        self.assertFalse(play.may_match("no way"))
        self.say("stop the game")
        self.say("twenty questions")
        self.assertTrue(play.may_match("no"))
        self.assertTrue(play.may_match("I don't know"))
        self.assertFalse(play.may_match("turn the telly down"))

    def test_session_only_writes_nothing(self):
        from mind.retention import MemoryPolicy, save_policy
        save_policy(self.dir, MemoryPolicy("session-only"))
        told = [self.line("tell me a joke") for _ in range(5)]
        self.assertEqual(len(set(told)), 5)
        self.assertFalse((self.dir / "play.json").exists())

    def test_background_write_lands_and_never_repeats(self):
        async def tell(count):
            ctx = self.ctx()
            return [(await play.handle("tell me a joke", ctx, rng=self.rng)).line for _ in range(count)]
        told = asyncio.run(tell(play.RECENT))
        self.assertEqual(len(set(told)), play.RECENT)
        stored = json.loads((self.dir / "play.json").read_text())
        self.assertEqual(len(stored["recent"]["joke"]), play.RECENT)

    def test_an_erase_is_not_undone_by_a_late_write(self):
        recent = play.load_recent(self.dir)
        play.erase_all(self.dir)
        play.save_recent(self.dir, recent, generation=1)     # older than the erase
        self.assertFalse((self.dir / "play.json").exists())

if __name__ == "__main__":
    unittest.main()
