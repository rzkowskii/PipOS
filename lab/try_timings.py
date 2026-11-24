"""Read a PIPLOGS folder from a Try stick and print what the boot measured.

    python3 lab/try_timings.py /Volumes/PIPLOGS            # every boot folder
    python3 lab/try_timings.py /Volumes/PIPLOGS/boot-0003-20261124-091500Z
    python3 lab/try_timings.py /Volumes/PIPLOGS --json      # the same, as JSON

For each boot it prints the CPU, the GL renderer, failed units and restarts,
the brainstem's state changes with their journal times, the gaps that matter
(end of speech to first word), and per model request: prompt tokens, cached
tokens, and prompt and decode speed. The voice's and the mind's own timing
lines ("voice timing {...}", "turn timing {...}") give medians and the
laptop gates of docs/PIP-FUNCTIONS.md 4.2: end of speech to first audio
<= 3.0 s for a warm model turn and <= 1.2 s for a code answer. Bench results
(bench-*.json written by `being.bench=1`) are summarised too, and the
mind's route lines ("route {...}") are counted by route: which step answered
each turn (the workshop, a command, an everyday skill, a greeting, the model).

Python standard library only. It never writes to the stick. The journal can
hold what was said to the being: this script prints no transcript text.
"""

import argparse
import json
import re
import sys
from pathlib import Path

BOOT_DIR = re.compile(r"^boot-(\d+)-")
# journalctl --output=short-monotonic: "[   12.345678] host ident[pid]: message"
STAMP = re.compile(r"^\[\s*(\d+\.\d+)\]\s+(\S+)\s+([^:\[\s]+)(?:\[\d+\])?:\s?(.*)$")
STATE = re.compile(r"brainstem: ([a-z]+) -> ([a-z]+)\s*$")
DRAWN = re.compile(r"face: drawn by (.+)$")
TASK = re.compile(r"\btask (\d+)\b")
PROMPT = re.compile(r"prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")
DECODE = re.compile(r"(?<!prompt )\beval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")
RELEASE = re.compile(r"stop processing: n_tokens = (\d+)")
RESTORED = re.compile(r"restored (\d+) context checkpoint")
FAILED = re.compile(r"(\S+\.service): (?:Failed with result|Main process exited, code=(?:killed|dumped)|"
                    r"Start request repeated too quickly)")
RESTART = re.compile(r"(\S+\.service): Scheduled restart job, restart counter is at (\d+)")
# One line of numbers per reply (mind/voice.py) and per model turn (mind/turn.py).
TIMING = re.compile(r"\b(voice|turn) timing (\{.*\})\s*$")
VOICE_KEYS = ("eos_to_final_s", "final_to_audio_s", "eos_to_audio_s")
# One line per turn (mind/turn.py _route): the step that answered it. A
# voice-debug boot adds the words; they are never read here.
ROUTE = re.compile(r"\broute (\{.*\})\s*$")
ROUTE_NAME = re.compile(r"[a-z0-9_.-]{1,96}")
TURN_KEYS = ("prompt_n", "cache_n", "prompt_per_second", "predicted_n", "predicted_per_second", "first_clause_s")
MODEL_GATE_S = 3.0
CODE_GATE_S = 1.2
# A turn's timing line is written when its reply has been generated, so its
# voice line (the reply's first audio) comes up to PAIR_S before it, or a
# moment after it for a reply that finished generating before its first
# clause was heard. A later line is the next utterance, not this reply.
PAIR_S = 60.0
PAIR_AFTER_S = 5.0
SUMMARY_FIELDS = ("Machine", "Firmware", "CPU", "GPU", "GL renderer", "Displays", "Audio", "Audio codecs",
                  "Lid switch", "Battery", "Charger", "applesmc", "Camera", "Wi-Fi chip", "Memory",
                  "Failed units", "Codec IDs", "HDA model", "Cable link", "Voice debug")


def read_text(path):
    try:
        return path.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n")
    except OSError:
        return None


def parse_summary(text):
    """summary.txt: 'Label:          value' lines (mind/usb_logs.py summary())."""
    fields = {}
    for line in (text or "").splitlines():
        if line.startswith("---"):
            break
        label, colon, value = line.partition(":")
        if colon and label in SUMMARY_FIELDS and label not in fields:
            fields[label] = value.strip()
    return fields


def journal_entries(text):
    for line in (text or "").splitlines():
        match = STAMP.match(line)
        if match:
            yield float(match[1]), match[3], match[4]


def timing_fields(text):
    """The numbers in a timing line's JSON; words never pass."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {key: value for key, value in data.items()
            if isinstance(key, str) and (type(value) in (int, float, bool) or value is None)}


def route_fields(text):
    """A route line's route name and length; the words never pass."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("route"), str):
        return None
    if not ROUTE_NAME.fullmatch(data["route"]):
        return None
    chars = data.get("chars")
    return {"route": data["route"], "chars": chars if type(chars) is int and chars >= 0 else None}


