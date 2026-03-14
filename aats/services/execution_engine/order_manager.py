from __future__ import annotations

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.execution import OrderIntent
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.storage.base import ExecutionRepository


class OrderManager:
    def __init__(
        self,
        *,
        bus: EventBus,
        adapter: ExchangeAdapter,
        execution_repo: ExecutionRepository,
    ) -> None:
        self.bus = bus
        self.adapter = adapter
        self.execution_repo = execution_repo
        self.logger = get_logger("aats.execution_engine")

    async def handle_order_intent(self, message: dict) -> None:
        intent = parse_payload(message, OrderIntent)
        if self.execution_repo.has_intent(intent.intent_id):
            return

        log_event(
            self.logger,
            "order_intent_received",
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
        )

        order_state, fills = self.adapter.submit(intent)
        self.execution_repo.save_order_state(order_state)
        await publish_model(
            bus=self.bus,
            topic=topics.ORDER_UPDATES,
            key=intent.symbol,
            payload_model=order_state,
            source_component="execution_engine",
        )

        for fill in fills:
            if not self.execution_repo.save_fill(fill):
                continue
            log_event(
                self.logger,
                "fill_event_created",
                decision_id=fill.decision_id,
                fill_id=fill.fill_id,
                symbol=fill.symbol,
                fill_qty=fill.fill_qty,
                fill_price=fill.fill_price,
            )
            await publish_model(
                bus=self.bus,
                topic=topics.FILL_EVENTS,
                key=fill.symbol,
                payload_model=fill,
                source_component="execution_engine",
            )
