"""Baseline 审查第 2 轮修复 — regression tests.

锁定契约:
  - R2-B1: stop_background_tasks 先调 poller.stop() 再 cancel tasks.
  - R2-B2: FeatureEngine.handle_market_snapshot 用 per-symbol asyncio.Lock 保证
    同 symbol 串行，NATS 并发调度下不发生 state 竞态.
  - R2-M1: baseline fallback age 用 abs(signed_age), clock skew 负值也拒绝.
  - R2-M2: LongShortRatioPoller._last_error 仅本轮所有 symbol 都新获得 sample 时清.
  - R2-M3: _extract_ls_ratio naive as_of_ts 拒绝而非隐式假设 UTC.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import DecisionContext
from aats.schemas.market import KlineBar, MarketSnapshot
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.feature_engine.calculator import FeatureCalculator, FeatureEngine
from aats.services.feature_engine.long_short_poller import (
    LongShortRatioPoller,
    LongShortRatioSample,
)
from aats.storage.event_store import InMemoryEventStore


class Round2FixesRegressionTests(unittest.TestCase):
    # ────────────────────────────────────────────────────────────────
    # R2-B2: FeatureEngine per-symbol lock
    # ────────────────────────────────────────────────────────────────

    def test_feature_engine_lock_exists_per_symbol(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        engine = FeatureEngine(bus=bus, calculator=FeatureCalculator())
        # Lock 懒创建
        lock_btc = engine._lock_for_symbol("BTC-USDT-SWAP")
        lock_eth = engine._lock_for_symbol("ETH-USDT-SWAP")
        self.assertIsInstance(lock_btc, asyncio.Lock)
        self.assertIsNot(lock_btc, lock_eth, "不同 symbol 必须独立 lock")
        # 同 symbol 幂等返回同一实例
        self.assertIs(engine._lock_for_symbol("BTC-USDT-SWAP"), lock_btc)

    def test_concurrent_handle_market_snapshot_same_symbol_serializes(self) -> None:
        """两条同 symbol MARKET_SNAPSHOTS 并发触发时, state 不被双重构造."""
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        calc = FeatureCalculator()
        engine = FeatureEngine(bus=bus, calculator=calc)
        ts = utc_now()
        snap = _snap(ts=ts, close=67000.0)
        envelope = build_envelope(
            topic=topics.MARKET_SNAPSHOTS,
            key=snap.symbol,
            payload_model=snap,
            source_component="test",
        )
        message = {"payload": envelope.model_dump(mode="json")}

        async def scenario() -> None:
            # 并发 10 条同 symbol 消息
            await asyncio.gather(
                *[engine.handle_market_snapshot(message) for _ in range(10)],
            )

        asyncio.run(scenario())
        # 同 symbol 同 ts 幂等 → state 只应 1 个 bar
        states = calc.rolling_state_snapshot()
        self.assertEqual(states[(snap.symbol, "15m")].bars_count(), 1)
        self.assertEqual(states[(snap.symbol, "1h")].bars_count(), 1)

    # ────────────────────────────────────────────────────────────────
    # R2-M1: clock skew 负 age 也拒绝
    # ────────────────────────────────────────────────────────────────

    def test_fallback_negative_age_also_refused(self) -> None:
        """本地时钟比 fallback_dt 早超过 max_stale → signed_age 大负 → abs > max_stale 拒绝."""
        store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "strategy_baseline_fallback_ts_check_enabled": True,
            "strategy_baseline_fallback_max_stale_seconds": 60.0,
        })
        strategy = BaselineStrategy(event_store=store, settings=settings)
        # fallback snapshot_ts 比 decision now 晚 5 分钟 (clock skew 正过去)
        now = utc_now()
        future_ts = now + timedelta(seconds=300)
        market = MarketSnapshot(
            symbol="BTC-USDT-SWAP", exchange="OKX", snapshot_ts=future_ts,
            best_bid=Decimal("67000"), best_ask=Decimal("67001"),
            last_price=Decimal("67000.5"), bid_size=Decimal("3"), ask_size=Decimal("2"),
            volume_24h=Decimal("1000"),
            kline_15m={"open": 66800, "high": 67200, "low": 66700, "close": 67100},
            kline_1h={"open": 66000, "high": 67300, "low": 65900, "close": 67100},
            orderbook_depth={
                "bids": [{"price": 67000, "size": 5}],
                "asks": [{"price": 67001, "size": 4}],
            },
            recent_trades=[{"side": "buy", "size": 0.9}],
        )
        snap = FeatureCalculator().calculate(market, market_snapshot_ref="evt_m")
        event = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS, key=snap.symbol,
            payload_model=snap, source_component="test",
        )
        store.append(event)
        context = DecisionContext(
            decision_id="dec_skew_test", symbol="BTC-USDT-SWAP", timeframe="15m",
            as_of_ts=now, market_snapshot_ref="evt_market",
            feature_snapshot_ref="evt_bogus",  # 触发 fallback
            portfolio_snapshot_ref="evt_p", health_snapshot_ref="evt_h",
            mode="guarded_live", current_position_qty=0.0, product_type="derivatives",
            current_exposure_side="flat", current_target_leverage=1.0,
        )
        with self.assertRaises(RuntimeError) as ctx:
            strategy.evaluate(context)
        self.assertIn("stale", str(ctx.exception).lower())

    # ────────────────────────────────────────────────────────────────
    # R2-M2: _last_error 清理逻辑
    # ────────────────────────────────────────────────────────────────

    def test_last_error_not_cleared_when_some_symbol_fails_in_round(self) -> None:
        """本轮部分 symbol 失败时, 已缓存 symbol 的 cache 非 None, 但 last_error 不清."""
        poller = LongShortRatioPoller(okx_rest_url="https://example.com")
        poller._last_error = "previous error"
        # 预先填充缓存 (上一轮成功)
        poller._cache["BTC-USDT-SWAP"] = LongShortRatioSample(
            symbol="BTC-USDT-SWAP", ts=utc_now(), ls_ratio=2.0,
        )
        poller._cache["ETH-USDT-SWAP"] = LongShortRatioSample(
            symbol="ETH-USDT-SWAP", ts=utc_now(), ls_ratio=1.5,
        )

        # 本轮: BTC 成功, ETH 失败 (mock _poll_one 返回 None for ETH)
        async def fake_poll_one(client, symbol):
            if symbol == "BTC-USDT-SWAP":
                return LongShortRatioSample(
                    symbol=symbol, ts=utc_now(), ls_ratio=2.2,
                )
            return None  # 静默失败

        poller._poll_one = fake_poll_one  # type: ignore[method-assign]
        asyncio.run(poller._poll_round(("BTC-USDT-SWAP", "ETH-USDT-SWAP")))

        self.assertIsNotNone(
            poller._last_error,
            "R2-M2: 部分失败时 _last_error 不应被清 (即使 cache 里旧值非 None)",
        )

    def test_last_error_cleared_only_when_all_symbols_succeed_this_round(self) -> None:
        poller = LongShortRatioPoller(okx_rest_url="https://example.com")
        poller._last_error = "previous error"

        async def fake_poll_one(client, symbol):
            return LongShortRatioSample(
                symbol=symbol, ts=utc_now(), ls_ratio=2.0,
            )

        poller._poll_one = fake_poll_one  # type: ignore[method-assign]
        asyncio.run(poller._poll_round(("BTC-USDT-SWAP", "ETH-USDT-SWAP")))
        self.assertIsNone(
            poller._last_error,
            "所有 symbol 都拿到新 sample 才清 last_error",
        )

    # ────────────────────────────────────────────────────────────────
    # R2-M3: _extract_ls_ratio naive as_of_ts 拒绝
    # ────────────────────────────────────────────────────────────────

    def test_extract_ls_ratio_rejects_naive_as_of_ts(self) -> None:
        poller = LongShortRatioPoller(okx_rest_url="https://example.com")
        poller._cache["BTC-USDT-SWAP"] = LongShortRatioSample(
            symbol="BTC-USDT-SWAP", ts=utc_now(), ls_ratio=2.0,
        )
        calc = FeatureCalculator(
            long_short_poller=poller, enable_ls_ratio_signal=True,
        )
        # 传 naive datetime → 应返回 None, 不强加 UTC
        naive_ts = datetime.now()  # no tz
        assert naive_ts.tzinfo is None
        ratio = calc._extract_ls_ratio("BTC-USDT-SWAP", naive_ts)
        self.assertIsNone(
            ratio,
            "R2-M3: naive as_of_ts 应拒绝而非隐式假设 UTC",
        )


def _snap(*, ts: datetime, close: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC-USDT-SWAP", exchange="OKX", snapshot_ts=ts,
        best_bid=Decimal(str(close - 0.5)), best_ask=Decimal(str(close + 0.5)),
        last_price=Decimal(str(close)),
        bid_size=Decimal("3"), ask_size=Decimal("2"), volume_24h=Decimal("1000"),
        kline_15m=KlineBar(
            open=Decimal("66800"), high=Decimal("67200"),
            low=Decimal("66700"), close=Decimal(str(close)), ts=ts,
        ),
        kline_1h=KlineBar(
            open=Decimal("66000"), high=Decimal("67300"),
            low=Decimal("65900"), close=Decimal(str(close)), ts=ts,
        ),
        orderbook_depth={
            "bids": [{"price": close - 0.5, "size": 5}],
            "asks": [{"price": close + 0.5, "size": 4}],
        },
        recent_trades=[{"side": "buy", "size": 0.9}],
    )


if __name__ == "__main__":
    unittest.main()
