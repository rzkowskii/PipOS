"""pip-diag's checks, its output folder and its plain-English summary.

A check is a dict: {id, section, status, value, evidence, hint}. `status` is
pass, warn, fail or skip. `value` is a short JSON value, `evidence` a short
line of what was measured or read, and `hint` what to do about it (empty when
nothing needs doing). `guarded()` runs a check function so that it can never
raise: a bug in a check becomes a warn about the check itself.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

STATUSES = ("pass", "warn", "fail", "skip")
RANK = {"fail": 3, "warn": 2, "skip": 1, "pass": 0}
PIPLOGS_STATE = Path("/run/being-usb-logs/folder")
RUN_DIR = Path("/run/being-diag")
LAST_RUN = RUN_DIR / "last-run.json"
MAX_FILES = 400
# The Try stick's PIPLOGS partition is 126 MiB and keeps the last boots' logs too:
# one run must leave room for the next (a full speaker run writes about 20 MB).
MAX_BYTES = 40 * 1024 * 1024
# Leave this much free on the stick for the boot's own logs.
KEEP_FREE = 48 * 1024 * 1024
EVIDENCE_LIMIT = 600

# The verdict areas of diag.txt and the check ids (prefixes) that feed them.
AREAS = (
    ("microphone", ("mic.", "codec.adc_cfg", "codec.mic_pin", "voice.mic_speech")),
    ("speakers", ("speaker.", "loopback.")),
    ("voice clarity", ("tts.", "voice.listening")),
    ("wake word", ("voice.wake",)),
    ("command recognition", ("commands.", "voice.corpus", "hear.heard", "hear.route", "asr.")),
    ("terminal opening", ("hear.terminal", "workshop.")),
)
SUBJECTS = {
    "microphone": ("The microphone", False),
    "speakers": ("The speakers", True),
    "voice clarity": ("Pip's spoken voice", False),
    "wake word": ("The wake word", False),
    "command recognition": ("Spoken commands", True),
    "terminal opening": ("Opening the terminal by voice", False),
}
VERDICT_WORDS = {
    "pass": ("looks fine", "look fine"),
    "warn": ("needs a look", "need a look"),
    "fail": ("has a problem", "have a problem"),
    None: ("was not tested this time", "were not tested this time"),
}


class Interrupted(BaseException):
    """A signal (Ctrl-C, a dropped SSH session, systemctl stop) ended the run.

    A BaseException, so that no check's own `except Exception` swallows it and
    every `finally` on the way out (the voice, the mixer) still runs.
    """

    def __init__(self, signum=None):
        super().__init__(signum)
        self.signum = signum


def short(text, limit=EVIDENCE_LIMIT):
    text = "" if text is None else str(text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def check(id, section, status, value=None, evidence="", hint=""):
    """One result. An unknown status is a bug and reads as warn."""
    if status not in STATUSES:
        evidence, status = f"(bad status {status!r}) {evidence}", "warn"
    return {"id": str(id), "section": str(section), "status": status, "value": _plain(value),
            "evidence": short(evidence), "hint": short(hint, 300)}


def _plain(value):
    """Something json.dumps accepts, whatever a check produced."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return short(repr(value), 300)


def guarded(id, section, function, *args, **kwargs):
    """Run `function` and return a list of checks; it never raises (except Interrupted)."""
    try:
        result = function(*args, **kwargs)
    except Interrupted:
        raise
    except Exception as error:  # a broken check must not end the run
        return [check(id, section, "warn", None, f"the check itself failed: {type(error).__name__}: {error}",
                      "This is a fault in pip-diag, not in Pip. Report it with diag.json.")]
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    return [item for item in result if isinstance(item, dict)]


def worst(checks):
    found = [item["status"] for item in checks]
    return max(found, key=RANK.get) if found else None


def exit_code(checks):
    """0 when nothing failed, 1 when anything failed."""
    return 1 if any(item["status"] == "fail" for item in checks) else 0


def utc_stamp(now=None):
    return time.strftime("%Y%m%d-%H%M%SZ", time.gmtime(now if now is not None else time.time()))


