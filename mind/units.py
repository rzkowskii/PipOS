"""Unit conversions, answered in code: "how many grams in a pound?".

Length, mass, volume, temperature, speed, data, time and cooking measures.
Where a unit has more than one meaning Pip says which one it used: a cup is
the metric cup of 250 millilitres, a tablespoon is the Australian one of 20
millilitres, pints, gallons and fluid ounces are UK measures unless "US" is
said, a year is 365 days, and data sizes count in thousands. Weight and
volume only convert for water; for anything else Pip says it depends.
"""

import re
from fractions import Fraction

from .calc import parse_number, say_value
from .reply import Reply, clean

F = Fraction
_UNITS = {}     # key -> (dimension, factor to the base unit, singular, plural)
_ALIASES = {}   # spoken form -> key
_NOTES = {
    "cup": "a metric cup of 250 millilitres",
    "tablespoon": "an Australian tablespoon of 20 millilitres",
    "pint": "a UK pint",
    "gallon": "a UK gallon",
    "fluid ounce": "a UK fluid ounce",
    "year": "a 365-day year",
}
_DATA_NOTE = "I'm counting in thousands; computers sometimes count in steps of 1,024."


def _unit(key, dimension, factor, singular, plural, *aliases):
    _UNITS[key] = (dimension, F(factor), singular, plural)
    for alias in (singular, plural, *aliases):
        _ALIASES[alias] = key


# Length, in metres.
_unit("mm", "length", F(1, 1000), "millimetre", "millimetres", "millimeter", "millimeters", "mm", "mil", "mils")
_unit("cm", "length", F(1, 100), "centimetre", "centimetres", "centimeter", "centimeters", "cm", "cms", "centi")
_unit("m", "length", 1, "metre", "metres", "meter", "meters", "m")
_unit("km", "length", 1000, "kilometre", "kilometres", "kilometer", "kilometers", "km", "kms", "k")
_unit("inch", "length", F(254, 10000), "inch", "inches")
_unit("foot", "length", F(3048, 10000), "foot", "feet", "ft")
_unit("yard", "length", F(9144, 10000), "yard", "yards", "yd", "yds")
_unit("mile", "length", F(1609344, 1000), "mile", "miles", "mi")
_unit("nautical mile", "length", 1852, "nautical mile", "nautical miles")
# Mass, in grams.
_unit("mg", "mass", F(1, 1000), "milligram", "milligrams", "milligramme", "milligrammes", "mg")
_unit("g", "mass", 1, "gram", "grams", "gramme", "grammes", "g", "gm", "gms")
_unit("kg", "mass", 1000, "kilogram", "kilograms", "kilogramme", "kilogrammes", "kilo", "kilos", "kg", "kgs")
_unit("tonne", "mass", 1000000, "tonne", "tonnes", "metric ton", "metric tons")
_unit("ounce", "mass", F(28349523125, 10 ** 9), "ounce", "ounces", "oz")
_unit("pound", "mass", F(45359237, 10 ** 5), "pound", "pounds", "lb", "lbs")
_unit("stone", "mass", F(635029318, 10 ** 5), "stone", "stone", "stones", "st")
# Volume, in millilitres.
_unit("ml", "volume", 1, "millilitre", "millilitres", "milliliter", "milliliters", "ml", "mls", "mil of water")
_unit("cl", "volume", 10, "centilitre", "centilitres", "centiliter", "centiliters", "cl")
_unit("l", "volume", 1000, "litre", "litres", "liter", "liters", "l")
_unit("cubic metre", "volume", 1000000, "cubic metre", "cubic metres", "cubic meter", "cubic meters")
_unit("teaspoon", "volume", 5, "teaspoon", "teaspoons", "tsp", "teaspoonful", "teaspoonfuls")
_unit("tablespoon", "volume", 20, "tablespoon", "tablespoons", "tbsp", "tablespoonful", "tablespoonfuls")
_unit("cup", "volume", 250, "cup", "cups", "metric cup", "metric cups", "cupful", "cupfuls")
_unit("us cup", "volume", F(2365882365, 10 ** 7), "US cup", "US cups", "american cup", "american cups")
_unit("fluid ounce", "volume", F(284130625, 10 ** 7), "fluid ounce", "fluid ounces", "fl oz", "uk fluid ounce",
      "uk fluid ounces")
_unit("us fluid ounce", "volume", F(295735295625, 10 ** 10), "US fluid ounce", "US fluid ounces",
      "american fluid ounce", "american fluid ounces")
