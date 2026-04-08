from __future__ import annotations

from aats.bootstrap.telemetry import start_span
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.market import MarketSnapshot


class MarketSnapshotPublisher:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    async def publish(self, snapshot: MarketSnapshot) -> None:
        # Stage 8：market gateway 是 4 进程里上游最外层的 producer。这里开一个
        # 新 trace root，Jaeger 里的每条链路都从 "market_gateway.publish_snapshot
        # → nats.publish.market_snapshots" 开始，下游 decision / execution 再通过
        # envelope.trace_context 延伸下去。
        # 设计文档：docs/task/stage_8_otel_integration_design.md §D5
        with start_span(
            "market_gateway.publish_snapshot",
            attributes={
                "aats.symbol": snapshot.symbol,
                "aats.timeframe": getattr(snapshot, "timeframe", ""),
            },
        ):
            await publish_model(
                bus=self.bus,
                topic=topics.MARKET_SNAPSHOTS,
                key=snapshot.symbol,
                payload_model=snapshot,
                source_component="market_gateway",
            )

