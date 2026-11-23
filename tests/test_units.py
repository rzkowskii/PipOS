import asyncio
import unittest
from fractions import Fraction

from mind import units

CUP = "That's using a metric cup of 250 millilitres."

CASES = [
    # Every phrase in the function spec, section 2.5.
    ("How many grams in a pound?", "There are about four hundred and fifty-four grams in a pound."),
    ("180 Fahrenheit in Celsius", "One hundred and eighty degrees Fahrenheit is about eighty-two point two degrees Celsius."),
    ("Convert 5 miles to kilometres", "Five miles is about eight point zero five kilometres."),
    ("How many cups in a litre?", f"There are four cups in a litre. {CUP}"),
    # Length.
    ("how many centimetres in an inch", "There are two point five four centimetres in an inch."),
    ("how many feet in a mile", "There are five thousand, two hundred and eighty feet in a mile."),
    ("how many metres in a kilometre", "There are one thousand metres in a kilometre."),
    ("how many inches in a foot", "There are twelve inches in a foot."),
    ("convert 100 metres to yards", "One hundred metres is about one hundred and nine yards."),
    ("what's 10 km in miles", "Ten kilometres is about six point two one miles."),
    ("5 foot 10 in centimetres", "Five foot ten is about one hundred and seventy-eight centimetres."),
    ("how many millimetres in a metre", "There are one thousand millimetres in a metre."),
    ("one nautical mile in metres", "One nautical mile is one thousand, eight hundred and fifty-two metres."),
    ("how many kilometers is 26.2 miles", "There are about forty-two point two kilometres in twenty-six point two miles."),
    # Mass.
    ("what is 10 kilos in pounds", "Ten kilograms is about twenty-two pounds."),
    ("how many grams in a kilo", "There are one thousand grams in a kilogram."),
    ("how many ounces in a pound", "There are sixteen ounces in a pound."),
    ("6 stone in kilos", "Six stone is about thirty-eight point one kilograms."),
    ("how many pounds in a stone", "There are fourteen pounds in a stone."),
    ("convert 2.5 kg to grams", "Two point five kilograms is two thousand, five hundred grams."),
    ("how many kilograms in a tonne", "There are one thousand kilograms in a tonne."),
    ("100 grams in ounces", "One hundred grams is about three point five three ounces."),
    # Volume and cooking.
    ("how many millilitres in a litre", "There are one thousand millilitres in a litre."),
    ("one cup in millilitres", f"One cup is two hundred and fifty millilitres. {CUP}"),
    ("how many tablespoons in a cup",
     "There are twelve point five tablespoons in a cup. That's using a metric cup of 250 millilitres and an "
     "Australian tablespoon of 20 millilitres."),
    ("how many teaspoons in a tablespoon",
     "There are four teaspoons in a tablespoon. That's using an Australian tablespoon of 20 millilitres."),
    ("convert 2 litres to pints", "Two litres is about three point five two pints. That's using a UK pint."),
    ("how many litres in a gallon", "There are about four point five five litres in a gallon. That's using a UK gallon."),
    ("how many litres in a US gallon", "There are about three point seven nine litres in a US gallon."),
    ("how many ounces in a cup",
     f"There are about eight point eight fluid ounces in a cup. That's using a metric cup of 250 millilitres and a "
     f"UK fluid ounce."),
    ("how many grams in a cup",
     "That depends on what you're measuring. I only know water: a metric cup of water weighs 250 grams."),
    ("how many grams of flour in a cup",
     "That depends on what you're measuring. I only know water: a metric cup of water weighs 250 grams."),
    ("how many grams in a litre of water", "There are one thousand grams in a litre. That's for water."),
    ("3 teaspoons in millilitres", "Three teaspoons is fifteen millilitres."),
    # Temperature.
    ("minus 40 celsius in fahrenheit", "Minus forty degrees Celsius is minus forty degrees Fahrenheit."),
    ("what's 30 degrees celsius in fahrenheit", "Thirty degrees Celsius is eighty-six degrees Fahrenheit."),
    ("20 c in f", "Twenty degrees Celsius is sixty-eight degrees Fahrenheit."),
    ("what is 0 kelvin in celsius", "Zero kelvin is about minus two hundred and seventy-three degrees Celsius."),
    ("100 celsius in kelvin", "One hundred degrees Celsius is about three hundred and seventy-three kelvin."),
    ("convert 98.6 fahrenheit to celsius", "Ninety-eight point six degrees Fahrenheit is thirty-seven degrees Celsius."),
    ("minus 300 celsius in kelvin", "Nothing can be colder than absolute zero, so that temperature can't happen."),
    # Speed.
    ("100 km/h in mph", "One hundred kilometres per hour is about sixty-two point one miles per hour."),
    ("60 miles per hour in kilometres per hour", "Sixty miles per hour is about ninety-six point six kilometres per hour."),
    ("10 knots in kilometres per hour", "Ten knots is about eighteen point five kilometres per hour."),
    ("convert 10 metres per second to km/h", "Ten metres per second is thirty-six kilometres per hour."),
    # Data.
    ("how many megabytes in a gigabyte",
     "There are one thousand megabytes in a gigabyte. I'm counting in thousands; computers sometimes count in "
     "steps of 1,024."),
    ("how many bits in a byte", "There are eight bits in a byte."),
    ("how many mebibytes in a gibibyte", "There are one thousand and twenty-four mebibytes in a gibibyte."),
    ("100 megabits in megabytes",
     "One hundred megabits is twelve point five megabytes. I'm counting in thousands; computers sometimes count in "
     "steps of 1,024."),
    # Time.
    ("how many seconds are there in a day", "There are eighty-six thousand, four hundred seconds in a day."),
    ("how many minutes in 3 hours", "There are one hundred and eighty minutes in three hours."),
    ("how many days in a year", "There are three hundred and sixty-five days in a year."),
    ("how many hours in a week", "There are one hundred and sixty-eight hours in a week."),
    ("how many weeks in a year", "There are about fifty-two point one weeks in a year. That's using a 365-day year."),
    ("how many days in a fortnight", "There are fourteen days in a fortnight."),
    # Mismatches and no-ops.
    ("kilograms to kilometres", "I can't turn kilograms into kilometres; they measure different things."),
    ("convert 5 litres to litres", "That's already in litres."),
]

