import asyncio
import unittest
from fractions import Fraction

from mind import calc

# (said, expected line without the tag)
CASES = [
    # Every phrase in the function spec, section 2.5.
    ("What's 17 times 23?", "Seventeen times twenty-three is three hundred and ninety-one."),
    ("What's 15 percent of 80?", "Fifteen percent of eighty is twelve."),
    ("What's 120 divided by 7?", "One hundred and twenty divided by seven is about seventeen point one."),
    ("Square root of 144", "The square root of one hundred and forty-four is twelve."),
    ("What's 2 to the power of 10?", "Two to the power of ten is one thousand and twenty-four."),
    # Spoken numbers.
    ("what is seventeen times twenty three", "Seventeen times twenty-three is three hundred and ninety-one."),
    ("what's twenty-five plus seventeen", "Twenty-five plus seventeen is forty-two."),
    ("what is a hundred and twenty plus three", "One hundred and twenty plus three is one hundred and twenty-three."),
    ("what is two thousand and five minus five", "Two thousand and five minus five is two thousand."),
    ("one million divided by four", "One million divided by four is two hundred and fifty thousand."),
    ("what is three point five times two", "Three point five times two is seven."),
    ("what's one and a half times four", "One point five times four is six."),
    ("a half plus a quarter", "Zero point five plus zero point two five is zero point seven five."),
    ("what is 1.5 million plus 1", "One million, five hundred thousand plus one is one million, five hundred thousand and one."),
    ("what is nine hundred and ninety nine plus one", "Nine hundred and ninety-nine plus one is one thousand."),
    ("what's 1,000 times 1,000", "One thousand times one thousand is one million."),
    # Every operator.
    ("seven plus eight", "Seven plus eight is fifteen."),
    ("what's 10 minus 3", "Ten minus three is seven."),
    ("what is ten take away three", "Ten minus three is seven."),
    ("what's 6 times 7", "Six times seven is forty-two."),
    ("what is 6 multiplied by 7", "Six times seven is forty-two."),
    ("what is 6 x 7", "Six times seven is forty-two."),
    ("17*23", "Seventeen times twenty-three is three hundred and ninety-one."),
    ("what's 84 divided by 2", "Eighty-four divided by two is forty-two."),
    ("twenty-five over five", "Twenty-five divided by five is five."),
    ("10/4", "Ten divided by four is two point five."),
    ("what's 12% of 50", "Twelve percent of fifty is six."),
    ("what is fifty per cent of 30", "Fifty percent of thirty is fifteen."),
    ("what is 10 squared", "Ten squared is one hundred."),
    ("what's 3 cubed", "Three cubed is twenty-seven."),
    ("what is the square root of 81", "The square root of eighty-one is nine."),
    ("what's the cube root of 27", "The cube root of twenty-seven is three."),
    ("2 to the 8th", "Two to the power of eight is two hundred and fifty-six."),
    ("what is 2 to the tenth", "Two to the power of ten is one thousand and twenty-four."),
    ("3 ^ 4", "Three to the power of four is eighty-one."),
    ("add 5 and 7", "Five plus seven is twelve."),
    ("add five to seven", "Five plus seven is twelve."),
    ("subtract 3 from 10", "Ten minus three is seven."),
    ("multiply 6 by 7", "Six times seven is forty-two."),
    ("divide 100 by 4", "One hundred divided by four is twenty-five."),
    ("what's half of 50", "Fifty divided by two is twenty-five."),
    ("double 21", "Two times twenty-one is forty-two."),
    ("the sum of 2 and 3", "Two plus three is five."),
    # Precedence and signs.
    ("what's 2 plus 3 times 4", "Two plus three times four is fourteen."),
    ("10 minus 2 minus 3", "Ten minus two minus three is five."),
    ("what's minus 5 plus 3", "Minus five plus three is minus two."),
    ("what is 3 times 4 squared", "Three times four squared is forty-eight."),
    ("10 plus 15 percent of 80", "Ten plus fifteen percent of eighty is twenty-two."),
    # Exact decimals and rounding.
    ("what is 0.1 plus 0.2", "Zero point one plus zero point two is zero point three."),
    ("what is 1 divided by 3", "One divided by three is about zero point three three three."),
    ("what is 10000 divided by 3", "Ten thousand divided by three is about three thousand, three hundred and thirty-three."),
    ("what is the square root of 2", "The square root of two is about one point four one."),
    ("what's 1 divided by 8", "One divided by eight is zero point one two five."),
    ("what is 2 to the power of 0.5", "Two to the power of zero point five is about one point four one."),
    ("square root of 2.25", "The square root of two point two five is one point five."),
    ("what's 100 divided by 7", "One hundred divided by seven is about fourteen point three."),
    # Honest limits.
    ("what's ten divided by zero", "Dividing by zero has no answer, so I can't give you one."),
    ("what is 5 over 0", "Dividing by zero has no answer, so I can't give you one."),
    ("square root of minus 4", "A negative number has no ordinary square root, so I can't give you one."),
    ("what's 2 to the power of 101", "I only work out powers up to a hundred."),
    ("2 to the power of 60", "That answer is bigger than a million billion, which is more than I'll work out."),
    ("what's a million times a billion", "One million times one billion is one thousand trillion."),
    ("what's a billion times a billion", "That answer is bigger than a million billion, which is more than I'll work out."),
    ("what is minus 8 to the power of 0.5", None),   # parsed as -(8 ** 0.5): honest about rounding
    ("what's 0 to the power of minus 1", "Dividing by zero has no answer, so I can't give you one."),
    ("cube root of minus 27", "The cube root of minus twenty-seven is minus three."),
    ("what's 999999 times 999999999",
     "That's nine hundred and ninety-nine trillion, nine hundred and ninety-eight billion, "
     "nine hundred and ninety-nine million and one."),
]

