import unittest

from lab.decision_eval import Prediction, PreparedCase
from lab.decision_fixtures import Case
from lab.recall_second_opinion import apply_opinion, evaluate


class RecallSecondOpinionTests(unittest.TestCase):
    def setUp(self):
        self.recall = Prediction("search_history", "archived", 0.5)

    def test_preserves_baseline_on_bypass_missing_and_unqualified_opinions(self):
        for opinion in (
            None,
            Prediction("search_history", "different", 300, 0.99),
            Prediction("conversation", None, 300, 0.799),
            Prediction("conversation", None, 301, 0.99),
        ):
            with self.subTest(opinion=opinion):
                self.assertIs(apply_opinion(self.recall, opinion, deadline_ms=300), self.recall)

        confident = Prediction("conversation", None, 300, 0.99)
        command = Prediction("search_history", "archived", 0.5, bypassed=True)
        ordinary = Prediction("conversation", None, 0.5)
        self.assertIs(apply_opinion(command, confident), command)
        self.assertIs(apply_opinion(ordinary, confident), ordinary)

    def test_cutoff_and_deadline_are_inclusive(self):
        opinion = Prediction("ask_clarification", None, 300, 0.8)
        applied = apply_opinion(self.recall, opinion, deadline_ms=300)
        self.assertEqual(applied.route, "ask_clarification")
        self.assertIsNone(applied.source)

    def test_scores_unnecessary_context_and_lost_sources_by_case(self):
        ordinary = Case("ordinary", "conversation", "What's the weather?", "conversation")
        memory = Case("detail", "implicit", "What was the name?", "search_history", "archived")
        rows = [PreparedCase(case, self.recall, (), 0.1) for case in (ordinary, memory)]
        opinions = {case.id: Prediction("conversation", None, 300, 0.9) for case in (ordinary, memory)}

        applied, _ = evaluate(rows, opinions, deadline_ms=300)
        self.assertEqual(applied["ordinary_context_prevented"], ["ordinary"])
        self.assertEqual(applied["lost_baseline_sources"], ["detail"])
        fallback, _ = evaluate(rows, opinions, deadline_ms=250)
        self.assertEqual(fallback["ordinary_context_prevented"], [])
        self.assertEqual(fallback["lost_baseline_sources"], [])
