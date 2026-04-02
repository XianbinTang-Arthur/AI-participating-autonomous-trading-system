from __future__ import annotations

from decimal import Decimal
import unittest

from aats.schemas.strategy_runtime import PortfolioAllocationDecision, StrategyBookRuntimeState, StrategySleeveIntent
from aats.services.strategy_engines.independent.replay import _decision_snapshot_from_sources
from aats.services.strategy_engines.independent.scoring import compute_score_stability
from tests.support.strategy_family import make_baseline, make_derivatives_hedge_settings


class TestIndependentScoreStabilityCompat(unittest.TestCase):
    def test_compute_score_stability_retains_small_compat_alias_surface(self) -> None:
        settings = make_derivatives_hedge_settings(
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
        )
        baseline = make_baseline(
            direction_bias="short",
            confidence=0.82,
            suggested_position_scale=1.0,
            volatility_target_scale=1.0,
            factor_scores={
                "momentum_alpha": -0.48,
                "trend_alpha": -0.42,
                "microstructure_alpha": -0.18,
            },
        ).model_copy(update={"regime": "trend", "composite_alpha_score": -0.32})

        metrics = compute_score_stability(
            settings=settings,
            leg="short",
            score=0.42,
            entry_threshold=0.30,
            baseline=baseline,
            ai_assessment=None,
            recent_score_history=(0.34, 0.37),
            min_confirm_ticks=2,
        )

        self.assertAlmostEqual(metrics.max_drawdown_bps or 0.0, metrics.upward_excursion_bps or 0.0)
        self.assertEqual(metrics.max_drawdown_bps_compat_source, "upward_excursion_bps")

    def test_decision_snapshot_prefers_new_fields_while_tolerating_legacy_alias_input(self) -> None:
        decision = PortfolioAllocationDecision(
            decision_id="decision_replay_score_metrics",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
        )
        sleeve_intent = StrategySleeveIntent(
            decision_id=decision.decision_id,
            family="independent",
            strategy_sleeve_id="sleeve_replay_score_metrics",
            state="candidate",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            inventory_policy="paired_inventory",
            route_action="override_target",
            family_action="hold_family",
            metrics={
                "long_score_support_count": 3,
                "long_score_stable": True,
                "long_score_stability_max_drawdown_bps": 8.0,
                "long_score_stability_max_drawdown_bps_compat_source": "upward_excursion_bps",
                "long_score_stability_upward_excursion_bps": 8.0,
                "long_score_stability_downward_drawdown_bps": 0.0,
                "long_score_stability_semantics_version": 2,
                "long_score_stability_source": "recent_target_history",
            },
        )
        runtime_state = StrategyBookRuntimeState(
            leg="long",
            current_qty=Decimal("0"),
            target_qty=Decimal("0.01"),
        )

        snapshot = _decision_snapshot_from_sources(
            decision=decision,
            sleeve_intent=sleeve_intent,
            leg="long",
            runtime_state=runtime_state,
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        assert snapshot.score_stability_metrics is not None
        self.assertEqual(snapshot.score_stability_metrics["semantics_version"], 2)
        self.assertEqual(snapshot.score_stability_metrics["upward_excursion_bps"], 8.0)
        self.assertEqual(snapshot.score_stability_metrics["downward_drawdown_bps"], 0.0)
        self.assertNotIn("max_drawdown_bps", snapshot.score_stability_metrics)
        self.assertNotIn("max_drawdown_bps_compat_source", snapshot.score_stability_metrics)


if __name__ == "__main__":
    unittest.main()
