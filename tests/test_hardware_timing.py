import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mind.hardware_timing import TOPICS, Timings, capture


def voice_events(seq=1):
    return [
        {"t": "state.changed", "state": "listening", "muted": False},
        {"t": "hear.final", "text": "Private question", "speaker": "Private name", "lid_seq": seq},
        {"t": "state.changed", "state": "thinking", "muted": False},
        {"t": "say.request", "text": "Private answer", "seq": 1, "last": False, "lid_seq": seq},
        {"t": "say.started", "lid_seq": seq},
    ]


class TimingTests(unittest.TestCase):
    def test_voice_measures_separate_clause_and_playback_events(self):
        timing = Timings()
        timing.record({"t": "body.lid", "seq": 1, "closed": False, "snapshot": True}, 0)
        for elapsed, event in zip((0.1, 0.2, 0.201, 0.33, 0.55), voice_events()):
            timing.record(event, elapsed)
        timing.record({"t": "state.changed", "state": "speaking", "muted": False}, 0.551)
        timing.record({"t": "say.started", "lid_seq": 1}, 0.6)
        timing.record({"t": "hear.final", "text": "Unrelated", "lid_seq": 1}, 0.65)
        timing.record({"t": "say.done", "final": True, "lid_seq": 0}, 0.7)
        timing.record({"t": "say.done", "final": True, "lid_seq": 1}, 1.0)
        timing.record({"t": "state.changed", "state": "attending", "muted": False}, 1.01)
        self.assertEqual(timing.voice_turns, [{
            "final_at_ms": 200,
            "final_to_first_clause_ms": 130,
            "final_to_playback_launch_ms": 350,
            "clause_to_playback_launch_ms": 220,
        }])
        self.assertEqual(timing.voice_outcomes, {"completed": 1})
        self.assertEqual(timing.voice_attempts, [{
            "outcome": "completed", "final_at_ms": 200, "first_clause_at_ms": 330,
            "playback_launch_at_ms": 550, "end_at_ms": 1000,
        }])
        self.assertFalse(timing.lid_opens)

    def test_voice_outcomes_include_trials_that_never_reach_playback(self):
        cases = (
            ({"t": "mind.cancel"}, "canceled"),
            ({"t": "say.stop", "reason": "private reason"}, "canceled"),
            ({"t": "mind.cancel", "reason": "think_timeout"}, "think_timeout"),
            ({"t": "state.changed", "state": "thinking", "muted": True}, "muted"),
            ({"t": "state.changed", "state": "attending", "muted": False}, "state_changed"),
            ({"t": "body.lid", "seq": 2, "closed": True}, "lid_changed"),
            ({"t": "organ.down", "name": "voice"}, "organ_lost"),
            ({"t": "stem.announce_ready", "announcement": "Private reminder"}, "superseded"),
            ({"t": "mind.reply_done", "spoke": False, "lid_seq": 1}, "no_playback"),
        )
        for terminal, outcome in cases:
            with self.subTest(event=terminal):
                timing = Timings()
                timing.record({"t": "body.lid", "seq": 1, "closed": False}, 0)
                *events, playback = voice_events()
                for elapsed, event in enumerate(events, 1):
                    timing.record(event, elapsed)
                timing.record(terminal, 5)
                timing.record({"t": "mind.cancel"}, 5.1)
                timing.record(playback, 6)
                self.assertFalse(timing.voice_turns)
                self.assertEqual(timing.voice_outcomes, {outcome: 1})
                self.assertEqual(timing.voice_attempts, [{
                    "outcome": outcome, "final_at_ms": 2000, "first_clause_at_ms": 4000,
                    "playback_launch_at_ms": None, "end_at_ms": 5000,
                }])

    def test_voice_timeout_after_launch_keeps_the_measured_onset(self):
        timing = Timings()
        timing.record({"t": "body.lid", "seq": 1, "closed": False}, 0)
        for elapsed, event in enumerate(voice_events(), 1):
            timing.record(event, elapsed)
        timing.record({"t": "state.changed", "state": "speaking", "muted": False}, 5.1)
        timing.record({"t": "say.stop", "reason": "speak_timeout"}, 66)
        self.assertEqual(timing.voice_outcomes, {"speak_timeout": 1})
        self.assertEqual(timing.voice_attempts[0]["playback_launch_at_ms"], 5000)
        self.assertEqual(timing.voice_turns[0]["final_to_first_clause_ms"], 2000)

    def test_unaccepted_transcript_is_not_counted(self):
        timing = Timings()
        timing.record({"t": "body.lid", "seq": 1, "closed": False}, 0)
        for elapsed, event in enumerate(voice_events()[:2], 1):
            timing.record(event, elapsed)
        timing.finish_turn("window_ended", 3)
        self.assertFalse(timing.voice_attempts)
        self.assertFalse(timing.voice_outcomes)

    def test_lid_ignores_snapshot_old_sequence_and_close_before_present(self):
        timing = Timings()
        for elapsed, event in enumerate((
            {"t": "body.lid", "seq": 1, "closed": False, "snapshot": True},
            {"t": "panel.ready", "seq": 1},
            {"t": "body.lid", "seq": 2, "closed": True},
            {"t": "body.lid", "seq": 3, "closed": False},
            {"t": "panel.ready", "seq": 1},
            {"t": "body.lid", "seq": 4, "closed": True},
            {"t": "panel.ready", "seq": 3},
            {"t": "body.lid", "seq": 5, "closed": False},
            {"t": "panel.ready", "seq": 5},
            {"t": "panel.ready", "seq": 5},
        )):
            timing.record(event, elapsed)
        self.assertEqual(timing.lid_opens, [{"open_at_ms": 7000, "open_to_panel_ready_ms": 1000}])


class TimingSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_subscription_closes_and_exports_no_payload(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        path = str(Path(work) / "bus.sock")
        received = asyncio.get_running_loop().create_future()

        async def serve(reader, writer):
            try:
                hello = json.loads(await reader.readline())
                events = [{"t": "body.lid", "seq": 1, "closed": False, "snapshot": True}, *voice_events()]
                writer.writelines(json.dumps(event).encode() + b"\n" for event in events)
                await writer.drain()
                received.set_result((hello, await reader.read()))
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_unix_server(serve, path):
            result = await capture(0.15, path=path)
            hello, remaining = await asyncio.wait_for(received, 1)
        self.assertEqual(hello["t"], "hello")
        self.assertEqual(tuple(hello["sub"]), TOPICS)
        self.assertEqual(remaining, b"")
        self.assertEqual(len(result["voice_turns"]), 1)
        self.assertEqual(result["voice_attempt_count"], 1)
        self.assertEqual(result["voice_outcomes"], {"window_ended": 1})
        self.assertEqual(result["voice_attempts"][0]["end_at_ms"], 150)
        self.assertIsNotNone(result["voice_attempts"][0]["playback_launch_at_ms"])
        self.assertEqual(result["summary"]["final_to_playback_launch_ms"]["count"], 1)
        self.assertIsNone(result["summary"]["open_to_panel_ready_ms"]["p95_ms"])
        self.assertNotIn("Private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
