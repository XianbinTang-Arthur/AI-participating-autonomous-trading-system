from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from decimal import Decimal

from aats.schemas.strategy_runtime import PortfolioAllocationDecision, StrategyBookRuntimeState, StrategySleeveIntent
from aats.services.strategy_engines.independent.adaptive import threshold_snapshot
from aats.services.strategy_engines.independent.health import evaluate_leg_health
from aats.services.strategy_engines.independent.models import IndependentBookDecision
from aats.services.strategy_engines.independent.replay import (
    _decision_snapshot_from_sources,
    _normalized_threshold_snapshot_value,
    replay_snapshot_from_decision,
)
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
        self.assertIsNone(snapshot.guard_state)
        self.assertEqual(snapshot.holding_phase, "entry")
        self.assertEqual(snapshot.health_state, "ok")
        self.assertIsNotNone(snapshot.threshold_snapshot)
        self.assertEqual(snapshot.prior_book_state, "flat")
        self.assertIsNone(snapshot.prior_guard_state)
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

    def test_replay_snapshot_surfaces_transition_violation(self) -> None:
        as_of_ts = datetime(2026, 4, 2, tzinfo=timezone.utc)
        decision = IndependentBookDecision(
            leg="long",
            expectancy=None,
            score=0.92,
            current_qty=Decimal("0.01"),
            target_qty=Decimal("0.03"),
            state="opening",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="scale_in",
            prior_book_state="holding",
            prior_guard_state="cooldown",
            current_scale_in_count=2,
            state_version=4,
            cooldown_until=as_of_ts + timedelta(seconds=30),
        )

        snapshot = replay_snapshot_from_decision(
            decision=decision,
            state_snapshot=snapshot_from_decision(decision=decision),
            health_snapshot=evaluate_leg_health(decision=decision),
            prior_book_state="holding",
            prior_guard_state="cooldown",
            prior_state_source="runtime_state",
        )

        self.assertTrue(snapshot.transition_reconstructed)
        self.assertFalse(snapshot.transition_valid)
        self.assertEqual(snapshot.prior_book_state, "holding")
        self.assertEqual(snapshot.prior_guard_state, "cooldown")
        self.assertEqual(snapshot.transition_violation_reason, "independent_transition_invalid:cooldown->building")

    def test_normalized_threshold_snapshot_backfills_legacy_drawdown_fields(self) -> None:
        normalized = _normalized_threshold_snapshot_value(
            sleeve_intent=StrategySleeveIntent(
                decision_id="decision_threshold_backfill",
                family="independent",
                strategy_sleeve_id="sleeve_threshold_backfill",
                state="candidate",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                inventory_policy="paired_inventory",
                route_action="override_target",
                family_action="open_independent_book",
                metrics={
                    "min_score_stability_bps": 2.0,
                    "effective_score_drawdown_threshold_bps": 6.0,
                    "long_threshold_snapshot": {
                        "leg": "long",
                        "entry_threshold": 0.60,
                        "effective_entry_threshold": 0.66,
                    },
                },
            ),
            leg="long",
            runtime_state=StrategyBookRuntimeState(
                leg="long",
                current_qty=Decimal("0"),
                target_qty=Decimal("0.01"),
            ),
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["score_drawdown_bps"], 6.0)
        self.assertEqual(normalized["effective_score_drawdown_bps"], 6.0)

    def test_decision_snapshot_prefers_upward_and_downward_drawdown_fields(self) -> None:
        decision = PortfolioAllocationDecision(
            decision_id="decision_replay_score_metrics",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
        )
        sleeve_intent = StrategySleeveIntent(
            decision_id=decision.decision_id,
            family="independent",
            strategy_sleeve_id="sleeve_replay_score_metrics",
            state="candidate",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            inventory_policy="paired_inventory",
            route_action="override_target",
            family_action="hold_family",
            metrics={
                "long_score_support_count": 3,
                "long_score_stable": True,
                "long_score_stability_max_drawdown_bps": 8.0,
                "long_score_stability_max_drawdown_bps_compat_source": "upward_excursion_bps",
                "long_score_stability_upward_excursion_bps": 8.0,
                "long_score_stability_downward_drawdown_bps": 0.0,
                "long_score_stability_source": "recent_target_history",
            },
        )
        runtime_state = StrategyBookRuntimeState(
            leg="long",
            current_qty=Decimal("0"),
            target_qty=Decimal("0.01"),
        )

        snapshot = _decision_snapshot_from_sources(
            decision=decision,
            sleeve_intent=sleeve_intent,
            leg="long",
            runtime_state=runtime_state,
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        assert snapshot.score_stability_metrics is not None
        self.assertEqual(snapshot.score_stability_metrics["upward_excursion_bps"], 8.0)
        self.assertEqual(snapshot.score_stability_metrics["downward_drawdown_bps"], 0.0)
        self.assertNotIn("max_drawdown_bps", snapshot.score_stability_metrics)
        self.assertNotIn("max_drawdown_bps_compat_source", snapshot.score_stability_metrics)


if __name__ == "__main__":
    unittest.main()
