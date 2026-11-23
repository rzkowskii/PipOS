"""Summarise bench-*.json files written by bench mode (mind/bench.py) on the laptop.

    python3 lab/bench_report.py /Volumes/PIPLOGS            # every bench file on the stick
    python3 lab/bench_report.py out/hardware-try/round1/boot-0004-*/bench-*.json

lab/try_timings.py prints the same summary inside each boot's report.
Standard library only.
"""

import argparse
import json
import sys
from pathlib import Path


def _num(value, digits=1, unit=""):
    return "-" if value is None else f"{value:.{digits}f}{unit}"


def _median(values):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _voice(result):
    """One line from either voice result: mind/voice_bench.py's (idle and busy
    per engine, decode slowdown, verdict) or the Flite and PocketSphinx one."""
    parts = []
    for key, value in sorted(result.items()):
        if not isinstance(value, dict) or key in ("busy", "verdict", "errors", "limits"):
            continue
        if "rtf" in value or "error" in value:
            parts.append(f"{key.upper() if len(key) <= 4 else key} RTF "
                         + (_num(value.get("rtf"), 2) if "rtf" in value else str(value.get("error", "-"))))
        elif any(isinstance(value.get(phase), dict) for phase in ("idle", "busy")):
            phases = []
            for phase in ("idle", "busy"):
                row = value.get(phase)
                if isinstance(row, dict):
                    number = row.get("rtf", row.get("ms_per_window"))
                    phases.append(f"{phase} {_num(number, 2)}")
            parts.append(f"{key} " + "/".join(phases))
    slowdown = (result.get("busy") or {}).get("decode_slowdown_pct")
    if slowdown is not None:
        parts.append(f"decode slowed {_num(slowdown, 1)}%")
    verdict = result.get("verdict") or {}
    if verdict:
        parts.append("verdict " + ", ".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in sorted(verdict.items())))
    errors = result.get("errors") or {}
    if errors:
        parts.append("not measured: " + ", ".join(sorted(errors)))
    return ", ".join(parts) or "-"