_unit("pint", "volume", F(56826125, 10 ** 5), "pint", "pints", "uk pint", "uk pints", "imperial pint", "imperial pints")
_unit("us pint", "volume", F(473176473, 10 ** 6), "US pint", "US pints", "american pint", "american pints")
_unit("gallon", "volume", F(454609, 100), "gallon", "gallons", "uk gallon", "uk gallons", "imperial gallon",
      "imperial gallons")
_unit("us gallon", "volume", F(3785411784, 10 ** 6), "US gallon", "US gallons", "american gallon", "american gallons")
# Speed, in metres per second.
_unit("m/s", "speed", 1, "metre per second", "metres per second", "meter per second", "meters per second",
      "m/s", "metres a second", "meters a second")
_unit("km/h", "speed", F(1000, 3600), "kilometre per hour", "kilometres per hour", "kilometer per hour",
      "kilometers per hour", "kilometres an hour", "kilometers an hour", "km/h", "kph", "kmh", "k an hour",
      "ks an hour", "km an hour", "kilometre an hour", "kilometer an hour")
_unit("mph", "speed", F(1609344, 3600000), "mile per hour", "miles per hour", "miles an hour", "mile an hour", "mph")
_unit("knot", "speed", F(1852, 3600), "knot", "knots", "kn")
_unit("ft/s", "speed", F(3048, 10000), "foot per second", "feet per second", "ft/s", "feet a second")
# Data, in bytes (decimal prefixes, said so).
_unit("bit", "data", F(1, 8), "bit", "bits")
_unit("byte", "data", 1, "byte", "bytes")
_unit("kb", "data", 10 ** 3, "kilobyte", "kilobytes", "kb", "kbs")
_unit("mb", "data", 10 ** 6, "megabyte", "megabytes", "mb", "mbs", "meg", "megs")
_unit("gb", "data", 10 ** 9, "gigabyte", "gigabytes", "gb", "gbs", "gig", "gigs")
_unit("tb", "data", 10 ** 12, "terabyte", "terabytes", "tb", "tbs")
_unit("pb", "data", 10 ** 15, "petabyte", "petabytes", "pb")
_unit("kibibyte", "data", 2 ** 10, "kibibyte", "kibibytes", "kib")
_unit("mebibyte", "data", 2 ** 20, "mebibyte", "mebibytes", "mib")
_unit("gibibyte", "data", 2 ** 30, "gibibyte", "gibibytes", "gib")
_unit("kilobit", "data", 125, "kilobit", "kilobits", "kbit")
_unit("megabit", "data", 125000, "megabit", "megabits", "mbit", "mbps", "megabits per second")
_unit("gigabit", "data", 125000000, "gigabit", "gigabits", "gbit")
# Time, in seconds.
_unit("ms", "time", F(1, 1000), "millisecond", "milliseconds", "ms")
_unit("second", "time", 1, "second", "seconds", "sec", "secs")
_unit("minute", "time", 60, "minute", "minutes", "min", "mins")
_unit("hour", "time", 3600, "hour", "hours", "hr", "hrs")
_unit("day", "time", 86400, "day", "days")
_unit("week", "time", 604800, "week", "weeks")
_unit("fortnight", "time", 1209600, "fortnight", "fortnights")
_unit("year", "time", 31536000, "year", "years")
_unit("decade", "time", 315360000, "decade", "decades")
# Temperature: converted by formula, not by factor.
_unit("celsius", "temperature", 1, "degree Celsius", "degrees Celsius", "celsius", "centigrade", "degrees c",
      "degree c", "c", "°c")
_unit("fahrenheit", "temperature", 1, "degree Fahrenheit", "degrees Fahrenheit", "fahrenheit", "degrees f",
      "degree f", "f", "°f")
_unit("kelvin", "temperature", 1, "kelvin", "kelvin", "kelvins", "degrees kelvin")

