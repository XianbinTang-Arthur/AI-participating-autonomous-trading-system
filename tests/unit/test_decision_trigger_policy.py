from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
import unittest

from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.services.decision_engine.trigger import DecisionCycleTrigger
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy


def _feature(*, snapshot_ts, momentum: float, regime: str, symbol: str = "BTC-USDT") -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        snapshot_ts=snapshot_ts,
        trend_strength=0.6,
        volatility_state="medium",
        volatility_value=1.0,
        momentum_score=momentum,
        liquidity_score=0.9,
        regime_indicator=regime,  # type: ignore[arg-type]
        feature_version="test",
    )


def _market(*, snapshot_ts, last_price: Decimal | float, symbol: str = "BTC-USDT") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        exchange="OKX",
        snapshot_ts=snapshot_ts,
        best_bid=last_price - 1.0,
        best_ask=last_price + 1.0,
        last_price=last_price,
        bid_size=1.0,
        ask_size=1.0,
        volume_24h=1000.0,
        kline_15m={"open": last_price, "high": last_price, "low": last_price, "close": last_price},
        kline_1h={"open": last_price, "high": last_price, "low": last_price, "close": last_price},
    )


class TestDecisionTriggerPolicy(unittest.TestCase):
    def test_duplicate_suppression_and_material_change_gating(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "decision_min_interval_seconds_15m": 60.0,
                "decision_min_price_move_bps": 5.0,
                "decision_min_momentum_delta": 0.2,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)

        allowed, reason = policy.should_trigger(
            feature_snapshot=feature,
            market_snapshot=market,
            timeframe="15m",
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "initial_decision")
        policy.record_trigger(feature_snapshot=feature, market_snapshot=market, timeframe="15m")

        duplicate_allowed, duplicate_reason = policy.should_trigger(
            feature_snapshot=feature,
            market_snapshot=market,
            timeframe="15m",
        )
        self.assertFalse(duplicate_allowed)
        self.assertEqual(duplicate_reason, "duplicate_market_snapshot")

        next_ts = base_ts + timedelta(seconds=10)
        small_move_market = _market(snapshot_ts=next_ts, last_price=67_001.0)
        small_move_feature = _feature(snapshot_ts=next_ts, momentum=0.11, regime="trend")
        suppressed, suppressed_reason = policy.should_trigger(
            feature_snapshot=small_move_feature,
            market_snapshot=small_move_market,
            timeframe="15m",
        )
        self.assertFalse(suppressed)
        self.assertEqual(suppressed_reason, "suppressed_duplicate")

        big_move_market = _market(snapshot_ts=next_ts, last_price=67_100.0)
        material, material_reason = policy.should_trigger(
            feature_snapshot=small_move_feature.model_copy(update={"momentum_score": 0.45}),
            market_snapshot=big_move_market,
            timeframe="15m",
        )
        self.assertTrue(material)
        self.assertEqual(material_reason, "material_change")

    def test_frequency_cap_is_enforced_in_trigger_policy(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "max_decisions_per_minute": 2,
                "decision_min_interval_seconds_15m": 0.0,
                "decision_min_price_move_bps": 0.0,
                "decision_min_momentum_delta": 0.0,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()

        first_feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")
        first_market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        self.assertEqual(
            policy.should_trigger(feature_snapshot=first_feature, market_snapshot=first_market, timeframe="15m"),
            (True, "initial_decision"),
        )
        policy.record_trigger(feature_snapshot=first_feature, market_snapshot=first_market, timeframe="15m")

        second_ts = base_ts + timedelta(seconds=10)
        second_feature = _feature(snapshot_ts=second_ts, momentum=0.2, regime="trend")
        second_market = _market(snapshot_ts=second_ts, last_price=67_010.0)
        second_allowed, second_reason = policy.should_trigger(
            feature_snapshot=second_feature,
            market_snapshot=second_market,
            timeframe="15m",
        )
        self.assertTrue(second_allowed)
        self.assertIn(second_reason, {"cadence_elapsed", "material_change"})
        policy.record_trigger(feature_snapshot=second_feature, market_snapshot=second_market, timeframe="15m")

        third_ts = base_ts + timedelta(seconds=20)
        third_feature = _feature(snapshot_ts=third_ts, momentum=0.3, regime="trend")
        third_market = _market(snapshot_ts=third_ts, last_price=67_020.0)
        third_allowed, third_reason = policy.should_trigger(
            feature_snapshot=third_feature,
            market_snapshot=third_market,
            timeframe="15m",
        )
        self.assertFalse(third_allowed)
        self.assertEqual(third_reason, "max_decision_frequency_reached")

    def test_decision_cycle_symbols_exclude_smart_arbitrage_companion_symbols(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                ),
            }
        )

        self.assertEqual(settings.decision_cycle_symbols(), ("BTC-USDT-SWAP",))
        self.assertEqual(settings.expanded_allowed_symbols(), ("BTC-USDT-SWAP", "BTC-USDT"))
        self.assertFalse(settings.symbol_allowed_for_decision_cycle("BTC-USDT"))
        self.assertTrue(settings.symbol_allowed_for_decision_cycle("BTC-USDT-SWAP"))


