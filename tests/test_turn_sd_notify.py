"""READY and watchdog pings to systemd over its datagram socket (task L3.10)."""

import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path

from mind import sd_notify


class Notify(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "notify.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.server.bind(self.path)
        self.server.settimeout(2)
        self.env = {"NOTIFY_SOCKET": self.path}

    def tearDown(self):
        self.server.close()
        self.dir.cleanup()

    def received(self):
        return self.server.recv(4096).decode()

    def test_ready_and_watchdog_reach_the_socket(self):
        self.assertTrue(sd_notify.ready(environ=self.env))
        self.assertEqual(self.received(), "READY=1")
        self.assertTrue(sd_notify.watchdog(environ=self.env))
        self.assertEqual(self.received(), "WATCHDOG=1")

    def test_without_systemd_it_is_a_quiet_no_op(self):
        self.assertFalse(sd_notify.ready(environ={}))
        self.assertFalse(sd_notify.notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": "/nonexistent/sock"}))
        self.assertFalse(sd_notify.notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": "relative"}))

    def test_an_abstract_socket_is_understood(self):
        name = f"being-test-{os.getpid()}"
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            abstract.bind("\0" + name)
        except OSError:
            abstract.close()
            self.skipTest("abstract Unix sockets are Linux only")
        try:
            abstract.settimeout(2)
            self.assertTrue(sd_notify.watchdog(environ={"NOTIFY_SOCKET": "@" + name}))
            self.assertEqual(abstract.recv(64), b"WATCHDOG=1")
        finally:
            abstract.close()

    def test_the_interval_follows_the_units_watchdog(self):
        self.assertEqual(sd_notify.watchdog_interval(environ={}), 5.0)
        self.assertEqual(sd_notify.watchdog_interval(environ={"WATCHDOG_USEC": "30000000"}), 5.0)
        self.assertEqual(sd_notify.watchdog_interval(environ={"WATCHDOG_USEC": "4000000"}), 2.0)
        self.assertEqual(sd_notify.watchdog_interval(environ={"WATCHDOG_USEC": "junk"}), 5.0)

    def drain(self):
        self.server.settimeout(0)
        found = []
        try:
            while True:
                found.append(self.server.recv(64).decode())
        except BlockingIOError:
            pass
        return found

    async def test_keep_alive_says_ready_once_the_bus_is_up_then_pings_from_the_loop(self):
        up = asyncio.Event()
        task = asyncio.create_task(sd_notify.keep_alive(ready_when=up, interval=0.02, environ=self.env))
        await asyncio.sleep(0.07)
        before = self.drain()
        self.assertNotIn("READY=1", before, "not ready before the bus is up")
        # The loop is alive while it waits for the bus: a watchdog armed at
        # start must not kill it for a slow brainstem.
        self.assertGreaterEqual(before.count("WATCHDOG=1"), 2)
        up.set()
        await asyncio.sleep(0.005)
        self.assertEqual(self.drain()[:1], ["READY=1"], "READY the moment the bus is up")
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        messages = self.drain()
        self.assertNotIn("READY=1", messages, "READY is said once")
        self.assertGreaterEqual(messages.count("WATCHDOG=1"), 3)
        self.assertEqual(messages[-1], "STOPPING=1")


if __name__ == "__main__":
    unittest.main()
