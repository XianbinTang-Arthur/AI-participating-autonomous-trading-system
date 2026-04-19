"""OKX open-interest 频道 normalizer 契约 (P1.6)."""

from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.market_gateway.okx_normalizer import (
    OKXInstrumentMarketState,
    OKXMarketSnapshotNormalizer,
)


def _ticker_message(inst_id: str, *, ts_ms: int) -> dict:
    return {
        "arg": {"channel": "tickers", "instId": inst_id},
        "data": [{
            "instId": inst_id, "bidPx": "67000.0", "askPx": "67001.0",
            "last": "67000.5", "bidSz": "3.0", "askSz": "2.0", "vol24h": "1000",
            "ts": str(ts_ms),
        }],
    }


def _candle_message(inst_id: str, *, channel: str, ts_ms: int) -> dict:
    return {
        "arg": {"channel": channel, "instId": inst_id},
        "data": [[str(ts_ms), "66800", "67200", "66700", "67100", "10", "670000", "670000", "1"]],
    }


def _oi_message(inst_id: str, *, ts_ms: int, oi: str = "40000000", oi_ccy: str | None = "40000") -> dict:
    data: dict = {"instType": "SWAP", "instId": inst_id, "oi": oi, "ts": str(ts_ms)}
    if oi_ccy is not None:
        data["oiCcy"] = oi_ccy
    return {"arg": {"channel": "open-interest", "instId": inst_id}, "data": [data]}


class OKXOpenInterestNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = OKXMarketSnapshotNormalizer()
        self.states: dict[str, OKXInstrumentMarketState] = {}
        self.inst_id = "BTC-USDT-SWAP"

    def test_oi_message_populates_state(self) -> None:
        self.normalizer.apply_message(
            message=_oi_message(self.inst_id, ts_ms=1_745_000_000_000, oi="40000000"),
            states=self.states,
        )
        oi_state = self.states[self.inst_id].open_interest
        self.assertIsNotNone(oi_state)
        assert oi_state is not None
        self.assertEqual(oi_state.open_interest, Decimal("40000000"))
        self.assertEqual(oi_state.open_interest_ccy, Decimal("40000"))

    def test_oi_feeds_into_market_snapshot(self) -> None:
        ts_ms = 1_745_000_000_000
        self.normalizer.apply_message(message=_ticker_message(self.inst_id, ts_ms=ts_ms), states=self.states)
        self.normalizer.apply_message(message=_candle_message(self.inst_id, channel="candle15m", ts_ms=ts_ms), states=self.states)
        self.normalizer.apply_message(message=_candle_message(self.inst_id, channel="candle1H", ts_ms=ts_ms), states=self.states)
        snapshots = self.normalizer.apply_message(
            message=_oi_message(self.inst_id, ts_ms=ts_ms, oi="40123456", oi_ccy="40123.456"),
            states=self.states,
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].open_interest, Decimal("40123456"))
        self.assertEqual(snapshots[0].open_interest_ccy, Decimal("40123.456"))

    def test_oi_without_oiCcy_still_parses(self) -> None:
        self.normalizer.apply_message(
            message=_oi_message(self.inst_id, ts_ms=1_745_000_000_000, oi_ccy=None),
            states=self.states,
        )
        oi_state = self.states[self.inst_id].open_interest
        assert oi_state is not None
        self.assertIsNone(oi_state.open_interest_ccy)

    def test_oi_missing_oi_raises(self) -> None:
        bogus = {"arg": {"channel": "open-interest", "instId": self.inst_id},
                 "data": [{"instType": "SWAP", "instId": self.inst_id, "ts": "1"}]}
        with self.assertRaises(ValueError):
            self.normalizer.apply_message(message=bogus, states=self.states)

    def test_oi_missing_ts_raises(self) -> None:
        bogus = {"arg": {"channel": "open-interest", "instId": self.inst_id},
                 "data": [{"instType": "SWAP", "instId": self.inst_id, "oi": "1"}]}
        with self.assertRaises(ValueError):
            self.normalizer.apply_message(message=bogus, states=self.states)


if __name__ == "__main__":
    unittest.main()
