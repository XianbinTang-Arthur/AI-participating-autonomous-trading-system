from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.engine import evaluate_independent_book
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentSizeDownEntryScenario(unittest.TestCase):
    def test_short_entry_uses_size_down_and_asymmetry_when_rollout_enabled(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=True,
            strategy_hedge_independent_size_down_entry_enabled=True,
            strategy_hedge_independent_long_short_asymmetry_enabled=True,
            strategy_hedge_independent_short_asymmetry_penalty_multiplier=0.80,
            strategy_hedge_independent_entry_size_down_floor=0.50,
            strategy_hedge_independent_short_entry_threshold=0.55,
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.40,
            suggested_position_scale=1.0,
            volatility_target_scale=0.50,
            factor_scores={
                "momentum_alpha": -0.46,
                "trend_alpha": -0.42,
                "microstructure_alpha": -0.18,
                "liquidity_scale": 0.82,
            },
        ).model_copy(update={"regime": "range", "composite_alpha_score": -0.38})
        expectancy = IndependentBookExpectancy(
            leg="short",
            expected_signal_edge_bps=16.0,
            expected_slippage_bps=1.0,
            expected_cost_bps=5.0,
            expected_net_edge_bps=11.0,
        )

        result = evaluate_independent_book(
            settings=settings,
            context=make_context(product_type="derivatives", current_exposure_side="flat"),
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=-0.32, confidence=0.42),
            leg="short",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.02"),
            scorer=lambda **_: 0.80,
        )

        self.assertEqual(result.book_action, "open")
        self.assertTrue(result.threshold_snapshot.live_applied)
        self.assertLess(result.target_qty, Decimal("0.02"))
        self.assertIn("independent_short_book_asymmetry_penalty_applied", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
