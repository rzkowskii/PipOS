"""Hangman for Pip's console. Type a letter and press Enter.

Part of Pip's built-in shelf. `--seed N` picks the same word every time.
"""

import random
import sys

WORDS = (
    "kettle", "lantern", "pebble", "meadow", "biscuit", "harbour", "violin",
    "compass", "thunder", "pocket", "garden", "rocket", "puzzle", "candle",
)
LIVES = 7


def show(word, guessed):
    return " ".join(letter if letter in guessed else "_" for letter in word)


def play(word, lines, write):
    guessed, misses = set(), 0
    write(f"I'm thinking of a word with {len(word)} letters. You have {LIVES} lives.")
    for raw in lines:
        letter = raw.strip().lower()[:1]
        if not letter.isalpha():
            write("Type one letter, then Enter.")
            continue
        if letter in guessed:
            write(f"You already tried {letter}.")
            continue
        guessed.add(letter)
        if letter not in word:
            misses += 1
            write(f"No {letter}. Lives left: {LIVES - misses}.")
        write(show(word, guessed))
        if all(c in guessed for c in word):
            write(f"You got it: {word}!")
            return True
        if misses >= LIVES:
            write(f"Out of lives. The word was {word}.")
            return False
    write(f"Bye. The word was {word}.")
    return False


def main(argv):
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else None
    word = random.Random(seed).choice(WORDS)

    def lines():
        while True:
            try:
                yield input("letter> ")
            except EOFError:
                return
    play(word, lines(), print)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