NOT_SUMS = [
    "what time is it", "game over", "what's 5", "tell me a joke", "add milk to my shopping list",
    "how many grams in a pound", "five six seven", "the plumber comes over thursday", "times are hard",
    "what's the square root of love", "minus", "3 plus", "plus 3 plus", "two to the",
    "convert 5 miles to kilometres", "over the moon", "set a timer for 10 minutes",
]


class Sums(unittest.TestCase):
    def test_cases(self):
        self.assertGreaterEqual(len(CASES), 60)
        for said, expected in CASES:
            with self.subTest(said=said):
                line = calc.answer(said)
                self.assertIsNotNone(line)
                self.assertTrue(line.startswith("[neutral] "), line)
                if expected is not None:
                    self.assertEqual(line[len("[neutral] "):], expected)
                self.assertLessEqual(len(line.split()) - 1, 25)

    def test_negative_power_root_is_rounded_honestly(self):
        self.assertEqual(calc.answer("what is minus 8 to the power of 0.5"),
                         "[neutral] Minus eight to the power of zero point five is about minus two point eight three.")

    def test_not_sums(self):
        for said in NOT_SUMS:
            with self.subTest(said=said):
                self.assertIsNone(calc.answer(said))

    def test_no_eval_and_whitelist(self):
        for source in ("__import__('os')", "n0.real", "[n0]", "n0 if n0 else n0", "n0 % 2", "open('x')", "n1"):
            with self.subTest(source=source), self.assertRaises((ValueError, SyntaxError)):
                calc.evaluate(source, {"n0": Fraction(1)})
        self.assertEqual(calc.evaluate("n0+n0*2", {"n0": Fraction(3)}), 9)

    def test_exact_fractions(self):
        value, _ = calc.calculate("0.1 plus 0.2")
        self.assertEqual(value, Fraction(3, 10))


class Numbers(unittest.TestCase):
    def test_parse(self):
        cases = {
            "seventeen": 17, "twenty-three": 23, "twenty three": 23, "a hundred": 100,
            "one hundred and five": 105, "two thousand and five": 2005, "3 hundred": 300,
            "1.5 million": 1500000, "three point one four": Fraction(314, 100), "a half": Fraction(1, 2),
            "two and a half": Fraction(5, 2), "point five": Fraction(1, 2), "zero": 0,
            "nine hundred and ninety nine thousand": 999000,
        }
        for said, expected in cases.items():
            with self.subTest(said=said):
                words = said.split()
                self.assertEqual(calc.parse_number(words), (Fraction(expected), len(words)))

    def test_parse_stops(self):
        self.assertEqual(calc.parse_number("five six".split()), (5, 1))
        self.assertIsNone(calc.parse_number(["milk"]))
        self.assertEqual(calc.parse_number("ten and seven".split()), (10, 1))

    def test_say(self):
        self.assertEqual(calc.say_number(1005), "one thousand and five")
        self.assertEqual(calc.say_number(1234567), "one million, two hundred and thirty-four thousand, "
                                                   "five hundred and sixty-seven")
        self.assertEqual(calc.say_number(Fraction(-21, 4)), "minus five point two five")
        self.assertEqual(calc.say_value(Fraction(1, 3)), ("zero point three three three", True))
        self.assertEqual(calc.say_value(12.0), ("twelve", False))
        self.assertEqual(calc.rounded(Fraction(33333, 10)), (Fraction(3333), True))


class Handler(unittest.TestCase):
    def test_handle_and_gate(self):
        self.assertTrue(calc.may_match("what's 17 times 23"))
        self.assertTrue(calc.may_match("square root of a hundred and forty four"))
        self.assertFalse(calc.may_match("how are you"))
        self.assertFalse(calc.may_match("it's all over"))
        reply = asyncio.run(calc.handle("what's 6 times 7", None))
        self.assertEqual(reply.line, "[neutral] Six times seven is forty-two.")
        self.assertFalse(reply.keep)
        self.assertIsNone(asyncio.run(calc.handle("how are you", None)))


class Review(unittest.TestCase):
    def test_tiny_answers_are_about_zero(self):
        self.assertEqual(calc.answer("what's 0.0000001 to the power of 100"),
                         "[neutral] Zero point zero zero zero zero zero zero one to the power of one hundred is about zero.")
        self.assertEqual(calc.answer("what's 10 to the power of minus 20"),
                         "[neutral] Ten to the power of minus twenty is about zero.")
        self.assertEqual(calc.answer("what's 1 divided by 1000000"),
                         "[neutral] One divided by one million is zero point zero zero zero zero zero one.")

    def test_typed_signs(self):
        self.assertEqual(calc.answer("what's 6 × 7"), "[neutral] Six times seven is forty-two.")
        self.assertEqual(calc.answer("what's 12 ÷ 4"), "[neutral] Twelve divided by four is three.")

    def test_long_input_is_not_a_sum(self):
        self.assertIsNone(calc.answer("what's " + " plus ".join(["1"] * 2000)))
        self.assertIsNone(calc.answer("what's " + " plus ".join(["1"] * 60)))

    def test_gate_stays_narrow(self):
        # The gate also decides whether a follow-up is meant for Pip.
        for said in ("the party is at six", "I have six kids", "it was a twenty-three minute drive",
                     "that was pretty funny", "what's next", "the box is empty"):
            with self.subTest(said=said):
                self.assertFalse(calc.may_match(said))
        for said in ("what's 6 x 7", "what's 4-2", "what is -3 plus 1", "what's 6 × 7", "twenty times three"):
            with self.subTest(said=said):
                self.assertTrue(calc.may_match(said))

if __name__ == "__main__":
    unittest.main()
