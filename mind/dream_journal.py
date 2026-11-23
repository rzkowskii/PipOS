"""Read the local Dream Journal and its original conversation excerpts."""

import argparse
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .dream_store import MAX_JOURNAL_LIMIT, journal
from .profile import FACTORY_CONFIG, default_mind_dir, load_profile
from .retention import load_policy
from .skills import Skills


def terminal_excerpt(text, limit=240):
    """Keep one bounded display line and remove terminal controls."""
    line = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    if len(line) <= limit:
        return line
    return line[:limit - 1].rstrip() + "…"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="being-dream-journal", description=__doc__)
    parser.add_argument("--mind-dir", type=Path, help="directory containing the local archive")
    parser.add_argument("--config", type=Path, default=FACTORY_CONFIG, help="factory configuration directory")
    parser.add_argument("--limit", type=int, default=20, choices=range(1, MAX_JOURNAL_LIMIT + 1),
                        metavar=f"1–{MAX_JOURNAL_LIMIT}", help="maximum number of recent entries to show")
    args = parser.parse_args(argv)
    mind_dir = args.mind_dir or default_mind_dir()
    policy = load_policy(mind_dir)
    if policy.mode != "persistent":
        print("The dream journal is available with persistent memory.")
        return 0
    database = mind_dir / "mind.db"
    try:
        # A first run may have no archive. Never create one just to read it.
        try:
            archive_owner = database.stat().st_uid
        except FileNotFoundError:
            print("No dream journal entries yet.")
            return 0
        if os.geteuid() not in (0, archive_owner):
            raise PermissionError("Run as the local archive owner.")
        owner = load_profile(args.config, mind_dir).owner
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            entries = journal(db, owner, limit=args.limit)
        cutoff = policy.cutoff(time.time())
        if cutoff is not None:
            entries = [entry for entry in entries if entry.source.ts >= cutoff]
        if not entries:
            print("No dream journal entries yet.")
            return 0
        zone = Skills(mind_dir).zone
        print("Dream journal")
        for entry in entries:
            published = datetime.fromtimestamp(entry.published_at, zone)
            source = datetime.fromtimestamp(entry.source.ts, zone)
            print(f"{published:%Y-%m-%d %H:%M} · Episode {entry.source.id} · heard {source:%Y-%m-%d %H:%M}")
            print(f"  {terminal_excerpt(entry.source.text)}")
    except (OSError, OverflowError, ValueError, sqlite3.Error) as error:
        print(f"Could not read the dream journal. {terminal_excerpt(str(error))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
