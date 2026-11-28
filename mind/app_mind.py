"""RAM-only view of the same route decisions the caption chip receives."""

import re
from collections import deque

CATEGORIES = ("code", "model", "waiting", "unavailable")
RECENT = 10
_ID = re.compile(r"[a-z0-9_.-]{1,65}\Z")


class RouteHistory:
    def __init__(self):
        self.counts = dict.fromkeys(CATEGORIES, 0)
        self.recent = deque(maxlen=RECENT)

    def add(self, via, route):
        if via in self.counts and _ID.fullmatch(route):
            self.counts[via] += 1
            self.recent.append(route)

    def clear(self):
        for via in self.counts:
            self.counts[via] = 0
        self.recent.clear()

    def snapshot(self, enabled=True):
        return {"status": "ok", "enabled": bool(enabled),
                "counts": dict(self.counts) if enabled else {},
                "rows": list(reversed(self.recent)) if enabled else []}


def render(page):
    lines = [
        "  \x1b[1;36mRouting illustration\x1b[0m",
        "",
        "  Pip checks built-in actions first.",
        "",
        "  \x1b[96mCode\x1b[0m   A built-in action matches.",
        "  \x1b[95mModel\x1b[0m  Other requests, when available.",
        "",
        "",
        "  \x1b[1;36mThis Mind session\x1b[0m",
    ]
    if page["status"] != "ok":
        lines.append("  Routing details are unavailable right now.")
    elif not page["enabled"]:
        lines.append("  Routing history is paused with this memory setting.")
    else:
        counts = [f"\x1b[0;{tone};44m  {via.capitalize()}  {page['counts'][via]}  \x1b[0m"
                  for via, tone in zip(CATEGORIES, (96, 95, 93, 37))]
        lines.append("  " + "  ".join(counts))
        lines.append("  \x1b[90mRecent routes, newest first\x1b[0m")
        lines.extend(f"  \x1b[36m·\x1b[0m  {route}" for route in page["rows"])
        if not page["rows"]:
            lines.append("  No routes recorded yet.")
    lines.extend(("", "  \x1b[90mEsc  Close\x1b[0m"))
    return ("\x1b[?25l\x1b[2J\x1b[H" + "\r\n".join(lines)).encode("utf-8")
