from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.strategy_engines.independent.adaptive import threshold_snapshot
from aats.services.strategy_engines.independent.health import IndependentLegHealthSnapshot
from aats.services.strategy_engines.independent.models import IndependentBookDecision
from tests.support.strategy_family import make_ai_assessment, make_baseline, make_context, make_derivatives_hedge_settings


class TestIndependentAdaptive(unittest.TestCase):
    def test_threshold_snapshot_produces_shadow_only_dynamic_thresholds(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_long_entry_threshold=0.66,
            strategy_hedge_independent_long_close_threshold=0.52,
            strategy_hedge_independent_long_scale_in_threshold=0.70,
            strategy_hedge_independent_max_thesis_age_seconds=1800,
            strategy_hedge_independent_de_risk_net_edge_bps=2.0,
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.58,
            suggested_position_scale=1.0,
            volatility_target_scale=0.72,
            factor_scores={"liquidity_scale": 0.42, "microstructure_alpha": 0.05},
        )
        ai_assessment = make_ai_assessment(direction=-0.18, confidence=0.57)
        decision = IndependentBookDecision(
            leg="long",
            expectancy=None,
            score=0.68,
            current_qty=Decimal("0"),
            target_qty=Decimal("0.01"),
            state="opening",
            reason_codes=[],
            blocked_reasons=[],
            min_hold_remaining_seconds=0.0,
            rebalance_cooldown_remaining_seconds=0.0,
            book_action="open",
            book_state="probing",
            holding_phase="entry",
            health_state="degraded",
        )

        snapshot = threshold_snapshot(
            settings=settings,
            leg="long",
            baseline=baseline,
            ai_assessment=ai_assessment,
            context=make_context(product_type="derivatives"),
            decision=decision,
            health_snapshot=IndependentLegHealthSnapshot(
                leg="long",
                health_state="degraded",
                only_reduce=True,
                warnings=("execution_health_degraded",),
            ),
        )

        self.assertTrue(snapshot.shadow_only)
        self.assertEqual(snapshot.entry_threshold, 0.66)
        self.assertEqual(snapshot.close_threshold, 0.52)
        self.assertEqual(snapshot.scale_in_threshold, 0.70)
        self.assertGreater(snapshot.adaptive_entry_threshold, snapshot.entry_threshold)
        self.assertGreater(snapshot.adaptive_scale_in_threshold, snapshot.scale_in_threshold)
        self.assertGreater(snapshot.adaptive_close_threshold, snapshot.close_threshold)
        self.assertLess(snapshot.adaptive_thesis_age_seconds, snapshot.thesis_age_seconds)
        self.assertGreater(snapshot.adaptive_de_risk_net_edge_bps, snapshot.de_risk_net_edge_bps)
        self.assertLess(snapshot.capital_multiplier, 1.0)
        self.assertIn("adaptive_shadow_health_degraded", snapshot.reason_codes)
        self.assertIn("adaptive_shadow_directional_alignment_penalty", snapshot.reason_codes)


if __name__ == "__main__":
    unittest.main()
