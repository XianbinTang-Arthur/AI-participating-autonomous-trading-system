from __future__ import annotations

import unittest

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.feature_engine.calculator import FeatureCalculator, FeatureEngine
from aats.storage.event_store import InMemoryEventStore


class TestFeatureEngine(unittest.TestCase):
    def test_feature_calculation_is_deterministic_for_same_snapshot(self) -> None:
        snapshot = self._snapshot()
        calculator = FeatureCalculator()

        first = calculator.calculate(snapshot, market_snapshot_ref="evt_market_1")
        second = calculator.calculate(snapshot, market_snapshot_ref="evt_market_1")

        self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))

    def test_feature_snapshot_contains_structured_analysis_context(self) -> None:
        snapshot = self._snapshot()
        features = FeatureCalculator().calculate(snapshot, market_snapshot_ref="evt_market_1")

        self.assertEqual(features.market_snapshot_ref, "evt_market_1")
        self.assertEqual(features.feature_version, "0.2.0")
        self.assertIsNotNone(features.analysis_context)
        analysis = features.analysis_context
        assert analysis is not None
        self.assertEqual(analysis.symbol, snapshot.symbol)
        self.assertEqual(analysis.analysis_version, "0.2.0")
        self.assertEqual(analysis.regime_version, "0.2.0")
        self.assertEqual(set(analysis.timeframe_features), {"15m", "1h"})
        self.assertEqual(analysis.timeframe_features["15m"].close_price, snapshot.kline_15m["close"])
        self.assertEqual(analysis.timeframe_features["1h"].close_price, snapshot.kline_1h["close"])
        self.assertEqual(features.liquidity_score, analysis.liquidity.liquidity_score)
        self.assertEqual(features.regime_indicator, analysis.regime_indicator)

    def test_feature_engine_publishes_audit_friendly_market_snapshot_ref(self) -> None:
        snapshot = self._snapshot()
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        engine = FeatureEngine(bus=bus, calculator=FeatureCalculator())
        market_envelope = build_envelope(
            topic=topics.MARKET_SNAPSHOTS,
            key=snapshot.symbol,
            payload_model=snapshot,
            source_component="test",
        )

        import asyncio

        asyncio.run(
            engine.handle_market_snapshot(
                {"payload": market_envelope.model_dump(mode="json")}
            )
        )

        feature_event = event_store.latest(topics.FEATURE_SNAPSHOTS, key=snapshot.symbol)
        self.assertIsNotNone(feature_event)
        if feature_event is not None:
            self.assertEqual(feature_event.payload["market_snapshot_ref"], market_envelope.event_id)

    @staticmethod
    def _snapshot() -> MarketSnapshot:
        now = utc_now()
        return MarketSnapshot(
            created_at=now,
            symbol="BTC-USDT",
            exchange="OKX",
            snapshot_ts=now,
            best_bid=67_000.0,
            best_ask=67_001.0,
            last_price=67_000.5,
            bid_size=3.0,
            ask_size=2.0,
            volume_24h=1000.0,
            kline_15m={"open": 66_800.0, "high": 67_200.0, "low": 66_700.0, "close": 67_100.0},
            kline_1h={"open": 66_000.0, "high": 67_300.0, "low": 65_900.0, "close": 67_100.0},
            orderbook_depth={
                "bids": [{"price": 67_000.0, "size": 5.0}, {"price": 66_999.0, "size": 6.0}],
                "asks": [{"price": 67_001.0, "size": 4.0}, {"price": 67_002.0, "size": 4.5}],
            },
        )


if __name__ == "__main__":
    unittest.main()
