from __future__ import annotations

from decimal import Decimal
import unittest

from aats.schemas.strategy_runtime import PortfolioAllocationDecision, StrategyBookRuntimeState, StrategySleeveIntent
from aats.services.strategy_engines.independent.models import ScoreStabilityMetrics
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

    def test_is_legacy_drawdown_compat_true_when_only_legacy_field_provided(self) -> None:
        """Task 142：只传旧字段构造时，is_legacy_drawdown_compat == True，
        下游可据此显式走兼容 switch。"""
        metrics = ScoreStabilityMetrics(
            support_count=3,
            min_score=0.50,
            mean_score=0.52,
            stable=True,
            source="recent_target_history",
            max_drawdown_bps=4.0,
        )
        self.assertTrue(metrics.is_legacy_drawdown_compat)
        self.assertEqual(metrics.max_drawdown_bps_compat_source, "upward_excursion_bps")
        # mirror：旧字段有值 → 新字段被自动填
        self.assertEqual(metrics.upward_excursion_bps, 4.0)

    def test_is_legacy_drawdown_compat_true_even_when_only_new_field_provided(self) -> None:
        """Task 142：当前 __post_init__ 行为 —— 即便只传新字段，compat_source 也被
        标记（因为新→旧的 mirror 把 max_drawdown_bps 回填了）。这条测试**锚定**当前
        行为，提醒将来如果去掉新→旧 mirror，compat_source 在"新字段生产者"场景必须
        保持 None，否则下游 is_legacy_drawdown_compat 会 false positive。"""
        metrics = ScoreStabilityMetrics(
            support_count=3,
            min_score=0.50,
            mean_score=0.52,
            stable=True,
            source="recent_target_history",
            upward_excursion_bps=5.0,
            downward_drawdown_bps=0.0,
        )
        # 注意：当前行为下 compat_source 也会被设置，这是 __post_init__ 新→旧 mirror
        # 的副作用。如果未来有 semantic cleanup，此断言应相应调整。
        self.assertEqual(metrics.max_drawdown_bps_compat_source, "upward_excursion_bps")
        self.assertTrue(metrics.is_legacy_drawdown_compat)


if __name__ == "__main__":
    unittest.main()
