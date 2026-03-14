from __future__ import annotations

from collections import defaultdict

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus, MessageHandler
from aats.schemas.common import EventEnvelope
from aats.storage.base import EventStore


class InMemoryEventBus(EventBus):
    def __init__(
        self,
        event_store: EventStore | None = None,
        *,
        persistence_mode: str = "strict",
    ) -> None:
        self._subs: dict[str, list[MessageHandler]] = defaultdict(list)
        self._event_store = event_store
        self._persistence_mode = persistence_mode
        self.logger = get_logger("aats.event_bus")

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        if self._event_store is not None:
            try:
                self._event_store.append(EventEnvelope.model_validate(payload))
            except Exception as exc:
                log_event(
                    self.logger,
                    "event_persistence_failed",
                    level="error",
                    topic=topic,
                    key=key,
                    persistence_mode=self._persistence_mode,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if self._persistence_mode == "strict":
                    raise

        message = {"topic": topic, "key": key, "payload": payload}
        for handler in tuple(self._subs[topic]):
            await handler(message)

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self._subs[topic].append(handler)