class TestDecisionCycleTrigger(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_same_snapshot_only_runs_one_cycle(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "enabled_decision_timeframes": ["15m"],
                "decision_min_interval_seconds_15m": 60.0,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")

        class _FakeOrchestrator:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run_cycle(self, *, symbol: str, timeframe: str):
                self.calls.append((symbol, timeframe))
                await asyncio.sleep(0)

        class _FakeMarketGateway:
            def latest_snapshot(self, symbol: str):
                return market if symbol == "BTC-USDT" else None

        orchestrator = _FakeOrchestrator()
        trigger = DecisionCycleTrigger(
            orchestrator=orchestrator,
            market_gateway=_FakeMarketGateway(),
            policy=policy,
        )
        envelope = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature.symbol,
            payload_model=feature,
            source_component="test",
        )
        message = {"topic": topics.FEATURE_SNAPSHOTS, "key": feature.symbol, "payload": envelope.model_dump(mode="json")}

        await asyncio.gather(
            trigger.handle_feature_snapshot(message),
            trigger.handle_feature_snapshot(message),
            trigger.handle_feature_snapshot(message),
        )

        self.assertEqual(orchestrator.calls, [("BTC-USDT", "15m")])

    async def test_halted_gate_skips_decision_cycle(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "enabled_decision_timeframes": ["15m"],
                "decision_min_interval_seconds_15m": 0.0,
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        market = _market(snapshot_ts=base_ts, last_price=67_000.0)
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend")

        class _FakeOrchestrator:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run_cycle(self, *, symbol: str, timeframe: str):
                self.calls.append((symbol, timeframe))

        class _FakeMarketGateway:
            def latest_snapshot(self, symbol: str):
                return market if symbol == "BTC-USDT" else None

        orchestrator = _FakeOrchestrator()
        trigger = DecisionCycleTrigger(
            orchestrator=orchestrator,
            market_gateway=_FakeMarketGateway(),
            policy=policy,
            can_trigger=lambda *, symbol: (False, "kill_switch_active"),
        )
        envelope = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature.symbol,
            payload_model=feature,
            source_component="test",
        )
        message = {"topic": topics.FEATURE_SNAPSHOTS, "key": feature.symbol, "payload": envelope.model_dump(mode="json")}

        await trigger.handle_feature_snapshot(message)

        self.assertEqual(orchestrator.calls, [])

    async def test_companion_spot_symbol_is_filtered_out_of_derivatives_decision_cycle(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "enabled_decision_timeframes": ["15m"],
                "decision_min_interval_seconds_15m": 0.0,
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                ),
            }
        )
        policy = DecisionTriggerPolicy(settings=settings)
        base_ts = utc_now()
        market = _market(snapshot_ts=base_ts, last_price=67_000.0, symbol="BTC-USDT")
        feature = _feature(snapshot_ts=base_ts, momentum=0.1, regime="trend", symbol="BTC-USDT")

        class _FakeOrchestrator:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run_cycle(self, *, symbol: str, timeframe: str):
                self.calls.append((symbol, timeframe))

        class _FakeMarketGateway:
            def latest_snapshot(self, symbol: str):
                return market if symbol == "BTC-USDT" else None

        orchestrator = _FakeOrchestrator()
        trigger = DecisionCycleTrigger(
            orchestrator=orchestrator,
            market_gateway=_FakeMarketGateway(),
            policy=policy,
            can_trigger=lambda *, symbol: (
                True,
                "ready",
            )
            if settings.symbol_allowed_for_decision_cycle(symbol)
            else (
                False,
                "symbol_not_enabled_for_decision_cycle",
            ),
        )
        envelope = build_envelope(
            topic=topics.FEATURE_SNAPSHOTS,
            key=feature.symbol,
            payload_model=feature,
            source_component="test",
        )
        message = {"topic": topics.FEATURE_SNAPSHOTS, "key": feature.symbol, "payload": envelope.model_dump(mode="json")}

        await trigger.handle_feature_snapshot(message)

        self.assertEqual(orchestrator.calls, [])
if __name__ == "__main__":
    unittest.main()
