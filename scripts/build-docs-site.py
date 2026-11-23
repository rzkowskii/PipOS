#!/usr/bin/env python3
"""Assemble the documentation site's source tree for MkDocs.

The documents in this repository link to each other and to source files
with paths that are relative to the repository. A website has no source
tree, so this script copies the documents into out/docs-site/src and
rewrites each relative link:

- to another published document: the link is kept, with a corrected path
- to an image elsewhere in the repository: the image is copied in
- to any other tracked file or folder: a link to it on GitHub
- to something that is not in the repository (local build logs under
  out/, for example): the link is dropped and its text is kept

The documents are written for GitHub, which reads Markdown by the
CommonMark rules. MkDocs reads it with Python-Markdown, which is
stricter about lists, so each copied document is also normalised
(outside fenced code blocks, and without touching the originals):

- a list or table that follows a paragraph line directly gets the blank
  line Python-Markdown needs before it
- everything inside a list item (nested items, continuation paragraphs,
  fenced code, tables) is re-indented to four spaces for each level
- an escaped pipe inside code in a table cell loses its backslash,
  which Python-Markdown would otherwise print

Run from anywhere:  python3 scripts/build-docs-site.py
Then:               mkdocs build
"""
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "out", "docs-site", "src")
REPO = "https://github.com/rzkowskii/PipOS"
IMAGES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
LINK = re.compile(r'(!?)\[((?:[^\]\\]|\\.)*)\]\(([^)\s]+)(\s+"[^"]*")?\)')
FENCE = re.compile(r"(`{3,}|~{3,})")
ITEM = re.compile(r"([-*+]|(\d{1,9})\.)( +|$)")
RULE = re.compile(r"([-*_])( *\1){2,} *$")
BLOCK = re.compile(r"#{1,6}( |$)|>")
CODE = re.compile(r"(`+)(.+?)\1")
DIVIDER = re.compile(r" *\|? *:?-+:? *(\| *:?-+:? *)*\|? *$")


def published():
    """Map each published repository path to its path inside the site."""
    pages = {"DESIGN-AND-PLAN.md": "DESIGN-AND-PLAN.md"}
    for top in ("docs", "research"):
        for folder, _, names in os.walk(os.path.join(ROOT, top)):
            for name in names:
                if name.startswith("."):
                    continue
                path = os.path.relpath(os.path.join(folder, name), ROOT)
                inside = os.path.relpath(path, "docs") if top == "docs" else path
                pages[path.replace(os.sep, "/")] = inside.replace(os.sep, "/")
    return pages


def rewrite(text, source, pages, assets):
    here = os.path.dirname(source)
    site_here = os.path.dirname(pages[source])

    def fix(match):
        bang, label, target, title = match.group(1), match.group(2), match.group(3), match.group(4) or ""
        if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("#", "/")):
            return match.group(0)
        path, _, anchor = target.partition("#")
        repo_path = os.path.normpath(os.path.join(here, path)).replace(os.sep, "/")
        anchor = "#" + anchor if anchor else ""
        if repo_path in pages:
            new = os.path.relpath(pages[repo_path], site_here or ".").replace(os.sep, "/")
            return f"{bang}[{label}]({new}{anchor}{title})"
        full = os.path.join(ROOT, repo_path)
        if repo_path.startswith("..") or not os.path.exists(full):
            return label
        if bang and repo_path.lower().endswith(IMAGES):
            assets.add(repo_path)
            new = os.path.relpath("_repo/" + repo_path, site_here or ".").replace(os.sep, "/")
            return f"![{label}]({new}{title})"
        kind = "tree" if os.path.isdir(full) else "blob"
        return f"{bang}[{label}]({REPO}/{kind}/main/{repo_path}{anchor}{title})"

    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        out.append(line if fenced else LINK.sub(fix, line))
    return "\n".join(out)


