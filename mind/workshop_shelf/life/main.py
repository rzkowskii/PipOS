"""Conway's game of life for Pip's console. Ctrl-C or Esc on the laptop stops it.

Part of Pip's built-in shelf. `--frames N` draws N generations quickly.
"""

import random
import sys
import time

WIDTH, HEIGHT = 60, 18


def generation(cells):
    counts = {}
    for x, y in cells:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    key = ((x + dx) % WIDTH, (y + dy) % HEIGHT)
                    counts[key] = counts.get(key, 0) + 1
    return {cell for cell, n in counts.items() if n == 3 or (n == 2 and cell in cells)}


def draw(cells, turn):
    rows = ["".join("#" if (x, y) in cells else "." for x in range(WIDTH)) for y in range(HEIGHT)]
    return "\x1b[H" + "\x1b[K\n".join(rows) + f"\x1b[K\ngeneration {turn}, {len(cells)} alive\x1b[K\n"


def main(argv):
    frames = int(argv[argv.index("--frames") + 1]) if "--frames" in argv else 500
    rng = random.Random(3 if "--frames" in argv else None)
    cells = {(x, y) for x in range(WIDTH) for y in range(HEIGHT) if rng.random() < 0.25}
    sys.stdout.write("\x1b[2J")
    try:
        for turn in range(frames):
            sys.stdout.write(draw(cells, turn))
            sys.stdout.flush()
            cells = generation(cells)
            if not cells:
                break
            if "--frames" not in argv:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    print("Life stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
