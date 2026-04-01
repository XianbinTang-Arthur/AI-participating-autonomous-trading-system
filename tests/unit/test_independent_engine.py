from __future__ import annotations

import unittest
from decimal import Decimal

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


if __name__ == "__main__":
    unittest.main()
