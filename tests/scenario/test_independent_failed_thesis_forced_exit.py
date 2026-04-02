from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.engine import evaluate_independent_book
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentFailedThesisScenario(unittest.TestCase):
    def test_failed_thesis_forces_exit(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_failed_thesis_net_edge_bps=1.0,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.70,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={"momentum_alpha": 0.10, "trend_alpha": 0.08, "microstructure_alpha": 0.04, "liquidity_scale": 0.95},
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.08})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=1.0,
            expected_slippage_bps=0.5,
            expected_cost_bps=1.0,
            expected_net_edge_bps=0.5,
        )

        result = evaluate_independent_book(
            settings=settings,
            context=make_context(
                product_type="derivatives",
                current_exposure_side="long",
                current_long_position_qty=0.03,
                current_position_qty=0.03,
                current_long_leg_opened_seconds_ago=1800,
            ),
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.02, confidence=0.55),
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.03"),
            scorer=None,
        )

        self.assertEqual(result.close_reason, "failed_thesis")
        self.assertEqual(result.book_action, "close_failed_thesis")
        self.assertEqual(result.book_state, "forced_exit")
        self.assertEqual(result.target_qty, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
