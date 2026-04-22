"""Round 3 Phase 1.1 · strategy_shadow schema + topics + settings 默认值测试。

覆盖:
- Schema 字段 + 默认值
- Topics 命名稳定（Grafana / event_store query 依赖）
- Settings 默认 OFF + 空 candidates
- Settings 验证：enabled=True 但 candidates 空时，消费方要能正确处理（schema 层不强制）
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from aats.schemas.strategy_shadow import (
    StrategyFamilyShadowDecision,
    StrategyFamilyShadowEvaluation,
)


class TestStrategyFamilyShadowDecisionSchema(unittest.TestCase):
    def _minimal_kwargs(self) -> dict:
        return {
            "decision_id": "decision_abc",
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "1m",
            "candidate_id": "low_threshold",
            "candidate_family": "independent",
            "candidate_config_version": "sha256-abc",
            "baseline_family": "independent",
            "baseline_target_qty": Decimal("0"),
            "baseline_action": "hold",
            "shadow_target_qty": Decimal("0.001"),
            "shadow_action": "entry_long",
            "would_override_baseline": True,
            "shadow_action_type": "entry_override",
        }

    def test_minimal_construction(self) -> None:
        d = StrategyFamilyShadowDecision(**self._minimal_kwargs())
        self.assertTrue(d.shadow_decision_id.startswith("strat_shadow"))
        self.assertEqual(d.decision_id, "decision_abc")
        self.assertTrue(d.would_override_baseline)
        # optional Phase 2+ fields default to None
        self.assertIsNone(d.reference_price)
        self.assertIsNone(d.reference_spread_bps)
        self.assertIsNone(d.market_snapshot_ref)
        # list defaults
        self.assertEqual(d.reason_codes, [])
        self.assertEqual(d.candidate_overrides, {})

    def test_shadow_action_type_literal_enforced(self) -> None:
        with self.assertRaises(Exception):
            StrategyFamilyShadowDecision(
                **{
                    **self._minimal_kwargs(),
                    "shadow_action_type": "unknown_type",  # not in Literal
                }
            )


class TestStrategyFamilyShadowEvaluationSchema(unittest.TestCase):
    def test_minimal_construction(self) -> None:
        from datetime import datetime, timezone

        e = StrategyFamilyShadowEvaluation(
            window_start=datetime(2026, 4, 22, 10, tzinfo=timezone.utc),
            window_end=datetime(2026, 4, 22, 11, tzinfo=timezone.utc),
            symbol="BTC-USDT-SWAP",
            timeframe="1m",
            candidate_id="low_threshold",
            candidate_config_version="sha256-abc",
            baseline_trade_count=10,
            shadow_trade_count=12,
            override_count=4,
            agreement_count=8,
            disagreement_count=4,
        )
        self.assertTrue(e.evaluation_id.startswith("strat_shadow_eval"))
        # Phase 2+ PnL fields default to None
        self.assertIsNone(e.baseline_net_pnl)
        self.assertIsNone(e.shadow_net_pnl)
        self.assertIsNone(e.shadow_outperformed)


class TestTopicsNamingStable(unittest.TestCase):
    """Topic 命名一旦 ship 就不该改 — 下游 Grafana / event_store query 依赖。"""

    def test_topic_names(self) -> None:
        from aats.events.topics import (
            STRATEGY_FAMILY_SHADOW_DECISIONS,
            STRATEGY_FAMILY_SHADOW_EVALUATIONS,
        )

        self.assertEqual(
            STRATEGY_FAMILY_SHADOW_DECISIONS,
            "strategy.family_shadow_decision",
        )
        self.assertEqual(
            STRATEGY_FAMILY_SHADOW_EVALUATIONS,
            "strategy.family_shadow_evaluation",
        )


class TestSettingsDefaults(unittest.TestCase):
    def test_paper_trading_defaults_off(self) -> None:
        """默认必须 OFF — 上线不能意外触发 shadow 消耗资源。"""
        from aats.bootstrap.settings import AATSSettings

        s = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
            }
        )
        self.assertFalse(s.paper_trading_shadow_enabled)
        self.assertEqual(s.paper_trading_shadow_candidates, ())

    def test_paper_trading_candidates_override(self) -> None:
        """可以显式配置 candidates。"""
        from aats.bootstrap.settings import AATSSettings

        s = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "paper_trading_shadow_enabled": True,
                "paper_trading_shadow_candidates": (
                    {
                        "candidate_id": "low_threshold",
                        "family": "independent",
                        "overrides": {
                            "strategy_hedge_independent_long_entry_threshold": 0.15
                        },
                    },
                ),
            }
        )
        self.assertTrue(s.paper_trading_shadow_enabled)
        self.assertEqual(len(s.paper_trading_shadow_candidates), 1)
        self.assertEqual(
            s.paper_trading_shadow_candidates[0]["candidate_id"],
            "low_threshold",
        )


if __name__ == "__main__":
    unittest.main()
