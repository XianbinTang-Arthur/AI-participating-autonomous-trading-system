from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.adaptive import threshold_snapshot
from aats.services.strategy_engines.independent.health import evaluate_leg_health
from aats.services.strategy_engines.independent.models import IndependentBookDecision
from aats.services.strategy_engines.independent.replay import replay_snapshot_from_decision
from aats.services.strategy_engines.independent.state_machine import snapshot_from_decision
from tests.support.strategy_family import make_derivatives_hedge_settings


class TestIndependentReplay(unittest.TestCase):
    def test_replay_snapshot_captures_additive_state_health_and_thresholds(self) -> None:
        settings = make_derivatives_hedge_settings()
        decision = IndependentBookDecision(
            leg="long",
            expectancy=None,
            score=0.81,
            current_qty=Decimal("0"),
            target_qty=Decimal("0.01"),
            state="opening",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="open",
            book_state="probing",
            holding_phase="entry",
            health_state="ok",
            policy_reason="independent_entry_guarded_passive_first",
        )
        snapshot = replay_snapshot_from_decision(
            decision=decision,
            threshold_snapshot=threshold_snapshot(settings=settings, leg="long"),
            state_snapshot=snapshot_from_decision(decision=decision),
            health_snapshot=evaluate_leg_health(decision=decision),
            prior_book_state="flat",
            prior_state_source="runtime_state",
        )

        self.assertEqual(snapshot.book_state, "probing")
        self.assertEqual(snapshot.holding_phase, "entry")
        self.assertEqual(snapshot.health_state, "ok")
        self.assertIsNotNone(snapshot.threshold_snapshot)
        self.assertEqual(snapshot.prior_book_state, "flat")
        self.assertTrue(snapshot.transition_reconstructed)
        self.assertEqual(snapshot.transition_source, "runtime_state")
        self.assertIsNotNone(snapshot.threshold_snapshot.adaptive_entry_threshold)

    def test_replay_snapshot_does_not_fabricate_transition_without_prior_state(self) -> None:
        decision = IndependentBookDecision(
            leg="short",
            expectancy=None,
            score=0.24,
            current_qty=Decimal("0.01"),
            target_qty=Decimal("0.00"),
            state="closing",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="close_stale_thesis",
            book_state="forced_exit",
            holding_phase="exit",
            health_state="degraded",
        )
        snapshot = replay_snapshot_from_decision(
            decision=decision,
            state_snapshot=snapshot_from_decision(decision=decision),
            health_snapshot=evaluate_leg_health(decision=decision),
        )

        self.assertIsNone(snapshot.prior_book_state)
        self.assertFalse(snapshot.transition_reconstructed)
        self.assertIsNone(snapshot.transition_source)


if __name__ == "__main__":
    unittest.main()
