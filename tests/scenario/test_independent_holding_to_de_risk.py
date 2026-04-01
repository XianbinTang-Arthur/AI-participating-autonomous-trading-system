from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.engine import evaluate_independent_book
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentHoldingToDeRiskScenario(unittest.TestCase):
    def test_health_enforcement_can_turn_hold_into_de_risk(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=True,
            strategy_hedge_independent_health_enforcement_enabled=True,
            strategy_performance_guard_min_closed_trades=1,
            strategy_max_fee_drag_ratio=0.10,
        )
        context = make_context(
            product_type="derivatives",
            current_exposure_side="long",
            current_long_position_qty=0.04,
            current_position_qty=0.04,
            current_long_leg_opened_seconds_ago=900,
            leg_strategy_health={
                "long": {
                    "recent_closed_trade_count": 3,
                    "recent_win_rate": 0.67,
                    "recent_fee_drag_ratio": 0.18,
                    "recent_churn_ratio": 0.02,
                    "recent_low_edge_trade_streak": 0,
                    "recent_low_edge_trade_at": None,
                    "recent_net_realized_pnl": Decimal("15"),
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
            confidence=0.85,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": 0.42,
                "trend_alpha": 0.38,
                "microstructure_alpha": 0.12,
                "liquidity_scale": 0.96,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": 0.34})
        expectancy = IndependentBookExpectancy(
            leg="long",
            expected_signal_edge_bps=14.0,
            expected_slippage_bps=1.0,
            expected_cost_bps=4.0,
            expected_net_edge_bps=10.0,
        )

        result = evaluate_independent_book(
            settings=settings,
            context=context,
            baseline=baseline,
            ai_assessment=make_ai_assessment(direction=0.20, confidence=0.80),
            leg="long",
            expectancy=expectancy,
            directional_leg_target_qty=Decimal("0.04"),
            scorer=None,
        )

        self.assertEqual(result.close_reason, "execution_health_degraded")
        self.assertEqual(result.book_action, "de_risk")
        self.assertEqual(result.book_state, "de_risking")
        self.assertLess(result.target_qty, result.current_qty)


if __name__ == "__main__":
    unittest.main()
