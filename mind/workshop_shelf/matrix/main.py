"""Falling green letters for Pip's console. Ctrl-C stops it.

Part of Pip's built-in shelf. `--frames N` draws N frames quickly.
"""

import random
import string
import sys
import time

WIDTH, HEIGHT = 80, 20
GLYPHS = string.ascii_letters + string.digits + "+-*/<>=$#"


def main(argv):
    frames = int(argv[argv.index("--frames") + 1]) if "--frames" in argv else 600
    rng = random.Random(5 if "--frames" in argv else None)
    drops = [rng.randrange(-HEIGHT, 0) for _ in range(WIDTH)]
    sys.stdout.write("\x1b[2J\x1b[?25l")
    try:
        for _ in range(frames):
            out = []
            for x, y in enumerate(drops):
                if 0 <= y < HEIGHT:
                    out.append(f"\x1b[{y + 1};{x + 1}H\x1b[1;92m{rng.choice(GLYPHS)}")
                if 1 <= y <= HEIGHT:
                    out.append(f"\x1b[{y};{x + 1}H\x1b[0;32m{rng.choice(GLYPHS)}")
                tail = y - 8
                if 0 <= tail < HEIGHT:
                    out.append(f"\x1b[{tail + 1};{x + 1}H ")
                drops[x] = y + 1 if y < HEIGHT + 8 else rng.randrange(-HEIGHT, 0)
            sys.stdout.write("".join(out) + "\x1b[0m")
            sys.stdout.flush()
            if "--frames" not in argv:
                time.sleep(0.08)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(f"\x1b[0m\x1b[{HEIGHT + 1};1H\x1b[?25h")
    print("The rain stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
