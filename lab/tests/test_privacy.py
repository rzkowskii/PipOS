"""Acceptance checks for forgotten data and guest isolation in the packaged VM."""

import json
import time
import unittest
from pathlib import Path

from lab.tests.test_l2 import CONTEXT_ENV, REPLY, sleep, wake
from lab.vm import IMAGE, VM

PROBE_SOURCE = Path(__file__).with_name("privacy_probe.py")
BACKUP_SOURCE = PROBE_SOURCE.parents[1] / "backup_privacy_roundtrip.py"
PACKAGE_PYTHON = "PYTHONPATH=/usr/lib/being python3"


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class Privacy(unittest.TestCase):
    def setUp(self):
        self.vm = VM()
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        wake(self.vm)
        self.workspace = self.sh("mktemp -d /run/being-privacy-XXXXXX")
        self.assertRegex(self.workspace, r"^/run/being-privacy-[A-Za-z0-9]+$")
        self.probe = self.workspace + "/privacy_probe.py"
        self.vm.put(self.probe, PROBE_SOURCE.read_text())

    def sh(self, command, timeout=30):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command} failed:\n{output}")
        return output

    def check(self, stage, use_context=False):
        paths = ' "$BEING_CONTEXT_DIR" "$BEING_SLOT_DIR"' if use_context else ""
        env = f"{CONTEXT_ENV} && " if use_context else ""
        return self.sh(f"{env}{PACKAGE_PYTHON} {self.probe} {stage}{paths}", 75)

    def wait_privacy(self, *, guest, muted, timeout=35):
        deadline = time.monotonic() + timeout
        command = f"{PACKAGE_PYTHON} {self.probe} snapshot"
        snapshot = {}
        while time.monotonic() < deadline:
            status, output = self.vm.run(command, 8)
            if status == 0:
                snapshot = json.loads(output)
                if (
                    snapshot.get("state.changed", {}).get("muted") is muted
                    and snapshot.get("app.privacy", {}).get("guest") is guest
                    and "mind.ready" in snapshot
                ):
                    return
            time.sleep(0.25)
        self.fail(f"expected guest={guest}, muted={muted} and ready Mind, last snapshot: {snapshot!r}")

    def restart(self, unit):
        old_pid = self.sh(f"systemctl show -p MainPID --value {unit}")
        self.assertRegex(old_pid, r"^[1-9][0-9]*$")
        self.sh(f"systemctl restart {unit}", 90)
        new_pid = self.sh(f"systemctl show -p MainPID --value {unit}")
        self.assertRegex(new_pid, r"^[1-9][0-9]*$")
        self.assertNotEqual(new_pid, old_pid)

    def test_canary_across_working_state_diagnostics_and_authenticated_restore(self):
        backup = f"{self.workspace}/backup_privacy_roundtrip.py"
        self.vm.put(backup, BACKUP_SOURCE.read_text())
        self.assertEqual(self.vm.converse("Remember that my bike is called Zanzibarine", 300),
                         "Okay. I'll remember that.")
        self.assertEqual(self.vm.converse("Remember that the safe word is Quixotically", 300),
                         "Okay. I'll remember that.")
        self.check("seed")
        self.sh(f"{PACKAGE_PYTHON} {backup} before", 190)

        reply = self.vm.converse("Tell me about the safe word.", REPLY)
        self.assertTrue(reply)
        self.assertNotIn("lost my thought", reply)
        self.assertNotIn("can't think right now", reply)
        self.check("before", use_context=True)

        # Deterministic recall points “Forget that” at the same fact without
        # changing the model slot that held the canary.
        self.assertIn("quixotically", self.vm.converse("What do you remember about the safe word?", 300).lower())
        self.assertTrue(self.vm.converse("Forget that", 300).startswith("Okay. That's forgotten."))
        self.assertEqual(self.vm.converse("What do you remember about the safe word?", 300),
                         "I don't remember anything about that.")
        self.check("after", use_context=True)
        self.sh(f"{PACKAGE_PYTHON} {backup} after", 180)
        self.check("diagnostics")

        sleep(self.vm)
        self.assertTrue(self.vm.disk_contains(b"zanzibarin"), "retained control missing from the offline disk")
        self.assertFalse(self.vm.disk_contains(b"quixot"), "canary remains on the offline disk")

    def test_mind_and_brainstem_restarts_keep_guest_admission_and_mute(self):
        self.wait_privacy(guest=False, muted=False)
        recall = "What do you remember about my bike?"
        self.assertEqual(self.vm.converse("Remember that my bike is called Zanzibarine", 300),
                         "Okay. I'll remember that.")
        self.assertIn("zanzibarine", self.vm.converse(recall, 300).lower())
        self.assertIn("Guest mode is on.", self.vm.converse("Start guest mode", 300))
        self.assertIn("for this session", self.vm.converse("Remember that my kite is Amberpost", 300))

        self.vm.say("/mute")
        self.wait_privacy(guest=True, muted=True)
        self.restart("being-mind")
        self.wait_privacy(guest=True, muted=True)
        self.restart("being-brainstem")
        self.wait_privacy(guest=True, muted=True)

        self.vm.say("/mute")
        self.wait_privacy(guest=True, muted=False)
        self.assertNotIn("zanzibarine", self.vm.converse(recall, 300).lower())
        self.assertNotIn("amberpost", self.vm.converse("What do you remember about my kite?", 300).lower())
        self.assertIn("Guest mode is off.", self.vm.converse("End guest mode", 300))
        self.wait_privacy(guest=False, muted=False)
        self.assertIn("zanzibarine", self.vm.converse(recall, 300).lower())
