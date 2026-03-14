from __future__ import annotations

from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.market import MarketSnapshot


class MarketSnapshotPublisher:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    async def publish(self, snapshot: MarketSnapshot) -> None:
        await publish_model(
            bus=self.bus,
            topic=topics.MARKET_SNAPSHOTS,
            key=snapshot.symbol,
            payload_model=snapshot,
            source_component="market_gateway",
        )

