"""Measure software timing on the appliance without retaining bus payloads."""

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
import json
import math
import os
import statistics
import sys


SOCKET = "/run/being/bus.sock"
TOPICS = (
    "body.lid", "panel.ready", "state.changed", "hear.final", "say.request",
    "say.started", "say.done", "say.stop", "mind.cancel", "mind.reply_done",
    "stem.announce_ready", "organ.down",
)
MAX_SECONDS = 300
MAX_SAMPLES = 300


def milliseconds(seconds):
    return round(seconds * 1000, 3)


def distribution(events, field):
    values = sorted(event[field] for event in events)
    return {
        "count": len(values),
        "median_ms": round(statistics.median(values), 3) if values else None,
        "p95_ms": values[math.ceil(0.95 * len(values)) - 1] if values else None,
    }


@dataclass
class VoiceAttempt:
    final_at: float
    accepted: bool = False
    clause_at: float | None = None
    playback_at: float | None = None


class Timings:
    def __init__(self):
        self.lid_seq = None
        self.lid_open = False
        self.panel_since = None
        self.state = None
        self.muted = True
        self.attempt = None
        self.voice_turns = []
        self.voice_attempts = []
        self.voice_outcomes = Counter()
        self.lid_opens = []

    def finish_turn(self, outcome, elapsed):
        attempt, self.attempt = self.attempt, None
        if attempt is None or not attempt.accepted:
            return
        self.voice_outcomes[outcome] += 1
        if len(self.voice_attempts) < MAX_SAMPLES:
            self.voice_attempts.append({
                "outcome": outcome,
                "final_at_ms": milliseconds(attempt.final_at),
                "end_at_ms": milliseconds(elapsed),
                "first_clause_at_ms": (milliseconds(attempt.clause_at) if attempt.clause_at is not None else None),
                "playback_launch_at_ms": (milliseconds(attempt.playback_at) if attempt.playback_at is not None else None),
            })

    def voice_state(self, event, elapsed):
        self.state = event.get("state")
        self.muted = event.get("muted") is not False
        if self.attempt is None:
            return
        if self.muted:
            self.finish_turn("muted", elapsed)
        elif self.state == "thinking":
            self.attempt.accepted = True
        elif self.state != "speaking":
            self.finish_turn("state_changed", elapsed)

    def voice_event(self, topic, event, elapsed):
        seq = event.get("lid_seq")
        if not self.lid_open or type(seq) is not int or seq != self.lid_seq:
            return
        if topic == "hear.final":
            text = event.get("text")
            if (self.muted or self.state not in ("attending", "listening")
                    or not isinstance(text, str) or not text.strip()):
                return
            self.attempt = VoiceAttempt(elapsed)
            return
        attempt = self.attempt
        if attempt is None or not attempt.accepted:
            return
        if topic == "say.request" and event.get("announcement"):
            self.finish_turn("superseded", elapsed)
        elif topic == "say.request" and self.state == "thinking" and type(event.get("seq")) is int and event["seq"] == 1:
            text = event.get("text")
            if attempt.clause_at is None and event.get("last") is False and isinstance(text, str) and text.strip():
                attempt.clause_at = elapsed
        elif topic == "say.started" and self.state == "thinking" and attempt.playback_at is None:
            attempt.playback_at = elapsed
            if attempt.clause_at is not None and len(self.voice_turns) < MAX_SAMPLES:
                self.voice_turns.append({
                    "final_at_ms": milliseconds(attempt.final_at),
                    "final_to_first_clause_ms": milliseconds(attempt.clause_at - attempt.final_at),
                    "final_to_playback_launch_ms": milliseconds(elapsed - attempt.final_at),
                    "clause_to_playback_launch_ms": milliseconds(elapsed - attempt.clause_at),
                })
        elif topic == "say.done" and event.get("final") is True:
            self.finish_turn("completed" if attempt.playback_at is not None else "no_playback", elapsed)
        elif topic == "mind.reply_done" and event.get("spoke") is False and attempt.playback_at is None:
            self.finish_turn("no_playback", elapsed)

    def record(self, event, elapsed):
        if not isinstance(event, dict):
            return
        topic = event.get("t")
        if topic == "body.lid":
            seq = event.get("seq")
            if type(seq) is int and seq >= 0 and seq != self.lid_seq:
                self.finish_turn("lid_changed", elapsed)
                self.lid_seq = seq
                self.lid_open = event.get("closed") is False
                self.panel_since = elapsed if self.lid_open and event.get("snapshot") is not True else None
            return
        if topic == "panel.ready":
            seq = event.get("seq")
            if self.panel_since is not None and type(seq) is int and seq == self.lid_seq:
                if len(self.lid_opens) < MAX_SAMPLES:
                    self.lid_opens.append({
                        "open_at_ms": milliseconds(self.panel_since),
                        "open_to_panel_ready_ms": milliseconds(elapsed - self.panel_since),
                    })
                self.panel_since = None
            return
        if topic == "state.changed":
            self.voice_state(event, elapsed)
        elif topic in ("say.stop", "mind.cancel"):
            reason = event.get("reason")
            outcome = reason if reason in ("think_timeout", "speak_timeout") else "canceled"
            self.finish_turn(outcome, elapsed)
        elif topic == "stem.announce_ready":
            self.finish_turn("superseded", elapsed)
        elif topic == "organ.down":
            organ = event.get("name")
            if organ == "face":
                self.panel_since = None
            if organ in ("voice", "mind"):
                self.finish_turn("organ_lost", elapsed)
        elif topic in ("hear.final", "say.request", "say.started", "say.done", "mind.reply_done"):
            self.voice_event(topic, event, elapsed)


