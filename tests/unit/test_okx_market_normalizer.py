from __future__ import annotations

import unittest

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
        self.assertAlmostEqual(snapshot.best_bid, 67250.0)
        self.assertAlmostEqual(snapshot.best_ask, 67250.2)
        self.assertAlmostEqual(snapshot.last_price, 67250.1)
        self.assertAlmostEqual(snapshot.volume_24h, 1234.5)
        self.assertEqual(snapshot.kline_15m["close"], 67250.1)
        self.assertEqual(snapshot.kline_1h["high"], 67400.0)


if __name__ == "__main__":
    unittest.main()
