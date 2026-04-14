from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import unittest

from aats.schemas.strategy_runtime import StrategyBookRuntimeState
from aats.services.strategy_engines.independent.diagnostics import runtime_state_from_decision
from aats.services.strategy_engines.independent.engine import evaluate_independent_book
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from tests.support.strategy_family import make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentEngineTransitionFlow(unittest.TestCase):
    def test_balance_aware_entry_size_expands_opening_qty_from_available_equity(self) -> None:
        settings = make_derivatives_hedge_settings(
            default_order_qty=0.004,
            default_target_leverage=5.0,
            max_target_leverage=10.0,
            max_margin_usage_fraction=0.75,
            strategy_dynamic_leverage_enabled=False,
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_long_entry_threshold=0.30,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="flat",
            market_last_price=100000.0,
            available_trading_equity=390.0,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.55,
                "trend_alpha": 0.41,
                "microstructure_alpha": 0.20,
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

        decision = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=None,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0"),
            scorer=lambda **_: 0.42,
            recent_score_history=(0.34, 0.37),
        )

        self.assertEqual(decision.state, "opening")
        self.assertEqual(decision.book_action, "open")
        self.assertEqual(decision.target_qty, Decimal("0.014625"))
        self.assertGreater(decision.target_qty, Decimal("0.004"))

    def test_strengthening_short_signal_is_not_treated_as_score_drawdown(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=False,
            strategy_hedge_independent_short_entry_threshold=0.30,
            strategy_hedge_independent_short_scale_in_threshold=0.55,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_rebalance_cooldown_seconds=0,
        )
        context = make_context(product_type="derivatives", current_exposure_side="flat")
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.9,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.55,
                "trend_alpha": -0.41,
                "microstructure_alpha": -0.20,
                "liquidity_scale": 0.95,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.4})
        expectancy = IndependentBookExpectancy(
            leg="short",
            expected_signal_edge_bps=18.0,
            expected_slippage_bps=1.2,
            expected_cost_bps=5.0,
            expected_net_edge_bps=13.0,
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
        runtime_state = runtime_state_from_decision(
            context=context,
            decision=decision,
            threshold_snapshot=decision.threshold_snapshot,
            health_snapshot=decision.health_snapshot,
        )

        self.assertEqual(decision.state, "opening")
        self.assertEqual(decision.book_action, "open")
        self.assertEqual(runtime_state.book_state, "probing")
        self.assertIsNone(runtime_state.guard_state)
        assert decision.score_stability_metrics is not None
        self.assertAlmostEqual(decision.score_stability_metrics.upward_excursion_bps or 0.0, 8.0)
        self.assertAlmostEqual(decision.score_stability_metrics.downward_drawdown_bps or 0.0, 0.0)
        self.assertTrue(decision.score_stability_metrics.stable)

    def test_pseudo_guard_runtime_state_can_reenter_probing(self) -> None:
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
                "microstructure_alpha": 0.20,
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
                    self.assertIsNotNone(decision.state_snapshot)
                    self.assertEqual(decision.state_snapshot.book_state, "probing")
                    self.assertIsNone(decision.state_snapshot.guard_state)
                    self.assertEqual(decision.state_snapshot.prior_book_state, "flat")
                    self.assertEqual(decision.state_snapshot.prior_guard_state, prior_guard_state)
                    self.assertTrue(decision.state_snapshot.transition_valid)
                    self.assertEqual(runtime_state.book_state, "probing")
                    self.assertIsNone(runtime_state.guard_state)
                    self.assertEqual(runtime_state.prior_book_state, "flat")
                    self.assertEqual(runtime_state.prior_guard_state, prior_guard_state)
                    self.assertTrue(runtime_state.transition_valid)
                    self.assertIsNone(runtime_state.transition_violation_reason)
                    self.assertTrue(str(runtime_state.execution_chain_id or "").startswith("independent:"))

    def test_active_guard_runtime_state_still_fail_closes(self) -> None:
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
                "microstructure_alpha": 0.20,
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
                    self.assertIn(
                        f"independent_transition_invalid:{prior_guard_state}->probing",
                        decision.blocked_reasons,
                    )
                    self.assertIsNotNone(decision.state_snapshot)
                    self.assertEqual(decision.state_snapshot.book_state, "flat")
                    self.assertEqual(decision.state_snapshot.guard_state, prior_guard_state)
                    self.assertFalse(decision.state_snapshot.transition_valid)
                    self.assertEqual(runtime_state.book_state, "flat")
                    self.assertEqual(runtime_state.guard_state, prior_guard_state)
                    self.assertFalse(runtime_state.transition_valid)
                    self.assertEqual(
                        runtime_state.transition_violation_reason,
                        f"independent_transition_invalid:{prior_guard_state}->probing",
                    )


if __name__ == "__main__":
    unittest.main()
