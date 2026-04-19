"""OKX mark-price 频道 normalizer 契约 (P1.4).

锁定契约:
  1. ``mark-price`` 推送 → OKXInstrumentMarketState.mark_price 被写入
  2. 之后若 ticker + candle_15m + candle_1h 都齐备，_build_snapshot 的 MarketSnapshot
     必须带上 mark_price 字段
  3. mark_price 的 snapshot_ts 纳入 snapshot_ts = max(...) 的计算
  4. 缺 ``markPx`` / ``ts`` 字段的推送 → raise ValueError（供 _handle_okx_message
     走 schema warning 路径，不参与系统错误升级计数）
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aats.services.market_gateway.okx_normalizer import (
    OKXInstrumentMarketState,
    OKXMarketSnapshotNormalizer,
)


def _ticker_message(inst_id: str, *, ts_ms: int, last: str = "67000.5") -> dict:
    return {
        "arg": {"channel": "tickers", "instId": inst_id},
        "data": [{
            "instId": inst_id,
            "bidPx": "67000.0",
            "askPx": "67001.0",
            "last": last,
            "bidSz": "3.0",
            "askSz": "2.0",
            "vol24h": "1000",
            "ts": str(ts_ms),
        }],
    }


def _candle_message(inst_id: str, *, channel: str, ts_ms: int, close: str = "67100") -> dict:
    return {
        "arg": {"channel": channel, "instId": inst_id},
        "data": [[str(ts_ms), "66800", "67200", "66700", close, "10.0", "670000", "670000", "1"]],
    }


def _mark_price_message(inst_id: str, *, ts_ms: int, markPx: str = "67050.0") -> dict:
    return {
        "arg": {"channel": "mark-price", "instId": inst_id},
        "data": [{
            "instType": "SWAP",
            "instId": inst_id,
            "markPx": markPx,
            "ts": str(ts_ms),
        }],
    }


class OKXMarkPriceNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = OKXMarketSnapshotNormalizer(exchange_name="OKX")
        self.states: dict[str, OKXInstrumentMarketState] = {}
        self.inst_id = "BTC-USDT-SWAP"

    def test_mark_price_message_populates_state(self) -> None:
        """仅 mark-price 推送 → state.mark_price 被写入，但 snapshot 尚未 build
        (因为还缺 ticker + candle)."""
        snapshots = self.normalizer.apply_message(
            message=_mark_price_message(self.inst_id, ts_ms=1_745_000_000_000),
            states=self.states,
        )
        self.assertEqual(snapshots, [])  # 缺 ticker/candle，snapshot 还没 build
        state = self.states[self.inst_id]
        self.assertIsNotNone(state.mark_price)
        assert state.mark_price is not None
        self.assertEqual(state.mark_price.mark_price, Decimal("67050.0"))
        self.assertEqual(state.mark_price.snapshot_ts.tzinfo, timezone.utc)

    def test_mark_price_feeds_into_market_snapshot_once_other_streams_present(self) -> None:
        """mark-price + ticker + candle_15m + candle_1h 齐备 → MarketSnapshot.mark_price 非 None."""
        self.normalizer.apply_message(
            message=_ticker_message(self.inst_id, ts_ms=1_745_000_000_000),
            states=self.states,
        )
        self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle15m", ts_ms=1_745_000_000_000),
            states=self.states,
        )
        self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle1H", ts_ms=1_745_000_000_000),
            states=self.states,
        )
        # 最后到达 mark-price → 这一刻 snapshot 会被 build 并返回
        snapshots = self.normalizer.apply_message(
            message=_mark_price_message(self.inst_id, ts_ms=1_745_000_000_000, markPx="67050.5"),
            states=self.states,
        )
        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots[0]
        self.assertEqual(snapshot.mark_price, Decimal("67050.5"))

    def test_snapshot_without_mark_price_has_none_mark_field(self) -> None:
        """只有 ticker + candle 的 snapshot 里 mark_price 字段应为 None（向后兼容）."""
        self.normalizer.apply_message(
            message=_ticker_message(self.inst_id, ts_ms=1_745_000_000_000),
            states=self.states,
        )
        self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle15m", ts_ms=1_745_000_000_000),
            states=self.states,
        )
        snapshots = self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle1H", ts_ms=1_745_000_000_000),
            states=self.states,
        )
        self.assertEqual(len(snapshots), 1)
        self.assertIsNone(snapshots[0].mark_price)

    def test_mark_price_ts_raises_snapshot_ts_when_newest(self) -> None:
        """snapshot_ts = max(ticker, candle_15m, candle_1h, mark_price).
        若 mark-price 最新，snapshot_ts 应取它的 ts。"""
        # 较早的 ticker/candle
        self.normalizer.apply_message(
            message=_ticker_message(self.inst_id, ts_ms=1_745_000_000_000),
            states=self.states,
        )
        self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle15m", ts_ms=1_745_000_000_000),
            states=self.states,
        )
        self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle1H", ts_ms=1_745_000_000_000),
            states=self.states,
        )
        # 更新的 mark-price
        newer_ts = 1_745_000_005_000
        snapshots = self.normalizer.apply_message(
            message=_mark_price_message(self.inst_id, ts_ms=newer_ts),
            states=self.states,
        )
        self.assertEqual(len(snapshots), 1)
        expected_ts = datetime.fromtimestamp(newer_ts / 1000.0, tz=timezone.utc)
        self.assertEqual(snapshots[0].snapshot_ts, expected_ts)

    def test_mark_price_payload_missing_markPx_raises_valueerror(self) -> None:
        """缺 markPx 字段应 raise ValueError（走 schema warning 路径）."""
        bogus = {
            "arg": {"channel": "mark-price", "instId": self.inst_id},
            "data": [{"instType": "SWAP", "instId": self.inst_id, "ts": "1"}],  # 少了 markPx
        }
        with self.assertRaises(ValueError) as ctx:
            self.normalizer.apply_message(message=bogus, states=self.states)
        self.assertIn("mark_price", str(ctx.exception).lower())

    def test_mark_price_payload_missing_ts_raises_valueerror(self) -> None:
        bogus = {
            "arg": {"channel": "mark-price", "instId": self.inst_id},
            "data": [{"instType": "SWAP", "instId": self.inst_id, "markPx": "1"}],  # 少了 ts
        }
        with self.assertRaises(ValueError):
            self.normalizer.apply_message(message=bogus, states=self.states)


if __name__ == "__main__":
    unittest.main()
