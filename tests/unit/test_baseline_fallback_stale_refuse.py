"""Bug-3 修复契约：baseline fallback 过期保护.

当 ``event_store.latest`` 兜底的 FeatureSnapshot 比 decision context 的
``created_at`` 老超过 ``strategy_baseline_fallback_max_stale_seconds``，必须
raise RuntimeError 拒绝 decision —— 继续用过期 snapshot 做决策比 raise 更危险
（系统在基于"几分钟前的行情"下单）。

契约:
  1. 新鲜 fallback（age < 限额）照常 WARN + 使用（兼容原 R4-D3 行为）
  2. 过期 fallback（age > 限额）raise，错误消息包含 age 和 limit
  3. feature flag 关闭时完全回退旧行为（紧急回滚）
"""

from __future__ import annotations

import unittest
from datetime import timedelta

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


def _feature_snapshot_at(snapshot_age_seconds: float) -> FeatureSnapshot:
    """构造一个 snapshot_ts = now - snapshot_age_seconds 的 FeatureSnapshot."""
    now = utc_now()
    old_ts = now - timedelta(seconds=snapshot_age_seconds)
    market = MarketSnapshot(
        created_at=old_ts,
        symbol="BTC-USDT-SWAP",
        exchange="OKX",
        snapshot_ts=old_ts,
        best_bid=67000.0,
        best_ask=67001.0,
        last_price=67000.5,
        bid_size=4.0,
        ask_size=2.2,
        volume_24h=1000.0,
        kline_15m={"open": 66800.0, "high": 67200.0, "low": 66700.0, "close": 67100.0},
        kline_1h={"open": 66000.0, "high": 67300.0, "low": 65900.0, "close": 67100.0},
        orderbook_depth={
            "bids": [{"price": 67000.0, "size": 7.0}],
            "asks": [{"price": 67001.0, "size": 4.0}],
        },
        recent_trades=[{"side": "buy", "size": 1.0}],
    )
    # FeatureCalculator.calculate 返回的 snapshot 的 snapshot_ts 继承自
    # market_snapshot.snapshot_ts，所以这里直接用 old_ts。
    return FeatureCalculator().calculate(market, market_snapshot_ref="evt_market")


def _context_now() -> DecisionContext:
    now = utc_now()
    return DecisionContext(
        decision_id="dec_bug3_test",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        as_of_ts=now,
        market_snapshot_ref="evt_market",
        feature_snapshot_ref="evt_never_existed_bogus",  # 触发 fallback
        portfolio_snapshot_ref="evt_portfolio",
        health_snapshot_ref="evt_health",
        mode="guarded_live",
        current_position_qty=0.0,
        product_type="derivatives",
        current_exposure_side="flat",
        current_target_leverage=1.0,
    )


class BaselineFallbackStaleRefuseTests(unittest.TestCase):
    def test_fresh_fallback_allowed_with_warning(self) -> None:
        """fallback 的 snapshot_ts 在限额内 → 照常 WARN + 继续（保留 R4-D3 语义）."""
        store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "strategy_baseline_fallback_ts_check_enabled": True,
            "strategy_baseline_fallback_max_stale_seconds": 60.0,
        })
        strategy = BaselineStrategy(event_store=store, settings=settings)

        snap = _feature_snapshot_at(snapshot_age_seconds=5.0)  # 5s 内，新鲜
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=snap.symbol,
            payload_model=snap,
            source_component="test",
        )
        store.append(event)

        with self.assertLogs("aats.decision_engine.baseline", level="WARNING") as captured:
            baseline = strategy.evaluate(_context_now())

        self.assertIsNotNone(baseline)
        self.assertTrue(
            any("baseline_feature_ref_miss_fallback" in line for line in captured.output),
            f"新鲜 fallback 应触发 R4-D3 warning，got: {captured.output}",
        )
        self.assertFalse(
            any("baseline_feature_fallback_stale_refused" in line for line in captured.output),
            "新鲜 fallback 不应触发 stale refusal",
        )

    def test_stale_fallback_raises_and_logs_error(self) -> None:
        """fallback 距今 > 限额 → raise RuntimeError."""
        store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "strategy_baseline_fallback_ts_check_enabled": True,
            "strategy_baseline_fallback_max_stale_seconds": 60.0,
        })
        strategy = BaselineStrategy(event_store=store, settings=settings)

        snap_stale = _feature_snapshot_at(snapshot_age_seconds=300.0)  # 5 分钟前
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=snap_stale.symbol,
            payload_model=snap_stale,
            source_component="test",
        )
        store.append(event)

        with self.assertRaises(RuntimeError) as ctx:
            with self.assertLogs("aats.decision_engine.baseline", level="ERROR") as captured:
                strategy.evaluate(_context_now())

        self.assertIn("stale", str(ctx.exception).lower())
        self.assertIn("60.0", str(ctx.exception))
        self.assertTrue(
            any("baseline_feature_fallback_stale_refused" in line for line in captured.output),
            f"过期 fallback 必须打 error 日志，got: {captured.output}",
        )

    def test_flag_disabled_falls_back_even_if_stale(self) -> None:
        """紧急回滚：flag 关闭后应恢复旧行为（stale 也照常 WARN + 继续）."""
        store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "strategy_baseline_fallback_ts_check_enabled": False,  # 关掉
            "strategy_baseline_fallback_max_stale_seconds": 60.0,
        })
        strategy = BaselineStrategy(event_store=store, settings=settings)

        snap_stale = _feature_snapshot_at(snapshot_age_seconds=300.0)
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=snap_stale.symbol,
            payload_model=snap_stale,
            source_component="test",
        )
        store.append(event)

        with self.assertLogs("aats.decision_engine.baseline", level="WARNING") as captured:
            baseline = strategy.evaluate(_context_now())

        self.assertIsNotNone(baseline, "flag off → 不 raise，回退旧行为")
        self.assertFalse(
            any("stale_refused" in line for line in captured.output),
            "flag off 时不应触发 stale refusal 日志",
        )


if __name__ == "__main__":
    unittest.main()
