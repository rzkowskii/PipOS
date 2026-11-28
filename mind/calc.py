"""Spoken sums, answered in code: "what's 17 times 23?".

The words are turned into a small expression, which is parsed with `ast` and
walked by a whitelist evaluator (numbers, + - * / and powers, square and cube
roots). Nothing is ever passed to `eval`. Arithmetic is exact (fractions)
wherever it can be, so "a tenth plus two tenths" is exactly 0.3.

Limits, all said honestly rather than guessed: exponents up to 100, answers
up to a million billion (1e15), no division by zero, no square root of a
negative number. A rounded answer always says "about".

`parse_number`, `say_number` and `say_value` are shared with mind/units.py.
"""

import ast
import math
import re
from decimal import Decimal
from fractions import Fraction

from .app_commands import spoken_numbers
from .reply import Reply, clean, is_guest
from .skills import number_words as _hundreds_words

MAX_EXPONENT = 100
MAX_RESULT = 10 ** 15
MAX_EXACT_BITS = 4096  # bound intermediate denominators under repeated powers
KEYBOARD_CHARS = 72
PANEL_TEXT = 160

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_UNITS["oh"] = 0
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALES = {"thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9, "trillion": 10 ** 12}
_FRACTIONS = {"half": Fraction(1, 2), "quarter": Fraction(1, 4), "third": Fraction(1, 3)}
_ORDINALS = {"second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
_DIGIT = re.compile(r"^\d+(?:\.\d+)?$|^\.\d+$")


class CalcError(Exception):
    """A sum with no honest answer; `line` says why."""

    def __init__(self, line):
        super().__init__(line)
        self.line = line


DIVIDE_BY_ZERO = "[neutral] Dividing by zero has no answer, so I can't give you one."
NO_REAL_ROOT = "[neutral] A negative number has no ordinary square root, so I can't give you one."
NO_REAL_POWER = "[neutral] That power of a negative number has no ordinary answer."
TOO_BIG = "[neutral] That answer is bigger than a million billion, which is more than I'll work out."
POWER_TOO_BIG = "[neutral] I only work out powers up to a hundred."


# -- numbers in ---------------------------------------------------------------

def _atom(token):
    """(value, kind) for one number word or digit string, else None."""
    if _DIGIT.match(token):
        return Fraction(token), "digits"
    if token in _UNITS:
        return Fraction(_UNITS[token]), "word"
    if token in _TENS:
        return Fraction(_TENS[token]), "tens"
    if "-" in token:
        parts = token.split("-")
        if len(parts) == 2 and parts[0] in _TENS and parts[1] in _UNITS and 0 < _UNITS[parts[1]] < 10:
            return Fraction(_TENS[parts[0]] + _UNITS[parts[1]]), "word"
    return None


def parse_number(tokens, start=0):
    """Read one spoken or written number at tokens[start].

    Returns (Fraction, next index) or None. Understands "17", "3.5",
    "twenty-three", "a hundred and twenty", "two thousand and five",
    "1.5 million", "three point one four", "a half", "two and a half".
    """
    index, total, group, previous = start, Fraction(0), None, None
    count = len(tokens)
    while index < count:
        token = tokens[index]
        atom = _atom(token)
        if atom is not None:
            value, kind = atom
            if group is None:
                group = value
            elif previous in ("hundred", "and") and group % 100 == 0 and value < 100:
                group += value
            elif previous == "tens" and value < 10 and kind == "word" and value.denominator == 1:
                group += value
            else:
                break
            previous = "tens" if kind == "tens" else "atom"
            index += 1
            continue
        nxt = tokens[index + 1] if index + 1 < count else ""
        if token in ("a", "an") and group is None and previous is None and (
                nxt == "hundred" or nxt in _SCALES or nxt in ("half", "quarter")):
            if nxt in ("half", "quarter"):
                return _FRACTIONS[nxt], index + 2
            group, previous = Fraction(1), "atom"
            index += 1
            continue
        if token == "hundred" and group is not None and group < 100 and previous != "hundred":
            group *= 100
            previous = "hundred"
            index += 1
            continue
        if token in _SCALES and group is not None and previous not in ("scale",):
            total += group * _SCALES[token]
            group, previous = None, "scale"
            index += 1
            continue
        if token == "and" and previous in ("hundred", "scale", "atom", "tens"):
            after = tokens[index + 2] if index + 2 < count else ""
            if nxt in ("a", "one") and after in ("half", "quarter"):
                return total + (group or 0) + _FRACTIONS[after], index + 3
            if previous in ("hundred", "scale") and _atom(nxt) is not None and _atom(nxt)[0] < 100:
                previous = "and"
                index += 1
                continue
            break
        if token == "point" and previous in ("atom", "tens", "hundred", "scale") or (
                token == "point" and group is None and previous is None and _atom(nxt) is not None):
            digits = ""
            look = index + 1
            while look < count:
                word = tokens[look]
                if word in _UNITS and _UNITS[word] < 10:
                    digits += str(_UNITS[word])
                elif word.isdigit() and (len(word) == 1 or not digits):
                    digits += word
                else:
                    break
                look += 1
                if len(digits) > 12:
                    break
            if not digits:
                break
            return total + (group or 0) + Fraction(int(digits), 10 ** len(digits)), look
        if token in ("half", "quarter") and group == 1 and previous == "atom" and total == 0:
            # "one half", "one quarter"
            return _FRACTIONS[token], index + 1
        break
    if previous is None or previous == "and":
        return None
    return total + (group or 0), index


# -- numbers out --------------------------------------------------------------

def _integer_words(number):
    if number < 0:
        return "minus " + _integer_words(-number)
    if number < 1000:
        return _hundreds_words(number)
    chunks = []
    for name, scale in (("trillion", 10 ** 12), ("billion", 10 ** 9), ("million", 10 ** 6), ("thousand", 10 ** 3)):
        if number >= scale:
            chunk, number = divmod(number, scale)
            chunks.append(f"{_integer_words(chunk)} {name}")
    if number:
        chunks.append(("and " if number < 100 else "") + _hundreds_words(number))
    return ", ".join(chunks).replace(", and ", " and ")


def say_number(value):
    """Exact words for an integer or a terminating decimal: 'seventeen point one'."""
    value = Fraction(value)
    if value.denominator == 1:
        return _integer_words(int(value))
    sign = "minus " if value < 0 else ""
    value = abs(value)
    whole = int(value)
    rest = value - whole
    digits = ""
    while rest and len(digits) < 20:
        rest *= 10
        digit = int(rest)
        digits += str(digit)
        rest -= digit
    return f"{sign}{_integer_words(whole)} point " + " ".join(_hundreds_words(int(d)) for d in digits)


TINY = Fraction(1, 10 ** 9)   # nearer zero than this is said as "about zero"


def rounded(value):
    """(Fraction, approximate) at three significant figures, never fewer than
    the whole-number digits: 17.142 -> 17.1, 3333.3 -> 3333, 0.33333 -> 0.333.
    Anything nearer zero than a billionth is about zero."""
    exact = Fraction(value)
    if exact == 0:
        return Fraction(0), False
    if abs(exact) < TINY:
        return Fraction(0), True
    magnitude = math.floor(math.log10(abs(float(exact))))
    scale = 10 ** max(0, 2 - magnitude)
    near = Fraction(round(exact * scale), scale)
    if isinstance(value, float):
        return near, abs(float(near) - value) > 1e-9 * max(1.0, abs(value))
    return near, near != exact


def say_value(value):
    """(words, approximate): the answer as Pip says it."""
    if isinstance(value, Fraction) and value.denominator == 1:
        return _integer_words(int(value)), False
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return _integer_words(int(value)), False
    near, approximate = rounded(value)
    return say_number(near), approximate


# -- the expression -----------------------------------------------------------

_PREFIX = re.compile(
    r"^(?:what(?:'s| is| are| does| do you get(?: if you| when you)?| would| will)?|how much(?: is| are)?"
    r"|calculate|compute|work out|solve|tell me|quick (?:maths|math)|do you know|is|whats)\s+", re.I)
_SUFFIX = re.compile(r"\s+(?:equal|equals|make|makes|come to|comes to|is|be|give|gives|=|in total|altogether)$", re.I)
_REWRITES = [
    (re.compile(r"^(?:the )?sum of (.+) and (.+)$"), r"\1 plus \2"),
    (re.compile(r"^(?:the )?product of (.+) and (.+)$"), r"\1 times \2"),
    (re.compile(r"^(?:the )?difference between (.+) and (.+)$"), r"\1 minus \2"),
    (re.compile(r"^add (.+?) (?:and|to|plus) (.+)$"), r"\1 plus \2"),
    (re.compile(r"^subtract (.+) from (.+)$"), r"\2 minus \1"),
    (re.compile(r"^take (.+) (?:away )?from (.+)$"), r"\2 minus \1"),
    (re.compile(r"^multiply (.+) (?:by|and) (.+)$"), r"\1 times \2"),
    (re.compile(r"^divide (.+) by (.+)$"), r"\1 divided by \2"),
    (re.compile(r"^(?:a )?half of (.+)$"), r"\1 divided by 2"),
    (re.compile(r"^(?:a )?quarter of (.+)$"), r"\1 divided by 4"),
    (re.compile(r"^(?:double|twice) (.+)$"), r"2 times \1"),
]
_TOKEN = re.compile(r"\d+(?:\.\d+)?|\.\d+|[a-z]+(?:-[a-z]+)*|\*\*|[+*/^()=%x-]")

_OPERATORS = [
    (("plus",), "+", "plus"), (("+",), "+", "plus"), (("and",), None, None),
    (("minus",), "-", "minus"), (("-",), "-", "minus"), (("take", "away"), "-", "minus"),
    (("less",), "-", "minus"),
    (("times",), "*", "times"), (("x",), "*", "times"), (("*",), "*", "times"),
    (("multiplied", "by"), "*", "times"),
    (("divided", "by"), "/", "divided by"), (("over",), "/", "divided by"), (("/",), "/", "divided by"),
    (("to", "the", "power", "of"), "**", "to the power of"), (("raised", "to", "the", "power", "of"), "**", "to the power of"),
    (("raised", "to"), "**", "to the power of"), (("to", "the"), "**", "to the power of"),
    (("^",), "**", "to the power of"), (("**",), "**", "to the power of"),
]
_ROOTS = [
    (("the", "square", "root", "of"), "sqrt"), (("square", "root", "of"), "sqrt"), (("square", "root"), "sqrt"),
    (("the", "cube", "root", "of"), "cbrt"), (("cube", "root", "of"), "cbrt"), (("cube", "root"), "cbrt"),
]


def _starts(tokens, index, words):
    return tuple(tokens[index:index + len(words)]) == words


MAX_TOKENS = 80     # far more than a 15-second breath; a longer "sum" is not one
MAX_CHARS = 400


def tokens_of(text, name="pip"):
    said = (text or "").replace("×", " times ").replace("÷", " divided by ").replace("−", "-")
    said = clean(said, name).casefold()
    said = said.rstrip("?").strip()
    for _ in range(3):
        before = said
        said = _PREFIX.sub("", said)
        said = _SUFFIX.sub("", said)
        if said == before:
            break
    said = re.sub(r"\bper cent\b", "percent", said)
    said = re.sub(r"\bmultiplied with\b", "multiplied by", said)
    said = re.sub(r"\bdivide by\b", "divided by", said)
    for pattern, replacement in _REWRITES:
        said = pattern.sub(replacement, said)
    said = spoken_numbers(said)
    return _TOKEN.findall(said)


def build(tokens):
    """(source, names, spoken words) for a sum, or None if it is not one."""
    source, words, names = [], [], {}
    index, expect_operand, operations = 0, True, 0
    count = len(tokens)
    if count > MAX_TOKENS:
        return None
    while index < count:
        token = tokens[index]
        if expect_operand:
            if token in ("minus", "negative", "-"):
                source.append("-")
                words.append("minus")
                index += 1
                continue
            if token in ("plus", "+", "positive"):
                index += 1
                continue
            root = next(((w, f) for w, f in _ROOTS if _starts(tokens, index, w)), None)
            if root:
                index += len(root[0])
                negative = index < count and tokens[index] in ("minus", "negative", "-")
                parsed = parse_number(tokens, index + negative)
                if parsed is None:
                    return None
                value, index = parsed
                value = -value if negative else value
                name = f"n{len(names)}"
                names[name] = value
                source.append(f"{root[1]}({name})")
                words.append(("the square root of " if root[1] == "sqrt" else "the cube root of ") + say_number(value))
                operations += 1
                expect_operand = False
                continue
            parsed = parse_number(tokens, index)
            if parsed is None:
                return None
            value, index = parsed
            name = f"n{len(names)}"
            names[name] = value
            source.append(name)
            words.append(say_number(value))
            expect_operand = False
            if index < count and tokens[index] in ("percent", "%"):
                index += 1
                operations += 1
                if index < count and tokens[index] == "of":
                    source.append("/100*")
                    words.append("percent of")
                    index += 1
                    expect_operand = True
                else:
                    source.append("/100")
                    words.append("percent")
            continue
        if token == "squared" or token == "cubed":
            source.append("**2" if token == "squared" else "**3")
            words.append(token)
            operations += 1
            index += 1
            continue
        match = next(((w, op, said) for w, op, said in _OPERATORS if _starts(tokens, index, w)), None)
        if match is None or match[1] is None:
            return None
        index += len(match[0])
        source.append(match[1])
        words.append(match[2])
        operations += 1
        expect_operand = True
        if match[1] == "**":
            # "2 to the 10th (power)", "2 to the tenth", "to the power of 10"
            if index < count and tokens[index] in _ORDINALS and tokens[index] != "second" or (
                    index < count and tokens[index] == "second" and index + 1 >= count):
                name = f"n{len(names)}"
                names[name] = Fraction(_ORDINALS[tokens[index]])
                source.append(name)
                words.append(say_number(names[name]))
                index += 1
                expect_operand = False
            else:
                negative = index < count and tokens[index] in ("minus", "negative", "-")
                parsed = parse_number(tokens, index + negative)
                if parsed is None:
                    return None
                value, index = parsed
                name = f"n{len(names)}"
                names[name] = -value if negative else value
                source.append(f"({name})")
                words.append(say_number(names[name]))
                expect_operand = False
            if index < count and tokens[index] in ("th", "st", "nd", "rd"):
                index += 1
            if index < count and tokens[index] == "power":
                index += 1
    if expect_operand or not operations or not names:
        return None
    return "".join(source), names, words


# -- the whitelist evaluator --------------------------------------------------

def _check(value):
    if isinstance(value, Fraction) and max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_EXACT_BITS:
        raise CalcError(TOO_BIG)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise CalcError(TOO_BIG)
    if abs(value) > MAX_RESULT * 1000:
        raise CalcError(TOO_BIG)
    return value


def _root(value, degree):
    if value < 0 and degree == 2:
        raise CalcError(NO_REAL_ROOT)
    if isinstance(value, Fraction):
        sign = -1 if value < 0 else 1
        top, bottom = abs(value.numerator), value.denominator
        exact_top = round(top ** (1 / degree))
        exact_bottom = round(bottom ** (1 / degree))
        for top_guess in (exact_top - 1, exact_top, exact_top + 1):
            for bottom_guess in (exact_bottom - 1, exact_bottom, exact_bottom + 1):
                if top_guess >= 0 and bottom_guess > 0 and top_guess ** degree == top and bottom_guess ** degree == bottom:
                    return Fraction(sign * top_guess, bottom_guess)
    magnitude = abs(float(value)) ** (1 / degree)
    return math.copysign(magnitude, float(value))


def _power(base, exponent):
    if isinstance(exponent, Fraction) and exponent.denominator == 1 or (
            isinstance(exponent, float) and exponent.is_integer()):
        whole = int(exponent)
        if abs(whole) > MAX_EXPONENT:
            raise CalcError(POWER_TOO_BIG)
        if base == 0 and whole < 0:
            raise CalcError(DIVIDE_BY_ZERO)
        if base != 0 and abs(base) != 1 and whole * math.log10(abs(float(base))) > 18:
            raise CalcError(TOO_BIG)
        return base ** whole
    if abs(exponent) > MAX_EXPONENT:
        raise CalcError(POWER_TOO_BIG)
    if base < 0:
        raise CalcError(NO_REAL_POWER)
    try:
        return float(base) ** float(exponent)
    except OverflowError:
        raise CalcError(TOO_BIG) from None


def evaluate(source, names):
    tree = ast.parse(source, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Name) and node.id in names:
            return _check(names[node.id])
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return Fraction(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return _check(left + right)
            if isinstance(node.op, ast.Sub):
                return _check(left - right)
            if isinstance(node.op, ast.Mult):
                return _check(left * right)
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise CalcError(DIVIDE_BY_ZERO)
                return _check(left / right)
            return _check(_power(left, right))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("sqrt", "cbrt")
                and len(node.args) == 1 and not node.keywords):
            return _check(_root(walk(node.args[0]), 2 if node.func.id == "sqrt" else 3))
        raise ValueError(f"not allowed: {ast.dump(node)[:40]}")

    value = walk(tree)
    if abs(value) > MAX_RESULT:
        raise CalcError(TOO_BIG)
    return value


def calculate(text, name="pip"):
    """(value, spoken sum) or None when `text` is not a sum. Raises CalcError."""
    if len(text or "") > MAX_CHARS:
        return None
    built = build(tokens_of(text, name))
    if built is None:
        return None
    source, names, words = built
    return evaluate(source, names), " ".join(words)


def _result(value):
    near, approximate = rounded(value)
    decimal = Decimal(near.numerator) / Decimal(near.denominator)
    return ("About " if approximate else "") + format(decimal, "f")


def _spoken_result(text, name):
    """The spoken line and optional calculator display for a recognised sum."""
    try:
        result = calculate(text, name)
    except CalcError as error:
        return error.line, "", error.line.removeprefix("[neutral] "), True
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        return None
    if result is None:
        return None
    value, spoken = result
    words, approximate = say_value(value)
    about = "about " if approximate else ""
    line = f"{spoken} is {about}{words}."
    if len(line.split()) > 25:
        line = f"That's {about}{words}."
    return "[neutral] " + line[0].upper() + line[1:], spoken, _result(value), False


def answer(text, name="pip"):
    """The spoken line for a sum, or None."""
    result = _spoken_result(text, name)
    return result[0] if result is not None else None


_KEYBOARD_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+|\*\*|[-+*/^()]")


def keyboard(text):
    """A strict symbolic expression from the focused keypad, evaluated exactly where possible."""
    if len(text) > KEYBOARD_CHARS:
        return "That calculation is too long.", True
    tokens = _KEYBOARD_TOKEN.findall(text)
    if not tokens or "".join(tokens) != text.replace(" ", ""):
        return "Enter numbers and arithmetic operators.", True
    names, source = {}, []
    for token in tokens:
        if token[0].isdigit() or token[0] == ".":
            key = f"n{len(names)}"
            names[key] = Fraction(token)
            source.append(key)
        else:
            source.append("**" if token == "^" else token)
    # Separating tokens keeps malformed "2* *3" or "2 3" from becoming valid.
    try:
        return _result(evaluate(" ".join(source), names)), False
    except CalcError as error:
        return error.line.removeprefix("[neutral] "), True
    except (ArithmeticError, SyntaxError, ValueError):
        return "Check the numbers, operators and parentheses.", True


_NUMBERISH = re.compile(
    r"\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    r"|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
    r"|hundred|thousand|million|billion|half)\b", re.I)
# The gate is also how the mind tells a follow-up meant for it from the room,
# so it stays narrow: "x" only as a word, "-" only between or before digits.
_OPERATOR_WORDS = re.compile(
    r"\b(?:plus|minus|times|multiplied|divided|divide|over|percent|per cent|squared|cubed|power|take away"
    r"|subtract|multiply|add)\b|[+*/^%×÷]|\bx\b|\d\s*-\s*\d|(?:^|\s)-\s*\d|\bsquare root\b|\bcube root\b", re.I)


def may_match(text):
    said = (text or "")[:MAX_CHARS]
    if re.search(r"\b(?:square|cube) root\b", said, re.I):
        return True
    return bool(_OPERATOR_WORDS.search(said) and _NUMBERISH.search(said))


async def handle(text, ctx):
    if not may_match(text):
        return None
    result = _spoken_result(text, getattr(ctx, "name", "pip"))
    if result is None:
        return None
    line, expression, shown, error = result
    if getattr(ctx, "calc_panel", False) and not is_guest(ctx):
        if len(expression) > PANEL_TEXT:
            expression = expression[:PANEL_TEXT - 3] + "..."
        await ctx.send("app.calc_display", expression=expression, result=shown, error=error)
    return Reply(line)
