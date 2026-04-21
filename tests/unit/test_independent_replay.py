from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from decimal import Decimal

from aats.schemas.strategy_runtime import StrategyBookRuntimeState, StrategySleeveIntent
from aats.services.strategy_engines.independent.adaptive import threshold_snapshot
from aats.services.strategy_engines.independent.health import evaluate_leg_health
from aats.services.strategy_engines.independent.models import IndependentBookDecision
from aats.services.strategy_engines.independent.replay import (
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


class TestBundleLegMatchesExcludesRejectedLegs(unittest.TestCase):
    """task109 §4 一致性锚点：`_bundle_leg_matches` 必须过滤掉 bundle safe
    subset 拒掉的 leg（带 risk_rejection_reasons / risk_approved=False）。
    否则被拒腿的 execution_chain_id 会被误当作 active execution 污染
    replay 状态；bundle.created_at 会被误当成最近一次执行时间。"""

    def _make_bundle_leg(
        self,
        *,
        risk_approved: bool | None = None,
        risk_rejection_reasons: list[str] | None = None,
    ):
        from aats.schemas.strategy_runtime import StrategyExecutionBundle, StrategyLegIntent
        return StrategyExecutionBundle(
            bundle_id="bundle_anchor",
            decision_id="decision_anchor",
            family="independent",
            participating_families=["independent"],
            strategy_sleeve_id="sleeve_anchor",
            product_type="derivatives",
            margin_mode="cross",
            route_action="override_target",
            status="submitted",
            selected_symbol="BTC-USDT-SWAP",
            legs=[
                StrategyLegIntent(
                    symbol="BTC-USDT-SWAP",
                    execution_chain_id="chain_rejected",
                    product_type="derivatives",
                    side="sell",
                    position_mode="long_short_mode",
                    pos_side="long",
                    action="close",
                    family="independent",
                    role="primary",
                    margin_mode="cross",
                    target_leverage=3.0,
                    current_position_qty=Decimal("0.01"),
                    target_position_qty=Decimal("0"),
                    delta_position_qty=Decimal("-0.01"),
                    reference_price=Decimal("80000"),
                    execution_compatible=True,
                    execution_mode="independent_long_book",
                    state_phase="active",
                    overlay_mode="independent",
                    trigger_reason_codes=[],
                    note="test leg",
                    strategy_sleeve_id="sleeve_anchor",
                    risk_approved=risk_approved,
                    risk_rejection_reasons=risk_rejection_reasons or [],
                )
            ],
        )

    def test_rejected_leg_with_false_risk_approved_filtered_out(self) -> None:
        from aats.services.strategy_engines.independent.replay import _bundle_leg_matches
        bundle = self._make_bundle_leg(
            risk_approved=False,
            risk_rejection_reasons=["bundle_leg_risk_constraints_applied"],
        )
        leg_intent = bundle.legs[0]
        self.assertFalse(
            _bundle_leg_matches(
                bundle=bundle,
                leg_intent=leg_intent,
                symbol="BTC-USDT-SWAP",
                strategy_sleeve_id="sleeve_anchor",
                leg="long",
            )
        )

    def test_rejected_leg_with_only_rejection_reasons_filtered_out(self) -> None:
        from aats.services.strategy_engines.independent.replay import _bundle_leg_matches
        bundle = self._make_bundle_leg(
            risk_approved=None,  # 未显式 False，但 rejection_reasons 非空
            risk_rejection_reasons=["symbol_notional_cap_exceeded"],
        )
        leg_intent = bundle.legs[0]
        self.assertFalse(
            _bundle_leg_matches(
                bundle=bundle,
                leg_intent=leg_intent,
                symbol="BTC-USDT-SWAP",
                strategy_sleeve_id="sleeve_anchor",
                leg="long",
            )
        )

    def test_executed_leg_not_filtered_out(self) -> None:
        from aats.services.strategy_engines.independent.replay import _bundle_leg_matches
        bundle = self._make_bundle_leg(risk_approved=True, risk_rejection_reasons=[])
        leg_intent = bundle.legs[0]
        self.assertTrue(
            _bundle_leg_matches(
                bundle=bundle,
                leg_intent=leg_intent,
                symbol="BTC-USDT-SWAP",
                strategy_sleeve_id="sleeve_anchor",
                leg="long",
            )
        )


if __name__ == "__main__":
    unittest.main()
