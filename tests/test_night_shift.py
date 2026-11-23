"""The night shift: fixed order, gated work, a fast stop, and a kept place."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from mind.memory import Memory
from mind.night_shift import ORDER, Gate, NightShift, VectorJob, standard_jobs

OPEN = Gate(night=True, on_ac=True, body_ok=True)


class Recorder:
    def __init__(self, name, log, chunks=1, seconds=0.0):
        self.name, self.log, self.left, self.seconds = name, log, chunks, seconds
        self.cursor = 0
        self.stopped = False

    def resume(self, cursor):
        self.cursor = cursor

    async def run_chunk(self, budget_s):
        self.log.append(self.name)
        if self.seconds:
            await asyncio.sleep(self.seconds)
        self.cursor += 1
        self.left -= 1
        return self.left <= 0

    def interrupted(self):
        self.stopped = True


class BlockingSleeper(Recorder):
    """A chunk that would run far past the budget if nothing stopped it."""

    async def run_chunk(self, budget_s):
        self.log.append(self.name)
        await asyncio.sleep(60)
        return True


class Order(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_run_in_the_fixed_order_whatever_the_registration_order(self):
        log = []
        shift = NightShift()
        for name in reversed(ORDER):
            shift.register(name, Recorder(name, log))
        results = await shift.step(OPEN, night="2026-11-23")
        self.assertEqual(log, list(ORDER))
        self.assertEqual(set(results.values()), {"done"})
        self.assertEqual(shift.done_tonight(), ORDER)

    async def test_unknown_jobs_and_jobs_without_chunks_are_refused(self):
        shift = NightShift()
        with self.assertRaises(ValueError):
            shift.register("mine_bitcoin", Recorder("x", []))
        with self.assertRaises(TypeError):
            shift.register("backup", object())

    async def test_a_job_done_tonight_is_not_run_again_until_the_next_night(self):
        log = []
        shift = NightShift()
        shift.register("search", Recorder("search", log, chunks=1))
        await shift.step(OPEN, night="2026-11-23")
        await shift.step(OPEN, night="2026-11-23")
        self.assertEqual(log, ["search"])
        shift.jobs["search"].left = 1
        await shift.step(OPEN, night="2026-11-24")
        self.assertEqual(log, ["search", "search"])


class Gating(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_checkpoint_runs_off_mains_or_without_bodycare(self):
        for gate in (Gate(night=True, on_ac=False, body_ok=True), Gate(night=True, on_ac=True, body_ok=False)):
            log = []
            shift = NightShift()
            for name in ("checkpoint", "search", "dream"):
                shift.register(name, Recorder(name, log))
            results = await shift.step(gate)
            self.assertEqual(log, ["checkpoint"])
            self.assertEqual(results, {"checkpoint": "done", "search": "waiting", "dream": "waiting"})

    async def test_nothing_runs_outside_the_night(self):
        log = []
        shift = NightShift()
        shift.register("checkpoint", Recorder("checkpoint", log))
        self.assertEqual(await shift.step(Gate(night=False, on_ac=True, body_ok=True)), {"checkpoint": "waiting"})
        self.assertEqual(await shift.step(False), {"checkpoint": "waiting"})
        self.assertEqual(log, [])

    async def test_the_gate_is_asked_again_before_every_chunk(self):
        log = []
        shift = NightShift()
        shift.register("vectors", Recorder("vectors", log, chunks=10))
        asked = []

        def gate():
            asked.append(1)
            return OPEN if len(asked) <= 3 else Gate(night=True)

        self.assertEqual(await shift.step(gate), {"vectors": "waiting"})
        self.assertEqual(len(log), 3)


class Interrupt(unittest.IsolatedAsyncioTestCase):
    async def test_a_long_chunk_is_stopped_within_two_seconds(self):
        log = []
        shift = NightShift(chunk_budget=1.0)
        dream = BlockingSleeper("dream", log)
        shift.register("dream", dream)
        shift.register("rewarm", Recorder("rewarm", log))
        task = asyncio.create_task(shift.step(OPEN))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        running = shift.interrupt()
        self.assertIs(running, task)
        results = await asyncio.wait_for(task, 2.0)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(results, {"dream": "interrupted"})
        self.assertTrue(dream.stopped, "a heavy job lets go of its server at once")
        self.assertEqual(log, ["dream"])
        self.assertIsNone(shift.interrupt())

    async def test_an_interrupt_between_chunks_stops_before_the_next(self):
        log = []
        shift = NightShift()
        job = Recorder("vectors", log, chunks=100, seconds=0.01)
        shift.register("vectors", job)
        task = asyncio.create_task(shift.step(OPEN))
        await asyncio.sleep(0.035)
        shift.interrupt()
        results = await task
        self.assertEqual(results["vectors"], "interrupted")
        self.assertLess(len(log), 10)
        self.assertTrue(job.stopped)

    async def test_a_job_paused_by_the_gate_lets_go_and_a_later_wake_finds_nothing_held(self):
        log = []
        shift = NightShift()
        dream = Recorder("dream", log, chunks=5)
        shift.register("dream", dream)
        asked = []

        def gate():
            asked.append(1)
            return OPEN if len(asked) <= 2 else Gate(night=True, on_ac=True, body_ok=False)

        self.assertEqual(await shift.step(gate), {"dream": "waiting"})
        self.assertTrue(dream.stopped, "too warm: the heavy job lets go before waiting")
        dream.stopped = False
        self.assertIsNone(shift.interrupt())
        self.assertFalse(dream.stopped, "nothing was held any more")

    async def test_a_wake_between_steps_still_reaches_a_job_that_holds_something(self):
        shift = NightShift()

        class Holder(Recorder):
            async def run_chunk(self, budget_s):
                self.log.append(self.name)
                return False  # more to do; between steps it keeps its server

        dream = Holder("dream", [])
        shift.register("dream", dream)
        shift._holding.add("dream")  # as after a chunk that was not the last
        self.assertIsNone(shift.interrupt())
        self.assertTrue(dream.stopped)


class Resume(unittest.IsolatedAsyncioTestCase):
    async def test_cursors_and_done_jobs_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = []
            shift = NightShift(tmp)
            shift.register("checkpoint", Recorder("checkpoint", log))
            vectors = Recorder("vectors", log, chunks=5, seconds=0.01)
            shift.register("vectors", vectors)
            task = asyncio.create_task(shift.step(OPEN, night="2026-11-23"))
            await asyncio.sleep(0.025)
            shift.interrupt()
            await task
            reached = vectors.cursor
            self.assertGreater(reached, 0)
            saved = json.loads((Path(tmp) / "night-shift.json").read_text())
            self.assertEqual(saved["done"], ["checkpoint"])
            self.assertEqual(saved["cursors"]["vectors"], reached)

            # The mind restarts: a new shift with new job objects.
            log2 = []
            again = NightShift(tmp)
            again.register("checkpoint", Recorder("checkpoint", log2))
            resumed = Recorder("vectors", log2, chunks=5 - reached)
            again.register("vectors", resumed)
            self.assertEqual(resumed.cursor, reached)
            await again.step(OPEN, night="2026-11-23")
            self.assertEqual(log2, ["vectors"] * (5 - reached))
            self.assertEqual(resumed.cursor, 5)

    async def test_a_damaged_state_file_starts_afresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "night-shift.json").write_text("{not json")
            shift = NightShift(tmp)
            self.assertEqual(shift.done_tonight(), ())

    async def test_a_failing_job_does_not_stop_the_rest(self):
        log = []

        class Broken:
            async def run_chunk(self, budget_s):
                raise OSError("disk")

        shift = NightShift()
        shift.register("backup", Broken())
        shift.register("rewarm", Recorder("rewarm", log))
        results = await shift.step(OPEN)
        self.assertEqual(results, {"backup": "failed", "rewarm": "done"})


class MemoryJobs(unittest.IsolatedAsyncioTestCase):
    async def test_memory_jobs_tidy_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp), embedder=None)
            for i in range(30):
                memory.log("heard", f"I watered the orchids in the garden, note {i}", "Richard")
            shift = standard_jobs(NightShift(tmp), memory, owner="Richard", night="2026-11-23")
            self.assertEqual(set(shift.jobs), {"checkpoint", "search", "vectors", "journal"})
            results = await shift.step(OPEN, night="2026-11-23")
            self.assertEqual(results["checkpoint"], "done")
            self.assertEqual(shift.jobs["checkpoint"].last_check, "ok")
            self.assertEqual(results["search"], "done")
            self.assertEqual(results["vectors"], "done")
            self.assertEqual(results["journal"], "done")
            self.assertGreater(shift.jobs["journal"].published, 0)
            self.assertEqual(len(memory.recall_history("orchids", limit=3)), 3)
            memory.close()

    async def test_the_day_record_and_the_dream_join_when_asked_for(self):
        from datetime import date

        memory = Memory(None, embedder=None)
        shift = standard_jobs(NightShift(), memory, owner="Richard", night="2026-11-23", cutoff=lambda: None,
                              today=date(2026, 11, 24), being="Pip")
        self.assertEqual([name for name in ORDER if name in shift.jobs],
                         ["checkpoint", "expiry", "search", "vectors", "journal", "diary", "dream"])
        memory.close()

    async def test_a_long_statement_never_holds_up_the_event_loop_and_an_interrupt_aborts_it(self):
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp), embedder=None)
            self.addCleanup(memory.close)
            ended, outcome = threading.Event(), []

            def slow_optimise(db=None):
                # Seconds of SQLite work: a VACUUM or quick_check of a big
                # archive on the old laptop's disk.
                self.assertIsNotNone(db, "the night's own connection, not memory's")
                self.assertIsNot(db, memory.db)
                try:
                    db.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
                               "WHERE x < 200000000) SELECT count(*) FROM c").fetchone()
                    outcome.append("finished")
                except Exception as exc:
                    outcome.append(type(exc).__name__)
                    raise
                finally:
                    ended.set()

            memory.optimize_search = slow_optimise
            shift = NightShift()
            shift.register("search", standard_jobs(NightShift(), memory, owner="R", night="n").jobs["search"])
            task = asyncio.create_task(shift.step(OPEN))
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await asyncio.sleep(0.2)
            self.assertLess(time.monotonic() - started, 0.5, "the loop kept running during the statement")
            shift.interrupt()
            results = await asyncio.wait_for(task, 2.0)
            self.assertEqual(results, {"search": "interrupted"})
            self.assertTrue(await asyncio.to_thread(ended.wait, 2.0), "the statement in flight was aborted")
            self.assertEqual(outcome, ["OperationalError"])

    async def test_compaction_needs_mains_and_never_runs_with_the_light_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp), embedder=None)
            self.addCleanup(memory.close)
            compacted = []
            memory.disk_state = lambda refresh=False: {"state": "low", "free_fraction": 0.08, "free_bytes": 1}
            memory.compact = lambda db=None: compacted.append(db is not None)
            shift = standard_jobs(NightShift(), memory, owner="Richard", night="2026-11-23")
            battery = Gate(night=True, on_ac=False, body_ok=False)
            results = await shift.step(battery, night="2026-11-23")
            self.assertEqual(results["checkpoint"], "done")
            self.assertEqual(shift.jobs["checkpoint"].last_check, "ok")
            self.assertEqual(results["search"], "waiting")
            self.assertEqual(compacted, [], "no vacuum on battery")
            results = await shift.step(OPEN, night="2026-11-23")
            self.assertEqual(results["search"], "done")
            self.assertEqual(compacted, [True], "on mains, on the night's own connection")

    async def test_the_vector_job_backfills_in_bounded_chunks(self):
        from mind import embed

        model = Path(__file__).resolve().parents[1] / "out" / "models" / "embeddings" / "blink-8M"
        if not (model / "model.safetensors").is_file():
            self.skipTest("blink-8M is not downloaded")
        memory = Memory(None, embedder=embed.load(model))
        self.addCleanup(memory.close)
        with memory.db:
            memory.db.executemany("INSERT INTO episodes (ts, kind, speaker, text) VALUES (?, 'heard', 'R', ?)",
                                  ((1.78e9 + i, f"sentence {i} about the garden") for i in range(1000)))
        job = VectorJob(memory, rows=100)
        chunks = 0
        while not await job.run_chunk(0.01):
            chunks += 1
        self.assertGreater(chunks, 0)
        self.assertEqual(memory.vector_counts(), (1000, 0))


if __name__ == "__main__":
    unittest.main()