async def capture(seconds, *, path=SOCKET, started_at=None):
    loop = asyncio.get_running_loop()
    start = loop.time() if started_at is None else started_at
    timing = Timings()
    writer = None
    subscribed = False
    try:
        async with asyncio.timeout_at(start + seconds):
            reader, writer = await asyncio.open_unix_connection(path)
            subscription = {"t": "hello", "name": f"hardware-timing-{os.getpid()}", "sub": TOPICS}
            writer.write(json.dumps(subscription).encode() + b"\n")
            await writer.drain()
            subscribed = True
            while True:
                line = await reader.readline()
                elapsed = loop.time() - start
                if not line:
                    raise RuntimeError("The brainstem disconnected during the timing window.")
                if elapsed >= seconds:
                    break
                try:
                    timing.record(json.loads(line), elapsed)
                except (ValueError, RecursionError):
                    pass
                # Only numeric timing and state are retained between messages.
                del line
    except TimeoutError:
        if not subscribed:
            raise RuntimeError("Could not subscribe to the brainstem before the deadline.") from None
    finally:
        if writer is not None:
            writer.close()
    timing.finish_turn("window_ended", seconds)
    return {
        "schema_version": 1,
        "window_seconds": seconds,
        "voice_turns": timing.voice_turns,
        "voice_attempts": timing.voice_attempts,
        "voice_attempt_count": sum(timing.voice_outcomes.values()),
        "voice_outcomes": dict(timing.voice_outcomes),
        "lid_opens": timing.lid_opens,
        "summary": {
            field: distribution(events, field)
            for events, fields in (
                (timing.voice_turns, ("final_to_first_clause_ms", "final_to_playback_launch_ms",
                                      "clause_to_playback_launch_ms")),
                (timing.lid_opens, ("open_to_panel_ready_ms",)),
            )
            for field in fields
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Measure local voice and display event timing.")
    parser.add_argument("--seconds", type=int, default=60, help=f"timing window, 1–{MAX_SECONDS} seconds")
    args = parser.parse_args()
    if not 1 <= args.seconds <= MAX_SECONDS:
        parser.error(f"--seconds must be between 1 and {MAX_SECONDS}")
    if os.geteuid() != 0:
        parser.error("Run from a local maintenance session as root.")
    try:
        print(json.dumps(asyncio.run(capture(args.seconds))))
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(f"hardware-timing: {error}")


if __name__ == "__main__":
    main()
