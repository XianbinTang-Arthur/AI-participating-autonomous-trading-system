from __future__ import annotations

from datetime import timedelta
import unittest
from decimal import Decimal

from aats.schemas.strategy_runtime import StrategyBookRuntimeState
from aats.services.strategy_engines.families.independent_family import _evaluate_independent_book
from aats.services.strategy_engines.independent.diagnostics import runtime_state_from_decision
from aats.services.strategy_engines.independent.engine import (
    _execution_health_state,
    _trial_guard_active,
    evaluate_independent_book,
)
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentEngine(unittest.TestCase):
    def test_evaluate_independent_book_matches_adapter_outputs_on_representative_fixture(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_passive_first_enabled=False,
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
        ai_assessment = make_ai_assessment(direction=0.25, confidence=0.82)
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.5,
            expected_cost_bps=6.0,
            expected_net_edge_bps=12.0,
        )

        extracted = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=None,
            recent_score_history=(),
        )
        legacy = _evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=None,
            recent_score_history=(),
        )

        self.assertEqual(extracted.state, legacy.state)
        self.assertEqual(extracted.book_action, legacy.book_action)
        self.assertEqual(extracted.close_reason, legacy.close_reason)
        self.assertEqual(extracted.blocked_reasons, legacy.blocked_reasons)
        self.assertEqual(extracted.policy_reason, legacy.policy_reason)
        self.assertEqual(extracted.score_adjusted, extracted.score)
        self.assertIsNotNone(extracted.threshold_snapshot)
        self.assertIsNotNone(extracted.health_snapshot)
        self.assertIsNotNone(extracted.replay_snapshot)

        runtime_state = runtime_state_from_decision(
            context=context,
            decision=extracted,
            threshold_snapshot=extracted.threshold_snapshot,
            health_snapshot=extracted.health_snapshot,
        )
        self.assertEqual(runtime_state.score_raw, extracted.score)
        self.assertEqual(runtime_state.score_adjusted, extracted.score)
        self.assertEqual(runtime_state.book_state, "flat")
        self.assertIsNone(runtime_state.guard_state)
        self.assertIsNone(runtime_state.holding_phase)
        self.assertIsNotNone(runtime_state.threshold_snapshot)
        self.assertIsNotNone(runtime_state.leg_health_summary)
        self.assertIsNotNone(runtime_state.threshold_snapshot.adaptive_entry_threshold)
        self.assertIsNotNone(runtime_state.threshold_snapshot.score_drawdown_bps)
        self.assertIsNotNone(runtime_state.threshold_snapshot.effective_score_drawdown_bps)
        self.assertIsNotNone(runtime_state.threshold_snapshot.capital_multiplier)
        self.assertTrue(runtime_state.threshold_snapshot.reason_codes)

    def test_evaluate_independent_book_uses_balance_aware_entry_size_when_directional_target_is_zero(self) -> None:
        settings = make_derivatives_hedge_settings(
            default_order_qty=0.004,
            default_target_leverage=5.0,
            max_target_leverage=10.0,
            max_margin_usage_fraction=0.75,
            strategy_dynamic_leverage_enabled=False,
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_min_score_stability_bps=0.0,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="flat",
            market_last_price=100000.0,
            available_trading_equity=390.0,
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
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.5,
            expected_cost_bps=6.0,
            expected_net_edge_bps=12.0,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0"),
            scorer=lambda **_: 0.99,
            recent_score_history=(0.99, 0.99, 0.99),
        )

        self.assertEqual(decision.target_qty, Decimal("0.014625"))
        self.assertGreater(decision.target_qty, Decimal("0.004"))

    def test_evaluate_independent_book_inherits_prior_runtime_state_for_counts_and_transition(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
            strategy_hedge_independent_min_score_stability_bps=0.0,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="long",
            current_long_position_qty=Decimal("0.01"),
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.92,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.58,
                "trend_alpha": 0.44,
                "microstructure_alpha": 0.22,
                "liquidity_scale": 0.97,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.41})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=19.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.5,
            expected_net_edge_bps=13.5,
        )
        prior_runtime_state = StrategyBookRuntimeState(
            leg="long",
            current_qty=Decimal("0.01"),
            target_qty=Decimal("0.01"),
            state="holding",
            book_state="holding",
            holding_phase="steady",
            current_scale_in_count=2,
            current_de_risk_count=1,
            prior_book_state="building",
            last_transition_reason="independent_scale_in",
            state_version=6,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.03"),
            scorer=lambda **_: 0.98,
            prior_runtime_state=prior_runtime_state,
            recent_score_history=(0.98, 0.98, 0.98),
        )

        self.assertEqual(decision.prior_book_state, "holding")
        self.assertEqual(decision.current_scale_in_count, 3)
        self.assertEqual(decision.current_de_risk_count, 1)
        self.assertEqual(decision.state_version, 7)
        self.assertIsNotNone(decision.state_snapshot)
        self.assertEqual(decision.state_snapshot.prior_book_state, "holding")
        self.assertTrue(decision.state_snapshot.transition_valid)

    def test_evaluate_independent_book_reopens_when_stale_guard_state_has_no_active_marker(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
            strategy_hedge_independent_min_score_stability_bps=0.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.55,
                "trend_alpha": 0.41,
                "microstructure_alpha": 0.2,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.4})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.0,
            expected_net_edge_bps=13.0,
        )
        for representation in ("legacy", "separated"):
            for prior_guard_state in ("cooldown", "suspended"):
                with self.subTest(representation=representation, prior_guard_state=prior_guard_state):
                    prior_runtime_state = StrategyBookRuntimeState(
                        leg="long",
                        current_qty=Decimal("0"),
                        target_qty=Decimal("0"),
                        state="blocked",
                        book_state="flat" if representation == "separated" else prior_guard_state,
                        guard_state=None if representation == "legacy" else prior_guard_state,
                        holding_phase=None,
                        prior_book_state="holding",
                        state_version=3,
                    )

                    decision = evaluate_independent_book(
                        settings=settings,
                        context=context,
                        baseline=baseline,
                        ai_assessment=None,
                        leg="long",
                        expectancy=expectancy,
                        directional_leg_target_qty=Decimal("0.01"),
                        scorer=lambda **_: 0.99,
                        prior_runtime_state=prior_runtime_state,
                        recent_score_history=(0.99, 0.99, 0.99),
                    )

                    runtime_state = runtime_state_from_decision(
                        context=context,
                        decision=decision,
                        threshold_snapshot=decision.threshold_snapshot,
                        health_snapshot=decision.health_snapshot,
                    )

                    self.assertEqual(decision.state, "opening")
                    self.assertEqual(decision.book_action, "open")
                    self.assertEqual(decision.target_qty, Decimal("0.01"))
                    self.assertNotIn("independent_state_transition_invalid", decision.reason_codes)
                    self.assertNotIn(f"independent_transition_invalid:{prior_guard_state}->probing", decision.blocked_reasons)
                    self.assertIsNotNone(decision.state_snapshot)
                    self.assertEqual(decision.state_snapshot.book_state, "probing")
                    self.assertIsNone(decision.state_snapshot.guard_state)
                    self.assertEqual(decision.state_snapshot.prior_book_state, "flat")
                    self.assertEqual(decision.state_snapshot.prior_guard_state, prior_guard_state)
                    self.assertTrue(decision.state_snapshot.transition_valid)
                    self.assertIsNone(decision.state_snapshot.transition_violation_reason)
                    self.assertEqual(runtime_state.book_state, "probing")
                    self.assertIsNone(runtime_state.guard_state)
                    self.assertEqual(runtime_state.prior_book_state, "flat")
                    self.assertEqual(runtime_state.prior_guard_state, prior_guard_state)

    def test_evaluate_independent_book_scale_in_recovers_from_stale_guard_state_with_inventory(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
            strategy_hedge_independent_min_score_stability_bps=0.0,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="long",
            current_long_position_qty=Decimal("0.01"),
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.92,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.58,
                "trend_alpha": 0.44,
                "microstructure_alpha": 0.22,
                "liquidity_scale": 0.97,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.41})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=19.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.5,
            expected_net_edge_bps=13.5,
        )
        for representation in ("legacy", "separated"):
            for prior_guard_state in ("cooldown", "suspended"):
                with self.subTest(representation=representation, prior_guard_state=prior_guard_state):
                    prior_runtime_state = StrategyBookRuntimeState(
                        leg="long",
                        current_qty=Decimal("0.01"),
                        target_qty=Decimal("0.01"),
                        state="blocked",
                        book_state="holding" if representation == "separated" else prior_guard_state,
                        guard_state=None if representation == "legacy" else prior_guard_state,
                        holding_phase=None,
                        prior_book_state="holding",
                        state_version=6,
                    )

                    decision = evaluate_independent_book(
                        settings=settings,
                        context=context,
                        baseline=baseline,
                        ai_assessment=None,
                        leg="long",
                        expectancy=expectancy,
                        directional_leg_target_qty=Decimal("0.03"),
                        scorer=lambda **_: 0.98,
                        prior_runtime_state=prior_runtime_state,
                        recent_score_history=(0.98, 0.98, 0.98),
                    )

                    runtime_state = runtime_state_from_decision(
                        context=context,
                        decision=decision,
                        threshold_snapshot=decision.threshold_snapshot,
                        health_snapshot=decision.health_snapshot,
                    )

                    self.assertEqual(decision.state, "opening")
                    self.assertEqual(decision.book_action, "scale_in")
                    self.assertGreater(decision.target_qty, Decimal("0.01"))
                    self.assertNotIn("independent_state_transition_invalid", decision.reason_codes)
                    self.assertNotIn(f"independent_transition_invalid:{prior_guard_state}->building", decision.blocked_reasons)
                    self.assertIsNotNone(decision.state_snapshot)
                    self.assertEqual(decision.state_snapshot.book_state, "building")
                    self.assertIsNone(decision.state_snapshot.guard_state)
                    self.assertEqual(decision.state_snapshot.prior_book_state, "holding")
                    self.assertEqual(decision.state_snapshot.prior_guard_state, prior_guard_state)
                    self.assertTrue(decision.state_snapshot.transition_valid)
                    self.assertEqual(runtime_state.book_state, "building")
                    self.assertIsNone(runtime_state.guard_state)

    def test_evaluate_independent_book_fail_closes_when_prior_guard_state_still_active(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
            strategy_hedge_independent_min_score_stability_bps=0.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.55,
                "trend_alpha": 0.41,
                "microstructure_alpha": 0.2,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.4})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.0,
            expected_net_edge_bps=13.0,
        )
        for representation in ("legacy", "separated"):
            for prior_guard_state, horizon_field in (
                ("cooldown", "cooldown_until"),
                ("suspended", "suspended_until"),
            ):
                with self.subTest(representation=representation, prior_guard_state=prior_guard_state):
                    prior_runtime_state = StrategyBookRuntimeState(
                        leg="long",
                        current_qty=Decimal("0"),
                        target_qty=Decimal("0"),
                        state="blocked",
                        book_state="flat" if representation == "separated" else prior_guard_state,
                        guard_state=None if representation == "legacy" else prior_guard_state,
                        holding_phase=None,
                        prior_book_state="holding",
                        state_version=3,
                        **{horizon_field: context.as_of_ts + timedelta(seconds=30)},
                    )

                    decision = evaluate_independent_book(
                        settings=settings,
                        context=context,
                        baseline=baseline,
                        ai_assessment=None,
                        leg="long",
                        expectancy=expectancy,
                        directional_leg_target_qty=Decimal("0.01"),
                        scorer=lambda **_: 0.99,
                        prior_runtime_state=prior_runtime_state,
                        recent_score_history=(0.99, 0.99, 0.99),
                    )

                    runtime_state = runtime_state_from_decision(
                        context=context,
                        decision=decision,
                        threshold_snapshot=decision.threshold_snapshot,
                        health_snapshot=decision.health_snapshot,
                    )

                    self.assertEqual(decision.state, "blocked")
                    self.assertEqual(decision.book_action, "blocked")
                    self.assertEqual(decision.target_qty, Decimal("0"))
                    self.assertIn("independent_state_transition_invalid", decision.reason_codes)
                    self.assertIn(
                        f"independent_transition_invalid:{prior_guard_state}->probing",
                        decision.blocked_reasons,
                    )
                    self.assertIsNotNone(decision.state_snapshot)
                    self.assertEqual(decision.state_snapshot.book_state, "flat")
                    self.assertEqual(decision.state_snapshot.guard_state, prior_guard_state)
                    self.assertEqual(decision.state_snapshot.prior_book_state, "flat")
                    self.assertEqual(decision.state_snapshot.prior_guard_state, prior_guard_state)
                    self.assertFalse(decision.state_snapshot.transition_valid)
                    self.assertEqual(
                        decision.state_snapshot.transition_violation_reason,
                        f"independent_transition_invalid:{prior_guard_state}->probing",
                    )
                    self.assertEqual(runtime_state.book_state, "flat")
                    self.assertEqual(runtime_state.guard_state, prior_guard_state)
                    self.assertEqual(runtime_state.prior_guard_state, prior_guard_state)

    def test_runtime_state_does_not_reuse_cooldown_horizon_as_suspension_horizon(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_trial_guard_enabled=True,
            strategy_performance_guard_min_closed_trades=1,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="flat",
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 2,
                    "recent_win_rate": 0.2,
                    "recent_fee_drag_ratio": 0.0,
                    "recent_churn_ratio": 0.0,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("-12"),
                }
            },
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.88,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.5, "trend_alpha": 0.45, "microstructure_alpha": 0.18, "liquidity_scale": 0.95},
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.36})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.0,
            expected_cost_bps=5.0,
            expected_net_edge_bps=13.0,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.02"),
            scorer=lambda **_: 0.82,
        )
        runtime_state = runtime_state_from_decision(
            context=context,
            decision=decision,
            threshold_snapshot=decision.threshold_snapshot,
            health_snapshot=decision.health_snapshot,
        )

        self.assertEqual(decision.book_state, "flat")
        self.assertEqual(decision.guard_state, "suspended")
        self.assertEqual(runtime_state.book_state, "flat")
        self.assertEqual(runtime_state.guard_state, "suspended")
        self.assertIsNone(runtime_state.suspended_until)

    def test_evaluate_independent_book_blocks_short_when_single_tick_confirmation_is_insufficient(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_short_entry_threshold=0.30,
            strategy_hedge_independent_short_scale_in_threshold=0.55,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.48,
                "trend_alpha": -0.42,
                "microstructure_alpha": -0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.32})
        expectancy = IndependentBookExpectancy(
            leg="short",
            expected_signal_edge_bps=38.0,
            expected_slippage_bps=5.6,
            expected_cost_bps=10.6,
            expected_net_edge_bps=27.4,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="short",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.304,
            recent_score_history=(0.286,),
        )

        self.assertEqual(decision.state, "blocked")
        self.assertEqual(decision.book_action, "blocked")
        self.assertIn("independent_short_book_score_support_below_min_confirm_ticks", decision.blocked_reasons)
        self.assertEqual(decision.score_stability_metrics.support_count, 1)
        self.assertFalse(bool(decision.score_stability_metrics and decision.score_stability_metrics.stable))
        self.assertEqual(decision.execution_health_state, "ok")
        self.assertIsNotNone(decision.eligibility)
        self.assertGreater(float(decision.eligibility.effective_max_cost_bps or 0.0), 10.6)
        self.assertLess(float(decision.eligibility.effective_max_cost_bps or 0.0), 15.0)

    def test_evaluate_independent_book_blocks_short_when_lifecycle_net_edge_falls_below_safe_floor(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_short_entry_threshold=0.30,
            strategy_hedge_independent_short_scale_in_threshold=0.55,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.48,
                "trend_alpha": -0.42,
                "microstructure_alpha": -0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.32})
        expectancy = IndependentBookExpectancy(
            leg="short",
            expected_signal_edge_bps=38.0,
            expected_slippage_bps=5.6,
            expected_cost_bps=10.6,
            expected_net_edge_bps=27.4,
            expected_lifecycle_cost_bps=34.0,
            expected_lifecycle_net_edge_bps=4.0,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="short",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.304,
            recent_score_history=(0.286,),
        )

        self.assertEqual(decision.state, "blocked")
        self.assertEqual(decision.book_action, "blocked")
        self.assertIn("independent_short_book_expected_net_edge_below_safe_threshold", decision.blocked_reasons)

    def test_evaluate_independent_book_keeps_long_two_tick_confirmation_requirement(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_long_entry_threshold=0.30,
            strategy_hedge_independent_long_scale_in_threshold=0.55,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.48,
                "trend_alpha": 0.42,
                "microstructure_alpha": 0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.32})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=38.0,
            expected_slippage_bps=5.6,
            expected_cost_bps=10.6,
            expected_net_edge_bps=27.4,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.304,
            recent_score_history=(0.286,),
        )

        self.assertEqual(decision.state, "blocked")
        self.assertEqual(decision.book_action, "blocked")
        self.assertIn("independent_long_book_score_support_below_min_confirm_ticks", decision.blocked_reasons)
        self.assertNotIn("independent_long_book_expected_cost_above_max_acceptable", decision.blocked_reasons)
        self.assertEqual(decision.score_stability_metrics.support_count, 1)
        self.assertFalse(bool(decision.score_stability_metrics and decision.score_stability_metrics.stable))

    def test_evaluate_independent_book_allows_short_when_signal_strengthens_without_drawdown(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_short_entry_threshold=0.30,
            strategy_hedge_independent_short_scale_in_threshold=0.55,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.48,
                "trend_alpha": -0.42,
                "microstructure_alpha": -0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.32})
        expectancy = IndependentBookExpectancy(
            leg="short",
            expected_signal_edge_bps=38.0,
            expected_slippage_bps=5.6,
            expected_cost_bps=10.6,
            expected_net_edge_bps=27.4,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="short",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.42,
            recent_score_history=(0.34, 0.37),
        )

        self.assertEqual(decision.state, "opening")
        self.assertEqual(decision.book_action, "open")
        self.assertEqual(decision.blocked_reasons, ())
        assert decision.score_stability_metrics is not None
        self.assertEqual(decision.score_stability_metrics.support_count, 3)
        self.assertAlmostEqual(decision.score_stability_metrics.upward_excursion_bps or 0.0, 8.0)
        self.assertAlmostEqual(decision.score_stability_metrics.downward_drawdown_bps or 0.0, 0.0)
        self.assertTrue(decision.score_stability_metrics.stable)

    def test_evaluate_independent_book_promotes_small_health_derisk_residual_to_full_close(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
            strategy_hedge_independent_min_score_stability_bps=0.0,
            strategy_performance_guard_min_closed_trades=4,
            strategy_max_fee_drag_ratio=0.48,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="long",
            current_long_position_qty=Decimal("0.01"),
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 4,
                    "recent_win_rate": 0.25,
                    "recent_fee_drag_ratio": 0.7,
                    "recent_churn_ratio": 0.1,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("-1"),
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
        ).model_copy(update={"current_long_position_notional": Decimal("20")})
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.55,
                "trend_alpha": 0.41,
                "microstructure_alpha": 0.2,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.4})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.0,
            expected_net_edge_bps=13.0,
            expected_lifecycle_cost_bps=10.0,
            expected_lifecycle_net_edge_bps=8.0,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.6,
            recent_score_history=(0.6, 0.6),
        )

        self.assertEqual(decision.state, "closing")
        self.assertEqual(decision.close_reason, "execution_health_degraded")
        self.assertEqual(decision.target_qty, Decimal("0"))
        self.assertIn("independent_long_book_de_risk_floor_promoted_to_close", decision.reason_codes)

    def test_evaluate_independent_book_uses_guard_eligible_fee_drag_for_execution_health(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
            strategy_hedge_independent_min_score_stability_bps=0.0,
            strategy_performance_guard_min_closed_trades=4,
            strategy_max_fee_drag_ratio=0.48,
            strategy_hedge_independent_long_entry_threshold=0.30,
            strategy_hedge_independent_long_scale_in_threshold=0.55,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="flat",
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 6,
                    "recent_guard_eligible_closed_trade_count": 4,
                    "recent_win_rate": 0.25,
                    "recent_guard_eligible_win_rate": 0.75,
                    "recent_fee_drag_ratio": 0.7,
                    "recent_guard_eligible_fee_drag_ratio": 0.2,
                    "recent_churn_ratio": 0.1,
                    "recent_guard_eligible_churn_ratio": 0.1,
                    "recent_low_edge_trade_streak": 0,
                    "recent_guard_eligible_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_guard_eligible_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("1"),
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
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.55,
                "trend_alpha": 0.41,
                "microstructure_alpha": 0.2,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.4})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.0,
            expected_net_edge_bps=13.0,
            expected_lifecycle_cost_bps=10.0,
            expected_lifecycle_net_edge_bps=8.0,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.6,
            recent_score_history=(0.6, 0.6),
        )

        self.assertEqual(decision.execution_health_state, "ok")
        self.assertEqual(decision.state, "opening")
        self.assertEqual(decision.book_action, "open")

    def test_evaluate_independent_book_uses_guard_eligible_net_pnl_for_trial_guard(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_trial_guard_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
            strategy_hedge_independent_min_score_stability_bps=0.0,
            strategy_performance_guard_min_closed_trades=4,
            strategy_hedge_independent_long_entry_threshold=0.30,
            strategy_hedge_independent_long_scale_in_threshold=0.55,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="flat",
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 6,
                    "recent_guard_eligible_closed_trade_count": 4,
                    "recent_win_rate": 0.8,
                    "recent_guard_eligible_win_rate": 0.25,
                    "recent_fee_drag_ratio": 0.1,
                    "recent_guard_eligible_fee_drag_ratio": 0.1,
                    "recent_churn_ratio": 0.1,
                    "recent_guard_eligible_churn_ratio": 0.1,
                    "recent_low_edge_trade_streak": 0,
                    "recent_guard_eligible_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_guard_eligible_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("-5"),
                    "recent_guard_eligible_net_realized_pnl": Decimal("1"),
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
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.55,
                "trend_alpha": 0.41,
                "microstructure_alpha": 0.2,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.4})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.0,
            expected_net_edge_bps=13.0,
            expected_lifecycle_cost_bps=10.0,
            expected_lifecycle_net_edge_bps=8.0,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.6,
            recent_score_history=(0.6, 0.6),
        )

        self.assertEqual(decision.execution_health_state, "ok")
        self.assertEqual(decision.state, "opening")
        self.assertEqual(decision.book_action, "open")

    def test_execution_health_state_falls_back_to_symbol_guard_metrics_when_leg_is_cold(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_performance_guard_min_closed_trades=4,
            strategy_max_fee_drag_ratio=0.48,
            strategy_max_churn_ratio=0.42,
        )
        context = make_context(
            recent_closed_trade_count=6,
            recent_guard_eligible_closed_trade_count=5,
            recent_guard_eligible_fee_drag_ratio=0.7,
            recent_guard_eligible_churn_ratio=0.8,
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 5,
                    "recent_guard_eligible_closed_trade_count": 5,
                    "recent_guard_eligible_fee_drag_ratio": 0.1,
                    "recent_guard_eligible_churn_ratio": 0.1,
                },
                "short": {
                    "recent_closed_trade_count": 0,
                    "recent_guard_eligible_closed_trade_count": 0,
                    "recent_guard_eligible_fee_drag_ratio": 0.0,
                    "recent_guard_eligible_churn_ratio": 0.0,
                },
            },
        )

        self.assertEqual(_execution_health_state(settings=settings, context=context, leg="short"), "blocked")

    def test_trial_guard_active_falls_back_to_symbol_guard_metrics_when_leg_is_cold(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_trial_guard_enabled=True,
            strategy_performance_guard_min_closed_trades=4,
        )
        context = make_context(
            recent_closed_trade_count=6,
            recent_guard_eligible_closed_trade_count=5,
            recent_guard_eligible_win_rate=0.2,
            recent_guard_eligible_net_realized_pnl=Decimal("-3"),
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 5,
                    "recent_guard_eligible_closed_trade_count": 5,
                    "recent_guard_eligible_win_rate": 0.8,
                    "recent_guard_eligible_net_realized_pnl": Decimal("2"),
                },
                "short": {
                    "recent_closed_trade_count": 0,
                    "recent_guard_eligible_closed_trade_count": 0,
                    "recent_guard_eligible_win_rate": 0.0,
                    "recent_guard_eligible_net_realized_pnl": Decimal("0"),
                },
            },
        )

        self.assertTrue(_trial_guard_active(settings=settings, context=context, leg="short"))

    def test_evaluate_independent_book_blocks_short_when_dynamic_cost_fuse_detects_anomaly(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_short_entry_threshold=0.30,
            strategy_hedge_independent_short_scale_in_threshold=0.55,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.48,
                "trend_alpha": -0.42,
                "microstructure_alpha": -0.18,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.32})
        expectancy = IndependentBookExpectancy(
            leg="short",
            expected_signal_edge_bps=38.0,
            expected_slippage_bps=5.6,
            expected_cost_bps=14.5,
            expected_net_edge_bps=27.4,
            depth_consumption_ratio=0.95,
            size_impact_bps=3.6,
            cost_confidence=0.85,
        )

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="short",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.304,
            recent_score_history=(0.286,),
        )

        self.assertEqual(decision.state, "blocked")
        self.assertEqual(decision.book_action, "blocked")
        self.assertIn("independent_short_book_expected_cost_above_max_acceptable", decision.blocked_reasons)
        self.assertEqual(decision.score_stability_metrics.support_count, 1)
        self.assertFalse(bool(decision.score_stability_metrics and decision.score_stability_metrics.stable))


if __name__ == "__main__":
    unittest.main()
