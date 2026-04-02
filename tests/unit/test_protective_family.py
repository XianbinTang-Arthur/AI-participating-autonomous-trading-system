from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest

from aats.schemas.decision import PositionTarget
from aats.services.strategy_engines.base import StrategyEvaluationContext, StrategyFamilyRuntimeControl
from aats.services.strategy_engines.families.protective_family import (
    OverlayParentExposureContract,
    _resolve_overlay_parent_exposure_contract,
    _resolve_overlay_main_leg_contract,
    build_protective_candidate_leg,
    evaluate_protective_overlay_decision,
    protective_candidate_from_directional_target,
)
from aats.schemas.strategy_runtime import StrategyLegIntent
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestProtectiveFamily(unittest.TestCase):
    def test_overlay_main_leg_contract_falls_back_to_context_and_settings_defaults(self) -> None:
        settings = make_derivatives_hedge_settings(default_target_leverage=2.5, margin_mode="cross")
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        evaluation_context = SimpleNamespace(
            context=context,
            directional_target=SimpleNamespace(
                symbol="",
                target_position_qty=Decimal("0.05"),
                target_leverage=0.0,
                margin_mode="",
            ),
        )

        contract = _resolve_overlay_main_leg_contract(
            settings=settings,
            evaluation_context=evaluation_context,
        )

        self.assertEqual(contract.symbol, context.symbol)
        self.assertEqual(contract.target_leverage, context.current_target_leverage)
        self.assertEqual(contract.margin_mode, "cross")
        self.assertEqual(contract.long_target_qty, Decimal("0.05"))
        self.assertEqual(contract.short_target_qty, Decimal("0"))
        self.assertEqual(contract.source, "context_or_settings_fallback")

    def test_parent_exposure_contract_prefers_inventory_signal_when_target_turns_flat(self) -> None:
        settings = make_derivatives_hedge_settings(default_target_leverage=2.5, margin_mode="cross")
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        evaluation_context = SimpleNamespace(
            context=context,
            directional_target=SimpleNamespace(
                symbol=context.symbol,
                target_position_qty=Decimal("0"),
                target_leverage=1.0,
                margin_mode="cross",
            ),
        )

        contract = _resolve_overlay_parent_exposure_contract(
            settings=settings,
            evaluation_context=evaluation_context,
        )

        self.assertEqual(contract.target_signal, "flat")
        self.assertEqual(contract.current_signal, "long")
        self.assertEqual(contract.effective_signal, "long")
        self.assertEqual(contract.signal_source, "inventory")
        self.assertEqual(contract.source_of_truth, "inventory")
        self.assertEqual(contract.target_qty, Decimal("0"))
        self.assertEqual(contract.current_qty, Decimal("0.05"))
        self.assertEqual(contract.effective_qty, Decimal("0.05"))
        self.assertEqual(contract.target_long_qty, Decimal("0"))
        self.assertEqual(contract.current_long_qty, Decimal("0.05"))

    def test_parent_exposure_contract_prefers_directional_primary_legs_over_net_target_qty(self) -> None:
        settings = make_derivatives_hedge_settings(default_target_leverage=2.5, margin_mode="cross")
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        evaluation_context = SimpleNamespace(
            context=context,
            directional_target=SimpleNamespace(
                symbol=context.symbol,
                target_position_qty=Decimal("0.01"),
                target_leverage=0.0,
                margin_mode="",
                strategy_execution_legs=[
                    StrategyLegIntent(
                        symbol=context.symbol,
                        product_type="derivatives",
                        side="buy",
                        pos_side="long",
                        family="directional",
                        role="primary",
                        margin_mode="isolated",
                        target_leverage=3.0,
                        target_position_qty=Decimal("0.08"),
                    )
                ],
            ),
        )

        contract = _resolve_overlay_parent_exposure_contract(
            settings=settings,
            evaluation_context=evaluation_context,
        )

        self.assertEqual(contract.target_long_qty, Decimal("0.08"))
        self.assertEqual(contract.target_short_qty, Decimal("0"))
        self.assertEqual(contract.margin_mode, "isolated")
        self.assertEqual(contract.target_leverage, 3.0)
        self.assertEqual(contract.source, "directional_primary_legs")
        self.assertEqual(contract.source_of_truth, "mixed")
        self.assertEqual(contract.target_qty, Decimal("0.08"))

    def test_protective_candidate_prefers_precomputed_parent_exposure_over_directional_target(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="protective",
            strategy_hedge_protective_enabled=True,
        )
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})
        directional_target = PositionTarget(
            decision_id="decision_target_test",
            symbol=context.symbol,
            current_position_qty=Decimal("0.05"),
            target_position_qty=Decimal("-0.03"),
            delta_position_qty=Decimal("-0.08"),
            current_notional=Decimal("0"),
            target_notional=Decimal("0"),
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"directional": 1.0},
            decision_expiry_ts=datetime.now(timezone.utc),
            product_type="derivatives",
            current_exposure_side="long",
            target_exposure_side="short",
            position_intent="reverse_to_short",
            target_leverage=1.0,
            margin_mode="cross",
            expected_signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
        )
        evaluation_context = StrategyEvaluationContext(
            context=context,
            baseline=baseline,
            directional_target=directional_target,
            latest_snapshot=None,
            latest_account_snapshot=None,
            latest_market_snapshot=None,
            recent_market_snapshots={},
            recent_targets_by_family={},
            ai_assessment=make_ai_assessment(direction=-0.12, confidence=0.83),
            family_runtime_controls={
                "protective": StrategyFamilyRuntimeControl(
                    enabled=True,
                    live_execution_enabled=True,
                )
            },
            overlay_parent_exposure=OverlayParentExposureContract(
                parent_family="directional",
                symbol=context.symbol,
                target_leverage=2.0,
                margin_mode="isolated",
                target_long_qty=Decimal("0.05"),
                target_short_qty=Decimal("0"),
                current_long_qty=Decimal("0.05"),
                current_short_qty=Decimal("0"),
                target_qty=Decimal("0.05"),
                current_qty=Decimal("0.05"),
                effective_qty=Decimal("0.05"),
                target_signal="long",
                current_signal="long",
                effective_signal="long",
                signal_source="target_position",
                source_of_truth="mixed",
                lifecycle_state="target_and_inventory",
                target_active=True,
                inventory_active=True,
                source="coordinator_parent_exposure",
            ),
            overlay_parent_exposures_by_family={},
        )

        candidate = protective_candidate_from_directional_target(
            settings=settings,
            evaluation_context=evaluation_context,
        )

        self.assertEqual(candidate.metrics["main_leg_contract_source"], "coordinator_parent_exposure")
        self.assertEqual(candidate.metrics["parent_effective_signal"], "long")
        self.assertEqual(candidate.metrics["parent_lifecycle_state"], "target_and_inventory")
        self.assertEqual(candidate.metrics["parent_source_of_truth"], "mixed")
        self.assertEqual(candidate.metrics["parent_target_qty"], Decimal("0.05"))
        self.assertEqual(candidate.metrics["parent_effective_qty"], Decimal("0.05"))
        self.assertEqual(candidate.recommended_symbol, context.symbol)
        self.assertTrue(candidate.legs)
        self.assertEqual(candidate.legs[0].margin_mode, "isolated")
        self.assertEqual(candidate.legs[0].pos_side, "short")

    def test_evaluate_protective_overlay_opens_short_hedge_leg_against_existing_long(self) -> None:
        settings = make_derivatives_hedge_settings()
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        overlay_decision = evaluate_protective_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.12, confidence=0.83),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
        )
        hedge_leg = build_protective_candidate_leg(
            symbol=context.symbol,
            target_leverage=1.0,
            margin_mode=str(settings.margin_mode),
            overlay_decision=overlay_decision,
        )

        self.assertTrue(overlay_decision.active)
        self.assertEqual(overlay_decision.state, "opening")
        self.assertIsNotNone(hedge_leg)
        assert hedge_leg is not None
        self.assertEqual(hedge_leg.pos_side, "short")
        self.assertEqual(hedge_leg.action, "open")
        self.assertEqual(hedge_leg.position_mode, "long_short_mode")
        self.assertEqual(hedge_leg.execution_mode, "protective_overlay")

    def test_evaluate_protective_overlay_respects_min_hold_before_closing_hedge(self) -> None:
        settings = make_derivatives_hedge_settings(strategy_hedge_min_hold_seconds=300.0)
        context = make_context(
            current_position_qty=0.03,
            current_long_position_qty=0.05,
            current_short_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="long",
            current_short_leg_opened_seconds_ago=60,
            latest_short_leg_fill_seconds_ago=60,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.62,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.12,
                "trend_alpha": 0.10,
                "microstructure_alpha": 0.04,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"composite_alpha_score": 0.18})

        overlay_decision = evaluate_protective_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.08, confidence=0.64),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
        )

        self.assertIn("protective_overlay_min_hold_active", overlay_decision.blocked_reasons)
        self.assertGreater(overlay_decision.min_hold_remaining_seconds, 0.0)
        self.assertEqual(overlay_decision.hedge_leg_target_qty, Decimal("0.02"))
        self.assertIsNone(
            build_protective_candidate_leg(
                symbol=context.symbol,
                target_leverage=1.0,
                margin_mode=str(settings.margin_mode),
                overlay_decision=overlay_decision,
            )
        )

    def test_evaluate_protective_overlay_respects_dedicated_enabled_switch(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_protective_enabled=False,
            strategy_hedge_overlay_mode="protective",
        )
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        overlay_decision = evaluate_protective_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.12, confidence=0.83),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
        )

        self.assertEqual(overlay_decision.state, "blocked")
        self.assertIn("protective_overlay_not_enabled", overlay_decision.blocked_reasons)

    def test_evaluate_protective_overlay_respects_rebalance_cooldown_before_reopening(self) -> None:
        settings = make_derivatives_hedge_settings(strategy_hedge_rebalance_cooldown_seconds=120.0)
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        overlay_decision = evaluate_protective_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.12, confidence=0.83),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
        )

        self.assertIn("protective_overlay_rebalance_cooldown_active", overlay_decision.blocked_reasons)
        self.assertGreater(overlay_decision.rebalance_cooldown_remaining_seconds, 0.0)
        self.assertFalse(overlay_decision.active)

    def test_evaluate_protective_overlay_uses_context_as_of_ts_for_rebalance_cooldown(self) -> None:
        settings = make_derivatives_hedge_settings(strategy_hedge_rebalance_cooldown_seconds=120.0)
        replay_ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        context = make_context(
            as_of_ts=replay_ts,
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.22,
                "trend_alpha": 0.03,
                "microstructure_alpha": -0.21,
                "liquidity_scale": 0.9,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.40})

        overlay_decision = evaluate_protective_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.12, confidence=0.83),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
        )

        self.assertIn("protective_overlay_rebalance_cooldown_active", overlay_decision.blocked_reasons)
        self.assertGreater(overlay_decision.rebalance_cooldown_remaining_seconds, 0.0)

    def test_evaluate_protective_overlay_preserves_residual_inventory_when_target_turns_flat(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_min_hold_seconds=0.0,
            strategy_hedge_rebalance_cooldown_seconds=0.0,
        )
        context = make_context(
            current_position_qty=0.03,
            current_long_position_qty=0.05,
            current_short_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.60,
            suggested_position_scale=0.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.08,
                "trend_alpha": 0.06,
                "microstructure_alpha": 0.02,
                "liquidity_scale": 0.90,
            },
        ).model_copy(update={"composite_alpha_score": 0.12})

        overlay_decision = evaluate_protective_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.05, confidence=0.60),
            long_target_qty=Decimal("0"),
            short_target_qty=Decimal("0"),
        )
        hedge_leg = build_protective_candidate_leg(
            symbol=context.symbol,
            target_leverage=1.0,
            margin_mode=str(settings.margin_mode),
            overlay_decision=overlay_decision,
        )

        self.assertTrue(overlay_decision.active)
        self.assertEqual(overlay_decision.state, "closing")
        self.assertEqual(overlay_decision.main_leg_signal, "long")
        self.assertEqual(overlay_decision.hedge_leg_signal, "short")
        self.assertEqual(overlay_decision.main_leg_current_qty, Decimal("0.05"))
        self.assertEqual(overlay_decision.hedge_leg_current_qty, Decimal("0.02"))
        self.assertEqual(overlay_decision.main_leg_target_qty, Decimal("0"))
        self.assertEqual(overlay_decision.hedge_leg_target_qty, Decimal("0"))
        self.assertIn("protective_overlay_main_signal_inferred_from_inventory", overlay_decision.reason_codes)
        self.assertIsNotNone(hedge_leg)
        assert hedge_leg is not None
        self.assertEqual(hedge_leg.pos_side, "short")
        self.assertEqual(hedge_leg.action, "close")


if __name__ == "__main__":
    unittest.main()
