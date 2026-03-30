from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from aats.services.strategy_engines.families.independent_family import evaluate_independent_books
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentFamily(unittest.TestCase):
    def test_evaluate_independent_books_allow_long_reentry_while_short_book_is_still_cooling_down(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_rebalance_cooldown_seconds=120.0,
            strategy_post_close_cooldown_seconds=300.0,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="flat",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.48,
                "trend_alpha": 0.42,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.25, confidence=0.82),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.75,
        )

        self.assertIn(
            "independent_short_book_post_close_cooldown_active",
            result.overlay_decision.short_leg_blocked_reasons,
        )
        self.assertEqual(len(result.legs), 1)
        self.assertEqual(result.legs[0].pos_side, "long")
        self.assertEqual(result.legs[0].action, "open")
        self.assertEqual(result.legs[0].execution_mode, "independent_long_book")

    def test_evaluate_independent_books_use_context_as_of_ts_for_leg_cooldowns(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_rebalance_cooldown_seconds=120.0,
            strategy_post_close_cooldown_seconds=300.0,
        )
        replay_ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        context = make_context(
            as_of_ts=replay_ts,
            product_type="derivatives",
            current_exposure_side="flat",
            last_short_leg_closed_seconds_ago=30,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.48,
                "trend_alpha": 0.42,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.25, confidence=0.82),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.75,
        )

        self.assertIn(
            "independent_short_book_post_close_cooldown_active",
            result.overlay_decision.short_leg_blocked_reasons,
        )
        self.assertGreater(result.overlay_decision.rebalance_cooldown_remaining_seconds, 0.0)

    def test_evaluate_independent_books_use_close_hysteresis_before_exit(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.60,
            strategy_hedge_independent_long_close_threshold=0.50,
            strategy_hedge_independent_long_scale_in_threshold=0.72,
        )
        context = make_context(
            current_position_qty=0.02,
            current_long_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="long",
            current_long_leg_opened_seconds_ago=900,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.32,
                "trend_alpha": 0.28,
                "microstructure_alpha": 0.12,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.24})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.20, confidence=0.78),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.55 if leg == "long" else 0.10,
        )

        self.assertIn(
            "independent_long_book_hold_above_close_threshold",
            result.overlay_decision.long_leg_reason_codes,
        )
        self.assertEqual(result.final_target_qty, Decimal("0.02"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_block_open_when_expected_net_edge_is_below_safe_threshold(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=1.0,
        )
        context = make_context(
            current_position_qty=0.0,
            current_long_position_qty=0.0,
            product_type="derivatives",
            current_exposure_side="flat",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.86,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.44,
                "trend_alpha": 0.40,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.24, confidence=0.82),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=10.0,
            expected_cost_bps=7.0,
            expected_net_edge_bps=3.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
        )

        self.assertIn(
            "independent_long_book_expected_net_edge_below_safe_threshold",
            result.overlay_decision.long_leg_blocked_reasons,
        )
        self.assertEqual(result.final_target_qty, Decimal("0"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_mark_weak_edge_with_passive_first_preferences(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.5,
            strategy_hedge_independent_expected_execution_buffer_bps=1.0,
            strategy_hedge_independent_weak_edge_execution_mode="report_only",
            strategy_hedge_independent_passive_first_enabled=True,
        )
        context = make_context(
            current_position_qty=0.0,
            current_long_position_qty=0.0,
            product_type="derivatives",
            current_exposure_side="flat",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.86,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.44,
                "trend_alpha": 0.40,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.24, confidence=0.82),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=10.0,
            expected_cost_bps=7.5,
            expected_net_edge_bps=4.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
        )

        self.assertIn(
            "independent_long_book_expected_net_edge_below_safe_threshold_report_only",
            result.overlay_decision.long_leg_reason_codes,
        )
        self.assertEqual(len(result.legs), 1)
        long_leg = result.legs[0]
        self.assertEqual(long_leg.execution_style_preference, "bounded_limit_ioc")
        self.assertEqual(long_leg.order_type_preference, "limit")
        self.assertEqual(long_leg.time_in_force_preference, "IOC")
        self.assertEqual(long_leg.limit_offset_bps_preference, Decimal("1.5"))
        self.assertIn(
            "independent_weak_edge_passive_first_required",
            long_leg.execution_preference_reason_codes,
        )

    def test_evaluate_independent_books_block_open_when_expected_cost_is_too_high(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_max_acceptable_cost_bps=6.0,
        )
        context = make_context(
            current_position_qty=0.0,
            current_long_position_qty=0.0,
            product_type="derivatives",
            current_exposure_side="flat",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.86,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.44,
                "trend_alpha": 0.40,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.24, confidence=0.82),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=18.0,
            expected_cost_bps=7.0,
            expected_net_edge_bps=10.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
        )

        self.assertIn(
            "independent_long_book_expected_cost_above_max_acceptable",
            result.overlay_decision.long_leg_blocked_reasons,
        )
        self.assertEqual(result.final_target_qty, Decimal("0"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_keep_short_book_when_long_book_trial_guard_is_bad(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_trial_guard_enabled=True,
            strategy_performance_guard_min_closed_trades=4,
        )
        context = make_context(
            current_position_qty=-0.02,
            current_short_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="short",
            current_short_leg_opened_seconds_ago=900,
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 5,
                    "recent_win_rate": 0.20,
                    "recent_fee_drag_ratio": 0.04,
                    "recent_churn_ratio": 0.08,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("-18"),
                },
                "short": {
                    "recent_closed_trade_count": 5,
                    "recent_win_rate": 0.80,
                    "recent_fee_drag_ratio": 0.03,
                    "recent_churn_ratio": 0.05,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("12"),
                },
            },
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.42,
                "trend_alpha": -0.38,
                "microstructure_alpha": -0.16,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "uncertain", "composite_alpha_score": -0.28})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.22, confidence=0.80),
            directional_target_qty=Decimal("-0.02"),
            target_leverage=1.0,
            signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.76 if leg == "long" else 0.72,
        )

        self.assertIn(
            "independent_long_book_trial_guard_active",
            result.overlay_decision.long_leg_blocked_reasons,
        )
        self.assertIn(
            "independent_short_book_hold_above_entry_threshold",
            result.overlay_decision.short_leg_reason_codes,
        )
        self.assertEqual(result.final_target_qty, Decimal("-0.02"))

    def test_evaluate_independent_books_block_live_runtime_before_rollout_stage_is_live(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_rollout_stage="dry_run",
            guarded_execution_dry_run=False,
            live_submit_enabled=True,
            okx_simulated_trading=False,
        )
        context = make_context(
            current_position_qty=0.0,
            current_long_position_qty=0.0,
            product_type="derivatives",
            current_exposure_side="flat",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.42,
                "trend_alpha": 0.38,
                "microstructure_alpha": 0.16,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.28})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.22, confidence=0.80),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
            execution_leg_family="independent",
        )

        self.assertEqual(result.overlay_decision.effective_mode, "independent")
        self.assertEqual(result.overlay_decision.rollout_stage, "dry_run")
        self.assertEqual(result.overlay_decision.runtime_rollout_stage, "live")
        self.assertIn(
            "independent_overlay_rollout_stage_blocks_live_runtime",
            result.overlay_decision.blocked_reasons,
        )
        self.assertFalse(result.legs)
        self.assertEqual(result.final_target_qty, Decimal("0.01"))

    def test_evaluate_independent_books_disabled_falls_back_to_directional_target(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=False,
        )
        context = make_context(
            current_position_qty=0.0,
            current_long_position_qty=0.0,
            product_type="derivatives",
            current_exposure_side="flat",
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.42,
                "trend_alpha": 0.38,
                "microstructure_alpha": 0.16,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.28})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.22, confidence=0.80),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
            execution_leg_family="independent",
        )

        self.assertEqual(result.overlay_decision.state, "blocked")
        self.assertIn("independent_books_not_enabled", result.overlay_decision.blocked_reasons)
        self.assertFalse(result.legs)
        self.assertEqual(result.final_target_qty, Decimal("0.01"))


if __name__ == "__main__":
    unittest.main()
