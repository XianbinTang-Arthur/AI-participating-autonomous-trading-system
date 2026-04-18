from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.services.market_gateway.okx_normalizer import OKXMarketSnapshotNormalizer


class TestOKXMarketSnapshotNormalizer(unittest.TestCase):
    def test_normalizes_ticker_and_candles_into_market_snapshot(self) -> None:
        normalizer = OKXMarketSnapshotNormalizer()
        states = {}

        ticker_message = {
            "arg": {"channel": "tickers", "instId": "BTC-USDT"},
            "data": [
                {
                    "instId": "BTC-USDT",
                    "last": "67250.1",
                    "lastSz": "0.001",
                    "askPx": "67250.2",
                    "askSz": "1.1",
                    "bidPx": "67250.0",
                    "bidSz": "1.2",
                    "open24h": "66900.0",
                    "high24h": "67500.0",
                    "low24h": "66000.0",
                    "vol24h": "1234.5",
                    "volCcy24h": "83000000",
                    "ts": "1710000000000",
                }
            ],
        }
        candle_15m = {
            "arg": {"channel": "candle15m", "instId": "BTC-USDT"},
            "data": [["1710000000000", "67000", "67300", "66950", "67250.1", "12.5", "0", "0", "1"]],
        }
        candle_1h = {
            "arg": {"channel": "candle1H", "instId": "BTC-USDT"},
            "data": [["1710000000000", "66800", "67400", "66700", "67250.1", "42.0", "0", "0", "1"]],
        }

        self.assertEqual(normalizer.apply_message(message=ticker_message, states=states), [])
        self.assertEqual(normalizer.apply_message(message=candle_15m, states=states), [])
        snapshots = normalizer.apply_message(message=candle_1h, states=states)

        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots[0]
        self.assertEqual(snapshot.symbol, "BTC-USDT")
        self.assertEqual(snapshot.exchange, "OKX")
        self.assertEqual(snapshot.best_bid, Decimal("67250.0"))
        self.assertEqual(snapshot.best_ask, Decimal("67250.2"))
        self.assertEqual(snapshot.last_price, Decimal("67250.1"))
        self.assertEqual(snapshot.volume_24h, Decimal("1234.5"))
        self.assertEqual(snapshot.kline_15m["close"], Decimal("67250.1"))
        self.assertEqual(snapshot.kline_1h["high"], Decimal("67400.0"))


class TestOKXMarketNormalizerCandleTsRegression(unittest.TestCase):
    """R6-M1：new_ts < last_ts（乱序 / 重放）时必须落 warning，
    且**不更新** _last_candle_ts；否则下次 gap 检测会基于回退的 ts
    判断，静默漏检真实缺口。
    """

    def _ts(self, ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    def test_candle_ts_regression_logged_and_tracking_not_rolled_back(self) -> None:
        normalizer = OKXMarketSnapshotNormalizer()
        symbol = "BTC-USDT"
        channel = "candle15m"
        last_ts = self._ts(1_710_000_900_000)
        old_ts = self._ts(1_710_000_000_000)

        normalizer._last_candle_ts[(symbol, channel)] = last_ts

        with self.assertLogs("aats.okx_normalizer", level="WARNING") as captured:
            normalizer._check_candle_gap(symbol=symbol, channel=channel, new_ts=old_ts)

        self.assertEqual(
            normalizer._last_candle_ts[(symbol, channel)],
            last_ts,
            "回退的 new_ts 不得覆盖 _last_candle_ts，否则后续 gap 检测失准",
        )
        self.assertEqual(normalizer._detected_gaps, [])
        self.assertTrue(
            any("okx_candle_ts_regression" in rec.getMessage() for rec in captured.records),
            "回退必须落 warning 让 ops 可见",
        )

    def test_candle_ts_equal_still_early_returns(self) -> None:
        normalizer = OKXMarketSnapshotNormalizer()
        symbol = "BTC-USDT"
        channel = "candle15m"
        ts = self._ts(1_710_000_000_000)
        normalizer._last_candle_ts[(symbol, channel)] = ts
        normalizer._check_candle_gap(symbol=symbol, channel=channel, new_ts=ts)
        self.assertEqual(normalizer._last_candle_ts[(symbol, channel)], ts)
        self.assertEqual(normalizer._detected_gaps, [])

    def test_candle_ts_forward_gap_still_detected(self) -> None:
        """确保 R6-M1 的回退分支不会误伤正向 gap 检测路径。"""
        normalizer = OKXMarketSnapshotNormalizer()
        symbol = "BTC-USDT"
        channel = "candle15m"
        last_ts = self._ts(1_710_000_000_000)
        # 正向跨 3 个 15m K 线，应被判定为 gap
        new_ts = last_ts + timedelta(minutes=45)
        normalizer._last_candle_ts[(symbol, channel)] = last_ts
        normalizer._check_candle_gap(symbol=symbol, channel=channel, new_ts=new_ts)
        self.assertEqual(len(normalizer._detected_gaps), 1)
        gap = normalizer._detected_gaps[0]
        self.assertEqual(gap.last_ts, last_ts)
        self.assertEqual(gap.new_ts, new_ts)


if __name__ == "__main__":
    unittest.main()