_FOLDED = {alias.casefold(): key for alias, key in _ALIASES.items()}
# Single letters are units only right after a number or in a conversion slot.
_SHORT = {alias for alias in _ALIASES if len(alias) <= 2}
_ALIAS = "|".join(sorted((re.escape(a.casefold()) for a in _ALIASES), key=len, reverse=True))
_U = rf"(?:degrees? )?(?:{_ALIAS})"
_N = r"(?:minus |negative )?[\w.\- ]+?"
_SUBSTANCE = r"(?: of (?P<stuff>[a-z ]+?))?"
_SUBSTANCE2 = r"(?: of (?P<stuff2>[a-z ]+?))?"
_PATTERNS = [
    # "how many grams in a pound", "how many cups are there in 2 litres", "how many grams of flour in a cup"
    ("in", re.compile(rf"^how (?:many|much) (?P<to>{_U}){_SUBSTANCE} (?:are |is )?(?:there )?in (?:a |an |one )?"
                      rf"(?:(?P<qty>{_N}) )??(?P<from>{_U}){_SUBSTANCE2}$")),
    # "how many kilometres is 5 miles"
    ("in", re.compile(rf"^how (?:many|much) (?P<to>{_U}) (?:is|are|make|makes|in) (?:a |an |one )?"
                      rf"(?:(?P<qty>{_N}) )??(?P<from>{_U}){_SUBSTANCE}$")),
    # "5 foot 10 in centimetres"
    ("height", re.compile(rf"^(?:convert |what(?:'s| is) |how much is |how tall is )?(?P<feet>{_N}) (?:foot|feet|ft)"
                          rf"(?: and)? (?P<inches>{_N})(?: inch(?:es)?)? (?:in|to|into|in to) (?P<to>{_U})$")),
    # "convert 5 miles to kilometres", "what's 180 fahrenheit in celsius", "10 kilos in pounds"
    ("to", re.compile(rf"^(?:convert |change |turn |what(?:'s| is| are) |how much is |how much are |how many is "
                      rf"|how (?:hot|cold|warm|far|fast|heavy|long|big) is )?(?:a |an )?(?:(?P<qty>{_N}) )??"
                      rf"(?P<from>{_U}){_SUBSTANCE} (?:in|to|into|in to|as) (?P<to>{_U})$")),
]
# The gate is also how the mind tells a follow-up meant for it from the room,
# so it stays narrow. A conversion is asked as "how many/much ... unit",
# "convert ...", or "<unit> in/to <unit>". One- and two-letter units ("m" in
# "I'm", "st", "c") count only right after a number.
_LONG_ALIAS = "|".join(sorted((re.escape(a.casefold()) for a in _ALIASES if len(a) > 2), key=len, reverse=True))
_SHORT_ALIAS = "|".join(sorted((re.escape(a.casefold()) for a in _SHORT), key=len, reverse=True))
_NUMBER_WORD = (r"\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty"
                r"|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|half|quarter)")
_GATE_UNIT = rf"(?:\b(?:{_LONG_ALIAS})\b|(?:{_NUMBER_WORD})\s*(?:{_SHORT_ALIAS})\b|°[cf]\b)"
_NEEDS = re.compile(rf"\b(?:how many|how much|convert)\b.*{_GATE_UNIT}", re.I)
_UNIT_TO_UNIT = re.compile(rf"(?:{_GATE_UNIT}|(?:{_NUMBER_WORD})\w*)\s+(?:in|to|into|as|in to)\s+(?:degrees? )?"
                           rf"(?:{_ALIAS})\b", re.I)


def unit(spoken):
    """The unit key for a spoken name ('kilos' -> 'kg'), or None."""
    said = re.sub(r"^degrees? (?=celsius|centigrade|fahrenheit|kelvin)", "", spoken.casefold().strip())
    return _FOLDED.get(said)


def quantity(spoken):
    """'a', 'five', '2.5', 'minus 40', 'a hundred and twenty' -> Fraction, else None."""
    if spoken is None:
        return F(1)
    words = spoken.casefold().split()
    negative = bool(words) and words[0] in ("minus", "negative")
    words = words[1:] if negative else words
    if words and words[0] in ("a", "an") and len(words) == 1:
        return F(1)
    parsed = parse_number(words)
    if parsed is None or parsed[1] != len(words):
        return None
    return -parsed[0] if negative else parsed[0]


def _celsius(value, key):
    if key == "celsius":
        return value
    if key == "fahrenheit":
        return (value - 32) * F(5, 9)
    return value - F(27315, 100)


def _from_celsius(value, key):
    if key == "celsius":
        return value
    if key == "fahrenheit":
        return value * F(9, 5) + 32
    return value + F(27315, 100)


def convert(value, source, target):
    """value in `source` units -> Fraction in `target` units (same dimension)."""
    dimension = _UNITS[source][0]
    if dimension != _UNITS[target][0]:
        raise ValueError("different dimensions")
    if dimension == "temperature":
        return _from_celsius(_celsius(value, source), target)
    return value * _UNITS[source][1] / _UNITS[target][1]


def _name(key, value, approximate=False):
    singular, plural = _UNITS[key][2], _UNITS[key][3]
    return singular if value == 1 and not approximate else plural


