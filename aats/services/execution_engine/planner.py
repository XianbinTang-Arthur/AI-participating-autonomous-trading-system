from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.common import new_id
from aats.schemas.execution import OrderIntent


class ExecutionPlanner:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def build_intent(
        self,
        *,
        decision_id: str,
        symbol: str,
        delta_qty: float,
        urgency: str,
    ) -> OrderIntent | None:
        if abs(delta_qty) < 1e-12:
            return None

        side = "buy" if delta_qty > 0 else "sell"
        quantity = abs(delta_qty)
        intent_id = new_id("intent")
        return OrderIntent(
            intent_id=intent_id,
            decision_id=decision_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            execution_style="taker",
            order_type="market",
            urgency=urgency if urgency in {"low", "medium", "high"} else "medium",
            time_in_force="IOC",
            reduce_only=False,
            close_only=False,
            idempotency_key=intent_id,
        )

    async def publish_intent(self, *, bus: EventBus, intent: OrderIntent) -> None:
        await publish_model(
            bus=bus,
            topic=topics.ORDER_INTENTS,
            key=intent.symbol,
            payload_model=intent,
            source_component="execution_engine",
        )

