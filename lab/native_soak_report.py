"""Assess one installed MacBook endurance log without changing the appliance."""

import argparse
import json
import math
import re
import statistics
from datetime import date
from pathlib import Path


HOURS = (24, 48, 168)
UNITS = tuple(f"being-{name}.service" for name in ("brainstem", "face", "voice", "mind", "cortex"))
FACE = "being-face.service"
GAP_LIMIT = 45
CUT_WINDOW = 900
SIZE_LIMIT = 96 * 1024 * 1024
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
KINDS = {"start", "sample", "armed_power_cut", "power_cut_expired", "recovery",
         "panel_handoff", "organ_fault", "organ_recovered", "end"}


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def stats(values):
    clean = [value for value in values if numeric(value)]
    if not clean:
        return {"count": 0}
    window = min(10, len(clean))
    return {"count": len(clean), "min": min(clean), "max": max(clean),
            "median": statistics.median(clean), "first_window_median": statistics.median(clean[:window]),
            "last_window_median": statistics.median(clean[-window:])}


def service(sample, unit):
    values = sample.get("services")
    value = values.get(unit) if isinstance(values, dict) else None
    return value if isinstance(value, dict) else {}


def activation(state):
    value = state.get("active_since_boot_us")
    return value if type(value) is int and value > 0 else None


def healthy(state):
    return (state.get("active_state") == "active" and state.get("sub_state") == "running"
            and activation(state) is not None and type(state.get("restarts")) is int and state["restarts"] >= 0)


def fault_pairs(records, issues):
    pending = {}
    pairs = {}
    for index, row in enumerate(records):
        if row["kind"] not in ("organ_fault", "organ_recovered"):
            continue
        unit, boot, stamp = row.get("unit"), row["boot_id"], row.get("activation_us")
        key = (unit, boot)
        if unit not in UNITS or type(stamp) is not int or stamp <= 0:
            issues.add("invalid controlled organ record")
        elif row["kind"] == "organ_fault":
            if key in pending or unit in pairs:
                issues.add(f"duplicate controlled interruption for {unit}")
            else:
                pending[key] = (index, stamp, row["utc"])
        elif key not in pending:
            issues.add(f"unmatched controlled recovery for {unit}")
        else:
            first, previous, started = pending.pop(key)
            if stamp <= previous or not 0 <= row["utc"] - started <= 150:
                issues.add(f"invalid controlled recovery for {unit}")
            else:
                pairs[unit] = (boot, first, index, previous, stamp)
    if pending:
        issues.add("a deliberately stopped organ did not recover")
    return pairs


def boot_transitions(records, issues):
    boot = records[0]["boot_id"]
    previous_boots = {boot}
    armed_at = None
    recovered = 0
    for row in records[1:]:
        kind, target_boot = row["kind"], row["boot_id"]
        if kind == "recovery":
            before = row.get("booted_at_utc")
            if (armed_at is None or recovered or target_boot in previous_boots or row.get("previous_boot_id") != boot
                    or not numeric(before) or not armed_at <= before <= armed_at + CUT_WINDOW
                    or row.get("lost_power") is not True or row.get("archive_ok") is not True):
                issues.add("the marked power interruption lacks matching unclean boot and archive evidence")
            else:
                recovered += 1
            boot, armed_at = target_boot, None
            previous_boots.add(boot)
        elif target_boot != boot:
            issues.add("boot changed without a matching recovery")
        elif kind == "armed_power_cut":
            if armed_at is not None or recovered:
                issues.add("overlapping power interruptions")
            armed_at = row["utc"]
        elif kind == "power_cut_expired":
            if armed_at is None:
                issues.add("power-cut expiry has no armed checkpoint")
            armed_at = None
    return recovered


