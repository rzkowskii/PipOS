"""Bench mode lives in mind/bench.py, so the image installs it with the mind
(/usr/lib/being/mind/bench.py, started by being-bench.service). This name is
kept for lab scripts and tests: importing it gives that same module."""

import sys

from mind import bench as _bench

sys.modules[__name__] = _bench

if __name__ == "__main__":
    raise SystemExit(_bench.main())
