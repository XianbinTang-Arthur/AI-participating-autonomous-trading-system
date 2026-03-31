from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from aats.services.strategy_engines.families.opportunistic_family import (
    build_opportunistic_candidate_leg,
    evaluate_opportunistic_overlay_decision,
)
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestOpportunisticFamily(unittest.TestCase):
    def test_evaluate_opportunistic_overlay_opens_short_opportunity_leg_against_existing_long(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_open_threshold=0.62,
            strategy_hedge_opportunistic_close_threshold=0.46,
        )
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.25, confidence=0.80),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.82,
        )
        hedge_leg = build_opportunistic_candidate_leg(
            symbol=context.symbol,
            target_leverage=1.0,
            margin_mode=str(settings.margin_mode),
            overlay_decision=overlay_decision,
        )

        self.assertEqual(overlay_decision.effective_mode, "opportunistic")
        self.assertTrue(overlay_decision.active)
        self.assertEqual(overlay_decision.state, "opening")
        self.assertIsNotNone(hedge_leg)
        assert hedge_leg is not None
        self.assertEqual(hedge_leg.pos_side, "short")
        self.assertEqual(hedge_leg.action, "open")
        self.assertEqual(hedge_leg.execution_mode, "opportunistic_overlay")
        self.assertEqual(hedge_leg.overlay_mode, "opportunistic")

    def test_evaluate_opportunistic_overlay_blocks_new_leg_when_fee_drag_is_too_high(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_max_fee_drag_ratio=0.18,
            strategy_performance_guard_min_closed_trades=4,
        )
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
            recent_closed_trade_count=6,
            recent_fee_drag_ratio=0.30,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.25, confidence=0.80),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.82,
        )

        self.assertIn("opportunistic_overlay_fee_drag_guard_active", overlay_decision.blocked_reasons)
        self.assertEqual(overlay_decision.hedge_leg_target_qty, Decimal("0"))
        self.assertFalse(overlay_decision.active)

    def test_evaluate_opportunistic_overlay_respects_min_hold_before_closing_leg(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_min_hold_seconds=180.0,
        )
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
            confidence=0.72,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.16,
                "trend_alpha": 0.12,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.24})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.12, confidence=0.70),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.10,
        )

        self.assertIn("opportunistic_overlay_min_hold_active", overlay_decision.blocked_reasons)
        self.assertGreater(overlay_decision.min_hold_remaining_seconds, 0.0)
        self.assertEqual(overlay_decision.hedge_leg_target_qty, Decimal("0.02"))
        self.assertIsNone(
            build_opportunistic_candidate_leg(
                symbol=context.symbol,
                target_leverage=1.0,
                margin_mode=str(settings.margin_mode),
                overlay_decision=overlay_decision,
            )
        )

    def test_evaluate_opportunistic_overlay_uses_context_as_of_ts_for_min_hold(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_min_hold_seconds=180.0,
        )
        replay_ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        context = make_context(
            as_of_ts=replay_ts,
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
            confidence=0.72,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.16,
                "trend_alpha": 0.12,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.24})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.12, confidence=0.70),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.10,
        )

        self.assertIn("opportunistic_overlay_min_hold_active", overlay_decision.blocked_reasons)
        self.assertGreater(overlay_decision.min_hold_remaining_seconds, 0.0)

    def test_evaluate_opportunistic_overlay_respects_rebalance_cooldown_before_reopening(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_rebalance_cooldown_seconds=90.0,
        )
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.25, confidence=0.80),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.82,
        )

        self.assertIn("opportunistic_overlay_rebalance_cooldown_active", overlay_decision.blocked_reasons)
        self.assertGreater(overlay_decision.rebalance_cooldown_remaining_seconds, 0.0)
        self.assertFalse(overlay_decision.active)

    def test_evaluate_opportunistic_overlay_returns_no_existing_inventory_during_reversal_handover(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
        )
        context = make_context(
            current_position_qty=-0.05,
            current_short_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="short",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.90,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.25, confidence=0.80),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.20,
        )

        self.assertEqual(overlay_decision.state, "inactive")
        self.assertFalse(overlay_decision.active)
        self.assertEqual(overlay_decision.hedge_leg_current_qty, Decimal("0"))
        self.assertEqual(overlay_decision.reason_codes, ["opportunistic_overlay_no_existing_inventory"])
        self.assertIsNone(
            build_opportunistic_candidate_leg(
                symbol=context.symbol,
                target_leverage=1.0,
                margin_mode=str(settings.margin_mode),
                overlay_decision=overlay_decision,
            )
        )

    def test_evaluate_opportunistic_overlay_preserves_residual_inventory_when_target_turns_flat(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_min_hold_seconds=0.0,
            strategy_hedge_opportunistic_rebalance_cooldown_seconds=0.0,
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
            confidence=0.70,
            suggested_position_scale=0.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.12,
                "trend_alpha": 0.10,
                "microstructure_alpha": 0.10,
                "liquidity_scale": 0.90,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": 0.10})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.05, confidence=0.60),
            long_target_qty=Decimal("0"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.10,
        )
        hedge_leg = build_opportunistic_candidate_leg(
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
        self.assertIn("opportunistic_overlay_main_signal_inferred_from_inventory", overlay_decision.reason_codes)
        self.assertIsNotNone(hedge_leg)
        assert hedge_leg is not None
        self.assertEqual(hedge_leg.pos_side, "short")
        self.assertEqual(hedge_leg.action, "close")

    def test_evaluate_opportunistic_overlay_blocks_live_runtime_before_rollout_stage_is_live(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_rollout_stage="dry_run",
            guarded_execution_dry_run=False,
            live_submit_enabled=True,
            okx_simulated_trading=False,
        )
        context = make_context(
            current_position_qty=0.05,
            current_long_position_qty=0.05,
            product_type="derivatives",
            current_exposure_side="long",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            volatility_state="high",
            factor_scores={
                "momentum_alpha": -0.75,
                "trend_alpha": 0.05,
                "microstructure_alpha": -0.95,
                "liquidity_scale": 0.88,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": 0.28})

        overlay_decision = evaluate_opportunistic_overlay_decision(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.25, confidence=0.80),
            long_target_qty=Decimal("0.05"),
            short_target_qty=Decimal("0"),
            scorer=lambda **_: 0.82,
        )

        self.assertEqual(overlay_decision.state, "blocked")
        self.assertEqual(overlay_decision.rollout_stage, "dry_run")
        self.assertEqual(overlay_decision.runtime_rollout_stage, "live")
        self.assertIn(
            "opportunistic_overlay_rollout_stage_blocks_live_runtime",
            overlay_decision.blocked_reasons,
        )


if __name__ == "__main__":
    unittest.main()
