from __future__ import annotations

import unittest
from decimal import Decimal

from aats.schemas.strategy_runtime import StrategyBookRuntimeState
from aats.services.strategy_engines.families.independent_family import _evaluate_independent_book
from aats.services.strategy_engines.independent.diagnostics import runtime_state_from_decision
from aats.services.strategy_engines.independent.engine import evaluate_independent_book
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
        self.assertIsNone(runtime_state.holding_phase)
        self.assertIsNotNone(runtime_state.threshold_snapshot)
        self.assertIsNotNone(runtime_state.leg_health_summary)
        self.assertIsNotNone(runtime_state.threshold_snapshot.adaptive_entry_threshold)
        self.assertIsNotNone(runtime_state.threshold_snapshot.capital_multiplier)
        self.assertTrue(runtime_state.threshold_snapshot.reason_codes)

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


if __name__ == "__main__":
    unittest.main()