NOT_CONVERSIONS = [
    "what time is it in london", "add milk to my shopping list", "what's 17 times 23", "tell me a joke",
    "how many lists do you have", "how many days until christmas", "how much battery do you have",
    "set a timer for 10 minutes", "c to f", "what is a metre", "how many reminders do i have",
]


class Conversions(unittest.TestCase):
    def test_cases(self):
        self.assertGreaterEqual(len(CASES), 50)
        for said, expected in CASES:
            with self.subTest(said=said):
                line = units.answer(said)
                self.assertEqual(line, f"[neutral] {expected}")
                self.assertLessEqual(len(line.split()) - 1, 30)

    def test_not_conversions(self):
        for said in NOT_CONVERSIONS:
            with self.subTest(said=said):
                self.assertIsNone(units.answer(said))

    def test_exact_arithmetic(self):
        self.assertEqual(units.convert(Fraction(1), "pound", "g"), Fraction(45359237, 100000))
        self.assertEqual(units.convert(Fraction(212), "fahrenheit", "celsius"), 100)
        self.assertEqual(units.convert(Fraction(1), "cup", "ml"), 250)
        self.assertEqual(units.unit("kilos"), "kg")
        self.assertEqual(units.unit("degrees celsius"), "celsius")
        self.assertIsNone(units.unit("parsecs"))

    def test_quantity(self):
        self.assertEqual(units.quantity("minus 40"), -40)
        self.assertEqual(units.quantity("a"), 1)
        self.assertEqual(units.quantity("two and a half"), Fraction(5, 2))
        self.assertIsNone(units.quantity("lots of"))

    def test_handler(self):
        self.assertTrue(units.may_match("how many grams in a pound"))
        self.assertFalse(units.may_match("how are you"))
        reply = asyncio.run(units.handle("how many bits in a byte", None))
        self.assertEqual(reply.line, "[neutral] There are eight bits in a byte.")
        self.assertIsNone(asyncio.run(units.handle("how are you", None)))


class Gate(unittest.TestCase):
    def test_gate_stays_narrow(self):
        # The gate also decides whether a follow-up is meant for Pip.
        for said in ("I'm going to bed", "I'll be back in an hour", "the next episode is in a minute",
                     "I'm off to work", "it's ten to five", "the meeting is from two to four", "quarter to ten",
                     "I walked five miles to the shop"):
            with self.subTest(said=said):
                self.assertFalse(units.may_match(said))

    def test_every_case_passes_the_gate(self):
        for said, _ in CASES:
            with self.subTest(said=said):
                self.assertTrue(units.may_match(said))
        for said in ("what's 98.6 f in c", "convert 10 c to f", "five foot ten in cm", "what's 100 km/h in mph"):
            with self.subTest(said=said):
                self.assertTrue(units.may_match(said))
                self.assertIsNotNone(units.answer(said))

if __name__ == "__main__":
    unittest.main()
