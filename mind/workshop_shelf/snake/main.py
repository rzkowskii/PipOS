"""Snake for Pip's console. W/A/S/D to steer, Q to quit.

Part of Pip's built-in shelf: written for this console, no internet needed.
`--frames N` draws N frames without a keyboard (used by the tests).
"""

import os
import random
import select
import sys
import time

WIDTH, HEIGHT = 40, 16
STEP = 0.16
MOVES = {"w": (0, -1), "a": (-1, 0), "s": (0, 1), "d": (1, 0)}


def frame(snake, food, score):
    rows = ["+" + "-" * WIDTH + "+"]
    body = set(snake)
    for y in range(HEIGHT):
        line = []
        for x in range(WIDTH):
            if (x, y) == snake[0]:
                line.append("@")
            elif (x, y) in body:
                line.append("o")
            elif (x, y) == food:
                line.append("*")
            else:
                line.append(" ")
        rows.append("|" + "".join(line) + "|")
    rows.append("+" + "-" * WIDTH + "+")
    rows.append(f"score {score}   W A S D to steer, Q to quit")
    return "\x1b[H" + "\x1b[K\n".join(rows) + "\x1b[K\n"


def place_food(snake, rng):
    free = [(x, y) for x in range(WIDTH) for y in range(HEIGHT) if (x, y) not in snake]
    return rng.choice(free) if free else None


def step(snake, heading, food, rng):
    """Move once. Returns (snake, food, alive, ate)."""
    head = ((snake[0][0] + heading[0]) % WIDTH, (snake[0][1] + heading[1]) % HEIGHT)
    if head in snake[:-1]:
        return snake, food, False, False
    snake = [head] + snake
    if head == food:
        return snake, place_food(snake, rng), True, True
    return snake[:-1], food, True, False


def read_key():
    if select.select([sys.stdin], [], [], 0)[0]:
        return os.read(sys.stdin.fileno(), 1).decode("ascii", "ignore").lower()
    return ""


def main(argv):
    frames = int(argv[argv.index("--frames") + 1]) if "--frames" in argv else None
    rng = random.Random(7 if frames is not None else None)
    snake = [(WIDTH // 2, HEIGHT // 2), (WIDTH // 2 - 1, HEIGHT // 2), (WIDTH // 2 - 2, HEIGHT // 2)]
    heading = (1, 0)
    food = place_food(snake, rng)
    score = 0
    interactive = frames is None and sys.stdin.isatty()
    saved = None
    if interactive:
        import termios
        import tty
        saved = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
    sys.stdout.write("\x1b[2J\x1b[?25l")
    try:
        count = 0
        while frames is None or count < frames:
            count += 1
            key = read_key() if interactive else ""
            if key == "q":
                break
            if key in MOVES:
                turn = MOVES[key]
                if (turn[0] + heading[0], turn[1] + heading[1]) != (0, 0):
                    heading = turn
            snake, food, alive, ate = step(snake, heading, food, rng)
            score += ate
            sys.stdout.write(frame(snake, food, score))
            sys.stdout.flush()
            if not alive or food is None:
                break
            if frames is None:
                time.sleep(STEP)
    finally:
        if saved is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[?25h")
    print(f"Game over. Score {score}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