def route_counts(routes):
    """{route: count}, most common first."""
    counts = {}
    for entry in routes:
        counts[entry["route"]] = counts.get(entry["route"], 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def parse_journal(text):
    states, requests, failed, restarts, renderer = [], [], [], {}, None
    routes = []
    timings = {"voice": [], "turn": []}
    open_tasks = {}
    checkpoints_restored = None
    for stamp, ident, message in journal_entries(text):
        if (match := STATE.search(message)):
            states.append({"t": stamp, "from": match[1], "to": match[2]})
        elif (match := DRAWN.search(message)) and renderer is None:
            renderer = {"t": stamp, "by": match[1].strip()}
        elif (match := FAILED.search(message)):
            failed.append({"t": stamp, "unit": match[1], "line": message.strip()[:160]})
        elif (match := RESTART.search(message)):
            restarts[match[1]] = max(restarts.get(match[1], 0), int(match[2]))
        if (match := TIMING.search(message)):
            fields = timing_fields(match[2])
            if fields is not None:
                timings[match[1]].append({"t": stamp, **fields})
        elif (match := ROUTE.search(message)):
            fields = route_fields(match[1])
            if fields is not None:
                routes.append({"t": stamp, **fields})
        if (match := RESTORED.search(message)):
            checkpoints_restored = int(match[1])
        if "print_timing" in message or "stop processing" in message:
            task = TASK.search(message)
            key = task[1] if task else "?"
            entry = open_tasks.setdefault(key, {"t": stamp, "task": key})
            if (match := PROMPT.search(message)):
                entry.update(prompt_ms=float(match[1]), prompt_tokens=int(match[2]), prompt_tps=float(match[3]))
            elif (match := DECODE.search(message)):
                entry.update(decode_ms=float(match[1]), decode_tokens=int(match[2]), decode_tps=float(match[3]))
            elif (match := RELEASE.search(message)):
                entry["context_tokens"] = int(match[1])
                if "prompt_tokens" in entry:
                    # n_tokens at release is cached + prompt + generated - 1: the last
                    # sampled token is never fed back (llama.cpp server logs: 224
                    # prompt + 10 decoded, cold, releases n_tokens = 233).
                    generated = max(entry.get("decode_tokens", 0) - 1, 0)
                    total = entry["context_tokens"] - generated
                    # Tokens of the prompt that came from the cache, not re-read.
                    entry["cached_tokens"] = max(total - entry["prompt_tokens"], 0)
                    requests.append(entry)
                open_tasks.pop(key, None)
    for entry in open_tasks.values():           # a request the log cut off
        if "prompt_tokens" in entry:
            requests.append(entry)
    requests.sort(key=lambda entry: entry["t"])
    return {"states": states, "requests": requests, "failed": failed, "restarts": restarts,
            "renderer": renderer, "checkpoints_restored": checkpoints_restored, "timings": timings,
            "routes": routes}


def classify(timings):
    """Split voice timing lines into model turns and code answers.

    Each turn timing line claims the nearest unclaimed voice line from
    PAIR_S seconds before it to PAIR_AFTER_S after it; a warm turn is one
    that found its prompt in the cache and did not fail. Voice lines no turn
    claims were code answers (no model call).
    """
    voice = list(timings.get("voice", []))
    claimed = {}
    for turn in timings.get("turn", []):
        best = None
        for index, line in enumerate(voice):
            offset = line["t"] - turn["t"]
            gap = abs(offset)
            if (index not in claimed and -PAIR_S <= offset <= PAIR_AFTER_S
                    and (best is None or gap < best[0])):
                best = (gap, index)
        if best is not None:
            claimed[best[1]] = turn
    model = [dict(line, turn=claimed[index]) for index, line in enumerate(voice) if index in claimed]
    warm = [line for line in model
            if (line["turn"].get("cache_n") or 0) > 0 and line["turn"].get("ok") is not False]
    code = [line for index, line in enumerate(voice) if index not in claimed]
    return {"model": model, "warm": warm, "code": code}


def timing_summary(timings):
    """Medians of every timing field, and the two laptop gates."""
    groups = classify(timings)
    summary = {"voice_lines": len(timings.get("voice", [])), "turn_lines": len(timings.get("turn", []))}
    summary["voice"] = {key: median(line.get(key) for line in timings.get("voice", [])) for key in VOICE_KEYS}
    summary["turn"] = {key: median(line.get(key) for line in timings.get("turn", [])) for key in TURN_KEYS}
    for name, lines, gate in (("warm_model", groups["warm"], MODEL_GATE_S), ("code", groups["code"], CODE_GATE_S)):
        value = median(line.get("eos_to_audio_s") for line in lines)
        summary[name] = {"count": len(lines), "median_eos_to_audio_s": value, "gate_s": gate,
                         "passes": None if value is None else value <= gate}
    return summary


def turns(states):
    """Per spoken turn: listening ended (end of speech) to speaking began (first word)."""
    found, heard = [], None
    for change in states:
        if change["from"] == "listening" and change["to"] == "thinking":
            heard = change["t"]
        elif change["to"] == "speaking" and heard is not None:
            found.append({"end_of_speech": heard, "first_word": change["t"],
                          "seconds": round(change["t"] - heard, 3)})
            heard = None
        elif change["to"] in ("idle", "attending", "asleep", "drowsy") and change["from"] == "thinking":
            if heard is not None:
                found.append({"end_of_speech": heard, "first_word": None, "seconds": None,
                              "ended_in": change["to"]})
            heard = None
    return found


def bench_summaries(folder):
    results = []
    for path in sorted(folder.glob("bench-*.json")):
        try:
            data = json.loads(read_text(path) or "")
        except ValueError:
            results.append({"file": path.name, "error": "not valid JSON (interrupted?)"})
            continue
        try:
            from lab import bench_report
        except ImportError:                       # run as a plain script from lab/
            import bench_report
        results.append({"file": path.name, **bench_report.summarise(data)})
    return results


def read_boot(folder):
    summary = parse_summary(read_text(folder / "summary.txt"))
    journal = parse_journal(read_text(folder / "journal.txt"))
    services = read_text(folder / "services.txt") or ""
    failed_services = sorted({unit for line in services.splitlines() if re.search(r"\bfailed\b", line)
                              for unit in re.findall(r"[\w@.-]+\.service", line)})
    cmdline = None
    for name in ("hardware-report.txt", "dmesg.txt"):
        text = read_text(folder / name) or ""
        match = re.search(r"Command line: (.+)", text)
        if match:
            cmdline = match[1].strip()
            break
    entry = "default"
    if cmdline:
        entry = ("boot messages" if "being.verbose=1" in cmdline else
                 "safe graphics" if "nomodeset" in cmdline else
                 "bench" if "being.bench=1" in cmdline else "default")
    return {
        "folder": folder.name,
        "entry": entry,
        "cable": bool(cmdline) and "being.cable=1" in cmdline.split() and "being.cable=0" not in cmdline.split(),
        "summary": summary,
        "renderer": journal["renderer"],
        "failed_units": journal["failed"],
        "failed_services": failed_services,
        "restarts": journal["restarts"],
        "states": journal["states"],
        "turns": turns(journal["states"]),
        "requests": journal["requests"],
        "checkpoints_restored": journal["checkpoints_restored"],
        "timing_lines": journal["timings"],
        "timing": timing_summary(journal["timings"]),
        "routes": journal["routes"],
        "route_counts": route_counts(journal["routes"]),
        "bench": bench_summaries(folder),
        "missing": [name for name in ("summary.txt", "journal.txt") if not (folder / name).is_file()],
    }


def boot_folders(path):
    path = Path(path)
    if BOOT_DIR.match(path.name):
        return [path]
    found = [child for child in path.iterdir() if child.is_dir() and BOOT_DIR.match(child.name)]
    return sorted(found, key=lambda child: int(BOOT_DIR.match(child.name)[1]))


def boots_txt(path):
    text = read_text(Path(path) / "boots.txt") if not BOOT_DIR.match(Path(path).name) else None
    if not text:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    clean = sum("ended cleanly" in line for line in lines)
    return {"lines": len(lines), "ended_cleanly": clean, "last": lines[-5:]}


def median(values):
    values = sorted(v for v in values if type(v) in (int, float))
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def report(path):
    return {"path": str(path), "boots_txt": boots_txt(path), "boots": [read_boot(f) for f in boot_folders(path)]}


def fmt(value, unit="", digits=1):
    return "-" if value is None else f"{value:.{digits}f}{unit}"


def render(data):
    out = [f"PIPLOGS: {data['path']}"]
    history = data["boots_txt"]
    if history:
        out.append(f"boots.txt: {history['lines']} lines, {history['ended_cleanly']} clean ends")
    if not data["boots"]:
        out.append("No boot-NNNN folders found. Is this the PIPLOGS volume?")
    for boot in data["boots"]:
        s = boot["summary"]
        cable = ", CABLE development boot" if boot.get("cable") else ""
        out += ["", f"== {boot['folder']} ({boot['entry']} entry{cable}) =="]
        if boot["missing"]:
            out.append(f"  missing: {', '.join(boot['missing'])} (a hard power-off or a crash?)")
        out.append(f"  CPU:          {s.get('CPU', 'unknown')}")
        out.append(f"  GPU:          {s.get('GPU', 'unknown')}")
        drawn = boot["renderer"]["by"] if boot["renderer"] else s.get("GL renderer", "not logged")
        soft = " (SOFTWARE rendering)" if re.search(r"llvmpipe|softpipe|swrast", drawn, re.I) else ""
        out.append(f"  Renderer:     {drawn}{soft}")
        if boot["renderer"]:
            out.append(f"  Face drawn:   {boot['renderer']['t']:.1f} s after the kernel started")
        for label in ("Audio", "Battery", "Lid switch", "Camera", "Memory"):
            if label in s:
                out.append(f"  {label + ':':<14}{s[label]}")
        failed = sorted({f["unit"] for f in boot["failed_units"]} | set(boot["failed_services"]))
        out.append(f"  Failed units: {', '.join(failed) if failed else s.get('Failed units', 'none')}")
        if boot["restarts"]:
            out.append("  Restarts:     " + ", ".join(f"{u} x{n}" for u, n in sorted(boot["restarts"].items())))
        if boot["states"]:
            out.append(f"  State changes ({len(boot['states'])}):")
            for change in boot["states"][:60]:
                out.append(f"    {change['t']:10.3f}  {change['from']} -> {change['to']}")
            if len(boot["states"]) > 60:
                out.append(f"    ... {len(boot['states']) - 60} more")
        if boot["turns"]:
            out.append("  Turns (end of speech -> first word):")
            for number, turn in enumerate(boot["turns"], 1):
                result = f"{turn['seconds']:.2f} s" if turn["seconds"] is not None else \
                    f"no speech (went {turn.get('ended_in')})"
                out.append(f"    {number:2}. at {turn['end_of_speech']:9.1f}  {result}")
            out.append(f"    median {fmt(median(t['seconds'] for t in boot['turns']), ' s', 2)}")
        timing = boot.get("timing") or {}
        if timing.get("voice_lines") or timing.get("turn_lines"):
            out.append(f"  Timing lines: {timing['voice_lines']} voice, {timing['turn_lines']} turn")
            voice = timing["voice"]
            out.append("    voice medians: end of speech to final words "
                       f"{fmt(voice['eos_to_final_s'], ' s', 2)}, final words to audio "
                       f"{fmt(voice['final_to_audio_s'], ' s', 2)}, end of speech to audio "
                       f"{fmt(voice['eos_to_audio_s'], ' s', 2)}")
            turn = timing["turn"]
            out.append(f"    turn medians: prompt {fmt(turn['prompt_n'], '', 0)} new, cached "
                       f"{fmt(turn['cache_n'], '', 0)}, prompt {fmt(turn['prompt_per_second'])} tok/s, "
                       f"decode {fmt(turn['predicted_per_second'])} tok/s, first clause "
                       f"{fmt(turn['first_clause_s'], ' s', 2)}")
            for name, label in (("warm_model", "warm model turns"), ("code", "code answers")):
                gate = timing[name]
                verdict = "no data" if gate["passes"] is None else ("PASS" if gate["passes"] else "FAIL")
                out.append(f"    {label}: {gate['count']}, median end of speech to audio "
                           f"{fmt(gate['median_eos_to_audio_s'], ' s', 2)} (gate <= {gate['gate_s']} s: {verdict})")
        if boot.get("route_counts"):
            out.append(f"  Routes ({len(boot['routes'])} turns): "
                       + ", ".join(f"{route} x{count}" for route, count in boot["route_counts"].items()))
        if boot["checkpoints_restored"] is not None:
            out.append(f"  Slot restore: {boot['checkpoints_restored']} context checkpoints restored")
        if boot["requests"]:
            out.append("  Model requests:")
            out.append("      at (s)  prompt  cached  prompt tok/s  decode  decode tok/s")
            for r in boot["requests"]:
                out.append(f"    {r['t']:8.1f}  {r['prompt_tokens']:6}  {r.get('cached_tokens', 0) or 0:6}  "
                           f"{fmt(r.get('prompt_tps')):>12}  {r.get('decode_tokens', 0):6}  "
                           f"{fmt(r.get('decode_tps')):>12}")
            out.append(f"    median prompt {fmt(median(r.get('prompt_tps') for r in boot['requests']))} tok/s, "
                       f"decode {fmt(median(r.get('decode_tps') for r in boot['requests']))} tok/s")
        else:
            out.append("  Model requests: none logged")
        for bench in boot["bench"]:
            out.append(f"  Bench {bench['file']}:")
            for line in bench.get("lines", [bench.get("error", "")]):
                out.append(f"    {line}")
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="the PIPLOGS volume, a copy of it, or one boot folder")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if not args.path.is_dir():
        parser.error(f"not a folder: {args.path}")
    data = report(args.path)
    sys.stdout.write(json.dumps(data, indent=1) + "\n" if args.json else render(data))
    return 0 if data["boots"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
