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


def quote(text, limit=12):
    """A short exact excerpt of the owner's words, never paraphrased."""
    words = terminal_excerpt(text).split()
    return " ".join(words[:limit]) + ("…" if len(words) > limit else "")


def dream_report(db, owner, *, now=None, zone=None, enabled=True, window_hours=18):
    """The answer to "What did you dream about?", built only from the journal.

    Returns (line, source_ids). A dream that did not happen is said plainly;
    nothing is ever invented to fill the gap.
    """
    now = time.time() if now is None else now
    if not enabled:
        return ("I didn't dream. Dreaming is switched off; you can turn it on in Setup, "
                "under Senses and memory."), ()
    entries = [entry for entry in journal(db, owner, limit=20) if now - entry.published_at <= window_hours * 3600]
    if not entries:
        if dreamt_recently(db, owner, now=now, zone=zone):
            # It did dream; it just kept nothing. Never claim it did not.
            return ("I dreamt last night, but nothing from our conversations stood out enough to keep, "
                    "so I have nothing to tell you."), ()
        return ("I didn't dream last night, so I have nothing to tell you. "
                "I only dream with my lid closed while I'm plugged in."), ()
    first = entries[0]
    count = len(entries)
    moments = "one moment" if count == 1 else f"{count} moments"
    line = (f"I went back over {moments} from our conversations. "
            f"One was when you said: \"{quote(first.source.text)}\"")
    return line, tuple(entry.source.id for entry in entries)


def dreamt_recently(db, owner, *, now=None, zone=None):
    """Whether a dream ran on the night that ended today or yesterday, from its checkpoint."""
    from datetime import date

    now = time.time() if now is None else now
    try:
        row = db.execute("SELECT night, batches FROM dream_checkpoints WHERE owner = ?", (owner,)).fetchone()
    except sqlite3.Error:
        return False
    if not row or not isinstance(row[0], str) or not row[1]:
        return False
    try:
        night = date.fromisoformat(row[0])
    except ValueError:
        return False
    return 0 <= (datetime.fromtimestamp(now, zone).date() - night).days <= 1


def morning_remark(db, owner, *, now=None, window_hours=12):
    """One tentative remark after a dream night, or None. Said at most once."""
    now = time.time() if now is None else now
    entries = [entry for entry in journal(db, owner, limit=5) if now - entry.published_at <= window_hours * 3600]
    if not entries:
        return None
    return f"I was thinking about when you said \"{quote(entries[0].source.text, 8)}\". I might have that wrong."


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
