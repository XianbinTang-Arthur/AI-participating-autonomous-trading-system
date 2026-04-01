from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.health import aggregate_family_health, evaluate_leg_health
from aats.services.strategy_engines.independent.models import IndependentBookDecision


class TestIndependentHealth(unittest.TestCase):
    def test_evaluate_leg_health_marks_blocked_trial_guard_as_suspended(self) -> None:
        decision = IndependentBookDecision(
            leg="long",
            expectancy=None,
            score=0.2,
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="blocked",
            reason_codes=[],
            blocked_reasons=["independent_long_book_trial_guard_active"],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="blocked",
            execution_health_state="blocked",
        )
        snapshot = evaluate_leg_health(decision=decision)
        self.assertTrue(snapshot.suspended)
        self.assertTrue(snapshot.halt_openings)

    def test_aggregate_family_health_uses_worst_leg_state(self) -> None:
        long_leg = evaluate_leg_health(
            decision=IndependentBookDecision(
                leg="long",
                expectancy=None,
                score=0.8,
                current_qty=Decimal("0"),
                target_qty=Decimal("0.01"),
                state="opening",
                reason_codes=[],
                blocked_reasons=[],
                min_hold_remaining_seconds=0.0,
                rebalance_cooldown_remaining_seconds=0.0,
                book_action="open",
                execution_health_state="ok",
            )
        )
        short_leg = evaluate_leg_health(
            decision=IndependentBookDecision(
                leg="short",
                expectancy=None,
                score=0.4,
                current_qty=Decimal("0.02"),
                target_qty=Decimal("0.01"),
                state="holding",
                reason_codes=[],
                blocked_reasons=["independent_short_book_execution_health_not_ok"],
                min_hold_remaining_seconds=0.0,
                rebalance_cooldown_remaining_seconds=0.0,
                book_action="de_risk",
                execution_health_state="degraded",
            )
        )
        family = aggregate_family_health(long_leg=long_leg, short_leg=short_leg)
        self.assertEqual(family.overall_state, "degraded")


if __name__ == "__main__":
    unittest.main()