def piplogs_folder(state=PIPLOGS_STATE):
    """This boot's PIPLOGS folder, when logging is on and the stick is there."""
    try:
        if (state.parent / "off").exists():
            return None
        folder = Path(state.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return folder if folder.is_absolute() and folder.is_dir() else None


def output_folder(explicit=None, *, now=None, state=PIPLOGS_STATE, fallback=RUN_DIR):
    """The run's folder: --out, else <PIPLOGS boot folder>/diag-<UTC>, else /run/being-diag/<UTC>."""
    stamp = utc_stamp(now)
    if explicit:
        folder = Path(explicit)
    else:
        base = piplogs_folder(state)
        folder = base / f"diag-{stamp}" if base is not None else Path(fallback) / stamp
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def free_bytes(path):
    try:
        info = os.statvfs(path)
    except OSError:
        return None
    return info.f_bavail * info.f_frsize


class Report:
    """The run's results, written to diag.json and diag.txt as they come in.

    With `stream_json`, each check is also printed as one JSON line; else a
    short human line. Printing never ends the run, except that a closed
    stdout (the SSH session went away) raises Interrupted once the files are
    saved, so a long run does not go on with nobody there.
    """

    def __init__(self, folder, meta, *, stream_json=False, out=None, keep_speech=False):
        self.folder = Path(folder)
        self.meta = dict(meta)
        self.stream_json = stream_json
        self.out = out if out is not None else sys.stdout
        self.keep_speech = keep_speech
        self.checks = []
        self.files = 0
        self.bytes = 0
        self.hung_up = False
        self.extra = {}
        self.overflow = None
        self.dropped = 0

    # Output -------------------------------------------------------------

    def say(self, text):
        """A line for the person running it (never in --json mode, which is machine-only)."""
        if not self.stream_json:
            self._print(text)

    def ask(self, question, default=False, reader=input):
        """A yes/no question on the terminal. No terminal, or Enter alone, is the default."""
        if self.stream_json:
            return default
        try:
            answer = reader(f"{question} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
        except (EOFError, OSError):
            return default
        if not answer:
            return default
        return answer in {"y", "yes"}

    def _print(self, text):
        if self.hung_up:
            return
        try:
            print(text, file=self.out, flush=True)
        except (BrokenPipeError, OSError):
            self.hung_up = True

    def add(self, items):
        for item in items:
            self.checks.append(item)
            if self.stream_json:
                self._print(json.dumps(item, separators=(",", ":")))
            else:
                mark = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}[item["status"]]
                line = f"{mark}  {item['id']}: {item['evidence']}"
                self._print(line)
                if item["hint"] and item["status"] in ("warn", "fail"):
                    self._print(f"      -> {item['hint']}")
        self.save()
        if self.hung_up:
            raise Interrupted(None)
        return items

    # Files --------------------------------------------------------------

    def may_write(self, size):
        """Whether `size` more bytes fit the run's caps and the stick's free space."""
        if self.files + 1 > MAX_FILES or self.bytes + size > MAX_BYTES:
            return False
        free = free_bytes(self.folder)
        return free is None or free - size > KEEP_FREE

    def path(self, name):
        """Where a new evidence file goes: the report folder while the caps allow,
        else a RAM folder that is removed when the run ends (the run goes on)."""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", name):
            raise ValueError(f"unsafe file name {name!r}")
        if self.may_write(256 * 1024):
            return self.folder / name
        if self.overflow is None:
            import tempfile

            self.overflow = Path(tempfile.mkdtemp(prefix="pip-diag-overflow-"))
        return self.overflow / name

    def note_file(self, path):
        path = Path(path)
        if self.overflow is not None and path.parent == self.overflow:
            self.dropped += 1
            self.meta["files_not_kept"] = self.not_kept()
            return
        try:
            self.bytes += path.stat().st_size
            self.files += 1
        except OSError:
            pass

    def write_text(self, name, text):
        data = (text or "").encode("utf-8", "replace")
        if not self.may_write(len(data)):
            return None
        target = self.path(name)
        try:
            target.write_bytes(data)
        except OSError:
            return None
        self.note_file(target)
        return target

    def save(self):
        data = {**self.meta, "checks": self.checks, "extra": self.extra,
                "counts": {status: sum(1 for item in self.checks if item["status"] == status) for status in STATUSES}}
        for name, text in (("diag.json", json.dumps(data, indent=1, default=str)), ("diag.txt", summary(data))):
            try:
                temporary = self.folder / f".{name}.tmp"
                temporary.write_text(text, encoding="utf-8")
                os.replace(temporary, self.folder / name)
            except OSError:
                pass

    def not_kept(self):
        return (f"{self.dropped} evidence files over the {MAX_FILES}-file or {MAX_BYTES // 2 ** 20} MB cap, "
                "or the stick was nearly full: their results are in this report, the files themselves are not")

    def finish(self):
        self.meta["finished_utc"] = utc_stamp()
        if self.overflow is not None:
            import shutil

            shutil.rmtree(self.overflow, ignore_errors=True)
            self.meta["files_not_kept"] = self.not_kept()
        self.save()
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            LAST_RUN.write_text(json.dumps({"folder": str(self.folder), "finished_utc": self.meta["finished_utc"]}),
                                encoding="utf-8")
        except OSError:
            pass
        try:
            os.sync()
        except (AttributeError, OSError):
            pass


# The summary ----------------------------------------------------------------

def area_checks(checks, prefixes):
    return [item for item in checks if item["id"].startswith(prefixes)]


def verdicts(checks):
    """[(area, status or None, sentence)] for the six areas the owner asked about."""
    result = []
    for area, prefixes in AREAS:
        found = [item for item in area_checks(checks, prefixes) if item["status"] != "skip"]
        status = worst(found) if found else None
        subject, plural = SUBJECTS[area]
        sentence = f"{subject} {VERDICT_WORDS[status][1 if plural else 0]}"
        if status in ("warn", "fail"):
            first = next(item for item in found if item["status"] == status)
            sentence += f": {first['evidence'].rstrip()}"
            if not sentence.endswith((".", "!", "?")):
                sentence += "."
            if first["hint"]:
                sentence += f" {first['hint']}"
        else:
            sentence += "."
        result.append((area, status, sentence))
    return result


def summary(data):
    """diag.txt: the top problems first, then a verdict per area, then every check."""
    checks = data.get("checks", [])
    lines = ["PipOS diagnostics (pip-diag)", ""]
    for key in ("command", "started_utc", "finished_utc", "boot_id", "build_id", "entry", "folder", "clock"):
        if data.get(key) not in (None, ""):
            lines.append(f"{key.replace('_', ' ')}: {data[key]}")
    counts = data.get("counts") or {}
    lines.append("result: " + ", ".join(f"{counts.get(status, 0)} {status}" for status in STATUSES))
    if data.get("files_not_kept"):
        lines.append(f"EVIDENCE NOT KEPT: {data['files_not_kept']}")
    lines.append("")
    problems = sorted((item for item in checks if item["status"] in ("fail", "warn")),
                      key=lambda item: -RANK[item["status"]])
    lines.append("Top problems" if problems else "No problems found.")
    for item in problems[:10]:
        lines.append(f"  {item['status'].upper()} {item['id']}: {item['evidence']}")
        if item["hint"]:
            lines.append(f"       {item['hint']}")
    lines.append("")
    lines.append("In plain English")
    for _area, _status, sentence in verdicts(checks):
        lines.append(f"  {sentence}")
    lines.append("")
    lines.append("Every check")
    for item in checks:
        value = item["value"]
        shown = "" if value in (None, "") else f" = {short(json.dumps(value), 120)}"
        lines.append(f"  [{item['status']}] {item['id']}{shown}")
    return "\n".join(lines) + "\n"


def latest_report(bases=None):
    """The newest diag.json under the PIPLOGS folder or /run/being-diag, or None."""
    candidates = []
    try:
        last = json.loads(LAST_RUN.read_text(encoding="utf-8"))
        folder = Path(last.get("folder", ""))
        if (folder / "diag.json").is_file():
            return folder / "diag.json"
    except (OSError, ValueError, AttributeError):
        pass
    for base in bases or [piplogs_folder(), RUN_DIR]:
        if base is None:
            continue
        try:
            candidates.extend(Path(base).glob("**/diag.json"))
        except OSError:
            continue
    candidates = [path for path in candidates if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
