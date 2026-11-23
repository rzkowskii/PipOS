"""PipOS lab suites (QEMU) and, in host/, fast host checks of the lab tools.

Gate mode, BEING_GATE=1 (scripts/check boot-gate and full set it): a skipped
test is a failure, so a missing image or a missing tool can never make a gate
look green. lab/vm.py also refuses images without a recorded source-id then.
"""

import os
import unittest

GATE = os.environ.get("BEING_GATE") == "1"


class SkippedInGate(AssertionError):
    pass


def _skip_is_failure(original):
    def add_skip(self, test, reason):
        try:
            raise SkippedInGate(f"skipped in gate mode (BEING_GATE=1): {reason}")
        except SkippedInGate:
            import sys
            self.addFailure(test, sys.exc_info())
    add_skip.__wrapped__ = original
    return add_skip


def install_gate():
    """Turn every skip into a failure for the rest of this process."""
    for result in (unittest.TestResult, unittest.TextTestResult):
        if "addSkip" in vars(result) and not hasattr(vars(result)["addSkip"], "__wrapped__"):
            result.addSkip = _skip_is_failure(vars(result)["addSkip"])


if GATE:
    install_gate()
