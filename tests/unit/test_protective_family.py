from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest

from aats.services.strategy_engines.families.protective_family import (
    _resolve_overlay_main_leg_contract,
    build_protective_candidate_leg,
    evaluate_protective_overlay_decision,
)
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
