"""Bug-2 修复契约：microstructure 不再 double-dip (composite + threshold).

旧实现让 microstructure_alpha 既进入 composite_alpha_score（权重 0.15），又
在 adjusted_threshold 里以 support / conflict 方式调整决策门槛 → 同一信号源
两处作用 → 隐式权重 ≫ 15%，conflicts 下双重惩罚。

本测试锁定的契约:
  1. 构造 "强 micro support" 与 "弱 micro" 两种场景，alpha_score 相同 →
     decision threshold 相同（修复前会不同）
  2. 构造 "强 micro conflict" 场景，threshold 不应被额外惩罚
  3. alignment_bonus（来自 directional_alignment，与 micro 独立）仍生效
"""

from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import DecisionContext
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.feature_engine.calculator import FeatureCalculator
from aats.storage.event_store import InMemoryEventStore


def _make_context(feature_snapshot_ref: str) -> DecisionContext:
    now = utc_now()
    return DecisionContext(
        decision_id="dec_bug2_test",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        as_of_ts=now,
        market_snapshot_ref="evt_market",
        feature_snapshot_ref=feature_snapshot_ref,
        portfolio_snapshot_ref="evt_portfolio",
        health_snapshot_ref="evt_health",
        mode="guarded_live",
        current_position_qty=0.0,
        product_type="derivatives",
        current_exposure_side="flat",
        current_target_leverage=1.0,
    )


def _base_snapshot() -> FeatureSnapshot:
    now = utc_now()
    market_snapshot = MarketSnapshot(
        created_at=now,
        symbol="BTC-USDT-SWAP",
        exchange="OKX",
        snapshot_ts=now,
        best_bid=67000.0,
        best_ask=67001.0,
        last_price=67000.5,
        bid_size=4.0,
        ask_size=2.2,
        volume_24h=1000.0,
        kline_15m={"open": 66800.0, "high": 67200.0, "low": 66700.0, "close": 67100.0},
        kline_1h={"open": 66000.0, "high": 67300.0, "low": 65900.0, "close": 67100.0},
        orderbook_depth={
            "bids": [{"price": 67000.0, "size": 7.0}, {"price": 66999.0, "size": 5.0}],
            "asks": [{"price": 67001.0, "size": 4.0}, {"price": 67002.0, "size": 4.5}],
        },
        recent_trades=[
            {"side": "buy", "size": 1.2},
            {"side": "buy", "size": 0.8},
            {"side": "sell", "size": 0.3},
        ],
    )
    return FeatureCalculator().calculate(market_snapshot, market_snapshot_ref="evt_market")


def _persist_snapshot(store: InMemoryEventStore, snap: FeatureSnapshot) -> str:
    event = build_envelope(
        topic=topics.FEATURE_SNAPSHOTS,
        key=snap.symbol,
        payload_model=snap,
        source_component="test",
    )
    store.append(event)
    return event.event_id


class BaselineMicroNotDoubleCountedTests(unittest.TestCase):
    def _snap_with(self, *, composite: float, micro: float, alignment: str = "flat") -> FeatureSnapshot:
        base = _base_snapshot()
        analysis = base.analysis_context
        assert analysis is not None
        return base.model_copy(
            update={
                "regime_indicator": "trend",
                "composite_alpha_score": composite,
                "analysis_context": analysis.model_copy(
                    update={
                        "alpha_factors": analysis.alpha_factors.model_copy(
                            update={"microstructure_alpha": micro}
                        ),
                        "multi_timeframe": analysis.multi_timeframe.model_copy(
                            update={"directional_alignment": alignment}
                        ),
                    }
                ),
            }
        )

    def test_threshold_identical_regardless_of_microstructure_support_level(self) -> None:
        """强 micro support（significant + same sign）与 弱 micro 下，相同 alpha
        应产出相同 threshold —— 修复前不同。

        context:
          - alpha=0.18 偏多，超过 trend threshold 0.16
          - 场景 A: micro=+0.50 (强 support, alpha 同号)
          - 场景 B: micro=+0.01 (弱 micro，不构成 significant)
          - alignment=flat（消除 alignment_bonus 影响）
        """
        store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            # 关 impulse override 避免短路
            "strategy_baseline_impulse_override_enabled": False,
        })
        strategy = BaselineStrategy(event_store=store, settings=settings)

        snap_strong_support = self._snap_with(composite=0.18, micro=0.50)
        snap_weak_micro = self._snap_with(composite=0.18, micro=0.01)

        ref_a = _persist_snapshot(store, snap_strong_support)
        baseline_a = strategy.evaluate(_make_context(feature_snapshot_ref=ref_a))

        ref_b = _persist_snapshot(store, snap_weak_micro)
        baseline_b = strategy.evaluate(_make_context(feature_snapshot_ref=ref_b))

        self.assertEqual(
            baseline_a.direction_threshold, baseline_b.direction_threshold,
            "Bug-2 修复后，microstructure 支持度不应改变决策 threshold",
        )

    def test_threshold_not_penalized_by_microstructure_conflict(self) -> None:
        """强 micro conflict 场景 threshold 不应比无 micro 高（penalty 已删除）.

        构造:
          - alpha = +0.18, micro = -0.50 (strong conflict)
          - 对比: alpha = +0.18, micro = 0.0 (neutral)
          - 这两个 case 的 threshold 必须相等（旧实现 conflict case 会多 +0.04 penalty）
        """
        store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "strategy_baseline_impulse_override_enabled": False,
        })
        strategy = BaselineStrategy(event_store=store, settings=settings)

        snap_conflict = self._snap_with(composite=0.18, micro=-0.50)
        snap_neutral = self._snap_with(composite=0.18, micro=0.0)

        ref_a = _persist_snapshot(store, snap_conflict)
        baseline_a = strategy.evaluate(_make_context(feature_snapshot_ref=ref_a))

        ref_b = _persist_snapshot(store, snap_neutral)
        baseline_b = strategy.evaluate(_make_context(feature_snapshot_ref=ref_b))

        self.assertEqual(
            baseline_a.direction_threshold, baseline_b.direction_threshold,
            "Bug-2 修复后，microstructure 冲突不应额外收紧 threshold",
        )

    def test_alignment_bonus_still_applies_independently_of_micro(self) -> None:
        """alignment_bonus（multi_timeframe 对齐奖励）仍应生效 —— 它与 micro 正交."""
        store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "strategy_baseline_impulse_override_enabled": False,
            "strategy_baseline_trend_alpha_threshold": 0.16,
            "strategy_baseline_alignment_bonus": 0.03,
            "strategy_baseline_min_threshold_floor": 0.01,
        })
        strategy = BaselineStrategy(event_store=store, settings=settings)

        # 两个 case alpha 相同、micro 相同（neutral），仅 alignment 不同
        snap_flat_align = self._snap_with(composite=0.18, micro=0.0, alignment="flat")
        snap_long_align = self._snap_with(composite=0.18, micro=0.0, alignment="long")

        ref_a = _persist_snapshot(store, snap_flat_align)
        baseline_a = strategy.evaluate(_make_context(feature_snapshot_ref=ref_a))

        ref_b = _persist_snapshot(store, snap_long_align)
        baseline_b = strategy.evaluate(_make_context(feature_snapshot_ref=ref_b))

        self.assertAlmostEqual(
            baseline_a.direction_threshold - baseline_b.direction_threshold, 0.03, places=5,
            msg="alignment_bonus=0.03 场景应让 threshold 降 0.03",
        )


if __name__ == "__main__":
    unittest.main()
