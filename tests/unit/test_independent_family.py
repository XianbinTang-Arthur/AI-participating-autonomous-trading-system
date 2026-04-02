from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest

from aats.schemas.decision import PositionTarget
from aats.schemas.market import MarketSnapshot
from aats.services.strategy_engines.base import StrategyEvaluationContext, StrategyFamilyRuntimeControl
from aats.services.strategy_engines.families.independent_family import (
    IndependentBookExpectancy,
    IndependentFamilyEvaluation,
    IndependentBookEvaluation,
    _independent_family_action,
    evaluate_independent_books,
    independent_candidate_from_directional_target,
)
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


def _expectancy_resolver(*, leg: str, **_: object) -> IndependentBookExpectancy:
    if leg == "long":
        return IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.5,
            expected_cost_bps=6.0,
            expected_net_edge_bps=12.0,
        )
    return IndependentBookExpectancy(
        leg="short",
        expected_signal_edge_bps=4.0,
        expected_slippage_bps=1.5,
        expected_cost_bps=6.0,
        expected_net_edge_bps=-2.0,
    )


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
            expectancy_resolver=_expectancy_resolver,
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
            expectancy_resolver=_expectancy_resolver,
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
            expectancy_resolver=_expectancy_resolver,
        )

        self.assertIn(
            "independent_long_book_hold_above_close_threshold",
            result.overlay_decision.long_leg_reason_codes,
        )
        self.assertEqual(result.final_target_qty, Decimal("0.02"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_blocks_new_open_when_expectancy_resolution_fails(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
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
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.10,
            expectancy_resolver=lambda **_: (_ for _ in ()).throw(RuntimeError("cost_boom")),
        )

        self.assertEqual(result.long_book.book_action, "blocked")
        self.assertIn(
            "independent_long_book_expectancy_resolution_failed",
            result.long_book.blocked_reasons,
        )
        self.assertIsNone(result.long_book.expectancy)
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_blocks_new_open_when_expectancy_resolver_returns_invalid_shape(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
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
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.10,
            expectancy_resolver=lambda **_: SimpleNamespace(
                leg="long",
                expected_signal_edge_bps=18.0,
                expected_slippage_bps=1.5,
                expected_cost_bps=6.0,
                expected_net_edge_bps=12.0,
            ),
        )

        self.assertEqual(result.long_book.book_action, "blocked")
        self.assertIn(
            "independent_long_book_expectancy_resolution_failed",
            result.long_book.blocked_reasons,
        )
        self.assertIsNone(result.long_book.expectancy)
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_do_not_use_directional_fallback_edges_after_expectancy_failure(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.60,
            strategy_hedge_independent_long_close_threshold=0.50,
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
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
            signal_edge_bps=4.0,
            expected_cost_bps=6.0,
            expected_net_edge_bps=-2.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.55 if leg == "long" else 0.10,
            expectancy_resolver=lambda **_: (_ for _ in ()).throw(RuntimeError("cost_boom")),
        )

        self.assertIsNone(result.long_book.expectancy)
        self.assertEqual(result.long_book.close_reason, None)
        self.assertEqual(result.long_book.book_action, "hold")
        self.assertEqual(result.final_target_qty, Decimal("0.02"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_close_failed_thesis_when_expected_net_edge_turns_negative(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.60,
            strategy_hedge_independent_long_close_threshold=0.50,
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
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
                "momentum_alpha": 0.22,
                "trend_alpha": 0.18,
                "microstructure_alpha": 0.06,
                "liquidity_scale": 0.90,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.18})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.08, confidence=0.70),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=4.0,
            expected_cost_bps=6.0,
            expected_net_edge_bps=-2.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.42 if leg == "long" else 0.10,
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=4.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=6.0,
                expected_net_edge_bps=-2.0 if leg == "long" else -3.0,
            ),
        )

        self.assertEqual(result.long_book.book_action, "close_failed_thesis")
        self.assertEqual(result.long_book.close_reason, "failed_thesis")
        self.assertEqual(result.long_book.target_qty, Decimal("0"))
        self.assertEqual(result.overlay_decision.close_reason, "failed_thesis")
        self.assertEqual(result.legs[0].action, "close")
        self.assertEqual(result.legs[0].policy_reason, "independent_failed_thesis_force_exit")
        self.assertEqual(result.legs[0].execution_policy_urgency, "high")
        self.assertEqual(result.legs[0].order_type_preference, "market")

    def test_evaluate_independent_books_close_stale_thesis_when_position_ages_out(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.60,
            strategy_hedge_independent_long_close_threshold=0.50,
            strategy_hedge_independent_max_thesis_age_seconds=1800,
        )
        context = make_context(
            current_position_qty=0.02,
            current_long_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="long",
            current_long_leg_opened_seconds_ago=3600,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.30,
                "trend_alpha": 0.24,
                "microstructure_alpha": 0.10,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.22})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.15, confidence=0.76),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=8.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=4.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.58 if leg == "long" else 0.08,
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=8.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=4.0,
                expected_net_edge_bps=4.0 if leg == "long" else -3.0,
            ),
        )

        self.assertEqual(result.long_book.book_action, "close_stale_thesis")
        self.assertEqual(result.long_book.close_reason, "stale_thesis")
        self.assertEqual(result.overlay_decision.close_reason, "stale_thesis")
        self.assertEqual(result.legs[0].action, "close")
        self.assertEqual(result.legs[0].policy_reason, "independent_stale_thesis_guarded_exit")
        self.assertEqual(result.legs[0].order_type_preference, "limit")
        self.assertEqual(result.legs[0].time_in_force_preference, "IOC")

    def test_evaluate_independent_books_de_risk_when_edge_thins_but_thesis_not_failed(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.60,
            strategy_hedge_independent_long_close_threshold=0.50,
            strategy_hedge_independent_de_risk_net_edge_bps=2.0,
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
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
                "momentum_alpha": 0.28,
                "trend_alpha": 0.22,
                "microstructure_alpha": 0.08,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.20})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.12, confidence=0.74),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=6.0,
            expected_cost_bps=5.0,
            expected_net_edge_bps=1.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.58 if leg == "long" else 0.10,
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=6.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=5.0,
                expected_net_edge_bps=1.0 if leg == "long" else -3.0,
            ),
        )

        self.assertEqual(result.long_book.book_action, "de_risk")
        self.assertEqual(result.long_book.close_reason, "weak_edge_de_risk")
        self.assertEqual(result.long_book.target_qty, Decimal("0.01"))
        self.assertEqual(result.overlay_decision.close_reason, "weak_edge_de_risk")
        self.assertEqual(result.legs[0].action, "reduce")
        self.assertEqual(result.legs[0].policy_reason, "independent_weak_edge_guarded_reduce")
        self.assertEqual(result.legs[0].order_type_preference, "limit")

    def test_evaluate_independent_books_de_risk_when_execution_health_is_blocked(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_execution_health_de_risk_enabled=True,
            strategy_hedge_independent_trial_guard_enabled=True,
            strategy_performance_guard_min_closed_trades=4,
        )
        context = make_context(
            current_position_qty=0.02,
            current_long_position_qty=0.02,
            product_type="derivatives",
            current_exposure_side="long",
            current_long_leg_opened_seconds_ago=900,
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 5,
                    "recent_win_rate": 0.20,
                    "recent_fee_drag_ratio": 0.04,
                    "recent_churn_ratio": 0.08,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("-18"),
                }
            },
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.28,
                "trend_alpha": 0.22,
                "microstructure_alpha": 0.08,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.20})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.12, confidence=0.74),
            directional_target_qty=Decimal("0.02"),
            target_leverage=1.0,
            signal_edge_bps=8.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=4.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.58 if leg == "long" else 0.10,
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=8.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=4.0,
                expected_net_edge_bps=4.0 if leg == "long" else -3.0,
            ),
        )

        self.assertEqual(result.long_book.execution_health_state, "blocked")
        self.assertEqual(result.long_book.book_action, "de_risk")
        self.assertEqual(result.long_book.close_reason, "execution_health_degraded")
        self.assertEqual(result.long_book.target_qty, Decimal("0.01"))
        self.assertEqual(result.legs[0].policy_reason, "independent_execution_health_urgent_exit")
        self.assertEqual(result.legs[0].order_type_preference, "market")

    def test_independent_execution_policy_respects_configured_action_modes(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_entry_execution_mode="passive_first",
            strategy_hedge_independent_scale_in_execution_mode="bounded_limit",
            strategy_hedge_independent_de_risk_execution_mode="bounded_taker",
            strategy_hedge_independent_close_failed_thesis_execution_mode="aggressive_bounded_taker",
            strategy_hedge_independent_close_stale_execution_mode="bounded_limit",
            strategy_hedge_independent_limit_offset_bps_entry=1.5,
            strategy_hedge_independent_limit_offset_bps_scale_in=1.0,
            strategy_hedge_independent_limit_offset_bps_stale_close=0.8,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.84,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.30,
                "trend_alpha": 0.24,
                "microstructure_alpha": 0.10,
                "liquidity_scale": 0.92,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.22})

        open_result = evaluate_independent_books(
            settings=settings,
            context=make_context(product_type="derivatives", current_exposure_side="flat"),
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.18, confidence=0.76),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=12.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=8.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.72 if leg == "long" else 0.08,
            expectancy_resolver=_expectancy_resolver,
        )
        self.assertEqual(open_result.legs[0].policy_reason, "independent_entry_configured_passive_first")
        self.assertEqual(open_result.legs[0].execution_style_preference, "bounded_limit_ioc")
        self.assertEqual(open_result.legs[0].order_type_preference, "limit")
        self.assertEqual(open_result.legs[0].limit_offset_bps_preference, Decimal("1.5"))

        stale_result = evaluate_independent_books(
            settings=settings,
            context=make_context(
                current_position_qty=0.02,
                current_long_position_qty=0.02,
                product_type="derivatives",
                current_exposure_side="long",
                current_long_leg_opened_seconds_ago=3600,
            ),
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.15, confidence=0.76),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=8.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=4.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.58 if leg == "long" else 0.08,
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=8.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=4.0,
                expected_net_edge_bps=4.0 if leg == "long" else -3.0,
            ),
        )
        self.assertEqual(stale_result.legs[0].policy_reason, "independent_stale_thesis_configured_bounded_limit")
        self.assertEqual(stale_result.legs[0].order_type_preference, "limit")
        self.assertEqual(stale_result.legs[0].limit_offset_bps_preference, Decimal("0.8"))

        failed_result = evaluate_independent_books(
            settings=settings,
            context=make_context(
                current_position_qty=0.02,
                current_long_position_qty=0.02,
                product_type="derivatives",
                current_exposure_side="long",
                current_long_leg_opened_seconds_ago=900,
            ),
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.08, confidence=0.70),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=4.0,
            expected_cost_bps=6.0,
            expected_net_edge_bps=-2.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.42 if leg == "long" else 0.10,
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=4.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=6.0,
                expected_net_edge_bps=-2.0 if leg == "long" else -3.0,
            ),
        )
        self.assertEqual(
            failed_result.legs[0].policy_reason,
            "independent_failed_thesis_configured_aggressive_bounded_taker",
        )
        self.assertEqual(failed_result.legs[0].execution_style_preference, "aggressive_bounded_taker_cap")
        self.assertEqual(failed_result.legs[0].order_type_preference, "market")

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
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=10.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=7.0,
                expected_net_edge_bps=3.0 if leg == "long" else 1.0,
            ),
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
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=10.0,
                expected_slippage_bps=1.5,
                expected_cost_bps=7.5,
                expected_net_edge_bps=4.0 if leg == "long" else 1.0,
            ),
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
        self.assertEqual(long_leg.policy_reason, "independent_weak_edge_passive_first_required")
        self.assertEqual(long_leg.execution_policy_urgency, "low")
        self.assertEqual(long_leg.expected_leg_cost_bps, 7.5)

    def test_evaluate_independent_books_block_open_when_expected_cost_is_extreme_cost_anomaly(self) -> None:
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
            expected_cost_bps=12.5,
            expected_net_edge_bps=10.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
            expectancy_resolver=lambda *, leg, **kwargs: IndependentBookExpectancy(
                leg=leg,
                expected_signal_edge_bps=18.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=12.5,
                expected_net_edge_bps=10.0 if leg == "long" else 1.0,
                depth_consumption_ratio=0.95 if leg == "long" else 0.05,
                size_impact_bps=3.8 if leg == "long" else 0.4,
                cost_confidence=0.85,
            ),
        )

        self.assertIn(
            "independent_long_book_expected_cost_above_max_acceptable",
            result.overlay_decision.long_leg_blocked_reasons,
        )
        self.assertEqual(result.final_target_qty, Decimal("0"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_block_open_when_liquidity_quality_is_below_threshold(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_min_liquidity_quality=0.55,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.86,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.44,
                "trend_alpha": 0.40,
                "microstructure_alpha": 0.10,
                "liquidity_scale": 0.05,
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
            expected_cost_bps=4.0,
            expected_net_edge_bps=12.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
            expectancy_resolver=_expectancy_resolver,
        )

        self.assertIn(
            "independent_long_book_liquidity_quality_below_minimum",
            result.overlay_decision.long_leg_blocked_reasons,
        )
        self.assertIsNotNone(result.long_book.liquidity_quality_score)
        self.assertLess(result.long_book.liquidity_quality_score or 1.0, 0.55)
        self.assertEqual(result.final_target_qty, Decimal("0"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_block_open_when_recent_score_support_is_insufficient(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_liquidity_quality=0.30,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
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
            expected_cost_bps=4.0,
            expected_net_edge_bps=12.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
            expectancy_resolver=_expectancy_resolver,
            recent_score_history_by_leg={"long": (0.18,), "short": ()},
        )

        self.assertIn(
            "independent_long_book_score_support_below_min_confirm_ticks",
            result.overlay_decision.long_leg_blocked_reasons,
        )
        assert result.long_book.score_stability_metrics is not None
        self.assertEqual(result.long_book.score_stability_metrics.source, "recent_target_history")
        self.assertEqual(result.long_book.score_stability_metrics.support_count, 1)
        self.assertEqual(result.final_target_qty, Decimal("0"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_block_open_when_execution_health_is_degraded(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_min_liquidity_quality=0.30,
            strategy_hedge_independent_require_execution_health_ok=True,
            strategy_max_fee_drag_ratio=0.40,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="flat",
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 5,
                    "recent_win_rate": 0.55,
                    "recent_fee_drag_ratio": 0.31,
                    "recent_churn_ratio": 0.05,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("4"),
                },
                "short": {
                    "recent_closed_trade_count": 0,
                    "recent_win_rate": 0.0,
                    "recent_fee_drag_ratio": 0.0,
                    "recent_churn_ratio": 0.0,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("0"),
                },
            },
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
            expected_cost_bps=4.0,
            expected_net_edge_bps=12.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
            expectancy_resolver=_expectancy_resolver,
        )

        self.assertIn(
            "independent_long_book_execution_health_not_ok",
            result.overlay_decision.long_leg_blocked_reasons,
        )
        self.assertEqual(result.long_book.execution_health_state, "degraded")
        self.assertEqual(result.final_target_qty, Decimal("0"))
        self.assertFalse(result.legs)

    def test_evaluate_independent_books_allow_open_when_entry_quality_gate_passes(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_liquidity_quality=0.55,
            strategy_hedge_independent_require_execution_health_ok=True,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
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
            expected_cost_bps=4.0,
            expected_net_edge_bps=12.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
            expectancy_resolver=_expectancy_resolver,
        )

        self.assertEqual(len(result.legs), 1)
        self.assertEqual(result.legs[0].pos_side, "long")
        self.assertEqual(result.long_book.state, "opening")
        self.assertEqual(result.long_book.execution_health_state, "ok")
        assert result.long_book.score_stability_metrics is not None
        self.assertTrue(result.long_book.score_stability_metrics.stable)
        self.assertGreaterEqual(result.long_book.liquidity_quality_score or 0.0, 0.55)

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
            expectancy_resolver=(
                lambda *, leg, **_: IndependentBookExpectancy(
                    leg=leg,
                    expected_signal_edge_bps=18.0 if leg == "long" else 10.0,
                    expected_slippage_bps=1.5,
                    expected_cost_bps=6.0,
                    expected_net_edge_bps=12.0 if leg == "long" else 4.0,
                )
            ),
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

    def test_evaluate_independent_books_uses_book_scoped_expectancy_for_open_gates(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.5, "trend_alpha": 0.5, "microstructure_alpha": 0.2},
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.35})

        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.3, confidence=0.85),
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=18.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=14.0,
            execution_leg_family="independent",
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.74,
            expectancy_resolver=_expectancy_resolver,
        )

        self.assertEqual({leg.pos_side for leg in result.legs}, {"long"})
        self.assertEqual(result.long_book.expectancy.expected_net_edge_bps, 12.0)
        self.assertEqual(result.short_book.expectancy.expected_net_edge_bps, -2.0)
        self.assertIn(
            "independent_short_book_expected_net_edge_below_safe_threshold",
            result.short_book.blocked_reasons,
        )

    def test_independent_candidate_metrics_publish_book_scoped_expectancy(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_family_independent_enabled=True,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.5, "trend_alpha": 0.5, "microstructure_alpha": 0.2},
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.35})
        directional_target = PositionTarget(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0.01"),
            current_notional=Decimal("0"),
            target_notional=Decimal("0"),
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=datetime.now(timezone.utc),
            product_type="derivatives",
            current_exposure_side="flat",
            target_exposure_side="long",
            position_intent="open_long",
            target_leverage=1.0,
            margin_mode="cross",
            expected_signal_edge_bps=22.0,
            expected_cost_bps=5.0,
            expected_net_edge_bps=17.0,
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
            ai_assessment=make_ai_assessment(direction=0.3, confidence=0.85),
            family_runtime_controls={
                "independent": StrategyFamilyRuntimeControl(
                    enabled=True,
                    shadow_mode_enabled=False,
                    live_execution_enabled=True,
                )
            },
        )

        candidate = independent_candidate_from_directional_target(
            settings=settings,
            evaluation_context=evaluation_context,
        )

        self.assertEqual(candidate.metrics["expectancy_source"], "independent_book")
        self.assertIn("long_expected_signal_edge_bps", candidate.metrics)
        self.assertIn("short_expected_signal_edge_bps", candidate.metrics)
        self.assertIn("long_liquidity_quality_score", candidate.metrics)
        self.assertIn("short_liquidity_quality_score", candidate.metrics)
        self.assertIn("long_score_support_count", candidate.metrics)
        self.assertIn("short_score_support_count", candidate.metrics)
        self.assertIn("min_score_drawdown_bps", candidate.metrics)
        self.assertIn("effective_score_drawdown_threshold_bps", candidate.metrics)
        self.assertIn("long_score_stability_upward_excursion_bps", candidate.metrics)
        self.assertIn("long_score_stability_downward_drawdown_bps", candidate.metrics)
        self.assertNotIn("long_score_stability_max_drawdown_bps", candidate.metrics)
        self.assertNotIn("long_score_stability_max_drawdown_bps_compat_source", candidate.metrics)
        self.assertIn("short_score_stability_upward_excursion_bps", candidate.metrics)
        self.assertIn("short_score_stability_downward_drawdown_bps", candidate.metrics)
        self.assertNotIn("short_score_stability_max_drawdown_bps", candidate.metrics)
        self.assertNotIn("short_score_stability_max_drawdown_bps_compat_source", candidate.metrics)
        self.assertIn("long_execution_health_state", candidate.metrics)
        self.assertIn("short_execution_health_state", candidate.metrics)
        self.assertIn("family_health_overall_state", candidate.metrics)
        self.assertIn("long_threshold_snapshot", candidate.metrics)
        self.assertIn("short_threshold_snapshot", candidate.metrics)
        self.assertIn("long_replay_snapshot", candidate.metrics)
        self.assertIn("short_replay_snapshot", candidate.metrics)
        self.assertIn("score_drawdown_bps", candidate.metrics["long_threshold_snapshot"])
        self.assertIn("effective_score_drawdown_bps", candidate.metrics["long_threshold_snapshot"])
        self.assertIn("max_thesis_age_seconds", candidate.metrics)
        self.assertIn("de_risk_net_edge_bps", candidate.metrics)
        self.assertIn("failed_thesis_net_edge_bps", candidate.metrics)
        self.assertIn("long_book_action", candidate.metrics)
        self.assertIn("short_book_action", candidate.metrics)
        self.assertNotEqual(
            candidate.metrics["long_expected_net_edge_bps"],
            candidate.metrics["short_expected_net_edge_bps"],
        )
        assert candidate.book_expectancy_summary is not None
        self.assertEqual(candidate.book_expectancy_summary.source, "independent_book")
        self.assertEqual([item.leg for item in candidate.book_expectancy_summary.books], ["long", "short"])
        self.assertEqual(
            candidate.book_expectancy_summary.books[0].expected_gross_edge_bps,
            candidate.metrics["long_expected_signal_edge_bps"],
        )
        self.assertEqual(
            candidate.book_expectancy_summary.books[0].expected_cost_bps,
            candidate.metrics["long_expected_cost_bps"],
        )
        self.assertEqual(
            candidate.book_expectancy_summary.books[1].expected_net_edge_bps,
            candidate.metrics["short_expected_net_edge_bps"],
        )
        self.assertEqual(candidate.book_expectancy_summary.books[0].required_safe_net_edge_bps, 0.0)
        self.assertEqual(candidate.book_expectancy_summary.books[0].max_acceptable_cost_bps, 0.0)
        self.assertEqual(
            candidate.book_expectancy_summary.books[0].weak_edge_execution_mode,
            settings.strategy_hedge_independent_weak_edge_execution_mode,
        )
        self.assertFalse(candidate.book_expectancy_summary.books[0].passive_first_required)
        self.assertEqual(
            candidate.book_expectancy_summary.books[0].book_action,
            candidate.metrics["long_book_action"],
        )
        self.assertEqual(
            candidate.book_expectancy_summary.books[0].policy_reason,
            candidate.metrics["long_execution_policy_reason"],
        )
        self.assertEqual(
            candidate.book_expectancy_summary.books[0].execution_policy_urgency,
            candidate.metrics["long_execution_policy_urgency"],
        )
        self.assertEqual(
            candidate.book_expectancy_summary.books[0].order_type_preference,
            candidate.metrics["long_order_type_preference"],
        )
        self.assertEqual([item.leg for item in candidate.book_runtime_states], ["long", "short"])
        self.assertEqual(
            candidate.book_runtime_states[0].book_action,
            candidate.metrics["long_book_action"],
        )
        self.assertEqual(
            candidate.book_runtime_states[1].book_action,
            candidate.metrics["short_book_action"],
        )
        self.assertEqual(
            candidate.book_runtime_states[0].expected_net_edge_bps,
            candidate.metrics["long_expected_net_edge_bps"],
        )
        self.assertEqual(
            candidate.book_runtime_states[1].expected_net_edge_bps,
            candidate.metrics["short_expected_net_edge_bps"],
        )
        self.assertEqual(
            candidate.book_runtime_states[0].leg_health_summary.health_state,
            candidate.metrics["long_execution_health_state"],
        )
        self.assertEqual(
            candidate.book_runtime_states[0].threshold_snapshot.entry_threshold,
            candidate.metrics["long_threshold_snapshot"]["entry_threshold"],
        )
        self.assertEqual(
            candidate.book_runtime_states[0].threshold_snapshot.effective_score_drawdown_bps,
            candidate.metrics["long_threshold_snapshot"]["effective_score_drawdown_bps"],
        )
        for state in candidate.book_runtime_states:
            if state.book_action in {"inactive", "hold", "blocked"}:
                self.assertIsNone(state.execution_chain_id)
            else:
                self.assertTrue(state.execution_chain_id)

    def test_independent_candidate_uses_directional_target_margin_mode_for_costs_and_legs(self) -> None:
        settings = make_derivatives_hedge_settings(
            margin_mode="cross",
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_family_independent_enabled=True,
            strategy_family_independent_live_execution_enabled=True,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.5, "trend_alpha": 0.5, "microstructure_alpha": 0.2},
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.35})
        directional_target = PositionTarget(
            decision_id="decision_target_test",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0.01"),
            current_notional=Decimal("0"),
            target_notional=Decimal("0"),
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=datetime.now(timezone.utc),
            product_type="derivatives",
            current_exposure_side="flat",
            target_exposure_side="long",
            position_intent="open_long",
            target_leverage=1.0,
            margin_mode="isolated",
            expected_signal_edge_bps=22.0,
            expected_cost_bps=5.0,
            expected_net_edge_bps=17.0,
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
            ai_assessment=make_ai_assessment(direction=0.3, confidence=0.85),
            family_runtime_controls={
                "independent": StrategyFamilyRuntimeControl(
                    enabled=True,
                    shadow_mode_enabled=False,
                    live_execution_enabled=True,
                )
            },
        )

        class FakeTradeCostService:
            def __init__(self) -> None:
                self.margin_modes: list[str] = []

            def estimate_single_leg_entry(self, **kwargs: object) -> object:
                self.margin_modes.append(str(kwargs["margin_mode"]))
                return SimpleNamespace(executable_total_drag_bps=Decimal("4.0"))

        trade_cost_service = FakeTradeCostService()

        independent_candidate_from_directional_target(
            settings=settings,
            evaluation_context=evaluation_context,
            trade_cost_service=trade_cost_service,  # type: ignore[arg-type]
        )

        self.assertEqual(trade_cost_service.margin_modes, ["isolated", "isolated"])
        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.3, confidence=0.85),
            runtime_margin_mode="isolated",
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=22.0,
            expected_cost_bps=5.0,
            expected_net_edge_bps=17.0,
            execution_leg_family="independent",
            trade_cost_service=trade_cost_service,  # type: ignore[arg-type]
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
            expectancy_resolver=_expectancy_resolver,
        )

        self.assertTrue(result.legs)
        self.assertTrue(all(leg.margin_mode == "isolated" for leg in result.legs))

    def test_evaluate_independent_books_propagates_size_aware_cost_diagnostics(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
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
        market_snapshot = MarketSnapshot(
            symbol="BTC-USDT-SWAP",
            exchange="OKX",
            snapshot_ts=datetime.now(timezone.utc),
            best_bid=Decimal("100"),
            best_ask=Decimal("101"),
            last_price=Decimal("100.5"),
            bid_size=Decimal("1.0"),
            ask_size=Decimal("1.0"),
            volume_24h=Decimal("1000000"),
            kline_15m={"open": Decimal("99"), "high": Decimal("102"), "low": Decimal("98"), "close": Decimal("100.5")},
            kline_1h={"open": Decimal("97"), "high": Decimal("103"), "low": Decimal("96"), "close": Decimal("100.5")},
            orderbook_depth={
                "bids": [{"price": Decimal("100"), "size": Decimal("1.0")}],
                "asks": [{"price": Decimal("101"), "size": Decimal("1.0")}],
            },
        )

        class FakeTradeCostService:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def estimate_single_leg_entry(self, **kwargs: object) -> object:
                self.calls.append(dict(kwargs))
                return SimpleNamespace(
                    executable_total_drag_bps=Decimal("8.6"),
                    executable_slippage_bps=Decimal("7.0"),
                    execution_context={
                        "size_impact_bps": Decimal("1.4"),
                        "projected_notional": kwargs["projected_notional"],
                        "reference_price": kwargs["reference_price"],
                        "quoted_depth_notional": Decimal("400"),
                        "depth_consumption_ratio": Decimal("0.3"),
                    },
                    execution_drag_components_bps={"size_impact_bps": Decimal("1.4")},
                    cost_confidence=0.82,
                )

        trade_cost_service = FakeTradeCostService()
        result = evaluate_independent_books(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.24, confidence=0.82),
            latest_market_snapshot=market_snapshot,
            directional_target_qty=Decimal("0.01"),
            target_leverage=1.0,
            signal_edge_bps=18.0,
            expected_cost_bps=6.0,
            expected_net_edge_bps=12.0,
            execution_leg_family="independent",
            trade_cost_service=trade_cost_service,  # type: ignore[arg-type]
            scorer=lambda *, leg, baseline, ai_assessment: 0.78 if leg == "long" else 0.08,
            expectancy_resolver=None,
        )

        self.assertEqual(len(trade_cost_service.calls), 2)
        self.assertIs(trade_cost_service.calls[0]["market_snapshot"], market_snapshot)
        self.assertGreater(Decimal(str(trade_cost_service.calls[0]["quantity"])), Decimal("0"))
        self.assertGreater(Decimal(str(trade_cost_service.calls[0]["projected_notional"])), Decimal("0"))
        self.assertIsNotNone(result.long_book.expectancy)
        self.assertAlmostEqual(result.long_book.expectancy.expected_slippage_bps, 8.4, places=6)
        self.assertAlmostEqual(float(result.long_book.expectancy.depth_consumption_ratio or 0.0), 0.3, places=6)
        self.assertAlmostEqual(result.long_book.expectancy.size_impact_bps, 1.4, places=6)
        self.assertAlmostEqual(float(result.long_book.expectancy.cost_confidence or 0.0), 0.82, places=6)
        self.assertEqual(result.long_book.expectancy.reference_price, Decimal("100.5"))

    def test_independent_family_action_reports_mixed_rebalance_when_opening_and_closing_coexist(self) -> None:
        result = IndependentFamilyEvaluation(
            final_target_qty=Decimal("0"),
            legs=[],
            overlay_decision=None,  # type: ignore[arg-type]
            long_book=IndependentBookEvaluation(
                leg="long",
                expectancy=_expectancy_resolver(leg="long"),
                score=0.72,
                current_qty=Decimal("0.01"),
                target_qty=Decimal("0"),
                state="closing",
                reason_codes=[],
                blocked_reasons=[],
                min_hold_remaining_seconds=0.0,
                rebalance_cooldown_remaining_seconds=0.0,
                book_action="close_failed_thesis",
                close_reason="failed_thesis",
            ),
            short_book=IndependentBookEvaluation(
                leg="short",
                expectancy=_expectancy_resolver(leg="short"),
                score=0.78,
                current_qty=Decimal("0"),
                target_qty=Decimal("0.01"),
                state="opening",
                reason_codes=[],
                blocked_reasons=[],
                min_hold_remaining_seconds=0.0,
                rebalance_cooldown_remaining_seconds=0.0,
                book_action="open",
            ),
        )

        self.assertEqual(_independent_family_action(result=result), "rebalance_independent_books")


if __name__ == "__main__":
    unittest.main()
