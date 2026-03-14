from __future__ import annotations

from aats.bus.base import EventBus, MessageHandler


class KafkaEventBus(EventBus):
    async def publish(self, topic: str, key: str, payload: dict) -> None:
        raise NotImplementedError("Kafka event bus is not implemented in the MVP")

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        raise NotImplementedError("Kafka event bus is not implemented in the MVP")

