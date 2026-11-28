"""The Journal panel reads existing owner day records, dreams and activity."""

import heapq
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from . import action_log, diary, dream_store

SECTIONS = ("Day record", "Dream journal", "Activity", "Last night")
RECENT_DAYS = 30
RECENT_DREAMS = 12   # the same count as Care
RECENT_ACTIONS = 32
ACTIVITY = SECTIONS.index("Activity")


def _time_label(timestamp, zone):
    try:
        return datetime.fromtimestamp(timestamp, zone).strftime("%d %b %H:%M")
    except (TypeError, OverflowError, OSError, ValueError):
        return ""


def _activity(state_dir, zone):
    latest = heapq.nlargest(RECENT_ACTIONS,
                            (entry for entry in action_log.entries(state_dir) if entry["count"]),
                            key=lambda entry: entry["ts"])
    rows = []
    for entry in latest:
        when = _time_label(entry["ts"], zone)
        if when:
            one, many = action_log.KINDS[entry["kind"]]
            detail = one if entry["count"] == 1 else many.format(n=action_log.count_word(entry["count"]))
            rows.append(f"{when}  {detail}")
    return rows


def _days(db, owner):
    rows = []
    for (label,) in db.execute("SELECT day FROM diary_days WHERE owner=? ORDER BY day DESC LIMIT ?",
                                (owner, RECENT_DAYS)).fetchall():
        try:
            day = date.fromisoformat(label)
        except (TypeError, ValueError):
            continue
        record = diary.stored(db, owner, day)
        if record is not None:
            when = f"on {day:%d %b %Y}"
            rows.append(diary.spoken(record, when).removeprefix("[neutral] "))
    return rows


def _dreams(db, owner, zone):
    rows = []
    for entry in dream_store.journal(db, owner, limit=RECENT_DREAMS):
        when = _time_label(entry.source.ts, zone)
        if when:
            rows.append(f"{when}  {entry.source.text}")
    return rows


def read_section(state_dir, owner, zone, group):
    """Return a title and the bounded rows from one Journal section."""
    title = SECTIONS[group]
    if group == ACTIVITY:
        return title, _activity(state_dir, zone)
    if title == "Last night":
        from .night_shift import NightShift

        summary = NightShift(state_dir).summary()
        if summary is None:
            return title, []
        night, finished = summary
        from .vitals import (active_baseline, active_trend, latest, latest_smart,
                             package_power_baseline, package_power_trend)

        rows = [f"Night shift for {night}", "Finished stages", *finished]
        try:
            vital = latest(state_dir)
        except (OSError, sqlite3.Error, ValueError):
            vital = None
        if vital is not None and vital[0] == night:
            offset = vital[1].get("rtc_offset_seconds")
            if type(offset) is int and abs(offset) <= 10_000_000_000:
                rows.append(f"Hardware clock offset from system time: {offset:+,d} s")
            written = vital[1].get("archive_drive_daily_write_bytes")
            if type(written) is int and written >= 0:
                rows.append(f"Archive drive wrote {written / (1 << 30):.2f} GiB since the previous night's sample.")
            watts = vital[1].get("package_watts")
            seconds = vital[1].get("seconds")
            if (type(watts) in (int, float) and 0 <= watts <= 200
                    and type(seconds) in (int, float) and 29 <= seconds <= 60):
                rows.append(f"Measured {watts:.2f} W package power over {seconds:.1f} s")
                try:
                    baseline = package_power_baseline(state_dir, night)
                    change = package_power_trend(state_dir, night)
                except (OSError, sqlite3.Error, ValueError):
                    baseline = change = None
                if baseline is not None:
                    rows.append(f"Build baseline: {baseline:.2f} W median over the first seven measured nights.")
                if change is not None:
                    direction = "higher" if change > 0 else "lower"
                    rows.append(f"Median package power over seven nights was {abs(change):.2f} W "
                                f"{direction} than the prior 28 nights.")
            active = vital[1].get("active")
            if isinstance(active, dict):
                read, write, disk = active.get("read_tps"), active.get("write_tps"), active.get("disk_sync_ms")
                if all(type(value) in (int, float) and value >= 0 for value in (read, write, disk)):
                    rows.append(f"Active workload: read {read:.2f} tok/s, write {write:.2f} tok/s, "
                                f"synced disk write {disk:.2f} ms")
                energy, peak = active.get("joules_per_token"), active.get("peak_c")
                if type(energy) in (int, float) and energy >= 0:
                    rows.append(f"Active model energy: {energy:.3f} joules per token")
                if type(peak) in (int, float) and -50 <= peak <= 150:
                    rows.append(f"Active workload peak: {peak:.1f} C")
                try:
                    baseline, trend = active_baseline(state_dir, night), active_trend(state_dir, night)
                except (OSError, sqlite3.Error, ValueError):
                    baseline = trend = None
                if baseline is not None:
                    rows.append(f"Active build baseline: read {baseline['read_tps']:.2f} tok/s, "
                                f"write {baseline['write_tps']:.2f} tok/s")
                if trend:
                    rows.append("Active workload has a sustained change from the prior 28 nights.")
        try:
            smart = latest_smart(state_dir)
        except (OSError, sqlite3.Error, ValueError):
            smart = None
        if smart is not None:
            sampled, values, delta = smart
            status = values.get("passed")
            rows.append(f"Archive disk SMART sample for {sampled}: "
                        + ("passed" if status is True else "reported a problem" if status is False else "status unavailable"))
            changes = delta.get("attributes", {}) if isinstance(delta, dict) else {}
            days = delta.get("days") if isinstance(delta, dict) else None
            attribute_map = values.get("attributes", {})
            attribute_map = attribute_map if isinstance(attribute_map, dict) else {}
            attribute_keys = sorted(
                (key for key in attribute_map
                 if isinstance(key, str) and key.isascii() and key.isdigit()), key=int)
            for key in attribute_keys:
                current = attribute_map.get(key)
                if (not isinstance(current, dict) or type(current.get("raw")) is not int
                        or type(current.get("normalized")) is not int):
                    continue
                line = f"SMART attribute {key}: raw {current['raw']}, normalized {current['normalized']}"
                change = changes.get(key) if isinstance(changes, dict) else None
                if isinstance(change, dict):
                    parts = []
                    if type(change.get("raw")) is int:
                        parts.append(f"raw +{change['raw']}")
                    if type(change.get("normalized")) is int:
                        parts.append(f"normalized {change['normalized']:+d}")
                    if parts and type(days) is int:
                        line += f"; {'; '.join(parts)} over {days} days"
                rows.append(line)
        return title, rows
    path = Path(state_dir) / "mind.db"
    if not path.is_file() or not owner:
        return title, []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)) as db:
        db.execute("BEGIN")
        if title == "Dream journal":
            return title, _dreams(db, owner, zone)
        if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='diary_days'").fetchone() is None:
            return title, []
        return title, _days(db, owner)
