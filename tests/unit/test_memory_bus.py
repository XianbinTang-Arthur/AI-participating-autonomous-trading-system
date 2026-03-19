from __future__ import annotations

import unittest

from aats.bootstrap.config import resilient_subscription_handler
from aats.bus.memory_bus import InMemoryEventBus
from aats.events.envelopes import build_envelope, publish_model
from aats.schemas.market import MarketSnapshot
from aats.storage.event_store import InMemoryEventStore
from aats.schemas.common import utc_now


class ExplodingEventStore:
    def append(self, envelope) -> None:
        raise RuntimeError("boom")

    def all(self) -> list:
        return []

    def count(self, *, topic=None, decision_id=None) -> int:
        return 0

    def get(self, event_id: str):
        return None

    def latest(self, topic: str, key: str | None = None):
        return None

    def by_topic(self, topic: str) -> list:
        return []

    def by_decision(self, decision_id: str) -> list:
        return []


class TestInMemoryEventBus(unittest.IsolatedAsyncioTestCase):
    async def test_publish_delivers_messages_and_records_envelopes(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store)
        received: list[dict] = []

        async def handler(message: dict) -> None:
            received.append(message)

        await bus.subscribe("market.snapshots", handler)
        snapshot = MarketSnapshot(
            symbol="BTC-USDT",
            exchange="PAPER",
            snapshot_ts=utc_now(),
            best_bid=1.0,
            best_ask=1.1,
            last_price=1.05,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10.0,
            kline_15m={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
            kline_1h={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
        )

        await publish_model(
            bus=bus,
            topic="market.snapshots",
            key="BTC-USDT",
            payload_model=snapshot,
            source_component="test",
        )

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["topic"], "market.snapshots")
        self.assertEqual(event_store.count(), 1)

    async def test_strict_persistence_mode_raises_on_store_failure(self) -> None:
        bus = InMemoryEventBus(event_store=ExplodingEventStore(), persistence_mode="strict")
        snapshot = MarketSnapshot(
            symbol="BTC-USDT",
            exchange="PAPER",
            snapshot_ts=utc_now(),
            best_bid=1.0,
            best_ask=1.1,
            last_price=1.05,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10.0,
            kline_15m={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
            kline_1h={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
        )

        with self.assertLogs("aats.event_bus", level="ERROR") as captured:
            with self.assertRaises(RuntimeError):
                await publish_model(
                    bus=bus,
                    topic="market.snapshots",
                    key="BTC-USDT",
                    payload_model=snapshot,
                    source_component="test",
                )
        self.assertTrue(any("event_persistence_failed" in line for line in captured.output))

    async def test_permissive_persistence_mode_logs_and_continues(self) -> None:
        bus = InMemoryEventBus(event_store=ExplodingEventStore(), persistence_mode="permissive")
        received: list[dict] = []

        async def handler(message: dict) -> None:
            received.append(message)

        await bus.subscribe("market.snapshots", handler)
        snapshot = MarketSnapshot(
            symbol="BTC-USDT",
            exchange="PAPER",
            snapshot_ts=utc_now(),
            best_bid=1.0,
            best_ask=1.1,
            last_price=1.05,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10.0,
            kline_15m={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
            kline_1h={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
        )

        with self.assertLogs("aats.event_bus", level="ERROR") as captured:
            await publish_model(
                bus=bus,
                topic="market.snapshots",
                key="BTC-USDT",
                payload_model=snapshot,
                source_component="test",
            )
        self.assertEqual(len(received), 1)
        self.assertTrue(any("event_persistence_failed" in line for line in captured.output))

    async def test_publish_envelope_can_skip_event_store_persistence(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store)
        received: list[dict] = []

        async def handler(message: dict) -> None:
            received.append(message)

        await bus.subscribe("market.snapshots", handler)
        snapshot = MarketSnapshot(
            symbol="BTC-USDT",
            exchange="PAPER",
            snapshot_ts=utc_now(),
            best_bid=1.0,
            best_ask=1.1,
            last_price=1.05,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10.0,
            kline_15m={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
            kline_1h={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
        )

        await bus.publish_envelope(
            build_envelope(
                topic="market.snapshots",
                key="BTC-USDT",
                payload_model=snapshot,
                source_component="test",
            ),
            persist=False,
        )

        self.assertEqual(len(received), 1)
        self.assertEqual(event_store.count(), 0)

    async def test_resilient_subscription_handler_logs_and_does_not_abort_publish(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store)
        received: list[str] = []

        async def critical_handler(message: dict) -> None:
            _ = message
            received.append("critical")

        async def noncritical_handler(message: dict) -> None:
            _ = message
            raise RuntimeError("boom")

        await bus.subscribe("market.snapshots", critical_handler)
        await bus.subscribe(
            "market.snapshots",
            resilient_subscription_handler(
                topic="market.snapshots",
                name="test.noncritical_handler",
                handler=noncritical_handler,
            ),
        )
        snapshot = MarketSnapshot(
            symbol="BTC-USDT",
            exchange="PAPER",
            snapshot_ts=utc_now(),
            best_bid=1.0,
            best_ask=1.1,
            last_price=1.05,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10.0,
            kline_15m={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
            kline_1h={"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05},
        )

        with self.assertLogs("aats.event_bus", level="ERROR") as captured:
            await publish_model(
                bus=bus,
                topic="market.snapshots",
                key="BTC-USDT",
                payload_model=snapshot,
                source_component="test",
            )

        self.assertEqual(received, ["critical"])
        self.assertTrue(any("noncritical_subscription_failed" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
