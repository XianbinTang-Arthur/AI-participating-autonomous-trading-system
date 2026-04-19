"""OKX funding-rate 频道 normalizer 契约 (P1.5).

锁定:
  1. funding-rate 推送 → OKXInstrumentMarketState.funding 被写入
  2. MarketSnapshot 正确填充 funding_rate / next_funding_rate / next_funding_time
  3. 缺字段 → ValueError 走 schema warning 路径
  4. nextFundingRate / nextFundingTime 可空 —— 某些 settlement 方法不返回这些
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


def _funding_message(
    inst_id: str, *, funding_time_ms: int, rate: str = "0.0001",
    next_rate: str | None = "0.00015", next_time_ms: int | None = None,
) -> dict:
    data: dict = {
        "instType": "SWAP",
        "instId": inst_id,
        "fundingRate": rate,
        "fundingTime": str(funding_time_ms),
        "method": "current_period",
    }
    if next_rate is not None:
        data["nextFundingRate"] = next_rate
    if next_time_ms is not None:
        data["nextFundingTime"] = str(next_time_ms)
    return {
        "arg": {"channel": "funding-rate", "instId": inst_id},
        "data": [data],
    }


class OKXFundingRateNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = OKXMarketSnapshotNormalizer(exchange_name="OKX")
        self.states: dict[str, OKXInstrumentMarketState] = {}
        self.inst_id = "BTC-USDT-SWAP"

    def test_funding_message_populates_state(self) -> None:
        self.normalizer.apply_message(
            message=_funding_message(
                self.inst_id,
                funding_time_ms=1_745_000_000_000,
                rate="0.0003",
                next_rate="0.0002",
                next_time_ms=1_745_028_800_000,
            ),
            states=self.states,
        )
        funding = self.states[self.inst_id].funding
        self.assertIsNotNone(funding)
        assert funding is not None
        self.assertEqual(funding.funding_rate, Decimal("0.0003"))
        self.assertEqual(funding.next_funding_rate, Decimal("0.0002"))
        self.assertEqual(
            funding.next_funding_time,
            datetime.fromtimestamp(1_745_028_800_000 / 1000.0, tz=timezone.utc),
        )

    def test_funding_feeds_into_market_snapshot(self) -> None:
        """ticker + candle_15m + candle_1h + funding 齐备 → MarketSnapshot 带 funding."""
        ts_ms = 1_745_000_000_000
        self.normalizer.apply_message(
            message=_ticker_message(self.inst_id, ts_ms=ts_ms), states=self.states,
        )
        self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle15m", ts_ms=ts_ms),
            states=self.states,
        )
        self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle1H", ts_ms=ts_ms),
            states=self.states,
        )
        snapshots = self.normalizer.apply_message(
            message=_funding_message(
                self.inst_id, funding_time_ms=ts_ms,
                rate="0.00025", next_rate="0.00030", next_time_ms=ts_ms + 28_800_000,
            ),
            states=self.states,
        )
        self.assertEqual(len(snapshots), 1)
        snap = snapshots[0]
        self.assertEqual(snap.funding_rate, Decimal("0.00025"))
        self.assertEqual(snap.next_funding_rate, Decimal("0.00030"))
        self.assertIsNotNone(snap.next_funding_time)

    def test_snapshot_without_funding_has_none_fields(self) -> None:
        """funding 未到 → MarketSnapshot 的 funding_* 字段都 None."""
        ts_ms = 1_745_000_000_000
        self.normalizer.apply_message(message=_ticker_message(self.inst_id, ts_ms=ts_ms), states=self.states)
        self.normalizer.apply_message(message=_candle_message(self.inst_id, channel="candle15m", ts_ms=ts_ms), states=self.states)
        snapshots = self.normalizer.apply_message(
            message=_candle_message(self.inst_id, channel="candle1H", ts_ms=ts_ms),
            states=self.states,
        )
        self.assertEqual(len(snapshots), 1)
        self.assertIsNone(snapshots[0].funding_rate)
        self.assertIsNone(snapshots[0].next_funding_rate)
        self.assertIsNone(snapshots[0].next_funding_time)

    def test_funding_with_no_next_fields_still_parses(self) -> None:
        """nextFundingRate / nextFundingTime 可空（某些 settlement 方法）."""
        self.normalizer.apply_message(
            message=_funding_message(
                self.inst_id, funding_time_ms=1_745_000_000_000,
                rate="0.0001", next_rate=None, next_time_ms=None,
            ),
            states=self.states,
        )
        funding = self.states[self.inst_id].funding
        assert funding is not None
        self.assertEqual(funding.funding_rate, Decimal("0.0001"))
        self.assertIsNone(funding.next_funding_rate)
        self.assertIsNone(funding.next_funding_time)

    def test_funding_missing_fundingRate_raises(self) -> None:
        bogus = {
            "arg": {"channel": "funding-rate", "instId": self.inst_id},
            "data": [{"instType": "SWAP", "instId": self.inst_id, "fundingTime": "1"}],
        }
        with self.assertRaises(ValueError):
            self.normalizer.apply_message(message=bogus, states=self.states)

    def test_funding_missing_fundingTime_raises(self) -> None:
        bogus = {
            "arg": {"channel": "funding-rate", "instId": self.inst_id},
            "data": [{"instType": "SWAP", "instId": self.inst_id, "fundingRate": "0.0001"}],
        }
        with self.assertRaises(ValueError):
            self.normalizer.apply_message(message=bogus, states=self.states)


if __name__ == "__main__":
    unittest.main()