def _article(word):
    return "an" if word[0] in "aeiou" or word.startswith("hour") else "a"


def _note(source, target):
    notes = [_NOTES[k] for k in dict.fromkeys((source, target)) if k in _NOTES
             and not (k == "year" and {source, target} == {"year", "day"})]
    line = f"That's using {' and '.join(notes)}." if notes else ""
    if _UNITS[source][0] == "data" and {source, target} & {"kb", "mb", "gb", "tb", "pb"} and not (
            {source, target} & {"kibibyte", "mebibyte", "gibibyte"}):
        line = _DATA_NOTE
    return line


def _mixed(source, target, stuff):
    """A weight asked for as a volume, or the other way round."""
    if stuff and stuff.strip() in ("water", "tap water"):
        return None
    if source == "ounce" and _UNITS[target][0] == "volume":
        return "fluid ounce", target
    if target == "ounce" and _UNITS[source][0] == "volume":
        return source, "fluid ounce"
    return ("[neutral] That depends on what you're measuring. I only know water: "
            "a metric cup of water weighs 250 grams.")


def answer(text, name="pip"):
    said = clean(text, name).casefold().rstrip("?").strip()
    said = re.sub(r"\bper cent\b", "percent", said)
    for kind, pattern in _PATTERNS:
        match = pattern.match(said)
        if not match:
            continue
        groups = match.groupdict()
        source, target = unit(groups.get("from") or "foot"), unit(groups["to"])
        if source is None or target is None:
            continue
        if kind == "height":
            feet, inches = quantity(groups["feet"]), quantity(groups["inches"])
            if feet is None or inches is None:
                continue
            value, source = feet * 12 + inches, "inch"
        else:
            value = quantity(groups.get("qty"))
            if value is None:
                continue
        if groups.get("qty") is None and kind == "to" and groups["from"] in _SHORT:
            continue    # "c to f" with no number is too thin to be sure of
        stuff = groups.get("stuff") or groups.get("stuff2")
        if _UNITS[source][0] != _UNITS[target][0]:
            dimensions = {_UNITS[source][0], _UNITS[target][0]}
            if dimensions == {"mass", "volume"}:
                swapped = _mixed(source, target, stuff)
                if isinstance(swapped, str):
                    return swapped
                if swapped is None:     # water: 1 gram per millilitre
                    grams = value * _UNITS[source][1]
                    result = grams / _UNITS[target][1]
                    return _line(kind, value, source, target, result, groups, note="That's for water.")
                source, target = swapped
            else:
                return (f"[neutral] I can't turn {_UNITS[source][3]} into {_UNITS[target][3]}; "
                        f"they measure different things.")
        if source == target:
            return f"[neutral] That's already in {_UNITS[source][3]}."
        if _UNITS[source][0] == "temperature" and _celsius(value, source) < F(-27315, 100):
            return "[neutral] Nothing can be colder than absolute zero, so that temperature can't happen."
        if abs(value) > 10 ** 15:
            return "[neutral] That number is too big for me to convert."
        return _line(kind, value, source, target, convert(value, source, target), groups)
    return None


def _line(kind, value, source, target, result, groups, note=""):
    amount, approximate = say_value(value)
    words, about = say_value(result)
    about_word = "about " if about else ""
    source_name = _name(source, value)
    target_name = _name(target, result, about)
    if kind == "in":
        if value == 1:
            lead = f"There {'is' if result == 1 and not about else 'are'} {about_word}{words} {target_name} in " \
                   f"{_article(source_name)} {source_name}."
        else:
            lead = f"There are {about_word}{words} {target_name} in {amount} {source_name}."
    elif kind == "height" and groups:
        lead = f"{groups['feet']} foot {groups['inches']} is {about_word}{words} {target_name}."
        lead = re.sub(r"\b\d+\b", lambda m: say_value(F(int(m[0])))[0], lead)
    else:
        if value == 1 and groups.get("qty") is None:
            amount = _article(source_name)
        lead = f"{amount} {source_name} is {about_word}{words} {target_name}."
    extra = " ".join(part for part in (note or _note(source, target),) if part)
    line = f"{lead} {extra}".strip()
    return "[neutral] " + line[0].upper() + line[1:]


def may_match(text):
    said = (text or "")[:400]
    return bool(_NEEDS.search(said) or _UNIT_TO_UNIT.search(said))


async def handle(text, ctx):
    if not may_match(text):
        return None
    line = answer(text, getattr(ctx, "name", "pip"))
    return Reply(line) if line else None