def normalise(text):
    """Re-shape GitHub-style lists into the form Python-Markdown reads."""
    lines = text.split("\n")
    out = []
    stack = []  # open list items: [content column in the source, still in its opening lines]
    depth = 0  # list depth at which the current run of non-blank lines began
    fence = None  # inside fenced code: (closing pattern, source indent, output indent)
    last = ""  # what the previous line was: "", "text", "table", "fence" or "block"

    def separate():
        # Begin a new run of lines, adding the blank line if there is none.
        nonlocal depth
        if out and out[-1]:
            out.append("")
        for item in stack:
            item[1] = False
        depth = len(stack)

    def close_to(indent, item=False):
        # Close the list items that a line at this indent is outside of.
        # Python-Markdown takes a run of lines that begins inside an item
        # as belonging to it, and glues other blocks onto a list they
        # follow directly, so a line that leaves an item may need to
        # begin a run of its own.
        before = len(stack)
        while stack and indent < stack[-1][0]:
            stack.pop()
        if not last or len(stack) < (depth if item else before):
            separate()

    def place(indent, body):
        # Up to three spaces of extra indentation mean nothing; four or
        # more make an indented code block, which keeps its extra depth.
        if not stack:
            return " " * indent + body
        extra = indent - stack[-1][0]
        return " " * (4 * len(stack) + (extra if extra > 3 else 0)) + body

    def unpipe(row):
        # GitHub drops the backslash of an escaped pipe in a table cell
        # even inside code. Python-Markdown needs no escape there.
        return CODE.sub(lambda code: code.group(0).replace("\\|", "|"), row)

    for index, line in enumerate(lines):
        body = line.lstrip(" ")
        indent = len(line) - len(body)
        if fence:
            closing, source, target = fence
            out.append(" " * (target + max(indent - source, 0)) + body if body.strip() else "")
            if closing.match(body):
                fence, last = None, "fence"
            continue
        if not body.strip():
            out.append("")
            separate()
            last = ""
            continue
        extra = indent - (stack[-1][0] if stack else 0)
        opening = FENCE.match(body)
        mark = None if RULE.match(body) else ITEM.match(body)
        heads_table = (
            "|" in body and last != "table" and index + 1 < len(lines)
            and "|" in lines[index + 1] and DIVIDER.match(lines[index + 1])
        )
        if mark and extra >= 0:
            # Not an item of an open list. It starts a new list unless it
            # is indented code or, by the CommonMark rule, it follows a
            # paragraph line and is empty or numbered other than 1.
            rest = body[mark.end():]
            if extra > 3 or last == "text" and (not rest or mark.group(2) not in (None, "1")):
                mark = None
            elif last in ("table", "fence") or last == "text" and not (stack and stack[-1][1]):
                separate()
        if mark:
            close_to(indent, item=True)
            out.append(" " * (4 * len(stack)) + body)
            gap = len(mark.group(3))
            width = mark.end() if 1 <= gap <= 4 and body[mark.end():] else len(mark.group(1)) + 1
            stack.append([indent + width, True])
            last = "text" if body[mark.end():] else "block"
        elif opening or heads_table or BLOCK.match(body) or RULE.match(body):
            close_to(indent)
            if stack and not opening or heads_table:
                separate()
            out.append(place(indent, unpipe(body) if heads_table else body))
            if opening:
                ticks = opening.group(1)
                closing = re.compile(re.escape(ticks[0]) + "{%d,} *$" % len(ticks))
                fence = (closing, indent, len(out[-1]) - len(body))
            last = "table" if heads_table else "text" if body.startswith(">") else "block"
        elif last in ("text", "table"):
            # The next line of a paragraph or a table. It stays in the
            # open list item however little it is indented.
            out.append(" " * max(indent, 4 * len(stack)) + (unpipe(body) if last == "table" else body))
        else:
            close_to(indent)
            out.append(place(indent, body))
            last = "text"
    return "\n".join(out)


def main():
    pages = published()
    assets = set()
    shutil.rmtree(SRC, ignore_errors=True)
    for source, inside in sorted(pages.items()):
        target = os.path.join(SRC, inside)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if source.endswith(".md"):
            with open(os.path.join(ROOT, source), encoding="utf-8") as handle:
                text = normalise(rewrite(handle.read(), source, pages, assets))
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text)
        else:
            shutil.copyfile(os.path.join(ROOT, source), target)
    for asset in sorted(assets):
        target = os.path.join(SRC, "_repo", asset)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, asset), target)
    print(f"{len(pages)} files and {len(assets)} linked images in {os.path.relpath(SRC, ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
