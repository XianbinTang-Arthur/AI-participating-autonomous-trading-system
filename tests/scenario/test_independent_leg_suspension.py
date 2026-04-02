from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.engine import evaluate_independent_book
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentLegSuspensionScenario(unittest.TestCase):
    def test_trial_guard_suspends_opening_leg(self) -> None:
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

        result = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.30, confidence=0.86),
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.02"),
            scorer=lambda **_: 0.82,
        )

        self.assertEqual(result.book_action, "blocked")
        self.assertEqual(result.book_state, "suspended")
        self.assertIn("independent_long_book_trial_guard_active", result.blocked_reasons)


if __name__ == "__main__":
    unittest.main()
