from __future__ import annotations

import unittest

from aats.services.strategy_engines.families.independent_family import (
    _independent_entry_quality_gate,
    _independent_open_gate,
)
from aats.services.strategy_engines.independent.gates import (
    evaluate_entry_quality_gate,
    evaluate_open_eligibility,
    resolve_entry_min_confirm_ticks,
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

    def test_evaluate_open_eligibility_uses_cost_threshold_as_anomaly_fuse(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
            strategy_hedge_independent_weak_edge_execution_mode="block",
        )
        context = make_context(current_exposure_side="flat")

        extracted = evaluate_open_eligibility(
            settings=settings,
            context=context,
            leg="short",
            expectancy=IndependentBookExpectancy(
                leg="short",
                expected_signal_edge_bps=38.0,
                expected_slippage_bps=5.6,
                expected_cost_bps=10.6,
                expected_net_edge_bps=27.4,
            ),
        )

        self.assertEqual(extracted.hard_block_reasons, ())
        self.assertAlmostEqual(float(extracted.effective_max_cost_bps or 0.0), 13.5, places=6)

    def test_evaluate_open_eligibility_still_blocks_extreme_cost_anomaly(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
            strategy_hedge_independent_weak_edge_execution_mode="block",
        )
        context = make_context(current_exposure_side="flat")

        extracted = evaluate_open_eligibility(
            settings=settings,
            context=context,
            leg="short",
            expectancy=IndependentBookExpectancy(
                leg="short",
                expected_signal_edge_bps=45.0,
                expected_slippage_bps=5.6,
                expected_cost_bps=15.0,
                expected_net_edge_bps=28.0,
            ),
        )

        self.assertIn("independent_short_book_expected_cost_above_max_acceptable", extracted.hard_block_reasons)

    def test_resolve_entry_min_confirm_ticks_relaxes_high_edge_short_entry(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        )

        extracted = resolve_entry_min_confirm_ticks(
            settings=settings,
            side="short",
            score=0.304,
            entry_threshold=0.30,
            scale_threshold=0.55,
            expected_net_edge_bps=27.4,
        )

        self.assertEqual(extracted, 1)

    def test_resolve_entry_min_confirm_ticks_keeps_long_entry_confirmation_requirement(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        )

        extracted = resolve_entry_min_confirm_ticks(
            settings=settings,
            side="long",
            score=0.304,
            entry_threshold=0.30,
            scale_threshold=0.55,
            expected_net_edge_bps=27.4,
        )

        self.assertEqual(extracted, 2)

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
