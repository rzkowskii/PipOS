"""An installed trial only confirms after read-only continuity checks."""

import asyncio
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mind import boot_health, continuity_baseline, diag_speech, diag_system
from mind.backup import DB_FILE, RESTORE_MARKER
from mind.memory import Memory
from mind.retention import MemoryPolicy, save_policy


class Slots:
    def __init__(self, tokens):
        self.tokens = tokens

    def request(self, method, path):
        assert (method, path) == ("GET", "/slots")
        return [{"id": slot, "n_ctx": self.tokens} for slot in (0, 1)]


class Continuity(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.host = diag_system.Host(root.name)
        self.mind = self.host.path("/mind")
        self.mind.mkdir()
        self.host.path("/etc/being").mkdir(parents=True)
        self.host.write("/etc/being/being.json", '{"ctx_tokens":4096}')
        self.host.write("/mind/life.json", '{"id":"0123456789abcdef0123456789abcdef","born":1}')
        slot = patch.object(diag_system, "cortex_client", return_value=Slots(4096))
        self.slots = slot.start()
        self.addCleanup(slot.stop)

    def test_persistent_archive_must_exist_and_open_in_the_current_format(self):
        self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.archive",))
        self.assertFalse((self.mind / DB_FILE).exists())
        memory = Memory(self.mind)
        self.addCleanup(memory.db.close)
        self.assertEqual(boot_health.continuity_failures(self.host), ())
        memory.db.execute("UPDATE meta SET value = '9000' WHERE key = 'schema'")
        memory.db.commit()
        self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.archive",))

    def test_session_only_allows_absent_archive_but_still_requires_life_and_slots(self):
        save_policy(self.mind, MemoryPolicy("session-only"))
        self.assertEqual(boot_health.continuity_failures(self.host), ())
        self.slots.return_value = Slots(512)
        self.host.write("/mind/life.json", "{")
        self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.life", "continuity.slots"))

    def test_baseline_rejects_another_life_and_unexplained_loss(self):
        with closing(Memory(self.mind, embedder=None)) as memory:
            fact = memory.remember("the telescope is named Ultraviolet", "Richard")
            self.assertIsNotNone(fact)
            self.assertTrue(continuity_baseline.write(
                self.mind, etc=self.host.path("/etc/being"), share=self.host.path("/usr/share/being"),
                models=self.host.path("/models"),
            ))
            self.assertEqual(boot_health.continuity_failures(self.host), ())

            original = (self.mind / "life.json").read_text()
            self.host.write("/mind/life.json", '{"id":"123456789abcdef0123456789abcdef0","born":1}')
            self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.baseline",))
            self.host.write("/mind/life.json", original)

            memory.forget([fact])
            self.assertEqual(boot_health.continuity_failures(self.host), ())
            memory.db.execute("DELETE FROM deletion_edits")
            memory.db.commit()
            self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.baseline",))

    def test_installed_trial_also_requires_the_ram_recall_and_route_fixtures(self):
        with closing(Memory(self.mind, embedder=None)):
            self.assertEqual(boot_health.continuity_failures(self.host, trial=True), ())
            with (patch.object(boot_health, "check_continuity_recall",
                               return_value={"id": "continuity.recall", "status": "fail"}) as recall,
                  patch.object(diag_speech, "check_command_routes",
                               return_value={"id": "commands.routes", "status": "fail"}) as routes):
                self.assertEqual(boot_health.continuity_failures(self.host), ())
                recall.assert_not_called()
                routes.assert_not_called()
                self.assertEqual(boot_health.continuity_failures(self.host, trial=True),
                                 ("continuity.recall", "commands.routes"))

    def test_interrupted_restore_and_dangling_path_do_not_count_as_an_absent_archive(self):
        save_policy(self.mind, MemoryPolicy("session-only"))
        (self.mind / RESTORE_MARKER).touch()
        self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.archive",))
        (self.mind / RESTORE_MARKER).unlink()
        (self.mind / DB_FILE).symlink_to("missing.db")
        self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.archive",))

    def test_session_only_does_not_excuse_a_corrupt_archive_or_active_exclusive_open(self):
        save_policy(self.mind, MemoryPolicy("session-only"))
        path = self.mind / DB_FILE
        path.write_bytes(b"invalid archive" * 32)
        self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.archive",))
        path.unlink()
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            self.assertEqual(boot_health.continuity_failures(self.host), ("continuity.archive",))


class HealthBus:
    def __init__(self, step=lambda _bus: None):
        self.clock = 1
        self.generation = 1
        self.connected = asyncio.Event()
        self.step = step

    async def start(self):
        self.connected.set()

    async def stop(self):
        self.connected.clear()
        self.on_disconnect()

    async def recv(self):
        await asyncio.sleep(0)
        self.clock += 1
        event = self.step(self)
        if event is None:
            event = self.ready() if self.clock == 2 else {"t": "stem.beat"}
        self.urgent(event)
        return event

    @staticmethod
    def ready():
        return {"t": "state.changed", "state": "idle", "unwell": False}


class OneMinute(unittest.IsolatedAsyncioTestCase):
    async def run_health(self, bus, verify, *, cortex=lambda: True, runner=None, deadline=120):
        async def inline(read):
            return read()

        with (patch.object(boot_health, "Bus", return_value=bus),
              patch.object(boot_health, "STABLE_SECONDS", 5),
              patch.object(boot_health, "cortex_ready", cortex),
              patch.object(boot_health, "time", monotonic=lambda: bus.clock),
              patch.object(boot_health.asyncio, "to_thread", runner or inline)):
            return await boot_health.healthy_for_a_minute(deadline, verify_continuity=verify)

    async def test_trial_retries_continuity_without_resetting_heartbeat_or_cortex_readiness(self):
        bus = HealthBus()
        outcomes = iter((("continuity.slots",), ()))
        with redirect_stdout(StringIO()) as captured:
            healthy = await self.run_health(bus, lambda: next(outcomes))
        self.assertTrue(healthy)
        self.assertIn("continuity.slots", captured.getvalue())
        self.assertLess(bus.clock, 20)

    async def test_regression_rejects_a_successful_read_from_an_older_healthy_span(self):
        for regression in ("organ", "cortex", "reconnect"):
            with self.subTest(regression=regression):
                read_started = []
                reached_regression = []
                released = asyncio.Event()
                verify = lambda: ()

                def step(bus):
                    if not read_started or len(read_started) != 1:
                        return None
                    age = bus.clock - read_started[0]
                    if age == 1 and regression == "organ":
                        reached_regression.append(bus.clock)
                        return {"t": "state.changed", "state": "idle", "unwell": True}
                    if age == 1 and regression == "reconnect":
                        reached_regression.append(bus.clock)
                        bus.on_disconnect()
                        bus.generation += 1
                        return bus.ready()
                    if age == 2 and regression == "organ":
                        return bus.ready()
                    if age == 4:
                        released.set()
                    return None

                bus = HealthBus(step)

                def cortex():
                    if (regression == "cortex" and read_started and not reached_regression
                            and bus.clock >= read_started[0] + 1):
                        reached_regression.append(bus.clock)
                        return False
                    return True

                async def worker(read):
                    if read is verify:
                        read_started.append(bus.clock)
                        if len(read_started) == 1:
                            await asyncio.wait_for(released.wait(), 2)
                    return read()

                self.assertTrue(await self.run_health(bus, verify, cortex=cortex, runner=worker))
                self.assertTrue(released.is_set())
                self.assertEqual(len(reached_regression), 1)
                self.assertEqual(len(read_started), 2)
                self.assertGreaterEqual(read_started[1], reached_regression[0] + 5)
                self.assertFalse(bus.connected.is_set())

    async def test_success_after_the_deadline_is_reaped_without_confirming(self):
        released, completed = asyncio.Event(), asyncio.Event()
        verify = lambda: ()

        def step(bus):
            if bus.clock == 10:
                released.set()

        async def worker(read):
            if read is verify:
                await asyncio.wait_for(released.wait(), 2)
                completed.set()
            return read()

        self.assertFalse(await self.run_health(HealthBus(step), verify, runner=worker, deadline=10))
        self.assertTrue(completed.is_set())


class InstalledSlot(unittest.TestCase):
    def test_bad_trial_reboots_but_confirmed_slot_remains_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            warning = root / "continuity-warning"
            cmdline = root / "cmdline"
            cmdline.write_text("rauc.slot=B being.installed=1\n")
            for confirmed in (False, True):
                with (self.subTest(confirmed=confirmed),
                      patch.object(boot_health, "CMDLINE", cmdline),
                      patch.object(boot_health, "READY", ready),
                      patch.object(boot_health, "SLOT_CONTINUITY_WARNING", warning),
                      patch.object(boot_health.subprocess, "check_output",
                                   return_value="GOOD=B\n" if confirmed else "GOOD=A\n"),
                      patch.object(boot_health, "continuity_failures", return_value=("continuity.slots",)),
                      patch.object(boot_health.subprocess, "run") as run,
                      redirect_stdout(StringIO())):
                    async def stable(deadline, *, verify_continuity):
                        if confirmed:
                            self.assertIsNone(verify_continuity)
                            return True
                        return not verify_continuity()
                    with patch.object(boot_health, "healthy_for_a_minute", stable):
                        boot_health.main()
                    self.assertFalse(ready.exists())
                    if confirmed:
                        run.assert_not_called()
                        self.assertEqual(warning.read_bytes(), b"B\n")
                    else:
                        self.assertFalse(warning.exists())
                        run.assert_called_once_with(["systemctl", "--no-block", "reboot"], check=True)
