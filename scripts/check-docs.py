#!/usr/bin/env python3
"""Check that the PipOS manual still matches the code it describes.

Each page in docs/manual starts with a header comment:

    <!--
    doc: architecture
    verified: 2026-11-23
    sources: crates/brainstem/src/machine.rs mind/turn.py ...
    -->

The checker reports three kinds of problem:

* a listed source file that is missing;
* a listed source file changed after the page's `verified` date (git
  commit date when the file is committed and clean, otherwise its mtime);
* a relative Markdown link or #anchor that does not resolve.

It exits 1 if anything is reported. Pass --stale-only to skip link checks.
"""

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "manual"
HEADER = re.compile(r"\A<!--(.*?)-->", re.S)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
FENCE = re.compile(r"^```.*?^```", re.S | re.M)


def parse_header(text):
    match = HEADER.match(text)
    if not match:
        return None
    fields = {}
    key = None
    for line in match.group(1).splitlines():
        if re.match(r"^\s*[a-z]+:", line):
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
        elif key and line.strip():
            fields[key] += " " + line.strip()
    return fields


def slug(heading):
    """GitHub-style anchor for a heading."""
    text = re.sub(r"[`*_]|<[^>]+>", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path, cache={}):
    if path not in cache:
        text = FENCE.sub("", path.read_text(encoding="utf-8"))
        seen, result = {}, set()
        for _, title in HEADING.findall(text):
            base = slug(title)
            count = seen.get(base, 0)
            result.add(base if count == 0 else f"{base}-{count}")
            seen[base] = count + 1
        cache[path] = result
    return cache[path]


def changed_at(path):
    """Last change time of a source file, as an aware UTC datetime."""
    rel = path.relative_to(ROOT)
    try:
        dirty = subprocess.run(["git", "status", "--porcelain", "--", str(rel)], cwd=ROOT,
                               capture_output=True, text=True, timeout=10)
        stamp = subprocess.run(["git", "log", "-1", "--format=%cI", "--", str(rel)], cwd=ROOT,
                               capture_output=True, text=True, timeout=10)
        if dirty.returncode == 0 and not dirty.stdout.strip() and stamp.stdout.strip():
            return dt.datetime.fromisoformat(stamp.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    if path.is_dir():
        files = [p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        mtime = max((p.stat().st_mtime for p in files), default=path.stat().st_mtime)
    else:
        mtime = path.stat().st_mtime
    return dt.datetime.fromtimestamp(mtime, dt.timezone.utc)


def check_page(page, stale_only):
    problems = []
    text = page.read_text(encoding="utf-8")
    header = parse_header(text)
    name = page.relative_to(ROOT)
    if header is None:
        return [f"{name}: missing doc header"]
    try:
        verified = dt.date.fromisoformat(header.get("verified", ""))
    except ValueError:
        return [f"{name}: bad or missing 'verified:' date"]
    # A page verified on a day covers changes made up to the end of that local day.
    cutoff = dt.datetime.combine(verified + dt.timedelta(days=1), dt.time()).astimezone()
    for source in header.get("sources", "").split():
        path = ROOT / source
        if not path.exists():
            problems.append(f"{name}: source missing: {source}")
        elif changed_at(path) >= cutoff:
            problems.append(f"{name}: {source} changed after verified {verified}")
    if stale_only:
        return problems
    body = FENCE.sub("", text)
    for target in LINK.findall(body):
        if re.match(r"^[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (page.parent / file_part).resolve() if file_part else page
        if not dest.exists():
            problems.append(f"{name}: broken link {target}")
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            problems.append(f"{name}: missing anchor {target}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stale-only", action="store_true", help="skip link and anchor checks")
    args = parser.parse_args()
    pages = sorted(MANUAL.glob("*.md"))
    problems = [p for page in pages for p in check_page(page, args.stale_only)]
    for problem in problems:
        print(problem)
    print(f"{len(pages)} pages checked, {len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
