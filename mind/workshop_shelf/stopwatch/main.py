"""A stopwatch for Pip's console. Enter marks a lap, Ctrl-C stops it.

Part of Pip's built-in shelf. `--for SECONDS` runs for a fixed time.
"""

import select
import sys
import time


def stamp(seconds):
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes):02d}:{rest:05.2f}"


def main(argv):
    limit = float(argv[argv.index("--for") + 1]) if "--for" in argv else None
    start = time.monotonic()
    laps = 0
    print("Stopwatch running. Enter marks a lap, Ctrl-C stops.")
    try:
        while True:
            elapsed = time.monotonic() - start
            if limit is not None and elapsed >= limit:
                break
            sys.stdout.write(f"\r{stamp(elapsed)}\x1b[K")
            sys.stdout.flush()
            ready = select.select([sys.stdin], [], [], 0.1)[0] if limit is None else []
            if ready:
                if not sys.stdin.readline():
                    break
                laps += 1
                print(f"\rlap {laps}: {stamp(elapsed)}\x1b[K")
            elif limit is not None:
                time.sleep(min(0.1, max(0.0, limit - elapsed)))
    except KeyboardInterrupt:
        pass
    print(f"\rStopped at {stamp(time.monotonic() - start)}.\x1b[K")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
