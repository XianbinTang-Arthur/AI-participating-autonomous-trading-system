from __future__ import annotations

from collections import defaultdict

from pydantic import ValidationError

from aats.bus.base import EventBus, MessageHandler
from aats.schemas.common import EventEnvelope
from aats.storage.base import EventStore


class InMemoryEventBus(EventBus):
    def __init__(self, event_store: EventStore | None = None) -> None:
        self._subs: dict[str, list[MessageHandler]] = defaultdict(list)
        self._event_store = event_store

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        if self._event_store is not None:
            try:
                self._event_store.append(EventEnvelope.model_validate(payload))
            except ValidationError:
                pass

        message = {"topic": topic, "key": key, "payload": payload}
        for handler in tuple(self._subs[topic]):
            await handler(message)

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self._subs[topic].append(handler)
