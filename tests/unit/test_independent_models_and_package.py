from __future__ import annotations

import importlib
import sys
import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.models import IndependentBookDecision


class TestIndependentModelsAndPackage(unittest.TestCase):
    def test_independent_book_decision_freezes_reason_sequences_as_tuples(self) -> None:
        decision = IndependentBookDecision(
            leg="long",
            expectancy=None,
            score=0.5,
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="inactive",
            reason_codes=["one", "two"],
            blocked_reasons=["three"],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="inactive",
        )

        self.assertIsInstance(decision.reason_codes, tuple)
        self.assertIsInstance(decision.blocked_reasons, tuple)
        with self.assertRaises(AttributeError):
            decision.reason_codes.append("boom")  # type: ignore[attr-defined]

    def test_strategy_engines_package_does_not_eager_import_coordinator(self) -> None:
        sys.modules.pop("aats.services.strategy_engines", None)
        sys.modules.pop("aats.services.strategy_engines.coordinator", None)

        package = importlib.import_module("aats.services.strategy_engines")

        self.assertNotIn("aats.services.strategy_engines.coordinator", sys.modules)
        self.assertFalse(hasattr(package, "StrategyCoordinatorService"))
        self.assertNotIn("aats.services.strategy_engines.coordinator", sys.modules)


if __name__ == "__main__":
    unittest.main()
