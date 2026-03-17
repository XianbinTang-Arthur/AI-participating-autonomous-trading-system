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
        self.assertGreaterEqual(analysis.alpha_factors.composite_alpha_score, -1.0)
        self.assertLessEqual(analysis.alpha_factors.composite_alpha_score, 1.0)
        self.assertGreaterEqual(analysis.alpha_factors.microstructure_alpha, -1.0)
        self.assertLessEqual(analysis.alpha_factors.microstructure_alpha, 1.0)
        self.assertGreaterEqual(analysis.liquidity.execution_quality_scale, 0.0)
        self.assertLessEqual(analysis.liquidity.execution_quality_scale, 1.0)
        self.assertGreaterEqual(analysis.position_sizing.suggested_position_scale, 0.0)
        self.assertLessEqual(analysis.position_sizing.suggested_position_scale, 1.0)
        self.assertEqual(features.composite_alpha_score, analysis.alpha_factors.composite_alpha_score)
        self.assertEqual(features.suggested_position_scale, analysis.position_sizing.suggested_position_scale)
        self.assertEqual(features.volatility_target_scale, analysis.position_sizing.volatility_target_scale)

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

    def test_high_volatility_reduces_suggested_position_scale(self) -> None:
        calculator = FeatureCalculator()
        low_vol = calculator.calculate(self._snapshot(), market_snapshot_ref="evt_market_1")
        high_vol = calculator.calculate(self._high_vol_snapshot(), market_snapshot_ref="evt_market_2")

        self.assertLess(high_vol.volatility_target_scale, low_vol.volatility_target_scale)
        self.assertLess(high_vol.suggested_position_scale, low_vol.suggested_position_scale)

    def test_buy_side_trade_flow_improves_microstructure_alpha(self) -> None:
        calculator = FeatureCalculator()
        neutral = calculator.calculate(self._snapshot(), market_snapshot_ref="evt_market_1")
        buy_flow = calculator.calculate(self._snapshot_with_buy_flow(), market_snapshot_ref="evt_market_2")

        self.assertGreater(
            buy_flow.analysis_context.alpha_factors.microstructure_alpha,  # type: ignore[union-attr]
            neutral.analysis_context.alpha_factors.microstructure_alpha,  # type: ignore[union-attr]
        )
        self.assertGreater(
            buy_flow.analysis_context.position_sizing.execution_quality_scale,  # type: ignore[union-attr]
            0.0,
        )

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
            recent_trades=[
                {"side": "buy", "size": 0.9},
                {"side": "sell", "size": 0.9},
            ],
        )

    @staticmethod
    def _high_vol_snapshot() -> MarketSnapshot:
        now = utc_now()
        return MarketSnapshot(
            created_at=now,
            symbol="BTC-USDT",
            exchange="OKX",
            snapshot_ts=now,
            best_bid=67_000.0,
            best_ask=67_015.0,
            last_price=67_007.5,
            bid_size=1.5,
            ask_size=1.2,
            volume_24h=800.0,
            kline_15m={"open": 66_000.0, "high": 68_500.0, "low": 65_500.0, "close": 67_800.0},
            kline_1h={"open": 64_000.0, "high": 69_000.0, "low": 63_500.0, "close": 67_800.0},
            orderbook_depth={
                "bids": [{"price": 67_000.0, "size": 1.0}, {"price": 66_995.0, "size": 1.1}],
                "asks": [{"price": 67_015.0, "size": 0.9}, {"price": 67_020.0, "size": 1.0}],
            },
            recent_trades=[
                {"side": "sell", "size": 1.2},
                {"side": "buy", "size": 0.4},
            ],
        )

    @staticmethod
    def _snapshot_with_buy_flow() -> MarketSnapshot:
        snapshot = TestFeatureEngine._snapshot()
        return snapshot.model_copy(
            update={
                "recent_trades": [
                    {"side": "buy", "size": 1.5},
                    {"side": "buy", "size": 1.1},
                    {"side": "sell", "size": 0.3},
                ],
                "bid_size": 4.0,
                "ask_size": 2.0,
            }
        )


if __name__ == "__main__":
    unittest.main()