def summarise(data):
    """One dict per file: headline numbers plus printable lines."""
    if not isinstance(data, dict) or data.get("kind") != "pipos-bench-v1":
        return {"error": "not a PipOS bench file", "lines": ["not a PipOS bench file"]}
    system = data.get("system", {})
    cpu = system.get("cpu", {})
    power = system.get("power", {})
    lines = [f"CPU {cpu.get('model') or 'unknown'}, {cpu.get('threads')} threads"
             f"{', AVX2' if cpu.get('avx2') else ''}; {system.get('memory_mib')} MiB; "
             f"{power.get('scaling_driver') or '?'} / {power.get('governor') or '?'}, "
             f"no_turbo={power.get('no_turbo')}"]
    if not data.get("finished"):
        lines.append("UNFINISHED: the bench stopped early (power cut, crash or still running)")
    models = []
    for model in data.get("models", []):
        setting = model.get("setting", {})
        label = (f"{model.get('name')} t{setting.get('threads', '?')}/{setting.get('threads_batch', '?')}"
                 + (" mlock" if setting.get("mlock") else ""))
        if model.get("error"):
            lines.append(f"{label}: ERROR {model['error']}")
        prefill = {row.get("tokens"): row.get("prompt_tps") for row in model.get("prefill", [])}
        decode = (model.get("decode") or {}).get("predicted_tps")
        turns = model.get("turns", [])
        first = _median(turn.get("first_token_s") for turn in turns)
        warm = _median(turn.get("prompt_n") for turn in turns[1:])
        slot = model.get("slot") or {}
        summary = {"label": label, "load_s": model.get("load_s"), "prefill_tps": prefill,
                   "decode_tps": decode, "median_first_token_s": first, "median_warm_prompt_n": warm,
                   "cache_n_after_restore": slot.get("cache_n_after_restore"),
                   "cancel_to_release_s": model.get("cancel_to_release_s")}
        models.append(summary)
        lines.append(f"{label}: load {_num(model.get('load_s'), 1, ' s')}, "
                     "prefill " + ", ".join(f"{k}:{_num(v)}" for k, v in sorted(prefill.items(), key=lambda kv: kv[0] or 0))
                     + f" tok/s, decode {_num(decode)} tok/s")
        if turns:
            lines.append(f"  6-turn chat: median first token {_num(first, 2, ' s')}, "
                         f"median warm prompt {_num(warm, 0)} new tokens, cached per turn "
                         + ", ".join(str(turn.get("cache_n")) for turn in turns))
        if slot:
            lines.append("  slot: " + (f"ERROR {slot['error']}" if "error" in slot else
                                       f"save {_num(slot.get('save_s'), 2, ' s')}, restore "
                                       f"{_num(slot.get('restore_s'), 2, ' s')}, cache_n after restore "
                                       f"{slot.get('cache_n_after_restore')}"))
        isolation = model.get("slot_isolation") or {}
        if isolation:
            summary["slot0_kept_after_slot1_job"] = isolation.get("kept")
            lines.append("  slot 1 code job: " + (f"ERROR {isolation['error']}" if "error" in isolation else
                                                 f"slot 0 cache_n {isolation.get('cache_n_before')} -> "
                                                 f"{isolation.get('cache_n_after')} "
                                                 f"({'kept' if isolation.get('kept') else 'LOST'})"))
        sweep = model.get("sweep") or []
        if sweep:
            summary["sweep"] = []
            for row in sweep:
                setting_row = row.get("setting", {})
                prefill_row = (row.get("prefill") or [{}])[0]
                decode_row = (row.get("decode") or {}).get("predicted_tps")
                summary["sweep"].append({"threads": setting_row.get("threads"),
                                         "threads_batch": setting_row.get("threads_batch"),
                                         "prefill_tps": prefill_row.get("prompt_tps"), "decode_tps": decode_row})
                lines.append(f"  threads {setting_row.get('threads')}/{setting_row.get('threads_batch')}: "
                             + (f"ERROR {row['error']}" if row.get("error") else
                                f"prefill {_num(prefill_row.get('prompt_tps'))}, decode {_num(decode_row)} tok/s"))
        if "cancel_to_release_s" in model:
            lines.append(f"  cancel to release: {_num(model.get('cancel_to_release_s'), 2, ' s')}")
        speech = model.get("voice") or model.get("speech_with_model_busy") or {}
        if speech:
            lines.append(f"  voice ({speech.get('engine', 'flite+pocketsphinx')}, model busy): " + _voice(speech))
        for row in model.get("soak", []) or []:
            if "cap" in row:
                lines.append(f"  soak {row['cap']}: {_num(row.get('tokens_per_s'), 2)} tok/s, "
                             f"{_num(row.get('watts'), 1, ' W')}, {_num(row.get('tokens_per_joule'), 3)} tok/J, "
                             f"max {_num(row.get('max_package_c'), 0, ' C')}"
                             + (f" (stopped early: {row['stopped_early']})" if row.get("stopped_early") else ""))
            elif "skipped" in row:
                lines.append(f"  soak skipped: {row['skipped']}")
            elif "restore_errors" in row:
                lines.append(f"  soak RESTORE ERRORS: {row['restore_errors']}")
    dream = data.get("dream") or {}
    if dream:
        prefill_rows = {row.get("tokens"): row.get("prompt_tps") for row in dream.get("prefill", [])}
        lines.append(f"dream {dream.get('file')}: " + (f"ERROR {dream['error']}" if dream.get("error") else
                     "prefill " + ", ".join(f"{k}:{_num(v)}" for k, v in sorted(prefill_rows.items()))
                     + f" tok/s, decode {_num((dream.get('decode') or {}).get('predicted_tps'))} tok/s"))
    checked = data.get("bench_pack")
    if checked:
        lines.append("bench pack: " + (f"verified, {checked.get('files')} files" if checked.get("ok")
                                       else f"NOT VERIFIED: {checked.get('error')}"))
    for label, checked in (("conversation pack", data.get("fallback_pack")), ("dream pack", dream.get("pack"))):
        if checked and not checked.get("ok"):
            lines.append(f"{label}: NOT VERIFIED: {checked.get('error')}")
    idle = data.get("voice") or data.get("speech_idle") or {}
    if idle:
        lines.append(f"voice ({idle.get('engine', 'flite+pocketsphinx')}): " + _voice(idle))
    if data.get("restore_errors"):
        lines.append(f"POWER SETTINGS NOT RESTORED: {data['restore_errors']}")
    return {"finished": bool(data.get("finished")), "models": models, "lines": lines}


def bench_files(paths):
    for path in paths:
        path = Path(path)
        if path.is_dir():
            yield from sorted(path.rglob("bench-*.json"))
        elif path.is_file():
            yield path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="bench files, or folders to search")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = []
    for path in bench_files(args.paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            results.append({"file": str(path), "error": str(error), "lines": [f"unreadable: {error}"]})
            continue
        results.append({"file": str(path), **summarise(data)})
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for result in results:
            print(result["file"])
            for line in result["lines"]:
                print("  " + line)
    if not results:
        print("No bench-*.json files found.", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
