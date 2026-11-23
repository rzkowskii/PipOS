"""Roll dice in Pip's console. Enter rolls again, q then Enter quits.

Part of Pip's built-in shelf. `dice 3` rolls three dice; `--seed N` repeats.
"""

import random
import sys

FACES = {
    1: ("     ", "  o  ", "     "),
    2: ("o    ", "     ", "    o"),
    3: ("o    ", "  o  ", "    o"),
    4: ("o   o", "     ", "o   o"),
    5: ("o   o", "  o  ", "o   o"),
    6: ("o   o", "o   o", "o   o"),
}


def picture(values):
    top = "  ".join("+-----+" for _ in values)
    middle = ["  ".join(f"|{FACES[v][row]}|" for v in values) for row in range(3)]
    return "\n".join([top, *middle, top, f"total {sum(values)}"])


def main(argv):
    seed, count, rest = None, 2, list(argv)
    while rest:
        word = rest.pop(0)
        if word == "--seed" and rest:
            seed = int(rest.pop(0))
        elif word.isdigit():
            count = max(1, min(6, int(word)))
    rng = random.Random(seed)
    while True:
        print(picture([rng.randint(1, 6) for _ in range(count)]))
        try:
            again = input("Enter rolls again, q quits> ")
        except EOFError:
            break
        if again.strip().lower().startswith("q"):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
