from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.state_machine import (
    derive_book_state,
    derive_holding_phase,
    snapshot_from_decision,
)
from aats.services.strategy_engines.independent.models import IndependentBookDecision


class TestIndependentStateMachine(unittest.TestCase):
    def test_derive_book_state_marks_flat_open_as_probing(self) -> None:
        decision = IndependentBookDecision(
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
        )
        snapshot = snapshot_from_decision(decision=decision)
        book_state = derive_book_state(snapshot=snapshot)
        holding_phase = derive_holding_phase(snapshot=snapshot, book_state=book_state)

        self.assertEqual(book_state, "probing")
        self.assertEqual(holding_phase, "entry")

    def test_derive_book_state_marks_trial_guard_block_as_suspended(self) -> None:
        decision = IndependentBookDecision(
            leg="short",
            expectancy=None,
            score=0.3,
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="blocked",
            reason_codes=[],
            blocked_reasons=["independent_short_book_trial_guard_active"],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="blocked",
        )
        snapshot = snapshot_from_decision(decision=decision)
        self.assertEqual(derive_book_state(snapshot=snapshot), "suspended")


if __name__ == "__main__":
    unittest.main()