def pair_watts(before, after, elapsed):
    a, b = before.get("package_energy"), after.get("package_energy")
    if not isinstance(a, dict) or not isinstance(b, dict) or not 0 < elapsed <= GAP_LIMIT:
        return {}
    result = {}
    for name in set(a) & set(b):
        if not matches(name, r"package-[0-9]{1,3}") or not isinstance(a[name], dict) or not isinstance(b[name], dict):
            continue
        start, end = a[name].get("energy_uj"), b[name].get("energy_uj")
        rollover = a[name].get("range_uj")
        if (all(numeric(value) for value in (start, end, rollover))
                and rollover > max(start, end) and rollover == b[name].get("range_uj")):
            result[name] = ((end - start) % rollover) / (1_000_000 * elapsed)
    return result


def new_dream(before, after):
    if not all(row.get("lid_closed") is True and row.get("dream_read") is True for row in (before, after)):
        return None
    old, new = before.get("dream"), after.get("dream")
    if not isinstance(new, dict) or new == old or type(new.get("batches")) is not int or new["batches"] <= 0:
        return None
    night = new.get("night")
    if not matches(night, r"[0-9]{4}-[0-9]{2}-[0-9]{2}"):
        return None
    try:
        date.fromisoformat(night)
    except ValueError:
        return None
    if isinstance(old, dict) and old.get("night") == night and new["batches"] <= old.get("batches", 0):
        return None
    return night


def observations(records, pairs, issues):
    observed = face_seconds = 0.0
    gaps, wattages, dream_nights = [], {}, set()
    ac_cycles, ac = 0, None
    history, handoff, previous = {}, False, None
    for index, row in enumerate(records):
        if row["kind"] == "recovery":
            history, handoff, previous, ac = {}, False, None, None
        if row["kind"] == "panel_handoff":
            handoff = True
        if row["kind"] != "sample":
            continue
        now = row.get("monotonic")
        if not numeric(now):
            issues.add("a sample has no valid monotonic clock")
        for unit in UNITS:
            current = service(row, unit)
            active = activation(current)
            count = current.get("restarts")
            pair = pairs.get(unit)
            during_fault = bool(pair and pair[0] == row["boot_id"] and pair[1] < index <= pair[2])
            while_panel = unit == FACE and (row.get("local_screen") is True or handoff)
            if not healthy(current) and not during_fault and not while_panel:
                issues.add(f"{unit} unavailable")
            if unit in history and healthy(current):
                old_active, old_count, old_panel = history[unit]
                changed = (active, count) != (old_active, old_count)
                verified_fault = during_fault and (old_active, active) == (pair[3], pair[4]) and count == old_count + 1
                expected_panel = unit == FACE and (while_panel or old_panel) and count <= old_count
                if changed and not verified_fault and not expected_panel:
                    issues.add(f"{unit} had an unexpected activation or restart")
            if healthy(current):
                history[unit] = (active, count, while_panel)
            elif while_panel and unit in history:
                history[unit] = (*history[unit][:2], True)
        if row.get("mains_online") is True:
            if ac is False:
                ac_cycles += 1
            ac = True
        elif row.get("mains_online") is False and ac is True:
            ac = False
        if previous is not None and numeric(now) and numeric(previous.get("monotonic")):
            elapsed = now - previous["monotonic"]
            if row["boot_id"] != previous["boot_id"] or not 0 < elapsed <= GAP_LIMIT:
                issues.add("an observation gap exceeded the sampling window")
            else:
                observed += elapsed
                gaps.append(elapsed)
                if (healthy(service(row, FACE)) and healthy(service(previous, FACE)) and not handoff
                        and not row.get("local_screen") and not previous.get("local_screen")):
                    face_seconds += elapsed
                for name, watts in pair_watts(previous, row, elapsed).items():
                    wattages.setdefault(name, []).append(watts)
                if night := new_dream(previous, row):
                    dream_nights.add(night)
        previous, handoff = row, False
    return observed, face_seconds, gaps, wattages, dream_nights, ac_cycles


