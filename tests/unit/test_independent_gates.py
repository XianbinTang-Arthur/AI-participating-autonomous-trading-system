from __future__ import annotations

import unittest

from aats.services.strategy_engines.families.independent_family import (
    _independent_entry_quality_gate,
    _independent_open_gate,
)
from aats.services.strategy_engines.independent.gates import (
    evaluate_entry_quality_gate,
    evaluate_open_eligibility,
)
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy, ScoreStabilityMetrics
from tests.support.strategy_family import make_context, make_derivatives_hedge_settings


class TestIndependentGates(unittest.TestCase):
    def test_evaluate_open_eligibility_matches_legacy_open_gate(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_post_close_cooldown_seconds=300.0,
            strategy_hedge_independent_max_acceptable_cost_bps=5.0,
            strategy_hedge_independent_min_safe_net_edge_bps=2.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=1.0,
            strategy_hedge_independent_weak_edge_execution_mode="block",
        )
        context = make_context(last_long_leg_closed_seconds_ago=30, current_exposure_side="flat")

        extracted = evaluate_open_eligibility(
            settings=settings,
            context=context,
            leg="long",
            expectancy=IndependentBookExpectancy(
                leg="long",
                expected_signal_edge_bps=2.0,
                expected_slippage_bps=1.0,
                expected_cost_bps=7.0,
                expected_net_edge_bps=1.0,
            ),
        )
        legacy = _independent_open_gate(
            settings=settings,
            context=context,
            leg="long",
            expected_cost_bps=7.0,
            expected_net_edge_bps=1.0,
        )

        self.assertEqual(list(extracted.hard_block_reasons), legacy["blocked_reasons"])
        self.assertFalse(bool(extracted.warnings))

    def test_evaluate_entry_quality_gate_matches_legacy_wrapper(self) -> None:
        metrics = ScoreStabilityMetrics(
            support_count=1,
            min_score=0.52,
            max_score=0.55,
            mean_score=0.54,
            max_drawdown_bps=3.0,
            stable=False,
            source="recent_target_history",
        )
        extracted = evaluate_entry_quality_gate(
            side="long",
            score=0.55,
            entry_threshold=0.60,
            liquidity_quality_score=0.40,
            score_stability_metrics=metrics,
            execution_health_state="degraded",
            min_confirm_ticks=2,
            min_liquidity_quality=0.60,
            require_execution_health_ok=True,
        )
        legacy = _independent_entry_quality_gate(
            side="long",
            score=0.55,
            entry_threshold=0.60,
            liquidity_quality_score=0.40,
            score_stability_metrics=metrics,
            execution_health_state="degraded",
            min_confirm_ticks=2,
            min_liquidity_quality=0.60,
            require_execution_health_ok=True,
        )

        self.assertEqual(extracted, legacy)
        self.assertIn("independent_long_book_signal_below_entry_threshold", extracted[1])
        self.assertIn("independent_long_book_execution_health_not_ok", extracted[1])


if __name__ == "__main__":
    unittest.main()
