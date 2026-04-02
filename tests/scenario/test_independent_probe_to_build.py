from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.engine import evaluate_independent_book
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentProbeToBuildScenario(unittest.TestCase):
    def test_long_book_moves_from_probe_to_build(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.60,
            strategy_hedge_independent_long_scale_in_threshold=0.68,
        )
        baseline = make_baseline(
            direction_bias="long",
            confidence=0.86,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.55,
                "trend_alpha": 0.48,
                "microstructure_alpha": 0.22,
                "liquidity_scale": 0.97,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.42})
        ai_assessment = make_ai_assessment(direction=0.35, confidence=0.84)
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=20.0,
            expected_slippage_bps=1.0,
            expected_cost_bps=5.0,
            expected_net_edge_bps=15.0,
        )

        probe = evaluate_independent_book(
            settings=settings,
            context=make_context(product_type="derivatives", current_exposure_side="flat"),
            baseline=baseline,
            ai_assessment=ai_assessment,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.01"),
            scorer=lambda **_: 0.84,
        )
        build = evaluate_independent_book(
            settings=settings,
            context=make_context(
                product_type="derivatives",
                current_exposure_side="long",
                current_long_position_qty=0.01,
                current_position_qty=0.01,
                current_long_leg_opened_seconds_ago=240,
            ),
            baseline=baseline,
            ai_assessment=ai_assessment,
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.03"),
            scorer=lambda **_: 0.84,
        )

        self.assertEqual(probe.book_action, "open")
        self.assertEqual(probe.book_state, "probing")
        self.assertEqual(probe.holding_phase, "entry")
        self.assertEqual(build.book_action, "scale_in")
        self.assertEqual(build.book_state, "building")
        self.assertEqual(build.holding_phase, "scale_in")


if __name__ == "__main__":
    unittest.main()
