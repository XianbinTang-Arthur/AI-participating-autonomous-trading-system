from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.state_machine import (
    derive_book_state,
    derive_holding_phase,
    snapshot_from_decision,
    transition_book_state,
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

    def test_snapshot_from_decision_prefers_persisted_counts_and_prior_state(self) -> None:
        decision = IndependentBookDecision(
            leg="long",
            expectancy=None,
            score=0.91,
            current_qty=Decimal("0.02"),
            target_qty=Decimal("0.03"),
            state="opening",
            reason_codes=["independent_long_book_signal_above_scale_in_threshold"],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="scale_in",
            prior_book_state="holding",
            current_scale_in_count=2,
            current_de_risk_count=1,
            last_transition_reason="independent_scale_in",
            state_version=7,
        )

        snapshot = snapshot_from_decision(decision=decision)

        self.assertEqual(snapshot.prior_book_state, "holding")
        self.assertEqual(snapshot.current_scale_in_count, 2)
        self.assertEqual(snapshot.current_de_risk_count, 1)
        self.assertEqual(snapshot.state_version, 7)
        self.assertEqual(snapshot.last_transition_reason, "independent_scale_in")

    def test_transition_book_state_marks_invalid_cooldown_to_building_jump(self) -> None:
        decision = IndependentBookDecision(
            leg="long",
            expectancy=None,
            score=0.95,
            current_qty=Decimal("0.01"),
            target_qty=Decimal("0.03"),
            state="opening",
            reason_codes=["independent_long_book_signal_above_scale_in_threshold"],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="scale_in",
            book_state="building",
        )

        transition = transition_book_state(
            prior_state="cooldown",
            snapshot=snapshot_from_decision(decision=decision),
        )

        self.assertFalse(transition.valid_transition)
        self.assertEqual(transition.prior_state, "cooldown")
        self.assertEqual(transition.next_state, "building")
        self.assertEqual(
            transition.violation_reason,
            "independent_transition_invalid:cooldown->building",
        )


if __name__ == "__main__":
    unittest.main()
