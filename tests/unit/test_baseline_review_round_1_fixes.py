"""Baseline 审查第 1 轮修复 — regression tests.

锁定以下审查修复契约，防止后续回退:
  - M-4: FeatureCalculator 用 kline.ts 而非 snapshot.snapshot_ts 去 update
    RollingCandleState, 保证同一未闭合 K 线在多源快照 (mark-price/funding) 连续
    推送时仍能正确幂等.
  - M-3: okx_normalizer._build_snapshot 的 snapshot_ts 不把 funding.snapshot_ts
    纳入 max() 计算, 避免 fundingTime (未来结算时刻) 把 snapshot_ts 拉到未来.
  - M-1: RollingCandleState.adx 用双重 Wilder 平滑, 需要 >= 2*atr_window+1 根
    bar 才 ready (单纯 ATR ready 不保证 ADX ready).
  - B-1: LongShortRatioPoller._stop_event 惰性创建 (不在 __init__).
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.schemas.market import KlineBar, MarketSnapshot
from aats.services.feature_engine.calculator import FeatureCalculator
from aats.services.feature_engine.long_short_poller import LongShortRatioPoller
from aats.services.market_gateway.okx_normalizer import (
    OKXInstrumentMarketState,
    OKXMarketSnapshotNormalizer,
)


class Round1FixesRegressionTests(unittest.TestCase):
    # ────────────────────────────────────────────────────────────────
    # M-4: kline.ts vs snapshot.snapshot_ts 幂等
    # ────────────────────────────────────────────────────────────────

    def test_kline_ts_drives_state_not_snapshot_ts(self) -> None:
        """mark-price 推送导致 snapshot_ts > kline.ts 时，state.update 仍基于 kline.ts.

        场景:
          - 第 1 个 tick: kline_ts=T, snapshot_ts=T (mark 也 = T)
          - 第 2 个 tick: kline_ts=T (同根未闭合 15m), snapshot_ts=T+5min (mark 更新)
        修复前: state 把两个 tick 视为"新 ts"反复 append, bar_count=2, EMA 推了一步
        修复后: state 认为同 kline_ts → 覆盖 bar, bar_count=1, EMA 未推
        """
        calc = FeatureCalculator()
        kline_ts = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        snap1 = _snapshot_with_kline_ts(
            kline_15m_ts=kline_ts,
            kline_1h_ts=kline_ts,
            snapshot_ts=kline_ts,
            close_15m=67000.0,
        )
        calc.calculate(snap1, market_snapshot_ref="evt_1")
        state_15m = calc.rolling_state_snapshot()[("BTC-USDT-SWAP", "15m")]
        self.assertEqual(state_15m.bars_count(), 1)

        # 第 2 个 tick: kline_ts 不变, snapshot_ts 被 mark 推到更晚
        snap2 = _snapshot_with_kline_ts(
            kline_15m_ts=kline_ts,
            kline_1h_ts=kline_ts,
            snapshot_ts=kline_ts + timedelta(minutes=5),
            close_15m=67100.0,
        )
        calc.calculate(snap2, market_snapshot_ref="evt_2")
        self.assertEqual(
            state_15m.bars_count(), 1,
            "M-4 修复: 同 kline_ts 下 snapshot_ts 变化不应让 state append",
        )

    def test_kline_ts_none_fallback_to_snapshot_ts(self) -> None:
        """kline 没有 ts 字段 (旧 payload / dict 构造) → fallback 用 snapshot_ts."""
        calc = FeatureCalculator()
        # 用 dict 构造 kline (没显式 ts, KlineBar.ts=None)
        ts1 = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        snap1 = _snapshot_dict_kline(snapshot_ts=ts1, close_15m=67000.0)
        calc.calculate(snap1, market_snapshot_ref="evt_1")
        state_15m = calc.rolling_state_snapshot()[("BTC-USDT-SWAP", "15m")]
        self.assertEqual(state_15m.bars_count(), 1)

        # snapshot_ts 递进 → fallback 行为：视为新 ts，state 应 append
        ts2 = ts1 + timedelta(minutes=15)
        snap2 = _snapshot_dict_kline(snapshot_ts=ts2, close_15m=67100.0)
        calc.calculate(snap2, market_snapshot_ref="evt_2")
        self.assertEqual(state_15m.bars_count(), 2)

    # ────────────────────────────────────────────────────────────────
    # M-3: funding.snapshot_ts 不参与 max(ts_candidates)
    # ────────────────────────────────────────────────────────────────

    def test_funding_future_timestamp_does_not_pull_snapshot_ts_forward(self) -> None:
        """Normalizer 收到 fundingTime 在未来 (临近结算) 时, snapshot_ts 不被拉未来."""
        normalizer = OKXMarketSnapshotNormalizer()
        states: dict[str, OKXInstrumentMarketState] = {}
        inst = "BTC-USDT-SWAP"
        base_ms = 1_745_000_000_000

        normalizer.apply_message(
            message={
                "arg": {"channel": "tickers", "instId": inst},
                "data": [{
                    "instId": inst, "bidPx": "67000", "askPx": "67001",
                    "last": "67000.5", "bidSz": "3", "askSz": "2", "vol24h": "1000",
                    "ts": str(base_ms),
                }],
            },
            states=states,
        )
        normalizer.apply_message(
            message={
                "arg": {"channel": "candle15m", "instId": inst},
                "data": [[str(base_ms), "66800", "67200", "66700", "67100", "10", "670000", "670000", "1"]],
            },
            states=states,
        )
        normalizer.apply_message(
            message={
                "arg": {"channel": "candle1H", "instId": inst},
                "data": [[str(base_ms), "66800", "67200", "66700", "67100", "10", "670000", "670000", "1"]],
            },
            states=states,
        )
        # Funding 的 fundingTime 在未来 2 小时 (临近下次结算)
        future_funding_ms = base_ms + 2 * 3600 * 1000
        snapshots = normalizer.apply_message(
            message={
                "arg": {"channel": "funding-rate", "instId": inst},
                "data": [{
                    "instType": "SWAP", "instId": inst,
                    "fundingRate": "0.0003",
                    "fundingTime": str(future_funding_ms),
                }],
            },
            states=states,
        )
        self.assertEqual(len(snapshots), 1)
        snapshot_ts_ms = int(snapshots[0].snapshot_ts.timestamp() * 1000)
        self.assertLessEqual(
            snapshot_ts_ms, base_ms + 1000,
            "M-3 修复: funding 未来时刻不应把 snapshot_ts 拉到未来",
        )

    # ────────────────────────────────────────────────────────────────
    # M-1: ADX 双重 Wilder 平滑, 需要 2*atr_window+1 根 bar
    # ────────────────────────────────────────────────────────────────

    def test_adx_needs_double_wilder_window(self) -> None:
        """20 根 bar (满足 ATR ready) 但 ADX 仍 None, 因为 ADX 需要 29 根."""
        from aats.services.feature_engine.timeseries import RollingCandleState
        state = RollingCandleState(
            symbol="BTC", timeframe="15m", atr_window=14, roc_window=5,
        )
        base_ts = datetime(2026, 4, 19, tzinfo=timezone.utc)
        for i in range(20):
            state.update(
                KlineBar(
                    open=Decimal("100"), high=Decimal("101"),
                    low=Decimal("99"), close=Decimal("100"),
                ),
                ts=base_ts + timedelta(minutes=15 * i),
            )
        ind = state.indicators()
        self.assertTrue(ind.ready)
        self.assertIsNotNone(ind.atr)
        self.assertIsNone(ind.adx, "M-1 修复: ADX 需 2*atr_window+1 根，20 根不够")

    # ────────────────────────────────────────────────────────────────
    # B-1: LongShortRatioPoller._stop_event 惰性创建
    # ────────────────────────────────────────────────────────────────

    def test_ls_poller_stop_event_not_constructed_in_init(self) -> None:
        """B-1 审查修复: __init__ 不在同步路径创建 asyncio.Event (避免 loop 绑错)."""
        poller = LongShortRatioPoller(okx_rest_url="https://example.com")
        self.assertIsNone(
            poller._stop_event,
            "B-1 修复: _stop_event 应在 run_forever 首行惰性创建",
        )

    def test_ls_poller_stop_before_run_forever_still_works(self) -> None:
        """stop() 在 run_forever 启动前调用, run_forever 应立即退."""
        poller = LongShortRatioPoller(okx_rest_url="https://example.com")

        async def scenario() -> bool:
            await poller.stop()  # stop 先于 run_forever
            # run_forever 应立即 return (因为 _stop_event 已 set)
            await asyncio.wait_for(
                poller.run_forever(("BTC-USDT-SWAP",)),
                timeout=1.0,  # 1s 足够 immediately return
            )
            return True

        ok = asyncio.run(scenario())
        self.assertTrue(ok, "stop-before-run path 应不阻塞")


# ── Helpers ─────────────────────────────────────────────────────────


def _snapshot_with_kline_ts(
    *,
    kline_15m_ts: datetime,
    kline_1h_ts: datetime,
    snapshot_ts: datetime,
    close_15m: float,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC-USDT-SWAP", exchange="OKX", snapshot_ts=snapshot_ts,
        best_bid=Decimal(str(close_15m - 0.5)),
        best_ask=Decimal(str(close_15m + 0.5)),
        last_price=Decimal(str(close_15m)),
        bid_size=Decimal("3"), ask_size=Decimal("2"), volume_24h=Decimal("1000"),
        kline_15m=KlineBar(
            open=Decimal("66800"), high=Decimal("67200"),
            low=Decimal("66700"), close=Decimal(str(close_15m)), ts=kline_15m_ts,
        ),
        kline_1h=KlineBar(
            open=Decimal("66000"), high=Decimal("67300"),
            low=Decimal("65900"), close=Decimal(str(close_15m)), ts=kline_1h_ts,
        ),
        orderbook_depth={
            "bids": [{"price": close_15m - 0.5, "size": 5.0}],
            "asks": [{"price": close_15m + 0.5, "size": 4.0}],
        },
        recent_trades=[{"side": "buy", "size": 0.9}],
    )


def _snapshot_dict_kline(*, snapshot_ts: datetime, close_15m: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC-USDT-SWAP", exchange="OKX", snapshot_ts=snapshot_ts,
        best_bid=Decimal(str(close_15m - 0.5)),
        best_ask=Decimal(str(close_15m + 0.5)),
        last_price=Decimal(str(close_15m)),
        bid_size=Decimal("3"), ask_size=Decimal("2"), volume_24h=Decimal("1000"),
        kline_15m={"open": 66800, "high": 67200, "low": 66700, "close": close_15m},
        kline_1h={"open": 66000, "high": 67300, "low": 65900, "close": close_15m},
        orderbook_depth={
            "bids": [{"price": close_15m - 0.5, "size": 5.0}],
            "asks": [{"price": close_15m + 0.5, "size": 4.0}],
        },
        recent_trades=[{"side": "buy", "size": 0.9}],
    )


if __name__ == "__main__":
    unittest.main()
