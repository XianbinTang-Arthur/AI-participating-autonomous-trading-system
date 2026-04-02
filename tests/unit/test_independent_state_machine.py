from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.state_machine import (
    derive_book_state,
    derive_guard_state,
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
        self.assertIsNone(derive_guard_state(snapshot=snapshot))
        self.assertEqual(holding_phase, "entry")

    def test_derive_guard_state_marks_trial_guard_block_as_suspended(self) -> None:
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
        self.assertEqual(derive_book_state(snapshot=snapshot), "flat")
        self.assertEqual(derive_guard_state(snapshot=snapshot), "suspended")

    def test_derive_book_state_marks_flat_block_without_cooldown_as_flat(self) -> None:
        decision = IndependentBookDecision(
            leg="short",
            expectancy=None,
            score=0.31,
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="blocked",
            reason_codes=[],
            blocked_reasons=["independent_short_book_score_stability_below_threshold"],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="blocked",
        )

        snapshot = snapshot_from_decision(decision=decision)

        self.assertEqual(derive_book_state(snapshot=snapshot), "flat")
        self.assertIsNone(derive_guard_state(snapshot=snapshot))

    def test_derive_guard_state_marks_flat_block_with_cooldown_reason_as_cooldown(self) -> None:
        decision = IndependentBookDecision(
            leg="short",
            expectancy=None,
            score=0.31,
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="blocked",
            reason_codes=[],
            blocked_reasons=["independent_short_book_post_close_cooldown_active"],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=120.0,
            book_action="blocked",
        )

        snapshot = snapshot_from_decision(decision=decision)

        self.assertEqual(derive_book_state(snapshot=snapshot), "flat")
        self.assertEqual(derive_guard_state(snapshot=snapshot), "cooldown")

    def test_derive_guard_state_marks_flat_block_with_active_suspension_as_suspended(self) -> None:
        as_of_ts = datetime(2026, 4, 2, tzinfo=timezone.utc)
        decision = IndependentBookDecision(
            leg="short",
            expectancy=None,
            score=0.31,
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="blocked",
            reason_codes=[],
            blocked_reasons=["independent_short_book_score_stability_below_threshold"],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="blocked",
            suspended_until=as_of_ts + timedelta(seconds=30),
        )

        snapshot = snapshot_from_decision(decision=decision)

        self.assertEqual(derive_book_state(snapshot=snapshot), "flat")
        self.assertEqual(derive_guard_state(snapshot=snapshot, as_of_ts=as_of_ts), "suspended")

    def test_snapshot_from_decision_prefers_persisted_counts_prior_states_and_guard(self) -> None:
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
            guard_state="cooldown",
            prior_book_state="holding",
            prior_guard_state="cooldown",
            current_scale_in_count=2,
            current_de_risk_count=1,
            last_transition_reason="independent_scale_in",
            state_version=7,
        )

        snapshot = snapshot_from_decision(decision=decision)

        self.assertEqual(snapshot.prior_book_state, "holding")
        self.assertEqual(snapshot.prior_guard_state, "cooldown")
        self.assertEqual(snapshot.guard_state, "cooldown")
        self.assertEqual(snapshot.current_scale_in_count, 2)
        self.assertEqual(snapshot.current_de_risk_count, 1)
        self.assertEqual(snapshot.state_version, 7)
        self.assertEqual(snapshot.last_transition_reason, "independent_scale_in")

    def test_transition_book_state_marks_invalid_active_cooldown_to_building_jump(self) -> None:
        as_of_ts = datetime(2026, 4, 2, tzinfo=timezone.utc)
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
            prior_book_state="holding",
            prior_guard_state="cooldown",
            cooldown_until=as_of_ts + timedelta(seconds=30),
        )

        transition = transition_book_state(
            prior_state="holding",
            prior_guard_state="cooldown",
            snapshot=snapshot_from_decision(decision=decision),
            as_of_ts=as_of_ts,
        )

        self.assertFalse(transition.valid_transition)
        self.assertEqual(transition.prior_state, "holding")
        self.assertEqual(transition.prior_guard_state, "cooldown")
        self.assertEqual(transition.next_state, "building")
        self.assertEqual(transition.next_guard_state, "cooldown")
        self.assertEqual(
            transition.violation_reason,
            "independent_transition_invalid:cooldown->building",
        )

    def test_transition_book_state_normalizes_stale_guard_states_to_inventory_backed_base_state(self) -> None:
        as_of_ts = datetime(2026, 4, 2, tzinfo=timezone.utc)
        scenarios = (
            ("legacy", "cooldown", None, Decimal("0"), Decimal("0.03"), "open", "flat", "probing"),
            ("legacy", "suspended", None, Decimal("0"), Decimal("0.03"), "open", "flat", "probing"),
            ("legacy", "cooldown", None, Decimal("0.01"), Decimal("0.03"), "scale_in", "holding", "building"),
            ("legacy", "suspended", None, Decimal("0.01"), Decimal("0.03"), "scale_in", "holding", "building"),
            ("separated", "flat", "cooldown", Decimal("0"), Decimal("0.03"), "open", "flat", "probing"),
            ("separated", "flat", "suspended", Decimal("0"), Decimal("0.03"), "open", "flat", "probing"),
            ("separated", "holding", "cooldown", Decimal("0.01"), Decimal("0.03"), "scale_in", "holding", "building"),
            ("separated", "holding", "suspended", Decimal("0.01"), Decimal("0.03"), "scale_in", "holding", "building"),
        )
        for representation, prior_state, prior_guard_state, current_qty, target_qty, book_action, expected_prior_state, expected_next_state in scenarios:
            with self.subTest(
                representation=representation,
                prior_state=prior_state,
                prior_guard_state=prior_guard_state,
                current_qty=str(current_qty),
                book_action=book_action,
            ):
                decision = IndependentBookDecision(
                    leg="short",
                    expectancy=None,
                    score=0.95,
                    current_qty=current_qty,
                    target_qty=target_qty,
                    state="opening",
                    reason_codes=[],
                    blocked_reasons=[],
                    min_hold_remaining_seconds=0.0,
                    rebalance_cooldown_remaining_seconds=0.0,
                    book_action=book_action,
                    book_state=expected_next_state,
                    prior_book_state=None if representation == "legacy" else expected_prior_state,
                    prior_guard_state=prior_guard_state,
                )

                transition = transition_book_state(
                    prior_state=prior_state,
                    prior_guard_state=prior_guard_state,
                    snapshot=snapshot_from_decision(decision=decision),
                    as_of_ts=as_of_ts,
                )

                self.assertTrue(transition.valid_transition)
                self.assertEqual(transition.prior_state, expected_prior_state)
                self.assertIsNone(transition.prior_guard_state)
                self.assertEqual(transition.next_state, expected_next_state)
                self.assertIsNone(transition.next_guard_state)
                self.assertIsNone(transition.violation_reason)

    def test_transition_book_state_keeps_active_cooldown_guarded(self) -> None:
        as_of_ts = datetime(2026, 4, 2, tzinfo=timezone.utc)
        decision = IndependentBookDecision(
            leg="short",
            expectancy=None,
            score=0.95,
            current_qty=Decimal("0"),
            target_qty=Decimal("0.03"),
            state="opening",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="open",
            book_state="probing",
            prior_book_state="flat",
            prior_guard_state="cooldown",
            cooldown_until=as_of_ts + timedelta(seconds=30),
        )

        transition = transition_book_state(
            prior_state="flat",
            prior_guard_state="cooldown",
            snapshot=snapshot_from_decision(decision=decision),
            as_of_ts=as_of_ts,
        )

        self.assertFalse(transition.valid_transition)
        self.assertEqual(transition.prior_state, "flat")
        self.assertEqual(transition.prior_guard_state, "cooldown")
        self.assertEqual(transition.next_state, "probing")
        self.assertEqual(
            transition.violation_reason,
            "independent_transition_invalid:cooldown->probing",
        )

    def test_transition_book_state_keeps_active_suspension_guarded(self) -> None:
        as_of_ts = datetime(2026, 4, 2, tzinfo=timezone.utc)
        decision = IndependentBookDecision(
            leg="short",
            expectancy=None,
            score=0.95,
            current_qty=Decimal("0"),
            target_qty=Decimal("0.03"),
            state="opening",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="open",
            book_state="probing",
            prior_book_state="flat",
            prior_guard_state="suspended",
            suspended_until=as_of_ts + timedelta(seconds=30),
        )

        transition = transition_book_state(
            prior_state="flat",
            prior_guard_state="suspended",
            snapshot=snapshot_from_decision(decision=decision),
            as_of_ts=as_of_ts,
        )

        self.assertFalse(transition.valid_transition)
        self.assertEqual(transition.prior_state, "flat")
        self.assertEqual(transition.prior_guard_state, "suspended")
        self.assertEqual(transition.next_state, "probing")
        self.assertEqual(
            transition.violation_reason,
            "independent_transition_invalid:suspended->probing",
        )


if __name__ == "__main__":
    unittest.main()
