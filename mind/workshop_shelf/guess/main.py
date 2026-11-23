"""Guess the number for Pip's console. Type a number and press Enter.

Part of Pip's built-in shelf. `--seed N` picks the same number every time.
"""

import random
import sys


def play(secret, lines, write, top=100):
    write(f"I picked a number from 1 to {top}. Can you guess it?")
    tries = 0
    for raw in lines:
        try:
            guess = int(raw.strip())
        except ValueError:
            write("Type a whole number, then Enter.")
            continue
        tries += 1
        if guess < secret:
            write("Higher.")
        elif guess > secret:
            write("Lower.")
        else:
            write(f"Yes! It was {secret}. You took {tries} {'try' if tries == 1 else 'tries'}.")
            return tries
    write(f"Bye. It was {secret}.")
    return None


def main(argv):
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else None
    secret = random.Random(seed).randint(1, 100)

    def lines():
        while True:
            try:
                yield input("guess> ")
            except EOFError:
                return
    play(secret, lines(), print)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