def assess(records):
    issues, missing = set(), []
    if (not records or any(not isinstance(row, dict) for row in records)
            or records[0].get("kind") != "start" or any(
                row.get("kind") not in KINDS or not numeric(row.get("utc"))
                or not matches(row.get("boot_id"), UUID) for row in records)):
        return {"verdict": "invalid", "issues": ["invalid evidence format"]}
    start = records[0]
    ends = [row for row in records if row["kind"] == "end"]
    if (start.get("version") != 1 or not matches(start.get("run"), r"[0-9a-f]{32}")
            or start.get("hours") not in HOURS or start.get("interval_s") != 30
            or start.get("hardware") != "MacBookPro9,2"
            or not matches(start.get("build_id"), r"[a-zA-Z0-9._-]{1,96}")
            or not matches(start.get("model_sha256"), r"[a-f0-9]{64}")):
        return {"verdict": "invalid", "issues": ["invalid run or build identity"]}
    if sum(row["kind"] == "start" for row in records) != 1 or len(ends) > 1 or (ends and ends[0] is not records[-1]):
        return {"verdict": "invalid", "issues": ["invalid start or end sequence"]}
    pairs = fault_pairs(records, issues)
    recovered = boot_transitions(records, issues)
    observed, face, gaps, watts, dreams, cycles = observations(records, pairs, issues)
    target = start["hours"] * 3600
    end = ends[0] if ends else {}
    if end.get("reason") == "duration" and (
        not numeric(end.get("observed_seconds")) or abs(end["observed_seconds"] - observed) > 1
    ):
        issues.add("elapsed coverage disagrees with the recorder")
    if end.get("reason") != "duration" or observed < target:
        missing.append("requested powered observation window")
    if not dreams:
        missing.append("a new dream commit with the lid closed")
    if cycles < 2:
        missing.append("two complete AC unplug and replug cycles")
    if not recovered:
        missing.append("a verified deliberate power cut and archive recovery")
    if start["hours"] == 48 and set(pairs) != set(UNITS):
        missing.append("deliberate recovery of every required organ")
    samples = [row for row in records if row["kind"] == "sample"]
    readings = {name: stats(row.get(name) for row in samples) for name in (
        "battery_percent", "battery_temp_c", "package_temp_c", "fan_rpm", "available_kib")}
    for name, metrics in readings.items():
        if metrics["count"] < 2:
            missing.append(f"{name} series")
    audio = stats(value for row in samples if isinstance(row.get("first_audio_s"), list) for value in row["first_audio_s"])
    if not audio["count"]:
        missing.append("end-of-speech to first-audio timing")
    if not watts:
        missing.append("RAPL package energy deltas")
    on_ac = [row for row in samples if row.get("mains_online") is True]
    cap = [row.get("cap_honoured") for row in on_ac if type(row.get("charge_limit")) is int]
    if False in cap:
        issues.add("the controller reported a charge cap violation on AC")
    if True not in cap:
        missing.append("measured charge cap holding on AC")
    return {"verdict": "issues" if issues else "incomplete" if missing else "measurements-ready",
            "issues": sorted(issues), "missing": missing,
            "identity": {key: start.get(key) for key in ("run", "hardware", "build_id", "kernel", "slot", "model_sha256")},
            "requested_seconds": target, "observed_seconds": observed, "face_observed_seconds": face,
            "sampling_gaps": stats(gaps), "ac_cycles": cycles, "dream_nights": sorted(dreams),
            "power_cut_recoveries": recovered, "controlled_organs": sorted(pairs),
            "readings": readings, "first_audio_s": audio,
            "package_watts": {name: stats(values) for name, values in sorted(watts.items())},
            "organ_memory_bytes": {unit: stats(service(row, unit).get("memory_bytes") for row in samples) for unit in UNITS},
            "review": "Compare sustained heat, battery, memory and paired latency with the native qualification targets."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.path.open("rb") as stream:
            raw = stream.read(SIZE_LIMIT + 1)
        if len(raw) > SIZE_LIMIT or (raw and not raw.endswith(b"\n")):
            raise ValueError("incomplete or oversized soak log")
        report = assess([json.loads(line) for line in raw.splitlines()])
    except (OSError, ValueError, RecursionError) as error:
        parser.exit(2, f"native soak report: {error}\n")
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
    return 0 if report["verdict"] == "measurements-ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
