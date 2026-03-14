from __future__ import annotations

import unittest

from aats.bus.memory_bus import InMemoryEventBus
from aats.events.envelopes import publish_model
from aats.schemas.market import MarketSnapshot
from aats.storage.event_store import InMemoryEventStore
from aats.schemas.common import utc_now


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
        self.assertEqual(len(event_store.all()), 1)


if __name__ == "__main__":
    unittest.main()

