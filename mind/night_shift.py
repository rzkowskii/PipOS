"""The night shift: Pip's chores while its lid is closed, in a fixed order.

Each job does its work in small chunks and says when it is done for the
night. Between chunks the shift asks again whether it may carry on, so a lid
opening, a spoken name or a key press stops it within two seconds:
`interrupt()` also cancels the chunk in flight, and every job commits a chunk
whole or not at all. What each job had finished, and where it had got to, is
written down, so the next quiet spell carries on instead of starting again.

Only the database checkpoint may run off mains power or without bodycare's
leave: it is quick and it protects the archive. Everything else needs both,
compaction (which may vacuum the whole file) included. Upkeep (the database
check, expiry, search, vectors, the day record, the backup, the model
re-check and the re-warm) runs on every eligible night; dreaming (the
journal and the dream model) also needs the owner's dream consent. The
memory jobs do their SQLite work on a connection of their own in a worker
thread, so a long statement never holds up the event loop and an interrupt
aborts it.

The order (PIP-FUNCTIONS 2.6): database checkpoint and check, gentle expiry,
search index upkeep, memory vectors, the rule-based journal, the day record,
a backup to a labelled stick, the dream model, the weekly model re-check, and
re-warming the conversation model for the morning.
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .storage import atomic_write

ORDER = ("checkpoint", "expiry", "search", "vectors", "journal", "diary",
         "backup", "dream", "rehash", "rewarm")
LIGHT = frozenset({"checkpoint"})
# Dreaming, which only runs with the owner's dream consent: the rule-based
# journal (whose entries only a consenting owner ever hears) and the model.
DREAMING = frozenset({"journal", "dream"})
CHUNK_BUDGET = 1.0  # seconds a chunk should take; the interrupt bound is 2 s
STATE_FILE = "night-shift.json"
STATE_VERSION = 1


def log(*parts):
    print("night:", *parts, flush=True)


@dataclass(frozen=True)
class Gate:
    """May the night shift work now?

    `night`: asleep with the lid closed, in the owner's dream window (the
    dream policy). `on_ac`: on mains power. `body_ok`: bodycare's leave for
    sustained load (the package is under the owner's temperature target).
    `dreams`: the owner's dream consent; without it the upkeep still runs
    and the DREAMING jobs wait.
    """

    night: bool = False
    on_ac: bool = False
    body_ok: bool = False
    dreams: bool = True

    def allows(self, name):
        if not self.night:
            return False
        if name in DREAMING and self.dreams is not True:
            return False
        return name in LIGHT or (self.on_ac and self.body_ok)


def as_gate(eligible):
    """Accept a Gate, a bool (True: everything allowed), or a callable
    returning either. A callable is asked again before every chunk."""
    value = eligible() if callable(eligible) else eligible
    if isinstance(value, Gate):
        return value
    allowed = value is True
    return Gate(allowed, allowed, allowed, allowed)


def upkeep_only(night=True):
    """The gate for an eligible night without dream consent."""
    return Gate(night is True, night is True, night is True, False)


class NightShift:
    def __init__(self, state_dir=None, *, chunk_budget=CHUNK_BUDGET, clock=time.monotonic):
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.chunk_budget = chunk_budget
        self.clock = clock
        self.jobs = {}
        self._state = self._load()
        self._interrupted = False
        self._chunk = None
        self._task = None
        # Jobs that have run a chunk and may still hold something heavy (the
        # dream server): each is told to let go when it pauses, fails or is
        # interrupted, not only when a chunk of it was in flight.
        self._holding = set()

    # -- jobs -------------------------------------------------------------

    def register(self, name, job):
        """Add a job under one of the fixed names. A job has
        `async run_chunk(budget_s) -> bool` (True: done for tonight), and may
        have a JSON-able `cursor` and a `resume(cursor)` to carry on later,
        and an `interrupted()` that lets go of anything heavy at once."""
        if name not in ORDER:
            raise ValueError(f"unknown night job {name!r}; the order is fixed: {', '.join(ORDER)}")
        if not callable(getattr(job, "run_chunk", None)):
            raise TypeError("a night job needs run_chunk(budget_s)")
        self.jobs[name] = job
        saved = self._state["cursors"].get(name)
        resume = getattr(job, "resume", None)
        if saved is not None and callable(resume):
            resume(saved)

    def done_tonight(self):
        return tuple(name for name in ORDER if name in self._state["done"])

    def settled(self, *, dreams=True):
        """True when nothing is left for tonight: every job is done, the
        DREAMING ones not counted when there is no dream consent."""
        done = set(self.done_tonight())
        return all(name in done or (name in DREAMING and dreams is not True) for name in self.jobs)

    # -- running ------------------------------------------------------------

    async def step(self, eligible, *, night=None):
        """Run the jobs in order while `eligible` allows. Returns
        {name: "done" | "more" | "waiting" | "interrupted" | "failed"} for
        every registered job that was not already done tonight."""
        if night is not None and night != self._state["night"]:
            self._state["night"] = night
            self._state["done"] = []
            self._save()
        self._interrupted = False
        self._task = asyncio.current_task()
        results = {}
        try:
            for name in ORDER:
                job = self.jobs.get(name)
                if job is None or name in self._state["done"]:
                    continue
                results[name] = await self._run(name, job, eligible)
                if results[name] == "interrupted":
                    break
        finally:
            self._task = None
            self._chunk = None
        return results

    async def _run(self, name, job, eligible):
        while True:
            if self._interrupted:
                self._release(name)
                return "interrupted"
            if not as_gate(eligible).allows(name):
                # Too warm, off mains, or morning: whatever it holds goes now.
                self._release(name)
                return "waiting"
            started = self.clock()
            self._holding.add(name)
            self._chunk = asyncio.ensure_future(job.run_chunk(self.chunk_budget))
            try:
                finished = await self._chunk
            except asyncio.CancelledError:
                if self._interrupted:
                    self._remember_cursor(name, job)
                    self._release(name)
                    return "interrupted"
                raise
            except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
                log(f"{name} failed:", type(exc).__name__)
                self._remember_cursor(name, job)
                self._release(name)
                return "failed"
            finally:
                self._chunk = None
            elapsed = self.clock() - started
            if elapsed > 2 * self.chunk_budget:
                log(f"{name} chunk took {elapsed:.2f} s")
            self._remember_cursor(name, job)
            if finished:
                self._holding.discard(name)
                self._state["done"].append(name)
                self._save()
                return "done"
            # Let wake, lid and key events through before the next chunk.
            await asyncio.sleep(0)

    def _release(self, name):
        if name not in self._holding:
            return
        self._holding.discard(name)
        stop = getattr(self.jobs.get(name), "interrupted", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:  # a stuck stop must not keep Pip asleep
                log(f"{name} did not stop cleanly:", type(exc).__name__)

    def interrupt(self):
        """Stop at once: the chunk in flight is cancelled (it commits nothing),
        no further chunk starts, and every job that may still hold something
        heavy lets go of it now, running or not. Returns the running step,
        to await, or None."""
        self._interrupted = True
        chunk = self._chunk
        if chunk is not None and not chunk.done():
            chunk.cancel()
        for name in tuple(self._holding):
            self._release(name)
        task = self._task
        return task if task is not None and not task.done() else None

    @property
    def running(self):
        return self._task is not None

    # -- state ----------------------------------------------------------------

    def _remember_cursor(self, name, job):
        if hasattr(job, "cursor"):
            try:
                cursor = job.cursor
                json.dumps(cursor)
            except (TypeError, ValueError):
                return
            if self._state["cursors"].get(name) != cursor:
                self._state["cursors"][name] = cursor
                self._save()

    def _load(self):
        empty = {"version": STATE_VERSION, "night": None, "done": [], "cursors": {}}
        if self.state_dir is None:
            return empty
        try:
            raw = (self.state_dir / STATE_FILE).read_bytes()
            if len(raw) > 65_536:
                return empty
            value = json.loads(raw)
        except (OSError, ValueError):
            return empty
        if (not isinstance(value, dict) or value.get("version") != STATE_VERSION
                or not isinstance(value.get("done"), list) or not isinstance(value.get("cursors"), dict)):
            return empty
        night = value.get("night")
        return {
            "version": STATE_VERSION,
            "night": night if isinstance(night, str) else None,
            "done": [name for name in value["done"] if name in ORDER],
            "cursors": {name: cursor for name, cursor in value["cursors"].items() if name in ORDER},
        }

    def _save(self):
        if self.state_dir is None:
            return
        try:
            atomic_write(self.state_dir / STATE_FILE, json.dumps(self._state, sort_keys=True) + "\n")
        except OSError as exc:
            log("could not save its place:", type(exc).__name__)


# -- jobs that live in memory's lane ---------------------------------------------

class ThreadJob:
    """Run a blocking `work(budget_s) -> bool` in a worker thread, off the
    event loop. `work` must commit each call whole (one transaction)."""

    def __init__(self, work):
        self.work = work

    async def run_chunk(self, budget_s):
        return await asyncio.to_thread(self.work, budget_s)


class SideWork:
    """Blocking database work on a connection of its own, in a worker
    thread, so the event loop (wake, lid, voice, keys) never waits for a
    checkpoint, an integrity check, a vacuum or a batch of vectors.

    `interrupt()` aborts the statement in flight: SQLite rolls its
    transaction back, so the chunk commits whole or not at all. An archive in
    RAM has no file to open a second connection on, and is small: its work
    runs in place.
    """

    BUSY_TIMEOUT = 0.5  # seconds a side statement waits for a lock

    def __init__(self, memory):
        self.memory = memory
        self._lock = threading.Lock()
        self._open = {}  # connection -> its stop flag

    async def run(self, work):
        """`work(db, stop) -> value`, where `stop` is a threading.Event set
        when the chunk is interrupted."""
        stop = threading.Event()
        root = getattr(self.memory, "root", None)
        if root is None:
            return work(self.memory.db, stop)
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_WORKER, self._in_thread, Path(root) / "mind.db", work, stop)
        except asyncio.CancelledError:
            stop.set()
            self.interrupt()
            raise

    def _in_thread(self, path, work, stop):
        check(stop)
        db = sqlite3.connect(path, timeout=self.BUSY_TIMEOUT)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA secure_delete = ON")
            with self._lock:
                self._open[db] = stop
            check(stop)
            return work(db, stop)
        finally:
            with self._lock:
                self._open.pop(db, None)
            db.close()

    def interrupt(self):
        with self._lock:
            for db, stop in self._open.items():
                stop.set()
                db.interrupt()

    interrupted = interrupt  # the shift's "let go now" hook


def check(stop):
    """Between statements, where SQLite's interrupt cannot reach."""
    if stop.is_set():
        raise sqlite3.OperationalError("interrupted")


def _lower_priority():
    """Night work yields the cores to the face, the voice and the turn. The
    night's own worker thread keeps the lower priority."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (AttributeError, OSError):
        pass


# One worker of the night's own: a second chunk waits for the first to let
# go, and the event loop's shared pool keeps its normal priority.
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="night-work", initializer=_lower_priority)


class CheckpointJob(SideWork):
    """Fold the WAL into the database and check it. Light enough for
    battery: compaction, which may vacuum the whole file, is search upkeep's,
    on mains only."""

    def __init__(self, memory):
        super().__init__(memory)
        self.last_check = None

    async def run_chunk(self, budget_s):
        def work(db, stop):
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            check(stop)
            row = db.execute("PRAGMA quick_check").fetchone()
            return row[0] if row else None

        self.last_check = await self.run(work)
        if self.last_check != "ok":
            log("database check did not pass")
        return True


class ExpiryJob:
    """Gentle expiry: one bounded step of the owner's retention policy per
    chunk. `cutoff()` returns the policy's cutoff, or None for no expiry."""

    def __init__(self, memory, cutoff, *, batch=200):
        self.memory, self.cutoff, self.batch = memory, cutoff, batch
        self.on_edit = None  # the mind's scrub of derived copies, if it wants one

    async def run_chunk(self, budget_s):
        from .retention import expiry_steps

        cutoff = self.cutoff()
        if cutoff is None:
            return True
        deadline = time.monotonic() + budget_s
        steps = expiry_steps(self.memory.db, cutoff, self.batch)
        for step in steps:
            change = self.memory.expire_before(step)
            if change and self.on_edit is not None:
                await self.on_edit(change)
            if time.monotonic() >= deadline and step != steps[-1]:
                return False
            await asyncio.sleep(0)
        return True


class SearchJob(SideWork):
    """Merge the search indexes into one segment each, and give space back
    (vacuuming when there is room) when the disk is low."""

    async def run_chunk(self, budget_s):
        memory = self.memory
        low = memory.disk_state(refresh=True)["state"] != "ok"

        def work(db, stop):
            if low:
                memory.compact(db)
            else:
                memory.optimize_search(db)

        await self.run(work)
        return True


class VectorJob(SideWork):
    """Embed every sentence and fact still without a vector, and remake
    vectors from an older model, a few hundred rows per chunk."""

    def __init__(self, memory, *, rows=400):
        super().__init__(memory)
        self.rows = rows

    async def run_chunk(self, budget_s):
        memory = self.memory
        if memory.embedder is None:
            return True
        deadline = time.monotonic() + budget_s

        def work(db, stop):
            while time.monotonic() < deadline and not stop.is_set():
                if not memory.backfill_vectors(self.rows, db=db):
                    return True
            return False

        return await self.run(work)


class JournalJob:
    """The rule-based dream: carry the day's notable owner sentences into the
    journal, one bounded, revision-checked batch per chunk."""

    def __init__(self, memory, owner, night):
        from .dream_store import DreamStore

        self.store = DreamStore(memory)
        self.owner, self.night = owner, night
        self.published = 0

    async def run_chunk(self, budget_s):
        night = self.night() if callable(self.night) else self.night
        try:
            batch = self.store.stage(self.owner, night=night)
            if batch is None:
                return True
            await asyncio.sleep(0)
            if self.store.publish(batch):
                self.published += len(batch.sources)
        except sqlite3.OperationalError:
            # The journal never waits for a lock (the conversation comes
            # first); the vector indexer or a turn is writing, so try again.
            await asyncio.sleep(0.05)
        return False


def standard_jobs(shift, memory, *, owner, night, cutoff=None, today=None, zone=None, state_dir=None,
                  being=None, dream_service=None):
    """Register memory's own jobs: checkpoint, expiry (with a `cutoff`),
    search, vectors, journal, the day record (with `today`) and the dream
    model (with `being`, Pip's own speaker name). Other lanes add backup,
    rehash and rewarm."""
    shift.register("checkpoint", CheckpointJob(memory))
    if cutoff is not None:
        shift.register("expiry", ExpiryJob(memory, cutoff))
    shift.register("search", SearchJob(memory))
    shift.register("vectors", VectorJob(memory))
    shift.register("journal", JournalJob(memory, owner, night))
    if today is not None:
        from .diary import DiaryJob

        shift.register("diary", DiaryJob(memory, owner, today=today, zone=zone, state_dir=state_dir))
    if being is not None:
        from .dream_model import DreamJob

        shift.register("dream", DreamJob(memory, owner, being, night, service=dream_service, zone=zone))
    return shift
