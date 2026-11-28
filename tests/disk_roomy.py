"""Keep a module's tests independent of how full the machine's disk is.

Memory's disk guard (mind.memory.DISK_LOW) stops archiving conversation once
under 10% of the disk is free, which is correct on a real device but turns
every test that logs a "heard" line into a no-op on a nearly full dev
machine. The guard has its own tests (test_memory_guard) that fake statvfs.
Import `setUpModule` from here in a module that is not about the guard.
"""

import unittest
from unittest.mock import patch


def setUpModule():
    patcher = patch("mind.memory._disk_reading",
                    return_value={"state": "ok", "free_fraction": None, "free_bytes": None})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
