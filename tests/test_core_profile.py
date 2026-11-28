"""Source-linked core references must follow the current owner archive."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mind import core_profile
from mind.memory import Memory
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


class CoreProfileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.memory = Memory(self.root, embedder=None)
        self.addCleanup(self.memory.close)
        self.owner = "Richard"

    def test_publication_is_stable_and_keeps_only_confirmed_owner_references(self):
        self.memory.remember("A tentative inference", self.owner, confidence=0.7)
        self.memory.remember("A guest preference", "Guest")
        source = self.memory.log("said", "An assistant assertion", "Pip")
        self.memory.remember("An assistant assertion", self.owner, source=source)
        fact = self.memory.remember("My favourite tree is a silver birch", self.owner)

        first = core_profile.publish(self.memory, self.owner)
        path = self.root / core_profile.FILE
        original = path.read_bytes()
        self.assertIn(f"Fact F{fact}".encode(), original)
        self.assertNotIn(b"silver birch", original)
        self.assertEqual(core_profile.publish(self.memory, self.owner), first)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(core_profile.load(self.root), first)
        self.assertEqual(core_profile.resolve(self.memory, self.owner, first),
                         ((fact, "My favourite tree is a silver birch"),))
        self.assertEqual(core_profile.resolve(self.memory, "Guest", first), ())

    def test_correction_revokes_old_profile_even_if_an_old_index_is_restored(self):
        fact = self.memory.remember("The house is blue", self.owner)
        old = core_profile.publish(self.memory, self.owner)
        path = self.root / core_profile.FILE
        saved = path.read_bytes()
        self.assertTrue(self.memory.correct(fact, "The house is red"))
        self.assertFalse(path.exists())
        path.write_bytes(saved)
        self.assertEqual(core_profile.resolve(self.memory, self.owner, core_profile.load(self.root)), ())
        new = core_profile.publish(self.memory, self.owner)
        self.assertEqual(core_profile.resolve(self.memory, self.owner, new), ((fact, "The house is red"),))
        self.memory.forget((fact,))
        self.assertFalse(path.exists())
        self.assertEqual(core_profile.resolve(self.memory, self.owner, new), ())

    def test_hand_written_core_takes_precedence_and_is_left_alone(self):
        manual = self.root / core_profile.MANUAL_FILE
        manual.write_text("The owner wrote this.\n", encoding="utf-8")
        fact = self.memory.remember("A separate archive fact", self.owner)
        self.assertIsNone(core_profile.publish(self.memory, self.owner))
        self.memory.forget((fact,))
        self.assertEqual(core_profile.manual_text(self.root), "The owner wrote this.\n")
