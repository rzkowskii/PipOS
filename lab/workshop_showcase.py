"""Bounded protocol checklist/replay for the owner's workshop showcase.

This does not inject commands or run programs.  Capture authenticated bus
JSONL from the existing cable/voice harness, then validate it with:

    python3 -m lab.workshop_showcase trace.jsonl

With no argument it prints the exact spoken checklist and causal events.
"""

import argparse
import json
from pathlib import Path

STEPS = (
    ("open", "Open the terminal", {"t": "app.state", "app": "terminal", "view": "shown"}),
    ("recipe", "Write a script that draws a spiral", {"t": "term.status", "source": "recipe", "phase": "writing"}),
    ("pause", "Stop writing that script for a second", {"t": "term.status", "source": "recipe", "phase": "paused"}),
    ("resume", "Carry on", {"t": "term.status", "source": "recipe", "phase": "writing"}),
    ("ready", None, {"t": "term.status", "source": "recipe", "phase": "waiting"}),
    ("run", "Run it", {"t": "term.status", "source": "recipe", "phase": "running"}),
    ("recipe-done", None, {"t": "term.status", "source": "recipe", "phase": "done", "exit": 0}),
    ("minimise", "Minimise the terminal", {"t": "app.state", "app": "terminal", "view": "minimised"}),
    ("restore", "Show me the terminal", {"t": "app.state", "app": "terminal", "view": "shown"}),
    ("shelf", "Install snake", {"t": "term.status", "source": "shelf", "kind": "install", "phase": "writing"}),
    ("shelf-done", None, {"t": "term.status", "source": "shelf", "kind": "install", "phase": "done", "exit": 0}),
    ("close", "Close the terminal", {"t": "app.state", "app": "terminal", "view": "closed"}),
)
MAX_EVENTS = 4096
MAX_LINE = 16384


def validate(events):
    cursor = 0
    results = []
    jobs = {}
    for key, phrase, expected in STEPS:
        required = dict(expected)
        source = required.get("source")
        if source in jobs:
            required["id"] = jobs[source]
        found = next((index for index in range(cursor, len(events))
                      if all(events[index].get(field) == value for field, value in required.items())), None)
        if found is None:
            raise ValueError(f"{key}: missing causal event {required}")
        event = events[found]
        if source is not None and source not in jobs:
            job_id = event.get("id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError(f"{key}: missing job ID")
            jobs[source] = job_id
        results.append({"step": key, "phrase": phrase,
                        "event": {field: event[field] for field in (*required, "id") if field in event}})
        cursor = found + 1
    return results


def read_trace(path):
    events = []
    with Path(path).open("rb") as source:
        for _line_number in range(MAX_EVENTS):
            line = source.readline(MAX_LINE + 1)
            if not line:
                return events
            if len(line) > MAX_LINE:
                raise ValueError("showcase trace line is too large")
            if line.strip():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("showcase trace event must be an object")
                events.append(event)
        if source.read(1):
            raise ValueError("showcase trace exceeds its bounded line limit")
    return events


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", nargs="?", help="captured authenticated bus JSONL")
    args = parser.parse_args(argv)
    if args.trace is None:
        print(json.dumps([{"step": key, "say": phrase, "expect": expected}
                          for key, phrase, expected in STEPS], indent=2))
        return 0
    try:
        result = validate(read_trace(args.trace))
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        return 1
    print(json.dumps({"ok": True, "steps": result}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
